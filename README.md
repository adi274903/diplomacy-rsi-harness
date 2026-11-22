# Diplomacy RSI Harness

Self-play recursive-self-improvement harness for reasoning-traced trust decisions in Diplomacy.
Several copies of Thinking Machines' Inkling 2 Mini play Diplomacy against each other. A privileged reviewer grades their trust decisions and evolves their playbook. The human deception dataset from Peskov et al. (ACL 2020) is used only for evaluation.

See [PLAN.md](PLAN.md) for the design.

## Status (work in progress)
- `crates/rsi-core`: Rust hot paths exposed to Python through PyO3: weighted metrics with a paired cluster bootstrap, an n-gram contamination index, and an Aho-Corasick prompt leak guard. Written but not yet compiled or tested.
- Planned next: `crates/rsi-gateway` (low-latency OpenAI-compatible gateway in front of vLLM/SGLang), the Python orchestrator (`rsi/`), serving scripts for H100 (Autolab) and Colab.

## Serving target
Production: vLLM or SGLang on an H100 80GB (Autolab), using the native NVFP4/MXFP8 checkpoint. Smoke tests: Google Colab.
