import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import benchmark as bench
from benchmarks.demo_answers import answer
from benchmarks.executors import CodexExecutor, MockExecutor, normalize_usage, usage_from_events
from benchmarks import tasks


class BenchmarkTests(unittest.TestCase):
    def plan(self, path, prices=None):
        manifest = bench.build_manifest(simulation=True, prices=prices)
        path.mkdir()
        bench.write_new(path / "manifest.json", manifest)
        return manifest

    def test_manifest_is_deterministic_frozen_and_shuffled(self):
        one, two = bench.build_manifest(), bench.build_manifest()
        self.assertEqual(one, two)
        self.assertEqual(len(one["jobs"]), 18)
        self.assertNotEqual([j["id"] for j in one["jobs"]], sorted(j["id"] for j in one["jobs"]))
        changed = bench.build_manifest(seed=10)
        self.assertNotEqual(one["manifest_hash"], changed["manifest_hash"])
        self.assertTrue(all(j["prompt_hash"] == bench.digest(j["prompt"]) for j in one["jobs"]))

    def test_router_assesses_text_without_expert_score(self):
        jobs = [j for j in bench.build_manifest()["jobs"] if j["strategy"] == "router"]
        for j in jobs:
            self.assertEqual(j["route"]["assessment_input"]["task_type"], "general")
            self.assertEqual(j["route"]["assessment_input"]["text"], j["task"]["instruction"])
            self.assertEqual(j["route"]["history"], {})
            self.assertIn("human_annotation", j["task"])

    def test_manifest_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            manifest = self.plan(path)
            manifest["seed"] = 9
            (path / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(bench.BenchmarkError):
                bench.load_manifest(path)

    def test_source_snapshot_is_complete_and_portably_replays(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            manifest = self.plan(path)
            bench.freeze_sources(path, manifest)
            for name, expected in manifest["code_hashes"].items():
                self.assertEqual(bench.file_hash(path / "source-snapshot" / name), expected)
            with patch("builtins.print"):
                bench.run_manifest(path, max_runs=1)
            import subprocess
            replay = subprocess.run([sys.executable, str(path / "source-snapshot/benchmarks/benchmark.py"),
                                     "replay", "--run-dir", str(path), "--output", str(Path(td) / "replayed.json")],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            result = bench.load_json(Path(td) / "replayed.json")
            self.assertEqual(result["code_drift"], [])
            self.assertEqual(result["model_requests"], 0)

    def test_all_reference_artifacts_independently_pass(self):
        for task in tasks.TASKS:
            with self.subTest(task=task["id"]):
                files = bench.validate_final(answer(task["id"]), task)
                self.assertTrue(bench.grade(task["id"], files)["passed"])

    def test_bad_grade_and_unsafe_code_fail(self):
        self.assertFalse(bench.grade("exact_edit", {"config.ini": "wrong"})["passed"])
        self.assertFalse(bench.grade("routine_code", {"solution.py": "import os\ndef union_ranges(x): return []\n"})["passed"])
        self.assertFalse(bench.grade("routine_code", {"solution.py": "def union_ranges(x):\n    while True: pass\n"}, timeout=0.1)["passed"])

    def test_artifact_paths_duplicates_and_missing_rejected(self):
        task = tasks.TASKS[0]
        for final in ({"files": [{"path": "../config.ini", "content": "x"}]},
                      {"files": []}, {"files": [{"path": "config.ini", "content": "x"}] * 2},
                      {"files": [{"path": "/tmp/config.ini", "content": "x"}]},
                      {"files": [{"path": "config.ini", "content": 3}]}, {"files": [], "extra": True}):
            with self.assertRaises(bench.BenchmarkError):
                bench.validate_final(final, task)

    def test_usage_missing_invalid_pseudozero_and_reasoning_not_added(self):
        raw = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 10}
        result = normalize_usage(raw)
        self.assertEqual(result["output_tokens"], 30)
        self.assertEqual(result["reasoning_tokens"], 10)
        for invalid in (None, {}, {**raw, "input_tokens": True}, {**raw, "cached_input_tokens": 101},
                        {**raw, "reasoning_output_tokens": 31},
                        {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}):
            self.assertFalse(normalize_usage(invalid)["known"])

    def test_duplicate_usage_and_identified_turn_sum(self):
        raw = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}
        self.assertFalse(usage_from_events([raw, raw], True)["known"])
        one = {"turn_id": "a", "usage": raw}
        self.assertEqual(usage_from_events([one, one], True)["input_tokens"], 100)
        self.assertEqual(usage_from_events([one, {"turn_id": "b", "usage": raw}], True)["input_tokens"], 200)
        self.assertFalse(usage_from_events([one, {"turn_id": "a", "usage": {**raw, "output_tokens": 40}}], True)["known"])

    def test_price_unknown_is_null_and_cached_reasoning_not_double_billed(self):
        usage = normalize_usage({"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 400, "reasoning_output_tokens": 100})
        prices = {"models": {"m": {"input_per_million": 2, "cached_input_per_million": 1, "output_per_million": 3}}}
        self.assertEqual(bench.price_cost(usage, "m", prices)["usd"], "0.0031")
        for price in (None, {"models": {}}, {"models": {"m": {"input": 0, "output": 0}}}):
            self.assertIsNone(bench.price_cost(usage, "m", price)["usd"])
        self.assertIsNone(bench.price_cost({"known": False}, "m", prices)["usd"])

    def test_resume_never_repeats_complete_requests_and_workspaces_are_public(self):
        class Counting(MockExecutor):
            calls = 0
            def execute(self, *args):
                self.calls += 1
                return super().execute(*args)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            manifest = self.plan(path)
            executor = Counting()
            with patch("builtins.print"):
                bench.run_manifest(path, executor, max_runs=1)
                self.assertEqual(executor.calls, 1)
                bench.run_manifest(path, executor, max_runs=18)
                self.assertEqual(executor.calls, 18)
                bench.run_manifest(path, executor, max_runs=18)
                self.assertEqual(executor.calls, 18)
            for job in manifest["jobs"]:
                workspace = path / "jobs" / job["id"] / "workspace"
                self.assertEqual(sorted(p.name for p in workspace.iterdir()), sorted(job["task"]["fixtures"]))
                self.assertTrue(bench.verify_workspace(workspace, job["task"]))
            original = bench.file_hash(tasks.FIXTURES / "config.ini")
            bench.regrade(path, Path(td) / "regrade.json")
            self.assertEqual(original, bench.file_hash(tasks.FIXTURES / "config.ini"))
            self.assertEqual(bench.report(path)["groups"]["router"]["successes"], 6)

    def test_inflight_requires_manual_resolution_with_unknown_cost(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            manifest = self.plan(path)
            job = manifest["jobs"][0]
            bench.write_new(path / "jobs" / job["id"] / "started.json", {"created_at": "test"})
            with self.assertRaisesRegex(bench.BenchmarkError, "unresolved inflight"):
                bench.run_manifest(path)
            bench.resolve_interrupted(path, job["id"])
            result = bench.load_json(path / "jobs" / job["id"] / "result.json")
            self.assertEqual(result["status"], "interrupted_unknown")
            self.assertIsNone(result["cost"]["usd"])
            with self.assertRaises(FileExistsError):
                bench.resolve_interrupted(path, job["id"])

    def test_failure_stays_in_denominator_and_cost_per_success(self):
        class FailOnce(MockExecutor):
            calls = 0
            def execute(self, *args):
                self.calls += 1
                value = super().execute(*args)
                if self.calls == 1:
                    value["final"] = {"files": []}
                return value
        prices = {"models": {m: {"input": 2, "cached_input": 1, "output": 3} for m in bench.DEMO_CATALOG["models"]}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            manifest = self.plan(path, prices)
            with patch("builtins.print"):
                bench.run_manifest(path, FailOnce())
            group = bench.report(path)["groups"][manifest["jobs"][0]["strategy"]]
            self.assertEqual(group["completed"], 6)
            self.assertEqual(group["successes"], 5)
            self.assertEqual(group["acceptance_rate"], 5/6)
            self.assertEqual(group["known_failure_cost_usd"], "0.0031")
            self.assertEqual(group["cost_per_success_usd"], "0.00372")

    def test_code_drift_blocks_new_requests_and_surfaces_in_report(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            self.plan(path)
            with patch.object(bench, "code_drift", return_value=[{"path": "test"}]):
                with self.assertRaisesRegex(bench.BenchmarkError, "frozen code"):
                    bench.run_manifest(path)
                self.assertEqual(bench.report(path)["code_drift"], [{"path": "test"}])

    def test_live_gate_no_silent_simulation_or_model_substitution(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run"
            self.plan(path)
            with self.assertRaisesRegex(bench.BenchmarkError, "requires --live"):
                bench.run_manifest(path, CodexExecutor())
            with self.assertRaisesRegex(bench.BenchmarkError, "simulation manifest"):
                bench.run_manifest(path, CodexExecutor(), live=True)
        catalog = copy.deepcopy(bench.DEMO_CATALOG)
        del catalog["models"]["gpt-6-astra"]
        with self.assertRaisesRegex(bench.BenchmarkError, "unsupported model"):
            bench.build_manifest(catalog=catalog)

    def test_codex_executor_discards_raw_bodies_removes_api_env_and_flags_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "fake_codex"
            fake.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv
assert all(k not in os.environ for k in ('OPENAI_API_KEY','CODEX_API_KEY','OPENAI_BASE_URL'))
path=args[args.index('--output-last-message')+1]
open(path,'w').write(json.dumps({'files':[{'path':'config.ini','content':'test'}]}))
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','body':'SECRET_REASONING'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}))
''')
            fake.chmod(0o755)
            workspace = root / "workspace"
            workspace.mkdir()
            attempt = root / "attempt"
            attempt.mkdir()
            job = {"route": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}, "prompt": "test"}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "CODEX_API_KEY": "test", "OPENAI_BASE_URL": "test"}):
                result = CodexExecutor(str(fake)).execute(job, workspace, attempt)
            self.assertEqual(result["status"], "protocol_violation")
            self.assertTrue(result["usage"]["known"])
            self.assertNotIn("SECRET_REASONING", json.dumps(result))
            self.assertFalse((attempt / "final-message.json").exists())
            command = CodexExecutor().command(job, workspace, attempt / "schema", attempt / "final")
            self.assertIn("read-only", command)
            self.assertIn("--ignore-user-config", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)


if __name__ == "__main__":
    unittest.main()
