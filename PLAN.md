# Plan: Self-Play RSI Harness for Reasoning-Traced Trust Decisions in Diplomacy

## Context
Several instances of the same agent play live Diplomacy games against each other: Thinking Machines **Inkling 2 Mini**, called through a **hosted API**. Every message carries:
- a reasoning trace
- a private truth/lie annotation from the sender
- a trust judgment from the receiver

A separate **reviewer** (ideally a stronger model from a different family, also via API) sees everything. It grades the trust decisions and proposes playbook edits.

**Self-improvement happens only at the playbook/prompt level.** No weights are trained: no LoRA, STaR or RL.

The **human dataset** (Peskov et al., ACL 2020, "It Takes Two to Lie") is used only as an uncontaminated external benchmark.

## Architecture
```
Genome store (system prompt + playbook + exemplars + effort + threshold, content-hashed)
   │
League ─► Game runner × G (asyncio)  ──► rsi-gateway :9000 ──► player model API (Inkling 2 Mini)
            diplomacy engine (MILA adjudicator)
            7 players, each sees ONLY its own view (board, own dialogs, own memory)
            Private vault: intent labels, plans at send time, analysis, memory, traces
            Leak guard (Rust Aho-Corasick) scans every player prompt before it is sent
   │
Game record (privileged) + Peskov-compatible dialogs
   │
Reviewer ──► rsi-gateway :9001 ──► reviewer model API
   │  grades (promise kept?, reasoning quality, leak flags) + ≤3 playbook diffs per game
   ▼
Evolve: lint diffs → vote/dedupe → candidates (top diffs, random subsets, mined exemplars, higher effort)
   │
Gate: (1) self-play replay gate, paired cluster bootstrap on AUPRC (p_gain ≥ 0.8) and ECE (≤ +0.02)
      (2) human-val transfer: AUPRC must not drop > 0.01
   │
Accepted → new incumbent, old one frozen into the league; Pareto front over (AUPRC, −ECE) picks the next parent
```

## Why the gateway stays (API mode)
- **Response cache.** Requests are keyed by blake3, so reruns and replays with the same seeds cost nothing.
- **Spend cap.** A USD budget, priced per million tokens, with per-run accounting.
- **Rate limits.** 429s are retried while honouring `Retry-After`, and `--max-inflight` matches the provider's rate limit.
- **Priority.** Game turns (sequential, latency-bound) go ahead of bulk eval sweeps.
- **Benchmarking.** `rsi-gateway bench` measures what the provider sustains at each concurrency level, which tells you what to set `--max-inflight` to.

## Data and evaluation protocol
- **Self-play gate set:** frozen gate games (v0 in every seat, seeds 10000+, never used for training). Receiver-seat replays are judged by the candidate and the incumbent on identical items, so the comparison is paired. The bootstrap resamples whole dialogs.
- **Human data** (`DenisPeskoff/2020_acl_diplomacy`, split by game):
  - **train:** baseline and reviewer validation only
  - **validation:** transfer gate, plus fitting the calibration temperature
  - **test:** run once (`TEST_LOCK`), on both the original and a paraphrased version
- **Label quirks handled in code:**
  - A lie is `sender_labels == false`.
  - `"NOANNOTATION"` is dropped for the suspected-lie task.
  - Scores and years are cast from strings.
- **Labels for self-play:**
  - `intent` (default): the sender's private label
  - `promise`: the reviewer judged the promise broken
  - `either`: the union of the two

## Contamination controls
| Risk | Control |
|---|---|
| Hidden info between players | Per-player views. Structural check (only own dialogs). Rust leak guard on every prompt. Reviewer `leak_flag`. A game with a leak is invalid and excluded |
| Across games | Fresh players per game; the genome is the only carry-over. Reviewer output never enters a player prompt |
| Human data into training | 8-gram index over human val+test. Every diff and exemplar is linted against it. Test set run once |
| Pretraining memorisation (dataset public since 2020) | Verbatim-continuation probe; paraphrased-test score reported alongside |
| Gate overfitting | Gate seeds and opponents disjoint from training games; append-only gate log |
| Reviewer bias | Different model family recommended; agreement with human labels reported |

## Code map
```
crates/rsi-core      Rust (PyO3): weighted metrics = sklearn, paired cluster bootstrap, n-gram index, leak guard
crates/rsi-gateway   Rust: API gateway (cache, budget, Retry-After, priority, metrics) + bench
rsi/env/             game.py (engine wrapper), bus.py (bilateral messages), vault.py (private state + isolation)
rsi/agents/          player.py (send/judge/orders + mock policies), reviewer.py (privileged review)
rsi/genome.py        genome, diffs, lint          rsi/evolve.py    candidates, exemplar mining, Pareto front
rsi/runner.py        one self-play game           rsi/judge.py     receiver-seat items, eval cache
rsi/human_data.py    benchmark loader             rsi/score.py     metrics, calibration
rsi/gate.py          acceptance gate              rsi/league.py    seating, ratings
rsi/contamination.py dedup, memorisation, paraphrase   rsi/loop.py  CLI      rsi/report.py  results.md
```

## Verification
- `pytest`:
  - metrics match sklearn, and Rust matches Python
  - genome diffs and lint behave as specified
  - a mock self-play game runs with zero leaks and produces Peskov-shaped logs
  - the human-data loader handles the label quirks and reweights balanced subsets correctly
- `python -m rsi.loop smoke --config configs/mock.yaml` runs one full game with no model.
- `python -m rsi.loop run --config configs/mock.yaml --mock` runs the whole loop end to end.
- **Real runs:** start `scripts/gateways.sh`, then run `smoke` against the API, then `run`, then `final-test` (once), then `report`.

## Limitations
- Few lies per split, so confidence intervals are wide.
- Labels are self-reported.
- Gap between the simulation and human play.
- Reviewer bias.
- Deception skills stay in the sandbox.
