//! Weighted binary-classification metrics for lie detection (positive class = lie).
//!
//! Definitions mirror scikit-learn so the Python fallback and sklearn can be used as oracles:
//! - `average_precision` == `sklearn.metrics.average_precision_score` (step-wise, no interpolation)
//! - `roc_auc` == `sklearn.metrics.roc_auc_score` (trapezoid over distinct thresholds)
//! Sample weights let a class-balanced eval subsample be scored at the natural base rate.

use rayon::prelude::*;
use std::cmp::Ordering;

use crate::rng::SplitMix64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Metric {
    Auprc,
    Auroc,
    LieF1,
    MacroF1,
    Brier,
    LogLoss,
    Ece,
}

impl Metric {
    pub fn parse(s: &str) -> Option<Metric> {
        Some(match s.to_ascii_lowercase().as_str() {
            "auprc" | "ap" | "average_precision" => Metric::Auprc,
            "auroc" | "roc_auc" => Metric::Auroc,
            "lie_f1" | "f1" => Metric::LieF1,
            "macro_f1" => Metric::MacroF1,
            "brier" => Metric::Brier,
            "log_loss" | "nll" => Metric::LogLoss,
            "ece" => Metric::Ece,
            _ => return None,
        })
    }

    pub fn higher_is_better(self) -> bool {
        matches!(self, Metric::Auprc | Metric::Auroc | Metric::LieF1 | Metric::MacroF1)
    }
}

/// Borrowed view over one eval set. `y[i]` is true when the message is a lie.
#[derive(Clone, Copy)]
pub struct Data<'a> {
    pub y: &'a [bool],
    pub p: &'a [f64],
    pub w: &'a [f64],
}

fn order_desc(p: &[f64], idx: &[usize]) -> Vec<usize> {
    let mut o = idx.to_vec();
    o.sort_unstable_by(|&a, &b| p[b].partial_cmp(&p[a]).unwrap_or(Ordering::Equal));
    o
}

/// Average precision over the rows in `idx` (a bootstrap resample may repeat rows).
pub fn average_precision_idx(d: Data, idx: &[usize]) -> f64 {
    let order = order_desc(d.p, idx);
    let total_pos: f64 = idx.iter().filter(|&&i| d.y[i]).map(|&i| d.w[i]).sum();
    if total_pos <= 0.0 {
        return f64::NAN;
    }
    let (mut tp, mut fp, mut ap, mut prev_recall) = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let mut k = 0;
    while k < order.len() {
        let thr = d.p[order[k]];
        while k < order.len() && d.p[order[k]] == thr {
            let i = order[k];
            if d.y[i] {
                tp += d.w[i];
            } else {
                fp += d.w[i];
            }
            k += 1;
        }
        let recall = tp / total_pos;
        let precision = if tp + fp > 0.0 { tp / (tp + fp) } else { 1.0 };
        ap += (recall - prev_recall) * precision;
        prev_recall = recall;
    }
    ap
}

pub fn roc_auc_idx(d: Data, idx: &[usize]) -> f64 {
    let order = order_desc(d.p, idx);
    let pos: f64 = idx.iter().filter(|&&i| d.y[i]).map(|&i| d.w[i]).sum();
    let neg: f64 = idx.iter().filter(|&&i| !d.y[i]).map(|&i| d.w[i]).sum();
    if pos <= 0.0 || neg <= 0.0 {
        return f64::NAN;
    }
    let (mut tp, mut fp, mut auc, mut prev_tpr, mut prev_fpr) = (0.0, 0.0, 0.0, 0.0, 0.0);
    let mut k = 0;
    while k < order.len() {
        let thr = d.p[order[k]];
        while k < order.len() && d.p[order[k]] == thr {
            let i = order[k];
            if d.y[i] {
                tp += d.w[i];
            } else {
                fp += d.w[i];
            }
            k += 1;
        }
        let (tpr, fpr) = (tp / pos, fp / neg);
        auc += (fpr - prev_fpr) * (tpr + prev_tpr) / 2.0;
        prev_tpr = tpr;
        prev_fpr = fpr;
    }
    auc
}

/// (tp, fp, fn, tn) at `p >= thr`.
fn confusion_idx(d: Data, idx: &[usize], thr: f64) -> (f64, f64, f64, f64) {
    let (mut tp, mut fp, mut fnn, mut tn) = (0.0, 0.0, 0.0, 0.0);
    for &i in idx {
        let pred = d.p[i] >= thr;
        match (pred, d.y[i]) {
            (true, true) => tp += d.w[i],
            (true, false) => fp += d.w[i],
            (false, true) => fnn += d.w[i],
            (false, false) => tn += d.w[i],
        }
    }
    (tp, fp, fnn, tn)
}

fn f1(tp: f64, fp: f64, fnn: f64) -> f64 {
    let denom = 2.0 * tp + fp + fnn;
    if denom > 0.0 {
        2.0 * tp / denom
    } else {
        0.0
    }
}

pub fn lie_f1_idx(d: Data, idx: &[usize], thr: f64) -> f64 {
    let (tp, fp, fnn, _) = confusion_idx(d, idx, thr);
    f1(tp, fp, fnn)
}

pub fn macro_f1_idx(d: Data, idx: &[usize], thr: f64) -> f64 {
    let (tp, fp, fnn, tn) = confusion_idx(d, idx, thr);
    // Truth class: "positive" is a predicted-truthful, actually-truthful message.
    (f1(tp, fp, fnn) + f1(tn, fnn, fp)) / 2.0
}

pub fn brier_idx(d: Data, idx: &[usize]) -> f64 {
    let (mut s, mut w) = (0.0, 0.0);
    for &i in idx {
        let y = if d.y[i] { 1.0 } else { 0.0 };
        s += d.w[i] * (d.p[i] - y).powi(2);
        w += d.w[i];
    }
    if w > 0.0 {
        s / w
    } else {
        f64::NAN
    }
}

pub fn log_loss_idx(d: Data, idx: &[usize]) -> f64 {
    const EPS: f64 = 1e-15;
    let (mut s, mut w) = (0.0, 0.0);
    for &i in idx {
        let p = d.p[i].clamp(EPS, 1.0 - EPS);
        s -= d.w[i] * if d.y[i] { p.ln() } else { (1.0 - p).ln() };
        w += d.w[i];
    }
    if w > 0.0 {
        s / w
    } else {
        f64::NAN
    }
}

/// Expected calibration error with `bins` equal-width bins on [0, 1].
pub fn ece_idx(d: Data, idx: &[usize], bins: usize) -> f64 {
    let bins = bins.max(1);
    let mut wsum = vec![0.0; bins];
    let mut ysum = vec![0.0; bins];
    let mut psum = vec![0.0; bins];
    let mut total = 0.0;
    for &i in idx {
        let b = ((d.p[i].clamp(0.0, 1.0) * bins as f64) as usize).min(bins - 1);
        wsum[b] += d.w[i];
        ysum[b] += d.w[i] * if d.y[i] { 1.0 } else { 0.0 };
        psum[b] += d.w[i] * d.p[i];
        total += d.w[i];
    }
    if total <= 0.0 {
        return f64::NAN;
    }
    (0..bins)
        .filter(|&b| wsum[b] > 0.0)
        .map(|b| (wsum[b] / total) * (ysum[b] / wsum[b] - psum[b] / wsum[b]).abs())
        .sum()
}

pub fn eval_idx(m: Metric, d: Data, idx: &[usize], thr: f64, bins: usize) -> f64 {
    match m {
        Metric::Auprc => average_precision_idx(d, idx),
        Metric::Auroc => roc_auc_idx(d, idx),
        Metric::LieF1 => lie_f1_idx(d, idx, thr),
        Metric::MacroF1 => macro_f1_idx(d, idx, thr),
        Metric::Brier => brier_idx(d, idx),
        Metric::LogLoss => log_loss_idx(d, idx),
        Metric::Ece => ece_idx(d, idx, bins),
    }
}

pub fn all_idx(n: usize) -> Vec<usize> {
    (0..n).collect()
}

/// Threshold that maximises lie-class F1 (scan over distinct predicted probabilities).
pub fn best_f1_threshold(d: Data) -> (f64, f64) {
    let idx = all_idx(d.y.len());
    let mut cands: Vec<f64> = d.p.to_vec();
    cands.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    cands.dedup();
    let mut best = (0.5, f64::NEG_INFINITY);
    for t in cands {
        let f = lie_f1_idx(d, &idx, t);
        if f > best.1 {
            best = (t, f);
        }
    }
    best
}

#[derive(Debug, Clone)]
pub struct BootResult {
    /// metric(b) - metric(a) on the full data
    pub delta: f64,
    pub mean: f64,
    pub lo: f64,
    pub hi: f64,
    /// fraction of valid resamples where b improves on a (direction-aware)
    pub p_gain: f64,
    pub n_valid: usize,
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let pos = q * (sorted.len() - 1) as f64;
    let (lo, hi) = (pos.floor() as usize, pos.ceil() as usize);
    sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo as f64)
}

/// Paired (optionally cluster) bootstrap of metric(b) - metric(a) on the same items.
/// `groups` (e.g. dialog or game ids) resamples whole clusters: messages in one dialog are correlated.
#[allow(clippy::too_many_arguments)]
pub fn paired_bootstrap(
    m: Metric,
    y: &[bool],
    p_a: &[f64],
    p_b: &[f64],
    w: &[f64],
    groups: Option<&[u64]>,
    thr_a: f64,
    thr_b: f64,
    bins: usize,
    n_boot: usize,
    seed: u64,
) -> BootResult {
    let n = y.len();
    let da = Data { y, p: p_a, w };
    let db = Data { y, p: p_b, w };
    let full = all_idx(n);
    let delta = eval_idx(m, db, &full, thr_b, bins) - eval_idx(m, da, &full, thr_a, bins);

    // Cluster membership lists (singletons when no groups are given).
    let clusters: Vec<Vec<usize>> = match groups {
        Some(g) => {
            let mut keys: Vec<u64> = g.to_vec();
            keys.sort_unstable();
            keys.dedup();
            let mut members = vec![Vec::new(); keys.len()];
            for (i, gi) in g.iter().enumerate() {
                let k = keys.binary_search(gi).expect("group key present");
                members[k].push(i);
            }
            members
        }
        None => (0..n).map(|i| vec![i]).collect(),
    };
    let nc = clusters.len();

    let mut deltas: Vec<f64> = (0..n_boot)
        .into_par_iter()
        .filter_map(|b| {
            let mut rng = SplitMix64::new(seed ^ (b as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15));
            let mut idx = Vec::with_capacity(n + 8);
            for _ in 0..nc {
                idx.extend_from_slice(&clusters[rng.below(nc as u64) as usize]);
            }
            let va = eval_idx(m, da, &idx, thr_a, bins);
            let vb = eval_idx(m, db, &idx, thr_b, bins);
            let d = vb - va;
            d.is_finite().then_some(d)
        })
        .collect();
    deltas.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let n_valid = deltas.len();
    let gains = deltas
        .iter()
        .filter(|&&d| if m.higher_is_better() { d > 0.0 } else { d < 0.0 })
        .count();
    BootResult {
        delta,
        mean: if n_valid > 0 { deltas.iter().sum::<f64>() / n_valid as f64 } else { f64::NAN },
        lo: percentile(&deltas, 0.025),
        hi: percentile(&deltas, 0.975),
        p_gain: if n_valid > 0 { gains as f64 / n_valid as f64 } else { f64::NAN },
        n_valid,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d<'a>(y: &'a [bool], p: &'a [f64], w: &'a [f64]) -> Data<'a> {
        Data { y, p, w }
    }

    #[test]
    fn ap_matches_sklearn_example() {
        // sklearn docs: average_precision_score([0,0,1,1],[0.1,0.4,0.35,0.8]) == 0.8333...
        let y = [false, false, true, true];
        let p = [0.1, 0.4, 0.35, 0.8];
        let w = [1.0; 4];
        let ap = average_precision_idx(d(&y, &p, &w), &all_idx(4));
        assert!((ap - 0.833_333_333).abs() < 1e-6, "{ap}");
    }

    #[test]
    fn auc_matches_sklearn_example() {
        // roc_auc_score([0,0,1,1],[0.1,0.4,0.35,0.8]) == 0.75
        let y = [false, false, true, true];
        let p = [0.1, 0.4, 0.35, 0.8];
        let w = [1.0; 4];
        let auc = roc_auc_idx(d(&y, &p, &w), &all_idx(4));
        assert!((auc - 0.75).abs() < 1e-9, "{auc}");
    }

    #[test]
    fn weights_equal_duplication() {
        let y = [true, false, false];
        let p = [0.7, 0.6, 0.2];
        let w = [1.0, 2.0, 1.0];
        let y2 = [true, false, false, false];
        let p2 = [0.7, 0.6, 0.6, 0.2];
        let w2 = [1.0; 4];
        let a = brier_idx(d(&y, &p, &w), &all_idx(3));
        let b = brier_idx(d(&y2, &p2, &w2), &all_idx(4));
        assert!((a - b).abs() < 1e-12);
        let a = average_precision_idx(d(&y, &p, &w), &all_idx(3));
        let b = average_precision_idx(d(&y2, &p2, &w2), &all_idx(4));
        assert!((a - b).abs() < 1e-12);
    }

    #[test]
    fn bootstrap_identical_is_null() {
        let y: Vec<bool> = (0..200).map(|i| i % 7 == 0).collect();
        let p: Vec<f64> = (0..200).map(|i| ((i * 37) % 100) as f64 / 100.0).collect();
        let w = vec![1.0; 200];
        let r = paired_bootstrap(Metric::Auprc, &y, &p, &p, &w, None, 0.5, 0.5, 10, 200, 7);
        assert_eq!(r.delta, 0.0);
        assert_eq!(r.p_gain, 0.0);
    }

    #[test]
    fn bootstrap_detects_improvement() {
        let y: Vec<bool> = (0..300).map(|i| i % 5 == 0).collect();
        let pa: Vec<f64> = (0..300).map(|i| ((i * 37) % 100) as f64 / 100.0).collect();
        let pb: Vec<f64> = y.iter().zip(&pa).map(|(&l, &p)| if l { 0.5 + p / 2.0 } else { p / 2.0 }).collect();
        let w = vec![1.0; 300];
        let groups: Vec<u64> = (0..300).map(|i| (i / 10) as u64).collect();
        let r = paired_bootstrap(Metric::Auprc, &y, &pa, &pb, &w, Some(&groups), 0.5, 0.5, 10, 300, 1);
        assert!(r.delta > 0.3 && r.p_gain > 0.99, "{r:?}");
    }
}
