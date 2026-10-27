# Let Claude set up the RRC bot from your Windows laptop

Claude, running in the **Claude Desktop app** on your laptop, does the technical work. It installs the Google Cloud tools, creates the server and backups, deploys the bot and checks it. You handle the few steps that need your accounts, your card or your bot token.

**Your hands-on time:** about 15 minutes. **Total time:** about 30–45 minutes. **Cost:** about $3.65/month (see [DEPLOY_GCP.md](DEPLOY_GCP.md#what-it-costs)).

---

## Part 1: You do these first

### A. Discord (about 10 minutes)
Follow the README's [Discord Developer Portal setup](../README.md#discord-developer-portal-setup), steps 1–4:
1. Create the application and bot.
2. **Bot tab → Reset Token**, and save the token in your password manager. **Don't give it to Claude.** You'll paste it into a Google page yourself in Part 2.
3. Turn on **Server Members Intent**. Leave Presence and Message Content off.
4. Invite the bot to your server with the generated URL (permissions `268553216`).
5. In Discord, open User Settings → Advanced and turn on **Developer Mode**. Then right-click your server and choose **Copy Server ID**. Claude will ask you for this number; it isn't secret.

### B. Google Cloud (about 5 minutes)
1. Go to <https://console.cloud.google.com/>, sign in with the Google account that should own the bot, accept the terms and **add a billing account** (card required).
2. Click the **Cloud Shell** icon (`>_`, top right) once and accept its terms. You can close it after it opens.

### C. Hand off to Claude
Open the **Claude Desktop app**, start a **Claude Code** session in any folder (for example `Documents\rrc`), and paste:

```text
Please set up the Rejuvenation Run Club Discord bot on Google Cloud for me.
Read and follow "Part 2: Instructions for Claude" in
https://github.com/rejuvenationrunclub/RRC-V1/blob/claude/rejuvenation-discord-bot-ladfug/docs/LAPTOP_SETUP.md
exactly. Stop and ask me whenever a step says HUMAN. Never ask me for the Discord bot token.
```

Claude will stop and ask you to:
- approve a Google sign-in in your browser;
- confirm the billing account;
- paste the bot token into a Google Secret Manager page;
- give it your Discord Server ID;
- finally, type `/rrc ping` in Discord.

---

## Part 2: Instructions for Claude

You are deploying the RRC bot for a non-technical owner on a **Windows** laptop. Use the Bash tool (Git Bash) unless a step says otherwise. Keep the human informed in plain language, one short message per stage.

### Hard rules
1. **Never ask for, display, log, or store the Discord bot token.** The human adds it to Secret Manager in their browser. Never run `gcloud secrets versions access`. To check the token exists, list version *names* only.
2. **Stay inside the free-tier shape.** Use only the scripts below. Don't change the VM type or disk, and don't create additional VMs. Zones must be in `us-west1`, `us-central1` or `us-east1`; default to `us-west1-b`, then `us-west1-a` or `us-west1-c` if it has no capacity (step 6).
3. **Ask the HUMAN before** anything that deletes resources, or costs more than the documented ~$3.65/month.
4. If a step fails, read the error and fix it if the cause is clear. Otherwise explain it plainly and ask. Never work around an error by weakening security, such as opening firewall ports, making the bucket public, or granting broad IAM roles.

### Variables
- `BRANCH`: use `main` if `https://github.com/rejuvenationrunclub/RRC-V1/blob/main/scripts/vm-deploy.sh` exists; otherwise `claude/rejuvenation-discord-bot-ladfug`. Check with `git ls-remote` or by fetching that URL.
- `PROJECT_ID`, `BILLING_ACCOUNT` and `GUILD_ID` are determined in the steps below.

### Steps

**1. Google Cloud CLI.** Run `gcloud --version`. If it isn't installed:
```bash
winget install -e --id Google.CloudSDK --accept-source-agreements --accept-package-agreements
```
Bash won't see the new `PATH` until a new shell starts. Find `gcloud` under `/c/Users/$USERNAME/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/` or `/c/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/`, and call it by full path or prepend that directory to `PATH` for this session.

**2. Sign in (HUMAN).** Run `gcloud auth login`. Tell the human a browser window will open and that they should sign in with the account from Part 1B and click **Allow**. Confirm with `gcloud auth list`.

**3. Project and billing.**
- Ask the HUMAN whether they already created a project for the bot. If yes, get its ID. If no, create one: `gcloud projects create rrc-bot-$(date +%y%m%d%H%M) --name="RRC Bot"`, and use the printed ID as `PROJECT_ID`.
- Run `gcloud billing accounts list --filter=open=true`. Show the human the display name(s) and **ask them to confirm** which to use. That ID is `BILLING_ACCOUNT`.
- Link it: `gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT`.

**4. Budget alert ($5).**
```bash
gcloud services enable billingbudgets.googleapis.com --project=PROJECT_ID
gcloud billing budgets create --billing-account=BILLING_ACCOUNT --billing-project=PROJECT_ID \
  --display-name="RRC bot" --budget-amount=5USD --filter-projects=projects/PROJECT_ID \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```
`--billing-project` matters: without it the command fails with "requires a quota project" or `SERVICE_DISABLED`. If it still fails, the HUMAN can create it instead: Billing → Budgets & alerts → Create budget, project `PROJECT_ID`, $5, alerts at 50/90/100%. Ask them to do that, then continue.

**5. Get the code into Cloud Shell.** Cloud Shell is Linux, which avoids Windows shell quirks:
```bash
gcloud cloud-shell ssh --authorize-session --command \
  "{ [ -d ~/RRC-V1/.git ] || git clone -q https://github.com/rejuvenationrunclub/RRC-V1.git ~/RRC-V1; } && cd ~/RRC-V1 && git fetch -q origin && git checkout -q BRANCH && git pull -q --ff-only origin BRANCH && git log --oneline -1"
```
If `gcloud cloud-shell` is unavailable, or hangs after printing "Tunnel stopped" (seen on Windows), do steps 5–10 locally in Git Bash from a clone of the repo instead. The scripts are portable bash. On Windows, clone with long paths enabled or checkout fails on deep paths:
```bash
git -c core.longpaths=true clone https://github.com/rejuvenationrunclub/RRC-V1.git && cd RRC-V1 && git checkout -q BRANCH
```

**6. Provision the cloud resources.**
```bash
gcloud cloud-shell ssh --authorize-session --command "cd ~/RRC-V1 && bash scripts/gcp-provision.sh PROJECT_ID"
```
This creates a least-privilege service account, a private backup bucket, an empty `discord-token` secret and the e2-micro VM. It's safe to re-run.

If VM creation fails with `ZONE_RESOURCE_POOL_EXHAUSTED` (no e2-micro capacity in `us-west1-b`), re-run it with another zone as the second argument: `bash scripts/gcp-provision.sh PROJECT_ID us-west1-a` (or `us-west1-c`). `gcp-vm.sh` finds the VM's zone by itself, so later steps need no change.

**7. Bot token (HUMAN).** Send the human this link and these instructions:
> Open https://console.cloud.google.com/security/secret-manager/secret/discord-token/versions?project=PROJECT_ID. Click **+ NEW VERSION**, paste your Discord bot token, then click **ADD NEW VERSION**. Tell me when it's done. Please don't paste the token here.

Then verify without reading the value:
```bash
gcloud secrets versions list discord-token --project=PROJECT_ID --filter=state=ENABLED --format="value(name)"
```
Expect at least one version number. If the list is empty, ask them to retry.

**8. Server ID (HUMAN).** Ask for the Discord Server ID from Part 1A. It must be all digits. This is `GUILD_ID`.

**9. Deploy.**
```bash
gcloud cloud-shell ssh --authorize-session --command "cd ~/RRC-V1 && bash scripts/gcp-vm.sh PROJECT_ID deploy --guild-id GUILD_ID --branch BRANCH"
```
This takes 5–10 minutes on the first run. Success ends with `SUCCESS: Connected as …`. If it prints an `ERROR:` line, follow its instructions. The usual causes are a missing token, the Server Members Intent being off, or a bad token. Each message says exactly what the human must fix. After they fix it, re-run this step.

**10. Prove backups work.**
```bash
gcloud cloud-shell ssh --authorize-session --command "cd ~/RRC-V1 && bash scripts/gcp-vm.sh PROJECT_ID backup-now"
```
Expect the log to end in `done` and the bucket listing to show an `rrc-….dump` file.

**11. Final check (HUMAN).** Ask the human to type `/rrc ping` in their Discord server. The private reply should show `Database: ✅ connected`.

**12. Report.** Give the human a short summary:
- the project ID, VM name/zone and backup bucket;
- that backups run nightly at 03:30 Phoenix time;
- the expected cost (~$3.65/month, with the $5 budget alert);
- a reminder to click **Activate full account** in Billing before the 90-day trial ends;
- how to ask for future updates: "Update the RRC bot", which means running step 5, then `bash scripts/gcp-vm.sh PROJECT_ID update --branch BRANCH` through Cloud Shell.

### Later requests the human may make
| They say | Do |
|---|---|
| "Update the bot" | Step 5, then `bash scripts/gcp-vm.sh PROJECT_ID update --branch BRANCH` via Cloud Shell |
| "Is the bot OK?" | `bash scripts/gcp-vm.sh PROJECT_ID status` via Cloud Shell |
| "I reset the bot token" | They add a **new version** in Secret Manager (step 7). Then run `update`, then `gcloud secrets versions disable <old-version> --secret=discord-token --project=PROJECT_ID` |
| "Restore a backup" | Confirm with the human first; it replaces all bot data. Then follow [DEPLOY_GCP.md → Restoring](DEPLOY_GCP.md#restoring). |
