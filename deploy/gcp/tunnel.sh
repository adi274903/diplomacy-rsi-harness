#!/usr/bin/env bash
# Open the backend UIs on your machine through an IAP tunnel (no public exposure):
#   http://localhost:8765/ui/game/   http://localhost:8765/ui/rsi/   http://localhost:8770/ui/play/
set -euo pipefail
VM="${VM:-rsi-backend}"; ZONE="${ZONE:-us-central1-a}"; PROJECT="${PROJECT:-rsitry}"
exec gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap -- \
  -N -L 8765:localhost:8765 -L 8770:localhost:8770
