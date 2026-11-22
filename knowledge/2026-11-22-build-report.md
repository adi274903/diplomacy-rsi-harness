# Build report: Diplomacy self-play RSI harness

**Date:** 2026-11-22
**Repo:** https://github.com/adi274903/diplomacy-rsi-harness
**Status at time of writing:** a live run (`watch1`) is in progress. These numbers come from its first generation.

---

## 1. TL;DR

- **What we built:** a working harness where several LLM agents, **each a different lab's model**, play Diplomacy against each other in real time. Every message carries:
  - the sender's private truth/lie label
  - the receiver's trust judgment (p_lie, decision and reasoning)

  A **privileged judge (Claude Opus 5.5)** reviews each finished game. It proposes **playbook edits**, and a statistical gate accepts or rejects them. Learning is **prompt/playbook-level only**: no model weights are trained.
- **Infrastructure:**
  - Rust: an API gateway, plus metrics, bootstrap, contamination and leak checks.
  - Python: the orchestrator, game environment, judge, gate and evolution.
  - A real-time web viewer with a decision-trace graph.
  - A reproducible environment: lockfiles, devcontainer, Makefile, and green CI.
- **First live results** (4 countries: Inkling 2 Mini / Claude Haiku 5.5 / DeepSeek V4.1 Flash / Mistral Small):
  - The games run cleanly: about 9 minutes per game to 1903, under 1% failed calls, and 0 isolation leaks.
  - Opus's reviews are substantive. It found real reasoning failures and proposed sensible, general playbook edits.
- **Biggest finding:** agents almost never *self-label* a message as deceptive. In ~180 messages there was 1 self-labelled lie, while Opus found 3 broken promises. So the self-play gate set had **no lies**, the gate could not measure improvement, and **every candidate playbook was rejected (AUPRC undefined)**.
- **The fix is designed** (§8): count reviewer-detected broken promises as deception, and play longer, more numerous gate games.
- **Total spend so far:** about $0.85 ($0.36 players + $0.49 judge), plus ~$0.10 in earlier probes.

---

## 2. Goal and how the design evolved

**Original ask:** an RSI (recursive self-improvement) harness for *reasoning-traced subjective decision making*, using the Diplomacy deception dataset from Peskov et al. (ACL 2020, "It Takes Two to Lie").

| Step | Decision | Why |
|---|---|---|
| 1 | Replace the earlier BERT-classifier plan (`.pi/plan/…`) with an LLM reasoning-trace harness | The ask was about reasoning traces and self-improvement, not a classifier |
| 2 | **Self-play environment.** Agents actually play Diplomacy; a reviewer grades them; the human dataset is used only as an uncontaminated benchmark | The user asked for "an environment where the actual game is played … reviewer reviews … no contamination" |
| 3 | Target model: **Thinking Machines Inkling 2 Mini** (released 2026-11-20) | User choice |
| 4 | Serving plan: A100 → **H100** on Autolab | The Inkling checkpoint is NVFP4/MXFP8 only; vLLM/SGLang list Hopper/Blackwell, not Ampere |
| 5 | **Pivot to hosted APIs, playbook-only learning.** Dropped self-hosting and LoRA/RL | User decision: "use apis … no rl with weight changes, more playbook changes" |
| 6 | Rust where it helps latency: the gateway (cache, budget, admission) and CPU hot paths | User asked for "optimized low latency code using rust" |
| 7 | Tinker (`TL_LAB`) for Inkling, then **OpenRouter only** | User decision. The first OpenRouter key had $0 credit (see §6.4); the dedicated `OPEN_ROUTER_RSI` key works |
| 8 | **Different model per country**, 4 active countries, live viewer, decision graphs | User asked to "see multiple models as different countries … live" |
| 9 | One game at a time with streamed messages, so RSI is visible game by game | User asked for "1 game but 3-4 agents playing it" |

---

## 3. What was made

### 3.1 Components

| Path | What it does |
|---|---|
| `crates/rsi-gateway` (Rust) | OpenAI-compatible gateway in front of the provider APIs. Content-addressed response cache (blake3) for free, reproducible replays. USD + token budget, using the provider-reported `usage.cost` when present. Retries 429/5xx and OpenRouter's in-flight-credit 402 while honouring `Retry-After`. Strict-priority admission (game turns ahead of bulk eval). Prefix-affinity routing. `/stats` JSON and `/metrics` Prometheus. A `bench` load generator with harness-shaped prompts. |
| `crates/rsi-core` (Rust, PyO3) | Weighted lie-detection metrics matching sklearn. Paired **cluster** bootstrap (resamples whole dialogs). N-gram contamination index. Aho-Corasick prompt **leak guard**. Pure-Python fallback with identical semantics, cross-checked in tests. |
| `rsi/env/` | `game.py`: MILA `diplomacy` engine wrapper; any map, a subset of active powers, legal-order validation, SVG board rendering. `bus.py`: private bilateral messages. `vault.py`: private state plus a structural and content isolation check on **every** player prompt. |
| `rsi/agents/` | `player.py`: send / judge / orders turns, per-power model override, mock policies. `reviewer.py`: privileged judge that grades promise kept/broken, reasoning quality and leak flags, and proposes ≤3 playbook diffs per game. |
| `rsi/genome.py`, `rsi/evolve.py` | The genome is system prompt + playbook + exemplars + effort + threshold, content-hashed. Diffs are linted (no game specifics, no human-data n-gram overlap), voted on, and built into candidates. Also: exemplar mining from self-play, an effort candidate, and a Pareto front. |
| `rsi/gate.py` | Accepts a candidate only if self-play AUPRC improves in ≥80% of bootstrap resamples, ECE gets no worse than +0.02, **and** human-validation AUPRC does not drop by more than 0.01. |
| `rsi/human_data.py`, `rsi/contamination.py` | Benchmark loader (handles the dataset's quirks). Memorisation probe, paraphrase test, n-gram dedup. |
| `rsi/runner.py` | One game. Checkpoints every round and phase. Optional event streaming for the live view. Per-agent activity. |
| `rsi/loop.py` | CLI: `smoke`, `human-stats`, `run`, `final-test` (lock-protected, run once), `report`. Publishes `rsi_status.json`. |
| `rsi/causal.py` | Per-phase **decision-trace graph**: plan → message → judgment → (cited by) later plan/orders → orders → SC change. |
| `rsi/viewer.py` | Static and live viewer: `/live` follow mode, `/api/game`, `/api/rsi`, `/api/current`, `/api/stats`. Mounts `/ui/<app>` from `web/` and hot-reloaded `/api/x/*` from `rsi/webapi.py`. |
| `web/game/` | Polished live spectator UI, built in a parallel session. |
| `web/rsi/`, `rsi/webapi.py` | RSI dashboard with F1 telemetry, playbook changelog and an interactive decision-graph explorer. **In progress** in a parallel session; not yet committed. |
| Dev environment | `uv.lock`, `Cargo.lock`, `Makefile`, `.devcontainer/`, GitHub Actions CI (Rust + Python tests, mock RSI loop, viewer artifact). |
| Configs | `mock.yaml` (no model), `api.yaml`, `live_demo.yaml` (parallel games), `live_one.yaml` (one watchable game at a time) |

### 3.2 Architecture

```
loop ── league / genomes ──► runner (1..N games, asyncio)
            │                   diplomacy engine + bus + vault (+ Rust leak guard on every prompt)
            │                   players: per-country model ──► rsi-gateway :9000 ──► OpenRouter
            ▼
     game records (privileged) ──► judge (Opus 5.5) ──► rsi-gateway :9001 ──► OpenRouter
            │                         grades + ≤3 playbook diffs
            ▼
     evolve (lint, vote, candidates) ──► gate (bootstrap on frozen gate replays + human-val transfer)
            │
            └──► viewer / web UIs (live game, RSI dashboard, decision graphs)
```

### 3.3 Verification state

- **Tests:** 15 Rust tests and 17 Python tests pass. CI is green on GitHub.
- **Mock backend:** a full game and the full RSI loop run end to end. The gate correctly rejects no-op playbooks.
- **Real APIs:**
  - one year of Inkling self-play on Tinker
  - a 4-model game on OpenRouter
  - the live run `watch1` (below)

---

## 4. How to run

```bash
make setup && make test                     # env from lockfiles, Rust + Python tests
make smoke && make run                      # mock model, no cost
cp .env.example .env                        # OpenRouter base URL + key, budgets
scripts/gateways.sh                         # :9000 players, :9001 judge
python -m rsi.loop run --config configs/live_one.yaml --run-name watch1
python -m rsi.viewer serve runs/watch1 --dev --gateway player=http://127.0.0.1:9000 --gateway judge=http://127.0.0.1:9001
# http://127.0.0.1:8765/live    (follows the game being played)
# http://127.0.0.1:8765/ui/game/  ·  http://127.0.0.1:8765/ui/rsi/
```

---

## 5. Results so far (live run `watch1`)

**Setup:**
- **Map and seats:** standard map; 4 agent countries; England, Russia and Turkey uncontrolled.
- **Agents:**

  | Country | Model |
  |---|---|
  | Austria | Inkling 2 Mini |
  | France | Claude Haiku 5.5 |
  | Germany | DeepSeek V4.1 Flash |
  | Italy | Mistral Small |

- **Judge:** Claude Opus 5.5.
- **Games:** played to 1903, 2 press rounds per phase, ≤2 messages per agent per round.

| Game | Role | Messages | Self-labelled lies | Caught | False alarms | Calls (failed) | Invalid orders | Final SC (AUS/FRA/GER/ITA) | Wall time |
|---|---|---|---|---|---|---|---|---|---|
| gate0 | frozen baseline (v0) | 91 | 0 | 0 | 8 | 120 (3) | 3 | 3 / 5 / 7 / 4 | 524 s |
| g1_0 | generation 1 (v0) | 87 | 1 | 1 | 8 | 122 (1) | 0 | 3 / 3 / 8 / 3 | 551 s |

**Opus review of g1_0:**
- **Grades:** 87 judgments graded. Promises: 80 kept, **3 broken**, 3 no promise, 1 unclear.
- **Key failure found:** Italy's "completely quiet" pledge was followed by `A VEN–TRI`, and Austria trusted it at p_lie 0.15 based only on Italy's track record.
- **Systematic flaw found:** receivers **cited current-phase orders they could not know yet**. That is invented evidence.
- **Proposed edits** (2 passed lint, 1 rejected), paraphrased:
  1. A record of restraint is not protection against a first strike. Weigh adjacency and the sender's growth options.
  2. Only cite orders that have actually resolved; never assert a sender's current-phase orders.
  3. When a sender promises to stay "away from" a region, check the adjacency of every destination it names.

**Gate:** all 4 candidates were **rejected**:
- AUPRC is undefined (the gate set has 0 lies).
- Two candidates also worsened ECE, by 0.082 and 0.052.
- The v0 baseline ECE was 0.388, because receivers gave honest messages an average p_lie of 0.39.

**Telemetry:**

| | Calls | Spend | p50 latency | Prompt tokens served from cache |
|---|---|---|---|---|
| Players | 381 | $0.36 | 5.5 s | 13% (Tinker gave 42%) |
| Judge | 3 | $0.49 | ~50 s | — |

---

## 6. What we learned

### 6.1 About the agents and the task
1. **LLM agents rarely admit deception in their private labels.** About 180 messages produced 1 self-labelled lie. The human dataset has 4–9% lies, and agents still break promises (Opus found 3). **Self-labels alone are too sparse to train or gate on.** Broken-promise detection by the judge is a necessary second label source.
2. **Receivers over-suspect honest messages.** There were 8 false alarms per game, and ECE was 0.39 on all-truthful gate items. Calibration is a real, measurable target for playbook learning.
3. **Receivers hallucinate evidence.** They cite unresolved current-phase orders. The judge catches this, and it is exactly the kind of general lesson a playbook can fix.
4. **The privileged judge is the most valuable component.** It produced specific, general, well-argued edits after a single game.
5. **Game dynamics:** DeepSeek's Germany grew fastest in both games (7–8 SCs by 1903), and Austria (Inkling) stagnated at 3. These are single games, so this is not evidence of model strength.

### 6.2 About the gate and statistics
6. **A gate needs positives.** AUPRC and lie-F1 are undefined with zero lies, so every candidate is rejected. The gate set must be built from enough games, with a label definition that produces lies.
7. **Few lies means noise.** Human validation has only **56 lies** in 1,416 messages, so the transfer check is noisy. Bootstrap CIs, clustered by dialog, are essential, and F1 should be reported with n_lie.

### 6.3 About the human benchmark (Peskov et al. 2020)

| Split | Dialogs | Messages | Lies | Lie rate | Games | Humans caught lies |
|---|---|---|---|---|---|---|
| train | 189 | 13,132 | 591 | 4.5% | 1–3, 5–10 | 11.9% |
| validation | 21 | 1,416 | 56 | 4.0% | 11 | 8.0% |
| test | 42 | 2,741 | 240 | 8.8% | 4, 12 | 20.3% |

- **Lie labels:** a lie is `sender_labels == false` (true means truthful).
- **Receiver labels:** `receiver_labels` mixes booleans with `"NOANNOTATION"`.
- **Types:** scores and years are strings.
- **Field name:** the README calls it `score_delta`, but the data uses `game_score_delta`.
- **Avoid:** the repo's `singlemessage_format.py` shuffles messages and destroys context.
- **Repo URL:** `DenisPeskoff/2020_acl_diplomacy`, with a double "f".
- **Human detectors are poor.** Receivers caught only 8–20% of lies, so a low bar, but a hard task.
- **Contamination risk:** the dataset has been public since 2020, so the harness includes a memorisation probe and a paraphrase test (not yet run).

### 6.4 About models, providers and serving
- **Inkling 2 Mini:**
  - 26.4B-parameter MoE with 3.4B active, looped layers (6–17 run three times).
  - Native NVFP4/MXFP8 checkpoint of about 20 GB.
  - vLLM/SGLang support at launch used pre-release images and listed Hopper/Blackwell only, **not A100**.
  - llama.cpp GGUFs exist (from b12600).
- **Tinker's OpenAI-compatible API** (`…/services/tinker-prod/oai/api/v1`):
  - **ignores `response_format`**, so the schema goes in the prompt and the reply is validated and repaired
  - uses `max_completion_tokens`, not `max_tokens`
  - takes `reasoning_effort` at the top level
  - returns `reasoning_content`
  - median latency about 7 s per call
- **OpenRouter:**
  - reserves `max_tokens × price` for *every in-flight request*. With low or zero credit, parallel agents get `402 in_flight_budget_exhausted`, even though each call is cheap. Keep `max_tokens` tight and treat 402 with `Retry-After` as retryable.
  - returns `usage.cost`, which gives exact spend with mixed models.
- **Model quirks the harness now tolerates:**
  - Mistral wraps JSON in code fences.
  - Claude Haiku writes orders as `PAR: A PAR H`.
  - Reasoning text can surround the JSON, so the parser takes the last JSON object.
- **Prefix-ordered prompts pay off.** System/genome first, then the shared board, then the private part. That gave 42% cached prompt tokens on Tinker.

### 6.5 Engineering lessons
- The **response cache plus deterministic per-call seeds** make mock and real reruns free. Streaming mode trades away byte-identical replay (message ids depend on timing), so it is a config flag.
- **Isolation needs two checks.** First, a structural check: a player only sees its own dialogs. Second, a content scan of the prompt with legitimately received messages removed, because quoting a message to a third party is legal gossip. A game with any leak is marked invalid.
- **Ops pitfall:** `pkill -f "<pattern>"` can kill the shell that runs it, because the pattern appears in its own command line. Use `pkill -f "[r]si…"`.
- **Parallel UI builds in one checkout work** when each session owns disjoint files, and the server exposes a static mount plus a hot-reloaded API module.

---

## 7. Costs

| Item | Spend |
|---|---|
| Probes (Tinker + OpenRouter) | ~$0.10 |
| `watch1` players (2 games, 381 calls) | $0.36 |
| `watch1` judge (Opus, 1 review) | $0.49 |
| **Total** | **≈ $0.95** |

- **Budget:** $15 across both gateways ($10 players, $5 judge).
- **Per generation:** about $0.15–0.20 for the players and about $0.50 for the Opus review. **The judge dominates cost.** Options: a cheaper judge, or reviewing in fewer, larger chunks.

---

## 8. Open issues and next steps

1. **Fix the gate signal (blocking RSI):**
   - set `label_mode: either` (self-label **or** a broken promise found by the judge)
   - have the judge review gate games too
   - 2+ gate games, played to 1905
   - restart the run
2. **Finish and commit the RSI dashboard.** It is in progress: F1 per genome and generation, live per-model F1, playbook changelog, decision-graph explorer. Also review and commit the live-game UI (`web/game/`).
3. **Validate the judge.** Measure Opus's agreement with human labels on the validation split, and spot-check reviews by hand.
4. **Run the contamination checks** (memorisation probe, paraphrased test), then `final-test` once, after the playbook has actually improved.
5. **Container:** `docker-compose.yml` with the gateways, runner and viewer, for the team.
6. **Longer and more games per generation** once the gate works, to get statistical power.

---

## 9. Sources
- Peskov et al., ACL 2020, "It Takes Two to Lie": data at [DenisPeskoff/2020_acl_diplomacy](https://github.com/DenisPeskoff/2020_acl_diplomacy)
- [Inkling 2 Mini, Thinking Machines](https://thinkingmachines.ai/news/inkling-2-mini/)
- [HF model card](https://huggingface.co/thinkingmachines/Inkling-2-Mini)
- [vLLM recipe](https://recipes.vllm.ai/thinkingmachines/Inkling-2-Mini)
- [SGLang cookbook](https://docs.sglang.io/cookbook/autoregressive/ThinkingMachines/Inkling-2-Mini)
- [Tinker docs: OpenAI-compatible inference](https://tinker-docs.thinkingmachines.ai/inference/openai/overview/)
- [OpenRouter API](https://openrouter.ai/docs)
- MILA `diplomacy` engine: https://github.com/diplomacy/diplomacy
