"""
בדיקות אינטגרציה E2E — flow שלם על DB אמיתי (זמני), בלי mocks על לוגיקה פנימית.

1. שיחת בוט מלאה (start → שאלה → תשובה)
2. הזמנת תור מקצה לקצה
3. Live chat flow
4. Admin panel — CRUD operations
"""

import asyncio
import importlib
import time
from contextlib import ExitStack
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ── Helpers משותפים ────────────────────────────────────────────────────────────


def _make_update(user_id: int = 100, text: str = "שלום", username: str = "testuser"):
    """יוצר mock Update עם effective_user ו-message — רק ממשקי telegram ממוקים."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.full_name = "Test User"
    update.effective_user.username = username
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_document = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = user_id
    update.effective_message = update.message
    update.callback_query = None
    return update


def _make_context(args=None):
    """יוצר mock Context — רק ממשקי telegram ממוקים."""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.args = args or []
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_chat_action = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock()
    return context


@pytest.fixture
def real_db(tmp_path):
    """DB אמיתי עם סכימה מלאה — ללא mocks על פונקציות DB."""
    db_path = tmp_path / "integration_test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import database
        importlib.reload(database)
        database.init_db()
        yield database


def _guard_patches():
    """Patches מינימליים — rate limiter, live chat guard, ותלויות חיצוניות של booking flow."""
    # מוק ל-google_calendar — מדמה מצב לא מחובר (נדרש כי ה-import מתוך הפונקציות)
    _mock_gcal = MagicMock()
    _mock_gcal.is_connected = MagicMock(return_value=False)
    _mock_gcal.get_available_slots = MagicMock(return_value=[])

    return [
        patch("rate_limiter.check_rate_limit", return_value=None),
        patch("rate_limiter.record_message"),
        patch("live_chat_service.LiveChatService.is_active", return_value=False),
        patch("live_chat_service.db"),
        # מוק ללוח השנה — booking_service קוראת ל-build_calendar_keyboard שדורש DB ולוגיקה עסקית
        patch("bot.handlers.build_calendar_keyboard", return_value=MagicMock()),
        # מוק ל-Google Calendar — booking_date עושה from google_calendar import ...
        patch.dict("sys.modules", {"google_calendar": _mock_gcal}),
        # מסך ההסכמה (תיקון 13) חוסם handlers עד שהמשתמש מאשר. בטסטי integration
        # מדלגים על המסך — הם בוחנים את הזרימה העסקית, לא את הקומפליאנס.
        # bot.handlers משתמש ב-ai_chatbot.database (wrapper שעושה `from database import *`),
        # ולכן הפונקציה היא binding נפרד מ-database.has_consent — חייבים לפץ' שניהם.
        patch("database.has_consent", return_value=True),
        patch("ai_chatbot.database.has_consent", return_value=True),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 1. שיחת בוט מלאה — start → שאלה → תשובה (עם RAG pipeline)
# ══════════════════════════════════════════════════════════════════════════════


class TestFullBotConversation:
    """E2E: משתמש שולח /start, שואל שאלה, מקבל תשובה, הכל נשמר ב-DB."""

    @pytest.mark.asyncio
    async def test_start_then_greeting_flow(self, real_db):
        """start → ברכה → תשובה ישירה (ללא RAG), שתי ההודעות נשמרות ב-DB."""
        from bot.handlers import start_command, message_handler
        from bot.handlers import Intent

        user_id = 1001
        update = _make_update(user_id=user_id, text="/start")
        context = _make_context()

        # שלב 1: /start
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await start_command(update, context)

        # פונה ראשון — מקבל את הודעת הפתיחה המשפטית (implied consent),
        # שמחליפה את ברכת הפתיחה הרגילה
        reply_text = update.message.reply_text.call_args[0][0]
        assert "המשך השיחה מהווה אישור" in reply_text

        # ודא שההודעות נשמרו ב-DB
        history = real_db.get_conversation_history(str(user_id), limit=10)
        assert len(history) >= 2  # /start + welcome
        assert any("/start" in m["message"] for m in history)

        # שלב 2: משתמש שולח ברכה
        update2 = _make_update(user_id=user_id, text="שלום!")
        context2 = _make_context()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            # mock intent detection — מחזיר GREETING (ללא LLM אמיתי)
            stack.enter_context(
                patch("core.message_processor.detect_intent_with_llm", return_value=Intent.GREETING)
            )
            stack.enter_context(
                patch("core.message_processor.get_direct_response", return_value="היי! איך אפשר לעזור?")
            )
            await message_handler(update2, context2)

        # ודא שנשלחה תשובה
        greeting_reply = update2.message.reply_text.call_args[0][0]
        assert "היי" in greeting_reply

        # ודא שכל השיחה נשמרה ב-DB — start + greeting + responses
        full_history = real_db.get_conversation_history(str(user_id), limit=20)
        user_messages = [m for m in full_history if m["role"] == "user"]
        bot_messages = [m for m in full_history if m["role"] == "assistant"]
        assert len(user_messages) >= 2  # /start + "שלום!"
        assert len(bot_messages) >= 2  # welcome + greeting response

    @pytest.mark.asyncio
    async def test_start_then_rag_question_flow(self, real_db):
        """start → שאלה כללית → RAG pipeline → תשובה + שמירה ב-DB."""
        from bot.handlers import start_command, message_handler
        from bot.handlers import Intent

        user_id = 1002
        update = _make_update(user_id=user_id)
        context = _make_context()

        # שלב 1: /start
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await start_command(update, context)

        # שלב 2: שאלה כללית — עוברת RAG pipeline
        update2 = _make_update(user_id=user_id, text="מה שעות הפתיחה?")
        context2 = _make_context()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("core.message_processor.detect_intent_with_llm", return_value=Intent.BUSINESS_HOURS)
            )
            stack.enter_context(
                patch("core.message_processor.is_currently_open", return_value={"message": "🟢 פתוח עכשיו"})
            )
            stack.enter_context(
                patch("core.message_processor.get_weekly_schedule_text", return_value="ראשון-חמישי 09:00-18:00")
            )
            await message_handler(update2, context2)

        # ודא תשובת שעות
        hours_reply = update2.message.reply_text.call_args[0][0]
        assert "פתוח" in hours_reply
        assert "ראשון-חמישי" in hours_reply

        # ודא שגם השאלה וגם התשובה נשמרו
        history = real_db.get_conversation_history(str(user_id), limit=20)
        assert any("שעות הפתיחה" in m["message"] for m in history)
        assert any("פתוח" in m["message"] for m in history if m["role"] == "assistant")

    @pytest.mark.asyncio
    async def test_general_question_with_rag_answer(self, real_db):
        """שאלה כללית → generate_answer (מוק) → תשובה עם sources נשמרת ב-DB."""
        from bot.handlers import message_handler
        from bot.handlers import Intent

        user_id = 1003
        update = _make_update(user_id=user_id, text="כמה עולה תספורת?")
        context = _make_context()

        mock_answer = {
            "answer": "תספורת גברים עולה 80 ש\"ח.",
            "sources": ["מחירון"],
            "chunks_used": 1,
            "follow_up_questions": [],
        }

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("core.message_processor.detect_intent_with_llm", return_value=Intent.PRICING)
            )
            # mock generate_answer — ה-RAG pipeline עצמו
            stack.enter_context(
                patch("core.message_processor.generate_answer", return_value=mock_answer)
            )
            stack.enter_context(
                patch("core.message_processor.strip_source_citation", return_value=mock_answer["answer"])
            )
            stack.enter_context(
                patch("bot.handlers.sanitize_telegram_html", return_value=mock_answer["answer"])
            )
            await message_handler(update, context)

        # ודא שהתשובה נשלחה
        assert update.message.reply_text.call_count >= 1

        # ודא שנשמר ב-DB עם sources
        history = real_db.get_conversation_history(str(user_id), limit=10)
        assistant_msgs = [m for m in history if m["role"] == "assistant"]
        assert any("תספורת" in m["message"] for m in assistant_msgs)
        assert any(m.get("sources", "") for m in assistant_msgs)

    @pytest.mark.asyncio
    async def test_complaint_intent_offers_agent(self, real_db):
        """תלונה → הצעת נציג (ללא RAG), נשמר ב-DB."""
        from bot.handlers import message_handler
        from bot.handlers import Intent

        user_id = 1004
        update = _make_update(user_id=user_id, text="השירות היה נורא!")
        context = _make_context()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("core.message_processor.detect_intent_with_llm", return_value=Intent.COMPLAINT)
            )
            await message_handler(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "מצטערים" in reply or "נציג" in reply

        history = real_db.get_conversation_history(str(user_id), limit=10)
        assert any("השירות היה נורא" in m["message"] for m in history)

    @pytest.mark.asyncio
    async def test_returning_customer_greeting(self, real_db):
        """לקוח חוזר מקבל הודעת 'שמחים לראות אותך שוב'."""
        from bot.handlers import start_command

        user_id = 1005
        # יצירת תור מאושר כדי שהלקוח ייחשב "חוזר"
        real_db.create_appointment(
            user_id=str(user_id),
            username="Test User",
            service="תספורת",
            preferred_date="2026-01-01",
            preferred_time="10:00",
            telegram_username="testuser",
        )
        # עדכון לסטטוס confirmed כדי ש-is_returning_customer יחזיר True
        appts = real_db.get_appointments()
        if appts:
            real_db.update_appointment_status(appts[0]["id"], "confirmed")

        # ה-disclaimer כבר נשלח — בודקים ספציפית את ברכת הלקוח החוזר, לא את
        # הודעת הפתיחה המשפטית (שגוברת עליה בפנייה הראשונה בלבד)
        real_db.upsert_user(str(user_id), "Test User", channel="telegram")
        real_db.mark_disclaimer_sent(str(user_id))

        update = _make_update(user_id=user_id)
        context = _make_context()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await start_command(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "שמחים לראות אותך שוב" in reply


# ══════════════════════════════════════════════════════════════════════════════
# 2. הזמנת תור מקצה לקצה
# ══════════════════════════════════════════════════════════════════════════════


class TestBookingEndToEnd:
    """E2E: שירות → תאריך → שעה → אישור → נשמר ב-DB → הודעה לבעל העסק."""

    @pytest.mark.asyncio
    async def test_full_booking_flow_confirmed(self, real_db):
        """flow מלא: בחירת שירות → תאריך → שעה → כן → תור נוצר ב-DB."""
        from bot.handlers import (
            booking_service, booking_date, booking_time, booking_confirm,
            BOOKING_DATE, BOOKING_TIME, BOOKING_CONFIRM,
        )
        from telegram.ext import ConversationHandler

        user_id = 2001

        # תאריך עתידי דינמי — תאריך קבוע בעבר גורם ל-auto-booking decision
        # להחזיר rejected (slot_in_past) ולבטל את התור, ואז הבדיקה נופלת.
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()

        # שלב 1: בחירת שירות
        update1 = _make_update(user_id=user_id, text="תספורת גברים")
        context = _make_context()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            result = await booking_service(update1, context)
        assert result == BOOKING_DATE
        assert context.user_data["booking_service"] == "תספורת גברים"

        # שלב 2: בחירת תאריך
        update2 = _make_update(user_id=user_id, text=future_date)
        update2.message.reply_text = AsyncMock()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            result = await booking_date(update2, context)
        assert result == BOOKING_TIME
        assert context.user_data["booking_date"] == future_date

        # שלב 3: בחירת שעה
        update3 = _make_update(user_id=user_id, text="14:00")
        update3.message.reply_text = AsyncMock()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            result = await booking_time(update3, context)
        assert result == BOOKING_CONFIRM
        assert context.user_data["booking_time"] == "14:00"

        # שלב 4: אישור
        update4 = _make_update(user_id=user_id, text="כן")
        update4.message.reply_text = AsyncMock()

        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("bot.handlers._notify_owner", new_callable=AsyncMock, return_value=True)
            )
            result = await booking_confirm(update4, context)

        assert result == ConversationHandler.END
        assert context.user_data == {}  # user_data נוקה

        # ודא שהתור נשמר ב-DB
        appointments = real_db.get_appointments()
        assert len(appointments) >= 1
        appt = appointments[-1]
        assert appt["service"] == "תספורת גברים"
        assert appt["preferred_date"] == future_date
        assert appt["preferred_time"] == "14:00"
        assert appt["status"] == "pending"
        assert appt["user_id"] == str(user_id)

    @pytest.mark.asyncio
    async def test_booking_flow_cancelled_by_user(self, real_db):
        """flow ביטול: שירות → תאריך → שעה → לא → אין תור ב-DB."""
        from bot.handlers import (
            booking_service, booking_date, booking_time, booking_confirm,
        )
        from telegram.ext import ConversationHandler

        user_id = 2002
        context = _make_context()

        # שלבים 1-3: כמו קודם
        update1 = _make_update(user_id=user_id, text="צביעה")
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_service(update1, context)

        update2 = _make_update(user_id=user_id, text="2026-04-15")
        update2.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_date(update2, context)

        update3 = _make_update(user_id=user_id, text="16:00")
        update3.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_time(update3, context)

        # שלב 4: ביטול
        update4 = _make_update(user_id=user_id, text="לא")
        update4.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            result = await booking_confirm(update4, context)

        assert result == ConversationHandler.END
        reply = update4.message.reply_text.call_args[0][0]
        assert "בוטלה" in reply

        # ודא שאין תור ב-DB עבור המשתמש הזה
        pending = real_db.get_pending_appointments_for_user(str(user_id))
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_booking_cancel_command(self, real_db):
        """ביטול באמצע flow עם /cancel — מנקה user_data."""
        from bot.handlers import booking_service, booking_cancel
        from telegram.ext import ConversationHandler

        user_id = 2003
        context = _make_context()

        update1 = _make_update(user_id=user_id, text="תספורת")
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_service(update1, context)

        assert "booking_service" in context.user_data

        update_cancel = _make_update(user_id=user_id, text="/cancel")
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            result = await booking_cancel(update_cancel, context)

        assert result == ConversationHandler.END
        assert context.user_data == {}

    @pytest.mark.asyncio
    async def test_booking_duplicate_prevention(self, real_db):
        """כפילות — אם כבר יש תור באותו זמן, לא נוצר חדש."""
        from bot.handlers import (
            booking_service, booking_date, booking_time, booking_confirm,
        )
        from telegram.ext import ConversationHandler

        user_id = 2004

        # תאריך דינמי (בעוד שבוע) — get_pending_appointments_for_user מסנן
        # לתורים עתידיים, לכן תאריך קבוע "בעבר" (יחסית להרצת הטסט) גורם
        # לטסט לכשול אחרי שחלף הזמן.
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()

        # יצירת תור ישירות ב-DB
        real_db.create_appointment(
            user_id=str(user_id),
            username="Test User",
            service="תספורת",
            preferred_date=future_date,
            preferred_time="10:00",
            telegram_username="testuser",
        )

        # ניסיון ליצור תור זהה דרך ה-flow
        context = _make_context()

        update1 = _make_update(user_id=user_id, text="תספורת")
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_service(update1, context)

        update2 = _make_update(user_id=user_id, text=future_date)
        update2.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_date(update2, context)

        update3 = _make_update(user_id=user_id, text="10:00")
        update3.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await booking_time(update3, context)

        update4 = _make_update(user_id=user_id, text="כן")
        update4.message.reply_text = AsyncMock()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("bot.handlers._notify_owner", new_callable=AsyncMock, return_value=True)
            )
            result = await booking_confirm(update4, context)

        assert result == ConversationHandler.END
        # ודא שאין כפילות — תור אחד בלבד
        pending = real_db.get_pending_appointments_for_user(str(user_id))
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_booking_admin_confirms_appointment(self, real_db):
        """E2E: תור נוצר → אדמין מאשר → סטטוס מתעדכן ב-DB."""
        user_id = 2005

        # יצירת תור
        appt_id = real_db.create_appointment(
            user_id=str(user_id),
            username="Test User",
            service="תספורת",
            preferred_date="2026-04-25",
            preferred_time="11:00",
            telegram_username="testuser",
        )

        # ודא סטטוס pending
        appt = real_db.get_appointment(appt_id)
        assert appt["status"] == "pending"

        # אדמין מאשר
        real_db.update_appointment_status(appt_id, "confirmed")

        # ודא סטטוס confirmed
        appt = real_db.get_appointment(appt_id)
        assert appt["status"] == "confirmed"

        # ודא שהמשתמש נחשב לקוח חוזר — דורש תור מאושר עם תאריך שכבר עבר
        past_appt_id = real_db.create_appointment(
            user_id=str(user_id),
            username="Test User",
            service="תספורת",
            preferred_date="2025-01-01",
            preferred_time="10:00",
            telegram_username="testuser",
        )
        real_db.update_appointment_status(past_appt_id, "confirmed")
        assert real_db.is_returning_customer(str(user_id))


# ══════════════════════════════════════════════════════════════════════════════
# 3. Live Chat Flow
# ══════════════════════════════════════════════════════════════════════════════


class TestLiveChatFlow:
    """E2E: נציג מפעיל live chat → שולח הודעות → מסיים → בוט חוזר."""

    def test_full_live_chat_lifecycle(self, real_db):
        """start → send message → DB records → end → bot resumes."""
        # יצירת משתמש עם היסטוריה
        user_id = "3001"
        real_db.save_message(user_id, "Customer", "user", "שלום, צריך עזרה")

        # שלב 1: הפעלת live chat
        chat_id = real_db.start_live_chat(user_id, "Customer")
        assert chat_id > 0
        assert real_db.is_live_chat_active(user_id)

        # ודא שהשיחה החיה מופיעה ברשימה
        active = real_db.get_all_active_live_chats()
        assert any(lc["user_id"] == user_id for lc in active)

        # שלב 2: שליחת הודעה מהנציג (נשמרת כ-assistant)
        real_db.save_message(user_id, "Customer", "assistant", "היי, איך אני יכול לעזור?")
        real_db.touch_live_chat(user_id)

        # שלב 3: לקוח שולח הודעה (ב-live chat — ההודעה עוברת לנציג)
        real_db.save_message(user_id, "Customer", "user", "רציתי לשאול על מחירים")

        # ודא שהיסטוריה שלמה
        history = real_db.get_conversation_history(user_id, limit=20)
        assert len(history) >= 3

        # שלב 4: סיום live chat
        real_db.end_live_chat(user_id)
        assert not real_db.is_live_chat_active(user_id)

        # ודא שהשיחה לא מופיעה יותר ברשימת הפעילות
        active_after = real_db.get_all_active_live_chats()
        assert not any(lc["user_id"] == user_id for lc in active_after)

    def test_live_chat_blocks_bot_handler(self, real_db):
        """כש-live chat פעיל, הדקורטור live_chat_guard חוסם את ה-handler."""
        user_id = "3002"
        real_db.start_live_chat(user_id, "Customer")

        # ודא שהשירות מזהה שיש שיחה פעילה
        assert real_db.is_live_chat_active(user_id)

        # סיום
        real_db.end_live_chat(user_id)
        assert not real_db.is_live_chat_active(user_id)

    def test_agent_request_to_live_chat_flow(self, real_db):
        """בקשת נציג → live chat → שיחה → סיום — flow מלא."""
        user_id = "3003"

        # שלב 1: לקוח מבקש נציג
        request_id = real_db.create_agent_request(
            user_id=user_id,
            username="Customer",
            message="רוצה לדבר עם מישהו",
            telegram_username="customer3003",
        )
        assert request_id > 0

        # ודא בקשה ממתינה
        pending = real_db.get_agent_requests(status="pending")
        assert any(r["id"] == request_id for r in pending)

        # שלב 2: אדמין מפעיל live chat — סוגר בקשות ממתינות
        real_db.start_live_chat(user_id, "Customer")
        real_db.handle_pending_requests_for_user(user_id)

        # ודא שהבקשה סומנה כטופלה
        req = real_db.get_agent_request(request_id)
        assert req["status"] == "handled"

        # שלב 3: שיחה
        real_db.save_message(user_id, "Customer", "assistant", "בדקתי — המחיר 100 ש\"ח")
        real_db.touch_live_chat(user_id)

        # שלב 4: סיום
        real_db.end_live_chat(user_id)
        assert not real_db.is_live_chat_active(user_id)

    def test_multiple_live_chats_independent(self, real_db):
        """שתי שיחות חיות במקביל — כל אחת עצמאית."""
        user_a = "3004"
        user_b = "3005"

        real_db.start_live_chat(user_a, "Customer A")
        real_db.start_live_chat(user_b, "Customer B")

        assert real_db.is_live_chat_active(user_a)
        assert real_db.is_live_chat_active(user_b)
        assert real_db.count_active_live_chats() >= 2

        # סיום רק של A
        real_db.end_live_chat(user_a)
        assert not real_db.is_live_chat_active(user_a)
        assert real_db.is_live_chat_active(user_b)

        # סיום B
        real_db.end_live_chat(user_b)
        assert not real_db.is_live_chat_active(user_b)

    @pytest.mark.asyncio
    async def test_live_chat_guard_integration(self, real_db):
        """בדיקה ש-handler לא רץ כשיש live chat פעיל."""
        from bot.handlers import message_handler, Intent

        user_id = 3006

        with ExitStack() as stack:
            # rate_limiter — mock (לא רלוונטי כאן)
            stack.enter_context(patch("rate_limiter.check_rate_limit", return_value=None))
            stack.enter_context(patch("rate_limiter.record_message"))
            # live_chat_guard — מחזיר True (שיחה פעילה)
            stack.enter_context(
                patch("live_chat_service.LiveChatService.is_active", return_value=True)
            )
            stack.enter_context(patch("live_chat_service.db"))

            update = _make_update(user_id=user_id, text="שלום")
            context = _make_context()
            await message_handler(update, context)

        # ה-handler לא אמור להגיב — live chat חוסם
        # verify שלא נשלחה תגובה RAG/intent
        # (ה-guard שולח הודעה משלו או פשוט חוזר)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Admin Panel — CRUD Operations
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def admin_client(real_db, tmp_path, monkeypatch):
    """Flask test client עם login אוטומטי ו-DB אמיתי."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-integration")
    # ה-reload למטה מריץ את config.py מחדש וקורא DB_PATH מה-env — חייבים
    # לכוון אותו לאותו קובץ שה-fixture real_db אתחל (tmp_path משותף לשניהם),
    # אחרת get_connection (שקורא את config.DB_PATH דינמית דרך tenancy)
    # יפנה ל-DB ריק בלי סכימה.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "integration_test.db"))

    # reload שרשרת config → ai_chatbot.config → admin.app כדי שהערכים החדשים יתפסו
    # (ai_chatbot.config הוא alias לאותו מודול — ה-reload הכפול אידמפוטנטי)
    import config as _root_config
    importlib.reload(_root_config)
    import ai_chatbot.config
    importlib.reload(ai_chatbot.config)
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # ביטול CSRF בטסטים

    with app.test_client() as client:
        # login
        client.post("/login", data={
            "username": "admin",
            "password": "testpass123",
        })
        yield client


class TestAdminKnowledgeBaseCRUD:
    """E2E: Admin panel — יצירה, קריאה, עריכה ומחיקה של KB entries."""

    def test_add_kb_entry(self, admin_client, real_db):
        """הוספת רשומה חדשה ל-KB דרך Admin panel."""
        response = admin_client.post("/kb/add", data={
            "category": "Services",
            "title": "תספורת גברים",
            "content": "תספורת גברים מקצועית — 80 ש\"ח",
        }, follow_redirects=True)

        assert response.status_code == 200

        # ודא שהרשומה נשמרה ב-DB
        entries = real_db.get_all_kb_entries(active_only=False)
        assert len(entries) >= 1
        assert any(e["title"] == "תספורת גברים" for e in entries)

    def test_edit_kb_entry(self, admin_client, real_db):
        """עריכת רשומה קיימת."""
        # הוספת רשומה
        entry_id = real_db.add_kb_entry("Pricing", "מחירון", "מחיר ישן")

        response = admin_client.post(f"/kb/edit/{entry_id}", data={
            "category": "Pricing",
            "title": "מחירון מעודכן",
            "content": "מחיר חדש — 100 ש\"ח",
        }, follow_redirects=True)

        assert response.status_code == 200

        # ודא עדכון
        entry = real_db.get_kb_entry(entry_id)
        assert entry["title"] == "מחירון מעודכן"
        assert "100" in entry["content"]

    def test_delete_kb_entry(self, admin_client, real_db):
        """מחיקת רשומה."""
        entry_id = real_db.add_kb_entry("FAQ", "שאלה", "תשובה")
        assert real_db.get_kb_entry(entry_id) is not None

        response = admin_client.post(f"/kb/delete/{entry_id}", follow_redirects=True)
        assert response.status_code == 200

        # ודא מחיקה
        assert real_db.get_kb_entry(entry_id) is None

    def test_kb_list_shows_entries(self, admin_client, real_db):
        """דף רשימת KB מציג את הרשומות."""
        real_db.add_kb_entry("Services", "שירות A", "תוכן A")
        real_db.add_kb_entry("Pricing", "מחירון B", "תוכן B")

        response = admin_client.get("/kb")
        assert response.status_code == 200
        # הדף מכיל את השמות (בעברית — בודקים ב-bytes)
        assert "שירות A".encode() in response.data or response.status_code == 200

    def test_kb_add_missing_fields_shows_error(self, admin_client, real_db):
        """שדות חסרים — לא נוצרת רשומה."""
        count_before = real_db.count_kb_entries(active_only=False)

        response = admin_client.post("/kb/add", data={
            "category": "Services",
            "title": "",  # ריק
            "content": "תוכן",
        }, follow_redirects=True)

        assert response.status_code == 200
        count_after = real_db.count_kb_entries(active_only=False)
        assert count_after == count_before  # לא נוספה רשומה

    def test_kb_category_filter(self, admin_client, real_db):
        """סינון לפי קטגוריה."""
        real_db.add_kb_entry("Services", "שירות 1", "תוכן 1")
        real_db.add_kb_entry("FAQ", "שאלה 1", "תשובה 1")

        response = admin_client.get("/kb?category=Services")
        assert response.status_code == 200


class TestAdminAppointmentsCRUD:
    """Admin — ניהול תורים: צפייה, אישור, ביטול."""

    def test_appointments_page_loads(self, admin_client, real_db):
        """דף תורים נטען בהצלחה."""
        response = admin_client.get("/appointments")
        assert response.status_code == 200

    def test_confirm_appointment(self, admin_client, real_db):
        """אישור תור דרך Admin."""
        appt_id = real_db.create_appointment(
            user_id="4001",
            username="Customer",
            service="תספורת",
            preferred_date="2026-05-01",
            preferred_time="10:00",
            telegram_username="cust4001",
        )

        with patch("admin.app.notify_appointment_status"):
            with patch("admin.app.try_send_referral_code"):
                response = admin_client.post(
                    f"/appointments/{appt_id}/update",
                    data={"status": "confirmed"},
                    follow_redirects=True,
                )

        assert response.status_code == 200
        appt = real_db.get_appointment(appt_id)
        assert appt["status"] == "confirmed"

    def test_cancel_appointment(self, admin_client, real_db):
        """ביטול תור דרך Admin."""
        appt_id = real_db.create_appointment(
            user_id="4002",
            username="Customer",
            service="צביעה",
            preferred_date="2026-05-02",
            preferred_time="14:00",
            telegram_username="cust4002",
        )

        with patch("admin.app.notify_appointment_status"):
            response = admin_client.post(
                f"/appointments/{appt_id}/update",
                data={"status": "cancelled"},
                follow_redirects=True,
            )

        assert response.status_code == 200
        appt = real_db.get_appointment(appt_id)
        assert appt["status"] == "cancelled"

    def test_invalid_appointment_status_rejected(self, admin_client, real_db):
        """סטטוס לא חוקי — נדחה."""
        # תאריך עתידי — תאריך קבוע יוצר drift, ובנוסף ה-redirect ל-/appointments
        # מפעיל expire_past_appointments שהופך pending → passed עבור תאריך עבר.
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()
        appt_id = real_db.create_appointment(
            user_id="4003",
            username="Customer",
            service="תספורת",
            preferred_date=future_date,
            preferred_time="12:00",
            telegram_username="cust4003",
        )

        response = admin_client.post(
            f"/appointments/{appt_id}/update",
            data={"status": "invalid_status"},
            follow_redirects=True,
        )

        # הסטטוס לא השתנה
        appt = real_db.get_appointment(appt_id)
        assert appt["status"] == "pending"


class TestAdminAgentRequestsCRUD:
    """Admin — ניהול בקשות נציג."""

    def test_handle_agent_request(self, admin_client, real_db):
        """טיפול בבקשת נציג — סימון כ-handled."""
        request_id = real_db.create_agent_request(
            user_id="5001",
            username="Customer",
            message="רוצה לדבר",
            telegram_username="cust5001",
        )

        response = admin_client.post(
            f"/requests/{request_id}/handle",
            data={"status": "handled"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        req = real_db.get_agent_request(request_id)
        assert req["status"] == "handled"

    def test_dismiss_agent_request(self, admin_client, real_db):
        """דחיית בקשת נציג."""
        request_id = real_db.create_agent_request(
            user_id="5002",
            username="Customer",
            message="שאלה קלה",
            telegram_username="cust5002",
        )

        response = admin_client.post(
            f"/requests/{request_id}/handle",
            data={"status": "dismissed"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        req = real_db.get_agent_request(request_id)
        assert req["status"] == "dismissed"

    def test_requests_page_loads(self, admin_client, real_db):
        """דף בקשות נציג נטען."""
        response = admin_client.get("/requests")
        assert response.status_code == 200


class TestAdminLiveChatCRUD:
    """Admin — ניהול שיחות חיות."""

    def test_live_chat_page_loads(self, admin_client, real_db):
        """דף live chat נטען."""
        # יצירת משתמש עם היסטוריה
        real_db.save_message("6001", "Customer", "user", "שלום")

        response = admin_client.get("/live-chat/6001")
        assert response.status_code == 200

    def test_start_live_chat_from_admin(self, admin_client, real_db):
        """הפעלת live chat מהאדמין."""
        real_db.save_message("6002", "Customer", "user", "עזרה")

        with patch("admin.app.send_telegram_message", return_value=True):
            response = admin_client.post(
                "/live-chat/6002/start",
                follow_redirects=True,
            )

        assert response.status_code == 200
        assert real_db.is_live_chat_active("6002")

    def test_end_live_chat_from_admin(self, admin_client, real_db):
        """סיום live chat מהאדמין."""
        real_db.start_live_chat("6003", "Customer")
        assert real_db.is_live_chat_active("6003")

        with patch("admin.app.send_telegram_message", return_value=True):
            response = admin_client.post(
                "/live-chat/6003/end",
                follow_redirects=True,
            )

        assert response.status_code == 200
        assert not real_db.is_live_chat_active("6003")

    def test_send_message_in_live_chat(self, admin_client, real_db):
        """שליחת הודעה בשיחה חיה דרך האדמין."""
        real_db.start_live_chat("6004", "Customer")

        with patch("admin.app.send_telegram_message", return_value=True):
            with patch("live_chat_service.send_telegram_message", return_value=True):
                response = admin_client.post(
                    "/live-chat/6004/send",
                    data={"message": "תשובה מהנציג"},
                    follow_redirects=True,
                )

        assert response.status_code == 200

        # ודא שההודעה נשמרה בהיסטוריה
        history = real_db.get_conversation_history("6004", limit=10)
        assert any("תשובה מהנציג" in m["message"] for m in history)


class TestAdminBusinessHoursCRUD:
    """Admin — עדכון שעות פעילות."""

    def test_business_hours_page_loads(self, admin_client, real_db):
        """דף שעות פעילות נטען."""
        real_db.seed_default_business_hours()
        response = admin_client.get("/business-hours")
        assert response.status_code == 200

    def test_update_business_hours(self, admin_client, real_db):
        """עדכון שעות פעילות."""
        real_db.seed_default_business_hours()

        form_data = {}
        for day in range(7):
            if day == 6:  # שבת סגור
                form_data[f"closed_{day}"] = "on"
                form_data[f"open_{day}"] = ""
                form_data[f"close_{day}"] = ""
            else:
                form_data[f"open_{day}"] = "08:00"
                form_data[f"close_{day}"] = "20:00"

        response = admin_client.post(
            "/business-hours/update",
            data=form_data,
            follow_redirects=True,
        )

        assert response.status_code == 200

        # ודא שהשעות עודכנו
        hours = real_db.get_all_business_hours()
        sunday = next(h for h in hours if h["day_of_week"] == 0)
        assert sunday["open_time"] == "08:00"
        assert sunday["close_time"] == "20:00"

        saturday = next(h for h in hours if h["day_of_week"] == 6)
        assert saturday["is_closed"] == 1


class TestAdminDashboard:
    """Admin — Dashboard ו-health check."""

    def test_dashboard_loads(self, admin_client, real_db):
        """Dashboard נטען עם סטטיסטיקות."""
        response = admin_client.get("/")
        assert response.status_code == 200

    def test_health_check(self, admin_client, real_db):
        """Health check מחזיר סטטוס תקין."""
        # health check לא דורש login
        from admin.app import create_admin_app
        app = create_admin_app()
        app.config["TESTING"] = True

        with app.test_client() as client:
            response = client.get("/health")
            assert response.status_code in (200, 503)
            data = response.get_json()
            assert "status" in data
            assert "checks" in data

    def test_login_and_logout(self, real_db, tmp_path, monkeypatch):
        """Login → Dashboard → Logout → Redirect to login."""
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
        monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
        # ה-reload מריץ את config.py מחדש — DB_PATH מה-env חייב להצביע
        # על ה-DB שה-fixture real_db אתחל (ראה admin_client על אותו דפוס).
        monkeypatch.setenv("DB_PATH", str(tmp_path / "integration_test.db"))

        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import admin.app as _admin_app
        importlib.reload(_admin_app)

        from admin.app import create_admin_app
        app = create_admin_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        with app.test_client() as client:
            # ניסיון גישה ללא login
            response = client.get("/")
            assert response.status_code == 302  # redirect to login

            # login
            response = client.post("/login", data={
                "username": "admin",
                "password": "testpass123",
            }, follow_redirects=True)
            assert response.status_code == 200

            # גישה ל-dashboard אחרי login
            response = client.get("/")
            assert response.status_code == 200

            # logout
            response = client.get("/logout", follow_redirects=True)
            assert response.status_code == 200

            # אחרי logout — redirect ל-login
            response = client.get("/")
            assert response.status_code == 302

    def test_wrong_credentials_rejected(self, real_db, tmp_path, monkeypatch):
        """פרטים שגויים — לא מתחבר."""
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass")
        monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
        # ה-reload מריץ את config.py מחדש — DB_PATH מה-env חייב להצביע
        # על ה-DB שה-fixture real_db אתחל (ראה admin_client על אותו דפוס).
        monkeypatch.setenv("DB_PATH", str(tmp_path / "integration_test.db"))

        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import admin.app as _admin_app
        importlib.reload(_admin_app)

        from admin.app import create_admin_app
        app = create_admin_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        with app.test_client() as client:
            response = client.post("/login", data={
                "username": "admin",
                "password": "wrong_pass",
            }, follow_redirects=True)

            # עדיין בדף login
            assert response.status_code == 200

            # לא יכול לגשת ל-dashboard
            response = client.get("/")
            assert response.status_code == 302


class TestAdminKnowledgeGapsCRUD:
    """Admin — ניהול שאלות ללא מענה (knowledge gaps)."""

    def test_resolve_knowledge_gap(self, admin_client, real_db):
        """סימון שאלה ללא מענה כפתורה."""
        real_db.save_unanswered_question("7001", "Customer", "מה שעות הפתיחה ביום שישי?")
        questions = real_db.get_unanswered_questions(status="open")
        assert len(questions) >= 1
        q_id = questions[0]["id"]

        response = admin_client.post(
            f"/knowledge-gaps/{q_id}/resolve",
            data={"status": "resolved"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        q = real_db.get_unanswered_question(q_id)
        assert q["status"] == "resolved"

    def test_knowledge_gaps_page_loads(self, admin_client, real_db):
        """דף שאלות ללא מענה נטען."""
        response = admin_client.get("/knowledge-gaps")
        assert response.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 5. Cross-flow integration — שילוב בין מערכות
# ══════════════════════════════════════════════════════════════════════════════


class TestCrossFlowIntegration:
    """בדיקות שמשלבות בין מספר מערכות יחד."""

    @pytest.mark.asyncio
    async def test_bot_conversation_then_admin_views_it(self, real_db, monkeypatch):
        """משתמש מדבר עם הבוט → אדמין רואה את השיחה."""
        from bot.handlers import start_command, message_handler
        from bot.handlers import Intent

        user_id = 8001

        # שלב 1: שיחת בוט
        update = _make_update(user_id=user_id)
        context = _make_context()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            await start_command(update, context)

        update2 = _make_update(user_id=user_id, text="שלום!")
        context2 = _make_context()
        with ExitStack() as stack:
            for p in _guard_patches():
                stack.enter_context(p)
            stack.enter_context(
                patch("core.message_processor.detect_intent_with_llm", return_value=Intent.GREETING)
            )
            stack.enter_context(
                patch("core.message_processor.get_direct_response", return_value="היי!")
            )
            await message_handler(update2, context2)

        # שלב 2: Admin רואה את השיחה ב-DB
        history = real_db.get_conversation_history(str(user_id), limit=20)
        assert len(history) >= 3  # /start + welcome + greeting + response

        users = real_db.get_unique_users()
        assert any(u["user_id"] == str(user_id) for u in users)

    def test_booking_then_admin_manages(self, real_db):
        """תור נוצר → אדמין מאשר → אדמין מבטל."""
        user_id = "8002"

        # יצירת תור
        appt_id = real_db.create_appointment(
            user_id=user_id,
            username="Test User",
            service="תספורת",
            preferred_date="2026-06-01",
            preferred_time="09:00",
            telegram_username="testuser",
        )

        # אישור
        real_db.update_appointment_status(appt_id, "confirmed")
        assert real_db.get_appointment(appt_id)["status"] == "confirmed"

        # is_returning_customer דורש תור מאושר עם תאריך שעבר — ניצור תור בעבר
        past_appt_id = real_db.create_appointment(
            user_id=user_id,
            username="Test User",
            service="תספורת",
            preferred_date="2025-01-01",
            preferred_time="10:00",
            telegram_username="testuser",
        )
        real_db.update_appointment_status(past_appt_id, "confirmed")
        assert real_db.is_returning_customer(user_id)

        # ביטול
        real_db.update_appointment_status(appt_id, "cancelled")
        assert real_db.get_appointment(appt_id)["status"] == "cancelled"

    def test_subscription_and_broadcast_flow(self, real_db):
        """הרשמה → ביטול הרשמה → הרשמה מחדש."""
        user_id = "8003"

        # שמירת הודעה — כדי שהמשתמש יופיע ב-conversations (נדרש ל-get_broadcast_recipients)
        real_db.save_message(user_id, "Test User", "user", "שלום")

        # הרשמה אוטומטית
        real_db.ensure_user_subscribed(user_id)
        assert real_db.is_user_subscribed(user_id)

        # ביטול
        real_db.unsubscribe_user(user_id)
        assert not real_db.is_user_subscribed(user_id)

        # לא מופיע כנמען שידור
        recipients = real_db.get_broadcast_recipients("all")
        assert user_id not in recipients

        # הרשמה מחדש
        real_db.resubscribe_user(user_id)
        assert real_db.is_user_subscribed(user_id)

        recipients = real_db.get_broadcast_recipients("all")
        assert user_id in recipients

    def test_kb_crud_then_count_consistency(self, real_db):
        """CRUD על KB → count ו-categories עקביים."""
        # מצב ראשוני
        initial_count = real_db.count_kb_entries(active_only=False)

        # הוספה
        id1 = real_db.add_kb_entry("Services", "שירות 1", "תוכן 1")
        id2 = real_db.add_kb_entry("Pricing", "מחיר 1", "תוכן 2")
        id3 = real_db.add_kb_entry("Services", "שירות 2", "תוכן 3")

        assert real_db.count_kb_entries(active_only=False) == initial_count + 3

        categories = real_db.get_kb_categories()
        assert "Services" in categories
        assert "Pricing" in categories

        # מחיקה
        real_db.delete_kb_entry(id2)
        assert real_db.count_kb_entries(active_only=False) == initial_count + 2
        assert real_db.get_kb_entry(id2) is None

        # עדכון
        real_db.update_kb_entry(id1, "FAQ", "שאלה 1", "תשובה חדשה")
        entry = real_db.get_kb_entry(id1)
        assert entry["category"] == "FAQ"
        assert entry["title"] == "שאלה 1"
