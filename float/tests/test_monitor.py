import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("monitor", Path(__file__).parents[1] / "backend/monitor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / "state_5.sqlite"
        self.history = self.home / "thread_history_1.sqlite"
        with sqlite3.connect(self.state) as db:
            db.executescript("""
            CREATE TABLE threads(id TEXT PRIMARY KEY,source TEXT,name TEXT,cwd TEXT,rollout_path TEXT,
              model TEXT,reasoning_effort TEXT,agent_path TEXT,agent_nickname TEXT,thread_source TEXT,
              tokens_used INT,updated_at INT,archived INT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);
            """)
        with sqlite3.connect(self.history) as db:
            db.executescript("""
            CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,started_at INT,completed_at INT,rollout_ordinal INT);
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_type TEXT,created_at_ms INT,item_json TEXT,rollout_ordinal INT);
            """)
        self.add("root")
        self.turn("root", "inProgress")
        self.monitor = m.Monitor(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, tid, parent=None, archived=0, updated=990, source=None):
        source = source or (json.dumps({"subagent":{"thread_spawn":{"parent_thread_id": parent}}}) if parent else "vscode")
        with sqlite3.connect(self.state) as db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                tid, source, "任务 " + tid, "/tmp/work", str(self.home / (tid + ".jsonl")),
                "gpt-6-astra" if not parent else "gpt-6-luna", "xhigh" if not parent else "medium",
                "/root/" + tid if parent else None, "Newton" if parent else None,
                "subagent" if parent else "user", 100, updated, archived))
            if parent:
                db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (parent, tid, "open"))

    def turn(self, tid, status, ordinal=1, item_type="reasoning", item=None):
        with sqlite3.connect(self.history) as db:
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?)", (tid,str(ordinal),status,980,995 if status == "completed" else None,ordinal))
            db.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (tid,str(ordinal),item_type,990000,json.dumps(item or {"type":item_type,"content":"SECRET_REASONING","status":"completed"}),ordinal))

    def test_reads_main_and_nested_children_with_actual_settings(self):
        self.add("child", "root"); self.add("grandchild", "child")
        self.turn("child", "inProgress"); self.turn("grandchild", "completed")
        state = self.monitor.snapshot(1000)
        rows = {r["id"]:r for r in state["agents"]}
        self.assertEqual(rows["child"]["parent"], "root")
        self.assertEqual(rows["grandchild"]["parent"], "child")
        self.assertEqual((rows["child"]["model"],rows["child"]["effort"]), ("gpt-6-luna","medium"))
        self.assertEqual(rows["root"]["status"], "running")
        self.assertEqual(rows["grandchild"]["status"], "completed")

    def usage(self, tid, model, inherited=None):
        events = [{"type":"turn_context", "payload":{"model":model}}]
        for owner in ([inherited] if inherited else []) + [tid]:
            events.append({"type":"token_usage_record", "payload":{
                "thread_id":owner, "response_id":"request-" + owner,
                "usage":{"input_tokens":1000,"cached_input_tokens":800,"output_tokens":100}}})
        (self.home / (tid + ".jsonl")).write_text("".join(json.dumps(e)+"\n" for e in events))

    def test_savings_aggregate_real_parent_and_descendant_usage_once(self):
        self.add("child", "root"); self.add("grandchild", "child")
        self.usage("root", "gpt-6-astra")
        self.usage("child", "gpt-6.1-sol", inherited="root")
        self.usage("grandchild", "gpt-6-luna", inherited="child")
        rows = {r["id"]:r for r in self.monitor.snapshot(1000)["agents"]}
        root, child, grandchild = (rows[tid]["savings"] for tid in ("root", "child", "grandchild"))
        self.assertEqual((root["taskCount"], child["taskCount"], grandchild["taskCount"]), (3, 2, 1))
        self.assertEqual((root["totalTokens"], child["totalTokens"], grandchild["totalTokens"]), (3300,2200,1100))
        self.assertAlmostEqual(root["savedCredits"], .158 + .195 - .00195)
        self.assertAlmostEqual(root["percent"], root["savedCredits"] / (.195*3) * 100)
        self.assertTrue(root["includesChildren"])
        self.assertFalse(grandchild["includesChildren"])
        self.assertFalse(root["partial"])
        # Repeated snapshots do not add already accounted requests again.
        second = {r["id"]:r for r in self.monitor.snapshot(1001)["agents"]}
        self.assertEqual(root, second["root"]["savings"])

    def test_missing_and_unknown_child_usage_are_not_free(self):
        self.add("unknown", "root"); self.add("missing", "root")
        self.usage("root", "gpt-6-sol")
        self.usage("unknown", "unknown-model")
        rows = {r["id"]:r for r in self.monitor.snapshot(1000)["agents"]}
        savings = rows["root"]["savings"]
        self.assertEqual((savings["taskCount"], savings["missingTaskCount"]), (3,2))
        self.assertEqual((savings["pricedTokens"], savings["totalTokens"]), (1100,2200))
        self.assertAlmostEqual(savings["percent"], 80)
        self.assertTrue(savings["partial"])
        self.assertIsNone(rows["unknown"]["savings"]["percent"])
        self.assertIsNone(rows["missing"]["savings"]["equivalentTokens"])

    def test_model_switch_and_completion_refresh(self):
        self.monitor.snapshot(1000)
        with sqlite3.connect(self.state) as db:
            db.execute("UPDATE threads SET model='gpt-5.6-terra',reasoning_effort='medium'")
        self.turn("root", "completed", 2)
        row = self.monitor.snapshot(1001)["agents"][0]
        self.assertEqual((row["model"],row["effort"],row["status"]), ("gpt-5.6-terra","medium","completed"))

    def route(self, score=8.2, created=990, execution="not_started", evidence="", status="selected"):
        folder = self.home / "adaptive-router"
        folder.mkdir(exist_ok=True)
        with sqlite3.connect(folder / "state.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS decisions(id TEXT,thread TEXT,created REAL,data TEXT)")
            record = {"created": created, "execution": execution, "evidence": evidence, "status": status,
                      "route": {"model": "gpt-6-astra", "reasoning_effort": "xhigh", "difficulty":
                                {"score": score, "source": "caller_assessment", "confidence": "medium",
                                 "task_type": "architecture", "reasons": ["scoped_review"]}}}
            db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (str(created), "root", created, json.dumps(record)))

    def test_router_score_is_separate_from_actual_model(self):
        self.route()
        row = self.monitor.snapshot(1000)["agents"][0]
        self.assertEqual(row["difficulty"]["score"], 8.2)
        self.assertEqual(row["routeStatus"], "not_started")
        self.assertEqual(row["model"], "gpt-6-astra")
        self.assertIsNone(row["usage"]["costUSD"])

    def test_stale_or_cancelled_routing_does_not_replace_current_score(self):
        self.route(created=900)
        self.route(created=990, status="cancelled")
        row = self.monitor.snapshot(1000)["agents"][0]
        self.assertEqual(row["difficulty"]["source"], "title_heuristic")
        self.assertNotIn("routeModel", row)

    def test_bad_score_is_ignored_and_child_uses_its_own_dispatch(self):
        self.add("child", "root"); self.turn("child", "inProgress")
        self.route(created=985, execution="dispatched", evidence="agent:/root/child")
        self.route(score="NaN", created=995)
        row = {r["id"]:r for r in self.monitor.snapshot(1000)["agents"]}["child"]
        self.assertEqual(row["difficulty"]["score"], 8.2)
        self.assertEqual(row["model"], "gpt-6-luna")
        self.assertEqual(row["routeModel"], "gpt-6-astra")

    def test_structured_dispatch_binding_and_acceptance_are_displayed(self):
        self.add('child', 'root'); self.turn('child', 'inProgress')
        self.route(execution='dispatched')
        with sqlite3.connect(self.home / 'adaptive-router/state.sqlite3') as db:
            record = json.loads(db.execute('SELECT data FROM decisions').fetchone()[0])
            record.update(agent_id='/root/child', turn_id='1', outcome={'status':'accepted'})
            record['route'].update(reason='capability and workload match', policy={'profile':'quality'})
            db.execute('UPDATE decisions SET data=?', (json.dumps(record),))
        row = {r['id']:r for r in self.monitor.snapshot(1000)['agents']}['child']
        self.assertEqual(row['routeProfile'], 'quality')
        self.assertEqual(row['routeOutcome'], 'accepted')
        self.assertEqual(row['routeReason'], 'capability and workload match')

    def test_stale_unfinished_turn_is_not_claimed_running(self):
        self.assertEqual(self.monitor.snapshot(1300)["agents"][0]["status"], "stale")

    def test_permission_loss_invalidates_cached_live_status(self):
        self.monitor.snapshot(1000)
        self.state.rename(self.home / "gone.sqlite")
        result = self.monitor.snapshot(1001)
        self.assertFalse(result["connected"])
        self.assertEqual(result["agents"][0]["status"], "unknown")

    def test_current_turn_uses_latest_ordinal_not_last_completed(self):
        self.turn("root", "completed", 2)
        self.turn("root", "inProgress", 3)
        self.assertEqual(self.monitor.snapshot(1000)["agents"][0]["status"], "running")

    def test_old_turn_usage_is_hidden_during_cold_backfill(self):
        self.turn('root', 'inProgress', 2)
        events = [{'type':'turn_context','payload':{'model':'gpt-6-astra'}},
                  {'type':'event_msg','timestamp':'1970-01-01T00:16:00Z',
                   'payload':{'type':'task_started','turn_id':'1'}},
                  {'type':'token_usage_record','payload':{'thread_id':'root','turn_id':'1',
                   'response_id':'old-request','usage':{'input_tokens':100,'output_tokens':10}}}]
        (self.home/'root.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        row=self.monitor.snapshot(1000)['agents'][0]
        self.assertIsNone(row['usage']['currentTurn'])
        self.assertEqual(row['usage']['totalTokens'],110)

    def test_new_rollout_lifecycle_overrides_lagging_terminal_index(self):
        self.turn("root", "interrupted", 2)
        events = [
            {"timestamp":"1970-01-01T00:16:36Z", "type":"event_msg",
             "payload":{"type":"task_started","turn_id":"new"}},
            {"timestamp":"1970-01-01T00:16:39Z", "type":"token_usage_record",
             "payload":{"thread_id":"root", "turn_id":"new", "response_id":"r1",
                        "usage":{"input_tokens":10,"output_tokens":2}}},
        ]
        path = self.home / "root.jsonl"
        path.write_text("".join(json.dumps(e)+"\n" for e in events))
        self.assertEqual(self.monitor.snapshot(1000)["agents"][0]["status"], "running")
        end = {"timestamp":"1970-01-01T00:16:40Z", "type":"event_msg",
               "payload":{"type":"task_complete","turn_id":"new"}}
        with path.open("a") as f: f.write(json.dumps(end)+"\n")
        self.assertEqual(self.monitor.snapshot(1001)["agents"][0]["status"], "completed")

    def test_new_start_does_not_wait_for_first_usage_to_be_running(self):
        self.turn('root', 'completed', 2)
        event = {'type':'event_msg', 'timestamp':'1970-01-01T00:16:39Z',
                 'payload':{'type':'task_started','turn_id':'next'}}
        (self.home/'root.jsonl').write_text(json.dumps(event)+'\n')
        self.assertEqual(self.monitor.snapshot(1000)['agents'][0]['status'], 'running')

    def test_no_message_reasoning_or_tool_arguments_in_output(self):
        self.turn("root", "inProgress", 2, "mcpToolCall", {"tool":"safe_tool", "arguments":{"secret":"SECRET_ARGUMENT"},"result":"SECRET_RESULT","status":"completed"})
        output = json.dumps(self.monitor.snapshot(1000))
        self.assertNotIn("SECRET", output)
        self.assertIn("safe_tool", output)

    def test_brief_classification_does_not_export_prompt(self):
        with sqlite3.connect(self.state) as db:
            db.execute("ALTER TABLE threads ADD COLUMN first_user_message TEXT")
            db.execute("UPDATE threads SET first_user_message=?", ("设计系统架构 SECRET_PRIVATE_BRIEF",))
        output = self.monitor.snapshot(1000)
        self.assertEqual(output["agents"][0]["difficulty"]["task_type"], "architecture")
        self.assertEqual(output["agents"][0]["difficulty"]["source"], "brief_heuristic")
        self.assertNotIn("SECRET", json.dumps(output))

    def test_read_only_and_database_bytes_unchanged(self):
        before = hashlib.sha256(self.state.read_bytes()).hexdigest()
        self.monitor.snapshot(1000)
        self.assertEqual(before, hashlib.sha256(self.state.read_bytes()).hexdigest())
        c = m.connect(self.state)
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("UPDATE threads SET model='bad'")
        c.close()

    def test_archived_root_and_unrelated_cli_task_excluded(self):
        self.add("archived", archived=1); self.add("orphan", "archived")
        self.add("cli", source="exec")
        self.assertEqual([r["id"] for r in self.monitor.snapshot(1000)["agents"]], ["root"])

    def test_source_parent_fallback_without_edges(self):
        self.add("child", "root")
        with sqlite3.connect(self.state) as db:db.execute("DELETE FROM thread_spawn_edges")
        self.assertEqual({r["id"]:r for r in self.monitor.snapshot(1000)["agents"]}["child"]["parent"], "root")

    def test_missing_metadata_schema_is_explicit(self):
        with sqlite3.connect(self.state) as db:db.execute("DROP TABLE threads")
        result = self.monitor.snapshot(1000)
        self.assertFalse(result["connected"])
        self.assertEqual(result["agents"], [])

    def test_read_only_rollout_fallback_and_partial_append(self):
        with sqlite3.connect(self.history) as db:db.execute("DELETE FROM thread_turns")
        p = self.home / "root.jsonl"
        event = {"timestamp":"1970-01-01T00:16:30Z","type":"event_msg","payload":{"type":"task_started","prompt":"SECRET_PROMPT"}}
        p.write_text(json.dumps(event)+"\n")
        row = self.monitor.snapshot(1000)["agents"][0]
        self.assertEqual(row["status"], "running")
        final = json.dumps({"timestamp":"1970-01-01T00:16:35Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"SECRET_FINAL"}})
        with p.open("a") as f:f.write(final[:20])
        self.assertEqual(self.monitor.snapshot(1000)["agents"][0]["status"], "running")
        with p.open("a") as f:f.write(final[20:]+"\n")
        result = self.monitor.snapshot(1000)
        self.assertEqual(result["agents"][0]["status"], "completed")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_asking_and_completed_are_not_guessed_from_reasoning(self):
        self.assertEqual(m.status_for("inProgress",990,1000,"reasoning","completed"), "running")
        self.assertEqual(m.status_for("inProgress",990,1000,"requestUserInput","inProgress"), "waiting")
        self.assertEqual(m.status_for("completed",990,1300), "completed")
        self.assertEqual(m.status_for(None,990,1000), "unknown")

    def test_unsafe_rollout_path_is_not_opened(self):
        with sqlite3.connect(self.history) as db:db.execute("DELETE FROM thread_turns")
        with sqlite3.connect(self.state) as db:db.execute("UPDATE threads SET rollout_path='/etc/passwd'")
        self.assertEqual(self.monitor.snapshot(1000)["agents"][0]["status"], "unknown")

    def test_real_monitor_process_streams_changes_and_exits(self):
        process = subprocess.Popen([sys.executable, "-u", str(Path(m.__file__)), "--home", str(self.home)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first["agents"][0]["model"], "gpt-6-astra")
            with sqlite3.connect(self.state) as db:
                db.execute("UPDATE threads SET model='gpt-6-luna',reasoning_effort='medium'")
            self.turn("root", "completed", 2)
            second = json.loads(process.stdout.readline())
            self.assertEqual((second["agents"][0]["model"],second["agents"][0]["status"]), ("gpt-6-luna","completed"))
            process.terminate()
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(stderr, "")
        finally:
            if process.poll() is None:process.kill();process.communicate()


if __name__ == "__main__": unittest.main()
