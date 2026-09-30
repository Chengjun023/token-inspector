#!/usr/bin/env python3
"""Versioned one-shot selector pilot. Live execution requires an explicit --live."""
import argparse
import csv
import hashlib
import importlib.util
import inspect
import json
import os
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

if __package__:
    from . import tasks
    from .executors import CodexExecutor, MockExecutor, MAX_FINAL_BYTES
else:
    import tasks
    from executors import CodexExecutor, MockExecutor, MAX_FINAL_BYTES

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT.parent
SCHEMA_VERSION = 1
STRATEGIES = ("fixed_strong", "fixed_mid", "router")
DEMO_CATALOG = {"source": "simulation-only capabilities", "models": {
    "gpt-6-astra": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-6.1-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-6-luna": ["low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-terra": ["medium", "high"], "gpt-6-sol": ["low", "medium", "high", "xhigh"],
    "gpt-5.6-sol": ["low", "medium", "high", "xhigh"]}}


class BenchmarkError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def capabilities(catalog):
    raw = catalog.get("models", catalog)
    if isinstance(raw, dict):
        return {k: v if isinstance(v, list) else v.get("reasoning_efforts", []) for k, v in raw.items()}
    result = {}
    for model in raw:
        name = model.get("slug", model.get("id", model.get("identity", {}).get("id")))
        efforts = model.get("supported_reasoning_levels", model.get("reasoning_efforts", model.get("capabilities", {}).get("efforts", [])))
        result[name] = [v["effort"] if isinstance(v, dict) else v for v in efforts]
    if not result or any(not k or not v for k, v in result.items()):
        raise BenchmarkError("catalog must contain explicit model IDs and supported reasoning efforts")
    return result


def import_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selector(task, catalog, router_dir, registry_snapshot=None, callback=None):
    models = capabilities(catalog)
    if callback is not None:
        return callback(task, catalog)
    # Keep old baselines importable without colliding with the current difficulty module.
    previous_path, previous_difficulty = list(sys.path), sys.modules.get("difficulty")
    try:
        sys.path.insert(0, str(router_dir))
        difficulty = import_from(router_dir / "difficulty.py", "benchmark_difficulty")
        sys.modules["difficulty"] = difficulty
        router = import_from(router_dir / "router.py", "benchmark_router")
        rating = difficulty.assess(text=task["instruction"], task_type="general", complexity="normal", risk="low")
        config = json.loads(json.dumps(router.DEFAULT))
        config.update(mode="auto", preset="balanced", overrides={})
        extra = {"profile": "balanced", "history": {}, "snapshot": registry_snapshot,
                 "host_snapshot": catalog.get("host_snapshot")}
        parameters = inspect.signature(router.select).parameters
        extra = {k: v for k, v in extra.items() if k in parameters}
        selected = router.select(config, "act", "normal", "low", True, 0, models,
                                 difficulty=rating, available_models=list(models), **extra)
        return {**selected, "assessment": rating, "assessment_input": {
            "text": task["instruction"], "task_type": "general", "complexity": "normal", "risk": "low"},
            "profile": "balanced", "history": {}, "feedback_scope": "benchmark"}
    finally:
        sys.path[:] = previous_path
        if previous_difficulty is None:
            sys.modules.pop("difficulty", None)
        else:
            sys.modules["difficulty"] = previous_difficulty


def code_hashes(router_dir):
    files = list(ROOT.glob("*.py")) + list((ROOT / "fixtures/v1").glob("*"))
    files += [p for p in router_dir.glob("*.py") if p.name in ("router.py", "difficulty.py", "model_registry.py", "registry.py", "policy.py", "feedback.py")]
    if (PLUGIN / "data/models.bundled.json").exists():
        files.append(PLUGIN / "data/models.bundled.json")
    return {str(p.relative_to(PLUGIN)) if p.is_relative_to(PLUGIN) else str(p): file_hash(p)
            for p in sorted(files)}


def code_drift(manifest):
    changes = []
    for name, expected in manifest["code_hashes"].items():
        path = Path(name)
        if not path.is_absolute():
            path = PLUGIN / path
        current = file_hash(path) if path.is_file() else None
        if current != expected:
            changes.append({"path": name, "expected": expected, "current": current})
    return changes


def freeze_sources(run_dir, manifest):
    """Save actual source bytes, not just hashes; hidden graders stay outside workspaces."""
    snapshot = Path(run_dir) / "source-snapshot"
    snapshot.mkdir()
    for name, expected in manifest["code_hashes"].items():
        relative = Path(name)
        if relative.is_absolute():
            # CLI router-dir is deliberately restricted to the plugin for portable snapshots.
            raise BenchmarkError("snapshot source must be under the plugin: " + name)
        source, destination = PLUGIN / relative, snapshot / relative
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise BenchmarkError("source changed while freezing: " + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(data)
    write_new(snapshot / "snapshot.json", {"manifest_hash": manifest["manifest_hash"],
                                           "files": manifest["code_hashes"]})


def build_manifest(seed=20260930, repetitions=1, catalog=None, prices=None, router_dir=None,
                   registry_snapshot=None, callback=None, simulation=False, max_runs=18):
    router_dir = Path(router_dir or PLUGIN / "scripts").resolve()
    if not isinstance(repetitions, int) or repetitions < 1 or repetitions * len(tasks.TASKS) * 3 > max_runs:
        raise BenchmarkError("planned jobs exceed max-runs or repetitions is invalid")
    catalog = catalog or DEMO_CATALOG
    models = capabilities(catalog)
    public = [tasks.public_task(task) for task in tasks.TASKS]
    jobs = []
    for repetition in range(repetitions):
        for task in public:
            # Metadata is analysis-only. The tested router gets text, never hand-scored task IDs.
            annotation = task.pop("routing", task.get("human_annotation", {}))
            task["human_annotation"] = annotation
            for strategy in STRATEGIES:
                if strategy == "router":
                    route = selector(task, catalog, router_dir, registry_snapshot, callback)
                else:
                    model, effort = (("gpt-6-astra", "xhigh") if strategy == "fixed_strong"
                                     else ("gpt-6.1-sol", "high"))
                    route = {"model": model, "reasoning_effort": effort, "reason": "fixed baseline"}
                if route["reasoning_effort"] not in models.get(route["model"], []):
                    raise BenchmarkError("unsupported model/effort: " + canonical(route))
                prompt = tasks.prompt_for(task)
                jobs.append({"id": "%s--%s--%02d" % (task["id"], strategy, repetition + 1),
                             "task": task, "strategy": strategy, "repetition": repetition + 1,
                             "route": route, "prompt": prompt, "prompt_hash": digest(prompt)})
    random.Random(seed).shuffle(jobs)
    manifest = {"schema": "adaptive-router.benchmark", "schema_version": SCHEMA_VERSION,
                "benchmark_version": tasks.VERSION, "seed": seed, "repetitions": repetitions,
                "max_runs": max_runs, "simulation": bool(simulation or catalog == DEMO_CATALOG),
                "comparison": "one-shot deterministic selector pilot; no multi-agent or failure escalation",
                "catalog": catalog, "catalog_hash": digest(catalog), "prices": prices,
                "prices_hash": digest(prices), "registry_snapshot": registry_snapshot,
                "registry_hash": digest(registry_snapshot), "router_source": str(router_dir),
                "code_hashes": code_hashes(router_dir), "jobs": jobs}
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def load_manifest(run_dir):
    manifest = load_json(Path(run_dir) / "manifest.json")
    expected = manifest.pop("manifest_hash")
    if digest(manifest) != expected:
        raise BenchmarkError("manifest hash mismatch; frozen plan was modified")
    manifest["manifest_hash"] = expected
    return manifest


def validate_final(final, task):
    if not isinstance(final, dict) or set(final) != {"files"} or not isinstance(final["files"], list):
        raise BenchmarkError("final must contain only a files array")
    files, size = {}, 0
    for item in final["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise BenchmarkError("invalid file record")
        path, content = item["path"], item["content"]
        if not isinstance(path, str) or path not in task["outputs"] or path in files:
            raise BenchmarkError("unexpected, duplicate, or unsafe artifact path")
        if not isinstance(content, str):
            raise BenchmarkError("artifact content must be UTF-8 text")
        size += len(content.encode("utf-8"))
        if size > MAX_FINAL_BYTES:
            raise BenchmarkError("artifacts exceed size limit")
        files[path] = content
    if set(files) != set(task["outputs"]):
        raise BenchmarkError("required artifact missing")
    return files


def grade(task_id, files, timeout=8):
    # Generated source runs ONLY in this limited child; it is never exec'd by the harness.
    with tempfile.TemporaryDirectory(prefix="router-grade-") as scratch:
        env = {"PATH": os.defpath, "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"}
        try:
            done = subprocess.run([sys.executable, "-I", str(ROOT / "grader_worker.py")],
                                  input=canonical({"task_id": task_id, "files": files}),
                                  capture_output=True, text=True, cwd=scratch, env=env, timeout=timeout)
            if done.returncode or len(done.stdout) > 4096:
                return {"passed": False, "error": "grader_process_failed", "grader_version": "grader-v1.0.0"}
            result = json.loads(done.stdout)
            if type(result.get("passed")) is not bool:
                raise ValueError("invalid grade result")
            return result
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "grader_timeout", "grader_version": "grader-v1.0.0"}
        except (ValueError, OSError):
            return {"passed": False, "error": "grader_invalid_result", "grader_version": "grader-v1.0.0"}


def price_cost(usage, model, prices):
    if not usage.get("known"):
        return {"known": False, "usd": None, "reason": "unknown_usage"}
    if not prices:
        return {"known": False, "usd": None, "reason": "missing_price_snapshot"}
    raw = prices.get("models", {})
    if isinstance(raw, list):
        match = next((m for m in raw if m.get("id", m.get("identity", {}).get("id")) == model), {})
        rate = match.get("pricing", {}).get("usd_per_million", {})
    else:
        rate = raw.get(model, {})
        rate = rate.get("usd_per_million", rate)
    values = [rate.get("input", rate.get("input_per_million")),
              rate.get("cached_input", rate.get("cached_input_per_million")),
              rate.get("output", rate.get("output_per_million"))]
    try:
        if any(v is None or isinstance(v, bool) for v in values):
            raise ValueError()
        rates = [Decimal(str(v)) for v in values]
        if any(not v.is_finite() or v < 0 for v in rates):
            raise ValueError()
        count = [usage["input_tokens"] - usage["cached_input_tokens"], usage["cached_input_tokens"], usage["output_tokens"]]
        cost = sum(Decimal(n) * r for n, r in zip(count, rates)) / Decimal(1000000)
        return {"known": True, "usd": str(cost), "reason": None, "price_snapshot_hash": digest(prices)}
    except (ValueError, ArithmeticError, KeyError):
        return {"known": False, "usd": None, "reason": "missing_or_invalid_model_price"}


def prepare_workspace(job_dir, job):
    workspace = job_dir / "workspace"
    workspace.mkdir()
    for name, content in job["task"]["fixture_contents"].items():
        (workspace / name).write_text(content, encoding="utf-8")
    return workspace


def verify_workspace(workspace, task):
    expected = task["fixture_contents"]
    if sorted(p.name for p in workspace.iterdir()) != sorted(expected):
        return False
    return all(not (workspace / name).is_symlink() and (workspace / name).read_text(encoding="utf-8") == value
               for name, value in expected.items())


def run_manifest(run_dir, executor=None, live=False, max_runs=18):
    run_dir = Path(run_dir).resolve()
    manifest = load_manifest(run_dir)
    executor = executor or MockExecutor()
    if not executor.simulation and not live:
        raise BenchmarkError("live execution requires --live")
    if not executor.simulation and manifest["simulation"]:
        raise BenchmarkError("a simulation manifest cannot execute live; create a plan with a real catalog")
    if executor.simulation != manifest["simulation"]:
        raise BenchmarkError("executor and manifest simulation labels must match")
    drift = code_drift(manifest)
    if drift:
        raise BenchmarkError("frozen code has changed; create a new plan before making more requests: " + canonical(drift))
    snapshot_root = run_dir / "source-snapshot"
    if not executor.simulation and not snapshot_root.exists():
        raise BenchmarkError("live plan requires its source-snapshot")
    if snapshot_root.exists():
        snapshot_changes = [{"path": name} for name, expected in manifest["code_hashes"].items()
                            if not (snapshot_root / name).is_file() or file_hash(snapshot_root / name) != expected]
        if snapshot_changes:
            raise BenchmarkError("source-snapshot hash mismatch: " + canonical(snapshot_changes))
    lock = run_dir / "execution.lock"
    try:
        write_new(lock, {"pid": os.getpid(), "created_at": now()})
    except FileExistsError:
        raise BenchmarkError("execution.lock exists; verify no owned process is running before removing stale lock")
    try:
        for job in manifest["jobs"]:
            path = run_dir / "jobs" / job["id"]
            if (path / "started.json").exists() and not (path / "result.json").exists():
                raise BenchmarkError("unresolved inflight job " + job["id"] + "; use resolve-interrupted; never automatically replay a possibly paid request")
        started_count = sum((run_dir / "jobs" / job["id"] / "started.json").exists() for job in manifest["jobs"])
        for job in manifest["jobs"]:
            job_dir = run_dir / "jobs" / job["id"]
            if (job_dir / "result.json").exists():
                continue
            if started_count >= min(max_runs, manifest["max_runs"]):
                break
            job_dir.mkdir(parents=True, exist_ok=True)
            workspace = prepare_workspace(job_dir, job)
            write_new(job_dir / "started.json", {"created_at": now(), "executor": executor.name,
                                                 "manifest_hash": manifest["manifest_hash"], "simulation": executor.simulation})
            started_count += 1
            result = executor.execute(job, workspace, job_dir)
            result.update(job_id=job["id"], strategy=job["strategy"], task_id=job["task"]["id"],
                          route=job["route"], completed_at=now(), manifest_hash=manifest["manifest_hash"])
            files = None
            try:
                if result["status"] == "completed":
                    files = validate_final(result["final"], job["task"])
                    if not verify_workspace(workspace, job["task"]):
                        raise BenchmarkError("fixture workspace was modified")
                    artifact_dir = job_dir / "artifacts"
                    artifact_dir.mkdir()
                    for name, content in files.items():
                        with (artifact_dir / name).open("x", encoding="utf-8") as output:
                            output.write(content)
                    result["grade"] = grade(job["task"]["id"], files)
                else:
                    result["grade"] = {"passed": False, "error": result.get("error") or result["status"]}
            except (BenchmarkError, OSError, UnicodeError) as e:
                result.update(status="invalid_artifact", grade={"passed": False, "error": str(e)[:200]})
            result.pop("final", None)
            result["artifact_hashes"] = {k: digest(v) for k, v in (files or {}).items()}
            result["cost"] = price_cost(result.get("usage", {}), job["route"]["model"], manifest["prices"])
            result["first_pass"] = result["final_pass"] = result["grade"]["passed"]
            write_new(job_dir / "result.json", result)
            print(canonical({"job": job["id"], "status": result["status"], "passed": result["final_pass"],
                             "simulation": executor.simulation}), flush=True)
    finally:
        lock.unlink()
    return report(run_dir)


def resolve_interrupted(run_dir, job_id):
    run_dir = Path(run_dir).resolve()
    manifest = load_manifest(run_dir)
    if (run_dir / "execution.lock").exists():
        raise BenchmarkError("remove a verified stale execution lock before resolving")
    job = next((j for j in manifest["jobs"] if j["id"] == job_id), None)
    if job is None:
        raise BenchmarkError("unknown job")
    path = run_dir / "jobs" / job_id
    if not (path / "started.json").exists():
        raise BenchmarkError("job has not started")
    write_new(path / "result.json", {"job_id": job_id, "task_id": job["task"]["id"], "strategy": job["strategy"],
              "route": job["route"], "status": "interrupted_unknown", "first_pass": False, "final_pass": False,
              "grade": {"passed": False, "error": "interrupted_request_may_have_been_billed"},
              "usage": {"known": False, "reason": "interrupted"}, "elapsed_seconds": None,
              "cost": {"known": False, "usd": None, "reason": "interrupted"}, "artifact_hashes": {},
              "simulation": manifest["simulation"], "completed_at": now(), "manifest_hash": manifest["manifest_hash"]})


def report(run_dir):
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    groups, rows = {}, []
    for strategy in STRATEGIES:
        jobs = [j for j in manifest["jobs"] if j["strategy"] == strategy]
        results = [load_json(run_dir / "jobs" / j["id"] / "result.json") for j in jobs
                   if (run_dir / "jobs" / j["id"] / "result.json").exists()]
        passed = sum(bool(r["final_pass"]) for r in results)
        priced = [r for r in results if r.get("cost", {}).get("known")]
        known_cost = sum((Decimal(r["cost"]["usd"]) for r in priced), Decimal(0))
        all_priced = len(priced) == len(results) and bool(results)
        times = [r["elapsed_seconds"] for r in results if r.get("elapsed_seconds") is not None]
        known_usage = [r["usage"] for r in results if r.get("usage", {}).get("known")]
        full_usage = len(known_usage) == len(results) and bool(results)
        token_totals = {key: sum(u[key] for u in known_usage) if full_usage else None
                        for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
        token_totals["total_tokens"] = (token_totals["input_tokens"] + token_totals["output_tokens"]) if full_usage else None
        token_totals["reasoning_tokens"] = sum(u["reasoning_tokens"] for u in known_usage) if full_usage and all(u.get("reasoning_tokens") is not None for u in known_usage) else None
        group = {"planned": len(jobs), "completed": len(results), "successes": passed,
                 "first_pass_successes": sum(bool(r["first_pass"]) for r in results),
                 "acceptance_rate": passed / len(results) if results else None,
                 "first_pass_rate": sum(bool(r["first_pass"]) for r in results) / len(results) if results else None,
                 "usage_known_runs": len(known_usage), "usage_coverage": len(known_usage) / len(results) if results else None,
                 "token_totals": token_totals,
                 "planned_acceptance_rate": passed / len(jobs),
                 "priced_runs": len(priced), "pricing_coverage": len(priced) / len(results) if results else None,
                 "known_cost_usd": str(known_cost) if priced else None,
                 "total_cost_usd": str(known_cost) if all_priced else None,
                 "cost_per_success_usd": str(known_cost / passed) if all_priced and passed else None,
                 "known_failure_cost_usd": str(sum((Decimal(r["cost"]["usd"]) for r in priced if not r["final_pass"]), Decimal(0))) if priced else None,
                 "total_elapsed_seconds": round(sum(times), 6) if len(times) == len(results) and results else None,
                 "mean_elapsed_seconds": sum(times) / len(times) if times else None}
        groups[strategy] = group
        for r in results:
            rows.append({"job_id": r["job_id"], "task_id": r["task_id"], "strategy": strategy,
                         "model": r["route"]["model"], "reasoning_effort": r["route"]["reasoning_effort"],
                         "status": r["status"], "first_pass": r["first_pass"], "final_pass": r["final_pass"],
                         "elapsed_seconds": r.get("elapsed_seconds"), "usage_known": r.get("usage", {}).get("known", False),
                         "input_tokens": r.get("usage", {}).get("input_tokens"),
                         "cached_input_tokens": r.get("usage", {}).get("cached_input_tokens"),
                         "output_tokens": r.get("usage", {}).get("output_tokens"),
                         "reasoning_tokens": r.get("usage", {}).get("reasoning_tokens"),
                         "cost_known": r.get("cost", {}).get("known", False), "cost_usd": r.get("cost", {}).get("usd"),
                         "simulation": r["simulation"]})
    return {"manifest_hash": manifest["manifest_hash"], "benchmark_version": manifest["benchmark_version"],
            "simulation": manifest["simulation"], "comparison": manifest["comparison"], "groups": groups, "runs": rows,
            "code_drift": code_drift(manifest), "model_identity_basis": "requested CLI model and effort; no server actual-model telemetry assumed",
            "cost_basis": "token usage multiplied by frozen requested-model reference prices; not the ChatGPT subscription bill",
            "limitations": ["One attempt per task and strategy; first and final acceptance coincide.",
                            "Group price differences are observed group costs, not per-task savings.",
                            "Unknown usage or model price remains unknown; total cost requires complete coverage.",
                            "Cost per success includes all priced successes and failures; no retry or escalation.",
                            "Pricing uses requested CLI model/effort; service-side actual-model identity is not claimed.",
                            "USD amounts are frozen reference token prices, not measured subscription charges.",
                            "Six synthetic tasks and one repetition are a pilot, not evidence of full routing workflow gains."]}


def export_report(run_dir, output):
    data = report(run_dir)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Reports are immutable snapshots; select another output directory to refresh them.
    write_new(output / "report.json", data)
    columns = list(data["runs"][0]) if data["runs"] else ["job_id", "task_id", "strategy", "status"]
    with (output / "report.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data["runs"])
    lines = ["# Router selector pilot" + (" — SIMULATION" if data["simulation"] else ""), "",
             "Manifest: `" + data["manifest_hash"] + "`", "", data["comparison"], "",
             "| Strategy | Completed/planned | Passed | Acceptance | Usage coverage | Total tokens | Price coverage | Total USD | USD/success |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value):
        return "unknown" if value is None else str(round(value, 4)) if isinstance(value, float) else str(value)
    for strategy, group in data["groups"].items():
        lines.append("| %s | %s/%s | %s | %s | %s | %s | %s | %s | %s |" % (strategy, group["completed"], group["planned"],
          group["successes"], fmt(group["acceptance_rate"]), fmt(group["usage_coverage"]), fmt(group["token_totals"]["total_tokens"]), fmt(group["pricing_coverage"]),
          fmt(group["total_cost_usd"]), fmt(group["cost_per_success_usd"])))
    lines += ["", *["- " + limit for limit in data["limitations"]], ""]
    with (output / "report.md").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return data


def regrade(run_dir, output):
    manifest, run_dir = load_manifest(run_dir), Path(run_dir)
    entries = []
    for job in manifest["jobs"]:
        path = run_dir / "jobs" / job["id"]
        if not (path / "result.json").exists():
            continue
        old = load_json(path / "result.json")
        if not old.get("artifact_hashes"):
            entries.append({"job_id": job["id"], "grade": old["grade"], "replayed": False})
            continue
        files = {name: (path / "artifacts" / name).read_text(encoding="utf-8") for name in job["task"]["outputs"]}
        if {k: digest(v) for k, v in files.items()} != old["artifact_hashes"]:
            raise BenchmarkError("artifact hash mismatch: " + job["id"])
        entries.append({"job_id": job["id"], "grade": grade(job["task"]["id"], files), "replayed": True})
    result = {"manifest_hash": manifest["manifest_hash"], "simulation": manifest["simulation"],
              "created_at": now(), "grader_hash": file_hash(ROOT / "graders.py"),
              "original_grader_hash": manifest["code_hashes"].get("benchmarks/graders.py"), "results": entries,
              "model_requests": 0, "original_results_modified": False, "code_drift": code_drift(manifest)}
    write_new(Path(output), result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", required=True)
    plan.add_argument("--seed", type=int, default=20260930)
    plan.add_argument("--repetitions", type=int, default=1)
    plan.add_argument("--max-runs", type=int, default=18)
    plan.add_argument("--catalog")
    plan.add_argument("--prices")
    plan.add_argument("--registry-snapshot")
    plan.add_argument("--router-dir", default=str(PLUGIN / "scripts"))
    for name in ("run", "demo"):
        run = commands.add_parser(name)
        run.add_argument("--run-dir", required=True)
        run.add_argument("--live", action="store_true")
        run.add_argument("--executor", choices=("mock", "codex"), default="mock")
        run.add_argument("--timeout", type=int, default=300)
        run.add_argument("--max-runs", type=int, default=18)
    reporting = commands.add_parser("report")
    reporting.add_argument("--run-dir", required=True)
    reporting.add_argument("--output", required=True)
    for name in ("regrade", "replay"):
        replay = commands.add_parser(name)
        replay.add_argument("--run-dir", required=True)
        replay.add_argument("--output", required=True)
    resolve = commands.add_parser("resolve-interrupted")
    resolve.add_argument("--run-dir", required=True)
    resolve.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            if not 1 <= args.max_runs <= 1000:
                raise BenchmarkError("max-runs must be 1..1000")
            catalog = load_json(args.catalog) if args.catalog else None
            prices = load_json(args.prices) if args.prices else None
            snapshot = load_json(args.registry_snapshot) if args.registry_snapshot else None
            manifest = build_manifest(args.seed, args.repetitions, catalog, prices, args.router_dir, snapshot,
                                      simulation=not bool(args.catalog), max_runs=args.max_runs)
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=False)
            write_new(output / "manifest.json", manifest)
            freeze_sources(output, manifest)
            write_new(output / "created.json", {"created_at": now(), "manifest_hash": manifest["manifest_hash"]})
            print(canonical({"manifest_hash": manifest["manifest_hash"], "jobs": len(manifest["jobs"]),
                             "simulation": manifest["simulation"], "routes": {j["id"]: j["route"] for j in manifest["jobs"]}}))
        elif args.command in ("run", "demo"):
            if not 1 <= args.timeout <= 3600 or not 1 <= args.max_runs <= 1000:
                raise BenchmarkError("invalid timeout or max-runs")
            if args.command == "demo":
                if args.live or args.executor != "mock":
                    raise BenchmarkError("demo only supports offline mock execution")
                output = Path(args.run_dir)
                if not output.exists():
                    output.mkdir(parents=True)
                    manifest = build_manifest(simulation=True)
                    write_new(output / "manifest.json", manifest)
                    freeze_sources(output, manifest)
            executor = CodexExecutor(timeout=args.timeout) if args.executor == "codex" else MockExecutor()
            data = run_manifest(args.run_dir, executor, args.live, args.max_runs)
            print(canonical({"simulation": data["simulation"], "groups": data["groups"]}))
        elif args.command == "report":
            export_report(args.run_dir, args.output)
        elif args.command in ("regrade", "replay"):
            data = regrade(args.run_dir, args.output)
            print(canonical({"regraded": len(data["results"]), "model_requests": 0}))
        else:
            resolve_interrupted(args.run_dir, args.job)
    except (BenchmarkError, FileExistsError, ValueError, KeyError) as e:
        print("benchmark error: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
