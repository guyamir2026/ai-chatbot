"""
LiveChatService — centralized service for managing live chat sessions.

Consolidates all live chat logic (start, end, send, is_active) that was
previously scattered across bot handlers and admin endpoints.  Enforces
the BOT_ACTIVE ↔ LIVE_CHAT state transitions with idempotent guards.

See: https://github.com/amirbiron/ai-business-bot/issues/49
"""

import logging
from functools import wraps
from typing import Optional

import requests as http_requests
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ai_chatbot import database as db
from ai_chatbot.config import (
    TELEGRAM_BOT_TOKEN,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
)

logger = logging.getLogger(__name__)


# ── Messaging Helpers ────────────────────────────────────────────────────────


def send_telegram_message(chat_id: str, text: str, parse_mode: str = "") -> bool:
    """Send a message to a Telegram user via the Bot HTTP API."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        payload: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, e)
        return False


def send_telegram_document(chat_id: str, file_data: bytes, filename: str, caption: str = "") -> bool:
    """שליחת קובץ כמסמך למשתמש טלגרם דרך Bot HTTP API."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        data: dict = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        files = {"document": (filename, file_data, "text/calendar")}
        resp = http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data=data,
            files=files,
            timeout=15,
        )
        return resp.ok
    except Exception as e:
        logger.error("Failed to send Telegram document to %s: %s", chat_id, e)
        return False


def send_whatsapp_message(chat_id: str, text: str) -> bool:
    """שליחת הודעת WhatsApp דרך Twilio API (סינכרוני)."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
        return False
    try:
        from messaging.whatsapp_sender import send_whatsapp
        send_whatsapp(chat_id, text)
        return True
    except Exception as e:
        logger.error("Failed to send WhatsApp message to %s: %s", chat_id, e)
        return False


def send_meta_message_by_user_id(user_id: str, text: str) -> bool:
    """שליחת הודעה למשתמש מטא (Instagram DM / Messenger) מתוך האדמין.

    שונה מ-`_send_meta_response` של ה-webhook: שם asset_id מועבר
    מבחוץ (מתוך entry.id של ה-event); כאן הקורא הוא האדמין שאין לו
    גישה ל-event המקורי, אז אנחנו שולפים את ה-asset_id מטבלת users
    (provider_asset_id) ועל סמך זה את ה-credentials.

    מחזיר True אם השליחה הצליחה, False אחרת. מתחזה לשליחה ה"רגילה" של
    ערוצים אחרים כדי ש-`send_message_by_channel` יוכל לקרוא לזה בלי
    מעבר מיוחד.
    """
    try:
        from messaging.meta_adapter import parse_channel, to_provider_recipient
        from messaging.meta_sender import send_meta_message

        channel = parse_channel(user_id)
        recipient = to_provider_recipient(user_id)
    except Exception:
        logger.error("send_meta_message_by_user_id: user_id לא תקף %s", user_id)
        return False

    user_row = db.get_user_provider_info(user_id)
    if not user_row or not user_row.get("provider_asset_id"):
        logger.error(
            "send_meta_message_by_user_id: אין asset_id ב-users עבור %s",
            user_id,
        )
        return False
    asset_id = user_row["provider_asset_id"]

    # ב-IG ה-asset_id הוא IGBA; ב-Messenger הוא page_id. שני המקרים
    # מובילים לאותה רשומת credentials של עמוד הפייסבוק.
    try:
        if channel == "meta_ig":
            creds = db.get_meta_credentials_by_ig_account(asset_id)
        else:
            creds = db.get_meta_credentials_by_page_id(asset_id)
    except Exception:
        logger.exception("שליפת credentials נכשלה ל-asset=%s", asset_id)
        return False

    if not creds:
        logger.error(
            "send_meta_message_by_user_id: אין credentials ל-channel=%s asset=%s",
            channel, asset_id,
        )
        return False

    try:
        send_meta_message(recipient, text, creds["access_token"])
        return True
    except Exception as e:
        logger.error("Failed to send Meta message to %s: %s", user_id, e)
        return False


def send_message_by_channel(chat_id: str, text: str, channel: str = "telegram") -> bool:
    """שליחת הודעה בערוץ המתאים — Telegram / WhatsApp / Meta (IG, Messenger).

    chat_id במטא הוא ה-`user_id` הפנימי (`meta_ig:<igsid>` / `meta_msg:<psid>`)
    שמכיל את ה-prefix; הפונקציה חולצת את ה-asset וה-credentials דרך DB.
    """
    if channel == "whatsapp":
        return send_whatsapp_message(chat_id, text)
    if channel in ("meta_ig", "meta_msg"):
        return send_meta_message_by_user_id(chat_id, text)
    return send_telegram_message(chat_id, text)


def _get_customer_username(user_id: str) -> str:
    """Look up the customer's display name for a given user_id."""
    return db.get_username_for_user(user_id) or user_id


# ── Service Layer ────────────────────────────────────────────────────────────


class LiveChatService:
    """Centralized service for all live chat operations.

    All state queries and transitions go through this class so that
    edge cases (duplicate starts, Telegram failures, username lookups)
    are handled in a single place.
    """

    # Timeout — שיחה חיה שלא עודכנה במשך שעתיים נסגרת אוטומטית (LC2)
    SESSION_TIMEOUT_MINUTES = 120

    # ── State Queries ────────────────────────────────────────────────

    @staticmethod
    def is_active(user_id: str) -> bool:
        """Check whether the user is currently in a live chat session.

        סוגר אוטומטית sessions שחרגו מה-timeout (לפי פעילות אחרונה, לא תחילת השיחה).
        """
        session = db.get_active_live_chat(user_id)
        if not session:
            return False
        # בדיקת timeout — אם עברו יותר מ-SESSION_TIMEOUT_MINUTES מאז הפעילות האחרונה
        try:
            from datetime import datetime, timezone, timedelta
            # עדיפות ל-updated_at (פעילות אחרונה); fallback ל-started_at (DB ישן ללא מיגרציה)
            last_activity_str = session.get("updated_at") or session["started_at"]
            last_activity = datetime.strptime(last_activity_str, "%Y-%m-%d %H:%M:%S")
            last_activity = last_activity.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_activity > timedelta(minutes=LiveChatService.SESSION_TIMEOUT_MINUTES):
                logger.info("Live chat session for user %s timed out after %d minutes of inactivity", user_id, LiveChatService.SESSION_TIMEOUT_MINUTES)
                db.end_live_chat(user_id)
                return False
        except (KeyError, ValueError, TypeError):
            pass
        return True

    @staticmethod
    def get_session(user_id: str) -> Optional[dict]:
        """Return the active session record, or None."""
        return db.get_active_live_chat(user_id)

    @staticmethod
    def get_all_active() -> list[dict]:
        """Return all currently active live chat sessions."""
        return db.get_all_active_live_chats()

    @staticmethod
    def count_active() -> int:
        """Count currently active live chat sessions."""
        return db.count_active_live_chats()

    @staticmethod
    def get_customer_username(user_id: str) -> str:
        """Look up the customer's display name."""
        return _get_customer_username(user_id)

    # ── State Transitions ────────────────────────────────────────────

    @staticmethod
    def _get_session_channel(user_id: str) -> str:
        """קבלת ערוץ ה-session הפעיל (telegram/whatsapp)."""
        session = db.get_active_live_chat(user_id)
        if session:
            return session.get("channel", "telegram") or "telegram"
        return "telegram"

    @staticmethod
    def start(user_id: str, channel: str = "telegram") -> tuple[bool, str]:
        """Transition BOT_ACTIVE → LIVE_CHAT.

        Idempotent — returns early if already active.
        Handles duplicate starts, username lookup, notification.

        Returns:
            (sent, status) where status is one of:
            "already_active", "started", "send_failed".
        """
        if db.is_live_chat_active(user_id):
            # גם אם השיחה כבר פעילה — לסגור בקשות נציג ממתינות (edge case)
            db.handle_pending_requests_for_user(user_id)
            return True, "already_active"

        username = _get_customer_username(user_id)
        db.start_live_chat(user_id, username, channel=channel)
        # סגירת בקשות נציג ממתינות — הנציג כבר נכנס לשיחה
        db.handle_pending_requests_for_user(user_id)

        notify_msg = "👤 בעל העסק הצטרף לשיחה. כעת תקבלו מענה ישיר."
        sent = send_message_by_channel(user_id, notify_msg, channel)
        if sent:
            db.save_message(user_id, username, "assistant", notify_msg, channel=channel)

        return sent, "started" if sent else "send_failed"

    @staticmethod
    def end(user_id: str) -> tuple[bool, str]:
        """Transition LIVE_CHAT → BOT_ACTIVE.

        Idempotent — returns early if not active.
        Sends the "bot is back" notification *before* deactivating so the
        bot stays suspended until the customer receives the message.

        Returns:
            (sent, status) where status is one of:
            "already_ended", "ended", "send_failed".
        """
        if not db.is_live_chat_active(user_id):
            return True, "already_ended"

        channel = LiveChatService._get_session_channel(user_id)
        username = _get_customer_username(user_id)
        end_msg = "🤖 הבוט חזר לנהל את השיחה."
        sent = send_message_by_channel(user_id, end_msg, channel)
        if sent:
            db.save_message(user_id, username, "assistant", end_msg, channel=channel)

        # Deactivate AFTER sending notification
        db.end_live_chat(user_id)

        return sent, "ended" if sent else "send_failed"

    @staticmethod
    def send(user_id: str, message_text: str) -> tuple[bool, str]:
        """Send a message from the human agent to the customer.

        Guards: session must be active, message must be non-empty.

        Returns:
            (success, status) where status is one of:
            "session_ended", "empty_message", "sent", "send_failed".
        """
        if not db.is_live_chat_active(user_id):
            return False, "session_ended"

        if not message_text or not message_text.strip():
            return False, "empty_message"

        channel = LiveChatService._get_session_channel(user_id)
        sent = send_message_by_channel(user_id, message_text, channel)
        if not sent:
            return False, "send_failed"

        username = _get_customer_username(user_id)
        db.save_message(user_id, username, "assistant", message_text, channel=channel)
        # עדכון פעילות אחרונה — גם תשובת נציג מאריכה את ה-session
        db.touch_live_chat(user_id)

        return True, "sent"

    # ── Lifecycle ────────────────────────────────────────────────────

    @staticmethod
    def cleanup_expired(max_hours: int = 4) -> int:
        """סגירת sessions שפתוחים יותר מ-max_hours ללא פעילות.

        נקרא מ-job_queue בצורה תקופתית — סוגר sessions שנשכחו פתוחים.
        """
        return db.end_expired_live_chats(max_hours)

    @staticmethod
    def cleanup_stale():
        """Deactivate sessions left over from a previous bot run."""
        db.cleanup_stale_live_chats()


# ── Bot-Layer Decorators (Middleware) ────────────────────────────────────────


def _get_user_info(update: Update) -> tuple[str, str]:
    """Extract user_id and display_name from an Update."""
    user = update.effective_user
    user_id = str(user.id)
    display_name = user.full_name or (
        f"@{user.username}" if user.username else f"User {user.id}"
    )
    return user_id, display_name


def live_chat_guard(handler):
    """Bot middleware decorator: if live chat is active, save the message and return silently.

    Use on regular bot handlers (commands, button handlers, message handler).
    The bot "goes silent" and lets the human agent handle the conversation.
    """

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id, display_name = _get_user_info(update)
        if LiveChatService.is_active(user_id):
            # Save the user's message for the human agent's context
            message_text = None
            if update.message and update.message.text:
                message_text = update.message.text
                db.save_message(user_id, display_name, "user", message_text)
            # עדכון פעילות אחרונה — מונע timeout בזמן שהלקוח פעיל
            db.touch_live_chat(user_id)
            # התראת Web Push לבעל העסק — עובד גם כשהדשבורד סגור.
            # נכשל בשקט אם VAPID לא מוגדר או אין מנויים.
            if message_text:
                try:
                    from notifications.push_service import notify_live_chat_message
                    notify_live_chat_message(user_id, display_name, message_text)
                except Exception:
                    logger.exception("notify_live_chat_message failed (Telegram live chat)")
            return
        return await handler(update, context)

    return wrapper


def live_chat_guard_booking(handler):
    """Bot middleware decorator for booking conversation handlers.

    Like ``live_chat_guard`` but returns ``ConversationHandler.END`` and
    clears booking state so the conversation handler exits cleanly.
    """

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id, display_name = _get_user_info(update)
        if LiveChatService.is_active(user_id):
            message_text = None
            if update.message and update.message.text:
                message_text = update.message.text
                db.save_message(user_id, display_name, "user", message_text)
            # עדכון פעילות אחרונה — מונע timeout בזמן שהלקוח פעיל
            db.touch_live_chat(user_id)
            # התראת Web Push לבעל העסק — עובד גם כשהדשבורד סגור.
            if message_text:
                try:
                    from notifications.push_service import notify_live_chat_message
                    notify_live_chat_message(user_id, display_name, message_text)
                except Exception:
                    logger.exception("notify_live_chat_message failed (booking guard)")
            context.user_data.clear()
            return ConversationHandler.END
        return await handler(update, context)

    return wrapper
