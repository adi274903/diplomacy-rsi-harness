#!/usr/bin/env bash
# Start a live game inside the running container: deploy/play.sh FRANCE 12
set -euo pipefail
POWER="${1:-FRANCE}"; MONTHS="${2:-12}"; NAME="play_$(date +%Y%m%d_%H%M%S)"
cd /app
exec python -m rsi.play --config configs/play.yaml --power "$POWER" --months "$MONTHS" \
  --run-name "$NAME" --host 0.0.0.0 --port 8770 > "runs/$NAME.log" 2>&1
