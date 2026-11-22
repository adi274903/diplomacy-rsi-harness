//! Word n-gram shingle index for contamination checks.
//!
//! Every text that may reach a training channel (playbook diffs, exemplars, SFT traces) is checked
//! against an index built from the held-out human data (val/test). Any shared n-gram is a hit.

use rayon::prelude::*;
use std::collections::HashMap;

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0100_0000_01b3;

/// Lowercased alphanumeric word tokens (apostrophes dropped, everything else splits).
pub fn words(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for ch in text.chars() {
        if ch.is_alphanumeric() {
            cur.extend(ch.to_lowercase());
        } else if ch == '\'' || ch == '’' {
            continue;
        } else if !cur.is_empty() {
            out.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

pub fn shingles(text: &str, n: usize) -> Vec<u64> {
    let w = words(text);
    if w.len() < n || n == 0 {
        return Vec::new();
    }
    w.windows(n)
        .map(|win| {
            let mut h = FNV_OFFSET;
            for (k, word) in win.iter().enumerate() {
                if k > 0 {
                    h ^= b' ' as u64;
                    h = h.wrapping_mul(FNV_PRIME);
                }
                for b in word.as_bytes() {
                    h ^= *b as u64;
                    h = h.wrapping_mul(FNV_PRIME);
                }
            }
            h
        })
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Overlap {
    pub hits: usize,
    pub total: usize,
    /// first indexed doc that shares a shingle, if any
    pub doc: Option<u32>,
}

impl Overlap {
    pub fn frac(&self) -> f64 {
        if self.total == 0 {
            0.0
        } else {
            self.hits as f64 / self.total as f64
        }
    }
}

pub struct NgramIndex {
    pub n: usize,
    map: HashMap<u64, u32>,
    pub docs: usize,
}

impl NgramIndex {
    pub fn new(n: usize) -> Self {
        NgramIndex { n: n.max(1), map: HashMap::new(), docs: 0 }
    }

    pub fn add(&mut self, doc_id: u32, text: &str) {
        for h in shingles(text, self.n) {
            self.map.entry(h).or_insert(doc_id);
        }
        self.docs += 1;
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    pub fn query(&self, text: &str) -> Overlap {
        let sh = shingles(text, self.n);
        let mut doc = None;
        let mut hits = 0;
        for h in &sh {
            if let Some(&d) = self.map.get(h) {
                hits += 1;
                doc.get_or_insert(d);
            }
        }
        Overlap { hits, total: sh.len(), doc }
    }

    pub fn query_many(&self, texts: &[String]) -> Vec<Overlap> {
        texts.par_iter().map(|t| self.query(t)).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_verbatim_and_case_insensitive() {
        let mut ix = NgramIndex::new(5);
        ix.add(3, "I will support your army into Munich this spring, trust me.");
        let o = ix.query("he said: i WILL support your army into munich!");
        assert!(o.hits >= 1);
        assert_eq!(o.doc, Some(3));
        assert_eq!(ix.query("completely unrelated text about the weather today ok").hits, 0);
    }

    #[test]
    fn short_text_has_no_shingles() {
        let ix = NgramIndex::new(8);
        assert_eq!(ix.query("too short").total, 0);
    }
}
