# Public pilot: provenance and offline reproduction

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
