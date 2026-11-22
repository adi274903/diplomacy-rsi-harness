import json

import pytest

from rsi import human_data
from rsi.judge import balance


def _dialog(game_id, labels, recv):
    n = len(labels)
    return {
        "messages": [f"message {i} in game {game_id}" for i in range(n)],
        "sender_labels": labels, "receiver_labels": recv,
        "speakers": ["italy" if i % 2 == 0 else "germany" for i in range(n)],
        "receivers": ["germany" if i % 2 == 0 else "italy" for i in range(n)],
        "absolute_message_index": list(range(n)), "relative_message_index": list(range(n)),
        "seasons": ["Spring"] * n, "years": ["1901"] * n,
        "game_score": ["3"] * n, "game_score_delta": ["1"] * n,
        "players": ["italy", "germany"], "game_id": game_id,
    }


def test_load_labels_and_items(tmp_path):
    p = tmp_path / "train.jsonl"
    p.write_text(json.dumps(_dialog(1, [True, False, "true", True], [True, "NOANNOTATION", False, True])) + "\n")
    dialogs = human_data.load(p, "train")
    rows = dialogs[0]
    assert [r.truthful for r in rows] == [True, False, True, True]
    assert rows[1].receiver_trusts is None and rows[2].receiver_trusts is False
    assert rows[0].speaker == "ITALY" and rows[0].score == 3 and rows[0].score_delta == 1
    items = human_data.to_items(dialogs, window=2)
    assert [it.is_lie for it in items] == [False, True, False, False]
    assert items[3].history == [(rows[1].speaker, rows[1].text), (rows[2].speaker, rows[2].text)]
    assert items[0].receiver_sc == 2
    sus = human_data.to_items(dialogs, window=2, task="suspected")
    assert len(sus) == 3 and sum(it.is_lie for it in sus) == 1


def test_overlap_assertion(tmp_path):
    a = human_data.load(_write(tmp_path / "a.jsonl", _dialog(1, [True], [True])), "train")
    b = human_data.load(_write(tmp_path / "b.jsonl", _dialog(1, [True], [True])), "test")
    with pytest.raises(AssertionError):
        human_data.assert_no_game_overlap({"train": a, "test": b})


def test_balance_reweights_to_base_rate(tmp_path):
    d = _dialog(2, [False] + [True] * 19, [True] * 20)
    items = human_data.to_items(human_data.load(_write(tmp_path / "c.jsonl", d), "val"), 4)
    b = balance(items, truth_ratio=4, seed=0)
    assert sum(it.is_lie for it in b) == 1 and len(b) == 5
    assert sum(it.weight for it in b if not it.is_lie) == pytest.approx(19)


def _write(p, d):
    p.write_text(json.dumps(d) + "\n")
    return p
