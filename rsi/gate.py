"""Acceptance gate. A candidate genome replaces the incumbent only if BOTH hold:

1. Primary gate: AUPRC improves in >= p_gain_min of paired cluster-bootstrap resamples and ECE does not
   worsen by more than ece_tolerance, on the primary item set. That is normally receiver-seat replays of
   frozen self-play gate games ("sim_gate"; seeds and opponents disjoint from training games), or human
   TRAIN games ("human_train_gate") when `loop.gate_source` selects them, e.g. because self-play
   produced too few lies for AUPRC to be defined. Field names below keep the `sim` prefix for
   compatibility; `primary_set` says which set it was.
2. Transfer check: AUPRC on human validation items does not drop by more than human_val_tolerance
   (guards against learning sim-only tells).

Every decision is appended to gate_log.jsonl.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rsi.config import LoopConfig
from rsi.genome import Genome
from rsi.judge import EvalStore, JudgeItem
from rsi.score import compare, evaluate


@dataclass
class GateDecision:
    candidate: str
    incumbent: str
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    sim: dict = field(default_factory=dict)
    human: dict = field(default_factory=dict)
    sim_metrics: dict = field(default_factory=dict)
    human_metrics: dict = field(default_factory=dict)
    primary_set: str = "sim_gate"

    @property
    def score(self) -> float:
        """Ranking among accepted candidates: sim AUPRC gain."""
        return self.sim.get("auprc", {}).get("delta", float("-inf"))


async def gate(candidate: Genome, incumbent: Genome, sim_items: list[JudgeItem], human_items: list[JudgeItem],
               llm, store: EvalStore, cfg: LoopConfig, log_path: str | Path,
               primary_set: str = "sim_gate") -> GateDecision:
    res_inc = await store.get(incumbent, primary_set, sim_items, llm)
    res_can = await store.get(candidate, primary_set, sim_items, llm)
    sim = compare(sim_items, res_inc, res_can, ("auprc", "ece", "brier"), cfg.bootstrap_n, cfg.seed)
    d = GateDecision(candidate=candidate.hash, incumbent=incumbent.hash, accepted=True, sim=sim,
                     primary_set=primary_set,
                     sim_metrics={"incumbent": evaluate(sim_items, res_inc, incumbent.threshold),
                                  "candidate": evaluate(sim_items, res_can, candidate.threshold)})
    if not (sim["auprc"]["p_gain"] >= cfg.p_gain_min):
        d.accepted = False
        d.reasons.append(f"{primary_set} AUPRC p_gain {sim['auprc']['p_gain']:.2f} < {cfg.p_gain_min}")
    if sim["ece"]["delta"] > cfg.ece_tolerance:
        d.accepted = False
        d.reasons.append(f"{primary_set} ECE worsened by {sim['ece']['delta']:.3f}")

    if d.accepted and human_items:  # only spend the transfer check on candidates that pass the sim gate
        h_inc = await store.get(incumbent, "human_val", human_items, llm)
        h_can = await store.get(candidate, "human_val", human_items, llm)
        d.human = compare(human_items, h_inc, h_can, ("auprc",), cfg.bootstrap_n, cfg.seed)
        d.human_metrics = {"incumbent": evaluate(human_items, h_inc, incumbent.threshold),
                           "candidate": evaluate(human_items, h_can, candidate.threshold)}
        if d.human["auprc"]["delta"] < -cfg.human_val_tolerance:
            d.accepted = False
            d.reasons.append(f"human-val AUPRC dropped {d.human['auprc']['delta']:.3f}")

    with open(log_path, "a") as f:
        f.write(json.dumps(asdict(d)) + "\n")
    return d


def choose_gate_set(gate_source: str, n_sim_lies: int, min_gate_lies: int) -> str:
    """Name of the primary gate item set for this run."""
    if gate_source == "human_train" or (gate_source == "auto" and n_sim_lies < min_gate_lies):
        return "human_train_gate"
    return "sim_gate"
