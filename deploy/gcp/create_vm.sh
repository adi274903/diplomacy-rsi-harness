#!/usr/bin/env bash
# One-time setup of the backend VM in GCP. Run locally after `gcloud auth login`.
#   OPENROUTER_API_KEY=... deploy/gcp/create_vm.sh          (or have it in ./.env as PLAYER_API_KEY)
# No public ports: the UIs are reachable only through an IAP tunnel (deploy/gcp/tunnel.sh).
set -euo pipefail
PROJECT="${PROJECT:-rsitry}"; ZONE="${ZONE:-us-central1-a}"; VM="${VM:-rsi-backend}"
MACHINE="${MACHINE:-e2-medium}"; REPO="${REPO:-https://github.com/adi274903/diplomacy-rsi-harness.git}"
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f .env ]; then set -a; . ./.env; set +a; OPENROUTER_API_KEY="${PLAYER_API_KEY:-}"; fi
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY or PLAYER_API_KEY in .env}"
gcloud config set project "$PROJECT" >/dev/null
SA="rsi-backend@$PROJECT.iam.gserviceaccount.com"

echo "[1/5] enabling APIs"
gcloud services enable compute.googleapis.com secretmanager.googleapis.com iap.googleapis.com

echo "[2/5] storing the OpenRouter key in Secret Manager (secret: openrouter-key)"
if gcloud secrets describe openrouter-key >/dev/null 2>&1; then
  printf %s "$OPENROUTER_API_KEY" | gcloud secrets versions add openrouter-key --data-file=-
else
  printf %s "$OPENROUTER_API_KEY" | gcloud secrets create openrouter-key --replication-policy=automatic --data-file=-
fi

echo "[3/5] service account with read access to that one secret"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create rsi-backend --display-name "RSI backend VM"
gcloud secrets add-iam-policy-binding openrouter-key --member "serviceAccount:$SA" \
  --role roles/secretmanager.secretAccessor >/dev/null

echo "[4/5] firewall: SSH only from Google's IAP range (no other inbound)"
gcloud compute firewall-rules describe rsi-allow-iap-ssh >/dev/null 2>&1 || \
  gcloud compute firewall-rules create rsi-allow-iap-ssh --network default --direction INGRESS \
    --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags rsi-backend

echo "[5/5] creating the VM ($MACHINE, Debian 12, 30 GB persistent disk)"
gcloud compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
  --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB \
  --service-account "$SA" --scopes cloud-platform --tags rsi-backend \
  --metadata-from-file startup-script=deploy/gcp/startup.sh \
  --metadata "rsi-repo=$REPO,rsi-bg=${RSI_BG:-1},bg-stop-usd=${BG_STOP_USD:-28}"
echo "done. First boot builds the image (~5-8 min). Then: deploy/gcp/tunnel.sh"
