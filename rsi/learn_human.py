"""Learn a playbook from the human deception dataset, with the judge model (Opus) as the learner.

    python -m rsi.learn_human --config configs/live_one.yaml --out playbooks/human_v1.json

Data rules: the learner sees the TRAIN and VALIDATION splits (games 1-3, 5-11). The TEST split
(games 4, 12) stays fully held out: every learned heuristic is linted against an 8-gram index of
the test messages, and test is only ever scored once via `rsi.loop final-test`. Because validation is
used for learning, the human-validation transfer gate is optimistic for genomes descended from this
playbook (set loop.human_val_gate: false).

Map: batches of labelled examples -> Opus extracts general lessons (catch lies / avoid false alarms /
calibration). Reduce: one Opus pass merges all lessons + the v0 playbook into <= MAX heuristics.
No player names, game ids, phases or quotes may survive into the playbook (genome lint).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rsi import human_data
from rsi.config import load_config
from rsi.contamination import build_index
from rsi.genome import Genome, Heuristic, default_genome, lint_diff
from rsi.llm import LLMError, make_llm, register_mock
from rsi.schemas import PlaybookDiff

MAX_HEURISTICS = 20


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lesson(_Strict):
    text: str = Field(description="One general, imperative heuristic for judging a received message (<= 300 chars)")
    kind: Literal["catch_lies", "avoid_false_alarms", "calibration"]
    rationale: str = Field(description="Why the examples support it (no quotes, no player names)")
    evidence_ids: list[str] = Field(description="Example ids that support it")


class LessonBatch(_Strict):
    lessons: list[Lesson] = Field(max_length=6)


class DraftHeuristic(_Strict):
    text: str
    kind: Literal["catch_lies", "avoid_false_alarms", "calibration"]
    rationale: str
    support: int = Field(ge=0, description="How many batch lessons this merges")


class PlaybookDraft(_Strict):
    heuristics: list[DraftHeuristic] = Field(max_length=MAX_HEURISTICS)
    notes: str


LEARNER_SYSTEM = """You are an expert analyst of deception in the board game Diplomacy, distilling a TRUST-ASSESSMENT PLAYBOOK for AI players. The playbook is read by a player when it receives a message and must decide how likely the sender is lying (p_lie) and whether to trust it.

You are shown real human games with ground truth: each target message is labelled LIE or TRUTH by its sender, and the human RECEIVER's own judgment is given (suspected / trusted / unknown). Humans caught only about 10-20% of lies, so the receivers' judgments are often wrong: learn from their mistakes.

Write lessons that GENERALISE to new games, players and phases:
- imperative, concrete, checkable heuristics ("When ..., raise/lower p_lie ...")
- about signals visible to a receiver: wording, commitment specificity, timing, position and supply-centre balance, consistency with earlier messages, what the sender asks you to do
- include lessons that AVOID FALSE ALARMS (most messages are truthful; the base rate of lies is roughly 4-9%) and lessons about CALIBRATION
- never quote message text, never name players, countries as individuals, games or phases; no example ids in the lesson text (put them in evidence_ids)
Answer with one JSON object matching the schema."""

CONSOLIDATE_SYSTEM = """You maintain a trust-assessment playbook for AI Diplomacy players. Merge the candidate lessons (learned from labelled human games) and the current playbook into ONE playbook of at most 20 heuristics:
- deduplicate and merge overlapping lessons; prefer lessons with more support
- keep a balance: lessons that catch lies, lessons that avoid false alarms, and 1-3 on calibration (lies are rare: keep p_lie low without concrete evidence)
- each heuristic: one imperative sentence or two, <= 300 characters, concrete and checkable, general (no quotes, names, game ids or phases)
- order them from most to least important
Answer with one JSON object matching the schema."""


@register_mock(LessonBatch)
def _mock_lessons(rng, ctx):
    return {"lessons": [{"text": rng.choice([
        "When a sender suddenly offers an unusually generous deal after silence, raise p_lie moderately.",
        "Specific, checkable commitments that cost the sender something are usually honest; keep p_lie low.",
        "Lies are rare; without concrete evidence keep p_lie below 0.2."]), "kind": rng.choice(
        ["catch_lies", "avoid_false_alarms", "calibration"]), "rationale": "mock", "evidence_ids": ["b0e1"]}]}


@register_mock(PlaybookDraft)
def _mock_draft(rng, ctx):
    return {"heuristics": [{"text": "Lies are rare; without concrete evidence keep p_lie below 0.2.",
                            "kind": "calibration", "rationale": "mock", "support": 3},
                           {"text": "In game 4 Italy lied about Venice, so distrust Italy.",   # must be linted out
                            "kind": "catch_lies", "rationale": "mock", "support": 1}], "notes": "mock"}


def _example_cards(dialogs, rng: random.Random, n_lies: int, n_truth: int, n_false_alarm: int,
                   window: int = 4) -> list[dict]:
    lies, truths, false_alarms = [], [], []
    for d in dialogs:
        for k, r in enumerate(d):
            ctx = d[max(0, k - window):k]
            card = {"r": r, "ctx": ctx}
            if not r.truthful:
                lies.append(card)
            elif r.receiver_trusts is False:
                false_alarms.append(card)
            else:
                truths.append(card)
    pick = (rng.sample(lies, min(n_lies, len(lies))) + rng.sample(truths, min(n_truth, len(truths)))
            + rng.sample(false_alarms, min(n_false_alarm, len(false_alarms))))
    rng.shuffle(pick)
    return pick


def _render(card: dict, eid: str) -> str:
    r = card["r"]
    recv = {True: "TRUSTED it", False: "SUSPECTED a lie", None: "unknown"}[r.receiver_trusts]
    lines = [f"[{eid}] {r.season} {r.year} · sender SC {r.score}, receiver SC {r.score - r.score_delta} · "
             f"label: {'LIE' if not r.truthful else 'TRUTH'} · human receiver {recv}"]
    for x in card["ctx"]:
        lines.append(f"   earlier, {'sender' if x.speaker == r.speaker else 'receiver'}: {x.text[:400]}")
    lines.append(f"   TARGET (sender -> receiver): {r.text[:700]}")
    return "\n".join(lines)


async def learn(cfg_path: str | None, out: Path, batches: int, per_batch: int, seed: int) -> Genome:
    cfg = load_config(cfg_path)
    llm = make_llm(cfg.reviewer)            # the judge model is the learner
    splits = human_data.load_all(cfg.paths.data)
    pool = splits["train"] + splits["validation"]
    test_index = build_index(human_data.all_texts(splits["test"]))
    rng = random.Random(seed)

    n_lie = per_batch * 5 // 10
    n_fa = per_batch * 2 // 10
    batches_cards = [_example_cards(pool, rng, n_lie, per_batch - n_lie - n_fa, n_fa) for _ in range(batches)]
    print(f"[learn] {batches} batches x ~{per_batch} examples from train+validation "
          f"({sum(len(d) for d in pool)} messages); test held out", flush=True)

    sem = asyncio.Semaphore(6)

    async def one(b: int, cards: list[dict]) -> list[Lesson]:
        body = "\n\n".join(_render(c, f"b{b}e{i}") for i, c in enumerate(cards))
        async with sem:
            try:
                res, _ = await llm.complete_json(
                    messages=[{"role": "system", "content": LEARNER_SYSTEM},
                              {"role": "user", "content": f"## Labelled examples (batch {b})\n\n{body}"}],
                    schema=LessonBatch, role="review", seed=seed * 1000 + b, effort="high", temperature=0.4)
            except LLMError as e:
                print(f"[learn] batch {b} failed: {e}", flush=True)
                return []
        print(f"[learn] batch {b}: {len(res.lessons)} lessons", flush=True)
        return res.lessons

    lessons = [lesson for batch in await asyncio.gather(*(one(b, c) for b, c in enumerate(batches_cards)))
               for lesson in batch]
    v0 = default_genome()
    listing = "\n".join(f"- ({l.kind}) {l.text}  [support ids: {len(l.evidence_ids)}]" for l in lessons)
    draft, _ = await llm.complete_json(
        messages=[{"role": "system", "content": CONSOLIDATE_SYSTEM},
                  {"role": "user", "content": f"## Current playbook\n{v0.render_playbook()}\n\n"
                                              f"## Candidate lessons ({len(lessons)})\n{listing}"}],
        schema=PlaybookDraft, role="review", seed=seed * 1000 + 999, effort="high", temperature=0.2)

    kept, rejected = [], []
    probe = Genome(name="probe", system_prompt=v0.system_prompt, playbook=[])
    for h in draft.heuristics:
        errs = lint_diff(PlaybookDiff(op="add", text=h.text, rationale=h.rationale, evidence_msg_ids=[]),
                         probe, test_index)
        (rejected if errs else kept).append((h, errs))
        if not errs:
            probe.playbook.append(Heuristic(id=f"H{len(probe.playbook) + 1}", text=h.text))
    genome = Genome(
        name="human-v1", parent=v0.hash, generation=0, system_prompt=v0.system_prompt,
        playbook=[Heuristic(id=f"H{i + 1}", text=h.text, rationale=f"[{h.kind}, support {h.support}] {h.rationale}",
                            evidence=["human:train+validation"]) for i, (h, _) in enumerate(kept)],
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(genome.model_dump_json(indent=2))
    meta = {"source": "Peskov et al. 2020 train+validation (games 1-3, 5-11); test (4, 12) held out",
            "learner": cfg.reviewer.model, "batches": batches, "per_batch": per_batch, "seed": seed,
            "lessons": [l.model_dump() for l in lessons], "consolidation_notes": draft.notes,
            "rejected_by_lint": [{"text": h.text, "errors": e} for h, e in rejected], "hash": genome.hash}
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
    md = [f"# Playbook `{genome.name}` ({genome.hash})", "",
          f"Learned by **{cfg.reviewer.model}** from labelled human Diplomacy games "
          "(Peskov et al. 2020, train + validation splits). Test split held out and linted against.", "",
          f"{len(lessons)} lessons from {batches} batches were merged into {len(kept)} heuristics "
          f"({len(rejected)} dropped by lint).", ""]
    for kind, title in (("catch_lies", "Catching lies"), ("avoid_false_alarms", "Avoiding false alarms"),
                        ("calibration", "Calibration")):
        rows = [(i, h) for i, (h, _) in enumerate(kept) if h.kind == kind]
        if rows:
            md.append(f"## {title}")
            md += [f"{i + 1}. {h.text}  \n   _support {h.support}: {h.rationale}_" for i, h in rows]
            md.append("")
    md += ["## Consolidation notes", draft.notes, ""]
    out.with_suffix(".md").write_text("\n".join(md))
    print(f"[learn] wrote {out} ({len(kept)} heuristics, {len(rejected)} rejected by lint), hash {genome.hash}",
          flush=True)
    await llm.aclose()
    return genome


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.learn_human")
    ap.add_argument("--config", default="configs/live_one.yaml")
    ap.add_argument("--out", default="playbooks/human_v1.json")
    ap.add_argument("--batches", type=int, default=14)
    ap.add_argument("--per-batch", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    asyncio.run(learn(a.config, Path(a.out), a.batches, a.per_batch, a.seed))


if __name__ == "__main__":
    main()
