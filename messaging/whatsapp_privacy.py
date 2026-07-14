"""
WhatsApp Privacy Router — תיקון 13.

מטפל בבקשות פרטיות שנכנסות כטקסט חופשי דרך WhatsApp:
1. "מחק אותי" / "ביטול הסכמה" — מחיקה מלאה עם אישור דו-שלבי
2. "המידע שלי" / "מה אתם יודעים עליי" — זכות עיון (כמו /myinfo)

מתבצע לפני ה-LLM/RAG pipeline. ההפרדה ממודול whatsapp_optout (תיקון 40)
היא מכוונת — שני המסלולים שונים משפטית:
    הסר → opt_out marketing בלבד; השירות ממשיך לעבוד.
    מחק אותי → revoke consent + delete_user_data; השירות נסגר.

UX לפי המלצת היועץ: אישור דו-שלבי במחיקה (cache 10 דקות) כדי למנוע
false-positives. בWhatsApp אין כפתורי inline אז משתמשים בטקסט.
"""

from __future__ import annotations

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# ─── Keyword sets ────────────────────────────────────────────────────────────

# מחיקה מלאה — כולל ביטול הסכמה ("השירות נסגר") ו"מחק אותי".
# נמרץ אחרי normalize, השוואה למחרוזת מלאה (לא substring) כדי למנוע
# match על "אל תמחק אותי" / "האם אתם מוחקים אותי?". הניגוד נבדק במפורש
# ב-_contains_negation למקרים שעדיין מתפספסים.
DELETE_KEYWORDS = frozenset({
    "מחק אותי",
    "מחק/י אותי",
    "תמחק אותי",
    "תמחקי אותי",
    "מחק את המידע שלי",
    "מחיקה",
    "מחק",  # קצר אבל ברור בהקשר WhatsApp; הניגוד נבדק
    "ביטול הסכמה",
    "אני מבטל הסכמה",
    "אני מבטלת הסכמה",
    "delete me",
    "delete my data",
    "forget me",
})

# עיון — בקשה לראות מה שמור על המשתמש.
ACCESS_KEYWORDS = frozenset({
    "המידע שלי",
    "מה אתם יודעים עליי",
    "מה אתם יודעים עלי",
    "מה יש לכם עליי",
    "מה יש לכם עלי",
    "המידע שיש עליי",
    "המידע שיש עלי",
    "אילו פרטים יש לכם",
    "what do you have on me",
    "my data",
    "my info",
    "myinfo",
})

# ביטוי האישור הסופי למחיקה — חייב להיות ייחודי כדי שלא יעלה בטעות
# בשיחה רגילה. "אישור מחיקה" הוא ביטוי שלא משתמשים בו אקראית.
DELETE_CONFIRMATION_PHRASE = "אישור מחיקה"

# ביטויי שלילה שמופיעים לפני keyword המחיקה — כדי שלא ניחס "אל תמחק"
# כבקשת מחיקה. נבדק על המחרוזת המנורמלת.
NEGATION_PREFIXES = ("אל ", "לא ", "אל תמחק", "לא רוצה ש", "לא צריך ש", "אסור ל")


# ─── Confirmation cache (10-minute TTL) ──────────────────────────────────────

# user_id → timestamp של כניסה ל-state "ממתין לאישור".
# in-process dict; מספיק ל-instance בודד. קצר מספיק (10 דק') שלא נחסום
# מצב נורמלי, ארוך מספיק שמשתמש שצריך זמן יוכל לאשר.
_PENDING_DELETE_TTL_SECONDS = 600
_PENDING_DELETE_MAX_TRACKED = 5_000
# מפתח: (tenant, user_id) — בקשת מחיקה אצל עסק אחד אינה חלה על אחר.
_pending_deletes: dict[tuple[str, str], float] = {}
_pending_deletes_lock = threading.Lock()


def _pending_key(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)


def register_pending_delete(user_id: str) -> None:
    """רושם שהמשתמש שלח בקשת מחיקה ועכשיו ממתין לאישור."""
    key = _pending_key(user_id)
    with _pending_deletes_lock:
        _pending_deletes[key] = time.time()
        # LRU eviction
        if len(_pending_deletes) > _PENDING_DELETE_MAX_TRACKED:
            oldest = min(_pending_deletes.items(), key=lambda kv: kv[1])[0]
            if oldest != key:
                del _pending_deletes[oldest]


def is_pending_delete(user_id: str) -> bool:
    """בודק אם המשתמש בstate של ממתין-לאישור (וה-TTL לא פג)."""
    now = time.time()
    cutoff = now - _PENDING_DELETE_TTL_SECONDS
    key = _pending_key(user_id)
    with _pending_deletes_lock:
        # cleanup רשומות פגות במקביל (על פני כל ה-tenants)
        stale = [k for k, ts in _pending_deletes.items() if ts < cutoff]
        for k in stale:
            del _pending_deletes[k]
        return key in _pending_deletes


def clear_pending_delete(user_id: str) -> None:
    """מנקה state אחרי ביצוע או ביטול מפורש."""
    with _pending_deletes_lock:
        _pending_deletes.pop(_pending_key(user_id), None)


# ─── Normalization + detection ───────────────────────────────────────────────


def _normalize(text: str) -> str:
    """lower + strip + הסרת סימני פיסוק מקצוות. לא הופך פיסוק פנימי
    כדי שלא נשבור משפטים שלמים.
    """
    if not text:
        return ""
    punctuation = "!?.,;:\"'()[]「」״׳ "
    # הקטנה למספר רווחים → רווח אחד, כדי שמשפט עם כמה רווחים יזוהה
    text = re.sub(r"\s+", " ", text)
    return text.strip().strip(punctuation).strip().lower()


_NEGATION_WORDS = frozenset({"אל", "לא", "אסור"})


def _is_word_boundary(normalized: str, idx: int) -> bool:
    """בודק אם המיקום idx ב-normalized נמצא בגבול מילה (מתחיל מילה).

    True אם idx==0 או התו לפניו הוא רווח. בעברית כשרוצים לתפוס keyword
    שמתחיל מילה, אסור לאפשר match באמצע מילה (כמו 'מחק' בתוך 'תמחק').
    """
    return idx == 0 or normalized[idx - 1] == " "


def _contains_negation(normalized: str, keyword: str) -> bool:
    """בודק אם לפני ה-keyword מופיעה מילת שלילה.

    דוגמאות שצריך לזהות כשלילה:
        "אל תמחק אותי"   → מילה אחרונה ב-prefix היא "אל"
        "לא רוצה שתמחק"  → "לא"
    """
    idx = normalized.find(keyword)
    if idx <= 0:
        return False
    prefix = normalized[:idx].strip()
    if not prefix:
        return False
    last_word = prefix.split()[-1]
    return last_word in _NEGATION_WORDS


def detect_delete_request(text: str) -> bool:
    """בקשת מחיקה מלאה / ביטול הסכמה.

    שלושה שלבים: (1) match מלא למחרוזת מנורמלת. (2) substring match
    ל-keywords ארוכים (>=8 תווים) רק בגבול מילה. (3) דחיית שלילה
    ("אל תמחק אותי") — בודק שהמילה לפני ה-keyword לא בtoken set.
    """
    normalized = _normalize(text)
    if not normalized:
        return False

    # שלב 1: match מלא — מחרוזת שלמה
    if normalized in DELETE_KEYWORDS:
        return True

    # שלב 2+3: substring match בגבול מילה, בלי שלילה לפני
    for keyword in DELETE_KEYWORDS:
        if len(keyword) < 8:
            continue  # מונע ש-"מחק" יתפוס באמצע "תמחק לי את התור"
        # find בכל המופעים — לא רק הראשון, כי הראשון יכול להיות באמצע מילה
        start = 0
        while True:
            idx = normalized.find(keyword, start)
            if idx < 0:
                break
            if _is_word_boundary(normalized, idx):
                if not _contains_negation(normalized, keyword):
                    return True
            start = idx + 1
    return False


def detect_access_request(text: str) -> bool:
    """בקשת עיון — match מלא או substring לטווח קצר."""
    normalized = _normalize(text)
    if not normalized:
        return False
    if normalized in ACCESS_KEYWORDS:
        return True
    # substring רק ל-keywords ארוכים (>= 10 תווים) כדי למנוע false-positive
    for keyword in ACCESS_KEYWORDS:
        if len(keyword) >= 10 and keyword in normalized:
            return True
    return False


def detect_delete_confirmation(text: str) -> bool:
    """אישור סופי למחיקה — match מדויק לביטוי 'אישור מחיקה'."""
    return _normalize(text) == DELETE_CONFIRMATION_PHRASE.lower()


# ─── User-facing message templates (לפי ניסוח היועץ) ─────────────────────────

# מסך 1 מתוך 2 — אזהרה + הסבר על מה שנשאר
DELETE_WARNING_TEMPLATE = (
    "⚠️ *מחיקת המידע שלך*\n\n"
    "ברגע שתאשר/י, נמחק את כל המידע שלך מהמערכת: "
    "השיחות, התורים, ההעדפות, פרטי הקשר, וכל מה שנגזר מהם.\n\n"
    "נשמור רשומה מצומצמת ומאובטחת אחת בלבד שמתעדת את אירוע ההסכמה "
    "ואת הביטול. הרשומה לא כוללת את שמך, הטלפון או תוכן השיחות — "
    "רק את עצם האירוע, התאריך, וזיהוי טכני סגור.\n\n"
    "הרשומה נשמרת עד 5 שנים ואז נמחקת אוטומטית.\n\n"
    "{privacy_link_line}"
)

# מסך 2 מתוך 2 — הוראת אישור (הודעה נפרדת כדי שלא תיחתך ב-WhatsApp)
DELETE_CONFIRMATION_PROMPT = (
    f"להמשך, כתוב/י את הביטוי הבא בדיוק:\n\n*{DELETE_CONFIRMATION_PHRASE}*\n\n"
    "(התוקף: 10 דקות)"
)

DELETE_COMPLETED_TEMPLATE = (
    "✅ המידע שלך נמחק מהמערכת ({total} רשומות).\n\n"
    "אם תפנה/י אלינו שוב — נצטרך לבקש את הסכמתך מחדש."
)

DELETE_NO_DATA_MESSAGE = (
    "✅ אין מידע השמור עליך במערכת.\n\n"
    "אם תפנה/י אלינו שוב — נצטרך לבקש את הסכמתך מחדש."
)

DELETE_ALREADY_IN_PROGRESS = (
    "⏳ בקשת המחיקה שלך כבר בטיפול. תקבל/י אישור בעוד רגע."
)

DELETE_PARTIAL_FAILURE = (
    "✅ המידע שלך נמחק ברובו ({total} רשומות), אבל היו מספר טבלאות "
    "שלא נמחקו עקב תקלה זמנית. צוות התמיכה יוודא שזה יושלם תוך 24 שעות."
)

# כל ה-DELETEs נכשלו — חובה להיות שקוף, אסור לומר "המידע נמחק" או
# "אין מידע" כשבפועל הכל נשאר. ה-ledger כבר תיעד deletion_failed.
DELETE_FAILED_MESSAGE = (
    "⚠️ אירעה תקלה זמנית במחיקת המידע. הבקשה שלך נרשמה אצלנו "
    "ותטופל תוך 24 שעות.\n\n"
    "אם לא קיבלת אישור עד אז — שלח/י *מחק אותי* שוב, "
    "או פנה/י לבעל העסק."
)


def build_delete_warning(privacy_link: str = "") -> str:
    """בונה את הודעת האזהרה עם קישור למדיניות (אם ADMIN_URL מוגדר)."""
    link_line = (
        f"🔒 הסבר מלא: {privacy_link}\n\n" if privacy_link else ""
    )
    return DELETE_WARNING_TEMPLATE.format(privacy_link_line=link_line)


def format_access_summary(summary: dict) -> str:
    """גרסת WhatsApp של /myinfo — plain text עם *bold*. לא HTML.

    מחזיר טקסט קצר שמתאר מה שמור על המשתמש, ללא חשיפת תוכן חופשי
    (user_notes, lead_followups.analysis_json) — counts בלבד.
    """
    if not summary.get("exists"):
        return (
            "🔍 לא מצאנו מידע השמור עליך במערכת.\n\n"
            "אם זו טעות — צרו קשר ונבדוק."
        )

    lines = ["🔍 *המידע השמור עליך במערכת*", ""]

    if summary.get("username"):
        lines.append(f"• שם משתמש: {summary['username']}")
    if summary.get("first_seen_at"):
        lines.append(f"• פנייה ראשונה: {summary['first_seen_at']}")
    if summary.get("last_active_at"):
        lines.append(f"• פעילות אחרונה: {summary['last_active_at']}")

    consent_at = summary.get("consent_given_at") or "לא תועדה הסכמה"
    lines.append(f"• הסכמה למדיניות: {consent_at}")

    appts = summary.get("appointments") or {}
    total_appts = int(appts.get("total") or 0)
    if total_appts:
        lines.append("")
        lines.append(f"📅 תורים: {total_appts}")

    convo_total = int(summary.get("conversations_total") or 0)
    if convo_total:
        lines.append(f"💬 הודעות בשיחות: {convo_total}")

    lf = summary.get("lead_followups") or {}
    lf_total = int(lf.get("total") or 0)
    if lf_total:
        lines.append(f"🤖 ניתוחי AI אוטומטיים על השיחה: {lf_total}")

    if summary.get("subscribed"):
        lines.append("📬 רשום/ה לקבלת שידורים: כן")

    bd_total = int(summary.get("broadcast_deliveries_total") or 0)
    if bd_total:
        lines.append(f"📨 הודעות שידור שנשלחו: {bd_total}")

    # סטטוס חסימה — למשתמש מותר לדעת שהוא חסום וברמת קטגוריה (תיקון 13).
    if summary.get("blocked"):
        bs = summary.get("block_status") or {}
        category_he = {
            "abuse": "התנהגות לא הולמת",
            "spam": "ספאם",
            "repeated_no_show": "אי-הופעה חוזרת לתורים",
            "manual": "החלטה של בעל העסק",
        }.get(bs.get("block_category"), "לא מוגדר")
        lines.append("")
        lines.append("🚫 *סטטוס חסימה:* חשבונך מוגבל מלהשתמש בשירות.")
        lines.append(f"   קטגוריה: {category_he}")
        if bs.get("blocked_month"):
            lines.append(f"   מועד: {bs['blocked_month']}")
        if bs.get("appeal_contact_method"):
            lines.append(f"   לערעור: {bs['appeal_contact_method']}")

    # הערות לקוח — נחשפות לפי ברירת מחדל (תיקון 13). withhold_reason
    # מאפשר חריג נקודתי שבו בעל העסק החליט לא לחשוף.
    note_text = summary.get("user_note_text") or ""
    if note_text:
        lines.append(f"📝 הערת בעל העסק: {note_text}")
    elif summary.get("user_note_withheld"):
        lines.append("📝 קיימת הערה פנימית (חסויה — לפנות במייל לפירוט)")
    elif summary.get("has_user_note"):
        lines.append("📝 קיימת הערה של בעל העסק")

    lines.append("")
    lines.append("ℹ️ למחיקה — שלח/י *מחק אותי*")

    return "\n".join(lines)
