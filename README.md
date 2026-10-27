# RRC Bot: Rejuvenation Run Club on Discord

The Discord bot that runs RRC's recognition system. It tracks lifetime **Rejuvenation Points (RP)**, three permanent **ranks** (Initiate, Relentless, Legacy), biweekly **Hot Streaks** with **shields**, and **milestone unlocks**. All of it lives in PostgreSQL with immutable ledgers and a full audit log.

- **Design and policy decisions:** [`docs/PLAN.md`](docs/PLAN.md) (approved 2026-10-27)
- **Stack:** Python 3.12 · discord.py 2.x · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic · uv

> **Reward safety:** the scoring values seeded in this repo are **PROPOSED** (plan §8-B). Members only ever see offer terms after an admin marks the offer `live` with approved terms. Nothing in this codebase treats an unconfirmed reward as live.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 Scaffold | Config, Docker Compose, Alembic baseline, CI, tests | ✅ this release |
| 1 Rules + data | Schema, rule engine, §10 edge-case tests | next |
| 2 Attendance | Events, check-in codes, staff roster, profile | planned |
| 3 Cycles | Hot Streaks, shields, cycle close, proof review | planned |
| 4 Recognition | Ranks, milestones, offers, nominations | planned |
| 5 Pilot | Monitoring, restore drill, 2-cycle pilot (GCP guide and backups already shipped) | planned |

In Phase 0 the bot connects to your server and has one command, **`/rrc ping`**. It replies privately with the bot version, Discord latency and whether the database is reachable, which makes it a quick health check after any deploy.

---

## Quickstart (local development)

**Prerequisites:** Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker (for PostgreSQL).

```bash
cp .env.example .env            # then edit: set a local POSTGRES_PASSWORD (and DATABASE_URLs to match)
make install                    # uv sync: runtime + dev dependencies into .venv
make db-up                      # PostgreSQL 16 on 127.0.0.1:5432 (also creates <db>_test)
make migrate                    # alembic upgrade head
make check                      # ruff + mypy + pytest
```

To run the bot you also need `DISCORD_TOKEN` and `DISCORD_GUILD_ID` in `.env` (see [Discord Developer Portal setup](#discord-developer-portal-setup)):

```bash
make run                        # python -m rrc_bot
```

Without those two variables the bot refuses to start. It names the missing variable and exits with code 2. It never falls back to a default. Once you see `Connected as …` in the log, type `/rrc ping` in your server. The database is optional for this check; ping shows ❌ until it's reachable.

### Running everything in containers

```bash
docker compose --profile bot up --build
```

The `bot` service applies migrations on start (`RUN_MIGRATIONS=1`) and reaches the database at host `db`.

## Configuration

All configuration comes from environment variables (or a git-ignored `.env`). See [`.env.example`](.env.example).

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | to run the bot | **yes** | Bot token from the Developer Portal |
| `DISCORD_APPLICATION_ID` | recommended | no | Application ID (for invite URLs and diagnostics) |
| `DISCORD_GUILD_ID` | to run the bot | no | The RRC server; commands are guild-scoped |
| `DATABASE_URL` | yes | **yes** | `postgresql+asyncpg://user:pass@host:5432/db` |
| `TEST_DATABASE_URL` | for integration tests | yes | Separate database. **Tests migrate it down to empty and back up**; never point it at real data. |
| `DISPLAY_TZ` | no (default `America/Phoenix`) | no | Calendar display only; storage is always UTC |
| `LOG_LEVEL` | no (default `INFO`) | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `POSTGRES_USER/PASSWORD/DB/PORT` | Compose only | password: yes | Initialize the local container |

**Secrets policy**
- Tokens are never hard-coded. Settings hold them as `SecretStr`, so they never appear in reprs or logs. A logging filter also redacts anything shaped like a Discord token.
- `.env` is git-ignored. CI runs **gitleaks** over the full history, and a pytest check fails if anything token-shaped is committed.
- In production, set variables in the host's secret manager, not in a file baked into the image. `.dockerignore` excludes `.env`.

## Tests

```bash
make test-unit                  # no database needed
make test                       # unit + integration (TEST_DATABASE_URL from env or .env)
```

Integration tests run against real PostgreSQL. Without `TEST_DATABASE_URL` they are **skipped** locally. CI sets `RRC_REQUIRE_DB=1`, which makes a missing database a **failure** instead.

## Database migrations

```bash
uv run alembic upgrade head     # apply
uv run alembic downgrade -1     # roll back one
uv run alembic revision -m "describe change" --autogenerate   # new migration (review it!)
uv run alembic check            # fails if models and migrations disagree
```

Alembic reads `DATABASE_URL` from the environment; no URL is stored in `alembic.ini`. The baseline migration installs `btree_gist` and the `rrc_forbid_mutation()` trigger function. Phase 1 attaches it to every append-only table (RP ledger, shield ledger, audit log, unlocks, promotions), so history can only be corrected with compensating rows.

---

## Discord Developer Portal setup

Do this once per environment (use a **private test server** first). Each step lists exactly what to click.

### 1. Create the application
1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it (e.g. `RRC Bot`) and accept the terms.
2. On **General Information**, copy the **Application ID** into `DISCORD_APPLICATION_ID`. It is not secret. You do **not** need the Public Key, because this bot uses the gateway, not an HTTP interactions endpoint.

### 2. Installation tab
1. **Installation Contexts:** enable **Guild Install** only. Uncheck **User Install**.
2. **Install Link:** choose **None**. We share the URL generated in step 4 directly.

### 3. Bot tab
1. Click **Reset Token**, then copy the token **once** directly into your secret manager or local `.env` as `DISCORD_TOKEN`. Don't paste it in chat, issues, code or commits. If it is ever exposed, click Reset Token again right away; the old one stops working.
2. **Public Bot:** **OFF** (only the application owner can add it to servers).
3. **Requires OAuth2 Code Grant:** **OFF**.
4. **Privileged Gateway Intents:**

   | Intent | Setting | Why |
   |---|---|---|
   | Presence Intent | **OFF** | not used |
   | Server Members Intent | **ON** | detect members leaving and rejoining, and re-apply rank roles |
   | Message Content Intent | **OFF** | the bot never reads messages; it uses slash commands and modals only |

   (Bots in fewer than 100 servers can toggle these without Discord verification.)

### 4. OAuth2 → URL Generator
1. **Scopes:** `bot` and `applications.commands`.
2. **Bot Permissions:** select **only** these:

   | Permission | Why it's needed |
   |---|---|
   | View Channels | see the configured RRC channels |
   | Send Messages | announcements, proof review cards, the admin log |
   | Embed Links | profile, event and review embeds |
   | Attach Files | re-upload proof photos to the private review channel; data exports |
   | Read Message History | re-fetch and edit its own review cards after a restart |
   | Manage Roles | assign Initiate, Relentless, Legacy and Hot Streak roles |

   The resulting **permissions integer is `268553216`**. **Not** requested: Administrator, Manage Server, Manage Messages, Manage Channels, Mention Everyone, Kick/Ban. *(Optional later: Create Events, only if we decide to mirror RRC events as Discord Scheduled Events.)*
3. Open the generated URL, pick your **test server** and click **Authorize**. The equivalent URL is:
   `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=268553216`

### 5. Server configuration
1. **Roles** (Server Settings → Roles): create `Initiate`, `Relentless`, `Legacy` and `Hot Streak`, plus staff roles `RRC Admin`, `RRC Staff`, `RRC Moderator` and `RRC Partnerships`.
   **Drag the bot's role above** the four rank/streak roles. Discord only lets a bot manage roles below its own highest role. The bot does not need to be above the staff roles.
2. **Channels:** create `#start-here`, `#announcements`, `#upcoming-runs`, `#progress`, `#community-spotlights` and `#rewards`, plus the staff-only `#rrc-admin-log` and `#rrc-proof-review`.
   - For the two staff-only channels, deny **View Channel** to `@everyone` and allow it for the staff roles **and the bot**.
   - The bot needs View Channel, Send Messages, Embed Links and Attach Files in each channel it posts to.
3. **Command permissions** (Server Settings → Integrations → RRC Bot): restrict `/rrc-admin` to the staff roles. This is defense in depth; the bot re-checks roles on every admin action regardless.

### 6. Collect IDs
In Discord, open User Settings → Advanced and turn on **Developer Mode**. Then right-click to **Copy ID** for the server (put it in `DISCORD_GUILD_ID`), each role and each channel. From Phase 2 onward these are configured in-app with `/rrc-admin setup`, which also verifies the bot's permissions and role position.

### 7. Token rotation runbook
1. Developer Portal → Bot → **Reset Token**.
2. Update `DISCORD_TOKEN` in the secret manager (or `.env`).
3. Restart the bot. Confirm the log shows `Connected as …`.
4. Record the rotation (who, when, why) in `#rrc-admin-log`.

---

## Deployment

**Step-by-step Google Cloud guide: [`docs/DEPLOY_GCP.md`](docs/DEPLOY_GCP.md).** It runs the bot and PostgreSQL on one e2-micro VM for about $3.65/month (the public IPv4 address) and includes nightly off-site backups. The bot token lives in Google Secret Manager, never in a file you edit.

**Rather have Claude do it from your Windows laptop?** Follow [`docs/LAPTOP_SETUP.md`](docs/LAPTOP_SETUP.md). You do the sign-ins and paste the token into Google yourself; Claude does the rest.

General requirements for any host:


- **Shape:** one always-on container (the Discord gateway is a persistent websocket), run as **a single replica**, plus PostgreSQL 16 (on the same VM via Compose, or managed).
- **Image:** `docker build -t rrc-bot .` produces a slim, non-root runtime image without dev tools.
- **Secrets:** set `DISCORD_TOKEN`, `DISCORD_GUILD_ID` and `DATABASE_URL` in the platform's secret store.
- **Migrations:** set `RUN_MIGRATIONS=1` to apply on start, or run `alembic upgrade head` as a release step.
- **Database role:** the app's role must own its database (or hold CREATE on it) so the baseline migration can create the trusted `btree_gist` extension. Superuser is not required.
- **Backups:** see [Backups and restore](#backups-and-restore).

## Backups and restore

`scripts/backup.sh` writes a compressed `pg_dump` archive, **verifies it** with `pg_restore --list`, keeps it owner-readable only, optionally copies it to Google Cloud Storage, and prunes local archives older than `RETENTION_DAYS`. If any step fails it exits non-zero.

```bash
sudo scripts/backup.sh                                  # dump the compose "db" service to /var/backups/rrc
sudo GCS_BUCKET=gs://my-rrc-backups scripts/backup.sh   # ...and keep an off-site copy
```

To run it nightly at 03:30 Phoenix time (10:30 UTC) with systemd, assuming the repo is at `/opt/rrc-bot`:

```bash
sudo cp deploy/systemd/rrc-backup.service deploy/systemd/rrc-backup.timer /etc/systemd/system/
echo 'GCS_BUCKET=gs://my-rrc-backups' | sudo tee /etc/rrc-backup.env   # optional
sudo systemctl daemon-reload && sudo systemctl enable --now rrc-backup.timer
systemctl list-timers rrc-backup.timer       # next run
journalctl -u rrc-backup.service             # last run's log
```

**Restore** replaces all bot data with the archive's contents. It runs in one transaction, so a failed restore changes nothing. It stops the bot during the restore and restarts it afterwards if it was running.

```bash
sudo scripts/restore.sh /var/backups/rrc/rrc-20261027T103000Z.dump --yes
sudo scripts/restore.sh gs://my-rrc-backups/rrc-20261027T103000Z.dump --yes   # e.g. onto a new VM
```

Both scripts default to the Docker Compose database. Set `BACKUP_SOURCE=direct` to use the host's `pg_dump`/`pg_restore` with the standard `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` variables, e.g. for a managed database. The integration tests run a full backup → disaster → restore round trip.

## Project layout

```
rrc_bot/
  config.py          environment-only settings (SecretStr for secrets)
  logging_setup.py   logging + token redaction
  bot/client.py      Discord client, least-privilege intents, guild command sync
  bot/commands/      slash-command groups (/rrc ping)
  db/                engine/session (UTC), declarative base, health probe
migrations/          Alembic (baseline: btree_gist + append-only trigger function)
tests/               unit/, integration/ (real Postgres), repo hygiene
docker/              entrypoint, Postgres init (creates the test DB)
scripts/             backup.sh, restore.sh; Google Cloud: gcp-provision.sh (laptop/Cloud Shell),
                     gcp-vm.sh (remote ops), vm-deploy.sh + gcp-vm-bootstrap.sh (on the VM)
deploy/              systemd backup timer, Cloud Storage lifecycle rule
docs/PLAN.md         approved architecture, schema, state diagrams, policy decisions
docs/DEPLOY_GCP.md   Google Cloud deployment, operations and restore runbook
docs/LAPTOP_SETUP.md hand-off for Claude on a Windows laptop to deploy it for you
```
