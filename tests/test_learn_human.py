import json

from rsi.genome import Genome
from rsi.learn_human import learn


def _dialog(game_id, labels):
    n = len(labels)
    return {"messages": [f"message number {i} about the north sea and munich support in game {game_id}" for i in range(n)],
            "sender_labels": labels, "receiver_labels": [True] * n,
            "speakers": ["italy", "austria"] * (n // 2) + ["italy"] * (n % 2),
            "receivers": ["austria", "italy"] * (n // 2) + ["austria"] * (n % 2),
            "absolute_message_index": list(range(n)), "relative_message_index": list(range(n)),
            "seasons": ["Spring"] * n, "years": ["1901"] * n, "game_score": ["4"] * n,
            "game_score_delta": ["1"] * n, "players": ["italy", "austria"], "game_id": game_id}


async def test_learn_with_mock(tmp_path):
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for name, gid in (("train", 1), ("validation", 11), ("test", 4)):
        (raw / f"{name}.jsonl").write_text(json.dumps(_dialog(gid, [False, True, True, False, True, True])) + "\n")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"llm: {{backend: mock}}\npaths: {{data: {tmp_path / 'data'}}}\n")
    out = tmp_path / "pb" / "human.json"
    g = await learn(str(cfg), out, batches=2, per_batch=4, seed=1)
    assert out.exists() and out.with_suffix(".md").exists()
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert len(g.playbook) == 1 and "rare" in g.playbook[0].text           # game-specific one was linted out
    assert meta["rejected_by_lint"] and Genome.load(out).hash == g.hash
