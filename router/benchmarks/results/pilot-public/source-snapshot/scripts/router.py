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
import feedback
import policy
try:
    import model_registry
except ImportError:  # permits existing installations to upgrade one file at a time
    model_registry = None

DEFAULT = {"mode": "auto", "preset": "balanced", "timeout": 60, "overrides": {},
           "profile": "balanced", "workload": "mixed"}
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


class Catalog(dict):
    def __init__(self, models, metadata):
        super().__init__(models)
        self.metadata = metadata


def catalog():
    """Read advertised capabilities, never a guessed pricing or availability table."""
    try:
        r = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True, timeout=20)
        if r.returncode:
            raise ValueError("catalog command failed")
        raw = json.loads(r.stdout)["models"]
        result = {}
        for m in raw:
            if m.get("slug") in {"codex-auto-review"}:
                continue
            result[m["slug"]] = [x["effort"] for x in m["supported_reasoning_levels"]]
        if not result:
            raise ValueError("empty")
        cache = Path(os.environ.get("ADAPTIVE_ROUTER_HOME", Path.home() / ".codex/adaptive-router")) / "catalog.json"
        previous = None
        try:
            previous = json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
        removed = sorted(set((previous or {}).get("models", {})) - set(result))
        metadata = {"hash": policy.digest(result), "observed_at": time.time(),
                    "status": "reduced" if removed else "fresh", "removed_since_previous": removed,
                    "source": "codex debug models", "fallback": False}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = cache.with_name("catalog." + str(uuid.uuid4()) + ".tmp")
            temp.write_text(json.dumps({"models": result, "metadata": metadata}))
            os.chmod(temp, 0o600)
            os.replace(str(temp), str(cache))
        except OSError:
            metadata["cache_write"] = "unavailable"
        return Catalog(result, metadata)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        cache = Path(os.environ.get("ADAPTIVE_ROUTER_HOME", Path.home() / ".codex/adaptive-router")) / "catalog.json"
        try:
            cached = json.loads(cache.read_text())
            age = time.time() - cached["metadata"]["observed_at"]
            if not 0 <= age <= 86400 or not cached["models"]:
                raise ValueError("expired catalog")
            return Catalog(cached["models"], {**cached["metadata"], "status": "stale",
                           "fallback": True, "cache_age_seconds": round(age, 1),
                           "warning": "目录暂不可读，使用24小时内缓存；宿主权限没有扩大"})
        except (OSError, ValueError, KeyError, TypeError):
            raise RouteError("模型目录不可用；保留当前模型，不猜测或静默替换。") from e


def host_models():
    allowed = set(HOST_SPAWN_MODELS)
    if model_registry is not None and hasattr(model_registry, "host_models"):
        extra = model_registry.host_models()
        allowed.update(extra.keys() if isinstance(extra, dict) else extra)
    return allowed


def host_capability_snapshot():
    pairs = {m: list(EFFORTS if m != "gpt-6-luna" else EFFORTS[:-1]) for m in HOST_SPAWN_MODELS}
    if model_registry is not None:
        pairs.update(model_registry.host_models())
    return {"models": pairs, "observed_at": time.time(), "source": "native-tool-baseline+evidence-admitted-host",
            "hash": policy.digest(pairs)}


def _host_snapshot(value=None):
    value = host_capability_snapshot() if value is None else value
    pairs = value.get("models") if isinstance(value, dict) else None
    if not isinstance(pairs, dict) or value.get("hash") != policy.digest(pairs) or value.get("source") != "native-tool-baseline+evidence-admitted-host":
        raise RouteError("宿主能力快照缺少来源或hash校验失败。")
    if any(not isinstance(levels, list) or any(e not in EFFORTS for e in levels) for levels in pairs.values()):
        raise RouteError("宿主能力快照包含无效efforts。")
    return value


def validate_pair(model, effort, models, available_models=None, host_snapshot=None):
    effort = ALIASES.get(effort, effort)
    host = _host_snapshot(host_snapshot)
    authorized = set(host["models"])
    available = authorized if available_models is None else set(available_models) & authorized
    if model not in available:
        raise RouteError("宿主 spawn 工具不可用模型：" + model)
    if model not in models:
        raise RouteError("模型未出现在本机目录中：" + model)
    if effort not in models[model]:
        raise RouteError(f"{model} 不支持 {effort}；支持：{', '.join(models[model])}")
    if effort not in host["models"].get(model, []):
        raise RouteError(f"宿主证据未准入 {model} {effort}")
    return {"model": model, "reasoning_effort": effort}


def _dynamic_candidates(difficulty, require_astra=False):
    score = difficulty["score"]
    kind = difficulty["task_type"]
    astra_required = require_astra or score >= 8.5 or (
        score >= 7 and kind in ("architecture", "reason", "debug"))
    if astra_required:
        effort = "xhigh" if score < 8.5 else "max" if score < 9.5 else "ultra"
        return [("gpt-6-astra", effort)], True
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
    return candidates, False


def select(config, phase, complexity, risk, plan_ready, failures, models,
           difficulty=None, available_models=None, *, snapshot=None, profile=None,
           history=None, constraints=None, workload=None, host_snapshot=None):
    if config.get("mode") not in MODES or config.get("preset") not in PRESETS:
        raise RouteError("无效的 mode 或 preset。")
    if (profile or config.get("profile", "balanced")) not in policy.PROFILES or (workload or config.get("workload", "mixed")) not in policy.WORKLOADS:
        raise RouteError("无效的 profile 或 workload。")
    host_snapshot = _host_snapshot(host_snapshot)
    if snapshot is not None and model_registry is not None:
        try:
            model_registry.validate_snapshot(snapshot)
        except ValueError as e:
            raise RouteError("模型快照无效：" + str(e)) from e
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
    policy_result = None
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
            candidates, require_safety = _dynamic_candidates(
                {**difficulty, "score": selection_score}, require_astra=risk == "high" or failures >= 2)
            authorized = set(host_snapshot["models"])
            available = authorized if available_models is None else set(available_models) & authorized
            if snapshot is None and model_registry is not None:
                snapshot = model_registry.bundled_snapshot()
            try:
                effective_models = {m: [e for e in levels if e in host_snapshot["models"].get(m, [])] for m, levels in models.items()}
                policy_result = policy.rank(candidates, effective_models, available, snapshot=snapshot,
                                            profile=profile or config.get("profile", "balanced"),
                                            workload=workload or (config.get("workload") if config.get("workload") != "mixed" else None) or policy.workload_for(difficulty["task_type"]),
                                            constraints=constraints, history=history,
                                            require_safety=require_safety,
                                            required_tier="frontier" if require_safety else "standard" if selection_score >= 4.5 else "light")
                policy_result["host_hash"] = host_snapshot["hash"]
            except ValueError as e:
                raise RouteError(str(e)) from e
            ranked = policy_result["candidates"]
            if not ranked:
                if require_safety:
                    raise RouteError(f"该工作块需要 gpt-6-astra {candidates[0][1]} 或已验证同等安全能力；没有合格宿主组合。")
                raise RouteError("模型目录与宿主能力交集中，没有满足工具/上下文与准入约束的组合。")
            model, effort = ranked[0]["model"], ranked[0]["reasoning_effort"]
            preferred = candidates[0]
            fallback = (f"首选 {preferred[0]} {preferred[1]}；回退 {model} {effort}（能力约束或本地策略排名）") if (model, effort) != preferred else None
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
    if constraints and (override or config["mode"] == "preset"):
        if snapshot is None and model_registry is not None:
            snapshot = model_registry.bundled_snapshot()
        try:
            check = policy.rank([(model, effort)], models, set(host_snapshot["models"]), snapshot=snapshot,
                                profile=profile or config.get("profile", "balanced"),
                                workload=workload or policy.workload_for((difficulty or {}).get("task_type", "general")),
                                constraints=constraints)
        except ValueError as e:
            raise RouteError(str(e)) from e
        if not any(c["model"] == model and c["reasoning_effort"] == effort for c in check["candidates"]):
            raise RouteError("用户指定模型不能满足已声明的工具或上下文约束。")
    return {"requested_phase": phase, "phase": effective,
            **validate_pair(model, effort, models, available_models, host_snapshot), "reason": reason,
            "requires_strong_review": phase == "act" and risk == "high",
            "difficulty": difficulty, "selection_score": selection_score,
            "policy": policy_result or {"version": policy.VERSION, "profile": profile or config.get("profile", "balanced"),
                                        "source": "explicit_pin" if override else "fixed_preset"}}


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
            feedback.install(db)
            db.execute("BEGIN IMMEDIATE")
            yield db

    def config(self, phase_override=None, **updates):
        with self.connect() as db:
            row = db.execute("SELECT data FROM config WHERE id=1").fetchone()
            cfg = {**json.loads(json.dumps(DEFAULT)), **(json.loads(row[0]) if row else {})}
            if updates.get("profile", cfg["profile"]) not in policy.PROFILES or updates.get("workload", cfg["workload"]) not in policy.WORKLOADS:
                raise RouteError("无效的 profile 或 workload。")
            cfg.update(updates)
            if phase_override:
                phase, pair = phase_override
                cfg["overrides"][phase] = pair
            if updates or phase_override:
                db.execute("INSERT OR REPLACE INTO config VALUES (1, ?)", (json.dumps(cfg),))
            return cfg

    def propose(self, thread, phase, complexity, risk, plan_ready, failures, models,
                mode=None, preset=None, timeout=None, task='', task_type='general',
                difficulty_score=None, score_source='heuristic', available_models=None, *,
                profile=None, workload=None, constraints=None, snapshot=None, learning=True):
        cfg = self.config()
        for key, value in (("mode", mode), ("preset", preset), ("timeout", timeout), ("profile", profile), ("workload", workload)):
            if value is not None:
                cfg[key] = value
        try:
            rating = assess(text=task, phase=phase, complexity=complexity, risk=risk,
                            failures=failures, score=difficulty_score, task_type=task_type,
                            source=score_source)
        except ValueError as e:
            raise RouteError(str(e)) from e
        selected_workload = cfg.get("workload") if cfg.get("workload") != "mixed" else policy.workload_for(rating["task_type"])
        effective = "think" if failures >= 2 or (phase == "act" and not plan_ready and rating["task_type"] not in ("read", "extract")) else phase
        cohort = policy.cohort(rating, effective, selected_workload)
        with self.connect() as db:
            history = feedback.history(db, cohort) if learning else {}
        if snapshot is None and model_registry is not None:
            snapshot = model_registry.load_snapshot(home=self.root)
        host_snapshot = host_capability_snapshot()
        chosen = select(cfg, phase, complexity, risk, plan_ready, failures, models,
                        rating, available_models, snapshot=snapshot, history=history,
                        constraints=constraints, workload=selected_workload, host_snapshot=host_snapshot)
        catalog_metadata = getattr(models, "metadata", None) or {"hash": policy.digest(models),
                            "observed_at": time.time(), "status": "caller_supplied", "fallback": False}
        record = {"id": str(uuid.uuid4()), "thread_id": thread, "created": time.time(),
                  "config": cfg, "inputs": {"phase": phase, "complexity": complexity, "risk": risk,
                  "plan_ready": plan_ready, "failures": failures, "difficulty": rating,
                  "available_models": sorted(set(available_models) & host_models()) if available_models is not None else None}, "route": chosen,
                  "status": "awaiting_question" if cfg["mode"] == "ask" else "selected",
                  "selection_source": "pending" if cfg["mode"] == "ask" else cfg["mode"],
                  "execution": "not_started", "agent_id": None, "turn_id": None, "outcome": None,
                  "catalog": catalog_metadata, "registry": {k: (snapshot or {}).get(k) for k in ("hash", "fetched_at", "cache_status", "stale")},
                  "host": host_snapshot,
                  "cohort": cohort, "learning": bool(learning)}
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

    def update(self, rid, thread, action, models=None, choice=None, model=None, effort=None, evidence=None,
               agent_id=None, turn_id=None):
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
                    if choice == "recommended":
                        validate_pair(r["route"]["model"], r["route"]["reasoning_effort"], models, r["inputs"].get("available_models"))
                    else:
                        r["route"] = select(cfg, models=models, **r["inputs"])
                    r.update(status="selected", selection_source="user")
                elif r["status"] == "pending":
                    # Clock shifts or reboot cannot silently consume the user's response window.
                    offset = time.time()-monotonic()
                    if abs(offset-r["clock_offset"]) > 5:
                        r.update(status="awaiting_question")
                        r.pop("deadline", None)
                    elif monotonic() >= r["monotonic_deadline"]:
                        # Timeout selects the frozen recommendation. It cannot
                        # observe new feedback/catalog data and change its mind.
                        validate_pair(r["route"]["model"], r["route"]["reasoning_effort"], models, r["inputs"].get("available_models"))
                        r.update(status="selected", selection_source="timeout")
            elif action == "mark":
                if r["status"] != "selected":
                    raise RouteError("只有已选择的路由可以记录执行。")
                if r["execution"] != "not_started":
                    raise RouteError("此路由已记录执行；不要重复派发。")
                try:
                    feedback.identifier(agent_id, "agent_id")
                    feedback.identifier(turn_id, "turn_id")
                except ValueError as e:
                    raise RouteError(str(e)) from e
                if (not evidence and not agent_id and not turn_id) or (evidence and len(evidence) > 200):
                    raise RouteError("仅记录实际返回的 agent ID 或 live publication 状态（最多 200 字符）。")
                r.update(execution="dispatched", evidence=evidence, agent_id=agent_id, turn_id=turn_id)
            elif action == "live_result":
                if evidence not in ("applied", "targetUnavailable", "unknown") or r["execution"] != "dispatched":
                    raise RouteError("无对应的 live publication 请求。")
                r.update(execution="live_"+evidence, evidence=evidence)
            elif action != "get":
                raise RouteError("未知状态操作。")
            db.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(r), rid))
        return public(r)

    def outcome(self, rid, thread, status, **fields):
        try:
            result = feedback.outcome(status, **fields)
        except ValueError as e:
            raise RouteError(str(e)) from e
        payload = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.connect() as db:
            receipt = db.execute("SELECT thread,payload FROM outcome_receipts WHERE decision_id=?", (rid,)).fetchone()
            if receipt:
                if receipt != (thread, payload):
                    raise RouteError("验收记录已存在，冲突反馈被拒绝。")
                row = db.execute("SELECT data FROM decisions WHERE id=? AND thread=?", (rid, thread)).fetchone()
                return public(json.loads(row[0])) if row else {"id": rid, "outcome": result, "idempotent": True, "decision_pruned": True}
            row = db.execute("SELECT data FROM decisions WHERE id=? AND thread=?", (rid, thread)).fetchone()
            if not row:
                raise RouteError("路由不存在或不属于当前任务。")
            record = json.loads(row[0])
            if record["execution"] == "not_started":
                raise RouteError("尚未记录实际执行，不能记录验收。")
            record["outcome"] = {**result, "recorded_at": time.time()}
            # Unobserved actual model/effort cannot be attributed to the selected
            # route. Unknown and benchmark results never improve acceptance.
            if record.get("learning", True) and result["actual_model"] and result["actual_effort"]:
                feedback.accumulate(db, record["cohort"], result["actual_model"], result["actual_effort"], result)
            db.execute("INSERT INTO outcome_receipts VALUES(?,?,?,?)", (rid, thread, payload, time.time()))
            db.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(record), rid))
        return public(record)

    def history(self, cohort):
        with self.connect() as db:
            return feedback.history(db, cohort)


def public(r):
    result = {k: v for k, v in r.items() if k not in ("monotonic_deadline", "clock_offset")}
    if r["status"] == "pending":
        result["remaining_seconds"] = max(0, int(r["monotonic_deadline"]-monotonic()+0.999))
    if r["status"] == "selected":
        result["native_spawn_settings"] = {"model": r["route"]["model"],
                                         "reasoning_effort": r["route"]["reasoning_effort"], "fork_turns": "none"}
    result["notice"] = "路由选择不等于模型已切换；执行后以原生子代理结果或 live 接口发布结果为准。"
    result["summary"] = {"decision_id": r["id"], "phase": r["route"]["phase"],
                         "model": r["route"]["model"], "effort": r["route"]["reasoning_effort"],
                         "reason": r["route"]["reason"], "profile": r["route"].get("policy", {}).get("profile", "balanced"),
                         "policy_version": r["route"].get("policy", {}).get("version"),
                         "status": r["status"], "execution": r["execution"],
                         "acceptance": (r.get("outcome") or {}).get("status", "unknown"),
                         "agent_id": r.get("agent_id"), "turn_id": r.get("turn_id"),
                         "catalog_status": r.get("catalog", {}).get("status")}
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
    c.add_argument("--profile", choices=policy.PROFILES)
    c.add_argument("--priority", choices=("accuracy", "balance", "cost", "latency"), help="Answer to the optional preference question")
    c.add_argument("--workload", choices=policy.WORKLOADS)
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
    a.add_argument("--profile", choices=policy.PROFILES)
    a.add_argument("--workload", choices=policy.WORKLOADS)
    a.add_argument("--context-tokens", type=int, default=0)
    a.add_argument("--require-tools", action="store_true", help="Require verified model tool-calling support")
    a.add_argument("--no-learning", action="store_true", help="Freeze an uncalibrated prior and exclude outcomes from production learning")
    for name in ("arm", "resolve", "get", "mark"):
        a = sub.add_parser(name)
        a.add_argument("--id", required=True)
        a.add_argument("--thread-id")
        if name == "resolve":
            a.add_argument("--choice", choices=("recommended", "custom", "cancel")+PRESETS)
            a.add_argument("--model")
            a.add_argument("--effort")
        if name == "mark":
            a.add_argument("--evidence")
            a.add_argument("--agent-id")
            a.add_argument("--turn-id")
    for name in ("outcome", "feedback"):
        a = sub.add_parser(name)
        a.add_argument("--id", required=True)
        a.add_argument("--thread-id")
        a.add_argument("--status", choices=feedback.STATUSES, required=True)
        a.add_argument("--failure", choices=feedback.FAILURES)
        a.add_argument("--actual-model")
        a.add_argument("--actual-effort", choices=("none", "minimal") + EFFORTS)
        a.add_argument("--usage-json", default="{}")
        a.add_argument("--cost-usd", type=float)
        a.add_argument("--latency-ms", type=float)
        a.add_argument("--verification-source", choices=feedback.SOURCES, default="unknown")
        a.add_argument("--scope", choices=feedback.SCOPES, default="production")
        a.add_argument("--evidence-id")
    args = p.parse_args()
    try:
        if getattr(args, "timeout", None) is not None and not 5 <= args.timeout <= 3600:
            raise RouteError("超时须为 5–3600 秒；默认 60 秒。")
        if getattr(args, "failures", 0) < 0:
            raise RouteError("失败次数不能为负数。")
        if args.command == "models":
            models = catalog()
            result = {"models": models, "catalog": models.metadata, "host_spawn_models": sorted(host_models()),
                      "source": "codex debug models + evidence-bounded native spawn host allowlist", "pricing": "价格来自模型注册表；未估算任务费用或节省比例"}
        elif args.command == "presets":
            result = MATRIX
        elif args.command == "config":
            s = Store()
            updates = {k: getattr(args,k) for k in ("mode", "preset", "timeout", "profile", "workload") if getattr(args,k) is not None}
            answers = policy.preference_answers(args.priority, args.workload)
            if args.profile and answers.get("profile") and args.profile != answers["profile"]:
                raise RouteError("--profile 与 --priority 的选择冲突。")
            updates.update(answers)
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
                                     args.available_models.split(",") if args.available_models else None,
                                     profile=args.profile, workload=args.workload,
                                     constraints={"context_tokens": args.context_tokens, "tools": args.require_tools},
                                     learning=not args.no_learning,
                                     snapshot=model_registry.load_snapshot(refresh=True) if model_registry is not None else None)
        elif args.command in ("outcome", "feedback"):
            result = Store().outcome(args.id, thread_id(args.thread_id), args.status,
                                     failure=args.failure, actual_model=args.actual_model, actual_effort=args.actual_effort,
                                     usage=json.loads(args.usage_json), cost=args.cost_usd, latency_ms=args.latency_ms,
                                     verification_source=args.verification_source, scope=args.scope,
                                     evidence_id=args.evidence_id)
        else:
            result = Store().update(args.id, thread_id(args.thread_id), args.command,
                                    models=catalog() if args.command == "resolve" else None,
                                    choice=getattr(args,"choice",None), model=getattr(args,"model",None),
                                    effort=getattr(args,"effort",None), evidence=getattr(args,"evidence",None),
                                    agent_id=getattr(args,"agent_id",None), turn_id=getattr(args,"turn_id",None))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RouteError, sqlite3.Error, ValueError) as e:
        print(json.dumps({"error": str(e), "model_switched": False}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
