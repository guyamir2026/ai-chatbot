"""
WhatsApp Sender — פונקציית שליחה משותפת לכל הערוצים שצריכים לשלוח הודעות WhatsApp.

מרכז את לוגיקת יצירת Twilio Client + format_message + messages.create
במקום אחד, כדי למנוע שכפול בין webhook, broadcast_service, ו-live_chat_service.

לשליחת הודעות אינטראקטיביות (כפתורים/רשימות) — ראו messaging/whatsapp_templates.py.
"""

import logging

from messaging.formatter import format_message

logger = logging.getLogger(__name__)

# Twilio Client — נוצר פעם אחת (lazy) וממוחזר לכל הקריאות
_twilio_client = None


def _get_twilio_client():
    """יצירת / קבלת Twilio Client singleton."""
    global _twilio_client
    if _twilio_client is None:
        from ai_chatbot.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
        from twilio.rest import Client
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def _is_phone_number(value: str) -> bool:
    """בדיקה האם ערך הוא מספר טלפון (ולא BSUID).

    מספר טלפון: מתחיל ב-'+' או מורכב מספרות בלבד (9+ תווים).
    BSUID: פורמט CC.ID — קוד מדינה ISO alpha-2 + נקודה + עד 128 תווים אלפאנומריים.
    דוגמה: IL.ABCdef123 (ישראל), US.13491208655302741918 (ארה"ב).
    """
    if value.startswith("+"):
        return True
    if value.isdigit() and len(value) >= 9:
        return True
    return False


def send_whatsapp(to_number: str, text: str, media_url: str | None = None) -> None:
    """שליחת הודעת WhatsApp מפורמטת דרך Twilio (סינכרוני).

    משמש את webhook, broadcast_service, ו-live_chat_service.
    מבצע format_message לפני השליחה.

    אם to_number הוא BSUID (לא מספר טלפון) — מבצע reverse lookup
    בטבלת user_identities כדי למצוא את מספר הטלפון לשליחה.

    media_url — אם מסופק, מצורף כקובץ מדיה (ICS/PDF/תמונה וכו') להודעה.
    Twilio מורידה את הקובץ מ-URL ציבורי (חייב HTTPS בפרודקשן).

    Raises:
        Exception: כל שגיאת Twilio — הקורא אחראי על טיפול בשגיאות.
        ValueError: אם לא ניתן למצוא מספר טלפון לשליחה.
    """
    from ai_chatbot.config import TWILIO_WHATSAPP_NUMBER, DEMO_MODE

    # ── מצב דמו — לא שולחים בפועל ל-Twilio ──
    # ה-deployment כולו הוא דמו (ראה docs/demo-mode-spec.md). המטרה: אפס
    # עלות Twilio, ולמנוע שליחת הודעות אמיתיות גם אם ה-middleware של
    # session הדמו פוספס (background jobs, follow-ups, broadcast).
    if DEMO_MODE:
        formatted = format_message(text, "whatsapp")
        logger.info(
            "DEMO_MODE: skipping WhatsApp send | to=%s chars=%d media=%s",
            to_number, len(formatted), bool(media_url),
        )
        return

    # reverse lookup — אם ה-user_id הוא BSUID (לא מספר טלפון), מחפשים טלפון.
    # אם אין טלפון — שולחים ישירות ל-BSUID (Twilio תומך ב-to=whatsapp:CC.BSUID).
    send_to = to_number
    if not _is_phone_number(to_number):
        from utils.user_identity import get_whatsapp_send_address
        resolved_phone = get_whatsapp_send_address(to_number)
        send_to = resolved_phone or to_number

    formatted = format_message(text, "whatsapp")
    client = _get_twilio_client()
    kwargs = {
        "body": formatted,
        "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
        "to": f"whatsapp:{send_to}",
    }
    if media_url:
        kwargs["media_url"] = [media_url]
    # ── לוג אבחון לבאג קציצה (ראה docs/truncation_investigation.md) ──
    # bytes חשוב כי עברית UTF-8 = 2 בייטים לתו, וייתכן ש-Twilio בודק bytes
    # ולא chars. אם chars<1600 אבל bytes>1600 — חשד מהותי לקציצה.
    formatted_bytes = len(formatted.encode("utf-8"))
    logger.info(
        "WA send diag: chars=%d utf8_bytes=%d to=%s tail=%r",
        len(formatted), formatted_bytes, send_to,
        formatted[-60:] if len(formatted) > 60 else formatted,
    )
    message = client.messages.create(**kwargs)
    # תגובת Twilio — sid לאיתור ב-Twilio Console; num_segments מאפיין
    # פיצול הודעה (אם >1, יתכן שחלק שני לא הגיע ולכן הלקוח רואה חיתוך).
    try:
        logger.info(
            "WA send response: sid=%s status=%s num_segments=%s body_len=%d",
            getattr(message, "sid", "?"),
            getattr(message, "status", "?"),
            getattr(message, "num_segments", "?"),
            len(formatted),
        )
    except Exception:
        pass


def notify_owner_whatsapp(text: str) -> bool:
    """שליחת התראה לבעל העסק דרך WhatsApp.

    שולח רק אם OWNER_WHATSAPP_NUMBER מוגדר.
    מחזיר True אם ההודעה נשלחה בפועל, False אם לא (חסר מספר / שגיאת שליחה).
    קוראים שצריכים לדעת אם ההתראה הגיעה (למשל owner_alert_sent flag) חייבים
    לבדוק את הערך — אחרת התראות חשובות עלולות להיסמן כנשלחו בלי שהגיעו.
    """
    from ai_chatbot.config import OWNER_WHATSAPP_NUMBER
    if not OWNER_WHATSAPP_NUMBER:
        logger.debug("OWNER_WHATSAPP_NUMBER לא מוגדר — דילוג על התראת WhatsApp")
        return False
    try:
        send_whatsapp(OWNER_WHATSAPP_NUMBER, text)
        return True
    except Exception as e:
        logger.error("כשל בשליחת התראת WhatsApp לבעל העסק: %s", e)
        return False
