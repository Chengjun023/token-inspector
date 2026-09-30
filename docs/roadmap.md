# Roadmap

The current release pairs Adaptive Router 0.3.0 with Codex Float 0.5.0. This roadmap names proposed work, not shipped capabilities or release promises.

## Current baseline

- Local rule-based difficulty and preferences, explicit model pins, capability/host admission, and evidence-backed acceptance.
- Public model candidate refresh with last-good recovery and versioned local prices.
- Read-only local Codex observation, native Float display, persistent request deduplication, current-turn/cumulative usage, and historical price freezing.
- A reproducible six-task, three-strategy one-shot pilot, with simulation and offline replay paths.

## Next experiments

| Question | Proposed work | Evidence needed |
| --- | --- | --- |
| Does the selector hold up beyond six tasks? | Add varied public tasks and repeated runs under a fixed request budget | Acceptance intervals, failures, token/cache coverage, latency, and reference cost per accepted result |
| Are difficulty thresholds useful? | Compare score bands against observed workload outcomes | Out-of-sample results and clear separation from training/feedback cohorts |
| How much does caching change comparisons? | Repeat matched conditions and report cache exposure explicitly | Input/cache/output breakdown, order, repetitions, and frozen prices |
| Does delegation pay for its repeated context? | Evaluate complete block decomposition and failure escalation | Total parent/child cost, acceptance, rework, and a matched single-agent baseline |

## Product and adapter work

Improve partial-coverage explanations and make the source of each displayed number easier to inspect. Add compatibility fixtures for changed Codex storage formats before declaring support for them. Explore signed release packaging after build and compatibility checks are reliable enough to maintain.

An API executor or another provider would require a separate implementation, credential design, capability evidence, and execution tests. Today those provider entries support catalog discovery. Automatic candidate discovery remains separate from local admission and executable host support.

## Discussion starters

What is a useful acceptance threshold for a personal router with sparse data? Which score explanations help users challenge a decision? How should a UI show price uncertainty without burying the useful number? A concrete fixture, counterexample, or experiment design is welcome through [contributions](../CONTRIBUTING.md).
