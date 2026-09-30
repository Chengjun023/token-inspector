# Engineering notes

The project makes usage comparisons inspectable by preserving identities, unknowns, historical prices, and verification evidence. Python backends use the standard library; the macOS window uses SwiftUI/AppKit.

## Three different quantities

| Quantity | Definition | Interpretation |
| --- | --- | --- |
| Actual tokens | Recorded input + output, with cache and reasoning as subsets | Observed usage, subject to the displayed coverage |
| Reference USD | Usage × the request's frozen API Standard model prices | A comparable reference cost, separate from subscription billing |
| Astra-equivalent savings | Reference price difference expressed in Astra-equivalent token units | Price-equivalent allowance, separate from actual tokens avoided |

Input includes cached input. Output includes reasoning output. Adding those subsets again double-counts usage. Prices, usage, and requested-model evidence must be available for a complete reference total; absent or invalid values remain unknown. Requested models and local task settings are the available evidence, not per-token server-side actual-model telemetry.

Float's automatic comparison prices the same measured token structure under Astra and the recorded models. A parent's savings view can include visible descendants; each row's task token count retains its own scope. Manual token-reduction scenarios remain separate from the automatic observed comparison.

## Persistence and privacy

Official Codex databases are read-only. The owned usage ledger stores numeric fields plus the IDs, model identifiers, price hashes, timestamps, and checkpoints required for durable deduplication. It saves no task, conversation, reasoning, or tool body. Router decisions retain configuration, feature summaries, structured dispatch/acceptance evidence, and provenance hashes. Local task briefs used for fallback scoring remain in memory.

Restarting Float or rotating an event file must not add the same request again. Historical pricing is attached at first metering, so a refreshed registry cannot rewrite earlier costs. These are persistence contracts, not display conveniences.

## Failure semantics

| Condition | Result |
| --- | --- |
| Public feed refresh fails | Keep the last-good/bundled usable snapshot; expose stale/failure state and cooldown |
| Candidate lacks capability or host evidence | Exclude it from executable selection |
| Required strong route or explicit pin is unavailable | Report an error rather than silently lowering the requirement |
| Model metadata disappears from the current catalog | Use the current allowed intersection and disclose removals/fallback |
| Usage or required price is missing/invalid | Preserve unknown or partial coverage |
| A task has no recent event in an unfinished turn | Show a state needing confirmation; a long tool call can also reach it |
| Dispatch succeeds without verification | Acceptance remains unknown/pending |
| Benchmark code/snapshot hash drifts | Block new live requests or disclose drift during offline analysis |
| Benchmark leaves an unresolved started job | Stop resumption until it is explicitly resolved as interrupted/unknown |

The experimental `router/scripts/live.py` settings path is separate from the native-subagent path. It has simulated validation for the local protocol; a verified shared desktop control socket is required before treating it as live main-task switching.

## Test and build discipline

From the repository root:

```sh
python3 -m unittest discover -s router/tests -q
python3 -m unittest discover -s float/tests -q
```

The imported baseline contains 130 router tests and 50 Float tests. Coverage includes thresholds and pins, host/capability intersections, registry validation and recovery, acceptance idempotency, learning isolation, unknown accounting, request deduplication, log rotation, persistent totals, read-only connections, and turn-index lag. Tests are local and do not require inference calls.

Build the native application with `cd float && sh scripts/build.sh` on macOS 14+ with Xcode Command Line Tools. The output is `float/build/Codex Float.app`, signed ad hoc. There is no prebuilt DMG. For shared backend changes, run `python3 float/scripts/sync_router.py --source router` and inspect the vendored hashes before testing both components.

## Reproducing the pilot

The [public result directory](../router/benchmarks/results/pilot-public/) contains the sanitized evidence; the [benchmark protocol](../router/benchmarks/README.md) documents command and artifact semantics. Offline demo and replay do not call a model. Live requests require `--live`, a fixed manifest, an existing Codex login, and a new output directory.

The pilot ran six synthetic tasks under three strategies once each, for 18 requests. Each group passed 6/6 deterministic graders:

| Strategy | Tokens | Cached input tokens | API Standard reference USD |
| --- | ---: | ---: | ---: |
| Fixed Astra | 102,162 | 0 | $1.237460 |
| Fixed 6.1 Sol | 105,285 | 24,832 | $0.1921092 |
| Router | 101,392 | 0 | $0.1621807 |

Router versus fixed Astra: 86.89% lower reference cost, 0.75% fewer actual tokens. This is a one-shot selector comparison on six tasks. General quality equivalence and complete multi-agent gains require broader, repeated experiments. Cache asymmetry and reference-price assumptions are part of the result.

The CLI benchmark adapter uses structured outputs, public fixtures, bounded execution, and an isolated grader. It records protocol violations and preserves failed/interrupted work. The grader is a restricted synthetic-program checker; its Python restrictions are not a general-purpose operating-system sandbox for arbitrary hostile programs.
