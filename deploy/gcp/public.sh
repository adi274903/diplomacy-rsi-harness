#!/usr/bin/env bash
# Expose the backend on HTTPS: dashboards public (read-only), play page behind a shared password.
#   deploy/gcp/public.sh            -> https://<ip-with-dashes>.sslip.io
# The password is generated once and kept in ./.demo-password (gitignored); user name is "demo".
set -euo pipefail
PROJECT="${PROJECT:-rsitry}"; ZONE="${ZONE:-us-central1-a}"; REGION="${ZONE%-*}"; VM="${VM:-rsi-backend}"
gcloud config set project "$PROJECT" >/dev/null
IP=$(gcloud compute instances describe "$VM" --zone "$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
gcloud compute addresses describe rsi-ip --region "$REGION" >/dev/null 2>&1 || \
  gcloud compute addresses create rsi-ip --region "$REGION" --addresses "$IP"          # keep this IP forever
gcloud compute firewall-rules describe rsi-allow-web >/dev/null 2>&1 || \
  gcloud compute firewall-rules create rsi-allow-web --network default --direction INGRESS \
    --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags rsi-backend
HOST="$(echo "$IP" | tr . -).sslip.io"
[ -s .demo-password ] || { umask 077; head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20 > .demo-password; }
PW=$(cat .demo-password)
gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --command "
set -e
command -v caddy >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq caddy; }
HASH=\$(caddy hash-password --plaintext '$PW')
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$HOST {
	encode gzip
	@play path /ui/play /ui/play/* /api/play/* /maps/*
	handle @play {
		basicauth {
			demo \$HASH
		}
		reverse_proxy 127.0.0.1:8770
	}
	handle {
		reverse_proxy 127.0.0.1:8765
	}
}
CADDY
sudo systemctl enable --now caddy >/dev/null 2>&1; sudo systemctl reload caddy
"
echo "public dashboards: https://$HOST/ui/game/  https://$HOST/ui/rsi/  (live game: https://$HOST/live)"
echo "play (user demo, password in ./.demo-password): https://$HOST/ui/play/"
