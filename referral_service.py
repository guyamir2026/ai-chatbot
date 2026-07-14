"""
שירות הפניות — לוגיקה משותפת לשליחת קוד הפניה.

מאחד את זרימת generate→mark→build-link→send→unmark
שמשמשת גם את הבוט וגם את פאנל האדמין, עם טקסט אחיד.
"""

import logging
from typing import Optional
from urllib.parse import quote

from ai_chatbot import database as db
from ai_chatbot.config import TELEGRAM_BOT_USERNAME, TWILIO_WHATSAPP_NUMBER
from utils.phone import to_wa_me_digits

logger = logging.getLogger(__name__)


def build_referral_link(code: str, channel: str = "telegram") -> str:
    """בניית לינק הפניה לפי הערוץ.

    Telegram: deep-link עם ?start=REF_XXX (מטופל ב-/start handler).
    WhatsApp: wa.me עם טקסט מוכן (החבר לוחץ → נפתחת שיחה עם הבוט וקוד מוכן לשליחה).
    Fallback: הקוד עצמו אם הקונפיג חסר.
    """
    if channel == "whatsapp":
        digits = to_wa_me_digits(TWILIO_WHATSAPP_NUMBER)
        if digits:
            return f"https://wa.me/{digits}?text={quote(code)}"
        return code
    if TELEGRAM_BOT_USERNAME:
        return f"https://telegram.me/{TELEGRAM_BOT_USERNAME}?start={code}"
    return code


def format_referral_period(days: int) -> str:
    """תיאור תקופת תוקף ידידותי בעברית."""
    if 28 <= days <= 31:
        return "לחודש"
    elif 56 <= days <= 62:
        return "לחודשיים"
    elif 84 <= days <= 93:
        return "ל-3 חודשים"
    elif days == 365:
        return "לשנה"
    return f"ל-{days} ימים"


def format_referral_discount(discount: float) -> str:
    """פורמט אחוז הנחה — שלם אם אין שבר."""
    return f"{int(discount)}%" if discount == int(discount) else f"{discount}%"


def get_referral_message_text(code: str, channel: str = "telegram") -> str:
    """טקסט הודעת ההפניה — מקור אמת יחיד לשני הנתיבים (בוט ואדמין).

    אחוז ההנחה ותקופת התוקף נקראים מהגדרות הבוט.
    הלינק מותאם לערוץ — Telegram deep-link או wa.me עם קוד מוכן לשליחה.
    """
    settings = db.get_bot_settings()
    discount_str = format_referral_discount(settings.get("referral_discount", 10.0))
    period = format_referral_period(settings.get("referral_validity_days", 60))

    link = build_referral_link(code, channel=channel)
    return (
        "🎁 רוצים לשתף עם חבר/ה?\n\n"
        f"שלחו להם את הלינק הזה:\n{link}\n\n"
        "כשהם יקבעו וישלימו תור — "
        f"גם אתם וגם הם תקבלו {discount_str} הנחה {period}!"
    )


def is_referral_enabled() -> bool:
    """בדיקה אם מערכת ההפניות מופעלת בהגדרות."""
    return bool(db.get_bot_settings().get("referral_enabled", 0))


def try_send_referral_code(user_id: str, send_fn, channel: str = "telegram") -> bool:
    """ניסיון אטומי לשלוח קוד הפניה למשתמש.

    send_fn(text: str) -> bool — פונקציית שליחה שמחזירה True בהצלחה.
    channel — קובע את פורמט הלינק שייכלל בטקסט (telegram/whatsapp).
    מחזיר True אם ההודעה נשלחה, False אחרת (כבוי / כבר נשלח / נכשל).
    אם השליחה נכשלת — הדגל מתאפס לניסיון חוזר עתידי.
    """
    if not is_referral_enabled():
        return False

    code = db.generate_referral_code(user_id)
    if not code:
        return False

    if not db.mark_referral_code_as_sent(user_id):
        return False

    text = get_referral_message_text(code, channel=channel)
    try:
        success = send_fn(text)
    except Exception:
        success = False
        logger.error("Exception sending referral code to user %s", user_id, exc_info=True)

    if not success:
        db.unmark_referral_code_sent(user_id)
        logger.error("Failed to send referral code to user %s, flag reset", user_id)
        return False

    return True
