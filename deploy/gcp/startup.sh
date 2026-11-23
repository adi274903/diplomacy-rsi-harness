#!/usr/bin/env bash
# VM startup script (runs as root on every boot): install docker, pull the repo, build, run the backend.
set -euo pipefail
md() { curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" || true; }
REPO="$(md rsi-repo)"; RSI_BG="$(md rsi-bg)"; STOP="$(md bg-stop-usd)"
command -v docker >/dev/null || { apt-get update && apt-get install -y docker.io git; }
mkdir -p /opt/rsi /var/lib/rsi
if [ -d /opt/rsi/src/.git ]; then git -C /opt/rsi/src pull --ff-only; else git clone "$REPO" /opt/rsi/src; fi
docker build -t rsi:latest /opt/rsi/src
umask 077
printf 'OPENROUTER_API_KEY=%s\nRSI_BG=%s\nBG_STOP_USD=%s\n' \
  "$(gcloud secrets versions access latest --secret=openrouter-key)" "${RSI_BG:-1}" "${STOP:-28}" > /opt/rsi/env
docker rm -f rsi >/dev/null 2>&1 || true
# ports bound to the VM's loopback only: reachable solely through an IAP SSH tunnel
docker run -d --name rsi --restart unless-stopped --env-file /opt/rsi/env -v /var/lib/rsi:/data \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:8770:8770 rsi:latest
