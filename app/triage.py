"""
triage.py -- put the cases that most need a human at the top of the list.

PURPOSE   One number per case (the risk score) and the one-line summary the cases page shows.
          The score is a weighted count: things the institution must fix weigh most.
NOTE      The weights are a judgement call, not a rule from the Handbook. They only decide the ORDER
          of the list; they never change a status.
"""
from __future__ import annotations

WEIGHTS = {
    "Contradicted": 10,
    "Expired": 6,
    "Missing": 6,
    "Source unavailable": 2,
    "Blurry scan": 2,
    "Insufficient evidence": 1,
    "Verified": 0,
}


def risk_score(counts: dict[str, int], missing_documents: int = 0) -> int:
    total = sum(WEIGHTS.get(status, 0) * n for status, n in counts.items())
    return total + WEIGHTS["Missing"] * missing_documents


def summarise(report: dict, review: dict, institution: str, created_at: str) -> dict:
    counts = report["counts"]
    missing = len(report["missing_documents"])
    return {
        "case_id": report["case_id"],
        "institution": institution,
        "created_at": created_at,
        "date": report["date"],
        "documents": len(report["documents"]),
        "counts": counts,
        "missing": missing,
        "attention": counts.get("Contradicted", 0) + counts.get("Expired", 0) + missing,
        "manual": sum(counts.get(s, 0) for s in ("Source unavailable", "Insufficient evidence", "Blurry scan")),
        "verified": counts.get("Verified", 0),
        "risk": risk_score(counts, missing),
        "review_state": review["case"],
        "open_clarifications": review["open_clarifications"],
    }


def sort_worst_first(rows: list[dict]) -> list[dict]:
    """Highest risk first; between equal scores the newest case comes first."""
    return sorted(rows, key=lambda r: (r["risk"], r["created_at"]), reverse=True)
