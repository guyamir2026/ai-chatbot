"""
טסטים ל-messaging/broadcast_scheduler.py + DB helpers לתזמון.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IL = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


def _make_approved_template(db, content_sid="HX_T1", category="UTILITY"):
    db.upsert_whatsapp_template({
        "content_sid": content_sid,
        "friendly_name": "t",
        "language": "he",
        "category": category,
        "approval_status": "approved",
        "body_text": "היי {{1}}",
        "variables": [{"index": "1", "name": "name"}],
    })


def _make_wa_user(db, user_id, opted_in=True):
    db.upsert_user(user_id, username=user_id, channel="whatsapp")
    if opted_in:
        db.set_wa_marketing_opt_in(user_id, source="test")


# ── DB helpers ───────────────────────────────────────────────────────────────


class TestScheduleDBHelpers:
    def test_schedule_draft_campaign(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        future = (datetime.now(_IL) + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert db.schedule_broadcast_campaign(cid, future) is True

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "scheduled"
        assert camp["scheduled_at"] == future

    def test_cannot_schedule_non_draft(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(cid, "completed")
        future = (datetime.now(_IL) + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert db.schedule_broadcast_campaign(cid, future) is False

    def test_cancel_scheduled(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        future = (datetime.now(_IL) + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.schedule_broadcast_campaign(cid, future)
        assert db.cancel_scheduled_campaign(cid) is True

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "draft"
        assert camp["scheduled_at"] is None

    def test_cancel_non_scheduled_noop(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        # Draft — לא scheduled, הביטול לא משנה
        assert db.cancel_scheduled_campaign(cid) is False

    def test_list_due_scheduled_returns_past_only(self, db):
        _make_approved_template(db)
        past_cid = db.create_broadcast_campaign(template_sid="HX_T1")
        future_cid = db.create_broadcast_campaign(template_sid="HX_T1")

        now_il = datetime.now(_IL)
        past = (now_il - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        future = (now_il + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = now_il.strftime("%Y-%m-%d %H:%M:%S")

        db.schedule_broadcast_campaign(past_cid, past)
        db.schedule_broadcast_campaign(future_cid, future)

        # עוברים now_str מפורש כדי שלא נהיה תלויים ב-TZ של השרת
        due = db.list_due_scheduled_campaigns(now_str=now_str)
        ids = [r["id"] for r in due]
        assert past_cid in ids
        assert future_cid not in ids

    def test_reschedule_updates_time_keeps_status(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        t1 = (datetime.now(_IL) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        t2 = (datetime.now(_IL) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        db.schedule_broadcast_campaign(cid, t1)

        assert db.reschedule_campaign_at(cid, t2) is True
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "scheduled"  # לא השתנה
        assert camp["scheduled_at"] == t2

    def test_reschedule_requires_scheduled_status(self, db):
        """draft לא reschedule — רק scheduled."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        t = (datetime.now(_IL) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert db.reschedule_campaign_at(cid, t) is False


# ── Scheduler loop ───────────────────────────────────────────────────────────


class TestSchedulerLoop:
    def test_due_utility_campaign_gets_started(self, db, monkeypatch):
        """scheduled + due + UTILITY → start_campaign_send נקרא."""
        from messaging import broadcast_scheduler as sched

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )
        past = (datetime.now(_IL) - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.schedule_broadcast_campaign(cid, past)

        # Mock start_campaign_send שלא יריץ thread אלא רק נספר קריאות
        calls = []

        def fake_start(campaign_id, **kw):
            calls.append((campaign_id, kw.get("from_status")))
            # מעבירים ידנית ל-sending כדי שלא יחזור ב-iteration הבאה
            db.set_campaign_status(campaign_id, "sending")
            return True

        monkeypatch.setattr(sched, "start_campaign_send", fake_start)

        sched._process_due_campaigns()

        assert len(calls) == 1
        assert calls[0] == (cid, "scheduled")

    def test_marketing_in_shabbat_defers(self, db, monkeypatch):
        """MARKETING שנפל בשבת — לא נשלח אלא scheduled_at עודכן קדימה."""
        from messaging import broadcast_scheduler as sched

        _make_approved_template(db, category="MARKETING")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        past = (datetime.now(_IL) - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.schedule_broadcast_campaign(cid, past)

        # Mock: שבת עכשיו. patches על ה-module scheduler (לא על shabbat_window)
        # כי ה-scheduler ייבא ב-module level.
        future_allowed = datetime.now(_IL) + timedelta(days=2, hours=5)
        monkeypatch.setattr(
            sched, "is_blocked_for_marketing",
            lambda _dt: (True, "mocked shabbat"),
        )
        monkeypatch.setattr(
            sched, "next_allowed_time",
            lambda _dt, category: future_allowed,
        )

        # start_campaign_send אמור *לא* להיקרא
        start_called = []
        monkeypatch.setattr(
            sched, "start_campaign_send",
            lambda *a, **kw: start_called.append(a),
        )

        sched._process_due_campaigns()

        assert start_called == []
        # scheduled_at עודכן ל-next_allowed_time; סטטוס נשאר scheduled
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "scheduled"
        expected = future_allowed.strftime("%Y-%m-%d %H:%M:%S")
        assert camp["scheduled_at"] == expected

    def test_missing_template_marks_failed(self, db, monkeypatch):
        """תבנית נמחקה אחרי תזמון — הקמפיין עובר ל-failed."""
        from messaging import broadcast_scheduler as sched

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        past = (datetime.now(_IL) - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.schedule_broadcast_campaign(cid, past)

        # מוחקים את התבנית
        with db.get_connection() as conn:
            conn.execute("DELETE FROM whatsapp_templates WHERE content_sid = ?", ("HX_T1",))

        monkeypatch.setattr(
            sched, "start_campaign_send",
            lambda *a, **kw: pytest.fail("start_campaign_send לא אמור להיקרא"),
        )

        sched._process_due_campaigns()

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_future_scheduled_not_processed(self, db, monkeypatch):
        """קמפיין מתוזמן לעתיד לא אמור להיתפס ע"י ה-scheduler."""
        from messaging import broadcast_scheduler as sched

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        future = (datetime.now(_IL) + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.schedule_broadcast_campaign(cid, future)

        monkeypatch.setattr(
            sched, "start_campaign_send",
            lambda *a, **kw: pytest.fail("start_campaign_send לא אמור להיקרא"),
        )

        sched._process_due_campaigns()
        # הסטטוס עדיין scheduled
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "scheduled"


class TestSchedulerLifecycle:
    def test_start_stop_scheduler(self, monkeypatch):
        """start_scheduler מריץ thread; stop_scheduler עוצר אותו."""
        import time
        monkeypatch.setenv("BROADCAST_SCHEDULER_ENABLED", "1")
        # נטרל את הלוגיקה האמיתית כדי שלא ירוץ על DB
        from messaging import broadcast_scheduler as sched
        monkeypatch.setattr(sched, "_POLL_INTERVAL", 0.1)
        monkeypatch.setattr(sched, "_process_due_campaigns", lambda: None)

        started = sched.start_scheduler()
        assert started is True
        assert sched._scheduler_thread is not None
        assert sched._scheduler_thread.is_alive()

        sched.stop_scheduler()
        time.sleep(0.3)  # מחכה קצר ליציאה
        assert not sched._scheduler_thread.is_alive()

    def test_start_scheduler_disabled(self, monkeypatch):
        monkeypatch.setenv("BROADCAST_SCHEDULER_ENABLED", "0")
        from messaging import broadcast_scheduler as sched
        # נקיון בין טסטים — עוצרים thread קודם אם רץ
        sched.stop_scheduler()
        sched._scheduler_thread = None

        assert sched.start_scheduler() is False
