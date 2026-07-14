"""
מודול זיהוי משתמשים — שכבת resolution ל-BSUID / מספר טלפון / שם משתמש.

מכין את המערכת לשינוי Meta Cloud API (יוני 2026):
- BSUID (Business-Scoped User ID) יחליף את מספר הטלפון כמזהה ברירת מחדל
- משתמשים שיאמצו שמות משתמש עשויים להפסיק לחשוף את מספר הטלפון שלהם

הלוגיקה:
1. אם יש BSUID ויש רשומה קיימת → מחזיר את ה-user_id הקיים
2. אם יש BSUID בלי רשומה → מחפש לפי טלפון, מעדכן BSUID, מחזיר user_id
3. אם אין BSUID → fallback למספר טלפון (מצב נוכחי)

מיגרציה הדרגתית: משתמשים ותיקים (מזוהים לפי טלפון) ימשיכו עם אותו user_id.
ה-BSUID יתווסף לרשומה שלהם ברגע שנקבל אותו מ-Twilio/Meta.
"""

import logging
import sqlite3
from typing import Optional

from ai_chatbot import database as db

logger = logging.getLogger(__name__)


def _safe_upsert(user_id: str, channel: str, **kwargs) -> None:
    """upsert עם טיפול ב-IntegrityError על UNIQUE index של whatsapp_bsuid.

    אם שתי בקשות מקבילות מנסות להכניס את אותו BSUID עם user_id שונה,
    השנייה תיכשל על ה-UNIQUE index. במקרה כזה — מתעלמים בשקט
    כי הבקשה הראשונה כבר יצרה את הרשומה.
    """
    try:
        db.upsert_user_identity(user_id, channel, **kwargs)
    except sqlite3.IntegrityError:
        logger.warning(
            "IntegrityError ב-upsert_user_identity (race condition): "
            "user_id=%s, bsuid=%s — כנראה נוצרה רשומה מבקשה מקבילית",
            user_id, kwargs.get("whatsapp_bsuid"),
        )


def resolve_whatsapp_user(
    phone_number: str,
    *,
    bsuid: Optional[str] = None,
    parent_bsuid: Optional[str] = None,
    wa_username: Optional[str] = None,
) -> str:
    """מתרגם מידע מ-webhook של WhatsApp ל-user_id קנוני.

    Args:
        phone_number: מספר הטלפון מהשדה From (E.164). עדיין נשלח תמיד כרגע.
        bsuid: Business-Scoped User ID (כשזמין מ-Twilio/Meta, None אחרת).
        parent_bsuid: Parent BSUID של Meta-managed portfolios (כשזמין). לא משמש
            ל-resolution — נשמר רק לטובת תיוג עתידי.
        wa_username: שם משתמש WhatsApp (כשזמין, None אחרת).

    Returns:
        user_id קנוני לשימוש בכל שאר המערכת.
    """
    # ── שלב 1: אם יש BSUID — מחפשים רשומה קיימת לפיו
    if bsuid:
        existing_user_id = db.lookup_user_id_by_bsuid(bsuid)
        if existing_user_id:
            # עדכון שדות נוספים (טלפון/שם משתמש) אם השתנו
            _safe_upsert(
                existing_user_id, "whatsapp",
                whatsapp_bsuid=bsuid,
                whatsapp_parent_bsuid=parent_bsuid,
                phone_number=phone_number or None,
                username=wa_username or "",
            )
            return existing_user_id

    # ── שלב 2: חיפוש לפי מספר טלפון (מיגרציה הדרגתית של משתמשים ותיקים)
    if phone_number:
        existing_user_id = db.lookup_user_id_by_phone(phone_number)
        if existing_user_id:
            # מעדכנים BSUID אם קיבלנו אותו לראשונה
            _safe_upsert(
                existing_user_id, "whatsapp",
                whatsapp_bsuid=bsuid,
                whatsapp_parent_bsuid=parent_bsuid,
                phone_number=phone_number,
                username=wa_username or "",
            )
            if bsuid:
                logger.info(
                    "קישור BSUID למשתמש ותיק: user_id=%s, bsuid=%s",
                    existing_user_id, bsuid,
                )
            return existing_user_id

    # ── שלב 3: משתמש חדש — יצירת רשומה
    # user_id = מספר הטלפון (תאימות לאחור). אם אין טלפון — BSUID.
    # parent_bsuid לעולם לא משמש כ-user_id (משותף בין משתמשים).
    user_id = phone_number or bsuid
    if not user_id:
        # מצב בלתי אפשרי — חייב להיות לפחות אחד מהשניים
        logger.error("resolve_whatsapp_user: לא התקבל טלפון ולא BSUID")
        raise ValueError("חייב לקבל phone_number או bsuid")

    _safe_upsert(
        user_id, "whatsapp",
        whatsapp_bsuid=bsuid,
        whatsapp_parent_bsuid=parent_bsuid,
        phone_number=phone_number or None,
        username=wa_username or "",
    )

    # אם ה-upsert נכשל ב-IntegrityError (race condition על BSUID) —
    # בקשה מקבילית כבר יצרה רשומה. ננסה שוב lookup לפי BSUID.
    if bsuid:
        race_winner = db.lookup_user_id_by_bsuid(bsuid)
        if race_winner and race_winner != user_id:
            logger.info(
                "race condition: משתמש כבר נוצר ע\"י בקשה מקבילית — "
                "user_id=%s (במקום %s)", race_winner, user_id,
            )
            return race_winner

    logger.info(
        "משתמש WhatsApp חדש: user_id=%s, bsuid=%s, username=%s",
        user_id, bsuid, wa_username,
    )
    return user_id


def get_whatsapp_send_address(user_id: str) -> Optional[str]:
    """מחזיר מספר טלפון לשליחה עבור משתמש WhatsApp, או None.

    הקוראים (sender/adapter/templates) יחליטו אם לשלוח ישירות ל-BSUID
    כשאין מספר טלפון — Twilio תומך ב-to=whatsapp:CC.BSUID.
    """
    return db.get_phone_for_user(user_id)
