import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import difficulty
import feedback
import model_registry
import policy
import router

MODELS = {m: list(router.EFFORTS if m != "gpt-6-luna" else router.EFFORTS[:-1]) for m in router.HOST_SPAWN_MODELS}
T1 = "00000000-0000-4000-8000-000000000001"
T2 = "00000000-0000-4000-8000-000000000002"


def sealed(snapshot):
    snapshot["hash"] = model_registry.snapshot_hash(snapshot)
    return snapshot


def row(snapshot, model):
    return next(m for m in snapshot["models"] if m["id"] == model)


def host(extra=None):
    models = copy.deepcopy(MODELS)
    models.update(extra or {})
    return {"models": models, "source": "native-tool-baseline+evidence-admitted-host",
            "observed_at": 1000, "hash": policy.digest(models)}


class Actions(unittest.TestCase):
    def test_quoted_architecture_is_replacement_object(self):
        for text in ('只把 README 里的“架构”替换为“设计”', "Only replace 'architecture' with 'design' in README.",
                     '先阅读README，再只把“architecture”替换为“design”'):
            with self.subTest(text=text):
                rating = difficulty.assess(text=text)
                self.assertEqual(rating["task_type"], "edit")
                self.assertLess(rating["score"], 3)
                self.assertIn("lexical_change", rating["features"]["constraints"])

    def test_proof_and_counterexample_carry_obligations(self):
        rating = difficulty.assess(text="证明附录的定理，并找反例")
        self.assertEqual(rating["task_type"], "reason")
        self.assertGreaterEqual(rating["score"], 7)
        self.assertIn("adversarial_check", rating["features"]["constraints"])
        self.assertFalse(rating["confidence_calibrated"])

    def test_actions_are_not_quoted_objects(self):
        rating = difficulty.assess(text='Edit the paragraph containing "prove this theorem".')
        self.assertEqual(rating["task_type"], "edit")
        self.assertNotIn("proof_obligation", rating["features"]["constraints"])
        self.assertEqual(difficulty.assess(text="Design a distributed system architecture")["task_type"], "architecture")

    def test_generic_repair_and_modify_actions(self):
        self.assertEqual(difficulty.assess(text="Repair the supplied Cache class to handle missing keys")["task_type"], "debug")
        for text in ("Modify one number in a document", "changing only numeric value; preserve every other byte"):
            rating = difficulty.assess(text=text)
            self.assertEqual(rating["task_type"], "edit")
            self.assertLess(rating["score"], 4.5)
        self.assertIn("preserve_non_target_content", difficulty.assess(text="changing only numeric value; preserve every other byte")["features"]["constraints"])

    def test_explicit_type_and_score_dominate(self):
        rating = difficulty.assess(text="Prove the theorem and find a counterexample", task_type="edit", score="2.1")
        self.assertEqual((rating["task_type"], rating["score"]), ("edit", 2.1))
        self.assertEqual(difficulty.assess(text='只把“架构”替换为“设计”', score="8.8")["score"], 8.8)


class Ranking(unittest.TestCase):
    def setUp(self):
        self.snapshot = model_registry.bundled_snapshot()
        self.cfg = copy.deepcopy(router.DEFAULT)
        self.rating = difficulty.assess(task_type="code", score=5.0)

    def choose(self, **kwargs):
        params = {"snapshot": sealed(copy.deepcopy(self.snapshot)), "history": {}, "host_snapshot": host()}
        params.update(kwargs)
        return router.select(self.cfg, "act", "normal", "low", True, 0, MODELS, self.rating, **params)

    def test_pins_survive_preferences_and_learning(self):
        self.cfg["overrides"] = {"act": {"model": "gpt-6-sol", "reasoning_effort": "high"}}
        rating = self.choose(profile="economy", history={"gpt-6-sol:high": {"accepted": 0, "rejected": 99}})
        self.assertEqual((rating["model"], rating["reasoning_effort"]), ("gpt-6-sol", "high"))
        self.assertEqual(rating["policy"]["source"], "explicit_pin")

    def test_fixed_preset_remains_fixed(self):
        self.cfg.update(mode="preset", preset="quality")
        self.assertEqual(self.choose(profile="economy")["model"], "gpt-6.1-sol")

    def test_verified_strength_can_change_quality_rank(self):
        first, second = row(self.snapshot, "gpt-6.1-sol"), row(self.snapshot, "gpt-6-sol")
        first["routing"].update(verified=True, strengths={"code": 0})
        second["routing"].update(verified=True, strengths={"code": 1})
        result = self.choose(profile="quality")
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertIn("verified_workload_strength", result["policy"]["candidates"][0]["reasons"])
        self.assertFalse(result["policy"]["candidates"][0]["prior_calibrated"])

    def test_unverified_strength_is_neutral(self):
        row(self.snapshot, "gpt-6-sol")["routing"].update(verified=False, strengths={"code": 1})
        self.assertEqual(self.choose(profile="quality")["model"], "gpt-6.1-sol")

    def test_economy_uses_only_verified_comparable_prices(self):
        for m in self.snapshot["models"]:
            m["pricing"].update(verified=True, usd_per_million={"input": 100, "output": 100})
        row(self.snapshot, "gpt-6-sol")["pricing"]["usd_per_million"] = {"input": 1, "output": 1}
        self.assertEqual(self.choose(profile="economy")["model"], "gpt-6-sol")
        row(self.snapshot, "gpt-6-sol")["pricing"]["verified"] = False
        self.assertEqual(self.choose(profile="economy")["model"], "gpt-6.1-sol")

    def test_speed_requires_separate_latency_verification(self):
        for m in self.snapshot["models"]:
            m["routing"].update(latency_ms=1000, latency_verified=True, verified=False)
        row(self.snapshot, "gpt-6-sol")["routing"]["latency_ms"] = 1
        self.assertEqual(self.choose(profile="speed")["model"], "gpt-6-sol")
        row(self.snapshot, "gpt-6-sol")["routing"]["latency_verified"] = False
        self.assertEqual(self.choose(profile="speed")["model"], "gpt-6.1-sol")

    def test_sample_gate_and_bad_history(self):
        low = {"gpt-6.1-sol:medium": {"accepted": 0, "rejected": 4},
               "gpt-6-sol:medium": {"accepted": 4, "rejected": 0}}
        self.assertEqual(self.choose(history=low)["model"], "gpt-6.1-sol")
        enough = {"gpt-6.1-sol:medium": {"accepted": 0, "rejected": 5},
                  "gpt-6-sol:medium": {"accepted": 5, "rejected": 0}}
        self.assertEqual(self.choose(history=enough)["model"], "gpt-6-sol")
        for bad in (-1, float("nan"), 3.5, True):
            with self.subTest(bad=bad), self.assertRaises(router.RouteError):
                self.choose(history={"gpt-6.1-sol:medium": {"accepted": bad}})

    def new_model(self, tier="standard", status="admitted"):
        new = copy.deepcopy(row(self.snapshot, "gpt-6.1-sol"))
        new.update(id="future-sol", status=status, endpoints=[{"provider": "codex", "id": "future-sol", "kind": "native", "verified": True}])
        new["routing"].update(tier=tier, verified=True, strengths={"code": 1}, safety_equivalent_verified=False)
        self.snapshot["models"].append(new)
        return new

    def test_catalog_only_model_cannot_expand_host(self):
        self.new_model()
        expanded = {**MODELS, "future-sol": list(router.EFFORTS)}
        result = router.select(self.cfg, "act", "normal", "low", True, 0, expanded, self.rating,
                               list(expanded), snapshot=sealed(self.snapshot), history={})
        self.assertNotEqual(result["model"], "future-sol")

    def test_candidate_stays_pending_even_with_host_proof(self):
        self.new_model(status="candidate")
        expanded = {"future-sol": list(router.EFFORTS)}
        with self.assertRaises(router.RouteError):
            router.select(self.cfg, "act", "normal", "low", True, 0, expanded, self.rating,
                          snapshot=sealed(self.snapshot), host_snapshot=host(expanded))

    def test_admitted_future_model_works_without_code_change(self):
        self.new_model()
        expanded = {"future-sol": list(router.EFFORTS)}
        result = router.select(self.cfg, "act", "normal", "low", True, 0, expanded, self.rating,
                               snapshot=sealed(self.snapshot), host_snapshot=host(expanded), profile="quality")
        self.assertEqual(result["model"], "future-sol")

    def test_light_new_model_cannot_buy_its_way_into_demanding_task(self):
        new = self.new_model(tier="light")
        new["pricing"].update(verified=True, usd_per_million={"input": 0, "output": 0})
        expanded = {**MODELS, "future-sol": list(router.EFFORTS)}
        result = router.select(self.cfg, "act", "normal", "low", True, 0, expanded, self.rating,
                               snapshot=sealed(self.snapshot), host_snapshot=host(expanded), profile="economy")
        self.assertNotIn("future-sol", [c["model"] for c in result["policy"]["candidates"]])
        self.assertTrue(all(row(self.snapshot, c["model"])["routing"]["tier"] != "light" for c in result["policy"]["candidates"]))

    def test_safety_equivalence_and_effort_must_be_verified(self):
        new = self.new_model(tier="frontier")
        expanded = {"future-sol": ["xhigh"]}
        args = (self.cfg, "act", "normal", "high", True, 0, expanded, self.rating)
        with self.assertRaises(router.RouteError):
            router.select(*args, snapshot=sealed(self.snapshot), host_snapshot=host(expanded))
        new["routing"]["safety_equivalent_verified"] = True
        self.assertEqual(router.select(*args, snapshot=sealed(self.snapshot), host_snapshot=host(expanded))["model"], "future-sol")
        expanded = {"future-sol": ["medium"]}
        with self.assertRaises(router.RouteError):
            router.select(self.cfg, "act", "normal", "high", True, 0, expanded, self.rating,
                          snapshot=sealed(self.snapshot), host_snapshot=host(expanded))

    def test_tool_and_context_unknown_fail_closed_including_pin(self):
        with self.assertRaises(router.RouteError):
            self.choose(constraints={"context_tokens": 100})
        self.assertTrue(self.choose(constraints={"tools": True})["model"])
        with self.assertRaises(router.RouteError):
            self.choose(constraints={"tools": ["browser"]})
        self.assertTrue(self.choose(constraints={"tools": ["browser"], "available_tools": ["browser"]})["model"])
        self.cfg["overrides"] = {"act": {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}}
        row(self.snapshot, "gpt-6.1-sol")["capabilities"]["tools"] = False
        with self.assertRaises(router.RouteError):
            self.choose(constraints={"tools": True})

    def test_three_effort_intersection(self):
        row(self.snapshot, "gpt-6.1-sol")["capabilities"]["efforts"] = ["high"]
        self.assertEqual(self.choose()["model"], "gpt-6-sol")
        frozen_host = host()
        frozen_host["models"]["gpt-6.1-sol"] = ["low"]
        frozen_host["hash"] = policy.digest(frozen_host["models"])
        self.assertEqual(self.choose(host_snapshot=frozen_host)["model"], "gpt-6-sol")

    def test_frozen_input_does_not_read_new_host_or_feedback(self):
        frozen = host()
        before = self.choose(host_snapshot=frozen)
        with patch("model_registry.host_models", side_effect=AssertionError("must not read mutable host admission")):
            after = self.choose(host_snapshot=frozen)
        self.assertEqual(before, after)
        tampered = copy.deepcopy(self.snapshot)
        row(tampered, "gpt-6.1-sol")["routing"]["strengths"] = {"code": 1}
        with self.assertRaises(router.RouteError):
            self.choose(snapshot=tampered)


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = router.Store(self.tmp.name)
        self.snapshot = model_registry.bundled_snapshot()

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, thread=T1, mode="auto", learning=True):
        return self.store.propose(thread, "act", "normal", "low", True, 0, MODELS,
                                  task_type="code", difficulty_score=5.0, mode=mode,
                                  snapshot=self.snapshot, learning=learning)

    def dispatched(self, **kwargs):
        result = self.propose(**kwargs)
        self.store.update(result["id"], result["thread_id"], "mark", agent_id="/root/worker", turn_id="turn-1")
        return result

    def accepted(self, record, **kwargs):
        data = {"actual_model": record["route"]["model"], "actual_effort": record["route"]["reasoning_effort"],
                "verification_source": "automated_tests", "evidence_id": "tests:run1"}
        data.update(kwargs)
        return self.store.outcome(record["id"], record["thread_id"], "accepted", **data)

    def test_completion_and_silence_never_imply_acceptance(self):
        record = self.dispatched()
        current = self.store.update(record["id"], T1, "get")
        self.assertIsNone(current["outcome"])
        self.assertEqual(current["summary"]["acceptance"], "unknown")
        with self.assertRaises(router.RouteError):
            self.accepted(record, verification_source="runtime")
        with self.assertRaises(router.RouteError):
            self.accepted(record, verification_source="unknown")

    def test_structure_legacy_dispatch_and_task_isolation(self):
        record = self.dispatched()
        current = self.store.update(record["id"], T1, "get")
        self.assertEqual((current["agent_id"], current["turn_id"]), ("/root/worker", "turn-1"))
        with self.assertRaises(router.RouteError):
            self.store.outcome(record["id"], T2, "unknown")
        legacy = self.propose(thread=T2)
        self.assertEqual(self.store.update(legacy["id"], T2, "mark", evidence="agent:test")["execution"], "dispatched")
        bad = self.propose()
        with self.assertRaises(router.RouteError):
            self.store.update(bad["id"], T1, "mark", agent_id="free form prompt text")

    def test_idempotent_receipt_rejects_conflict(self):
        record = self.dispatched()
        first = self.accepted(record, usage={"input_tokens": 10, "output_tokens": 3}, cost=.001, latency_ms=40)
        second = self.accepted(record, usage={"output_tokens": 3, "input_tokens": 10}, cost=.001, latency_ms=40)
        self.assertEqual(first["outcome"], second["outcome"])
        self.assertEqual(self.store.history(record["cohort"])[record["route"]["model"] + ":" + record["route"]["reasoning_effort"]]["accepted"], 1)
        with self.assertRaises(router.RouteError):
            self.store.outcome(record["id"], T1, "rejected", failure="quality", verification_source="user")

    def test_pre_upgrade_decision_acceptance_does_not_guess_learning_cohort(self):
        record = self.dispatched()
        with self.store.connect() as db:
            legacy = json.loads(db.execute('SELECT data FROM decisions WHERE id=?', (record['id'],)).fetchone()[0])
            legacy.pop('cohort')
            legacy.pop('learning', None)
            db.execute('UPDATE decisions SET data=? WHERE id=?', (json.dumps(legacy),record['id']))
        first = self.accepted(record)
        second = self.accepted(record)
        self.assertEqual(first['outcome'], second['outcome'])
        self.assertEqual(first['outcome']['status'], 'accepted')
        self.assertEqual(first['outcome']['learning_skip_reason'], 'legacy_decision_without_cohort')
        self.assertEqual(self.store.history(record['cohort']), {})

    def test_bad_feedback_does_not_mutate_state(self):
        record = self.dispatched()
        for fields in ({"status": "rejected"}, {"status": "accepted", "verification_source": "user", "failure": "quality"},
                       {"status": "unknown", "usage": {"prompt": "text"}}, {"status": "unknown", "usage": {"input_tokens": -1}},
                       {"status": "unknown", "cost": float("nan")}, {"status": "unknown", "latency_ms": -1},
                       {"status": "unknown", "actual_effort": "imaginary"}):
            with self.subTest(fields=fields), self.assertRaises(router.RouteError):
                self.store.outcome(record["id"], T1, **fields)
        self.assertIsNone(self.store.update(record["id"], T1, "get")["outcome"])
        self.assertEqual(self.store.history(record["cohort"]), {})
        pending = self.propose()
        with self.assertRaises(router.RouteError):
            self.accepted(pending)

    def test_benchmark_and_unknown_results_do_not_train_success(self):
        benchmark = self.dispatched()
        self.accepted(benchmark, scope="benchmark")
        frozen = self.dispatched(learning=False)
        self.accepted(frozen)
        unknown = self.dispatched()
        self.store.outcome(unknown["id"], T1, "unknown", actual_model=unknown["route"]["model"],
                           actual_effort=unknown["route"]["reasoning_effort"], verification_source="runtime")
        stats = self.store.history(unknown["cohort"])
        self.assertEqual((stats["gpt-6.1-sol:medium"]["accepted"], stats["gpt-6.1-sol:medium"]["rejected"], stats["gpt-6.1-sol:medium"]["unknown"]), (0, 0, 1))

    def test_unobserved_actual_pair_does_not_train(self):
        record = self.dispatched()
        self.store.outcome(record["id"], T1, "accepted", verification_source="user")
        self.assertEqual(self.store.history(record["cohort"]), {})

    def test_aggregate_and_receipts_survive_decision_pruning(self):
        with patch("router.time.time", return_value=1000):
            record = self.dispatched()
            self.accepted(record)
        with patch("router.time.time", return_value=1000 + 8 * 86400):
            self.propose()
        self.assertEqual(self.store.history(record["cohort"])["gpt-6.1-sol:medium"]["accepted"], 1)
        repeat = self.accepted(record)
        self.assertTrue(repeat["decision_pruned"])
        with self.assertRaises(router.RouteError):
            self.store.outcome(record["id"], T1, "unknown")

    def test_feedback_changes_same_cohort_only_after_gate(self):
        for _ in range(5):
            record = self.dispatched()
            self.store.outcome(record["id"], T1, "rejected", failure="quality", actual_model="gpt-6.1-sol",
                               actual_effort="medium", verification_source="user")
        self.assertEqual(self.propose()["route"]["model"], "gpt-6-sol")
        other = self.store.propose(T1, "act", "normal", "low", True, 0, MODELS,
                                   task_type="read", difficulty_score=5.0, snapshot=self.snapshot)
        self.assertEqual(other["route"]["model"], "gpt-6.1-sol")

    def test_ask_recommendation_is_frozen(self):
        record = self.propose(mode="ask")
        original = copy.deepcopy(record["route"])
        for _ in range(5):
            other = self.dispatched(thread=T2)
            self.store.outcome(other["id"], T2, "rejected", failure="quality", actual_model="gpt-6.1-sol",
                               actual_effort="medium", verification_source="user")
        resolved = self.store.update(record["id"], T1, "resolve", MODELS, choice="recommended")
        self.assertEqual(resolved["route"], original)
        self.assertEqual(self.propose()["route"]["model"], "gpt-6-sol")

    def test_preferences_keep_pins_and_input_text_is_not_retained(self):
        self.store.config(phase_override=("act", {"model": "gpt-6-sol", "reasoning_effort": "high"}))
        self.store.config(**policy.preference_answers("cost", "write"))
        self.assertEqual(self.store.config()["overrides"]["act"]["model"], "gpt-6-sol")
        record = self.store.propose(T1, "act", "normal", "low", True, 0, MODELS,
                                    task='Only replace "SECRET_ARCHITECTURE_TARGET_7391" with "SECRET_DESIGN_TARGET_7391"', snapshot=self.snapshot)
        self.assertNotIn("SECRET_", self.store.db.read_bytes().decode(errors="ignore"))
        self.assertEqual(record["route"]["model"], "gpt-6-sol")
        self.assertTrue(record["catalog"]["hash"].startswith("sha256:"))
        self.assertEqual(record["registry"]["hash"], self.snapshot["hash"])


class CatalogCache(unittest.TestCase):
    def test_reduced_then_recovered_catalog_is_explicit(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"ADAPTIVE_ROUTER_HOME": home}):
            def cli(models):
                return subprocess.CompletedProcess([], 0, json.dumps({"models": [{"slug": m, "supported_reasoning_levels": [{"effort": e} for e in levels]} for m, levels in models.items()]}), "")
            with patch("router.subprocess.run", return_value=cli(MODELS)):
                first = router.catalog()
            reduced = {k: v for k, v in MODELS.items() if k != "gpt-6.1-sol"}
            with patch("router.subprocess.run", return_value=cli(reduced)):
                second = router.catalog()
            self.assertEqual(second.metadata["status"], "reduced")
            self.assertNotIn("gpt-6.1-sol", second)
            self.assertNotEqual(first.metadata["hash"], second.metadata["hash"])
            with patch("router.subprocess.run", return_value=cli(MODELS)):
                recovered = router.catalog()
            self.assertEqual(recovered.metadata["status"], "fresh")
            with patch("router.subprocess.run", side_effect=OSError("not available")):
                stale = router.catalog()
            self.assertEqual(stale.metadata["status"], "stale")
            self.assertTrue(stale.metadata["fallback"])
            with patch("router.time.time", return_value=recovered.metadata["observed_at"] + 86401), patch("router.subprocess.run", side_effect=OSError("not available")):
                with self.assertRaises(router.RouteError):
                    router.catalog()


if __name__ == "__main__":
    unittest.main()
