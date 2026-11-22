# Plan: Self-Play RSI Harness for Reasoning-Traced Trust Decisions in Diplomacy (6-hour sprint)

## Context
The goal is a harness where **several instances of the same agent** (Thinking Machines **Inkling 2 Mini**, cloud-served) **play live Diplomacy games** against each other. Each message they send carries reasoning traces and private truth/lie annotations.

A separate **reviewer** agent with privileged access to everything judges the agents' trust decisions and reasoning. It then drives recursive self-improvement in two stages:
1. **Stage 1:** playbook/prompt evolution.
2. **Stage 2:** distilling traces into LoRA weights.

The **human dataset** (Peskov et al., ACL 2020, "It Takes Two to Lie") is used only as an uncontaminated external benchmark for transfer. It never feeds training. The plan also enforces **no contamination**:
- between players (hidden information)
- between games
- between self-play data and the human eval set
- from Inkling's pretraining (the dataset has been public since 2020)

This plan replaces the BERT-only plan in `.pi/plan/6-hour-...md`.

## Architecture
```
                ┌──────────── Genome store (versioned prompt+playbook+exemplars, hashed) ───────────┐
                ▼                                                                                   │
League scheduler ─► Game runner × G (parallel)                                                      │
                     │  diplomacy engine (adjudication, board state)                                 │
                     │  7 × Player agent (Inkling 2 Mini, same genome or league mix)                │
                     │     each sees ONLY its own observation: board, its own inbox/outbox, its own memory│
                     │  Message bus (private bilateral channels, N press rounds per phase)            │
                     │  Private vault: sender intent label + committed plan (never routed to players) │
                     ▼                                                                               │
              Game log (Peskov-compatible jsonl + extra fields) ─► Reviewer agent (privileged view)  │
                                                                     ├ grades trust decisions vs ground truth
                                                                     ├ grades reasoning, flags leakage/cheating
                                                                     └ proposes playbook diffs ──► Gate ┘
                                                                                  (self-play gate games
                                                                                   + human-data val transfer)
```

## 1. Game environment
- **Engine:** the open-source `diplomacy` Python package (MILA adjudicator, standard map). Check in H0 that it installs on Python 3.12. If not, use a Python 3.10 `uv` venv.
- **Wrapper:** `env/game.py` implements this phase loop:
  1. Observe.
  2. Run R=2 press rounds. In each round every power may send ≤ 2 bilateral messages.
  3. Orders.
  4. Adjudicate.
- **Game length:** truncate at the end of 1906 (about 12 phases), score by supply centres, and record outcomes per power. A full game is too long for a 6-hour budget.
- **Per message sent, the sender's turn writes:**
  - The message text.
  - A **private intent label** (truthful/deceptive), matching the dataset's sender label.
  - A **private committed plan**: the orders it currently intends, locked into the vault before the message goes out.
  - Its reasoning trace.
- **Per message received, the receiver's turn writes (the decision being improved):**
  - A trust decision, `p_lie`, and a reasoning trace. These mirror the dataset's receiver label.
- **Objective ground truth on top of the self-reported label:** the reviewer checks each promise against the orders actually submitted (whether the promise was kept or broken). That gives two label sources: intent and outcome. This removes some of the self-report noise the human dataset suffers from.
- **Log format:** game logs use the **same schema as the Peskov jsonl** (messages, sender_labels, receiver_labels, speakers, receivers, indices, seasons, years, game_score, game_score_delta, players, game_id). Extra fields: `private_plan`, `orders`, `promise_kept`, `traces`, `review`. One scorer then works on both self-play and human data.

## 2. Agent instances and isolation
- **One stateless agent per power per game.** The context is rebuilt each call from that power's observation only:
  - board
  - its own past messages
  - its own scratch memory for this game
- Fresh instances for every game. Nothing carries over between games **except the versioned genome** (the only intended learning channel).
- **The orchestrator enforces information hiding.** The vault, other powers' traces and labels, and reviewer output are never put into a player prompt.
- A unit test renders every player prompt in a game and asserts that no vault and no foreign-trace strings appear.
- **League:** each game seats a mix of the current genome, the best previous genomes (frozen snapshots) and v0. This avoids self-play collapse, where agents only learn to read their own clones.
- **Seating:** each power's genome is assigned at random per game with a fixed seed.

## 3. Reviewer
- **Separate role** with its own prompt, run at high reasoning effort. Ideally use a different model family from the players to reduce self-preference bias. If Inkling is the only option, use an Inkling instance with a different prompt and seed, and state the bias as a limitation.
- **Inputs:** the full game log including the vault, orders and promise outcomes.
- **Outputs (per game):**
  - (a) a correctness label for each receiver decision
  - (b) a reasoning-quality rubric score (uses the available evidence, no invented facts, calibrated language)
  - (c) **cheating/leakage flags**, e.g. a trace that cites information the player could not have seen
  - (d) up to 3 game-agnostic playbook diffs, each citing the decisions that motivated it
- **Reviewer validation (H1):** run the reviewer on **human val** dialogs with labels hidden. Measure its agreement with the human sender labels before trusting it as a judge, and report that agreement.

## 4. RSI loop
**Stage 1 (playbook evolution, about 4 generations):**
1. Play G≈12 truncated games in parallel with the league.
2. The reviewer reviews every game.
3. Aggregate the diffs.
4. A **lint step** rejects diffs that contain game IDs, quoted message text or long n-gram overlap with any human data.
5. Build 3 candidate genomes.

**Gate.** A candidate is accepted only if both conditions hold:
1. **Self-play gate:** 6 fixed-seed games against *frozen* v0 and best-so-far opponents (never used in training). Receiver AUPRC and ECE are measured against vault labels, and a paired bootstrap must show a gain in ≥ 80% of resamples.
2. **Transfer check:** human **val** game ACTUAL_LIE AUPRC must not drop by more than 0.01. This guards against learning sim-only tells.

Secondary metric: supply-centre outcome against frozen opponents (TrueSkill). It is tracked but is not a gate, so the optimization stays aimed at detection rather than winning.

**Stage 2 (weights, time-boxed):**
1. Mine the receiver traces that are correct and confident from self-play games only. Balance lies against truths at about 1:2.
2. LoRA SFT on Inkling, **if** the provider or Tinker supports this 2-day-old MoE/looped architecture. Check in H0.
3. Fallback: an exemplar bank of self-play traces retrieved with the cached `bge-base-en-v1.5`.
4. Send the result through the same gate.

## 5. Contamination controls
| Risk | Control |
|---|---|
| Hidden info leaks between players | Per-player context assembly. Vault isolation. Prompt-render test. Reviewer flags traces that cite unseeable facts |
| Leakage across games | Fresh instances each game. Genome is the only carry-over. Reviewer output never enters a player context |
| Gate overfitting | Gate seeds and frozen opponents are disjoint from training games. Gate log is append-only |
| Self-play data → human eval | Human data is never in playbooks, exemplars or SFT. MinHash/8-gram dedup of all training text against human val/test. Test run exactly once |
| Inkling pretraining memorization of the public dataset | **Memorization probe** on human test: (1) prefix→verbatim continuation rate; (2) label prediction from the message alone, with no context. **Paraphrase check:** score the original against an LLM-paraphrased test set. A large gap means contaminated, so report results on the paraphrased set |
| Reviewer bias | Validate on human val labels. Different model or prompt from the players. Spot-check 20 reviews by hand |

## 6. Human dataset handling (benchmark only)
- **Source:** `github.com/DenisPeskoff/2020_acl_diplomacy` `data/{train,validation,test}.jsonl` (CC-BY-4.0), split by game, `game_id` 1–12.
- **Gotchas:**
  - **A lie is `sender_labels == false`.**
  - `receiver_labels` contains `"NOANNOTATION"`.
  - Scores and years are strings.
  - The README says `score_delta`, but the data uses `game_score_delta`.
  - Do not use `singlemessage_format.py`, which shuffles away context.
- **Roles of each split:**
  - Human **train**: v0 baseline only, plus reviewer validation.
  - Human **val**: transfer gate.
  - Human **test**: final evaluation, run once, on both the original and paraphrased versions.
- Compute the real stats in H0 and take the paper's baselines from the PDF via `pdftotext`.

## 7. Infrastructure
- **Env:** `uv` venv with `diplomacy`, `openai`, `pydantic`, `tenacity`, `duckdb`, `pandas`, `scikit-learn`, `scipy`, `datasketch` (MinHash), `trueskill`, `pyyaml` and `matplotlib`. No local torch.
- **Serving:** an OpenAI-compatible cloud endpoint for Inkling 2 Mini (vLLM on 1×H100/A100-80GB, or the provider's API), configured via `.env` (`BASE_URL`, `MODEL`, `API_KEY`, `REVIEWER_MODEL`).
- **Client:** async with a semaphore (about 64 concurrent requests), retries, strict JSON-schema output, and a DuckDB cache keyed by (prompt hash, model, effort, seed).
- **Per-run budget:** a hard cap on token spend and a dashboard line per generation.
- **Estimated load:** about 7 powers × (2 press rounds × 2 messages + receiver judgments + orders) ≈ 50 calls per phase. That is about 600 calls per game and about 7k calls per generation, which is feasible with 3.4B active parameters on one GPU.
- **Storage:**
  - `runs/<run_id>/gen_<k>/games/*.jsonl`
  - `reviews.jsonl`
  - `genomes/`
  - `gate_log.jsonl`
  - `metrics.jsonl`
  - `traces.parquet`

## Files to create (`/home/adi274903/Documents/Strategic_Decision_Making`)
```
rsi/env/game.py         diplomacy wrapper, phase loop, truncation, scoring
rsi/env/bus.py          private bilateral message bus, press rounds
rsi/env/vault.py        private intents/plans/orders, promise-kept checker
rsi/agents/player.py    observation→prompt, send/receive/orders turns (JSON schema)
rsi/agents/reviewer.py  privileged review, rubric, leakage flags, diffs
rsi/genome.py           genome model, hashing, diff apply, lint
rsi/llm.py              async client, cache, budget
rsi/league.py           seating, frozen snapshots, TrueSkill
rsi/gate.py             self-play gate + human-val transfer gate, bootstrap
rsi/contamination.py    MinHash dedup, memorization probe, paraphrase set
rsi/human_data.py       download, flatten, leakage asserts, human-data rollout
rsi/score.py            AUPRC, Lie-F1, macro-F1, ECE, Brier, reweighting
rsi/star.py             Stage 2 trace mining → LoRA SFT or exemplar bank
rsi/loop.py             generations orchestration (python -m rsi.loop --config configs/sprint.yaml)
rsi/report.py           results.md, reliability plots, gate history, playbook diff history
tests/test_isolation.py, tests/test_leakage.py, tests/test_score.py, tests/test_game_smoke.py
```

## 6-hour schedule
| Time | Work |
|---|---|
| 0–1 | Env, endpoint, check `diplomacy` install and LoRA support. Human data and its stats. Memorization/paraphrase probe started in the background |
| 1–2 | Game wrapper, bus, vault, player agent. Smoke game with 7 × v0 to 1902. Isolation tests. Reviewer, validated on human val |
| 2–2.5 | v0 baselines: human val/test-paraphrase (test held back), self-play gate games |
| 2.5–4.5 | Stage 1: about 4 generations (12 games each, then review, diffs, gate) |
| 4.5–5.25 | Stage 2: trace mining, then LoRA or the exemplar bank, then gate |
| 5.25–6 | Final genome, calibration on human val, **human test once** (original + paraphrased), report |

## Verification
- `pytest`:
  - Rendered player prompts contain no vault text and no foreign traces.
  - No human game appears in two roles.
  - Context indices are lower than the target index.
  - The metric functions match sklearn.
  - A smoke game reaches 1902 with legal orders.
- **Determinism:** rerunning a game from cache with the same seed reproduces an identical log.
- **Gate sanity:**
  - A harmful diff ("always trust") is rejected.
  - A no-op genome's bootstrap delta is centred near 0.
- **Reviewer:** agreement with human val labels is reported. 20 reviews are checked by hand.
- **Contamination report:**
  - Memorization rates.
  - Original-vs-paraphrased gap.
  - Dedup hits, which must be 0.
- **`results.md`** contains:
  - v0 against the final genome on self-play gate games and human test (original and paraphrased), with 95% CIs, set against the paper baselines.
  - Calibration before and after.
  - Trust over-trust and under-trust tables.
  - League TrueSkill.
  - Playbook diff history.
  - Limitations: few lies; self-report labels; sim-to-human gap; reviewer bias; deception skills stay confined to the sandbox and are not exported.
