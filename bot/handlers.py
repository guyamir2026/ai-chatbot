"""
Telegram Bot Handlers — all command and callback handlers for the customer-facing bot.

Features:
- /start — Welcome message with main menu buttons
- Free-text messages — Answered via RAG + LLM pipeline
- "Book Appointment" button — Starts appointment booking flow
- "Talk to Agent" button — Sends notification to business owner
- "Send Location" button — Sends business location
- "Price List" button — Shows the price list from KB
- Conversation history per user
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import html as _html
import logging
import time
from zoneinfo import ZoneInfo
from io import BytesIO
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlite3 import IntegrityError
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import ContextTypes, ConversationHandler

from ai_chatbot import database as db
from ai_chatbot.llm import generate_answer, strip_source_citation, sanitize_telegram_html, maybe_summarize
from ai_chatbot.intent import Intent, detect_intent_with_llm, get_direct_response
from ai_chatbot.business_hours import is_currently_open, get_weekly_schedule_text, get_out_of_office_agent_notice, DAY_NAMES_HE
from ai_chatbot.config import (
    ADMIN_URL,
    get_business_config,
    build_intro_disclaimer,
    CONSENT_SCREEN_ENABLED,
    TELEGRAM_OWNER_CHAT_ID,
    FALLBACK_RESPONSE,
    CONTEXT_WINDOW_SIZE,
    FOLLOW_UP_ENABLED,
)
from ai_chatbot.entity_extraction import extract_dates, normalize_date
from ai_chatbot.live_chat_service import live_chat_guard, live_chat_guard_booking
from ai_chatbot.rate_limiter import (
    block_guard, block_guard_booking,
    rate_limit_guard, rate_limit_guard_booking,
    check_rate_limit, record_message,
)
from ai_chatbot.vacation_service import (
    VacationService,
    vacation_guard_booking,
    vacation_guard_agent,
)
from ai_chatbot.core.message_processor import (
    MessageResult,
    process_incoming_message,
    process_rag_query,
    should_handoff_to_human,
)
from ai_chatbot.bot.calendar_keyboard import (
    build_calendar_keyboard,
    parse_calendar_callback,
    CB_CALENDAR_SELECT,
    CB_CALENDAR_PREV,
    CB_CALENDAR_NEXT,
    CB_CALENDAR_IGNORE,
)

logger = logging.getLogger(__name__)

# Conversation states for appointment booking
BOOKING_SERVICE, BOOKING_DATE, BOOKING_TIME, BOOKING_CONFIRM = range(4)

# ─── סיווג סיבת-דחייה ⇒ לאיזה שלב ב-flow לחזור (מקומי לטלגרם) ────────────────
# אחרי דחיית תור נשארים ב-flow בשלב המתאים כדי שהבוט יאזין לתיקון (שעה/תאריך),
# במקום לנקות user_data ולסיים ⇒ הלקוח נופל ל-handler הכללי. מיפוי מקומי (מקביל
# ל-messaging/whatsapp_booking.py, בלי איחוד — כל ערוץ עצמאי). drift-guard בטסטים.
# דחיות שהלקוח מתקן ע"י בחירת *שעה* אחרת (אותו תאריך תקין):
_REJECT_RETRY_TIME = frozenset({
    "calendar_busy", "slot_already_taken", "before_business_hours",
    "exceeds_closing_time", "slot_in_past", "slot_crosses_midnight",
})
# דחיות שהלקוח מתקן ע"י בחירת *תאריך* אחר (היום עצמו סגור/רחוק):
_REJECT_RETRY_DATE = frozenset({
    "closed_regular", "closed_holiday", "closed_special_day", "slot_too_far_ahead",
})


def _rejection_recovery_step(reason: str) -> str:
    """מחזיר 'time' / 'date' / 'terminal' — לאיזה שלב ב-flow לחזור אחרי דחייה.
    terminal (חופשה / שגיאה פנימית / לא-ידוע) ⇒ הקורא ינקה state ויציע להתחיל מחדש.
    """
    if reason in _REJECT_RETRY_TIME:
        return "time"
    if reason in _REJECT_RETRY_DATE:
        return "date"
    return "terminal"


# Button label constants — used for routing and filtering
BUTTON_PRICE_LIST = "📋 מחירון"
BUTTON_BOOKING = "📅 בקשת תור"
BUTTON_LOCATION = "📍 שליחת מיקום"
BUTTON_SAVE_CONTACT = "📇 שמור איש קשר"
BUTTON_AGENT = "👤 דברו עם נציג"
BUTTON_REFERRAL = "🎁 קוד הפניה"
ALL_BUTTON_TEXTS = [BUTTON_PRICE_LIST, BUTTON_BOOKING, BUTTON_LOCATION, BUTTON_SAVE_CONTACT, BUTTON_AGENT, BUTTON_REFERRAL]


@asynccontextmanager
async def _typing_indicator(bot, chat_id: int, interval: float = 4.0):
    """שולח אינדיקציית הקלדה בלולאה כל interval שניות עד שהבלוק מסתיים."""
    stop = asyncio.Event()

    async def _loop():
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logger.debug("typing indicator failed for chat %s: %s", chat_id, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break  # stop נקבע — יוצאים
            except asyncio.TimeoutError:
                pass  # עוד סיבוב

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        await task


async def _generate_answer_async(*args, **kwargs):
    return await asyncio.to_thread(generate_answer, *args, **kwargs)


async def _summarize_safe(user_id: str):
    """Run summarization in background without blocking the caller."""
    try:
        await asyncio.to_thread(maybe_summarize, user_id)
    except Exception as e:
        logger.error("Background summarization failed for user %s: %s", user_id, e)


async def _analyze_lead_safe(user_id: str, *, username: str = "", channel: str = "telegram"):
    """ניתוח ליד ברקע — לא חוסם את ה-handler."""
    try:
        from ai_chatbot.followup_service import analyze_lead
        await asyncio.to_thread(analyze_lead, user_id, username=username, channel=channel)
    except Exception as e:
        logger.error("Background lead analysis failed for user %s: %s", user_id, e)


async def _reply_html_safe(message, text: str, **kwargs):
    """שליחת הודעה עם HTML formatting, עם fallback לטקסט רגיל אם טלגרם דוחה."""
    if message is None:
        return None
    try:
        return await message.reply_text(text, parse_mode="HTML", **kwargs)
    except BadRequest:
        return await message.reply_text(text, **kwargs)


async def _send_html_safe(bot, chat_id: int, text: str, **kwargs):
    """שליחת הודעה עם HTML ל-chat_id, עם fallback לטקסט רגיל."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", **kwargs)
    except BadRequest:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


def _get_main_keyboard(update: Update | None = None) -> ReplyKeyboardMarkup:
    """Create the main menu keyboard with action buttons.

    אם יש update עם user_id שיש לו קוד הפניה — מוסיף כפתור שחזור קוד.
    """
    # כפתור בקשת תור מוצג רק כשקביעת תורים מופעלת לעסק
    booking_on = True
    try:
        booking_on = db.is_booking_enabled()
    except Exception:
        pass  # ברירת מחדל בטוחה — מציגים את הכפתור
    first_row = [KeyboardButton(BUTTON_PRICE_LIST)]
    if booking_on:
        first_row.append(KeyboardButton(BUTTON_BOOKING))
    keyboard = [
        first_row,
        [KeyboardButton(BUTTON_LOCATION), KeyboardButton(BUTTON_SAVE_CONTACT)],
        [KeyboardButton(BUTTON_AGENT)],
    ]
    try:
        if update and update.effective_user:
            user_id = str(update.effective_user.id)
            # כפתור הפניה מוצג רק אם הפיצ'ר מופעל ויש קוד
            if db.get_bot_settings().get("referral_enabled", 0) and db.get_user_referral_code(user_id):
                keyboard.append([KeyboardButton(BUTTON_REFERRAL)])
    except Exception:
        pass  # לא חוסם — המקלדת תוצג בלי הכפתור
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def _make_consent_guard(*, booking: bool):
    """Factory לבניית consent_guard / consent_guard_booking.

    booking=True: חוסם handlers שהם entry points של ConversationHandler
    (כמו booking_start). מחזיר ConversationHandler.END בעת חסימה כדי
    לסיים שיחה במקום לסמן "handler לא הותאם" (None).
    booking=False: handler רגיל. מחזיר None בעת חסימה.
    """
    from functools import wraps
    from telegram.ext import ConversationHandler

    block_return = ConversationHandler.END if booking else None

    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            try:
                user_id, _, _ = _get_user_info(update)
            except Exception:
                # אין user_id — מאפשרים את ה-handler להתמודד עם זה כרגיל
                return await func(update, context, *args, **kwargs)
            if not db.has_consent(user_id):
                await _send_consent_screen(update, context)
                return block_return
            return await func(update, context, *args, **kwargs)
        return wrapper

    return decorator


# Guard לתיקון 13 — חוסם handlers שמעבדים PII עד שהמשתמש נתן הסכמה.
# יש להחיל על handlers שמייצרים אינטראקציה עם PII (talk_to_agent, referral).
# start_command ו-message_handler מטפלים בזה ידנית כדי גם לקבל deep-link
# arg ולעבד אותו לפני החסימה.
consent_guard = _make_consent_guard(booking=False)

# גרסה ל-ConversationHandler entry points (כמו booking_start) שמחזירה
# ConversationHandler.END במקום None בעת חסימה. ההבדל קריטי: ב-PTB,
# החזרת None מ-entry point נחשבת כ"handler לא הותאם" ועלולה לגרום ל-
# update לא להיצרך כראוי, בעוד ש-END מסיים את השיחה הקצרה כמתוכנן
# (סימטרי ל-block_guard_booking, rate_limit_guard_booking, וכו').
consent_guard_booking = _make_consent_guard(booking=True)


def _get_user_info(update: Update) -> tuple[str, str, str]:
    """Extract user ID, display name, and Telegram username (without @)."""
    user = update.effective_user
    user_id = str(user.id)
    display_name = user.full_name or (f"@{user.username}" if user.username else f"User {user.id}")
    telegram_username = user.username or ""
    return user_id, display_name, telegram_username


def _tg_handle(telegram_username: str) -> str:
    return f"@{telegram_username}" if telegram_username else ""


import re as _re

# תבנית לזיהוי שעה מספרית — "8:30", "08:30", "14:00", "14:00:00"
_TIME_NUMERIC_RE = _re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")


def _normalize_time_for_gcal(raw_time: str) -> str | None:
    """נרמול שעה ל-HH:MM לצורך השוואה מול slots של Google Calendar.

    מחזיר None אם הקלט הוא טקסט חופשי (לא מספרי) — במקרה כזה מדלגים על הבדיקה.
    "8:30" → "08:30", "14:00:00" → "14:00", "אחר הצהריים" → None.
    """
    m = _TIME_NUMERIC_RE.match(raw_time.strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _format_date_display(iso_date: str) -> str:
    """המרת תאריך מפורמט ISO (YYYY-MM-DD) לפורמט תצוגה DD/MM/YYYY."""
    try:
        parts = iso_date.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (IndexError, AttributeError):
        return iso_date


# _should_handoff_to_human הועבר ל-core/message_processor.py — should_handoff_to_human
_should_handoff_to_human = should_handoff_to_human


# ─── Follow-up Questions (שאלות המשך) ────────────────────────────────────

# קידומת callback_data לשאלות המשך — הטקסט מאוחסן ב-context.bot_data
FOLLOW_UP_CB_PREFIX = "followup_"

# זמן תפוגה (בשניות) לכפתורי שאלות המשך שלא נלחצו — מנקים כדי למנוע דליפת זיכרון
_FOLLOW_UP_TTL_SECONDS = 3600  # שעה


def _cleanup_stale_follow_ups(bot_data: dict) -> None:
    """ניקוי רשומות שאלות המשך ישנות מ-bot_data כדי למנוע צמיחה בלתי מוגבלת.

    שלב 1: אוסף מפתחות ישנים לרשימה נפרדת (stale_keys).
    שלב 2: מוחק מ-dict — בטוח כי לא עוברים על ה-dict בזמן שינוי.
    """
    now = int(time.time())
    stale_keys = []
    for key in bot_data:
        if not key.startswith(FOLLOW_UP_CB_PREFIX):
            continue
        # חילוץ ה-timestamp מהמפתח: followup_{user_id}_{timestamp}_{index}
        parts = key.split("_")
        try:
            ts = int(parts[-2])
            if now - ts > _FOLLOW_UP_TTL_SECONDS:
                stale_keys.append(key)
        except (ValueError, IndexError):
            continue
    for key in stale_keys:
        bot_data.pop(key, None)


def _build_follow_up_keyboard(questions: list[str], bot_data: dict, user_id: str) -> InlineKeyboardMarkup | None:
    """בניית מקלדת inline עם שאלות המשך.

    שומר את טקסט השאלה ב-bot_data כדי לאפשר שליפה ב-callback
    (callback_data מוגבל ל-64 בתים בטלגרם).
    המפתח כולל user_id למניעת התנגשויות בין משתמשים בו-זמניים.
    """
    if not questions:
        return None

    # ניקוי רשומות ישנות שלא נלחצו
    _cleanup_stale_follow_ups(bot_data)

    buttons = []
    now = int(time.time())
    for i, q in enumerate(questions):
        # מזהה ייחודי לכל שאלה — כולל user_id למניעת התנגשויות
        cb_id = f"{FOLLOW_UP_CB_PREFIX}{user_id}_{now}_{i}"
        bot_data[cb_id] = q
        buttons.append([InlineKeyboardButton(f"💡 {q}", callback_data=cb_id)])
    return InlineKeyboardMarkup(buttons)


async def _notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str, max_retries: int = 3) -> bool:
    """שליחת התראה לבעל העסק עם retry ו-exponential backoff.

    מנסה עד max_retries פעמים במקרה של שגיאות רשת זמניות (TimedOut, NetworkError).
    שגיאות אחרות (למשל chat_id לא תקין) גורמות לכשלון מיידי — אין טעם לנסות שוב.
    """
    if not TELEGRAM_OWNER_CHAT_ID:
        return False

    for attempt in range(max_retries):
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_OWNER_CHAT_ID, text=text,
            )
            return True
        except (TimedOut, NetworkError) as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                logger.warning("Owner notification retry %d/%d: %s", attempt + 1, max_retries, e)
            else:
                logger.error("Owner notification failed after %d attempts: %s", max_retries, e)
        except Exception as e:
            logger.error("Owner notification unexpected error: %s", e)
            return False
    return False


async def _create_request_and_notify_owner(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: str,
    display_name: str,
    telegram_username: str,
    message: str,
) -> int:
    request_id = db.create_agent_request(
        user_id,
        display_name,
        message=message,
        telegram_username=telegram_username,
    )

    handle = _tg_handle(telegram_username) or "(ללא שם משתמש)"
    now_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
    panel_link = f"\n\n🔗 {ADMIN_URL}/requests" if ADMIN_URL else ""
    notification = (
        f"🔔 בקשת נציג #{request_id}\n\n"
        f"לקוח: {display_name}\n"
        f"יוזר: {handle}\n"
        f"זמן: {now_il}\n\n"
        f"{message}"
        f"{panel_link}"
    )
    await _notify_owner(context, notification)

    return request_id


async def _handoff_to_human(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: str,
    display_name: str,
    telegram_username: str,
    reason: str,
    *,
    chat_id: int | None = None,
) -> None:
    await _create_request_and_notify_owner(
        context,
        user_id=user_id,
        display_name=display_name,
        telegram_username=telegram_username,
        message=reason,
    )

    response_text = FALLBACK_RESPONSE
    db.save_message(user_id, display_name, "assistant", response_text)
    # callback queries לא מספקים update.message — שליחה ישירה לצ'אט
    if chat_id is not None and update.message is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=response_text,
            reply_markup=_get_main_keyboard(update),
        )
    else:
        await update.message.reply_text(
            response_text,
            reply_markup=_get_main_keyboard(update),
        )


# ─── /start Command ──────────────────────────────────────────────────────────

@block_guard
@rate_limit_guard
@live_chat_guard
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command — send welcome message with menu.

    אם ה-deep link מכיל פרמטר ref_XXX — נרשום את ההפניה.
    """
    user_id, display_name, _telegram_username = _get_user_info(update)

    # תיקון 13 — לפני שכותבים PII, מוודאים שיש הסכמה.
    # קוד הפניה מה-deep link (REF_XXXXXXXX) נשמר בלבד ב-context.user_data
    # ויעובד אחרי לחיצה על "אני מסכים" ב-consent_callback. כך:
    # 1) לא מאבדים את הקוד אם המשתמש פותח את מסך ההסכמה ויחזור דרך callback
    # 2) ההפניה לא נכתבת ל-DB עד שהמשתמש הסכים
    pending_ref: str | None = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("REF_"):
            pending_ref = arg

    has_consent_now = db.has_consent(user_id)
    if not has_consent_now:
        # שומרים את ההפניה ל-callback של ההסכמה. לא נכתב כלום ל-DB.
        if pending_ref:
            context.user_data["pending_referral_code"] = pending_ref
        await _send_consent_screen(update, context)
        return

    # יש הסכמה — מותר לכתוב PII: יוצרים/מעדכנים את שורת המשתמש,
    # רושמים אותו לשידורים, ומבצעים את ההפניה אם הגיע עם REF_.
    db.upsert_user(user_id, display_name, channel="telegram")
    db.ensure_user_subscribed(user_id)

    referral_registered = False
    if pending_ref:
        referral_registered = db.register_referral(pending_ref, user_id)
        if referral_registered:
            logger.info("Referral registered: user %s via code %s", user_id, pending_ref)

    # בדיקה אם לקוח חוזר (יש לו תורים שאושרו/בוצעו בעבר)
    returning = db.is_returning_customer(user_id)

    # הודעת פתיחה משפטית (implied consent) — פעם אחת לפונה חדש. רק כשמסך
    # ההסכמה המפורש כבוי (ברירת המחדל); כשהוא דלוק — המסך כבר טיפל בהסכמה.
    show_disclaimer = (not CONSENT_SCREEN_ENABLED) and not db.disclaimer_sent(user_id)

    # _html.escape לערכי קונפיג בודדים; sanitize_telegram_html לפלט LLM שלם
    if show_disclaimer:
        welcome_text = build_intro_disclaimer(html_link=True)
    elif returning:
        welcome_text = (
            f"😊 שמחים לראות אותך שוב ב-<b>{_html.escape(get_business_config().name)}</b>!\n\n"
            f"איך אפשר לעזור הפעם?\n"
            f"פשוט כתבו את השאלה שלכם או השתמשו בכפתורים למטה! 👇"
        )
    else:
        welcome_text = (
            f"👋 ברוכים הבאים ל-<b>{_html.escape(get_business_config().name)}</b>!\n\n"
            f"אני העוזר הווירטואלי שלכם. אני יכול לעזור לכם עם:\n"
            f"• מידע על השירותים והמחירים שלנו\n"
            f"• בקשת תורים\n"
            f"• מענה על שאלות\n"
            f"• חיבור לבעל העסק\n\n"
            f"פשוט כתבו את השאלה שלכם או השתמשו בכפתורים למטה! 👇"
        )

    if referral_registered:
        # טקסט הנחה דינמי לפי הגדרות הבוט — helpers משותפים עם referral_service
        from ai_chatbot.referral_service import format_referral_discount, format_referral_period
        _ref_settings = db.get_bot_settings()
        _d_str = format_referral_discount(_ref_settings.get("referral_discount", 10.0))
        _p_str = format_referral_period(_ref_settings.get("referral_validity_days", 60))
        welcome_text += (
            "\n\n🎁 <b>הגעתם דרך הפניה!</b> "
            "לאחר שתקבעו ותשלימו את התור הראשון שלכם — "
            f"גם אתם וגם החבר/ה שהפנה אתכם תקבלו <b>{_d_str} הנחה {_p_str}!</b>"
        )

    await update.message.reply_text(
        welcome_text,
        parse_mode="HTML",
        reply_markup=_get_main_keyboard(update)
    )
    # סימון שנשלח (אחרי upsert_user לעיל — השורה קיימת) כדי לא לחזור עליו.
    if show_disclaimer:
        db.mark_disclaimer_sent(user_id)

    # Log the interaction
    db.save_message(user_id, display_name, "user", "/start")
    db.save_message(user_id, display_name, "assistant", "[Welcome message sent]")


# ─── /stop Command (ביטול הרשמה לשידורים) ────────────────────────────────────

@block_guard
@rate_limit_guard
@live_chat_guard
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בפקודת /stop — ביטול הרשמה לקבלת הודעות שידור."""
    user_id, display_name, _ = _get_user_info(update)

    if not db.is_user_subscribed(user_id):
        await update.message.reply_text(
            "ההרשמה שלכם כבר בוטלה. לא תקבלו הודעות שידור.\n"
            "כדי להירשם מחדש, שלחו /subscribe",
            reply_markup=_get_main_keyboard(update),
        )
        return

    db.unsubscribe_user(user_id)
    db.save_message(user_id, display_name, "user", "/stop")
    db.save_message(user_id, display_name, "assistant", "[ביטול הרשמה לשידורים]")

    await update.message.reply_text(
        "✅ ההרשמה שלכם לקבלת הודעות שידור בוטלה.\n"
        "תמשיכו לקבל תשובות רגילות מהבוט.\n\n"
        "כדי להירשם מחדש, שלחו /subscribe",
        reply_markup=_get_main_keyboard(update),
    )


# ─── /subscribe Command (הרשמה מחדש לשידורים) ────────────────────────────────

# consent_guard נדרש כדי שמשתמש שעשה /forget (וביטל הסכמה) לא יוכל לרשום
# PII דרך /subscribe. סדר: live_chat_guard *לפני* consent_guard — אחרת
# משתמש בשיחה חיה היה מקבל מסך הסכמה (אחרי bump של CURRENT_CONSENT_VERSION)
# למרות שהבוט אמור להיות שקט בשיחה חיה.
@block_guard
@rate_limit_guard
@live_chat_guard
@consent_guard
async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בפקודת /subscribe — הרשמה מחדש לקבלת שידורים."""
    user_id, display_name, _ = _get_user_info(update)

    if db.is_user_subscribed(user_id):
        await update.message.reply_text(
            "אתם כבר רשומים לקבלת הודעות שידור.",
            reply_markup=_get_main_keyboard(update),
        )
        return

    db.resubscribe_user(user_id)
    db.save_message(user_id, display_name, "user", "/subscribe")
    db.save_message(user_id, display_name, "assistant", "[הרשמה מחדש לשידורים]")

    await update.message.reply_text(
        "✅ נרשמתם מחדש לקבלת הודעות שידור!\n"
        "כדי לבטל בכל עת, שלחו /stop",
        reply_markup=_get_main_keyboard(update),
    )


# ─── זכויות נושאי מידע (תיקון 13 לחוק הגנת הפרטיות) ──────────────────────────

# קידומות callback_data לפעולות אישור/ביטול מחיקה — חייבות להיות ייחודיות
# כדי לא להתנגש עם calendar callbacks או follow-up callbacks קיימים.
CB_FORGET_CONFIRM = "fgt:yes"
CB_FORGET_CANCEL = "fgt:no"
CB_CONSENT_ACCEPT = "csn:ok"
CB_CONSENT_DECLINE = "csn:no"


def _format_user_info(summary: dict) -> str:
    """פורמט קריא ב-HTML של סיכום מידע משתמש (לזכות עיון)."""
    if not summary.get("exists"):
        return (
            "🔍 <b>מידע השמור עליך</b>\n\n"
            "לא נמצא מידע שמור על המשתמש שלך במערכת.\n"
            "ייתכן שלא יצרת אינטראקציה משמעותית עם הבוט עדיין."
        )

    lines = ["🔍 <b>המידע השמור עליך במערכת</b>", ""]

    lines.append(f"• מזהה משתמש: <code>{_html.escape(str(summary.get('user_id', '')))}</code>")
    if summary.get("username"):
        lines.append(f"• שם משתמש: {_html.escape(str(summary['username']))}")
    if summary.get("channel"):
        lines.append(f"• ערוץ: {_html.escape(str(summary['channel']))}")
    if summary.get("first_seen_at"):
        lines.append(f"• פנייה ראשונה: {_html.escape(str(summary['first_seen_at']))}")
    if summary.get("last_active_at"):
        lines.append(f"• פעילות אחרונה: {_html.escape(str(summary['last_active_at']))}")
    lines.append(f"• מספר הודעות שנשלחו: {int(summary.get('message_count') or 0)}")

    consent_at = summary.get("consent_given_at") or "לא תועדה הסכמה (משתמש ישן)"
    lines.append(f"• הסכמה למדיניות: {_html.escape(str(consent_at))}")

    appts = summary.get("appointments") or {}
    total_appts = int(appts.get("total") or 0)
    lines.append("")
    lines.append(f"📅 <b>תורים:</b> {total_appts} בסך הכל")
    if appts.get("by_status"):
        for status, cnt in appts["by_status"].items():
            lines.append(f"  • {_html.escape(str(status))}: {int(cnt)}")

    lines.append("")
    lines.append(f"💬 הודעות בשיחות: {int(summary.get('conversations_total') or 0)}")
    lines.append(f"📋 סיכומי שיחה אוטומטיים: {int(summary.get('conversation_summaries_total') or 0)}")
    lines.append(f"💬 שיחות נציג: {int(summary.get('live_chats_total') or 0)}")
    lines.append(f"🆘 בקשות נציג: {int(summary.get('agent_requests_total') or 0)}")
    lines.append(f"❓ שאלות שלא נענו ונשמרו: {int(summary.get('unanswered_questions_total') or 0)}")

    # מעקבי לידים — תיקון 13 דורש שקיפות על קבלת החלטות אוטומטית של AI.
    # מציגים שהמערכת ביצעה ניתוח, בלי לחשוף את התוכן עצמו (חשיפת analysis_json
    # מלא ממתינה להחלטה משפטית).
    lf = summary.get("lead_followups") or {}
    lf_total = int(lf.get("total") or 0)
    if lf_total:
        lines.append(f"🤖 ניתוחי AI אוטומטיים על השיחה: {lf_total}")

    lines.append("")
    lines.append(f"📬 רשום לקבלת שידורים: {'כן' if summary.get('subscribed') else 'לא'}")
    bd_total = int(summary.get("broadcast_deliveries_total") or 0)
    if bd_total:
        lines.append(f"📨 הודעות שידור שנשלחו אליך: {bd_total}")

    # הפניות (אופציונלי — מציג רק אם רלוונטי)
    ref_as_referrer = int(summary.get("referrals_as_referrer_total") or 0)
    ref_as_referred = int(summary.get("referrals_as_referred_total") or 0)
    if summary.get("has_referral_code") or ref_as_referrer or ref_as_referred:
        lines.append("")
        lines.append("🔗 <b>הפניות:</b>")
        if summary.get("has_referral_code"):
            lines.append("  • יש לך קוד הפניה אישי")
        if ref_as_referrer:
            lines.append(f"  • הפניות שיצרת: {ref_as_referrer}")
        if ref_as_referred:
            lines.append(f"  • הצטרפת באמצעות הפניה: {ref_as_referred}")

    credits = summary.get("credits") or {}
    if int(credits.get("total") or 0):
        lines.append(f"💰 זיכויים (סה\"כ / פעילים): {int(credits.get('total') or 0)} / {int(credits.get('active') or 0)}")

    rp_total = int(summary.get("response_pages_total") or 0)
    if rp_total:
        lines.append(f"🔗 עמודי תשובה ארוכים שנשמרו: {rp_total}")

    identities = int(summary.get("identities_total") or 0)
    if identities > 1:
        lines.append(f"📱 זהויות ערוץ מקושרות: {identities}")

    # סטטוס חסימה — חשיפה חלקית לפי תיקון 13: למשתמש מותר לדעת שהוא
    # חסום וברמת קטגוריה, אבל לא את הסיבה הפנימית שבעל העסק כתב.
    if summary.get("blocked"):
        bs = summary.get("block_status") or {}
        category_he = {
            "abuse": "התנהגות לא הולמת",
            "spam": "ספאם",
            "repeated_no_show": "אי-הופעה חוזרת לתורים",
            "manual": "החלטה של בעל העסק",
        }.get(bs.get("block_category"), "לא מוגדר")
        lines.append("")
        lines.append(f"🚫 <b>סטטוס חסימה:</b> חשבונך מוגבל מלהשתמש בשירות.")
        lines.append(f"   קטגוריה: {_html.escape(category_he)}")
        if bs.get("blocked_month"):
            lines.append(f"   מועד: {_html.escape(bs['blocked_month'])}")
        if bs.get("appeal_contact_method"):
            lines.append(f"   לערעור: {_html.escape(bs['appeal_contact_method'])}")

    # הערות לקוח — חשיפה בעיון לפי המלצת היועץ (תיקון 13). ברירת מחדל
    # להציג את התוכן; אם בעל העסק סימן withhold_reason — מציינים שיש
    # הערה אבל לא חושפים אותה.
    note_text = summary.get("user_note_text") or ""
    if note_text:
        # escape כדי שטקסט מבעל העסק לא יעבור כ-HTML
        lines.append(f"📝 הערת בעל העסק: {_html.escape(note_text)}")
    elif summary.get("user_note_withheld"):
        lines.append("📝 קיימת הערה פנימית של בעל העסק (חסויה — אפשר לבקש פירוט במייל)")
    elif summary.get("has_user_note"):
        # backward compat — אם יש has_user_note אבל בלי שדות חדשים
        lines.append("📝 קיימת הערה של בעל העסק")

    lines.append("")
    lines.append(
        "ℹ️ למחיקת המידע השמור עליך — שלח/י <b>/forget</b>\n"
        "לפרטים נוספים — מדיניות הפרטיות שהוצגה לך בתחילת השימוש."
    )
    return "\n".join(lines)


@block_guard
@rate_limit_guard
@live_chat_guard
async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /myinfo — זכות עיון: מציגה למשתמש סיכום מידע השמור עליו.

    הערה: הפקודה לא מתועדת ב-conversations כדי לא לכתוב PII (user_id +
    display_name) של משתמש שיכול שלא נתן הסכמה. הפקודה היא מימוש זכות
    עיון, ולכן חייבת להיות זמינה גם בלי הסכמה — שזה אומר ללא יצירת
    רשומות חדשות שהמשתמש לא הסכים אליהן.
    """
    user_id, _display_name, _ = _get_user_info(update)

    # ייבוא ledger helpers בנפרד מ-try blocks — אחרת אם הייבוא נכשל,
    # ה-try השני יזרוק NameError במקום הסיבה האמיתית, וה-log יטעה.
    try:
        from utils.consent_ledger import (
            record_consent_event,
            EVENT_ACCESS_REQUESTED,
            EVENT_ACCESS_DELIVERED,
        )
        ledger_available = True
    except Exception:
        logger.error("myinfo_command: כשל ב-import של consent_ledger", exc_info=True)
        ledger_available = False

    # ledger: access_requested — תיעוד שהמשתמש מימש זכות עיון
    if ledger_available:
        try:
            record_consent_event(
                user_id=user_id, channel="telegram",
                event_type=EVENT_ACCESS_REQUESTED,
            )
        except Exception:
            logger.error("myinfo_command: כשל ב-access_requested ל-ledger", exc_info=True)

    summary = db.get_user_data_summary(user_id)
    text = _format_user_info(summary)
    await _reply_html_safe(update.message, text)

    # ledger: access_delivered — אחרי שההודעה נשלחה. אם השליחה נכשלה,
    # _reply_html_safe יזרוק חריגה והאירוע הזה לא ייכתב — וזה הרצוי
    # (לא מדווחים מסירה שלא קרתה).
    if ledger_available:
        try:
            record_consent_event(
                user_id=user_id, channel="telegram",
                event_type=EVENT_ACCESS_DELIVERED,
            )
        except Exception:
            logger.error("myinfo_command: כשל ב-access_delivered ל-ledger", exc_info=True)


@block_guard
@rate_limit_guard
@live_chat_guard
async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /forget — שלב ראשון: שאלת אישור עם כפתורים inline.
    המחיקה עצמה מתבצעת רק אחרי קליק על "כן, מחק/י" (callback).

    הערה: הפקודה לא מתועדת ב-conversations — לא הגיוני שבקשת מחיקה
    תיצור רשומה חדשה של PII. ה-callback של אישור מבצע את המחיקה ישירות.
    """
    user_id, _display_name, _ = _get_user_info(update)

    # קישור למדיניות הפרטיות המלאה — מאפשר למשתמש לראות את ההסבר על
    # ה-ledger המצומצם שנשמר אחרי המחיקה (ראה docs/legal/privacy.md
    # → "שמירת הוכחת הסכמה לאחר מחיקה").
    base = (ADMIN_URL or "").rstrip("/")
    privacy_link = f"{base}/legal/privacy" if base else ""
    privacy_line = (
        f'\n\n🔒 <a href="{privacy_link}">הסבר מלא על מה שנשאר ולמה</a>'
        if privacy_link else ""
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ כן, מחק/י את כל המידע", callback_data=CB_FORGET_CONFIRM),
        InlineKeyboardButton("❌ ביטול", callback_data=CB_FORGET_CANCEL),
    ]])
    # ניסוח לפי המלצת היועץ — שקיפות מלאה לגבי מה שנמחק ומה שנשאר
    # (ledger פסאודונימי). אופציה א של אופציות תיקון 13: לקשר היסטוריה
    # ולהיות שקופים, במקום אנונימיזציה אמיתית.
    text = (
        "⚠️ <b>מחיקת המידע שלך</b>\n\n"
        "ברגע שתאשר/י, נמחק את כל המידע שלך מהמערכת: "
        "השיחות, התורים, ההעדפות, פרטי הקשר, וכל מה שנגזר מהם.\n\n"
        "נשמור רשומה מצומצמת ומאובטחת אחת בלבד: עובדת מתן ההסכמה "
        "ועובדת ביטול ההסכמה. הרשומה הזו לא כוללת את שמך, את הטלפון "
        "שלך, את תוכן השיחות או כל מידע אישי אחר — אלא רק את עצם "
        "האירוע, התאריך, וזיהוי טכני סגור שאין לנו דרך לפתוח חזרה "
        "למידע אישי.\n\n"
        "מטרת הרשומה היא יחידה: היכולת להראות, אם נצטרך אי פעם, "
        "שפעלנו לפי הסכמתך ושכיבדנו את הביטול שלה. "
        "הרשומה נשמרת עד 5 שנים ואז נמחקת אוטומטית.\n\n"
        "אם בעתיד תפתח/י חשבון חדש מאותו טלפון או מאותו מזהה, "
        "נתייחס לזה כאל הסכמה חדשה ונפרדת.\n\n"
        "פעולה זו <b>אינה ניתנת לביטול</b>. האם להמשיך?"
        f"{privacy_line}"
    )
    await _reply_html_safe(update.message, text)
    await update.message.reply_text("בחר/י:", reply_markup=keyboard)


@live_chat_guard
async def forget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback של מחיקת מידע — מתבצע אחרי לחיצה על אישור/ביטול בכפתור."""
    query = update.callback_query
    await query.answer()
    user_id, display_name, _ = _get_user_info(update)

    if query.data == CB_FORGET_CANCEL:
        try:
            await query.edit_message_text("בקשת המחיקה בוטלה. המידע שלך נשאר במערכת.")
        except Exception:
            logger.error("forget_callback: שגיאה בעדכון הודעת ביטול", exc_info=True)
        return

    if query.data != CB_FORGET_CONFIRM:
        return

    counts = db.delete_user_data(user_id)

    # idempotency: אם בקשה קודמת עוד בעיבוד (callback כפול / לחיצה כפולה)
    if counts.get("already_in_progress"):
        try:
            await query.edit_message_text(
                "⏳ בקשת המחיקה שלך כבר בטיפול. תקבל/י אישור בעוד רגע.",
            )
        except Exception:
            logger.error("forget_callback: שגיאה בהודעת already_in_progress", exc_info=True)
        return

    # ביטול הסכמה גם — אבל delete_user_data מחק את כל שורת users, אז ההסכמה נופלת ממילא.
    # _result_total_count מתעלם ממפתחות מטא (__failed_tables__ וכד').
    total = db._result_total_count(counts)
    status = db.deletion_status(counts)
    try:
        if status == "failed":
            # כל ה-DELETEs נכשלו — אסור לומר למשתמש "המידע נמחק" או
            # "אין מידע", זה false confirmation והפרת ציות. ה-ledger
            # כבר תיעד deletion_failed; הנה אנחנו שקופים מול המשתמש.
            await query.edit_message_text(
                "⚠️ אירעה תקלה זמנית במחיקת המידע. הבקשה שלך נרשמה "
                "אצלנו ותטופל תוך 24 שעות.\n\n"
                "אם לא קיבלת אישור עד אז — שלח/י <b>/forget</b> שוב, "
                "או פנה/י לבעל העסק.",
                parse_mode="HTML",
            )
        elif status == "partial":
            await query.edit_message_text(
                f"✅ המידע שלך נמחק ברובו ({total} רשומות), "
                "אבל היו מספר טבלאות שלא נמחקו עקב תקלה זמנית. "
                "צוות התמיכה יוודא שזה יושלם תוך 24 שעות.",
            )
        elif total == 0:
            # DB ריק לגיטימית — לא היה מידע מלכתחילה. (status == "full"
            # עם counts ריק — לא נכשל.)
            await query.edit_message_text(
                "✅ אין מידע השמור עליך במערכת.\n\n"
                "אם תיצור/י שיחה חדשה עם הבוט — נצטרך לבקש שוב הסכמה למדיניות הפרטיות.",
            )
        else:
            await query.edit_message_text(
                f"✅ המידע שלך נמחק מהמערכת ({total} רשומות).\n\n"
                "אם תיצור/י שיחה חדשה עם הבוט — נצטרך לבקש שוב הסכמה למדיניות הפרטיות.",
            )
    except Exception:
        logger.error("forget_callback: שגיאה בעדכון הודעת אישור", exc_info=True)
    logger.info(
        "forget_callback: deletion %s for user=%s, total=%d",
        status, user_id, total,
    )


# ─── מסך הסכמה (תיקון 13) ────────────────────────────────────────────────────

def _build_consent_keyboard() -> InlineKeyboardMarkup:
    """שני כפתורים — אישור (כולל אימות גיל) / סירוב.

    טקסט הכפתור כולל "ואני בן/בת 18+" כדי שהאישור יהיה אקטיבי לפי
    המלצת היועץ. אין צ'קבוקס נפרד כי Telegram inline keyboards לא
    תומכים ב-toggle נטיב; הצמדת אימות הגיל לטקסט הכפתור עצמו היא
    הפתרון הברור ביותר ליצירת "הסכמה מודעת ומפורשת".
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ אני מסכים/ה ובן/בת 18+",
            callback_data=CB_CONSENT_ACCEPT,
        ),
        InlineKeyboardButton("❌ לא מסכים/ה", callback_data=CB_CONSENT_DECLINE),
    ]])


def _consent_message_text() -> str:
    """טקסט מסך הסכמה ראשוני. מסביר במפורש את עיבוד ה-AI ואת ההעברה
    לחו"ל לפני האישור — הסכמה מודעת לפי תיקון 13.

    שינויי v2: אזכור AI ו-OpenAI/Google מועלה לסעיף ייעודי מודגש (לא
    bullet אחד מכמה), אישור גיל מועבר לטקסט הכפתור, מוזכר במפורש שזה
    תנאי לשימוש (לא אופציונלי).
    """
    from config import get_business_config, ADMIN_URL
    BUSINESS_NAME = get_business_config().name
    base = (ADMIN_URL or "").rstrip("/")
    terms_link = f"{base}/legal/terms" if base else ""
    privacy_link = f"{base}/legal/privacy" if base else ""

    lines = [
        f"👋 ברוך/ה הבא/ה ל-<b>{_html.escape(BUSINESS_NAME or 'הבוט')}</b>!",
        "",
        "לפני שמתחילים — חשוב שתדע/י איך השירות עובד:",
        "",
        "🤖 <b>עיבוד באמצעות בינה מלאכותית</b>",
        "ההודעות שלך מועברות ומעובדות אצל ספקי AI חיצוניים "
        "(OpenAI / Google) הממוקמים בארה\"ב. בלי זה, השירות לא יוכל "
        "לעבוד.",
        "",
        "🔞 <b>גיל מינימלי</b>",
        "השירות מיועד לבני 18 ומעלה בלבד. לחיצה על \"אני מסכים/ה\" "
        "מהווה גם אישור שאת/ה בגיל זה.",
        "",
        "📋 <b>מה עוד כדאי לדעת:</b>",
        "• המידע שלך נשמר בהתאם למפורט במדיניות הפרטיות (12 חודשים לשיחות)",
        "• אפשר לראות את המידע השמור — <b>/myinfo</b>",
        "• אפשר לבקש מחיקה — <b>/forget</b>",
        "• אפשר להפסיק לקבל הודעות שיווקיות — <b>/stop</b>",
    ]
    if terms_link and privacy_link:
        lines += [
            "",
            f'📄 <a href="{terms_link}">תנאי שימוש מלאים</a>',
            f'🔒 <a href="{privacy_link}">מדיניות פרטיות מלאה</a>',
        ]
    lines += [
        "",
        "האם תרצה/י להמשיך?",
    ]
    return "\n".join(lines)


async def _send_consent_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """שליחת מסך ההסכמה ללקוח — נקרא מ-start_command או מ-message_handler.

    קריטי שהמסך *תמיד* יישלח: אם הוא נכשל, has_consent ימשיך להחזיר False
    ובכל פנייה הבאה נחזור לכאן ושוב נכשל — המשתמש תקוע ללא מוצא.
    מנגנון fallback: אם HTML נדחה ע"י Telegram (BadRequest), שולחים שוב כטקסט רגיל.
    """
    text = _consent_message_text()
    keyboard = _build_consent_keyboard()
    msg = update.effective_message
    if msg is None:
        return
    try:
        await msg.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return
    except BadRequest:
        # HTML נדחה — ננסה ללא parse_mode (גם בלי disable_web_page_preview, שלא
        # יהיה בעיה משלים). הכפתורים עדיין מוצגים — וזה החלק החשוב.
        logger.warning("_send_consent_screen: HTML נדחה, נופל לטקסט רגיל")
        try:
            await msg.reply_text(text, reply_markup=keyboard)
            return
        except Exception:
            logger.error(
                "_send_consent_screen: גם fallback לטקסט רגיל נכשל",
                exc_info=True,
            )
    except Exception:
        logger.error("_send_consent_screen: שגיאה בשליחת מסך הסכמה", exc_info=True)


@live_chat_guard
async def consent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback למסך ההסכמה — מסכים/ה => record_consent + הודעת ברוכים. דחייה => הודעת חסימה רכה."""
    query = update.callback_query
    await query.answer()
    user_id, display_name, _ = _get_user_info(update)
    channel = "telegram"

    if query.data == CB_CONSENT_DECLINE:
        try:
            await query.edit_message_text(
                "תודה. ללא הסכמה אין באפשרות הבוט לתת שירות.\n"
                "אם תשנה/י את דעתך — אפשר תמיד לחזור עם /start.",
            )
        except Exception:
            logger.error("consent_callback: שגיאה בעדכון הודעת דחייה", exc_info=True)
        return

    if query.data != CB_CONSENT_ACCEPT:
        return

    # רושמים את ההסכמה (גם יוצר את שורת המשתמש ב-users אם לא הייתה),
    # רושמים לשידורים (אופט-אין נדחה עד אחרי הסכמה),
    # ואם הייתה הפניה ממתינה מ-deep link — מבצעים אותה כעת.
    db.record_consent(user_id, username=display_name, channel=channel)
    db.ensure_user_subscribed(user_id)

    pending_ref = context.user_data.pop("pending_referral_code", None)
    if pending_ref:
        try:
            registered = db.register_referral(pending_ref, user_id)
            if registered:
                logger.info(
                    "Referral registered after consent: user %s via code %s",
                    user_id, pending_ref,
                )
        except Exception:
            logger.error(
                "consent_callback: שגיאה ברישום הפניה ממתינה (%s)",
                pending_ref, exc_info=True,
            )
    try:
        await query.edit_message_text(
            "✅ תודה! ההסכמה נשמרה.\n\n"
            "אפשר להתחיל לשלוח שאלות, לבקש תור, או ללחוץ על אחד הכפתורים בתפריט.",
        )
    except Exception:
        logger.error("consent_callback: שגיאה בעדכון הודעת אישור", exc_info=True)


# ─── /help Command ───────────────────────────────────────────────────────────

@block_guard
@rate_limit_guard
@live_chat_guard
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    user_id, display_name, _ = _get_user_info(update)

    help_text = (
        "🤖 <b>איך להשתמש בבוט:</b>\n\n"
        "• פשוט כתבו כל שאלה ואעשה כמיטב יכולתי לענות!\n"
        "• לחצו על <b>📋 מחירון</b> כדי לראות את השירותים והמחירים\n"
        "• לחצו על <b>📅 בקשת תור</b> כדי לבקש תור\n"
        "• לחצו על <b>📍 שליחת מיקום</b> כדי לקבל את הכתובת והמפה שלנו\n"
        "• לחצו על <b>📇 שמור איש קשר</b> כדי לשמור אותנו באנשי הקשר\n"
        "• לחצו על <b>👤 דברו עם נציג</b> כדי לדבר עם בעל העסק\n\n"
        "אפשר גם לשאול שאלות כמו:\n"
        '  <i>"מה שעות הפתיחה שלכם?"</i>\n'
        '  <i>"האם אתם מציעים צביעת שיער?"</i>\n'
        '  <i>"מה מדיניות הביטולים שלכם?"</i>'
    )

    await update.message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=_get_main_keyboard(update)
    )


# ─── Price List Button ───────────────────────────────────────────────────────

async def _price_list_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """לוגיקה פנימית של מחירון — ללא דקורטורים."""
    user_id, display_name, telegram_username = _get_user_info(update)

    await update.message.reply_text("📋 תנו לי רגע לחפש את המחירון שלנו...")

    await _handle_rag_query(
        update, context,
        user_id=user_id,
        display_name=display_name,
        telegram_username=telegram_username,
        user_message="📋 מחירון",
        query="הצג לי את המחירון המלא עם כל השירותים והמחירים",
        handoff_reason="הלקוח ביקש מחירון, אך אין מידע זמין במאגר.",
    )


@block_guard
@rate_limit_guard
@live_chat_guard
async def price_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Price List button — retrieve pricing info from KB."""
    return await _price_list_core(update, context)

# גרסה ללא rate_limit — לניתוב פנימי
@live_chat_guard
async def _price_list_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _price_list_core(update, context)


# ─── Send Location Button ────────────────────────────────────────────────────

async def _location_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """לוגיקה פנימית של מיקום — ללא דקורטורים."""
    user_id, display_name, telegram_username = _get_user_info(update)

    await _handle_rag_query(
        update, context,
        user_id=user_id,
        display_name=display_name,
        telegram_username=telegram_username,
        user_message="📍 מיקום",
        query="מה הכתובת והמיקום של העסק? איך מגיעים?",
        handoff_reason="הלקוח ביקש לקבל מיקום/כתובת, אך אין מידע זמין במאגר.",
    )


@block_guard
@rate_limit_guard
@live_chat_guard
async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Send Location button — send business location info."""
    return await _location_core(update, context)

# גרסה ללא rate_limit — לניתוב פנימי
@live_chat_guard
async def _location_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _location_core(update, context)


# ─── Save Contact (vCard) Button ─────────────────────────────────────────────

def _vcard_escape(value: str) -> str:
    """Escape לתווים מיוחדים ב-vCard לפי RFC 6350 — backslash, נקודה-פסיק,
    פסיק, וירידת-שורה (‏\\n בתוך ערך — למשל שעות פעילות רב-שורתיות ב-NOTE)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _generate_vcard_text() -> str:
    """יצירת טקסט vCard מפרטי העסק שבקונפיגורציה."""
    # סיכום שעות ל-NOTE — שם יום בעברית, יום בשורה נפרדת (כולל ימים סגורים),
    # כדי שבכרטיס איש הקשר הלוח יופיע מסודר ולא כשורה אחת צפופה.
    hours_lines = []
    for h in db.get_all_business_hours():
        day = DAY_NAMES_HE.get(h["day_of_week"], "?")
        if h["is_closed"]:
            hours_lines.append(f"{day}: סגור")
        else:
            hours_lines.append(f"{day}: {h['open_time']}-{h['close_time']}")
    hours_summary = "שעות פעילות:\n" + "\n".join(hours_lines) if hours_lines else ""

    _biz = get_business_config()
    escaped_name = _vcard_escape(_biz.name)

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{escaped_name}",
        f"N:{escaped_name};;;;",
        f"ORG:{escaped_name}",
    ]
    if _biz.phone:
        lines.append(f"TEL;TYPE=WORK,VOICE:{_biz.phone}")
    if _biz.address:
        lines.append(f"ADR;TYPE=WORK:;;{_vcard_escape(_biz.address)};;;;")
    if _biz.website:
        lines.append(f"URL:{_biz.website}")
    if hours_summary:
        lines.append(f"NOTE:{_vcard_escape(hours_summary)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines)


async def _save_contact_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """לוגיקה פנימית של שמירת איש קשר — ללא דקורטורים."""
    user_id, display_name, _ = _get_user_info(update)

    vcard_content = _generate_vcard_text()
    vcard_file = BytesIO(vcard_content.encode("utf-8"))
    vcard_file.name = f"{get_business_config().name}.vcf"

    db.save_message(user_id, display_name, "user", "📇 שמירת איש קשר")

    await update.message.reply_document(
        document=vcard_file,
        caption="הנה כרטיס הביקור שלנו! לחצו עליו ושמרו באנשי הקשר. 👇",
        reply_markup=_get_main_keyboard(update),
    )

    db.save_message(user_id, display_name, "assistant", "[כרטיס ביקור נשלח]")


@block_guard
@rate_limit_guard
@live_chat_guard
async def save_contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """שליחת כרטיס ביקור דיגיטלי (vCard) כקובץ .vcf."""
    return await _save_contact_core(update, context)

# גרסה ללא rate_limit — לניתוב פנימי
@live_chat_guard
async def _save_contact_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _save_contact_core(update, context)


# ─── Talk to Agent Button ────────────────────────────────────────────────────

async def _talk_to_agent_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """לוגיקה פנימית של בקשת נציג — ללא דקורטורים, משמשת את שני הניתובים."""
    user_id, display_name, telegram_username = _get_user_info(update)

    # אם הגענו מזיהוי intent — ההודעה כבר נשמרה ב-message_handler, לא שומרים שוב
    real_message = context.user_data.get("_agent_real_message")
    skip_user_save = real_message is not None

    # Create agent request in database
    # אם הגענו מ-intent detection — נעביר לבעל העסק את ההודעה המקורית של הלקוח
    agent_msg = (
        f"הלקוח ביקש נציג: {real_message}"
        if real_message
        else "הלקוח מבקש לדבר עם נציג אנושי."
    )
    await _create_request_and_notify_owner(
        context,
        user_id=user_id,
        display_name=display_name,
        telegram_username=telegram_username,
        message=agent_msg,
    )

    # הודעת "חוץ מהמשרד" — ציפייה נכונה לזמן חזרה של הנציג
    ooo_notice = get_out_of_office_agent_notice()
    if ooo_notice:
        response_text = (
            "👤 הפנייה שלך הועברה בהצלחה!\n\n"
            f"{ooo_notice}\n"
            "בינתיים, אתם מוזמנים לשאול אותי כל שאלה נוספת!"
        )
    else:
        response_text = (
            "👤 הפנייה שלך הועברה בהצלחה! "
            "בעל העסק יראה את ההודעה ויחזור אליך ברגע שיתפנה.\n\n"
            "בינתיים, אתם מוזמנים לשאול אותי כל שאלה נוספת!"
        )

    if not skip_user_save:
        db.save_message(user_id, display_name, "user", "👤 שיחה עם נציג")
    db.save_message(user_id, display_name, "assistant", response_text)

    await update.message.reply_text(
        response_text,
        reply_markup=_get_main_keyboard(update)
    )


# גרסה מלאה — עם כל הדקורטורים, לשימוש כ-handler ראשי
# סדר ה-decorators: block → rate_limit → vacation → live_chat → consent.
# rate_limit לפני consent — מונע שפם של מסך ההסכמה.
# live_chat לפני consent — בשיחה חיה הבוט שקט; consent_guard לא צריך
# להציג מסך הסכמה גם אחרי bump של CURRENT_CONSENT_VERSION.
@block_guard
@rate_limit_guard
@vacation_guard_agent
@live_chat_guard
@consent_guard
async def talk_to_agent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Talk to Agent button — notify the business owner."""
    return await _talk_to_agent_core(update, context)

# גרסה ללא rate_limit — לניתוב פנימי מ-message_handler (שכבר עבר rate limit)
@vacation_guard_agent
@live_chat_guard
async def _talk_to_agent_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ניתוב פנימי — מדלג על rate_limit (הקורא כבר עבר אותו)."""
    return await _talk_to_agent_core(update, context)


# ─── Appointment Booking Flow ────────────────────────────────────────────────

async def _booking_start_core(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """לוגיקה פנימית של התחלת תור — ללא דקורטורים, משמשת את שני הניתובים."""
    user_id, display_name, telegram_username = _get_user_info(update)

    # קביעת תורים כבויה לעסק — לא פותחים flow תורים. מפנים לבעל העסק
    # (בקשת נציג) ומחזירים END כדי לא להיכנס ל-ConversationHandler.
    if not db.is_booking_enabled():
        await _handoff_to_human(
            update, context,
            user_id=user_id, display_name=display_name,
            telegram_username=telegram_username,
            reason="הלקוח ביקש לקבוע תור, אך העסק אינו מתאם תורים אונליין.",
        )
        return ConversationHandler.END

    # Log the user's booking attempt even if we handoff to human.
    db.save_message(user_id, display_name, "user", "📅 בקשת תור")

    # Get available services from KB
    async with _typing_indicator(context.bot, update.effective_chat.id):
        result = await _generate_answer_async("אילו שירותים אתם מציעים? פרטו בקצרה.")

    stripped = strip_source_citation(result["answer"])
    # בדיקת handoff חייבת להיות לפני הסרת הטוקן (הטוקן הוא הסיגנל)
    is_handoff = _should_handoff_to_human(stripped)
    # בכל מקרה — מסירים את הטוקן כדי שלא יגיע ללקוח גם בטעות
    from core.message_processor import strip_handoff_marker
    stripped = strip_handoff_marker(stripped)

    if is_handoff:
        await _handoff_to_human(
            update,
            context,
            user_id=user_id,
            display_name=display_name,
            telegram_username=telegram_username,
            reason="הלקוח ביקש לקבוע תור, אך אין מידע זמין על השירותים במאגר.",
        )
        return ConversationHandler.END

    stripped = sanitize_telegram_html(stripped)
    text = (
        "📅 <b>בקשת תור</b>\n\n"
        f"{stripped}\n\n"
        "אנא כתבו את <b>השירות</b> שתרצו להזמין "
        "(או הקלידו /cancel כדי לחזור):"
    )

    await _reply_html_safe(update.message, text)
    return BOOKING_SERVICE


# גרסה מלאה — עם כל הדקורטורים, לשימוש כ-entry point של ConversationHandler
# סדר: block → rate_limit → vacation → live_chat → consent.
# rate_limit לפני consent — מונע שפם של מסך ההסכמה.
# live_chat לפני consent — בשיחה חיה הבוט שקט; consent_guard לא יציג מסך
# הסכמה גם אחרי bump של CURRENT_CONSENT_VERSION.
# consent_guard_booking (ולא consent_guard) — מחזיר ConversationHandler.END
# בעת חסימה, סימטרי לשאר *_booking guards. None היה משפיע על PTB כ-"handler
# לא הותאם" ועלול היה לפגוע בפעימת ה-update.
@block_guard_booking
@rate_limit_guard_booking
@vacation_guard_booking
@live_chat_guard_booking
@consent_guard_booking
async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the appointment booking conversation."""
    return await _booking_start_core(update, context)

# גרסה ללא rate_limit — לניתוב פנימי מ-booking_button_interrupt (שכבר עבר rate limit)
@vacation_guard_booking
@live_chat_guard_booking
async def _booking_start_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ניתוב פנימי — מדלג על rate_limit (הקורא כבר עבר אותו)."""
    return await _booking_start_core(update, context)


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the service selection and show inline calendar."""
    context.user_data["booking_service"] = update.message.text

    # משך לחישוב זמינות — ברירת מחדל גלובלית מ-bot_settings (אין יותר משך פר-שירות)
    service_duration = int(
        db.get_appointment_duration_settings().get("default_minutes") or 60
    )
    context.user_data["booking_service_duration"] = service_duration

    # הצגת לוח שנה של החודש הנוכחי — לפי timezone ישראל (עקבי עם calendar_keyboard)
    from bot.calendar_keyboard import _today_israel
    today = _today_israel()
    buf_min = db.get_auto_booking_buffer_minutes()
    cal_keyboard = build_calendar_keyboard(
        today.year, today.month, service_duration,
        buffer_after_event_minutes=buf_min,
    )

    await update.message.reply_text(
        "📆 מעולה! בחרו <b>תאריך</b> מהלוח:\n"
        "(או הקלידו תאריך כמו 'מחר', '15 במרץ')\n\n"
        "הקלידו /cancel כדי לחזור.",
        parse_mode="HTML",
        reply_markup=cal_keyboard,
    )
    return BOOKING_DATE


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the preferred date."""
    user_text = update.message.text

    normalized = normalize_date(user_text)
    if normalized is None:
        # לא הצלחנו לפרסר — מבקשים מהמשתמש לנסות שוב
        await _reply_html_safe(
            update.message,
            "🤔 לא הצלחתי לזהות תאריך.\n\n"
            "אפשר לכתוב למשל:\n"
            "• <b>מחר</b> / <b>מחרתיים</b>\n"
            "• <b>יום ראשון</b> / <b>ביום שלישי</b>\n"
            "• <b>15/03</b> / <b>14 במרץ</b>\n\n"
            "הקלידו /cancel כדי לחזור.",
        )
        return BOOKING_DATE  # נשאר באותו שלב — מחכים לקלט חדש

    context.user_data["booking_date"] = normalized

    # משך לחישוב זמינות — ברירת מחדל גלובלית מ-bot_settings (אין יותר פר-שירות)
    service_duration = int(
        db.get_appointment_duration_settings().get("default_minutes") or 60
    )

    # בדיקת זמינות ביומן Google (אם מחובר)
    available_slots_text = ""
    no_slots_available = False
    try:
        from google_calendar import is_connected, get_available_slots
        connected = is_connected()
        logger.info("booking_date: Google Calendar connected=%s, date=%s", connected, normalized)
        if connected:
            from datetime import date as _date_type
            target = _date_type.fromisoformat(normalized)
            buf_min = db.get_auto_booking_buffer_minutes()
            slots = get_available_slots(
                target, service_duration_minutes=service_duration,
                buffer_after_event_minutes=buf_min,
            )
            if slots:
                # הצגת כל השעות הפנויות
                slots_str = " | ".join(f"<b>{s}</b>" for s in slots)
                available_slots_text = f"\n\n🟢 שעות פנויות: {slots_str}"
            else:
                no_slots_available = True
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בבדיקת זמינות Google Calendar", exc_info=True)

    # אם אין שעות פנויות — חוזרים לבחירת תאריך במקום להמשיך ל-BOOKING_TIME
    if no_slots_available:
        await _reply_html_safe(
            update.message,
            f"📅 תאריך: <b>{_format_date_display(normalized)}</b>\n\n"
            "🔴 אין שעות פנויות בתאריך זה.\n"
            "אנא כתבו <b>תאריך אחר</b>, או הקלידו /cancel כדי לחזור.",
        )
        return BOOKING_DATE

    await _reply_html_safe(
        update.message,
        f"📅 תאריך: <b>{_format_date_display(normalized)}</b>{available_slots_text}\n\n"
        "🕐 איזו <b>שעה</b> מתאימה לכם?\n"
        "(לדוגמה, '10:00', 'אחר הצהריים', '14:00')\n\n"
        "הקלידו /cancel כדי לחזור.",
    )
    return BOOKING_TIME


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the preferred time and show confirmation."""
    context.user_data["booking_time"] = update.message.text

    service = _html.escape(context.user_data.get("booking_service", ""))
    date_raw = context.user_data.get("booking_date", "")
    date_display = _html.escape(_format_date_display(date_raw))
    preferred_time = _html.escape(context.user_data.get("booking_time", ""))

    confirmation_text = (
        "📋 <b>סיכום בקשת התור:</b>\n\n"
        f"• שירות: {service}\n"
        f"• תאריך: {date_display}\n"
        f"• שעה: {preferred_time}\n\n"
        "אנא אשרו על ידי כתיבת <b>כן</b> או <b>לא</b>:"
    )

    await _reply_html_safe(update.message, confirmation_text)
    return BOOKING_CONFIRM


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle booking confirmation."""
    user_id, display_name, telegram_username = _get_user_info(update)
    answer = update.message.text.lower().strip()
    
    if answer in ("yes", "y", "confirm", "כן", "אישור"):
        service = context.user_data.get("booking_service", "")
        date = context.user_data.get("booking_date", "")
        date_display = _format_date_display(date)
        preferred_time = context.user_data.get("booking_time", "")

        # בדיקה חוזרת מול Google Calendar — ודא שהשעה עדיין פנויה
        try:
            from google_calendar import is_connected, get_available_slots
            if is_connected() and date and preferred_time:
                from datetime import date as _date_type
                target = _date_type.fromisoformat(date)
                # נרמול שעה ל-HH:MM — תמיכה ב"8:30" → "08:30", "14:00:00" → "14:00"
                # אם הקלט הוא טקסט חופשי (לא מספרי) — מדלגים על הבדיקה
                time_check = _normalize_time_for_gcal(preferred_time)
                if time_check is not None:
                    # ברירת מחדל גלובלית — אין יותר משך פר-שירות
                    svc_dur = int(
                        db.get_appointment_duration_settings().get("default_minutes") or 60
                    )
                    buf_min = db.get_auto_booking_buffer_minutes()
                    slots = get_available_slots(
                        target, service_duration_minutes=svc_dur,
                        buffer_after_event_minutes=buf_min,
                    )
                    if time_check not in slots:
                        # נשארים ב-flow (בלי לנקות user_data) כדי שהבוט יאזין
                        # לתיקון. מבחינים בין "יש שעות אחרות" ל"היום מלא":
                        if not slots:
                            # אין שעות פנויות ביום הזה כלל — חוזרים לבחירת *תאריך*
                            # (כמו booking_date), אחרת הלקוח תקוע בשלב השעה שבו
                            # אף שעה לא תעבוד באותו יום.
                            await _reply_html_safe(
                                update.message,
                                f"📅 תאריך: <b>{date_display}</b>\n\n"
                                "🔴 אין שעות פנויות בתאריך זה.\n"
                                "אנא כתבו <b>תאריך אחר</b> (או /cancel לביטול):",
                            )
                            return BOOKING_DATE
                        # יש שעות אחרות — נשארים בשלב השעה ומציגים אותן
                        slots_str = " | ".join(f"<b>{_html.escape(s)}</b>" for s in slots)
                        await _reply_html_safe(
                            update.message,
                            f"⚠️ לצערנו, השעה {_html.escape(preferred_time)} כבר לא פנויה "
                            f"בתאריך {date_display}.\n\n🟢 שעות פנויות: {slots_str}\n\n"
                            "🕐 אנא בחרו <b>שעה אחרת</b> (או /cancel לביטול):",
                        )
                        return BOOKING_TIME
        except ImportError:
            pass
        except Exception:
            logger.error("שגיאה בבדיקה חוזרת מול Google Calendar", exc_info=True)

        # הגנה מפני עיבוד כפול — בודקים אם כבר נוצר תור זהה (race condition / double-tap)
        existing = [
            a for a in db.get_pending_appointments_for_user(user_id)
            if a["preferred_date"] == date and a["preferred_time"] == preferred_time
        ]
        if existing:
            logger.info("תור כפול נחסם (כבר קיים): user=%s date=%s time=%s", user_id, date, preferred_time)
            context.user_data.clear()
            return ConversationHandler.END

        # Save appointment to database
        try:
            appt_id = db.create_appointment(
                user_id=user_id,
                username=display_name,
                service=service,
                preferred_date=date,
                preferred_time=preferred_time,
                telegram_username=telegram_username,
            )
        except IntegrityError:
            # רשת ביטחון — race condition צמוד שעבר את הבדיקה למעלה
            logger.warning("כפילות תור (IntegrityError): user=%s date=%s time=%s", user_id, date, preferred_time)
            await update.message.reply_text(
                f"⚠️ כבר יש לכם בקשת תור לתאריך {date_display} בשעה {preferred_time}.\n"
                "אם תרצו לשנות — בטלו את הבקשה הקיימת ונסו שוב.",
                reply_markup=_get_main_keyboard(update),
            )
            context.user_data.clear()
            return ConversationHandler.END

        # סימון המרה — אם הלקוח הגיע דרך follow-up
        from ai_chatbot.config import FOLLOWUP_ENABLED
        if FOLLOWUP_ENABLED:
            from ai_chatbot.followup_service import handle_booking_created
            handle_booking_created(user_id)

        # ─── החלטת auto-booking (לפי הגדרת בעל העסק) ───────────────
        # ב-mode=manual ⇒ pending (כמו תמיד), אלא אם הוחזר rejected מסיבה
        # הגיונית (סלוט בעבר/רחוק/חוצה חצות) — אז מבטלים ושולחים סירוב.
        # ב-auto_with_check / auto_always: confirmed כשהתנאים מתקיימים, או
        # rejected כשהתנאים נפסלים. תרחיש "pending" בקוד הזה = fallback
        # בטוח (חוסר ודאות, כמו GCal disconnected).
        auto_confirmed = False
        rejected_reason: str | None = None
        try:
            from ai_chatbot.core.booking_decision import (
                gather_and_decide, get_rejection_message,
            )
            decision = gather_and_decide(
                user_id=user_id,
                slot_date_str=date,
                slot_time_str=preferred_time,
                # מחריגים את התור שזה עתה נוצר כדי שלא יחסום את השעה של עצמו
                # בבדיקת הזמינות מול היומן (calendar_busy כוזב).
                exclude_appointment_id=appt_id,
            )
            if decision.decision == "confirmed":
                duration_settings = db.get_appointment_duration_settings()
                duration = int(duration_settings.get("default_minutes") or 60)
                db.update_appointment_status(
                    appt_id, "confirmed", confirmed_duration_minutes=duration,
                )
                auto_confirmed = True
            elif decision.decision == "rejected":
                # ביטול התור שיצרנו — אם ה-update נכשל, ה-except למטה יתפוס
                # ו-rejected_reason יישאר None ⇒ נופלים חזרה ל-pending רגיל,
                # עקבי עם המסלול ה-confirmed שמסמן auto_confirmed רק אחרי הצלחה.
                db.update_appointment_status(appt_id, "cancelled")
                rejected_reason = decision.reason
        except Exception:
            logger.error("auto-booking decision failed (Telegram)", exc_info=True)

        # ─── מסלול דחייה ──────────────────────────────────────────
        if rejected_reason:
            from ai_chatbot.core.booking_decision import get_rejection_message
            rejection_msg = get_rejection_message(rejected_reason)
            # שומרים בהיסטוריה את הטקסט שהוצג ללקוח בלבד — לא קוד שגיאה
            # פנימי. ראה הסבר ב-messaging/whatsapp_booking.py על דליפת קוד
            # שגיאה ללקוח דרך LLM context.
            logger.info(
                "Telegram booking rejected: user=%s date=%s time=%s reason=%s",
                user_id, date, preferred_time, rejected_reason,
            )
            db.save_message(user_id, display_name, "assistant", f"⚠️ {rejection_msg}")

            # נשארים ב-flow בשלב המתאים כדי שהבוט יאזין לתיקון (שעה/תאריך),
            # במקום לנקות user_data ולסיים ⇒ הלקוח נופל ל-handler הכללי.
            # הודעת הדחייה עצמה כבר מזמינה "בחרו שעה/תאריך אחר/ת".
            recovery = _rejection_recovery_step(rejected_reason)
            if recovery == "time":
                await _reply_html_safe(
                    update.message,
                    f"⚠️ {_html.escape(rejection_msg)}\n\n"
                    "🕐 שלחו <b>שעה אחרת</b> (או /cancel לביטול):",
                )
                return BOOKING_TIME
            if recovery == "date":
                await _reply_html_safe(
                    update.message,
                    f"⚠️ {_html.escape(rejection_msg)}\n\n"
                    "📆 שלחו <b>תאריך אחר</b> (או /cancel לביטול):",
                )
                return BOOKING_DATE

            # terminal (חופשה / שגיאה פנימית / לא-ידוע) — מנקים ומציעים מחדש
            await update.message.reply_text(
                f"⚠️ {rejection_msg}\n\nשלחו /book כדי לנסות שוב.",
                reply_markup=_get_main_keyboard(update),
            )
            context.user_data.clear()
            return ConversationHandler.END

        # Notify business owner
        handle = _tg_handle(telegram_username) or "(ללא שם משתמש)"
        panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
        if auto_confirmed:
            notification = (
                f"✅ תור חדש אושר אוטומטית #{appt_id}\n\n"
                f"לקוח: {display_name}\n"
                f"יוזר: {handle}\n"
                f"שירות: {service}\n"
                f"תאריך: {date_display}\n"
                f"שעה: {preferred_time}"
                f"{panel_link}"
            )
        else:
            notification = (
                f"📅 בקשת תור חדשה לאישור #{appt_id}\n\n"
                f"לקוח: {display_name}\n"
                f"יוזר: {handle}\n"
                f"שירות: {service}\n"
                f"תאריך: {date_display}\n"
                f"שעה: {preferred_time}"
                f"{panel_link}"
            )
        await _notify_owner(context, notification)

        # אישור אוטומטי — מפעיל את צינור ההתראה הסטנדרטי (ICS + Google Calendar sync).
        # שומרים האם ההודעה נשלחה בהצלחה כדי לדעת אם נצטרך fallback ללקוח.
        notify_succeeded = False
        if auto_confirmed:
            try:
                from appointment_notifications import notify_appointment_status
                appt = db.get_appointment(appt_id)
                if appt:
                    notify_succeeded = bool(notify_appointment_status(appt))
            except Exception:
                logger.error(
                    "auto-confirm: notify_appointment_status failed", exc_info=True,
                )

        db.save_message(user_id, display_name, "assistant",
                        f"בקשת תור: {service} בתאריך {date_display} בשעה {preferred_time}")

        # אישור ידני: "בקשת התור התקבלה" הרגילה.
        # אישור אוטומטי שהצליח: notify_appointment_status שלח את האישור המפורט,
        # אנחנו רק משחזרים את המקלדת הראשית עם ack מינימלי — כי בלי reply_text
        # הלקוח נשאר עם המקלדת של ה-booking flow (notify שולח דרך הבוט הסטנדאלון
        # בלי reply_markup).
        # אישור אוטומטי שנכשל: שולחים אישור fallback מלא עם המקלדת.
        if not auto_confirmed:
            await update.message.reply_text(
                f"📋 בקשת התור התקבלה!\n\n"
                f"• שירות: {service}\n"
                f"• תאריך: {date_display}\n"
                f"• שעה: {preferred_time}\n\n"
                f"העברנו את הפרטים לבית העסק. "
                f"ניצור איתכם קשר בהקדם לאישור סופי של השעה.",
                reply_markup=_get_main_keyboard(update),
            )
        elif not notify_succeeded:
            # ההתראה הסטנדרטית נכשלה — fallback מלא (התור confirmed ב-DB).
            await update.message.reply_text(
                f"✅ התור אושר!\n\n"
                f"• שירות: {service}\n"
                f"• תאריך: {date_display}\n"
                f"• שעה: {preferred_time}",
                reply_markup=_get_main_keyboard(update),
            )
        else:
            # auto-confirmed + notify הצליח — האישור המפורט כבר הגיע ללקוח.
            # שולחים ack מינימלי רק כדי לשחזר את המקלדת הראשית.
            await update.message.reply_text(
                "👌",
                reply_markup=_get_main_keyboard(update),
            )

        # קוד הפניה נשלח רק כשהתור מאושר ע"י בעל העסק (ב-admin)
    else:
        await update.message.reply_text(
            "❌ בקשת התור בוטלה. אין בעיה!\n"
            "אתם מוזמנים לבקש תור חדש בכל עת.",
            reply_markup=_get_main_keyboard(update)
        )
    
    context.user_data.clear()
    return ConversationHandler.END


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the booking flow."""
    context.user_data.clear()
    await update.message.reply_text(
        "תהליך בקשת התור בוטל. איך עוד אפשר לעזור לכם?",
        reply_markup=_get_main_keyboard(update)
    )
    return ConversationHandler.END


@block_guard_booking
@rate_limit_guard_booking
@live_chat_guard_booking
async def booking_button_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button clicks during an active booking — cancel booking and route to the clicked button."""
    context.user_data.clear()
    user_message = update.message.text

    # מדלגים על rate_limit (הקורא כבר עבר אותו) אבל שומרים על
    # live_chat_guard (ו-vacation_guard היכן שרלוונטי) דרך גרסאות _skip_ratelimit.
    if user_message == BUTTON_BOOKING:
        return await _booking_start_skip_ratelimit(update, context)

    if user_message == BUTTON_PRICE_LIST:
        await _price_list_skip_ratelimit(update, context)
    elif user_message == BUTTON_LOCATION:
        await _location_skip_ratelimit(update, context)
    elif user_message == BUTTON_SAVE_CONTACT:
        await _save_contact_skip_ratelimit(update, context)
    elif user_message == BUTTON_AGENT:
        await _talk_to_agent_skip_ratelimit(update, context)
    elif user_message == BUTTON_REFERRAL:
        await _referral_skip_ratelimit(update, context)
    else:
        # Safety fallback — should not happen, but avoid a silent dead-end
        logger.warning("booking_button_interrupt: unexpected text %r", user_message)
        await update.message.reply_text(
            "תהליך בקשת התור בוטל. איך עוד אפשר לעזור לכם?",
            reply_markup=_get_main_keyboard(update),
        )

    return ConversationHandler.END


# ─── Calendar Inline Keyboard Callbacks ────────────────────────────────────────

@live_chat_guard_booking
async def calendar_navigate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ניווט בין חודשים בלוח השנה (◀ / ▶)."""
    query = update.callback_query
    await query.answer()

    parsed = parse_calendar_callback(query.data)
    if parsed["action"] not in ("prev", "next"):
        return BOOKING_DATE

    year = parsed["year"]
    month = parsed["month"]
    service_duration = context.user_data.get("booking_service_duration", 60)
    buf_min = db.get_auto_booking_buffer_minutes()

    cal_keyboard = build_calendar_keyboard(
        year, month, service_duration,
        buffer_after_event_minutes=buf_min,
    )
    try:
        await query.edit_message_reply_markup(reply_markup=cal_keyboard)
    except BadRequest:
        logger.debug("calendar_navigate: edit_message_reply_markup failed (message unchanged)")

    return BOOKING_DATE


@live_chat_guard_booking
async def calendar_ignore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """לחיצה על כפתור לא פעיל בלוח השנה (כותרת, יום סגור)."""
    query = update.callback_query
    await query.answer()
    return BOOKING_DATE


@live_chat_guard_booking
async def calendar_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """בחירת יום מלוח השנה — בודק זמינות ומציג שעות פנויות."""
    query = update.callback_query
    await query.answer()

    parsed = parse_calendar_callback(query.data)
    if parsed["action"] != "select":
        return BOOKING_DATE

    normalized = parsed["date"]  # ISO format: YYYY-MM-DD
    context.user_data["booking_date"] = normalized

    # משך תור לפי שירות
    service_duration = context.user_data.get("booking_service_duration", 60)

    # בדיקת זמינות ביומן Google (אם מחובר)
    available_slots_text = ""
    no_slots_available = False
    try:
        from google_calendar import is_connected, get_available_slots
        connected = is_connected()
        if connected:
            from datetime import date as _date_type
            target = _date_type.fromisoformat(normalized)
            buf_min = db.get_auto_booking_buffer_minutes()
            slots = get_available_slots(
                target, service_duration_minutes=service_duration,
                buffer_after_event_minutes=buf_min,
            )
            if slots:
                slots_str = " | ".join(f"<b>{s}</b>" for s in slots)
                available_slots_text = f"\n\n🟢 שעות פנויות: {slots_str}"
            else:
                no_slots_available = True
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בבדיקת זמינות Google Calendar", exc_info=True)

    # אם אין שעות פנויות — חוזרים ללוח השנה
    if no_slots_available:
        await query.edit_message_text(
            f"📅 תאריך: <b>{_format_date_display(normalized)}</b>\n\n"
            "🔴 אין שעות פנויות בתאריך זה.\n"
            "אנא בחרו <b>תאריך אחר</b> מהלוח, או הקלידו /cancel כדי לחזור.",
            parse_mode="HTML",
        )
        # מציגים את הלוח מחדש
        from datetime import date as _date_type
        target = _date_type.fromisoformat(normalized)
        cal_keyboard = build_calendar_keyboard(
            target.year, target.month, service_duration,
            buffer_after_event_minutes=db.get_auto_booking_buffer_minutes(),
        )
        await query.message.reply_text(
            "📆 בחרו תאריך:",
            reply_markup=cal_keyboard,
        )
        return BOOKING_DATE

    # מסירים את לוח השנה ומציגים את השעות
    await query.edit_message_text(
        f"📅 תאריך: <b>{_format_date_display(normalized)}</b>{available_slots_text}\n\n"
        "🕐 איזו <b>שעה</b> מתאימה לכם?\n"
        "(לדוגמה, '10:00', 'אחר הצהריים', '14:00')\n\n"
        "הקלידו /cancel כדי לחזור.",
        parse_mode="HTML",
    )
    return BOOKING_TIME


# ─── Shared RAG pipeline ─────────────────────────────────────────────────────

async def _handle_rag_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: str,
    display_name: str,
    telegram_username: str,
    user_message: str,
    query: str,
    handoff_reason: str,
    chat_id: int | None = None,
) -> None:
    """הרצת צינור RAG + LLM ושליחת התוצאה (או העברה לנציג).

    מעטפת Telegram סביב process_rag_query — מוסיפה typing indicator,
    סניטציית HTML, שאלות המשך, וסיכום ברקע.

    כש-chat_id מסופק ו-update.message לא קיים (למשל callback query),
    השליחה נעשית ישירות לצ'אט במקום כ-reply.
    """
    effective_chat_id = chat_id or update.effective_chat.id
    use_direct_send = chat_id is not None and update.message is None

    async with _typing_indicator(context.bot, effective_chat_id):
        result = await asyncio.to_thread(
            process_rag_query,
            user_id=user_id,
            display_name=display_name,
            user_message=user_message,
            query=query,
            handoff_reason=handoff_reason,
            consecutive_fallbacks=context.user_data.get("consecutive_fallbacks", 0),
            channel="telegram",
        )

    # עדכון מונה fallbacks
    context.user_data["consecutive_fallbacks"] = result.consecutive_fallbacks

    if result.action == "handoff_to_human":
        # יצירת בקשת נציג + התראה — הפרוססור כבר שמר את ההודעה ב-DB
        await _create_request_and_notify_owner(
            context,
            user_id=user_id,
            display_name=display_name,
            telegram_username=telegram_username,
            message=result.handoff_reason,
        )
        if use_direct_send:
            await context.bot.send_message(
                chat_id=effective_chat_id, text=result.text,
                reply_markup=_get_main_keyboard(update),
            )
        else:
            await update.message.reply_text(
                result.text,
                reply_markup=_get_main_keyboard(update),
            )
    else:
        # תשובה רגילה (כולל soft fallback) — סניטציית HTML לטלגרם
        text_to_send = sanitize_telegram_html(result.text) if result.is_html else result.text
        reply_markup = _get_main_keyboard(update) if result.show_keyboard else None
        if use_direct_send:
            await _send_html_safe(context.bot, effective_chat_id, text_to_send, reply_markup=reply_markup)
        else:
            await _reply_html_safe(update.message, text_to_send, reply_markup=reply_markup)

        # שאלות המשך — שליחה כהודעה נפרדת עם כפתורי inline
        if FOLLOW_UP_ENABLED and result.follow_up_questions:
            follow_up_kb = _build_follow_up_keyboard(result.follow_up_questions, context.bot_data, user_id)
            if follow_up_kb:
                if use_direct_send:
                    await _send_html_safe(
                        context.bot, effective_chat_id,
                        "💡 <b>אולי תרצו גם לשאול:</b>",
                        reply_markup=follow_up_kb,
                    )
                else:
                    await update.message.reply_text(
                        "💡 <b>אולי תרצו גם לשאול:</b>",
                        parse_mode="HTML",
                        reply_markup=follow_up_kb,
                    )

    if result.needs_summarization:
        context.application.create_task(_summarize_safe(user_id))

    # ניתוח ליד ברקע — לא מנתחים אם המשתמש ביקש נציג אנושי,
    # כדי לא ליצור follow-up אוטומטי למי שכבר מחכה למענה אנושי.
    # רץ בנפרד מ-needs_summarization כדי לתפוס גם שיחות קצרות (4-6 הודעות)
    # שלא עוברות סף סיכום אבל יכולות להיות לידים חמים.
    from ai_chatbot.config import FOLLOWUP_ENABLED
    if FOLLOWUP_ENABLED and result.action != "handoff_to_human":
        context.application.create_task(
            _analyze_lead_safe(user_id, username=display_name, channel="telegram")
        )


# ─── Free-Text Message Handler ─────��─────────────────────────────��───────────

@block_guard
@rate_limit_guard
@live_chat_guard
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle any free-text message from the user.

    הלוגיקה העסקית (intent detection, RAG, rate limiting) מופעלת דרך
    process_incoming_message מ-core/message_processor.py.
    ה-handler ממפה את התוצאה לפעולות Telegram ספציפיות.
    """
    user_id, display_name, telegram_username = _get_user_info(update)
    user_message = update.message.text

    # תיקון 13 — לפני שכותבים PII, מוודאים שיש הסכמה.
    # הפקודות /start, /myinfo, /forget, /stop וה-callbacks של ההסכמה עוברות
    # נתיבים נפרדים ולא נכנסות לכאן, אז המשתמש לא נתקע במלכוד.
    if not db.has_consent(user_id):
        await _send_consent_screen(update, context)
        return

    # יש הסכמה — מותר לעדכן את שורת המשתמש ולרשום לשידורים.
    db.ensure_user_subscribed(user_id)
    db.upsert_user(user_id, display_name, channel="telegram")

    # סימון שהמשתמש חזר — אם קיבל follow-up ועכשיו מגיב
    from ai_chatbot.config import FOLLOWUP_ENABLED
    if FOLLOWUP_ENABLED:
        from ai_chatbot.followup_service import handle_user_returned
        handle_user_returned(user_id)

    # בדיקת מעורבות גבוהה — רץ ברקע על כל סוגי ההודעות (כולל ברכות,
    # כפתורים, תורים וכו'). הבדיקה עצמה זולה (early exit אם כבר נשלח).
    context.application.create_task(
        _check_high_engagement_referral(update, user_id)
    )

    # ניתוב כפתורים — מדלגים על rate_limit (כבר נספר פעם אחת) אבל
    # שומרים על live_chat_guard (ו-vacation_guard היכן שרלוונטי).
    # איפוס מונה fallbacks — לחיצת כפתור = המשתמש התקדם, לא צריך לספור fallback
    if user_message == BUTTON_PRICE_LIST:
        context.user_data["consecutive_fallbacks"] = 0
        return await _price_list_skip_ratelimit(update, context)
    elif user_message == BUTTON_LOCATION:
        context.user_data["consecutive_fallbacks"] = 0
        return await _location_skip_ratelimit(update, context)
    elif user_message == BUTTON_SAVE_CONTACT:
        context.user_data["consecutive_fallbacks"] = 0
        return await _save_contact_skip_ratelimit(update, context)
    elif user_message == BUTTON_AGENT:
        context.user_data["consecutive_fallbacks"] = 0
        return await _talk_to_agent_skip_ratelimit(update, context)
    elif user_message == BUTTON_REFERRAL:
        context.user_data["consecutive_fallbacks"] = 0
        return await _referral_skip_ratelimit(update, context)

    # ── reschedule flow — אם יש state פתוח, מטפלים בקלט תאריך/שעה ──────
    reschedule_state = context.user_data.get("reschedule_state")
    if reschedule_state:
        await _handle_reschedule_text_input(update, context, reschedule_state, user_message)
        return

    # ── עיבוד הודעת טקסט חופשי דרך ה-processor ──────────────────────────
    async with _typing_indicator(context.bot, update.effective_chat.id):
        result = await asyncio.to_thread(
            process_incoming_message,
            user_id=user_id,
            text=user_message,
            user_info={"display_name": display_name, "telegram_username": telegram_username},
            consecutive_fallbacks=context.user_data.get("consecutive_fallbacks", 0),
            rate_limit_already_checked=True,
        )

    # עדכון מונה fallbacks
    context.user_data["consecutive_fallbacks"] = result.consecutive_fallbacks

    # ── מיפוי תוצאה לפעולות Telegram ────────────────────────────────────

    if result.action == "rate_limited":
        try:
            await update.message.reply_text(result.text, parse_mode="HTML")
        except BadRequest:
            await update.message.reply_text(result.text)
        return

    if result.action == "request_agent":
        # יצירת בקשת נציג + התראה לבעל העסק
        await _create_request_and_notify_owner(
            context,
            user_id=user_id,
            display_name=display_name,
            telegram_username=telegram_username,
            message=result.agent_request_message,
        )
        await update.message.reply_text(
            result.text,
            reply_markup=_get_main_keyboard(update),
        )
        return

    if result.action == "start_booking":
        await _reply_html_safe(
            update.message, result.text, reply_markup=_get_main_keyboard(update)
        )
        return

    if result.action == "cancel_appointment":
        pending = db.get_pending_appointments_for_user(user_id)
        if not pending:
            await update.message.reply_text(
                "לא רשום אצלנו תור על שמך. 🤔\nתרצו שאעביר את הבקשה לבעל העסק כדי לברר?",
                reply_markup=_get_main_keyboard(update),
            )
            return

        if len(pending) == 1:
            # תור יחיד — ישר לאישור
            appt = pending[0]
            date_display = _format_date_display(appt.get("preferred_date", ""))
            confirm_text = (
                f"האם לבטל את התור הזה?\n\n"
                f"📋 <b>שירות:</b> {_html.escape(appt.get('service', ''))}\n"
                f"📅 <b>תאריך:</b> {_html.escape(date_display)}\n"
                f"🕐 <b>שעה:</b> {_html.escape(appt.get('preferred_time', ''))}"
            )
            confirm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("כן, לבטל", callback_data=f"cancel_appt_yes_{appt['id']}"),
                    InlineKeyboardButton("לא, טעות", callback_data="cancel_appt_no"),
                ]
            ])
            await _reply_html_safe(update.message, confirm_text, reply_markup=confirm_kb)
        else:
            # מספר תורים — שלב בחירה
            buttons = []
            for appt in pending:
                date_display = _format_date_display(appt.get("preferred_date", ""))
                label = f"{appt.get('service', '')} | {date_display} | {appt.get('preferred_time', '')}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"cancel_select_{appt['id']}")])
            buttons.append([InlineKeyboardButton("ביטול — לא רוצה לבטל", callback_data="cancel_appt_no")])
            select_kb = InlineKeyboardMarkup(buttons)
            await update.message.reply_text("איזה תור תרצו לבטל?", reply_markup=select_kb)
        return

    if result.action == "reschedule_appointment":
        pending = db.get_pending_appointments_for_user(user_id)
        if not pending:
            await update.message.reply_text(
                "לא רשום אצלנו תור על שמך. 🤔\nתרצו שאעביר את הבקשה לבעל העסק כדי לברר?",
                reply_markup=_get_main_keyboard(update),
            )
            return

        if len(pending) == 1:
            # תור יחיד — ישר לבחירת תאריך חדש
            appt = pending[0]
            date_display = _format_date_display(appt.get("preferred_date", ""))
            text = (
                f"🔄 שינוי תור:\n\n"
                f"📋 <b>שירות:</b> {_html.escape(appt.get('service', ''))}\n"
                f"📅 <b>תאריך נוכחי:</b> {_html.escape(date_display)}\n"
                f"🕐 <b>שעה נוכחית:</b> {_html.escape(appt.get('preferred_time', ''))}\n\n"
                f"📅 מה <b>התאריך החדש</b> שמתאים לכם?\n"
                f"(למשל: מחר, יום ראשון, 15/03)"
            )
            context.user_data["reschedule_appt_id"] = appt["id"]
            context.user_data["reschedule_service"] = appt.get("service", "")
            context.user_data["reschedule_state"] = "date"
            # מעדיפים את המשך שאושר בפועל לתור הזה (אם קיים), אחרת ברירת מחדל גלובלית
            context.user_data["reschedule_service_duration"] = (
                db.resolve_appointment_duration_minutes(appt)
            )
            await _reply_html_safe(update.message, text)
        else:
            # מספר תורים — שלב בחירה
            buttons = []
            for appt in pending:
                date_display = _format_date_display(appt.get("preferred_date", ""))
                label = f"{appt.get('service', '')} | {date_display} | {appt.get('preferred_time', '')}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"reschedule_select_{appt['id']}")])
            buttons.append([InlineKeyboardButton("ביטול — לא רוצה לשנות", callback_data="reschedule_no")])
            select_kb = InlineKeyboardMarkup(buttons)
            await update.message.reply_text("איזה תור תרצו לשנות?", reply_markup=select_kb)
        return

    if result.action == "handoff_to_human":
        # יצירת בקשת נציג + התראה — הפרוססור כבר שמר את ההודעה ב-DB
        await _create_request_and_notify_owner(
            context,
            user_id=user_id,
            display_name=display_name,
            telegram_username=telegram_username,
            message=result.handoff_reason,
        )
        await update.message.reply_text(
            result.text,
            reply_markup=_get_main_keyboard(update),
        )
        if result.needs_summarization:
            context.application.create_task(_summarize_safe(user_id))
        return

    # ── reply / complaint / default — שליחת תשובה ────────────────────────
    text_to_send = sanitize_telegram_html(result.text) if result.is_html else result.text
    reply_markup = _get_main_keyboard(update) if result.show_keyboard else None

    if result.is_html:
        await _reply_html_safe(update.message, text_to_send, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text_to_send, reply_markup=reply_markup)

    # שאלות המשך — שליחה כהודעה נפרדת עם כפתורי inline
    if FOLLOW_UP_ENABLED and result.follow_up_questions:
        follow_up_kb = _build_follow_up_keyboard(
            result.follow_up_questions, context.bot_data, user_id,
        )
        if follow_up_kb:
            await update.message.reply_text(
                "💡 <b>אולי תרצו גם לשאול:</b>",
                parse_mode="HTML",
                reply_markup=follow_up_kb,
            )

    if result.needs_summarization:
        context.application.create_task(_summarize_safe(user_id))

    # ניתוח ליד ברקע — רץ בנפרד מ-needs_summarization כדי לתפוס
    # גם שיחות קצרות (4-6 הודעות). analyze_lead כבר בודק כפילויות פנימית.
    from ai_chatbot.config import FOLLOWUP_ENABLED
    if FOLLOWUP_ENABLED:
        context.application.create_task(
            _analyze_lead_safe(user_id, username=display_name, channel="telegram")
        )


# ─── Cancellation Confirmation Callback ──────────────────────────────────────

async def cancel_appointment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the inline-button response to the cancellation confirmation prompt.

    callback query handler — חייבים לקרוא ל-query.answer() לפני כל בדיקה
    אחרת, כי דקורטורים (rate_limit_guard, live_chat_guard) יכולים לחזור מוקדם
    ולהשאיר את אינדיקטור הטעינה של טלגרם תקוע. לכן הבדיקות נעשות ידנית.
    """
    query = update.callback_query
    # תמיד לענות ל-callback query כדי לבטל את אינדיקטור הטעינה של טלגרם
    await query.answer()

    from ai_chatbot.live_chat_service import LiveChatService
    user = update.effective_user
    if LiveChatService.is_active(str(user.id)):
        return

    user_id, display_name, telegram_username = _get_user_info(update)
    data = query.data

    # בחירת תור מרשימה (כשיש יותר מאחד) → הצגת אישור
    if data.startswith("cancel_select_"):
        appt_id = int(data.replace("cancel_select_", ""))
        appt = db.get_appointment(appt_id)
        if not appt or appt["user_id"] != user_id:
            response = "התור לא נמצא. 🤔"
        else:
            date_display = _format_date_display(appt.get("preferred_date", ""))
            response = (
                f"האם לבטל את התור הזה?\n\n"
                f"📋 <b>שירות:</b> {_html.escape(appt.get('service', ''))}\n"
                f"📅 <b>תאריך:</b> {_html.escape(date_display)}\n"
                f"🕐 <b>שעה:</b> {_html.escape(appt.get('preferred_time', ''))}"
            )
            confirm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("כן, לבטל", callback_data=f"cancel_appt_yes_{appt_id}"),
                    InlineKeyboardButton("לא, טעות", callback_data="cancel_appt_no"),
                ]
            ])
            db.save_message(user_id, display_name, "assistant", response)
            await query.edit_message_text(response, parse_mode="HTML", reply_markup=confirm_kb)
            return

    # אישור ביטול — עם ID ספציפי של התור
    elif data.startswith("cancel_appt_yes"):
        appt_id_str = data.replace("cancel_appt_yes", "").lstrip("_")
        if appt_id_str:
            # ביטול תור ספציפי לפי ID
            appt_id = int(appt_id_str)
            appt = db.get_appointment(appt_id)
        else:
            # תאימות לאחור — בלי ID, לוקח את הראשון
            pending = db.get_pending_appointments_for_user(user_id)
            appt = pending[0] if pending else None
            appt_id = appt["id"] if appt else 0

        if not appt or appt["user_id"] != user_id:
            response = "התור לא נמצא. 🤔"
        else:
            cancelled = db.cancel_appointment_and_sync(appt_id, user_id)
            if cancelled:
                service = appt.get('service', '')
                date_display = _format_date_display(appt.get('preferred_date', ''))
                time_str = appt.get('preferred_time', '')
                response = (
                    f"התור שלך בוטל בהצלחה ✅\n\n"
                    f"📋 <b>שירות:</b> {_html.escape(service)}\n"
                    f"📅 <b>תאריך:</b> {_html.escape(date_display)}\n"
                    f"🕐 <b>שעה:</b> {_html.escape(time_str)}\n\n"
                    f"לקביעת תור חדש, שלחו /book"
                )
                # התראה לבעל העסק
                handle = _tg_handle(telegram_username) or "(ללא שם משתמש)"
                panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
                notification = (
                    f"❌ ביטול תור #{appt_id}\n\n"
                    f"לקוח: {display_name}\n"
                    f"יוזר: {handle}\n"
                    f"שירות: {service}\n"
                    f"תאריך: {date_display}\n"
                    f"שעה: {time_str}"
                    f"{panel_link}"
                )
                await _notify_owner(context, notification)
            else:
                response = (
                    "לא הצלחנו לבטל את התור — ייתכן שהסטטוס שלו השתנה. 🤔\n"
                    "נסו שוב, או לחצו על <b>👤 דברו עם נציג</b> למטה לעזרה."
                )

    else:
        response = "בסדר גמור, התור נשאר! 👍\nאיך עוד אפשר לעזור?"

    db.save_message(user_id, display_name, "assistant", response)
    if "<b>" in response:
        await query.edit_message_text(response, parse_mode="HTML")
    else:
        await query.edit_message_text(response)
    # הצגת מקלדת ראשית מחדש אחרי inline button
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👇",
        reply_markup=_get_main_keyboard(update),
    )


# ─── Reschedule Appointment ──────────────────────────────────────────────

def _clear_reschedule_state(context: ContextTypes.DEFAULT_TYPE):
    """ניקוי state של reschedule מ-user_data."""
    for key in ("reschedule_state", "reschedule_appt_id", "reschedule_service",
                "reschedule_service_duration", "reschedule_date", "reschedule_time"):
        context.user_data.pop(key, None)


async def reschedule_appointment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בכפתורי inline של שינוי תור — בחירת תור ואישור סופי."""
    query = update.callback_query
    await query.answer()

    from ai_chatbot.live_chat_service import LiveChatService
    user = update.effective_user
    if LiveChatService.is_active(str(user.id)):
        return

    user_id, display_name, telegram_username = _get_user_info(update)
    data = query.data

    # בחירת תור מרשימה → מעבר לשלב בחירת תאריך
    if data.startswith("reschedule_select_"):
        appt_id = int(data.replace("reschedule_select_", ""))
        appt = db.get_appointment(appt_id)
        if not appt or appt["user_id"] != user_id:
            response = "התור לא נמצא. 🤔"
            db.save_message(user_id, display_name, "assistant", response)
            await query.edit_message_text(response)
            return

        date_display = _format_date_display(appt.get("preferred_date", ""))
        response = (
            f"🔄 שינוי תור:\n\n"
            f"📋 <b>שירות:</b> {_html.escape(appt.get('service', ''))}\n"
            f"📅 <b>תאריך נוכחי:</b> {_html.escape(date_display)}\n"
            f"🕐 <b>שעה נוכחית:</b> {_html.escape(appt.get('preferred_time', ''))}\n\n"
            f"📅 מה <b>התאריך החדש</b> שמתאים לכם?\n"
            f"(למשל: מחר, יום ראשון, 15/03)"
        )
        context.user_data["reschedule_appt_id"] = appt_id
        context.user_data["reschedule_service"] = appt.get("service", "")
        context.user_data["reschedule_state"] = "date"
        # מעדיפים את המשך שאושר בפועל לתור הזה (אם קיים), אחרת ברירת מחדל גלובלית
        context.user_data["reschedule_service_duration"] = (
            db.resolve_appointment_duration_minutes(appt)
        )
        db.save_message(user_id, display_name, "assistant", response)
        await query.edit_message_text(response, parse_mode="HTML")
        return

    # אישור שינוי
    if data.startswith("reschedule_confirm_yes_"):
        appt_id = int(data.replace("reschedule_confirm_yes_", ""))
        new_date = context.user_data.get("reschedule_date", "")
        new_time = context.user_data.get("reschedule_time", "")
        service = context.user_data.get("reschedule_service", "")

        updated = db.update_appointment_and_sync(
            appt_id, user_id,
            preferred_date=new_date,
            preferred_time=new_time,
        )
        if updated:
            date_display = _format_date_display(new_date)
            response = (
                f"התור עודכן בהצלחה ✅\n\n"
                f"📋 <b>שירות:</b> {_html.escape(service)}\n"
                f"📅 <b>תאריך חדש:</b> {_html.escape(date_display)}\n"
                f"🕐 <b>שעה חדשה:</b> {_html.escape(new_time)}"
            )
            # התראה לבעל העסק
            handle = _tg_handle(telegram_username) or "(ללא שם משתמש)"
            panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
            notification = (
                f"🔄 שינוי תור #{appt_id}\n\n"
                f"לקוח: {display_name}\n"
                f"יוזר: {handle}\n"
                f"שירות: {service}\n"
                f"תאריך חדש: {date_display}\n"
                f"שעה חדשה: {new_time}"
                f"{panel_link}"
            )
            try:
                await _notify_owner(context, notification)
            except Exception:
                logger.error("שגיאה בהתראת בעל העסק על שינוי תור #%s", appt_id, exc_info=True)
        else:
            response = (
                "לא הצלחנו לעדכן את התור — ייתכן שהסטטוס שלו השתנה. 🤔\n"
                "נסו שוב, או לחצו על <b>👤 דברו עם נציג</b> למטה לעזרה."
            )
        _clear_reschedule_state(context)
        db.save_message(user_id, display_name, "assistant", response)
        await query.edit_message_text(response, parse_mode="HTML")
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="👇",
            reply_markup=_get_main_keyboard(update),
        )
        return

    # ביטול (reschedule_no / reschedule_confirm_no)
    _clear_reschedule_state(context)
    response = "בסדר, התור נשאר ללא שינוי! 👍\nאיך עוד אפשר לעזור?"
    db.save_message(user_id, display_name, "assistant", response)
    await query.edit_message_text(response)
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text="👇",
        reply_markup=_get_main_keyboard(update),
    )


async def _handle_reschedule_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    state: str, text: str,
):
    """טיפול בקלט טקסט חופשי בזמן reschedule flow (תאריך/שעה)."""
    user_id, display_name, _ = _get_user_info(update)

    # ביטול
    if text.strip().lower() in ("ביטול", "cancel", "בטל", "/cancel"):
        _clear_reschedule_state(context)
        await update.message.reply_text(
            "בסדר, התור נשאר ללא שינוי! 👍\nאיך עוד אפשר לעזור?",
            reply_markup=_get_main_keyboard(update),
        )
        return

    if state == "date":
        normalized = normalize_date(text)
        if normalized is None:
            await _reply_html_safe(
                update.message,
                "🤔 לא הצלחתי לזהות תאריך.\n\n"
                "אפשר לכתוב למשל:\n"
                "• <b>מחר</b> / <b>מחרתיים</b>\n"
                "• <b>יום ראשון</b> / <b>ביום שלישי</b>\n"
                "• <b>15/03</b> / <b>14 במרץ</b>\n\n"
                "הקלידו /cancel כדי לחזור.",
            )
            return

        # בדיקת זמינות ביומן Google
        service_duration = context.user_data.get("reschedule_service_duration", 60)
        available_slots_text = ""
        no_slots = False
        try:
            from google_calendar import is_connected, get_available_slots
            if is_connected():
                from datetime import date as _date_type
                target = _date_type.fromisoformat(normalized)
                buf_min = db.get_auto_booking_buffer_minutes()
                slots = get_available_slots(
                    target, service_duration_minutes=service_duration,
                    buffer_after_event_minutes=buf_min,
                )
                if slots:
                    slots_str = " | ".join(f"<b>{s}</b>" for s in slots)
                    available_slots_text = f"\n\n🟢 שעות פנויות: {slots_str}"
                else:
                    no_slots = True
        except ImportError:
            pass
        except Exception:
            logger.error("שגיאה בבדיקת זמינות Google Calendar (reschedule)", exc_info=True)

        if no_slots:
            await _reply_html_safe(
                update.message,
                f"📅 תאריך: <b>{_format_date_display(normalized)}</b>\n\n"
                "🔴 אין שעות פנויות בתאריך זה.\n"
                "אנא כתבו <b>תאריך אחר</b>, או הקלידו /cancel כדי לחזור.",
            )
            return

        context.user_data["reschedule_date"] = normalized
        context.user_data["reschedule_state"] = "time"
        await _reply_html_safe(
            update.message,
            f"📅 תאריך חדש: <b>{_format_date_display(normalized)}</b>{available_slots_text}\n\n"
            "🕐 איזו <b>שעה</b> מתאימה לכם?\n"
            "(לדוגמה: 10:00, אחר הצהריים, 14:00)\n\n"
            "הקלידו /cancel כדי לחזור.",
        )
        return

    if state == "time":
        new_time = text.strip()
        context.user_data["reschedule_time"] = new_time
        context.user_data["reschedule_state"] = "confirm"

        appt_id = context.user_data.get("reschedule_appt_id")
        service = _html.escape(context.user_data.get("reschedule_service", ""))
        date_display = _html.escape(_format_date_display(context.user_data.get("reschedule_date", "")))

        confirm_text = (
            f"🔄 <b>סיכום שינוי תור:</b>\n\n"
            f"📋 שירות: {service}\n"
            f"📅 תאריך חדש: {date_display}\n"
            f"🕐 שעה חדשה: {_html.escape(new_time)}\n\n"
            f"לאשר את השינוי?"
        )
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("כן, לשנות", callback_data=f"reschedule_confirm_yes_{appt_id}"),
                InlineKeyboardButton("לא, ביטול", callback_data="reschedule_confirm_no"),
            ]
        ])
        await _reply_html_safe(update.message, confirm_text, reply_markup=confirm_kb)
        return

    if state == "confirm":
        # בשלב אישור — צריך ללחוץ על הכפתורים, לא לכתוב טקסט
        await update.message.reply_text(
            "אנא לחצו על אחד הכפתורים למעלה — <b>כן, לשנות</b> או <b>לא, ביטול</b>.",
            parse_mode="HTML",
        )
        return

    # state לא מוכר — ניקוי
    _clear_reschedule_state(context)


# ─── Referral System (מערכת הפניות) ──────────────────────────────────────

async def _maybe_send_referral_code(update: Update, user_id: str):
    """שליחת קוד הפניה אם המשתמש עדיין לא קיבל אחד.

    נקרא אחרי אישור תור או לאחר מעורבות גבוהה.
    הטקסט מגיע מ-referral_service (מקור אמת יחיד לבוט ולאדמין).
    נעילה אטומית ו-rollback בכישלון — כולל כשלון שקט (message=None).
    """
    from ai_chatbot.referral_service import get_referral_message_text

    code = db.generate_referral_code(user_id)
    if not code:
        return

    if not db.mark_referral_code_as_sent(user_id):
        return

    text = get_referral_message_text(code)
    success = False
    try:
        result = await _reply_html_safe(update.message, text)
        success = result is not None
    except Exception:
        logger.error("Exception sending referral code to user %s", user_id, exc_info=True)

    if not success:
        db.unmark_referral_code_sent(user_id)
        logger.error("Failed to send referral code to user %s, flag reset", user_id)


async def _check_high_engagement_referral(update: Update, user_id: str):
    """בדיקת מעורבות גבוהה — שליחת קוד הפניה אם המשתמש מאוד פעיל.

    תנאים (אחד מהם מספיק):
    - 10+ הודעות ב-30 הדקות האחרונות
    - 20+ הודעות ביום האחרון
    """
    # בדיקה שמערכת ההפניות מופעלת
    if not db.get_bot_settings().get("referral_enabled", 0):
        return

    # אם כבר נשלח קוד — לא צריך לבדוק
    if db.is_referral_code_sent(user_id):
        return

    if db.check_high_engagement(user_id):
        await _maybe_send_referral_code(update, user_id)


async def _referral_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """לוגיקת שחזור קוד הפניה — ללא rate limit guard."""
    from ai_chatbot.referral_service import get_referral_message_text

    user_id, _display_name, _tg_username = _get_user_info(update)
    code = db.get_user_referral_code(user_id)

    if code:
        text = get_referral_message_text(code)
    else:
        text = (
            "🎁 עדיין לא קיבלתם קוד הפניה.\n\n"
            "קוד הפניה נשלח לאחר אישור תור או מעורבות גבוהה — "
            "ממשיכו להשתמש בבוט ותקבלו אחד בקרוב!"
        )

    await _reply_html_safe(update.message, text, reply_markup=_get_main_keyboard(update))


# סדר: block → rate_limit → live_chat → consent.
# rate_limit לפני consent — מונע שפם של מסך ההסכמה.
# live_chat לפני consent — בשיחה חיה הבוט שקט; consent_guard לא יציג מסך
# הסכמה גם אחרי bump של CURRENT_CONSENT_VERSION.
@block_guard
@rate_limit_guard
@live_chat_guard
@consent_guard
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /referral — שחזור קוד הפניה קיים או הסבר שעדיין אין."""
    return await _referral_core(update, context)


async def _referral_skip_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ניתוב פנימי — מדלג על rate_limit (הקורא כבר עבר אותו)."""
    return await _referral_core(update, context)


# ─── Follow-up Question Callback ─────────────────────────────────────────────

async def follow_up_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בלחיצה על כפתור שאלת המשך — שולח את השאלה כאילו המשתמש הקליד אותה."""
    query = update.callback_query
    await query.answer()

    from ai_chatbot.live_chat_service import LiveChatService
    user = update.effective_user
    user_id = str(user.id)

    # בדיקת חסימה
    if db.is_user_blocked(user_id):
        return

    if LiveChatService.is_active(user_id):
        return

    _, display_name, telegram_username = _get_user_info(update)

    # בדיקת rate limit — שאלות המשך צורכות קריאת LLM כמו הודעה רגילה
    limit_msg = check_rate_limit(user_id)
    if limit_msg is not None:
        try:
            await query.edit_message_text(limit_msg, parse_mode="HTML")
        except Exception:
            await query.edit_message_text(limit_msg)
        return

    cb_data = query.data
    # שליפת טקסט השאלה מ-bot_data (נתונים in-memory — נמחקים ברסטרט)
    question_text = context.bot_data.pop(cb_data, None)
    if not question_text:
        logger.warning("follow_up_callback: missing question for %s", cb_data)
        try:
            await query.edit_message_text("⏳ השאלה כבר לא זמינה. אפשר לשאול אותי ישירות!")
        except Exception as e:
            logger.error("Failed to edit expired follow-up message: %s", e)
        return

    # רישום rate limit רק אחרי שוידאנו שהשאלה קיימת
    record_message(user_id)

    chat_id = update.effective_chat.id

    # עדכון ההודעה המקורית — להראות איזו שאלה נבחרה
    try:
        await query.edit_message_text(f"💡 {question_text}")
    except Exception as e:
        logger.error("Failed to edit follow-up message: %s", e)

    # שימוש בצינור RAG המשותף
    await _handle_rag_query(
        update, context,
        user_id=user_id,
        display_name=display_name,
        telegram_username=telegram_username,
        user_message=question_text,
        query=question_text,
        handoff_reason=f"הלקוח שאל שאלת המשך: {question_text}",
        chat_id=chat_id,
    )


# ─── Error Handler ───────────────────────────────────────────────────────────

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors gracefully."""
    logger.error("Update %s caused error: %s", update, context.error)
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "מצטערים, משהו השתבש. אנא נסו שוב או לחצו על "
            "'👤 דברו עם נציג' כדי לדבר עם בעל העסק.",
            reply_markup=_get_main_keyboard(update)
        )
