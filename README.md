# Diplomacy RSI Harness

A harness for recursive self-improvement on reasoning-traced trust decisions in Diplomacy:
- **Players:** several copies of Thinking Machines' Inkling 2 Mini, called through a hosted API, play Diplomacy against each other.
- **Reviewer:** sees everything, grades the players' trust decisions, and proposes changes to their **playbook**.
- **Learning:** happens only at the prompt/playbook level. No model weights are trained.
- **Benchmark:** the human deception dataset (Peskov et al., ACL 2020) is used only for evaluation.

See [PLAN.md](PLAN.md) for the design and the contamination controls.

> **Status:** all code is written but has **not yet been compiled or run**. Expect a round of fixes on the first `cargo test` / `pytest`.

## Quickstart

```bash
# 1. Python env (Python 3.10–3.13)
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
# optional: build the Rust hot paths (pure-Python fallback otherwise)
uv pip install -e crates/rsi-core

# 2. Tests + a full game with no model
pytest
python -m rsi.loop smoke --config configs/mock.yaml
python -m rsi.loop run   --config configs/mock.yaml --run-name mock1

# 3. Real run against hosted APIs
cp .env.example .env            # fill in provider URLs, keys, prices, budgets
scripts/gateways.sh             # player gateway :9000, reviewer gateway :9001
python -m rsi.loop human-stats --config configs/api.yaml
python -m rsi.loop smoke       --config configs/api.yaml
python -m rsi.loop run         --config configs/api.yaml --run-name sprint1
python -m rsi.loop final-test  --config configs/api.yaml --run-name sprint1   # exactly once
python -m rsi.loop report      --run-name sprint1
```

## Components
| Path | What |
|---|---|
| `crates/rsi-gateway` | Rust OpenAI-compatible gateway in front of the APIs. Content-addressed response cache, so reruns are free. USD/token budget. 429 retries that honour `Retry-After`. Priority admission. `/stats`, `/metrics`. `bench` load generator. |
| `crates/rsi-core` | Rust (PyO3): weighted lie-detection metrics matching sklearn, a paired cluster bootstrap, an n-gram contamination index, and an Aho-Corasick prompt leak guard. |
| `rsi/env` | Diplomacy engine wrapper, private bilateral message bus, and a private vault with isolation checks on every prompt. |
| `rsi/agents` | Player (send / judge / orders) and privileged reviewer. Mock policies let everything run without a model. |
| `rsi/evolve.py`, `rsi/gate.py` | Playbook evolution (diff voting, candidates, mined exemplars, Pareto front) and the acceptance gate (self-play bootstrap plus a human-val transfer check). |
| `rsi/human_data.py`, `rsi/contamination.py` | Human benchmark loader (handles its label quirks), plus dedup, memorisation probe and paraphrase test. |

## Gateway

```bash
cargo build --release -p rsi-gateway
./target/release/rsi-gateway serve --upstream https://YOUR_PROVIDER/v1 --cache-path runs/cache.bin --max-inflight 64 --usd-budget 50
./target/release/rsi-gateway bench --target http://127.0.0.1:9000/v1 --concurrency 1,8,32,64
```

Request headers it understands:
- `x-rsi-priority: high|normal|low`
- `x-rsi-run: <name>` (per-run accounting)
- `x-rsi-cache: off`
