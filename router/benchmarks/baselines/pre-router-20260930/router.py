#!/usr/bin/env python3
"""Local phase routing. Selects models; does not itself launch an agent."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from difficulty import TASK_TYPES, assess

DEFAULT = {"mode": "auto", "preset": "balanced", "timeout": 60, "overrides": {}}
MODES = ("auto", "ask", "preset")
PRESETS = ("economy", "balanced", "quality", "ultra")
AUTO_PRESET_OFFSETS = {"economy": -0.3, "balanced": 0.0, "quality": 0.4, "ultra": 0.8}
PHASES = ("think", "act", "review")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
ALIASES = {"极高": "xhigh", "最高": "max", "中": "medium", "高": "high"}
MATRIX = {
    "economy": {"think": ("gpt-6-astra", "xhigh"), "act": ("gpt-6-luna", "medium"), "review": ("gpt-6.1-sol", "high")},
    "balanced": {"think": ("gpt-6-astra", "xhigh"), "act": ("gpt-5.6-terra", "medium"), "review": ("gpt-6-astra", "xhigh")},
    "quality": {"think": ("gpt-6-astra", "max"), "act": ("gpt-6.1-sol", "high"), "review": ("gpt-6-astra", "max")},
    "ultra": {"think": ("gpt-6-astra", "ultra"), "act": ("gpt-6.1-sol", "high"), "review": ("gpt-6-astra", "max")},
}
# Models currently exposed by the native spawn tool on this host. Callers may
# narrow this set with --available-models; the CLI catalog is checked as well.
HOST_SPAWN_MODELS = frozenset(("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                               "gpt-5.6-sol", "gpt-5.6-terra"))


class RouteError(Exception):
    pass


def monotonic():
    # Python 3.9 on macOS gives time.monotonic() a per-process origin. Decisions
    # survive separate CLI invocations, so use the system clock where available.
    if hasattr(time, "CLOCK_MONOTONIC"):
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    return time.monotonic()


def catalog():
    """Read advertised capabilities, never a guessed pricing or availability table."""
    try:
        r = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True, timeout=20)
        if r.returncode:
            raise RouteError("无法读取 Codex 模型目录；请检查 codex CLI。未切换模型。")
        raw = json.loads(r.stdout)["models"]
        result = {}
        for m in raw:
            if m.get("slug") in {"codex-auto-review"}:
                continue
            result[m["slug"]] = [x["effort"] for x in m["supported_reasoning_levels"]]
        if not result:
            raise ValueError("empty")
        return result
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        raise RouteError("模型目录不可用；保留当前模型，不猜测或静默替换。") from e


def validate_pair(model, effort, models, available_models=None):
    effort = ALIASES.get(effort, effort)
    available = HOST_SPAWN_MODELS if available_models is None else set(available_models) & HOST_SPAWN_MODELS
    if model not in available:
        raise RouteError("宿主 spawn 工具不可用模型：" + model)
    if model not in models:
        raise RouteError("模型未出现在本机目录中：" + model)
    if effort not in models[model]:
        raise RouteError(f"{model} 不支持 {effort}；支持：{', '.join(models[model])}")
    return {"model": model, "reasoning_effort": effort}


def _dynamic_pair(difficulty, models, available_models, require_astra=False):
    score = difficulty["score"]
    kind = difficulty["task_type"]
    astra_required = require_astra or score >= 8.5 or (
        score >= 7 and kind in ("architecture", "reason", "debug"))
    if astra_required:
        effort = "xhigh" if score < 8.5 else "max" if score < 9.5 else "ultra"
        try:
            validate_pair("gpt-6-astra", effort, models, available_models)
        except RouteError as e:
            raise RouteError(f"该工作块需要 gpt-6-astra {effort}；不自动降为 Sol。{e}") from e
        return "gpt-6-astra", effort, None
    if score < 3:
        candidates = [("gpt-6-luna", "low" if score < 2 else "medium"),
                      ("gpt-5.6-luna", "medium"), ("gpt-5.6-terra", "medium"),
                      ("gpt-6.1-sol", "medium"), ("gpt-6-sol", "medium"),
                      ("gpt-5.6-sol", "medium"), ("gpt-6-astra", "xhigh")]
    elif score < 4.5:
        candidates = ([("gpt-5.6-terra", "medium"), ("gpt-6-luna", "medium")]
                      if kind in ("read", "extract") else
                      [("gpt-6-luna", "medium"), ("gpt-5.6-terra", "medium")])
        candidates += [("gpt-6.1-sol", "medium"), ("gpt-6-sol", "medium"),
                       ("gpt-5.6-sol", "medium"), ("gpt-6-astra", "xhigh")]
    else:
        effort = "medium" if score < 6 else "high" if score < 7 else "xhigh"
        candidates = [("gpt-6.1-sol", effort)]
        for model in ("gpt-6-sol", "gpt-5.6-sol"):
            candidates.append((model, effort))
            if effort == "xhigh":
                candidates.append((model, "high"))
        candidates.append(("gpt-6-astra", "xhigh"))
    available = HOST_SPAWN_MODELS if available_models is None else set(available_models) & HOST_SPAWN_MODELS
    for model, effort in candidates:
        if model in available and effort in models.get(model, ()):
            preferred = candidates[0]
            fallback = (f"首选 {preferred[0]} {preferred[1]} 不在目录与宿主能力交集中，"
                        f"回退 {model} {effort}") if (model, effort) != preferred else None
            return model, effort, fallback
    raise RouteError("模型目录与宿主 spawn 能力交集中，没有适合该难度的组合。")


def select(config, phase, complexity, risk, plan_ready, failures, models,
           difficulty=None, available_models=None):
    if config["mode"] != "preset":
        difficulty = difficulty or assess(phase=phase, complexity=complexity,
                                          risk=risk, failures=failures)
    effective = phase
    reason = "按预设选择阶段模型"
    if failures >= 2 or (phase == "act" and not plan_ready and config["mode"] == "preset"):
        effective = "think"
        reason = "连续失败，重新诊断" if failures >= 2 else "执行方案尚未明确"
    if config["mode"] != "preset" and phase == "act" and not plan_ready and difficulty["task_type"] not in ("read", "extract"):
        effective = "think"
        reason = "执行方案尚未明确，先分析具体工作"
    selection_score = None
    override = config["overrides"].get(effective)
    if config["mode"] == "preset":
        model, effort = MATRIX[config["preset"]][effective]
    else:
        offset = AUTO_PRESET_OFFSETS[config["preset"]]
        selection_score = round(max(1.0, min(10.0, difficulty["score"] + offset)), 1)
        score_reasons = []
        if offset:
            score_reasons.append(f"{config['preset']} 偏好 {offset:+.1f}")
        if failures >= 2 and selection_score < 7.0:
            selection_score = 7.0
            score_reasons.append("连续失败两次，路由分数至少 7.0")
        if risk == "high" and selection_score < 7.0:
            selection_score = 7.0
            score_reasons.append("高风险，路由分数至少 7.0")
        if override:
            model, effort = override["model"], override["reasoning_effort"]
            fallback = None
        else:
            model, effort, fallback = _dynamic_pair(
                {**difficulty, "score": selection_score}, models, available_models,
                require_astra=risk == "high" or failures >= 2)
        reason = (f"{effective} 阶段；{difficulty['task_type']} 原始难度 {difficulty['score']:.1f}/10，"
                  f"选模分数 {selection_score:.1f}/10，选择 {model} {effort}")
        if score_reasons:
            reason += "；" + "；".join(score_reasons)
        if fallback:
            reason += "；" + fallback
        if risk == "high" and not override:
            reason += "；高风险，要求 Astra"
        if failures >= 2:
            reason += "；连续失败，重新分析证据" + ("并交给 Astra" if not override else "")
        elif failures == 1:
            reason += "；首次失败，仅按评分所需升级"
    if override:
        model, effort = override["model"], override["reasoning_effort"]
        reason += "；使用用户指定的阶段模型"
    return {"requested_phase": phase, "phase": effective,
            **validate_pair(model, effort, models, available_models), "reason": reason,
            "requires_strong_review": phase == "act" and risk == "high",
            "difficulty": difficulty, "selection_score": selection_score}


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("ADAPTIVE_ROUTER_HOME", Path.home() / ".codex/adaptive-router"))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "state.sqlite3"

    @contextlib.contextmanager
    def connect(self):
        with sqlite3.connect(str(self.db), timeout=10) as db:
            os.chmod(self.db, 0o600)
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, thread TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def config(self, phase_override=None, **updates):
        with self.connect() as db:
            row = db.execute("SELECT data FROM config WHERE id=1").fetchone()
            cfg = json.loads(row[0]) if row else json.loads(json.dumps(DEFAULT))
            cfg.update(updates)
            if phase_override:
                phase, pair = phase_override
                cfg["overrides"][phase] = pair
            if updates or phase_override:
                db.execute("INSERT OR REPLACE INTO config VALUES (1, ?)", (json.dumps(cfg),))
            return cfg

    def propose(self, thread, phase, complexity, risk, plan_ready, failures, models,
                mode=None, preset=None, timeout=None, task='', task_type='general',
                difficulty_score=None, score_source='heuristic', available_models=None):
        cfg = self.config()
        for key, value in (("mode", mode), ("preset", preset), ("timeout", timeout)):
            if value is not None:
                cfg[key] = value
        try:
            rating = assess(text=task, phase=phase, complexity=complexity, risk=risk,
                            failures=failures, score=difficulty_score, task_type=task_type,
                            source=score_source)
        except ValueError as e:
            raise RouteError(str(e)) from e
        chosen = select(cfg, phase, complexity, risk, plan_ready, failures, models,
                        rating, available_models)
        record = {"id": str(uuid.uuid4()), "thread_id": thread, "created": time.time(),
                  "config": cfg, "inputs": {"phase": phase, "complexity": complexity, "risk": risk,
                  "plan_ready": plan_ready, "failures": failures, "difficulty": rating,
                  "available_models": sorted(set(available_models) & HOST_SPAWN_MODELS) if available_models is not None else None}, "route": chosen,
                  "status": "awaiting_question" if cfg["mode"] == "ask" else "selected",
                  "selection_source": "pending" if cfg["mode"] == "ask" else cfg["mode"],
                  "execution": "not_started"}
        with self.connect() as db:
            # A new phase invalidates old pending questions in the same task only.
            for rid, data in db.execute("SELECT id,data FROM decisions WHERE thread=?", (thread,)).fetchall():
                old = json.loads(data)
                if old["status"] in ("awaiting_question", "pending", "selected") and old["execution"] == "not_started":
                    old["status"] = "superseded"
                    db.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(old), rid))
            db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)",
                       (record["id"], thread, record["created"], json.dumps(record)))
            db.execute("DELETE FROM decisions WHERE created < ?", (time.time() - 7 * 86400,))
        return public(record)

    def update(self, rid, thread, action, models=None, choice=None, model=None, effort=None, evidence=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM decisions WHERE id=? AND thread=?", (rid, thread)).fetchone()
            if not row:
                raise RouteError("路由不存在或不属于当前任务。")
            r = json.loads(row[0])
            if action == "arm":
                if r["status"] == "awaiting_question":
                    r.update(status="pending", deadline=time.time()+r["config"]["timeout"],
                             monotonic_deadline=monotonic()+r["config"]["timeout"],
                             clock_offset=time.time()-monotonic())
            elif action == "resolve":
                if choice == "cancel" and r["status"] in ("awaiting_question", "pending", "selected") and r["execution"] == "not_started":
                    r.update(status="cancelled", selection_source="user")
                elif r["status"] not in ("awaiting_question", "pending"):
                    if choice:
                        raise RouteError("路由已结束；请为新选择重新 propose。")
                elif choice:
                    cfg = dict(r["config"])
                    if choice in PRESETS:
                        cfg.update(mode="preset", preset=choice, overrides={})
                    elif choice == "custom":
                        if not model or not effort:
                            raise RouteError("自定义选择须同时指定 --model 和 --effort。")
                        cfg = {**cfg, "overrides": {**cfg["overrides"], r["route"]["phase"]:
                               validate_pair(model, effort, models, r["inputs"].get("available_models"))}}
                    elif choice != "recommended":
                        raise RouteError("未知选择。")
                    r["route"] = select(cfg, models=models, **r["inputs"])
                    r.update(status="selected", selection_source="user")
                elif r["status"] == "pending":
                    # Clock shifts or reboot cannot silently consume the user's response window.
                    offset = time.time()-monotonic()
                    if abs(offset-r["clock_offset"]) > 5:
                        r.update(status="awaiting_question")
                        r.pop("deadline", None)
                    elif monotonic() >= r["monotonic_deadline"]:
                        r["route"] = select(r["config"], models=models, **r["inputs"])
                        r.update(status="selected", selection_source="timeout")
            elif action == "mark":
                if r["status"] != "selected":
                    raise RouteError("只有已选择的路由可以记录执行。")
                if r["execution"] != "not_started":
                    raise RouteError("此路由已记录执行；不要重复派发。")
                if not evidence or len(evidence) > 200:
                    raise RouteError("仅记录实际返回的 agent ID 或 live publication 状态（最多 200 字符）。")
                r.update(execution="dispatched", evidence=evidence)
            elif action == "live_result":
                if evidence not in ("applied", "targetUnavailable", "unknown") or r["execution"] != "dispatched":
                    raise RouteError("无对应的 live publication 请求。")
                r.update(execution="live_"+evidence, evidence=evidence)
            elif action != "get":
                raise RouteError("未知状态操作。")
            db.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(r), rid))
        return public(r)


def public(r):
    result = {k: v for k, v in r.items() if k not in ("monotonic_deadline", "clock_offset")}
    if r["status"] == "pending":
        result["remaining_seconds"] = max(0, int(r["monotonic_deadline"]-monotonic()+0.999))
    if r["status"] == "selected":
        result["native_spawn_settings"] = {"model": r["route"]["model"],
                                         "reasoning_effort": r["route"]["reasoning_effort"], "fork_turns": "none"}
    result["notice"] = "路由选择不等于模型已切换；执行后以原生子代理结果或 live 接口发布结果为准。"
    return result


def thread_id(value):
    value = value or os.environ.get("CODEX_THREAD_ID")
    if not value:
        raise RouteError("缺少当前任务 ID：设置 --thread-id 或在 Codex 任务内运行。")
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise RouteError("任务 ID 必须为 UUID。") from e


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("models")
    sub.add_parser("presets")
    c = sub.add_parser("config")
    c.add_argument("--mode", choices=MODES)
    c.add_argument("--preset", choices=PRESETS)
    c.add_argument("--timeout", type=int)
    c.add_argument("--phase", choices=PHASES)
    c.add_argument("--model")
    c.add_argument("--effort")
    c.add_argument("--clear-overrides", action="store_true")
    a = sub.add_parser("propose")
    a.add_argument("--phase", choices=PHASES, required=True)
    a.add_argument("--complexity", choices=("simple", "normal", "hard"), default="normal")
    a.add_argument("--risk", choices=("low", "medium", "high"), default="low")
    a.add_argument("--plan-ready", action="store_true")
    a.add_argument("--failures", type=int, default=0)
    a.add_argument("--task", default="", help="Task text used only in memory for heuristic features")
    a.add_argument("--task-type", choices=TASK_TYPES, default="general")
    a.add_argument("--difficulty", help="Caller-assessed score, 1.0–10.0 in 0.1 steps")
    a.add_argument("--score-source", choices=("heuristic", "caller_assessment"), default="heuristic")
    a.add_argument("--available-models", help="Comma-separated native spawn model allowlist")
    a.add_argument("--mode", choices=MODES)
    a.add_argument("--preset", choices=PRESETS)
    a.add_argument("--timeout", type=int)
    a.add_argument("--thread-id")
    for name in ("arm", "resolve", "get", "mark"):
        a = sub.add_parser(name)
        a.add_argument("--id", required=True)
        a.add_argument("--thread-id")
        if name == "resolve":
            a.add_argument("--choice", choices=("recommended", "custom", "cancel")+PRESETS)
            a.add_argument("--model")
            a.add_argument("--effort")
        if name == "mark":
            a.add_argument("--evidence", required=True)
    args = p.parse_args()
    try:
        if getattr(args, "timeout", None) is not None and not 5 <= args.timeout <= 3600:
            raise RouteError("超时须为 5–3600 秒；默认 60 秒。")
        if getattr(args, "failures", 0) < 0:
            raise RouteError("失败次数不能为负数。")
        if args.command == "models":
            result = {"models": catalog(), "host_spawn_models": sorted(HOST_SPAWN_MODELS),
                      "source": "codex debug models + native spawn host allowlist", "pricing": "未估算价格或节省比例"}
        elif args.command == "presets":
            result = MATRIX
        elif args.command == "config":
            s = Store()
            updates = {k: getattr(args,k) for k in ("mode", "preset", "timeout") if getattr(args,k) is not None}
            if args.clear_overrides:
                updates["overrides"] = {}
            override = None
            if any((args.phase, args.model, args.effort)):
                if not all((args.phase, args.model, args.effort)):
                    raise RouteError("自定义阶段须同时指定 --phase、--model、--effort。")
                override = (args.phase, validate_pair(args.model, args.effort, catalog()))
            result = s.config(phase_override=override, **updates)
        elif args.command == "propose":
            result = Store().propose(thread_id(args.thread_id), args.phase, args.complexity, args.risk,
                                     args.plan_ready, args.failures, catalog(), args.mode, args.preset, args.timeout,
                                     args.task, args.task_type, args.difficulty, args.score_source,
                                     args.available_models.split(",") if args.available_models else None)
        else:
            result = Store().update(args.id, thread_id(args.thread_id), args.command,
                                    models=catalog() if args.command == "resolve" else None,
                                    choice=getattr(args,"choice",None), model=getattr(args,"model",None),
                                    effort=getattr(args,"effort",None), evidence=getattr(args,"evidence",None))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RouteError, sqlite3.Error) as e:
        print(json.dumps({"error": str(e), "model_switched": False}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
