"""
WhatsApp Templates — שליחה לאישור Meta דרך Twilio Content API.

רקע:
    Twilio חושפת endpoint ליצירת Approval Request ל-WhatsApp:
      POST /v1/Content/{content_sid}/ApprovalRequests/whatsapp
      Content-Type: application/json
      Body: {"name": ..., "category": ...} (lowercase keys)
      params: Name, Category

    Meta עונה תוך 1-24 שעות. עד אז התבנית במצב `pending`; אחר כך
    `approved`, `rejected`, או `paused`. הסטטוס נקרא חזרה ל-DB דרך
    whatsapp_templates_sync.

    הגבלות מ-Meta:
      - Name חייב להיות lowercase + underscore (lowercase_alphanumeric_)
      - Name חייב להיות ייחודי בתוך WABA
      - Category חייבת להיות אחת מ: UTILITY | MARKETING | AUTHENTICATION
      - אחרי submission לא ניתן לערוך את התבנית. אפשר לשלוח גרסה חדשה
        (כ-Content SID נוסף) אם היא נדחתה.

שימוש:
    from messaging.whatsapp_templates_submit import submit_template_for_approval
    result = submit_template_for_approval(
        content_sid="HXxxx",
        category="UTILITY",
        name="order_update_v2",
    )
    # result == {"success": True, "approval_status": "pending", "error": None}
"""

from __future__ import annotations

import logging
import re
import time

import requests

from messaging.twilio_content_api import get_auth, content_api_url

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
# Meta: name מורכב מאותיות קטנות, ספרות, קו תחתון. מקס ~512 תווים.
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def sanitize_template_name(name: str) -> str:
    """ניקוי שם תבנית לפורמט שמטא מאפשר.

    - להמרה ל-lowercase
    - החלפת כל תו שאינו אות/ספרה/קו תחתון בקו תחתון
    - הסרת קווים תחתונים בתחילה/סוף
    - fallback אם ריק: template_<timestamp>

    דוגמה: "Order Update v2" → "order_update_v2"
    """
    cleaned = _NAME_SANITIZE_RE.sub("_", (name or "").lower().strip())
    # קיבוץ קווים תחתונים רצופים + חיתוך מהשוליים
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = f"template_{int(time.time())}"
    return cleaned[:512]


def _approval_url(content_sid: str) -> str:
    """בניית URL של ApprovalRequests/whatsapp."""
    return content_api_url(f"{content_sid}/ApprovalRequests/whatsapp")


def submit_template_for_approval(
    content_sid: str,
    category: str,
    name: str,
    timeout: int = 15,
) -> dict:
    """שליחת בקשת אישור Meta ל-Twilio + עדכון ה-DB המקומי.

    Args:
        content_sid: ה-SID של התבנית ב-Twilio (חייב להיות קיים).
        category: UTILITY | MARKETING | AUTHENTICATION.
        name: שם ייחודי לתבנית (יעבור sanitize לפני שליחה).
        timeout: timeout ל-HTTP request בשניות.

    Returns:
        dict עם:
          success (bool)
          approval_status (str): הסטטוס החדש ('pending' על הצלחה)
          category (str): הקטגוריה שנקבעה
          name (str): השם שנשלח אחרי sanitize
          error (str|None): תיאור שגיאה אם נכשל

    Raises:
        ValueError: אם category לא חוקית או content_sid/name ריקים.
    """
    if not content_sid:
        raise ValueError("submit_template_for_approval: content_sid חובה")
    if category not in _VALID_CATEGORIES:
        raise ValueError(
            f"submit_template_for_approval: category חייבת להיות אחת מ-"
            f"{sorted(_VALID_CATEGORIES)}, התקבל: {category!r}"
        )
    if not name:
        raise ValueError("submit_template_for_approval: name חובה")

    sanitized_name = sanitize_template_name(name)
    auth = get_auth()

    try:
        # Twilio Approval endpoint דורש JSON (לא form-encoded). אם
        # שולחים data= מקבלים HTTP 415 "does not support this payload
        # format". השדות גם lowercase (name/category) ולא PascalCase.
        resp = requests.post(
            _approval_url(content_sid),
            json={"name": sanitized_name, "category": category},
            auth=auth,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.error(
            "submit_for_approval: שגיאת רשת ל-%s",
            content_sid,
            exc_info=True,
        )
        return {
            "success": False,
            "approval_status": "unsubmitted",
            "category": category,
            "name": sanitized_name,
            "error": f"שגיאת רשת: {exc}",
        }

    if resp.status_code not in (200, 201, 202):
        error_body = resp.text[:500]
        logger.error(
            "submit_for_approval: Twilio החזיר %d עבור %s: %s",
            resp.status_code,
            content_sid,
            error_body,
        )
        return {
            "success": False,
            "approval_status": "unsubmitted",
            "category": category,
            "name": sanitized_name,
            "error": f"HTTP {resp.status_code}: {error_body}",
        }

    # עדכון ה-DB המקומי — ההגעה של הסטטוס הסופי תתרחש בסנכרון הבא.
    # כרגע מסמנים pending + category, כדי שה-UI יראה מיידית שהתבנית נשלחה.
    try:
        from ai_chatbot import database as db
        tpl = db.get_whatsapp_template(content_sid)
        if tpl:
            tpl_update = {
                "content_sid": content_sid,
                "friendly_name": tpl["friendly_name"],
                "language": tpl["language"],
                "category": category,
                "approval_status": "pending",
                "rejection_reason": None,
                "header_type": tpl["header_type"],
                "body_text": tpl["body_text"],
                "footer_text": tpl["footer_text"],
                "buttons": tpl["buttons"],
                "variables": tpl["variables"],
                "content_type": tpl.get("content_type", ""),
                "raw": tpl.get("raw", {}),
            }
            db.upsert_whatsapp_template(tpl_update)
    except Exception:
        logger.error(
            "submit_for_approval: התשובה מ-Twilio הצליחה אבל עדכון ה-DB נכשל "
            "(הסנכרון הבא יקרא את הסטטוס שוב)",
            exc_info=True,
        )

    logger.info(
        "submit_for_approval: content_sid=%s name=%s category=%s status=%d",
        content_sid,
        sanitized_name,
        category,
        resp.status_code,
    )
    return {
        "success": True,
        "approval_status": "pending",
        "category": category,
        "name": sanitized_name,
        "error": None,
    }


# Wrapper על _VALID_CATEGORIES ו-sanitize שיוכלו להיקרא ע"י ה-UI
VALID_CATEGORIES = sorted(_VALID_CATEGORIES)
