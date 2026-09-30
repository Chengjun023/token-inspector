#!/usr/bin/env python3
"""Read-only local monitor; a bounded task brief is classified locally, never exported."""
import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meter import Meter, aggregate_savings
from difficulty import assess

BUSY = {"running", "waiting"}
STALE_AFTER = 180


def connect(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=200")
    return connection


def fields(connection, table):
    return {r[1] for r in connection.execute("PRAGMA table_info(" + table + ")")}


def source_parent(source):
    try:
        return json.loads(source).get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
    except (ValueError, AttributeError, TypeError):
        return None


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0


def status_for(turn_status, updated, now, activity="", item_status=""):
    if turn_status in ("completed", "interrupted", "failed"):
        return {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}[turn_status]
    if turn_status == "inProgress":
        if now - updated > STALE_AFTER:
            return "stale"
        if activity in ("requestUserInput", "approval") and item_status not in ("completed", "failed"):
            return "waiting"
        return "running"
    return "unknown"


class Tail:
    """Bounded incremental fallback for older rollouts. Retains only metadata, never content."""
    def __init__(self):
        self.identity = None
        self.offset = 0
        self.partial = b""
        self.values = {}

    def read(self, path):
        stat = path.stat()
        if self.identity != stat.st_ino or stat.st_size < self.offset:
            self.identity = stat.st_ino
            self.offset = max(0, stat.st_size - 256 * 1024)
            self.partial = b""
            self.values = {}
            skip_first = self.offset > 0
        else:
            skip_first = False
        if stat.st_size - self.offset > 512 * 1024:
            self.offset = max(0, stat.st_size - 256 * 1024)
            self.partial = b""
            skip_first = True
        with path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(512 * 1024)
            self.offset = stream.tell()
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()
        if skip_first and lines:
            lines.pop(0)
        for line in lines:
            try:
                event = json.loads(line)
                value = event.get("payload", {})
                kind = event.get("type")
                when = timestamp(event.get("timestamp"))
                if kind == "turn_context":
                    self.values.update(model=value.get("model"), effort=value.get("effort"))
                elif kind == "event_msg":
                    state = {"task_started": "inProgress", "task_complete": "completed",
                             "turn_aborted": "interrupted"}.get(value.get("type"))
                    if state:
                        self.values.update(status=state, updated=when, turn_id=value.get('turn_id'))
                    elif value.get("type") == "item_completed":
                        self.values["activity"] = value.get("item", {}).get("type", "")
                        self.values["updated"] = when
                elif kind == "response_item" and value.get("type") in ("function_call", "custom_tool_call"):
                    self.values.update(activity="tool", updated=when)
            except (ValueError, TypeError, AttributeError):
                continue
        # Bound malformed/incomplete lines too.
        if len(self.partial) > 512 * 1024:
            self.partial = b""
        return dict(self.values)


class Monitor:
    def __init__(self, home):
        self.home = Path(home).expanduser()
        self.tails = {}
        self.last_good = []
        self.meter = Meter(self.home)

    def routes(self):
        """Read routing decisions only; never write to the plugin's state store."""
        path = self.home / "adaptive-router/state.sqlite3"
        by_thread, by_agent = {}, {}
        if not path.exists():
            return by_thread, by_agent
        try:
            with closing(connect(path)) as db:
                rows = db.execute("SELECT thread,data FROM decisions ORDER BY created DESC LIMIT 2000")
                for row in rows:
                    try:
                        record = json.loads(row["data"])
                        route = record.get("route", {})
                        difficulty = route.get("difficulty")
                        if not isinstance(difficulty, dict) or record.get("status") in ("cancelled", "superseded"):
                            continue
                        # Validate score at the boundary; output only a small allowlist.
                        score = difficulty.get("score")
                        checked = assess(score=score, source="caller_assessment")
                        checked.update(source=str(difficulty.get("source", "heuristic"))[:40],
                                       confidence=str(difficulty.get("confidence", "low"))[:20],
                                       task_type=str(difficulty.get("task_type", "general"))[:30],
                                       reasons=[str(x)[:120] for x in difficulty.get("reasons", [])[:5]])
                        policy = route.get('policy') or {}
                        outcome = record.get('outcome') or {}
                        profile = policy.get('profile')
                        if not profile and record.get('config', {}).get('preset'):
                            profile = '旧预设 ' + str(record['config']['preset'])
                        item = {"difficulty": checked, "routeModel": route.get("model"),
                                "routeEffort": route.get("reasoning_effort"), "routeStatus": record.get("execution"),
                                "routeCreatedAt": float(record.get("created", 0)),
                                'routeDecisionID': str(record.get('id', ''))[:80],
                                'routeReason': str(route.get('reason', ''))[:600],
                                'routeProfile': str(profile or '')[:30],
                                'routeRegistry': str(policy.get('registry_hash', ''))[:80],
                                'routeOutcome': str(outcome.get('acceptance', outcome.get('status', 'unknown')))[:30],
                                'routeTurnID': record.get('turn_id')}
                        by_thread.setdefault(row["thread"], item)
                        if isinstance(record.get('agent_id'), str):
                            by_agent.setdefault((row['thread'], 'id:'+record['agent_id']), item)
                            if record['agent_id'].startswith('/'):
                                by_agent.setdefault((row['thread'], record['agent_id']), item)
                        evidence = record.get("evidence", "")
                        if record.get("execution") == "dispatched" and evidence.startswith("agent:"):
                            by_agent.setdefault((row["thread"], evidence[6:]), item)
                    except (ValueError, TypeError, AttributeError):
                        continue
        except sqlite3.Error:
            pass
        return by_thread, by_agent

    def titles(self):
        path = self.home / "sqlite/codex-dev.db"
        if not path.exists():
            return {}
        try:
            with closing(connect(path)) as db:
                return {r["thread_id"]: r["display_title"] for r in db.execute(
                    "SELECT thread_id,display_title FROM local_thread_catalog WHERE host_id='local' AND missing_candidate=0")}
        except sqlite3.Error:
            return {}

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        try:
            return self._snapshot(now)
        except (sqlite3.Error, OSError, ValueError) as error:
            # Cached cards must not retain a misleading live state after loss of access.
            cached = []
            for old in self.last_good:
                row = dict(old)
                row["status"] = "unknown"
                row["activity"] = ""
                if row.get("savings"):
                    row["savings"] = {**row["savings"], "partial": True}
                cached.append(row)
            return {"type": "state", "connected": False, "updatedAt": now, "agents": cached,
                    "message": "本机任务数据暂时不可读，显示上次记录。" if cached else "未找到可读的本机 Codex 任务数据。",
                    "diagnostic": type(error).__name__, "warnings": []}

    def _snapshot(self, now):
        path = self.home / "state_5.sqlite"
        with closing(connect(path)) as db:
            columns = fields(db, "threads")
            if not {"id", "source", "updated_at"}.issubset(columns):
                raise ValueError("Unsupported metadata schema")
            wanted = ["id", "source", "name", "cwd", "rollout_path", "model", "reasoning_effort",
                      "agent_path", "agent_nickname", "thread_source", "tokens_used", "updated_at", "archived"]
            selection = ",".join(key if key in columns else "NULL AS " + key for key in wanted)
            # Bounded first-user brief is used by deterministic scoring only. It is
            # never included in snapshots, persisted, or sent to a model/service.
            selection += "," + ("substr(first_user_message,1,6000) AS task_brief" if "first_user_message" in columns else "NULL AS task_brief")
            records = [dict(r) for r in db.execute("SELECT " + selection + " FROM threads ORDER BY updated_at DESC")]
            try:
                parents = {r["child_thread_id"]: r["parent_thread_id"] for r in db.execute(
                    "SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges")}
            except sqlite3.Error:
                parents = {}
        titles = self.titles()
        for r in records:
            r["parent"] = parents.get(r["id"]) or source_parent(r["source"])
        allowed = {r["id"] for r in records if not r["parent"] and not r["archived"] and
                   (r["source"] == "vscode" or r["id"] in titles)}
        selected = set(allowed)
        for _ in range(32):
            children = {r["id"] for r in records if r["parent"] in selected}
            new = children - selected
            if not new:
                break
            selected.update(new)
        history = None
        warnings = []
        try:
            history = connect(self.home / "thread_history_1.sqlite")
            if not {"thread_id", "turn_id", "status", "rollout_ordinal"}.issubset(fields(history, "thread_turns")):
                history.close(); history = None
        except sqlite3.Error:
            warnings.append("轮次索引不可读，兼容读取本机事件。")
        agents = []
        usage = self.meter.refresh([r for r in records if r["id"] in selected])
        routes, agent_routes = self.routes()
        try:
            for r in records:
                tid = r["id"]
                if tid not in selected:
                    continue
                parent = r["parent"] if r["parent"] in selected else ""
                turn, item, data = {}, {}, {}
                if history:
                    try:
                        result = history.execute("SELECT turn_id,status,started_at,completed_at FROM thread_turns "
                            "WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (tid,)).fetchone()
                        turn = dict(result) if result else {}
                        if turn:
                            result = history.execute("SELECT item_type,created_at_ms, "
                                "json_extract(item_json,'$.status') AS status, "
                                "json_extract(item_json,'$.tool') AS tool "
                                "FROM thread_items WHERE thread_id=? AND turn_id=? "
                                "ORDER BY rollout_ordinal DESC LIMIT 1", (tid, turn["turn_id"])).fetchone()
                            item = dict(result) if result else {}
                    except sqlite3.Error:
                        pass
                updated = max(timestamp(r["updated_at"]), (item.get("created_at_ms") or 0) / 1000,
                              timestamp(turn.get("completed_at")), timestamp(turn.get("started_at")))
                activity = item.get("item_type", "")
                if not turn:
                    rollout = Path(r.get("rollout_path") or "")
                    try:
                        # Do not follow arbitrary metadata paths outside this Codex home.
                        resolved = rollout.resolve()
                        if self.home.resolve() in resolved.parents and resolved.is_file():
                            data = self.tails.setdefault(tid, Tail()).read(resolved)
                            updated = max(updated, data.get("updated", 0))
                            activity = data.get("activity", activity)
                    except OSError:
                        pass
                state = status_for(turn.get("status") or data.get("status"), updated, now,
                                   activity, item.get("status", ""))
                observed = (usage.get(tid) or {}).get("observedTurn", {})
                indexed_time = max(timestamp(turn.get("completed_at")), timestamp(turn.get("started_at")),
                                   (item.get("created_at_ms") or 0) / 1000)
                # The official history index can lag behind a new rollout segment.
                # An explicit newer lifecycle event takes precedence over an old terminal turn.
                if observed.get("turn_id") and (observed.get("started", 0) > indexed_time or (
                        observed.get("turn_id") == turn.get("turn_id") and observed.get("updated", 0) > indexed_time)):
                    updated = max(updated, observed.get("updated", 0))
                    state = status_for(observed.get("status"), updated, now)
                    activity = ""  # old indexed item belongs to an earlier turn
                    turn = {"started_at": observed.get("started", 0), 'turn_id': observed.get('turn_id')}
                # Do not infer 'thinking' from a stored reasoning item: it may have ended.
                title = (titles.get(tid) or r.get("name") or "未命名任务") if not parent else (
                    r.get("agent_path", "").rsplit("/", 1)[-1] if r.get("agent_path") else r.get("agent_nickname") or "子代理")
                route = routes.get(tid)
                if route and (route["routeCreatedAt"] < timestamp(turn.get("started_at")) or
                              (route.get('routeTurnID') and route['routeTurnID'] != turn.get('turn_id'))):
                    route = None
                route = route or agent_routes.get((parent, 'id:'+tid)) or agent_routes.get((parent, r.get("agent_path")))
                if not route:
                    brief = r.get("task_brief")
                    estimate = assess(text=brief or title)
                    estimate.update(source="brief_heuristic" if brief else "title_heuristic", confidence="low")
                    route = {"difficulty": estimate}
                measured = usage.get(tid)
                latest_turn_id = turn.get('turn_id') or data.get('turn_id')
                if measured and measured.get('currentTurn'):
                    # During cold backfill the reader may still be on an older
                    # turn. Never label that turn's numbers as the current one.
                    if not latest_turn_id or measured['currentTurn']['turnId'] != latest_turn_id:
                        measured = {**measured, 'currentTurn': None}
                agents.append({"id": tid, "parent": parent or "", "title": title[:160],
                    "nickname": r.get("agent_nickname") or "", "model": r.get("model") or data.get("model") or "未报告",
                    "effort": r.get("reasoning_effort") or data.get("effort") or "未报告",
                    "status": state, "activity": activity, "tool": item.get("tool") or "",
                    "cwd": r.get("cwd") or "", "tokens": r.get("tokens_used") or 0,
                    "updatedAt": updated, "startedAt": timestamp(turn.get("started_at")),
                    "source": "本机任务元数据 · 轮次索引" if turn else "本机任务元数据 · 事件记录",
                    "usage": measured, **route})
        finally:
            if history:
                history.close()
        self.tails = {k:v for k,v in self.tails.items() if k in selected}
        savings = aggregate_savings(agents)
        for agent in agents:
            agent["savings"] = savings[agent["id"]]
        self.last_good = agents
        return {"type": "state", "connected": True, "updatedAt": now, "agents": agents,
                "message": "本机 Codex · 只读同步", "diagnostic": "", "warnings": warnings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    monitor = Monitor(args.home)
    parent = os.getppid()
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping and os.getppid() == parent:
        try:
            print(json.dumps(monitor.snapshot(), ensure_ascii=False), flush=True)
        except BrokenPipeError:
            break
        if args.once:
            break
        for _ in range(20):
            if stopping:
                break
            time.sleep(.1)


if __name__ == "__main__":
    main()
