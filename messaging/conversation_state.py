"""
WhatsApp Conversation State — ניהול state machine לשיחות WhatsApp.

ב-Telegram יש ConversationHandler מובנה עם states.
ב-WhatsApp אין מנגנון כזה — לכן מנהלים state ב-dict בזיכרון.

ה-state שומר לכל user_id:
- state: שלב נוכחי ("booking_service", "booking_date", "booking_time", "booking_confirm")
- data: נתונים שנאספו עד כה (שירות, תאריך, שעה)
- created_at: זמן יצירה — לניקוי sessions ישנים

Timeout: session שלא עודכן 30 דקות נמחק אוטומטית.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# timeout — 30 דקות של חוסר פעילות
_SESSION_TIMEOUT = 30 * 60

# state storage — in-memory dict (מספיק לשימוש נוכחי, אפשר להעביר ל-DB בהמשך).
# המפתח: (tenant, user_id) — אותו מספר טלפון מול שני עסקים הוא שתי שיחות
# נפרדות (multi-tenant שלב 2).
_sessions: dict[tuple[str, str], dict] = {}


def _k(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)

# שלבי ה-booking flow
STATE_BOOKING_SERVICE = "booking_service"
STATE_BOOKING_DATE = "booking_date"
STATE_BOOKING_TIME = "booking_time"
STATE_BOOKING_CONFIRM = "booking_confirm"

# שלבי ביטול תור
STATE_CANCEL_SELECT = "cancel_select"    # בחירת תור מרשימה (כשיש יותר מאחד)
STATE_CANCEL_CONFIRM = "cancel_confirm"  # ממתין לאישור מהלקוח

# שלבי שינוי תור (reschedule)
STATE_RESCHEDULE_SELECT = "reschedule_select"    # בחירת תור מרשימה
STATE_RESCHEDULE_DATE = "reschedule_date"        # בחירת תאריך חדש
STATE_RESCHEDULE_TIME = "reschedule_time"        # בחירת שעה חדשה
STATE_RESCHEDULE_CONFIRM = "reschedule_confirm"  # אישור שינוי


def get_state(user_id: str) -> Optional[dict]:
    """קבלת state נוכחי למשתמש. מחזיר None אם אין או שפג תוקף."""
    key = _k(user_id)
    session = _sessions.get(key)
    if session is None:
        return None
    # בדיקת timeout
    if time.time() - session.get("updated_at", 0) > _SESSION_TIMEOUT:
        logger.info("WhatsApp booking session expired for user %s", user_id)
        del _sessions[key]
        return None
    return session


def set_state(user_id: str, state: str, data: Optional[dict] = None) -> None:
    """הגדרת state חדש למשתמש."""
    now = time.time()
    key = _k(user_id)
    existing = _sessions.get(key)
    merged_data = {}
    if existing and existing.get("data"):
        merged_data.update(existing["data"])
    if data:
        merged_data.update(data)
    _sessions[key] = {
        "state": state,
        "data": merged_data,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }


def clear_state(user_id: str) -> None:
    """מחיקת state למשתמש (סיום/ביטול flow)."""
    _sessions.pop(_k(user_id), None)


def get_session_data(user_id: str, key: str, default=None):
    """קריאת ערך מתוך data של ה-session."""
    session = get_state(user_id)
    if session is None:
        return default
    return session.get("data", {}).get(key, default)


def cleanup_expired() -> int:
    """ניקוי sessions שפג תוקפם. מחזיר מספר sessions שנוקו."""
    now = time.time()
    # הניקוי גלובלי — עובר על sessions של כל ה-tenants (המפתח כולל tenant)
    expired = [
        key for key, s in _sessions.items()
        if now - s.get("updated_at", 0) > _SESSION_TIMEOUT
    ]
    for key in expired:
        del _sessions[key]
    if expired:
        logger.info("Cleaned up %d expired WhatsApp booking sessions", len(expired))
    return len(expired)
