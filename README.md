# Diplomacy RSI Harness

Self-play recursive-self-improvement harness for reasoning-traced trust decisions in Diplomacy.
Several copies of Thinking Machines' Inkling 2 Mini play Diplomacy against each other. A privileged reviewer grades their trust decisions and evolves their playbook. The human deception dataset from Peskov et al. (ACL 2020) is used only for evaluation.

See [PLAN.md](PLAN.md) for the design.

## Status (work in progress)
- `crates/rsi-core`: Rust hot paths exposed to Python through PyO3: weighted metrics with a paired cluster bootstrap, an n-gram contamination index, and an Aho-Corasick prompt leak guard. Written but not yet compiled or tested.
- `crates/rsi-gateway`: low-latency OpenAI-compatible gateway in front of vLLM/SGLang. Written but not yet compiled or tested.
- Planned next: the Python orchestrator (`rsi/`) and serving scripts for the H100 (Autolab) and Colab.

## rsi-gateway

```
harness (many async calls) ──► rsi-gateway :9000 ──► vLLM / SGLang :8000 (H100)
```

- **Priority admission.** `x-rsi-priority: high|normal|low`. Game turns run before bulk eval sweeps, and in-flight requests are capped by `--max-inflight`.
- **Response cache.** Keyed by a blake3 hash of the canonical request. Only seeded or greedy requests are cached, so reruns are free and reproducible. Bypass it with `x-rsi-cache: off`.
- **Prefix-affinity routing.** Requests that share a system prompt go to the same replica, so that replica's KV prefix cache stays hot.
- **Retries and budget.** Retries 429 and 5xx with backoff. `--token-budget` is a hard token cap, and the `x-rsi-run` header buckets token accounting per run.
- **Monitoring.** `/stats` returns JSON and `/metrics` returns Prometheus text.

```bash
cargo build --release -p rsi-gateway
./target/release/rsi-gateway serve --upstream http://127.0.0.1:8000 --cache-path runs/cache.bin --max-inflight 256
./target/release/rsi-gateway bench --target http://127.0.0.1:9000 --concurrency 1,8,32,64,128,256
```

`bench` replays traffic shaped like the harness: a shared genome prefix, a shared phase board, a short private suffix, and output constrained by a JSON schema. It reports TTFT and end-to-end p50/p90/p99 and throughput for each concurrency level. Use it to tune the engine flags against the real workload.

## Serving target
Production: vLLM or SGLang on an H100 80GB (Autolab), using the native NVFP4/MXFP8 checkpoint. Smoke tests: Google Colab.
