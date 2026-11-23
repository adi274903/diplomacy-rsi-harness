# GCP backend (project `rsitry`)

One Compute Engine VM (e2-medium, Debian 12, 30 GB persistent disk, about $25/month) runs everything in one container:

- **Gateways:**
  - player :9000 (background RSI, $12 cap)
  - judge :9001 ($18 cap)
  - play :9002 ($10 cap)
- **Background loop:** `rsi.continuous`, soft stop at $28.
- **Dashboard and live-game server** on :8765.
- **Live play** on :8770, started on demand.

**No public ports.** The UI ports are bound to the VM's loopback, and the firewall only admits SSH from Google's IAP range. Users reach the UIs through an **IAP tunnel**, so Google IAM is the login.

## Setup (once)

1. Install the Cloud CLI and log in:
   ```
   gcloud auth login
   gcloud config set project rsitry
   ```
2. From the repo root:
   ```
   deploy/gcp/create_vm.sh
   ```
   It uses `PLAYER_API_KEY` from `.env` as the OpenRouter key and stores it in Secret Manager. Nothing secret goes into the image or the repo.
3. Wait about 5–8 minutes on first boot while the image builds.

## Use

| What | Command | Then open |
|---|---|---|
| Open the UIs | `deploy/gcp/tunnel.sh` | http://localhost:8765/ui/game/ and http://localhost:8765/ui/rsi/ |
| Play a game yourself | `deploy/gcp/play.sh FRANCE 12` | http://localhost:8770/ui/play/ |
| Update after a push | `gcloud compute instances reset rsi-backend --zone us-central1-a` | the startup script pulls, rebuilds and restarts |
| Stop background RSI | `gcloud compute ssh rsi-backend --tunnel-through-iap -- sudo touch /var/lib/rsi/runs/rsi_bg/STOP` | |

## Team access

Grant each teammate both of these roles on the project or the VM:
- `roles/iap.tunnelResourceAccessor`
- `roles/compute.instanceAdmin.v1`

They can then run `deploy/gcp/tunnel.sh` themselves.

## Budgets

**Gateway caps reset when the container restarts**, because they are held in memory. The OpenRouter key's own limit is the hard ceiling across restarts.
