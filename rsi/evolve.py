"""Playbook-level learning (no weight updates).

Each generation turns reviewer output into candidate genomes:
- diff candidates: linted reviewer diffs, ranked by how many games proposed them (votes) and how
  much evidence they cite; one greedy "top votes" candidate plus random subsets for diversity
- exemplar candidate: worked examples mined from THIS generation's self-play judgments that were
  correct and confident (self-play text only, contamination-checked); replaces the exemplar set
- effort candidate: same genome with a higher judging effort (trades cost for accuracy)
Parents are drawn from the Pareto front of accepted genomes over (gate AUPRC, -gate ECE), so a
genome that is better calibrated but slightly less discriminative is not lost (GEPA-style).
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field

from rsi.genome import Genome, lint_diff
from rsi.schemas import GameReview, PlaybookDiff

EFFORT_LADDER = ["minimal", "low", "medium", "high"]


@dataclass
class ScoredDiff:
    diff: PlaybookDiff
    votes: int = 1
    games: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return diff_key(self.diff)


def diff_key(d: PlaybookDiff) -> str:
    text = re.sub(r"\W+", " ", (d.text or "").lower()).strip()
    return f"{d.op}|{d.heuristic_id or ''}|{text}"


def aggregate_diffs(reviews: dict[str, GameReview], genome: Genome, contamination=None) -> tuple[list[ScoredDiff], list[dict]]:
    """Lint and deduplicate diffs across games. Returns (accepted diffs ranked, rejection log)."""
    pool: dict[str, ScoredDiff] = {}
    rejected = []
    for gid, rev in reviews.items():
        for d in rev.diffs:
            errs = lint_diff(d, genome, contamination)
            if errs:
                rejected.append({"game": gid, "diff": d.model_dump(), "errors": errs})
                continue
            k = diff_key(d)
            if k in pool:
                pool[k].votes += 1
                pool[k].games.append(gid)
                pool[k].diff.evidence_msg_ids += d.evidence_msg_ids
            else:
                pool[k] = ScoredDiff(d.model_copy(deep=True), 1, [gid])
    ranked = sorted(pool.values(), key=lambda s: (-s.votes, -len(s.diff.evidence_msg_ids), s.key))
    return ranked, rejected


def _apply(parent: Genome, diffs: list[ScoredDiff], name: str) -> Genome:
    child, _ = parent.apply([s.diff for s in diffs], name=name)
    return child


def propose_candidates(parent: Genome, ranked: list[ScoredDiff], k: int, max_diffs: int,
                       rng: random.Random, gen: int) -> list[Genome]:
    if not ranked:
        return []
    out = [_apply(parent, ranked[:max_diffs], f"g{gen}c0-top")]
    seen = {out[0].hash, parent.hash}
    tries = 0
    while len(out) < k and tries < 20 * k:
        tries += 1
        size = rng.randint(1, min(max_diffs, len(ranked)))
        subset = rng.sample(ranked, size)
        g = _apply(parent, subset, f"g{gen}c{len(out)}")
        if g.hash not in seen:
            seen.add(g.hash)
            out.append(g)
    return out


def effort_candidate(parent: Genome, gen: int) -> Genome | None:
    cur = parent.effort.get("judge", "medium")
    if cur not in EFFORT_LADDER or cur == EFFORT_LADDER[-1]:
        return None
    nxt = EFFORT_LADDER[EFFORT_LADDER.index(cur) + 1]
    child = parent.model_copy(deep=True)
    child.effort = dict(parent.effort, judge=nxt, item_judge=nxt)
    child.name, child.parent, child.generation = f"g{gen}-effort-{nxt}", parent.hash, parent.generation + 1
    return child


# ---- exemplar mining (self-play only) ------------------------------------------------------------

def mine_exemplars(games: list[dict], k: int, rng: random.Random, contamination=None,
                   lie_share: float = 0.5, max_chars: int = 700) -> list[str]:
    """Pick receiver judgments that were right and confident, formatted as worked examples.
    Only self-play text is used; anything overlapping held-out human data is dropped."""
    lies, truths = [], []
    for g in games:
        if g.get("error"):
            continue
        sends = {int(m): v for m, v in g["sends"].items()}
        msgs = {m["id"]: m for m in g["messages"]}
        for mid, j in g["judgments"].items():
            mid = int(mid)
            if not j.get("ok") or mid not in sends or not j.get("reasoning"):
                continue
            lie = sends[mid]["intent"] == "deceptive"
            if lie and j["p_lie"] >= 0.7:
                lies.append((g, msgs[mid], j))
            elif not lie and j["p_lie"] <= 0.15:
                truths.append((g, msgs[mid], j))
    n_lie = min(len(lies), round(k * lie_share))
    picks = rng.sample(lies, n_lie) + rng.sample(truths, min(len(truths), k - n_lie))
    out = []
    for g, m, j in picks:
        ex = (f"Message from {m['sender']} to {m['recipient']} ({m['season']} {m['year']}; supply centres "
              f"{m['sender']} {m['sender_sc']}, {m['recipient']} {m['recipient_sc']}): {m['text']}\n"
              f"Good judgment: " + json.dumps({"reasoning": j["reasoning"], "cues": j["cues"],
                                                "p_lie": j["p_lie"], "decision": j["decision"]}))
        ex = ex[:max_chars]
        if contamination is not None and contamination.query(ex)[0] > 0:
            continue
        out.append(ex)
    return out


def exemplar_candidate(parent: Genome, exemplars: list[str], gen: int) -> Genome | None:
    if not exemplars:
        return None
    child = parent.model_copy(deep=True)
    child.exemplars = exemplars
    child.name, child.parent, child.generation = f"g{gen}-exemplars", parent.hash, parent.generation + 1
    return child


# ---- Pareto front -------------------------------------------------------------------------------

@dataclass
class FrontEntry:
    genome: Genome
    auprc: float
    ece: float


def pareto_front(entries: list[FrontEntry]) -> list[FrontEntry]:
    front = []
    for e in entries:
        dominated = any((o.auprc >= e.auprc and o.ece <= e.ece) and (o.auprc > e.auprc or o.ece < e.ece)
                        for o in entries if o is not e)
        if not dominated:
            front.append(e)
    return front
