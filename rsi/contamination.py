"""Contamination controls.

1. Training-channel dedup: an n-gram index over held-out human data (val + test). Every playbook
   diff and exemplar is checked against it before it can enter a genome (see `genome.lint_diff`).
2. Pretraining memorisation probe: the human dataset has been public since 2020, so the API model
   may have seen it. We measure verbatim continuation of message prefixes.
3. Paraphrase check: score the human test set both as-is and paraphrased. A large gap suggests the
   original-text score is inflated by memorisation; report the paraphrased one as primary then.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from rsi.core import NgramIndex
from rsi.judge import JudgeItem


def build_index(texts: list[str], n: int = 8):
    ix = NgramIndex(n)
    ix.add_many([(i, t) for i, t in enumerate(texts)])
    return ix


def overlap_report(index, texts: list[str]) -> dict:
    hits = index.query_many(texts)
    flagged = [i for i, (h, _, _) in enumerate(hits) if h > 0]
    return {"checked": len(texts), "flagged": len(flagged), "flagged_idx": flagged[:50]}


async def memorization_probe(llm, texts: list[str], n: int = 200, seed: int = 0, ngram: int = 6) -> dict:
    """Give the first half of a (long enough) message; count continuations that reproduce any
    `ngram`-gram of the true second half."""
    rng = random.Random(seed)
    cands = [t for t in texts if len(t.split()) >= 24]
    sample = rng.sample(cands, min(n, len(cands)))
    sem = asyncio.Semaphore(64)

    async def one(i: int, t: str) -> bool:
        words = t.split()
        half = len(words) // 2
        prefix, rest = " ".join(words[:half]), " ".join(words[half:])
        async with sem:
            out, _ = await llm.complete_text(messages=[
                {"role": "system", "content": "Continue the text exactly. Output only the continuation."},
                {"role": "user", "content": prefix}], seed=seed + i, temperature=0.0)
        ix = NgramIndex(ngram)
        ix.add(0, rest)
        return ix.query(out)[0] > 0

    flags = await asyncio.gather(*(one(i, t) for i, t in enumerate(sample)))
    return {"probed": len(sample), "verbatim_hits": sum(flags), "rate": sum(flags) / max(1, len(sample))}


async def paraphrase_items(llm, items: list[JudgeItem], cache_path: str | Path, seed: int = 0) -> list[JudgeItem]:
    """Paraphrase each target message (meaning-preserving). Cached so the paraphrased test set is
    fixed once created."""
    cache_path = Path(cache_path)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    sem = asyncio.Semaphore(64)

    async def one(it: JudgeItem) -> None:
        if it.item_id in cache:
            return
        async with sem:
            out, _ = await llm.complete_text(messages=[
                {"role": "system", "content": "Paraphrase the message, keeping its meaning, commitments, tone and "
                                              "length. Output only the paraphrase."},
                {"role": "user", "content": it.target}], seed=seed, temperature=0.0)
        cache[it.item_id] = out.strip() or it.target

    await asyncio.gather(*(one(it) for it in items))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache))
    out = []
    for it in items:
        d = dict(vars(it))
        d["target"] = cache[it.item_id]
        d["item_id"] = it.item_id + ":para"
        out.append(JudgeItem(**d))
    return out
