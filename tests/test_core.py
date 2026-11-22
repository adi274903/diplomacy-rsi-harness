import random

import pytest

from rsi import core


def _data(n=300, seed=0):
    rng = random.Random(seed)
    y = [rng.random() < 0.1 for _ in range(n)]
    p = [min(1.0, max(0.0, (0.35 if yi else 0.1) + rng.gauss(0, 0.15))) for yi in y]
    w = [rng.choice([1.0, 1.0, 2.5]) for _ in range(n)]
    return y, p, w


def test_sklearn_doc_examples():
    y, p = [0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]
    assert core.py_metric("auprc", y, p) == pytest.approx(0.8333333, abs=1e-6)
    assert core.py_metric("auroc", y, p) == pytest.approx(0.75)


def test_python_matches_sklearn():
    sk = pytest.importorskip("sklearn.metrics")
    y, p, w = _data()
    assert core.py_metric("auprc", y, p, w) == pytest.approx(sk.average_precision_score(y, p, sample_weight=w))
    assert core.py_metric("auroc", y, p, w) == pytest.approx(sk.roc_auc_score(y, p, sample_weight=w))
    assert core.py_metric("brier", y, p, w) == pytest.approx(sk.brier_score_loss(y, p, sample_weight=w))
    pred = [pi >= 0.3 for pi in p]
    assert core.py_metric("lie_f1", y, p, w, thr=0.3) == pytest.approx(sk.f1_score(y, pred, sample_weight=w))
    assert core.py_metric("macro_f1", y, p, w, thr=0.3) == pytest.approx(
        sk.f1_score(y, pred, sample_weight=w, average="macro"))


@pytest.mark.skipif(core.BACKEND != "rust", reason="rsi_core extension not built")
def test_rust_matches_python():
    y, p, w = _data(seed=3)
    for m in ("auprc", "auroc", "brier", "log_loss", "ece", "lie_f1", "macro_f1"):
        assert core.metric(m, y, p, w, 0.3) == pytest.approx(core.py_metric(m, y, p, w, 0.3), rel=1e-9, abs=1e-12)
    groups = [i // 10 for i in range(len(y))]
    q = [min(1.0, x + 0.05) for x in p]
    r = core.paired_bootstrap("auprc", y, p, q, w, groups, n_boot=200, seed=5)
    pr = core.py_paired_bootstrap("auprc", y, p, q, w, groups, n_boot=200, seed=5)
    assert r["delta"] == pytest.approx(pr["delta"])
    assert r["p_gain"] == pytest.approx(pr["p_gain"])
    ix, pix = core.NgramIndex(5), core.PyNgramIndex(5)
    for idx in (ix, pix):
        idx.add(1, "We'll support your army into Munich this spring for sure")
    assert ix.query("we will support your army into munich this spring") == pix.query(
        "we will support your army into munich this spring")


def test_bootstrap_null_and_gain():
    y, p, w = _data(seed=1)
    r = core.paired_bootstrap("auprc", y, p, p, w, n_boot=100, seed=0)
    assert r["delta"] == 0 and r["p_gain"] == 0
    better = [min(1.0, pi + (0.3 if yi else 0.0)) for yi, pi in zip(y, p)]
    r = core.paired_bootstrap("auprc", y, p, better, w, [i // 5 for i in range(len(y))], n_boot=200, seed=0)
    assert r["delta"] > 0 and r["p_gain"] > 0.95


def test_leak_guard_and_ngram():
    g = core.LeakGuard(20)
    assert not g.add("short", "x")
    assert g.add("I will stab Germany next fall", "FRANCE:memory")
    assert g.scan("blah i will stab germany next fall blah") == ["FRANCE:memory"]
    ix = core.NgramIndex(4)
    ix.add(7, "keep the channel demilitarised please")
    hits, total, doc = ix.query("Keep the Channel demilitarised, please!")
    assert hits == total == 2 and doc == 7
