"""
WhatsApp Templates Sync — סנכרון תבניות מאושרות מ-Twilio Content API אל ה-DB המקומי.

רקע:
    Twilio Content API חושף שני endpoints עיקריים:
      1. GET /v1/Content — רשימת כל ה-Contents (תבניות) בחשבון
      2. GET /v1/Content/{sid}/ApprovalRequests — סטטוס אישור Meta לכל תבנית

    בגלל ש-broadcast ל-WhatsApp מחוץ לחלון 24 שעות מחייב תבנית שאושרה מראש
    ע"י Meta (HSM), צריך להחזיק ב-DB את הסטטוס כדי לאפשר ל-UI להציג רק
    תבניות עם approval_status='approved' (ראה תיקון 40 לחוק התקשורת +
    כללי WABA של Meta).

    הסנכרון מחלץ מתוך ה-JSON של Twilio את השדות שה-UI צריך:
      - body text + placeholders ({{1}}, {{2}}...)
      - header type (text/image/video/document/location/none)
      - buttons (quick reply / call to action / url)
      - variables (עם שמות ודוגמאות כשקיים)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from messaging.twilio_content_api import get_auth as _get_auth, content_api_url as _content_api_url

logger = logging.getLogger(__name__)

# זיהוי placeholders בסגנון WhatsApp/Twilio: {{1}}, {{2}}, {{customer_name}}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")

# מיפוי מבני Content של Twilio אל header_type אחיד.
# header-type הוא קומפוננטה *נפרדת* מהגוף ב-WhatsApp HSM (תמונה/וידאו/מסמך/
# טקסט מעל ה-body). תבניות text-based כמו quick-reply/list-picker/card הן
# body-only כברירת מחדל — לכן מוגדרות כאן כ-"none". רק media/location הם
# header אינהרנטי לסוג ה-content. אם Twilio יחזיר header טקסטואלי מפורש
# בעתיד (שדה `header`), נצטרך לחלץ אותו ב-_detect_content_type_and_body.
_HEADER_TYPE_BY_CONTENT_KEY = {
    "twilio/text": "none",
    "twilio/quick-reply": "none",
    "twilio/call-to-action": "none",
    "twilio/list-picker": "none",
    "twilio/card": "none",
    "twilio/media": "image",
    "twilio/location": "location",
}

# קטגוריות המותרות ע"י ה-CHECK constraint ב-DB (ראה database.py:whatsapp_templates).
# כל קטגוריה אחרת שמגיעה מ-Twilio (למשל ערך חדש שנוסף בעתיד) תומר ל-UNKNOWN
# כדי שה-upsert לא ייכשל ב-IntegrityError ויפיל את הסנכרון לתבנית זו.
_ALLOWED_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION", "UNKNOWN"}


class _PaginationIncomplete(RuntimeError):
    """הסנכרון הופסק באמצע דפדוף — הרשימה שנאספה חלקית.

    זורק מ-_iter_all_contents כשיש שגיאת רשת או סטטוס לא-200.
    סנכרון שתופס את החריגה ימנע prune (מחיקת תבניות שלא נראו) כדי
    לא למחוק בטעות תבניות שהיו בדפים שלא נמשכו.
    """


# ── פרסור Content Object של Twilio ───────────────────────────────────────────


def _extract_variable_indices(text: str) -> list[str]:
    """חילוץ רשימת placeholders ייחודיים לפי סדר הופעה.

    לדוגמה: "היי {{1}}, ההזמנה {{2}}" → ["1", "2"].
    """
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        var = match.group(1)
        if var not in seen:
            seen.append(var)
    return seen


def _detect_content_type_and_body(types: dict) -> tuple[str, str, str, str, list[dict]]:
    """מזהה את סוג התבנית ומחלץ body/footer/header_type/buttons.

    Twilio מחזיר `types` dict שבו המפתח הוא שם ה-type (למשל "twilio/quick-reply")
    והערך הוא dict עם body/actions/וכו׳.

    Returns:
        (content_type, body_text, footer_text, header_type, buttons)
    """
    if not types:
        return ("", "", "", "none", "", "", [])

    # לוקחים את ה-type הראשון (בד"כ יש רק אחד לכל Content)
    content_type, payload = next(iter(types.items()))
    payload = payload or {}
    body = (payload.get("body") or "").strip()
    footer = (payload.get("footer") or "").strip()
    # header_text רלוונטי כשה-content הוא card (title משמש ככותרת).
    # שאר הסוגים לא נושאים header text.
    header_text = (payload.get("title") or "").strip() if content_type == "twilio/card" else ""
    # header_media_url — twilio/card יכול להחזיק media (תמונה/וידאו/מסמך)
    # יחד עם או במקום title. שואבים את הראשון אם קיים.
    header_media_url = ""
    header_type = _HEADER_TYPE_BY_CONTENT_KEY.get(content_type, "none")

    if content_type == "twilio/card":
        card_media = payload.get("media") or []
        if card_media:
            header_media_url = str(card_media[0]).strip()
            url_lower = header_media_url.lower().split("?", 1)[0]
            if any(url_lower.endswith(ext) for ext in (".mp4", ".mov", ".webm")):
                header_type = "video"
            elif url_lower.endswith(".pdf"):
                header_type = "document"
            else:
                header_type = "image"
        elif header_text:
            header_type = "text"

    # media type — header הוא מדיה (תמונה/וידאו/מסמך). מנסים להסיק לפי URL.
    if content_type == "twilio/media":
        media_urls = payload.get("media") or []
        if media_urls:
            header_media_url = str(media_urls[0]).strip()
            # מסירים query string לפני בדיקת סיומת — URLs חתומים נראים
            # כמו foo.mp4?token=xyz, ובלי הסרה הם מסווגים כ-image.
            url_lower = header_media_url.lower().split("?", 1)[0]
            if any(url_lower.endswith(ext) for ext in (".mp4", ".mov", ".webm")):
                header_type = "video"
            elif any(url_lower.endswith(ext) for ext in (".pdf", ".doc", ".docx")):
                header_type = "document"
            else:
                header_type = "image"

    # חילוץ כפתורים — פורמטים שונים לפי סוג ה-content
    buttons: list[dict] = []
    for action in payload.get("actions") or []:
        btn = {
            "type": action.get("type") or _infer_button_type(content_type),
            "title": action.get("title") or action.get("item") or "",
            "id": action.get("id") or "",
        }
        if action.get("url"):
            btn["url"] = action["url"]
        if action.get("phone"):
            btn["phone"] = action["phone"]
        buttons.append(btn)

    # List Picker: ה-items משמשים כ"כפתורים" בתצוגה
    for item in payload.get("items") or []:
        buttons.append({
            "type": "list_item",
            "title": item.get("item") or "",
            "id": item.get("id") or "",
            "description": item.get("description") or "",
        })

    return (content_type, body, footer, header_type, header_text,
            header_media_url, buttons)


def _infer_button_type(content_type: str) -> str:
    """מיפוי content_type ל-type כפתור ברירת מחדל."""
    if content_type == "twilio/quick-reply":
        return "quick_reply"
    if content_type == "twilio/call-to-action":
        return "call_to_action"
    if content_type == "twilio/list-picker":
        return "list_item"
    return "unknown"


def _parse_content(content: dict) -> dict:
    """פרסור Content object שלם מ-Twilio למבנה שמתאים ל-upsert ב-DB.

    מתעלם מהעטיפה (sid, friendly_name, language) ומחזיר dict עם המפתחות
    הנדרשים ל-upsert_whatsapp_template.
    """
    types = content.get("types") or {}
    (content_type, body, footer, header_type, header_text,
     header_media_url, buttons) = _detect_content_type_and_body(types)

    # Variables — Twilio מחזיר dict {index: example_value} בשדה variables
    declared_vars = content.get("variables") or {}
    indices_in_body = _extract_variable_indices(body)

    # מאחדים את המקורות (body + declared) לשמירה על סדר הופעה ב-body
    all_indices: list[str] = []
    for idx in indices_in_body:
        if idx not in all_indices:
            all_indices.append(idx)
    for idx in declared_vars.keys():
        if idx not in all_indices:
            all_indices.append(idx)

    variables = [
        {
            "index": idx,
            "name": f"variable_{idx}",
            "example": declared_vars.get(idx, ""),
        }
        for idx in all_indices
    ]

    return {
        "content_sid": content.get("sid", ""),
        "friendly_name": content.get("friendly_name", "") or "",
        "language": (content.get("language") or "he").lower(),
        "header_type": header_type,
        "header_text": header_text,
        "header_media_url": header_media_url,
        "body_text": body,
        "footer_text": footer,
        "buttons": buttons,
        "variables": variables,
        "content_type": content_type,
        # שמירת raw מסננת כדי לא לנפח DB — רק types + variables + שדות metadata
        "raw": {
            "types": types,
            "variables": declared_vars,
            "date_created": content.get("date_created"),
            "date_updated": content.get("date_updated"),
            "url": content.get("url"),
        },
    }


# ── HTTP — שליפה מ-Twilio ────────────────────────────────────────────────────


def _iter_all_contents(timeout: int = 15):
    """יוצר (generator) על כל ה-Contents בחשבון, עם pagination.

    Raises:
        _PaginationIncomplete: כשל ברשת/סטטוס לא-200 באמצע pagination.
            הקורא חייב לתפוס זאת ולבטל prune, אחרת יימחקו בטעות תבניות
            שהיו בדפים שלא נמשכו.
    """
    auth = _get_auth()
    url: Optional[str] = _content_api_url()
    while url:
        try:
            resp = requests.get(url, auth=auth, timeout=timeout)
        except requests.RequestException as exc:
            logger.error("sync_templates: שגיאת רשת ב-GET %s", url, exc_info=True)
            raise _PaginationIncomplete(f"network error fetching {url}") from exc
        if resp.status_code != 200:
            logger.error(
                "sync_templates: Twilio Content API החזיר %d עבור %s: %s",
                resp.status_code,
                url,
                resp.text[:500],
            )
            raise _PaginationIncomplete(
                f"HTTP {resp.status_code} fetching {url}"
            )
        data = resp.json()
        for content in data.get("contents", []):
            yield content
        # Twilio מחזיר next_page_url ב-meta; None בסיום
        url = (data.get("meta") or {}).get("next_page_url")


def _fetch_approval_status(content_sid: str, timeout: int = 15) -> dict:
    """שליפת סטטוס אישור Meta לתבנית אחת.

    החזרה: dict עם {approval_status, category, rejection_reason}.
    כשאין מידע מ-Twilio (404, שגיאת רשת, או 5xx) — category=None כדי
    שה-upsert ב-DB לא ידרוס קטגוריה קיימת (COALESCE). זה חיוני כדי
    שתבנית שיצרנו עם MARKETING ועדיין לא נשלחה לאישור לא תיהפך
    שקטה ל-UTILITY אחרי סנכרון ראשון, ותיעלם מהפילטר של המשתמש.
    """
    auth = _get_auth()
    url = _content_api_url(f"{content_sid}/ApprovalRequests")
    try:
        resp = requests.get(url, auth=auth, timeout=timeout)
    except requests.RequestException:
        logger.error(
            "sync_templates: שגיאת רשת בשליפת approval עבור %s",
            content_sid,
            exc_info=True,
        )
        return {"approval_status": "unsubmitted", "category": None,
                "rejection_reason": None}

    if resp.status_code == 404:
        # לא נשלחה בקשת אישור — לא דורסים category קיימת.
        return {"approval_status": "unsubmitted", "category": None,
                "rejection_reason": None}

    if resp.status_code != 200:
        logger.error(
            "sync_templates: approval request החזיר %d עבור %s: %s",
            resp.status_code,
            content_sid,
            resp.text[:300],
        )
        return {"approval_status": "unsubmitted", "category": None,
                "rejection_reason": None}

    return _parse_approval_payload(resp.json())


def _parse_approval_payload(payload: dict) -> dict:
    """פרסור תשובת ApprovalRequests — Twilio מחזיר מבנה עטוף תחת whatsapp או global."""
    # הפורמט החדש: {"whatsapp": {"status": "approved", "category": "UTILITY", ...}}
    wa = payload.get("whatsapp") if isinstance(payload, dict) else None
    if isinstance(wa, dict) and wa.get("status"):
        status = str(wa.get("status") or "unsubmitted").lower()
        return {
            "approval_status": _normalize_approval_status(status),
            "category": _normalize_category(wa.get("category")),
            "rejection_reason": wa.get("rejection_reason"),
        }
    # פורמט ישן: {"status": "approved", ...}
    if isinstance(payload, dict) and payload.get("status"):
        status = str(payload.get("status") or "unsubmitted").lower()
        return {
            "approval_status": _normalize_approval_status(status),
            "category": _normalize_category(payload.get("category")),
            "rejection_reason": payload.get("rejection_reason"),
        }
    return {"approval_status": "unsubmitted", "category": None,
            "rejection_reason": None}


def _normalize_approval_status(status: str) -> str:
    """המרה של ערכי סטטוס Twilio לערכי enum שלנו."""
    mapping = {
        "approved": "approved",
        "pending": "pending",
        "received": "pending",
        "rejected": "rejected",
        "paused": "paused",
        "disabled": "paused",
        "unsubmitted": "unsubmitted",
    }
    return mapping.get(status, "unsubmitted")


def _normalize_category(category):
    """המרה של ערך קטגוריה מ-Twilio לערך מותר ב-CHECK constraint של ה-DB.

    מחזיר None כשהערך חסר/ריק כדי שה-upsert ב-DB ישמור את הקטגוריה
    הקיימת (COALESCE) — חשוב במיוחד לתבניות pending שעדיין אין להן
    קטגוריה ב-Twilio. ערכים לא מוכרים (Meta הוסיפה קטגוריה חדשה)
    מוחזרים כ-UNKNOWN כדי שהסנכרון לא ייכשל עם IntegrityError.
    """
    if not category:
        return None
    normalized = str(category).upper().strip()
    if not normalized:
        return None
    return normalized if normalized in _ALLOWED_CATEGORIES else "UNKNOWN"


# ── נקודת כניסה ──────────────────────────────────────────────────────────────


def sync_templates_from_twilio(prune_deleted: bool = True) -> dict:
    """סנכרון מלא — מושך את כל התבניות + סטטוס אישור ושומר ב-DB.

    Args:
        prune_deleted: אם True — מוחק מה-DB תבניות שלא קיימות יותר ב-Twilio.
            מופעל רק כש-pagination הסתיים בהצלחה מלאה; כשל באמצע דפדוף
            משאיר את הרשימה חלקית ולכן prune יידלג כדי לא למחוק בטעות.

    Returns:
        dict עם סטטיסטיקות: {fetched, upserted, deleted, errors,
        pagination_complete}.
    """
    from ai_chatbot import database as db

    stats = {
        "fetched": 0,
        "upserted": 0,
        "deleted": 0,
        "errors": 0,
        "pagination_complete": True,
    }
    seen_sids: list[str] = []

    try:
        for content in _iter_all_contents():
            stats["fetched"] += 1
            try:
                parsed = _parse_content(content)
                if not parsed["content_sid"]:
                    logger.warning("sync_templates: content ללא SID — מדלג")
                    stats["errors"] += 1
                    continue

                approval = _fetch_approval_status(parsed["content_sid"])
                # category=None מ-_fetch_approval_status (404 או שגיאה)
                # אומר "אין מידע מ-Twilio". לא דורסים את הבחירה הקיימת
                # ב-DB — המשתמש יצר את התבנית עם MARKETING/UTILITY מסוים
                # ב-create flow, ואין סיבה להפיל אותה ל-UTILITY רק כי
                # עדיין לא נשלחה לאישור.
                if approval.get("category") is None:
                    existing = db.get_whatsapp_template(parsed["content_sid"])
                    if existing and existing.get("category"):
                        approval["category"] = existing["category"]
                    else:
                        # תבנית חדשה לחלוטין שלא נצפתה ב-DB — UTILITY
                        # ברירת מחדל סבירה (תואם ל-CHECK constraint).
                        approval["category"] = "UTILITY"
                parsed.update(approval)

                db.upsert_whatsapp_template(parsed)
                seen_sids.append(parsed["content_sid"])
                stats["upserted"] += 1
            except Exception:
                # כשל בתבנית אחת לא עוצר את כל הסנכרון (CLAUDE.md — לולאות I/O).
                logger.error(
                    "sync_templates: כשל בעיבוד content_sid=%s",
                    content.get("sid"),
                    exc_info=True,
                )
                stats["errors"] += 1
    except _PaginationIncomplete:
        # רשימת seen_sids חלקית — אסור למחוק תבניות ישנות כי חלקן אולי
        # נמצאות בדפים שלא נמשכו. מתעדים את הכשל ומדלגים על prune.
        stats["pagination_complete"] = False
        stats["errors"] += 1
        logger.error(
            "sync_templates: pagination נקטע — מדלג על prune (seen=%d)",
            len(seen_sids),
        )

    if prune_deleted and stats["pagination_complete"] and seen_sids:
        try:
            stats["deleted"] = db.delete_whatsapp_templates_not_in(seen_sids)
        except Exception:
            logger.error("sync_templates: כשל במחיקת תבניות שנעלמו", exc_info=True)
            stats["errors"] += 1

    logger.info(
        "sync_templates: fetched=%d upserted=%d deleted=%d errors=%d complete=%s",
        stats["fetched"],
        stats["upserted"],
        stats["deleted"],
        stats["errors"],
        stats["pagination_complete"],
    )
    return stats
