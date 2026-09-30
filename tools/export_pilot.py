#!/usr/bin/env python3
"""Export the public pilot and verify it offline using only the standard library.

No executor or model endpoint is called. Source data is read-only. Existing output
directories are refused. Checksums are integrity evidence, not authentication.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

DEFAULT_PACKAGE = Path(__file__).resolve().parents[1] / "router/benchmarks/results/pilot-public"
UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
PRIVATE_PATH = re.compile(r"/(?:Users|home|private|var/folders|Volumes)/[^\s\"']+|[A-Za-z]:\\(?:Users|Documents and Settings)\\[^\s\"']+")
SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[A-Z0-9]{16}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
EXPECTED = {
    "fixed_strong": (6, 102162, Decimal("1.237460"), 0),
    "fixed_mid": (6, 105285, Decimal("0.1921092"), 24832),
    "router": (6, 101392, Decimal("0.1621807"), 0),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def relative_path(name):
    path = Path(name)
    require(isinstance(name, str) and not path.is_absolute() and ".." not in path.parts and str(path) == name,
            "unsafe package path")
    return path


def privacy_check(root):
    for path in root.rglob("*"):
        require(not path.is_symlink(), "symlinks are forbidden in the public package")
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            require(not PRIVATE_PATH.search(text), "private path in " + str(path.relative_to(root)))
            require(not UUID.search(text), "private UUID in " + str(path.relative_to(root)))
            require(not SECRET.search(text), "secret pattern in " + str(path.relative_to(root)))


def frozen_command(root, command, output):
    # Copy only frozen source to scratch. Its imports can create bytecode; neither
    # verification nor a frozen worker may write into the published evidence.
    with tempfile.TemporaryDirectory(prefix="public-pilot-source-") as scratch:
        frozen = Path(scratch) / "source-snapshot"
        shutil.copytree(root / "source-snapshot", frozen)
        argv = [sys.executable, "-B", str(frozen / "benchmarks/benchmark.py"), command,
                "--run-dir", str(root.resolve()), "--output", str(output)]
        done = subprocess.run(argv, cwd=scratch, capture_output=True, text=True, timeout=90,
                              env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"})
        require(done.returncode == 0, "frozen " + command + " failed (output omitted to protect host paths)")


def offline_evidence(root):
    with tempfile.TemporaryDirectory(prefix="public-pilot-replay-") as scratch:
        scratch = Path(scratch)
        frozen_command(root, "regrade", scratch / "regrade.json")
        frozen_command(root, "report", scratch / "report")
        replay = read_json(scratch / "regrade.json")
        for name in ("report.csv", "report.md"):
            require((scratch / "report" / name).read_bytes() == (root / "report" / name).read_bytes(),
                    "frozen report export mismatch: " + name)
        # The grader results, identity and zero-request proof are deterministic;
        # wall-clock execution time is deliberately excluded from this evidence.
        replay.pop("created_at")
        return replay, read_json(scratch / "report/report.json")


def verify(root, check_inventory=True):
    require(root.is_dir(), "package directory missing")
    privacy_check(root)
    if check_inventory:
        inventory = read_json(root / "CHECKSUMS.json")
        files = {str(p.relative_to(root)): file_hash(p) for p in root.rglob("*")
                 if p.is_file() and p != root / "CHECKSUMS.json"}
        require(files == inventory["files"], "package byte hash or file inventory mismatch")
    manifest = read_json(root / "manifest.json")
    body = dict(manifest)
    expected_hash = body.pop("manifest_hash")
    require(digest(body) == expected_hash, "public manifest hash mismatch")
    require(manifest["simulation"] is False and len(manifest["jobs"]) == 18, "not the real 18-run pilot")
    require(len({j["id"] for j in manifest["jobs"]}) == 18, "duplicate job")
    provenance = read_json(root / "PROVENANCE.json")
    require(provenance["public_manifest_hash"] == expected_hash, "provenance manifest mismatch")
    snapshot = read_json(root / "source-snapshot/snapshot.json")
    require(snapshot == {"manifest_hash": expected_hash, "files": manifest["code_hashes"]}, "snapshot index mismatch")
    for name, expected in manifest["code_hashes"].items():
        path = root / "source-snapshot" / relative_path(name)
        require(file_hash(path) == expected, "frozen source changed: " + name)
        require(provenance["original_file_sha256"]["source-snapshot/" + name] == expected,
                "source bytes differ from original source evidence")
    for key, name in (("catalog_hash", "catalog"), ("prices_hash", "prices"), ("registry_hash", "registry_snapshot")):
        require(digest(manifest[name]) == manifest[key], name + " snapshot hash mismatch")
    rates = {m["id"]: m["pricing"]["usd_per_million"] for m in manifest["prices"]["models"]}
    for job in manifest["jobs"]:
        relative_path(job["id"])
        require(digest(job["prompt"]) == job["prompt_hash"], "prompt hash mismatch")
        folder = root / "jobs" / job["id"]
        result = read_json(folder / "result.json")
        require(result["manifest_hash"] == expected_hash, "result manifest reference mismatch")
        require(read_json(folder / "started.json")["manifest_hash"] == expected_hash,
                "started manifest reference mismatch")
        require(result["job_id"] == job["id"] and result["task_id"] == job["task"]["id"]
                and result["strategy"] == job["strategy"] and result["route"] == job["route"], "job/result identity mismatch")
        require(result["simulation"] is False and result["status"] == "completed", "job did not complete live")
        files = {name: (folder / "artifacts" / relative_path(name)).read_text(encoding="utf-8")
                 for name in job["task"]["outputs"]}
        require({name: digest(content) for name, content in files.items()} == result["artifact_hashes"], "artifact content mismatch")
        for name in files:
            artifact = "jobs/" + job["id"] + "/artifacts/" + name
            require(file_hash(root / artifact) == provenance["original_file_sha256"][artifact],
                    "artifact bytes differ from original evidence")
        require({p.name for p in (folder / "artifacts").iterdir()} == set(files), "extra artifact")
        usage = result["usage"]
        require(usage["known"] and usage["input_includes_cached"] and usage["output_includes_reasoning"], "usage semantics mismatch")
        require(0 <= usage["cached_input_tokens"] <= usage["input_tokens"], "invalid cached token count")
        rate = rates[result["route"]["model"]]
        cost = ((usage["input_tokens"] - usage["cached_input_tokens"]) * Decimal(str(rate["input"]))
                + usage["cached_input_tokens"] * Decimal(str(rate["cached_input"]))
                + usage["output_tokens"] * Decimal(str(rate["output"]))) / Decimal(1000000)
        require(result["cost"]["known"] and Decimal(result["cost"]["usd"]) == cost, "reference cost mismatch")
        require(result["cost"]["price_snapshot_hash"] == manifest["prices_hash"], "result price snapshot mismatch")
    replay, report = offline_evidence(root)
    require(replay["code_drift"] == [] and replay["model_requests"] == 0 and not replay["original_results_modified"], "replay integrity failed")
    require(len(replay["results"]) == 18 and all(r["replayed"] and r["grade"]["passed"] for r in replay["results"]), "offline acceptance not 18/18")
    for entry in replay["results"]:
        saved = read_json(root / "jobs" / entry["job_id"] / "result.json")
        require(entry["grade"] == saved["grade"] and saved["first_pass"] and saved["final_pass"],
                "saved acceptance differs from independent replay")
    archived_replay = read_json(root / "original-regrade.json")
    require(archived_replay["manifest_hash"] == expected_hash and archived_replay["results"] == replay["results"],
            "archived original replay differs from public replay")
    if check_inventory:
        require(replay == read_json(root / "offline-regrade.json"), "deterministic replay evidence changed")
        require(report == read_json(root / "report/report.json"), "frozen report recomputation mismatch")
    for strategy, (passed, tokens, usd, cached) in EXPECTED.items():
        group = report["groups"][strategy]
        require((group["successes"], group["token_totals"]["total_tokens"], Decimal(group["total_cost_usd"]),
                 group["token_totals"]["cached_input_tokens"]) == (passed, tokens, usd, cached), "pilot totals changed")
    return replay, report


def export(source, output):
    require(source.is_dir() and not source.is_symlink(), "source directory missing or is a symlink")
    require(not output.exists(), "output already exists; select a fresh directory")
    manifest = read_json(source / "manifest.json")
    original_manifest_hash = manifest["manifest_hash"]
    original_body = dict(manifest)
    original_body.pop("manifest_hash")
    require(digest(original_body) == original_manifest_hash, "original manifest hash mismatch")
    paths = ["manifest.json", "created.json", "catalog.json", "registry.json", "source-snapshot/snapshot.json"]
    paths += ["source-snapshot/" + name for name in manifest["code_hashes"]]
    for job in manifest["jobs"]:
        relative_path(job["id"])
        prefix = "jobs/" + job["id"] + "/"
        paths += [prefix + name for name in ("result.json", "started.json", "output-schema.json")]
        paths += [prefix + "artifacts/" + str(relative_path(name)) for name in job["task"]["outputs"]]
    report_source = source.parent / (source.name + "-report")
    inputs = {name: source / relative_path(name) for name in paths}
    inputs.update({"report/" + name: report_source / name for name in ("report.json", "report.csv", "report.md", "ANALYSIS.zh-CN.md")})
    inputs["original-regrade.json"] = source.parent / (source.name + "-regrade.json")
    for path in inputs.values():
        require(path.is_file() and not path.is_symlink(), "source evidence missing or symlink")
    before = {name: file_hash(path) for name, path in inputs.items()}
    ids = sorted({match for path in inputs.values() for match in UUID.findall(path.read_text(encoding="utf-8"))})
    replacements = {identity: "public-id-%03d" % (i + 1) for i, identity in enumerate(ids)}
    # Frozen code and generated artifacts must retain original bytes. If privacy
    # redaction would affect them, fail rather than silently change the experiment.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pilot-export-", dir=output.parent) as scratch:
        staged = Path(scratch) / "package"
        staged.mkdir()
        for name, path in inputs.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        manifest["router_source"] = "source-snapshot/scripts"
        serialized = canonical(manifest)
        for identity, public_id in replacements.items():
            serialized = serialized.replace(identity, public_id)
        manifest = json.loads(serialized)
        manifest.pop("manifest_hash")
        manifest["manifest_hash"] = digest(manifest)
        public_hash = manifest["manifest_hash"]
        changed = []
        for name in inputs:
            path = staged / name
            if name.startswith("source-snapshot/") and name != "source-snapshot/snapshot.json" or "/artifacts/" in name:
                continue
            original_text = path.read_text(encoding="utf-8")
            text = original_text.replace(original_manifest_hash, public_hash)
            if name == "report/ANALYSIS.zh-CN.md":
                text = text.replace("../" + source.name + "/", "../")
                text = text.replace("../" + source.name + "-regrade.json", "../original-regrade.json")
            for identity, public_id in replacements.items():
                text = text.replace(identity, public_id)
            if name == "manifest.json":
                write_json(path, manifest)
            elif text != original_text:
                path.write_text(text, encoding="utf-8")
            if file_hash(path) != before[name]:
                changed.append(name)
        provenance = {
            "schema": "token-inspector.public-pilot-provenance.v1",
            "experiment_date": "2026-09-30", "original_manifest_hash": original_manifest_hash,
            "public_manifest_hash": public_hash, "original_file_sha256": before,
            "changed_files": sorted(changed), "uuid_replacements": len(replacements),
            "redaction": ["router_source absolute host path replaced with source-snapshot/scripts",
                          "manifest_hash recalculated; all selected references updated",
                          "Chinese analysis relative links adapted to the nested public report directory",
                          "UUIDs, if present, mapped in lexical order to public-id-NNN; original IDs omitted"],
            "preserved": ["all frozen source and generated artifact bytes", "prompts, task definitions and output schemas",
                          "requested models/efforts, usage, experiment timestamps, elapsed time and frozen prices"],
            "excluded": ["execution workspaces (redundant fixtures)", "all execution and command logs", "private host state"],
            "source_modified": False,
            "verification": "offline deterministic frozen grader; zero model requests; SHA-256 inventory plus logical hashes",
            "trust_boundary": "Checksums prove internal integrity, not a signed or independently witnessed execution. Usage is saved executor telemetry; actual server model identity is unavailable.",
        }
        write_json(staged / "PROVENANCE.json", provenance)
        (staged / "REPRODUCE.md").write_text(REPRODUCE, encoding="utf-8")
        replay, report = verify(staged, check_inventory=False)
        require(report == read_json(staged / "report/report.json"), "source report differs from recomputation")
        write_json(staged / "offline-regrade.json", replay)
        write_json(staged / "CHECKSUMS.json", {"algorithm": "sha256-file-bytes", "excluded": ["CHECKSUMS.json"],
                   "files": {str(p.relative_to(staged)): file_hash(p) for p in sorted(staged.rglob("*")) if p.is_file()}})
        verify(staged)
        require(before == {name: file_hash(path) for name, path in inputs.items()}, "read-only source changed during export")
        staged.rename(output)
    return report


REPRODUCE = """# Public pilot: provenance and offline reproduction

Last updated: `2026-09-30T23:45:00+08:00`

This package contains the real 2026-09-30 pilot: six synthetic tasks, three strategies,
one attempt each. `jobs/*/result.json` is saved structured executor telemetry and
`jobs/*/artifacts/` contains the complete returned file contents. Raw event streams,
command logs and execution workspaces are excluded. No new model requests were made
to produce or verify this export. `original-regrade.json` retains the experiment's
offline replay time; `offline-regrade.json` is the public deterministic replay evidence.

| Strategy | Accepted | Actual input + output tokens | Reference USD | Cached input | Sum of run seconds |
|---|---:|---:|---:|---:|---:|
| fixed_strong (Astra xhigh) | 6/6 | 102162 | 1.237460 | 0 | 207.969153 |
| fixed_mid (6.1 Sol high) | 6/6 | 105285 | 0.1921092 | 24832 | 230.328798 |
| router | 6/6 | 101392 | 0.1621807 | 0 | 178.478042 |

Router reference cost was 86.89% below fixed Astra and actual tokens were 0.75% lower.
Six tasks and one repetition do not establish broad quality equivalence. The fixed
Sol group had 24832 cached input tokens; caching affects this cost comparison.
USD is frozen Standard token-price arithmetic, not a measured subscription charge.
Models and efforts are requested CLI settings; server-side identity is unobserved.
Reasoning tokens are already included in output, and cached input is included in input.
Run seconds are summed elapsed times, not end-to-end pipeline wall-clock latency.
The pilot tests a one-shot selector, with no retries, escalation or agent workflow.
The registry supports other provider descriptions; execution here is Codex only.

From the repository root, with Python 3.9+ and no additional dependencies:

```sh
python3 tools/export_pilot.py verify
```

To recreate the export from a separately retained private experiment, supply the
experiment directory. Its sibling `NAME-report/` and `NAME-regrade.json` are required.
The output directory must not exist. Local source paths are command arguments only
and are never persisted in the package:

```sh
python3 tools/export_pilot.py export --source "$PILOT_SOURCE" --output "$PUBLIC_OUTPUT"
python3 tools/export_pilot.py verify --package "$PUBLIC_OUTPUT"
```

Verification checks the complete file inventory and byte SHA-256 values; canonical
JSON manifest, prompt, price and artifact-content hashes; all 16 frozen source
hashes; result/job identity; per-run Decimal price calculations; and exact saved
report totals. It runs the frozen independent grader in serial child processes and
compares all 18 deterministic grades to saved evidence. Replay uses a scratch copy
of the source so imports cannot add bytecode to the published package. The grader
has timeout/CPU/memory limits and restricted Python execution; this is not an OS
sandbox. Use only this reviewed synthetic evidence. No live executor is invoked.

`PROVENANCE.json` records original file hashes and the original manifest hash. The
public manifest substitutes `source-snapshot/scripts` for one private host path,
recalculates its canonical hash, and updates selected manifest references. UUIDs,
if any, receive deterministic public IDs; the mapping to private values is omitted.
Frozen source and generated artifacts must remain byte-identical or export fails.
The current source contained no thread/request/decision UUIDs in selected evidence.
The immutable source snapshot includes a reference-answer module to preserve the
original 16-file freeze; offline replay uses graders and artifacts, never that module.
Checksums show internal integrity, not independent authentication of the original
execution. The original private experiment is never modified.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--source", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, default=DEFAULT_PACKAGE)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    args = parser.parse_args()
    try:
        if args.command == "export":
            report = export(args.source.resolve(), args.output.resolve())
        else:
            _, report = verify(args.package.resolve())
        print(json.dumps({"accepted": "18/18", "model_requests": 0, "groups": {
            strategy: {"tokens": group["token_totals"]["total_tokens"], "reference_usd": group["total_cost_usd"]}
            for strategy, group in report["groups"].items()}}, sort_keys=True))
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as error:
        print("pilot verification failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
