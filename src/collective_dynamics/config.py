"""JSON configuration, inheritance and validation. Paths resolve from their defining file."""
from copy import deepcopy
from pathlib import Path
from .storage import read_json

ROOT = Path(__file__).resolve().parents[2]
PATH_FIELDS = {"manifest", "prompt", "output_root", "protocol"}


def merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def load_config(path, stack=()):
    path = Path(path).resolve()
    if path in stack:
        raise ValueError("Configuration inheritance cycle")
    own = read_json(path)
    if not isinstance(own, dict):
        raise ValueError("Configuration must be an object")
    base = load_config(path.parent / own.pop("extends"), (*stack, path)) if "extends" in own else {}
    for section in own.values():
        if isinstance(section, dict):
            for key in PATH_FIELDS & section.keys():
                section[key] = str((path.parent / section[key]).resolve())
    return merge(base, own)


def validate(cfg):
    allowed = {"experiment", "data", "environment", "agent", "measurement", "runtime"}
    if set(cfg) != allowed:
        raise ValueError(f"Expected configuration sections: {sorted(allowed)}")
    fields = {
        "experiment": {"id", "protocol", "modes", "seeds"},
        "data": {"manifest", "events", "split"},
        "environment": {"nodes", "degree", "rewiring", "rounds", "start_hour", "step_minutes", "online_rate", "diurnal", "community_rate", "capacity", "message_lifetime", "memory_rounds", "interest_weight", "heat_weight"},
        "agent": {"variants", "prompt", "temperature", "max_tokens", "endpoint_env", "api_key_env", "model_revision"},
        "measurement": {"axis", "scope"},
        "runtime": {"output_root", "timeout_seconds", "max_attempts", "max_requests"},
    }
    for section, keys in fields.items():
        extras = set(cfg[section]) - keys
        if extras:
            raise ValueError(f"Unknown {section} fields: {sorted(extras)}")
    e = cfg["environment"]
    for key in ("nodes", "degree", "rounds", "capacity", "message_lifetime", "memory_rounds", "step_minutes"):
        if type(e[key]) is not int or e[key] < (0 if key == "memory_rounds" else 1):
            raise ValueError(f"Invalid environment.{key}")
    if not 2 <= e["degree"] < e["nodes"] or e["degree"] % 2:
        raise ValueError("degree must be even, >=2 and smaller than nodes")
    for key in ("rewiring", "online_rate", "community_rate"):
        if not 0 <= e[key] <= 1:
            raise ValueError(f"{key} must be in [0,1]")
    if type(e["diurnal"]) is not bool or not 0 <= e["start_hour"] < 24:
        raise ValueError("Invalid time settings")
    if any(e[k] < 0 for k in ("interest_weight", "heat_weight")):
        raise ValueError("Ranking weights must be nonnegative")
    exp = cfg["experiment"]
    if not exp["modes"] or not set(exp["modes"]) <= {"fixed_replay", "closed_loop"} or len(set(exp["modes"])) != len(exp["modes"]):
        raise ValueError("Invalid or duplicate modes")
    if not exp["seeds"] or any(type(s) is not int for s in exp["seeds"]) or len(set(exp["seeds"])) != len(exp["seeds"]):
        raise ValueError("Seeds must be unique integers")
    variants = cfg["agent"]["variants"]
    if not variants or len({v["id"] for v in variants}) != len(variants):
        raise ValueError("Variants must have unique IDs")
    for v in variants:
        if set(v) != {"id", "backend", "model"} or v["backend"] not in {"fixture", "chat"}:
            raise ValueError("Invalid agent variant")
        if v["backend"] == "fixture" and v["model"] not in {"fixture-cautious", "fixture-social"}:
            raise ValueError("Unknown fixture model")
    for key in ("timeout_seconds", "max_attempts", "max_requests"):
        if cfg["runtime"][key] <= 0:
            raise ValueError(f"Invalid runtime.{key}")
    if cfg["measurement"] != {"axis": "discretion", "scope": "current_utterance"}:
        raise ValueError("v0.1 supports current_utterance/discretion only; scoring is not yet calibrated")
    output = Path(cfg["runtime"]["output_root"]).resolve()
    if not output.is_relative_to(ROOT / "outputs") or output == ROOT / "outputs":
        raise ValueError("output_root must be a new subdirectory inside this project's outputs")
    if not cfg["data"]["events"] or len(set(cfg["data"]["events"])) != len(cfg["data"]["events"]):
        raise ValueError("Events must be nonempty and unique")
    for path in (cfg["data"]["manifest"], cfg["agent"]["prompt"], cfg["experiment"]["protocol"]):
        if not Path(path).is_file():
            raise ValueError(f"Missing input: {path}")
    return cfg
