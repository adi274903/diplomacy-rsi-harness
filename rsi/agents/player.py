"""Player agent: one stateless instance per power per game. Its only state is its own memory notes,
which live in the vault and are fed back through its own PlayerView."""

from __future__ import annotations

import random
import zlib

from rsi import prompts
from rsi.genome import Genome
from rsi.llm import CallMeta, register_mock
from rsi.schemas import GraphItemJudgment, GraphJudgeTurn, ItemJudgment, JudgeTurn, OrdersTurn, SendTurn


def call_seed(*parts) -> int:
    """Deterministic per-call seed: same game seed + same call site => same request => cache hit."""
    return zlib.crc32("|".join(map(str, parts)).encode()) & 0x7FFFFFFF


class Player:
    def __init__(self, power: str, genome: Genome, llm, game_seed: int, model: str | None = None):
        self.power = power
        self.model = model          # None = the default model of `llm`
        self.genome = genome
        self.llm = llm
        self.game_seed = game_seed

    async def send(self, board: str, private: str, recipients: list[str], max_messages: int,
                   round_no: int, rounds: int, phase: str, possible: dict[str, list[str]]) -> tuple[SendTurn, CallMeta]:
        task = prompts.send_task(recipients, max_messages, round_no, rounds)
        return await self.llm.complete_json(
            messages=prompts.player_messages(self.genome, board, private, task),
            schema=SendTurn, role="send", effort=self.genome.effort.get("send", "low"),
            seed=call_seed(self.game_seed, phase, self.power, "send", round_no),
            temperature=self.genome.temperature, priority="high", model=self.model,
            mock_ctx={"recipients": recipients, "max": max_messages, "possible": possible, "power": self.power},
        )

    async def judge(self, board: str, private: str, incoming: list[prompts.ShownMessage],
                    phase: str, round_no: int, shown_ids: list[int] | None = None) -> tuple[JudgeTurn, CallMeta]:
        structured = self.genome.structured_judgments
        task = prompts.judge_task(incoming, structured=structured)
        return await self.llm.complete_json(
            messages=prompts.player_messages(self.genome, board, private, task),
            schema=GraphJudgeTurn if structured else JudgeTurn, role="judge",
            effort=self.genome.effort.get("judge", "medium"),
            seed=call_seed(self.game_seed, phase, self.power, "judge", round_no),
            temperature=self.genome.temperature, priority="high", model=self.model,
            mock_ctx={"ids": [m.id for m in incoming], "shown": shown_ids or []},
        )

    async def orders(self, board: str, private: str, possible: dict[str, list[str]],
                     phase: str) -> tuple[OrdersTurn, CallMeta]:
        task = prompts.orders_task(possible)
        return await self.llm.complete_json(
            messages=prompts.player_messages(self.genome, board, private, task),
            schema=OrdersTurn, role="orders", effort=self.genome.effort.get("orders", "low"),
            seed=call_seed(self.game_seed, phase, self.power, "orders"),
            temperature=self.genome.temperature, priority="high", model=self.model,
            mock_ctx={"possible": possible},
        )


# ---- mock policies (MockLLM) ---------------------------------------------------------------------

_PROMISES = [
    "I will support your move into {loc} this turn.",
    "I won't move against {loc}; you have my word.",
    "Let's keep a demilitarised zone around {loc}.",
    "I plan to hold my position near {loc}.",
]


def _random_orders(rng: random.Random, possible: dict[str, list[str]]) -> list[str]:
    return [rng.choice(opts) for loc, opts in sorted(possible.items()) if opts and rng.random() < 0.9]


@register_mock(SendTurn)
def _mock_send(rng: random.Random, ctx: dict) -> dict:
    recipients = ctx.get("recipients", [])
    k = rng.randint(0, min(ctx.get("max", 2), len(recipients)))
    locs = sorted(ctx.get("possible", {})) or ["BUR"]
    msgs = []
    for to in rng.sample(recipients, k):
        deceptive = rng.random() < 0.15
        text = rng.choice(_PROMISES).format(loc=rng.choice(locs))
        msgs.append({"to": to, "text": f"{text} (note {rng.randint(0, 10**6)})",
                     "intent": "deceptive" if deceptive else "truthful"})
    return {"analysis": f"Mock analysis for {ctx.get('power')} number {rng.randint(0, 10**9)} of the position.",
            "plan": _random_orders(rng, ctx.get("possible", {})), "messages": msgs,
            "memory": f"Mock memory note {rng.randint(0, 10**9)} about alliances this year."}


@register_mock(JudgeTurn)
def _mock_judge(rng: random.Random, ctx: dict) -> dict:
    out = []
    for i in ctx.get("ids", []):
        p = round(rng.betavariate(1.2, 6.0), 3)
        out.append({"msg_id": i, "reasoning": f"Mock judgement reasoning {rng.randint(0, 10**9)} for this message.",
                    "cues": ["mock"], "p_lie": p, "decision": "distrust" if p >= 0.5 else "trust"})
    return {"judgments": out}


@register_mock(OrdersTurn)
def _mock_orders(rng: random.Random, ctx: dict) -> dict:
    return {"reasoning": f"Mock orders reasoning {rng.randint(0, 10**9)} for this phase.",
            "orders": _random_orders(rng, ctx.get("possible", {})),
            "memory": f"Mock orders memory {rng.randint(0, 10**9)} for later phases."}


def _mock_graph(rng: random.Random, p: float, shown: list[str]) -> dict:
    """A small, internally consistent graph; cites a shown source when there is one."""
    ev = [{"id": "e1", "source": rng.choice(shown) if shown else "message", "claim": "c1",
           "relation": rng.choice(["supports", "contradicts", "background"]), "note": "mock evidence"}]
    adj = [{"basis": "e1", "direction": "toward_lie" if p >= 0.05 else "toward_truth", "strength": "weak",
            "note": "mock step"}]
    return {"claims": [{"id": "c1", "text": "mock claim", "type": "statement_of_intent", "checkable": True}],
            "evidence": ev, "adjustments": adj}


@register_mock(GraphJudgeTurn)
def _mock_graph_judge(rng: random.Random, ctx: dict) -> dict:
    out = _mock_judge(rng, ctx)
    shown = [f"#{i}" for i in ctx.get("shown", [])]
    for j in out["judgments"]:
        j.update(_mock_graph(rng, j["p_lie"], shown))
    return out


@register_mock(GraphItemJudgment)
def _mock_graph_item(rng: random.Random, ctx: dict) -> dict:
    out = _mock_item(rng, ctx)
    out.update(_mock_graph(rng, out["p_lie"], [f"h{k}" for k in range(1, ctx.get("history", 0) + 1)]))
    return out


@register_mock(ItemJudgment)
def _mock_item(rng: random.Random, ctx: dict) -> dict:
    p = round(rng.betavariate(1.2, 6.0), 3)
    return {"reasoning": "mock", "cues": ["mock"], "p_lie": p, "decision": "distrust" if p >= 0.5 else "trust"}
