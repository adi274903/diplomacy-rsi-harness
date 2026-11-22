"""Privileged reviewer.

Sees the whole game: every message, the sender's private intent and committed plan, the orders that
were actually submitted, and every receiver judgment with its reasoning. Grades promise keeping and
reasoning quality, flags reasoning that cites unseeable information, and proposes up to three
general playbook diffs. Its output never enters any player context; diffs reach players only via a
linted, gated genome.
"""

from __future__ import annotations

import random

from rsi.agents.player import call_seed
from rsi.genome import Genome
from rsi.llm import LLMError, register_mock
from rsi.schemas import GameReview, PlaybookDiff

REVIEWER_SYSTEM = """You are the reviewer for a research sandbox in which AI agents play Diplomacy. Unlike the players, you can see everything: each message, the sender's PRIVATE intent label and the orders it planned when sending, the orders it actually submitted, and the receiver's trust judgment with its reasoning.

For every judged message, output a grade:
- promise: did the sender's actual orders keep the commitments stated in the message? (kept / broken / no_promise / unclear)
- reasoning_quality 1-5: does the receiver's reasoning use the evidence it could see, avoid invented facts, and express calibrated confidence?
- leak_flag: true only if the receiver's reasoning cites information it could not have seen (another pair's conversation, a private plan or label).

Then propose at most 3 edits to the players' trust-assessment playbook that would have improved the receivers' judgments. Edits must be GENERAL lessons that transfer to other games: no game, phase, message ids, quotes or specific unit positions in the heuristic text (put message ids only in evidence_msg_ids). Prefer fixing confident mistakes, especially deceptive messages that were trusted. Answer with one JSON object matching the schema."""


def _fmt_message(m: dict, send: dict | None, judgment: dict | None, actual: list[str]) -> str:
    lines = [f"[#{m['id']} {m['phase']} round {m['round']}] {m['sender']} -> {m['recipient']}: {m['text']}"]
    if send:
        lines.append(f"  sender intent: {send['intent']}; sender plan when sending: {'; '.join(send['plan']) or '-'}")
    lines.append(f"  sender actual orders this phase: {'; '.join(actual) or '-'}")
    if judgment:
        lines.append(f"  receiver judgment: p_lie={judgment['p_lie']:.2f} {judgment['decision']}; "
                     f"reasoning: {judgment['reasoning']}")
    else:
        lines.append("  receiver judgment: (none)")
    return "\n".join(lines)


class Reviewer:
    def __init__(self, llm, effort: str = "high", chunk: int = 40):
        self.llm = llm
        self.effort = effort
        self.chunk = chunk

    async def review_game(self, record: dict, genome: Genome) -> GameReview:
        """`record` is the privileged game record written by `rsi.runner`."""
        msgs = record["messages"]
        sends = {int(k): v for k, v in record["sends"].items()}
        judgments = {int(k): v for k, v in record["judgments"].items()}
        orders = record["orders"]
        grades, diffs, summaries = [], [], []
        for start in range(0, max(len(msgs), 1), self.chunk):
            part = msgs[start:start + self.chunk]
            if not part:
                break
            body = "\n\n".join(
                _fmt_message(m, sends.get(m["id"]), judgments.get(m["id"]),
                             orders.get(m["phase"], {}).get(m["sender"], []))
                for m in part)
            user = (f"## Current playbook\n{genome.render_playbook()}\n\n"
                    f"## Game transcript (privileged), messages {start}-{start + len(part) - 1}\n{body}")
            try:
                review, _ = await self.llm.complete_json(
                    messages=[{"role": "system", "content": REVIEWER_SYSTEM}, {"role": "user", "content": user}],
                    schema=GameReview, role="review", effort=self.effort, priority="normal", temperature=0.6,
                    seed=call_seed("review", record["game_id"], record["seed"], start),
                    mock_ctx={"ids": [m["id"] for m in part if m["id"] in judgments],
                              "playbook_ids": [h.id for h in genome.playbook]},
                )
            except LLMError as e:
                summaries.append(f"chunk {start}: review failed ({e})")
                continue
            valid_ids = {m["id"] for m in part}
            grades += [g for g in review.grades if g.msg_id in valid_ids]
            diffs += review.diffs
            summaries.append(review.summary)
        return GameReview(grades=grades, diffs=diffs[:3], summary=" | ".join(summaries))


@register_mock(GameReview)
def _mock_review(rng: random.Random, ctx: dict) -> dict:
    grades = [{"msg_id": i, "promise": rng.choice(["kept", "broken", "no_promise", "unclear"]),
               "reasoning_quality": rng.randint(1, 5), "leak_flag": False, "note": "mock"} for i in ctx.get("ids", [])]
    diffs = []
    if rng.random() < 0.7:
        diffs.append(PlaybookDiff(op="add", text=rng.choice([
            "When a sender proposes a move that benefits only themselves, raise p_lie moderately.",
            "Treat sudden friendliness after a recent conflict as a warning sign, not evidence of honesty.",
            "If a sender's promises have been consistent with their revealed orders, lower p_lie.",
        ]), rationale="mock", evidence_msg_ids=ctx.get("ids", [])[:2]).model_dump())
    if rng.random() < 0.2 and ctx.get("playbook_ids"):
        diffs.append({"op": "delete", "heuristic_id": rng.choice(ctx["playbook_ids"]), "text": None,
                      "rationale": "mock", "evidence_msg_ids": []})
    return {"grades": grades, "diffs": diffs, "summary": "mock review"}
