"""Gating on human TRAIN games when self-play produces too few lies."""

import json

import pytest

from rsi import human_data
from rsi.gate import choose_gate_set
from tests.test_human_data import _dialog


def _splits(tmp_path):
    rows = {
        "train": [_dialog(7, [True] * 18 + [False, False], [True] * 20),
                  _dialog(8, [True] * 9 + [False], [True] * 10),
                  _dialog(1, [False] * 5, [True] * 5)],
        "validation": [_dialog(11, [True, False], [True, True])],
        "test": [_dialog(12, [True, False], [True, True])],
    }
    out = {}
    for split, ds in rows.items():
        p = tmp_path / f"{split}.jsonl"
        p.write_text("".join(json.dumps(d) + "\n" for d in ds))
        out[split] = human_data.load(p, split)
    return out


def test_train_gate_items_use_only_requested_train_games(tmp_path):
    splits = _splits(tmp_path)
    items = human_data.train_gate_items(splits, [7, 8], window=12, truth_ratio=2, seed=0)
    assert {it.item_id.split(":")[0] for it in items} == {"h7", "h8"}
    assert sum(it.is_lie for it in items) == 3                       # every lie kept
    assert sum(not it.is_lie for it in items) == 6                   # 2 truths per lie
    true_rate = 3 / 30
    weighted = sum(it.weight for it in items if it.is_lie) / sum(it.weight for it in items)
    assert weighted == pytest.approx(true_rate)                      # weights restore the real lie rate


def test_train_gate_items_refuse_held_out_games(tmp_path):
    splits = _splits(tmp_path)
    with pytest.raises(ValueError, match="held out"):
        human_data.train_gate_items(splits, [7, 11], window=12, truth_ratio=2, seed=0)
    with pytest.raises(ValueError, match="not train games"):
        human_data.train_gate_items(splits, [99], window=12, truth_ratio=2, seed=0)


def test_choose_gate_set():
    assert choose_gate_set("sim", 0, 20) == "sim_gate"
    assert choose_gate_set("human_train", 500, 20) == "human_train_gate"
    assert choose_gate_set("auto", 3, 20) == "human_train_gate"
    assert choose_gate_set("auto", 25, 20) == "sim_gate"
