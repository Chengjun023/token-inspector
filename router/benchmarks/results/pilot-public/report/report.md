# Router selector pilot

Manifest: `7189b64127eadc2c4e90f9ed2993efc44f0cc400782918baee409287ae67ea15`

one-shot deterministic selector pilot; no multi-agent or failure escalation

| Strategy | Completed/planned | Passed | Acceptance | Usage coverage | Total tokens | Price coverage | Total USD | USD/success |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed_strong | 6/6 | 6 | 1.0 | 1.0 | 102162 | 1.0 | 1.23746 | 0.2062433333333333333333333333 |
| fixed_mid | 6/6 | 6 | 1.0 | 1.0 | 105285 | 1.0 | 0.1921092 | 0.0320182 |
| router | 6/6 | 6 | 1.0 | 1.0 | 101392 | 1.0 | 0.1621807 | 0.02703011666666666666666666667 |

- One attempt per task and strategy; first and final acceptance coincide.
- Group price differences are observed group costs, not per-task savings.
- Unknown usage or model price remains unknown; total cost requires complete coverage.
- Cost per success includes all priced successes and failures; no retry or escalation.
- Pricing uses requested CLI model/effort; service-side actual-model identity is not claimed.
- USD amounts are frozen reference token prices, not measured subscription charges.
- Six synthetic tasks and one repetition are a pilot, not evidence of full routing workflow gains.
