"""Unified executor results. No API keys are read, persisted, or required."""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

MAX_FINAL_BYTES = 512 * 1024
FINAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["files"],
                "properties": {"files": {"type": "array", "items": {"type": "object",
                    "additionalProperties": False, "required": ["path", "content"],
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}}


def normalize_usage(raw, success=True):
    """Codex input includes cached input; output includes reasoning. Never add again."""
    if not isinstance(raw, dict):
        return {"known": False, "reason": "missing_usage"}
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    if any(k not in raw or isinstance(raw[k], bool) or not isinstance(raw[k], int) or raw[k] < 0 for k in keys):
        return {"known": False, "reason": "invalid_or_missing_usage"}
    if raw["cached_input_tokens"] > raw["input_tokens"]:
        return {"known": False, "reason": "cached_input_exceeds_input"}
    reasoning = raw.get("reasoning_output_tokens", raw.get("reasoning_tokens"))
    if reasoning is not None and (isinstance(reasoning, bool) or not isinstance(reasoning, int)
                                  or reasoning < 0 or reasoning > raw["output_tokens"]):
        return {"known": False, "reason": "invalid_reasoning_usage"}
    if success and raw["input_tokens"] == 0 and raw["output_tokens"] == 0:
        return {"known": False, "reason": "zero_usage_for_success"}
    return {"known": True, **{k: raw[k] for k in keys}, "reasoning_tokens": reasoning,
            "output_includes_reasoning": True, "input_includes_cached": True}


def usage_from_events(usages, success):
    if not usages:
        return normalize_usage(None, success)
    # One turn.completed is normally emitted for the one-shot request. When
    # identifiable additional turns occur, sum each once; never add reasoning.
    unique, seen = [], {}
    for index, record in enumerate(usages):
        raw = record.get("usage", record)
        identity = record.get("turn_id", record.get("id"))
        signature = json.dumps(raw, sort_keys=True)
        key = ("turn", identity) if identity is not None else ("usage", signature)
        if key in seen:
            if identity is None or seen[key] != signature:
                return {"known": False, "reason": "ambiguous_duplicate_completion_usage"}
            continue
        seen[key] = signature
        value = normalize_usage(raw, success)
        if not value["known"]:
            return value
        unique.append(value)
    return {"known": True, **{key: sum(v[key] for v in unique) for key in
            ("input_tokens", "cached_input_tokens", "output_tokens")},
            "reasoning_tokens": sum(v["reasoning_tokens"] for v in unique) if all(v["reasoning_tokens"] is not None for v in unique) else None,
            "output_includes_reasoning": True, "input_includes_cached": True, "completion_count": len(unique)}


class CodexExecutor:
    name = "codex_exec"
    simulation = False

    def __init__(self, executable="codex", timeout=300):
        self.executable, self.timeout = executable, timeout

    def command(self, job, workspace, schema, final_path):
        return [self.executable, "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--json", "--output-schema", str(schema),
                "--output-last-message", str(final_path), "-s", "read-only", "-C", str(workspace),
                "-m", job["route"]["model"], "-c",
                "model_reasoning_effort=" + json.dumps(job["route"]["reasoning_effort"]), "-"]

    def execute(self, job, workspace, attempt_dir):
        started = time.monotonic()
        schema, final_path = attempt_dir / "output-schema.json", attempt_dir / "final-message.json"
        schema.write_text(json.dumps(FINAL_SCHEMA, sort_keys=True), encoding="utf-8")
        events, usages, invalid_lines, tool_items = [], [], [0], []
        status, error, proc = "executor_error", None, None
        # Read raw events only in memory. Persist event TYPE and usage, never item/reasoning bodies.
        def consume(stream, parse):
            for line in iter(stream.readline, b""):
                if not parse:
                    continue  # stderr can contain paths/secrets; deliberately discarded.
                if len(line) > 1024 * 1024:
                    invalid_lines[0] += 1
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    invalid_lines[0] += 1
                    continue
                kind = event.get("type")
                if isinstance(kind, str) and len(events) < 10000:
                    events.append(kind)
                if kind == "turn.completed" and "usage" in event:
                    usages.append({"usage": event["usage"], **{k: event[k] for k in ("turn_id", "id") if k in event}})
                item_type = event.get("item", {}).get("type") if isinstance(event.get("item"), dict) else None
                if item_type in ("command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"):
                    tool_items.append(item_type)
        threads = []
        try:
            child_env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")}
            proc = subprocess.Popen(self.command(job, workspace, schema, final_path),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, env=child_env)
            for stream, parse in ((proc.stdout, True), (proc.stderr, False)):
                thread = threading.Thread(target=consume, args=(stream, parse), daemon=True)
                thread.start()
                threads.append(thread)
            proc.stdin.write(job["prompt"].encode("utf-8"))
            proc.stdin.close()
            proc.wait(timeout=self.timeout)
            status = "completed" if proc.returncode == 0 else "executor_error"
            if proc.returncode:
                error = "codex_exit_" + str(proc.returncode)
        except subprocess.TimeoutExpired:
            status, error = "timeout", "executor_timeout"
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            error = type(e).__name__
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.wait()
            for thread in threads:
                thread.join(timeout=2)
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    stream.close()
        final = None
        if final_path.exists():
            try:
                if final_path.stat().st_size > MAX_FINAL_BYTES:
                    raise ValueError("final_too_large")
                final = json.loads(final_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError, OSError):
                status, error = "invalid_final", "invalid_or_oversized_final_json"
            finally:
                # Preserve only validated parsed artifacts, not an unstructured final message.
                final_path.unlink()
        elif status == "completed":
            status, error = "invalid_final", "missing_final_json"
        if status == "completed" and tool_items:
            status, error = "protocol_violation", "unexpected_model_tool_use"
        return {"status": status, "final": final, "usage": usage_from_events(usages, status == "completed"),
                "elapsed_seconds": round(time.monotonic() - started, 6), "event_types": events,
                "invalid_event_lines": invalid_lines[0], "unexpected_tool_types": sorted(set(tool_items)),
                "error": error, "simulation": False}


class MockExecutor:
    name = "mock"
    simulation = True

    def execute(self, job, workspace, attempt_dir):
        try:
            from .demo_answers import answer
        except ImportError:
            from demo_answers import answer
        final = answer(job["task"]["id"])
        return {"status": "completed", "final": final,
                "usage": normalize_usage({"input_tokens": 1000, "cached_input_tokens": 100,
                                           "output_tokens": 400, "reasoning_output_tokens": 100}),
                "elapsed_seconds": 0.001, "event_types": ["simulation.completed"], "error": None,
                "simulation": True}
