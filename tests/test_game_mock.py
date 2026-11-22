"""End-to-end self-play with the mock backend: game runs, logs are Peskov-shaped, isolation holds."""

import pytest

pytest.importorskip("diplomacy")

from rsi import prompts  # noqa: E402
from rsi.config import GameConfig  # noqa: E402
from rsi.env.game import POWERS  # noqa: E402
from rsi.env.vault import IsolationError, Vault  # noqa: E402
from rsi.genome import default_genome  # noqa: E402
from rsi.judge import items_from_game  # noqa: E402
from rsi.llm import MockLLM  # noqa: E402
from rsi.runner import run_game, to_peskov_dialogs  # noqa: E402

PESKOV_FIELDS = {"messages", "sender_labels", "receiver_labels", "speakers", "receivers", "absolute_message_index",
                 "relative_message_index", "seasons", "years", "game_score", "game_score_delta", "players", "game_id"}


async def test_mock_game_runs_isolated(tmp_path):
    llm = MockLLM()
    v0 = default_genome()
    cfg = GameConfig(end_year=1901, strict_isolation=True)
    rec = await run_game("t0", 1, {p: v0 for p in POWERS}, llm, cfg, tmp_path)
    assert rec["error"] is None, rec["error"]
    assert rec["leak_hits"] == []
    assert rec["messages"], "mock players should exchange messages"
    assert rec["final_phase"].startswith("S1902")
    # every message got a judgment record
    assert set(rec["judgments"]) == {m["id"] for m in rec["messages"]}
    for d in to_peskov_dialogs(rec):
        assert PESKOV_FIELDS <= set(d)
        assert len({len(d[k]) for k in PESKOV_FIELDS - {"players", "game_id"}}) == 1
    # no player prompt contains another dialog's message text
    for msgs in llm.prompts:
        user = msgs[-1]["content"]
        me = user.split("## You are ")[1].split("\n")[0] if "## You are " in user else None
        if me is None:
            continue
        for m in rec["messages"]:
            if me not in (m["sender"], m["recipient"]):
                assert m["text"] not in user
    items = items_from_game(rec, 12)
    assert len(items) == len(rec["messages"])
    assert all(all(t != it.target for _, t in it.history) for it in items)


def test_vault_detects_leak():
    v = Vault()
    v.secret("FRANCE", "I am secretly planning to attack Belgium next spring.", "memory:x")
    view = prompts.PlayerView(power="ENGLAND", units=[], centers=[], memory=[], dialogs={})
    with pytest.raises(IsolationError):
        v.check("ENGLAND", view, "notes: i am secretly planning to attack belgium next spring.", True, "test")
    # the owner itself may see its own secret
    v.check("FRANCE", view.__class__(power="FRANCE", units=[], centers=[], memory=[], dialogs={}),
            "I am secretly planning to attack Belgium next spring.", True, "test")


def test_vault_structural_check():
    v = Vault()
    m = prompts.ShownMessage(0, "S1901M", "FRANCE", "GERMANY", "hello there")
    view = prompts.PlayerView(power="ENGLAND", units=[], centers=[], memory=[], dialogs={"FRANCE": [m]})
    with pytest.raises(IsolationError):
        v.check("ENGLAND", view, "", True, "test")
