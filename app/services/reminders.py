"""Deterministic reminder engine.

`plan()` is a pure function: given an item, the current time and the reminder keys already sent, it
returns what to send now. `run_reminders()` executes plans. Every reminder has a unique key logged in
`reminder_log`, so running the tick twice never double-sends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Item, ReminderLog
from app.services import messages, notifier, routines
from app.timeutil import local, to_utc_naive, utcnow

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "asked", "interested", "registered", "active")


@dataclass
class Action:
    keys: list[str]
    text: str = ""
    buttons: list[tuple[str, str]] | None = None
    urgent: bool = False
    new_status: str | None = None
    clear_snooze: bool = False
    extra: dict = field(default_factory=dict)


def _offset_keys(anchor: datetime, offsets: list[float], created: datetime, now: datetime, prefix: str) -> list[str]:
    """Keys of offset reminders whose fire time has arrived (and came after the item was created)."""
    keys = []
    for off in offsets:
        fire = anchor - timedelta(hours=off)
        if created < fire <= now < anchor:
            keys.append(f"{prefix}{off:g}")
    return keys


def _nag_key(item: Item, now: datetime) -> str | None:
    s = get_settings()
    now_l = local(now)
    if now_l.hour < s.event_nag_hour:
        return None
    nag_time = now_l.replace(hour=s.event_nag_hour, minute=0, second=0, microsecond=0)
    if local(item.created_at) >= nag_time:
        return None  # created after today's nag slot: first nag tomorrow
    if item.last_nag_at and now - item.last_nag_at < timedelta(hours=6):
        return None  # a registration reminder went out recently
    return f"nag{now_l.date().isoformat()}"


def plan_routine(item: Item, now: datetime, sent: set[str]) -> list[Action]:
    """Reminders the user set up in chat. At most one message per tick; missed slots collapse into one."""
    sch = item.schedule or {}
    if item.status != "active" or not (sch.get("start") or sch.get("times")):
        return []
    now_l = local(now)
    today = now_l.date()
    one_off = routines.is_one_off(sch)
    single = int(sch.get("every") or 0) <= 0
    if one_off and today.isoformat() > sch.get("date", "") and not item.snoozed_until:
        return [Action([], new_status="expired")]
    running = routines.runs_on(sch, today) and routines.done_key(today) not in sent
    created_l = local(item.created_at or now)
    due = [t for t in routines.day_slots(sch, today) if created_l < t <= now_l] if running else []
    nxt = routines.next_slot(sch, now, item.created_at, sent)
    extra = {"scheduled": True, "expires_at": to_utc_naive(nxt) if nxt else now + timedelta(hours=3)}
    buttons = messages.routine_buttons(item)

    if item.snoozed_until:
        if now < item.snoozed_until:
            return []
        key = f"snz{item.snoozed_until.isoformat()}"
        keys = [key] + ([routines.slot_key(due[-1])] if due else [])  # the snooze replaces the current slot
        if key in sent or not (running or one_off):
            return [Action([], clear_snooze=True)]
        return [Action([k for k in keys if k not in sent], messages.routine_reminder(item, None, nxt), buttons,
                       clear_snooze=True, new_status="done" if one_off and single and nxt is None else None,
                       extra=extra)]

    if not due or routines.slot_key(due[-1]) in sent:
        return []
    return [Action([routines.slot_key(due[-1])], messages.routine_reminder(item, len(due), nxt), buttons,
                   new_status="done" if one_off and single and nxt is None else None, extra=extra)]


def _placement_nag(item: Item, now: datetime, sent: set[str]) -> tuple[str | None, bool]:
    """Placement items also get nagged at fixed hours every day (PLACEMENT_NAG_HOURS) until handled.
    -> (key, send). A slot right after another reminder is only marked as used (send=False)."""
    now_l = local(now)
    due_hours = [h for h in get_settings().placement_nag_hour_list if h <= now_l.hour]
    if not due_hours:
        return None, False
    slot = now_l.replace(hour=due_hours[-1], minute=0, second=0, microsecond=0)
    key = f"pn{slot.strftime('%Y-%m-%dT%H')}"
    if key in sent or local(item.created_at) >= slot or now_l - slot > timedelta(hours=2):
        return None, False
    recent = item.last_nag_at and now - item.last_nag_at < timedelta(minutes=90)
    return key, not recent


def plan(item: Item, now: datetime, sent: set[str]) -> list[Action]:
    s = get_settings()
    out: list[Action] = []
    if item.kind == "routine":
        return plan_routine(item, now, sent)

    def fresh(keys: list[str]) -> list[str]:
        return [k for k in keys if k not in sent]

    # --- snooze ---------------------------------------------------------
    if item.snoozed_until:
        if now < item.snoozed_until:
            return []
        key = f"snz{item.snoozed_until.isoformat()}"
        if key not in sent:
            if item.kind == "deadline" and item.status == "pending" and item.due_at and now < item.due_at:
                return [Action([key], messages.deadline_reminder(item), messages.deadline_buttons(item), clear_snooze=True)]
            if item.kind == "event" and item.status == "interested":
                return [Action([key], messages.registration_reminder(item), messages.registration_buttons(item), clear_snooze=True)]
            if item.kind == "event" and item.status == "asked":
                return [Action([key], messages.reask_interest(item), messages.event_interest_buttons(item), clear_snooze=True)]
        out.append(Action([], clear_snooze=True))

    # --- deadlines ------------------------------------------------------
    if item.kind == "deadline" and item.status == "pending" and item.due_at:
        if now >= item.due_at:
            return out + [Action(["passed"], messages.deadline_passed(item), [(f"done:{item.id}", "✅ I did it")], new_status="expired")]
        offsets = s.placement_deadline_offsets if item.placement else s.deadline_offsets
        keys = fresh(_offset_keys(item.due_at, offsets, item.created_at, now, "d"))
        urgent = (item.due_at - now) <= timedelta(hours=s.placement_urgent_hours if item.placement else 6)
        if item.placement:
            nag, send = _placement_nag(item, now, sent)
            if nag and (send or keys):
                keys.append(nag)
            elif nag:
                out.append(Action([nag]))
        if keys:
            out.append(Action(keys, messages.deadline_reminder(item), messages.deadline_buttons(item), urgent=urgent,
                              extra={"nagged": True}))
        return out

    # --- events ---------------------------------------------------------
    if item.kind == "event":
        start, reg = item.event_start, item.reg_deadline
        end = item.event_end or (start + timedelta(hours=3) if start else None)

        if item.status == "registered":
            if end and now >= end:
                return out + [Action([], new_status="expired")]
            if start:
                offsets = s.placement_event_offsets if item.placement else s.event_offsets
                keys = fresh(_offset_keys(start, offsets, item.created_at, now, "e"))
                if keys:
                    out.append(Action(keys, messages.event_reminder(item), urgent=True))
            return out

        closes = reg or start
        if item.status in ("asked", "interested") and closes and now >= closes:
            if item.status == "interested" and reg and "regclosed" not in sent:
                return out + [Action(["regclosed"], messages.registration_closed(item), [(f"reg:{item.id}", "✅ I registered")], new_status="expired")]
            return out + [Action([], new_status="expired")]

        if item.status == "asked":
            if now - item.created_at >= timedelta(hours=6 if item.placement else 24) and "reask" not in sent:
                out.append(Action(["reask"], messages.reask_interest(item), messages.event_interest_buttons(item)))
            return out

        if item.status == "interested":
            offsets = s.placement_registration_offsets if item.placement else s.registration_offsets
            keys = fresh(_offset_keys(reg, offsets, item.created_at, now, "r")) if reg else []
            if item.placement:
                nag, send = _placement_nag(item, now, sent)
                if nag and (send or keys):
                    keys.append(nag)
                elif nag:
                    out.append(Action([nag]))
            else:
                nag = _nag_key(item, now)
                if nag and nag not in sent:
                    keys.append(nag)
            if keys:
                urgent = bool(reg and (reg - now) <= timedelta(hours=s.placement_urgent_hours if item.placement else 6))
                out.append(Action(keys, messages.registration_reminder(item), messages.registration_buttons(item),
                                  urgent=urgent, extra={"nagged": True}))
            return out
    return out


def run_reminders(session: Session) -> int:
    now = utcnow()
    items = session.scalars(select(Item).where(Item.status.in_(ACTIVE_STATUSES))).all()
    sent_count = 0
    for item in items:
        sent = set(session.scalars(select(ReminderLog.key).where(ReminderLog.item_id == item.id)).all())
        for action in plan(item, now, sent):
            scheduled = bool(action.extra.get("scheduled"))
            if action.text and not (scheduled and notifier.is_paused(session)):  # paused: skip, don't pile up
                notifier.notify(session, action.text, action.buttons, urgent=action.urgent, item_id=item.id,
                                scheduled=scheduled, expires_at=action.extra.get("expires_at"))
                sent_count += 1
            for key in action.keys:
                session.add(ReminderLog(item_id=item.id, key=key))
            if action.new_status:
                item.status = action.new_status
            if action.clear_snooze:
                item.snoozed_until = None
            if action.extra.get("nagged"):
                item.last_nag_at = now
        session.flush()
    return sent_count
