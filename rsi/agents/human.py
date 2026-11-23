"""A human player seat.

Drop-in replacement for `rsi.agents.player.Player`: the game loop calls send / judge / orders exactly as it
does for model agents and gets the same structured turns back, so the human is recorded, isolated and judged
like any other player. Each call publishes a pending action and waits until the web UI submits it (from the
HTTP thread, via `submit`) or the deadline passes (then a safe default is used: no messages, no judgment,
default orders).
"""

from __future__ import annotations

import asyncio
import time

from rsi.genome import Genome
from rsi.llm import CallMeta
from rsi.prompts import ShownMessage
from rsi.schemas import JudgeTurn, Judgment, OrdersTurn, OutMessage, SendTurn


class HumanSeat:
    def __init__(self, power: str, genome: Genome, timeout_s: float = 600.0):
        self.power = power
        self.genome = genome            # not read by a human; only stamped on records like any seat
        self.model = "human"
        self.timeout_s = timeout_s
        self.pending: dict | None = None
        self.history: list[dict] = []   # what this seat submitted, for the UI
        self._fut: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- called from the HTTP thread ------------------------------------------------------------------
    def state(self) -> dict:
        p = self.pending
        if p is None:
            return {"pending": None}
        return {"pending": {k: v for k, v in p.items() if k != "_t0"},
                "seconds_left": max(0, int(p["_t0"] + self.timeout_s - time.time()))}

    def submit(self, data: dict) -> tuple[bool, str]:
        p, fut, loop = self.pending, self._fut, self._loop
        if p is None or fut is None or loop is None:
            return False, "nothing to submit right now"
        if data.get("kind") != p["kind"] or data.get("key") != p["key"]:
            return False, f"expected a '{p['kind']}' submission for {p['key']}"
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(data))
        return True, "ok"

    # -- called by the game loop ----------------------------------------------------------------------
    async def _ask(self, kind: str, key: str, payload: dict) -> dict | None:
        self._loop = asyncio.get_running_loop()
        self._fut = self._loop.create_future()
        self.pending = {"kind": kind, "key": key, "_t0": time.time(), **payload}
        try:
            return await asyncio.wait_for(self._fut, self.timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending, self._fut = None, None

    async def send(self, board: str, private: str, recipients: list[str], max_messages: int,
                   round_no: int, rounds: int, phase: str, possible: dict[str, list[str]]):
        data = await self._ask("send", f"{phase}:send:{round_no}", {
            "phase": phase, "round": round_no, "rounds": rounds, "board": board, "private": private,
            "recipients": recipients, "max_messages": max_messages})
        msgs = []
        for m in (data or {}).get("messages", [])[:max_messages]:
            to, text = str(m.get("to", "")).upper(), str(m.get("text", "")).strip()
            if to in recipients and text:
                msgs.append(OutMessage(to=to, text=text[:2000],
                                       intent="deceptive" if m.get("deceptive") else "truthful"))
        turn = SendTurn(analysis=str((data or {}).get("analysis", ""))[:2000], plan=[], messages=msgs,
                        memory=str((data or {}).get("memory", ""))[:500])
        self.history.append({"phase": phase, "kind": "send", "messages": [m.model_dump() for m in msgs]})
        return turn, CallMeta(reasoning="")

    async def judge(self, board: str, private: str, incoming: list[ShownMessage], phase: str, round_no: int,
                    shown_ids: list[int] | None = None):
        data = await self._ask("judge", f"{phase}:judge:{round_no}", {
            "phase": phase, "round": round_no, "board": board, "private": private,
            "incoming": [{"id": m.id, "sender": m.sender, "text": m.text} for m in incoming]})
        ids = {m.id for m in incoming}
        out = []
        for j in (data or {}).get("judgments", []):
            try:
                mid, p = int(j["msg_id"]), min(1.0, max(0.0, float(j["p_lie"])))
            except (KeyError, TypeError, ValueError):
                continue
            if mid in ids:
                out.append(Judgment(msg_id=mid, reasoning=str(j.get("reasoning", ""))[:2000], cues=["human"],
                                    p_lie=p, decision="distrust" if p >= 0.5 else "trust"))
        self.history.append({"phase": phase, "kind": "judge", "judgments": [j.model_dump() for j in out]})
        return JudgeTurn(judgments=out), CallMeta(reasoning="")

    async def orders(self, board: str, private: str, possible: dict[str, list[str]], phase: str):
        data = await self._ask("orders", f"{phase}:orders", {
            "phase": phase, "board": board, "private": private, "possible": possible})
        orders = [str(o) for o in (data or {}).get("orders", [])]
        turn = OrdersTurn(reasoning=str((data or {}).get("reasoning", ""))[:2000], orders=orders,
                          memory=str((data or {}).get("memory", ""))[:500])
        self.history.append({"phase": phase, "kind": "orders", "orders": orders})
        return turn, CallMeta(reasoning="")
