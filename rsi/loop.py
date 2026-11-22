"""Orchestration CLI.

    python -m rsi.loop smoke       --config configs/mock.yaml            # one game, no model needed
    python -m rsi.loop human-stats --config configs/api.yaml             # download + dataset stats
    python -m rsi.loop run         --config configs/api.yaml --run-name sprint1
    python -m rsi.loop final-test  --config configs/api.yaml --run-name sprint1   # exactly once
    python -m rsi.loop report      --run-name sprint1
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
from rsi.contamination import build_index, memorization_probe, overlap_report, paraphrase_items
from rsi.env.game import game_powers
from rsi.evolve import (FrontEntry, aggregate_diffs, effort_candidate, exemplar_candidate, mine_exemplars,
                        pareto_front, propose_candidates)
from rsi.gate import gate
from rsi.genome import Genome, default_genome
from rsi.judge import EvalStore, balance, items_from_game, judge_items
from rsi.league import League
from rsi.llm import make_llm
from rsi.runner import run_game
from rsi.score import apply_temperature, arrays, evaluate, fit_temperature


def _log(run_dir: Path, name: str, row: dict) -> None:
    with open(run_dir / name, "a") as f:
        f.write(json.dumps(row) + "\n")


class RSIStatus:
    """Live RSI state for the viewer (runs/<run>/rsi_status.json): stage, playbook, diffs, gate."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "rsi_status.json"
        self.s: dict = {"stage": "starting", "gen": 0, "events": []}

    def update(self, stage: str | None = None, event: str | None = None, **kw) -> None:
        if stage:
            self.s["stage"] = stage
        if event:
            self.s["events"] = ([{"t": time.strftime("%H:%M:%S"), "text": event}] + self.s["events"])[:40]
            _progress("[rsi] " + event)
        self.s.update(kw)
        self.s["updated"] = time.strftime("%H:%M:%S")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s))
        tmp.replace(self.path)

    @staticmethod
    def playbook(g: Genome) -> dict:
        return {"name": g.name, "hash": g.hash, "heuristics": [{"id": h.id, "text": h.text} for h in g.playbook],
                "exemplars": len(g.exemplars), "judge_effort": g.effort.get("judge")}


def _progress(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


async def play_many(specs: list[tuple[str, int, dict[str, Genome]]], llm, cfg: Config, out_dir: Path) -> list[dict]:
    sem = asyncio.Semaphore(cfg.loop.parallel_games)

    async def one(gid, seed, seating):
        async with sem:
            return await run_game(gid, seed, seating, llm, cfg.game, out_dir, progress=_progress)

    games = await asyncio.gather(*(one(*s) for s in specs))
    bad = [g["game_id"] for g in games if g["error"]]
    if bad:
        print(f"  ! {len(bad)} game(s) invalid and excluded: {bad}")
    return [g for g in games if not g["error"]]


def _human_val_items(cfg: Config, splits) -> list:
    items = human_data.to_items(splits["validation"], cfg.game.history_window)
    return balance(items, cfg.loop.human_val_truth_ratio, cfg.loop.seed)


# ---- commands ---------------------------------------------------------------------------------------

async def cmd_smoke(cfg: Config, run_dir: Path) -> None:
    llm = make_llm(cfg.llm)
    v0 = default_genome()
    g = await run_game("smoke", cfg.loop.seed, {p: v0 for p in game_powers(cfg.game)}, llm, cfg.game,
                       run_dir / "games", progress=_progress)
    s = g["stats"]
    print(f"phase reached {g['final_phase']}, messages {len(g['messages'])}, judgments {len(g['judgments'])}, "
          f"calls {s['calls']} (failed {s['failed_calls']}), invalid orders {s['invalid_orders']}, "
          f"leaks {len(g['leak_hits'])}, error {g['error']}, wall {s['wall_s']:.1f}s")
    print("final supply centres:", g["final_sc"])
    await llm.aclose()


def cmd_human_stats(cfg: Config) -> None:
    splits = human_data.load_all(cfg.paths.data)
    print(json.dumps(human_data.stats(splits), indent=2))


async def cmd_run(cfg: Config, run_dir: Path) -> None:
    lc, rng = cfg.loop, random.Random(cfg.loop.seed)
    llm = make_llm(cfg.llm)
    reviewer = Reviewer(make_llm(cfg.reviewer) if cfg.reviewer_llm else llm, effort=cfg.reviewer_effort)
    store = EvalStore(run_dir / "evals")
    (run_dir / "genomes").mkdir(parents=True, exist_ok=True)

    print("[data] human benchmark (validation = transfer gate; test untouched)")
    splits = human_data.load_all(cfg.paths.data)
    val_items = _human_val_items(cfg, splits)
    contamination = build_index(human_data.all_texts(splits["validation"]) + human_data.all_texts(splits["test"]))

    v0 = default_genome()
    v0.save(run_dir / "genomes")
    league = League(v0)
    st = RSIStatus(run_dir)
    st.update("gate games", f"playing {lc.gate_games} frozen gate game(s) with playbook v0",
              incumbent=RSIStatus.playbook(v0), decisions=[], diffs=[])

    print(f"[gate] playing {lc.gate_games} frozen gate games (v0 in every seat, disjoint seeds)")
    powers = game_powers(cfg.game)
    gate_games = await play_many([(f"gate{i}", 10_000 + i, {p: v0 for p in powers}) for i in range(lc.gate_games)],
                                 llm, cfg, run_dir / "gate_games")
    sim_items = balance([it for g in gate_games for it in items_from_game(g, cfg.game.history_window, lc.label_mode)],
                        lc.sim_gate_truth_ratio, lc.seed)
    n_lies = sum(it.is_lie for it in sim_items)
    print(f"[gate] {len(sim_items)} sim gate items ({n_lies} lies), {len(val_items)} human-val items")
    if n_lies < 10:
        print("  ! very few self-labelled lies in gate games; the sim gate will be noisy")

    res0 = await store.get(v0, "sim_gate", sim_items, llm)
    m0 = evaluate(sim_items, res0, v0.threshold)
    front = [FrontEntry(v0, m0.get("auprc", 0.0), m0.get("ece", 1.0))]
    incumbent, stale = v0, 0
    _log(run_dir, "generations.jsonl", {"gen": 0, "incumbent": v0.hash, "sim_gate": m0})
    st.update("baseline", f"v0 baseline on the gate: AUPRC {m0.get('auprc', float('nan')):.3f}, "
                          f"ECE {m0.get('ece', float('nan')):.3f} ({m0.get('n_lie', 0)} lies / {m0.get('n', 0)} items)",
              baseline=m0)

    for gen in range(1, lc.generations + 1):
        t0 = time.time()
        parent = rng.choice(pareto_front(front)).genome
        specs = [(f"g{gen}_{i}", gen * 1000 + i, league.seat(parent, rng, lc.frozen_share, powers))
                 for i in range(lc.games_per_generation)]
        st.update(f"generation {gen}: playing", f"generation {gen}: playing {len(specs)} game(s) with playbook "
                  f"{parent.name}", gen=gen, parent=RSIStatus.playbook(parent))
        games = await play_many(specs, llm, cfg, run_dir / f"gen_{gen}" / "games")
        for g in games:
            league.record(g)

        st.update(f"generation {gen}: judge reviewing", f"judge reviewing {len(games)} finished game(s)")
        reviews_list = await asyncio.gather(*(reviewer.review_game(g, parent) for g in games))
        reviews = {g["game_id"]: r for g, r in zip(games, reviews_list)}
        for gid, r in reviews.items():   # next to each game, so the viewer can show it
            (run_dir / f"gen_{gen}" / "games" / f"review_{gid}.json").write_text(r.model_dump_json(indent=2))
        with open(run_dir / f"gen_{gen}" / "reviews.jsonl", "w") as f:
            for gid, r in reviews.items():
                f.write(json.dumps({"game_id": gid, **r.model_dump()}) + "\n")
        leak_flags = sum(gr.leak_flag for r in reviews.values() for gr in r.grades)

        ranked, rejected = aggregate_diffs(reviews, parent, contamination)
        cands = propose_candidates(parent, ranked, lc.candidates, lc.max_diffs_per_candidate, rng, gen)
        ex = exemplar_candidate(parent, mine_exemplars(games, 4, rng, contamination), gen)
        eff = effort_candidate(parent, gen)
        cands += [c for c in (ex, eff) if c is not None]
        st.update(f"generation {gen}: gating", f"judge proposed {len(ranked) + len(rejected)} playbook edit(s): "
                  f"{len(ranked)} passed lint, {len(rejected)} rejected; testing {len(cands)} candidate playbook(s)",
                  diffs=[{"op": d.diff.op, "id": d.diff.heuristic_id, "text": d.diff.text, "votes": d.votes,
                          "why": d.diff.rationale} for d in ranked[:8]])

        decisions = []
        for c in cands:
            c.save(run_dir / "genomes")
            d = await gate(c, incumbent, sim_items, val_items, llm, store, lc, run_dir / "gate_log.jsonl")
            decisions.append((c, d))
            st.update(event=f"candidate {c.name}: {'ACCEPTED' if d.accepted else 'rejected'} "
                            f"(ΔAUPRC {d.sim['auprc']['delta']:+.3f}, p_gain {d.sim['auprc']['p_gain']:.2f})"
                            + (f" — {'; '.join(d.reasons)}" if d.reasons else ""),
                      decisions=(st.s.get("decisions", []) + [{"gen": gen, "name": c.name, "hash": c.hash,
                                 "accepted": d.accepted, "d_auprc": d.sim["auprc"]["delta"],
                                 "p_gain": d.sim["auprc"]["p_gain"], "reasons": d.reasons}])[-20:])

        accepted = [(c, d) for c, d in decisions if d.accepted]
        if accepted:
            best, bd = max(accepted, key=lambda cd: cd[1].score)
            league.freeze(incumbent)
            incumbent, stale = best, 0
            cm = bd.sim_metrics["candidate"]
            front.append(FrontEntry(best, cm.get("auprc", 0.0), cm.get("ece", 1.0)))
            (run_dir / "incumbent.json").write_text(best.model_dump_json(indent=2))
            st.update(event=f"playbook updated → {best.name} ({best.hash}); next games use it",
                      incumbent=RSIStatus.playbook(best))
        else:
            stale += 1
            st.update(event=f"generation {gen}: no candidate passed the gate; keeping {incumbent.name}")
        _log(run_dir, "generations.jsonl", {
            "gen": gen, "parent": parent.hash, "incumbent": incumbent.hash, "games": len(games),
            "diffs_kept": len(ranked), "diffs_rejected": len(rejected), "candidates": len(cands),
            "accepted": [c.hash for c, _ in accepted], "leak_flags": leak_flags,
            "league": league.table(), "wall_s": time.time() - t0})
        if stale >= lc.patience:
            print(f"[stop] no accepted candidate for {stale} generations")
            break

    (run_dir / "final_genome.json").write_text(incumbent.model_dump_json(indent=2))
    st.update("finished", f"run finished; final playbook {incumbent.name} ({incumbent.hash})")
    print(f"[done] final genome {incumbent.name} ({incumbent.hash})")
    await llm.aclose()


async def cmd_final_test(cfg: Config, run_dir: Path, force: bool) -> None:
    lock = run_dir / "TEST_LOCK"
    if lock.exists() and not force:
        raise SystemExit(f"test set already evaluated for this run ({lock}); refusing to re-run")
    llm = make_llm(cfg.llm)
    store = EvalStore(run_dir / "evals")
    splits = human_data.load_all(cfg.paths.data)
    final = Genome.model_validate_json((run_dir / "final_genome.json").read_text())
    v0 = default_genome()
    val_items = _human_val_items(cfg, splits)
    test_items = human_data.to_items(splits["test"], cfg.game.history_window)
    para_items = await paraphrase_items(llm, test_items, run_dir / "paraphrase_test.json", seed=cfg.loop.seed)

    out = {"final": final.hash, "v0": v0.hash, "results": {}}
    for g in (v0, final):
        # calibration is fit on human validation only, then applied to test
        vres = await store.get(g, "human_val", val_items, llm)
        y, p, w, _, _ = arrays(val_items, vres)
        t = fit_temperature(y, p, w)
        row = {"temperature": t}
        for name, items in (("test", test_items), ("test_paraphrased", para_items)):
            res = await judge_items(g, items, llm, priority="low")
            row[name] = evaluate(items, res, g.threshold)
            for r in res:
                r.p_lie = apply_temperature([r.p_lie], t)[0]
            row[name + "_calibrated"] = evaluate(items, res, g.threshold)
        out["results"][g.name] = row

    texts = [it.target for it in test_items]
    out["memorization_probe"] = await memorization_probe(llm, texts, seed=cfg.loop.seed)
    exemplars = [e for p in (run_dir / "genomes").glob("*.json") for e in Genome.load(p).exemplars]
    heuristics = [h.text for p in (run_dir / "genomes").glob("*.json") for h in Genome.load(p).playbook]
    out["training_channel_overlap"] = overlap_report(build_index(texts), exemplars + heuristics)
    (run_dir / "final_test.json").write_text(json.dumps(out, indent=2))
    lock.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    print(json.dumps(out["results"], indent=2))
    await llm.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.loop")
    ap.add_argument("command", choices=["smoke", "human-stats", "run", "final-test", "report"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--mock", action="store_true", help="force the mock LLM backend")
    ap.add_argument("--force", action="store_true", help="final-test: allow re-running (recorded)")
    ap.add_argument("--end-year", type=int, default=None, help="override game.end_year")
    a = ap.parse_args()

    cfg = load_config(a.config)
    if a.end_year:
        cfg.game.end_year = a.end_year
    if a.mock:
        cfg.llm.backend = "mock"
        if cfg.reviewer_llm:
            cfg.reviewer_llm.backend = "mock"
    name = a.run_name or time.strftime("run_%Y%m%d_%H%M%S")
    cfg.llm.run_name = name
    run_dir = Path(cfg.paths.runs) / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(cfg.model_dump_json(indent=2))

    if a.command == "smoke":
        asyncio.run(cmd_smoke(cfg, run_dir))
    elif a.command == "human-stats":
        cmd_human_stats(cfg)
    elif a.command == "run":
        asyncio.run(cmd_run(cfg, run_dir))
    elif a.command == "final-test":
        asyncio.run(cmd_final_test(cfg, run_dir, a.force))
    else:
        from rsi.report import write_report

        print(write_report(run_dir))


if __name__ == "__main__":
    main()
