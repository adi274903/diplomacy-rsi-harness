"""Metrics over judged items (positive class = lie). Thin layer over `rsi.core` (Rust or Python)."""

from __future__ import annotations

import math

from rsi import core
from rsi.judge import ItemResult, JudgeItem


def arrays(items: list[JudgeItem], results: list[ItemResult]):
    by_id = {r.item_id: r for r in results}
    y = [it.is_lie for it in items]
    p = [by_id[it.item_id].p_lie for it in items]
    w = [it.weight for it in items]
    groups = [hash_group(it.group) for it in items]
    ok = sum(by_id[it.item_id].ok for it in items)
    return y, p, w, groups, ok


def hash_group(g: str) -> int:
    import zlib

    return zlib.crc32(g.encode())


def evaluate(items: list[JudgeItem], results: list[ItemResult], thr: float = 0.5) -> dict:
    y, p, w, _, ok = arrays(items, results)
    n_lie = sum(y)
    out = {"n": len(y), "n_lie": n_lie, "ok_rate": ok / max(1, len(y)), "threshold": thr}
    if not y:
        return out
    for m in ("auprc", "auroc", "brier", "log_loss", "ece"):
        out[m] = core.metric(m, y, p, w)
    out["lie_f1"] = core.metric("lie_f1", y, p, w, thr)
    out["macro_f1"] = core.metric("macro_f1", y, p, w, thr)
    bt, bf = core.best_f1_threshold(y, p, w)
    out["best_threshold"], out["best_lie_f1"] = bt, bf
    W = sum(w)
    out["base_rate"] = sum(wi for yi, wi in zip(y, w) if yi) / W if W else math.nan
    return out


def compare(items: list[JudgeItem], res_a: list[ItemResult], res_b: list[ItemResult],
            metrics=("auprc", "ece"), n_boot: int = 1000, seed: int = 0) -> dict:
    """Paired cluster bootstrap (clusters = dialogs) of b - a on the same items."""
    y, pa, w, groups, _ = arrays(items, res_a)
    _, pb, _, _, _ = arrays(items, res_b)
    return {m: core.paired_bootstrap(m, y, pa, pb, w, groups, n_boot=n_boot, seed=seed) for m in metrics}


# ---- calibration (fit on human validation only) -------------------------------------------------

def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def apply_temperature(p: list[float], t: float) -> list[float]:
    return [1 / (1 + math.exp(-_logit(x) / t)) for x in p]


def fit_temperature(y: list[bool], p: list[float], w: list[float] | None = None) -> float:
    """Single temperature minimising weighted log loss; golden-section search over log T."""
    w = w or [1.0] * len(y)

    def loss(log_t: float) -> float:
        return core.metric("log_loss", y, apply_temperature(p, math.exp(log_t)), w)

    lo, hi = math.log(0.05), math.log(20.0)
    g = (math.sqrt(5) - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    for _ in range(60):
        if loss(a) < loss(b):
            hi = b
        else:
            lo = a
        a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    return math.exp((lo + hi) / 2)
