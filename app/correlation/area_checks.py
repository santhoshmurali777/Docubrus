"""
area_checks.py -- is the declared built-up area enough for the declared intake?

PURPOSE   Two checks, both driven by a rule (never hard-coded numbers):
            enough   declared built-up area >= intake x the rule's norm per student
            in_plan  declared built-up area <= area on the approved building plan
                     (within the rule's tolerance)
INPUTS    built-up and intake claims, the building-plan area claim, a Rule
          (rule.definition: {"min_built_up_sqm_per_intake_seat": float,
                             "plan_tolerance_rel": float})
OUTPUTS   list[AreaIssue]; area_conflicts() gives status.Conflict items
IMPORTS   contracts, correlation.normalizers, status
TESTED BY tests/test_area.py
NOTE      The norm lives in config/rules.json and is ILLUSTRATIVE until checked
          against the real Handbook. This file contains no norm of its own. If the
          rule or a value is missing/unreadable the result is an 'unreadable' or
          'no_rule' issue, never a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.contracts import Claim, Rule
from app.correlation.normalizers import normalize_number
from app.status import Conflict


@dataclass(frozen=True)
class AreaIssue:
    kind: str                       # "too_small" | "exceeds_plan" | "unreadable" | "no_rule"
    description: str
    claim_ids: tuple[str, ...] = ()
    rule_version: Optional[str] = None


def check_area(built_up: Optional[Claim], intake: Optional[Claim], plan_area: Optional[Claim],
               rule: Optional[Rule]) -> list[AreaIssue]:
    if rule is None:
        return [AreaIssue("no_rule", "no area rule is available, so the area could not be checked")]
    ver = rule.version
    d = rule.definition
    issues: list[AreaIssue] = []
    if built_up is None:
        return [AreaIssue("unreadable", "the declared built-up area was not found", (), ver)]
    area = normalize_number(built_up.raw_value)
    if area is None:
        return [AreaIssue("unreadable", f"declared built-up area '{built_up.raw_value}' could not be read", (built_up.id,), ver)]

    norm = d.get("min_built_up_sqm_per_intake_seat")
    if norm is not None:
        seats = normalize_number(intake.raw_value) if intake else None
        if seats is None:
            issues.append(AreaIssue("unreadable", "the declared intake could not be read, so the area norm could not be applied",
                                    tuple(c.id for c in (built_up, intake) if c), ver))
        elif area < seats * norm:
            issues.append(AreaIssue("too_small",
                                    f"built-up area {area:g} sq m is below {seats * norm:g} sq m "
                                    f"({seats:g} seats x {norm:g} sq m per seat, rule {rule.id} v{ver})",
                                    (built_up.id, intake.id), ver))

    if plan_area is not None:
        planned = normalize_number(plan_area.raw_value)
        if planned is None:
            issues.append(AreaIssue("unreadable", f"building-plan area '{plan_area.raw_value}' could not be read",
                                    (plan_area.id,), ver))
        elif area > planned * (1 + d.get("plan_tolerance_rel", 0.0)) + 1e-9:
            issues.append(AreaIssue("exceeds_plan",
                                    f"declared built-up area {area:g} sq m is more than the {planned:g} sq m on the approved building plan",
                                    (built_up.id, plan_area.id), ver))
    return issues


def area_conflicts(issues: list[AreaIssue], claim_id: str) -> list[Conflict]:
    """Only real contradictions go to status.evaluate; 'unreadable' and 'no_rule' need a human."""
    return [Conflict(i.description) for i in issues if i.kind in ("too_small", "exceeds_plan") and claim_id in i.claim_ids]
