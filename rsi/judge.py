"""Single-item judging from the receiver's seat, shared by the self-play gate (replays of frozen gate
games) and the human-data benchmark. Using one prompt + schema for both is what makes the
human-data number a transfer measurement of the same policy."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from rsi import decision_graph, prompts
from rsi.agents.player import call_seed
from rsi.genome import Genome
from rsi.llm import LLMError
from rsi.schemas import GraphItemJudgment, ItemJudgment


@dataclass
class JudgeItem:
    item_id: str
    source: str                      # "sim" | "human"
    group: str                       # dialog id: bootstrap resamples whole dialogs
    receiver: str
    sender: str
    season: str
    year: int
    target: str
    is_lie: bool
    history: list[tuple[str, str]]   # (speaker, text), oldest first, strictly before the target
    board: str | None = None
    sender_sc: int | None = None
    receiver_sc: int | None = None
    weight: float = 1.0


@dataclass
class ItemResult:
    item_id: str
    p_lie: float
    decision: str
    reasoning: str
    cues: list[str]
    ok: bool
    graph: dict | None = None
    graph_checks: dict | None = None


async def judge_items(genome: Genome, items: list[JudgeItem], llm, *, priority: str = "low",
                      concurrency: int = 128, salt: str = "") -> list[ItemResult]:
    sem = asyncio.Semaphore(concurrency)
    structured = genome.structured_judgments

    async def one(it: JudgeItem) -> ItemResult:
        async with sem:
            try:
                out, _ = await llm.complete_json(
                    messages=prompts.item_messages(genome, it),
                    schema=GraphItemJudgment if structured else ItemJudgment, role="item_judge",
                    effort=genome.effort.get("item_judge", "medium"), priority=priority,
                    temperature=genome.temperature, seed=call_seed("item", it.item_id, salt),
                    mock_ctx={"history": len(it.history)})
                res = ItemResult(it.item_id, out.p_lie, out.decision, out.reasoning, list(out.cues), True)
                if structured:
                    res.graph = decision_graph.graph_of(out)
                    # items show numbered history lines h1..hn, and a board only for self-play replays
                    allowed = {f"h{k}" for k in range(1, len(it.history) + 1)}
                    res.graph_checks = decision_graph.check(res.graph, allowed, board_shown=bool(it.board))
                return res
            except LLMError:
                # Failed calls abstain at the prior; the failure rate is reported alongside metrics.
                return ItemResult(it.item_id, 0.5, "trust", "", [], False)

    return list(await asyncio.gather(*(one(it) for it in items)))


class EvalIncomplete(RuntimeError):
    """Too many judge calls failed (e.g. provider outage or exhausted budget); results are not usable."""


class EvalStore:
    """Caches per-(genome hash, item set) results on disk so the incumbent is never re-judged.

    Failed calls are retried once and never cached: a cached failure would score as p_lie 0.5 forever
    and could turn an API outage into a plausible-looking gate verdict.
    """

    min_ok_rate = 0.9

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, genome: Genome, set_name: str) -> Path:
        return self.root / f"{set_name}__{genome.hash}.jsonl"

    async def get(self, genome: Genome, set_name: str, items: list[JudgeItem], llm, **kw) -> list[ItemResult]:
        p = self._path(genome, set_name)
        if p.exists():
            rows = [ItemResult(**json.loads(line)) for line in p.read_text().splitlines() if line]
            if [r.item_id for r in rows] == [it.item_id for it in items]:
                return rows
        rows = await judge_items(genome, items, llm, **kw)
        failed = [i for i, r in enumerate(rows) if not r.ok]
        if failed:   # one retry for the failures only (different salt = different seed)
            retry = await judge_items(genome, [items[i] for i in failed], llm, salt="retry", **kw)
            for i, r in zip(failed, retry):
                rows[i] = r
        ok_rate = sum(r.ok for r in rows) / max(1, len(rows))
        if ok_rate < self.min_ok_rate:
            raise EvalIncomplete(f"{set_name} for {genome.name} ({genome.hash}): only {ok_rate:.0%} of "
                                 f"{len(rows)} judge calls succeeded; not caching or scoring")
        if ok_rate == 1.0:
            p.write_text("".join(json.dumps(asdict(r)) + "\n" for r in rows))
        return rows


# ---- item construction from self-play games --------------------------------------------------------

def items_from_game(record: dict, window: int, label_mode: str = "intent",
                    reviews: dict[int, str] | None = None) -> list[JudgeItem]:
    """Receiver-seat items for every message in a (frozen) game.

    label_mode: "intent"  -> lie iff the sender privately labelled the message deceptive
                "promise" -> lie iff the reviewer found the promise broken (needs `reviews`)
                "either"  -> union of the two
    """
    sends = {int(k): v for k, v in record["sends"].items()}
    by_pair: dict[tuple[str, str], list[dict]] = {}
    items = []
    for m in record["messages"]:
        pair = tuple(sorted((m["sender"], m["recipient"])))
        prior = by_pair.setdefault(pair, [])
        intent_lie = sends.get(m["id"], {}).get("intent") == "deceptive"
        promise_lie = (reviews or {}).get(m["id"]) == "broken"
        is_lie = {"intent": intent_lie, "promise": promise_lie, "either": intent_lie or promise_lie}[label_mode]
        items.append(JudgeItem(
            item_id=f"{record['game_id']}:{m['id']}", source="sim", group=f"{record['game_id']}:{pair[0]}-{pair[1]}",
            receiver=m["recipient"], sender=m["sender"], season=m["season"], year=m["year"], target=m["text"],
            is_lie=is_lie, history=[(x["sender"], x["text"]) for x in prior[-window:]],
            board=record["boards"].get(m["phase"]), sender_sc=m["sender_sc"], receiver_sc=m["recipient_sc"]))
        prior.append(m)
    return items


def balance(items: list[JudgeItem], truth_ratio: int, seed: int) -> list[JudgeItem]:
    """Keep every lie and `truth_ratio` truths per lie; weight sampled truths so weighted metrics
    reflect the natural base rate."""
    import random

    lies = [it for it in items if it.is_lie]
    truths = [it for it in items if not it.is_lie]
    k = min(len(truths), max(1, truth_ratio * max(1, len(lies))))
    sampled = random.Random(seed).sample(truths, k) if truths else []
    w = len(truths) / k if k else 1.0
    for it in sampled:
        it.weight = w
    out = lies + sampled
    out.sort(key=lambda it: it.item_id)
    return out
