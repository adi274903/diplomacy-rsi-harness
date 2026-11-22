"""The receiver's decision graph: claims, cited evidence, and adjustments from the base rate.

With `Genome.structured_judgments` on, each judgment states how it was reached instead of only a
free-text reasoning. That gives three things for free, on every judgment and without a reviewer call:

  * citation checks: evidence must cite something the receiver was actually shown. A citation of an
    unseen message or of orders that have not resolved yet is invented evidence.
  * replay: each adjustment is a step in log-odds from the base rate, so the probability its own steps
    imply can be compared with the stated p_lie, and any node can be dropped to see what it was worth.
  * explicit edges for the causal graph (rsi/causal.py): evidence -> judgment, by message id.

The reviewer also sees the graph, so it can rule on specific nodes ("cited a contradiction and did not
adjust") rather than on the judgment as a whole.
"""

from __future__ import annotations

import math

BASE_RATE = 0.05
STRENGTH_LOGIT = {"weak": 0.5, "moderate": 1.0, "strong": 2.0}
FIXED_SOURCES = {"message", "scores", "board"}

INSTRUCTIONS = f"""Show how you reached each judgment as a decision graph, built in this order:
1. claims: every claim or commitment in the message (ids c1, c2, ...), with a type and whether it could later be checked. Statements about the sender's own plans or uncertainty are statement_of_intent and count as claims. Use no_substantive_claim only for pure greetings or small talk.
2. evidence: each piece of evidence you used (ids e1, e2, ...), the claim it bears on, and whether it supports or contradicts that claim or is background. Cite its source exactly as shown: an earlier message as #<id> (or h<n> for a numbered history line), the public board and revealed orders as "board", the message itself as "message", supply-centre counts as "scores". Never cite anything you were not shown, and never cite orders that have not been revealed yet.
3. adjustments: starting from a base rate of {BASE_RATE:.0%}, each step that moved your estimate, with its basis (an evidence id, a claim id or "base_rate_context"), its direction and its strength (weak, moderate or strong). Strong steps should be rare.
Your p_lie should follow from your adjustments; keep reasoning to one or two sentences."""


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def implied_p(adjustments: list[dict], drop: set[str] | None = None) -> float:
    """Replay the adjustments from the base rate, optionally dropping some by basis."""
    z = logit(BASE_RATE)
    for a in adjustments:
        if drop and a.get("basis") in drop:
            continue
        step = STRENGTH_LOGIT.get(a.get("strength"), 0.0)
        z += step if a.get("direction") == "toward_lie" else -step
    return 1 / (1 + math.exp(-z))


def check(graph: dict, allowed_sources: set[str], board_shown: bool = True) -> dict:
    """Mechanical checks on one judgment's graph. `allowed_sources` are the message ids shown, as '#<id>'
    or 'h<n>'; 'message' and 'scores' are always allowed, 'board' only when a board was shown."""
    claims = graph.get("claims", [])
    evidence = graph.get("evidence", [])
    adjustments = graph.get("adjustments", [])
    claim_ids = {c["id"] for c in claims}
    evidence_ids = {e["id"] for e in evidence}
    allowed = allowed_sources | FIXED_SOURCES - (set() if board_shown else {"board"})
    invalid = [e["source"] for e in evidence if e["source"] not in allowed]
    dangling = [e["id"] for e in evidence if e["claim"] not in claim_ids]
    dangling += [a["basis"] for a in adjustments if a["basis"] not in claim_ids | evidence_ids | {"base_rate_context"}]
    replayed = implied_p(adjustments)
    contradicted_unadjusted = [
        e["id"] for e in evidence
        if e["relation"] == "contradicts"
        and not any(a["basis"] == e["id"] and a["direction"] == "toward_lie" for a in adjustments)
    ]
    return {
        "n_claims": len(claims),
        "n_checkable_claims": sum(bool(c.get("checkable")) for c in claims),
        "n_evidence": len(evidence),
        "invalid_sources": invalid,
        "dangling_refs": dangling,
        "contradictions_without_adjustment": contradicted_unadjusted,
        "implied_p": round(replayed, 4),
        "stated_vs_implied_logit_gap": round(logit(graph.get("p_lie", BASE_RATE)) - logit(replayed), 3),
    }


def graph_of(judgment) -> dict:
    """The graph part of a GraphJudgment / GraphItemJudgment, as plain dicts."""
    d = judgment.model_dump()
    return {k: d[k] for k in ("claims", "evidence", "adjustments", "p_lie")}


def cited_message_ids(graph: dict) -> list[int]:
    """Message ids cited as '#<id>' in the evidence, in order, without duplicates."""
    out = []
    for e in graph.get("evidence", []):
        s = e.get("source", "")
        if s.startswith("#") and s[1:].isdigit() and int(s[1:]) not in out:
            out.append(int(s[1:]))
    return out


def summary(graph: dict, checks: dict | None) -> str:
    """Compact text rendering for the reviewer and the viewer."""
    lines = ["claims: " + "; ".join(f"{c['id']} [{c['type']}{', checkable' if c['checkable'] else ''}] {c['text']}"
                                    for c in graph.get("claims", [])) or "claims: (none)"]
    for e in graph.get("evidence", []):
        lines.append(f"evidence {e['id']} {e['source']} {e['relation']} {e['claim']}: {e['note']}")
    for a in graph.get("adjustments", []):
        lines.append(f"step {a['basis']} {a['direction']} {a['strength']}: {a['note']}")
    if checks:
        flags = []
        if checks["invalid_sources"]:
            flags.append(f"cites sources it was not shown: {', '.join(checks['invalid_sources'])}")
        if checks["dangling_refs"]:
            flags.append(f"dangling references: {', '.join(checks['dangling_refs'])}")
        if checks["contradictions_without_adjustment"]:
            flags.append("contradicting evidence with no step toward lie: "
                         + ", ".join(checks["contradictions_without_adjustment"]))
        flags.append(f"steps imply p_lie={checks['implied_p']:.2f}")
        lines.append("checks: " + "; ".join(flags))
    return "\n".join(lines)
