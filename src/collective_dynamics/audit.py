"""Read-only E23 importer. Historical full-history scores retain their original scope."""
import json
from pathlib import Path
from collections import Counter
from .storage import file_hash, atomic_json
from .config import ROOT


def audit_e23(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not output.is_relative_to(ROOT / "outputs"):
        raise ValueError("Audit output must be inside the new project's outputs")
    if output.exists():
        raise ValueError("Audit output already exists; choose a new directory")
    files = sorted(source.glob("*.jsonl")) if source.is_dir() else [source]
    rows, sources, counts = [], [], Counter()
    for path in files:
        sha = file_hash(path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        header = json.loads(lines[0])
        if header.get("kind") != "scenario":
            raise ValueError(f"Not an E23 trajectory: {path.name}")
        sources.append({"path": str(path), "sha256": sha})
        for line_number, line in enumerate(lines[1:], 2):
            old = json.loads(line)
            if old.get("kind") != "turn":
                raise ValueError("Unexpected E23 record")
            post = old["post"]
            status = "baseline" if post is None else "silent" if not post["text"] else "published"
            for axis, rating in old["judge"].items():
                value = rating["score"]
                counts[f"{axis}:{value}"] += 1
                if value not in (-2, -1, 0, 1, 2, None):
                    raise ValueError("Unknown historical score")
                rows.append({"schema_version": 1, "fixture": False, "event": header["event"], "arm": header["arm"],
                             "seed": header["seed"], "agent": old["agent"], "tick": old["round"], "status": status,
                             "measurement": {"axis": axis, "value": value, "scope": "own_full_history",
                                             "time_range": [0, old["round"]], "status": "legacy_uncalibrated",
                                             "original": rating},
                             "source": {"path": str(path), "sha256": sha, "line": line_number}, "original": old})
        if file_hash(path) != sha:
            raise ValueError("Source changed during read-only import")
    if not files:
        raise ValueError("No E23 files found")
    output.mkdir(parents=True)
    atomic_json(output / "records.json", rows)
    report = {"kind": "historical_audit", "files": len(files), "measurement_records": len(rows),
              "sources": sources, "score_counts": dict(counts),
              "limitations": ["Historical uncalibrated scores; not latent ground truth", "No dynamics fitting performed"]}
    atomic_json(output / "audit.json", report)
    return report
