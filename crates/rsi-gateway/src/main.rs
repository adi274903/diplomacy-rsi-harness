//! rsi-gateway: the single OpenAI-compatible endpoint the harness talks to.
//!
//! ```text
//! harness (many async calls) ──► rsi-gateway ──► vLLM / SGLang replicas (H100)
//!                                  │ priority admission (game turns before bulk eval)
//!                                  │ content-addressed response cache (replayable runs)
//!                                  │ prefix-affinity routing (keeps the engine's prefix-cache hot)
//!                                  │ retries with backoff, token budget, per-run accounting
//!                                  └ /stats (JSON), /metrics (Prometheus)
//! ```
//!
//! Request headers understood:
//! - `x-rsi-priority: high|normal|low` (default normal)
//! - `x-rsi-run: <name>`   token accounting bucket
//! - `x-rsi-cache: off`    bypass the response cache

mod admission;
mod bench;
mod cache;
mod stats;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use clap::{Args, Parser, Subcommand};
use futures_util::StreamExt;

use admission::{Admission, Permit};
use cache::Cache;
use stats::Stats;

#[derive(Parser)]
#[command(name = "rsi-gateway", version, about = "Low-latency gateway for the Diplomacy RSI harness")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the gateway in front of one or more OpenAI-compatible engines.
    Serve(ServeArgs),
    /// Replay harness-shaped traffic against an endpoint and report TTFT / latency / throughput.
    Bench(bench::BenchArgs),
}

#[derive(Args, Clone)]
struct ServeArgs {
    #[arg(long, env = "RSI_LISTEN", default_value = "0.0.0.0:9000")]
    listen: SocketAddr,
    /// Engine base URLs (without /v1), comma separated, e.g. http://127.0.0.1:8000
    #[arg(long, env = "RSI_UPSTREAMS", value_delimiter = ',', required = true)]
    upstream: Vec<String>,
    /// Bearer token sent to the engines, if they require one.
    #[arg(long, env = "UPSTREAM_API_KEY")]
    upstream_api_key: Option<String>,
    /// Max requests in flight to the engines (summed over replicas).
    #[arg(long, env = "RSI_MAX_INFLIGHT", default_value_t = 256)]
    max_inflight: usize,
    /// Response cache file; omit to disable caching.
    #[arg(long, env = "RSI_CACHE_PATH")]
    cache_path: Option<PathBuf>,
    /// Namespace mixed into cache keys (bump when the served weights change).
    #[arg(long, env = "RSI_CACHE_NAMESPACE", default_value = "inkling-2-mini")]
    cache_namespace: String,
    /// Hard cap on prompt+completion tokens across the gateway's lifetime (0 = unlimited).
    #[arg(long, env = "RSI_TOKEN_BUDGET", default_value_t = 0)]
    token_budget: u64,
    #[arg(long, default_value_t = 3)]
    retries: u32,
    /// Per-request timeout for non-streaming calls.
    #[arg(long, default_value_t = 900)]
    timeout_s: u64,
    /// How many more in-flight requests the affine replica may carry than the least-loaded one
    /// before we give up on prefix affinity.
    #[arg(long, default_value_t = 16)]
    affinity_slack: usize,
}

struct Upstream {
    url: String,
    inflight: AtomicUsize,
}

struct InflightGuard(Arc<Upstream>);

impl InflightGuard {
    fn new(u: Arc<Upstream>) -> Self {
        u.inflight.fetch_add(1, Relaxed);
        InflightGuard(u)
    }
}

impl Drop for InflightGuard {
    fn drop(&mut self) {
        self.0.inflight.fetch_sub(1, Relaxed);
    }
}

struct App {
    client: reqwest::Client,
    upstreams: Vec<Arc<Upstream>>,
    adm: Arc<Admission>,
    cache: Cache,
    stats: Stats,
    cfg: ServeArgs,
}

impl App {
    /// Prefer the replica chosen by the prompt prefix (its KV prefix cache already holds the
    /// shared genome/system prompt), unless it is much busier than the least-loaded replica.
    fn pick(&self, affinity: u64) -> Arc<Upstream> {
        let n = self.upstreams.len();
        if n == 1 {
            return self.upstreams[0].clone();
        }
        let pref = &self.upstreams[(affinity % n as u64) as usize];
        let least = self.upstreams.iter().min_by_key(|u| u.inflight.load(Relaxed)).unwrap();
        if pref.inflight.load(Relaxed) <= least.inflight.load(Relaxed) + self.cfg.affinity_slack {
            pref.clone()
        } else {
            least.clone()
        }
    }
}

fn priority(h: &HeaderMap) -> usize {
    match h.get("x-rsi-priority").and_then(|v| v.to_str().ok()) {
        Some("high") | Some("0") => 0,
        Some("low") | Some("2") => 2,
        _ => 1,
    }
}

/// Hash of the first message (the system prompt carrying the genome), the longest prefix shared
/// across most calls.
fn affinity_key(body: &serde_json::Value) -> u64 {
    let first = body["messages"].get(0).map(|m| m.to_string()).unwrap_or_default();
    let cut = first.len().min(8192);
    let bytes = &first.as_bytes()[..cut];
    let h = blake3::hash(bytes);
    u64::from_le_bytes(h.as_bytes()[..8].try_into().unwrap())
}

fn json_response(status: StatusCode, body: Bytes, cache_state: &'static str) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-rsi-cache", cache_state)
        .body(Body::from(body))
        .unwrap()
}

fn error(status: StatusCode, kind: &str, msg: impl std::fmt::Display) -> Response {
    let body = serde_json::json!({"error": {"type": kind, "message": msg.to_string()}});
    (status, [(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
}

fn retryable(s: StatusCode) -> bool {
    s == StatusCode::TOO_MANY_REQUESTS || s.is_server_error()
}

async fn backoff(attempt: u32) {
    let base = 250u64 << attempt.min(6);
    let jitter = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0)
        % 100) as u64;
    tokio::time::sleep(Duration::from_millis(base + jitter)).await;
}

enum Step {
    Done(Response),
    Retry(String),
}

async fn chat(State(app): State<Arc<App>>, headers: HeaderMap, body: Bytes) -> Response {
    let t0 = Instant::now();
    app.stats.requests.fetch_add(1, Relaxed);
    let prio = priority(&headers);
    let run = headers.get("x-rsi-run").and_then(|v| v.to_str().ok()).unwrap_or("default").to_string();
    let no_cache = headers.get("x-rsi-cache").and_then(|v| v.to_str().ok()) == Some("off");

    let parsed: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => return error(StatusCode::BAD_REQUEST, "invalid_json", e),
    };
    let stream = parsed.get("stream").and_then(|v| v.as_bool()).unwrap_or(false);

    let key = (app.cache.enabled() && !no_cache && cache::cacheable(&parsed))
        .then(|| cache::key_for(&app.cfg.cache_namespace, &parsed));
    if let Some(k) = &key {
        if let Some(hit) = app.cache.get(k) {
            app.stats.cache_hits.fetch_add(1, Relaxed);
            app.stats.record_latency(prio, t0.elapsed());
            return json_response(StatusCode::OK, hit, "hit");
        }
    }

    if app.cfg.token_budget > 0 && app.stats.used_tokens() >= app.cfg.token_budget {
        app.stats.budget_rejects.fetch_add(1, Relaxed);
        return error(StatusCode::TOO_MANY_REQUESTS, "budget_exhausted", "gateway token budget exhausted");
    }

    let waited = Instant::now();
    let permit: Permit = app.adm.acquire(prio).await;
    app.stats.record_wait(waited.elapsed());
    let affinity = affinity_key(&parsed);

    let mut attempt = 0u32;
    loop {
        let up = app.pick(affinity);
        let guard = InflightGuard::new(up.clone());
        let mut req = app
            .client
            .post(format!("{}/v1/chat/completions", up.url))
            .header(header::CONTENT_TYPE, "application/json")
            .body(body.clone());
        if let Some(k) = &app.cfg.upstream_api_key {
            req = req.bearer_auth(k);
        }
        if !stream {
            req = req.timeout(Duration::from_secs(app.cfg.timeout_s));
        }

        let step = match req.send().await {
            Err(e) => Step::Retry(format!("send: {e}")),
            Ok(resp) => {
                let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
                if status.is_success() && stream {
                    // The stream owns the slot and the in-flight count until the client finishes.
                    let s = resp.bytes_stream().map(move |chunk| {
                        let _hold = (&permit, &guard);
                        chunk
                    });
                    return Response::builder()
                        .status(status)
                        .header(header::CONTENT_TYPE, "text/event-stream")
                        .header("x-rsi-cache", "bypass")
                        .body(Body::from_stream(s))
                        .unwrap();
                } else if status.is_success() {
                    match resp.bytes().await {
                        Ok(bytes) => {
                            app.stats.record_usage(&run, &bytes);
                            if let Some(k) = key {
                                app.cache.put(k, bytes.clone());
                            }
                            Step::Done(json_response(status, bytes, if key.is_some() { "miss" } else { "off" }))
                        }
                        Err(e) => Step::Retry(format!("read body: {e}")),
                    }
                } else if retryable(status) {
                    let text = resp.text().await.unwrap_or_default();
                    Step::Retry(format!("{status}: {}", text.chars().take(300).collect::<String>()))
                } else {
                    // Client errors (bad schema, context overflow) are not retried.
                    app.stats.upstream_errors.fetch_add(1, Relaxed);
                    let bytes = resp.bytes().await.unwrap_or_default();
                    Step::Done(json_response(status, bytes, "off"))
                }
            }
        };
        drop(guard);

        match step {
            Step::Done(r) => {
                app.stats.record_latency(prio, t0.elapsed());
                return r;
            }
            Step::Retry(why) if attempt < app.cfg.retries => {
                app.stats.retries.fetch_add(1, Relaxed);
                tracing::warn!(attempt, upstream = %up.url, %why, "retrying");
                backoff(attempt).await;
                attempt += 1;
            }
            Step::Retry(why) => {
                app.stats.upstream_errors.fetch_add(1, Relaxed);
                app.stats.record_latency(prio, t0.elapsed());
                return error(StatusCode::BAD_GATEWAY, "upstream_failed", why);
            }
        }
    }
}

async fn models(State(app): State<Arc<App>>) -> Response {
    let up = &app.upstreams[0];
    let mut req = app.client.get(format!("{}/v1/models", up.url)).timeout(Duration::from_secs(10));
    if let Some(k) = &app.cfg.upstream_api_key {
        req = req.bearer_auth(k);
    }
    match req.send().await {
        Ok(r) => {
            let status = StatusCode::from_u16(r.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            json_response(status, r.bytes().await.unwrap_or_default(), "off")
        }
        Err(e) => error(StatusCode::BAD_GATEWAY, "upstream_failed", e),
    }
}

async fn health(State(app): State<Arc<App>>) -> Response {
    let mut out = Vec::new();
    let mut all_ok = true;
    for u in &app.upstreams {
        let ok = app
            .client
            .get(format!("{}/health", u.url))
            .timeout(Duration::from_secs(3))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false);
        all_ok &= ok;
        out.push(serde_json::json!({"url": u.url, "ok": ok, "inflight": u.inflight.load(Relaxed)}));
    }
    let status = if all_ok { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
    (status, axum::Json(serde_json::json!({"ok": all_ok, "upstreams": out}))).into_response()
}

async fn stats_json(State(app): State<Arc<App>>) -> Response {
    axum::Json(app.stats.to_json(app.adm.snapshot(), app.cache.len())).into_response()
}

async fn metrics_text(State(app): State<Arc<App>>) -> Response {
    (
        [(header::CONTENT_TYPE, "text/plain; version=0.0.4")],
        app.stats.to_prometheus(app.adm.snapshot(), app.cache.len()),
    )
        .into_response()
}

async fn serve(cfg: ServeArgs) -> anyhow::Result<()> {
    let client = reqwest::Client::builder()
        .pool_max_idle_per_host(cfg.max_inflight)
        .tcp_nodelay(true)
        .connect_timeout(Duration::from_secs(5))
        .build()?;
    let cache = match &cfg.cache_path {
        Some(p) => Cache::open(p)?,
        None => Cache::disabled(),
    };
    let upstreams = cfg
        .upstream
        .iter()
        .map(|u| Arc::new(Upstream { url: u.trim_end_matches('/').to_string(), inflight: AtomicUsize::new(0) }))
        .collect();
    let app = Arc::new(App {
        client,
        upstreams,
        adm: Admission::new(cfg.max_inflight),
        cache,
        stats: Stats::new(),
        cfg: cfg.clone(),
    });

    let router = Router::new()
        .route("/v1/chat/completions", post(chat))
        .route("/v1/models", get(models))
        .route("/health", get(health))
        .route("/stats", get(stats_json))
        .route("/metrics", get(metrics_text))
        .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
        .with_state(app);

    tracing::info!(listen = %cfg.listen, upstreams = ?cfg.upstream, max_inflight = cfg.max_inflight, "rsi-gateway up");
    let listener = tokio::net::TcpListener::bind(cfg.listen).await?;
    axum::serve(listener, router).with_graceful_shutdown(shutdown()).await?;
    Ok(())
}

async fn shutdown() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutting down");
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    match Cli::parse().cmd {
        Cmd::Serve(a) => serve(a).await,
        Cmd::Bench(a) => bench::run(a).await,
    }
}
