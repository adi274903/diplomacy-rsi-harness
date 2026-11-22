# Build Plan: Trustworthy Subjective Decisions on the Diplomacy Deception Dataset (6-hour sprint)

## Goal
Build a lie-detection / trust-decision system on the Diplomacy deception dataset that (a) predicts whether a message is a lie, (b) reports calibrated confidence, and (c) self-improves via an RLAIF-style loop — all runnable locally in ~6 hours on the available hardware.

## Dataset
- **Source:** HuggingFace `community-datasets/diplomacy_detection` (cached-loadable via `datasets` 5.1 in the transformers venv).
- **Shape:** 252 game-conversations → flatten to ~17,289 message rows. Fields: `messages`, `sender_labels` (lie/truth), `receiver_labels` (suspected lie/truth/no-annotation), `speakers`, `receivers`, `seasons`, `years`, `game_score`, `game_score_delta`, `game_id`.
- **Splits:** provided train/val/test (by game). Verify no `game_id` leakage across splits before training.
- **Label of truth:** `sender_labels` (intended truthfulness). Treat as noisy self-report (ceiling ~human ~70% per paper).

## Data amount & prep (Hour 0–1)
- Flatten each conversation into one row per message, carrying sender/receiver labels, speaker/receiver identities, season/year, score, score_delta, message index within conversation, and conversation id.
- Class balance check: lies are a minority (~10–20%). Use macro-F1 / AUPRC, not accuracy.
- Train: ~14k messages, Val: ~1.5k, Test: ~3k (exact from split).
- Context features to ablate: message text only; +speaker/receiver/power-delta; +conversation history (last 3 messages).

## Model & training (Hour 1–3)
- **Base:** `bert-base-uncased` (cached). Tokenize max length 128 (messages are short). Fine-tune with `transformers.Trainer`, 3 epochs, lr 2e-5, batch 16, weight decay 0.01, class-weighted CE (or focal) for imbalance.
- **Venv:** `/home/adi274903/Documents/transformers/.venv` (has datasets, pandas, pyarrow, transformers, torch CPU, accelerate). CPU training: ~45 min/run for bert-base on 14k × 3 epochs. Keep to 2–3 runs total.
- **Fallback if too slow:** DistilBERT (download ~260MB, faster) or reduce to 2 epochs / max_len 96.
- **Checkpoint:** best by val macro-F1; save to `checkpoints/baseline/`.

## Calibration (Hour 3)
- Temperature scaling on val set (fit single T on NLL). Report ECE (10 equal-width bins), Brier score, NLL before/after.
- Reliability diagram saved to `reports/calibration.png`.

## RLAIF-style self-improvement (Hour 3–5)
No generative LLM is cached locally and no API keys exist, so use a **surrogate judge** (flag as limitation; optional stretch: download Qwen2.5-0.5B-Instruct for a true LLM judge).

1. **Principle-based heuristic judge** (rule features): message contradicts prior commitments; evasive hedges ("maybe", "I think"); sender losing (negative score_delta); very short message; promise without concrete action. Produces a 0–1 suspicion score.
2. **Augmented input (iteration 1):** prepend a structured critique string (heuristic rationale) to the message; retrain; measure ΔF1/ΔECE.
3. **Self-training / pseudo-labeling (iteration 2):** predict on val messages where receiver label is missing; keep high-confidence predictions (p > 0.9 or p < 0.1) as pseudo-labels with confidence weighting; retrain; measure Δ.
4. **Constitutional filtering:** hard rules (e.g., never mark a message truthful if it contradicts a recorded prior promise from the same sender) post-process predictions; report effect.
5. **Weak-to-strong (optional):** train a small model on heuristic-judge labels, use it to label, train a larger model — report if time permits.

## Trust-calibration analysis (Hour 4–5)
- Compare `receiver_labels` vs `sender_labels`: Cohen's κ, over-trust rate (receiver truth / sender lie), under-trust rate (receiver suspects lie / sender truth).
- Stratify by `game_score_delta` (power asymmetry) and message index (relationship stage).
- Outcome re-anchoring (exploratory): correlate per-conversation trust-decision aggregate with final `game_score_delta`.

## Ablations & reporting (Hour 5–6)
- Ablations: text-only; +identity/power; +history; +critique-augmented.
- Error analysis: top-20 false positives and false negatives with heuristic rationale.
- Final report: `reports/results.md` (metrics table, calibration curve, confusion matrix, ablation table, limitations).

## Metrics summary
| Category | Metrics |
|---|---|
| Lie detection | Accuracy, Precision/Recall/F1 (lie class + macro), AUROC, AUPRC |
| Calibration | ECE (10 bins), Brier score, NLL; before/after temperature scaling |
| Trust calibration | Cohen's κ (receiver vs sender), over-trust rate, under-trust rate, stratified |
| Outcome (exploratory) | Spearman corr. of trust aggregate vs game_score_delta |
| RSI gain | ΔF1, ΔECE across iterations |

## Validation / success criteria
- Baseline val macro-F1 ≥ 0.60 (human ~0.70 is ceiling).
- Temperature scaling reduces ECE by ≥ 20% relative.
- At least one RLAIF iteration shows non-negative ΔF1 or ΔECE.
- No game_id leakage between splits (assertion).
- Reproducible: single command `python train.py --config configs/baseline.yaml` reproduces baseline numbers.

## Risks & honest limits
- Sender intent labels are self-reported and noisy; ~70% human ceiling.
- Severe class imbalance → rely on F1/AUPRC, not accuracy.
- No real LLM judge available locally → RLAIF is heuristic/surrogate; true RLAIF requires downloading a small instruct model (stretch).
- CPU training → budget runs carefully; prefer fewer, well-instrumented experiments.
- Game-level confounds (player skill, nationality) not fully controlled.

## Files to create
```
src/data.py            # load + flatten + split assertion
src/model.py           # tokenizer + bert classifier wrapper
src/train.py           # Trainer loop, class weights
src/calibrate.py       # temperature scaling, ECE, Brier
src/rlaif.py           # heuristic judge, critique augmentation, pseudo-labeling, constitutional filter
src/eval.py            # metrics + trust-calibration + ablations
src/report.py          # plots + markdown report
configs/baseline.yaml
reports/results.md
```
