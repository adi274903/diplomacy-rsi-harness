//! Load generator that replays harness-shaped traffic.
//!
//! Real harness calls share long prefixes: a genome system prompt (shared by every call of a
//! league member) and a phase board (shared by all 7 powers in a phase), followed by a short
//! private suffix. This generator reproduces that structure so engine knobs (prefix caching,
//! chunked prefill, max running requests, speculative decoding) are tuned against the actual
//! workload rather than random prompts.
//!
//! Output: one JSON line per concurrency level with TTFT / end-to-end percentiles and throughput.

use std::io::Write as _;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use clap::Args;
use futures_util::StreamExt;
use hdrhistogram::Histogram;

#[derive(Args, Clone)]
pub struct BenchArgs {
    /// Base URL of the gateway or engine (without /v1).
    #[arg(long, default_value = "http://127.0.0.1:9000")]
    target: String,
    #[arg(long, default_value = "inkling")]
    model: String,
    #[arg(long, env = "UPSTREAM_API_KEY")]
    api_key: Option<String>,
    /// Concurrency levels to sweep.
    #[arg(long, value_delimiter = ',', default_value = "1,8,32,64,128,256")]
    concurrency: Vec<usize>,
    /// Requests per level (raised to at least 4 x concurrency).
    #[arg(long, default_value_t = 128)]
    requests: usize,
    /// Approximate words in the shared genome/system prefix.
    #[arg(long, default_value_t = 1200)]
    genome_words: usize,
    /// Approximate words in the shared phase-board block.
    #[arg(long, default_value_t = 600)]
    board_words: usize,
    /// Approximate words in the unique private suffix.
    #[arg(long, default_value_t = 250)]
    suffix_words: usize,
    /// Distinct genomes (league members) in rotation.
    #[arg(long, default_value_t = 3)]
    genomes: usize,
    /// Distinct phase boards in rotation.
    #[arg(long, default_value_t = 8)]
    boards: usize,
    #[arg(long, default_value_t = 384)]
    max_tokens: u32,
    #[arg(long, default_value = "low")]
    reasoning_effort: String,
    /// How to pass reasoning effort: top (request field), kwargs (chat_template_kwargs), both, none.
    #[arg(long, default_value = "both")]
    effort_mode: String,
    /// Constrain output with a small JSON schema (exercises the grammar backend).
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    json_schema: bool,
    #[arg(long, default_value = "bench.jsonl")]
    out: PathBuf,
}

struct SplitMix(u64);

impl SplitMix {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

const VOCAB: &[&str] = &[
    "army", "fleet", "support", "hold", "move", "convoy", "Munich", "Paris", "Vienna", "Warsaw", "Trieste",
    "Black", "Sea", "Channel", "alliance", "trust", "promise", "spring", "fall", "build", "disband",
    "centre", "border", "attack", "defend", "Russia", "Turkey", "France", "Germany", "Italy", "Austria",
    "England", "the", "we", "will", "not", "if", "you", "and", "into", "from", "with", "deal", "honest",
];

fn text(seed: u64, words: usize) -> String {
    let mut r = SplitMix(seed);
    let mut s = String::with_capacity(words * 7);
    for i in 0..words {
        if i > 0 {
            s.push(if i % 17 == 0 { '\n' } else { ' ' });
        }
        s.push_str(VOCAB[(r.next() % VOCAB.len() as u64) as usize]);
    }
    s
}

fn build_body(a: &BenchArgs, genomes: &[String], boards: &[String], i: usize) -> serde_json::Value {
    let suffix = text(0xC0FFEE ^ i as u64, a.suffix_words);
    let user = format!(
        "## Public board\n{}\n\n## Your private view (request {i})\n{suffix}\n\nJudge the latest message. Reply in JSON.",
        boards[i % boards.len()]
    );
    let mut body = serde_json::json!({
        "model": a.model,
        "messages": [
            {"role": "system", "content": genomes[i % genomes.len()]},
            {"role": "user", "content": user},
        ],
        "max_tokens": a.max_tokens,
        "temperature": 1.0,
        "seed": i,
        "stream": true,
        "stream_options": {"include_usage": true},
    });
    if matches!(a.effort_mode.as_str(), "top" | "both") {
        body["reasoning_effort"] = serde_json::json!(a.reasoning_effort);
    }
    if matches!(a.effort_mode.as_str(), "kwargs" | "both") {
        body["chat_template_kwargs"] = serde_json::json!({"reasoning_effort": a.reasoning_effort});
    }
    if a.json_schema {
        body["response_format"] = serde_json::json!({
            "type": "json_schema",
            "json_schema": {"name": "judgment", "strict": true, "schema": {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string"},
                    "p_lie": {"type": "number", "minimum": 0, "maximum": 1},
                    "decision": {"type": "string", "enum": ["trust", "distrust"]}
                },
                "required": ["reasoning", "p_lie", "decision"],
                "additionalProperties": false
            }}
        });
    }
    body
}

/// True once an SSE `data:` line carries generated text (answer or reasoning).
fn has_token(line: &str) -> bool {
    let Some(data) = line.strip_prefix("data:") else { return false };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(data.trim()) else { return false };
    let d = &v["choices"][0]["delta"];
    ["content", "reasoning_content", "reasoning"]
        .iter()
        .any(|k| d[*k].as_str().map(|s| !s.is_empty()).unwrap_or(false))
}

fn usage_tokens(line: &str) -> Option<u64> {
    let data = line.strip_prefix("data:")?;
    let v: serde_json::Value = serde_json::from_str(data.trim()).ok()?;
    v["usage"]["completion_tokens"].as_u64()
}

struct Sample {
    ttft: Option<Duration>,
    e2e: Duration,
    out_tokens: u64,
    ok: bool,
}

async fn one(client: &reqwest::Client, a: &BenchArgs, body: serde_json::Value) -> Sample {
    let t0 = Instant::now();
    let mut req = client
        .post(format!("{}/v1/chat/completions", a.target.trim_end_matches('/')))
        .header("x-rsi-cache", "off")
        .header("x-rsi-run", "bench")
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(serde_json::to_vec(&body).unwrap_or_default());
    if let Some(k) = &a.api_key {
        req = req.bearer_auth(k);
    }
    let fail = |t0: Instant| Sample { ttft: None, e2e: t0.elapsed(), out_tokens: 0, ok: false };
    let Ok(resp) = req.send().await else { return fail(t0) };
    if !resp.status().is_success() {
        return fail(t0);
    }
    let mut stream = resp.bytes_stream();
    let mut buf = String::new();
    let (mut ttft, mut out_tokens) = (None, 0u64);
    while let Some(chunk) = stream.next().await {
        let Ok(chunk) = chunk else { return fail(t0) };
        buf.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(pos) = buf.find('\n') {
            let line: String = buf.drain(..=pos).collect();
            let line = line.trim();
            if ttft.is_none() && has_token(line) {
                ttft = Some(t0.elapsed());
            }
            if let Some(n) = usage_tokens(line) {
                out_tokens = n;
            }
        }
    }
    Sample { ttft, e2e: t0.elapsed(), out_tokens, ok: true }
}

fn pct(h: &Histogram<u64>, q: f64) -> u64 {
    if h.is_empty() {
        0
    } else {
        h.value_at_quantile(q)
    }
}

pub async fn run(a: BenchArgs) -> anyhow::Result<()> {
    let client = reqwest::Client::builder()
        .pool_max_idle_per_host(1024)
        .tcp_nodelay(true)
        .build()?;
    let genomes: Vec<String> = (0..a.genomes.max(1))
        .map(|g| format!("You are a Diplomacy player (genome {g}).\n{}", text(1000 + g as u64, a.genome_words)))
        .collect();
    let boards: Vec<String> = (0..a.boards.max(1)).map(|b| text(5000 + b as u64, a.board_words)).collect();
    let mut out = std::fs::OpenOptions::new().create(true).append(true).open(&a.out)?;

    println!(
        "{:>6} {:>8} {:>10} {:>9} {:>9} {:>9} {:>9} {:>9} {:>6}",
        "conc", "req/s", "out_tok/s", "ttft_p50", "ttft_p99", "e2e_p50", "e2e_p90", "e2e_p99", "errors"
    );
    for &conc in &a.concurrency {
        let total = a.requests.max(conc * 4);
        let next = Arc::new(AtomicUsize::new(0));
        let samples = Arc::new(Mutex::new(Vec::with_capacity(total)));
        let started = Instant::now();
        let mut workers = Vec::new();
        for _ in 0..conc {
            let (client, a, genomes, boards, next, samples) =
                (client.clone(), a.clone(), genomes.clone(), boards.clone(), next.clone(), samples.clone());
            workers.push(tokio::spawn(async move {
                loop {
                    let i = next.fetch_add(1, Relaxed);
                    if i >= total {
                        break;
                    }
                    let s = one(&client, &a, build_body(&a, &genomes, &boards, i)).await;
                    samples.lock().unwrap().push(s);
                }
            }));
        }
        for w in workers {
            w.await?;
        }
        let wall = started.elapsed().as_secs_f64();
        let samples = samples.lock().unwrap();
        let mut ttft = Histogram::<u64>::new_with_bounds(1, 3_600_000, 3)?;
        let mut e2e = Histogram::<u64>::new_with_bounds(1, 3_600_000, 3)?;
        let (mut errors, mut tokens) = (0usize, 0u64);
        for s in samples.iter() {
            if !s.ok {
                errors += 1;
                continue;
            }
            if let Some(t) = s.ttft {
                ttft.record((t.as_millis() as u64).max(1))?;
            }
            e2e.record((s.e2e.as_millis() as u64).max(1))?;
            tokens += s.out_tokens;
        }
        let ok = samples.len() - errors;
        let row = serde_json::json!({
            "concurrency": conc,
            "requests": samples.len(),
            "errors": errors,
            "wall_s": wall,
            "req_per_s": ok as f64 / wall,
            "out_tok_per_s": tokens as f64 / wall,
            "ttft_ms": {"p50": pct(&ttft, 0.5), "p90": pct(&ttft, 0.9), "p99": pct(&ttft, 0.99)},
            "e2e_ms": {"p50": pct(&e2e, 0.5), "p90": pct(&e2e, 0.9), "p99": pct(&e2e, 0.99)},
            "config": {
                "genome_words": a.genome_words, "board_words": a.board_words, "suffix_words": a.suffix_words,
                "max_tokens": a.max_tokens, "reasoning_effort": a.reasoning_effort, "json_schema": a.json_schema,
                "target": a.target,
            },
        });
        writeln!(out, "{row}")?;
        println!(
            "{:>6} {:>8.2} {:>10.1} {:>9} {:>9} {:>9} {:>9} {:>9} {:>6}",
            conc,
            ok as f64 / wall,
            tokens as f64 / wall,
            pct(&ttft, 0.5),
            pct(&ttft, 0.99),
            pct(&e2e, 0.5),
            pct(&e2e, 0.9),
            pct(&e2e, 0.99),
            errors
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_parsing() {
        assert!(has_token(r#"data: {"choices":[{"delta":{"content":"hi"}}]}"#));
        assert!(has_token(r#"data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}"#));
        assert!(!has_token(r#"data: {"choices":[{"delta":{"role":"assistant","content":""}}]}"#));
        assert!(!has_token("data: [DONE]"));
        assert_eq!(usage_tokens(r#"data: {"choices":[],"usage":{"completion_tokens":42}}"#), Some(42));
    }

    #[test]
    fn prompts_share_prefixes() {
        let a = BenchArgs {
            target: "x".into(), model: "m".into(), api_key: None, concurrency: vec![1], requests: 1,
            genome_words: 50, board_words: 20, suffix_words: 5, genomes: 2, boards: 3, max_tokens: 8,
            reasoning_effort: "low".into(), effort_mode: "both".into(), json_schema: true, out: "o".into(),
        };
        let g: Vec<String> = (0..2).map(|i| text(i, 50)).collect();
        let b: Vec<String> = (0..3).map(|i| text(10 + i, 20)).collect();
        let b0 = build_body(&a, &g, &b, 0);
        let b2 = build_body(&a, &g, &b, 2);
        assert_eq!(b0["messages"][0], b2["messages"][0]); // same genome (0 % 2 == 2 % 2)
        assert_ne!(b0["messages"][1], b2["messages"][1]);
    }
}
