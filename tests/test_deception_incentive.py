"""Opt-in deception incentive: prompt text, unchanged default genome, and the per-power believed-lie count."""

import pytest

from rsi.genome import DECEPTION_INCENTIVE, default_genome


def test_default_genome_unchanged_and_incentive_opt_in():
    base = default_genome()
    on = default_genome(deception_incentive=True)
    assert DECEPTION_INCENTIVE not in base.system_prompt
    assert on.system_prompt.endswith(DECEPTION_INCENTIVE)
    assert on.hash != base.hash and default_genome(deception_incentive=False).hash == base.hash


async def test_game_record_counts_believed_lies(tmp_path):
    pytest.importorskip("diplomacy")
    from rsi.config import GameConfig
    from rsi.env.game import POWERS
    from rsi.llm import MockLLM
    from rsi.runner import run_game

    g = default_genome(deception_incentive=True)
    llm = MockLLM()
    rec = await run_game("dec", 2, {p: g for p in POWERS}, llm, GameConfig(end_year=1901), tmp_path)
    assert rec["error"] is None
    sends = {int(k): v for k, v in rec["sends"].items()}
    judg = {int(k): v for k, v in rec["judgments"].items()}
    for power, d in rec["deception"].items():
        lies = [m for m in rec["messages"] if m["sender"] == power and sends[m["id"]]["intent"] == "deceptive"]
        assert d["deceptive_sent"] == len(lies)
        assert d["believed"] == sum(judg[m["id"]]["ok"] and judg[m["id"]]["decision"] == "trust" for m in lies)
        assert d["believed"] + d["caught"] + d["unjudged"] == d["deceptive_sent"]
    assert any(DECEPTION_INCENTIVE in msgs[0]["content"] for msgs in llm.prompts)
