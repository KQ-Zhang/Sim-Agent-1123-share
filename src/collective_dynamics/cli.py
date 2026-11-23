import argparse
import json
from .runner import prepare, run_batch
from .audit import audit_e23


def main():
    parser = argparse.ArgumentParser(description="Collective dynamics framework: offline by default")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", required=True)
        if name == "run":
            sub.add_argument("--allow-remote", action="store_true")
            sub.add_argument("--until", type=int, help="Commit only this many rounds; same config resumes later")
    audit = commands.add_parser("audit-e23")
    audit.add_argument("--source", required=True)
    audit.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "plan":
        cfg, jobs = prepare(args.config)
        result = {"jobs": len(jobs), "max_attempts_total": len(jobs)*cfg["runtime"]["max_requests"],
                  "runs": [{"run_id": j["run_id"], **j["identity"]} for j in jobs]}
    elif args.command == "run":
        result = run_batch(args.config, args.allow_remote, args.until)
    else:
        result = audit_e23(args.source, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("failures"):
        raise SystemExit(1)
