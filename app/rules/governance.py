"""
governance.py -- draft, dry-run, approve. The author can never approve.

PURPOSE   The only path from an idea to an approved rule:
            1 create_draft    author writes a rule (state draft)
            2 dry_run         run it on sample values; produces a report tied to the
                              exact definition (a digest), so an edited draft needs a new run
            3 approve         a DIFFERENT person approves, citing that report
INPUTS    connection, Rule fields, sample values
OUTPUTS   DryRunReport, approved Rule
IMPORTS   contracts, rules.rule_store, rules.rule_engine
TESTED BY tests/test_governance.py
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from app.contracts import Rule, RuleState
from app.rules import rule_engine, rule_store


class GovernanceError(Exception):
    pass


@dataclass(frozen=True)
class DryRunReport:
    rule_id: str
    version: str
    digest: str
    results: tuple          # (sample index, RuleResult)

    @property
    def failures(self) -> int:
        return sum(1 for _, r in self.results if r.passed is False)

    @property
    def unjudged(self) -> int:
        return sum(1 for _, r in self.results if r.passed is None)


def _digest(rule: Rule) -> str:
    body = json.dumps({"id": rule.id, "version": rule.version, "clause_ref": rule.clause_ref, "illustrative": rule.illustrative,
                       "effective_from": rule.effective_from.isoformat(), "definition": rule.definition}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def create_draft(conn, rule_id: str, version: str, clause_ref: str, effective_from: date, author: str,
                 definition: dict[str, Any], illustrative: bool = True) -> Rule:
    if not author:
        raise GovernanceError("a draft needs an author")
    return rule_store.save(conn, Rule(id=rule_id, version=version, clause_ref=clause_ref, illustrative=illustrative,
                                      effective_from=effective_from, state=RuleState.DRAFT, author=author, definition=definition))


def dry_run(conn, rule_id: str, version: str, samples: Sequence[Mapping[str, str]]) -> DryRunReport:
    rule = rule_store.get(conn, rule_id, version)
    if rule is None:
        raise GovernanceError(f"no such rule {rule_id} v{version}")
    if not samples:
        raise GovernanceError("a dry run needs at least one sample")
    return DryRunReport(rule_id, version, _digest(rule),
                        tuple((i, rule_engine.apply_rule(rule, s)) for i, s in enumerate(samples)))


def approve(conn, rule_id: str, version: str, approver: str, report: DryRunReport,
            illustrative: Optional[bool] = None) -> Rule:
    rule = rule_store.get(conn, rule_id, version)
    if rule is None:
        raise GovernanceError(f"no such rule {rule_id} v{version}")
    if rule.state == RuleState.APPROVED:
        raise GovernanceError(f"rule {rule_id} v{version} is already approved")
    if not approver or approver == rule.author:
        raise GovernanceError("the author cannot approve their own rule")
    if report.rule_id != rule_id or report.version != version or report.digest != _digest(rule):
        raise GovernanceError("the dry-run report does not match this draft; run it again on the current draft")
    approved = Rule(id=rule.id, version=rule.version, clause_ref=rule.clause_ref,
                    illustrative=rule.illustrative if illustrative is None else illustrative,
                    effective_from=rule.effective_from, state=RuleState.APPROVED, author=rule.author,
                    approver=approver, definition=rule.definition)
    rule_store.save(conn, approved)
    return approved
