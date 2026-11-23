"""Replaceable fixture and chat agents, with durable per-request attempt logs."""
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen
from .storage import canonical, digest, atomic_json, read_json, keyed_rng


def validate_action(answer, context):
    if not isinstance(answer, dict) or set(answer) != {"action", "text", "target"}:
        raise ValueError("Expected action/text/target object")
    if answer["action"] not in {"silent", "original", "repost", "bridge"} or not isinstance(answer["text"], str) or len(answer["text"]) > 500:
        raise ValueError("Invalid action or text")
    if answer["action"] in {"repost", "bridge"}:
        source = context["cards"] if answer["action"] == "repost" else context["remembered"]
        if answer["target"] not in {p["id"] for p in source}:
            raise ValueError("Action target was not visible")
    elif answer["target"] is not None:
        raise ValueError("Unexpected target")
    if answer["action"] == "silent" and answer["text"]:
        raise ValueError("Silent action must have empty text")
    if answer["action"] == "original" and (not answer["text"].strip() or not context["cards"]):
        raise ValueError("Original requires text and current evidence")
    return answer


def fixture_action(variant, context, seed):
    rng = keyed_rng(seed, "action", context["tick"], context["agent"])
    if rng.random() < (.25 if variant["model"] == "fixture-cautious" else .08):
        return {"action": "silent", "text": "", "target": None}
    if context["remembered"] and rng.random() < .3:
        return {"action": "bridge", "text": "", "target": context["remembered"][0]["id"]}
    if context["cards"] and rng.random() < .6:
        return {"action": "repost", "text": "", "target": context["cards"][0]["id"]}
    if not context["cards"]:
        return {"action": "silent", "text": "", "target": None}
    return {"action": "original", "text": "【离线fixture】需要核查材料，并考虑统一规则与个案裁量。", "target": None}


class AgentClient:
    def __init__(self, folder, cfg, variant, seed, allow_remote=False):
        self.folder, self.cfg, self.variant, self.seed = Path(folder), cfg, variant, seed
        self.prompt = Path(cfg["agent"]["prompt"]).read_text(encoding="utf-8")
        self.cache = self.folder / "requests"
        self.cache.mkdir(exist_ok=True)
        self.log = self.folder / "attempts.jsonl"
        self.attempts = len(self.log.read_text(encoding="utf-8").splitlines()) if self.log.exists() else 0
        if variant["backend"] == "chat" and not allow_remote:
            raise ValueError("Chat execution requires explicit --allow-remote at manual launch")

    def call(self, context):
        body = {"model": self.variant["model"], "temperature": self.cfg["agent"]["temperature"],
                "max_tokens": self.cfg["agent"]["max_tokens"],
                "seed": int(digest([self.seed, context["tick"], context["agent"]])[:8], 16),
                "messages": [{"role": "system", "content": self.prompt},
                             {"role": "user", "content": canonical(context)}],
                "response_format": {"type": "json_object"}}
        key = digest([self.variant, self.cfg["agent"]["model_revision"], body])
        path = self.cache / (key + ".json")
        if path.exists():
            return validate_action(read_json(path)["answer"], context), key
        prior = []
        if self.log.exists():
            prior = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        used = sum(row["request_id"] == key for row in prior)
        last_error = "Attempt limit exhausted; use a new protocol/config for additional attempts"
        for attempt in range(used, self.cfg["runtime"]["max_attempts"]):
            if self.attempts >= self.cfg["runtime"]["max_requests"]:
                raise RuntimeError("Per-run request budget exhausted")
            record = {"request_id": key, "tick": context["tick"], "agent": context["agent"],
                      "attempt": attempt + 1, "backend": self.variant["backend"], "parameters": body}
            # Persist the attempt before sending, so interruption cannot reset the budget.
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(canonical(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.attempts += 1
            try:
                if self.variant["backend"] == "fixture":
                    raw = {"fixture": True}
                    answer = fixture_action(self.variant, context, self.seed)
                else:
                    endpoint = os.environ[self.cfg["agent"]["endpoint_env"]].rstrip("/")
                    headers = {"Content-Type": "application/json"}
                    api_key = os.environ.get(self.cfg["agent"]["api_key_env"])
                    if api_key:
                        headers["Authorization"] = "Bearer " + api_key
                    request = Request(endpoint + "/chat/completions", canonical(body).encode("utf-8"), headers)
                    with urlopen(request, timeout=self.cfg["runtime"]["timeout_seconds"]) as response:
                        raw = json.load(response)
                    answer = json.loads(raw["choices"][0]["message"]["content"])
                validate_action(answer, context)
                atomic_json(path, {"request_id": key, "answer": answer, "raw_response": raw})
                return answer, key
            except Exception as error:
                # Avoid persisting connection URLs/keys from exception strings.
                last_error = type(error).__name__
                atomic_json(self.cache / f"{key}.attempt-{attempt+1}.error.json", {"error_type": last_error, "request_id": key})
                if attempt + 1 < self.cfg["runtime"]["max_attempts"]:
                    time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"Request failed: {last_error}")
