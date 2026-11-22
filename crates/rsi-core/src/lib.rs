//! `rsi_core`: Rust hot paths for the Diplomacy RSI harness.
//!
//! Python API (see `rsi/core.py` for the pure-Python fallback with identical semantics):
//! - `metric(name, y, p, w=None, thr=0.5, bins=10) -> float`
//! - `best_f1_threshold(y, p, w=None) -> (thr, f1)`
//! - `paired_bootstrap(name, y, p_a, p_b, w=None, groups=None, thr_a=0.5, thr_b=0.5, bins=10, n_boot=1000, seed=0) -> dict`
//! - `NgramIndex(n)`: `.add(doc_id, text)`, `.query(text) -> (hits, total, doc|None)`, `.query_many(texts) -> list[float]`
//! - `LeakGuard(min_len)`: `.add(pattern, tag) -> bool`, `.scan(text) -> list[str]`

pub mod leak;
pub mod metrics;
pub mod ngram;
pub mod rng;

#[cfg(feature = "python")]
mod py {
    use super::{leak, metrics, ngram};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;

    fn weights(w: Option<Vec<f64>>, n: usize) -> PyResult<Vec<f64>> {
        match w {
            Some(w) if w.len() != n => Err(PyValueError::new_err("w length mismatch")),
            Some(w) => Ok(w),
            None => Ok(vec![1.0; n]),
        }
    }

    fn parse_metric(name: &str) -> PyResult<metrics::Metric> {
        metrics::Metric::parse(name).ok_or_else(|| PyValueError::new_err(format!("unknown metric {name}")))
    }

    fn check(y: &[bool], p: &[f64]) -> PyResult<()> {
        if y.len() != p.len() {
            return Err(PyValueError::new_err("y and p length mismatch"));
        }
        Ok(())
    }

    #[pyfunction]
    #[pyo3(signature = (name, y, p, w=None, thr=0.5, bins=10))]
    fn metric(name: &str, y: Vec<bool>, p: Vec<f64>, w: Option<Vec<f64>>, thr: f64, bins: usize) -> PyResult<f64> {
        check(&y, &p)?;
        let w = weights(w, y.len())?;
        let d = metrics::Data { y: &y, p: &p, w: &w };
        Ok(metrics::eval_idx(parse_metric(name)?, d, &metrics::all_idx(y.len()), thr, bins))
    }

    #[pyfunction]
    #[pyo3(signature = (y, p, w=None))]
    fn best_f1_threshold(y: Vec<bool>, p: Vec<f64>, w: Option<Vec<f64>>) -> PyResult<(f64, f64)> {
        check(&y, &p)?;
        let w = weights(w, y.len())?;
        Ok(metrics::best_f1_threshold(metrics::Data { y: &y, p: &p, w: &w }))
    }

    #[pyfunction]
    #[pyo3(signature = (name, y, p_a, p_b, w=None, groups=None, thr_a=0.5, thr_b=0.5, bins=10, n_boot=1000, seed=0))]
    #[allow(clippy::too_many_arguments)]
    fn paired_bootstrap<'py>(
        py: Python<'py>,
        name: &str,
        y: Vec<bool>,
        p_a: Vec<f64>,
        p_b: Vec<f64>,
        w: Option<Vec<f64>>,
        groups: Option<Vec<u64>>,
        thr_a: f64,
        thr_b: f64,
        bins: usize,
        n_boot: usize,
        seed: u64,
    ) -> PyResult<Bound<'py, PyDict>> {
        check(&y, &p_a)?;
        check(&y, &p_b)?;
        if let Some(g) = &groups {
            if g.len() != y.len() {
                return Err(PyValueError::new_err("groups length mismatch"));
            }
        }
        let m = parse_metric(name)?;
        let w = weights(w, y.len())?;
        let r = py.detach(|| {
            metrics::paired_bootstrap(m, &y, &p_a, &p_b, &w, groups.as_deref(), thr_a, thr_b, bins, n_boot, seed)
        });
        let d = PyDict::new(py);
        d.set_item("metric", name)?;
        d.set_item("delta", r.delta)?;
        d.set_item("mean", r.mean)?;
        d.set_item("lo", r.lo)?;
        d.set_item("hi", r.hi)?;
        d.set_item("p_gain", r.p_gain)?;
        d.set_item("n_valid", r.n_valid)?;
        d.set_item("higher_is_better", m.higher_is_better())?;
        Ok(d)
    }

    #[pyclass(name = "NgramIndex", module = "rsi_core")]
    struct PyNgramIndex {
        inner: ngram::NgramIndex,
    }

    #[pymethods]
    impl PyNgramIndex {
        #[new]
        #[pyo3(signature = (n=8))]
        fn new(n: usize) -> Self {
            PyNgramIndex { inner: ngram::NgramIndex::new(n) }
        }

        fn add(&mut self, doc_id: u32, text: &str) {
            self.inner.add(doc_id, text)
        }

        fn add_many(&mut self, py: Python<'_>, docs: Vec<(u32, String)>) {
            let inner = &mut self.inner;
            py.detach(|| {
                for (id, t) in &docs {
                    inner.add(*id, t);
                }
            })
        }

        fn query(&self, text: &str) -> (usize, usize, Option<u32>) {
            let o = self.inner.query(text);
            (o.hits, o.total, o.doc)
        }

        /// Overlap fraction per text, computed in parallel.
        fn query_many(&self, py: Python<'_>, texts: Vec<String>) -> Vec<(usize, usize, Option<u32>)> {
            let inner = &self.inner;
            py.detach(|| inner.query_many(&texts).into_iter().map(|o| (o.hits, o.total, o.doc)).collect())
        }

        fn __len__(&self) -> usize {
            self.inner.len()
        }

        #[getter]
        fn n(&self) -> usize {
            self.inner.n
        }
    }

    #[pyclass(name = "LeakGuard", module = "rsi_core")]
    struct PyLeakGuard {
        inner: leak::LeakGuard,
    }

    #[pymethods]
    impl PyLeakGuard {
        #[new]
        #[pyo3(signature = (min_len=24))]
        fn new(min_len: usize) -> Self {
            PyLeakGuard { inner: leak::LeakGuard::new(min_len) }
        }

        fn add(&mut self, pattern: &str, tag: &str) -> bool {
            self.inner.add(pattern, tag)
        }

        fn scan(&mut self, text: &str) -> Vec<String> {
            self.inner.scan(text)
        }

        fn __len__(&self) -> usize {
            self.inner.len()
        }
    }

    #[pymodule]
    fn rsi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(metric, m)?)?;
        m.add_function(wrap_pyfunction!(best_f1_threshold, m)?)?;
        m.add_function(wrap_pyfunction!(paired_bootstrap, m)?)?;
        m.add_class::<PyNgramIndex>()?;
        m.add_class::<PyLeakGuard>()?;
        Ok(())
    }
}
