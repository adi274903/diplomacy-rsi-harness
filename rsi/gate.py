"""Acceptance gate. A candidate genome replaces the incumbent only if BOTH hold:

1. Self-play gate: on receiver-seat replays of frozen gate games (seeds and opponents disjoint from
   training games), AUPRC improves in >= p_gain_min of paired cluster-bootstrap resamples and ECE
   does not worsen by more than ece_tolerance.
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

    @property
    def score(self) -> float:
        """Ranking among accepted candidates: sim AUPRC gain."""
        return self.sim.get("auprc", {}).get("delta", float("-inf"))


async def gate(candidate: Genome, incumbent: Genome, sim_items: list[JudgeItem], human_items: list[JudgeItem],
               llm, store: EvalStore, cfg: LoopConfig, log_path: str | Path) -> GateDecision:
    res_inc = await store.get(incumbent, "sim_gate", sim_items, llm)
    res_can = await store.get(candidate, "sim_gate", sim_items, llm)
    sim = compare(sim_items, res_inc, res_can, ("auprc", "ece", "brier"), cfg.bootstrap_n, cfg.seed)
    d = GateDecision(candidate=candidate.hash, incumbent=incumbent.hash, accepted=True, sim=sim,
                     sim_metrics={"incumbent": evaluate(sim_items, res_inc, incumbent.threshold),
                                  "candidate": evaluate(sim_items, res_can, candidate.threshold)})
    if not (sim["auprc"]["p_gain"] >= cfg.p_gain_min):
        d.accepted = False
        d.reasons.append(f"sim AUPRC p_gain {sim['auprc']['p_gain']:.2f} < {cfg.p_gain_min}")
    if sim["ece"]["delta"] > cfg.ece_tolerance:
        d.accepted = False
        d.reasons.append(f"sim ECE worsened by {sim['ece']['delta']:.3f}")

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
