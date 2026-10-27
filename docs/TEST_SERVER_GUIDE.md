# Testing the RRC bot in your test server (v0.2.0)

This version adds joining, events, check-in codes, staff confirmation and profiles. Try it in your **test** server (RRC IGNITION TEST) before any real members use it.

Every scoring number is **proposed**: 10 RP per flagship run, 3 or 5 per pop-up, shields every 100 RP. They can change before launch without rebuilding the bot.

**Not in this version yet:** Hot Streak bonuses and using shields on missed weeks (they need the 2-week cycles to close, which comes next), solo-workout proof, ranks being awarded, rewards and nominations. Your profile already shows your progress toward the next rank.

---

## 1. Get the new version onto the bot

Ask Claude: *"Update the RRC bot to the `claude/phase-1-2-attendance` branch."* Claude backs up the database first, installs the new version and checks that it connects. Then type `/rrc ping` in Discord; it should say **v0.2.0**.

## 2. Prepare the server (about 5 minutes)

In Discord, as the server owner:

1. **Roles** (Server Settings → Roles → Create Role):
   - `RRC Staff`: people who run events. Give it to anyone helping you test.
   - `Initiate`: the bot gives this to everyone who joins.
   - Drag the bot's own role (**RRC Bot**) **above** `Initiate`. Discord only lets a bot hand out roles below its own.
2. **Channels:**
   - `#rrc-admin-log`: private. Only you, staff and the bot can see it. The bot writes a line here for every staff action.
   - `#upcoming-runs`: the bot announces new runs here.
3. **Let staff see the staff commands:** Server Settings → Integrations → RRC Bot → `/rrc-admin` → add the `RRC Staff` role. (By default only people with Manage Server see them. The bot double-checks roles either way.)

## 3. Run setup

Type `/rrc-admin setup` and fill in the options:

| Option | What to pick |
|---|---|
| `cycle_start` | For testing: **today**. For the real launch, the first flagship date. It's locked once runs exist. |
| `staff_role` | `RRC Staff` |
| `initiate_role` | `Initiate` |
| `admin_log` | `#rrc-admin-log` |
| `upcoming_runs` | `#upcoming-runs` |

The reply lists your settings and ends with **"Everything the bot needs looks good."** If it shows ⚠️ lines instead, fix what they say and run setup again. You can re-run it any time to change a role or channel.

## 4. Try a run from start to finish

You can play both parts yourself. A second person (or a second Discord account) makes it more realistic.

| Step | Who | Type | You should see |
|---|---|---|---|
| 1 | Staff | `/rrc-admin event create` with kind **Flagship run**, title `Test run`, date `today`, start = **10 minutes from now** (e.g. `7:10pm`) | "Flagship run **Test run** (#1) published". An announcement appears in #upcoming-runs. |
| 2 | Member | `/rrc events` | The test run, marked "check-in open now" (check-in opens 30 minutes before the start) |
| 3 | Member | `/rrc join`, then click **I agree, join RRC** | "Welcome to RRC!" and the `Initiate` role |
| 4 | Staff | `/rrc-admin event code` and pick the run | A 6-letter code with a **Refresh code** button. Codes change every minute. |
| 5 | Member | `/rrc checkin` and type the code | "You're checked in to **Test run**! Staff confirm the roster after the run…" |
| 6 | Staff | `/rrc-admin event roster` | The member under "Waiting for confirmation" |
| 7 | Staff | `/rrc-admin event finalize` | "Roster confirmed for **Test run**: 1 member(s), 10 RP awarded" |
| 8 | Member | (check DMs) | A DM from the bot: "+10 RP" |
| 9 | Member | `/rrc profile` | 10 RP, 1 shield, progress toward **Relentless** |

## 5. Things worth trying

- **A wrong code:** `/rrc checkin` with `AAAAAA`. It's refused. After 5 wrong codes in 10 minutes it asks you to wait.
- **Checking in twice:** "You're already checked in."
- **An old code:** wait 2–3 minutes after copying a code. It no longer works.
- **Someone who wasn't there:** before finalizing, `/rrc-admin attendance reject` them with a reason. They get no RP.
- **Someone whose phone died:** `/rrc-admin attendance add` adds and confirms them right away. After check-in has closed, a reason is required.
- **A pop-up:** `event create` with kind **Pop-up run** and `points` 5.
- **Two events that count as one:** create two pop-ups with the same `group` (e.g. `nov7`), like a pop-up run and a challenge held at it. A member can check in to only one of them.
- **Canceling:** `/rrc-admin event cancel` (admins only). Check-ins already made are kept.
- **Check-in times:** `/rrc-admin event open` opens check-in early; `close` ends it early.

Everything staff do is listed in #rrc-admin-log.

## 6. Found something odd?

Tell Claude what you typed and what the bot replied (a screenshot is fine). Test data stays in the database. Before the real launch we'll decide whether to wipe it (the plan says to start fresh).
