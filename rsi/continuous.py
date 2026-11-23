"""Event-driven RSI: games of random lengths run in parallel; whenever games finish, Opus judges them
(privileged transcript + decision-trace graph) and rewrites the playbook; a passing rewrite goes live in
every game still running, from each agent's next message.

    python -m rsi.continuous --config configs/continuous.yaml --run-name cont1 --games 10

- each game gets a random length (months, 3-month steps: one press round = 3 months, orders end the
  6-month season) and a seeded random seating of the model pool
- finished games queue up; one update runs at a time, once >= `batch_messages` new messages are queued
  (or when no game is left running)
- candidates are gated on human TRAIN games `select_games`; the start and final playbooks are compared on
  `report_games` (never selected on). Every message/judgment records the playbook version it used.
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
from rsi.causal import game_graphs
from rsi.config import Config, load_config
from rsi.contamination import build_index
from rsi.env.game import game_powers
from rsi.evolve import aggregate_diffs, exemplar_candidate, mine_exemplars, propose_candidates
from rsi.gate import gate
from rsi.genome import Genome, default_genome
from rsi.judge import EvalIncomplete, EvalStore
from rsi.llm import make_llm
from rsi.loop import RSIStatus, _progress, seat_models
from rsi.runner import run_game
from rsi.score import arrays, compare, evaluate
from rsi import core


def tuned(items, results) -> tuple[float, float]:
    """Lie-F1-maximising cutoff on these items (picked on the select games, applied elsewhere)."""
    y, p, w, _, _ = arrays(items, results)
    return core.best_f1_threshold(y, p, w)
from rsi.standings import game_result, short_model, standings

LENGTHS = [3, 6, 9, 12, 15, 18, 21, 24]     # months; 14 and 20 round up to the 3-month game clock


def graph_summary(record: dict, max_lines: int = 60) -> str:
    """The decision-trace graph in text form: which judgments the agents later cited when planning or
    ordering, and how much of each plan they actually played."""
    lines = []
    for phase, g in game_graphs(record).items():
        for e in g["edges"]:
            if e["kind"] == "cited":
                mid = e["from"].split(":")[1]
                who = e["to"].split(":")[1]
                where = "orders" if e["to"].startswith("orders") else "next plan"
                lines.append(f"{phase}: {who}'s judgment of #{mid} was cited in its {where}: \"{e['evidence'][0][:160]}\"")
            elif e["kind"] == "kept":
                lines.append(f"{phase}: {e['to'].split(':')[1]} {e['label']} planned orders")
    if not lines:
        return ""
    return "## Decision-trace graph (what each agent's later reasoning relied on)\n" + "\n".join(lines[:max_lines])


async def _spend(cfg: Config) -> float:
    """Spend so far through this run's gateways (players + judge), from their /stats."""
    import httpx

    total = 0.0
    urls = {cfg.llm.base_url, cfg.reviewer.base_url}
    async with httpx.AsyncClient(timeout=5) as c:
        for u in urls:
            try:
                total += (await c.get(u.rsplit("/v1", 1)[0] + "/stats")).json().get("est_cost_usd", 0.0)
            except Exception:  # noqa: BLE001 - mock runs have no gateway
                pass
    return total


async def run_pool(cfg: Config, run_dir: Path, n_games: int, batch_messages: int, select_games: list[int],
                   report_games: list[int], truth_ratio: int, forever: bool = False,
                   stop_usd: float = 0.0, seed_history: str | None = None, seed_max: int = 8) -> None:
    """`forever`: keep `n_games` slots busy, starting a new game whenever one finishes, until spend reaches
    `stop_usd` or a STOP file appears in the run directory; then finish running games and report."""
    lc = cfg.loop
    rng = random.Random(lc.seed)
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
    s = {"incumbent": start, "version": 0, "queue": [], "running": {}, "updates": 0}
    lock = asyncio.Lock()

    r0 = await store.get(start, "select", select_items, llm)
    m0 = evaluate(select_items, r0, start.threshold)
    thr0, f0 = tuned(select_items, r0)
    m0.update(tuned_threshold=thr0, lie_f1_tuned=f0)
    versions = [{"version": 0, "hash": start.hash, "name": start.name, "select": m0, "t": time.strftime("%H:%M:%S")}]
    s["thr"] = {start.hash: thr0}
    _log(run_dir, {"event": "start", **versions[0]})
    lengths = [rng.choice(LENGTHS) for _ in range(n_games)]
    counter = {"next": n_games}
    if seed_history:   # past games (e.g. earlier runs copied to the VM) go straight into Opus's review queue
        past = []
        for f in Path(seed_history).rglob("game_*.json"):
            try:
                r = json.loads(f.read_text())
            except ValueError:
                continue
            if r.get("status") == "finished" and not r.get("error") and len(r.get("messages", [])) >= 20:
                r["game_id"] = f"hist_{f.parent.parent.name}_{r['game_id']}"
                past.append((f.stat().st_mtime, r))
        seeded = [r for _, r in sorted(past, key=lambda x: -x[0])[:seed_max]]
        s["queue"].extend(seeded)
        st.update(event=f"history: queued {len(seeded)} past game(s) from {seed_history} "
                        f"({sum(len(r['messages']) for r in seeded)} messages) for Opus's first review")
    verified = Path("knowledge/results/2026-11-22-test-human_v2-vs-v0.json")
    if verified.exists() and start.hash == json.loads(verified.read_text()).get("final"):
        t = json.loads(verified.read_text())
        a0, a1 = t["results"]["v0"]["test"], t["results"][t["final_name"]]["test"]
        st.update(event=f"starting point = Opus's playbook {start.name}, verified on held-out human test: AUPRC "
                        f"{a1['auprc']:.3f} (v0 {a0['auprc']:.3f}), AUROC {a1['auroc']:.3f} (v0 {a0['auroc']:.3f}), "
                        f"ECE {a1['ece']:.3f} (v0 {a0['ece']:.3f}); human receivers AUROC "
                        f"{t['human_receivers']['auroc']:.3f}", opus_baseline=t)
    st.update("playing", f"{n_games} game slots started (lengths {sorted(lengths)} months)"
                         + (f"; continuous until ${stop_usd:.0f} or a STOP file" if forever else "")
                         + f"; playbook v0 = {start.name}, "
                         f"select AUPRC {m0.get('auprc', float('nan')):.3f}",
              incumbent=RSIStatus.playbook(start) | {"version": 0}, versions=versions, decisions=[], diffs=[])

    async def update(force: bool = False) -> None:
        async with lock:
            while s["queue"] and (force or sum(len(r["messages"]) for r in s["queue"]) >= batch_messages
                                  or not s["running"]):
                batch, s["queue"] = s["queue"], []
                await _update_once(batch)

    async def _update_once(batch: list[dict]) -> None:
        t0 = time.time()
        s["updates"] += 1
        u, inc = s["updates"], s["incumbent"]
        ids = [r["game_id"] for r in batch]
        n_msgs = sum(len(r["messages"]) for r in batch)
        st.update(f"update {u}: Opus judging", f"update {u}: Opus judging {len(batch)} finished game(s) {ids} "
                  f"({n_msgs} messages) with their decision graphs", gen=u)
        reviews_list = await asyncio.gather(*(reviewer.review_game(r, inc, extra_context=graph_summary(r))
                                              for r in batch))
        reviews = {r["game_id"]: rv for r, rv in zip(batch, reviews_list)}
        for gid, rv in reviews.items():
            (run_dir / "games" / f"review_{gid}.json").write_text(rv.model_dump_json(indent=2))
        ranked, rejected = aggregate_diffs(reviews, inc, contamination)
        cands = propose_candidates(inc, ranked, lc.candidates, lc.max_diffs_per_candidate, rng, u)
        ex = exemplar_candidate(inc, mine_exemplars(batch, 4, rng, contamination), u)
        if ex is not None:
            cands.append(ex)
        st.update(f"update {u}: gating", f"Opus proposed {len(ranked) + len(rejected)} edit(s): {len(ranked)} passed "
                  f"lint, {len(rejected)} rejected; gating {len(cands)} candidate(s) on human games {select_games}",
                  diffs=[{"op": d.diff.op, "id": d.diff.heuristic_id, "text": d.diff.text, "votes": d.votes,
                          "why": d.diff.rationale} for d in ranked[:8]])
        best, best_d = None, None
        for c in cands:
            c.save(run_dir / "genomes")
            d = await gate(c, inc, select_items, [], llm, store, lc, run_dir / "gate_log.jsonl", primary_set="select")
            st.update(event=f"candidate {c.name}: {'ACCEPTED' if d.accepted else 'rejected'} "
                            f"(ΔAUPRC {d.sim['auprc']['delta']:+.3f}, p_gain {d.sim['auprc']['p_gain']:.2f})",
                      decisions=(st.s.get("decisions", []) + [{"gen": u, "name": c.name, "hash": c.hash,
                                 "accepted": d.accepted, "d_auprc": d.sim["auprc"]["delta"],
                                 "p_gain": d.sim["auprc"]["p_gain"], "reasons": d.reasons}])[-40:])
            if d.accepted and (best_d is None or d.score > best_d.score):
                best, best_d = c, d
        if best is not None:
            s["incumbent"], s["version"] = best, s["version"] + 1
            live = [gid for gid, players in s["running"].items()]
            for players in s["running"].values():          # every running agent reads it from its next message
                for pl in players.values():
                    pl.genome = best
            m = dict(best_d.sim_metrics["candidate"])
            thr, f1t = tuned(select_items, await store.get(best, "select", select_items, llm))
            m.update(tuned_threshold=thr, lie_f1_tuned=f1t)
            s["thr"][best.hash] = thr
            versions.append({"version": s["version"], "hash": best.hash, "name": best.name, "select": m,
                             "t": time.strftime("%H:%M:%S"), "from_games": ids})
            st.update(event=f"playbook v{s['version']} ({best.name}) is live in {len(live)} running game(s); "
                            f"select AUPRC {m.get('auprc', float('nan')):.3f}",
                      incumbent=RSIStatus.playbook(best) | {"version": s["version"]}, versions=versions)
            st.update(event=f"v{s['version']} lie-F1 on the select games: {m.get('lie_f1', 0):.3f} at 0.5, "
                            f"{f1t:.3f} at its tuned cutoff {thr:.2f}")
        else:
            st.update(event=f"update {u}: no candidate passed the gate; playbook stays v{s['version']}")
        _log(run_dir, {"event": "update", "update": u, "games": ids, "messages": n_msgs, "accepted": bool(best),
                       "version": s["version"], "incumbent": s["incumbent"].hash, "diffs_kept": len(ranked),
                       "diffs_rejected": len(rejected), "candidates": len(cands), "wall_s": time.time() - t0})
        st.update("playing" if s["running"] else "finishing", f"update {u} done in {time.time() - t0:.0f}s")

    powers = game_powers(cfg.game)

    async def one(i: int) -> dict | None:
        gid = f"g{i:02d}"
        months = lengths[i] if i < n_games else rng.choice(LENGTHS)
        models = seat_models(cfg, i)
        _progress(f"[{gid}] {months} months; seats: " + ", ".join(f"{p}={short_model(m)}" for p, m in (models or {}).items()))
        players: dict = {}
        s["running"][gid] = players
        try:
            rec = await run_game(gid, 70_000 + i, {p: s["incumbent"] for p in powers}, llm, cfg.game,
                                 run_dir / "games", progress=_progress, models=models, max_months=months,
                                 players_out=players)
        finally:
            s["running"].pop(gid, None)
        if rec.get("error"):
            _progress(f"[{gid}] excluded: {rec['error'][:120]}")
            return rec
        r = game_result(rec)
        _progress(f"[{gid}] finished after {months} months; leader: "
                  + " & ".join(f"{p} ({short_model(m)})" for p, m in zip(r["winners"], r["winner_models"])))
        s["queue"].append(rec)
        # review in the background: the slot starts its next game right away instead of idling
        # while Opus judges (updates still run one at a time under the lock)
        s.setdefault("tasks", set()).add(t := asyncio.get_running_loop().create_task(update()))
        t.add_done_callback(s["tasks"].discard)
        return rec

    async def slot(i: int) -> list:
        out = []
        while True:
            out.append(await one(i))
            if not forever or (run_dir / "STOP").exists():
                return out
            spent = await _spend(cfg)
            if spent >= stop_usd:
                st.update(event=f"budget stop: ${spent:.2f} spent >= ${stop_usd:.2f}; no new games in this slot")
                return out
            i = counter["next"]
            counter["next"] += 1
            st.update(event=f"slot freed: starting game g{i:02d} with playbook v{s['version']} "
                            f"(${spent:.2f} spent so far)")

    if s["queue"]:
        asyncio.get_running_loop().create_task(update())   # review the history while the new games play
    nested = await asyncio.gather(*(slot(i) for i in range(n_games)), return_exceptions=True)
    results = [x for n in nested for x in (n if isinstance(n, list) else [n])]
    fatal = [x for x in results if isinstance(x, EvalIncomplete)]
    if fatal:
        raise fatal[0]
    if s.get("tasks"):
        await asyncio.gather(*list(s["tasks"]), return_exceptions=True)
    await update(force=True)

    st.update("final report", f"scoring v0 vs final playbook on human games {report_games} (never selected on)")
    final_g = s["incumbent"]
    a = await store.get(start, "report", report_items, llm)
    b = await store.get(final_g, "report", report_items, llm)
    out = {"games": n_games, "lengths": lengths, "updates": s["updates"], "versions": versions,
           "start": start.hash, "final": final_g.hash, "final_name": final_g.name, "report_games": report_games,
           "report": {"start": evaluate(report_items, a, start.threshold),
                      "final": evaluate(report_items, b, final_g.threshold),
                      # cutoffs tuned on the select games only, then applied to the untouched report games
                      "start_tuned": evaluate(report_items, a, s["thr"].get(start.hash, 0.5)),
                      "final_tuned": evaluate(report_items, b, s["thr"].get(final_g.hash, 0.5))},
           "judge_model": cfg.llm.model,
           "paired": compare(report_items, a, b, ("auprc", "lie_f1", "ece"), lc.bootstrap_n, lc.seed)
           if final_g.hash != start.hash else None,
           "standings": standings(run_dir)["by_model"]}
    (run_dir / "final_genome.json").write_text(final_g.model_dump_json(indent=2))
    (run_dir / "continuous_result.json").write_text(json.dumps(out, indent=2))
    st.update("finished", f"done: {s['updates']} update(s), {len(versions) - 1} accepted; final playbook "
                          f"v{s['version']} {final_g.name} ({final_g.hash})")
    await llm.aclose()


def _log(run_dir: Path, row: dict) -> None:
    with open(run_dir / "updates.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.continuous")
    ap.add_argument("--config", default="configs/cadence.yaml")
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--batch-messages", type=int, default=40)
    ap.add_argument("--select-games", default="7,8")
    ap.add_argument("--report-games", default="9,10")
    ap.add_argument("--truth-ratio", type=int, default=2)
    ap.add_argument("--forever", action="store_true", help="keep slots busy until --stop-usd or a STOP file")
    ap.add_argument("--stop-usd", type=float, default=0.0, help="soft budget: no new games once spend reaches it")
    ap.add_argument("--seed-history", default=None, help="dir of past runs whose games Opus reviews first")
    ap.add_argument("--seed-max", type=int, default=8)
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.mock:
        cfg.llm.backend = "mock"
        if cfg.reviewer_llm:
            cfg.reviewer_llm.backend = "mock"
    cfg.llm.run_name = a.run_name
    cfg.game.end_year = max(cfg.game.end_year, 1903)    # length is governed by max_months
    run_dir = Path(cfg.paths.runs) / a.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(cfg.model_dump_json(indent=2))
    try:
        asyncio.run(run_pool(cfg, run_dir, a.games, a.batch_messages, [int(x) for x in a.select_games.split(",")],
                             [int(x) for x in a.report_games.split(",")], a.truth_ratio, a.forever, a.stop_usd,
                             a.seed_history, a.seed_max))
    except EvalIncomplete as e:
        raise SystemExit(f"[abort] judge calls are failing; no verdict can be trusted: {e}") from e


if __name__ == "__main__":
    main()
