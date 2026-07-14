"""
טסטים למודול followup_service — ניתוח לידים, בדיקות זכאות, מנוע החלטה, ושליחה.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def followup_db(tmp_path):
    """DB מאותחל עם הסכימה המלאה — כולל טבלת lead_followups."""
    db_path = tmp_path / "test.db"
    os.environ["DB_PATH"] = str(db_path)
    with patch("ai_chatbot.config.DB_PATH", db_path), \
         patch("database.DB_PATH", db_path):
        from database import init_db, get_connection
        init_db()
        with get_connection() as conn:
            yield conn


@pytest.fixture
def _enable_followup(monkeypatch):
    """מפעיל את FOLLOWUP_ENABLED לטסטים."""
    monkeypatch.setenv("FOLLOWUP_ENABLED", "true")
    monkeypatch.setattr("followup_service.FOLLOWUP_ENABLED", True)


# ── followup_config: render_template ─────────────────────────────────────────


class TestRenderTemplate:
    def test_interest_check_with_name(self):
        from followup_config import render_template

        text = render_template(
            "followup_interest_check",
            name="דניאל",
            service_name="ניקוי שיניים",
        )
        assert "דניאל" in text
        assert "ניקוי שיניים" in text

    def test_booking_resume_without_name(self):
        from followup_config import render_template

        text = render_template(
            "followup_booking_resume",
            service_name="טיפול פנים",
        )
        assert "היי," in text  # ללא שם — רווח בלבד
        assert "טיפול פנים" in text

    def test_fallback_for_unknown_template(self):
        from followup_config import render_template

        text = render_template("nonexistent_key", service_name="שירות")
        assert "שירות" in text  # fallback ל-interest_check

    def test_default_service_name(self):
        from followup_config import render_template

        text = render_template("followup_interest_check")
        assert "השירות שהתעניינת בו" in text


# ── database: CRUD functions ──────────────────────────────────────────────────


class TestDatabaseCRUD:
    def test_create_and_get_followup(self, followup_db):
        import database as db

        due_at = "2026-04-05 10:00:00"
        fid = db.create_lead_followup(
            user_id="u123",
            followup_due_at=due_at,
            username="דניאל",
            channel="telegram",
            service_of_interest="ניקוי שיניים",
            intent_type="booking_intent",
            lead_temperature="hot",
            conversation_summary="שאל על מחיר וזמינות",
        )
        assert fid > 0

        followups = db.get_all_followups()
        assert len(followups) == 1
        assert followups[0]["user_id"] == "u123"
        assert followups[0]["lead_temperature"] == "hot"
        assert followups[0]["status"] == "pending"

    def test_get_pending_followups_by_due_date(self, followup_db):
        import database as db

        # due בעבר — צריך להופיע
        db.create_lead_followup("u1", followup_due_at="2020-01-01 00:00:00")
        # due בעתיד — לא צריך להופיע
        db.create_lead_followup("u2", followup_due_at="2099-01-01 00:00:00")

        pending = db.get_pending_followups(due_before="2026-04-03 12:00:00")
        assert len(pending) == 1
        assert pending[0]["user_id"] == "u1"

    def test_has_pending_or_sent_followup(self, followup_db):
        import database as db

        assert not db.has_pending_or_sent_followup("u1")
        db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        assert db.has_pending_or_sent_followup("u1")

    def test_update_followup_status(self, followup_db):
        import database as db

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(
            fid, "sent",
            template_key="followup_interest_check",
            template_variables='{"service_name": "טיפול"}',
        )

        followups = db.get_all_followups()
        assert followups[0]["status"] == "sent"
        assert followups[0]["template_key"] == "followup_interest_check"
        assert followups[0]["followup_sent_at"] is not None

    def test_mark_followup_replied(self, followup_db):
        import database as db

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(fid, "sent")

        result = db.mark_followup_replied("u1")
        assert result is True

        followups = db.get_all_followups()
        assert followups[0]["status"] == "replied"
        assert followups[0]["user_replied"] == 1

    def test_mark_followup_replied_no_sent(self, followup_db):
        import database as db

        # אין follow-up ב-sent — מחזיר False
        result = db.mark_followup_replied("u1")
        assert result is False

    def test_mark_followup_converted(self, followup_db):
        import database as db

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(fid, "sent")
        db.mark_followup_replied("u1")

        result = db.mark_followup_converted("u1")
        assert result is True

        followups = db.get_all_followups()
        assert followups[0]["status"] == "converted"
        assert followups[0]["booking_after_followup"] == 1

    def test_expire_old_followups(self, followup_db):
        import database as db

        # ישן מאוד — צריך לפוג
        db.create_lead_followup("u1", followup_due_at="2020-01-01 00:00:00")
        expired = db.expire_old_followups(max_age_hours=72)
        assert expired == 1

        followups = db.get_all_followups()
        assert followups[0]["status"] == "expired"

    def test_get_followup_stats(self, followup_db):
        import database as db

        stats = db.get_followup_stats()
        assert stats["total"] == 0
        assert stats["reply_rate"] == 0

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(fid, "sent")

        stats = db.get_followup_stats()
        assert stats["total"] == 1
        assert stats["sent"] == 1

    def test_has_recent_booking(self, followup_db):
        import database as db

        assert not db.has_recent_booking("u1")

        # יצירת תור אחרון
        db.create_appointment(
            user_id="u1",
            username="דניאל",
            service="ניקוי",
            preferred_date="2026-04-05",
            preferred_time="10:00",
        )
        assert db.has_recent_booking("u1")


# ── followup_service: check_eligibility ──────────────────────────────────────


class TestCheckEligibility:
    def test_blocked_user(self, followup_db):
        import database as db
        from followup_service import check_eligibility

        db.block_user("u1", username="test")
        eligible, reason = check_eligibility("u1")
        assert not eligible
        assert reason == "user_blocked"

    def test_has_recent_booking(self, followup_db):
        import database as db
        from followup_service import check_eligibility

        db.create_appointment("u1", "test", "service", "2026-04-05", "10:00")
        eligible, reason = check_eligibility("u1")
        assert not eligible
        assert reason == "has_recent_booking"

    def test_unsubscribed_user(self, followup_db):
        import database as db
        from followup_service import check_eligibility

        db.ensure_user_subscribed("u1")
        db.unsubscribe_user("u1")
        eligible, reason = check_eligibility("u1")
        assert not eligible
        assert reason == "unsubscribed"

    @patch("live_chat_service.LiveChatService.is_active", return_value=True)
    def test_live_chat_active(self, mock_is_active, followup_db):
        from followup_service import check_eligibility

        eligible, reason = check_eligibility("u1")
        assert not eligible
        assert reason == "live_chat_active"

    @patch("live_chat_service.LiveChatService.is_active", return_value=False)
    def test_eligible_user(self, mock_is_active, followup_db):
        from followup_service import check_eligibility

        eligible, reason = check_eligibility("u1")
        assert eligible
        assert reason == ""


# ── followup_service: analyze_lead ───────────────────────────────────────────


class TestAnalyzeLead:
    @patch("followup_service._call_llm")
    def test_creates_followup_for_warm_lead(self, mock_llm, followup_db, _enable_followup):
        import database as db
        from followup_service import _analyze_lead_inner

        # מכין היסטוריית שיחה
        db.save_message("u1", "דניאל", "user", "כמה עולה ניקוי?")
        db.save_message("u1", "דניאל", "assistant", "המחיר הוא 250 ש\"ח.")
        db.save_message("u1", "דניאל", "user", "יש פנוי מחר?")
        db.save_message("u1", "דניאל", "assistant", "כן, יש ב-11:00 וב-14:00.")

        mock_llm.return_value = {
            "service_of_interest": "ניקוי שיניים",
            "intent_type": "booking_intent",
            "lead_temperature": "hot",
            "summary": "שאל על מחיר וזמינות",
        }

        _analyze_lead_inner("u1", username="דניאל")

        followups = db.get_all_followups()
        assert len(followups) == 1
        assert followups[0]["lead_temperature"] == "hot"
        assert followups[0]["service_of_interest"] == "ניקוי שיניים"

    @patch("followup_service._call_llm")
    def test_whatsapp_due_at_subtracts_buffer(
        self, mock_llm, followup_db, _enable_followup, monkeypatch,
    ):
        """ליד WhatsApp — due_at מוגדר 15 דקות מוקדם יותר מ-24h כדי לא
        לחרוג מחלון השיחה של Twilio (אחריו ההודעה הופכת ל-template עם
        תמחור גבוה).
        """
        import database as db
        from datetime import datetime, timezone, timedelta
        from followup_service import _analyze_lead_inner

        # נעילת זמן לרגע נתון כדי שאפשר יהיה להשוות את due_at בדיוק
        fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("followup_service.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            db.save_message("uw", "דניאל", "user", "כמה עולה?", channel="whatsapp")
            db.save_message("uw", "דניאל", "assistant", "₪250.", channel="whatsapp")
            db.save_message("uw", "דניאל", "user", "יש מחר?", channel="whatsapp")
            db.save_message("uw", "דניאל", "assistant", "כן.", channel="whatsapp")

            mock_llm.return_value = {
                "service_of_interest": "ניקוי",
                "intent_type": "booking_intent",
                "lead_temperature": "hot",
                "summary": "x",
            }
            _analyze_lead_inner("uw", username="דניאל", channel="whatsapp")

        followups = db.get_all_followups()
        wa = next(f for f in followups if f["user_id"] == "uw")
        # 24h - 15min = 23h45m אחרי fixed_now
        expected = (fixed_now + timedelta(hours=24) - timedelta(minutes=15))
        actual = datetime.strptime(wa["followup_due_at"], "%Y-%m-%d %H:%M:%S")
        assert actual == expected.replace(tzinfo=None)

    @patch("followup_service._call_llm")
    def test_telegram_due_at_unchanged(
        self, mock_llm, followup_db, _enable_followup, monkeypatch,
    ):
        """ליד Telegram לא מקבל את ה-buffer של WhatsApp — אין שם חלון תמחור."""
        import database as db
        from datetime import datetime, timezone, timedelta
        from followup_service import _analyze_lead_inner

        fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("followup_service.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            db.save_message("ut", "דניאל", "user", "כמה עולה?", channel="telegram")
            db.save_message("ut", "דניאל", "assistant", "₪250.", channel="telegram")
            db.save_message("ut", "דניאל", "user", "יש מחר?", channel="telegram")
            db.save_message("ut", "דניאל", "assistant", "כן.", channel="telegram")

            mock_llm.return_value = {
                "service_of_interest": "ניקוי",
                "intent_type": "booking_intent",
                "lead_temperature": "hot",
                "summary": "x",
            }
            _analyze_lead_inner("ut", username="דניאל", channel="telegram")

        followups = db.get_all_followups()
        tg = next(f for f in followups if f["user_id"] == "ut")
        expected = fixed_now + timedelta(hours=24)
        actual = datetime.strptime(tg["followup_due_at"], "%Y-%m-%d %H:%M:%S")
        assert actual == expected.replace(tzinfo=None)

    @patch("followup_service._call_llm")
    def test_skips_cold_lead(self, mock_llm, followup_db, _enable_followup):
        import database as db
        from followup_service import _analyze_lead_inner

        db.save_message("u1", "דניאל", "user", "מה שעות הפעילות?")
        db.save_message("u1", "דניאל", "assistant", "אנחנו פתוחים 9-18.")
        db.save_message("u1", "דניאל", "user", "ואיפה אתם נמצאים?")
        db.save_message("u1", "דניאל", "assistant", "ברחוב הרצל 10.")

        mock_llm.return_value = {
            "service_of_interest": "",
            "intent_type": "info_only",
            "lead_temperature": "cold",
            "summary": "שאלה כללית",
        }

        _analyze_lead_inner("u1")

        followups = db.get_all_followups()
        # ליד קר שומר רשומה cancelled כדי למנוע קריאות LLM חוזרות
        assert len(followups) == 1
        assert followups[0]["status"] == "cancelled"
        assert followups[0]["stop_reason"] == "cold_lead"

    @patch("followup_service._call_llm")
    def test_skips_if_already_has_followup(self, mock_llm, followup_db, _enable_followup):
        import database as db
        from followup_service import _analyze_lead_inner

        # יצירת follow-up קיים
        db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")

        db.save_message("u1", "דניאל", "user", "כמה עולה?")
        db.save_message("u1", "דניאל", "assistant", "250 ש\"ח")

        _analyze_lead_inner("u1")

        # LLM לא נקרא — כבר יש follow-up
        mock_llm.assert_not_called()


# ── followup_service: process_pending_followups ──────────────────────────────


class TestProcessSingleLead:
    """טסטים ל-_process_single_lead — הלוגיקה הסינכרונית של עיבוד ליד."""

    @patch("ai_chatbot.live_chat_service.send_message_by_channel", return_value=True)
    @patch("followup_service.get_followup_decision")
    @patch("followup_service.check_eligibility", return_value=(True, ""))
    def test_sends_followup(self, mock_elig, mock_decision, mock_send,
                            followup_db, _enable_followup):
        import database as db
        from followup_service import _process_single_lead

        fid = db.create_lead_followup(
            "u1", followup_due_at="2020-01-01 00:00:00",
            username="דניאל", service_of_interest="ניקוי",
            lead_temperature="hot",
        )

        mock_decision.return_value = {
            "should_send_followup": True,
            "confidence": 85,
            "recommended_template_key": "followup_booking_resume",
            "template_variables": {"service_name": "ניקוי שיניים"},
        }

        lead = db.get_all_followups()[0]
        stats = {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}
        _process_single_lead(lead, stats)

        assert stats["sent"] == 1
        mock_send.assert_called_once()

        # וידוא שסטטוס עודכן ל-sent
        updated = db.get_all_followups()[0]
        assert updated["status"] == "sent"

    @patch("followup_service.get_followup_decision")
    @patch("followup_service.check_eligibility", return_value=(False, "user_blocked"))
    def test_skips_ineligible(self, mock_elig, mock_decision,
                               followup_db, _enable_followup):
        import database as db
        from followup_service import _process_single_lead

        db.create_lead_followup("u1", followup_due_at="2020-01-01 00:00:00")

        lead = db.get_all_followups()[0]
        stats = {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}
        _process_single_lead(lead, stats)

        assert stats["skipped"] == 1
        mock_decision.assert_not_called()

        # וידוא שסטטוס עודכן ל-cancelled
        updated = db.get_all_followups()[0]
        assert updated["status"] == "cancelled"
        assert updated["stop_reason"] == "user_blocked"

    @patch("ai_chatbot.live_chat_service.send_message_by_channel", return_value=True)
    @patch("followup_service.get_followup_decision")
    @patch("followup_service.check_eligibility", return_value=(True, ""))
    def test_skips_low_confidence(self, mock_elig, mock_decision, mock_send,
                                   followup_db, _enable_followup):
        import database as db
        from followup_service import _process_single_lead

        db.create_lead_followup("u1", followup_due_at="2020-01-01 00:00:00")

        mock_decision.return_value = {
            "should_send_followup": True,
            "confidence": 30,  # מתחת לסף
            "recommended_template_key": "followup_interest_check",
            "template_variables": {},
        }

        lead = db.get_all_followups()[0]
        stats = {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}
        _process_single_lead(lead, stats)

        assert stats["skipped"] == 1
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, followup_db, monkeypatch):
        monkeypatch.setattr("followup_service.FOLLOWUP_ENABLED", False)
        from followup_service import process_pending_followups

        stats = await process_pending_followups()
        assert stats["processed"] == 0


# ── followup_service: send_followup_message ──────────────────────────────────


class TestSendFollowupMessage:
    @patch("ai_chatbot.live_chat_service.send_message_by_channel", return_value=True)
    def test_sends_telegram_message(self, mock_send):
        from followup_service import send_followup_message

        lead = {
            "user_id": "u1",
            "username": "דניאל",
            "channel": "telegram",
            "service_of_interest": "ניקוי",
        }
        result = send_followup_message(
            lead, "followup_interest_check", {"service_name": "ניקוי שיניים"},
        )
        assert result is True
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == "u1"
        assert "ניקוי שיניים" in call_args[0][1]
        assert call_args[0][2] == "telegram"


# ── followup_service: handle_user_returned / handle_booking_created ──────────


class TestHandleUserEvents:
    def test_handle_user_returned_marks_replied(self, followup_db, _enable_followup):
        import database as db
        from followup_service import handle_user_returned

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(fid, "sent")

        handle_user_returned("u1")

        followups = db.get_all_followups()
        assert followups[0]["status"] == "replied"

    def test_handle_booking_created_marks_converted(self, followup_db, _enable_followup):
        import database as db
        from followup_service import handle_booking_created

        fid = db.create_lead_followup("u1", followup_due_at="2026-04-05 10:00:00")
        db.update_followup_status(fid, "sent")
        db.mark_followup_replied("u1")

        handle_booking_created("u1")

        followups = db.get_all_followups()
        assert followups[0]["status"] == "converted"
        assert followups[0]["booking_after_followup"] == 1


class TestFollowupDecisionPrompt:
    """רגרסיה: הפרומפט של מנוע ההחלטה חייב להגדיר במפורש את שדות ה-JSON.

    בלי זה ה-LLM מחזיר JSON עם שדות כלליים (לא 'should_send_followup'),
    הקוד נופל ל-default False/0 → כל ליד מסומן 'llm_declined (confidence=0)'.
    זו רגרסיה שדווחה בפרודקשן.
    """

    def test_prompt_lists_required_fields(self):
        """הפרומפט חייב לכלול את כל שמות השדות הנדרשים בסכמה."""
        from followup_config import FOLLOWUP_DECISION_PROMPT, FOLLOWUP_DECISION_SCHEMA
        for field in FOLLOWUP_DECISION_SCHEMA["required"]:
            assert field in FOLLOWUP_DECISION_PROMPT, (
                f"שדה חובה {field} לא מופיע בפרומפט — ה-LLM לא יודע על קיומו"
            )

    def test_prompt_includes_json_example(self):
        """הפרומפט חייב לכלול דוגמת JSON עם ערכי boolean מפורשים."""
        from followup_config import FOLLOWUP_DECISION_PROMPT
        assert "true" in FOLLOWUP_DECISION_PROMPT
        assert "false" in FOLLOWUP_DECISION_PROMPT

    def test_prompt_does_not_reference_undefined_fields(self):
        """רגרסיה: הפרומפט הזכיר requires_template/can_send_freeform שלא בסכמה,
        ויצר סתירה מול 'חובה לכלול את כל השדות'. כעת — רק שדות בסכמה."""
        from followup_config import FOLLOWUP_DECISION_PROMPT
        forbidden_fields = ["requires_template", "can_send_freeform"]
        for field in forbidden_fields:
            assert field not in FOLLOWUP_DECISION_PROMPT, (
                f"שדה {field} מוזכר בפרומפט אבל לא קיים בסכמה — מבלבל את ה-LLM"
            )
