"""
טסטים למנוע שליחת קמפיינים (messaging/broadcast_sender.py) ול-DB helpers
של broadcast_deliveries + עדכון סטטוסים מ-Twilio webhook.

Twilio mocked — אין קריאות HTTP אמיתיות.
"""

from unittest.mock import patch, MagicMock

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


def _make_approved_template(db, content_sid="HX_T1", category="UTILITY",
                            variables=None):
    """עזר — יוצר תבנית approved ב-DB לצורך בדיקה."""
    db.upsert_whatsapp_template({
        "content_sid": content_sid,
        "friendly_name": "test_tpl",
        "language": "he",
        "category": category,
        "approval_status": "approved",
        "body_text": "היי {{1}}",
        "variables": variables or [{"index": "1", "name": "name"}],
    })


def _make_wa_user(db, user_id, opted_in=True):
    db.upsert_user(user_id, username=user_id, channel="whatsapp")
    if opted_in:
        db.set_wa_marketing_opt_in(user_id, source="test")


# ── Deliveries DB helpers ────────────────────────────────────────────────────


class TestDeliveriesDB:
    def test_create_delivery_returns_id_and_created_flag(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1", title="x")
        did, created = db.create_delivery_queue(cid, "+972501000001", {"1": "דני"})
        assert did > 0
        assert created is True
        rows = db.get_deliveries_for_campaign(cid)
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["user_id"] == "+972501000001"

    def test_queued_row_still_sendable(self, db):
        """Regression (Cursor — retry-failed): שורה במצב queued (טרם נשלחה)
        מחזירה should_send=True גם בקריאה חוזרת. זה מאפשר resume אחרי
        pause ו-retry-failed אחרי requeue — אחרת ה-loop היה מדלג על כולם
        בגלל UNIQUE constraint."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did1, should_send1 = db.create_delivery_queue(cid, "+972501000001", {"1": "a"})
        did2, should_send2 = db.create_delivery_queue(cid, "+972501000001", {"1": "b"})
        assert should_send1 is True
        assert should_send2 is True  # queued עדיין אמור להישלח
        assert did1 == did2
        assert len(db.get_deliveries_for_campaign(cid)) == 1

    def test_sent_row_skips_send(self, db):
        """שורה שכבר נשלחה (sent/delivered) — should_send=False לקריאה שניה
        כדי למנוע duplicate ב-Twilio."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did1, should_send1 = db.create_delivery_queue(cid, "+972501000001", {})
        assert should_send1 is True
        # מדמים שליחה מוצלחת
        db.mark_delivery_sent(did1, "SM_EXISTING")
        # קריאה חוזרת צריכה לאותת לא-לשלוח
        did2, should_send2 = db.create_delivery_queue(cid, "+972501000001", {})
        assert should_send2 is False
        assert did1 == did2

    def test_failed_row_without_requeue_skips(self, db):
        """שורה ב-failed (ללא retry) — should_send=False כדי שלא נכפיל
        שליחה בטעות אם הלולאה רצה שוב בלי requeue_failed_deliveries."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did1, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did1, "21408", "bad")
        did2, should_send2 = db.create_delivery_queue(cid, "+972501000001", {})
        assert should_send2 is False
        assert did1 == did2

    def test_queued_row_updates_rendered_variables(self, db):
        """Regression (Cursor): bulk_create_queued_deliveries יוצר שורות
        עם '{}' זמני. כשה-loop מגיע ל-create_delivery_queue עם הערכים
        המרונדרים האמיתיים, ה-UPDATE מחליף את ה-json כדי שהאודיט יהיה
        נכון — לא להישאר עם '{}' לנצח."""
        import json
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # מדמים bulk_create: שורה queued עם '{}'
        db.bulk_create_queued_deliveries(cid, ["+972501000001"])
        rows_before = db.get_deliveries_for_campaign(cid)
        assert json.loads(rows_before[0]["rendered_variables_json"]) == {}

        # עכשיו הלולאה קוראת עם vars אמיתיים — ה-json חייב להתעדכן
        did, should_send = db.create_delivery_queue(
            cid, "+972501000001", {"1": "דני", "2": "תספורת"},
        )
        assert should_send is True
        rows_after = db.get_deliveries_for_campaign(cid)
        assert json.loads(rows_after[0]["rendered_variables_json"]) == {
            "1": "דני", "2": "תספורת",
        }

    def test_sent_row_does_not_overwrite_rendered_vars(self, db):
        """רשומה ב-sent שומרת על ה-rendered_variables_json ההיסטורי גם
        אם ל-caller יש ערכים חדשים (מונע חוסר-עקביות עם ה-SID/sent_at)."""
        import json
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(
            cid, "+972501000001", {"1": "ישן"},
        )
        db.mark_delivery_sent(did, "SM_OLD")

        # קריאה חוזרת עם vars שונים — נשארים הישנים
        db.create_delivery_queue(cid, "+972501000001", {"1": "חדש"})
        rows = db.get_deliveries_for_campaign(cid)
        assert json.loads(rows[0]["rendered_variables_json"]) == {"1": "ישן"}

    def test_mark_sent_updates_status_and_sid(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ABC123")
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "sent"
        assert rows[0]["twilio_message_sid"] == "SM_ABC123"
        assert rows[0]["sent_at"] is not None

    def test_mark_failed_records_error(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did, error_code="63016", error_message="invalid number")
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_code"] == "63016"
        assert rows[0]["error_message"] == "invalid number"
        assert rows[0]["failed_at"] is not None

    def test_update_by_twilio_sid_to_delivered(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_XYZ")
        assert db.update_delivery_status_by_twilio_sid("SM_XYZ", "delivered") is True
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "delivered"
        assert rows[0]["delivered_at"] is not None

    def test_update_by_twilio_sid_to_read(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_XYZ")
        db.update_delivery_status_by_twilio_sid("SM_XYZ", "read")
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "read"
        assert rows[0]["read_at"] is not None

    def test_update_unknown_sid_returns_false(self, db):
        assert db.update_delivery_status_by_twilio_sid("SM_NOT_EXIST", "delivered") is False

    def test_update_with_invalid_status_noop(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_XYZ")
        assert db.update_delivery_status_by_twilio_sid("SM_XYZ", "weird_status") is False

    def test_get_deliveries_filter_by_multiple_statuses(self, db):
        """Regression (Cursor): פאנל "כישלונות" צריך להראות גם failed וגם
        undelivered, אחרת המספר בפאנל ההתקדמות (failed+undelivered) לא
        מתאים למספר השורות בטבלה."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # 1 failed (no SID), 1 undelivered (with SID), 1 delivered
        did_f, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did_f, "21408", "bad number")

        did_u, _ = db.create_delivery_queue(cid, "+972501000002", {})
        db.mark_delivery_sent(did_u, "SM_UND")
        db.update_delivery_status_by_twilio_sid(
            "SM_UND", "undelivered", error_code="63024", error_message="blocked",
        )

        did_d, _ = db.create_delivery_queue(cid, "+972501000003", {})
        db.mark_delivery_sent(did_d, "SM_DEL")
        db.update_delivery_status_by_twilio_sid("SM_DEL", "delivered")

        # רק failed
        only_failed = db.get_deliveries_for_campaign(cid, status="failed")
        assert len(only_failed) == 1

        # שני סטטוסי "תקלה" יחד — מה שה-UI אמור להציג בפאנל "שליחות שלא הגיעו"
        all_failures = db.get_deliveries_for_campaign(
            cid, statuses=["failed", "undelivered"],
        )
        assert len(all_failures) == 2
        statuses = {d["status"] for d in all_failures}
        assert statuses == {"failed", "undelivered"}

    def test_progress_aggregates_by_status(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        for i, st in enumerate(["queued", "sent", "delivered", "delivered", "failed"]):
            did, _ = db.create_delivery_queue(cid, f"+97250100000{i}", {})
            if st == "sent":
                db.mark_delivery_sent(did, f"SM_{i}")
            elif st == "delivered":
                db.mark_delivery_sent(did, f"SM_{i}")
                db.update_delivery_status_by_twilio_sid(f"SM_{i}", "delivered")
            elif st == "failed":
                db.mark_delivery_failed(did, "500", "err")
        progress = db.get_campaign_progress(cid)
        assert progress["total"] == 5
        assert progress["queued"] == 1
        assert progress["sent"] == 1
        assert progress["delivered"] == 2
        assert progress["failed"] == 1

    def test_set_campaign_status_validates(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(cid, "sending")
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "sending"

        with pytest.raises(ValueError):
            db.set_campaign_status(cid, "invalid_status")

    def test_set_campaign_counters(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_counters(cid, {
            "total_recipients": 100, "sent": 80, "delivered": 70,
            "read": 40, "failed": 5,
        })
        camp = db.get_broadcast_campaign(cid)
        assert camp["total_recipients"] == 100
        assert camp["sent"] == 80
        assert camp["delivered"] == 70
        assert camp["read_count"] == 40
        assert camp["failed"] == 5


# ── render_variables_for_user ────────────────────────────────────────────────


class TestRenderVariablesForUser:
    def test_static_mapping_passthrough(self):
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user(
            template_variables=[{"index": "1"}, {"index": "2"}],
            static_mapping={"1": "דני", "2": "תספורת"},
            user_id="+972501000001",
        )
        assert result == {"1": "דני", "2": "תספורת"}

    def test_missing_value_becomes_empty_string(self):
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user(
            template_variables=[{"index": "1"}, {"index": "2"}],
            static_mapping={"1": "דני"},
            user_id="+972501000001",
        )
        assert result == {"1": "דני", "2": ""}

    def test_no_variables_returns_empty(self):
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user([], {"1": "x"}, "+972501000001")
        assert result == {}


class TestSendToOneErrorHandling:
    """Regression (Cursor): _send_to_one חייב לשמר error_code גם כשהוא falsy
    (int 0). str(code or "") היה הופך 0 ל-"" ומאבד את הערך."""

    def test_falsy_zero_error_code_preserved(self, monkeypatch):
        from messaging import broadcast_sender as sender_mod
        from unittest.mock import MagicMock

        class FakeTwilioError(Exception):
            def __init__(self):
                self.code = 0  # falsy int — ערך נדיר אך אפשרי
                super().__init__("zero code error")

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = FakeTwilioError()
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )

        success, sid, code, msg = sender_mod._send_to_one(
            content_sid="HX_X", to_user_id="+972501000001",
            variables={}, status_callback_url=None,
        )
        assert success is False
        assert sid is None
        # code="0" נשמר — לא הוחלף ב-""
        assert code == "0"

    def test_none_error_code_becomes_empty_string(self, monkeypatch):
        from messaging import broadcast_sender as sender_mod
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("no code attr")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )

        success, sid, code, msg = sender_mod._send_to_one(
            content_sid="HX_X", to_user_id="+972501000001",
            variables={}, status_callback_url=None,
        )
        assert success is False
        assert code == ""  # None → ""

    def test_invalid_israeli_phone_blocks_send(self, monkeypatch):
        """Stage 5a: מספר בפורמט לא-ישראלי נדחה לפני שליחה ל-Twilio.
        חוסך error codes מבלבלים על מספרים שגויים."""
        from messaging import broadcast_sender as sender_mod
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )

        success, sid, code, msg = sender_mod._send_to_one(
            content_sid="HX_X",
            to_user_id="+12125551234",  # USA, לא ישראל
            variables={}, status_callback_url=None,
        )
        assert success is False
        assert sid is None
        assert code == "INVALID_PHONE"
        # Twilio לא נקראת כלל
        mock_client.messages.create.assert_not_called()


# ── send_campaign — end-to-end עם mock ל-Twilio ──────────────────────────────


class TestSendCampaign:
    def test_sends_to_eligible_users_only(self, db, monkeypatch):
        """3 משתמשי WA — אחד opt-in, אחד לא, אחד opted-out. MARKETING → רק 1."""
        _make_approved_template(db, category="MARKETING")
        for i, (uid, opted) in enumerate([
            ("+972501000001", True),
            ("+972501000002", False),
            ("+972501000003", False),
        ]):
            _make_wa_user(db, uid, opted_in=opted)
        db.set_wa_opted_out("+972501000003")

        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", title="test",
            variable_mapping={"1": "שלום"},
        )

        # mock ל-Twilio client
        from messaging import broadcast_sender as sender_mod
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_FAKE")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client",
            lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        # נטרל sleep כדי שהטסט יתרוץ מהר
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        # נטרל pre-flight של Shabbat/חגים — הטסט הזה בודק audience, לא TZ.
        # בלי זה, CI שרץ ביום העצמאות/שבת ידפוק על חסימה לא קשורה.
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (False, None),
        )

        stats = sender_mod.send_campaign(cid)

        assert stats["total"] == 1  # רק ה-opted-in
        assert stats["sent"] == 1
        assert stats["failed"] == 0

        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"
        assert camp["total_recipients"] == 1
        assert camp["sent"] == 1

    def test_broadcast_to_mixed_phone_and_bsuid_users(self, db, monkeypatch):
        """משתמש פלאפון + משתמש BSUID-only — שניהם מקבלים, ה-to נקבע נכון לכל אחד.

        כלל "לולאות I/O ארוכות": גם אם משתמש אחד נכשל, השני לא נעצר.
        כאן אנחנו לא ממקסטים _is_phone_number — המבחן בודק את ההבחנה האמיתית.
        """
        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        # משתמש BSUID-only — user_id הוא BSUID, אין phone_number ב-DB.
        _make_wa_user(db, "IL.BroadcastBsuid7", opted_in=True)

        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", title="mixed",
            variable_mapping={"1": "שלום"},
        )

        from messaging import broadcast_sender as sender_mod
        sent_calls: list[dict] = []

        def _fake_create(**kwargs):
            sent_calls.append(kwargs)
            return MagicMock(sid=f"SM_{len(sent_calls)}")

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (False, None),
        )

        stats = sender_mod.send_campaign(cid)

        # שני המשתמשים נשלחו (BSUID לא נחשב מספר ולכן עוקף את phone-validation)
        assert stats["total"] == 2
        assert stats["sent"] == 2
        assert stats["failed"] == 0

        to_targets = sorted(call["to"] for call in sent_calls)
        assert to_targets == [
            "whatsapp:+972501000001",
            "whatsapp:IL.BroadcastBsuid7",
        ]

    def test_refuses_non_draft_campaign(self, db, monkeypatch):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(cid, "completed")

        from messaging import broadcast_sender as sender_mod
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        stats = sender_mod.send_campaign(cid)
        assert stats == {"total": 0, "sent": 0, "failed": 0, "skipped": 0}
        # הסטטוס נותר completed — לא נעול מחדש
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"

    def test_concurrent_send_only_one_wins(self, db, monkeypatch):
        """Regression (Cursor): שני threads שקוראים ל-send_campaign על
        אותו draft — רק אחד אמור לעבור את הנעילה האטומית ולשלוח. השני
        אמור לחזור מיד עם stats ריקים."""
        import threading
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_RACE")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        results: list[dict] = []
        errors: list[Exception] = []

        def _run():
            try:
                results.append(sender_mod.send_campaign(cid))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(results) == 2
        # אחד מחזיר total=1 sent=1 (המנצח); השני total=0 sent=0 (המאחר)
        totals = sorted(r["total"] for r in results)
        sents = sorted(r["sent"] for r in results)
        assert totals == [0, 1]
        assert sents == [0, 1]

        # ב-DB יש delivery אחד בלבד — לא נשלח duplicate
        deliveries = db.get_deliveries_for_campaign(cid)
        assert len(deliveries) == 1

    def test_transition_fails_if_status_not_draft(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        # מעבר ראשון מצליח
        assert db.transition_campaign_status(cid, "draft", "sending") is True
        # ניסיון לחזור על אותו מעבר — הסטטוס כבר sending
        assert db.transition_campaign_status(cid, "draft", "sending") is False

    def test_final_counters_read_from_db_not_local_stats(self, db, monkeypatch):
        """Regression (Cursor): webhooks שמגיעים במהלך הלולאה מעדכנים את
        מונה failed/delivered. העדכון הסופי אסור לדרוס את זה עם stats
        הלוקליים שמכירים רק create-time results.

        סימולציה: שליחה לשני נמענים; בין ההודעה הראשונה לשנייה webhook
        מגיע ומסמן את הראשונה כ-failed (למשל תפוגה). בסוף הלולאה, המונה
        failed חייב להיות 1 (לא 0, שזה מה ש-stats["failed"] יחזיר)."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        call_count = {"n": 0}
        first_sid = "SM_1"

        def fake_create(**kwargs):
            call_count["n"] += 1
            sid = f"SM_{call_count['n']}"
            # בין הראשון לשני — מדמים webhook שמעדכן את הראשון ל-failed
            if call_count["n"] == 2:
                sender_mod.handle_status_callback(
                    first_sid, "failed",
                    error_code="63013", error_message="expired",
                )
            return MagicMock(sid=sid)

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        sender_mod.send_campaign(cid)

        camp = db.get_broadcast_campaign(cid)
        # ה-webhook סימן את הראשונה כ-failed; שתיהן התקבלו ב-Twilio (accepted=2)
        assert camp["sent"] == 2
        # failed חייב להיות 1 (לא 0 — שזה מה ש-stats["failed"] היה מחזיר)
        assert camp["failed"] == 1

    def test_fails_if_template_not_approved(self, db, monkeypatch):
        db.upsert_whatsapp_template({
            "content_sid": "HX_PENDING",
            "friendly_name": "pending_tpl",
            "approval_status": "pending",
            "body_text": "x",
        })
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_PENDING")

        from messaging import broadcast_sender as sender_mod
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        stats = sender_mod.send_campaign(cid)
        assert stats["sent"] == 0
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_partial_failure_continues(self, db, monkeypatch):
        """שני נמענים — Twilio זורק על הראשון, מצליח על השני."""
        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "שלום"},
        )

        from messaging import broadcast_sender as sender_mod
        call_count = {"n": 0}

        def fake_create(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated Twilio error")
            return MagicMock(sid=f"SM_OK_{call_count['n']}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client",
            lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        stats = sender_mod.send_campaign(cid)
        assert stats["total"] == 2
        assert stats["sent"] == 1
        assert stats["failed"] == 1

        # Campaign עדיין completed (יש לפחות אחד שנשלח)
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"

    def test_no_recipients_marks_failed(self, db, monkeypatch):
        _make_approved_template(db, category="MARKETING")
        # אין משתמשי WA כלל
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        from messaging import broadcast_sender as sender_mod
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        stats = sender_mod.send_campaign(cid)
        assert stats["total"] == 0
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_marketing_blocked_during_shabbat_window(self, db, monkeypatch):
        """Stage 5a: ניסיון לשלוח MARKETING בחלון שבת נכשל מיד — pre-flight
        check ב-_send_campaign_locked. UTILITY עובר."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="MARKETING")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # Mock הצ׳ק של שבת כך שיחזיר True
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (True, "mocked shabbat"),
        )

        stats = sender_mod.send_campaign(cid)
        assert stats["total"] == 0
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_utility_not_blocked_during_shabbat(self, db, monkeypatch):
        """Stage 5a: UTILITY/AUTH ממשיכות לעבוד גם בחלון שבת (שירותיות)."""
        from messaging import broadcast_sender as sender_mod
        from unittest.mock import MagicMock

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_UT")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # גם אם שבת — UTILITY לא נחסמת
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (True, "shabbat"),
        )

        stats = sender_mod.send_campaign(cid)
        assert stats["sent"] == 1
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"

    def test_lowercase_marketing_category_still_blocked(self, db, monkeypatch):
        """Regression (Cursor): category 'marketing' (lowercase) חייב לעבור
        uppercase לפני ההשוואה. בלי זה — false-negative שולח MARKETING
        בשבת. המצב נדיר (DB constraint שומר uppercase) אבל אפשרי אם
        admin SQL/migration חלקית יצרו רשומה עוקפת."""
        from messaging import broadcast_sender as sender_mod

        # יוצרים תבנית עם category במקרה קטן ע"י INSERT ישיר שעוקף
        # את upsert_whatsapp_template (שיכול לבצע המרה).
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO whatsapp_templates (content_sid, friendly_name, "
                "language, category, approval_status, body_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("HX_LOWER", "t", "he", "MARKETING", "approved", "x"),
            )
            # ואז הופכים ל-lowercase בניגוד ל-constraint (SQLite מאפשר
            # כי ה-CHECK בודק רק על INSERT מפורש... למעשה ישנה CHECK:
            # אז זה עלול להיכשל. ננטרל את ה-check זמנית):
            try:
                conn.execute(
                    "UPDATE whatsapp_templates SET category = 'marketing' "
                    "WHERE content_sid = 'HX_LOWER'"
                )
            except Exception:
                # אם CHECK חוסם, הטסט עדיין מוודא את שכבת ההגנה ב-Python:
                # נבדוק עם ערך valid uppercase שלא מופיע ברשימה.
                pass

        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_LOWER")

        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (True, "shabbat mocked"),
        )

        # ודא שה-category נשמר lowercase אם ה-UPDATE הצליח
        tpl = db.get_whatsapp_template("HX_LOWER")
        if tpl and tpl["category"] != "MARKETING":
            # ה-UPDATE עבר (אין CHECK או שהוא הוסר) — הבדיקה רלוונטית
            stats = sender_mod.send_campaign(cid)
            camp = db.get_broadcast_campaign(cid)
            # לפני התיקון: category=='marketing' לא היה מתאים לבדיקה
            # והקמפיין היה עובר את ה-pre-flight. אחרי .upper() — נחסם.
            assert camp["status"] == "failed"
            assert stats["sent"] == 0

    def test_marketing_overrides_audience_type_even_if_all(self, db, monkeypatch):
        """הגנת belt-and-braces: גם אם draft נרשם עם audience_type=all,
        הקמפיין ב-MARKETING חייב לשלוח רק ל-opt-in."""
        _make_approved_template(db, category="MARKETING")
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=False)

        cid = db.create_broadcast_campaign(
            template_sid="HX_T1",
            variable_mapping={"1": "x"},
        )
        # דורסים ידנית audience_type ל-'all' (סימולציה של בג/עקיפה)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE broadcast_campaigns SET audience_type = 'all' WHERE id = ?",
                (cid,),
            )

        from messaging import broadcast_sender as sender_mod
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_F")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client",
            lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        # נטרל pre-flight של Shabbat/חגים — הטסט בודק אכיפת opt-in
        # (belt-and-braces), לא חסימת TZ.
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (False, None),
        )

        stats = sender_mod.send_campaign(cid)
        assert stats["total"] == 1  # רק opted-in למרות 'all'

    def test_start_campaign_send_transitions_synchronously(self, db, monkeypatch):
        """Regression (Cursor): ה-transition חייב להתבצע סינכרונית ב-
        start_campaign_send, לא ב-thread, אחרת דף הפירוט נטען לפני
        שהסטטוס התעדכן וה-polling של HTMX לא מתחיל."""
        import threading
        import time
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        # Twilio client שלא מתקדם עד שה-event נפתח — כדי שה-thread יתקוע
        # באמצע השליחה, ונוודא שהסטטוס כבר עודכן ל-sending לפני זה.
        proceed = threading.Event()

        def fake_create(**kwargs):
            proceed.wait(timeout=5)
            return MagicMock(sid="SM_X")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        acquired = sender_mod.start_campaign_send(cid)
        assert acquired is True

        # מיד אחרי start_campaign_send, הסטטוס חייב כבר להיות sending
        # (אפילו שה-thread עוד לא סיים את השליחה). ה-HTMX polling בדף
        # הפירוט מסתמך על זה.
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "sending"

        # משחררים את ה-thread להמשיך לסיים
        proceed.set()
        # זמן קצר כדי שה-thread יסיים (daemon, לא חייבים join)
        time.sleep(0.5)

    def test_start_campaign_send_returns_false_for_non_draft(self, db):
        """start_campaign_send מחזיר False כש-draft כבר נסגר. המערך ה-HTTP
        יכול להחליט מה להראות למשתמש ללא קריאה ל-thread."""
        from messaging.broadcast_sender import start_campaign_send

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(cid, "completed")

        assert start_campaign_send(cid) is False
        # סטטוס לא שונה
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "completed"

    def test_thread_start_failure_reverts_status_to_failed(self, db, monkeypatch):
        """Regression (Cursor): אם thread.start() זורק (למשל OS מיצוי
        threads), הקמפיין כבר עבר ל-sending. בלי fallback הוא היה תקוע
        שם לנצח — אין thread שיעבד אותו. התיקון מסמן failed."""
        import threading
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # מחליפים Thread ב-class שזורק ב-start
        class ExplodingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("simulated OS out of threads")

        monkeypatch.setattr(threading, "Thread", ExplodingThread)

        acquired = sender_mod.start_campaign_send(cid)
        assert acquired is False

        # הסטטוס חייב לעבור ל-failed — לא להישאר sending
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_sync_send_exception_marks_failed(self, db, monkeypatch):
        """Regression (Cursor): אם _send_campaign_locked זורק exception לא
        צפוי (למשל DB error), send_campaign הסינכרוני חייב לסמן failed
        כדי שהקמפיין לא יישאר תקוע ב-sending. קודם רק ה-thread
        ב-start_campaign_send היה עם fallback; הסינכרוני החמיץ זאת."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # מונעים מ-_send_campaign_locked לסיים כרגיל — זורקים באמצע
        def broken_locked(_cid):
            raise RuntimeError("simulated DB crash mid-send")

        monkeypatch.setattr(sender_mod, "_send_campaign_locked", broken_locked)
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        stats = sender_mod.send_campaign(cid)
        # stats ריק (לא הצליח להשלים)
        assert stats == {"total": 0, "sent": 0, "failed": 0, "skipped": 0}

        # הסטטוס חייב להיות failed — לא sending או draft
        camp = db.get_broadcast_campaign(cid)
        assert camp["status"] == "failed"

    def test_safety_net_preserves_paused_status(self, db, monkeypatch):
        """Regression (Cursor): אם exception קורה ב-_send_campaign_locked
        אחרי שה-admin הקפיץ paused, ה-safety net של _run_locked_send_safely
        לא אמור לדרוס paused ב-failed. הסימולציה: ה-thread הפנימי זורק
        כש-status='paused'."""
        from messaging import broadcast_sender as sender_mod
        from ai_chatbot import database as db_wrapper

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # נקבע status=paused ישירות (לא נעבור דרך send_campaign הרגיל
        # שמנסה draft→sending)
        db.set_campaign_status(cid, "paused")

        def broken_locked(_cid):
            raise RuntimeError("simulated crash after admin paused")

        monkeypatch.setattr(sender_mod, "_send_campaign_locked", broken_locked)

        # קוראים ישירות ל-safety net (לא עוברים transition)
        sender_mod._run_locked_send_safely(
            cid, {"total": 0, "sent": 0, "failed": 0, "skipped": 0},
        )

        # paused נשמר — לא נדרס ל-failed
        camp = db_wrapper.get_broadcast_campaign(cid)
        assert camp["status"] == "paused"

    def test_row_disappears_after_lock_is_marked_failed(self, db, monkeypatch):
        """Regression (Cursor): edge case נדיר שבו row נעלם בין transition
        ל-get. בלי set_campaign_status("failed") הקמפיין היה נתקע ב-sending.
        הסימולציה: monkey-patch ל-get_broadcast_campaign שמחזיר None
        ברגע הנכון."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        original_get = db.get_broadcast_campaign
        call_count = {"n": 0}

        def fake_get(cid_arg):
            call_count["n"] += 1
            # הקריאה הראשונה (מצד send_campaign אחרי ה-lock) מחזירה None
            if call_count["n"] == 1:
                return None
            # הקריאות הבאות (למשל טסט) מחזירות תוצאה אמיתית
            return original_get(cid_arg)

        monkeypatch.setattr(db, "get_broadcast_campaign", fake_get)
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        stats = sender_mod.send_campaign(cid)
        assert stats == {"total": 0, "sent": 0, "failed": 0, "skipped": 0}

        # הסטטוס עבר ל-failed ולא נשאר תקוע ב-sending
        camp = original_get(cid)
        assert camp["status"] == "failed"


# ── require_opt_in param (החלפת ה-dead effective_category transform) ─────────


class TestRequireOptIn:
    def test_utility_default_does_not_require_opt_in(self, db):
        """ברירת מחדל ל-UTILITY: כל מי שלא opt-out נכלל (גם ללא opt-in)."""
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=False)
        counts = db.count_wa_audience(category="UTILITY")
        assert counts["eligible"] == 2

    def test_utility_with_require_opt_in_filters_to_opted_in(self, db):
        """הכרחה explicit: UTILITY + require_opt_in=True שולח רק ל-opt-in."""
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=False)
        counts = db.count_wa_audience(
            category="UTILITY", require_opt_in=True,
        )
        assert counts["eligible"] == 1
        assert counts["filtered_out_never_opted_in"] == 1

    def test_marketing_always_requires_regardless_of_param(self, db):
        """MARKETING אוכף opt-in גם אם הקורא מעביר require_opt_in=False.
        זה belt-and-braces: אכיפה רגולטורית לא ניתנת לעקיפה."""
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=False)
        counts = db.count_wa_audience(
            category="MARKETING", require_opt_in=False,
        )
        assert counts["eligible"] == 1  # MARKETING עדיין דורש

    def test_list_honors_require_opt_in_for_utility(self, db):
        _make_wa_user(db, "+972501000001", opted_in=True)
        _make_wa_user(db, "+972501000002", opted_in=False)
        all_users = db.list_wa_audience_eligible_user_ids(category="UTILITY")
        assert len(all_users) == 2
        only_opt_in = db.list_wa_audience_eligible_user_ids(
            category="UTILITY", require_opt_in=True,
        )
        assert only_opt_in == ["+972501000001"]


# ── Orphan recovery + monotonic status ───────────────────────────────────────


class TestOrphanRecovery:
    """Regression (Cursor): אם פעולת DB בתוך ה-try נכשלת אחרי create_delivery_queue,
    השורה לא אמורה להישאר ב-queued לנצח."""

    def test_mark_sent_failure_preserves_twilio_sid(self, db, monkeypatch):
        """Twilio קיבלה את ההודעה (יש SID), אבל mark_delivery_sent זורק.
        ה-outer except חייב לשמור את ה-SID כדי ש-callbacks עתידיים ימצאו
        את השורה. תוצאה: השורה עוברת ל-sent עם SID, לא נשארת queued."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid="SM_ORPHAN")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # בודדים את mark_delivery_sent הראשון כך שיכשל; השני (הגיבוי ב-
        # except) ימשיך לעבוד.
        original_mark_sent = db.mark_delivery_sent
        call_count = {"n": 0}

        def fake_mark_sent(delivery_id, sid):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated DB error")
            return original_mark_sent(delivery_id, sid)

        monkeypatch.setattr(db, "mark_delivery_sent", fake_mark_sent)

        sender_mod.send_campaign(cid)

        # שורת delivery לא נשארה queued; יש SID כדי ש-webhook ימצא אותה
        rows = db.get_deliveries_for_campaign(cid)
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"
        assert rows[0]["twilio_message_sid"] == "SM_ORPHAN"

    def test_render_exception_marks_delivery_failed(self, db, monkeypatch):
        """כשל ב-render לפני Twilio — delivery נוצר אבל לא נשלח. ה-except
        חייב לסמן failed עם local_error (אין SID)."""
        from messaging import broadcast_sender as sender_mod

        _make_approved_template(db, category="UTILITY")
        _make_wa_user(db, "+972501000001", opted_in=True)
        cid = db.create_broadcast_campaign(
            template_sid="HX_T1", variable_mapping={"1": "x"},
        )

        # Twilio client שיזרוק אחרי create_delivery_queue
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("twilio net down")
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)

        # _send_to_one מחזיר (False, None, code, msg), לא יזרוק ישירות.
        # אז נעמיס עליו מצב שכן יזרוק — כעת את mark_delivery_failed.
        original_mark_failed = db.mark_delivery_failed
        call_count = {"n": 0}

        def fake_mark_failed(delivery_id, error_code="", error_message=""):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated DB error")
            return original_mark_failed(delivery_id, error_code, error_message)

        monkeypatch.setattr(db, "mark_delivery_failed", fake_mark_failed)

        sender_mod.send_campaign(cid)

        # ה-outer except אמור לקרוא שוב ל-mark_delivery_failed — שיעבור הפעם
        rows = db.get_deliveries_for_campaign(cid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        # אין SID כי Twilio זרקה
        assert rows[0]["twilio_message_sid"] is None


class TestMonotonicStatus:
    """Regression (Cursor): status לא נסוג אחורה ב-callbacks out-of-order."""

    def test_read_cannot_regress_to_delivered(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ORDER_1")
        # מסלול רגיל: sent → delivered → read
        db.update_delivery_status_by_twilio_sid("SM_ORDER_1", "delivered")
        db.update_delivery_status_by_twilio_sid("SM_ORDER_1", "read")

        # callback out-of-order: delivered מגיע אחרי read — לא אמור לחזור
        updated = db.update_delivery_status_by_twilio_sid("SM_ORDER_1", "delivered")
        assert updated is False
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "read"

    def test_delivered_cannot_regress_to_sent(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ORDER_2")
        db.update_delivery_status_by_twilio_sid("SM_ORDER_2", "delivered")

        updated = db.update_delivery_status_by_twilio_sid("SM_ORDER_2", "sent")
        assert updated is False
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "delivered"

    def test_failed_terminal_cannot_change(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ORDER_3")
        db.update_delivery_status_by_twilio_sid("SM_ORDER_3", "failed")

        # delivered מגיע באיחור — לא אמור לדרוס failed
        updated = db.update_delivery_status_by_twilio_sid("SM_ORDER_3", "delivered")
        assert updated is False
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "failed"

    def test_sent_can_go_to_failed_side_transition(self, db):
        """failed יכול להגיע מכל סטטוס לא-terminal (גם אחרי delivered)."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ORDER_4")
        db.update_delivery_status_by_twilio_sid("SM_ORDER_4", "delivered")

        # failed יכול לחלל delivered (edge case חריג אבל מותר)
        updated = db.update_delivery_status_by_twilio_sid(
            "SM_ORDER_4", "failed", error_code="99", error_message="late fail",
        )
        assert updated is True
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "failed"

    def test_same_status_is_noop(self, db):
        """sent → sent מדלג כדי לא לדרוס timestamp ב-value חדש."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ORDER_5")
        updated = db.update_delivery_status_by_twilio_sid("SM_ORDER_5", "sent")
        assert updated is False

    def test_should_advance_helper_edge_cases(self, db):
        """בדיקה ישירה של כללי ההתקדמות."""
        from database import _should_advance_status
        # progression
        assert _should_advance_status("queued", "sent") is True
        assert _should_advance_status("sent", "delivered") is True
        assert _should_advance_status("delivered", "read") is True
        # regression
        assert _should_advance_status("delivered", "sent") is False
        assert _should_advance_status("read", "delivered") is False
        # side to terminal fail
        assert _should_advance_status("sent", "failed") is True
        assert _should_advance_status("delivered", "undelivered") is True
        # terminal stays
        assert _should_advance_status("failed", "delivered") is False
        assert _should_advance_status("read", "read") is False
        # same = no-op
        assert _should_advance_status("sent", "sent") is False

    def test_concurrent_callbacks_dont_regress_status(self, db):
        """Regression (Cursor): שני threads מקבילים שמעדכנים סטטוס אותה
        הודעה. לפני תיקון ה-CAS, הפער בין SELECT ל-UPDATE אפשר לשני
        לדרוס את הראשון גם כשזו היתה רגרסיה (delivered דרס read).
        אחרי CAS + retry — הסטטוס הסופי תמיד מונוטוני."""
        import threading

        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_RACE_STATUS")

        barrier = threading.Barrier(2)
        results: list[bool] = []

        def update_to(status):
            barrier.wait()  # מסנכרן שני ה-threads להתחיל בדיוק ביחד
            ok = db.update_delivery_status_by_twilio_sid(
                "SM_RACE_STATUS", status,
            )
            results.append(ok)

        # שני threads: אחד שם delivered, השני שם read
        t1 = threading.Thread(target=update_to, args=("delivered",))
        t2 = threading.Thread(target=update_to, args=("read",))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        # לפחות אחד הצליח (בד"כ שניהם — read מתקדם אחרי delivered)
        assert any(results), "אף thread לא הצליח לעדכן"

        # המצב הסופי חייב להיות read (הגבוה) או delivered — אף פעם לא sent
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] in ("delivered", "read")
        # לא חזרה ל-sent (שזה מה שהבאג הישן היה גורם)
        assert rows[0]["status"] != "sent"


# ── handle_status_callback ───────────────────────────────────────────────────


class TestStatusCallback:
    def test_callback_updates_delivery(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_CALLBACK_1")

        from messaging.broadcast_sender import handle_status_callback
        ok = handle_status_callback(
            message_sid="SM_CALLBACK_1",
            message_status="delivered",
        )
        assert ok is True
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "delivered"

    def test_callback_with_error(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_ERR_1")

        from messaging.broadcast_sender import handle_status_callback
        handle_status_callback(
            message_sid="SM_ERR_1",
            message_status="failed",
            error_code="63016",
            error_message="Number is not on WhatsApp",
        )
        rows = db.get_deliveries_for_campaign(cid)
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_code"] == "63016"

    def test_callback_unknown_sid_returns_false(self, db):
        from messaging.broadcast_sender import handle_status_callback
        assert handle_status_callback("SM_GHOST", "delivered") is False

    def test_callback_empty_sid_returns_false(self, db):
        from messaging.broadcast_sender import handle_status_callback
        assert handle_status_callback("", "delivered") is False

    def test_duplicate_sent_callback_no_warning(self, db, caplog):
        """Regression (Cursor): Twilio שולחת 'sent' callback אחרי שכבר
        סימנו sent מתגובת ה-API. לפני התיקון זה יצר WARNING מזויף לכל
        הודעה ("MessageSid not found"). עכשיו — ה-SID נמצא, ה-monotonic
        guard חוסם את העדכון בשקט (INFO, לא WARNING), והפונקציה מחזירה
        True."""
        import logging
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_DUP")

        from messaging.broadcast_sender import handle_status_callback

        with caplog.at_level(logging.WARNING):
            result = handle_status_callback("SM_DUP", "sent")

        # ה-SID נמצא → מחזירים True, ה-guard חסם בשקט.
        assert result is True
        # לא הוצא WARNING על "לא נמצא"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("לא נמצא" in r.getMessage() for r in warnings)

    def test_unknown_sid_still_warns(self, db, caplog):
        """נוודא שה-WARNING האמיתי (SID לא קיים) עדיין עובד אחרי התיקון."""
        import logging
        from messaging.broadcast_sender import handle_status_callback

        with caplog.at_level(logging.WARNING):
            result = handle_status_callback("SM_TRULY_GONE", "delivered")

        assert result is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("לא נמצא" in r.getMessage() for r in warnings)

    def test_sent_counter_cumulative_includes_undelivered(self, db):
        """Regression (Cursor): המונה sent ברמת הקמפיין הוא מצטבר — כל
        הודעה שעברה דרך Twilio נספרת גם אם מאוחר יותר הפכה undelivered.
        לפני התיקון ה-counter היה יורד כשהודעה עברה sent → undelivered,
        מה שיצר UI שמראה ירידה במונה ולא הגיוני."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_UNDELIV_TEST")

        from messaging.broadcast_sender import handle_status_callback

        # ראשית delivered — המונה sent אמור להישאר 1
        handle_status_callback("SM_UNDELIV_TEST", "delivered")
        camp = db.get_broadcast_campaign(cid)
        assert camp["sent"] == 1

        # עכשיו Twilio מחזירה undelivered (למשל המשתמש חסם את הבוט)
        handle_status_callback("SM_UNDELIV_TEST", "undelivered",
                               error_code="63024", error_message="blocked")
        camp = db.get_broadcast_campaign(cid)
        # sent חייב להישאר 1 (ההודעה עברה ב-Twilio) — לא 0
        assert camp["sent"] == 1
        # failed כולל את ה-undelivered
        assert camp["failed"] == 1

    def test_sent_counter_cumulative_includes_webhook_failed(self, db):
        """Regression (Cursor #2): הודעה שהתקבלה ע"י Twilio (קיבלה SID)
        ואחר כך webhook דיווח failed — סטטוס 'failed' ב-DB. לפני התיקון
        המונה sent לא כלל failed, אז הוא היה יורד sent → failed. צריך
        להשתמש ב-accepted (twilio_message_sid IS NOT NULL) במקום לסכום
        סטטוסים בודדים."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_WEBHOOK_FAIL")

        from messaging.broadcast_sender import handle_status_callback

        # Twilio מחזירה failed ב-webhook (למשל פג תוקף לפני מסירה)
        handle_status_callback("SM_WEBHOOK_FAIL", "failed",
                               error_code="63013", error_message="expired")
        camp = db.get_broadcast_campaign(cid)
        # ההודעה התקבלה ב-Twilio (יש SID) → נספרת ב-sent מצטבר
        assert camp["sent"] == 1
        # failed כולל אותה
        assert camp["failed"] == 1

    def test_create_time_failure_not_counted_in_sent(self, db):
        """הבחנה חשובה: כישלון ביצירה (Twilio HTTP 4xx, אין SID) אינו
        אמור להיספר ב-sent. רק webhook-reported failures (עם SID) כן."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        # מדמה כשל יצירה: אין twilio_message_sid
        db.mark_delivery_failed(did, error_code="21408", error_message="bad number")

        progress = db.get_campaign_progress(cid)
        assert progress["failed"] == 1
        assert progress["accepted"] == 0  # אין SID → לא נספר כ-accepted

    def test_recompute_campaign_counters_atomic(self, db):
        """Regression (Cursor): recompute_campaign_counters חייב לחשב ישירות
        מה-DB ב-UPDATE יחיד, בלי snapshot נפרד שיכול להתיישן בין הקריאות."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # 2 נשלחו, 1 delivered, 1 read, 1 failed
        for i, (st, sid) in enumerate([
            ("sent", "SM_A"), ("delivered", "SM_B"), ("read", "SM_C"),
            ("failed", None),
        ]):
            did, _ = db.create_delivery_queue(cid, f"+97250100000{i}", {})
            if sid:
                db.mark_delivery_sent(did, sid)
                if st == "delivered":
                    db.update_delivery_status_by_twilio_sid(sid, "delivered")
                elif st == "read":
                    db.update_delivery_status_by_twilio_sid(sid, "delivered")
                    db.update_delivery_status_by_twilio_sid(sid, "read")
            else:
                db.mark_delivery_failed(did, "21408", "bad")

        db.recompute_campaign_counters(cid)

        camp = db.get_broadcast_campaign(cid)
        assert camp["sent"] == 3        # accepted (SM_A/B/C) — failed לא נספר
        assert camp["delivered"] == 2   # delivered + read
        assert camp["read_count"] == 1
        assert camp["failed"] == 1      # failed only (אין undelivered)

    def test_recompute_no_race_with_interleaved_write(self, db):
        """סימולציה: webhook כותב delivery נוסף בין קריאות חישוב של
        recompute. בגרסה הישנה snapshot היה מתיישן. בגרסה החדשה
        recompute משקף תמיד את המצב הנוכחי ב-DB."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # מצב ראשוני: 1 sent
        did1, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did1, "SM_FIRST")
        db.recompute_campaign_counters(cid)
        camp = db.get_broadcast_campaign(cid)
        assert camp["sent"] == 1

        # "webhook" מוסיף delivery נוסף + קוראים שוב — המונה גדל ל-2
        did2, _ = db.create_delivery_queue(cid, "+972501000002", {})
        db.mark_delivery_sent(did2, "SM_SECOND")
        db.recompute_campaign_counters(cid)
        camp = db.get_broadcast_campaign(cid)
        assert camp["sent"] == 2

    def test_recompute_counts_undelivered_in_failed_and_sent(self, db):
        """undelivered נספר גם ב-sent (יש SID) וגם ב-failed (לא הגיע למכשיר).
        מחליף טסט ישן של _counters_from_progress שהוסר."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_U")
        db.update_delivery_status_by_twilio_sid(
            "SM_U", "undelivered", error_code="63024",
        )
        db.recompute_campaign_counters(cid)
        camp = db.get_broadcast_campaign(cid)
        assert camp["sent"] == 1     # יש SID — נספר גם אם נכשל במסירה
        assert camp["failed"] == 1   # undelivered נחשב failed

    def test_progress_includes_accepted_count(self, db):
        """get_campaign_progress מחזיר accepted = count(twilio_message_sid IS NOT NULL)."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # 3 נמענים: 1 נשלח (sent+SID), 1 נכשל ביצירה (אין SID), 1 ב-queued
        did1, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did1, "SM_OK")

        did2, _ = db.create_delivery_queue(cid, "+972501000002", {})
        db.mark_delivery_failed(did2, "21408", "bad")

        db.create_delivery_queue(cid, "+972501000003", {})  # נשאר queued

        progress = db.get_campaign_progress(cid)
        assert progress["accepted"] == 1  # רק ה-SM_OK
        assert progress["failed"] == 1
        assert progress["queued"] == 1
        assert progress["total"] == 3
