"""Peskov et al. (ACL 2020) "It Takes Two to Lie" — held-out human benchmark ONLY.

Never used for training: no human text may enter a playbook, exemplar or diff (enforced by the
n-gram contamination index in `rsi.contamination`). Split roles:
    train      -> reviewer validation and baseline; optionally the acceptance gate's item set
                  (`loop.gate_source: human_train | auto`, games `loop.human_gate_games`). The reviewer
                  never sees these items: they only decide which reviewer-proposed edits survive.
    validation -> transfer gate (each accepted genome must not regress on it)
    test       -> final evaluation, exactly once (guarded by a lock file)

Data gotchas handled here:
    - sender_labels True = truthful, so a lie is `sender_labels == False`
    - receiver_labels mixes booleans with the string "NOANNOTATION"
    - game_score / game_score_delta / years are strings
    - do NOT use the repo's singlemessage_format.py (it shuffles messages, destroying context)
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

from rsi.judge import JudgeItem

REPO = "DenisPeskoff/2020_acl_diplomacy"
FILES = {"train": "train.jsonl", "validation": "validation.jsonl", "test": "test.jsonl"}


def download(data_dir: str | Path) -> dict[str, Path]:
    raw = Path(data_dir) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    out = {}
    for split, name in FILES.items():
        p = raw / name
        if not p.exists():
            for branch in ("master", "main"):
                r = httpx.get(f"https://raw.githubusercontent.com/{REPO}/{branch}/data/{name}",
                              timeout=60, follow_redirects=True)
                if r.status_code == 200:
                    p.write_bytes(r.content)
                    break
            else:
                raise RuntimeError(f"could not download {name} from {REPO}")
        out[split] = p
    return out


def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return True if s == "true" else False if s == "false" else None


@dataclass
class Row:
    split: str
    game_id: int
    dialog: int
    rel_idx: int
    abs_idx: int
    speaker: str
    receiver: str
    text: str
    season: str
    year: int
    score: int
    score_delta: int
    truthful: bool
    receiver_trusts: bool | None   # None = NOANNOTATION


def load(path: str | Path, split: str) -> list[list[Row]]:
    """Dialogs as lists of rows, message order preserved."""
    dialogs = []
    for d_idx, line in enumerate(Path(path).read_text().splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        rows = []
        for i, text in enumerate(d["messages"]):
            truthful = _bool(d["sender_labels"][i])
            if truthful is None:
                continue
            rows.append(Row(
                split=split, game_id=int(d["game_id"]), dialog=d_idx, rel_idx=int(d["relative_message_index"][i]),
                abs_idx=int(d["absolute_message_index"][i]), speaker=d["speakers"][i].upper(),
                receiver=d["receivers"][i].upper(), text=text, season=d["seasons"][i], year=int(d["years"][i]),
                score=int(d["game_score"][i]), score_delta=int(d["game_score_delta"][i]),
                truthful=truthful, receiver_trusts=_bool(d["receiver_labels"][i])))
        dialogs.append(rows)
    return dialogs


def load_all(data_dir: str | Path) -> dict[str, list[list[Row]]]:
    paths = download(data_dir)
    splits = {s: load(p, s) for s, p in paths.items()}
    assert_no_game_overlap(splits)
    return splits


def assert_no_game_overlap(splits: dict[str, list[list[Row]]]) -> None:
    games = {s: {r.game_id for d in ds for r in d} for s, ds in splits.items()}
    names = list(games)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = games[a] & games[b]
            assert not both, f"game_id overlap between {a} and {b}: {sorted(both)}"


def stats(splits: dict[str, list[list[Row]]]) -> dict:
    out = {}
    for s, ds in splits.items():
        rows = [r for d in ds for r in d]
        annotated = [r for r in rows if r.receiver_trusts is not None]
        out[s] = {
            "dialogs": len(ds), "messages": len(rows),
            "games": sorted(Counter(r.game_id for r in rows)),
            "lies": sum(not r.truthful for r in rows),
            "lie_rate": sum(not r.truthful for r in rows) / max(1, len(rows)),
            "suspected_rate": sum(r.receiver_trusts is False for r in annotated) / max(1, len(annotated)),
            "noannotation": len(rows) - len(annotated),
            # How often humans caught actual lies (receiver distrusted a lie)
            "human_lie_recall": (sum(1 for r in annotated if not r.truthful and r.receiver_trusts is False)
                                 / max(1, sum(1 for r in annotated if not r.truthful))),
        }
    return out


def to_items(dialogs: list[list[Row]], window: int, task: str = "actual") -> list[JudgeItem]:
    """task="actual": label = sender's lie; task="suspected": label = receiver suspected a lie
    (NOANNOTATION rows dropped). History is strictly before the target message."""
    items = []
    for rows in dialogs:
        for k, r in enumerate(rows):
            if task == "suspected" and r.receiver_trusts is None:
                continue
            is_lie = (not r.truthful) if task == "actual" else (r.receiver_trusts is False)
            # sender's score is given; the receiver's is score - delta
            items.append(JudgeItem(
                item_id=f"h{r.game_id}:{r.split}:{r.dialog}:{r.rel_idx}", source="human",
                group=f"h{r.game_id}:{r.split}:{r.dialog}", receiver=r.receiver, sender=r.speaker,
                season=r.season, year=r.year, target=r.text, is_lie=is_lie,
                history=[(x.speaker, x.text) for x in rows[max(0, k - window):k]],
                board=None, sender_sc=r.score, receiver_sc=r.score - r.score_delta))
    return items


def all_texts(dialogs: list[list[Row]]) -> list[str]:
    return [r.text for d in dialogs for r in d]


def train_gate_items(splits: dict[str, list[list[Row]]], games: list[int], window: int,
                     truth_ratio: int, seed: int) -> list[JudgeItem]:
    """Gate items from human TRAIN games: every lie plus `truth_ratio` sampled truths per lie, weighted
    back to the games' true lie rate. Refuses games that are not in the train split."""
    from rsi.judge import balance

    train_games = {r.game_id for d in splits["train"] for r in d}
    held_out = {r.game_id for s in ("validation", "test") for d in splits.get(s, []) for r in d}
    bad = sorted(set(games) - train_games)
    if bad:
        raise ValueError(f"human gate games {bad} are not train games"
                         + (f" (held out: {sorted(set(bad) & held_out)})" if set(bad) & held_out else ""))
    dialogs = [d for d in splits["train"] if d and d[0].game_id in set(games)]
    return balance(to_items(dialogs, window), truth_ratio, seed)
