"""
יצירת תבניות WhatsApp חדשות מהאדמין דרך Twilio Content API.

מודול זה שונה מ-`whatsapp_templates.py` — שם מתבצעת יצירה דינמית של
Quick Reply / List Picker לזרימת השיחה. כאן מדובר בתבניות broadcast
שעוברות אישור Meta, נוצרות ידנית ע"י בעל העסק דרך טופס באדמין.

Phase 1 כולל: body + (אופציונלי) Quick Reply buttons.
Phase 2 הוסיף: header_text, footer, ו-CTA buttons (URL/PHONE).

זרימה:
    spec = TemplateSpec(friendly_name="promo_2026", language="he",
                        category="MARKETING", body="שלום {{1}}, מבצע על...",
                        sample_values=["דני"], quick_reply_buttons=["מעוניין"])
    errors = validate_spec(spec)
    if errors: ...
    result = create_marketing_template(spec)  # קורא ל-Twilio + מעדכן DB
    # result["content_sid"] → ניתן לשלוח לאישור Meta דרך
    # whatsapp_templates_submit.submit_template_for_approval(...)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from messaging.twilio_content_api import (
    get_auth as _get_auth,
    content_api_url as _content_api_url,
)
# מקור יחיד לקטגוריות broadcast — מוגדר ב-database.py ומשמש גם את
# הפילטר בפאנל וגם את ה-wizard. alias כאן כדי לשמור על שמות קיימים.
from ai_chatbot.database import (
    BROADCAST_TEMPLATE_CATEGORIES as BROADCAST_CATEGORIES,
)

logger = logging.getLogger(__name__)

# מגבלות לפי תיעוד Meta WhatsApp Business Platform.
# תיעוד רשמי: https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
FRIENDLY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SUPPORTED_LANGUAGES = ("he", "en")
BODY_MAX_LEN = 1024
QUICK_REPLY_LABEL_MAX = 25
QUICK_REPLY_MAX_BUTTONS = 3
HEADER_TEXT_MAX_LEN = 60
FOOTER_MAX_LEN = 60
CTA_LABEL_MAX = 25
CTA_URL_MAX_LEN = 2000
CTA_PHONE_MAX_LEN = 20
CTA_MAX_BUTTONS = 2
CTA_TYPES = ("URL", "PHONE")
HEADER_MEDIA_TYPES = ("image", "video", "document")
# מיפוי סוג מדיה ל-extensions שמותרים. בדיקה ידידותית, לא מחליפה את
# הוולידציה האמיתית של Meta אחרי ה-upload.
_MEDIA_EXTENSIONS = {
    "image": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"),
    "video": (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"),
    "document": (".pdf",),
}
PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
# E.164 לאומי בסיסי: + ואחריו 1–15 ספרות. Twilio מחמיר יותר אבל זו
# בדיקה ידידותית לפני שליחה.
PHONE_RE = re.compile(r"^\+?[1-9]\d{1,14}$")


@dataclass
class CTAButton:
    """כפתור Call-To-Action. type: 'URL' או 'PHONE'.

    label: טקסט שיופיע על הכפתור (≤25 תווים).
    value: ה-URL או מספר הטלפון (לפי type).
    """
    type: str
    label: str
    value: str


@dataclass
class TemplateSpec:
    """מפרט תבנית broadcast.

    חוזה sample_values: position i-1 ⇒ ערך עבור {{i}}.
    כלומר sample_values[0] תמיד {{1}}, sample_values[1] תמיד {{2}}.
    זה לא תלוי בסדר ההופעה של placeholders ב-body.

    כפתורים: quick_reply_buttons ו-cta_buttons הדדית בלעדיים — Meta WABA
    לא מאפשרת ערבוב של quick reply ו-CTA באותה תבנית.
    """

    friendly_name: str
    language: str
    category: str
    body: str
    sample_values: list[str] = field(default_factory=list)
    quick_reply_buttons: list[str] = field(default_factory=list)
    # Phase 2:
    header_text: Optional[str] = None
    footer: Optional[str] = None
    cta_buttons: list[CTAButton] = field(default_factory=list)
    # Phase 3 — header media (URL חיצוני):
    # header_media_type ∈ {"image", "video", "document"}.
    # header_media_url חייב להיות URL ציבורי (Twilio נכנס ומוריד).
    # text ו-media הדדית בלעדיים — או טקסט, או מדיה, לא שניהם.
    header_media_type: Optional[str] = None
    header_media_url: Optional[str] = None


def extract_variable_indices(body: str) -> list[int]:
    """מחזיר את אינדקסי המשתנים ({{N}}) שנמצאו ב-body, לפי סדר הופעה (ייחודי)."""
    seen: list[int] = []
    for match in PLACEHOLDER_RE.finditer(body or ""):
        idx = int(match.group(1))
        if idx not in seen:
            seen.append(idx)
    return seen


def _validate_consecutive_indices(indices: list[int]) -> Optional[str]:
    """Meta דורש משתנים רציפים מ-1: {{1}}, {{2}}, {{3}} וכו'.

    מחזיר הודעת שגיאה אם הסדר לא תקין, או None אם בסדר.
    {{0}} או אינדקס שלילי נחשבים שגיאה (Twilio/Meta לא מקבלים).
    """
    if not indices:
        return None
    if any(i < 1 for i in indices):
        return (
            "משתני התבנית חייבים להתחיל מ-{{1}} ומעלה. נמצא: "
            + ", ".join("{{" + str(i) + "}}" for i in sorted(indices))
        )
    sorted_idx = sorted(indices)
    expected = list(range(1, len(sorted_idx) + 1))
    if sorted_idx != expected:
        return (
            "משתני התבנית חייבים להיות רציפים מ-{{1}} (למשל "
            "{{1}}, {{2}}, {{3}}). נמצא: "
            + ", ".join("{{" + str(i) + "}}" for i in sorted_idx)
        )
    return None


def validate_spec(spec: TemplateSpec) -> list[str]:
    """מאמת את כל השדות ומחזיר רשימת הודעות שגיאה (ריק = תקין)."""
    errors: list[str] = []

    if not spec.friendly_name:
        errors.append("חסר שם תבנית.")
    elif not FRIENDLY_NAME_RE.match(spec.friendly_name):
        errors.append(
            "שם תבנית: חייב להתחיל באות קטנה באנגלית, ולהכיל רק "
            "אותיות קטנות/מספרים/קווים תחתונים (1–64 תווים)."
        )

    if spec.language not in SUPPORTED_LANGUAGES:
        errors.append(
            f"שפה לא נתמכת: {spec.language!r}. נתמכות: "
            f"{', '.join(SUPPORTED_LANGUAGES)}."
        )

    if spec.category not in BROADCAST_CATEGORIES:
        errors.append(
            f"קטגוריה לא תקפה: {spec.category!r}. אפשרויות: "
            f"{', '.join(BROADCAST_CATEGORIES)}."
        )

    body_raw = spec.body or ""
    body_trimmed = body_raw.strip()
    if not body_trimmed:
        errors.append("גוף ההודעה חובה.")
    elif len(body_raw) > BODY_MAX_LEN:
        # מודדים את האורך שיגיע ל-Twilio (ללא strip), כי המחרוזת שנשלחת
        # ל-derive_twilio_payload ול-DB היא spec.body כפי שהוא.
        errors.append(
            f"גוף ההודעה ארוך מדי ({len(body_raw)}/{BODY_MAX_LEN} תווים)."
        )

    indices = extract_variable_indices(body_raw)
    seq_err = _validate_consecutive_indices(indices)
    if seq_err:
        errors.append(seq_err)

    # Meta דורש sample value לכל placeholder באישור — חובה כשיש משתנים.
    if indices and len(spec.sample_values) < len(indices):
        errors.append(
            f"חסרים ערכי דוגמה למשתנים — דרושים {len(indices)} ערכים, "
            f"סופקו {len(spec.sample_values)}. ערכי דוגמה משמשים את "
            "Meta לסקירת התבנית."
        )
    for i, val in enumerate(spec.sample_values[:len(indices)], start=1):
        if not str(val).strip():
            errors.append(f"ערך הדוגמה ל-{{{{{i}}}}} לא יכול להיות ריק.")

    if len(spec.quick_reply_buttons) > QUICK_REPLY_MAX_BUTTONS:
        errors.append(
            f"Quick Reply תומך עד {QUICK_REPLY_MAX_BUTTONS} כפתורים "
            f"(נמצאו {len(spec.quick_reply_buttons)})."
        )
    for i, label in enumerate(spec.quick_reply_buttons, start=1):
        label_clean = (label or "").strip()
        if not label_clean:
            errors.append(f"טקסט כפתור #{i} ריק.")
        elif len(label_clean) > QUICK_REPLY_LABEL_MAX:
            errors.append(
                f"טקסט כפתור #{i} ארוך מדי "
                f"({len(label_clean)}/{QUICK_REPLY_LABEL_MAX} תווים)."
            )

    # ── Phase 2: header / footer / CTA ─────────────────────────────────────
    if spec.header_text is not None:
        ht = spec.header_text.strip()
        if len(ht) > HEADER_TEXT_MAX_LEN:
            errors.append(
                f"Header ארוך מדי ({len(ht)}/{HEADER_TEXT_MAX_LEN} תווים)."
            )
        # Meta לא מאפשר משתנים בכותרת טקסט בפורמט HSM סטנדרטי.
        if PLACEHOLDER_RE.search(ht):
            errors.append(
                "Header text לא יכול להכיל משתנים ({{N}}). שמרו את "
                "ההתאמה האישית בגוף ההודעה."
            )

    # ── Phase 3: header media (URL חיצוני) ────────────────────────────────
    has_header_text = bool(spec.header_text and spec.header_text.strip())
    has_header_media = bool(
        spec.header_media_type and spec.header_media_url
        and spec.header_media_url.strip()
    )
    if has_header_text and has_header_media:
        errors.append(
            "Header text ו-Header media הדדית בלעדיים. בחרו או טקסט או מדיה."
        )
    if spec.header_media_type and not (spec.header_media_url
                                        and spec.header_media_url.strip()):
        errors.append("Header media: נבחר סוג אבל חסר URL.")
    if spec.header_media_url and spec.header_media_url.strip() \
            and not spec.header_media_type:
        errors.append("Header media: נמסר URL אבל חסר סוג (image/video/document).")
    if has_header_media:
        mtype = (spec.header_media_type or "").strip().lower()
        url = spec.header_media_url.strip()
        if mtype not in HEADER_MEDIA_TYPES:
            errors.append(
                f"Header media: סוג לא תקף ({spec.header_media_type!r}). "
                f"אפשרויות: {', '.join(HEADER_MEDIA_TYPES)}."
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            errors.append(
                "Header media URL חייב להתחיל ב-http:// או https://."
            )
        # סיומת קובץ — בדיקה רכה: רק אם ה-URL כולל סיומת מוכרת לסוג
        # אחר, נחשב את זה לטעות. URLs בלי סיומת (Drive, SharePoint,
        # signed URLs ללא extension גלוי) עוברים ולידציה — Twilio/Meta
        # יבדקו את ה-MIME type בעצמם בעת ההורדה.
        if mtype in _MEDIA_EXTENSIONS:
            url_path = url.lower().split("?", 1)[0]
            mismatched = []
            for other_type, exts in _MEDIA_EXTENSIONS.items():
                if other_type == mtype:
                    continue
                if any(url_path.endswith(ext) for ext in exts):
                    mismatched.append(other_type)
            if mismatched:
                errors.append(
                    f"Header media URL נראה כ-{'/'.join(mismatched)} "
                    f"אבל הסוג שנבחר הוא {mtype}. ודאו שהקובץ תואם."
                )

    footer_set = bool(spec.footer and spec.footer.strip())
    if spec.footer is not None:
        ft = spec.footer.strip()
        if len(ft) > FOOTER_MAX_LEN:
            errors.append(
                f"Footer ארוך מדי ({len(ft)}/{FOOTER_MAX_LEN} תווים)."
            )
        if PLACEHOLDER_RE.search(ft):
            errors.append("Footer לא יכול להכיל משתנים ({{N}}).")

    # twilio/text (גוף בלבד) לא נושא footer ב-Twilio Content API. אם
    # המשתמש הזין footer בלי header/buttons — נכשל את הולידציה במקום
    # להפיל אותו שקטה (תצוגה מקדימה הראתה אותו, אבל בפועל הוא נמחק).
    has_buttons = bool(spec.quick_reply_buttons or spec.cta_buttons)
    has_header = bool(spec.header_text and spec.header_text.strip()) \
        or _has_media_header(spec)
    if footer_set and not has_buttons and not has_header:
        errors.append(
            "Footer דורש כפתור (Quick Reply / CTA) או Header — אחרת "
            "Twilio שולח את ההודעה כ-text רגיל בלי footer. הוסיפו אחד מהם "
            "או הסירו את ה-footer."
        )

    # מניעת ערבוב Quick Reply + CTA — Meta WABA לא תומך בזה.
    if spec.quick_reply_buttons and spec.cta_buttons:
        errors.append(
            "אסור לערבב Quick Reply ו-CTA buttons באותה תבנית. בחרו אחד מהם."
        )

    if len(spec.cta_buttons) > CTA_MAX_BUTTONS:
        errors.append(
            f"CTA buttons תומך עד {CTA_MAX_BUTTONS} כפתורים "
            f"(נמצאו {len(spec.cta_buttons)})."
        )
    for i, btn in enumerate(spec.cta_buttons, start=1):
        btn_type = (btn.type or "").strip().upper()
        label_clean = (btn.label or "").strip()
        value_clean = (btn.value or "").strip()
        if btn_type not in CTA_TYPES:
            errors.append(
                f"CTA #{i}: סוג לא תקף ({btn.type!r}). אפשרויות: "
                f"{', '.join(CTA_TYPES)}."
            )
        if not label_clean:
            errors.append(f"CTA #{i}: חסר טקסט כפתור.")
        elif len(label_clean) > CTA_LABEL_MAX:
            errors.append(
                f"CTA #{i}: טקסט כפתור ארוך מדי "
                f"({len(label_clean)}/{CTA_LABEL_MAX} תווים)."
            )
        if not value_clean:
            errors.append(f"CTA #{i}: חסר ערך (URL או מספר טלפון).")
        elif btn_type == "URL":
            if not (value_clean.startswith("http://")
                    or value_clean.startswith("https://")):
                errors.append(
                    f"CTA #{i}: URL חייב להתחיל ב-http:// או https://."
                )
            elif len(value_clean) > CTA_URL_MAX_LEN:
                errors.append(
                    f"CTA #{i}: URL ארוך מדי "
                    f"({len(value_clean)}/{CTA_URL_MAX_LEN} תווים)."
                )
        elif btn_type == "PHONE":
            if len(value_clean) > CTA_PHONE_MAX_LEN:
                errors.append(
                    f"CTA #{i}: מספר טלפון ארוך מדי "
                    f"({len(value_clean)}/{CTA_PHONE_MAX_LEN} תווים)."
                )
            elif not PHONE_RE.match(value_clean):
                errors.append(
                    f"CTA #{i}: מספר טלפון לא תקף — דורש פורמט "
                    f"בינלאומי (למשל +972501234567)."
                )

    return errors


def _has_media_header(spec: TemplateSpec) -> bool:
    return bool(
        spec.header_media_type and spec.header_media_url
        and spec.header_media_url.strip()
    )


def _select_content_type(spec: TemplateSpec) -> str:
    """בוחר את ה-Content Type של Twilio לפי השדות במפרט.

    סדר העדיפויות (לפי תמיכה הרחבה ביותר נדרשת):
    - header_text או header_media → twilio/card (התומך בכותרות).
    - cta_buttons → twilio/call-to-action.
    - quick_reply_buttons → twilio/quick-reply.
    - אחרת → twilio/text.
    """
    if (spec.header_text and spec.header_text.strip()) \
            or _has_media_header(spec):
        return "twilio/card"
    if spec.cta_buttons:
        return "twilio/call-to-action"
    if spec.quick_reply_buttons:
        return "twilio/quick-reply"
    return "twilio/text"


def _build_quick_reply_actions(spec: TemplateSpec) -> list[dict]:
    return [
        {"title": label.strip(), "id": f"qr_{i}"}
        for i, label in enumerate(spec.quick_reply_buttons, start=1)
        if label.strip()
    ]


def _build_cta_actions(spec: TemplateSpec) -> list[dict]:
    actions: list[dict] = []
    for btn in spec.cta_buttons:
        btn_type = (btn.type or "").strip().upper()
        if btn_type == "URL":
            actions.append({
                "type": "URL",
                "title": (btn.label or "").strip(),
                "url": (btn.value or "").strip(),
            })
        elif btn_type == "PHONE":
            actions.append({
                "type": "PHONE_NUMBER",
                "title": (btn.label or "").strip(),
                "phone": (btn.value or "").strip(),
            })
    return actions


def derive_twilio_payload(spec: TemplateSpec) -> dict:
    """בונה את ה-payload ל-Twilio Content API לפי המפרט.

    מופרד מ-HTTP כדי שניתן יהיה לטסט אותו בנפרד.
    """
    indices = extract_variable_indices(spec.body)
    # Twilio מצפה ל-variables כ-{ "1": "sample", "2": "sample" }.
    # i >= 1 מגן מ-placeholder {{0}} (i-1=-1 היה מאחזר את האיבר האחרון).
    variables = {
        str(i): spec.sample_values[i - 1]
        for i in sorted(indices)
        if i >= 1 and i - 1 < len(spec.sample_values)
    }

    content_type = _select_content_type(spec)
    inner: dict = {"body": spec.body}

    # footer רלוונטי לכל הסוגים שתומכים בו (Twilio מקבל "footer" ב-payload
    # של quick-reply / call-to-action / card).
    if spec.footer and spec.footer.strip() and content_type != "twilio/text":
        inner["footer"] = spec.footer.strip()

    if content_type == "twilio/card":
        # ב-card השדה title משמש ככותרת (header text של Meta).
        if spec.header_text and spec.header_text.strip():
            inner["title"] = spec.header_text.strip()
        # Phase 3: header media — Twilio מצפה ל-media: ["url"] (רשימה).
        if _has_media_header(spec):
            inner["media"] = [spec.header_media_url.strip()]
        # על card ניתן לשים actions של quick-reply או של CTA — בודקים מה יש.
        if spec.cta_buttons:
            inner["actions"] = _build_cta_actions(spec)
        elif spec.quick_reply_buttons:
            inner["actions"] = _build_quick_reply_actions(spec)
    elif content_type == "twilio/call-to-action":
        inner["actions"] = _build_cta_actions(spec)
    elif content_type == "twilio/quick-reply":
        inner["actions"] = _build_quick_reply_actions(spec)
    # twilio/text — אין actions/header/footer

    return {
        "friendly_name": spec.friendly_name,
        "language": spec.language,
        "variables": variables,
        "types": {content_type: inner},
    }


def _build_db_buttons(spec: TemplateSpec) -> list[dict]:
    """בונה את רשימת ה-buttons לשמירה ב-DB — schema תואם sync.

    quick_reply ו-CTA הדדית בלעדיים (validation אוכף), אז רק אחד מהם
    יחזיר תוכן בכל קריאה.
    """
    buttons: list[dict] = []
    for i, label in enumerate(spec.quick_reply_buttons, start=1):
        if (label or "").strip():
            buttons.append({
                "type": "quick_reply",
                "title": label.strip(),
                "id": f"qr_{i}",
            })
    for i, btn in enumerate(spec.cta_buttons, start=1):
        btn_type = (btn.type or "").strip().upper()
        record = {
            "type": "call_to_action",
            "title": (btn.label or "").strip(),
            "id": f"cta_{i}",
        }
        if btn_type == "URL":
            record["url"] = (btn.value or "").strip()
        elif btn_type == "PHONE":
            record["phone"] = (btn.value or "").strip()
        buttons.append(record)
    return buttons


def _build_db_variables(spec: TemplateSpec) -> list[dict]:
    """בונה את רשימת ה-variables לשמירה ב-DB — schema תואם sync.

    מיון מספרי כדי שיישמרו 1,2,3,... ולא לפי סדר ההופעה ב-body
    (חשוב כש-body כולל {{2}} לפני {{1}}). i >= 1 מגן מ-{{0}}.
    """
    return [
        {
            "index": str(i),
            "name": f"variable_{i}",
            "example": spec.sample_values[i - 1]
                if i >= 1 and i - 1 < len(spec.sample_values) else "",
        }
        for i in sorted(extract_variable_indices(spec.body))
        if i >= 1
    ]


def _resolve_header_type(spec: TemplateSpec) -> str:
    """מחזיר את ה-header_type הראוי ב-DB:
    'text' אם header_text מוגדר, 'image'/'video'/'document' אם header_media,
    'none' אחרת. ה-CHECK ב-DB מקבל את כל הערכים האלה.
    """
    if spec.header_text and spec.header_text.strip():
        return "text"
    if _has_media_header(spec):
        mtype = (spec.header_media_type or "").strip().lower()
        if mtype in HEADER_MEDIA_TYPES:
            return mtype
    return "none"


def create_marketing_template(spec: TemplateSpec) -> dict:
    """יוצר תבנית ב-Twilio + מסנכרן ל-DB.

    Returns: dict עם {content_sid, friendly_name, body_text, ...}
        מוכן ל-render בפאנל. הסטטוס הראשוני הוא 'unsubmitted'.

    Raises:
        ValueError: אם המפרט לא תקין.
        RuntimeError: אם Twilio API נכשל.
    """
    errors = validate_spec(spec)
    if errors:
        raise ValueError(" | ".join(errors))

    payload = derive_twilio_payload(spec)
    auth = _get_auth()
    resp = requests.post(
        _content_api_url(),
        json=payload,
        auth=auth,
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        logger.error(
            "Twilio Content API create failed (%d): %s",
            resp.status_code, resp.text,
        )
        # מחזירים שגיאה ידידותית — Twilio לפעמים מחזיר JSON עם message.
        try:
            err_msg = resp.json().get("message") or resp.text
        except Exception:
            err_msg = resp.text
        raise RuntimeError(
            f"Twilio Content API החזירה {resp.status_code}: {err_msg}"
        )

    data = resp.json()
    content_sid = data["sid"]
    logger.info(
        "תבנית נוצרה: %s (%s)", spec.friendly_name, content_sid,
    )

    # שמירה ב-DB עם סטטוס unsubmitted — המשתמש ישלח לאישור בנפרד.
    # אם ה-upsert נכשל, אנחנו צריכים לנקות את ה-Twilio template שכבר
    # נוצר — אחרת הוא יישאר orphan ויחסום rollback (אותו friendly_name
    # תפוס) ואת ניסיונות עתידיים לאותו שם.
    from ai_chatbot import database as db
    try:
        db.upsert_whatsapp_template({
            "content_sid": content_sid,
            "friendly_name": spec.friendly_name,
            "language": spec.language,
            "category": spec.category,
            "approval_status": "unsubmitted",
            "rejection_reason": None,
            "header_type": _resolve_header_type(spec),
            "header_text": (spec.header_text or "").strip(),
            "header_media_url": (spec.header_media_url or "").strip()
                if _has_media_header(spec) else "",
            "body_text": spec.body,
            "footer_text": (spec.footer or "").strip(),
            # buttons + variables — שומרים על אותן מוסכמות key כמו ב-sync
            # (type=quick_reply/call_to_action lowercase, title, id, url/phone;
            # index, name, example) כדי שכל ה-renderers ירדו עם אותו schema.
            "buttons": _build_db_buttons(spec),
            "variables": _build_db_variables(spec),
            "content_type": _select_content_type(spec),
            "raw": data,
        })
    except Exception:
        logger.error(
            "create_marketing_template: ה-upsert ל-DB נכשל אחרי יצירה "
            "ב-Twilio (sid=%s) — מנסים למחוק את ה-orphan כדי לא לחסום "
            "rollback/retry", content_sid, exc_info=True,
        )
        try:
            from messaging.whatsapp_templates import delete_template
            delete_template(content_sid)
        except Exception:
            logger.error(
                "create_marketing_template: גם delete של ה-orphan נכשל "
                "(sid=%s) — חובה לנקות ידנית מ-Twilio Console",
                content_sid, exc_info=True,
            )
        raise

    return db.get_whatsapp_template(content_sid) or _row_after_upsert(
        content_sid, spec, data,
    )


def _row_after_upsert(content_sid: str, spec: TemplateSpec, raw: dict) -> dict:
    """Fallback אם get_whatsapp_template החזיר None (race/קריסת DB).

    בונה dict ידני עם המידע שיש לנו כדי שה-caller לא יקרוס על
    tpl['content_sid']. ה-caller יקבל את אותם המפתחות העיקריים שהיה
    מקבל מ-DB.
    """
    logger.warning(
        "get_whatsapp_template החזיר None מיד אחרי upsert עבור %s — "
        "בונים dict חלופי מהמפרט", content_sid,
    )
    return {
        "content_sid": content_sid,
        "friendly_name": spec.friendly_name,
        "language": spec.language,
        "category": spec.category,
        "approval_status": "unsubmitted",
        "rejection_reason": None,
        "header_type": _resolve_header_type(spec),
        "header_text": (spec.header_text or "").strip(),
        "header_media_url": (spec.header_media_url or "").strip()
            if _has_media_header(spec) else "",
        "body_text": spec.body,
        "footer_text": (spec.footer or "").strip(),
        "buttons": _build_db_buttons(spec),
        "variables": _build_db_variables(spec),
        "content_type": _select_content_type(spec),
        "last_synced_at": None,
    }
