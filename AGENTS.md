# Contributor map

This repository combines `router/`, `float/`, and a frozen public benchmark.
Read `docs/architecture.md` and `docs/engineering.md` before changing behavior.

- Python runtime uses only the standard library. Float requires macOS 14+.
- Edit shared scoring/registry definitions in `router/scripts/`; vendor them with
  `python3 float/scripts/sync_router.py --source router`, then test both components.
- Never write to official Codex databases, publish live task logs, or turn unknown
  usage/prices into zero. Keep request pricing frozen after first metering.
- Keep difficulty heuristics separate from calibrated success probabilities.
- Preserve frozen pilot files. New experiments use a new directory. Live inference
  requires an explicit request-count budget; offline replay never invokes a model.
- Demo mode must never launch a live reader. Screenshots use synthetic fixtures.
- Validate with `python3 tools/check_public.py`, both unittest suites, and
  `python3 tools/export_pilot.py verify`. Build Swift on macOS after UI changes.
- USD is API reference arithmetic, not subscription billing. Price-equivalent
  savings and observed token differences must remain separate in every report.
