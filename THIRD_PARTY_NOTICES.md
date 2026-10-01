# Sources and attribution

Agent Smith uses Python's standard library and Apple's SwiftUI/AppKit frameworks.
No model weights, private Codex data, provider SDKs, or credential files are bundled.

Model catalog refreshes use [models.dev](https://models.dev/), an open-source catalog
maintained by [sst/models.dev](https://github.com/sst/models.dev). Candidate metadata
and capability evidence are kept separate from locally verified execution access.
The bundled registry is a curated local snapshot with per-record source URLs and
price dates. Public API reference prices do not measure subscription charges.

Codex and model names identify compatible services. This project is independent of
OpenAI and other providers. The logo-head lawyer advertisement is AI-generated artwork
inspired by the visual language of Better Call Saul ads, with a ChatGPT-style knot
emblem pasted onto the lawyer. Better Call Saul and the OpenAI/ChatGPT marks
belong to their respective owners; the artwork is a playful project advertisement. The native screenshot uses the
repository's explicitly labeled synthetic demo fixture.

The public pilot includes generated solutions to this project's synthetic tasks,
along with frozen source and offline graders. Export provenance and byte checksums
are recorded in `router/benchmarks/results/pilot-public/`.
