"""
WhatsApp Content Templates — יצירה ושליחה של הודעות אינטראקטיביות דרך Twilio Content API.

Twilio Content API מאפשר שליחת כפתורי Quick Reply ו-List Picker ב-WhatsApp.
- Quick Reply: עד 3 כפתורים ב-session (ללא אישור מ-Meta)
- List Picker: רשימת אפשרויות לבחירה (ב-session בלבד)

Templates נוצרים פעם אחת ונשמרים ב-DB (content_sid). בשליחה — מעבירים את ה-SID בלבד.

שימוש:
    # יצירת template עם כפתורים
    sid = ensure_quick_reply("welcome_menu", "איך אפשר לעזור?", [
        ("📋 מחירון", "menu_price"),
        ("📅 בקשת תור", "menu_booking"),
        ("👤 נציג", "menu_agent"),
    ])

    # שליחה
    send_with_template(to_number, sid)
"""

import hashlib
import json
import logging
import re
from typing import Optional

import requests

from messaging.twilio_content_api import get_auth as _get_auth, content_api_url as _content_api_url

logger = logging.getLogger(__name__)

# קאש in-memory — מונע קריאות מיותרות ל-API.
# מפתח: (tenant, friendly_name כולל hash תוכן), ערך: content_sid.
# ה-SIDs שייכים לחשבון ה-Twilio של ה-tenant — אסור שיערבבו בין עסקים.
_template_cache: dict[tuple[str, str], str] = {}


def _tpl_key(friendly_name: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), friendly_name)


# ── יצירת Templates ─────────────────────────────────────────────────────────


def create_quick_reply(
    friendly_name: str,
    body: str,
    buttons: list[tuple[str, str]],
) -> str:
    """יצירת Content Template מסוג Quick Reply.

    Args:
        friendly_name: שם ייחודי ל-template (למשל "welcome_menu").
        body: טקסט ההודעה.
        buttons: רשימת (title, id) — עד 3 כפתורים ב-session.

    Returns:
        content_sid (HXxxxx) של ה-template שנוצר.

    Raises:
        RuntimeError: אם היצירה נכשלה.
    """
    # מגבלת WhatsApp: עד 3 כפתורים ב-session (ללא אישור Meta)
    if len(buttons) > 3:
        raise ValueError("Quick Reply ב-session מוגבל ל-3 כפתורים")

    # זיהוי placeholders בגוף ההודעה (למשל {{1}}, {{2}}) — Twilio דורש הצהרת variables
    placeholders = re.findall(r"\{\{(\d+)\}\}", body)
    variables = {p: p for p in placeholders} if placeholders else {}

    payload = {
        "friendly_name": friendly_name,
        "language": "he",
        "variables": variables,
        "types": {
            "twilio/quick-reply": {
                "body": body,
                "actions": [
                    {"title": title, "id": btn_id}
                    for title, btn_id in buttons
                ],
            }
        },
    }

    auth = _get_auth()
    resp = requests.post(
        _content_api_url(),
        json=payload,
        auth=auth,
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        logger.error("Twilio Content API error (%d): %s", resp.status_code, resp.text)
        raise RuntimeError(f"יצירת Quick Reply template נכשלה: {resp.status_code} {resp.text}")

    sid = resp.json()["sid"]
    _template_cache[_tpl_key(friendly_name)] = sid
    logger.info("Quick Reply template created: %s → %s", friendly_name, sid)
    return sid


def create_list_picker(
    friendly_name: str,
    body: str,
    button_text: str,
    items: list[dict],
) -> str:
    """יצירת Content Template מסוג List Picker.

    Args:
        friendly_name: שם ייחודי ל-template.
        body: טקסט ההודעה.
        button_text: טקסט כפתור פתיחת הרשימה (למשל "בחרו שירות").
        items: רשימת פריטים — כל פריט dict עם title, id, ו-description (אופציונלי).
               עד 10 פריטים.

    Returns:
        content_sid.

    Raises:
        RuntimeError: אם היצירה נכשלה.
    """
    if len(items) > 10:
        raise ValueError("List Picker תומך בעד 10 פריטים")

    payload = {
        "friendly_name": friendly_name,
        "language": "he",
        "variables": {},
        "types": {
            "twilio/list-picker": {
                "body": body,
                "button": button_text,
                "items": [
                    {
                        "item": item["title"],
                        "id": item["id"],
                        **({"description": item["description"]} if item.get("description") else {}),
                    }
                    for item in items
                ],
            }
        },
    }

    auth = _get_auth()
    resp = requests.post(
        _content_api_url(),
        json=payload,
        auth=auth,
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        logger.error("Twilio Content API error (%d): %s", resp.status_code, resp.text)
        raise RuntimeError(f"יצירת List Picker template נכשלה: {resp.status_code} {resp.text}")

    sid = resp.json()["sid"]
    _template_cache[_tpl_key(friendly_name)] = sid
    logger.info("List Picker template created: %s → %s", friendly_name, sid)
    return sid


# ── שליפה וקאש ───────────────────────────────────────────────────────────────


def find_template(friendly_name: str) -> Optional[str]:
    """חיפוש template קיים לפי friendly_name. מחזיר content_sid או None."""
    # בדיקה בקאש
    cached = _template_cache.get(_tpl_key(friendly_name))
    if cached:
        return cached

    # חיפוש ב-API — עם pagination (Twilio מחזיר דפים של תוצאות)
    auth = _get_auth()
    try:
        url = _content_api_url()
        while url:
            resp = requests.get(url, auth=auth, timeout=15)
            if resp.status_code != 200:
                logger.error("Twilio Content API list error (%d): %s", resp.status_code, resp.text)
                return None

            data = resp.json()
            for item in data.get("contents", []):
                if item.get("friendly_name") == friendly_name:
                    sid = item["sid"]
                    _template_cache[_tpl_key(friendly_name)] = sid
                    return sid

            # דף הבא — Twilio מחזיר URI יחסי ב-meta.next_page_url
            next_page = data.get("meta", {}).get("next_page_url")
            url = next_page if next_page else None
    except Exception:
        logger.error("שגיאה בחיפוש Content Template: %s", friendly_name, exc_info=True)

    return None


def delete_template(content_sid: str) -> bool:
    """מחיקת template לפי SID — משמש לניקוי/עדכון templates.

    מטופל כ-idempotent: 204 (Twilio מחק) ו-404 (כבר לא קיים) שניהם
    נחשבים הצלחה. זה חשוב כדי שעריכה לא תיתקע אם תבנית נמחקה
    out-of-band (Twilio Console / admin אחר).
    """
    auth = _get_auth()
    try:
        resp = requests.delete(
            _content_api_url(content_sid),
            auth=auth,
            timeout=15,
        )
        if resp.status_code in (204, 404):
            # ניקוי מהקאש בכל אופן (גם אם הייתה כבר מחוקה)
            _template_cache.pop(
                next((k for k, v in _template_cache.items() if v == content_sid), ("", "")),
                None,
            )
            if resp.status_code == 204:
                logger.info("Content Template deleted: %s", content_sid)
            else:
                logger.info(
                    "Content Template %s לא נמצאה ב-Twilio (404) — "
                    "מתייחסים כהצלחה (idempotent)", content_sid,
                )
            return True
        logger.error("Failed to delete template %s: %d", content_sid, resp.status_code)
    except Exception:
        logger.error("שגיאה במחיקת Content Template: %s", content_sid, exc_info=True)
    return False


def _compute_content_hash(*parts) -> str:
    """חישוב hash קצר מתוכן ה-template — לזיהוי שינויים."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ensure_fresh(friendly_name: str, content_hash: str) -> Optional[str]:
    """בודק אם template קיים ועדכני לפי hash בשם. מוחק גרסאות ישנות.

    ה-friendly_name האמיתי הוא {friendly_name}_{content_hash}.
    אם קיים template עם hash שונה — מוחקים אותו ומחזירים None ליצירה מחדש.
    """
    versioned_name = f"{friendly_name}_{content_hash}"

    # בדיקה מהירה בקאש
    cached_sid = _template_cache.get(_tpl_key(versioned_name))
    if cached_sid:
        return cached_sid

    # חיפוש ב-API — מוצא את ה-template הנוכחי או גרסה ישנה
    auth = _get_auth()
    found_sid = None
    old_sids = []  # SIDs של גרסאות ישנות למחיקה
    try:
        url = _content_api_url()
        while url:
            resp = requests.get(url, auth=auth, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            for item in data.get("contents", []):
                name = item.get("friendly_name", "")
                sid = item["sid"]
                if name == versioned_name:
                    found_sid = sid
                elif name.startswith(f"{friendly_name}_"):
                    # גרסה ישנה — למחיקה
                    old_sids.append(sid)
            next_page = data.get("meta", {}).get("next_page_url")
            url = next_page if next_page else None
    except Exception:
        logger.error("שגיאה בחיפוש templates: %s", friendly_name, exc_info=True)

    # מחיקת גרסאות ישנות — תמיד, גם אם מצאנו את הנוכחי
    for old_sid in old_sids:
        try:
            delete_template(old_sid)
        except Exception:
            logger.error("שגיאה במחיקת template ישן: %s", old_sid, exc_info=True)

    if found_sid:
        _template_cache[_tpl_key(versioned_name)] = found_sid
        return found_sid

    return None


def ensure_quick_reply(
    friendly_name: str,
    body: str,
    buttons: list[tuple[str, str]],
) -> str:
    """מוודא שה-template קיים ועדכני. אם לא — יוצר/מחדש אותו. מחזיר content_sid."""
    content_hash = _compute_content_hash("qr", body, buttons)
    existing = _ensure_fresh(friendly_name, content_hash)
    if existing:
        return existing
    versioned_name = f"{friendly_name}_{content_hash}"
    return create_quick_reply(versioned_name, body, buttons)


def ensure_list_picker(
    friendly_name: str,
    body: str,
    button_text: str,
    items: list[dict],
) -> str:
    """מוודא שה-template קיים ועדכני. אם לא — יוצר/מחדש אותו. מחזיר content_sid."""
    content_hash = _compute_content_hash("lp", body, button_text, items)
    existing = _ensure_fresh(friendly_name, content_hash)
    if existing:
        return existing
    versioned_name = f"{friendly_name}_{content_hash}"
    return create_list_picker(versioned_name, body, button_text, items)


# ── שליחת הודעות עם template ─────────────────────────────────────────────────


def send_with_template(
    to_number: str,
    content_sid: str,
    content_variables: Optional[dict] = None,
) -> None:
    """שליחת הודעת WhatsApp עם Content Template (כפתורים/רשימה).

    Args:
        to_number: מספר הנמען (ללא whatsapp: prefix). יכול להיות גם BSUID.
        content_sid: ה-SID של ה-template (HXxxxx).
        content_variables: משתנים דינמיים (אופציונלי).
    """
    from ai_chatbot.config import TWILIO_WHATSAPP_NUMBER
    from messaging.whatsapp_sender import _get_twilio_client, _is_phone_number

    # reverse lookup — אם ה-user_id הוא BSUID (לא מספר טלפון), מחפשים טלפון.
    # אם אין טלפון — שולחים ישירות ל-BSUID (Twilio תומך ב-to=whatsapp:CC.BSUID).
    send_to = to_number
    if not _is_phone_number(to_number):
        from utils.user_identity import get_whatsapp_send_address
        resolved = get_whatsapp_send_address(to_number)
        send_to = resolved or to_number

    client = _get_twilio_client()

    kwargs = {
        "content_sid": content_sid,
        "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
        "to": f"whatsapp:{send_to}",
    }
    if content_variables:
        kwargs["content_variables"] = json.dumps(content_variables)

    client.messages.create(**kwargs)
