//! Runtime information-isolation guard.
//!
//! Before any player prompt is sent, it is scanned for strings that the player must not see:
//! other powers' private reasoning, private memory, vault entries, and bilateral messages between
//! *other* pairs. Aho-Corasick makes this a single linear pass over the prompt regardless of how
//! many secrets the game has accumulated.

use aho_corasick::{AhoCorasick, AhoCorasickBuilder, MatchKind};

pub struct LeakGuard {
    patterns: Vec<String>,
    tags: Vec<String>,
    automaton: Option<AhoCorasick>,
    dirty: bool,
    pub min_len: usize,
}

impl LeakGuard {
    pub fn new(min_len: usize) -> Self {
        LeakGuard { patterns: Vec::new(), tags: Vec::new(), automaton: None, dirty: false, min_len }
    }

    /// Register a secret. Very short strings are ignored: they collide with ordinary text.
    /// Returns false when skipped.
    pub fn add(&mut self, pattern: &str, tag: &str) -> bool {
        let p = pattern.trim();
        if p.chars().count() < self.min_len {
            return false;
        }
        self.patterns.push(p.to_string());
        self.tags.push(tag.to_string());
        self.dirty = true;
        true
    }

    pub fn len(&self) -> usize {
        self.patterns.len()
    }

    pub fn is_empty(&self) -> bool {
        self.patterns.is_empty()
    }

    fn build(&mut self) {
        if self.dirty || self.automaton.is_none() {
            self.automaton = if self.patterns.is_empty() {
                None
            } else {
                Some(
                    AhoCorasickBuilder::new()
                        .ascii_case_insensitive(true)
                        .match_kind(MatchKind::Standard)
                        .build(&self.patterns)
                        .expect("aho-corasick build"),
                )
            };
            self.dirty = false;
        }
    }

    /// Tags of every secret found in `text` (deduplicated, in first-seen order).
    pub fn scan(&mut self, text: &str) -> Vec<String> {
        self.build();
        let Some(ac) = &self.automaton else { return Vec::new() };
        let mut seen = vec![false; self.patterns.len()];
        let mut out = Vec::new();
        // Overlapping search: secrets may overlap or nest, and every one must be reported.
        for m in ac.find_overlapping_iter(text) {
            let id = m.pattern().as_usize();
            if !seen[id] {
                seen[id] = true;
                out.push(self.tags[id].clone());
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_secret_and_ignores_short() {
        let mut g = LeakGuard::new(20);
        assert!(!g.add("short", "x"));
        assert!(g.add("I plan to stab Germany in the fall", "FRANCE:memory:3"));
        assert_eq!(g.scan("...i plan to stab germany in the fall..."), vec!["FRANCE:memory:3".to_string()]);
        assert!(g.scan("nothing to see").is_empty());
        g.add("Russia is lying about Sevastopol support", "TURKEY:trace:9");
        assert_eq!(g.scan("note: Russia is lying about Sevastopol support").len(), 1);
    }
}
