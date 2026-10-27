# Deploying the RRC bot on Google Cloud (e2-micro)

This guide runs the bot **and** its PostgreSQL database on one small Google Compute Engine VM with Docker Compose. It adds nightly backups to Google Cloud Storage. Plan for about 45 minutes the first time.

## What it costs

| Item | Free tier? | Expected cost |
|---|---|---|
| e2-micro VM in `us-west1`, `us-central1` or `us-east1` | Yes: one per account | $0 |
| 30 GB **standard** persistent disk | Yes: up to 30 GB standard (not "balanced") | $0 |
| Public IPv4 address (the bot needs it to reach Discord) | **No** | **≈ $3.65/month** |
| Outbound traffic | First 1 GB/month free; the bot uses far less | $0 |
| Backups in Cloud Storage (a few MB) | Small free allowance in US regions | $0 to a few cents |
| **Total** | | **≈ $3.65/month** |

Free-tier rules change. Check <https://cloud.google.com/free> before you start, and set the budget alert in step 1 so a surprise never goes unnoticed.

**Two settings that quietly cost money if you get them wrong:** a region outside the three listed, or the console's default **balanced** disk. The commands below set both correctly.

---

## 1. Google Cloud account, project and budget alert

1. Go to <https://console.cloud.google.com/> and sign in. Accept the terms, and add a billing account (a card is required for identity checks).
   - New accounts start on a **90-day free trial with credits**. **Before day 90, click "Activate full account"** (Billing → Overview). Otherwise Google stops the VM when the trial ends. Always Free usage stays free after activation.
2. Create a project: click the top-bar project picker, then **New Project**, name it `rrc-bot` and click **Create**. Note the **Project ID** (e.g. `rrc-bot-123456`).
3. Budget alert: open **Billing → Budgets & alerts → Create budget**. Choose scope: project `rrc-bot`; amount: **$5**; alert thresholds: 50%, 90%, 100%. Click **Finish**. You'll be emailed if spending heads above the expected ~$3.65.

## 2. Create the VM (copy-paste in Cloud Shell)

Click the **Cloud Shell** icon (`>_`, top right of the console). A terminal opens in your browser, already logged in. Paste:

```bash
gcloud config set project YOUR_PROJECT_ID        # e.g. rrc-bot-123456
gcloud services enable compute.googleapis.com storage.googleapis.com

gcloud compute instances create rrc-bot \
  --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard \
  --scopes=storage-rw,logging-write,monitoring-write \
  --metadata=enable-oslogin=TRUE
```

- `us-west1` (Oregon) is the free-tier region closest to Phoenix.
- `pd-standard` keeps the disk free. The console defaults to "balanced", which is billed.
- `storage-rw` lets the VM upload backups to Cloud Storage.
- No inbound ports are opened beyond Google's default SSH rule. The bot only makes **outbound** connections to Discord, and PostgreSQL listens on localhost only.

*(Optional hardening)* Remove the default rule that allows Windows Remote Desktop from the internet: `gcloud compute firewall-rules delete default-allow-rdp --quiet`.

## 3. Backup bucket (still in Cloud Shell)

Bucket names are global, so this one includes your project ID:

```bash
PROJECT_ID=$(gcloud config get-value project)
BUCKET=gs://rrc-backups-$PROJECT_ID

gcloud storage buckets create "$BUCKET" --location=us-west1 \
  --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention

# Delete backups older than 30 days automatically
cat > /tmp/lifecycle.json <<'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}
EOF
gcloud storage buckets update "$BUCKET" --lifecycle-file=/tmp/lifecycle.json

# Let the VM write and read backups (but not delete them)
SA=$(gcloud compute instances describe rrc-bot --zone=us-west1-b \
      --format='value(serviceAccounts[0].email)')
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:$SA" --role=roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:$SA" --role=roles/storage.objectViewer

echo "Your bucket: $BUCKET"      # write this down for step 8
```

## 4. Connect to the VM

Console → **Compute Engine → VM instances** → on the `rrc-bot` row click **SSH**. A terminal on the VM opens in a new window. (Or from Cloud Shell: `gcloud compute ssh rrc-bot --zone=us-west1-b`.)

**Everything from here on runs on the VM.**

## 5. Get the code and run the one-time setup

```bash
sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/rrc-bot && sudo chown "$USER" /opt/rrc-bot
```

**If the GitHub repository is private** (most likely), give the VM a read-only deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rrc_deploy -N "" -C "rrc-bot VM"
cat ~/.ssh/rrc_deploy.pub
```

Copy the line that starts with `ssh-ed25519`. On GitHub, open the repo, then **Settings → Deploy keys → Add deploy key**. Title it `rrc-bot VM`, paste the key, leave **Allow write access unchecked** and click **Add key**. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/rrc_deploy
  IdentitiesOnly yes
EOF
git clone git@github.com:rejuvenationrunclub/RRC-V1.git /opt/rrc-bot
```

(If the repo is public, use `git clone https://github.com/rejuvenationrunclub/RRC-V1.git /opt/rrc-bot` instead.)

```bash
cd /opt/rrc-bot
git checkout claude/rejuvenation-discord-bot-ladfug   # until this work is merged; then use main
sudo scripts/gcp-vm-bootstrap.sh
```

The bootstrap script takes a few minutes. It installs OS updates and automatic security updates, a 1 GB swap file, Docker and Compose, Docker log rotation and the backup folder. It is safe to re-run.

## 6. Create the secrets file

```bash
cd /opt/rrc-bot
cp .env.example .env && chmod 600 .env
PW=$(openssl rand -hex 24)
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$PW/;
        s#^DATABASE_URL=.*#DATABASE_URL=postgresql+asyncpg://rrc:$PW@localhost:5432/rrc#;
        s#^TEST_DATABASE_URL=.*#TEST_DATABASE_URL=#" .env
unset PW
nano .env
```

In `nano`, fill in `DISCORD_TOKEN=` (from the Developer Portal, see the README) and `DISCORD_GUILD_ID=` (right-click your server → Copy Server ID). Optionally fill in `DISCORD_APPLICATION_ID=`. Save with **Ctrl+O, Enter** and exit with **Ctrl+X**.

The token now lives only in this file, readable only by you and root. Never commit it, paste it in chat, or copy it into the repo.

## 7. Start the bot

```bash
sudo docker compose --profile bot up -d --build      # first build takes ~3–5 min on an e2-micro
sudo docker compose logs -f bot                      # Ctrl+C to stop watching (the bot keeps running)
```

Look for `Connected as RRC Bot#…`. Then, in your Discord server, type **`/rrc ping`**. You should get a private reply:

> 🏃 Pong! RRC Bot v0.1.0 is online.
> • Discord latency: 40 ms
> • Database: ✅ connected

Both containers restart on their own after a crash or VM reboot. Test it once with `sudo reboot`, wait a minute, and `/rrc ping` again.

## 8. Nightly backups

```bash
cd /opt/rrc-bot
sudo cp deploy/systemd/rrc-backup.service deploy/systemd/rrc-backup.timer /etc/systemd/system/
echo 'GCS_BUCKET=gs://rrc-backups-YOUR_PROJECT_ID' | sudo tee /etc/rrc-backup.env
sudo systemctl daemon-reload
sudo systemctl enable --now rrc-backup.timer

# Run one now to prove it works:
sudo systemctl start rrc-backup.service
journalctl -u rrc-backup.service -n 20 --no-pager     # expect "wrote ..." then "done"
gcloud storage ls gs://rrc-backups-YOUR_PROJECT_ID/   # the archive should be listed
```

Backups run every night at **03:30 Phoenix time**. The VM keeps 14 days locally and Cloud Storage keeps 30 days. Check the schedule with `systemctl list-timers rrc-backup.timer`.

---

## Day-to-day operations

| Task | Command (on the VM, in `/opt/rrc-bot`) |
|---|---|
| Is it running? | `sudo docker compose --profile bot ps`, or `/rrc ping` in Discord |
| Live logs | `sudo docker compose logs -f bot` |
| Restart the bot | `sudo docker compose --profile bot restart bot` |
| **Update to new code** | `sudo scripts/backup.sh && git pull && sudo docker compose --profile bot up -d --build`. Migrations apply automatically on start. |
| Rotate the Discord token | Reset in the Developer Portal → `nano .env` → `sudo docker compose --profile bot up -d --force-recreate bot` |
| Last backup result | `journalctl -u rrc-backup.service -n 20 --no-pager` |
| Disk space | `df -h /` and `sudo docker system df` |

## Restoring

From a local archive:

```bash
ls -lt /var/backups/rrc | head
sudo scripts/restore.sh /var/backups/rrc/rrc-YYYYMMDDTHHMMSSZ.dump --yes
```

**If the VM itself is lost:** repeat steps 2 and 4–7 on a new VM. Then:

```bash
gcloud storage ls gs://rrc-backups-YOUR_PROJECT_ID/
sudo scripts/restore.sh gs://rrc-backups-YOUR_PROJECT_ID/rrc-YYYYMMDDTHHMMSSZ.dump --yes
```

The restore runs in a single transaction, so if it fails, nothing changes. The bot is stopped during the restore and started again afterwards.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Log says `Missing required environment variable(s)` | `DISCORD_TOKEN` or `DISCORD_GUILD_ID` is blank in `.env` |
| `Improper token has been passed` | The token was mistyped or reset. Copy a fresh one into `.env` and restart the bot. |
| `/rrc ping` doesn't appear in Discord | Wrong `DISCORD_GUILD_ID`, or the bot was invited without the `applications.commands` scope. Re-invite with the README URL. |
| Ping shows `Database: ❌ unreachable` | `sudo docker compose ps db` should show *healthy*. Check `sudo docker compose logs db`. |
| Log warns `not a member of configured guild` | Invite the bot with the OAuth2 URL from the README |
| Backup fails with `upload ... failed` | The VM's scopes or bucket permissions from steps 2–3 are missing. Re-run the two `add-iam-policy-binding` commands. |
| Unexpected charges | Billing → Reports. Check the region, disk type (`pd-standard`) and that only one VM exists. |
