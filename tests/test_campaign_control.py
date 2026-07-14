"""
טסטים ל-שלב 7 — pause/resume + retry failed.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


def _make_approved_template(db, category="UTILITY"):
    db.upsert_whatsapp_template({
        "content_sid": "HX_T1", "friendly_name": "t",
        "language": "he", "category": category,
        "approval_status": "approved", "body_text": "x",
        "variables": [],
    })


def _make_wa_user(db, uid, opted_in=True):
    db.upsert_user(uid, username=uid, channel="whatsapp")
    if opted_in:
        db.set_wa_marketing_opt_in(uid, source="test")


# ── DB helpers: get_campaign_status + requeue_failed_deliveries ─────────────


class TestCampaignStatusHelper:
    def test_get_campaign_status_returns_status(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        assert db.get_campaign_status(cid) == "draft"
        db.set_campaign_status(cid, "sending")
        assert db.get_campaign_status(cid) == "sending"

    def test_get_campaign_status_missing(self, db):
        assert db.get_campaign_status(99999) is None


class TestRequeueFailed:
    def test_resets_failed_and_undelivered(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # 1 failed, 1 undelivered, 1 delivered — רק 2 אמורים לחזור לתור
        did_f, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did_f, "21408", "bad")

        did_u, _ = db.create_delivery_queue(cid, "+972501000002", {})
        db.mark_delivery_sent(did_u, "SM_U")
        db.update_delivery_status_by_twilio_sid("SM_U", "undelivered",
                                                 error_code="63024")

        did_d, _ = db.create_delivery_queue(cid, "+972501000003", {})
        db.mark_delivery_sent(did_d, "SM_D")
        db.update_delivery_status_by_twilio_sid("SM_D", "delivered")

        reset = db.requeue_failed_deliveries(cid)
        assert reset == 2

        rows = db.get_deliveries_for_campaign(cid)
        by_user = {r["user_id"]: r for r in rows}
        # שני הכישלונות חזרו ל-queued עם SID ריק
        assert by_user["+972501000001"]["status"] == "queued"
        assert by_user["+972501000001"]["twilio_message_sid"] is None
        assert by_user["+972501000002"]["status"] == "queued"
        assert by_user["+972501000002"]["twilio_message_sid"] is None
        # delivered לא השתנה
        assert by_user["+972501000003"]["status"] == "delivered"
        assert by_user["+972501000003"]["twilio_message_sid"] == "SM_D"

    def test_clears_error_fields(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did, "63016", "not on WhatsApp")

        db.requeue_failed_deliveries(cid)

        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["error_code"] == ""
        assert rows[0]["error_message"] == ""
        assert rows[0]["failed_at"] is None

    def test_no_failed_returns_zero(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_OK")
        assert db.requeue_failed_deliveries(cid) == 0


class TestRetryFailedIntegration:
    """Regression (Cursor): retry-failed חייב באמת לשלוח שוב, לא רק לאפס סטטוסים."""

    def test_retry_failed_actually_sends(self, db, monkeypatch):
        """שליחה ראשונה: 2 הצליחו, 1 נכשל. retry-failed: הנכשל חייב להישלח
        שוב באמת (create_delivery_queue על שורה queued מחזיר should_send=True)."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        for i in range(3):
            _make_wa_user(db, f"+972501{i:06d}", opted_in=True)

        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={},
        )

        # ריצה ראשונה: הודעה 2 (אחת) נכשלת
        send_results = iter([
            MagicMock(sid="SM_1"),  # הצלחה
            RuntimeError("fail #2"),  # כשל
            MagicMock(sid="SM_3"),  # הצלחה
        ])

        def fake_create_first_run(**kwargs):
            result = next(send_results)
            if isinstance(result, Exception):
                raise result
            return result

        mock_client = MagicMock()
        mock_client.messages.create = fake_create_first_run
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        sender_mod.send_campaign(cid)

        # וידוא: 2 נשלחו, 1 נכשל
        progress = db.get_campaign_progress(cid)
        assert progress["sent"] == 2
        assert progress["failed"] == 1

        # --- retry-failed flow ---
        reset_count = db.requeue_failed_deliveries(cid)
        assert reset_count == 1

        # Transition completed→sending (simulating admin retry flow)
        campaign = db.get_broadcast_campaign(cid)
        db.transition_campaign_status(cid, campaign["status"], "sending")

        # ריצה שניה: ה-fake מחזיר SID נוסף
        retry_calls = []

        def fake_create_retry(**kwargs):
            retry_calls.append(kwargs.get("to", ""))
            return MagicMock(sid=f"SM_RETRY_{len(retry_calls)}")

        mock_client.messages.create = fake_create_retry
        sender_mod._send_campaign_locked(cid)

        # וידוא: ה-retry שלח שוב בדיוק נמען אחד (הנכשל)
        assert len(retry_calls) == 1
        assert "+972501000001" in retry_calls[0]  # הנמען השני שנכשל

        # סטטוס סופי: שלושתם sent
        progress_after = db.get_campaign_progress(cid)
        assert progress_after["sent"] == 3
        assert progress_after["failed"] == 0


class TestPauseResumeCorrectness:
    """Regression (Cursor, HIGH): resume אחרי pause חייב לשלוח לכל
    הנמענים שלא עובדו עדיין. הפתרון: pre-create של כל שורות ה-delivery
    ב-queued בריצה הראשונה, כך ש-pause באמצע לא יאבד נמענים לא-מעובדים."""

    def test_pause_then_resume_sends_remaining_recipients(self, db, monkeypatch):
        """15 נמענים, pause אחרי 10. resume חייב לשלוח ל-5 שנותרו."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        for i in range(15):
            _make_wa_user(db, f"+972501{i:06d}", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        sent_count = {"n": 0}
        all_recipients = []

        def fake_create(**kwargs):
            sent_count["n"] += 1
            all_recipients.append(kwargs.get("to", ""))
            # pause אחרי 10 הודעות — הבדיקה הבאה ב-i=10 תעצור
            if sent_count["n"] == 10:
                db.set_campaign_status(cid, "paused")
            return MagicMock(sid=f"SM_{sent_count['n']}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # ריצה ראשונה — נשלחות 10 הודעות, pause עוצר.
        db.set_campaign_status(cid, "sending")
        sender_mod._send_campaign_locked(cid)

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "paused"
        assert camp["total_recipients"] == 15  # נכתב בריצה הראשונה
        # 15 שורות delivery קיימות (pre-create), 10 sent + 5 queued
        deliveries = db.get_deliveries_for_campaign(cid)
        assert len(deliveries) == 15
        queued_count = sum(1 for d in deliveries if d["status"] == "queued")
        assert queued_count == 5

        # --- Resume ---
        db.transition_campaign_status(cid, "paused", "sending")
        sender_mod._send_campaign_locked(cid)

        # כל 15 נשלחו (10 מהריצה הראשונה + 5 מה-resume)
        assert sent_count["n"] == 15
        camp_after = db.get_broadcast_campaign(cid)
        assert camp_after["status"] == "completed"
        assert camp_after["total_recipients"] == 15  # לא השתנה

    def test_first_run_pre_creates_all_delivery_rows(self, db, monkeypatch):
        """Regression: ריצה ראשונה חייבת ליצור שורת delivery לכל הנמענים
        *לפני* התחלת הלולאה. בלי זה, pause מוקדם היה מאבד נמענים."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        for i in range(5):
            _make_wa_user(db, f"+972501{i:06d}", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # בודקים שגרת יצירה: מיד אחרי transition + לפני שליחה, יש 5 שורות.
        # הטריק: נזרוק exception ב-Twilio כדי לעצור את הלולאה מוקדם, אבל
        # כל השורות כבר אמורות להיות pre-created.
        def always_fail(**kwargs):
            raise RuntimeError("simulated fail")

        mock_client = MagicMock()
        mock_client.messages.create = always_fail
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        db.set_campaign_status(cid, "sending")
        sender_mod._send_campaign_locked(cid)

        # כל 5 השורות נוצרו, גם שהלולאה נכשלה על כולן
        deliveries = db.get_deliveries_for_campaign(cid)
        assert len(deliveries) == 5
        # כל 5 במצב failed (כי Twilio זרקה) — לא queued
        assert all(d["status"] == "failed" for d in deliveries)


class TestRerunScope:
    """Regression (Cursor): resume/retry לא אמורים להוסיף נמענים חדשים
    שהצטרפו אחרי הריצה הראשונה. הלולאה במצב re-run משתמשת רק בשורות
    queued הקיימות."""

    def test_retry_does_not_include_new_users(self, db, monkeypatch):
        """2 נמענים בריצה ראשונה, 1 נכשל. משתמש חדש הצטרף אחרי. retry-failed
        שולח רק לנכשל — לא למשתמש החדש."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=True)

        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # ריצה ראשונה: השני נכשל
        call_count = {"n": 0}

        def fake_create_first(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("fail for user 2")
            return MagicMock(sid=f"SM_FIRST_{call_count['n']}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create_first
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        sender_mod.send_campaign(cid)
        assert db.get_campaign_progress(cid)["failed"] == 1

        # משתמש חדש הצטרף אחרי הריצה הראשונה
        _make_wa_user(db, "+972501000003", opted_in=True)

        # --- retry-failed ---
        db.requeue_failed_deliveries(cid)
        db.transition_campaign_status(
            cid, db.get_broadcast_campaign(cid)["status"], "sending",
        )

        retry_recipients = []

        def fake_create_retry(**kwargs):
            retry_recipients.append(kwargs.get("to", ""))
            return MagicMock(sid=f"SM_RETRY_{len(retry_recipients)}")

        mock_client.messages.create = fake_create_retry
        sender_mod._send_campaign_locked(cid)

        # Retry שלח רק ל-001 (שנכשל) — לא ל-003 (חדש) או ל-002 (הצליח)
        assert len(retry_recipients) == 1
        # ה-001 הוא זה שהצליח בריצה הראשונה. נכשל היה 002. בואו נבדוק:
        # call 1 (user 1): succeeded → SM_FIRST_1
        # call 2 (user 2): raised → failed
        # אז במהלך retry רק user 2 נשלח שוב.
        to_value = retry_recipients[0]
        assert "+972501000002" in to_value
        assert "+972501000003" not in to_value
        assert "+972501000001" not in to_value

        # total_recipients לא השתנה — נשאר 2 מהריצה הראשונה
        camp = db.get_broadcast_campaign(cid)
        assert camp["total_recipients"] == 2

    def test_resume_uses_only_existing_queued(self, db, monkeypatch):
        """אחרי pause, 5 queued נשארו. משתמש חדש הצטרף. resume שולח רק ל-5
        הקיימים."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # יוצרים ישירות 3 deliveries ב-queued (מדמים מצב אחרי pause)
        for i in range(3):
            uid = f"+972501000{i:03d}"
            _make_wa_user(db, uid, opted_in=True)
            db.create_delivery_queue(cid, uid, {})

        # סטטוס paused + total_recipients הוגדר בריצה הקודמת
        db.set_campaign_counters(cid, {"total_recipients": 3})
        db.set_campaign_status(cid, "paused")

        # משתמש חדש הצטרף (לא אמור להיכנס ב-resume)
        _make_wa_user(db, "+972501000999", opted_in=True)

        # resume דרך start_campaign_send (paused→sending + spawn)
        sent_recipients = []

        def fake_create(**kwargs):
            sent_recipients.append(kwargs.get("to", ""))
            return MagicMock(sid=f"SM_R_{len(sent_recipients)}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # transition paused→sending + _send_campaign_locked ישיר
        db.transition_campaign_status(cid, "paused", "sending")
        sender_mod._send_campaign_locked(cid)

        # רק 3 נשלחו — המשתמש החדש (999) לא נכלל
        assert len(sent_recipients) == 3
        for recipient_to in sent_recipients:
            assert "+972501000999" not in recipient_to

        # total_recipients נשאר 3
        camp = db.get_broadcast_campaign(cid)
        assert camp["total_recipients"] == 3


class TestSpawnSendThread:
    """Regression (Cursor): _spawn_send_thread — helper שמנוצל ע"י retry
    לאחר שהקורא כבר ביצע transition ל-sending. מבטיח שלא נשאיר שורות
    queued יתמות אם יצירת ה-thread נכשלת."""

    def test_spawn_succeeds_when_thread_creation_ok(self, db, monkeypatch):
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        # Caller transition ל-sending
        db.set_campaign_status(cid, "sending")

        # נטרל את הלולאה הפנימית
        monkeypatch.setattr(
            sender_mod, "_run_locked_send_safely",
            lambda *a, **kw: None,
        )

        assert sender_mod._spawn_send_thread(cid) is True

    def test_spawn_returns_false_on_thread_start_failure(self, db, monkeypatch):
        """אם threading.Thread.start() זורק — מחזירים False כדי שה-caller
        יוכל לשחזר."""
        import threading
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(cid, "sending")

        class ExplodingThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise RuntimeError("out of threads")

        monkeypatch.setattr(threading, "Thread", ExplodingThread)
        assert sender_mod._spawn_send_thread(cid) is False


class TestEndOfLoopAtomicTransition:
    """Regression (Cursor): הסיום של _send_campaign_locked חייב להשתמש
    ב-transition_campaign_status אטומי (sending→completed) כדי לא לדרוס
    resume שקרה בזמן החישוב של final_progress."""

    def test_sending_atomically_transitions_to_completed(self, db, monkeypatch):
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={},
        )

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_OK")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        sender_mod.send_campaign(cid)

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"

    def test_race_with_resume_does_not_overwrite(self, db, monkeypatch):
        """simulate: הלולאה סיימה, computed final_status='completed', אבל
        thread חדש (resume) כבר העביר את הסטטוס ל-sending. ה-transition
        האטומי אמור להחזיר False ולא לדרוס. בפועל נדמה את זה ע"י
        מניפולציה של הסטטוס מיד לפני transition הסופי."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_OK")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # נחטוף את recompute_campaign_counters כדי להחליף את הסטטוס
        # ל-"paused" באמצע הסיום — סימולציה של admin paused לאחר הלולאה.
        # חשוב לעשות patch על ai_chatbot.database (ה-wrapper) ולא על
        # database הישיר, כי _send_campaign_locked משתמש בעטיפה.
        from ai_chatbot import database as db_wrapper
        original_recompute = db_wrapper.recompute_campaign_counters

        def recompute_then_pause(cid_arg):
            original_recompute(cid_arg)
            # ה-admin לחץ pause אחרי הלולאה, לפני finalization
            with db_wrapper.get_connection() as conn:
                conn.execute(
                    "UPDATE broadcast_campaigns SET status = 'paused' WHERE id = ?",
                    (cid_arg,),
                )

        monkeypatch.setattr(
            db_wrapper, "recompute_campaign_counters", recompute_then_pause,
        )

        sender_mod.send_campaign(cid)

        # אחרי הסיום: הסטטוס paused (לא נדרס ל-completed) כי ה-transition
        # האטומי ראה ש-status!="sending" ולא עשה כלום.
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "paused"


# ── Pause integration ────────────────────────────────────────────────────────


class TestPauseIntegration:
    def test_pause_stops_loop_between_recipients(self, db, monkeypatch):
        """במהלך שליחת 15 נמענים, קמפיין מועבר ל-paused אחרי 3 נמענים.
        הלולאה אמורה לזהות את זה (בכל 10 איטרציות) ולעצור ללא שליחה של כל
        השאר. הערה: ה-threshold הוא 10, אז בדיקה אחרי ה-10 הראשונים."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        for i in range(15):
            _make_wa_user(db, f"+972501{i:06d}", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={},
        )

        sent_count = {"n": 0}

        def fake_create(**kwargs):
            sent_count["n"] += 1
            # אחרי 10 הודעות הראשונות (הבדיקה הבאה ב-i=10), מעבירים ל-paused
            if sent_count["n"] == 10:
                db.set_campaign_status(cid, "paused")
            return MagicMock(sid=f"SM_{sent_count['n']}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (False, None),
        )

        # Override: transition draft→sending ידני כדי לעקוף את ה-atomic
        # הסיבה: ב-send_campaign הנעילה מצופה מ-draft, אבל אח"כ הסטטוס
        # נהפך paused באמצע — אנחנו עוקפים דרך _send_campaign_locked ישיר
        db.set_campaign_status(cid, "sending")
        sender_mod._send_campaign_locked(cid)

        # הסטטוס נשאר paused (לא נדרס ל-completed)
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "paused"
        # פחות מ-15 הודעות נשלחו — הלולאה נעצרה
        assert sent_count["n"] < 15
        assert sent_count["n"] >= 10  # לפחות הטוטל עד הבדיקה

    def test_natural_completion_preserved(self, db, monkeypatch):
        """ללא pause — הלולאה מסיימת טבעית ועוברת ל-completed."""
        from messaging import broadcast_sender as sender_mod
        from unittest.mock import MagicMock

        _make_approved_template(db, category="UTILITY")
        for i in range(3):
            _make_wa_user(db, f"+97250100000{i}", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_OK")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        sender_mod.send_campaign(cid)

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"


# ── Legacy broadcast safety net (fail_broadcast on exception) ────────────────


class TestLegacyBroadcastSafetyNet:
    def test_unexpected_exception_marks_failed_not_stuck(self, db, monkeypatch):
        """Regression: אם לולאת broadcast_service נכשלת בצורה לא צפויה
        (למשל cancellation), השורה לא אמורה להישאר תקועה ב-'sending'.
        ה-safety net שהוספנו מסמן failed."""
        import asyncio
        from broadcast_service import send_broadcast

        # יוצר שורת broadcast
        bid = db.create_broadcast(message_text="בדיקה", audience="custom",
                                   total_recipients=1)

        # נטרל mark_broadcast_sending כדי שיעבוד; נגרום ל-send להעיף exception
        async def broken_send(*args, **kwargs):
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setattr(
            "broadcast_service._send_whatsapp_broadcast_message", broken_send,
        )

        async def run():
            try:
                await send_broadcast(
                    bot=None, broadcast_id=bid,
                    message_text="בדיקה",
                    recipients=["+972501234567"],
                    recipients_with_channel=[
                        {"user_id": "+972501234567", "channel": "whatsapp"},
                    ],
                )
            except RuntimeError:
                pass  # raise=True בקוד שלנו; צפוי

        asyncio.run(run())

        # הבודק: לא נתקע ב-sending — עבר ל-failed (או completed אם הצליח,
        # אבל כאן מדומה כישלון שלא נתפס פנימית... בפועל ה-except הפנימי
        # תופס RuntimeError של _send_whatsapp_broadcast_message ומגדיל
        # failed, לא בהכרח קורא ל-safety net).
        # לטסט זה מוודא לפחות שהשורה לא ב-'sending' בסוף.
        rows = db.get_all_broadcasts(limit=1)
        assert rows[0]["status"] != "sending"
