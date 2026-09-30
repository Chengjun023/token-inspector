#!/usr/bin/env python3
"""Experimental app-server adapter. Never starts or restarts a daemon."""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

from router import RouteError, Store, catalog, thread_id, validate_pair


class RPC:
    def __init__(self, socket=None):
        command = ["codex", "app-server", "proxy"]
        if socket:
            command += ["--sock", socket]
        self.p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.queue = queue.Queue()
        self.seq = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "adaptive_router", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.p.stdout:
                self.queue.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.queue.put(None)

    def _send(self, data):
        try:
            self.p.stdin.write(json.dumps(data)+"\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RouteError("无法连接现有 app-server；请使用原生子代理路由。") from e

    def call(self, method, params):
        self.seq += 1
        rid = self.seq
        self._send({"id": rid, "method": method, "params": params})
        end = time.monotonic()+12
        while time.monotonic() < end:
            try:
                message = self.queue.get(timeout=max(0.001, end-time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                raise RouteError("app-server 连接不可用；当前桌面可能未开放 control socket。")
            if message.get("id") == rid:
                if "error" in message:
                    # Do not persist raw server errors, which may include unrelated context.
                    raise RouteError(f"app-server 拒绝 {method}，code={message['error'].get('code')}；未确认生效。")
                return message["result"]
        raise RouteError(f"{method} 超时；如果请求包含写入，结果未知，禁止自动重试。")

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=2)
        self.reader.join(timeout=1)
        for stream in (self.p.stdin, self.p.stdout):
            if stream:
                stream.close()


def inspect(rpc, tid):
    t = rpc.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
    turns = rpc.call("thread/turns/list", {"threadId": tid, "limit": 1,
                                          "itemsView": "notLoaded", "sortDirection": "desc"})["data"]
    return {"thread_id": t["id"], "status": t["status"], "configured_model": t.get("model"),
            "configured_effort": t.get("reasoningEffort"),
            "turn": {k: turns[0].get(k) for k in ("id", "status")} if turns else None,
            "notice": "configured_* 为任务设置，不是当前步骤实际模型遥测。"}


def apply(rpc, store, rid, tid, turn):
    r = store.update(rid, tid, "get")
    if r["status"] != "selected" or r["execution"] != "not_started":
        raise RouteError("路由未选定或已派发；不能写入。")
    validate_pair(r["route"]["model"], r["route"]["reasoning_effort"], catalog())
    info = inspect(rpc, tid)
    if info["status"]["type"] != "active" or info["turn"] != {"id": turn, "status": "inProgress"}:
        raise RouteError("指定轮次已不在运行中；没有写入或改为下一轮。")
    # Reserve before the RPC so uncertain delivery cannot cause automatic duplicate execution.
    store.update(rid, tid, "mark", evidence="live publication requested; result not yet known")
    try:
        result = rpc.call("turn/settings/update", {"threadId": tid, "turnId": turn,
                            "model": r["route"]["model"], "effort": r["route"]["reasoning_effort"]})
    except Exception:
        store.update(rid, tid, "live_result", evidence="unknown")
        raise
    store.update(rid, tid, "live_result", evidence=result["status"])
    return {"status": result["status"], "route": r["route"], "thread_id": tid, "turn_id": turn,
            "notice": "applied 仅表示发布给后续捕获的步骤；不改变已捕获请求，且不保证还有后续推理。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "apply"))
    parser.add_argument("--thread-id")
    parser.add_argument("--socket")
    parser.add_argument("--id")
    parser.add_argument("--turn-id")
    args = parser.parse_args()
    rpc = None
    try:
        tid = thread_id(args.thread_id)
        if args.command == "apply" and (not args.id or not args.turn_id):
            raise RouteError("apply 需要已选择的 --id 和刚刚 probe 得到的 --turn-id。")
        rpc = RPC(args.socket)
        result = inspect(rpc, tid) if args.command == "probe" else apply(rpc, Store(), args.id, tid, args.turn_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RouteError, OSError, KeyError) as e:
        print(json.dumps({"error": str(e), "effect_confirmed": False,
                          "fallback": "native_subagent", "daemon_started": False}, ensure_ascii=False))
        return 2
    finally:
        if rpc:
            rpc.close()


if __name__ == "__main__":
    sys.exit(main())
