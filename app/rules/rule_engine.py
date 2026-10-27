"""
rule_engine.py -- test values against the rule version in force on a date.

PURPOSE   Pick the right rule version for the date of the application, then apply it.
          Kinds: min_value, max_value, allowed_values. (area_per_seat is applied by
          correlation/area_checks.py, which receives the rule from here.)
INPUTS    connection, rule id, date, the values being tested
OUTPUTS   RuleResult (passed True / False / None, reason, rule version)
IMPORTS   contracts, correlation.normalizers, rules.rule_store
TESTED BY tests/test_rule_engine.py
NOTE      passed=None means "could not be judged" (no rule in force, or value unreadable).
          None must never be treated as a pass. An illustrative rule says so in its reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional

from app.contracts import Rule
from app.correlation.normalizers import normalize_number
from app.rules import rule_store


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    rule_version: Optional[str]
    passed: Optional[bool]
    reason: str
    illustrative: bool = True


def apply_rule(rule: Rule, values: Mapping[str, str]) -> RuleResult:
    d = rule.definition
    kind = d.get("kind")
    tag = " (illustrative rule, not yet checked against the Handbook)" if rule.illustrative else ""

    def res(ok, why):
        return RuleResult(rule.id, rule.version, ok, why + tag, rule.illustrative)

    if kind not in ("min_value", "max_value", "allowed_values"):
        return res(None, f"rule kind '{kind}' is not applied by the rule engine")
    field = d["field"]
    raw = values.get(field)
    n = normalize_number(raw) if raw is not None else None
    if n is None:
        return res(None, f"'{field}' is missing or unreadable, so rule {rule.id} v{rule.version} could not be applied")
    if kind == "min_value":
        ok = n >= d["min"]
        return res(ok, f"{field} {n:g} is {'at least' if ok else 'below'} the minimum {d['min']:g} (rule {rule.id} v{rule.version})")
    if kind == "max_value":
        ok = n <= d["max"]
        return res(ok, f"{field} {n:g} is {'within' if ok else 'above'} the maximum {d['max']:g} (rule {rule.id} v{rule.version})")
    ok = any(abs(n - v) < 1e-9 for v in d["values"])
    return res(ok, f"{field} {n:g} is {'one of' if ok else 'not one of'} the permitted values {d['values']} (rule {rule.id} v{rule.version})")


def evaluate(conn, rule_id: str, on: date, values: Mapping[str, str]) -> RuleResult:
    rule = rule_store.effective(conn, rule_id, on)
    if rule is None:
        return RuleResult(rule_id, None, None, f"no approved version of rule '{rule_id}' is in force on {on.isoformat()}")
    return apply_rule(rule, values)


def evaluate_all(conn, on: date, values: Mapping[str, str]) -> list[RuleResult]:
    """Every rule that has an approved version in force on that date and applies via the engine."""
    out = []
    for rid in rule_store.all_rule_ids(conn):
        rule = rule_store.effective(conn, rid, on)
        if rule and rule.definition.get("kind") in ("min_value", "max_value", "allowed_values"):
            out.append(apply_rule(rule, values))
    return out
