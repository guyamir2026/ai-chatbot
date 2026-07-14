"""
טסטים ל-core/message_processor.py — עיבוד הודעות גנרי.

Mock ל-LLM ו-RAG כדי לא לקרוא ל-API בטסטים.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

import core.message_processor  # noqa: F401 — נדרש כדי ש-mock.patch ימצא את המודול
from intent import Intent


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def processor_db(tmp_path):
    """DB זמני — משתמש ב-patch ישיר על database.DB_PATH."""
    db_path = tmp_path / "test.db"
    os.environ["DB_PATH"] = str(db_path)
    with patch("database.DB_PATH", db_path), \
         patch("config.DB_PATH", db_path):
        from database import init_db
        init_db()
        yield


@pytest.fixture
def mock_llm():
    """Mock ל-generate_answer — מחזיר תשובה מוצלחת ברירת מחדל."""
    answer = {
        "answer": "תשובה מהמודל\n\nמקור: מאגר ידע",
        "sources": ["מאגר ידע"],
        "chunks_used": 2,
        "follow_up_questions": [],
    }
    with patch("core.message_processor.generate_answer", return_value=answer) as mock:
        yield mock


@pytest.fixture
def mock_intent():
    """Mock ל-detect_intent_with_llm — ברירת מחדל GENERAL."""
    with patch("core.message_processor.detect_intent_with_llm", return_value=Intent.GENERAL) as mock:
        yield mock


@pytest.fixture
def mock_vacation_off():
    """Mock ל-VacationService — חופשה לא פעילה."""
    with patch("core.message_processor.VacationService") as mock:
        mock.is_active.return_value = False
        yield mock


@pytest.fixture
def mock_business_hours():
    """Mock ל-business_hours — פתוח עכשיו."""
    with patch("core.message_processor.is_currently_open", return_value={"message": "אנחנו פתוחים!"}), \
         patch("core.message_processor.get_weekly_schedule_text", return_value="א-ה 9:00-18:00"):
        yield


@pytest.fixture
def user_info():
    """מידע משתמש בסיסי."""
    return {"display_name": "Test User", "telegram_username": "testuser"}


# ── טסטים ────────────────────────────────────────────────────────────────────


class TestGreetingIntent:
    """greeting intent → תשובת ברכה."""

    def test_greeting_returns_direct_response(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.GREETING

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="123",
            text="שלום",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.GREETING
        assert result.action == "reply"
        assert "שלום" in result.text or "ברוכים הבאים" in result.text
        assert result.follow_up_questions == []
        assert result.needs_summarization is False

    def test_farewell_returns_direct_response(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.FAREWELL

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="124",
            text="תודה",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.FAREWELL
        assert result.action == "reply"
        assert "תודה" in result.text or "פניתם" in result.text


class TestGeneralQuestion:
    """general question → עובר דרך RAG."""

    def test_general_goes_through_rag(self, processor_db, mock_intent, mock_llm, user_info):
        mock_intent.return_value = Intent.GENERAL

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="456",
            text="מה שעות הפעילות?",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        # וידוא שה-LLM נקרא
        mock_llm.assert_called_once()
        assert result.intent == Intent.GENERAL
        assert result.is_html is True
        assert result.needs_summarization is True
        # וידוא שציון מקור הוסר מהטקסט
        assert "מקור:" not in result.text

    def test_pricing_goes_through_rag_with_prefix(self, processor_db, mock_intent, mock_llm, user_info):
        mock_intent.return_value = Intent.PRICING

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="457",
            text="כמה עולה תספורת?",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        # וידוא שהשאילתה כוללת את הקידומת "מחירון:"
        call_kwargs = mock_llm.call_args
        query = call_kwargs.kwargs.get("user_query", "")
        assert "מחירון:" in query


class TestRateLimitExceeded:
    """rate limit exceeded → הודעת חסימה."""

    def test_rate_limited_returns_limit_message(self, processor_db, user_info):
        limit_msg = "קצב ההודעות מהיר מדי"
        with patch("core.message_processor.check_rate_limit", return_value=limit_msg):
            from core.message_processor import process_incoming_message
            result = process_incoming_message(
                user_id="789",
                text="שלום",
                user_info=user_info,
                rate_limit_already_checked=False,
            )

        assert result.action == "rate_limited"
        assert result.text == limit_msg

    def test_rate_limit_skipped_when_already_checked(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.GREETING

        with patch("core.message_processor.check_rate_limit") as mock_check:
            from core.message_processor import process_incoming_message
            result = process_incoming_message(
                user_id="790",
                text="שלום",
                user_info=user_info,
                rate_limit_already_checked=True,
            )

        # check_rate_limit לא אמור להיקרא
        mock_check.assert_not_called()
        assert result.intent == Intent.GREETING


class TestComplaintIntent:
    """complaint intent → הצעת נציג אנושי."""

    def test_complaint_returns_complaint_action(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.COMPLAINT

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="321",
            text="שירות גרוע",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.COMPLAINT
        assert result.action == "complaint"
        assert "מצטערים" in result.text
        assert result.is_html is True


class TestHumanAgentIntent:
    """human_agent intent → action=request_agent."""

    def test_human_agent_returns_request_agent(self, processor_db, mock_intent, mock_vacation_off, user_info):
        mock_intent.return_value = Intent.HUMAN_AGENT

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="654",
            text="תעביר לנציג",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.HUMAN_AGENT
        assert result.action == "request_agent"
        assert result.agent_request_message != ""
        assert "נציג" in result.agent_request_message


class TestBookingIntent:
    """appointment_booking intent → action=start_booking."""

    def test_booking_returns_start_booking(self, processor_db, mock_intent, mock_vacation_off, user_info):
        mock_intent.return_value = Intent.APPOINTMENT_BOOKING

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="111",
            text="אפשר לקבוע תור?",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.APPOINTMENT_BOOKING
        assert result.action == "start_booking"
        assert "תור" in result.text
        assert result.is_html is True


class TestBookingDisabled:
    """booking_enabled=0 → בקשת תור/פגישה מנותבת ל-HUMAN_AGENT (הפניה לנציג)."""

    def test_booking_disabled_routes_to_agent(
        self, processor_db, mock_intent, mock_vacation_off, user_info,
    ):
        import database as _db
        s = _db.get_bot_settings()
        _db.update_bot_settings(
            s["tone"], s.get("custom_phrases", ""), booking_enabled=False,
        )
        assert _db.is_booking_enabled() is False

        mock_intent.return_value = Intent.APPOINTMENT_BOOKING

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="222",
            text="אפשר לקבוע תור?",
            user_info=user_info,
            rate_limit_already_checked=True,
        )
        # לא פותחים flow תורים — מנותב לצינור הנציג
        assert result.action != "start_booking"
        assert result.intent == Intent.HUMAN_AGENT
        assert result.action == "request_agent"


class TestCancelIntent:
    """appointment_cancel intent → action=cancel_appointment."""

    def test_cancel_returns_cancel_action(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.APPOINTMENT_CANCEL

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="222",
            text="רוצה לבטל תור",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.APPOINTMENT_CANCEL
        assert result.action == "cancel_appointment"
        assert "לבטל" in result.text


class TestRescheduleIntent:
    """appointment_reschedule intent → action=reschedule_appointment."""

    def test_reschedule_returns_reschedule_action(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.APPOINTMENT_RESCHEDULE

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="222",
            text="רוצה לשנות תור",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.APPOINTMENT_RESCHEDULE
        assert result.action == "reschedule_appointment"
        assert "לשנות" in result.text


class TestBusinessHoursIntent:
    """business_hours intent → תשובה עם שעות פעילות."""

    def test_business_hours_returns_schedule(
        self, processor_db, mock_intent, mock_business_hours, mock_vacation_off, user_info,
    ):
        mock_intent.return_value = Intent.BUSINESS_HOURS

        from core.message_processor import process_incoming_message
        result = process_incoming_message(
            user_id="333",
            text="מתי אתם פתוחים?",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.BUSINESS_HOURS
        assert "פתוחים" in result.text
        assert "9:00-18:00" in result.text

    def test_business_hours_during_vacation_mentions_vacation(
        self, processor_db, mock_intent, mock_business_hours, user_info,
    ):
        """בזמן חופשה: שאלת שעות פתיחה חייבת לציין חופשה ולא להציג 'פתוח עכשיו'."""
        mock_intent.return_value = Intent.BUSINESS_HOURS

        with patch("core.message_processor.VacationService") as mock_vac:
            mock_vac.is_active.return_value = True
            mock_vac.get_hours_message.return_value = (
                "אנחנו בחופשה עד 2026-04-30.\nנחזור לפעילות החל מ-2026-04-30."
            )

            from core.message_processor import process_incoming_message
            result = process_incoming_message(
                user_id="334",
                text="באיזה שעות אתם פתוחים?",
                user_info=user_info,
                rate_limit_already_checked=True,
            )

        assert result.intent == Intent.BUSINESS_HOURS
        assert "בחופשה" in result.text
        assert "2026-04-30" in result.text
        # סטטוס "פתוחים עכשיו" המטעה לא צריך להופיע במצב חופשה
        assert "אנחנו פתוחים!" not in result.text
        # שעות הפעילות הרגילות עדיין נכללות לעיון לאחר החזרה
        assert "9:00-18:00" in result.text


class TestRagQuery:
    """process_rag_query — צינור RAG."""

    def test_successful_response_resets_fallbacks(self, processor_db, mock_llm):
        from core.message_processor import process_rag_query

        result = process_rag_query(
            user_id="444",
            display_name="Test",
            user_message="שאלה כללית",
            query="שאלה כללית",
            handoff_reason="test",
            consecutive_fallbacks=2,
        )

        assert result.consecutive_fallbacks == 0
        assert result.needs_summarization is True
        assert result.action == "reply"

    def test_handoff_after_three_failures(self, processor_db):
        """fallback שלישי → העברה לנציג."""
        from config import FALLBACK_RESPONSE
        fallback_answer = {
            "answer": FALLBACK_RESPONSE,
            "sources": [],
            "chunks_used": 0,
            "follow_up_questions": [],
        }
        with patch("core.message_processor.generate_answer", return_value=fallback_answer):
            from core.message_processor import process_rag_query

            result = process_rag_query(
                user_id="555",
                display_name="Test",
                user_message="שאלה",
                query="שאלה",
                handoff_reason="לא מצאנו מידע",
                consecutive_fallbacks=2,
            )

        assert result.action == "handoff_to_human"
        assert result.consecutive_fallbacks == 0
        assert result.handoff_reason == "לא מצאנו מידע"

    def test_soft_fallback_first_attempt(self, processor_db):
        """fallback ראשון → הצעה לנסח מחדש."""
        from config import FALLBACK_RESPONSE
        fallback_answer = {
            "answer": FALLBACK_RESPONSE,
            "sources": [],
            "chunks_used": 0,
            "follow_up_questions": [],
        }
        with patch("core.message_processor.generate_answer", return_value=fallback_answer):
            from core.message_processor import process_rag_query

            result = process_rag_query(
                user_id="666",
                display_name="Test",
                user_message="שאלה",
                query="שאלה",
                handoff_reason="test",
                consecutive_fallbacks=0,
            )

        assert result.action == "reply"
        assert result.consecutive_fallbacks == 1
        assert "לנסח" in result.text
        assert result.show_keyboard is False


class TestUserIdAlwaysString:
    """וידוא ש-user_id מטופל כ-string."""

    def test_numeric_user_id_converted_to_string(self, processor_db, mock_intent, user_info):
        mock_intent.return_value = Intent.GREETING

        from core.message_processor import process_incoming_message
        # שולחים user_id כמספר — הפרוססור צריך להמיר ל-string
        result = process_incoming_message(
            user_id=12345,
            text="שלום",
            user_info=user_info,
            rate_limit_already_checked=True,
        )

        assert result.intent == Intent.GREETING


class TestOutOfOfficeNotice:
    """הודעת 'חוץ מהמשרד' כשהעסק סגור."""

    def test_agent_request_includes_ooo_when_closed(
        self, processor_db, mock_intent, mock_vacation_off, user_info
    ):
        """בקשת נציג כשסגור — כוללת הודעת חוץ מהמשרד."""
        mock_intent.return_value = Intent.HUMAN_AGENT
        ooo_msg = "🕐 העסק סגור כרגע.\nהבקשה שלכם נרשמה — בעל העסק יחזור אליכם מחר (ראשון) בשעה 09:00."

        with patch("core.message_processor.get_out_of_office_agent_notice", return_value=ooo_msg):
            from core.message_processor import process_incoming_message
            result = process_incoming_message(
                user_id="ooo1",
                text="תעביר לנציג",
                user_info=user_info,
                rate_limit_already_checked=True,
            )

        assert result.action == "request_agent"
        assert "סגור" in result.text
        assert "נרשמה" in result.text
        assert "09:00" in result.text

    def test_agent_request_normal_when_open(
        self, processor_db, mock_intent, mock_vacation_off, user_info
    ):
        """בקשת נציג כשפתוח — הודעה רגילה ללא חוץ מהמשרד."""
        mock_intent.return_value = Intent.HUMAN_AGENT

        with patch("core.message_processor.get_out_of_office_agent_notice", return_value=None):
            from core.message_processor import process_incoming_message
            result = process_incoming_message(
                user_id="ooo2",
                text="תעביר לנציג",
                user_info=user_info,
                rate_limit_already_checked=True,
            )

        assert result.action == "request_agent"
        assert "בעל העסק" in result.text
        assert "סגור" not in result.text

    def test_rag_response_no_ooo_notice(self, processor_db, mock_llm):
        """תשובת RAG לא כוללת הודעת חוץ מהמשרד — רק בבקשות נציג."""
        from core.message_processor import process_rag_query
        result = process_rag_query(
            user_id="ooo3",
            display_name="Test",
            user_message="מה המחירים?",
            query="מחירון: מה המחירים?",
            handoff_reason="test",
        )

        assert "סגור" not in result.text
        assert "תשובה מהמודל" in result.text


class TestShouldHandoffToHuman:
    """זיהוי handoff מבוסס טוקן — דטרמיניסטי, ללא fuzzy matching.

    הארכיטקטורה: ה-LLM מורה להוסיף HANDOFF_MARKER בתחילת תשובתו כשהוא
    רוצה להעביר לבעל העסק. הפרסר מזהה את הטוקן, מסיר אותו, ומפעיל את
    צינור בקשת הנציג. בלי טוקן → אין handoff (אלא במקרה של טקסט זהה
    לחלוטין ל-FALLBACK_RESPONSE כ-safety net).
    """

    def test_marker_at_start_triggers_handoff(self):
        from core.message_processor import should_handoff_to_human
        from config import HANDOFF_MARKER, FALLBACK_RESPONSE
        text = f"{HANDOFF_MARKER}\n\n{FALLBACK_RESPONSE}"
        assert should_handoff_to_human(text) is True

    def test_marker_with_whitespace_prefix(self):
        from core.message_processor import should_handoff_to_human
        from config import HANDOFF_MARKER
        text = f"  \n{HANDOFF_MARKER}\nbody"
        assert should_handoff_to_human(text) is True

    def test_exact_fallback_safety_net(self):
        """Safety net — אם הטוקן הוסר אבל נשאר הטקסט המדויק."""
        from core.message_processor import should_handoff_to_human, FALLBACK_RESPONSE
        assert should_handoff_to_human(FALLBACK_RESPONSE) is True

    def test_no_marker_no_handoff_even_with_handoff_words(self):
        """ללא טוקן — גם תשובות שמכילות 'אעביר את הפנייה' לא נחשבות handoff.
        זה היתרון של marker-based: אין false positives ב-RAG תקין."""
        from core.message_processor import should_handoff_to_human
        text = (
            "אני מבין שתרצו לדבר עם נציג אנושי. בשמחה אעביר את הפנייה "
            "שלכם לבעל העסק והוא יחזור אליכם בהקדם האפשרי."
        )
        # ללא טוקן בתחילת התשובה — לא handoff. ה-LLM אמור להוסיף את הטוקן
        # אם באמת רצה handoff. בלי הטוקן זו תשובה רגילה.
        assert should_handoff_to_human(text) is False

    def test_normal_answer_not_caught(self):
        from core.message_processor import should_handoff_to_human
        assert should_handoff_to_human("המחיר הוא 150 ש\"ח. מקור: מחירון") is False
        assert should_handoff_to_human("אנחנו פתוחים מ-9 עד 18.") is False

    def test_empty_text_returns_false(self):
        from core.message_processor import should_handoff_to_human
        assert should_handoff_to_human("") is False
        assert should_handoff_to_human(None) is False  # type: ignore


class TestStripHandoffMarker:
    """הטוקן הוא פנימי בלבד — אסור שיגיע ללקוח."""

    def test_strips_marker_from_start(self):
        from core.message_processor import strip_handoff_marker
        from config import HANDOFF_MARKER
        text = f"{HANDOFF_MARKER}\n\nתוכן"
        assert strip_handoff_marker(text) == "תוכן"

    def test_strips_marker_with_leading_whitespace(self):
        from core.message_processor import strip_handoff_marker
        from config import HANDOFF_MARKER
        assert strip_handoff_marker(f"  \n{HANDOFF_MARKER} body") == "body"

    def test_no_marker_returns_unchanged(self):
        from core.message_processor import strip_handoff_marker
        assert strip_handoff_marker("regular answer") == "regular answer"

    def test_marker_in_middle_not_stripped(self):
        """הטוקן נחשב סיגנל רק כשהוא בתחילת התשובה."""
        from core.message_processor import strip_handoff_marker
        from config import HANDOFF_MARKER
        text = f"some text {HANDOFF_MARKER} more"
        assert strip_handoff_marker(text) == text

    def test_empty_input(self):
        from core.message_processor import strip_handoff_marker
        assert strip_handoff_marker("") == ""
        assert strip_handoff_marker(None) is None  # type: ignore


class TestHandoffSystemPrompt:
    """הפרומפט מורה ל-LLM להשתמש בטוקן."""

    def test_prompt_mentions_marker(self):
        from config import HANDOFF_MARKER, build_system_prompt
        prompt = build_system_prompt(channel="whatsapp")
        assert HANDOFF_MARKER in prompt

    def test_prompt_includes_fallback_response(self):
        from config import FALLBACK_RESPONSE, build_system_prompt
        prompt = build_system_prompt(channel="whatsapp")
        assert FALLBACK_RESPONSE in prompt
        prompt_tg = build_system_prompt(channel="telegram")
        assert FALLBACK_RESPONSE in prompt_tg
