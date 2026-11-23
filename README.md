# Diplomacy RSI Harness

[![ci](https://github.com/adi274903/diplomacy-rsi-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/adi274903/diplomacy-rsi-harness/actions/workflows/ci.yml)

A self-improvement harness for **reasoning-traced trust decisions**:
1. **LLM agents play Diplomacy against each other.** They negotiate in private, label their own messages truthful or deceptive, and judge every message they receive with a lie probability.
2. **Claude Opus 5.5 is the privileged judge.** It sees everything, reviews finished games with each agent's decision-trace graph, and rewrites the agents' shared **trust-assessment playbook**.
3. **A statistical gate decides.** A rewrite is kept only if it improves lie detection on **real human games** from the Peskov et al. (ACL 2020) deception dataset, and accepted versions go live in every running game.

No weights are trained; all learning is at the prompt level.

**Live demo:** https://34-59-183-174.sslip.io/ui/rsi/ · **Technical report:** [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md)

![Live game view](docs/screenshots/live-game.png)

---

## Key results

| | Result |
|---|---|
| **Playbook learned by Opus from human games** (human-v2), on the held-out test set | AUPRC **0.150 → 0.195** (Δ +0.044, 95% CI [+0.016, +0.087]); ECE **0.275 → 0.143**; AUROC **0.73** vs **0.58** for the human receivers |
| **Self-play refinement** (rsi-v1, written by Opus after reviewing self-play games), on the human gate games | AUPRC **0.107 → 0.142**; lie-F1 **0.072 → 0.152**; accepted with 99% bootstrap confidence |
| **Vs the paper** (test lie-F1 at a 0.5 cutoff) | **18.4** for us, vs **20.9** (LSTM+Power+BERT), **27.0** (best model) and **22.5** (humans). **Not beaten** on this metric. |
| **Is it RSI?** | An automated, gated self-improvement loop with an external critic. **Not** strong RSI: the critic never improves itself, and gains flattened after one accepted step. |

Details, caveats and what did not work are in the [technical report](docs/TECHNICAL_REPORT.md).

## Live demo

| Page | What it shows |
|---|---|
| [RSI dashboard](https://34-59-183-174.sslip.io/ui/rsi/) | Playbook versions, Opus's edits with rationale, gate decisions with confidence intervals, F1/AUPRC per version, standings, decision-graph explorer |
| [Live game view](https://34-59-183-174.sslip.io/ui/game/) | Board, message threads with each sender's private intent and each receiver's lie probability, a model per country, the winner |
| [Example game played with rsi-v1](https://34-59-183-174.sslip.io/ui/game/?f=/games/game_v1demo.html) | 43 messages, 5 lies, 4 caught ([decision-trace graph](https://34-59-183-174.sslip.io/games/game_v1demo.html)) |
| [Play against the models](https://34-59-183-174.sslip.io/ui/play/) | You take a country; the models play the rest (password-protected) |

The deployment is in dashboard-only mode. Live games need API budget (see [Deployment](#deployment)).

<details>
<summary><b>More screenshots</b></summary>

**Playbook evolution:** the heuristics Opus added and the candidates the gate rejected
![Playbook update history](docs/screenshots/playbook.png)

**Decision-trace graph:** plan → message → judgment → the later reasoning that cites it, with caught and fooled lies marked
![Decision graph explorer](docs/screenshots/decision-graph.png)

**Standings:** 101 games with models rotated across countries; leaderboards, deception stats and lie-F1 per model
![Standings](docs/screenshots/standings.png)
</details>

## How it works

```
 game slots (parallel, random length, models randomly seated)  ──►  finished games + decision graphs
      ▲  every agent reads the current playbook                              │
      │                                                                      ▼
 accepted playbook goes live in all running games        Claude Opus 5.5 reviews them (privileged view)
      ▲                                                                      │  proposes playbook edits
      │                                                                      ▼
 gate: paired cluster bootstrap on human games 7–8  ◄──  lint (no game specifics, no held-out overlap)
       (Inkling 2 Small judges; AUPRC gain + ECE check)
```

- **Players** are four similar-size open reasoning models, rotated across countries every game: Inkling 2 Mini, Qwen3.6-35B-A3B, Gemma 4 26B-A4B and Nemotron 3 Nano 30B-A3B.
- **Isolation:** every prompt is checked so a player only sees its own conversations. A game with any leak is discarded.
- **Ground truth for deception** is the sender's private label **or** a promise that Opus found broken against the orders actually played.
- **Human data is evaluation-only:**

  | Games | Use |
  |---|---|
  | 1, 2, 3, 5, 6, 11 | Learning the human playbook |
  | 7–8 | Gate |
  | 9–10 | Clean report |
  | 4, 12 | Held-out test (used once) |

- **Every model call goes through a Rust gateway:** response cache, spend caps, retries and priority admission.

## Repository layout

| Path | Contents |
|---|---|
| `rsi/env/` | Diplomacy engine wrapper, private message bus, vault with isolation checks |
| `rsi/agents/` | Model players, the Opus reviewer, a human seat for live play |
| `rsi/continuous.py` | Event-driven continuous RSI loop (used in production) |
| `rsi/loop.py`, `rsi/cadence.py` | Generation-based and fixed-cadence loops; `final-test` (lock-protected) |
| `rsi/genome.py`, `rsi/evolve.py`, `rsi/gate.py` | Playbooks, edit lint and voting, candidates, the acceptance gate |
| `rsi/learn_human.py` | Opus learns a playbook from labelled human games |
| `rsi/causal.py`, `rsi/decision_graph.py` | Decision-trace graphs; optional structured judgments |
| `rsi/human_data.py`, `rsi/contamination.py` | Benchmark loader; n-gram dedup, memorisation probe, paraphrase test |
| `rsi/viewer.py`, `rsi/webapi.py`, `rsi/play.py` | Dashboard server and APIs; live-play server |
| `web/game`, `web/rsi`, `web/play` | Spectator UI, RSI dashboard, play page |
| `crates/rsi-gateway` | Rust OpenAI-compatible gateway and load generator |
| `crates/rsi-core` | Rust (PyO3): weighted metrics, paired cluster bootstrap, n-gram index, Aho-Corasick leak guard |
| `playbooks/` | Every playbook version (human_v1, human_v2, rsi_v1) with diffs and metadata |
| `configs/` | `cadence.yaml` (current models and loop), `play.yaml`, `mock.yaml`, `api.yaml`; others are from earlier runs |
| `deploy/` | Container entrypoint, GCP VM / IAP / HTTPS scripts |
| `knowledge/` | Chronological build log and raw result files |
| `docs/` | Technical report, screenshots |

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Rust. You can also open the repo in GitHub Codespaces; the devcontainer sets everything up.

```bash
make setup    # Python env from uv.lock, Rust extension and gateway
```

```bash
make test     # 43 Python + 15 Rust tests
```

```bash
make smoke    # one self-play game on the mock model (no API cost)
```

```bash
make run      # full RSI loop on the mock model
```

```bash
make serve    # dashboard at http://127.0.0.1:8765
```

## Running with real models

1. **Configure.** Copy `.env.example` to `.env` and fill in your OpenRouter key, prices and budget caps.
2. **Start the gateways:** `scripts/gateways.sh` (players on :9000, judge on :9001).
3. **Run continuous RSI and its dashboard:**

```bash
python -m rsi.continuous --config configs/cadence.yaml --run-name run1 --games 10 --forever --stop-usd 20
```

```bash
python -m rsi.viewer serve runs/run1 --gateway player=http://127.0.0.1:9000 --gateway judge=http://127.0.0.1:9001
```

Other entry points:

| Command | What it does |
|---|---|
| `python -m rsi.learn_human` | Opus learns a playbook from human train games |
| `python -m rsi.play --power FRANCE --months 12` | Play a game yourself, at `http://127.0.0.1:8770/ui/play/` |
| `python -m rsi.loop final-test ... --genome <playbook>` | One-time held-out test evaluation |

**Budget note:** OpenRouter reserves `max_tokens × price` for every in-flight request. Low-credit or workspace-capped keys reject parallel calls (402/403). The harness detects this and stops rather than recording failed calls as results.

## Deployment

One container runs everything: gateways, loop, dashboards and play server. `deploy/gcp/` sets it up on a Compute Engine VM:
- the API key in Secret Manager
- an IAP-only firewall
- optional public HTTPS with Caddy, where dashboards are open and the play page needs a password

See [deploy/gcp/README.md](deploy/gcp/README.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md) | What was built, results, what worked and what did not, limitations |
| [knowledge/2026-11-22-build-report.md](knowledge/2026-11-22-build-report.md) | Chronological build log with every decision and incident |
| [playbooks/](playbooks/) | Playbook versions with the changes each version made |
| [PLAN.md](PLAN.md) | The original design document (partly superseded: the self-hosting and weight-training stages were dropped) |

## Acknowledgements

- **Data:** Peskov, Cheng, Elgohary, Barrow, Danescu-Niculescu-Mizil and Boyd-Graber, *It Takes Two to Lie: One to Lie, and One to Listen*, ACL 2020 ([data](https://github.com/DenisPeskoff/2020_acl_diplomacy)).
- **Game engine:** the [MILA diplomacy](https://github.com/diplomacy/diplomacy) adjudicator.
- **Contributions:** structured judgments and the human-train gate by [@dshkol](https://github.com/dshkol).
