# Deploying the RRC bot on Google Cloud (e2-micro)

This guide runs the bot **and** its PostgreSQL database on one small Google Compute Engine VM with Docker Compose. It adds nightly backups to Google Cloud Storage. Two scripts do the work:

| Script | Runs on | Does |
|---|---|---|
| `scripts/gcp-provision.sh` | your laptop or Cloud Shell | Creates the VM, backup bucket, token secret and a least-privilege service account. Re-runnable. Refuses billed regions. |
| `scripts/vm-deploy.sh` | the VM | Host setup, settings file, token from Secret Manager, backups, start, and a connection check. Re-runnable; also used for updates. |
| `scripts/gcp-vm.sh` | your laptop or Cloud Shell | Runs deploy, update, backup-now, status or ssh on the VM for you. |

> **Want Claude to do this for you on your Windows laptop?** See [`LAPTOP_SETUP.md`](LAPTOP_SETUP.md). This page is the manual path, using only a web browser and Cloud Shell.

## What it costs

| Item | Free tier? | Expected cost |
|---|---|---|
| e2-micro VM in `us-west1`, `us-central1` or `us-east1` | Yes: one per account | $0 |
| 30 GB **standard** persistent disk | Yes: up to 30 GB standard (not "balanced") | $0 |
| Public IPv4 address (the bot needs it to reach Discord) | **No** | **≈ $3.65/month** |
| Outbound traffic | First 1 GB/month free; the bot uses far less | $0 |
| Secret Manager (1 secret, a few reads per deploy) | Small free allowance | $0 |
| Backups in Cloud Storage (a few MB) | Small free allowance in US regions | $0 to a few cents |
| **Total** | | **≈ $3.65/month** |

Free-tier rules change. Check <https://cloud.google.com/free> and set the budget alert in step 1. The provisioning script hard-codes the free-tier VM shape and disk type, and rejects other regions.

## Before you start: Discord

Do the README's [Discord Developer Portal setup](../README.md#discord-developer-portal-setup) steps 1–4 first:
- create the app;
- turn on **Server Members Intent**;
- invite the bot to your server with permissions `268553216`;
- copy the **bot token** into a password manager.

Also turn on Discord Developer Mode, then right-click your server and choose **Copy Server ID**.

---

## 1. Google Cloud account, project and budget alert

1. Go to <https://console.cloud.google.com/>, sign in, and add a billing account. A card is required for identity checks.
   - New accounts start on a **90-day free trial**. **Before day 90, click "Activate full account"** (Billing → Overview), or Google stops the VM when the trial ends.
2. Create a project: top-bar project picker → **New Project** → name `rrc-bot` → **Create**. Note the **Project ID** (e.g. `rrc-bot-123456`). Make sure it's linked to your billing account (Billing → Account management → My projects).
3. **Budget alert:** Billing → **Budgets & alerts** → **Create budget**. Set scope to project `rrc-bot`, amount to **$5**, and alerts at 50%, 90% and 100%.

## 2. Create the resources (Cloud Shell)

Open **Cloud Shell** (the `>_` icon, top right) and run:

```bash
git clone https://github.com/rejuvenationrunclub/RRC-V1.git && cd RRC-V1
git checkout claude/rejuvenation-discord-bot-ladfug      # until merged; then main
bash scripts/gcp-provision.sh YOUR_PROJECT_ID             # default zone us-west1-b (Oregon, closest to Phoenix)
```

The script finishes with a summary and a link for the next step.

## 3. Add the bot token to Secret Manager (you, in the browser)

Open the link the script printed: **Security → Secret Manager → `discord-token`**. Click **+ NEW VERSION**, paste the bot token, and click **ADD NEW VERSION**.

The token now lives only in Secret Manager. Only the VM's service account can read it. It never goes in chat, email, the repo, or any file you edit by hand.

## 4. Deploy onto the VM

Still in Cloud Shell, in the `RRC-V1` folder (replace the two values):

```bash
bash scripts/gcp-vm.sh YOUR_PROJECT_ID deploy --guild-id YOUR_DISCORD_SERVER_ID \
  --branch claude/rejuvenation-discord-bot-ladfug     # omit --branch once merged to main
```

This connects to the VM and downloads the code to `/opt/rrc-bot`. It then runs `scripts/vm-deploy.sh` there. The first run takes about 5–10 minutes: OS updates, Docker install and the image build. It ends with:

```
==> SUCCESS: Connected as RRC Bot#1234 (id=...)
==> Now type /rrc ping in your Discord server.
```

In Discord, `/rrc ping` should reply privately with **Database: ✅ connected**.

## 5. Prove backups work

```bash
bash scripts/gcp-vm.sh YOUR_PROJECT_ID backup-now
```

The log should end with `done`, and the bucket listing should show an `rrc-….dump` file. After this, backups run nightly at **03:30 Phoenix time**. The VM keeps 14 days and Cloud Storage keeps 30. The VM can add backups but not delete them.

---

## Day-to-day operations

From Cloud Shell (or any machine with `gcloud`), in the `RRC-V1` folder:

| Task | Command |
|---|---|
| Is it running? | `/rrc ping` in Discord, or `bash scripts/gcp-vm.sh PROJECT_ID status` |
| **Update to new code** | `bash scripts/gcp-vm.sh PROJECT_ID update`. This backs up first, migrates and restarts. Add `--branch …` if not deploying `main`. |
| **Rotate the Discord token** | Reset it in the Developer Portal → Secret Manager → `discord-token` → **+ NEW VERSION** → `bash scripts/gcp-vm.sh PROJECT_ID update`. Then disable the old version in Secret Manager. |
| Back up right now | `bash scripts/gcp-vm.sh PROJECT_ID backup-now` |
| Shell on the VM | `bash scripts/gcp-vm.sh PROJECT_ID ssh` (or the **SSH** button in Compute Engine) |

On the VM itself: live logs `cd /opt/rrc-bot && sudo docker compose --profile bot logs -f bot`, last backup `sudo journalctl -u rrc-backup.service -n 20 --no-pager`, disk `df -h /`.

## Restoring

From a local archive:

```bash
ls -lt /var/backups/rrc | head
sudo /opt/rrc-bot/scripts/restore.sh /var/backups/rrc/rrc-YYYYMMDDTHHMMSSZ.dump --yes
```

**If the VM itself is lost:** delete it if it still exists (`gcloud compute instances list` shows its zone; then `gcloud compute instances delete rrc-bot --zone=THAT_ZONE`). Re-run steps 2 and 4; your token is still in Secret Manager. Then restore on the VM (`bash scripts/gcp-vm.sh PROJECT_ID ssh`):

```bash
sudo /opt/rrc-bot/scripts/restore.sh gs://rrc-backups-YOUR_PROJECT_ID/rrc-YYYYMMDDTHHMMSSZ.dump --yes
```

The restore runs in a single transaction, so if it fails nothing changes. The bot is stopped during the restore and started again afterwards.

## Troubleshooting

| Message / symptom | Fix |
|---|---|
| `no Discord token in Secret Manager secret 'discord-token' yet` | Do step 3, then re-run step 4 |
| `Discord rejected the Server Members intent` | Developer Portal → Bot → enable **Server Members Intent** → re-run step 4 |
| `Discord rejected the token` | Reset the token → add it as a **new version** in Secret Manager → re-run step 4 |
| `/rrc ping` doesn't appear in Discord | Wrong server ID (re-run step 4 with the right `--guild-id`), or the bot was invited without `applications.commands`. Re-invite with the README URL. |
| Ping shows `Database: ❌ unreachable` | `sudo docker compose -f /opt/rrc-bot/docker-compose.yml ps db` should show *healthy*. Check its logs. |
| `project ... has no billing account linked` | Billing → Account management → link the project, then re-run `gcp-provision.sh` |
| `zone ... is outside the free-tier regions` | Use a zone in `us-west1`, `us-central1` or `us-east1` |
| `ZONE_RESOURCE_POOL_EXHAUSTED` when creating the VM | No e2-micro capacity in that zone. Re-run `bash scripts/gcp-provision.sh YOUR_PROJECT_ID us-west1-a` (or `us-west1-c`). `gcp-vm.sh` finds the VM's zone by itself. |
| Backup `upload ... failed` | Re-run `gcp-provision.sh`; it re-applies the bucket permissions |
| Unexpected charges | Billing → Reports. Check that only one VM exists, in a free region, with a `pd-standard` disk. |
