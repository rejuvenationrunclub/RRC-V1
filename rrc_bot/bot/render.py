"""Message text for every command. Pure functions, so wording is unit-tested."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rrc_bot.domain.ranks import RANK_TITLES
from rrc_bot.domain.timeutil import cycle_bounds, cycle_seq, format_local
from rrc_bot.services.attendance import FinalizeResult, RosterEntry, VerifyResult
from rrc_bot.services.checkin import CheckinResult
from rrc_bot.services.common import GuildSettings
from rrc_bot.services.events import CodeView, EventView
from rrc_bot.services.members import JoinResult
from rrc_bot.services.profile import Profile

DISCORD_LIMIT = 1900  # leave headroom under Discord's 2000-character message limit


def clip(text: str) -> str:
    return text if len(text) <= DISCORD_LIMIT else text[: DISCORD_LIMIT - 1] + "…"


def when(instant: datetime, tz: ZoneInfo) -> str:
    return format_local(instant, tz)


def when_clock(instant: datetime, tz: ZoneInfo) -> str:
    return format_local(instant, tz, with_date=False)


def event_line(e: EventView, tz: ZoneInfo, now: datetime) -> str:
    where = f" · {e.location}" if e.location else ""
    open_now = " · ✅ **check-in open now**" if e.accepting_codes(now) else ""
    return (
        f"**{e.title}** ({e.type_name}, {e.base_rp} RP) · {when(e.starts_at, tz)}{where}{open_now}"
    )


def event_choice(e: EventView, tz: ZoneInfo) -> str:
    """Autocomplete label (max 100 chars)."""
    label = f"#{e.id} {e.title} · {when(e.starts_at, tz)} · {e.status.replace('_', ' ')}"
    return label if len(label) <= 100 else label[:99] + "…"


def join_prompt(settings: GuildSettings) -> str:
    shields = settings.rules.shields.enrollment_grant
    # "Coming soon" names features only; never terms, prices or how spending works
    # (plan §0: recognition first, never over-promise rewards).
    return (
        "**Join the Rejuvenation Run Club**\n"
        "🏃 Earn **Rejuvenation Points (RP)** for every run you attend. Staff confirm who "
        "was there, then your points arrive.\n"
        "🏅 Climb permanent ranks: **Initiate → Relentless → Legacy**.\n"
        f"🛡️ You start with {shields} streak shield{'' if shields == 1 else 's'}.\n\n"
        "**Coming soon** 🚧\n"
        "🛒 **RRC Marketplace**: put your points to use on gear, perks and partner offers\n"
        f"🔥 **Hot Streaks**: show up {settings.rules.streak.activation_cycles} cycles in a row "
        "to earn bonus points\n"
        "💪 **Solo workouts**: log training on your own for points\n"
        "🎉 **Milestone rewards** and member spotlights\n"
        "_We'll share the details before each one goes live._\n\n"
        "**Privacy:** the bot stores your Discord ID, your check-ins and your points to run "
        "the program. Only you and RRC staff see your records. Ask an admin any time to "
        "export or remove your data.\n\n"
        "_Point values may be adjusted during the beta._"
    )


def joined(result: JoinResult, settings: GuildSettings) -> str:
    if not result.created:
        return (
            f"You're already a member (since {when(result.enrolled_at, settings.tz)}). "
            "See your numbers with `/rrc profile`."
        )
    return (
        "🎉 **Welcome to RRC!** You're an **Initiate** with "
        f"🛡️ {result.shield_balance} shield. At a run, check in with `/rrc checkin` and the "
        "code staff show you. See upcoming runs with `/rrc events`."
    )


def events_list(events: list[EventView], settings: GuildSettings, now: datetime) -> str:
    lines = ["**Upcoming RRC runs** (Phoenix time)"]
    lines += [f"• {event_line(e, settings.tz, now)}" for e in events] or [
        "• Nothing scheduled yet."
    ]
    lines.append("\n" + cycle_line(settings, now))
    return clip("\n".join(lines))


def cycle_line(settings: GuildSettings, now: datetime) -> str:
    """Where we are in the cycle calendar, naming days rather than midnights."""
    length = settings.rules.cycle_length_days
    seq = cycle_seq(now, settings.anchor_day, length, settings.tz)
    if seq < 0:
        first, _ = cycle_bounds(0, settings.anchor_day, length, settings.tz)
        return f"Cycle 1 starts **{day_label(first, settings.tz)}**."
    _, ends = cycle_bounds(seq, settings.anchor_day, length, settings.tz)
    last_day = day_label(ends - timedelta(seconds=1), settings.tz)
    return f"This cycle (cycle {seq + 1}) runs through **{last_day}**."


def day_label(instant: datetime, tz: ZoneInfo) -> str:
    """``Fri Nov 6`` in ``tz``."""
    local = instant.astimezone(tz)
    return f"{local:%a} {local:%b} {local.day}"


def checkin_reply(result: CheckinResult, settings: GuildSettings) -> str:
    title = f"**{result.event.title}**" if result.event else "the run"
    match result.outcome:
        case "pending":
            return (
                f"✅ You're checked in to {title}! Staff confirm the roster after the run, "
                "and then your RP arrives (you'll get a DM)."
            )
        case "verified":
            return f"✅ Checked in to {title}: **+{result.rp_awarded} RP**!"
        case "already":
            return f"👍 You're already checked in to {title}."
        case "group_taken":
            return f"👍 You're already checked in to an activity that counts together with {title}."
        case "bad_code":
            return (
                "❌ That code isn't valid right now. Codes change every minute, so ask staff "
                "for the current one. Check-in opens 30 minutes before a run."
            )
        case "rate_limited":
            return "⏳ Too many wrong codes. Please wait 10 minutes, or ask staff to add you."
        case _:
            return "👍 Already handled."


def profile_text(p: Profile, settings: GuildSettings) -> str:
    rank = RANK_TITLES.get(p.rank, p.rank)
    lines = [
        "**Your RRC profile**",
        f"🏅 Rank: **{rank}** · member since {when(p.enrolled_at, settings.tz).split(',')[0]}",
        f"⭐ Lifetime: **{p.lifetime_rp} RP**",
        f"🔥 Hot Streak: {p.streak_count}/{p.activation_cycles} cycles"
        + (" · **HOT** (1.5x on your first run each cycle)" if p.hot else "")
        + "  _(streaks start counting in a later update)_",
        f"🛡️ Shields: {p.shields}/{p.shield_cap}",
        f"🏃 In-person runs: {p.inperson_runs}",
    ]
    if p.pending_checkins:
        lines.append(f"⏳ Check-ins waiting for staff: {p.pending_checkins}")
    if p.next_rank is not None:
        lines.append(f"\n**Next rank: {RANK_TITLES[p.next_rank.rank]}**")
        lines += [
            f"• {c.label}: {min(c.have, c.need)}/{c.need}{' ✅' if c.met else ''}"
            for c in p.next_rank.criteria
        ]
    else:
        lines.append("\n**You've reached Legacy, the highest rank. 🏆**")
    if p.recent:
        lines.append("\n**Recent points**")
        lines += [
            f"• {'+' if r.amount > 0 else ''}{r.amount} RP · {r.label} · "
            f"{when(r.at, settings.tz).split(',')[0]}"
            for r in p.recent
        ]
    return clip("\n".join(lines))


def event_created(e: EventView, settings: GuildSettings) -> str:
    status = "published" if e.status == "scheduled" else "saved as a draft (publish it when ready)"
    group = f"\nActivity group: `{e.group_key}`" if e.group_key else ""
    return (
        f"✅ {e.type_name} **{e.title}** (#{e.id}) {status}.\n"
        f"Starts {when(e.starts_at, settings.tz)} · {e.base_rp} RP\n"
        f"Check-in window: {when(e.checkin_opens_at, settings.tz)} to "
        f"{format_local(e.checkin_closes_at, settings.tz, with_date=False)}{group}\n"
        "At the run, show members the code from `/rrc-admin event code`."
    )


def event_status(e: EventView, settings: GuildSettings, now: datetime) -> str:
    state = e.status.replace("_", " ")
    window = (
        "✅ accepting codes now"
        if e.accepting_codes(now)
        else f"check-in window {when(e.checkin_opens_at, settings.tz)} to "
        f"{format_local(e.checkin_closes_at, settings.tz, with_date=False)}"
    )
    return f"**{e.title}** (#{e.id}) is now **{state}** · {window}"


def code_text(c: CodeView, settings: GuildSettings, now: datetime) -> str:
    e = c.event
    lines = [
        f"## Check-in code: `{c.code}`",
        f"**{e.title}** · changes in {c.seconds_left}s (the previous code also works for "
        f"{c.period_s}s)",
        "Members type `/rrc checkin` and enter this code.",
    ]
    if not e.accepting_codes(now):
        if now < e.checkin_opens_at:
            lines.append(
                f"⚠️ Check-in opens at {when_clock(e.checkin_opens_at, settings.tz)}. "
                "Use `/rrc-admin event open` to open it now."
            )
        else:
            lines.append(
                "⚠️ The check-in window has closed. Use `/rrc-admin event open` to reopen it."
            )
    return "\n".join(lines)


def roster_text(e: EventView, entries: list[RosterEntry], settings: GuildSettings) -> str:
    groups: dict[str, list[RosterEntry]] = {
        "pending": [],
        "verified": [],
        "rejected": [],
        "reversed": [],
    }
    for entry in entries:
        groups.setdefault(entry.status, []).append(entry)
    lines = [f"**Roster: {e.title}** (#{e.id}, {e.status.replace('_', ' ')})"]
    labels = {
        "pending": "⏳ Waiting for confirmation",
        "verified": "✅ Confirmed",
        "rejected": "❌ Rejected",
        "reversed": "↩️ Reversed",
    }
    for status, label in labels.items():
        members = groups.get(status, [])
        if not members:
            continue
        lines.append(f"\n{label} ({len(members)})")
        lines += [
            f"• <@{m.discord_user_id}> · {when_clock(m.checked_in_at, settings.tz)}"
            + (" · late" if m.is_late else "")
            + ("" if m.method == "code" else " · added by staff")
            for m in members
        ]
    if not entries:
        lines.append("Nobody has checked in yet.")
    if groups["pending"]:
        lines.append(
            "\nReject anyone who wasn't there with `/rrc-admin attendance reject`, then confirm "
            "everyone else with `/rrc-admin event finalize`."
        )
    return clip("\n".join(lines))


def finalized(r: FinalizeResult) -> str:
    return (
        f"✅ Roster confirmed for **{r.event.title}**: {r.verified} member(s), "
        f"{r.rp_awarded} RP awarded. Members get a DM."
    )


def added(r: VerifyResult) -> str:
    shield = " They earned a shield 🛡️." if r.shields_gained else ""
    return (
        f"✅ Added <@{r.discord_user_id}>: **+{r.rp_awarded} RP** "
        f"(lifetime {r.lifetime_rp} RP).{shield}"
    )


def setup_text(settings: GuildSettings, warnings: list[str]) -> str:
    def role(key: str) -> str:
        rid = settings.role_ids.get(key)
        return f"<@&{rid}>" if rid else "_not set_"

    def channel(key: str) -> str:
        cid = settings.channel_ids.get(key)
        return f"<#{cid}>" if cid else "_not set_"

    start, _ = cycle_bounds(0, settings.anchor_day, settings.rules.cycle_length_days, settings.tz)
    lines = [
        "**RRC bot setup**",
        f"• Cycle 1 starts: {when(start, settings.tz)} (cycles are "
        f"{settings.rules.cycle_length_days} days)",
        f"• Rules: v{settings.rule_set_version}, {settings.rules.label}",
        f"• Admin role: {role('rrc_admin')} · Staff role: {role('rrc_staff')} · "
        f"Initiate role: {role('initiate')}",
        f"• Admin log: {channel('admin_log')} · Upcoming runs: {channel('upcoming_runs')}",
    ]
    if warnings:
        lines.append("\n**Please fix:**")
        lines += [f"⚠️ {w}" for w in warnings]
    else:
        lines.append("\n✅ Everything the bot needs looks good.")
    return clip("\n".join(lines))
