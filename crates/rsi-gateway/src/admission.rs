//! Strict-priority admission control.
//!
//! The engine batches everything it is given, so the gateway caps in-flight requests and decides
//! *who* gets the next slot. Game turns (a sequential dependency chain: press -> judge -> orders)
//! run at high priority so self-play wall-clock stays low, while bulk eval sweeps (human-data
//! rollouts, gate replays) soak up the remaining capacity at low priority.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use tokio::sync::oneshot;

pub const LEVELS: usize = 3;

pub struct Admission {
    st: Mutex<State>,
    pub capacity: usize,
}

struct State {
    available: usize,
    queues: [VecDeque<oneshot::Sender<Permit>>; LEVELS],
}

/// Holding a permit means one request slot. Dropping it hands the slot to the next waiter.
pub struct Permit {
    adm: Arc<Admission>,
    armed: bool,
}

impl Admission {
    pub fn new(capacity: usize) -> Arc<Self> {
        Arc::new(Admission {
            st: Mutex::new(State { available: capacity, queues: Default::default() }),
            capacity,
        })
    }

    /// `prio`: 0 = high, 1 = normal, 2 = low.
    pub async fn acquire(self: &Arc<Self>, prio: usize) -> Permit {
        let prio = prio.min(LEVELS - 1);
        let rx = {
            let mut st = self.st.lock().unwrap();
            let ahead: usize = st.queues[..=prio].iter().map(|q| q.len()).sum();
            if st.available > 0 && ahead == 0 {
                st.available -= 1;
                return Permit { adm: self.clone(), armed: true };
            }
            let (tx, rx) = oneshot::channel();
            st.queues[prio].push_back(tx);
            rx
        };
        // If this future is cancelled after a permit was sent, the permit is dropped inside the
        // channel and its Drop releases the slot again, so nothing leaks.
        rx.await.expect("admission dropped while waiting")
    }

    fn release(self: &Arc<Self>) {
        let mut st = self.st.lock().unwrap();
        for level in 0..LEVELS {
            while let Some(tx) = st.queues[level].pop_front() {
                match tx.send(Permit { adm: self.clone(), armed: true }) {
                    Ok(()) => return,
                    Err(mut p) => p.armed = false, // waiter gave up; try the next one
                }
            }
        }
        st.available += 1;
    }

    /// (free slots, queued waiters per priority level)
    pub fn snapshot(&self) -> (usize, [usize; LEVELS]) {
        let st = self.st.lock().unwrap();
        (st.available, [st.queues[0].len(), st.queues[1].len(), st.queues[2].len()])
    }
}

impl Drop for Permit {
    fn drop(&mut self) {
        if self.armed {
            self.armed = false;
            self.adm.release();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn high_priority_jumps_queue() {
        let adm = Admission::new(1);
        let held = adm.acquire(1).await;
        let order = Arc::new(Mutex::new(Vec::new()));
        let mut handles = Vec::new();
        for (name, prio) in [("low", 2usize), ("high", 0usize)] {
            let (adm, order) = (adm.clone(), order.clone());
            handles.push(tokio::spawn(async move {
                let _p = adm.acquire(prio).await;
                order.lock().unwrap().push(name);
            }));
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        drop(held);
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(*order.lock().unwrap(), vec!["high", "low"]);
        assert_eq!(adm.snapshot().0, 1);
    }

    #[tokio::test]
    async fn cancelled_waiter_does_not_leak() {
        let adm = Admission::new(1);
        let held = adm.acquire(0).await;
        let a2 = adm.clone();
        let waiter = tokio::spawn(async move {
            let _p = a2.acquire(0).await;
        });
        tokio::time::sleep(Duration::from_millis(10)).await;
        waiter.abort();
        let _ = waiter.await;
        drop(held);
        assert_eq!(adm.snapshot().0, 1);
    }
}
