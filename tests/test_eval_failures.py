import pytest

from rsi.genome import default_genome
from rsi.judge import EvalIncomplete, EvalStore, JudgeItem
from rsi.llm import LLMError, MockLLM


class FlakyLLM(MockLLM):
    """Fails every call whose seed is in `bad` (a provider outage for part of the items)."""

    def __init__(self, fail_all=False):
        super().__init__()
        self.fail_all = fail_all

    async def complete_json(self, **kw):
        if self.fail_all:
            raise LLMError("HTTP 403: budget exceeded")
        return await super().complete_json(**kw)


def _items(n=20):
    return [JudgeItem(item_id=f"x{i}", source="sim", group=f"g{i // 4}", receiver="FRANCE", sender="ITALY",
                      season="Spring", year=1901, target=f"message {i}", is_lie=i % 5 == 0, history=[])
            for i in range(n)]


async def test_outage_is_not_cached_or_scored(tmp_path):
    store, g = EvalStore(tmp_path), default_genome()
    with pytest.raises(EvalIncomplete):
        await store.get(g, "sim_gate", _items(), FlakyLLM(fail_all=True))
    assert not list(tmp_path.glob("*.jsonl")), "failed results must never be cached"
    rows = await store.get(g, "sim_gate", _items(), FlakyLLM())
    assert all(r.ok for r in rows) and len(list(tmp_path.glob("*.jsonl"))) == 1
