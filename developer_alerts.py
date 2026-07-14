"""
Developer Alerts — שליחת התראות אקטיביות למפתח (ספק ה-SaaS).

המודול שולח הודעות טלגרם ל-`DEVELOPER_TELEGRAM_CHAT_ID` כדי לסמן בעיות
שדורשות תשומת לב ידנית של המפתח (למשל channel mismatch בין החבילה
המוגדרת לבין הערוץ הפעיל בפועל).

התראות לא קורסות את האפליקציה לעולם — כשל בשליחה רושם לוג בלבד.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)


def _get_deployment_label() -> str:
    """
    מזהה ייחודי של הפריסה להצגה בהתראה. עדיפות:
    1. DEPLOYMENT_NAME מפורש (בקונפיג).
    2. RENDER_SERVICE_NAME (Render מספק אוטומטית).
    3. BUSINESS_NAME (שם העסק — מזהה לוגי לאדם).
    4. hostname (fallback אחרון).
    """
    # קוראים דרך המודול כדי לתפוס עדכוני env בזמן ריצה
    import ai_chatbot.config as _cfg

    label = (
        getattr(_cfg, "DEPLOYMENT_NAME", "")
        or os.getenv("RENDER_SERVICE_NAME", "")
        or getattr(_cfg, "BUSINESS_NAME", "")
        or ""
    ).strip()
    if label:
        return label
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-deployment"


def _get_developer_chat_id() -> str:
    """החזרת chat_id של המפתח, או מחרוזת ריקה אם לא מוגדר."""
    import ai_chatbot.config as _cfg
    return (getattr(_cfg, "DEVELOPER_TELEGRAM_CHAT_ID", "") or "").strip()


def _send_telegram(chat_id: str, text: str) -> bool:
    """
    שליחה סינכרונית של הודעת טלגרם דרך HTTP API. מחזיר True בהצלחה.
    משתמש ב-live_chat_service.send_telegram_message כדי לא לשכפל קוד.
    """
    try:
        from ai_chatbot.live_chat_service import send_telegram_message
        return bool(send_telegram_message(chat_id, text))
    except Exception:
        logger.error("developer_alerts: send_telegram_message raised", exc_info=True)
        return False


def notify_developer(message: str, *, level: str = "warning") -> bool:
    """
    שליחת התראה כללית למפתח. לא זורק לעולם — מחזיר True בהצלחה,
    False אם לא נשלח (לא מוגדר chat_id, או כשל בשליחה).
    """
    chat_id = _get_developer_chat_id()
    if not chat_id:
        logger.info(
            "developer_alerts: DEVELOPER_TELEGRAM_CHAT_ID not configured — "
            "skipping alert (level=%s, msg=%r)",
            level, message[:120],
        )
        return False

    icon = {"warning": "⚠️", "error": "🛑", "info": "ℹ️"}.get(level, "🔔")
    deployment = _get_deployment_label()
    text = f"{icon} *Developer alert* — `{deployment}`\n\n{message}"
    ok = _send_telegram(chat_id, text)
    if not ok:
        logger.error(
            "developer_alerts: failed to send notification (level=%s, msg=%r)",
            level, message[:120],
        )
    return ok


def detect_active_channel() -> Optional[str]:
    """
    זיהוי הערוץ הפעיל בפועל — בודק אילו ENV vars מוגדרים.

    משמש כ-fallback לתצוגה בלבד (preview של הפרומפט / מסך "החבילה שלי")
    עבור ה-tenant של ברירת המחדל, שאין לו subscription.channel מנוהל.
    הערוץ פר-tenant נקבע ב-feature_flags.get_channel — לא כאן.

    מחזיר:
    - 'telegram'   — רק TELEGRAM_BOT_TOKEN מוגדר.
    - 'whatsapp'   — רק Twilio מוגדר במלואו.
    - None         — אף אחד מוגדר, או **שניהם מוגדרים יחד** (dual-channel
                     לסביבות בדיקה).
    """
    import ai_chatbot.config as _cfg

    twilio_configured = all([
        getattr(_cfg, "TWILIO_ACCOUNT_SID", ""),
        getattr(_cfg, "TWILIO_AUTH_TOKEN", ""),
        getattr(_cfg, "TWILIO_WHATSAPP_NUMBER", ""),
    ])
    telegram_configured = bool(getattr(_cfg, "TELEGRAM_BOT_TOKEN", ""))

    if twilio_configured and not telegram_configured:
        return "whatsapp"
    if telegram_configured and not twilio_configured:
        return "telegram"
    # שניהם מוגדרים (dual-channel לבדיקות) או אף אחד — לא קובעים ערוץ
    return None
