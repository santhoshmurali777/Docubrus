"""
run.py -- score the pipeline on degraded scans.   python -m eval.run [--full] [--scenarios a,b] [--perturb x,y] [--severities 1,3,5]

For every (scenario, perturbation, severity) the scenario's PDFs are degraded (eval/perturb.py), pushed through
analyze(), and every finding is compared with data/synthetic/manifest.json.

Metrics (per perturbation x severity, pooled over scenarios):
  field accuracy     fraction of fields whose value equals the one read from the clean text PDF
  false-Verified     findings Verified although the manifest says otherwise            (target 0)
  false-Contradicted findings Contradicted although the manifest says otherwise        (target ~0; should fall to Blurry scan)
  status match       findings whose status equals the manifest
  lost files         uploads the pipeline could not classify or read
Real documents in eval/real/<case>/*.pdf are run as-is and reported separately (see eval/real/README.md).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from app import db                                    # noqa: E402
from app.documents import ocr                         # noqa: E402
from app.orchestrator import Upload, analyze          # noqa: E402
from eval.perturb import NAMES, degrade               # noqa: E402

SYN = os.path.join(ROOT, "data", "synthetic")
OUT = os.path.join(os.path.dirname(__file__), "results")
TODAY = date(2026, 10, 26)
MANIFEST = {m["id"]: m for m in json.load(open(os.path.join(SYN, "manifest.json"), encoding="utf-8"))}
DEFAULT_SCENARIOS = ("clean", "forged_qr", "intake_mismatch")


def _norm(v) -> str:
    return " ".join(str(v).lower().split())


def _analyze(ups):
    conn = db.connect(); db.init_db(conn)
    return analyze(conn, ups, today=TODAY)


def _files(sc):
    folder = os.path.join(SYN, sc)
    return [(f, open(os.path.join(folder, f), "rb").read()) for f in sorted(os.listdir(folder)) if f.endswith(".pdf")]


def truth_values(sc) -> dict:
    """(doc_type.field) -> value, read from the clean text PDFs."""
    rep = _analyze([Upload(f, d) for f, d in _files(sc)])
    return {f"{x['doc_type']}.{x['field_key']}": _norm(x["value"]) for x in rep["findings"]}


def one_case(job) -> dict:
    sc, name, sev, truth = job
    t0 = time.time()
    rep = _analyze([Upload(f, degrade(d, name, sev)) for f, d in _files(sc)])
    exp = MANIFEST[sc]["expected"]
    got = {f"{x['doc_type']}.{x['field_key']}": x for x in rep["findings"]}
    fv = fc = match = 0
    for k, x in got.items():
        e = exp.get(k)
        match += x["status"] == e
        fv += x["status"] == "Verified" and e != "Verified"
        fc += x["status"] == "Contradicted" and e != "Contradicted"
    ok = sum(1 for k, v in truth.items() if k in got and _norm(got[k]["value"]) == v)
    return {"scenario": sc, "perturbation": name, "severity": sev, "fields": len(truth), "fields_ok": ok,
            "findings": len(got), "status_match": match, "false_verified": fv, "false_contradicted": fc,
            "blurry": sum(x["status"] == "Blurry scan" for x in got.values()),
            "lost_files": len(rep["unrecognised_files"]), "seconds": round(time.time() - t0, 1)}


def aggregate(rows) -> list:
    out: dict = {}
    for r in rows:
        a = out.setdefault((r["perturbation"], r["severity"]), dict.fromkeys(
            ("runs", "fields", "fields_ok", "findings", "status_match", "false_verified", "false_contradicted", "blurry", "lost_files"), 0))
        a["runs"] += 1
        for k in list(a)[1:]:
            a[k] += r[k]
    return [{"perturbation": p, "severity": s, **a} for (p, s), a in sorted(out.items())]


def _pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def table(agg) -> str:
    L = ["| Perturbation | Sev | Runs | Field accuracy | Status match | Blurry scan | False-Verified | False-Contradicted | Lost files |",
         "|---|---|---|---|---|---|---|---|---|"]
    for a in agg:
        L.append(f"| {a['perturbation']} | {a['severity']} | {a['runs']} | {_pct(a['fields_ok'], a['fields'])} | "
                 f"{_pct(a['status_match'], a['findings'])} | {_pct(a['blurry'], a['findings'])} | "
                 f"{a['false_verified']} | {a['false_contradicted']} | {a['lost_files']} |")
    tot = {k: sum(a[k] for a in agg) for k in ("fields", "fields_ok", "findings", "false_verified", "false_contradicted")}
    L.append(f"| **all** | | {sum(a['runs'] for a in agg)} | {_pct(tot['fields_ok'], tot['fields'])} | | | "
             f"**{tot['false_verified']}** | **{tot['false_contradicted']}** | |")
    return "\n".join(L)


def real_report() -> str:
    base = os.path.join(os.path.dirname(__file__), "real")
    cases = [d for d in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, d))] if os.path.isdir(base) else []
    if not cases:
        return "_No real documents yet: put PDFs in `eval/real/<case>/` (see eval/real/README.md)._"
    L = ["| Case | Files | Findings | Verified | Blurry | Contradicted | Lost files | Wrong vs expected.json |", "|---|---|---|---|---|---|---|---|"]
    for c in cases:
        folder = os.path.join(base, c)
        pdfs = [f for f in sorted(os.listdir(folder)) if f.lower().endswith(".pdf")]
        rep = _analyze([Upload(f, open(os.path.join(folder, f), "rb").read()) for f in pdfs])
        n = rep["counts"]
        ep = os.path.join(folder, "expected.json")
        wrong = "no expected.json"
        if os.path.exists(ep):
            exp = json.load(open(ep, encoding="utf-8"))
            wrong = str(sum(1 for x in rep["findings"] if f"{x['doc_type']}.{x['field_key']}" in exp
                            and exp[f"{x['doc_type']}.{x['field_key']}"] != x["status"]))
        L.append(f"| {c} | {len(pdfs)} | {len(rep['findings'])} | {n.get('Verified', 0)} | {n.get('Blurry scan', 0)} | "
                 f"{n.get('Contradicted', 0)} | {len(rep['unrecognised_files'])} | {wrong} |")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="all scenarios x all severities (about an hour)")
    ap.add_argument("--scenarios"); ap.add_argument("--perturb"); ap.add_argument("--severities")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--real-only", action="store_true")
    a = ap.parse_args(argv)
    if not ocr.available():
        print("tesseract is not installed; the eval needs it"); return 2
    if a.real_only:
        print(real_report()); return 0
    scs = a.scenarios.split(",") if a.scenarios else (list(MANIFEST) if a.full else list(DEFAULT_SCENARIOS))
    names = a.perturb.split(",") if a.perturb else NAMES
    sevs = [int(s) for s in a.severities.split(",")] if a.severities else ([1, 2, 3, 4, 5] if a.full else [1, 3, 5])
    truth = {sc: truth_values(sc) for sc in scs}
    jobs = [(sc, n, s, truth[sc]) for n in names for s in sevs for sc in scs]
    print(f"{len(jobs)} runs ({len(scs)} scenarios x {len(names)} perturbations x {len(sevs)} severities)", file=sys.stderr)
    rows = []
    with ProcessPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(one_case, jobs), 1):
            rows.append(r)
            print(f"[{i}/{len(jobs)}] {r['scenario']} {r['perturbation']}@{r['severity']}: FV={r['false_verified']} FC={r['false_contradicted']} ({r['seconds']}s)", file=sys.stderr)
    agg = aggregate(rows)
    md = f"## Synthetic scans, degraded\n\n{table(agg)}\n\n## Real documents\n\n{real_report()}\n"
    os.makedirs(OUT, exist_ok=True)
    json.dump({"rows": rows, "aggregate": agg}, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    open(os.path.join(OUT, "results.md"), "w", encoding="utf-8").write(md)
    print(md)
    return 1 if any(r["false_verified"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
