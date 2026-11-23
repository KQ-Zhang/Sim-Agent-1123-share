"""Expand paired runs, freeze provenance and resume atomic round checkpoints."""
from collections import Counter
from itertools import product
from pathlib import Path
from .agents import AgentClient
from .config import load_config, validate, ROOT
from .environment import initial_state, contexts, commit
from .legacy import fixed_graph, exclusive_lock
from .storage import atomic_json, read_json, file_hash, digest


def code_hashes():
    files = [ROOT / "run.py", *sorted((ROOT / "src").rglob("*.py"))]
    return {str(p.relative_to(ROOT)): file_hash(p) for p in files}


def prepare(path):
    cfg = validate(load_config(path))
    manifest = read_json(cfg["data"]["manifest"])
    if manifest.get("fixture") is not True:
        raise ValueError("v0.1 scenario loader accepts fixture manifests only; use audit-e23 for historical data")
    scenarios = manifest["events"]
    if len({s["id"] for s in scenarios}) != len(scenarios):
        raise ValueError("Duplicate scenario IDs")
    lookup = {s["id"]: s for s in scenarios}
    for event in cfg["data"]["events"]:
        if event not in lookup:
            raise ValueError(f"Unknown event: {event}")
        s = lookup[event]
        if s["split"] != cfg["data"]["split"]:
            raise ValueError("Scenario split mismatch")
        if not s["profiles"] or any(not isinstance(p["role"], str) or not isinstance(p["interests"], list) for p in s["profiles"]):
            raise ValueError("Invalid profiles")
        messages = s["messages"]
        if len({m["id"] for m in messages}) != len(messages):
            raise ValueError("Duplicate source message IDs")
        for m in messages:
            if not m["id"].startswith("source:") or m["event"] != event or m["channel"] not in {"platform", "community"} or type(m["tick"]) is not int or m["tick"] < 0 or m["author"] is not None or not isinstance(m["text"], str):
                raise ValueError("Invalid source message")
        by_id = {m["id"]: m for m in messages}
        if "fixed_replay" in cfg["experiment"]["modes"]:
            for tick in range(cfg["environment"]["rounds"]):
                replay = s["replay"].get(str(tick))
                if replay is None or replay["channel"] not in {"platform", "community"} or len(replay["cards"]) > cfg["environment"]["capacity"] or len(set(replay["cards"])) != len(replay["cards"]):
                    raise ValueError("Missing/invalid replay round")
                if any(pid not in by_id or by_id[pid]["tick"] > tick for pid in replay["cards"]):
                    raise ValueError("Replay contains unknown or future messages")
                if not isinstance(replay["history"], list) or any(p["tick"] >= tick for p in replay["history"]):
                    raise ValueError("Replay history must predate current tick")
    provenance = {"framework_version": "0.1.0", "config": cfg, "code_sha256": code_hashes(),
                  "manifest_sha256": file_hash(cfg["data"]["manifest"]),
                  "prompt_sha256": file_hash(cfg["agent"]["prompt"]),
                  "protocol_sha256": file_hash(cfg["experiment"]["protocol"])}
    jobs = []
    for event, mode, seed, variant in product(cfg["data"]["events"], cfg["experiment"]["modes"], cfg["experiment"]["seeds"], cfg["agent"]["variants"]):
        identity = {"event": event, "mode": mode, "seed": seed, "variant": variant}
        frozen = {**provenance, "identity": identity}
        fingerprint = digest(frozen)
        jobs.append({"run_id": fingerprint[:24], "fingerprint": fingerprint, "frozen": frozen,
                     "identity": identity, "scenario": lookup[event]})
    return cfg, jobs


def run_one(cfg, job, allow_remote=False, until=None):
    folder = Path(cfg["runtime"]["output_root"]) / job["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(folder / ".run.lock"):
        frozen_path = folder / "resolved_config.json"
        if frozen_path.exists() and read_json(frozen_path) != job["frozen"]:
            raise ValueError("Frozen provenance mismatch")
        if not frozen_path.exists():
            atomic_json(frozen_path, job["frozen"])
        identity = job["identity"]
        state = initial_state(cfg["environment"]["nodes"])
        checkpoint = folder / "checkpoint.json"
        if checkpoint.exists():
            saved = read_json(checkpoint)
            if saved["fingerprint"] != job["fingerprint"] or saved["state_sha256"] != digest(saved["state"]):
                raise ValueError("Checkpoint integrity/configuration mismatch")
            state = saved["state"]
        graph, edges, _ = fixed_graph(cfg["environment"]["nodes"], identity["seed"],
                                     cfg["environment"]["degree"], cfg["environment"]["rewiring"])
        atomic_json(folder / "network.json", {"edges": edges, "seed": identity["seed"]})
        client = AgentClient(folder, cfg, identity["variant"], identity["seed"], allow_remote)
        rounds = cfg["environment"]["rounds"]
        stop = min(rounds, until) if until is not None else rounds
        if stop < 0:
            raise ValueError("until must be nonnegative")
        try:
            while state["next_tick"] < stop:
                inputs = contexts(cfg, job["scenario"], graph, state, identity["seed"], identity["mode"])
                answers = [client.call(c) if c["online"] else (None, None) for c in inputs]
                commit(cfg, job["scenario"], state, inputs, answers, True)
                atomic_json(checkpoint, {"fingerprint": job["fingerprint"], "state_sha256": digest(state), "state": state})
            counts = Counter(row["status"] for row in state["records"])
            status = {"run_id": job["run_id"], **identity, "fixture": True,
                      "backend": identity["variant"]["backend"], "completed_rounds": state["next_tick"],
                      "planned_rounds": rounds, "status": "complete" if state["next_tick"] == rounds else "paused",
                      "records": len(state["records"]), "counts": dict(counts), "request_attempts": client.attempts,
                      "measurement_status": "not_measured", "trajectory_sha256": digest(state["records"])}
            # Derived exports can always be rebuilt from the single authoritative checkpoint.
            atomic_json(folder / "trajectory.json", state["records"])
            atomic_json(folder / "summary.json", status)
            return status
        except Exception as error:
            atomic_json(folder / "failure.json", {"error_type": type(error).__name__, "run_id": job["run_id"],
                                                   "note": "Resume starts from last atomic checkpoint"})
            raise


def run_batch(path, allow_remote=False, until=None):
    cfg, jobs = prepare(path)
    if any(j["identity"]["variant"]["backend"] == "chat" for j in jobs) and not allow_remote:
        raise ValueError("Remote jobs require explicit --allow-remote at manual launch")
    summaries, failures = [], []
    for job in jobs:
        try:
            summaries.append(run_one(cfg, job, allow_remote, until))
        except Exception as error:
            failures.append({"run_id": job["run_id"], "error_type": type(error).__name__})
    report = {"fixture": True, "jobs": len(jobs), "completed": sum(s["status"] == "complete" for s in summaries),
              "summaries": summaries, "failures": failures}
    atomic_json(Path(cfg["runtime"]["output_root"]) / "batch.json", report)
    return report
