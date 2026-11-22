"""Run one self-play game.

Phase loop (movement phases):  R press rounds x [all SEND concurrently -> deliver -> all JUDGE
concurrently]  ->  all ORDERS concurrently  ->  adjudicate.  Retreat/adjustment phases: ORDERS only.
Only the powers in `seating` are played by agents; any other power is uncontrolled (no press, no
orders: its units hold and it can be conquered).

Outputs (in `out_dir`), rewritten after every press round and phase so the viewer can follow a
game live (`status` is "running" until the game ends):
- `game_<id>.json`        privileged record (vault included) for the reviewer, gate and viewer
- `dialogs_<id>.jsonl`    Peskov-compatible dialogs (same schema as the human dataset) + extras
- `maps_<id>/<phase>.svg` board with that phase's orders drawn on it
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from rsi import prompts
from rsi.agents.player import Player
from rsi.config import GameConfig
from rsi.env.bus import Bus, Message
from rsi.env.game import DiplomacyEnv
from rsi.env.vault import IsolationError, JudgeRecord, SendRecord, Vault
from rsi.genome import Genome
from rsi.llm import LLMError


class GameStats:
    def __init__(self) -> None:
        self.calls = 0
        self.failed_calls = 0
        self.invalid_orders = 0
        self.dropped_messages = 0
        self.latency_s = 0.0
        self.errors: list[str] = []     # last few call errors, for debugging / the viewer


async def _safe(coro, stats: GameStats):
    stats.calls += 1
    try:
        out, meta = await coro
        stats.latency_s += meta.latency_s
        return out, meta
    except LLMError as e:
        stats.failed_calls += 1
        stats.errors = (stats.errors + [str(e)[:400]])[-8:]
        return None, None


def _view(env: DiplomacyEnv, bus: Bus, vault: Vault, power: str, cfg: GameConfig,
          possible: dict | None = None) -> prompts.PlayerView:
    return prompts.PlayerView(
        power=power,
        units=list(env.game.get_units(power)),
        centers=list(env.game.get_centers(power)),
        memory=vault.memory[power][-cfg.memory_window:],
        dialogs=bus.dialogs_for(power, cfg.history_window),
        possible_orders=possible or {},
    )


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)   # atomic: the live viewer never reads a half-written file


async def run_game(game_id: str, seed: int, seating: dict[str, Genome], llm, cfg: GameConfig,
                   out_dir: str | Path, progress: Callable[[str], None] | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    env = DiplomacyEnv(end_year=cfg.end_year, max_order_options=cfg.max_order_options, map_name=cfg.map_name)
    active = sorted(seating)
    unknown = set(active) - set(env.powers)
    assert not unknown, f"seating has powers not on map {cfg.map_name}: {unknown}"
    bus, vault, stats = Bus(), Vault(powers=env.powers), GameStats()
    models = {p: cfg.power_models.get(p) or getattr(getattr(llm, "cfg", None), "model", "mock") for p in active}
    players = {p: Player(p, seating[p], llm, game_seed=seed, model=cfg.power_models.get(p)) for p in active}
    boards: dict[str, str] = {}
    reasoning: dict[str, dict] = {}       # phase -> power -> private reasoning texts (viewer / causal graph)
    sc_history: list[dict] = []
    error = None
    maps_dir = out_dir / f"maps_{game_id}"
    if cfg.render_maps:
        maps_dir.mkdir(exist_ok=True)

    def record(status: str) -> dict:
        return {
            "game_id": game_id, "seed": seed, "status": status, "map": cfg.map_name,
            "active_powers": active, "all_powers": env.powers,
            "seating": {p: g.hash for p, g in seating.items()},
            "genome_names": {p: g.name for p, g in seating.items()}, "models": models,
            "error": error, "final_phase": env.phase, "final_sc": env.sc_counts(),
            "sc_history": sc_history,
            "messages": [m.to_dict() for m in bus.messages],
            "sends": {m: asdict(r) for m, r in vault.sends.items()},
            "judgments": {m: asdict(r) for m, r in vault.judgments.items()},
            "orders": vault.orders, "boards": boards, "reasoning": reasoning,
            "phases": [h["phase"] for h in sc_history] + [env.phase],
            "maps_dir": maps_dir.name if cfg.render_maps else None,
            "leak_hits": vault.leak_hits,
            "stats": vars(stats) | {"wall_s": time.time() - t0},
        }

    def checkpoint() -> None:
        _write_json(out_dir / f"game_{game_id}.json", record("running"))

    def say(msg: str) -> None:
        if progress:
            progress(f"[{game_id}] {msg}")

    def note(phase: str, power: str, key: str, text: str) -> None:
        if text:
            reasoning.setdefault(phase, {}).setdefault(power, {})[key] = text

    try:
        while not env.done():
            phase = env.phase
            season, year, ptype = env.phase_parts()
            board = prompts.board_block(phase, env.units(), env.centers(), env.last_orders, env.last_phase)
            boards[phase] = board
            alive = [p for p in env.alive() if p in players]
            sc = env.sc_counts()
            # legal orders are fixed within a phase; computing them once saves engine time per call
            poss = {p: env.possible_orders(p) for p in alive}
            n_msgs0 = len(bus.messages)

            if ptype == "M":
                for rnd in range(1, cfg.press_rounds + 1):
                    # -- SEND ------------------------------------------------------------------
                    async def do_send(p: str):
                        view = _view(env, bus, vault, p, cfg, poss[p])
                        private = prompts.private_block(view)
                        recipients = [q for q in alive if q != p]
                        task = prompts.send_task(recipients, cfg.max_messages_per_round, rnd, cfg.press_rounds)
                        vault.check(p, view, private + task, cfg.strict_isolation, f"{phase}:send:{rnd}")
                        return p, await _safe(players[p].send(board, private, recipients, cfg.max_messages_per_round,
                                                              rnd, cfg.press_rounds, phase, poss[p]), stats)

                    sent: dict[str, list[Message]] = {p: [] for p in alive}
                    for p, (turn, meta) in await asyncio.gather(*(do_send(p) for p in alive)):
                        if turn is None:
                            continue
                        vault.secret(p, turn.analysis, f"analysis:{phase}:{rnd}")
                        vault.secret(p, meta.reasoning, f"thinking:{phase}:send:{rnd}")
                        vault.memory[p].append(turn.memory)
                        vault.secret(p, turn.memory, f"memory:{phase}:{rnd}")
                        note(phase, p, f"send{rnd}_analysis", turn.analysis)
                        note(phase, p, f"send{rnd}_thinking", meta.reasoning)
                        note(phase, p, f"send{rnd}_plan", "; ".join(turn.plan))
                        for om in turn.messages[: cfg.max_messages_per_round]:
                            to = om.to.strip().upper()
                            if to not in sent or to == p or not om.text.strip():
                                stats.dropped_messages += 1
                                continue
                            m = bus.post(phase=phase, season=season, year=year, round=rnd, sender=p,
                                         recipient=to, text=om.text.strip(), sender_sc=sc[p], recipient_sc=sc[to])
                            vault.sends[m.id] = SendRecord(m.id, om.intent, list(turn.plan), turn.analysis)
                            vault.bilateral(p, to, m.text, m.id)
                            sent[to].append(m)
                    checkpoint()

                    # -- JUDGE (receivers judge on receipt, before any orders are revealed) ------
                    async def do_judge(p: str, incoming: list[Message]):
                        view = _view(env, bus, vault, p, cfg)
                        private = prompts.private_block(view)
                        shown = [m.shown() for m in incoming]
                        task = prompts.judge_task(shown)
                        vault.check(p, view, private + task, cfg.strict_isolation, f"{phase}:judge:{rnd}")
                        return p, incoming, await _safe(players[p].judge(board, private, shown, phase, rnd), stats)

                    jobs = [do_judge(p, ms) for p, ms in sent.items() if ms]
                    for p, incoming, (turn, meta) in await asyncio.gather(*jobs):
                        got = {j.msg_id: j for j in (turn.judgments if turn else [])}
                        for m in incoming:
                            j = got.get(m.id)
                            if j is None:  # missing judgment: abstain at the prior, flagged
                                vault.judgments[m.id] = JudgeRecord(m.id, p, 0.5, "trust", "", [], ok=False)
                                continue
                            vault.judgments[m.id] = JudgeRecord(m.id, p, j.p_lie, j.decision, j.reasoning,
                                                                list(j.cues), meta.reasoning if meta else "")
                            vault.secret(p, j.reasoning, f"judge:{m.id}")
                        if meta:
                            vault.secret(p, meta.reasoning, f"thinking:{phase}:judge:{rnd}")
                    checkpoint()

            # -- ORDERS -----------------------------------------------------------------------------
            async def do_orders(p: str):
                possible = poss[p]
                if not possible:
                    return p, possible, (None, None)
                view = _view(env, bus, vault, p, cfg, possible)
                private = prompts.private_block(view)
                vault.check(p, view, private + prompts.orders_task(possible), cfg.strict_isolation,
                            f"{phase}:orders")
                return p, possible, await _safe(players[p].orders(board, private, possible, phase), stats)

            submitted: dict[str, list[str]] = {}
            for p, possible, (turn, meta) in await asyncio.gather(*(do_orders(p) for p in alive)):
                if not possible:
                    continue
                valid, invalid = env.validate(p, turn.orders if turn else [], possible)
                stats.invalid_orders += len(invalid)
                env.submit(p, valid)
                submitted[p] = valid
                if turn:
                    vault.memory[p].append(turn.memory)
                    vault.secret(p, turn.memory, f"memory:{phase}:orders")
                    vault.secret(p, turn.reasoning, f"orders_reasoning:{phase}")
                    note(phase, p, "orders_reasoning", turn.reasoning)
                if meta:
                    vault.secret(p, meta.reasoning, f"thinking:{phase}:orders")
                    note(phase, p, "orders_thinking", meta.reasoning)
            vault.orders[phase] = submitted
            if cfg.render_maps:
                # board with this phase's orders drawn on it, rendered off the event loop
                svg = await asyncio.to_thread(env.render_svg)
                if svg:
                    (maps_dir / f"{phase}.svg").write_text(svg)
            env.process(submitted)
            sc_history.append({"phase": phase, "sc": env.sc_counts()})
            new = bus.messages[n_msgs0:]
            lies = [m for m in new if vault.sends[m.id].intent == "deceptive"]
            caught = [m for m in lies if m.id in vault.judgments and vault.judgments[m.id].decision == "distrust"]
            say(f"{phase}: {len(new)} msgs, {len(lies)} lies ({len(caught)} caught) | SC "
                + " ".join(f"{p[:3]}{n}" for p, n in sorted(env.sc_counts().items()) if p in players))
            checkpoint()
    except IsolationError as e:
        error = f"isolation: {e}"          # the game is invalid and must not be used for training
    if cfg.render_maps and error is None:
        svg = await asyncio.to_thread(env.render_svg)
        if svg:
            (maps_dir / f"{env.phase}.svg").write_text(svg)

    final = record("error" if error else "finished")
    _write_json(out_dir / f"game_{game_id}.json", final)
    with open(out_dir / f"dialogs_{game_id}.jsonl", "w") as f:
        for d in to_peskov_dialogs(final):
            f.write(json.dumps(d) + "\n")
    say(f"finished at {final['final_phase']} in {final['stats']['wall_s']:.0f}s (error={error})")
    return final


def to_peskov_dialogs(record: dict) -> list[dict]:
    """Same fields as the Peskov et al. jsonl, so one scorer works on sim and human data.
    sender_labels: True = truthful; receiver_labels: True = trusted / False = distrusted."""
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for m in record["messages"]:
        by_pair.setdefault(tuple(sorted((m["sender"], m["recipient"]))), []).append(m)
    j = {int(k): v for k, v in record["judgments"].items()}
    s = {int(k): v for k, v in record["sends"].items()}
    out = []
    for (a, b), msgs in sorted(by_pair.items()):
        out.append({
            "messages": [m["text"] for m in msgs],
            "sender_labels": [s.get(m["id"], {}).get("intent") == "truthful" for m in msgs],
            "receiver_labels": [("NOANNOTATION" if not j.get(m["id"]) or not j[m["id"]]["ok"]
                                 else j[m["id"]]["decision"] == "trust") for m in msgs],
            "speakers": [m["sender"].lower() for m in msgs],
            "receivers": [m["recipient"].lower() for m in msgs],
            "absolute_message_index": [m["id"] for m in msgs],
            "relative_message_index": list(range(len(msgs))),
            "seasons": [m["season"] for m in msgs],
            "years": [str(m["year"]) for m in msgs],
            "game_score": [str(m["sender_sc"]) for m in msgs],
            "game_score_delta": [str(m["sender_sc"] - m["recipient_sc"]) for m in msgs],
            "players": [a.lower(), b.lower()],
            "game_id": record["game_id"],
            # extras
            "p_lie": [(j[m["id"]]["p_lie"] if j.get(m["id"]) else None) for m in msgs],
        })
    return out
