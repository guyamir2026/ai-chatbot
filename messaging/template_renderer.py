"""
WhatsApp Template Renderer — החלפת משתנים ובדיקות preview.

רקע:
    תבניות WhatsApp HSM משתמשות ב-placeholders בסגנון Twilio: {{1}}, {{2}}...
    הקורא מהאדמין מזין ערכים ידניים (stage 2: literal בלבד), ומנוע זה
    מייצר מראה מקדים של ההודעה הסופית + בדיקות תקינות.

    בשלב 3 נתמוך ב-user-field substitution (למשל user:first_name) שייפתר
    per-recipient בזמן שליחה. עד אז ה-preview מראה את הערכים הליטרליים.

שימוש:
    from messaging.template_renderer import render_preview, substitute_variables

    template = db.get_whatsapp_template("HX111")   # dict עם body_text/buttons/...
    preview = render_preview(template, {"1": "דני", "2": "תספורת"})
    # preview["body"] == "היי דני, תור ל-תספורת ..."
    # preview["missing_variables"] == []
"""

from __future__ import annotations

import html
import logging
import re

logger = logging.getLogger(__name__)

# רגקס זהה ל-placeholder של Twilio/WhatsApp (מיושם גם ב-sync).
_VAR_RE = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")

# ── User-field substitution ─────────────────────────────────────────────────
# תמיכה ב-{{user:FIELD}} בתוך ערכי מיפוי משתנים — מאפשר הודעות אישיות.
# "היי {{user:username}}, תודה!" ייפתר per-recipient לפני השליחה ל-Twilio.
_USER_FIELD_RE = re.compile(r"\{\{\s*user:(\w+)\s*\}\}")

# שדות משתמש מותרים. הרחבה דורשת עדכון ב-get_user_info_for_broadcast.
ALLOWED_USER_FIELDS = frozenset({"username", "user_id", "phone"})


def substitute_user_fields(text: str, user_row: dict) -> str:
    """החלפת {{user:FIELD}} בערכים של user_row.

    שדות מותרים: username (שם הלקוח), user_id (מזהה גולמי), phone
    (מספר בפורמט ישראלי מקומי 0XXXXXXXXX אם מתחיל ב-+972).

    שדה לא מוכר → משאירים את ה-placeholder כפי שהוא (ה-UI יסמן זאת
    למנהל דרך preview — "{{user:xxx}}" יופיע literal).
    """
    if not text or "{{" not in text or "user:" not in text:
        return text or ""

    def _replace(match: re.Match) -> str:
        field = match.group(1)
        if field not in ALLOWED_USER_FIELDS:
            return match.group(0)
        if field == "phone":
            raw = user_row.get("user_id", "") or ""
            # format_phone ממיר +972XXXXXXXXX → 0XXXXXXXXX; ערכים אחרים
            # (BSUID, מספרים זרים) מחוזרים כמו שהם
            try:
                from utils.phone import format_phone
                return str(format_phone(raw))
            except Exception:
                return str(raw)
        value = user_row.get(field, "")
        return str(value or "")

    return _USER_FIELD_RE.sub(_replace, text)

# ── WhatsApp markdown → HTML ─────────────────────────────────────────────────
# WhatsApp מפרש תחביר markdown מצומצם (*bold*, _italic_, ~strike~, `code`).
# החוקים: הסמן חייב להיות ב-word boundary — לא בתוך מילה. "5*3=15" לא נהפך
# ל-bold; "*דני*" כן. שימוש ב-\w (Unicode) כדי שגם עברית תיחשב תו-מילה.

# `code` — נטפל בזה ראשון כי תוכן code לא אמור להתפרש כ-markdown נוסף.
_WA_CODE_RE = re.compile(r"`([^`\n]+?)`")
# *bold* — לא ב-word boundary, לא ריק, לא \n באמצע.
_WA_BOLD_RE = re.compile(r"(?<!\w)\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\w)")
# _italic_
_WA_ITALIC_RE = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")
# ~strike~
_WA_STRIKE_RE = re.compile(r"(?<!\w)~(?=\S)([^~\n]+?)(?<=\S)~(?!\w)")


def wa_markdown_to_html(text: str) -> str:
    """המרה של תחביר WhatsApp (``*bold*``, ``_italic_``, ``~strike~``, `` `code` ``) ל-HTML.

    הקלט נעבר קודם דרך html.escape כדי למנוע הזרקת HTML מה-body של התבנית
    (או מערכי משתנים מרונדרים). הפלט מסומל כ-safe ע"י הקורא (Jinja2).

    מיועד לתצוגה מקדימה בפאנל האדמין בלבד — WhatsApp עצמה מפרשת את
    ה-markdown ישירות כשהיא מקבלת את ההודעה דרך Twilio.
    """
    if not text:
        return ""
    # 1. escape הכל כדי שלא יוזרק HTML מגוף התבנית/ערכי משתנים.
    escaped = html.escape(text)

    # 2. code — מוצאים תחילה ומחליפים ב-placeholders ייחודיים, כדי שהתוכן
    # בתוכם לא יעבור עיבוד נוסף (למשל "`*not bold*`" לא יהפוך ל-bold).
    code_blocks: list[str] = []

    def _capture_code(match: re.Match) -> str:
        code_blocks.append(match.group(1))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    escaped = _WA_CODE_RE.sub(_capture_code, escaped)

    # 3. bold / italic / strike — סדר לא משנה כי כל אחד משתמש בסמן שונה.
    escaped = _WA_BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _WA_ITALIC_RE.sub(r"<em>\1</em>", escaped)
    escaped = _WA_STRIKE_RE.sub(r"<del>\1</del>", escaped)

    # 4. שחזור code blocks מה-placeholders.
    for i, content in enumerate(code_blocks):
        escaped = escaped.replace(f"\x00CODE{i}\x00", f"<code>{content}</code>")

    return escaped


# אורך מקסימלי של body ב-WhatsApp — מעליו ההודעה תידחה ע"י Twilio.
_WA_BODY_SOFT_LIMIT = 1024
_WA_BODY_HARD_LIMIT = 1600

# מגבלות כפתורים — לא ניתן לשנות ב-render, אבל שווה לסמן ב-preview
# כאזהרה אם התבנית עצמה (מסונכרנת) חורגת.
_MAX_QUICK_REPLY_BUTTONS = 3
_MAX_LIST_PICKER_ITEMS = 10


def substitute_variables(text: str, values: dict) -> str:
    """החלפת {{N}} בערכים מה-mapping. משתנים חסרים נשארים {{N}} ב-output.

    Args:
        text: טקסט עם placeholders (למשל "היי {{1}}").
        values: מילון {"1": "דני", ...}. מפתחות יכולים להיות str או int.

    Returns:
        מחרוזת עם הערכים מוחלפים.
    """
    if not text:
        return ""

    # normalize keys to str כדי לתמוך גם ב-{1: "x"} וגם ב-{"1": "x"}
    normalized = {str(k): v for k, v in (values or {}).items()}

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key in normalized:
            val = normalized[key]
            # None או מחרוזת ריקה — משאירים placeholder כדי שה-UI יסמן חסר
            if val is None or str(val).strip() == "":
                return match.group(0)
            return str(val)
        return match.group(0)

    return _VAR_RE.sub(_replace, text)


def find_missing_variables(template: dict, values: dict) -> list[str]:
    """רשימת אינדקסי משתנים שהתבנית מצהירה עליהם אך חסר להם ערך."""
    required = [str(v["index"]) for v in (template.get("variables") or []) if v.get("index") is not None]
    normalized = {str(k): v for k, v in (values or {}).items()}
    missing: list[str] = []
    for idx in required:
        if idx not in normalized:
            missing.append(idx)
            continue
        val = normalized[idx]
        if val is None or str(val).strip() == "":
            missing.append(idx)
    return missing


def render_preview(template: dict, variable_values: dict) -> dict:
    """יצירת תצוגה מקדימה של תבנית עם ערכי משתנים.

    Args:
        template: dict כפי שמוחזר מ-db.get_whatsapp_template (עם body_text,
                  footer_text, buttons, variables, header_type, approval_status).
        variable_values: {"1": "דני", ...}.

    Returns:
        dict עם:
          body (str): body מרונדר
          footer (str): footer מרונדר
          buttons (list): כפתורים (ללא רינדור משתנים — הטקסט שלהם קבוע)
          header_type (str): none | text | image | video | document | location
          missing_variables (list[str]): אינדקסי משתנים חסרים
          warnings (list[str]): אזהרות תקינות לצגה ב-UI
          body_length (int): אורך ה-body המרונדר — לבדיקת חריגה
          can_send (bool): האם ניתן לשלוח (approved + אין missing)
    """
    body = substitute_variables(template.get("body_text", ""), variable_values)
    footer = substitute_variables(template.get("footer_text", "") or "", variable_values)
    missing = find_missing_variables(template, variable_values)

    warnings: list[str] = []

    if missing:
        warnings.append(
            f"חסרים ערכים למשתנים: {', '.join('{{' + i + '}}' for i in missing)}"
        )

    approval_status = template.get("approval_status") or "unsubmitted"
    if approval_status != "approved":
        warnings.append(
            f"התבנית במצב '{approval_status}' — לא ניתן לשלוח broadcast "
            "מחוץ לחלון 24 שעות עד שהתבנית תאושר ע\"י Meta."
        )

    body_length = len(body)
    if body_length > _WA_BODY_HARD_LIMIT:
        warnings.append(
            f"אורך ה-body ({body_length}) חורג מהמגבלה הקשה של WhatsApp "
            f"({_WA_BODY_HARD_LIMIT}). ההודעה תידחה ע\"י Twilio."
        )
    elif body_length > _WA_BODY_SOFT_LIMIT:
        warnings.append(
            f"אורך ה-body ({body_length}) מתקרב למגבלה ({_WA_BODY_HARD_LIMIT})."
        )

    buttons = list(template.get("buttons") or [])
    quick_reply_count = sum(1 for b in buttons if b.get("type") == "quick_reply")
    list_item_count = sum(1 for b in buttons if b.get("type") == "list_item")
    if quick_reply_count > _MAX_QUICK_REPLY_BUTTONS:
        warnings.append(
            f"יותר מדי Quick Reply ({quick_reply_count}) — WhatsApp מגביל "
            f"ל-{_MAX_QUICK_REPLY_BUTTONS}."
        )
    if list_item_count > _MAX_LIST_PICKER_ITEMS:
        warnings.append(
            f"יותר מדי פריטי List Picker ({list_item_count}) — WhatsApp "
            f"מגביל ל-{_MAX_LIST_PICKER_ITEMS}."
        )

    can_send = (
        not missing
        and approval_status == "approved"
        and body_length <= _WA_BODY_HARD_LIMIT
    )

    return {
        "body": body,
        "body_html": wa_markdown_to_html(body),
        "footer": footer,
        "footer_html": wa_markdown_to_html(footer),
        "buttons": buttons,
        "header_type": template.get("header_type") or "none",
        "missing_variables": missing,
        "warnings": warnings,
        "body_length": body_length,
        "can_send": can_send,
    }
