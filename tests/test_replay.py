"""Replay mode: a recorded game is rebuilt step by step as a live game, with no model calls."""

import json

import pytest

pytest.importorskip("diplomacy")

from rsi.config import GameConfig  # noqa: E402
from rsi.env.game import POWERS  # noqa: E402
from rsi.genome import default_genome  # noqa: E402
from rsi.llm import MockLLM  # noqa: E402
from rsi.replay import is_replay, pick_games, replay_game, snapshots  # noqa: E402
from rsi.runner import run_game  # noqa: E402
from rsi.standings import standings  # noqa: E402
from rsi.viewer import _current_game, build_index, game_data  # noqa: E402


@pytest.fixture
async def recorded(tmp_path):
    games = tmp_path / "games"
    await run_game("t1", 5, {p: default_genome() for p in POWERS}, MockLLM(), GameConfig(end_year=1901), games)
    src = games / "game_t1.json"
    return tmp_path, src, json.loads(src.read_text())


async def test_snapshots_rebuild_the_game_in_order(recorded):
    _, _, rec = recorded
    steps = list(snapshots(rec))
    kinds = [k for k, _ in steps]
    assert kinds[-1] == "finished" and steps[-1][1] == dict(rec, status="finished")
    assert all(s["status"] == "running" for _, s in steps[:-1])
    assert kinds.count("message") == len(rec["messages"])
    assert kinds.count("judgment") == len(rec["judgments"])
    assert kinds.count("orders") == len(rec["sc_history"])
    prev_msgs = prev_phases = prev_sc = 0
    for _, s in steps[:-1]:
        shown = {str(m["id"]) for m in s["messages"]}
        assert set(map(str, s["judgments"])) <= shown           # never a verdict before its message
        assert set(map(str, s["sends"])) <= shown
        assert len(s["messages"]) >= prev_msgs and len(s["phases"]) >= prev_phases
        assert len(s["sc_history"]) >= prev_sc
        assert set(s["orders"]) == {h["phase"] for h in s["sc_history"]}
        prev_msgs, prev_phases, prev_sc = len(s["messages"]), len(s["phases"]), len(s["sc_history"])
        game_data(s)                                              # every step renders in the live view


async def test_replay_is_served_live_and_never_counted(recorded):
    run_dir, src, rec = recorded
    before = standings(run_dir)
    seen = []

    def watch(_s):   # at each pause, the live view must point at the replay while it is running
        cur = _current_game(run_dir)
        seen.append(cur)

    dest = replay_game(src, run_dir, {}, sleep=watch)
    assert is_replay(dest, run_dir) and dest.parent.name == "replay"
    out = json.loads(dest.read_text())
    assert out["status"] == "finished" and out["game_id"] == "rt1" and out["replay_of"] == "games/game_t1.json"
    assert len(out["messages"]) == len(rec["messages"])
    assert (dest.parent / out["maps_dir"]).is_dir()
    assert seen and all(p == "/replay/game_rt1.html" for p in seen[:-1])
    # replays are not extra games: standings, the index and the picker ignore them
    assert len(standings(run_dir)["games"]) == len(before["games"]) == 1
    assert "replay/" not in build_index(run_dir).read_text()
    assert pick_games(run_dir, run_dir, min_messages=1) == [src]


async def test_pick_games_filters(recorded):
    run_dir, src, rec = recorded
    assert pick_games(run_dir, run_dir, min_messages=len(rec["messages"]) + 1) == []
    assert pick_games(run_dir, run_dir, names=["nope"], min_messages=1) == []
    bad = dict(rec, game_id="t2", error="boom")
    (src.parent / "game_t2.json").write_text(json.dumps(bad))
    assert pick_games(run_dir, run_dir, min_messages=1) == [src]
