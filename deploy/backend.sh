#!/usr/bin/env bash
# Container entrypoint: three capped gateways, the dashboard/live-game server, and (optionally) the
# continuous background RSI loop. State lives in /data (a persistent disk on the VM).
set -euo pipefail
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"
BASE=https://openrouter.ai/api/v1
RUN="${RSI_RUN:-rsi_bg}"
mkdir -p /data/runs /data/data
ln -sfn /data/runs /app/runs
ln -sfn /data/data /app/data

gw() {  # name port usd_budget max_inflight
  UPSTREAM_API_KEY="$OPENROUTER_API_KEY" rsi-gateway serve --listen "127.0.0.1:$2" --upstream "$BASE" \
    --cache-path "/data/runs/cache-$1.bin" --cache-namespace "$1" --max-inflight "$4" --usd-budget "$3" \
    --usd-per-mtok-in 0.15 --usd-per-mtok-out 0.6 &
}
gw player 9000 "${BG_PLAYER_USD:-12}" 96
gw judge  9001 "${BG_JUDGE_USD:-18}"  12
gw play   9002 "${PLAY_USD:-10}"      16
for p in 9000 9001 9002; do until curl -sf -o /dev/null "localhost:$p/stats" 2>/dev/null \
  || python -c "import urllib.request;urllib.request.urlopen('http://localhost:$p/stats')" 2>/dev/null; do sleep 1; done; done

if [ "${RSI_BG:-1}" = "1" ]; then
  if [ -d "runs/$RUN" ]; then RUN="${RUN}_$(date +%Y%m%d_%H%M)"; fi
  SEED=(); [ -d runs/history ] && SEED=(--seed-history runs/history --seed-max "${SEED_MAX:-8}")
  python -m rsi.continuous --config configs/cadence.yaml --run-name "$RUN" --games "${BG_GAMES:-10}" \
    --forever --stop-usd "${BG_STOP_USD:-28}" "${SEED[@]}" > "runs/$RUN.log" 2>&1 &
  until [ -d "runs/$RUN" ]; do sleep 1; done
fi
mkdir -p "runs/$RUN"
exec python -m rsi.viewer serve "runs/$RUN" --host 0.0.0.0 --port 8765 --dev \
  --gateway player=http://127.0.0.1:9000 --gateway judge=http://127.0.0.1:9001
