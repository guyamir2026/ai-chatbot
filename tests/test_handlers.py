"""
טסטים ל-bot/handlers.py — פונקציות עזר, ניתוב intent, ו-handlers עיקריים.

מוקים: telegram Update/Context, DB, LLM, config values.
"""

import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_update(user_id: int = 100, text: str = "שלום", username: str = "testuser"):
    """יוצר mock Update עם effective_user ו-message."""
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
    """יוצר mock Context עם bot, user_data, bot_data."""
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
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── Pure / semi-pure helpers ────────────────────────────────────────────────


class TestGetUserInfo:
    def test_extracts_user_info(self):
        from bot.handlers import _get_user_info
        update = _make_update(user_id=42, username="moshe")
        update.effective_user.full_name = "Moshe Cohen"
        uid, name, uname = _get_user_info(update)
        assert uid == "42"
        assert name == "Moshe Cohen"
        assert uname == "moshe"

    def test_fallback_when_no_full_name(self):
        from bot.handlers import _get_user_info
        update = _make_update(user_id=7, username="dani")
        update.effective_user.full_name = ""
        _, name, _ = _get_user_info(update)
        assert "@dani" in name

    def test_fallback_when_no_username(self):
        from bot.handlers import _get_user_info
        update = _make_update(user_id=7, username="")
        update.effective_user.full_name = ""
        update.effective_user.username = ""
        _, name, _ = _get_user_info(update)
        assert "7" in name


class TestTgHandle:
    def test_with_username(self):
        from bot.handlers import _tg_handle
        assert _tg_handle("moshe") == "@moshe"

    def test_without_username(self):
        from bot.handlers import _tg_handle
        assert _tg_handle("") == ""


class TestShouldHandoffToHuman:
    def test_empty_text(self):
        from bot.handlers import _should_handoff_to_human
        assert not _should_handoff_to_human("")
        assert not _should_handoff_to_human(None)

    def test_fallback_response(self):
        from bot.handlers import _should_handoff_to_human, FALLBACK_RESPONSE
        assert _should_handoff_to_human(FALLBACK_RESPONSE)

    def test_handoff_marker(self):
        """אחרי המעבר ל-marker-based detection: זיהוי מבוסס טוקן בלבד.
        ניסוחים חופשיים של ה-LLM ('תנו לי להעביר את הפנייה') לא נחשבים
        handoff בלי הטוקן — מונע false positives."""
        from bot.handlers import _should_handoff_to_human
        from config import HANDOFF_MARKER, FALLBACK_RESPONSE
        assert _should_handoff_to_human(f"{HANDOFF_MARKER}\n\n{FALLBACK_RESPONSE}")

    def test_normal_text(self):
        from bot.handlers import _should_handoff_to_human
        assert not _should_handoff_to_human("שעות הפתיחה שלנו הן 9-17")
        # ניסוח עם 'אעביר את הפנייה' בלי טוקן — לא handoff
        assert not _should_handoff_to_human(
            "אני מבין שתרצו לדבר עם נציג. אעביר את הפנייה לבעל העסק."
        )


class TestVcardEscape:
    def test_escapes_special_chars(self):
        from bot.handlers import _vcard_escape
        assert _vcard_escape("a;b,c\\d") == "a\\;b\\,c\\\\d"

    def test_plain_text(self):
        from bot.handlers import _vcard_escape
        assert _vcard_escape("hello") == "hello"

    def test_escapes_newline(self):
        """‏\\n בערך — נדרש לשעות רב-שורתיות ב-NOTE (RFC 6350)."""
        from bot.handlers import _vcard_escape
        assert _vcard_escape("a\nb") == "a\\nb"


class TestGenerateVcardText:
    def test_generates_valid_vcard(self, db):
        from bot.handlers import _generate_vcard_text
        vcard = _generate_vcard_text()
        assert vcard.startswith("BEGIN:VCARD")
        assert vcard.endswith("END:VCARD")
        assert "VERSION:3.0" in vcard

    def test_hours_note_hebrew_multiline(self, db):
        """שעות ב-NOTE — שמות ימים בעברית, שורה ליום (\\n), כולל 'סגור'."""
        from bot.handlers import _generate_vcard_text
        db.upsert_business_hours(0, "09:00", "19:30", False)  # ראשון
        db.upsert_business_hours(6, "00:00", "00:00", True)   # שבת — סגור
        vcard = _generate_vcard_text()
        assert "שעות פעילות:" in vcard
        assert "ראשון: 09:00-19:30" in vcard
        assert "שבת: סגור" in vcard
        # ריבוי-שורות מקודד כ-\n בתוך ערך ה-NOTE (RFC 6350), לא כשורה פיזית
        note_line = next(l for l in vcard.split("\r\n") if l.startswith("NOTE:"))
        assert "\\n" in note_line


# ── Follow-up questions helpers ──────────────────────────────────────────────


class TestCleanupStaleFollowUps:
    def test_removes_old_entries(self):
        from bot.handlers import _cleanup_stale_follow_ups, FOLLOW_UP_CB_PREFIX
        old_ts = int(time.time()) - 7200  # שעתיים לפני
        bot_data = {
            f"{FOLLOW_UP_CB_PREFIX}123_{old_ts}_0": "שאלה ישנה",
            f"{FOLLOW_UP_CB_PREFIX}123_{old_ts}_1": "שאלה ישנה 2",
            "some_other_key": "value",
        }
        _cleanup_stale_follow_ups(bot_data)
        assert "some_other_key" in bot_data
        assert len(bot_data) == 1  # רק some_other_key נשאר

    def test_keeps_fresh_entries(self):
        from bot.handlers import _cleanup_stale_follow_ups, FOLLOW_UP_CB_PREFIX
        now_ts = int(time.time())
        bot_data = {
            f"{FOLLOW_UP_CB_PREFIX}123_{now_ts}_0": "שאלה חדשה",
        }
        _cleanup_stale_follow_ups(bot_data)
        assert len(bot_data) == 1


class TestBuildFollowUpKeyboard:
    def test_creates_keyboard(self):
        from bot.handlers import _build_follow_up_keyboard
        bot_data = {}
        kb = _build_follow_up_keyboard(["שאלה 1", "שאלה 2"], bot_data, "42")
        assert kb is not None
        assert len(bot_data) == 2  # שתי שאלות נשמרו

    def test_empty_questions_returns_none(self):
        from bot.handlers import _build_follow_up_keyboard
        assert _build_follow_up_keyboard([], {}, "42") is None


# ── _reply_html_safe ────────────────────────────────────────────────────────


class TestReplyHtmlSafe:
    @pytest.mark.asyncio
    async def test_sends_html(self):
        from bot.handlers import _reply_html_safe
        message = AsyncMock()
        await _reply_html_safe(message, "<b>test</b>")
        message.reply_text.assert_awaited_once_with("<b>test</b>", parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_falls_back_on_bad_request(self):
        from bot.handlers import _reply_html_safe
        from telegram.error import BadRequest
        message = AsyncMock()
        message.reply_text.side_effect = [BadRequest("bad html"), None]
        await _reply_html_safe(message, "<bad>")
        assert message.reply_text.call_count == 2

    @pytest.mark.asyncio
    async def test_none_message(self):
        from bot.handlers import _reply_html_safe
        result = await _reply_html_safe(None, "text")
        assert result is None


# ── _notify_owner ────────────────────────────────────────────────────────────


class TestNotifyOwner:
    @pytest.mark.asyncio
    async def test_success(self):
        from bot.handlers import _notify_owner
        context = _make_context()
        with patch("bot.handlers.TELEGRAM_OWNER_CHAT_ID", "999"):
            result = await _notify_owner(context, "test notification")
        assert result is True
        context.bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_owner_id(self):
        from bot.handlers import _notify_owner
        context = _make_context()
        with patch("bot.handlers.TELEGRAM_OWNER_CHAT_ID", ""):
            result = await _notify_owner(context, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        from bot.handlers import _notify_owner
        from telegram.error import TimedOut
        context = _make_context()
        context.bot.send_message.side_effect = [TimedOut(), None]
        with patch("bot.handlers.TELEGRAM_OWNER_CHAT_ID", "999"):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await _notify_owner(context, "test", max_retries=2)
        assert result is True
        assert context.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self):
        from bot.handlers import _notify_owner
        from telegram.error import TimedOut
        context = _make_context()
        context.bot.send_message.side_effect = TimedOut()
        with patch("bot.handlers.TELEGRAM_OWNER_CHAT_ID", "999"):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await _notify_owner(context, "test", max_retries=2)
        assert result is False

    @pytest.mark.asyncio
    async def test_unexpected_error_fails_immediately(self):
        from bot.handlers import _notify_owner
        context = _make_context()
        context.bot.send_message.side_effect = RuntimeError("boom")
        with patch("bot.handlers.TELEGRAM_OWNER_CHAT_ID", "999"):
            result = await _notify_owner(context, "test")
        assert result is False
        context.bot.send_message.assert_awaited_once()


# ── Intent routing in message_handler ────────────────────────────────────────


def _handler_patches():
    """Context managers משותפים לכל טסטי handlers — מוקים לדקורטורים ולתלויות.

    הדקורטורים (rate_limit_guard, live_chat_guard) מפנים לפונקציות ב-modules
    המקוריים שלהם, לכן צריך לעשות patch שם ולא ב-bot.handlers.
    """
    return [
        patch("rate_limiter.check_rate_limit", return_value=None),
        patch("rate_limiter.record_message"),
        patch("live_chat_service.LiveChatService.is_active", return_value=False),
        patch("live_chat_service.db"),
        patch("bot.handlers.db"),
    ]


from contextlib import ExitStack


class TestMessageHandlerIntentRouting:
    """בדיקת ניתוב intent ב-message_handler — ללא RAG/LLM.

    message_handler מפעיל את process_incoming_message מ-core/message_processor.py
    דרך asyncio.to_thread, ולכן אנחנו עושים mock ל-process_incoming_message
    ובודקים שה-handler ממפה את MessageResult לפעולות Telegram נכונות.
    """

    @pytest.mark.asyncio
    async def test_greeting_routed_directly(self, db):
        from bot.handlers import message_handler
        from core.message_processor import MessageResult
        from intent import Intent
        update = _make_update(text="שלום!")
        context = _make_context()

        greeting_result = MessageResult(text="היי!", intent=Intent.GREETING)

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.is_referral_code_sent = MagicMock(return_value=True)
            stack.enter_context(patch("bot.handlers.process_incoming_message", return_value=greeting_result))

            await message_handler(update, context)

        update.message.reply_text.assert_awaited()
        call_args = update.message.reply_text.call_args
        assert "היי!" in str(call_args)

    @pytest.mark.asyncio
    async def test_business_hours_routed_directly(self, db):
        from bot.handlers import message_handler
        from core.message_processor import MessageResult
        from intent import Intent
        update = _make_update(text="מתי אתם פתוחים?")
        context = _make_context()

        hours_result = MessageResult(
            text="פתוח\n\nראשון-חמישי 9-17",
            intent=Intent.BUSINESS_HOURS,
        )

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.is_referral_code_sent = MagicMock(return_value=True)
            stack.enter_context(patch("bot.handlers.process_incoming_message", return_value=hours_result))

            await message_handler(update, context)

        update.message.reply_text.assert_awaited()
        text_sent = update.message.reply_text.call_args[0][0]
        assert "פתוח" in text_sent
        assert "ראשון-חמישי" in text_sent

    @pytest.mark.asyncio
    async def test_complaint_offers_agent(self, db):
        from bot.handlers import message_handler
        from core.message_processor import MessageResult
        from intent import Intent
        update = _make_update(text="שירות גרוע!")
        context = _make_context()

        complaint_result = MessageResult(
            text="אנחנו מצטערים לשמוע שהחוויה לא הייתה טובה.",
            intent=Intent.COMPLAINT,
            action="complaint",
            is_html=True,
        )

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.is_referral_code_sent = MagicMock(return_value=True)
            stack.enter_context(patch("bot.handlers.process_incoming_message", return_value=complaint_result))

            await message_handler(update, context)

        update.message.reply_text.assert_awaited()

    @pytest.mark.asyncio
    async def test_appointment_booking_during_vacation(self, db):
        from bot.handlers import message_handler
        from core.message_processor import MessageResult
        from intent import Intent
        update = _make_update(text="רוצה לקבוע תור")
        context = _make_context()

        vacation_result = MessageResult(
            text="אנחנו בחופשה!",
            intent=Intent.APPOINTMENT_BOOKING,
        )

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.is_referral_code_sent = MagicMock(return_value=True)
            stack.enter_context(patch("bot.handlers.process_incoming_message", return_value=vacation_result))

            await message_handler(update, context)

        text_sent = update.message.reply_text.call_args[0][0]
        assert "חופשה" in text_sent


# ── Start command ────────────────────────────────────────────────────────────


class TestStartCommand:
    @pytest.mark.asyncio
    async def test_sends_welcome_message(self, db):
        from bot.handlers import start_command
        update = _make_update()
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.save_message = MagicMock()
            mock_db.register_referral = MagicMock(return_value=False)
            mock_db.is_returning_customer = MagicMock(return_value=False)

            await start_command(update, context)

        update.message.reply_text.assert_awaited_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "ברוכים הבאים" in call_text

    @pytest.mark.asyncio
    async def test_referral_code_bonus_text(self, db):
        from bot.handlers import start_command
        update = _make_update()
        context = _make_context(args=["REF_ABC123"])

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.save_message = MagicMock()
            mock_db.register_referral = MagicMock(return_value=True)
            mock_db.is_returning_customer = MagicMock(return_value=False)
            mock_db.get_bot_settings = MagicMock(return_value={
                "referral_enabled": 1,
                "referral_discount": 10.0,
                "referral_validity_days": 60,
            })

            await start_command(update, context)

        call_text = update.message.reply_text.call_args[0][0]
        assert "הפניה" in call_text

    @pytest.mark.asyncio
    async def test_referral_pending_until_consent(self, db):
        """משתמש חדש (ללא הסכמה) מגיע עם REF_ קוד מ-deep link:
        - אסור שיירשם ב-DB עד שהמשתמש מסכים (תיקון 13: לא לכתוב PII בלי הסכמה)
        - אסור ש-upsert_user / ensure_user_subscribed יקראו לפני הסכמה
        - הקוד נשמר ב-context.user_data ועובר ל-consent_callback
        """
        from bot.handlers import start_command
        update = _make_update()
        context = _make_context(args=["REF_NEWUSER"])

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.upsert_user = MagicMock()
            mock_db.has_consent = MagicMock(return_value=False)
            mock_db.register_referral = MagicMock(return_value=True)

            await start_command(update, context)

        # Compliance: ללא הסכמה — אסור לכתוב PII או לרשום הפניה.
        mock_db.register_referral.assert_not_called()
        mock_db.upsert_user.assert_not_called()
        mock_db.ensure_user_subscribed.assert_not_called()
        # הקוד נשמר ב-user_data לעיבוד אחרי לחיצה על "אני מסכים"
        assert context.user_data.get("pending_referral_code") == "REF_NEWUSER"

    @pytest.mark.asyncio
    async def test_referral_registered_after_consent(self, db):
        """משתמש שכבר נתן הסכמה ומגיע עם REF_ קוד — נרשם מיד."""
        from bot.handlers import start_command
        update = _make_update()
        context = _make_context(args=["REF_RETURNING"])

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.upsert_user = MagicMock()
            mock_db.has_consent = MagicMock(return_value=True)
            mock_db.is_returning_customer = MagicMock(return_value=False)
            mock_db.register_referral = MagicMock(return_value=True)
            mock_db.save_message = MagicMock()
            mock_db.get_bot_settings = MagicMock(return_value={
                "referral_enabled": 1, "referral_discount": 10.0, "referral_validity_days": 60,
            })

            await start_command(update, context)

        mock_db.register_referral.assert_called_once_with("REF_RETURNING", "100")
        mock_db.upsert_user.assert_called_once()
        mock_db.ensure_user_subscribed.assert_called_once_with("100")

    @pytest.mark.asyncio
    async def test_agent_command_blocks_without_consent(self, db):
        """רגרסיה: /agent (alias ל-talk_to_agent) חייב לחסום ולהציג מסך הסכמה
        אם המשתמש עוד לא נתן הסכמה. אחרת PII (סיבת פנייה) נשלח לבעל העסק
        בלי קשר למסך ההסכמה."""
        from bot.handlers import talk_to_agent_handler
        update = _make_update()
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.has_consent = MagicMock(return_value=False)
            # _talk_to_agent_core לא אמור להיקרא בכלל. נמקן את כל ה-DB
            # קוראים שלו כדי שאם הוא ייקרא בטעות, נראה שהטסט נפל בתוצאה.
            mock_db.create_agent_request = MagicMock()

            await talk_to_agent_handler(update, context)

        # אם consent_guard עובד — create_agent_request לא נקרא, מסך הסכמה הוצג
        mock_db.create_agent_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_consent_screen_falls_back_to_plaintext_on_html_error(self, db):
        """רגרסיה: אם Telegram דוחה HTML (BadRequest) במסך ההסכמה —
        חייב להישלח fallback לטקסט רגיל. אחרת המשתמש תקוע ללא יציאה
        כי has_consent ימשיך להחזיר False וכל פעולה תפיל את אותה שגיאה.
        """
        from telegram.error import BadRequest
        from bot.handlers import _send_consent_screen
        update = _make_update()
        context = _make_context()

        # קריאה ראשונה זורקת BadRequest, השנייה (fallback) מצליחה
        update.message.reply_text = AsyncMock(
            side_effect=[BadRequest("can't parse entities"), MagicMock()],
        )

        await _send_consent_screen(update, context)

        # נקרא פעמיים: פעם עם HTML, פעם בלי
        assert update.message.reply_text.call_count == 2
        first_call_kwargs = update.message.reply_text.call_args_list[0].kwargs
        second_call_kwargs = update.message.reply_text.call_args_list[1].kwargs
        assert first_call_kwargs.get("parse_mode") == "HTML"
        assert "parse_mode" not in second_call_kwargs
        # הכפתורים נשמרים גם ב-fallback
        assert second_call_kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_returning_customer_greeting(self, db):
        """לקוח חוזר מקבל הודעת 'שמחים לראות אותך שוב'."""
        from bot.handlers import start_command
        update = _make_update()
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.ensure_user_subscribed = MagicMock()
            mock_db.save_message = MagicMock()
            mock_db.register_referral = MagicMock(return_value=False)
            mock_db.is_returning_customer = MagicMock(return_value=True)

            await start_command(update, context)

        call_text = update.message.reply_text.call_args[0][0]
        assert "שמחים לראות אותך שוב" in call_text


# ── Referral command ─────────────────────────────────────────────────────────


class TestReferralCommand:
    @pytest.mark.asyncio
    async def test_referral_command_with_existing_code(self, db):
        """משתמש עם קוד הפניה מקבל את הקוד בחזרה."""
        from bot.handlers import referral_command
        update = _make_update(user_id=500)
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.get_user_referral_code = MagicMock(return_value="REF_ABCD1234")

            await referral_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "REF_ABCD1234" in reply_text

    @pytest.mark.asyncio
    async def test_referral_command_no_code(self, db):
        """משתמש ללא קוד הפניה מקבל הודעת הסבר."""
        from bot.handlers import referral_command
        update = _make_update(user_id=501)
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.get_user_referral_code = MagicMock(return_value=None)

            await referral_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "עדיין לא" in reply_text


# ── Booking flow ─────────────────────────────────────────────────────────────


class TestBookingDisabled:
    """קביעת תורים כבויה לעסק — הכפתור מוסתר וכניסת ה-flow מחזירה END."""

    def test_keyboard_hides_booking_button_when_disabled(self, db):
        # telegram ממוקק ב-conftest (ReplyKeyboardMarkup/KeyboardButton = MagicMock),
        # לכן בודקים אילו כפתורים *נבנו* (call_args של KeyboardButton) ולא את
        # מבנה המקלדת המוחזר.
        from bot import handlers
        # פעיל (ברירת מחדל) — כפתור בקשת התור נבנה
        with patch.object(handlers, "KeyboardButton") as m_on:
            handlers._get_main_keyboard()
        on_labels = [c.args[0] for c in m_on.call_args_list if c.args]
        assert handlers.BUTTON_BOOKING in on_labels
        # כבוי — כפתור בקשת התור לא נבנה, אבל שאר הכפתורים כן
        s = db.get_bot_settings()
        db.update_bot_settings(s["tone"], s.get("custom_phrases", ""), booking_enabled=False)
        with patch.object(handlers, "KeyboardButton") as m_off:
            handlers._get_main_keyboard()
        off_labels = [c.args[0] for c in m_off.call_args_list if c.args]
        assert handlers.BUTTON_BOOKING not in off_labels
        assert handlers.BUTTON_PRICE_LIST in off_labels

    @pytest.mark.asyncio
    async def test_booking_start_core_returns_end_when_disabled(self, db):
        from bot.handlers import _booking_start_core
        from telegram.ext import ConversationHandler
        s = db.get_bot_settings()
        db.update_bot_settings(s["tone"], s.get("custom_phrases", ""), booking_enabled=False)
        update = _make_update(text="📅 בקשת תור")
        context = _make_context()
        with patch("bot.handlers._handoff_to_human", new=AsyncMock()) as mock_handoff:
            result = await _booking_start_core(update, context)
        assert result == ConversationHandler.END
        mock_handoff.assert_awaited_once()


class TestBookingFlow:
    @pytest.mark.asyncio
    async def test_booking_service_saves_and_advances(self, db):
        from bot.handlers import booking_service, BOOKING_DATE
        update = _make_update(text="תספורת")
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            # מוק ללוח השנה — booking_service קוראת לו אחרי בחירת שירות
            stack.enter_context(patch("bot.handlers.build_calendar_keyboard", return_value=MagicMock()))
            result = await booking_service(update, context)

        assert result == BOOKING_DATE
        assert context.user_data["booking_service"] == "תספורת"

    @pytest.mark.asyncio
    async def test_booking_date_saves_and_advances(self, db):
        from bot.handlers import booking_date, BOOKING_TIME
        update = _make_update(text="יום שני")
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            result = await booking_date(update, context)

        assert result == BOOKING_TIME
        # normalize_date ממיר "יום שני" לתאריך בפורמט YYYY-MM-DD
        assert context.user_data["booking_date"].count("-") == 2

    @pytest.mark.asyncio
    async def test_booking_time_saves_and_advances(self, db):
        from bot.handlers import booking_time, BOOKING_CONFIRM
        update = _make_update(text="10:00")
        context = _make_context()
        context.user_data = {
            "booking_service": "תספורת",
            "booking_date": "2026-04-06",
        }

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            result = await booking_time(update, context)

        assert result == BOOKING_CONFIRM
        assert context.user_data["booking_time"] == "10:00"

    @pytest.mark.asyncio
    async def test_booking_confirm_yes(self, db):
        from bot.handlers import booking_confirm
        from telegram.ext import ConversationHandler
        from datetime import date, timedelta
        # תאריך עתידי — תאריך בעבר גורם ל-decision להחזיר rejected(slot_in_past),
        # שמאז התיקון משאיר את הלקוח ב-flow (BOOKING_TIME) ולא מסיים ב-END.
        future_date = (date.today() + timedelta(days=7)).isoformat()
        update = _make_update(text="כן")
        context = _make_context()
        context.user_data = {
            "booking_service": "תספורת",
            "booking_date": future_date,
            "booking_time": "10:00",
        }

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            stack.enter_context(patch("bot.handlers._notify_owner", new_callable=AsyncMock, return_value=True))
            mock_db.create_appointment = MagicMock(return_value=1)
            mock_db.save_message = MagicMock()
            mock_db.get_pending_appointments_for_user = MagicMock(return_value=[])
            result = await booking_confirm(update, context)

        assert result == ConversationHandler.END
        assert context.user_data == {}
        mock_db.create_appointment.assert_called_once()

    @pytest.mark.asyncio
    async def test_booking_confirm_no(self, db):
        from bot.handlers import booking_confirm
        from telegram.ext import ConversationHandler
        update = _make_update(text="לא")
        context = _make_context()
        context.user_data = {"booking_service": "x"}

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.save_message = MagicMock()
            result = await booking_confirm(update, context)

        assert result == ConversationHandler.END
        assert "בוטלה" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_booking_cancel(self, db):
        from bot.handlers import booking_cancel
        from telegram.ext import ConversationHandler
        update = _make_update(text="/cancel")
        context = _make_context()
        context.user_data = {"booking_service": "תספורת"}

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            result = await booking_cancel(update, context)

        assert result == ConversationHandler.END
        assert context.user_data == {}

    # ── דחיית auto-booking — נשארים ב-flow ומאזינים לתיקון ────────────────
    async def _run_confirm_with_rejection(self, reason):
        """מריץ booking_confirm כשה-decision מחזיר rejected(reason).
        ה-recheck מדולג (google_calendar לא מיובא בסביבת הטסט). מחזיר
        (result, update, context)."""
        from bot.handlers import booking_confirm
        from core.booking_decision import BookingDecisionResult
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()
        update = _make_update(text="כן")
        context = _make_context()
        context.user_data = {
            "booking_service": "תספורת",
            "booking_date": future_date,
            "booking_time": "10:00",
        }
        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.create_appointment = MagicMock(return_value=1)
            mock_db.get_pending_appointments_for_user = MagicMock(return_value=[])
            mock_db.update_appointment_status = MagicMock()
            mock_db.save_message = MagicMock()
            stack.enter_context(patch(
                "ai_chatbot.core.booking_decision.gather_and_decide",
                return_value=BookingDecisionResult("rejected", reason),
            ))
            result = await booking_confirm(update, context)
        return result, update, context

    @pytest.mark.asyncio
    async def test_confirm_rejected_time_stays_in_flow(self, db):
        """דחיית שעה (calendar_busy) ⇒ BOOKING_TIME + user_data נשמר."""
        from bot.handlers import BOOKING_TIME
        result, _update, context = await self._run_confirm_with_rejection("calendar_busy")
        assert result == BOOKING_TIME
        assert context.user_data.get("booking_service") == "תספורת"
        assert context.user_data.get("booking_date")

    @pytest.mark.asyncio
    async def test_confirm_rejected_date_stays_in_flow(self, db):
        """דחיית יום סגור (closed_regular) ⇒ BOOKING_DATE + user_data נשמר."""
        from bot.handlers import BOOKING_DATE
        result, _update, context = await self._run_confirm_with_rejection("closed_regular")
        assert result == BOOKING_DATE
        assert context.user_data.get("booking_service") == "תספורת"

    @pytest.mark.asyncio
    async def test_confirm_rejected_terminal_ends(self, db):
        """דחייה סופית (vacation_active) ⇒ END + ניקוי user_data + /book."""
        from telegram.ext import ConversationHandler
        result, update, context = await self._run_confirm_with_rejection("vacation_active")
        assert result == ConversationHandler.END
        assert context.user_data == {}
        assert "/book" in update.message.reply_text.call_args[0][0]

    async def _run_confirm_recheck(self, slots):
        """מריץ booking_confirm עם GCal מזויף שמחזיר slots בבדיקה החוזרת.
        מחזיר (result, update, mock_db)."""
        import sys
        import types
        from bot.handlers import booking_confirm
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()
        update = _make_update(text="כן")
        context = _make_context()
        context.user_data = {
            "booking_service": "תספורת",
            "booking_date": future_date,
            "booking_time": "10:00",
        }
        fake_gcal = types.ModuleType("google_calendar")
        fake_gcal.is_connected = lambda: True
        fake_gcal.get_available_slots = lambda *a, **k: slots
        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.get_appointment_duration_settings = MagicMock(return_value={"default_minutes": 60})
            mock_db.get_auto_booking_buffer_minutes = MagicMock(return_value=0)
            mock_db.create_appointment = MagicMock(return_value=1)
            stack.enter_context(patch.dict(sys.modules, {"google_calendar": fake_gcal}))
            result = await booking_confirm(update, context)
        return result, update, mock_db

    @pytest.mark.asyncio
    async def test_recheck_no_slots_returns_to_date(self, db):
        """בדיקה חוזרת עם יום מלא (רשימה ריקה) ⇒ BOOKING_DATE, לא נתקעים בשעה."""
        from bot.handlers import BOOKING_DATE
        result, update, mock_db = await self._run_confirm_recheck([])
        assert result == BOOKING_DATE
        assert "אין שעות פנויות" in update.message.reply_text.call_args[0][0]
        mock_db.create_appointment.assert_not_called()

    @pytest.mark.asyncio
    async def test_recheck_other_slots_returns_to_time(self, db):
        """בדיקה חוזרת עם שעות אחרות ⇒ BOOKING_TIME + הצגת השעות."""
        from bot.handlers import BOOKING_TIME
        result, update, mock_db = await self._run_confirm_recheck(["09:00", "11:00"])
        assert result == BOOKING_TIME
        msg = update.message.reply_text.call_args[0][0]
        assert "09:00" in msg and "11:00" in msg
        mock_db.create_appointment.assert_not_called()

    def test_rejection_recovery_covers_all_known_reasons(self):
        """drift-guard: כל סיבת דחייה מוכרת נכנסת ל-time/date/terminal. מונע
        שכפול-שקט מול הסטים המקומיים ב-WhatsApp כשמוסיפים סיבה חדשה."""
        from bot.handlers import (
            _rejection_recovery_step, _REJECT_RETRY_TIME, _REJECT_RETRY_DATE,
        )
        from core.booking_decision import _REJECTION_MESSAGES
        assert _REJECT_RETRY_TIME.isdisjoint(_REJECT_RETRY_DATE)
        for reason in _REJECTION_MESSAGES:
            step = _rejection_recovery_step(reason)
            assert step in ("time", "date", "terminal")
            if reason in _REJECT_RETRY_TIME:
                assert step == "time"
            elif reason in _REJECT_RETRY_DATE:
                assert step == "date"
            else:
                assert step == "terminal"


# ── Error handler ────────────────────────────────────────────────────────────


class TestErrorHandler:
    @pytest.mark.asyncio
    async def test_replies_to_user(self):
        from bot.handlers import error_handler
        update = _make_update()
        context = _make_context()
        context.error = RuntimeError("boom")
        await error_handler(update, context)
        update.effective_message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_no_update(self):
        from bot.handlers import error_handler
        context = _make_context()
        context.error = RuntimeError("boom")
        # לא צריך לקרוס
        await error_handler(None, context)


# ── format_context (rag engine — מכוסה כאן כי קל לבדוק) ─────────────────────


class TestFormatContext:
    def test_formats_chunks(self):
        from rag.engine import format_context
        chunks = [
            {"category": "שירותים", "title": "תספורת", "text": "מחיר 50 ש\"ח"},
        ]
        result = format_context(chunks)
        assert "Context 1" in result
        assert "שירותים" in result
        assert "תספורת" in result

    def test_empty_chunks(self):
        from rag.engine import format_context
        result = format_context([])
        assert "No relevant" in result


class TestPrivacyCommandsNoSaveMessage:
    """רגרסיה: /myinfo ו-/forget לא שומרים PII ב-conversations.

    הסיבה: אלו פקודות "זכויות נושא מידע" שחייבות לעבוד גם בלי הסכמה.
    אם הן יוצרות רשומות PII חדשות בעצמן, אנו מפרים את עיקרון תיקון 13.
    """

    @pytest.mark.asyncio
    async def test_myinfo_does_not_save_message(self, db):
        from bot.handlers import myinfo_command
        update = _make_update()
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.save_message = MagicMock()
            mock_db.get_user_data_summary = MagicMock(return_value={"exists": False})

            await myinfo_command(update, context)

        mock_db.save_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_forget_command_does_not_save_message(self, db):
        from bot.handlers import forget_command
        update = _make_update()
        context = _make_context()

        with ExitStack() as stack:
            for p in _handler_patches():
                stack.enter_context(p)
            mock_db = stack.enter_context(patch("bot.handlers.db"))
            mock_db.save_message = MagicMock()

            await forget_command(update, context)

        mock_db.save_message.assert_not_called()
