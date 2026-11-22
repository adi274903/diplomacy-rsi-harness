//! Counters and latency histograms, exported as JSON (`/stats`) and Prometheus text (`/metrics`).

use hdrhistogram::Histogram;
use std::collections::HashMap;
use std::fmt::Write as _;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::Mutex;
use std::time::Duration;

use crate::admission::LEVELS;

pub struct Stats {
    pub requests: AtomicU64,
    pub cache_hits: AtomicU64,
    pub upstream_errors: AtomicU64,
    pub retries: AtomicU64,
    pub budget_rejects: AtomicU64,
    pub prompt_tokens: AtomicU64,
    pub cached_prompt_tokens: AtomicU64,
    pub completion_tokens: AtomicU64,
    latency_ms: Mutex<Vec<Histogram<u64>>>,
    queue_wait_ms: Mutex<Histogram<u64>>,
    /// per-run token accounting, keyed by the `x-rsi-run` header: (requests, prompt, completion)
    pub runs: Mutex<HashMap<String, (u64, u64, u64)>>,
}

fn hist() -> Histogram<u64> {
    Histogram::new_with_bounds(1, 3_600_000, 3).expect("histogram bounds")
}

impl Stats {
    pub fn new() -> Self {
        Stats {
            requests: AtomicU64::new(0),
            cache_hits: AtomicU64::new(0),
            upstream_errors: AtomicU64::new(0),
            retries: AtomicU64::new(0),
            budget_rejects: AtomicU64::new(0),
            prompt_tokens: AtomicU64::new(0),
            cached_prompt_tokens: AtomicU64::new(0),
            completion_tokens: AtomicU64::new(0),
            latency_ms: Mutex::new((0..LEVELS).map(|_| hist()).collect()),
            queue_wait_ms: Mutex::new(hist()),
            runs: Mutex::new(HashMap::new()),
        }
    }

    pub fn used_tokens(&self) -> u64 {
        self.prompt_tokens.load(Relaxed) + self.completion_tokens.load(Relaxed)
    }

    pub fn record_latency(&self, prio: usize, d: Duration) {
        let ms = (d.as_millis() as u64).max(1);
        let _ = self.latency_ms.lock().unwrap()[prio.min(LEVELS - 1)].record(ms);
    }

    pub fn record_wait(&self, d: Duration) {
        let _ = self.queue_wait_ms.lock().unwrap().record((d.as_millis() as u64).max(1));
    }

    /// Account usage from an OpenAI-style response body.
    pub fn record_usage(&self, run: &str, body: &[u8]) {
        let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) else { return };
        let u = &v["usage"];
        let p = u["prompt_tokens"].as_u64().unwrap_or(0);
        let c = u["completion_tokens"].as_u64().unwrap_or(0);
        let cached = u["prompt_tokens_details"]["cached_tokens"].as_u64().unwrap_or(0);
        self.prompt_tokens.fetch_add(p, Relaxed);
        self.completion_tokens.fetch_add(c, Relaxed);
        self.cached_prompt_tokens.fetch_add(cached, Relaxed);
        let mut runs = self.runs.lock().unwrap();
        let e = runs.entry(run.to_string()).or_insert((0, 0, 0));
        e.0 += 1;
        e.1 += p;
        e.2 += c;
    }

    pub fn to_json(&self, queue: (usize, [usize; LEVELS]), cache_entries: usize) -> serde_json::Value {
        let lat = self.latency_ms.lock().unwrap();
        let wait = self.queue_wait_ms.lock().unwrap();
        let q = |h: &Histogram<u64>| {
            serde_json::json!({
                "count": h.len(),
                "p50": h.value_at_quantile(0.5),
                "p90": h.value_at_quantile(0.9),
                "p99": h.value_at_quantile(0.99),
                "max": h.max(),
            })
        };
        let runs: serde_json::Map<String, serde_json::Value> = self
            .runs
            .lock()
            .unwrap()
            .iter()
            .map(|(k, (n, p, c))| (k.clone(), serde_json::json!({"requests": n, "prompt_tokens": p, "completion_tokens": c})))
            .collect();
        serde_json::json!({
            "requests": self.requests.load(Relaxed),
            "cache_hits": self.cache_hits.load(Relaxed),
            "cache_entries": cache_entries,
            "upstream_errors": self.upstream_errors.load(Relaxed),
            "retries": self.retries.load(Relaxed),
            "budget_rejects": self.budget_rejects.load(Relaxed),
            "prompt_tokens": self.prompt_tokens.load(Relaxed),
            "cached_prompt_tokens": self.cached_prompt_tokens.load(Relaxed),
            "completion_tokens": self.completion_tokens.load(Relaxed),
            "free_slots": queue.0,
            "queued": {"high": queue.1[0], "normal": queue.1[1], "low": queue.1[2]},
            "latency_ms": {"high": q(&lat[0]), "normal": q(&lat[1]), "low": q(&lat[2])},
            "queue_wait_ms": q(&wait),
            "runs": runs,
        })
    }

    pub fn to_prometheus(&self, queue: (usize, [usize; LEVELS]), cache_entries: usize) -> String {
        let mut s = String::new();
        let counters = [
            ("rsi_requests_total", &self.requests),
            ("rsi_cache_hits_total", &self.cache_hits),
            ("rsi_upstream_errors_total", &self.upstream_errors),
            ("rsi_retries_total", &self.retries),
            ("rsi_budget_rejects_total", &self.budget_rejects),
            ("rsi_prompt_tokens_total", &self.prompt_tokens),
            ("rsi_cached_prompt_tokens_total", &self.cached_prompt_tokens),
            ("rsi_completion_tokens_total", &self.completion_tokens),
        ];
        for (name, v) in counters {
            let _ = writeln!(s, "# TYPE {name} counter\n{name} {}", v.load(Relaxed));
        }
        let _ = writeln!(s, "# TYPE rsi_cache_entries gauge\nrsi_cache_entries {cache_entries}");
        let _ = writeln!(s, "# TYPE rsi_free_slots gauge\nrsi_free_slots {}", queue.0);
        let _ = writeln!(s, "# TYPE rsi_queued gauge");
        for (i, name) in ["high", "normal", "low"].iter().enumerate() {
            let _ = writeln!(s, "rsi_queued{{priority=\"{name}\"}} {}", queue.1[i]);
        }
        let lat = self.latency_ms.lock().unwrap();
        let _ = writeln!(s, "# TYPE rsi_latency_ms summary");
        for (i, name) in ["high", "normal", "low"].iter().enumerate() {
            for qq in [0.5, 0.9, 0.99] {
                let _ = writeln!(
                    s,
                    "rsi_latency_ms{{priority=\"{name}\",quantile=\"{qq}\"}} {}",
                    lat[i].value_at_quantile(qq)
                );
            }
            let _ = writeln!(s, "rsi_latency_ms_count{{priority=\"{name}\"}} {}", lat[i].len());
        }
        s
    }
}
