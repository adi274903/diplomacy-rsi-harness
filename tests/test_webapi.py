"""RSI dashboard endpoints (rsi/webapi.py) on a small run directory built with the mock backend."""

import asyncio
import json
import random
from dataclasses import asdict

import pytest

pytest.importorskip("diplomacy")

from rsi import webapi  # noqa: E402
from rsi.agents.reviewer import Reviewer  # noqa: E402
from rsi.config import Config, GameConfig  # noqa: E402
from rsi.evolve import aggregate_diffs, propose_candidates  # noqa: E402
from rsi.gate import GateDecision  # noqa: E402
from rsi.genome import default_genome  # noqa: E402
from rsi.llm import MockLLM  # noqa: E402
from rsi.runner import run_game  # noqa: E402
from rsi.schemas import PlaybookDiff  # noqa: E402

POWERS = ["AUSTRIA", "FRANCE", "GERMANY", "ITALY"]


def _cmp(delta, p_gain, lo=None, hi=None):
    return {"delta": delta, "mean": delta, "lo": delta - 0.02 if lo is None else lo,
            "hi": delta + 0.02 if hi is None else hi, "p_gain": p_gain, "n_valid": 200}


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    return asyncio.run(_build_run(tmp_path_factory))


async def _build_run(tmp_path_factory):
    """gate game + one generation (1 game, judge review, 2 candidates, one accepted) + a second
    generation in progress, laid out exactly as rsi.loop writes it."""
    rd = tmp_path_factory.mktemp("run")
    llm, v0 = MockLLM(), default_genome()
    gcfg = GameConfig(end_year=1901, render_maps=False, active_powers=POWERS)
    (rd / "config.json").write_text(Config(game=gcfg).model_dump_json())
    v0.save(rd / "genomes")
    await run_game("gate0", 10_000, {p: v0 for p in POWERS}, llm, gcfg, rd / "gate_games")
    base = {"n": 40, "n_lie": 0, "auprc": float("nan"), "ece": 0.12, "brier": 0.2}
    rows = [{"gen": 0, "incumbent": v0.hash, "sim_gate": base}]

    g = await run_game("g1_0", 1000, {p: v0 for p in POWERS}, llm, gcfg, rd / "gen_1" / "games")
    review = await Reviewer(llm).review_game(g, v0)
    # fixed edits (the mock reviewer's are random): one add, one delete, and one the lint must reject
    review.diffs = [PlaybookDiff(op="add", text="Weigh a promise more when the sender has kept earlier promises to you.",
                                 rationale="receivers over-trusted repeat liars", evidence_msg_ids=[0, 1]),
                    PlaybookDiff(op="delete", heuristic_id="H6", rationale="never used", evidence_msg_ids=[]),
                    PlaybookDiff(op="add", text="In game 12 FRANCE lied twice.", rationale="x", evidence_msg_ids=[2])]
    (rd / "gen_1" / "games" / "review_g1_0.json").write_text(review.model_dump_json())
    (rd / "gen_1" / "reviews.jsonl").write_text(json.dumps({"game_id": "g1_0", **review.model_dump()}) + "\n")
    ranked, rejected = aggregate_diffs({"g1_0": review}, v0)
    cands = propose_candidates(v0, ranked, 2, 3, random.Random(0), 1)
    assert cands, "the mock reviewer should propose at least one usable diff"
    log = []
    for i, c in enumerate(cands):
        c.save(rd / "genomes")
        ok = i == 0
        log.append(GateDecision(candidate=c.hash, incumbent=v0.hash, accepted=ok,
                                reasons=[] if ok else ["sim AUPRC p_gain 0.40 < 0.8"],
                                sim={"auprc": _cmp(0.05 if ok else -0.01, 0.9 if ok else 0.4),
                                     "ece": _cmp(-0.01, 0.6), "brier": _cmp(0.0, 0.5)},
                                sim_metrics={"incumbent": {"auprc": 0.3, "ece": 0.12},
                                             "candidate": {"auprc": 0.35 if ok else 0.29, "ece": 0.11}}))
    with open(rd / "gate_log.jsonl", "w") as f:
        for d in log:
            f.write(json.dumps(asdict(d)) + "\n")
    rows.append({"gen": 1, "parent": v0.hash, "incumbent": cands[0].hash, "games": 1, "diffs_kept": len(ranked),
                 "diffs_rejected": len(rejected), "candidates": len(cands), "accepted": [cands[0].hash],
                 "leak_flags": 0, "league": [], "wall_s": 1.0})
    (rd / "generations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (rd / "incumbent.json").write_text(cands[0].model_dump_json())
    (rd / "rsi_status.json").write_text(json.dumps({
        "stage": "generation 2: playing", "gen": 2, "events": [{"t": "12:00:00", "text": "generation 2: playing"}],
        "incumbent": {"name": cands[0].name, "hash": cands[0].hash},
        "parent": {"name": cands[0].name, "hash": cands[0].hash}, "updated": "12:00:00"}))
    return rd, cands, v0


def call(rd, name, **q):
    status, ctype, body = webapi.handle(name, {k: [v] for k, v in q.items()}, rd)
    assert ctype == "application/json"
    return status, json.loads(body)   # strict JSON: NaN must have been turned into null


def test_run(run_dir):
    rd, cands, v0 = run_dir
    st, d = call(rd, "run")
    assert st == 200 and d["run"] == rd.name
    assert d["baseline"]["auprc"] is None and d["gate_set"] == {"games": 1, "finished": 1, "n": 40, "n_lie": 0}
    assert [g["gen"] for g in d["generations"]] == [1, 2]
    g1, g2 = d["generations"]
    assert g1["done"] and g1["accepted"][0]["hash"] == cands[0].hash and g1["best_p_gain"] == 0.9
    assert g1["incumbent_metrics"]["auprc"] == 0.35 and g1["reviews"] == 1 and g1["games_finished"] == 1
    assert g2["live"] and not g2["done"] and g2["stage"] == "generation 2: playing"
    assert d["incumbent"]["hash"] == cands[0].hash and d["incumbent"]["generation_adopted"] == 1
    assert d["incumbent"]["genome"]["playbook"]
    assert d["totals"]["games"] == 2 and d["totals"]["candidates"] == len(cands) and d["totals"]["accepted"] == 1
    assert d["config"]["active_powers"] == POWERS and d["config"]["p_gain_min"] == 0.8


def test_gates(run_dir):
    rd, cands, _ = run_dir
    _, d = call(rd, "gates")
    assert [x["candidate"] for x in d["decisions"]] == [c.hash for c in cands]
    first = d["decisions"][0]
    assert first["gen"] == 1 and first["accepted"] and first["name"] == cands[0].name and first["kind"] == "playbook"
    assert set(first["sim"]) == {"auprc", "ece", "brier"}
    assert {"delta", "lo", "hi", "p_gain"} <= set(first["sim"]["auprc"]) and first["incumbent_name"] == "v0"


def test_lineage(run_dir):
    rd, cands, v0 = run_dir
    _, d = call(rd, "lineage")
    nodes = {n["hash"]: n for n in d["nodes"]}
    assert set(nodes) == {v0.hash, *(c.hash for c in cands)}
    assert nodes[v0.hash]["status"] == "root" and nodes[v0.hash]["parent"] is None
    top = nodes[cands[0].hash]
    assert top["status"] == "incumbent" and top["parent"] == v0.hash and top["loop_gens"] == [1]
    diff = top["diff"]
    assert set(diff) == {"added", "edited", "removed", "exemplars", "effort", "other"}
    assert diff["added"] or diff["edited"] or diff["removed"]
    assert top["proposals_used"] and all(p["votes"] >= 1 for p in top["proposals_used"])
    assert all(n["status"] == "rejected" for h, n in nodes.items() if h not in (v0.hash, cands[0].hash))
    p = d["proposals"][0]
    assert p["gen"] == 1 and len(p["ranked"]) == 2 and p["ranked"][0]["votes"] == 1
    assert len(p["rejected"]) == 1 and "references a specific game/run/seed" in p["rejected"][0]["errors"]


def test_games_and_deception(run_dir):
    rd, _, v0 = run_dir
    _, d = call(rd, "games")
    games = {g["id"]: g for g in d["games"]}
    assert set(games) == {"gate0", "g1_0"} and games["gate0"]["gen"] == "gate" and games["g1_0"]["gen"] == 1
    g = games["g1_0"]
    assert g["has_review"] and g["status"] == "finished" and g["messages"] > 0
    assert set(g["seats"]) == set(POWERS) and g["seats"]["FRANCE"]["genome"] == v0.hash
    assert g["caught"] + g["fooled"] <= g["lies"] and g["version"]
    _, dd = call(rd, "deception")
    rows = [r for r in dd["rows"] if r["game"] == "g1_0"]
    assert {r["power"] for r in rows} == set(POWERS)
    assert sum(r["sent"] for r in rows) == sum(r["received"] for r in rows) == g["messages"]
    assert sum(r["lies"] for r in rows) == sum(r["lies_received"] for r in rows) == g["lies"]


def test_review(run_dir):
    rd, _, _ = run_dir
    st, d = call(rd, "review", game="g1_0")
    assert st == 200 and d["grades"] and d["summary"]
    gr = d["grades"][0]
    assert {"msg_id", "promise", "sender", "recipient", "text", "intent", "outcome"} <= set(gr)
    assert all("votes" in x and "lint_errors" in x for x in d["diffs"])
    assert [bool(x["lint_errors"]) for x in d["diffs"]] == [False, False, True]
    assert call(rd, "review", game="gate0")[1]["review"] is None


def test_graphs(run_dir):
    rd, _, _ = run_dir
    st, d = call(rd, "graph", game="g1_0", phase="S1901M")
    assert st == 200 and d["mode"] == "phase" and d["nodes"] and "chains" in d
    ids = {n["id"] for n in d["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in d["edges"])
    st, t = call(rd, "graph", game="g1_0")
    assert st == 200 and t["mode"] == "game" and t["phases"][0] == "S1901M"
    ids = {n["id"] for n in t["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in t["edges"])
    assert any(e["kind"] == "carry" for e in t["edges"])
    for c in t["chains"]:
        assert c["path"] and all(p in ids for p in c["path"]) and c["outcome"] in ("caught", "fooled", "unjudged")


def test_f1(run_dir):
    rd, cands, v0 = run_dir
    st, d = call(rd, "f1")
    assert st == 200
    g = {r["hash"]: r for r in d["genomes"]}
    # baseline has no lies: F1 is undefined (None), never 0, and flagged unstable
    assert g[v0.hash]["sim_source"] == "baseline" and g[v0.hash]["sim"]["lie_f1"] is None
    assert g[v0.hash]["sim"]["unstable"] and g[cands[0].hash]["sim_source"] == "gate_log"
    assert [t["gen"] for t in d["trajectory"]] == [0, 1] and d["trajectory"][1]["hash"] == cands[0].hash
    c0 = d["candidates"][0]
    assert c0["accepted"] and c0["delta"]["auprc"] == pytest.approx(0.05) and c0["ci"]["auprc"]["p_gain"] == 0.9

    live = d["live"]
    assert live["rule"] == "decision" and live["scope"] == "all" and live["scopes"] == ["all", "gate", "1"]
    o = live["overall"]
    assert o["n"] == o["caught"] + o["fooled"] + o["false_alarms"] + o["trusted_truths"]
    assert o["n_lie"] == o["caught"] + o["fooled"]
    assert set(live["by_country"]) == set(POWERS) and sum(m["n"] for m in live["by_country"].values()) == o["n"]
    assert sum(m["n"] for m in live["by_model"].values()) == o["n"]
    for m in [o, *live["by_country"].values()]:
        if m["n_lie"] == 0:
            assert m["lie_f1"] is None and m["macro_f1"] is None and m["recall"] is None
        else:
            assert m["lie_f1"] == pytest.approx(2 * m["caught"] / (2 * m["caught"] + m["false_alarms"] + m["fooled"]))
        assert m["unstable"] == (m["n_lie"] < 5)
    game = next(x for x in live["games"] if x["id"] == "g1_0")
    assert game["series"] and game["series"][-1]["overall"] == game["overall"]
    ns = [x["overall"]["n"] for x in game["series"]]
    assert ns == sorted(ns) and set(game["series"][0]["by_country"]) == set(POWERS)   # cumulative per phase
    assert live["run_series"][-1]["n"] == o["n"]

    _, gate_only = call(rd, "f1", scope="gate", rule="p_lie")
    assert gate_only["live"]["rule"] == "p_lie" and [x["id"] for x in gate_only["live"]["games"]] == ["gate0"]


def test_f1_counts_by_hand():
    from rsi.webapi import _conf

    m = _conf(tp=2, fp=1, fn=2, tn=5)
    assert m["precision"] == pytest.approx(2 / 3) and m["recall"] == pytest.approx(0.5)
    assert m["lie_f1"] == pytest.approx(4 / 7) and m["truth_f1"] == pytest.approx(10 / 13)
    assert m["macro_f1"] == pytest.approx((4 / 7 + 10 / 13) / 2) and m["unstable"]
    assert _conf(tn=3)["lie_f1"] is None and _conf(tn=3)["macro_f1"] is None


def test_metrics_from_evals_matches_scorer(tmp_path):
    """The evals fallback rebuilds the sim-gate items exactly as the loop does."""
    from rsi.judge import EvalStore, balance, items_from_game
    from rsi.score import evaluate

    async def build():
        llm, v0 = MockLLM(), default_genome()
        gcfg = GameConfig(end_year=1901, render_maps=False, active_powers=POWERS)
        cfg = Config(game=gcfg)
        (tmp_path / "config.json").write_text(cfg.model_dump_json())
        v0.save(tmp_path / "genomes")
        g = await run_game("gate0", 10_000, {p: v0 for p in POWERS}, llm, gcfg, tmp_path / "gate_games")
        items = balance(items_from_game(g, gcfg.history_window, cfg.loop.label_mode), cfg.loop.sim_gate_truth_ratio,
                        cfg.loop.seed)
        res = await EvalStore(tmp_path / "evals").get(v0, "sim_gate", items, llm)
        return v0, evaluate(items, res, v0.threshold)
    v0, want = asyncio.run(build())
    got = webapi._metrics_from_evals(tmp_path, v0.hash, v0.threshold)
    assert got is not None and got["n"] == want["n"] and got["n_lie"] == want["n_lie"]
    _, d = call(tmp_path, "f1")
    assert d["genomes"][0]["sim_source"] == "evals" and d["genomes"][0]["sim"]["n"] == want["n"]


def test_changelog(run_dir):
    rd, cands, v0 = run_dir
    st, d = call(rd, "changelog")
    assert st == 200 and [e["gen"] for e in d["entries"]] == [2, 1]   # newest first, live gen included
    live, g1 = d["entries"]
    assert live["live"] and not live["done"] and live["stage"] == "generation 2: playing"
    assert g1["done"] and g1["adopted"]["hash"] == cands[0].hash and g1["incumbent_before"] == v0.hash
    acc = g1["accepted"][0]
    assert acc["hash"] == cands[0].hash and acc["ci"]["auprc"]["lo"] == pytest.approx(0.03)
    assert set(acc["delta"]) == {"lie_f1", "macro_f1", "auprc", "ece"} and acc["changes"]["removed"]
    ops = {(p["op"], p["lint"]) for p in g1["proposals"]}
    assert ops == {("add", "passed"), ("delete", "passed"), ("add", "rejected")} and g1["lint_rejected"] == 1
    add = next(p for p in g1["proposals"] if p["op"] == "add" and p["lint"] == "passed")
    assert add["votes"] == 1 and add["evidence"] == [{"game": "g1_0", "msg_ids": [0, 1]}]
    assert add["rationale"] == "receivers over-trusted repeat liars" and cands[0].name in add["used_by"]
    rej = next(p for p in g1["proposals"] if p["lint"] == "rejected")
    assert "references a specific game/run/seed" in rej["lint_errors"]
    assert len(g1["candidates"]) == len(cands)


def test_standings(run_dir):
    rd, _, _ = run_dir
    st, d = call(rd, "standings")
    assert st == 200 and d["finished"] == 2
    games = {g["game_id"]: g for g in d["games"]}
    assert set(games) == {"gate0", "g1_0"} and games["gate0"]["gen"] == "gate" and games["g1_0"]["gen"] == 1
    for g in games.values():
        top = max(g["final_sc"].values())
        assert g["winner_sc"] == top and g["winners"] == sorted(p for p, n in g["final_sc"].items() if n == top)
    # matrix: one cell per (model, country) played; games sum to seats in finished games; wins sum to games
    M = d["matrix"]
    assert set(M["countries"]) == set(POWERS)
    cells = [c for row in M["cells"].values() for c in row.values()]
    assert sum(c["games"] for c in cells) == 2 * len(POWERS)
    assert sum(c["wins"] for c in cells) == pytest.approx(2)
    # leaderboards agree with the matrix; each model row carries deception counts and F1
    for r in d["by_model"]:
        row = M["cells"][r["name"]]
        assert r["games"] == sum(c["games"] for c in row.values()) and r["wins"] == pytest.approx(sum(c["wins"] for c in row.values()))
        assert {"lies_told", "promises_broken", "caught", "fooled", "false_alarms", "catch_rate"} <= set(r)
        assert r["f1"]["n_lie"] == r.get("caught", 0) + r.get("fooled", 0)
    assert sum(r["wins"] for r in d["by_country"]) == pytest.approx(2)


def test_f1_label_modes(run_dir):
    rd, _, _ = run_dir
    intent = call(rd, "f1", label="intent")[1]["live"]
    either = call(rd, "f1", label="either")[1]["live"]
    assert intent["label"] == "intent" and either["label"] == "either"
    assert intent["overall"]["n"] == either["overall"]["n"]
    # broken promises (from the judge's review of g1_0) can only add deceptive messages
    assert either["overall"]["n_lie"] >= intent["overall"]["n_lie"]
    assert either["unreviewed_games"] == ["gate0"] and intent["unreviewed_games"] == []
    assert call(rd, "f1")[1]["live"]["label"] == "intent"   # the run's own label_mode (config default)


def test_safety(run_dir):
    rd, _, _ = run_dir
    for bad in ["../../etc/passwd", "gate0/../x", "", "g1_0.json", "nope"]:
        assert call(rd, "graph", game=bad)[0] == 404
        assert call(rd, "review", game=bad)[0] == 404
    assert call(rd, "graph", game="g1_0", phase="S1999M")[0] == 404
    assert call(rd, "nonexistent")[0] == 404
    assert call(rd, "ping")[1]["ok"]


def test_empty_run_dir(tmp_path):
    """A run that has only just started (no files yet) must not error."""
    for name in ("run", "gates", "lineage", "games", "deception", "f1", "changelog", "standings"):
        st, d = call(tmp_path, name)
        assert st == 200, (name, d)
