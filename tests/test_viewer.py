import pytest

pytest.importorskip("diplomacy")

from rsi.config import GameConfig  # noqa: E402
from rsi.env.game import POWERS  # noqa: E402
from rsi.genome import default_genome  # noqa: E402
from rsi.llm import MockLLM  # noqa: E402
from rsi.runner import run_game  # noqa: E402
from rsi.viewer import build_index  # noqa: E402


async def test_viewer_builds_with_maps(tmp_path):
    games = tmp_path / "games"
    rec = await run_game("v0", 3, {p: default_genome() for p in POWERS}, MockLLM(), GameConfig(end_year=1901), games)
    maps = games / rec["maps_dir"]
    assert {p + ".svg" for p in rec["phases"]} <= {f.name for f in maps.iterdir()}
    index = build_index(tmp_path)
    html = index.read_text()
    assert "games/game_v0.html" in html
    page = (games / "game_v0.html").read_text()
    assert '"phases"' in page and "</script>" in page and "lies caught" in page


async def test_causal_graph_and_live_data(tmp_path):
    from rsi.causal import phase_graph
    from rsi.viewer import game_data

    rec = await run_game("c0", 5, {p: default_genome() for p in ["AUSTRIA", "FRANCE", "GERMANY", "ITALY"]},
                         MockLLM(), GameConfig(end_year=1901, render_maps=False), tmp_path)
    assert set(rec["active_powers"]) == {"AUSTRIA", "FRANCE", "GERMANY", "ITALY"}
    assert all(m["sender"] in rec["active_powers"] for m in rec["messages"])
    g = phase_graph(rec, "S1901M")
    ids = {n["id"] for n in g["nodes"]}
    assert g["nodes"] and all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    judged = [e for e in g["edges"] if e["kind"] == "judged"]
    assert len(judged) == sum(1 for m in rec["messages"] if m["phase"] == "S1901M")
    d = game_data(rec)
    assert d["status"] == "finished" and d["graphs"]["S1901M"]["nodes"]
