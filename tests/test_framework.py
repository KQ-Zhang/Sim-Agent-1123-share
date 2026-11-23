"""Behavioral integration checks, including real loopback HTTP and interrupted execution."""
import ast
import copy
import json
import math
import random
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collective_dynamics.config import load_config, validate
from collective_dynamics.runner import prepare, run_one
from collective_dynamics.environment import contexts, initial_state
from collective_dynamics.agents import AgentClient, validate_action
from collective_dynamics.legacy import fixed_graph, daily_visit_weight, PostHeatLedger, exclusive_lock
from collective_dynamics.storage import read_json, atomic_json, file_hash, digest
from collective_dynamics.audit import audit_e23


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "outputs", prefix="test-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        cfg = load_config(ROOT / "configs/smoke.json")
        cfg["experiment"]["seeds"] = [101]
        cfg["experiment"]["modes"] = ["closed_loop"]
        cfg["agent"]["variants"] = cfg["agent"]["variants"][:1]
        cfg["data"]["events"] = ["fixture-library"]
        cfg["runtime"]["output_root"] = str(self.work / "runs")
        self.config = self.work / "config.json"
        atomic_json(self.config, cfg)
        self.cfg, jobs = prepare(self.config)
        self.job = jobs[0]

    def folder(self, cfg=None, job=None):
        return Path((cfg or self.cfg)["runtime"]["output_root"]) / (job or self.job)["run_id"]

    def test_plan_and_inheritance(self):
        cfg, jobs = prepare(ROOT / "configs/smoke.json")
        self.assertEqual(len(jobs), 24)
        self.assertEqual(len({j["run_id"] for j in jobs}), 24)
        self.assertEqual(Path(cfg["data"]["manifest"]), ROOT / "data/fixtures/scenarios.json")
        a, b = self.work / "a.json", self.work / "b.json"
        atomic_json(a, {"extends": "b.json"})
        atomic_json(b, {"extends": "a.json"})
        with self.assertRaises(ValueError):
            load_config(a)

    def test_validation_rejects_typos_and_bad_parameters(self):
        for section, key, value in [("environment", "capcity", 4), ("environment", "degree", 5),
                                    ("environment", "online_rate", 2), ("runtime", "output_root", str(ROOT.parent)),
                                    ("experiment", "seeds", [1, 1])]:
            cfg = copy.deepcopy(self.cfg)
            cfg[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(cfg)

    def test_migrated_graph_and_rhythm_match_originals(self):
        files = [("experiments/E01/code/exp01_small_world_pilot.py", "fixed_graph"),
                 ("experiments/E02/code/exp02_small_world_paired.py", "daily_visit_weight")]
        functions = {}
        reference = read_json(ROOT / "tests/legacy_reference.json")
        for relative, name in files:
            original = ROOT.parent / relative
            if not original.is_file():
                continue
            tree = ast.parse(original.read_text(encoding="utf-8-sig"))
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            namespace = {"NODES": 48, "SEED": 20261111, "NEIGHBORS": 6, "REWIRING": .12,
                         "random": random, "math": math}
            exec(compile(ast.Module(body=[node], type_ignores=[]), relative, "exec"), namespace)
            functions[name] = namespace[name]
        for seed in (1, 101, 20261111):
            graph, edges, shortcuts = fixed_graph(48, seed)
            self.assertEqual(digest([[sorted(n) for n in graph], edges, sorted(shortcuts)]), reference["graphs"][str(seed)])
            if "fixed_graph" in functions:
                self.assertEqual(fixed_graph(48, seed), functions["fixed_graph"](48, seed))
        for i in range(240):
            self.assertAlmostEqual(daily_visit_weight(i/10), reference["rhythm"][i], places=12)
            if "daily_visit_weight" in functions:
                self.assertEqual(daily_visit_weight(i/10), functions["daily_visit_weight"](i/10))
        ledger = PostHeatLedger()
        ledger.append(Counter({"p": 4}), Counter({"p": 1}))
        ledger.append(Counter({"p": 2}), Counter())
        self.assertEqual(ledger.snapshot()["p"].score, 6)

    def test_fixed_inputs_are_paired_and_do_not_use_model_history(self):
        scenario = self.job["scenario"]
        graph, _, _ = fixed_graph(12, 101)
        a, b = initial_state(12), initial_state(12)
        a["next_tick"] = b["next_tick"] = 3
        a["own"][0].append({"tick": 1, "text": "model specific"})
        self.assertEqual(contexts(self.cfg, scenario, graph, a, 101, "fixed_replay"),
                         contexts(self.cfg, scenario, graph, b, 101, "fixed_replay"))

    def test_resume_equals_continuous_and_completed_run_is_idempotent(self):
        partial = run_one(self.cfg, self.job, until=2)
        self.assertEqual(partial["status"], "paused")
        resumed = run_one(self.cfg, self.job)
        attempts = resumed["request_attempts"]
        self.assertEqual(run_one(self.cfg, self.job)["request_attempts"], attempts)
        cfg = copy.deepcopy(self.cfg)
        cfg["runtime"]["output_root"] = str(self.work / "continuous")
        path = self.work / "continuous.json"
        atomic_json(path, cfg)
        cfg, jobs = prepare(path)
        continuous = run_one(cfg, jobs[0])
        self.assertEqual(resumed["trajectory_sha256"], continuous["trajectory_sha256"])
        self.assertEqual(resumed["records"], 72)

    def test_interrupted_round_reuses_completed_requests(self):
        original = AgentClient.call
        calls = 0
        def interrupted(client, context):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("Injected interruption")
            return original(client, context)
        with patch.object(AgentClient, "call", interrupted), self.assertRaises(RuntimeError):
            run_one(self.cfg, self.job)
        result = run_one(self.cfg, self.job)
        cfg = copy.deepcopy(self.cfg)
        cfg["runtime"]["output_root"] = str(self.work / "reference")
        path = self.work / "reference.json"
        atomic_json(path, cfg)
        cfg, jobs = prepare(path)
        reference = run_one(cfg, jobs[0])
        self.assertEqual(result["trajectory_sha256"], reference["trajectory_sha256"])
        self.assertEqual(result["request_attempts"], reference["request_attempts"])

    def test_visibility_synchronous_commit_and_distinct_missing_status(self):
        with patch("collective_dynamics.agents.urlopen", side_effect=AssertionError("Fixture called network")):
            run_one(self.cfg, self.job)
        records = read_json(self.folder() / "trajectory.json")
        statuses = {r["status"] for r in records}
        self.assertTrue({"offline", "silent", "published"} <= statuses)
        graph, _, _ = fixed_graph(12, 101)
        for row in records:
            self.assertTrue(row["fixture"])
            self.assertIsNone(row["measurement"]["value"])
            for card in row["context"]["cards"]:
                self.assertLessEqual(card["tick"], row["tick"])
                if card["author"] is not None:
                    self.assertLess(card["tick"], row["tick"])
                    if row["context"]["channel"] == "community":
                        self.assertIn(card["author"], graph[row["agent"]])
            if not row["context"]["online"]:
                self.assertIsNone(row["request_id"])
        with self.assertRaises(ValueError):
            validate_action({"action": "repost", "text": "", "target": "invisible"}, records[0]["context"])

    def test_future_replay_is_rejected_before_execution(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["experiment"]["modes"] = ["fixed_replay"]
        manifest = read_json(cfg["data"]["manifest"])
        manifest["events"][0]["replay"]["0"]["cards"] = ["source:fixture-library:2"]
        source = self.work / "future.json"
        atomic_json(source, manifest)
        cfg["data"]["manifest"] = str(source)
        atomic_json(self.config, cfg)
        with self.assertRaises(ValueError):
            prepare(self.config)

    def test_corrupted_checkpoint_rejected(self):
        run_one(self.cfg, self.job, until=1)
        path = self.folder() / "checkpoint.json"
        checkpoint = read_json(path)
        checkpoint["state"]["next_tick"] = 5
        atomic_json(path, checkpoint)
        with self.assertRaises(ValueError):
            run_one(self.cfg, self.job)

    def test_lock_excludes_second_writer(self):
        path = self.work / "lock"
        with exclusive_lock(path):
            with self.assertRaises(OSError), exclusive_lock(path):
                pass

    def test_loopback_chat_adapter_and_persistent_cache(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"choices": [{"message": {"content": json.dumps({"action": "silent", "text": "", "target": None})}}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        folder = self.work / "chat"
        folder.mkdir()
        variant = {"id": "local-http-test", "backend": "chat", "model": "stub"}
        try:
            with self.assertRaises(ValueError):
                AgentClient(folder, self.cfg, variant, 101)
            with patch.dict("os.environ", {"LCD_API_BASE": f"http://127.0.0.1:{server.server_port}/v1"}):
                client = AgentClient(folder, self.cfg, variant, 101, allow_remote=True)
                graph, _, _ = fixed_graph(12, 101)
                context = contexts(self.cfg, self.job["scenario"], graph, initial_state(12), 101, "fixed_replay")[0]
                self.assertEqual(client.call(context), client.call(context))
                self.assertEqual(len(received), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_historical_import_preserves_scope_null_and_source(self):
        source = self.work / "legacy.jsonl"
        header = {"kind": "scenario", "event": "test", "arm": "discussion", "seed": 1}
        row = {"kind": "turn", "agent": 0, "round": 1, "post": {"text": ""}, "judge": {"discretion": {"score": None}}}
        source.write_text(json.dumps(header)+"\n"+json.dumps(row)+"\n", encoding="utf-8")
        before = file_hash(source)
        out = self.work / "audit"
        audit_e23(source, out)
        record = read_json(out / "records.json")[0]
        self.assertEqual(record["measurement"]["scope"], "own_full_history")
        self.assertEqual(record["status"], "silent")
        self.assertIsNone(record["measurement"]["value"])
        self.assertEqual(before, file_hash(source))
        with self.assertRaises(ValueError):
            audit_e23(source, out)


if __name__ == "__main__":
    unittest.main()
