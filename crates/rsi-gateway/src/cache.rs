//! Content-addressed response cache: blake3(canonical request JSON) -> response bytes.
//!
//! Makes every rollout replayable: rerunning a game or eval with the same seeds costs zero GPU
//! time and reproduces identical logs. Stored as an append-only log of
//! `[32-byte key][u32 LE length][body]` records, loaded fully into memory at startup. Writes go
//! through one background thread so request handlers never block on disk.

use bytes::Bytes;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::RwLock;
use tokio::sync::mpsc;

pub type Key = [u8; 32];

pub struct Cache {
    map: RwLock<HashMap<Key, Bytes>>,
    tx: Option<mpsc::UnboundedSender<(Key, Bytes)>>,
}

impl Cache {
    pub fn disabled() -> Self {
        Cache { map: RwLock::new(HashMap::new()), tx: None }
    }

    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let mut map = HashMap::new();
        if path.exists() {
            let mut buf = Vec::new();
            std::fs::File::open(path)?.read_to_end(&mut buf)?;
            let mut off = 0usize;
            while off + 36 <= buf.len() {
                let mut key = [0u8; 32];
                key.copy_from_slice(&buf[off..off + 32]);
                let len = u32::from_le_bytes(buf[off + 32..off + 36].try_into().unwrap()) as usize;
                if off + 36 + len > buf.len() {
                    tracing::warn!("cache: truncated tail record ignored");
                    break;
                }
                map.insert(key, Bytes::copy_from_slice(&buf[off + 36..off + 36 + len]));
                off += 36 + len;
            }
            tracing::info!(entries = map.len(), "cache loaded");
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let (tx, rx) = mpsc::unbounded_channel();
        let p: PathBuf = path.to_path_buf();
        std::thread::spawn(move || writer(p, rx));
        Ok(Cache { map: RwLock::new(map), tx: Some(tx) })
    }

    pub fn enabled(&self) -> bool {
        self.tx.is_some()
    }

    pub fn get(&self, k: &Key) -> Option<Bytes> {
        self.map.read().unwrap().get(k).cloned()
    }

    pub fn put(&self, k: Key, v: Bytes) {
        if let Some(tx) = &self.tx {
            self.map.write().unwrap().insert(k, v.clone());
            let _ = tx.send((k, v));
        }
    }

    pub fn len(&self) -> usize {
        self.map.read().unwrap().len()
    }
}

fn writer(path: PathBuf, mut rx: mpsc::UnboundedReceiver<(Key, Bytes)>) {
    let file = std::fs::OpenOptions::new().create(true).append(true).open(&path);
    let Ok(file) = file else {
        tracing::error!(?path, "cache: cannot open for append; persistence disabled");
        return;
    };
    let mut w = std::io::BufWriter::new(file);
    while let Some((k, v)) = rx.blocking_recv() {
        let res = w
            .write_all(&k)
            .and_then(|_| w.write_all(&(v.len() as u32).to_le_bytes()))
            .and_then(|_| w.write_all(&v))
            // Flush whenever the queue drains, so a crash loses at most the in-flight burst.
            .and_then(|_| if rx.is_empty() { w.flush() } else { Ok(()) });
        if let Err(e) = res {
            tracing::error!(%e, "cache write failed");
        }
    }
    let _ = w.flush();
}

/// Cache key over the canonical request. `serde_json::Value` objects are BTreeMaps (sorted keys),
/// so re-serialising gives a canonical byte string independent of client key order.
pub fn key_for(namespace: &str, body: &serde_json::Value) -> Key {
    let mut h = blake3::Hasher::new();
    h.update(namespace.as_bytes());
    h.update(&[0]);
    h.update(&serde_json::to_vec(body).unwrap_or_default());
    *h.finalize().as_bytes()
}

/// Only deterministic requests are cacheable: greedy decoding, or an explicit sampling seed.
pub fn cacheable(body: &serde_json::Value) -> bool {
    if body.get("stream").and_then(|v| v.as_bool()).unwrap_or(false) {
        return false;
    }
    let greedy = body.get("temperature").and_then(|v| v.as_f64()).map(|t| t == 0.0).unwrap_or(false);
    greedy || body.get("seed").map(|s| !s.is_null()).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn key_ignores_field_order() {
        let a = json!({"model": "m", "seed": 1, "messages": [{"role": "user", "content": "hi"}]});
        let b: serde_json::Value =
            serde_json::from_str(r#"{"messages":[{"content":"hi","role":"user"}],"seed":1,"model":"m"}"#).unwrap();
        assert_eq!(key_for("ns", &a), key_for("ns", &b));
        assert_ne!(key_for("ns", &a), key_for("other", &a));
    }

    #[test]
    fn cacheability() {
        assert!(cacheable(&json!({"seed": 3})));
        assert!(cacheable(&json!({"temperature": 0.0})));
        assert!(!cacheable(&json!({"temperature": 0.7})));
        assert!(!cacheable(&json!({"seed": 3, "stream": true})));
    }

    #[test]
    fn persists_and_reloads() {
        let dir = std::env::temp_dir().join(format!("rsi-cache-test-{}", std::process::id()));
        let path = dir.join("c.bin");
        let _ = std::fs::remove_file(&path);
        {
            let c = Cache::open(&path).unwrap();
            c.put([7u8; 32], Bytes::from_static(b"{\"ok\":1}"));
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
        let c = Cache::open(&path).unwrap();
        assert_eq!(c.get(&[7u8; 32]).as_deref(), Some(&b"{\"ok\":1}"[..]));
        let _ = std::fs::remove_dir_all(dir);
    }
}
