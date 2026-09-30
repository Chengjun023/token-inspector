import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router
import live
import difficulty

MODELS = {"gpt-6.1-sol": list(router.EFFORTS), "gpt-5.6-sol": list(router.EFFORTS),
          "gpt-6-astra": list(router.EFFORTS), "gpt-6-sol": list(router.EFFORTS),
          "gpt-5.6-terra": list(router.EFFORTS), "gpt-6-luna": list(router.EFFORTS[:-1])}
T1 = "00000000-0000-4000-8000-000000000001"
T2 = "00000000-0000-4000-8000-000000000002"


class Routes(unittest.TestCase):
    def route(self, phase="act", complexity="normal", risk="low", plan=True, failures=0, **cfg):
        c = copy.deepcopy(router.DEFAULT)
        c.update(cfg)
        return router.select(c, phase, complexity, risk, plan, failures, MODELS)

    def test_general_implementation_uses_luna(self):
        r = self.route()
        self.assertEqual((r["model"], r["reasoning_effort"]), ("gpt-6-luna", "medium"))

    def test_simple_implementation_uses_luna(self):
        self.assertEqual(self.route(complexity="simple")["model"], "gpt-6-luna")

    def test_general_work_without_plan_returns_to_think(self):
        r = self.route(plan=False)
        self.assertEqual((r["phase"], r["model"]), ("think", "gpt-6-luna"))

    def test_failure_escalation_is_bounded(self):
        self.assertEqual(self.route(failures=1)["model"], "gpt-6.1-sol")
        self.assertEqual(self.route(failures=2)["phase"], "think")

    def test_hard_general_reasoning_uses_score(self):
        self.assertEqual(self.route(phase="think", complexity="hard")["reasoning_effort"], "medium")

    def test_ultra_is_explicit_preset(self):
        self.assertEqual(self.route(phase="think", preset="ultra", mode="preset")["reasoning_effort"], "ultra")

    def test_preset_is_fixed_with_clear_plan(self):
        r = self.route(complexity="simple", mode="preset")
        self.assertEqual(r["model"], "gpt-5.6-terra")

    def test_high_risk_requires_review(self):
        r = self.route(risk="high")
        self.assertTrue(r["requires_strong_review"])
        self.assertEqual(self.route(phase="review", risk="high")["reasoning_effort"], "xhigh")

    def test_user_override_preserved(self):
        r = self.route(overrides={"act": {"model": "gpt-6-sol", "reasoning_effort": "high"}})
        self.assertEqual(r["model"], "gpt-6-sol")

    def test_missing_model_not_silently_substituted(self):
        with self.assertRaises(router.RouteError):
            router.validate_pair("unknown", "high", MODELS)

    def test_luna_ultra_rejected(self):
        with self.assertRaises(router.RouteError):
            router.validate_pair("gpt-6-luna", "ultra", MODELS)

    def test_effort_alias(self):
        self.assertEqual(router.validate_pair("gpt-6-astra", "最高", MODELS)["reasoning_effort"], "max")

    def test_host_allowlist_rejects_catalog_only_model(self):
        with self.assertRaises(router.RouteError):
            router.validate_pair("gpt-5.6-luna", "medium", {**MODELS, "gpt-5.6-luna": ["medium"]})

    def test_dynamic_score_boundaries(self):
        c = copy.deepcopy(router.DEFAULT)
        for score, expected in ((1.0, ("gpt-6-luna", "low")), (2.9, ("gpt-6-luna", "medium")),
                                (4.4, ("gpt-6-luna", "medium")), (4.5, ("gpt-6.1-sol", "medium")),
                                (6.9, ("gpt-6.1-sol", "high")), (7.0, ("gpt-6.1-sol", "xhigh")),
                                (8.5, ("gpt-6-astra", "max")), (9.5, ("gpt-6-astra", "ultra"))):
            with self.subTest(score=score):
                d = difficulty.assess(score=score)
                r = router.select(c, "act", "normal", "low", True, 0, MODELS, d)
                self.assertEqual((r["model"], r["reasoning_effort"]), expected)

    def test_reading_middle_score_uses_terra(self):
        d = difficulty.assess(task_type="read", score=3.6)
        self.assertEqual(router.select(copy.deepcopy(router.DEFAULT), "think", "normal", "low", False, 0, MODELS, d)["model"], "gpt-5.6-terra")

    def test_default_read_and_simple_extract_split(self):
        read = difficulty.assess(task_type="read")
        extract = difficulty.assess(task_type="extract", complexity="simple")
        self.assertEqual((read["score"], extract["score"]), (3.2, 2.6))
        self.assertEqual(router.select(copy.deepcopy(router.DEFAULT), "think", "normal", "low", False, 0, MODELS, read)["model"], "gpt-5.6-terra")
        self.assertEqual(router.select(copy.deepcopy(router.DEFAULT), "act", "simple", "low", False, 0, MODELS, extract)["model"], "gpt-6-luna")

    def test_failure_floor_preserves_raw_score(self):
        d = difficulty.assess(task_type="read", score=1.0)
        r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", True, 2, MODELS, d)
        self.assertEqual((r["phase"], r["model"], r["reasoning_effort"]), ("think", "gpt-6-astra", "xhigh"))
        self.assertEqual((r["difficulty"]["score"], r["selection_score"]), (1.0, 7.0))
        self.assertIn("连续失败两次，路由分数至少 7.0", r["reason"])

    def test_high_risk_floor_and_override(self):
        d = difficulty.assess(task_type="read", score=1.0)
        cfg = copy.deepcopy(router.DEFAULT)
        cfg["preset"] = "economy"
        r = router.select(cfg, "act", "normal", "high", True, 0, MODELS, d)
        self.assertEqual((r["selection_score"], r["model"]), (7.0, "gpt-6-astra"))
        self.assertIn("高风险，路由分数至少 7.0", r["reason"])
        cfg["overrides"] = {"act": {"model": "gpt-6-luna", "reasoning_effort": "medium"}}
        overridden = router.select(cfg, "act", "normal", "high", True, 0, MODELS, d)
        self.assertEqual((overridden["selection_score"], overridden["model"]), (7.0, "gpt-6-luna"))

    def test_auto_preset_offsets_change_boundary_without_changing_rating(self):
        d = difficulty.assess(task_type="general", score=4.6)
        observed = {}
        for preset in router.PRESETS:
            cfg = copy.deepcopy(router.DEFAULT)
            cfg["preset"] = preset
            r = router.select(cfg, "act", "normal", "low", True, 0, MODELS, d)
            observed[preset] = (r["selection_score"], r["model"], r["difficulty"]["score"])
        self.assertEqual(observed, {"economy": (4.3, "gpt-6-luna", 4.6),
                                    "balanced": (4.6, "gpt-6.1-sol", 4.6),
                                    "quality": (5.0, "gpt-6.1-sol", 4.6),
                                    "ultra": (5.4, "gpt-6.1-sol", 4.6)})

    def test_catalog_and_host_fallback(self):
        d = difficulty.assess(task_type="code", score=8.0)
        models = {k: v for k, v in MODELS.items() if k != "gpt-6.1-sol"}
        r = router.select(copy.deepcopy(router.DEFAULT), "think", "hard", "low", False, 0, models, d)
        self.assertEqual((r["model"], r["reasoning_effort"]), ("gpt-6-sol", "xhigh"))
        self.assertIn("首选 gpt-6.1-sol xhigh", r["reason"])
        self.assertIn("回退 gpt-6-sol xhigh", r["reason"])
        with self.assertRaises(router.RouteError):
            router.select(copy.deepcopy(router.DEFAULT), "think", "hard", "low", False, 0, MODELS, d, ["gpt-5.6-luna"])

    def test_all_threshold_edges(self):
        expected = [(1.0, "gpt-6-luna", "low"), (1.9, "gpt-6-luna", "low"),
                    (2.0, "gpt-6-luna", "medium"), (2.9, "gpt-6-luna", "medium"),
                    (3.0, "gpt-6-luna", "medium"), (4.4, "gpt-6-luna", "medium"),
                    (4.5, "gpt-6.1-sol", "medium"), (5.9, "gpt-6.1-sol", "medium"),
                    (6.0, "gpt-6.1-sol", "high"), (6.9, "gpt-6.1-sol", "high"),
                    (7.0, "gpt-6.1-sol", "xhigh"), (8.4, "gpt-6.1-sol", "xhigh"),
                    (8.5, "gpt-6-astra", "max"), (9.4, "gpt-6-astra", "max"),
                    (9.5, "gpt-6-astra", "ultra"), (10.0, "gpt-6-astra", "ultra")]
        for score, model, effort in expected:
            with self.subTest(score=score):
                r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", True, 0,
                                  MODELS, difficulty.assess(score=score))
                self.assertEqual((r["model"], r["reasoning_effort"]), (model, effort))

    def test_task_roles_and_phase_are_independent(self):
        for kind in difficulty.TASK_TYPES:
            for phase in router.PHASES:
                with self.subTest(kind=kind, phase=phase):
                    d = difficulty.assess(task_type=kind, score=7.5)
                    r = router.select(copy.deepcopy(router.DEFAULT), phase, "normal", "low", True, 0, MODELS, d)
                    expected = "gpt-6-astra" if kind in ("architecture", "reason", "debug") else "gpt-6.1-sol"
                    self.assertEqual((r["model"], r["reasoning_effort"]), (expected, "xhigh"))

    def test_read_and_extract_middle_band(self):
        for kind in ("read", "extract"):
            for score in (3.0, 4.4, 4.5):
                with self.subTest(kind=kind, score=score):
                    d = difficulty.assess(task_type=kind, score=score)
                    r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", False, 0, MODELS, d)
                    self.assertEqual(r["model"], "gpt-5.6-terra" if score < 4.5 else "gpt-6.1-sol")

    def test_safety_routes_do_not_downgrade_from_astra(self):
        for kind, score, risk, failures in (("code", 1.0, "high", 0),
                                          ("read", 1.0, "low", 2),
                                          ("architecture", 7.0, "low", 0),
                                          ("debug", 7.0, "low", 0),
                                          ("reason", 7.0, "low", 0),
                                          ("code", 8.5, "low", 0)):
            for unavailable in ("catalog", "host", "effort"):
                with self.subTest(kind=kind, unavailable=unavailable):
                    models = copy.deepcopy(MODELS)
                    available = None
                    if unavailable == "catalog":
                        del models["gpt-6-astra"]
                    elif unavailable == "host":
                        available = ["gpt-6.1-sol", "gpt-6-sol"]
                    else:
                        models["gpt-6-astra"] = ["low"]
                    with self.assertRaisesRegex(router.RouteError, "需要 gpt-6-astra"):
                        router.select(copy.deepcopy(router.DEFAULT), "act", "normal", risk, True,
                                      failures, models, difficulty.assess(task_type=kind, score=score), available)

    def test_available_model_intersection_and_effort_fallback(self):
        d = difficulty.assess(task_type="code", score=7.5)
        variants = [(MODELS, ["gpt-6-sol"], "gpt-6-sol", "xhigh"),
                    ({**MODELS, "gpt-6.1-sol": ["medium"], "gpt-6-sol": ["high"]}, None, "gpt-6-sol", "high"),
                    (MODELS, ["gpt-5.6-sol"], "gpt-5.6-sol", "xhigh"),
                    ({**MODELS, "gpt-5.6-sol": ["high"]}, ["gpt-5.6-sol"], "gpt-5.6-sol", "high"),
                    (MODELS, ["gpt-6-astra"], "gpt-6-astra", "xhigh")]
        for models, available, model, effort in variants:
            with self.subTest(model=model, effort=effort):
                r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", True, 0, models, d, available)
                self.assertEqual((r["model"], r["reasoning_effort"]), (model, effort))
                self.assertIn("首选 gpt-6.1-sol xhigh", r["reason"])

    def test_safety_and_failed_routes_preserve_explicit_pin_without_astra(self):
        models = {k: v for k, v in MODELS.items() if k != "gpt-6-astra"}
        for phase, failures in (("act", 0), ("think", 2)):
            cfg = copy.deepcopy(router.DEFAULT)
            cfg["overrides"] = {phase: {"model": "gpt-6-sol", "reasoning_effort": "high"}}
            r = router.select(cfg, "act", "normal", "high", True, failures, models,
                              difficulty.assess(task_type="code", score=1.0))
            self.assertEqual((r["model"], r["reasoning_effort"]), ("gpt-6-sol", "high"))
            self.assertTrue(r["requires_strong_review"])

    def test_fixed_presets_only_upgrade_default_sol(self):
        for preset, phase in (("economy", "review"), ("quality", "act"), ("ultra", "act")):
            with self.subTest(preset=preset, phase=phase):
                r = self.route(phase=phase, preset=preset, mode="preset")
                self.assertEqual((r["model"], r["reasoning_effort"]), ("gpt-6.1-sol", "high"))
                cfg = copy.deepcopy(router.DEFAULT)
                cfg.update(mode="preset", preset=preset,
                           overrides={phase: {"model": "gpt-6-sol", "reasoning_effort": "high"}})
                self.assertEqual(router.select(cfg, phase, "normal", "low", True, 0, MODELS)["model"], "gpt-6-sol")
        self.assertEqual(self.route(mode="preset", preset="economy")["model"], "gpt-6-luna")
        self.assertEqual(self.route(mode="preset", preset="balanced")["model"], "gpt-5.6-terra")

    def test_failure_safety_applies_even_with_high_original_score(self):
        d = difficulty.assess(task_type="code", score=8.0)
        r = router.select(copy.deepcopy(router.DEFAULT), "review", "normal", "low", True, 2, MODELS, d)
        self.assertEqual((r["phase"], r["model"], r["reasoning_effort"]), ("think", "gpt-6-astra", "xhigh"))
        self.assertEqual((r["difficulty"]["score"], r["selection_score"]), (8.0, 8.0))

    def test_sol_middle_band_fallback_keeps_supported_effort(self):
        for score, effort in ((4.5, "medium"), (6.0, "high")):
            for available, expected in ((["gpt-6-sol"], "gpt-6-sol"),
                                        (["gpt-5.6-sol"], "gpt-5.6-sol"),
                                        (["gpt-6-astra"], "gpt-6-astra")):
                with self.subTest(score=score, available=available):
                    r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", True, 0,
                                      MODELS, difficulty.assess(task_type="code", score=score), available)
                    self.assertEqual((r["model"], r["reasoning_effort"]),
                                     (expected, "xhigh" if expected == "gpt-6-astra" else effort))

    def test_user_pin_still_requires_capability_validation(self):
        cfg = copy.deepcopy(router.DEFAULT)
        cfg["overrides"] = {"act": {"model": "gpt-6.1-sol", "reasoning_effort": "ultra"}}
        with self.assertRaises(router.RouteError):
            router.select(cfg, "act", "normal", "high", True, 0,
                          {**MODELS, "gpt-6.1-sol": ["medium"]})

    def test_light_work_falls_back_to_new_sol_before_old_sol(self):
        for score in (1.0, 2.9, 3.0, 4.4):
            for kind in ("general", "read", "extract"):
                for models, available in (({"gpt-6.1-sol": list(router.EFFORTS)}, None),
                                          (MODELS, ["gpt-6.1-sol", "gpt-6-sol"])):
                    with self.subTest(score=score, kind=kind, available=available):
                        r = router.select(copy.deepcopy(router.DEFAULT), "act", "normal", "low", True, 0,
                                          models, difficulty.assess(task_type=kind, score=score), available)
                        self.assertEqual((r["model"], r["reasoning_effort"]), ("gpt-6.1-sol", "medium"))
                        self.assertIn("回退 gpt-6.1-sol medium", r["reason"])


class Difficulty(unittest.TestCase):
    def test_heuristics_and_explicit_override(self):
        self.assertLess(difficulty.assess(task_type="read")["score"], difficulty.assess(task_type="architecture")["score"])
        self.assertGreater(difficulty.assess(task_type="debug", risk="high", failures=2)["score"], difficulty.assess(task_type="debug")["score"])
        self.assertEqual(difficulty.assess(text="Please debug this error")["task_type"], "debug")
        d = difficulty.assess(text="debug", score="2.1", task_type="edit")
        self.assertEqual((d["score"], d["task_type"], d["source"]), (2.1, "edit", "caller_assessment"))
        self.assertEqual(d["confidence"], "medium")
        self.assertEqual(difficulty.assess()["confidence"], "low")
        uncertain = difficulty.assess(text="unclear requirements", task_type="code")
        self.assertGreater(uncertain["score"], difficulty.assess(task_type="code")["score"])
        self.assertIn("uncertainty_signal", uncertain["reasons"])

    def test_bad_scores_rejected(self):
        for score in ("nan", "inf", "-inf", "3.14", 0.9, 10.1, True, "abc"):
            with self.subTest(score=score), self.assertRaises(ValueError):
                difficulty.assess(score=score)


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = router.Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, thread=T1, mode="ask"):
        return self.store.propose(thread, "act", "normal", "low", True, 0, MODELS, mode=mode)

    def test_no_timeout_before_question_shown(self):
        r = self.propose()
        self.assertEqual(r["status"], "awaiting_question")
        self.assertEqual(self.store.update(r["id"],T1,"resolve",MODELS)["status"], "awaiting_question")

    def test_timeout_selects_recommendation_without_running_it(self):
        with patch("router.time.time", return_value=1000), patch("router.monotonic",return_value=500):
            r = self.propose()
            self.store.update(r["id"],T1,"arm")
        with patch("router.time.time",return_value=1059), patch("router.monotonic",return_value=559):
            self.assertEqual(self.store.update(r["id"],T1,"resolve",MODELS)["status"], "pending")
        with patch("router.time.time",return_value=1060), patch("router.monotonic",return_value=560):
            d = self.store.update(r["id"],T1,"resolve",MODELS)
            self.assertEqual((d["status"],d["selection_source"],d["execution"]),("selected","timeout","not_started"))

    def test_timeout_uses_new_sol_for_hard_ordinary_work(self):
        with patch("router.time.time", return_value=1000), patch("router.monotonic", return_value=500):
            r = self.store.propose(T1, "think", "normal", "low", True, 0, MODELS,
                                   mode="ask", task_type="code", difficulty_score=7.5)
            self.store.update(r["id"], T1, "arm")
        with patch("router.time.time", return_value=1060), patch("router.monotonic", return_value=560):
            selected = self.store.update(r["id"], T1, "resolve", MODELS)
        self.assertEqual((selected["selection_source"], selected["execution"]), ("timeout", "not_started"))
        self.assertEqual(selected["native_spawn_settings"],
                         {"model": "gpt-6.1-sol", "reasoning_effort": "xhigh", "fork_turns": "none"})

    def test_ask_custom_pin_preserves_old_sol_on_safety_route(self):
        r = self.store.propose(T1, "act", "normal", "high", True, 0, MODELS,
                               mode="ask", task_type="code", difficulty_score=1.0)
        selected = self.store.update(r["id"], T1, "resolve", MODELS, choice="custom",
                                     model="gpt-6-sol", effort="high")
        self.assertEqual((selected["route"]["model"], selected["selection_source"]), ("gpt-6-sol", "user"))
        self.assertEqual(selected["route"]["selection_score"], 7.0)

    def test_persistent_old_sol_pin_survives_default_upgrade(self):
        self.store.config(phase_override=("act", {"model": "gpt-6-sol", "reasoning_effort": "high"}))
        for mode in ("auto", "ask", "preset"):
            with self.subTest(mode=mode):
                r = self.store.propose(T1, "act", "normal", "low", True, 0, MODELS,
                                       mode=mode, task_type="code", difficulty_score=5.1)
                if mode == "ask":
                    r = self.store.update(r["id"], T1, "resolve", MODELS, choice="recommended")
                self.assertEqual((r["route"]["model"], r["route"]["reasoning_effort"]), ("gpt-6-sol", "high"))

    def test_clock_jump_requires_new_question(self):
        with patch("router.time.time",return_value=1000), patch("router.monotonic",return_value=500):
            r = self.propose()
            self.store.update(r["id"],T1,"arm")
        with patch("router.time.time",return_value=10000), patch("router.monotonic",return_value=561):
            self.assertEqual(self.store.update(r["id"],T1,"resolve",MODELS)["status"],"awaiting_question")

    def test_user_choice_before_timer(self):
        r = self.propose()
        d = self.store.update(r["id"],T1,"resolve",MODELS,choice="economy")
        self.assertEqual((d["route"]["model"],d["selection_source"]),("gpt-6-luna","user"))

    def test_cancel_does_not_dispatch(self):
        r = self.propose(mode="auto")
        d = self.store.update(r["id"],T1,"resolve",MODELS,choice="cancel")
        self.assertEqual(d["status"],"cancelled")
        with self.assertRaises(router.RouteError):
            self.store.update(r["id"],T1,"mark",evidence="agent:test")

    def test_task_isolation(self):
        r = self.propose()
        self.propose(thread=T2)
        self.assertEqual(self.store.update(r["id"],T1,"get")["status"],"awaiting_question")
        with self.assertRaises(router.RouteError):
            self.store.update(r["id"],T2,"resolve",MODELS,choice="recommended")

    def test_new_phase_supersedes_stale_decision(self):
        r = self.propose(mode="auto")
        self.propose()
        self.assertEqual(self.store.update(r["id"],T1,"get")["status"],"superseded")

    def test_double_dispatch_rejected(self):
        r = self.propose(mode="auto")
        self.store.update(r["id"],T1,"mark",evidence="agent:test")
        with self.assertRaises(router.RouteError):
            self.store.update(r["id"],T1,"mark",evidence="agent:second")

    def test_unknown_live_result_cannot_retry(self):
        r = self.propose(mode="auto")
        self.store.update(r["id"],T1,"mark",evidence="live request")
        self.store.update(r["id"],T1,"live_result",evidence="unknown")
        with self.assertRaises(router.RouteError):
            self.store.update(r["id"],T1,"mark",evidence="retry")

    def test_config_retains_unrelated_preferences(self):
        self.store.config(mode="ask")
        self.store.config(phase_override=("think",{"model":"gpt-6-astra","reasoning_effort":"max"}))
        self.store.config(phase_override=("act",{"model":"gpt-6-luna","reasoning_effort":"high"}))
        c = router.Store(self.tmp.name).config()
        self.assertEqual(c["mode"],"ask")
        self.assertEqual(len(c["overrides"]),2)

    def test_persisted_clock_uses_same_origin_across_processes(self):
        before=router.monotonic()
        child=subprocess.check_output([sys.executable,"-c","import router; print(router.monotonic())"],
                                      cwd=str(Path(router.__file__).parent),text=True)
        after=router.monotonic()
        self.assertLessEqual(before,float(child))
        self.assertLessEqual(float(child),after)

    def test_prompt_text_is_not_persisted(self):
        secret = "PRIVATE_TASK_BODY_7391"
        r = self.store.propose(T1, "think", "normal", "low", False, 0, MODELS,
                               task=f"Read {secret}", task_type="read")
        self.assertIn("difficulty", r["route"])
        self.assertNotIn(secret, self.store.db.read_bytes().decode("utf-8", errors="ignore"))

    def test_read_without_plan_and_two_failures(self):
        read = self.store.propose(T1, "act", "normal", "low", False, 0, MODELS,
                                  task_type="read")
        self.assertEqual((read["route"]["phase"], read["route"]["model"]), ("act", "gpt-5.6-terra"))
        retried = self.store.propose(T1, "act", "normal", "low", True, 2, MODELS,
                                     task_type="debug")
        self.assertEqual(retried["route"]["phase"], "think")
        self.assertEqual(retried["route"]["model"], "gpt-6-astra")
        self.assertIn("连续失败", retried["route"]["reason"])


class FakeRPC:
    def __init__(self, turn="turn1", result="applied"):
        self.calls=[]
        self.turn=turn
        self.result=result

    def call(self, method, params):
        self.calls.append((method,params))
        if method == "thread/read":
            return {"thread":{"id":T1,"status":{"type":"active"},"model":"gpt-6-astra"}}
        if method == "thread/turns/list":
            return {"data":[{"id":self.turn,"status":"inProgress"}]}
        if self.result == "timeout":
            raise router.RouteError("timeout")
        return {"status":self.result}


class Live(unittest.TestCase):
    def test_exact_target_and_minimal_mutation(self):
        with tempfile.TemporaryDirectory() as root, patch("live.catalog",return_value=MODELS):
            s=router.Store(root);r=s.propose(T1,"act","normal","low",True,0,MODELS)
            rpc=FakeRPC()
            result=live.apply(rpc,s,r["id"],T1,"turn1")
            self.assertEqual(result["status"],"applied")
            self.assertEqual(rpc.calls[-1],("turn/settings/update",{"threadId":T1,"turnId":"turn1","model":"gpt-6-luna","effort":"medium"}))
            self.assertEqual(s.update(r["id"],T1,"get")["execution"],"live_applied")

    def test_stale_turn_causes_no_mutation(self):
        with tempfile.TemporaryDirectory() as root, patch("live.catalog",return_value=MODELS):
            s=router.Store(root);r=s.propose(T1,"act","normal","low",True,0,MODELS)
            rpc=FakeRPC(turn="new-turn")
            with self.assertRaises(router.RouteError):
                live.apply(rpc,s,r["id"],T1,"old-turn")
            self.assertFalse(any(m=="turn/settings/update" for m,_ in rpc.calls))

    def test_write_timeout_persists_unknown(self):
        with tempfile.TemporaryDirectory() as root, patch("live.catalog",return_value=MODELS):
            s=router.Store(root);r=s.propose(T1,"act","normal","low",True,0,MODELS)
            with self.assertRaises(router.RouteError):
                live.apply(FakeRPC(result="timeout"),s,r["id"],T1,"turn1")
            self.assertEqual(s.update(r["id"],T1,"get")["execution"],"live_unknown")


if __name__ == "__main__":
    unittest.main()
