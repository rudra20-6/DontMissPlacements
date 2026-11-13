from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.db import session_scope
from app.mail.base import RawEmail
from app.models import Item, ReminderLog
from app.services import decisions, pipeline, placement
from app.services.commands import handle_message
from app.services.decisions import Triage
from app.services.extraction import Deadline, EventInfo, Extraction
from app.services.reminders import plan
from app.timeutil import local, to_utc_naive, utcnow

IST = ZoneInfo("Asia/Kolkata")


def _on():
    with session_scope() as s:
        placement.set_mode(s, True)


class _Pool:
    enabled = True

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate_json(self, system, prompt, schema, max_tokens=2048, media=None):
        self.calls.append({"prompt": prompt, "media": media})
        return self.replies.pop(0)


def _analysis(title, *, placement_=True, due=None, event=None, category="placement", link="https://forms.gle/x"):
    r = {"category": category, "importance": 3, "has_deadline": bool(due), "is_event": bool(event),
         "needs_registration": bool(event), "is_noise": False, "is_placement": placement_, "title": title,
         "summary": f"{title} summary.", "deadlines": [{"what": "Register", "due": due}] if due else [],
         "event": event or {}, "primary_link": link}
    return r


def _iso(**delta):
    return (local(utcnow()) + timedelta(**delta)).replace(second=0, microsecond=0).isoformat()


# ---------------------------------------------------------------- mail in placement mode

def test_placement_mode_tracks_only_placement_mail(monkeypatch, sent):
    _on()
    due = utcnow() + timedelta(days=2)
    course = (Triage(category="coursework", importance=3, has_deadline=1.0, source="gemini"),
              Extraction("OS Assignment 3", "Submit on Moodle.", deadlines=[Deadline("Submit", due)]))
    job = (Triage(category="placement", importance=3, has_deadline=1.0, is_placement=1.0, source="gemini"),
           Extraction("Amazon SDE Intern registration", "Register on the portal.", deadlines=[Deadline("Register", due)]))
    results = [course, job]
    monkeypatch.setattr(pipeline, "analyze_email", lambda *a, **k: results.pop(0))
    pipeline.ingest(RawEmail("<c>", "Moodle <m@x>", "OS Assignment 3", utcnow(), "body"))
    pipeline.ingest(RawEmail("<p>", "Placement Cell <tpo@x>", "Amazon drive", utcnow(), "body"))
    pipeline.process_pending()
    with session_scope() as s:
        items = {i.title: i for i in s.query(Item)}
        assert items["OS Assignment 3"].kind == "announcement" and items["OS Assignment 3"].in_digest
        amazon = items["Amazon SDE Intern registration"]
        assert amazon.kind == "deadline" and amazon.placement and amazon.label == "D1"
    assert len(sent) == 1 and "Amazon" in sent[0]["text"]


def test_placement_mode_off_tracks_everything(monkeypatch, sent):
    due = utcnow() + timedelta(days=2)
    monkeypatch.setattr(pipeline, "analyze_email", lambda *a, **k: (
        Triage(category="coursework", importance=3, has_deadline=1.0, source="gemini"),
        Extraction("OS Assignment 3", "x", deadlines=[Deadline("Submit", due)])))
    pipeline.ingest(RawEmail("<c>", "Moodle <m@x>", "OS Assignment 3", utcnow(), "body"))
    pipeline.process_pending()
    with session_scope() as s:
        assert s.query(Item).one().kind == "deadline"


def test_placement_on_off_commands(sent):
    with session_scope() as s:
        handle_message(s, text="placement on")
        assert placement.is_on(s)
        handle_message(s, text="placement off")
        assert not placement.is_on(s)
    assert "OFF" in sent[-1]["text"]


# ---------------------------------------------------------------- aggressive reminders

def _at(h, m=0, day=7):
    return to_utc_naive(datetime(2030, 1, day, h, m, tzinfo=IST))


def _run(item, now, sent):
    actions = plan(item, now, sent)
    for a in actions:
        sent.update(a.keys)
        if a.extra.get("nagged") and a.text:
            item.last_nag_at = now
        if a.new_status:
            item.status = a.new_status
    return [a for a in actions if a.text]


def test_placement_deadline_is_nagged_hard():
    due = _at(23, 59, day=9)
    item = Item(id=1, kind="deadline", title="Amazon form", status="pending", due_at=due, created_at=_at(9, 30),
                placement=True, category="placement", importance=3, summary="", link="", label="D1")
    sent: set[str] = set()
    assert len(_run(item, _at(10, 1), sent)) == 1  # 10:00 daily nag
    assert _run(item, _at(11), sent) == []
    assert len(_run(item, _at(14, 1), sent)) == 1  # 14:00 nag
    assert len(_run(item, _at(0, 0, day=8), sent)) == 1  # 48h before
    sent_before = set(sent)
    assert len(_run(item, _at(10, 1, day=8), sent)) == 1
    assert {k for k in sent - sent_before} == {"pn2030-01-08T10"}
    # 12h, 6h, 3h, 2h, 1h, 30 min before: all fire, and the last stretch ignores quiet hours
    for h, m in ((11, 59), (17, 59), (20, 59), (21, 59), (22, 59), (23, 29)):
        acts = _run(item, _at(h, m, day=9), sent)
        assert len(acts) == 1 and acts[0].urgent, (h, m)


def test_nag_right_after_a_reminder_is_skipped():
    due = _at(21, 50, day=7)
    item = Item(id=1, kind="deadline", title="Flipkart OA", status="pending", due_at=due, created_at=_at(8),
                placement=True, category="placement", importance=3, summary="", link="", label="D1")
    sent: set[str] = set()
    first = _run(item, _at(9, 59), sent)  # the 12h reminder (09:50)
    assert len(first) == 1
    assert _run(item, _at(10, 5), sent) == []  # the 10:00 nag is swallowed
    assert "pn2030-01-07T10" in sent


def test_normal_deadline_keeps_gentle_schedule():
    due = _at(23, 59, day=9)
    item = Item(id=1, kind="deadline", title="OS assignment", status="pending", due_at=due, created_at=_at(9, 30),
                placement=False, category="coursework", importance=3, summary="", link="", label="D1")
    sent: set[str] = set()
    assert _run(item, _at(10, 1), sent) == [] and _run(item, _at(14, 1), sent) == []


# ---------------------------------------------------------------- forwarded WhatsApp messages

POST = """*Amazon SDE Intern 2027* 🚀
All eligible students must register on the portal by 15 Nov, 6 PM.
Link: https://forms.gle/amazon123"""


def test_forwarded_post_becomes_placement_deadline(monkeypatch, sent):
    _on()
    pool = _Pool(_analysis("Amazon SDE Intern registration", due=_iso(days=2), link="https://forms.gle/amazon123"))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text=POST, forwarded=True)
    with session_scope() as s:
        item = s.query(Item).one()
        assert (item.kind, item.placement, item.source, item.label) == ("deadline", True, "whatsapp", "D1")
        assert item.link == "https://forms.gle/amazon123"
    assert "Tracking it" in sent[-1]["text"] and sent[-1]["buttons"]
    with session_scope() as s:
        handle_message(s, text=POST, forwarded=True)  # forwarded twice: no duplicate, no second AI call
    assert "already have" in sent[-1]["text"] and len(pool.calls) == 1


def test_forward_of_something_already_tracked_from_mail(monkeypatch, sent):
    due = utcnow() + timedelta(days=2)
    with session_scope() as s:
        s.add(Item(kind="deadline", title="Amazon SDE Intern registration", status="pending", due_at=due,
                   placement=True, label="D1"))
    pool = _Pool(_analysis("Amazon SDE Intern 2027 registration",
                           due=local(due + timedelta(minutes=20)).isoformat()))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text=POST, forwarded=True)
    with session_scope() as s:
        assert s.query(Item).count() == 1
    assert "Already tracking" in sent[-1]["text"]


def test_forwarded_event_with_registration_is_tracked_as_interested(monkeypatch, sent):
    _on()
    event = {"name": "Google PPT", "start": _iso(days=3), "end": "", "venue": "H105",
             "registration_deadline": _iso(days=1), "registration_link": "https://forms.gle/g"}
    monkeypatch.setattr(decisions, "get_pool", lambda: _Pool(_analysis("Google PPT", event=event)))
    with session_scope() as s:
        handle_message(s, text="Google PPT on Friday, register by tomorrow https://forms.gle/g", forwarded=True)
    with session_scope() as s:
        item = s.query(Item).one()
        assert item.kind == "event" and item.status == "interested" and item.placement
    assert "Registered" in str(sent[-1]["buttons"])


def test_post_without_deadline_offers_nag_then_task_until_done(monkeypatch, sent):
    _on()
    monkeypatch.setattr(decisions, "get_pool", lambda: _Pool(_analysis("Microsoft internship applications open", link="https://aka.ms/x")))
    with session_scope() as s:
        handle_message(s, text="Microsoft internship applications are open https://aka.ms/x", forwarded=True)
    assert "No deadline" in sent[-1]["text"]
    nag_button = sent[-1]["buttons"][0][0]
    assert nag_button.startswith("nag:")
    with session_scope() as s:
        handle_message(s, button_id=nag_button)
    with session_scope() as s:
        task = s.query(Item).filter_by(kind="routine").one()
        assert task.schedule["task"] and task.schedule["times"] == ["10:00", "18:00"] and task.placement
        assert task.link == "https://aka.ms/x" and task.label == "R1"
    assert "until you tap ✅ Done" in sent[-1]["text"]
    with session_scope() as s:
        handle_message(s, text="done microsoft")
    with session_scope() as s:
        assert s.query(Item).filter_by(kind="routine").one().status == "done"  # for good, not "for today"


def test_remind_me_to_register_for_this_company(monkeypatch, sent):
    _on()
    pool = _Pool(_analysis("Uber hiring drive", link="https://uber.com/apply"))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="Uber is hiring! details https://uber.com/apply", forwarded=True)
    with session_scope() as s:
        notice = s.query(Item).filter_by(kind="announcement").one()
        notice_id = notice.id
    pool.replies.append({"intent": "add", "item_id": notice_id, "task_title": "Register for Uber", "task_due": "",
                         "is_placement": True})
    with session_scope() as s:
        handle_message(s, text="remind me to register for this company")
    assert f"{notice_id} (announcement: Uber hiring drive)" in pool.calls[-1]["prompt"]
    with session_scope() as s:
        task = s.query(Item).filter_by(kind="routine").one()
        assert task.title == "Register for Uber" and task.link == "https://uber.com/apply" and task.placement
    # later: it has a date now
    pool.replies.append({"intent": "reschedule", "item_id": task.id, "new_time": _iso(days=1)})
    with session_scope() as s:
        handle_message(s, text="uber registration closes tomorrow")
    with session_scope() as s:
        item = s.get(Item, task.id)
        assert item.kind == "deadline" and item.label == "D1" and item.placement


def test_remind_with_date_is_aggressive_placement_deadline(monkeypatch, sent):
    pool = _Pool({"intent": "add", "item_id": 0, "task_title": "Fill Amazon form", "task_due": _iso(days=3),
                  "is_placement": True})
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="remind me to fill the amazon form by friday 6pm")
    with session_scope() as s:
        item = s.query(Item).one()
        assert item.kind == "deadline" and item.placement and item.category == "placement"


def test_pasted_announcement_is_read_too(monkeypatch, sent):
    _on()
    pool = _Pool({"intent": "share", "item_id": 0},
                 _analysis("Goldman Sachs OA", due=_iso(days=1)))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="Goldman Sachs OA link will close tomorrow 11pm, attempt asap https://gs.com/oa")
    with session_scope() as s:
        assert s.query(Item).one().title == "Goldman Sachs OA"
    assert len(pool.calls) == 2


def test_poster_image_goes_to_gemini(monkeypatch, sent):
    pool = _Pool(_analysis("Atlassian intern drive", due=_iso(days=2)))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="", media=[("image/jpeg", b"\xff\xd8poster")])
    assert pool.calls[0]["media"] == [("image/jpeg", b"\xff\xd8poster")]
    with session_scope() as s:
        assert s.query(Item).one().title == "Atlassian intern drive"


def test_ai_busy_keeps_forward_queued_and_handles_it_later(monkeypatch, sent):
    from app.clients.gemini import LLMUnavailable

    class Busy:
        enabled = True

        def generate_json(self, *a, **k):
            raise LLMUnavailable("quota")

    monkeypatch.setattr(decisions, "get_pool", lambda: Busy())
    with session_scope() as s:
        handle_message(s, text=POST, forwarded=True)
    assert "saved this" in sent[-1]["text"]
    monkeypatch.setattr(decisions, "get_pool",
                        lambda: _Pool(_analysis("Amazon SDE Intern registration", due=_iso(days=2))))
    pipeline.process_pending()
    with session_scope() as s:
        assert s.query(Item).one().source == "whatsapp"
    assert "Tracking it" in sent[-1]["text"]


# ---------------------------------------------------------------- webhook

def _hook(msg, phone_id="1304633239405337", statuses=None):
    value = {"metadata": {"phone_number_id": phone_id}}
    if msg:
        value["messages"] = [msg]
    if statuses:
        value["statuses"] = statuses
    return {"entry": [{"changes": [{"value": value}]}]}


def test_webhook_forwarded_image(monkeypatch, sent):
    from app.clients.whatsapp import WhatsAppClient
    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "whatsapp_phone_number_id", "1304633239405337")
    monkeypatch.setattr(WhatsAppClient, "download_media", lambda self, mid: ("image/png", b"PNG" + mid.encode()))
    pool = _Pool(_analysis("Zomato SDE drive", due=_iso(days=2)))
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    msg = {"from": "919999999999", "id": "wamid.f1", "type": "image", "context": {"forwarded": True},
           "image": {"id": "media1", "mime_type": "image/png", "caption": "Zomato drive, register fast"}}
    TestClient(app).post("/webhook/whatsapp", json=_hook(msg))
    assert pool.calls[0]["media"] == [("image/png", b"PNGmedia1")]
    with session_scope() as s:
        assert s.query(Item).one().title == "Zomato SDE drive"

    other = dict(msg, id="wamid.f2")
    TestClient(app).post("/webhook/whatsapp", json=_hook(other, phone_id="999"))  # another bot's number
    assert len(pool.calls) == 1


def test_delivery_report_for_someone_else_is_ignored(sent):
    from app.main import app

    status = {"id": "wamid.x", "status": "failed", "recipient_id": "918888888888",
              "errors": [{"code": 131047, "title": "Re-engagement message"}]}
    TestClient(app).post("/webhook/whatsapp", json=_hook(None, statuses=[status]))
    with session_scope() as s:
        from app.services import notifier

        assert notifier.window_open(s)


def test_digest_lists_placements_first(sent):
    from app.services.digest import build_digest

    with session_scope() as s:
        s.add(Item(kind="deadline", title="Amazon form", status="pending", due_at=utcnow() + timedelta(days=1),
                   placement=True, label="D1"))
        s.add(Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=1),
                   label="D2"))
        s.flush()
        text = build_digest(s)
    assert text.index("Placements") < text.index("Amazon form") < text.index("OS assignment")
