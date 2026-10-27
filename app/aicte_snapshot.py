"""
aicte_snapshot.py -- the regulator record from an IMPORTED SNAPSHOT of AICTE's public approved-institutions data.

PURPOSE   Same lookup() interface as MockAicte, so the orchestrator treats both alike. The difference is honesty:
          every answer carries the snapshot date and where it came from, so the report and the audit trail
          say "AICTE data as of <date>", never "AICTE says".
FILES     A snapshot is one JSON file:
            {"snapshot_date": "2026-10-20", "origin": "<where it was downloaded from>",
             "institutions": {"<key>": {"approved_intake": "120", "legal_name": "...", "institution_type": "..."}}}
          Build one from a downloaded CSV:
            python -m app.aicte_snapshot import data.csv out.json --date 2026-10-20 --origin "<url or file>" \
                   --key-col "<column>" --name-col "<column>" --intake-col "<column>" --intake-mode sum|first|max
NOTE      --intake-mode has no default on purpose: the public data lists intake per programme, and whether the
          institution's "approved intake" is their sum or one row is a decision for you, not for this code.
          The lookup KEY must be the same kind of number the application documents print. If the portal keys
          institutions by an AICTE ID and your documents print a society registration number, the lookup will
          honestly return 'no record'.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, timedelta
from typing import Optional

from app.contracts import SourceResponse
from app.correlation.normalizers import normalize_identifier

SOURCE = "aicte_snapshot"
STALE_AFTER = timedelta(days=90)


class AicteSnapshot:
    def __init__(self, records: dict, snapshot_date: date, origin: str, outage: bool = False):
        self.records = {normalize_identifier(k): v for k, v in records.items()}
        self.snapshot_date, self.origin, self.outage = snapshot_date, origin, outage

    @classmethod
    def load(cls, path: str, outage: bool = False) -> "AicteSnapshot":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for k in ("snapshot_date", "origin", "institutions"):
            if k not in raw:
                raise ValueError(f"snapshot file is missing '{k}'")
        return cls(raw["institutions"], date.fromisoformat(raw["snapshot_date"]), str(raw["origin"]), outage)

    @property
    def label(self) -> str:
        return f"AICTE snapshot dated {self.snapshot_date.isoformat()}"

    def age_note(self, today: date) -> Optional[str]:
        age = today - self.snapshot_date
        return (f"the {self.label} is {age.days} days old; the regulator's record may have changed since"
                if age > STALE_AFTER else None)

    def lookup(self, registration_number: str) -> SourceResponse:
        key = normalize_identifier(registration_number)
        if self.outage:
            return SourceResponse(id=f"sr_aicte_{key}", source=SOURCE, ok=False, error="regulator snapshot unavailable (simulated outage)")
        rec = self.records.get(key)
        meta = {"snapshot_date": self.snapshot_date.isoformat(), "origin": self.origin}
        payload = {"found": True, **rec, **meta} if rec else {"found": False, "registration_number": registration_number, **meta}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return SourceResponse(id=f"sr_aicte_{key}", source=SOURCE, ok=True, payload=payload, digest=digest)


def import_csv(csv_path: str, out_path: str, *, snapshot_date: date, origin: str, key_col: str, name_col: str,
               intake_col: str, intake_mode: str, type_col: Optional[str] = None) -> int:
    if intake_mode not in ("sum", "first", "max"):
        raise ValueError("intake_mode must be 'sum', 'first' or 'max'")
    inst: dict[str, dict] = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        missing = [c for c in (key_col, name_col, intake_col) + ((type_col,) if type_col else ()) if c not in (rd.fieldnames or [])]
        if missing:
            raise ValueError(f"column(s) not in the CSV: {missing}; it has {rd.fieldnames}")
        for row in rd:
            key = (row[key_col] or "").strip()
            if not key:
                continue
            try:
                n = int(float(str(row[intake_col]).replace(",", "").strip() or 0))
            except ValueError:
                continue
            cur = inst.get(key)
            if cur is None:
                inst[key] = {"approved_intake": n, "legal_name": row[name_col].strip(),
                             **({"institution_type": row[type_col].strip()} if type_col else {})}
            elif intake_mode == "sum":
                cur["approved_intake"] += n
            elif intake_mode == "max":
                cur["approved_intake"] = max(cur["approved_intake"], n)
    for v in inst.values():
        v["approved_intake"] = str(v["approved_intake"])
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"snapshot_date": snapshot_date.isoformat(), "origin": origin, "intake_mode": intake_mode,
                   "institutions": inst}, fh, indent=1, ensure_ascii=False)
    return len(inst)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Import a downloaded AICTE CSV as a dated snapshot")
    ap.add_argument("cmd", choices=["import"]); ap.add_argument("csv"); ap.add_argument("out")
    ap.add_argument("--date", required=True); ap.add_argument("--origin", required=True)
    ap.add_argument("--key-col", required=True); ap.add_argument("--name-col", required=True)
    ap.add_argument("--intake-col", required=True); ap.add_argument("--intake-mode", required=True, choices=["sum", "first", "max"])
    ap.add_argument("--type-col")
    a = ap.parse_args()
    n = import_csv(a.csv, a.out, snapshot_date=date.fromisoformat(a.date), origin=a.origin, key_col=a.key_col,
                   name_col=a.name_col, intake_col=a.intake_col, intake_mode=a.intake_mode, type_col=a.type_col)
    print(f"wrote {n} institutions to {a.out}")
