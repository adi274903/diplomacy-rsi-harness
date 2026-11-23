"""Structured judgments: schema, checks, replay, and an end-to-end mock game with graphs on."""

import math

import pytest

from rsi import decision_graph as dg
from rsi.genome import default_genome
from rsi.schemas import GraphItemJudgment


def _graph(**kw):
    g = {
        "claims": [{"id": "c1", "text": "will support you", "type": "promise_of_support", "checkable": True}],
        "evidence": [
            {"id": "e1", "source": "#5", "claim": "c1", "relation": "contradicts", "note": ""},
            {"id": "e2", "source": "#99", "claim": "c9", "relation": "supports", "note": ""},
            {"id": "e3", "source": "board", "claim": "c1", "relation": "background", "note": ""},
        ],
        "adjustments": [{"basis": "e7", "direction": "toward_truth", "strength": "weak", "note": ""}],
        "p_lie": 0.2,
    }
    return g | kw


def test_check_flags_unseen_sources_dangling_refs_and_ignored_contradictions():
    out = dg.check(_graph(), {"#5", "#6"})
    assert out["invalid_sources"] == ["#99"]
    assert out["dangling_refs"] == ["e2", "e7"]
    assert out["contradictions_without_adjustment"] == ["e1"]
    assert dg.check(_graph(), {"#5", "#99"}, board_shown=False)["invalid_sources"] == ["board"]


def test_replay_moves_in_log_odds_and_can_drop_nodes():
    adj = [{"basis": "e1", "direction": "toward_lie", "strength": "strong"},
           {"basis": "e2", "direction": "toward_truth", "strength": "weak"}]
    assert math.isclose(dg.implied_p([]), dg.BASE_RATE)
    assert math.isclose(dg.logit(dg.implied_p(adj)), dg.logit(dg.BASE_RATE) + 1.5)
    assert math.isclose(dg.logit(dg.implied_p(adj, drop={"e1"})), dg.logit(dg.BASE_RATE) - 0.5)


def test_cited_ids_and_schema():
    assert dg.cited_message_ids(_graph()) == [5, 99]
    GraphItemJudgment.model_validate({**_graph(), "reasoning": "r", "cues": [], "decision": "trust"} | {})


def test_flag_off_keeps_existing_genome_hashes():
    assert default_genome().hash == default_genome(structured=False).hash
    assert default_genome(structured=True).hash != default_genome().hash


async def test_mock_game_with_structured_judgments(tmp_path):
    pytest.importorskip("diplomacy")
    from rsi.causal import game_graphs
    from rsi.config import GameConfig
    from rsi.env.game import POWERS
    from rsi.llm import MockLLM
    from rsi.runner import run_game

    g = default_genome(structured=True)
    llm = MockLLM()
    rec = await run_game("sg", 3, {p: g for p in POWERS}, llm, GameConfig(end_year=1901), tmp_path)
    assert rec["error"] is None and rec["leak_hits"] == []
    judged = [j for j in rec["judgments"].values() if j["ok"]]
    assert judged and all(j["graph"] and j["graph_checks"] is not None for j in judged)
    # mock receivers only cite messages they were shown, so no citation is flagged
    assert not any(j["graph_checks"]["invalid_sources"] for j in judged)
    assert any("Show how you reached each judgment" in m[-1]["content"] for m in llm.prompts)
    edges = [e for graph in game_graphs(rec).values() for e in graph["edges"]]
    assert any(e["kind"] == "evidence" for e in edges)


async def test_item_judging_produces_checked_graphs():
    from rsi.judge import JudgeItem, judge_items
    from rsi.llm import MockLLM

    it = JudgeItem("h1:train:0:3", "human", "h1:train:0", "FRANCE", "ITALY", "Spring", 1901, "trust me",
                   False, [("ITALY", "hi"), ("FRANCE", "hello")])
    (res,) = await judge_items(default_genome(structured=True), [it], MockLLM())
    assert res.ok and res.graph and res.graph_checks["invalid_sources"] == []
