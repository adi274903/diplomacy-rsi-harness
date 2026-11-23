#!/usr/bin/env bash
# Start a live game on the VM where you play one country: deploy/gcp/play.sh FRANCE 12
# Then (with deploy/gcp/tunnel.sh running) open http://localhost:8770/ui/play/
set -euo pipefail
VM="${VM:-rsi-backend}"; ZONE="${ZONE:-us-central1-a}"; PROJECT="${PROJECT:-rsitry}"
gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap -- \
  sudo docker exec -d rsi bash deploy/play.sh "${1:-FRANCE}" "${2:-12}"
echo "game starting; open http://localhost:8770/ui/play/ (tunnel must be running)"
