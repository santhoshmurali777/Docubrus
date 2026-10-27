"""
rule_store.py -- load and save rule versions; several versions coexist.

PURPOSE   Keep every version of every rule. An APPROVED version is never overwritten
          or edited: a change is a new version with a later effective date.
          DRAFT versions may be replaced until approved.
INPUTS    sqlite connection, Rule objects, config/rules.json
OUTPUTS   stored rules; effective() picks the version in force on a date
IMPORTS   contracts, db
TESTED BY tests/test_rule_store.py
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from app import db
from app.contracts import Rule, RuleState


class RuleStoreError(Exception):
    pass


def _get(conn, rule_id: str, version: str) -> Optional[Rule]:
    return next((r for r in db.list_rule_versions(conn, rule_id) if r.version == version), None)


def get(conn, rule_id: str, version: str) -> Optional[Rule]:
    return _get(conn, rule_id, version)


def save(conn, rule: Rule) -> Rule:
    """Insert a new version, or replace an existing DRAFT. Approved versions are frozen."""
    existing = _get(conn, rule.id, rule.version)
    if existing is not None and existing.state == RuleState.APPROVED:
        raise RuleStoreError(f"rule {rule.id} v{rule.version} is approved and cannot be changed; create a new version")
    if existing is not None and rule.state == RuleState.DRAFT and existing.author != rule.author:
        raise RuleStoreError(f"draft {rule.id} v{rule.version} belongs to {existing.author}")
    db.save_rule(conn, rule)
    return rule


def versions(conn, rule_id: str) -> list[Rule]:
    return db.list_rule_versions(conn, rule_id)


def effective(conn, rule_id: str, on: date) -> Optional[Rule]:
    """The APPROVED version in force on `on`: latest effective_from not after that date."""
    ok = [r for r in db.list_rule_versions(conn, rule_id) if r.state == RuleState.APPROVED and r.effective_from <= on]
    return max(ok, key=lambda r: (r.effective_from, r.version)) if ok else None


def all_rule_ids(conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT DISTINCT id FROM rules ORDER BY id")]


def load_seed(conn, path: str) -> int:
    """Load rules from config/rules.json. Skips versions already stored. Returns how many were added."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    n = 0
    for r in data["rules"]:
        rule = Rule(id=r["id"], version=r["version"], clause_ref=r["clause_ref"], illustrative=r.get("illustrative", True),
                    effective_from=date.fromisoformat(r["effective_from"]), state=RuleState(r["state"]),
                    author=r["author"], approver=r.get("approver"), definition=r["definition"])
        if _get(conn, rule.id, rule.version) is None:
            save(conn, rule); n += 1
    return n
