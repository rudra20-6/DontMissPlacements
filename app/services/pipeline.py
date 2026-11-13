"""Email -> (store) -> AI analysis -> items -> WhatsApp notification.

Every new email is first stored as `pending`, then analysed. If Gemini quota is exhausted the email just
stays pending and is retried on the next tick, so nothing is lost. After LLM_RETRY_MAX_MINUTES it is
processed with keyword rules instead, so an important mail is never delayed for long.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.clients.gemini import LLMUnavailable
from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail, get_mail_source
from app.models import Email, Item
from app.services import health, labels, messages, notifier, placement
from app.services.decisions import Triage, analyze_email
from app.services.extraction import Extraction
from app.textutil import truncate
from app.timeutil import local, utcnow

log = logging.getLogger(__name__)
MAX_PER_TICK = 20
CATCHUP_AFTER_HOURS = 24  # mails originally sent longer ago than this are summarised, not pushed one by one


def poll_mail() -> int:
    """Fetch new mail into the queue, then work through the queue. Returns emails processed."""
    try:
        raws = get_mail_source().fetch_new()
    except Exception as exc:  # noqa: BLE001 - a login/network problem must not stop reminders
        log.warning("Mail fetch failed: %s", exc)
        with session_scope() as session:
            health.mail_error(session, str(exc) if isinstance(exc, MailError) else f"{type(exc).__name__}: {exc}")
    else:
        for raw in raws:
            ingest(raw)
        with session_scope() as session:
            health.mail_ok(session, len(raws))
    return process_pending()


def ingest(raw: RawEmail) -> bool:
    sent_at = raw.original_date or raw.received_at
    with session_scope() as session:
        if session.scalar(select(Email.id).where(Email.message_id == raw.message_id)):
            return False
        # The same mail can arrive twice with different Message-IDs (forwarded singly AND in a bulk forward).
        twin = session.scalar(select(Email.id).where(
            Email.subject == raw.subject[:1000], Email.sender == raw.sender[:500],
            Email.received_at >= sent_at - timedelta(minutes=3), Email.received_at <= sent_at + timedelta(minutes=3),
        ))
        if twin:
            return False
        session.add(Email(
            message_id=raw.message_id, sender=raw.sender[:500], subject=raw.subject[:1000],
            received_at=sent_at, web_link=raw.web_link, body=truncate(raw.body, 20000),
            links=raw.links[:20], action="pending",
        ))
        return True


def process_pending(limit: int = MAX_PER_TICK) -> int:
    with session_scope() as session:
        ids = session.scalars(
            select(Email.id).where(Email.action == "pending").order_by(Email.received_at).limit(limit)
        ).all()
    done = 0
    for email_id in ids:
        try:
            process_one(email_id)
            done += 1
        except LLMUnavailable as exc:
            log.warning("AI quota exhausted, %d email(s) stay queued: %s", len(ids) - done, exc)
            break
        except Exception:  # noqa: BLE001 - one bad email must not block the queue
            log.exception("Failed processing email %s", email_id)
            with session_scope() as session:
                email = session.get(Email, email_id)
                email.attempts += 1
                if email.attempts >= 3:
                    email.action = "error"
                    health.mail_failed(session, email.sender, email.subject)
    return done


def process_one(email_id: int) -> None:
    s = get_settings()
    with session_scope() as session:
        email = session.get(Email, email_id)
        if email is None or email.action != "pending":
            return
        waited = utcnow() - (email.created_at or utcnow())
        allow_heuristic = waited >= timedelta(minutes=s.llm_retry_max_minutes)
        triage, ext = analyze_email(email.sender, email.subject, email.body, local(email.received_at).isoformat(),
                                    email.links or [], allow_heuristic=allow_heuristic)
        items = apply_analysis(session, email, triage, ext)
        if email.source == "whatsapp" and items:
            send_shared_items(session, items, reply=False)  # was queued while the AI was busy


def apply_analysis(session: Session, email: Email, triage: Triage, ext: Extraction | None,
                   dupes: list[Item] | None = None) -> list[Item]:
    """Turns one analysed mail (or forwarded WhatsApp message) into items, and pushes mail items."""
    if triage.source == "heuristic":
        health.ai_fallback(session, email.sender, email.subject, health.ai_reason())
    elif triage.source == "gemini":
        health.ai_ok(session)
    email.category = triage.category
    email.importance = triage.importance
    email.decision = triage.to_dict()
    action = decide_action(triage)
    shared = email.source == "whatsapp"
    is_placement = triage.is_placement >= 0.5
    track = shared or is_placement or not placement.is_on(session)
    if shared and ext is not None:
        action = "notify"  # the user forwarded it on purpose: always track it
    elif is_placement and action == "digest":
        action = "notify"
    elif not track and action == "notify":
        action = "digest"  # placement mode: other mail is only mentioned in the digest
    email.action = action
    email.processed_at = utcnow()
    log.info("Mail %r -> %s (%s, importance %.0f via %s)", email.subject, action, triage.category,
             triage.importance, triage.source)
    body, links = email.body, email.links or []
    email.body = ""  # don't keep mail bodies around once analysed
    if action == "dropped" or ext is None:
        email.action = "dropped"
        return []
    raw = RawEmail(email.message_id, email.sender, email.subject, email.received_at, body, email.web_link, links)
    items = build_items(session, email, triage, ext, raw, track=track, dupes=dupes)
    for item in items:
        item.placement = is_placement or (shared and placement.is_on(session))
        item.source = email.source
        if shared and item.kind == "event":
            needs_reg = item.reg_deadline or item.reg_link or triage.needs_registration >= 0.5
            item.status = "interested" if needs_reg else "registered"
    old = utcnow() - email.received_at > timedelta(hours=CATCHUP_AFTER_HOURS)
    for item in items:
        item.in_digest = action == "digest"
        if old:
            # Old mail (e.g. a one-time import of last week's inbox): no individual message, it goes
            # into one catch-up summary instead. Old low-value announcements are simply archived.
            # (an "announcement" from an old deadline/event mail means the date already passed: skip it)
            item.catchup = item.kind != "announcement" or (
                item.importance >= 3 and triage.has_deadline < 0.5 and triage.is_event < 0.5)
            item.in_digest = False
            item.digested = True
    session.flush()
    if action == "notify" and not old and not shared:
        for item in items:
            send_new_item(session, item, _short_sender(email.sender))
    return items


def decide_action(t: Triage) -> str:
    """Thresholds on the AI's signals -> dropped | digest | notify."""
    s = get_settings()
    actionable = t.has_deadline >= 0.5 or (t.is_event >= 0.5 and t.importance >= 1.0)
    if t.is_noise >= 0.7 and t.importance < 2.5:
        return "dropped"
    if t.importance < s.importance_drop_below and not actionable:
        return "dropped"
    if t.importance >= s.importance_immediate_min or actionable:
        return "notify"
    return "digest"


def build_items(session: Session, email: Email, t: Triage, ext: Extraction, raw: RawEmail, track: bool = True,
                dupes: list[Item] | None = None) -> list[Item]:
    """track=False (placement mode, non-placement mail): a plain notice, no deadline/event reminders.
    Deadlines/events already tracked (e.g. a forward of a mail we already have) are skipped and put in `dupes`."""
    now = utcnow()
    link = ext.primary_link or raw.web_link
    base = dict(email_id=email.id, category=t.category, importance=t.importance, summary=ext.summary, link=link)
    items: list[Item] = []

    ev = ext.event
    if not track:
        ev = None
    if ev and (same := _similar_open(session, "event", ev.start or ev.registration_deadline, ev.name or ext.title)):
        if dupes is not None and same not in dupes:
            dupes.append(same)
        ev = None
    if t.is_event >= 0.5 and ev and ((ev.start and ev.start > now) or (ev.registration_deadline and ev.registration_deadline > now)):
        items.append(Item(
            kind="event", title=ev.name or ext.title, event_start=ev.start, event_end=ev.end, venue=ev.venue,
            reg_deadline=ev.registration_deadline, reg_link=ev.registration_link, status="asked", **base,
        ))

    if t.has_deadline >= 0.5 and track:
        for d in ext.deadlines:
            if not d.due or d.due <= now:
                continue
            if same := _similar_open(session, "deadline", d.due, f"{ext.title} {d.what}"):
                if dupes is not None and same not in dupes:
                    dupes.append(same)
                continue
            # an event's registration deadline is tracked on the event itself
            if items and items[0].kind == "event" and items[0].reg_deadline and abs((d.due - items[0].reg_deadline).total_seconds()) < 3600:
                continue
            title = ext.title if len(ext.deadlines) == 1 else f"{ext.title}: {d.what}"[:200]
            items.append(Item(kind="deadline", title=title, due_at=d.due, status="pending", **base))

    if not items and not (dupes and len(dupes) > 0):
        items.append(Item(kind="announcement", title=ext.title, status="info", **base))
    for item in items:
        session.add(item)
        labels.assign(session, item)
    return items


def send_new_item(session: Session, item: Item, sender: str = "") -> None:
    text = messages.item_card(item, sender)
    urgent = item.kind == "deadline" and item.due_at and (item.due_at - utcnow()).total_seconds() < 6 * 3600
    if item.kind == "event":
        text += "\n\n🙋 *Are you interested in this?*"
        notifier.notify(session, text, messages.event_interest_buttons(item), item_id=item.id)
    elif item.kind == "deadline":
        notifier.notify(session, text, messages.deadline_buttons(item), urgent=bool(urgent), item_id=item.id)
    else:
        notifier.notify(session, text, item_id=item.id)


_STOP = {"the", "and", "for", "with", "from", "your", "you", "are", "this", "that", "will", "has", "have", "all",
         "students", "registration", "register", "deadline", "form", "link", "drive", "hiring", "batch"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP}


def _similar_open(session: Session, kind: str, when: datetime | None, title: str) -> Item | None:
    """An open deadline/event at about the same time with a similar title (e.g. the mail and the WhatsApp post)."""
    if when is None:
        return None
    window = (when - timedelta(hours=1), when + timedelta(hours=1))
    if kind == "deadline":
        cond = Item.due_at.between(*window)
    else:
        cond = or_(Item.event_start.between(*window), Item.reg_deadline.between(*window))
    words = _words(title)
    for other in session.scalars(select(Item).where(Item.kind == kind, Item.status.in_(labels.OPEN), cond)).all():
        theirs = _words(other.title)
        if words and theirs and len(words & theirs) / min(len(words), len(theirs)) >= 0.5:
            return other
    return None


def send_shared_items(session: Session, items: list[Item], reply: bool = True) -> None:
    """Cards for things the user forwarded, with the follow-up spelled out."""
    def send(text: str, buttons=None, item_id=None):
        if reply:
            notifier.reply(session, text, buttons, item_id=item_id)
        else:
            notifier.notify(session, text, buttons, item_id=item_id)

    for item in items:
        text = messages.item_card(item)
        if item.kind == "deadline":
            text += "\n\n📌 *Tracking it.* I'll keep reminding you until you tap ✅ Done."
            send(text, messages.deadline_buttons(item), item_id=item.id)
        elif item.kind == "event" and item.status == "interested":
            text += "\n\n📌 *Tracking it.* I'll keep reminding you to register until you tap ✅ Registered."
            send(text, messages.registration_buttons(item), item_id=item.id)
        elif item.kind == "event":
            text += "\n\n📌 *Tracking it.* I'll remind you before it starts."
            send(text, item_id=item.id)
        else:
            text += ("\n\n🤔 *No deadline in this one.* Want me to nag you about it until you've done it?\n"
                     "_Or tell me a date, e.g._ `remind me to register for this by Friday 6pm`")
            send(text, messages.task_buttons(item), item_id=item.id)


def _short_sender(sender: str) -> str:
    name = sender.split("<")[0].strip().strip('"')
    return name or sender
