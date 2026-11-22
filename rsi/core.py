"""Hot paths: the Rust extension `rsi_core` if installed, otherwise a pure-Python fallback with the
same semantics (tests cross-check the two).

API:
    metric(name, y, p, w=None, thr=0.5, bins=10) -> float
    best_f1_threshold(y, p, w=None) -> (thr, f1)
    paired_bootstrap(name, y, p_a, p_b, w=None, groups=None, thr_a=.5, thr_b=.5, bins=10, n_boot=1000, seed=0) -> dict
    NgramIndex(n=8): add(doc_id, text), add_many([(id, text)]), query(text) -> (hits, total, doc), query_many(texts)
    LeakGuard(min_len=24): add(pattern, tag) -> bool, scan(text) -> list[str]
"""

from __future__ import annotations

import math
import re

try:  # pragma: no cover - exercised when the extension is built
    import rsi_core as _rs

    metric = _rs.metric
    best_f1_threshold = _rs.best_f1_threshold
    paired_bootstrap = _rs.paired_bootstrap
    NgramIndex = _rs.NgramIndex
    LeakGuard = _rs.LeakGuard
    BACKEND = "rust"
except ImportError:  # pure-Python fallback
    BACKEND = "python"


# ---------------------------------------------------------------------------------------------
# Pure-Python reference implementations (always importable for cross-checking)
# ---------------------------------------------------------------------------------------------

HIGHER_IS_BETTER = {"auprc": True, "auroc": True, "lie_f1": True, "macro_f1": True,
                    "brier": False, "log_loss": False, "ece": False}
_ALIASES = {"ap": "auprc", "average_precision": "auprc", "roc_auc": "auroc", "f1": "lie_f1", "nll": "log_loss"}


def _canon(name: str) -> str:
    n = name.lower()
    n = _ALIASES.get(n, n)
    if n not in HIGHER_IS_BETTER:
        raise ValueError(f"unknown metric {name}")
    return n


def _ap(y, p, w, idx):
    order = sorted(idx, key=lambda i: -p[i])
    total_pos = sum(w[i] for i in idx if y[i])
    if total_pos <= 0:
        return math.nan
    tp = fp = ap = prev_r = 0.0
    k = 0
    while k < len(order):
        thr = p[order[k]]
        while k < len(order) and p[order[k]] == thr:
            i = order[k]
            if y[i]:
                tp += w[i]
            else:
                fp += w[i]
            k += 1
        r = tp / total_pos
        prec = tp / (tp + fp) if tp + fp > 0 else 1.0
        ap += (r - prev_r) * prec
        prev_r = r
    return ap


def _auc(y, p, w, idx):
    order = sorted(idx, key=lambda i: -p[i])
    pos = sum(w[i] for i in idx if y[i])
    neg = sum(w[i] for i in idx if not y[i])
    if pos <= 0 or neg <= 0:
        return math.nan
    tp = fp = auc = ptpr = pfpr = 0.0
    k = 0
    while k < len(order):
        thr = p[order[k]]
        while k < len(order) and p[order[k]] == thr:
            i = order[k]
            if y[i]:
                tp += w[i]
            else:
                fp += w[i]
            k += 1
        tpr, fpr = tp / pos, fp / neg
        auc += (fpr - pfpr) * (tpr + ptpr) / 2
        ptpr, pfpr = tpr, fpr
    return auc


def _conf(y, p, w, idx, thr):
    tp = fp = fn = tn = 0.0
    for i in idx:
        pred = p[i] >= thr
        if pred and y[i]:
            tp += w[i]
        elif pred:
            fp += w[i]
        elif y[i]:
            fn += w[i]
        else:
            tn += w[i]
    return tp, fp, fn, tn


def _f1(tp, fp, fn):
    d = 2 * tp + fp + fn
    return 2 * tp / d if d > 0 else 0.0


def _eval(name, y, p, w, idx, thr, bins):
    if name == "auprc":
        return _ap(y, p, w, idx)
    if name == "auroc":
        return _auc(y, p, w, idx)
    if name in ("lie_f1", "macro_f1"):
        tp, fp, fn, tn = _conf(y, p, w, idx, thr)
        return _f1(tp, fp, fn) if name == "lie_f1" else (_f1(tp, fp, fn) + _f1(tn, fn, fp)) / 2
    W = sum(w[i] for i in idx)
    if W <= 0:
        return math.nan
    if name == "brier":
        return sum(w[i] * (p[i] - (1.0 if y[i] else 0.0)) ** 2 for i in idx) / W
    if name == "log_loss":
        eps = 1e-15
        s = 0.0
        for i in idx:
            q = min(max(p[i], eps), 1 - eps)
            s -= w[i] * (math.log(q) if y[i] else math.log(1 - q))
        return s / W
    # ece
    bins = max(1, bins)
    ws, ys, ps = [0.0] * bins, [0.0] * bins, [0.0] * bins
    for i in idx:
        b = min(int(min(max(p[i], 0.0), 1.0) * bins), bins - 1)
        ws[b] += w[i]
        ys[b] += w[i] * (1.0 if y[i] else 0.0)
        ps[b] += w[i] * p[i]
    return sum((ws[b] / W) * abs(ys[b] / ws[b] - ps[b] / ws[b]) for b in range(bins) if ws[b] > 0)


def py_metric(name, y, p, w=None, thr=0.5, bins=10):
    y, p = [bool(v) for v in y], [float(v) for v in p]
    w = [1.0] * len(y) if w is None else [float(v) for v in w]
    return _eval(_canon(name), y, p, w, list(range(len(y))), thr, bins)


def py_best_f1_threshold(y, p, w=None):
    y, p = [bool(v) for v in y], [float(v) for v in p]
    w = [1.0] * len(y) if w is None else list(w)
    idx = list(range(len(y)))
    best = (0.5, -math.inf)
    for t in sorted(set(p)):
        tp, fp, fn, _ = _conf(y, p, w, idx, t)
        f = _f1(tp, fp, fn)
        if f > best[1]:
            best = (t, f)
    return best


class _SplitMix:
    MASK = (1 << 64) - 1

    def __init__(self, seed):
        self.s = seed & self.MASK

    def next(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & self.MASK
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self.MASK
        return z ^ (z >> 31)

    def below(self, n):
        return (self.next() * n) >> 64


def py_paired_bootstrap(name, y, p_a, p_b, w=None, groups=None, thr_a=0.5, thr_b=0.5, bins=10,
                        n_boot=1000, seed=0):
    m = _canon(name)
    y = [bool(v) for v in y]
    p_a, p_b = [float(v) for v in p_a], [float(v) for v in p_b]
    w = [1.0] * len(y) if w is None else [float(v) for v in w]
    full = list(range(len(y)))
    delta = _eval(m, y, p_b, w, full, thr_b, bins) - _eval(m, y, p_a, w, full, thr_a, bins)
    if groups is None:
        clusters = [[i] for i in full]
    else:
        keys = sorted(set(groups))
        pos = {k: j for j, k in enumerate(keys)}
        clusters = [[] for _ in keys]
        for i, g in enumerate(groups):
            clusters[pos[g]].append(i)
    nc = len(clusters)
    deltas = []
    for b in range(n_boot):
        rng = _SplitMix(seed ^ ((b * 0x9E3779B97F4A7C15) & _SplitMix.MASK))
        idx = []
        for _ in range(nc):
            idx.extend(clusters[rng.below(nc)])
        d = _eval(m, y, p_b, w, idx, thr_b, bins) - _eval(m, y, p_a, w, idx, thr_a, bins)
        if math.isfinite(d):
            deltas.append(d)
    deltas.sort()

    def pct(q):
        if not deltas:
            return math.nan
        x = q * (len(deltas) - 1)
        lo, hi = math.floor(x), math.ceil(x)
        return deltas[lo] + (deltas[hi] - deltas[lo]) * (x - lo)

    hib = HIGHER_IS_BETTER[m]
    gains = sum(1 for d in deltas if (d > 0 if hib else d < 0))
    n = len(deltas)
    return {"metric": name, "delta": delta, "mean": sum(deltas) / n if n else math.nan,
            "lo": pct(0.025), "hi": pct(0.975), "p_gain": gains / n if n else math.nan,
            "n_valid": n, "higher_is_better": hib}


_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_FNV_OFFSET, _FNV_PRIME, _M64 = 0xCBF29CE484222325, 0x100000001B3, (1 << 64) - 1


def _words(text: str) -> list[str]:
    return _WORD.findall(text.replace("'", "").replace("’", "").lower())


def _shingles(text: str, n: int) -> list[int]:
    w = _words(text)
    out = []
    for k in range(len(w) - n + 1):
        h = _FNV_OFFSET
        for j, word in enumerate(w[k:k + n]):
            if j:
                h = ((h ^ 32) * _FNV_PRIME) & _M64
            for b in word.encode():
                h = ((h ^ b) * _FNV_PRIME) & _M64
        out.append(h)
    return out


class PyNgramIndex:
    def __init__(self, n: int = 8):
        self.n = max(1, n)
        self._map: dict[int, int] = {}

    def add(self, doc_id: int, text: str) -> None:
        for h in _shingles(text, self.n):
            self._map.setdefault(h, doc_id)

    def add_many(self, docs) -> None:
        for d, t in docs:
            self.add(d, t)

    def query(self, text: str):
        sh = _shingles(text, self.n)
        doc, hits = None, 0
        for h in sh:
            if h in self._map:
                hits += 1
                if doc is None:
                    doc = self._map[h]
        return hits, len(sh), doc

    def query_many(self, texts):
        return [self.query(t) for t in texts]

    def __len__(self):
        return len(self._map)


class PyLeakGuard:
    def __init__(self, min_len: int = 24):
        self.min_len = min_len
        self._pats: list[tuple[str, str]] = []

    def add(self, pattern: str, tag: str) -> bool:
        p = pattern.strip()
        if len(p) < self.min_len:
            return False
        self._pats.append((p.lower(), tag))
        return True

    def scan(self, text: str) -> list[str]:
        t = text.lower()
        out, seen = [], set()
        for p, tag in self._pats:
            if p in t and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out

    def __len__(self):
        return len(self._pats)


if BACKEND == "python":
    metric = py_metric
    best_f1_threshold = py_best_f1_threshold
    paired_bootstrap = py_paired_bootstrap
    NgramIndex = PyNgramIndex
    LeakGuard = PyLeakGuard
