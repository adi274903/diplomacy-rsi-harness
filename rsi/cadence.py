"""Update-cadence experiment: Opus judges and rewrites the playbook MID-GAME, and every agent reads the
updated playbook from its next message on.

    python -m rsi.cadence --config configs/cadence.yaml --run-name cad_6m --update-every 1   # every 6 months
    python -m rsi.cadence --config configs/cadence.yaml --run-name cad_1y --update-every 2   # every year
    python -m rsi.cadence --config configs/cadence.yaml --run-name cad_2y --update-every 4   # every 2 years

Each arm plays N games in parallel (models rotated across countries) to the same end year. All games
pause at a barrier after every season (one movement phase = 6 in-game months). Every `update_every`
seasons:
  1. Opus reviews the play since the last update in `review_games` of the games (privileged view),
  2. its playbook edits are linted (no game specifics, no overlap with held-out human text) and voted,
  3. candidate playbooks are gated on human TRAIN games `select_games` (paired cluster bootstrap),
  4. the winner, if any, is swapped into every player of every game, and play resumes.
Arms are compared on human TRAIN games `report_games`, which no arm ever selects on; test stays locked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

from rsi import human_data
from rsi.agents.reviewer import Reviewer
from rsi.config import Config, load_config
from rsi.contamination import build_index
from rsi.env.game import game_powers
from rsi.evolve import aggregate_diffs, mine_exemplars, exemplar_candidate, propose_candidates
from rsi.gate import gate
from rsi.genome import Genome, default_genome
from rsi.judge import EvalIncomplete, EvalStore
from rsi.llm import make_llm
from rsi.loop import RSIStatus, _progress, seat_models
from rsi.runner import run_game
from rsi.score import compare, evaluate
from rsi.standings import game_result, short_model, standings


class SeasonBarrier:
    """All live games wait here after each season; the update runs once everyone has arrived.
    Games that end or crash leave the barrier, so a dead game can never deadlock the others."""

    def __init__(self, n: int, on_all):
        self.n_active = n
        self.on_all = on_all
        self.arrived: dict[int, dict] = {}
        self.started: set[int] = set()
        self.done: set[int] = set()
        self.cond = asyncio.Condition()

    async def _maybe_run(self, season: int) -> None:
        async with self.cond:
            ready = season not in self.started and len(self.arrived.get(season, {})) >= self.n_active
            if ready:
                self.started.add(season)
        if ready:
            try:
                await self.on_all(season, self.arrived[season])
            finally:
                async with self.cond:
                    self.done.add(season)
                    self.cond.notify_all()

    async def arrive(self, gid: str, season: int, record: dict, players: dict) -> None:
        async with self.cond:
            self.arrived.setdefault(season, {})[gid] = (record, players)
        await self._maybe_run(season)
        async with self.cond:
            await self.cond.wait_for(lambda: season in self.done)

    async def leave(self, gid: str) -> None:
        async with self.cond:
            self.n_active -= 1
            pending = [s for s in self.arrived if s not in self.started]
        for s in pending:
            await self._maybe_run(s)


def _segment(record: dict, phases: set[str]) -> dict:
    """The record restricted to the messages sent in `phases` (press only happens in movement phases);
    orders of every phase stay available so promises can be checked against what was actually played."""
    seg = dict(record)
    seg["messages"] = [m for m in record["messages"] if m["phase"] in phases]
    ids = {m["id"] for m in seg["messages"]}
    seg["sends"] = {k: v for k, v in record["sends"].items() if int(k) in ids}
    seg["judgments"] = {k: v for k, v in record["judgments"].items() if int(k) in ids}
    return seg


async def run_arm(cfg: Config, run_dir: Path, update_every: int, n_games: int, review_games: int,
                  select_games: list[int], report_games: list[int], truth_ratio: int) -> None:
    lc, rng = cfg.loop, random.Random(cfg.loop.seed)
    llm = make_llm(cfg.llm)
    reviewer = Reviewer(make_llm(cfg.reviewer) if cfg.reviewer_llm else llm, effort=cfg.reviewer_effort)
    store = EvalStore(run_dir / "evals")
    (run_dir / "genomes").mkdir(parents=True, exist_ok=True)
    st = RSIStatus(run_dir)

    splits = human_data.load_all(cfg.paths.data)
    select_items = human_data.train_gate_items(splits, select_games, cfg.game.history_window, truth_ratio, lc.seed)
    report_items = human_data.train_gate_items(splits, report_games, cfg.game.history_window, truth_ratio, lc.seed + 1)
    held = [d for d in splits["train"] if d and d[0].game_id in set(select_games) | set(report_games)]
    contamination = build_index(human_data.all_texts(splits["test"]) + human_data.all_texts(held))

    start = Genome.load(lc.initial_genome) if lc.initial_genome else default_genome()
    if lc.structured_judgments:
        start = start.model_copy(update={"structured_judgments": True})
    start.save(run_dir / "genomes")
    incumbent = start
    state = {"incumbent": start, "since": [], "updates": 0}

    r0 = await store.get(start, "select", select_items, llm)
    m0 = evaluate(select_items, r0, start.threshold)
    _log(run_dir, {"season": 0, "phase": "start", "incumbent": start.hash, "name": start.name, "select": m0})
    st.update("playing", f"arm: update every {update_every} season(s) ({update_every * 6} months); "
                         f"{n_games} games in parallel; start playbook {start.name}: select-set AUPRC "
                         f"{m0.get('auprc', float('nan')):.3f}, ECE {m0.get('ece', float('nan')):.3f}",
              incumbent=RSIStatus.playbook(start), gen=0, update_every=update_every, decisions=[], diffs=[])

    async def on_all(season: int, arrivals: dict) -> None:
        nonlocal incumbent
        phase = next(iter(arrivals.values()))[0]["sc_history"][-1]["phase"]
        state["since"].append(phase)
        total_seasons = 2 * (cfg.game.end_year - 1900)
        if season >= total_seasons:   # the game is over after this season: nothing left to play with it
            st.update(event=f"final season {phase} resolved; no further update")
            return
        if season % update_every:
            st.update(event=f"season {season} ({phase}) resolved in {len(arrivals)} game(s); next update in "
                            f"{update_every - season % update_every} season(s)")
            return
        t0 = time.time()
        state["updates"] += 1
        u = state["updates"]
        st.update(f"update {u}: Opus reviewing", f"update {u} after {phase}: Opus reviewing the last "
                  f"{len(state['since'])} season(s) in {min(review_games, len(arrivals))} game(s)", gen=u)
        gids = sorted(arrivals)
        chosen = [gids[(u * review_games + i) % len(gids)] for i in range(min(review_games, len(gids)))]
        segs = {g: _segment(arrivals[g][0], set(state["since"])) for g in chosen}
        reviews_list = await asyncio.gather(*(reviewer.review_game(seg, incumbent) for seg in segs.values()))
        reviews = dict(zip(segs, reviews_list))
        (run_dir / "reviews").mkdir(exist_ok=True)
        for g, r in reviews.items():
            (run_dir / "reviews" / f"u{u}_{g}.json").write_text(r.model_dump_json(indent=2))
        ranked, rejected = aggregate_diffs(reviews, incumbent, contamination)
        cands = propose_candidates(incumbent, ranked, lc.candidates, lc.max_diffs_per_candidate, rng, u)
        ex = exemplar_candidate(incumbent, mine_exemplars(list(segs.values()), 4, rng, contamination), u)
        if ex is not None:
            cands.append(ex)
        st.update(f"update {u}: gating", f"Opus proposed {len(ranked) + len(rejected)} edit(s): {len(ranked)} passed "
                  f"lint, {len(rejected)} rejected; gating {len(cands)} candidate playbook(s) on human games "
                  f"{select_games}", diffs=[{"op": d.diff.op, "id": d.diff.heuristic_id, "text": d.diff.text,
                                             "votes": d.votes, "why": d.diff.rationale} for d in ranked[:8]])
        best, best_d = None, None
        for c in cands:
            c.save(run_dir / "genomes")
            d = await gate(c, incumbent, select_items, [], llm, store, lc, run_dir / "gate_log.jsonl",
                           primary_set="select")
            st.update(event=f"candidate {c.name}: {'ACCEPTED' if d.accepted else 'rejected'} (ΔAUPRC "
                            f"{d.sim['auprc']['delta']:+.3f}, p_gain {d.sim['auprc']['p_gain']:.2f})",
                      decisions=(st.s.get("decisions", []) + [{"gen": u, "name": c.name, "hash": c.hash,
                                 "accepted": d.accepted, "d_auprc": d.sim["auprc"]["delta"],
                                 "p_gain": d.sim["auprc"]["p_gain"], "reasons": d.reasons}])[-30:])
            if d.accepted and (best_d is None or d.score > best_d.score):
                best, best_d = c, d
        if best is not None:
            incumbent = best
            for _, players in arrivals.values():       # every agent reads the new playbook from now on
                for pl in players.values():
                    pl.genome = best
            st.update(event=f"playbook updated mid-game → {best.name} ({best.hash}); all agents now use it",
                      incumbent=RSIStatus.playbook(best))
        else:
            st.update(event=f"update {u}: no candidate passed the gate; agents keep {incumbent.name}")
        cur = evaluate(select_items, await store.get(incumbent, "select", select_items, llm), incumbent.threshold)
        _log(run_dir, {"season": season, "phase": phase, "update": u, "incumbent": incumbent.hash,
                       "name": incumbent.name, "accepted": best.hash if best else None, "select": cur,
                       "diffs_kept": len(ranked), "diffs_rejected": len(rejected), "candidates": len(cands),
                       "reviewed_games": chosen, "wall_s": time.time() - t0})
        state["since"] = []
        st.update("playing", f"update {u} done in {time.time() - t0:.0f}s; resuming play")

    barrier = SeasonBarrier(n_games, on_all)
    powers = game_powers(cfg.game)
    pool = cfg.game.model_pool

    async def one(i: int):
        gid = f"g{i:02d}"
        models = seat_models(cfg, i)
        if models:
            _progress(f"[{gid}] seats: " + ", ".join(f"{p}={short_model(m)}" for p, m in models.items()))

        async def hook(season, record, players):
            await barrier.arrive(gid, season, record, players)
        try:
            return await run_game(gid, 50_000 + i, {p: incumbent for p in powers}, llm, cfg.game,
                                  run_dir / "games", progress=_progress, models=models, season_hook=hook)
        finally:
            await barrier.leave(gid)

    games = await asyncio.gather(*(one(i) for i in range(n_games)), return_exceptions=True)
    fatal = [g for g in games if isinstance(g, EvalIncomplete)]
    if fatal:
        raise fatal[0]
    for g in games:
        if isinstance(g, BaseException):
            _progress(f"[arm] a game crashed: {g!r}")
    ok = [g for g in games if isinstance(g, dict) and not g.get("error")]
    for g in ok:
        r = game_result(g)
        _progress(f"[{g['game_id']}] WINNER: " + " & ".join(f"{p} ({short_model(m)})" for p, m in
                                                           zip(r["winners"], r["winner_models"])))

    # final comparison on report games no arm ever selected on
    st.update("final report", f"scoring start vs final playbook on human games {report_games}")
    a = await store.get(start, "report", report_items, llm)
    b = await store.get(incumbent, "report", report_items, llm)
    final = {"update_every_seasons": update_every, "n_games": n_games, "games_ok": len(ok),
             "updates": state["updates"], "start": start.hash, "final": incumbent.hash, "final_name": incumbent.name,
             "report_games": report_games, "report": {"start": evaluate(report_items, a, start.threshold),
                                                      "final": evaluate(report_items, b, incumbent.threshold)},
             "paired": compare(report_items, a, b, ("auprc", "lie_f1", "ece"), lc.bootstrap_n, lc.seed)
             if incumbent.hash != start.hash else None,
             "standings": standings(run_dir)["by_model"]}
    (run_dir / "final_genome.json").write_text(incumbent.model_dump_json(indent=2))
    (run_dir / "cadence_result.json").write_text(json.dumps(final, indent=2))
    st.update("finished", f"done: {state['updates']} update(s); final playbook {incumbent.name} ({incumbent.hash})")
    await llm.aclose()


def _log(run_dir: Path, row: dict) -> None:
    with open(run_dir / "updates.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.cadence")
    ap.add_argument("--config", default="configs/cadence.yaml")
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--update-every", type=int, required=True, help="seasons between updates (1 = 6 months)")
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--review-games", type=int, default=5, help="games Opus reviews at each update")
    ap.add_argument("--select-games", default="7,8")
    ap.add_argument("--report-games", default="9,10")
    ap.add_argument("--truth-ratio", type=int, default=2)
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.mock:
        cfg.llm.backend = "mock"
        if cfg.reviewer_llm:
            cfg.reviewer_llm.backend = "mock"
    cfg.llm.run_name = a.run_name
    run_dir = Path(cfg.paths.runs) / a.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(cfg.model_dump_json(indent=2))
    try:
        asyncio.run(run_arm(cfg, run_dir, a.update_every, a.games, a.review_games,
                            [int(x) for x in a.select_games.split(",")], [int(x) for x in a.report_games.split(",")],
                            a.truth_ratio))
    except EvalIncomplete as e:
        raise SystemExit(f"[abort] judge calls are failing; no verdict can be trusted: {e}") from e


if __name__ == "__main__":
    main()
