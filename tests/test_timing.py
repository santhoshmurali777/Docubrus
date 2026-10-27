"""
test_timing.py -- benchmark the processing engine on all synthetic scenarios.

Runs each scenario through analyze() and checks that:
  1. Every expected status matches.
  2. Processing completes well under the 28 s deadline.
  3. The verdict (status counts) is correct.

Run:  python -m pytest tests/test_timing.py -v -s
  or: python tests/test_timing.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date
from app import db
from app.orchestrator import Upload, analyze

ROOT = os.path.join(os.path.dirname(__file__), "..")
SYN = os.path.join(ROOT, "data", "synthetic")
TODAY = date(2026, 10, 26)


def load_manifest():
    with open(os.path.join(SYN, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def run_scenario(sc):
    folder = os.path.join(SYN, sc["id"])
    uploads = []
    for dt in sc["files"]:
        path = os.path.join(folder, f"{dt}.pdf")
        with open(path, "rb") as fh:
            uploads.append(Upload(f"{dt}.pdf", fh.read()))

    conn = db.connect()
    db.init_db(conn)
    t0 = time.perf_counter()
    report = analyze(conn, uploads, today=TODAY)
    elapsed = time.perf_counter() - t0
    conn.close()
    return report, elapsed


def test_all_scenarios_timing():
    """Run every scenario with timing and correctness checks."""
    manifest = load_manifest()
    rows = []
    all_ok = True

    print("\n" + "=" * 100)
    print(f"{'Scenario':<35} {'Files':>5} {'Findings':>8} {'Verified':>8} {'Contrad.':>8} {'Expired':>7} {'Missing':>7} {'Time':>8} {'Match':>6}")
    print("-" * 100)

    for sc in manifest:
        report, elapsed = run_scenario(sc)

        # check expected
        expected = sc["expected"]
        got = {f"{x['doc_type']}.{x['field_key']}": x["status"] for x in report["findings"]}
        mismatches = []
        for k, want in expected.items():
            actual = got.get(k, "(missing)")
            if actual != want:
                mismatches.append(f"  {k}: expected {want}, got {actual}")

        counts = report["counts"]
        n_verified = counts.get("Verified", 0)
        n_contrad = counts.get("Contradicted", 0)
        n_expired = counts.get("Expired", 0)
        n_missing_docs = len(report["missing_documents"])
        match_pct = sum(1 for k in expected if got.get(k) == expected[k]) / len(expected) * 100 if expected else 100

        ok = len(mismatches) == 0 and elapsed < 28.0
        status_icon = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False

        print(f"[{status_icon}] {sc['id']:<30} {len(sc['files']):>5} {len(report['findings']):>8} "
              f"{n_verified:>8} {n_contrad:>8} {n_expired:>7} {n_missing_docs:>7} {elapsed:>7.2f}s {match_pct:>5.1f}%")

        if mismatches:
            for m in mismatches[:5]:
                print(f"    {m}")
            if len(mismatches) > 5:
                print(f"    ... and {len(mismatches) - 5} more")

        rows.append({
            "scenario": sc["id"],
            "title": sc["title"],
            "files": len(sc["files"]),
            "findings": len(report["findings"]),
            "verified": n_verified,
            "contradicted": n_contrad,
            "expired": n_expired,
            "missing_docs": n_missing_docs,
            "elapsed": elapsed,
            "match_pct": match_pct,
            "ok": ok,
        })

    print("-" * 100)
    total_time = sum(r["elapsed"] for r in rows)
    avg_time = total_time / len(rows) if rows else 0
    total_findings = sum(r["findings"] for r in rows)
    print(f"  {'TOTAL':<33} {sum(r['files'] for r in rows):>5} {total_findings:>8} "
          f"{sum(r['verified'] for r in rows):>8} {sum(r['contradicted'] for r in rows):>8} "
          f"{sum(r['expired'] for r in rows):>7} {sum(r['missing_docs'] for r in rows):>7} {total_time:>7.2f}s")
    print(f"  {'AVERAGE':<33} {'':>5} {'':>8} {'':>8} {'':>8} {'':>7} {'':>7} {avg_time:>7.2f}s")
    print("=" * 100)

    # assertion: all scenarios match and run under deadline
    assert all_ok, f"Some scenarios failed correctness or timing checks"
    assert total_time < 28.0, f"Total processing time {total_time:.2f}s exceeds 28s deadline"
    print(f"\n[PASS] All {len(rows)} scenarios passed. Total: {total_time:.2f}s, Average: {avg_time:.2f}s per scenario.")


if __name__ == "__main__":
    test_all_scenarios_timing()
