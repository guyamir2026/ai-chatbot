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


def notify_channel_mismatch(
    plan: str,
    expected_channel: str,
    actual_channel: Optional[str],
) -> bool:
    """
    התראה על אי-התאמה בין הערוץ הצפוי בחבילה לבין הערוץ הפעיל בפועל.
    נקרא ב-startup ב-main.py.
    """
    actual_str = actual_channel if actual_channel else "(none detected)"
    msg = (
        f"Channel mismatch detected on startup\n"
        f"Configured plan: `{plan}` (expected channel: `{expected_channel}`)\n"
        f"Actual channel: `{actual_str}`\n\n"
        f"Action: עדכנו את ה-plan ב-/dev/subscription, או ה-env vars של הערוץ."
    )
    return notify_developer(msg, level="warning")


def detect_active_channel() -> Optional[str]:
    """
    זיהוי הערוץ הפעיל בפועל — בודק אילו ENV vars מוגדרים.

    מחזיר:
    - 'telegram'   — רק TELEGRAM_BOT_TOKEN מוגדר.
    - 'whatsapp'   — רק Twilio מוגדר במלואו.
    - None         — אף אחד מוגדר, או **שניהם מוגדרים יחד** (dual-channel
                     לסביבות בדיקה). המקרה הזה חוקי, ובמקרה כזה לא נייצר
                     התראת mismatch — actual_channel == None ↔ אין mismatch
                     ב-`check_and_alert_channel_mismatch`.
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


def check_and_alert_channel_mismatch() -> Optional[dict]:
    """
    מבצע את הבדיקה המלאה של channel mismatch:
    1. שולף את החבילה הנוכחית.
    2. מזהה את הערוץ הפעיל בפועל.
    3. אם יש mismatch — שולח התראה למפתח.

    מחזיר dict עם סיכום הבדיקה (שימושי לטסטים), או None אם לא ניתן
    לבצע (אין subscription / DB / config).
    לא זורק לעולם.
    """
    try:
        from ai_chatbot import feature_flags
        from ai_chatbot import plans_config
    except Exception:
        logger.error("developer_alerts: failed to import modules", exc_info=True)
        return None

    try:
        plan = feature_flags.get_current_plan()
        plan_def = plans_config.get_plan_definition(plan)
        expected_channel = plan_def.get("channel")
    except Exception:
        logger.error(
            "developer_alerts: failed to read current plan for mismatch check",
            exc_info=True,
        )
        return None

    actual_channel = detect_active_channel()

    is_mismatch = (
        expected_channel is not None
        and actual_channel is not None
        and actual_channel != expected_channel
    )

    summary = {
        "plan": plan,
        "expected_channel": expected_channel,
        "actual_channel": actual_channel,
        "is_mismatch": is_mismatch,
        "alert_sent": False,
    }

    if is_mismatch:
        logger.warning(
            "channel_mismatch: plan=%s expected=%s actual=%s",
            plan, expected_channel, actual_channel,
        )
        summary["alert_sent"] = notify_channel_mismatch(
            plan, expected_channel, actual_channel
        )
    else:
        logger.info(
            "channel_check: ok plan=%s channel=%s",
            plan, expected_channel,
        )

    return summary
