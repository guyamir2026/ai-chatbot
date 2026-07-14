"""
Meta sender — שליחת הודעות לערוצי Facebook Messenger ו-Instagram DM
דרך Graph API.

Graph API משתמשת בנקודת קצה זהה לשני הערוצים (`/me/messages`); ההבדל
היחיד הוא הטוקן שמשמש: page access token של עמוד הפייסבוק, גם כשהיעד
הוא IG (כי חשבון ה-IG הוא Business Account המקושר לעמוד פייסבוק).

**אסור לקרוא לפונקציה הזו ישירות מ-handlers חיצוניים.** השער היחיד
לשליחה אל מטא הוא `_send_meta_response` ב-`messaging/meta_webhook.py`,
שמבצע בדיקת אורך + נפילה לעמוד HTML ציבורי במידת הצורך (זהה לדפוס
של WhatsApp).
"""
from __future__ import annotations

import logging

import requests

from messaging.meta_graph_client import (
    MetaGraphError,
    _graph_url,
    _raise_for_graph_error,
    _TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def send_meta_message(
    recipient_external_id: str,
    text: str,
    page_token: str,
) -> str:
    """שולח הודעת טקסט לעמוד מטא דרך Graph API.

    טיעונים:
        recipient_external_id: PSID/IGSID טהור (אחרי `to_provider_recipient`).
        text: גוף ההודעה. **המתקשר אחראי לוודא שהאורך תחת המגבלה** של מטא;
            הצ'ק עצמו נעשה ב-`_send_meta_response`, לא כאן.
        page_token: page access token (לערוץ Messenger וגם IG — מטא מבדילה
            ביעד דרך הסקופ של הטוקן).

    מחזיר את `message_id` שמטא הקצתה (שימושי ללוגים).

    זורק `MetaGraphError` בכשל — בדומה לקריאות OAuth המשך אותו דפוס.
    """
    payload = {
        "recipient": {"id": recipient_external_id},
        "message": {"text": text},
        # messaging_type נדרש ל-Messenger; ב-IG מטא מתעלמת. RESPONSE = תשובה
        # להודעת לקוח (מותרת בלי תיוג).
        "messaging_type": "RESPONSE",
    }
    resp = requests.post(
        _graph_url("me/messages"),
        params={"access_token": page_token},
        json=payload,
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "send_meta_message")

    try:
        body = resp.json()
    except Exception:
        raise MetaGraphError("send_meta_message: תגובה אינה JSON תקין")

    message_id = body.get("message_id", "")
    if not message_id:
        # Send API מחזיר תמיד message_id בהצלחה; חוסר ⇒ כשל לוגי גם אם
        # status code היה 200.
        raise MetaGraphError(
            f"send_meta_message: לא חזר message_id (body={body!r})"
        )

    return message_id
