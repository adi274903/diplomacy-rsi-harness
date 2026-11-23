# Diplomacy RSI Harness

A harness for recursive self-improvement on reasoning-traced trust decisions in Diplomacy:
- **Players:** several copies of Thinking Machines' Inkling 2 Mini, called through a hosted API, play Diplomacy against each other.
- **Reviewer:** sees everything, grades the players' trust decisions, and proposes changes to their **playbook**.
- **Learning:** happens only at the prompt/playbook level. No model weights are trained.
- **Benchmark:** the human deception dataset (Peskov et al., ACL 2020) is used only for evaluation.

## Live deployment (GCP)

- **RSI dashboard:** https://34-59-183-174.sslip.io/ui/rsi/. Playbook versions, Opus's edits, gate verdicts, F1 and AUPRC per version, standings, and the decision-graph explorer.
- **Live game view:** https://34-59-183-174.sslip.io/ui/game/. Board, messages with intent and judgment reveals, a model per country, and a link to each game's decision-trace graph.
- **Play against the models:** https://34-59-183-174.sslip.io/ui/play/. Password-protected.
- **Example game played entirely with rsi-v1:** https://34-59-183-174.sslip.io/ui/game/?f=/games/game_v1demo.html. 21 months, 43 messages, 5 lies, **4 caught**. Its decision-trace graph is at https://34-59-183-174.sslip.io/games/game_v1demo.html.

These run on a Compute Engine VM in project `rsitry` (see [deploy/gcp/README.md](deploy/gcp/README.md)). The dashboards show the run where Opus's self-play refinement **rsi-v1** was accepted, at 99% gate confidence:
- gate AUPRC 0.107 → 0.142
- lie-F1 0.072 → 0.152

See [playbooks/rsi_v1.md](playbooks/rsi_v1.md) and [knowledge/](knowledge/).

The VM is currently in dashboard-only mode. Live games need OpenRouter budget (see `deploy/gcp/README.md` to restart the loop).

See [PLAN.md](PLAN.md) for the design and the contamination controls.

> **Status:** builds and passes all tests (15 Rust, 16 Python). The full self-play loop runs end to end on the mock backend. It has **not yet been run against a real model API**.

## Quickstart

**Easiest:** open the repo in GitHub Codespaces or the VS Code Dev Containers extension. The devcontainer installs Python 3.12, Rust and uv, then runs setup, the tests and a mock game.

**Locally** (needs [uv](https://docs.astral.sh/uv/) and Rust). Exact versions are pinned in `uv.lock` and `Cargo.lock`.

```bash
make setup     # .venv from uv.lock + Rust extension + gateway binary
make test      # Rust + Python tests
make smoke     # one 7-agent self-play game, mock model (no API cost)
make run       # full RSI loop on the mock (RUN=mock1)
make serve     # viewer at http://127.0.0.1:8765
```

## Viewing games and telemetry

- `make viewer RUN=<run>` writes static pages to `runs/<run>/index.html`. You can open them directly, or zip the run folder to share it.
- `make serve RUN=<run>` serves the same pages. It rebuilds the overview as new games finish and shows **live gateway telemetry**, refreshed every 5 seconds: requests, cache hits, estimated cost, tokens, latency percentiles, queue depth and retries.
- **Run overview:** baseline, a table per generation, every gate decision with its bootstrap CI, human-test results, and a list of games.
- **Game page:** the rendered board with each phase's orders (use the arrow keys to step through phases). It shows every message with the sender's private intent, the receiver's p_lie, its decision and reasoning, and the outcome (lie caught / fooled / false alarm). It also shows orders, supply centres and isolation-leak counts. This is the **privileged** view; players never see it.
- CI uploads the viewer for a mock run as a build artifact on every push.

## Live multi-model demo (OpenRouter)

There are four agent-controlled countries on the standard map, each played by a different lab's model. The other three countries are uncontrolled. Claude Opus 5.5 is the privileged judge. See `configs/live_demo.yaml`.

```bash
# .env: PLAYER_API_BASE / REVIEWER_API_BASE = https://openrouter.ai/api/v1, keys = your OpenRouter key
scripts/gateways.sh
python -m rsi.loop run --config configs/live_demo.yaml --run-name live1
python -m rsi.viewer serve runs/live1 --gateway player=http://127.0.0.1:9000 --gateway judge=http://127.0.0.1:9001
```

Game pages update in place while a game is being played: a live feed, the board, and messages with intent, p_lie and outcome. Each page also has a **decision-trace graph** per phase. Its edges run plan → message → judgment → (cited in) later plan/orders → orders → supply-centre change, and clicking a node shows that agent's reasoning.

> The OpenRouter account needs credits. On the free tier with $0 credit, OpenRouter rejects parallel requests with `402 in_flight_budget_exhausted`, because it reserves `max_tokens × price` for every request in flight.

## Real API run

```bash
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
| `rsi/decision_graph.py` | Opt-in structured judgments (`loop.structured_judgments: true`). Receivers answer with a decision graph: claims, evidence cited by message id, and log-odds steps from the base rate. Every judgment gets automatic checks (citations of unseen messages, contradictions cited without an adjustment, stated vs implied p_lie). The reviewer and the causal graph use it. |

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
