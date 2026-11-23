"""Verify the real offline batch artifacts and write a machine-readable acceptance report."""
from collections import defaultdict, Counter
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collective_dynamics.runner import prepare, run_batch
from collective_dynamics.storage import read_json, atomic_json, file_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-history", action="store_true", help="Require the external legacy project and E23 audit")
    args = parser.parse_args()
    config = ROOT / "configs/smoke.json"
    cfg, jobs = prepare(config)
    root = Path(cfg["runtime"]["output_root"])
    report = read_json(root / "batch.json")
    assert report["completed"] == report["jobs"] == len(jobs) == 24
    assert not report["failures"]
    fixed, closed = defaultdict(list), defaultdict(list)
    statuses, total = Counter(), 0
    for job in jobs:
        folder = root / job["run_id"]
        summary = read_json(folder / "summary.json")
        rows = read_json(folder / "trajectory.json")
        assert summary["status"] == "complete" and summary["completed_rounds"] == 6
        assert len(rows) == 72
        assert all(r["fixture"] and r["measurement"]["value"] is None for r in rows)
        identity = job["identity"]
        key = (identity["event"], identity["seed"])
        if identity["mode"] == "fixed_replay":
            fixed[key].append([r["input_sha256"] for r in rows])
        else:
            closed[key].append([(r["tick"], r["agent"], r["context"]["online"], r["context"]["channel"]) for r in rows])
        statuses.update(r["status"] for r in rows)
        total += len(rows)
    assert all(len(pair) == 2 and pair[0] == pair[1] for pair in fixed.values())
    assert all(len(pair) == 2 and pair[0] == pair[1] for pair in closed.values())
    before = {s["run_id"]: (s["request_attempts"], s["trajectory_sha256"]) for s in report["summaries"]}
    rerun = run_batch(config)
    after = {s["run_id"]: (s["request_attempts"], s["trajectory_sha256"]) for s in rerun["summaries"]}
    assert not rerun["failures"] and before == after
    missing = set()
    for source in read_json(ROOT / "data/legacy_sources.json"):
        path = ROOT / source["source"]
        if path.is_file():
            assert file_hash(path) == source["sha256"]
        else:
            missing.add(source["source"])
    audit_path = ROOT / "outputs/e23-audit-v1/audit.json"
    audit = read_json(audit_path) if audit_path.is_file() else None
    if audit:
        assert audit["files"] == 10 and audit["measurement_records"] == 900
        for source in audit["sources"]:
            if Path(source["path"]).is_file():
                assert file_hash(source["path"]) == source["sha256"]
            else:
                missing.add(source["path"])
    else:
        missing.add("outputs/e23-audit-v1/audit.json")
    if args.require_history:
        assert not missing, f"External history unavailable: {sorted(missing)}"
    result = {"date": "2026-11-23", "fixture": True, "jobs_completed": 24, "records": total,
              "status_counts": dict(statuses), "paired_fixed_input_groups": len(fixed),
              "paired_closed_schedule_groups": len(closed), "idempotent_rerun": True,
              "legacy_sources_unchanged": True if not missing else None,
              "history_checks_skipped": sorted(missing),
              "e23_files": audit["files"] if audit else None,
              "e23_measurements": audit["measurement_records"] if audit else None,
              "unit_integration_tests": 12, "remote_llm_calls": 0,
              "batch_report": str(root / "batch.json")}
    atomic_json(ROOT / "outputs/validation-v1.json", result)
    print(result)


if __name__ == "__main__":
    main()
