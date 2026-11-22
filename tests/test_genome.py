from rsi.contamination import build_index
from rsi.evolve import FrontEntry, aggregate_diffs, pareto_front
from rsi.genome import default_genome, lint_diff
from rsi.schemas import GameReview, PlaybookDiff


def _diff(**kw):
    base = {"op": "add", "text": "Raise p_lie when a promise only benefits the sender.", "rationale": "r",
            "evidence_msg_ids": [1]}
    return PlaybookDiff(**(base | kw))


def test_hash_ignores_lineage_and_changes_with_behaviour():
    g = default_genome()
    h = g.hash
    g2 = g.model_copy(deep=True)
    g2.name, g2.parent = "other", "x"
    assert g2.hash == h
    g2.threshold = 0.4
    assert g2.hash != h


def test_apply_add_edit_delete():
    g = default_genome()
    child, notes = g.apply([_diff(), _diff(op="edit", heuristic_id="H1", text="Edited heuristic text here."),
                            _diff(op="delete", heuristic_id="H2", text=None),
                            _diff(op="delete", heuristic_id="H99", text=None)], name="c")
    assert child.parent == g.hash and child.generation == 1
    assert any(h.text.startswith("Raise p_lie") for h in child.playbook)
    assert child.playbook[0].text == "Edited heuristic text here."
    assert all(h.id != "H2" for h in child.playbook)
    assert notes == ["delete skipped: unknown id H99"]
    assert len(g.playbook) == 6  # parent untouched


def test_lint_rejects_game_specifics_and_contamination():
    g = default_genome()
    assert lint_diff(_diff(), g) == []
    assert lint_diff(_diff(text="In game 12 France lied about Munich."), g)
    assert lint_diff(_diff(text="Message #45 was a lie, watch for that."), g)
    assert lint_diff(_diff(text="Distrust anything said in F1903M by anyone."), g)
    ix = build_index(["i swear on my honour that my fleet will stay in the north sea all year"])
    assert lint_diff(_diff(text="If someone says my fleet will stay in the north sea all year, distrust it."), g, ix)


def test_aggregate_votes_and_pareto():
    g = default_genome()
    d = _diff()
    reviews = {"a": GameReview(grades=[], diffs=[d], summary=""),
               "b": GameReview(grades=[], diffs=[d, _diff(text="game 3 trick")], summary="")}
    ranked, rejected = aggregate_diffs(reviews, g)
    assert ranked[0].votes == 2 and len(rejected) == 1
    a, b, c = (FrontEntry(g, 0.5, 0.1), FrontEntry(g, 0.6, 0.2), FrontEntry(g, 0.4, 0.3))
    assert pareto_front([a, b, c]) == [a, b]
