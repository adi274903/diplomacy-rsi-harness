#!/usr/bin/env bash
# Start two rsi-gateway instances in front of the hosted APIs:
#   :9000 -> player model (Inkling 2 Mini provider)
#   :9001 -> reviewer model
# Each gets its own response cache (replays are free) and USD budget.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
cargo build --release -p rsi-gateway
BIN=./target/release/rsi-gateway
mkdir -p runs

UPSTREAM_API_KEY="$PLAYER_API_KEY" $BIN serve \
  --listen 127.0.0.1:9000 --upstream "$PLAYER_API_BASE" \
  --cache-path runs/cache-player.bin --cache-namespace player \
  --max-inflight "${PLAYER_MAX_INFLIGHT:-64}" \
  --usd-budget "${PLAYER_USD_BUDGET:-0}" \
  --usd-per-mtok-in "${PLAYER_USD_PER_MTOK_IN:-0}" --usd-per-mtok-out "${PLAYER_USD_PER_MTOK_OUT:-0}" &
P1=$!

UPSTREAM_API_KEY="$REVIEWER_API_KEY" $BIN serve \
  --listen 127.0.0.1:9001 --upstream "$REVIEWER_API_BASE" \
  --cache-path runs/cache-reviewer.bin --cache-namespace reviewer \
  --max-inflight "${REVIEWER_MAX_INFLIGHT:-16}" \
  --usd-budget "${REVIEWER_USD_BUDGET:-0}" \
  --usd-per-mtok-in "${REVIEWER_USD_PER_MTOK_IN:-0}" --usd-per-mtok-out "${REVIEWER_USD_PER_MTOK_OUT:-0}" &
P2=$!

trap 'kill $P1 $P2 2>/dev/null' EXIT INT TERM
echo "gateways up: player :9000 (pid $P1), reviewer :9001 (pid $P2). stats: curl localhost:9000/stats"
wait
