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
