# Contributing

Small, inspectable changes are welcome. Agent Smith (史密斯专员) is maintained by Chengjun (`Chengjun023`), with Python standard-library backends and a native SwiftUI/AppKit window.

For a bug report, include the component, OS/Codex/Python version, a minimal synthetic reproduction, expected behavior, and observed behavior. Redact task titles, paths, IDs, credentials, and conversation content before sharing logs or screenshots. A fixture that reproduces the failure is usually more useful than a copy of a live Codex database.

## Working locally

From the repository root:

```sh
python3 -m unittest discover -s router/tests -q
python3 -m unittest discover -s float/tests -q
```

For native UI changes, build on macOS 14+ with Xcode Command Line Tools:

```sh
cd float
sh scripts/build.sh
open "build/Codex Float.app"
```

Shared difficulty and registry code originates in `router/`. If you change it, synchronize Float's vendored copies and check both suites:

```sh
python3 float/scripts/sync_router.py --source router
```

Include meaningful tests for changed decision, metering, persistence, or failure behavior. For visual changes, supply a screenshot with synthetic demo data and say which display mode and scaling you checked.

## Contracts to preserve

- Official Codex storage stays read-only. Application writes belong in owned state and preference stores.
- Usage and prices have explicit unknown states. Cached input is part of input; reasoning output is part of output. Neither subset is added twice.
- Existing ledger entries and benchmark artifacts keep their original prices and provenance.
- Public model metadata starts as a candidate. Execution requires capability, endpoint, and host evidence.
- Dispatch success and process exit are separate from quality acceptance. Pins, rejection evidence, and benchmark isolation remain visible.
- Routine scoring and metering remain local. A change that introduces model calls, network activity, or another dependency should explain why.

## Benchmark changes

Use the [benchmark protocol](router/benchmarks/README.md). `demo` checks the harness with simulation data; `replay` regrades saved artifacts offline. A live experiment requires an explicit live command and a new output directory. Report model/effort, catalog and price snapshots, grader/code hashes, cache tokens, failures, coverage, and request count alongside headline results.

Keep production learning separate from benchmark outcomes. Preserve failed and interrupted jobs in the denominator. An interrupted request with uncertain usage must be resolved as unknown before another experiment; silently retrying it can hide cost.

## Pull requests

Describe the concrete problem, the resulting behavior, and how you verified it. Link the relevant issue when there is one. Scope the patch so a reviewer can follow the change without reconstructing a whole Codex session.

Useful discussion topics include score calibration, cache-aware comparisons, evidence thresholds for local feedback, adapter compatibility, and ways to show partial coverage clearly. Proposed work lives in the [roadmap](docs/roadmap.md).
