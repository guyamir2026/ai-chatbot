"""
Web Admin Panel — Flask application for business owners to manage the chatbot.

Features:
- Dashboard with stats
- Knowledge Base management (CRUD)
- Conversation logs viewer
- Agent request notifications
- Appointment management
- Rebuild RAG index
"""

import hmac
import html as _html
import io
import json
import logging
import os
import re
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    g,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    session,
    send_file,
)

from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from tenancy import DEFAULT_TENANT, set_current_tenant, reset_current_tenant
from ai_chatbot import database as db
from ai_chatbot.config import (
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    ADMIN_PASSWORD_HASH,
    ADMIN_SECRET_KEY,
    ADMIN_HOST,
    ADMIN_PORT,
    BUSINESS_ID,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_BOT_USERNAME,
    DEVELOPER_PASSWORD,
    DEMO_MODE,
    DEMO_CTA_WHATSAPP,
    DEMO_LIVE_BOT_URL,
    TONE_DEFINITIONS,
    TONE_LABELS,
    FOLLOW_UP_ENABLED,
    WEBHOOK_SECRET,
    build_system_prompt,
    get_business_config,
)
from ai_chatbot.rag.engine import rebuild_index, mark_index_stale, is_index_stale, retrieve
from ai_chatbot import feature_flags
from ai_chatbot import plans_config
from ai_chatbot.live_chat_service import LiveChatService, send_message_by_channel, send_telegram_message
from ai_chatbot.referral_service import try_send_referral_code
from ai_chatbot.appointment_notifications import notify_appointment_status
from ai_chatbot.vacation_service import VacationService
from ai_chatbot.business_hours import DAY_NAMES_HE
from ai_chatbot.developer_alerts import detect_active_channel

logger = logging.getLogger(__name__)

VALID_AGENT_REQUEST_STATUSES = {"pending", "handled", "dismissed"}
VALID_APPOINTMENT_STATUSES = {"pending", "confirmed", "cancelled", "passed"}

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# ביטוי רגולרי לפורמט שעה תקין (00:00–23:59)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _is_valid_time(val: str | None) -> bool:
    """בודק אם מחרוזת היא שעה חוקית בפורמט HH:MM (00:00–23:59)."""
    return val is None or val == "" or bool(_TIME_RE.match(val))

CATEGORY_TRANSLATION = {
    "Staff": "הצוות",
    "Services": "שירותים",
    "Promotions": "הטבות",
    "Pricing": "מחירון",
    "Policies": "מדיניות",
    "Location": "מיקום",
    "Hours": "שעות",
    "FAQ": "שאלות נפוצות",
}

STATUS_TRANSLATION = {
    "pending": "ממתין",
    "handled": "טופל",
    "dismissed": "נדחה",
    "confirmed": "מאושר",
    "cancelled": "בוטל",
    "passed": "עבר",
}

# שלב 7 — תרגום סוגים/סטטוסים של customer_facts.
# Dict נפרד מ-STATUS_TRANSLATION כדי לא לערבב — של agent_requests חופף שמית
# (pending/handled/dismissed) ויצירת mapping אחד הייתה גורמת לקונפליקטים.
FACT_TYPE_TRANSLATION = {
    "preference": "העדפה",
    "personal_info": "מידע אישי",
    "relationship": "מערכת יחסים",
    "open_issue": "נושא פתוח",
    "vocabulary": "כינוי",
}
FACT_STATUS_TRANSLATION = {
    "active": "פעיל",
    "pending_approval": "ממתין לאישור",
    "rejected": "נדחה",
    "superseded": "מוחלף",
    "resolved": "נסגר",
}


def _format_il_datetime(value: str) -> str:
    """Format a UTC datetime string to Israel time as DD/MM/YYYY HH:MM."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ISRAEL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return value


def _format_il_datetime_local(value: str) -> str:
    """\u05e4\u05d5\u05e8\u05de\u05d8 \u05ea\u05d0\u05e8\u05d9\u05da/\u05e9\u05e2\u05d4 \u05e9\u05db\u05d1\u05e8 \u05e0\u05e9\u05de\u05e8 \u05d1\u05e9\u05e2\u05d5\u05df \u05d9\u05e9\u05e8\u05d0\u05dc (\u05db\u05de\u05d5 scheduled_at).

    \u05d0\u05d9\u05df \u05d4\u05de\u05e8\u05ea timezone \u2014 \u05de\u05e6\u05d9\u05d2 \u05d0\u05ea \u05d4\u05e2\u05e8\u05da \u05db\u05e4\u05d9 \u05e9\u05d4\u05d5\u05d0 \u05d1\u05e4\u05d5\u05e8\u05de\u05d8 DD/MM/YYYY HH:MM.
    """
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        # \u05d9\u05d9\u05ea\u05db\u05df \u05e9\u05d4\u05e4\u05d5\u05e8\u05de\u05d8 \u05d4\u05d5\u05d0 \u05e8\u05e7 'YYYY-MM-DD HH:MM' \u2014 \u05e0\u05e0\u05e1\u05d4 \u05d2\u05dd \u05d6\u05d4
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
            return dt.strftime("%d/%m/%Y %H:%M")
        except (ValueError, TypeError):
            return value


def _format_il_date(value: str) -> str:
    """המרת תאריך מפורמט YYYY-MM-DD לפורמט ישראלי DD/MM/YYYY."""
    if not value:
        return ""
    try:
        parts = value.split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (ValueError, TypeError, IndexError):
        pass
    return value


def _format_il_phone(value: str) -> str:
    """המרת טלפון בינלאומי 972 לפורמט ישראלי מקומי.

    +972543978620 → 0543978620
    972543978620  → 0543978620
    תומך גם בקלט שכבר מפורמט (0543978620 נשאר כמו שהוא).
    מספרים שאינם ישראליים מוחזרים כמו שהם — שומרים את הפורמט המקורי.

    מאחורי הקלעים מואצל ל-utils.phone.format_phone שמכיל את הלוגיקה
    האודיטית (כולל מקרה הקצה 9720XXX שלא נוגעים בו).
    """
    if not value:
        return ""
    from utils.phone import format_phone
    return format_phone(str(value))


def _format_relative_time(value: str) -> str:
    """המרת timestamp לזמן יחסי בעברית (לפני X דקות, אתמול, וכו').

    עד שבוע — זמן יחסי. מעל שבוע — פורמט מלא DD-MM-YYYY HH:MM.
    """
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ISRAEL_TZ)
    except (ValueError, TypeError):
        return value

    now = datetime.now(ISRAEL_TZ)
    diff = now - dt

    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        # זמן עתידי — מציגים פורמט מלא
        return _format_il_datetime(value)

    if total_seconds < 60:
        return "עכשיו"

    minutes = total_seconds // 60
    if minutes < 60:
        return f"לפני {minutes} דקות" if minutes > 1 else "לפני דקה"

    hours = total_seconds // 3600
    if hours < 24:
        return f"לפני {hours} שעות" if hours > 1 else "לפני שעה"

    days = diff.days
    if days == 1:
        return f"אתמול בשעה {dt.strftime('%H:%M')}"
    if days < 7:
        return f"לפני {days} ימים"

    # מעל שבוע — פורמט מלא
    return _format_il_datetime(value)


def _translate_category(value: str) -> str:
    """Translate an English KB category name to Hebrew."""
    return CATEGORY_TRANSLATION.get(value, value)


def _translate_status(value: str) -> str:
    """Translate an English status to Hebrew."""
    return STATUS_TRANSLATION.get(value, value)


def _validate_admin_security_config() -> None:
    if not ADMIN_SECRET_KEY:
        raise RuntimeError(
            "ADMIN_SECRET_KEY must be set (required for session + CSRF protection)."
        )
    if not ADMIN_USERNAME:
        raise RuntimeError("ADMIN_USERNAME must be set.")
    if not (ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
        raise RuntimeError(
            "Either ADMIN_PASSWORD_HASH (recommended) or ADMIN_PASSWORD must be set."
        )


def _verify_admin_credentials(username: str, password: str) -> bool:
    # קוראים מהמודול ישירות — כדי לתפוס עדכונים שנעשו בזמן ריצה דרך /bot-config
    import ai_chatbot.config as _cfg

    if not username or not password:
        return False

    username_ok = hmac.compare_digest(str(username), str(_cfg.ADMIN_USERNAME))

    # Always perform the password check to avoid a timing oracle that can
    # distinguish "wrong username" from "right username, wrong password".
    if _cfg.ADMIN_PASSWORD_HASH:
        try:
            password_ok = check_password_hash(_cfg.ADMIN_PASSWORD_HASH, str(password))
        except Exception:
            password_ok = False
    else:
        password_ok = hmac.compare_digest(str(password), str(_cfg.ADMIN_PASSWORD))

    return username_ok and password_ok


def _is_developer_access_enabled() -> bool:
    """
    האם איזור ה-/dev/* נגיש בכלל. אם DEVELOPER_PASSWORD לא מוגדר — לא.
    הקריאה דרך המודול כדי לתפוס עדכוני env בזמן ריצה.
    """
    import ai_chatbot.config as _cfg
    return bool((_cfg.DEVELOPER_PASSWORD or "").strip())


def _verify_developer_password(password: str) -> bool:
    """
    אימות סיסמת מפתח. השוואה מוגנת מפני timing attacks. אם הסיסמה
    לא מוגדרת ב-env — תמיד נכשל.
    """
    import ai_chatbot.config as _cfg
    expected = (_cfg.DEVELOPER_PASSWORD or "").strip()
    if not expected:
        return False
    if not password:
        return False
    return hmac.compare_digest(str(password), str(expected))


def _safe_redirect_back(default_url: str) -> str:
    """
    Return a safe same-origin redirect target derived from Referer, or a default.
    """
    ref = request.referrer
    if not ref:
        return default_url
    try:
        ref_url = urlparse(ref)
        host_url = urlparse(request.host_url)
        if ref_url.scheme in ("http", "https") and ref_url.netloc == host_url.netloc:
            path = ref_url.path or "/"
            # Prevent protocol-relative redirects (e.g. "//evil.com") and require an absolute path.
            if not path.startswith("/") or path.startswith("//"):
                return default_url
            return f"{path}?{ref_url.query}" if ref_url.query else path
    except Exception:
        return default_url
    return default_url


# תגיות HTML שטלגרם תומך בהן — מותרות לתצוגה בפאנל (ללא מאפיינים)
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "em", "strong"}
_ALLOWED_TAG_RE = re.compile(
    r"<(/?)(\w+)(\s[^>]*)?>",
    re.IGNORECASE,
)
# מאפשר רק href עם http/https בתגית <a>
_SAFE_HREF_RE = re.compile(r'^\s*href\s*=\s*"(https?://[^"]*)"\s*$', re.IGNORECASE)


# ── WhatsApp markdown — *bold*, _italic_, ~strike~ ──────────────────────────
# WhatsApp משתמש בכוכבית/קו תחתון/טילדה בודדים ליצירת bold/italic/strike,
# בעוד שטלגרם משתמש ב-HTML ישירות. הודעות שנשלחו ב-WhatsApp נשמרות ב-DB
# עם הסימון של WhatsApp, וצריך להמיר ל-HTML לפני התצוגה בפאנל — אחרת
# הלקוח רואה כוכביות וקווים תחתונים גולמיים במקום עיצוב.
# הומרה רק כש: לפני המסמן יש תחילת מחרוזת/רווח/פיסוק, ואחריו אותו דבר —
# כדי לא לעוות זיהוי לא רצוי בתוך מילים (כמו snake_case או 5*2).
_WA_BOLD_RE = re.compile(
    r"(^|[\s(\[\.,!?])\*(?!\s)([^*\n]{1,200}?)(?<!\s)\*(?=$|[\s)\]\.,!?])",
)
_WA_ITALIC_RE = re.compile(
    r"(^|[\s(\[\.,!?])_(?!\s)([^_\n]{1,200}?)(?<!\s)_(?=$|[\s)\]\.,!?])",
)
_WA_STRIKE_RE = re.compile(
    r"(^|[\s(\[\.,!?])~(?!\s)([^~\n]{1,200}?)(?<!\s)~(?=$|[\s)\]\.,!?])",
)


def _convert_whatsapp_markdown(text: str) -> str:
    """המרת WhatsApp markdown ל-HTML tags. נקרא לפני _telegram_html."""
    if not text:
        return text
    text = _WA_BOLD_RE.sub(r"\1<b>\2</b>", text)
    text = _WA_ITALIC_RE.sub(r"\1<i>\2</i>", text)
    text = _WA_STRIKE_RE.sub(r"\1<s>\2</s>", text)
    return text


def _telegram_html(text: str, channel: str | None = None) -> str:
    """פילטר Jinja2: מציג תגיות עיצוב של טלגרם כ-HTML, ומסנן את השאר.

    channel — אופציונלי. כש-'whatsapp', גם ממירים את WhatsApp markdown
    (*bold*, _italic_, ~strike~) ל-HTML. ערוצים אחרים — בלי המרה, כדי לא
    לשבש הודעות טלגרם תמימות (משתמש שכותב *50* או _my_var_ לא מתכוון
    ל-bold/italic).

    תגיות עם מאפיינים נחסמות (מניעת XSS דרך onclick, javascript: וכו'),
    למעט <a href="https://..."> שמותר עם כתובת http/https בלבד.

    תגים יתומים: משתמש שכתב למשל "<u>" כדוגמה בתוך הודעה (בלי "</u>")
    היה גורם לקו תחתון "לדלוף" לכל טקסט שאחריו בפאנל. כדי למנוע זאת
    עוקבים אחרי מחסנית של תגים פתוחים וסוגרים בסוף את כל מה שנשאר פתוח.
    תג סגירה שלא תואם לראש המחסנית — מושלך (לא escape, כדי לא להציג
    סמלי תג גולמיים למשתמש הפאנל).
    """
    from markupsafe import Markup, escape

    if not text:
        return text

    # המרת WhatsApp markdown ל-HTML — *רק* כשהמסר נשלח בערוץ WhatsApp.
    # הודעות טלגרם נשמרות כטקסט גולמי, וכוכבית/קו תחתון בודדים שם
    # אינם סימני עיצוב — אסור לפרש "*50*" או "_my_var_" כ-bold/italic.
    if channel == "whatsapp":
        text = _convert_whatsapp_markdown(text)

    parts: list[str] = []
    last_end = 0
    open_stack: list[str] = []

    for match in _ALLOWED_TAG_RE.finditer(text):
        tag_name = match.group(2).lower()
        slash = match.group(1)  # "/" לתגית סגירה, "" לפתיחה
        attrs = match.group(3)  # מאפיינים (כולל רווח מוביל) או None
        # טקסט לפני התגית — escape
        parts.append(str(escape(text[last_end:match.start()])))
        if tag_name not in _ALLOWED_TAGS:
            # תגית לא מותרת — escape
            parts.append(str(escape(match.group(0))))
        elif attrs and attrs.strip():
            # תגית מותרת עם מאפיינים — חוסמים הכל חוץ מ-href בטוח על <a>
            if tag_name == "a" and not slash and _SAFE_HREF_RE.match(attrs):
                href = _SAFE_HREF_RE.match(attrs).group(1)
                # escape לשמירת & כ-&amp; — מונע פענוח לא רצוי של HTML entities בכתובת
                parts.append(f'<a href="{escape(href)}">')
                open_stack.append("a")
            else:
                # תגית עם מאפיינים לא בטוחים — escape
                parts.append(str(escape(match.group(0))))
        elif slash:
            # תג סגירה מותר ללא מאפיינים — רק אם תואם לראש המחסנית
            if open_stack and open_stack[-1] == tag_name:
                open_stack.pop()
                parts.append(f"</{tag_name}>")
            # תג סגירה יתום — מתעלמים
        else:
            # תג פתיחה מותר ללא מאפיינים
            open_stack.append(tag_name)
            parts.append(f"<{tag_name}>")
        last_end = match.end()

    # טקסט שנשאר אחרי התגית האחרונה
    parts.append(str(escape(text[last_end:])))

    # סגירת תגים פתוחים שנותרו — מונע "דליפה" של עיצוב להודעות הבאות
    while open_stack:
        parts.append(f"</{open_stack.pop()}>")

    # המרת מעברי שורה ל-<br> כדי לשמור על העיצוב המקורי מטלגרם
    return Markup("".join(parts).replace("\n", "<br>"))


# ─── Login Rate Limiting ───────────────────────────────────────────────────
# הגבלת ניסיונות התחברות — 5 ניסיונות כושלים לכל IP בחלון של 15 דקות
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_TRACKED_IPS = 1_000
# dict רגיל (לא defaultdict) — מונע יצירת רשומות ריקות ב-check
_login_attempts: dict[str, list[float]] = {}

# Rate limiting נפרד לכניסות /dev/login — כדי שתוקפים לא יוכלו להציף את
# כניסת האדמין הרגילה ובאותה הזדמנות לחסום את כניסת המפתח (DoS עקיף).
# מגבלה הדוקה יותר (3 ניסיונות / שעה) כי אין צורך לגיטימי לניסיונות חוזרים.
_DEV_LOGIN_MAX_ATTEMPTS = 3
_DEV_LOGIN_WINDOW_SECONDS = 60 * 60
_dev_login_attempts: dict[str, list[float]] = {}

# Rate limiting ל-/demo entry — endpoint אנונימי פתוח לאינטרנט. גולש לגיטימי
# מהמודעה ייכנס פעם או פעמיים; 20/שעה לכל IP מספיק בנדיבות ומונע sweeping
# scripts ו-session enumeration.
_DEMO_ENTRY_MAX_ATTEMPTS = 20
_DEMO_ENTRY_WINDOW_SECONDS = 60 * 60
_demo_entry_attempts: dict[str, list[float]] = {}

# משך session של מפתח — קצר יותר משל admin רגיל (30 דק').
# מאוחסן ב-session["dev_auth_expires_at"] כ-ISO string ב-UTC.
_DEV_SESSION_LIFETIME_MINUTES = 30


# הגבלת קצב לעמוד ציבורי /p/<slug> ו-/ics/<slug> — הגנה בעומק נגד
# ניחוש של slugs (גם אם 128 ביט אנטרופיה הופך ניחוש לבלתי-סביר).
# 60 בקשות לדקה לכל IP — נדיב למשתמש לגיטימי, חוסם scanners.
_PUBLIC_PAGE_MAX_REQUESTS = 60
_PUBLIC_PAGE_WINDOW_SECONDS = 60
_PUBLIC_PAGE_MAX_TRACKED_IPS = 5_000
_public_page_requests: dict[str, list[float]] = {}


def _check_public_page_rate_limit(ip: str) -> bool:
    """מחזיר True אם ה-IP חרג ממגבלת בקשות לעמוד ציבורי. רושם את הבקשה
    הנוכחית בכל מקרה (כולל בלוקים — כדי שהחלון יזוז קדימה תחת תוקף)."""
    import time
    now = time.time()
    cutoff = now - _PUBLIC_PAGE_WINDOW_SECONDS
    requests_list = _public_page_requests.get(ip, [])
    fresh = [ts for ts in requests_list if ts > cutoff]
    fresh.append(now)
    _public_page_requests[ip] = fresh
    # LRU eviction — מונע גידול לא חסום בזיכרון
    if len(_public_page_requests) > _PUBLIC_PAGE_MAX_TRACKED_IPS:
        oldest_ip = next(iter(_public_page_requests))
        if oldest_ip != ip:
            del _public_page_requests[oldest_ip]
    return len(fresh) > _PUBLIC_PAGE_MAX_REQUESTS


def _check_login_rate_limit(ip: str) -> bool:
    """בודק אם ה-IP חרג ממגבלת ניסיונות ההתחברות. מחזיר True אם חסום."""
    import time
    attempts = _login_attempts.get(ip)
    if not attempts:
        return False
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    # ניקוי ניסיונות ישנים
    fresh = [ts for ts in attempts if ts > cutoff]
    if fresh:
        _login_attempts[ip] = fresh
    else:
        # אין ניסיונות רלוונטיים — מוחקים את ה-IP לחלוטין
        del _login_attempts[ip]
        return False
    return len(fresh) >= _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    """רושם ניסיון התחברות כושל."""
    import time
    if ip not in _login_attempts:
        _login_attempts[ip] = []
        # LRU eviction — מוחקים את ה-IP הישן ביותר אם חרגנו
        if len(_login_attempts) > _LOGIN_MAX_TRACKED_IPS:
            oldest_ip = next(iter(_login_attempts))
            del _login_attempts[oldest_ip]
    _login_attempts[ip].append(time.time())


def _check_dev_login_rate_limit(ip: str) -> bool:
    """גרסה מקבילה של _check_login_rate_limit עם dict + מגבלות נפרדות לאיזור dev."""
    import time
    attempts = _dev_login_attempts.get(ip)
    if not attempts:
        return False
    now = time.time()
    cutoff = now - _DEV_LOGIN_WINDOW_SECONDS
    fresh = [ts for ts in attempts if ts > cutoff]
    if fresh:
        _dev_login_attempts[ip] = fresh
    else:
        del _dev_login_attempts[ip]
        return False
    return len(fresh) >= _DEV_LOGIN_MAX_ATTEMPTS


def _record_dev_login_attempt(ip: str) -> None:
    """רישום ניסיון התחברות כושל ל-/dev/login."""
    import time
    if ip not in _dev_login_attempts:
        _dev_login_attempts[ip] = []
        if len(_dev_login_attempts) > _LOGIN_MAX_TRACKED_IPS:
            oldest_ip = next(iter(_dev_login_attempts))
            del _dev_login_attempts[oldest_ip]
    _dev_login_attempts[ip].append(time.time())


def _check_and_record_demo_entry(ip: str) -> bool:
    """בודק את חלון ה-rate limit של /demo ומחזיר True אם ה-IP חסום.
    בכל מקרה רושם את הניסיון (גם אם חסום) — אחרת תוקף יכול להמשיך לדפוק
    על ה-endpoint כל זמן שהוא בחסימה ולעולם לא לפוג את החלון.
    """
    import time
    now = time.time()
    cutoff = now - _DEMO_ENTRY_WINDOW_SECONDS
    attempts = _demo_entry_attempts.get(ip, [])
    fresh = [ts for ts in attempts if ts > cutoff]
    fresh.append(now)
    # LRU eviction
    if ip not in _demo_entry_attempts and len(_demo_entry_attempts) >= _LOGIN_MAX_TRACKED_IPS:
        oldest_ip = next(iter(_demo_entry_attempts))
        del _demo_entry_attempts[oldest_ip]
    _demo_entry_attempts[ip] = fresh
    return len(fresh) > _DEMO_ENTRY_MAX_ATTEMPTS


# ─── Audit Log ─────────────────────────────────────────────────────────────
# רישום פעולות admin חשובות ללוג (אבטחה וביקורת)
def _audit_log(action: str, details: str = "") -> None:
    """רושם פעולת admin ללוג — IP, נתיב ופרטים."""
    ip = request.remote_addr or "unknown"
    path = request.path
    logger.info("AUDIT | ip=%s | path=%s | action=%s | %s", ip, path, action, details)


# ─── User ID Validation ───────────────────────────────────────────────────
# מזהה משתמש תקין:
#   - Telegram (מספר חיובי)
#   - WhatsApp טלפון (`+972...`)
#   - WhatsApp BSUID (`IL.abc123XYZ`) — Meta Business-Scoped User ID, מסוף 2026.
#     פורמט: ISO-2 country code + נקודה + alphanumeric עד 128 תווים.
_USER_ID_RE = re.compile(
    r"^\+?\d{1,15}$|^[A-Z]{2}\.[A-Za-z0-9]{1,128}$"
)
# Regex עצמאי ל-BSUID לזיהוי מהיר ב-normalize / handlers (Meta spec).
_BSUID_RE = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9]{1,128}$")


from utils.phone import format_phone as _format_phone


# ─── Demo PII masking — Jinja filters ─────────────────────────────────
# פעיל רק כש-session["demo"]==True (גישה דרך /demo). בגישה רגילה של
# בעל העסק — passthrough ל-format_phone. שכבת תצוגה בלבד; ה-DB וה-API
# לא משתנים. ראה plan: "Demo PII masking" ב-CLAUDE plans.

def _looks_like_phone(value) -> bool:
    """מזהה האם הערך נראה כטלפון או user_id מספרי שניתן למסך כטלפון.
    תופס: +972..., 972..., 05X..., Telegram chat_id (numeric ארוך).
    דורש לפחות 6 ספרות לאחר נירמול — קצר מזה לא נראה כטלפון.
    """
    s = (str(value) if value is not None else "").strip()
    if not s:
        return False
    if not re.fullmatch(r"\+?[\d\s\-]{6,20}", s):
        return False
    return len(re.sub(r"\D", "", s)) >= 6


def _demo_active() -> bool:
    """True רק כשרצים בתוך request של גולש דמו. ב-CLI/tests ללא
    request context — False (passthrough)."""
    from flask import has_request_context, session as _session
    return has_request_context() and bool(_session.get("demo"))


def _mask_phone(value) -> str:
    """פילטר תצוגה לטלפון של משתמש קצה.
    - דמו: ••••••XXXX (4 ספרות אחרונות).
    - גישה רגילה: format_phone (פורמט IL כפי שהיום).
    - ערך ריק/None: ''.
    """
    if value is None:
        return ""
    formatted = _format_phone(value)
    if not _demo_active():
        return formatted
    digits = re.sub(r"\D", "", str(formatted))
    if len(digits) < 4:
        return formatted
    return "••••••" + digits[-4:]


def _mask_name(value) -> str:
    """פילטר תצוגה לשם משתמש קצה (drop-in ל-format_phone).
    - ערך שנראה כטלפון/user_id מספרי → mask_phone (גם בלי דמו: פורמט בלבד).
    - גישה רגילה (לא דמו): format_phone (תאימות לאחור).
    - דמו + שם 2+ מילים: 'שם_ראשון X.' (אות ראשונה של שם משפחה).
    - דמו + שם בודד: נשאר as-is (לפי דרישת המשתמש — מקרה של "Amir"
      או placeholder "לקוח"). שימוש ב-mask_username ל-handles
      ייחודיים (telegram/instagram) שגם מילה אחת מזהה.
    - ערך ריק/None: ''.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if _looks_like_phone(s):
        return _mask_phone(s)
    if not _demo_active():
        return _format_phone(s)
    parts = s.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][:1]}."


def _mask_username(value) -> str:
    """פילטר תצוגה ל-handle ייחודי (telegram_username, ig_username
    של משתמש קצה). בניגוד ל-mask_name, גם מילה בודדת מטושטשת — כי
    handle הוא unique identifier ולא placeholder.
    - דמו: 2 תווים ראשונים + ••• (לדוגמה 'amir_xyz' → 'am•••').
      handle של 2 תווים או פחות → כולו •••.
    - גישה רגילה (לא דמו): as-is.
    - ערך ריק/None: ''.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if not _demo_active():
        return s
    if len(s) <= 2:
        return "•••"
    return s[:2] + "•••"


def create_admin_app() -> Flask:
    """Create and configure the Flask admin application."""
    _validate_admin_security_config()
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    # מאחורי ה-proxy של Render — בלי זה request.url חוזר http:// והקוד
    # נאלץ לתקן ידנית X-Forwarded-Proto בכל מקום (ראה whatsapp_webhook).
    # רמת trust אחת בלבד (proxy יחיד של הפלטפורמה).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = ADMIN_SECRET_KEY
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    # ── Tenant context (multi-tenant שלב 1) ─────────────────────────────
    # כל בקשת HTTP רצה תחת tenant מוגדר. כרגע תמיד DEFAULT_TENANT; בשלב 2
    # ה-resolve יהיה לפי ה-session (אדמין) או מפתח ראוטינג (webhooks).
    @app.before_request
    def _bind_tenant_context():
        # ברירת המחדל — ה-tenant ה-legacy. session של בעל עסק (tenant_id)
        # או של platform admin במצב "פעל-כ" (acting_tenant) קובע אחרת.
        # ה-webhooks והנתיבים הציבוריים עושים resolve משלהם ועוטפים
        # ב-tenant_context פנימי — ה-binding כאן לא משפיע עליהם.
        tenant = DEFAULT_TENANT
        chosen = session.get("tenant_id") or session.get("acting_tenant")
        if chosen:
            from control_plane import get_tenant_status_cached
            from tenancy import InvalidTenantSlug, validate_tenant_id

            try:
                validate_tenant_id(chosen)
                status = get_tenant_status_cached(chosen)
            except InvalidTenantSlug:
                status = None
            except Exception:
                logger.error("tenant status check failed", exc_info=True)
                status = None
            if status == "active":
                tenant = chosen
            else:
                # ה-tenant הושעה/נמחק בזמן שה-session חי — ניתוק מסודר.
                logger.warning("session bound to inactive tenant — logging out")
                session.clear()
                flash("החשבון אינו פעיל כרגע. פנו לתמיכה.", "warning")
                return redirect(url_for("login"))
        g._tenant_token = set_current_tenant(tenant)

    @app.teardown_request
    def _release_tenant_context(exc):
        token = g.pop("_tenant_token", None)
        if token is not None:
            try:
                reset_current_tenant(token)
            except Exception:
                # לא מפילים teardown — רק מתעדים (Exceptions תמיד ללוג)
                logger.error("failed to reset tenant context", exc_info=True)

    csrf = CSRFProtect()
    csrf.init_app(app)

    # ── רישום routes ציבוריות של widget (embed.js / api/chat / demo) ──
    # פטורות מ-CSRF ומ-login_required. ה-route הפנימי לעמוד הוראות הטמעה
    # נרשם בהמשך, אחרי הגדרת login_required, כדי שיהיה תחת auth.
    from admin.widget import register_widget_routes
    register_widget_routes(app, csrf)

    app.jinja_env.filters["il_datetime"] = _format_il_datetime
    app.jinja_env.filters["il_datetime_local"] = _format_il_datetime_local
    app.jinja_env.filters["il_date"] = _format_il_date
    app.jinja_env.filters["il_phone"] = _format_il_phone
    app.jinja_env.filters["relative_time"] = _format_relative_time
    app.jinja_env.filters["translate_category"] = _translate_category
    app.jinja_env.filters["translate_status"] = _translate_status
    app.jinja_env.filters["telegram_html"] = _telegram_html
    app.jinja_env.filters["format_phone"] = _format_phone
    # Demo PII masking — פעיל רק במצב דמו (session["demo"]==True)
    app.jinja_env.filters["mask_name"] = _mask_name
    app.jinja_env.filters["mask_phone"] = _mask_phone
    app.jinja_env.filters["mask_username"] = _mask_username
    # שלב 7 — תרגום לעובדות זיכרון
    app.jinja_env.filters["translate_fact_type"] = (
        lambda v: FACT_TYPE_TRANSLATION.get(v, v)
    )
    app.jinja_env.filters["translate_fact_status"] = (
        lambda v: FACT_STATUS_TRANSLATION.get(v, v)
    )

    def _detect_tenant_channel():
        """ערוץ התצוגה של ה-tenant הנוכחי — לתצוגה/preview כשהערוץ עדיין
        לא ננעל (feature_flags.get_channel ריק).

        ל-tenant של ברירת המחדל (legacy): זיהוי לפי env (detect_active_channel).
        ל-tenant בפלטפורמה: לפי הסודות שהוגדרו לו ב-control plane — כי
        ה-credentials שלו **אינם ב-env** בכלל, ולכן detect_active_channel
        (שקורא env) חסר משמעות עבורו.
        """
        from tenancy import DEFAULT_TENANT, get_current_tenant

        tenant = get_current_tenant()
        if tenant == DEFAULT_TENANT:
            return detect_active_channel() or ""
        try:
            import control_plane as _cp

            names = set(_cp.list_tenant_secret_names(tenant))
        except Exception:
            logger.error("detect tenant channel failed", exc_info=True)
            return ""
        if "telegram_bot_token" in names:
            return "telegram"
        if {"twilio_account_sid", "twilio_auth_token",
                "twilio_whatsapp_number"} <= names:
            return "whatsapp"
        return ""

    @app.context_processor
    def _inject_globals():
        # שמירת user_notes ב-g כדי לא לבצע שאילתת DB בכל render_template()
        if not hasattr(g, '_user_notes'):
            g._user_notes = db.get_all_user_notes()
        return {
            # הזהות העסקית נשלפת פר-בקשה (multi-tenant שלב 1) — במקום
            # kwarg ידני בכל render_template.
            "business_name": get_business_config().name,
            "rag_index_stale": is_index_stale(),
            "user_notes": g._user_notes,
            "today_iso": datetime.now(ISRAEL_TZ).date().isoformat(),
            # דגלי דמו לתבניות (banner, CTA, toast).
            "is_demo_session": bool(session.get("demo")),
            "demo_cta_whatsapp": DEMO_CTA_WHATSAPP,
            "demo_live_bot_url": DEMO_LIVE_BOT_URL,
            # מזהה ייחודי לסשן דמו — ה-bubble timer בצד לקוח משווה אותו
            # למזהה השמור ב-sessionStorage; אם השתנה (סשן דמו חדש), הזמן
            # המצטבר מתאפס.
            "demo_session_id": session.get("demo_session_id", ""),
        }

    @app.context_processor
    def _inject_feature_flags():
        # חשיפת has_feature ופרטי החבילה הנוכחית ל-Jinja templates.
        # נטען לפי בקשה (per-request) כדי שעדכון מ-/dev/subscription יתפוס
        # מיד בעמוד הבא, בלי cache בין בקשות.
        # כל הקריאות עטופות ב-try/except כדי שכשל DB טרנזיינטי לא יפיל
        # את כל הפאנל (defense in depth — feature_flags.get_subscription_row
        # אמור להיות bullet-proof, אבל גם פה מגנים).
        if not hasattr(g, "_subscription_row"):
            try:
                g._subscription_row = feature_flags.get_subscription_row()
            except Exception:
                logger.error("context_processor: failed to load subscription row", exc_info=True)
                g._subscription_row = None

        def _safe_has_feature(name: str) -> bool:
            try:
                return feature_flags.has_feature(name)
            except Exception:
                logger.error("has_feature(%r) failed in template", name, exc_info=True)
                return False

        try:
            in_grace = feature_flags.is_in_grace_period()
        except Exception:
            logger.error("is_in_grace_period() failed in template", exc_info=True)
            in_grace = False

        try:
            grace_days_left = feature_flags.days_remaining_in_grace()
        except Exception:
            logger.error("days_remaining_in_grace() failed in template", exc_info=True)
            grace_days_left = 0

        try:
            grace_ended = feature_flags.is_grace_ended()
        except Exception:
            logger.error("is_grace_ended() failed in template", exc_info=True)
            grace_ended = False

        try:
            grace_ends_at = feature_flags.grace_period_ends_at()
        except Exception:
            logger.error("grace_period_ends_at() failed in template", exc_info=True)
            grace_ends_at = None

        def _safe_min_plan_for_feature(name: str) -> Optional[str]:
            try:
                return plans_config.get_min_plan_for_feature(name)
            except Exception:
                logger.error(
                    "get_min_plan_for_feature(%r) failed in template",
                    name, exc_info=True,
                )
                return None

        # הערוץ של ה-tenant (נעילה אוטומטית) — ריק = טרם נקבע / tenant ברירת
        # המחדל. detected_channel הוא fallback תצוגתי בלבד (env), לעולם לא
        # משמש לנעילת מקטעים — רק tenant_channel נועל.
        try:
            tenant_channel = feature_flags.get_channel()
        except Exception:
            logger.error("get_channel() failed in template context", exc_info=True)
            tenant_channel = ""
        try:
            detected_channel = _detect_tenant_channel()
        except Exception:
            logger.error("_detect_tenant_channel() failed in template context", exc_info=True)
            detected_channel = ""

        return {
            "has_feature": _safe_has_feature,
            "current_plan": (g._subscription_row or {}).get("plan", plans_config.DEFAULT_PLAN),
            "plan_definition": plans_config.get_plan_definition(
                (g._subscription_row or {}).get("plan", plans_config.DEFAULT_PLAN)
            ),
            "tenant_channel": tenant_channel,
            "detected_channel": detected_channel,
            "plan_display_names": {
                key: cfg["display_name"] for key, cfg in plans_config.PLANS.items()
            },
            "min_plan_for_feature": _safe_min_plan_for_feature,
            "is_in_grace_period": in_grace,
            "days_remaining_in_grace": grace_days_left,
            "is_grace_ended": grace_ended,
            "grace_ends_at": grace_ends_at,
        }

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(e):
        logger.warning(
            "CSRF error | ip=%s | path=%s | method=%s | reason=%s",
            request.remote_addr, request.path, request.method, e.description,
        )
        if request.headers.get("HX-Request"):
            # Return a lightweight 403 so HTMX doesn't replace content with
            # a full redirect page.  The csrfExpired trigger tells client JS
            # to show a reload prompt.
            resp = app.make_response(("", 403))
            # Prevent any DOM swap on HTMX requests.
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = "csrfExpired"
            return resp
        # Regular form submission — flash and redirect.
        flash("פג תוקף הטופס. נסו שוב.", "danger")
        default = url_for("dashboard") if session.get("logged_in") else url_for("login")
        return redirect(_safe_redirect_back(default))
    
    # ─── Auth Decorator ───────────────────────────────────────────────────

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                if request.headers.get("HX-Request"):
                    resp = app.make_response(("", 401))
                    resp.headers["HX-Redirect"] = url_for("login")
                    return resp
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    # רישום route פנימי של עמוד הוראות הטמעת ה-widget (תחת login_required)
    from admin.widget import register_widget_admin_routes
    register_widget_admin_routes(app, login_required)

    # Meta DM OAuth — נרשם רק אם משתני הסביבה מוגדרים. ראה
    # docs/meta_dm_spec.md.
    from ai_chatbot.config import META_APP_ID as _META_APP_ID
    if _META_APP_ID:
        from admin.meta_oauth import register_meta_oauth_routes
        register_meta_oauth_routes(app, login_required)
        logger.info("Meta OAuth routes registered at /admin/meta/*")

    def require_developer(f):
        """
        מגן על routes תחת /dev/*. אם DEVELOPER_PASSWORD לא מוגדר ב-env —
        מחזיר 404 (כדי לא לחשוף שאיזור dev קיים בכלל). אחרת — בודק שיש
        session.dev_authenticated עם תוקף, ומפנה ל-/dev/login אם לא.
        """
        @wraps(f)
        def decorated(*args, **kwargs):
            if not _is_developer_access_enabled():
                # מחזירים 404 עקבי עם נתיב לא קיים — לא חושפים את הקיום
                return ("", 404)

            if not session.get("dev_authenticated"):
                return redirect(url_for("dev_login"))

            # בדיקת תוקף ה-session (timeout קצר)
            expires_iso = session.get("dev_auth_expires_at")
            if not expires_iso:
                session.pop("dev_authenticated", None)
                return redirect(url_for("dev_login"))
            try:
                expires_at = datetime.fromisoformat(expires_iso)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                logger.error("require_developer: invalid expires_iso in session")
                session.pop("dev_authenticated", None)
                session.pop("dev_auth_expires_at", None)
                return redirect(url_for("dev_login"))

            if datetime.now(timezone.utc) >= expires_at:
                session.pop("dev_authenticated", None)
                session.pop("dev_auth_expires_at", None)
                flash("פג תוקף ה-session של המפתח. אנא התחברו מחדש.", "info")
                return redirect(url_for("dev_login"))

            # התוקף עוד קיים — מאריכים את החלון (sliding window)
            new_expiry = datetime.now(timezone.utc) + timedelta(
                minutes=_DEV_SESSION_LIFETIME_MINUTES
            )
            session["dev_auth_expires_at"] = new_expiry.isoformat()

            return f(*args, **kwargs)
        return decorated

    # ─── Feature Gating (Phase 3) ──────────────────────────────────────────
    # מיפוי path-prefix → feature name. כל request שמתחיל בתחילית הזו —
    # נבדק מול has_feature(...) לפני שה-route רץ. זה מחליף את הצורך
    # להוסיף @require_feature ידנית ל-30+ routes של broadcast.
    # ⚠ הסדר חשוב: prefix ארוך יותר חייב להופיע לפני קצר יותר אם יש חפיפה
    # (אין פה כרגע, אבל נוהג טוב).
    _FEATURE_GATE_PREFIXES = (
        ("/broadcast", "broadcast"),
        ("/followups", "followup_24h"),
        ("/widget-embed", "widget"),  # עמוד הוראות הטמעת ה-widget לבעל העסק
    )

    # ראוטים שתמיד מותרים (גם אם הם תחת תחילית מסוננת — אין כרגע, אבל
    # שומר עתידיות. לא משפיע על ה-prefix matching שלמטה).
    _FEATURE_GATE_BYPASS_PATHS: frozenset[str] = frozenset({
        "/health",
        "/login",
        "/logout",
    })

    def _path_matches_feature_gate(path: str) -> Optional[tuple[str, str]]:
        """מחזיר (prefix, feature_name) אם הנתיב כפוף ל-gate, אחרת None."""
        if not path:
            return None
        for prefix, feature in _FEATURE_GATE_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return prefix, feature
        return None

    def _feature_denied_response(feature: str):
        """בניית תשובה אחידה לחסימת פיצ'ר — HTMX, JSON, או redirect רגיל."""
        if request.headers.get("HX-Request"):
            resp = app.make_response(("", 403))
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = "featureDenied"
            return resp
        if request.is_json or request.path.startswith("/api/"):
            return jsonify({
                "error": "feature_not_available",
                "feature": feature,
                "current_plan": feature_flags.get_current_plan(),
            }), 403
        flash(
            f"הפיצ'ר '{feature}' לא זמין בחבילה הנוכחית שלך. "
            "ליצירת קשר לשדרוג — דברו עם ספק השירות.",
            "warning",
        )
        return redirect(url_for("dashboard"))

    @app.before_request
    def _enforce_feature_flags():
        """
        אכיפה ברמת הנתיב — לפני כל route, בודקים אם הוא כפוף ל-feature gate.
        מתבצע *רק* על משתמשי admin מאומתים — login_required של הראוט
        עצמו ידאג להפנות לא-מאומתים ל-/login.
        איזור /dev/* תמיד עובר (המפתח שולט בחבילה — אסור לחסום אותו).
        """
        path = request.path or ""
        # /dev/* תמיד עובר — המפתח לא נחסם לעולם.
        if path.startswith("/dev/") or path == "/dev":
            return None
        if path in _FEATURE_GATE_BYPASS_PATHS:
            return None

        match = _path_matches_feature_gate(path)
        if not match:
            return None

        # אם המשתמש לא מאומת — login_required של הראוט יזרוק redirect.
        # אנחנו לא צריכים ליצור flash מיותר ל-feature denied במצב כזה.
        if not session.get("logged_in"):
            return None

        prefix, feature = match
        try:
            allowed = feature_flags.has_feature(feature)
        except Exception:
            logger.error(
                "feature_gate: has_feature(%r) raised — defaulting to deny",
                feature, exc_info=True,
            )
            allowed = False
        if allowed:
            return None

        logger.info(
            "feature_gate: blocked path=%s feature=%s plan=%s",
            path, feature, feature_flags.get_current_plan(),
        )
        return _feature_denied_response(feature)

    # ─── Demo Mode (read-only enforcement) ─────────────────────────────────
    # מצב דמו לקמפיין שיווקי — ראה docs/demo-mode-spec.md.
    # ה-/demo route יוצר session עם session["demo"]=True. ה-middleware הבא
    # חוסם כל POST/PUT/PATCH/DELETE מ-session כזה, חוץ מנתיבים מותרים
    # (logout + analytics).

    # נתיבים שמותר לכתוב אליהם גם במצב דמו.
    _DEMO_WRITE_ALLOWLIST: frozenset[str] = frozenset({
        "/logout",
        "/demo/track",
    })

    # נתיבים שגולש דמו לא יכול לגשת אליהם בכלל (אזור המפתח).
    _DEMO_PATH_BLOCKLIST_PREFIXES: tuple[str, ...] = (
        "/dev",
    )

    @app.before_request
    def _enforce_demo_readonly():
        """
        אכיפת read-only על session של דמו. רץ אחרי _enforce_feature_flags.
        """
        if not session.get("demo"):
            return None

        path = request.path or ""

        # חסימת אזור המפתח לחלוטין לגולשי דמו.
        for prefix in _DEMO_PATH_BLOCKLIST_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                if request.headers.get("HX-Request"):
                    return app.make_response(("", 404))
                return redirect(url_for("dashboard"))

        # שיטות קריאה בלבד תמיד מותרות.
        # HEAD ו-OPTIONS חיוניים: Flask מנתב אוטומטית HEAD ל-handler של GET,
        # ו-OPTIONS משמש ל-CORS preflight. שתיהן בטוחות וחסרות side effects.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None

        # נכסים סטטיים — מותרים (לא אמורים להגיע ל-before_request, ליתר ביטחון).
        if path.startswith("/static/"):
            return None

        # רשימה לבנה של כתיבות מותרות.
        if path in _DEMO_WRITE_ALLOWLIST:
            return None

        # כל כתיבה אחרת — חסומה.
        logger.info("demo_mode: blocked %s %s", request.method, path)
        is_htmx = bool(request.headers.get("HX-Request"))

        if is_htmx:
            # HTMX: מחזיר fragment toast עם HX-Retarget לקונטיינר ייעודי.
            try:
                html = render_template("_partials/demo_blocked_toast.html")
            except Exception:
                logger.error("demo_mode: failed to render toast partial", exc_info=True)
                html = (
                    '<div class="alert alert-info">'
                    "במצב דמו השינויים לא נשמרים."
                    "</div>"
                )
            resp = app.make_response((html, 200))
            resp.headers["HX-Retarget"] = "#demo-toast-container"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp

        # POST רגיל (form submission): flash + redirect חזרה לעמוד המקור.
        # אותו דפוס שמשמש את _handle_csrf_error למעלה — מונע מצב שבו הדפדפן
        # מציג תגובת HTML חלקית כעמוד שלם.
        flash("במצב דמו השינויים לא נשמרים.", "info")
        return redirect(_safe_redirect_back(url_for("dashboard")))

    def require_feature(feature_name: str):
        """
        Decorator לאכיפה ברמת ראוט בודד (defense in depth — ה-before_request
        כבר מטפל בקבוצות נתיבים, אבל ראוט ספציפי יכול להוסיף הגנה מפורשת).
        """
        if not plans_config.is_valid_feature(feature_name):
            raise ValueError(f"require_feature: unknown feature {feature_name!r}")

        def decorator(f):
            @wraps(f)
            def decorated(*args, **kwargs):
                try:
                    allowed = feature_flags.has_feature(feature_name)
                except Exception:
                    logger.error(
                        "require_feature(%r) check raised — denying",
                        feature_name, exc_info=True,
                    )
                    allowed = False
                if not allowed:
                    return _feature_denied_response(feature_name)
                return f(*args, **kwargs)
            return decorated
        return decorator
    
    # ─── Health Check ────────────────────────────────────────────────────

    @app.route("/health")
    def health_check():
        """בדיקת בריאות אמיתית — DB + RAG index."""
        checks = {}
        healthy = True

        # בדיקת DB
        try:
            db.count_kb_entries(active_only=False)
            checks["database"] = "ok"
        except Exception as e:
            logger.error("Health check — DB failure: %s", e)
            checks["database"] = "error"
            healthy = False

        # בדיקת FAISS index
        try:
            from ai_chatbot.rag.engine import is_index_stale
            checks["rag_index"] = "stale" if is_index_stale() else "ok"
        except Exception as e:
            logger.error("Health check — RAG failure: %s", e)
            checks["rag_index"] = "error"
            healthy = False

        # בדיקת Telegram token (לא קריאת API — רק שהוגדר)
        checks["telegram_token"] = "configured" if TELEGRAM_BOT_TOKEN else "missing"

        status_code = 200 if healthy else 503
        return jsonify({"status": "ok" if healthy else "degraded", "checks": checks}), status_code

    # ─── PWA — Manifest & Service Worker ───────────────────────────────
    # מאפשרים התקנה כאפליקציה מהדפדפן ("הוסף למסך הבית" באנדרואיד / iOS).
    # שני הקבצים דינמיים כדי לכלול את BUSINESS_NAME ולנהל גרסאות cache.

    @app.route("/manifest.webmanifest")
    def pwa_manifest():
        """Web App Manifest — מתאר את האפליקציה לדפדפן."""
        manifest = {
            "name": f"{get_business_config().name} — פאנל ניהול",
            "short_name": get_business_config().name[:12] or "פאנל",
            "description": "פאנל ניהול לבוט עסקי",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            # ללא orientation — לפי המפרט, חסרון השדה = "any" = הולכים עם
            # סיבוב המכשיר. Chrome מתעקש לפעמים על "portrait" כשהשדה
            # מצוין במפורש, אז מעדיפים להשמיט.
            "lang": "he",
            "dir": "rtl",
            "theme_color": "#2563EB",
            "background_color": "#0f172a",
            "icons": [
                {
                    "src": url_for("static", filename="icons/icon-192.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": url_for("static", filename="icons/icon-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": url_for("static", filename="icons/icon-192-maskable.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                {
                    "src": url_for("static", filename="icons/icon-512-maskable.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
        resp = jsonify(manifest)
        resp.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
        # cache קצר מאוד — שינויים ב-BUSINESS_NAME / orientation / icons
        # ייכנסו תוך דקה. אחרת Chrome אופה את הערכים לתוך ה-PWA המותקנת
        # ושינויים לא נכנסים גם אחרי uninstall.
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    @app.route("/sw.js")
    def pwa_service_worker():
        """
        Service Worker — נדרש ל-PWA installable + מטפל בהתראות Web Push.
        אין offline caching אמיתי כדי לא לשרת תוכן ישן מ-DB; רק network passthrough.
        Scope: שורש האתר (חייב להיות מוגש מ-/).
        עדכון משמעותי: bump של CACHE_VERSION מאלץ דפדפנים לרענן את ה-SW.
        """
        sw = """
const CACHE_VERSION = 'v2-push';
self.addEventListener('install', (event) => { self.skipWaiting(); });
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (event) => {
    // network-first בלי cache — הפאנל מציג נתונים בזמן אמת, לא רוצים stale.
    // ה-handler חייב להתקיים כדי ש-Chrome יחשיב את ה-app כ-installable.
    event.respondWith(fetch(event.request));
});

// ── Web Push: הודעה חדשה בשיחה חיה ─────────────────────────────────────
// ה-payload מגיע מ-notifications/push_service.py:
//   { title, body, url, tag, user_id }
self.addEventListener('push', (event) => {
    if (!event.data) return;
    let payload = {};
    try { payload = event.data.json(); } catch (e) {
        payload = { title: 'הודעה חדשה', body: event.data.text() };
    }
    const url = payload.url || '/';
    const tag = payload.tag || 'live-chat';

    event.waitUntil((async () => {
        // אם יש לשונית פאנל ב-focus — הפולינג של /api/stats כל 5 שניות
        // רץ ללא throttle, ומציג Notification/טוסט (אלא אם המשתמש על אותו
        // /live-chat/X שעבורו ההודעה — אז ה-HTMX polling מציג בדף עצמו).
        // לשוניות ברקע ממותנות אגרסיבית (Chrome: 1 wake/דקה) ולא יכולות
        // לסמוך על הפולינג — שם push *חייב* להציג. אם אין שום לשונית
        // ב-focus — מציגים push.
        const allClients = await self.clients.matchAll({
            type: 'window',
            includeUncontrolled: true,
        });
        const hasFocusedClient = allClients.some(c => c.focused);
        if (hasFocusedClient) return;

        await self.registration.showNotification(payload.title || 'הודעה חדשה', {
            body: payload.body || '',
            tag: tag,                              // דורס notifications קודמות מאותו לקוח
            renotify: true,                        // צפצוף גם כשהוחלף tag קיים
            icon: '/static/icons/icon-192.png',
            badge: '/static/icons/icon-192.png',
            dir: 'rtl',
            lang: 'he',
            data: { url: url },
        });
    })());
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil((async () => {
        const allClients = await self.clients.matchAll({
            type: 'window',
            includeUncontrolled: true,
        });
        // אם יש כבר לשונית של הפאנל פתוחה — לנווט אותה ולמקד אותה.
        for (const client of allClients) {
            if ('focus' in client) {
                try { await client.navigate(targetUrl); } catch (e) {}
                return client.focus();
            }
        }
        if (self.clients.openWindow) {
            return self.clients.openWindow(targetUrl);
        }
    })());
});
""".strip()
        resp = app.make_response(sw)
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        # SW חייב להיות עם scope=/, לכן ה-header הזה מאפשר את זה גם אם
        # מוגש מתת-ניתב (כאן זה לא רלוונטי, אבל זה best practice).
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ─── עמוד ציבורי (ללא אותנטיקציה) ──────────────────────────────────

    def _apply_public_page_security_headers(resp):
        """Headers שמונעים אינדוקס/cache/דליפת referer לעמוד ציבורי שמכיל
        תוכן AI על משתמש קצה. ראה docs/privacy_data_matrix.md → response_pages.
        """
        resp.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def _bind_public_tenant(tenant_id):
        """resolve של tenant לנתיב ציבורי (/t/<slug>/...).

        מחזיר context manager או None (⇒ 404). לא מבחינים כלפי חוץ בין
        slug לא-חוקי, tenant לא-רשום ו-tenant מושעה — הכול 404, כדי לא
        לחשוף מצב פנימי לנתיב ציבורי.
        """
        from tenancy import InvalidTenantSlug, tenant_context, validate_tenant_id
        import control_plane as _cp

        try:
            validate_tenant_id(tenant_id)
        except InvalidTenantSlug:
            return None
        row = _cp.get_tenant(tenant_id)
        if row is None or row["status"] != "active":
            return None
        return tenant_context(tenant_id)

    @app.route("/p/<page_id>")
    @app.route("/t/<tenant_id>/p/<page_id>", endpoint="public_page_tenant")
    def public_page(page_id, tenant_id=None):
        """עמוד תשובה ציבורי — מגיש תוכן HTML לתשובות ארוכות מ-WhatsApp.

        מוגן ב-rate limit פר-IP כדי למנוע ניחוש מסיבי של slugs (גם עם
        128 ביט אנטרופיה זה הגנה בעומק). headers מונעים אינדוקס וcache.
        הנתיב עם /t/<slug>/ מגיש את העמוד מה-DB של אותו tenant.
        """
        if tenant_id is not None:
            ctx = _bind_public_tenant(tenant_id)
            if ctx is None:
                resp = app.make_response(("", 404))
                return _apply_public_page_security_headers(resp)
            with ctx:
                return _public_page_impl(page_id)
        return _public_page_impl(page_id)

    def _public_page_impl(page_id):
        client_ip = request.remote_addr or "unknown"
        if _check_public_page_rate_limit(client_ip):
            logger.warning("public_page rate limit exceeded for IP %s", client_ip)
            resp = app.make_response(("Too Many Requests", 429))
            return _apply_public_page_security_headers(resp)

        page = db.get_response_page(page_id)
        status_code = 200 if page else 404
        if not page:
            # נספור גם 404 כדי שניחוש slugs ייחסם — ה-rate limiter כבר רץ למעלה
            logger.info("public_page: 404 for page_id=%s ip=%s", page_id[:6], client_ip)

        resp = app.make_response((render_template(
            "public_page.html",
            page=page,
            title=page["title"] if page else "",
            content=page["content"] if page else "",
            business_phone=get_business_config().phone,
            business_address=get_business_config().address,
        ), status_code))
        return _apply_public_page_security_headers(resp)

    @app.route("/ics/<page_id>")
    @app.route("/t/<tenant_id>/ics/<page_id>", endpoint="public_ics_tenant")
    def public_ics(page_id, tenant_id=None):
        """קובץ ICS ציבורי — נטען ע"י Twilio (media_url) או הלקוח ישירות.

        משתמש באותה תשתית של response_pages: ה-content הוא טקסט ICS,
        ה-title הוא שם הקובץ ללא סיומת. headers זהים לעמוד הציבורי.
        הנתיב עם /t/<slug>/ מגיש מה-DB של אותו tenant.
        """
        if tenant_id is not None:
            ctx = _bind_public_tenant(tenant_id)
            if ctx is None:
                resp = app.make_response(("", 404))
                return _apply_public_page_security_headers(resp)
            with ctx:
                return _public_ics_impl(page_id)
        return _public_ics_impl(page_id)

    def _public_ics_impl(page_id):
        client_ip = request.remote_addr or "unknown"
        if _check_public_page_rate_limit(client_ip):
            logger.warning("public_ics rate limit exceeded for IP %s", client_ip)
            resp = app.make_response(("Too Many Requests", 429))
            return _apply_public_page_security_headers(resp)

        page = db.get_response_page(page_id)
        if not page:
            resp = app.make_response(("", 404))
            return _apply_public_page_security_headers(resp)
        resp = app.make_response(page["content"])
        resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
        filename = (page.get("title") or "appointment") + ".ics"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return _apply_public_page_security_headers(resp)

    # ─── Auth Routes ──────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            client_ip = request.remote_addr or "unknown"
            if _check_login_rate_limit(client_ip):
                logger.warning("Login rate limit exceeded for IP %s", client_ip)
                flash("יותר מדי ניסיונות התחברות. נסו שוב בעוד מספר דקות.", "danger")
                return render_template("login.html")

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            remember = bool(request.form.get("remember_me"))

            # מסלול legacy — פרטי env (בעל העסק של ה-tenant של ברירת המחדל)
            if _verify_admin_credentials(username, password):
                session.clear()  # מניעת session fixation
                if remember:
                    session.permanent = True
                session["logged_in"] = True
                flash("ברוכים השבים!", "success")
                _audit_log("login_success", f"user={username}")
                return redirect(url_for("dashboard"))

            # מסלול פלטפורמה — משתמשי admin_users מה-control plane
            # (multi-tenant שלב 2). ה-session נקשר ל-tenant של המשתמש.
            try:
                from control_plane import verify_admin_login

                platform_user = verify_admin_login(username, password)
            except Exception:
                # דפוס קריטי #10: כשל תשתיתי אינו "פרטים שגויים" — מציגים
                # הודעת זמינות ולא סופרים ניסיון כושל.
                logger.error("platform login backend failed", exc_info=True)
                flash("השירות אינו זמין כרגע — נסו שוב בעוד רגע.", "danger")
                return render_template("login.html")

            if platform_user:
                session.clear()  # מניעת session fixation
                if remember:
                    session.permanent = True
                session["logged_in"] = True
                session["admin_email"] = platform_user["email"]
                session["admin_role"] = platform_user["role"]
                if platform_user["role"] == "owner":
                    session["tenant_id"] = platform_user["tenant_id"]
                flash("ברוכים השבים!", "success")
                # בלי email בלוג (דפוס #7) — role + tenant מספיקים לאודיט
                _audit_log(
                    "login_success",
                    f"platform_user role={platform_user['role']} "
                    f"tenant={platform_user.get('tenant_id') or '-'}",
                )
                if platform_user["role"] == "platform_admin":
                    return redirect(url_for("platform_home"))
                return redirect(url_for("dashboard"))

            _record_login_attempt(client_ip)
            logger.warning("Failed login attempt from IP %s", client_ip)
            flash("פרטי התחברות שגויים.", "danger")
        return render_template("login.html")
    
    @app.route("/logout")
    def logout():
        session.clear()
        flash("התנתקת בהצלחה.", "info")
        return redirect(url_for("login"))

    # ─── Platform Admin — ניהול ה-tenants (multi-tenant שלב 2) ────────────

    def platform_admin_required(f):
        """גישה ל-platform admins בלבד. לאחרים — 404 (לא חושפים קיום,
        אותו דפוס כמו /dev)."""

        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            if session.get("admin_role") != "platform_admin":
                return "Not found", 404
            return f(*args, **kwargs)

        return wrapper

    @app.route("/platform")
    @platform_admin_required
    def platform_home():
        """מסך הפלטפורמה: רשימת ה-tenants, סטטוסים, ומעבר 'פעל-כ'."""
        import control_plane as _cp

        tenants = _cp.list_tenants()
        # העשרה קלה לתצוגה — אילו חיבורים רשומים לכל tenant + ערוץ נעול
        from tenancy import tenant_context, TenancyError

        for t in tenants:
            t["has_telegram"] = bool(
                _cp.get_tenant_route_key(t["tenant_id"], "telegram_webhook_key")
            )
            t["has_twilio"] = bool(
                _cp.get_tenant_route_key(t["tenant_id"], "twilio_webhook_key")
            )
            t["has_widget"] = bool(
                _cp.get_tenant_route_key(t["tenant_id"], "widget_key")
            )
            # הערוץ הנעול יושב ב-DB של ה-tenant (subscription.channel);
            # tenant מושעה/פגום לא מפיל את כל המסך — מציגים ריק.
            t["channel"] = ""
            try:
                with tenant_context(t["tenant_id"]):
                    t["channel"] = feature_flags.get_channel()
            except TenancyError:
                # מושעה/במעבר — מצב צפוי, אין ערוץ להציג
                logger.debug(
                    "platform_home: tenant %s blocked for channel read", t["tenant_id"]
                )
            except Exception:
                logger.error(
                    "platform_home: reading channel failed (tenant=%s)",
                    t["tenant_id"], exc_info=True,
                )
        return render_template(
            "platform.html",
            tenants=tenants,
            acting_tenant=session.get("acting_tenant", ""),
        )

    @app.route("/platform/act-as/<tenant_id>", methods=["POST"])
    @platform_admin_required
    def platform_act_as(tenant_id):
        """כניסה למצב 'פעל-כ' — כל הפאנל עובר לעבוד על ה-tenant הנבחר."""
        import control_plane as _cp
        from tenancy import InvalidTenantSlug, validate_tenant_id

        try:
            validate_tenant_id(tenant_id)
        except InvalidTenantSlug:
            flash("מזהה tenant לא תקין.", "danger")
            return redirect(url_for("platform_home"))
        row = _cp.get_tenant(tenant_id)
        if row is None or row["status"] != "active":
            flash("ה-tenant אינו פעיל.", "warning")
            return redirect(url_for("platform_home"))
        session["acting_tenant"] = tenant_id
        _audit_log("platform_act_as", f"tenant={tenant_id}")
        flash(f"פועל כעת כ-{row['display_name']} ({tenant_id}).", "info")
        return redirect(url_for("dashboard"))

    @app.route("/platform/act-as-clear", methods=["POST"])
    @platform_admin_required
    def platform_act_as_clear():
        session.pop("acting_tenant", None)
        _audit_log("platform_act_as_clear")
        flash("חזרת לתצוגת הפלטפורמה.", "info")
        return redirect(url_for("platform_home"))

    @app.route("/platform/tenants/<tenant_id>/status", methods=["POST"])
    @platform_admin_required
    def platform_tenant_status(tenant_id):
        """השעיה/הפעלה של tenant מהמסך."""
        import control_plane as _cp

        status = request.form.get("status", "")
        try:
            _cp.set_tenant_status(tenant_id, status)
        except Exception as exc:
            logger.error("platform status change failed", exc_info=True)
            flash(f"שינוי הסטטוס נכשל: {exc}", "danger")
            return redirect(url_for("platform_home"))
        # אם השעינו את ה-tenant שאנחנו פועלים-כ — יוצאים מהמצב
        if status != "active" and session.get("acting_tenant") == tenant_id:
            session.pop("acting_tenant", None)
        _audit_log("platform_tenant_status", f"tenant={tenant_id} status={status}")
        flash("הסטטוס עודכן.", "success")
        return redirect(url_for("platform_home"))

    @app.route("/platform/tenants/<tenant_id>/channel-unlock", methods=["POST"])
    @platform_admin_required
    def platform_tenant_channel_unlock(tenant_id):
        """שחרור נעילת הערוץ של tenant — פותח את שני מקטעי הערוצים.

        הנעילה נקבעת אוטומטית בחיבור הערוץ הראשון (bot_config). השחרור
        לא מוחק כלום — נתוני הערוץ הקודם נמחקים רק כשהלקוח מזין בפועל
        את נתוני הערוץ החדש (auto-set הבא).
        """
        import control_plane as _cp
        from tenancy import tenant_context, validate_tenant_id, InvalidTenantSlug

        try:
            validate_tenant_id(tenant_id)
        except InvalidTenantSlug:
            flash("מזהה tenant לא תקין.", "danger")
            return redirect(url_for("platform_home"))
        if _cp.get_tenant(tenant_id) is None:
            flash("ה-tenant אינו רשום.", "warning")
            return redirect(url_for("platform_home"))
        try:
            with tenant_context(tenant_id):
                feature_flags.set_channel("")
        except Exception:
            logger.error("channel unlock failed (tenant=%s)", tenant_id, exc_info=True)
            flash("שחרור הנעילה נכשל — נסו שוב.", "danger")
            return redirect(url_for("platform_home"))
        _audit_log("platform_channel_unlock", f"tenant={tenant_id}")
        flash(
            "נעילת הערוץ שוחררה. שני מקטעי הערוצים פתוחים כעת אצל הלקוח; "
            "עם חיבור הערוץ החדש — נתוני הערוץ הקודם יימחקו אוטומטית.",
            "success",
        )
        return redirect(url_for("platform_home"))

    @app.route("/platform/tenants/new", methods=["GET", "POST"])
    @platform_admin_required
    def platform_new_tenant():
        """אשף יצירת לקוח חדש: tenant (DB + רישום) + משתמש כניסה לבעל העסק."""
        import control_plane as _cp
        from tenancy import InvalidTenantSlug

        if request.method == "POST":
            slug = request.form.get("slug", "").strip().lower()
            display_name = request.form.get("display_name", "").strip()
            owner_email = request.form.get("owner_email", "").strip()
            owner_password = request.form.get("owner_password", "")

            if not display_name:
                flash("שם העסק חובה.", "danger")
                return render_template("platform_new_tenant.html", form=request.form)
            # יצירת ה-tenant (DB + seed) — ולידציית ה-slug בתוך create_tenant
            try:
                _cp.create_tenant(slug, display_name)
            except InvalidTenantSlug:
                flash("מזהה לא תקין — אותיות קטנות באנגלית, ספרות ומקפים בלבד.", "danger")
                return render_template("platform_new_tenant.html", form=request.form)
            except _cp.TenantExistsError:
                flash("כבר קיים לקוח עם המזהה הזה.", "danger")
                return render_template("platform_new_tenant.html", form=request.form)
            except Exception:
                logger.error("platform_new_tenant: create_tenant failed", exc_info=True)
                flash("יצירת הלקוח נכשלה — נסו שוב.", "danger")
                return render_template("platform_new_tenant.html", form=request.form)

            # יצירת משתמש הכניסה של בעל העסק
            try:
                _cp.create_admin_user(
                    owner_email, owner_password, role="owner", tenant_id=slug,
                    display_name=display_name,
                )
            except ValueError as exc:
                # ה-tenant כבר נוצר — משאירים אותו ומדווחים על בעיית המשתמש
                flash(
                    f"הלקוח נוצר, אך יצירת משתמש הכניסה נכשלה: {exc}. "
                    "אפשר להוסיף משתמש בנפרד.",
                    "warning",
                )
                return redirect(url_for("platform_home"))
            except Exception:
                logger.error("platform_new_tenant: create_admin_user failed", exc_info=True)
                flash("הלקוח נוצר, אך יצירת משתמש הכניסה נכשלה — נסו להוסיפו בנפרד.", "warning")
                return redirect(url_for("platform_home"))

            _audit_log("platform_new_tenant", f"tenant={slug}")
            flash(
                f"הלקוח '{display_name}' נוצר! בעל העסק יכול להתחבר עם האימייל "
                "שהזנת. השתמש ב'פעל-כ' כדי להזין את פרטי הערוצים, או שבעל העסק "
                "יזין אותם בעצמו במסך 'הגדרות תשתית'.",
                "success",
            )
            return redirect(url_for("platform_home"))

        return render_template("platform_new_tenant.html", form={})

    # ─── Demo Mode entry ─────────────────────────────────────────────────
    @app.route("/demo")
    def demo_entry():
        """
        כניסה למצב דמו — בלי הרשמה, בלי סיסמה. יוצרת session ייעודית
        שמסומנת ב-session["demo"]=True; ה-middleware _enforce_demo_readonly
        חוסם כל POST/PUT/PATCH/DELETE מ-session כזה.
        כשה-DEMO_MODE כבוי — מחזיר 404 כדי לא לחשוף שהראוט קיים.
        rate limit: 20/שעה לכל IP, כדי למנוע sweeping של ה-endpoint
        האנונימי. גולש לגיטימי מהמודעה ייכנס פעם או פעמיים.
        """
        if not DEMO_MODE:
            return ("", 404)
        client_ip = request.remote_addr or "unknown"
        if _check_and_record_demo_entry(client_ip):
            logger.warning("Demo entry rate limit exceeded for IP %s", client_ip)
            _audit_log("demo_entry_throttled", f"ip={client_ip}")
            return ("יותר מדי בקשות. נסו שוב בעוד שעה.", 429)
        session.clear()
        session["logged_in"] = True
        session["demo"] = True
        # מזהה סשן דמו ייחודי — מאפשר ל-bubble timer בצד לקוח לזהות
        # סשנים חדשים ולאפס את הזמן המצטבר ב-sessionStorage. בלעדיו,
        # `aicw_first_visit_ts` היה שורד יציאה והתחברות מחדש באותו tab.
        session["demo_session_id"] = secrets.token_urlsafe(16)
        _audit_log("demo_entry", f"ip={client_ip}")
        return redirect(url_for("dashboard"))

    # ─── Developer Panel (/dev/*) — Plans + Feature Flags ─────────────────
    # האיזור הזה מנוהל ע"י ספק ה-SaaS (המפתח), נפרד לחלוטין מהפאנל של בעל
    # העסק. מוגן ב-DEVELOPER_PASSWORD נפרד; אם לא מוגדר — ראוטים מחזירים 404
    # כדי לא לחשוף את קיומם. ראה docs/plans_feature_flags_spec.md סעיף 3.5.

    @app.route("/dev/login", methods=["GET", "POST"])
    def dev_login():
        if not _is_developer_access_enabled():
            return ("", 404)

        if request.method == "POST":
            client_ip = request.remote_addr or "unknown"
            if _check_dev_login_rate_limit(client_ip):
                logger.warning("Dev login rate limit exceeded for IP %s", client_ip)
                flash("יותר מדי ניסיונות התחברות. נסו שוב בעוד שעה.", "danger")
                return render_template("dev_login.html")

            password = request.form.get("password", "")
            if _verify_developer_password(password):
                session["dev_authenticated"] = True
                session["dev_auth_expires_at"] = (
                    datetime.now(timezone.utc)
                    + timedelta(minutes=_DEV_SESSION_LIFETIME_MINUTES)
                ).isoformat()
                _audit_log("dev_login_success", f"ip={client_ip}")
                flash("התחברת בהצלחה לאיזור המפתח.", "success")
                return redirect(url_for("dev_subscription"))

            _record_dev_login_attempt(client_ip)
            logger.warning("Failed dev login from IP %s", client_ip)
            flash("סיסמה שגויה.", "danger")

        return render_template("dev_login.html")

    @app.route("/dev/logout", methods=["POST"])
    def dev_logout():
        if not _is_developer_access_enabled():
            return ("", 404)
        session.pop("dev_authenticated", None)
        session.pop("dev_auth_expires_at", None)
        flash("התנתקת מאיזור המפתח.", "info")
        return redirect(url_for("dev_login"))

    @app.route("/dev/subscription", methods=["GET"])
    @require_developer
    def dev_subscription():
        subscription_row = feature_flags.get_subscription_row()
        history = feature_flags.get_plan_history(limit=50)
        # מבנה תצוגה לכל פיצ'ר: ערך אפקטיבי, ברירת מחדל מהחבילה, האם יש override
        overrides = feature_flags.get_feature_overrides()
        plan_def = plans_config.get_plan_definition(subscription_row.get("plan"))
        plan_defaults = plan_def.get("features", {})
        feature_rows = []
        for feat in sorted(plans_config.ALL_FEATURES):
            default_value = plan_defaults.get(feat)
            has_override = feat in overrides
            current_value = overrides[feat] if has_override else default_value
            feature_rows.append({
                "name": feat,
                "default_value": default_value,
                "has_override": has_override,
                "current_value": current_value,
                "is_active": feature_flags.has_feature(feat),
            })
        return render_template(
            "dev_subscription.html",
            subscription=subscription_row,
            plan_definition=plan_def,
            plans=plans_config.PLANS,
            valid_plans=plans_config.VALID_PLANS,
            feature_rows=feature_rows,
            overrides_json=json.dumps(overrides, ensure_ascii=False, indent=2),
            history=history,
            grace_ends_at=feature_flags.grace_period_ends_at(),
        )

    @app.route("/dev/subscription/set-plan", methods=["POST"])
    @require_developer
    def dev_set_plan():
        new_plan = (request.form.get("plan") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if not plans_config.is_valid_plan(new_plan):
            flash("חבילה לא תקינה.", "danger")
            return redirect(url_for("dev_subscription"))
        try:
            feature_flags.set_plan(new_plan, reason=reason)
        except Exception:
            logger.error("dev_set_plan failed", exc_info=True)
            flash("שגיאה בעדכון החבילה. ראו לוגים.", "danger")
            return redirect(url_for("dev_subscription"))
        _audit_log("dev_set_plan", f"plan={new_plan} reason={reason!r}")
        flash(f"החבילה עודכנה ל-{plans_config.PLANS[new_plan]['display_name']}.", "success")
        return redirect(url_for("dev_subscription"))

    @app.route("/dev/subscription/override", methods=["POST"])
    @require_developer
    def dev_override_feature():
        feature_name = (request.form.get("feature") or "").strip()
        raw_value = (request.form.get("value") or "").strip().lower()
        if not plans_config.is_valid_feature(feature_name):
            flash("שם פיצ'ר לא תקין.", "danger")
            return redirect(url_for("dev_subscription"))
        # ערכים מותרים: 'true' / 'false' / מספר שלם / 'null'
        # 'reset' מטופל בנתיב נפרד; כאן מטפלים רק ב-overrides אמיתיים.
        if raw_value == "true":
            value = True
        elif raw_value == "false":
            value = False
        elif raw_value == "null":
            value = None
        else:
            try:
                value = int(raw_value)
            except (ValueError, TypeError):
                flash("ערך לא תקין. השתמשו ב-true / false / null / מספר שלם.", "danger")
                return redirect(url_for("dev_subscription"))
        try:
            feature_flags.override_feature(feature_name, value)
        except Exception:
            logger.error("dev_override_feature failed", exc_info=True)
            flash("שגיאה בעדכון הפיצ'ר. ראו לוגים.", "danger")
            return redirect(url_for("dev_subscription"))
        _audit_log("dev_override_feature", f"feature={feature_name} value={value!r}")
        flash(f"הפיצ'ר {feature_name} עודכן ידנית ל-{value}.", "success")
        return redirect(url_for("dev_subscription"))

    @app.route("/dev/subscription/reset/<feature_name>", methods=["POST"])
    @require_developer
    def dev_reset_feature(feature_name: str):
        if not plans_config.is_valid_feature(feature_name):
            flash("שם פיצ'ר לא תקין.", "danger")
            return redirect(url_for("dev_subscription"))
        try:
            feature_flags.reset_feature_to_plan_default(feature_name)
        except Exception:
            logger.error("dev_reset_feature failed", exc_info=True)
            flash("שגיאה באיפוס הפיצ'ר. ראו לוגים.", "danger")
            return redirect(url_for("dev_subscription"))
        _audit_log("dev_reset_feature", f"feature={feature_name}")
        flash(f"הפיצ'ר {feature_name} אופס לברירת המחדל של החבילה.", "success")
        return redirect(url_for("dev_subscription"))

    # ─── My Plan (Phase 5) — תצוגת חבילה עבור בעל העסק ───────────────────
    # מסך read-only. בעל העסק רואה את החבילה הנוכחית, פיצ'רים פעילים/נעולים,
    # ותקופת חסד. שינויים דרך /dev/subscription בלבד (מפתח). NOT feature-gated —
    # הלקוח חייב לראות את החבילה שלו תמיד.

    @app.route("/my-plan")
    @login_required
    def my_plan():
        subscription = feature_flags.get_subscription_row()
        plan_def = plans_config.get_plan_definition(subscription.get("plan"))
        # מבנה תצוגה לכל פיצ'ר
        overrides = feature_flags.get_feature_overrides()
        plan_features = plan_def.get("features", {})
        feature_rows = []
        # מיפוי לתיאורים יפים — מתועד בקוד ומשמש פה ובתבנית.
        # רק פיצ'רים שיכולים להיחסם מוצגים ב-/my-plan: calendar_sync ו-
        # scenarios_max פעילים בכל החבילות (universal) ולכן מיותרים בתצוגה
        # של בעל העסק. מסונן בלולאה למטה דרך get_min_plan_for_feature.
        labels_he = {
            "followup_24h": "פולואפ אוטומטי 24h",
            "broadcast": "שליחת broadcasts ידנית",
            "widget": "Widget להטמעה באתר",
        }
        for feat in sorted(plans_config.ALL_FEATURES):
            min_plan = plans_config.get_min_plan_for_feature(feat)
            # מסננים פיצ'רים universal (None מ-get_min_plan_for_feature) —
            # הם פעילים תמיד ואין מה להציג עליהם בעמוד "החבילה שלי".
            if min_plan is None:
                continue
            default_value = plan_features.get(feat)
            has_override = feat in overrides
            current_value = overrides[feat] if has_override else default_value
            is_active = feature_flags.has_feature(feat)
            feature_rows.append({
                "name": feat,
                "label": labels_he.get(feat, feat),
                "is_active": is_active,
                "default_value": default_value,
                "current_value": current_value,
                "has_override": has_override,
                "min_plan_key": min_plan,
            })
        return render_template(
            "my_plan.html",
            subscription=subscription,
            plan_definition=plan_def,
            plans=plans_config.PLANS,
            feature_rows=feature_rows,
        )

    # ─── Dashboard ────────────────────────────────────────────────────────

    @app.route("/")
    @login_required
    def dashboard():
        db.expire_past_appointments()
        # שאילתה מאוחדת — כל מוני הדשבורד בשאילתה אחת
        counts = db.get_dashboard_counts()
        stats = {
            **counts,
            "active_live_chats": LiveChatService.count_active(),
        }
        pending_requests = db.get_agent_requests(status="pending", limit=5)
        # רשימת "תורים חדשים" — חייבת להתאים לסמנטיקה של pending_appointments
        # ב-stats (owner_seen=0 AND status!='cancelled'), לא רק status='pending'.
        # אחרת הספירה למעלה והרשימה למטה לא יתאמו (למשל auto-confirmed שלא נצפה
        # יספור ב-badge אבל לא יופיע ברשימה).
        pending_appointments = db.get_unseen_appointments(limit=5)
        active_live_chats = LiveChatService.get_all_active()
        recent_gaps = db.get_unanswered_questions(status="open", limit=5)

        # סטטוסים לדשבורד
        vacation = db.get_vacation_mode()
        gcal_creds = db.get_google_calendar_credentials()
        # gcal_connected משקף בריאות אמיתית — credentials קיימים *וגם* לא
        # סומנו כשבורים. זה חיוני כדי שהדשבורד לא יראה "מחובר" לבעלי עסק
        # שהטוקן שלהם פג ותורים לא מסונכרנים ליומן.
        gcal_auth_invalid = bool(gcal_creds and gcal_creds.get("auth_invalid_at"))
        gcal_connected = bool(gcal_creds and not gcal_auth_invalid)

        return render_template(
            "dashboard.html",
            stats=stats,
            recent_requests=pending_requests,
            recent_appointments=pending_appointments,
            active_live_chats=active_live_chats,
            recent_gaps=recent_gaps,
            vacation_active=bool(vacation.get("is_active")),
            gcal_connected=gcal_connected,
            gcal_auth_invalid=gcal_auth_invalid,
        )
    
    # ─── Knowledge Base Management ────────────────────────────────────────
    
    @app.route("/kb")
    @login_required
    def kb_list():
        category_filter = request.args.get("category", None)
        entries = db.get_all_kb_entries(category=category_filter, active_only=False)
        categories = db.get_kb_categories()
        return render_template(
            "kb_list.html",
            entries=entries,
            categories=categories,
            current_category=category_filter,
        )
    
    @app.route("/kb/add", methods=["GET", "POST"])
    @login_required
    def kb_add():
        if request.method == "POST":
            category = request.form.get("category", "").strip()
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            gap_id = request.form.get("gap_id", "").strip()

            if not all([category, title, content]):
                flash("כל השדות הם חובה.", "danger")
            else:
                db.add_kb_entry(category, title, content)
                mark_index_stale()
                _audit_log("kb_add", f"category={category} title={title}")
                # Auto-resolve the knowledge gap if this entry was added from one
                if gap_id:
                    try:
                        db.update_unanswered_question_status(int(gap_id), "resolved")
                    except (ValueError, Exception):
                        pass
                flash(f"הרשומה '{title}' נוספה בהצלחה!", "success")
                return redirect(url_for("kb_list"))

        # Pre-fill from knowledge gap link
        prefill_question = request.args.get("question", "")
        gap_id = request.args.get("gap_id", "")

        categories = db.get_kb_categories()
        return render_template(
            "kb_form.html",
            entry=None,
            categories=categories,
            action="Add",
            prefill_question=prefill_question,
            gap_id=gap_id,
        )
    
    @app.route("/kb/edit/<int:entry_id>", methods=["GET", "POST"])
    @login_required
    def kb_edit(entry_id):
        entry = db.get_kb_entry(entry_id)
        if not entry:
            flash("הרשומה לא נמצאה.", "danger")
            return redirect(url_for("kb_list"))
        
        if request.method == "POST":
            category = request.form.get("category", "").strip()
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            
            if not all([category, title, content]):
                flash("כל השדות הם חובה.", "danger")
            else:
                db.update_kb_entry(entry_id, category, title, content)
                mark_index_stale()
                _audit_log("kb_edit", f"entry_id={entry_id} title={title}")
                flash(f"הרשומה '{title}' עודכנה בהצלחה!", "success")
                return redirect(url_for("kb_list"))
        
        categories = db.get_kb_categories()
        return render_template(
            "kb_form.html",
            entry=entry,
            categories=categories,
            action="Edit",
        )
    
    @app.route("/kb/delete/<int:entry_id>", methods=["POST"])
    @login_required
    def kb_delete(entry_id):
        db.delete_kb_entry(entry_id)
        mark_index_stale()
        _audit_log("kb_delete", f"entry_id={entry_id}")
        if request.headers.get("HX-Request"):
            if db.count_kb_entries(active_only=False) == 0:
                resp = app.make_response(
                    render_template("partials/kb_empty.html")
                )
                resp.headers["HX-Retarget"] = "#kb-table-wrapper"
                resp.headers["HX-Reswap"] = "outerHTML"
            else:
                resp = app.make_response("")
            resp.headers["HX-Trigger"] = "showStaleWarning"
            return resp
        flash("הרשומה נמחקה.", "success")
        return redirect(url_for("kb_list"))
    
    @app.route("/kb/rebuild", methods=["POST"])
    @login_required
    def kb_rebuild():
        try:
            rebuild_index(force_full=True)
            flash("אינדקס RAG נבנה מחדש בהצלחה!", "success")
        except Exception as e:
            logger.error("Index rebuild failed: %s", e)
            flash(f"בניית האינדקס נכשלה: {str(e)}", "danger")
        return redirect(url_for("kb_list"))

    @app.route("/kb/search")
    @login_required
    def kb_search():
        """חיפוש סמנטי ב-Knowledge Base — מחזיר את הקטעים הרלוונטיים ביותר לשאילתה."""
        query = request.args.get("q", "").strip()
        if not query:
            if request.headers.get("HX-Request"):
                return ""
            return redirect(url_for("kb_list"))

        try:
            chunks = retrieve(query, top_k=10)
        except Exception as e:
            logger.error("KB search failed: %s", e)
            chunks = []

        if request.headers.get("HX-Request"):
            return render_template("partials/kb_search_results.html", chunks=chunks, query=query)

        # Fallback — redirect ל-KB list (החיפוש עובד רק דרך HTMX)
        return redirect(url_for("kb_list"))

    # ─── Conversations ────────────────────────────────────────────────────
    
    @app.route("/conversations")
    @login_required
    def conversations():
        users = db.get_unique_users()
        selected_user = request.args.get("user_id", None)
        # נורמליזציה דרך helper משותף — מטפל ב-' 972...' (אחרי URL decode
        # של `+` ל-space) וב-'972...' (ללא קידומת). חשוב להשתמש בפונקציה
        # הקיימת במקום בדיקה inline, כדי לא לעוות את 9720XXX (אסור) או
        # מחרוזות קצרות מדי.
        if selected_user:
            selected_user = _normalize_user_id(selected_user)

        if selected_user:
            messages = db.get_conversation_history(selected_user, limit=100)
        else:
            messages = db.get_all_conversations(limit=200)

        # Build a set of user_ids with active live chats for quick lookup
        active_live_chats = {lc["user_id"] for lc in LiveChatService.get_all_active()}
        # Pending agent requests (transfer notifications)
        pending_requests = db.get_agent_requests(status="pending")

        return render_template(
            "conversations.html",
            users=users,
            messages=messages,
            selected_user=selected_user,
            active_live_chats=active_live_chats,
            pending_requests=pending_requests,
        )
    
    # ─── Live Chat ────────────────────────────────────────────────────────

    def require_active_live_chat(f):
        """Admin decorator: reject request if the live chat session is not active."""
        @wraps(f)
        def decorated(user_id, *args, **kwargs):
            if not LiveChatService.is_active(user_id):
                if request.headers.get("HX-Request"):
                    resp = app.make_response(("", 409))
                    resp.headers["HX-Trigger"] = json.dumps(
                        {"showToast": {"message": "השיחה החיה הסתיימה. רעננו את הדף.", "type": "warning"}}
                    )
                    return resp
                flash("השיחה החיה הסתיימה.", "warning")
                return redirect(url_for("live_chat", user_id=user_id))
            return f(user_id, *args, **kwargs)
        return decorated

    def _normalize_user_id(user_id: str) -> str:
        """החזרת מזהה מנורמל אחרי URL decode.

        כש-user_id של WhatsApp מועבר ב-URL כ-'+972...', הקידום `+` עלול
        להתפרש כרווח (תקן application/x-www-form-urlencoded). אחרי שהמזהה
        מגיע ל-Flask כ-' 972...' (עם רווח מוביל), אנו מחזירים את ה-+
        כדי שיתאים ל-DB.

        BSUID (`IL.abc123XYZ`) לא דורש נורמליזציה — הוא לא מכיל `+` ולא
        רגישות ל-URL encoding (נקודה ו-alphanumeric בלבד). short-circuit
        מוקדם כדי שלא נפעיל עליו לוגיקת טלפון בטעות.
        """
        if not isinstance(user_id, str):
            return user_id
        cleaned = user_id.lstrip()
        if _BSUID_RE.match(cleaned):
            return cleaned
        if cleaned.startswith("972") and len(cleaned) >= 12 and cleaned[3:4].isdigit() and cleaned[3] != "0":
            return "+" + cleaned
        return cleaned if cleaned != user_id else user_id

    def _validate_user_id(f):
        """דקורטור: מוודא ש-user_id הוא מזהה תקין (Telegram מספרי או WhatsApp טלפוני).

        מבצע נורמליזציה לפני הוולידציה — מטפל ב-' 972...' (אחרי URL decode
        של `+`) ומחזיר אותו ל-+972... כדי שיתאים ל-DB.
        """
        @wraps(f)
        def decorated(user_id, *args, **kwargs):
            user_id = _normalize_user_id(user_id)
            if not _USER_ID_RE.match(str(user_id)):
                if request.headers.get("HX-Request"):
                    return app.make_response(("", 400))
                flash("מזהה משתמש לא תקין.", "danger")
                return redirect(url_for("conversations"))
            return f(user_id, *args, **kwargs)
        return decorated

    @app.route("/live-chat/<user_id>")
    @login_required
    @_validate_user_id
    def live_chat(user_id):
        live_session = LiveChatService.get_session(user_id)
        messages = db.get_conversation_history(user_id, limit=100)
        username = LiveChatService.get_customer_username(user_id)
        return render_template(
            "live_chat.html",
            user_id=user_id,
            username=username,
            messages=messages,
            live_session=live_session,
        )

    @app.route("/live-chat/<user_id>/start", methods=["POST"])
    @login_required
    @_validate_user_id
    def live_chat_start(user_id):
        channel = db.get_user_channel(user_id)
        sent, status = LiveChatService.start(user_id, channel=channel)
        if status == "already_active":
            flash("השיחה החיה כבר פעילה.", "info")
        elif status == "send_failed":
            flash("השיחה החיה הופעלה, אך ההודעה ללקוח נכשלה.", "warning")
        return redirect(url_for("live_chat", user_id=user_id))

    @app.route("/live-chat/<user_id>/end", methods=["POST"])
    @login_required
    @_validate_user_id
    def live_chat_end(user_id):
        back = _safe_redirect_back(url_for("conversations"))
        sent, status = LiveChatService.end(user_id)
        if status == "already_ended":
            flash("השיחה החיה כבר הסתיימה.", "info")
        elif status == "send_failed":
            flash("השיחה הוחזרה לבוט, אך ההודעה ללקוח נכשלה.", "warning")
        return redirect(back)

    @app.route("/live-chat/<user_id>/send", methods=["POST"])
    @login_required
    @_validate_user_id
    @require_active_live_chat
    def live_chat_send(user_id):
        message_text = request.form.get("message", "").strip()
        success, status = LiveChatService.send(user_id, message_text)

        if not success:
            error_messages = {
                "session_ended": ("השיחה החיה הסתיימה.", "warning", 409),
                "empty_message": ("לא ניתן לשלוח הודעה ריקה.", "danger", 422),
                "send_failed": ("שליחת ההודעה נכשלה.", "danger", 500),
            }
            msg, level, code = error_messages.get(status, ("שגיאה לא צפויה.", "danger", 500))
            if request.headers.get("HX-Request"):
                resp = app.make_response(("", code))
                if status != "empty_message":
                    resp.headers["HX-Trigger"] = json.dumps(
                        {"showToast": {"message": msg, "type": level}}
                    )
                return resp
            flash(msg, level)
            return redirect(url_for("live_chat", user_id=user_id))

        if request.headers.get("HX-Request"):
            messages = db.get_conversation_history(user_id, limit=100)
            return render_template("partials/live_chat_messages.html", messages=messages)

        return redirect(url_for("live_chat", user_id=user_id))

    @app.route("/api/live-chat/<user_id>/messages")
    @login_required
    @_validate_user_id
    def api_live_chat_messages(user_id):
        """Polling endpoint for live chat messages (HTMX)."""
        messages = db.get_conversation_history(user_id, limit=100)
        return render_template("partials/live_chat_messages.html", messages=messages)

    # ─── Agent Requests ───────────────────────────────────────────────────

    @app.route("/requests")
    @login_required
    def agent_requests():
        requests_list = db.get_agent_requests()
        # שיחות חיות פעילות — כדי להציג סטטוס נכון בבקשות נציג
        active_live_chats = {lc["user_id"] for lc in LiveChatService.get_all_active()}
        return render_template(
            "requests.html",
            requests=requests_list,
            active_live_chats=active_live_chats,
        )
    
    @app.route("/requests/<int:request_id>/handle", methods=["POST"])
    @login_required
    def handle_request(request_id):
        status = request.form.get("status", "handled")
        if status not in VALID_AGENT_REQUEST_STATUSES:
            if request.headers.get("HX-Request"):
                resp = app.make_response(("", 422))
                resp.headers["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": "סטטוס לא חוקי.", "type": "danger"}}
                )
                return resp
            flash("סטטוס לא חוקי.", "danger")
            return redirect(url_for("agent_requests"))
        db.update_agent_request_status(request_id, status)

        if request.headers.get("HX-Request"):
            req = db.get_agent_request(request_id)
            if req:
                return render_template("partials/request_row.html", req=req)
            return ""
        flash(f"בקשה #{request_id} סומנה כ-{status}.", "success")
        return redirect(url_for("agent_requests"))
    
    # ─── Appointments ─────────────────────────────────────────────────────

    def _enrich_pending_with_duration_options(appointments_list):
        """הוספת duration_options + default_duration_minutes לכל תור pending,
        ו-effective_duration_minutes לכל תור מאושר.

        משותף לדף התורים, ל-/api/appointments/rows ול-/api/appointments/data
        כדי שכפתורי המשך יישמרו בכל ריענון. ללא העשרה, ה-template נופל ל-fallback
        של ברירת מחדל יחידה ובעל העסק יאבד את האפשרות לבחור משך אחר.

        משתמש בגרסת ה-batch של compute_duration_options כדי למנוע N+1 על polling
        כל 15 שניות (קישור שאילתות לפי תאריך, cache לשירותים, GCal פעם אחת).

        ברירת המחדל היא גלובלית — אחידה לכל השירותים (default_appointment_duration_minutes
        מ-bot_settings). ביטלנו את ההגדרה הפר-שירות.

        עבור תורים מאושרים — מחושב effective_duration_minutes (confirmed או fallback
        לברירת מחדל) כדי להציג בעמודת "משך תור" בטבלה ובמודל היום.
        """
        # ברירת מחדל גלובלית אחידה — קוראים פעם אחת ולא פר-תור
        default_minutes = int(db.get_appointment_duration_settings().get("default_minutes") or 60)

        # תורים מאושרים: מחשבים את המשך האפקטיבי להצגה בטבלה
        for appt in appointments_list:
            if appt.get("status") == "confirmed":
                appt["effective_duration_minutes"] = int(
                    appt.get("confirmed_duration_minutes") or default_minutes
                )

        pending = [a for a in appointments_list if a.get("status") == "pending"]
        if not pending:
            return
        try:
            options_by_id = db.compute_duration_options_for_pending(pending)
        except Exception:
            logger.error(
                "שגיאה בחישוב duration_options ב-batch", exc_info=True,
            )
            options_by_id = {}
        for appt in pending:
            appt["duration_options"] = options_by_id.get(appt["id"], [])
            appt["default_duration_minutes"] = default_minutes

    def _build_calendar_context(appointments_list):
        """חישוב הקשר לוח שנה — משותף לדף הראשי ול-API partial."""
        import calendar as _cal
        from collections import defaultdict

        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        today = datetime.now(ISRAEL_TZ).date()
        if not year or not month:
            year, month = today.year, today.month

        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        appt_by_date = defaultdict(list)
        for a in appointments_list:
            d = a.get("preferred_date", "")
            if d:
                appt_by_date[d].append(a)

        month_days = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)
        he_months = [
            "", "ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
            "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר",
        ]

        return dict(
            cal_weeks=month_days,
            cal_year=year,
            cal_month=month,
            cal_month_name=he_months[month],
            prev_year=prev_year,
            prev_month=prev_month,
            next_year=next_year,
            next_month=next_month,
            appt_by_date=appt_by_date,
            today_iso=today.isoformat(),
        )

    @app.route("/appointments", methods=["GET", "POST"])
    @login_required
    def appointments():
        if request.method == "POST":
            form_type = request.form.get("form_type", "")
            if form_type == "reminder":
                # טופס תזכורות — עדכון רק שדות תזכורת
                reminder_enabled = bool(request.form.get("reminder_enabled"))
                reminder_time = request.form.get("reminder_time", "10:00").strip()
                second_reminder_enabled = bool(request.form.get("second_reminder_enabled"))
                if not reminder_time or not _is_valid_time(reminder_time):
                    flash("שעה לא חוקית — יש להזין בפורמט HH:MM.", "danger")
                    return redirect(url_for("appointments"))

                # שעות לפני התור לתזכורת שנייה — ולידציה
                try:
                    second_reminder_hours = float(request.form.get("second_reminder_hours", "2"))
                    if not (0.5 <= second_reminder_hours <= 24):
                        raise ValueError
                except (ValueError, TypeError):
                    flash("ערך שעות לא חוקי — יש להזין מספר בין 0.5 ל-24.", "danger")
                    return redirect(url_for("appointments"))

                current = db.get_bot_settings()
                db.update_bot_settings(
                    current["tone"], current.get("custom_phrases", ""),
                    reminder_enabled, reminder_time,
                    second_reminder_enabled=second_reminder_enabled,
                    second_reminder_hours=second_reminder_hours,
                )
                _audit_log("appointments", f"reminder_enabled={reminder_enabled}, reminder_time={reminder_time}, second_reminder_enabled={second_reminder_enabled}, second_reminder_hours={second_reminder_hours}")
                flash("הגדרות תזכורת עודכנו בהצלחה!", "success")
            elif form_type == "duration_options":
                # טופס אופציות משך תור — default + step + כמה אופציות אחורה וקדימה
                try:
                    default_min = int(request.form.get("default_appointment_duration_minutes", "60"))
                    step_min = int(request.form.get("appointment_duration_step_minutes", "15"))
                    backward = int(request.form.get("appointment_duration_steps_backward", "2"))
                    forward = int(request.form.get("appointment_duration_steps_forward", "4"))
                except (ValueError, TypeError):
                    flash("ערך לא תקין באחת מהשדות של אופציות משך תור.", "danger")
                    return redirect(url_for("appointments"))
                if default_min < 5 or default_min > 24 * 60:
                    flash("ברירת מחדל למשך תור חייבת להיות בין 5 דקות ל-24 שעות.", "danger")
                    return redirect(url_for("appointments"))
                if step_min < 5 or step_min > 120:
                    flash("גודל קפיצה חייב להיות בין 5 ל-120 דקות.", "danger")
                    return redirect(url_for("appointments"))
                if not (0 <= backward <= 10) or not (0 <= forward <= 10):
                    flash("מספר האופציות אחורה/קדימה חייב להיות בין 0 ל-10.", "danger")
                    return redirect(url_for("appointments"))
                db.update_appointment_duration_settings(
                    step_min, backward, forward, default_minutes=default_min,
                )
                _audit_log(
                    "appointments",
                    f"default={default_min}, duration_step={step_min}, backward={backward}, forward={forward}",
                )
                flash("הגדרות משך תור עודכנו בהצלחה!", "success")
            elif form_type == "ics_settings":
                # טופס קובץ יומן .ics — הפעלה/כיבוי
                ics_enabled = bool(request.form.get("ics_enabled"))
                current = db.get_bot_settings()
                db.update_bot_settings(
                    current["tone"], current.get("custom_phrases", ""),
                    ics_enabled=ics_enabled,
                )
                _audit_log("appointments", f"ics_enabled={ics_enabled}")
                flash("הגדרות קובץ יומן עודכנו בהצלחה!", "success")
            elif form_type == "auto_booking":
                # טופס אישור תורים אוטומטי — manual / auto_with_check / auto_always
                mode = request.form.get("auto_booking_mode", "manual").strip()
                if mode not in {"manual", "auto_with_check", "auto_always"}:
                    flash("מצב אישור תורים לא תקין.", "danger")
                    return redirect(url_for("appointments"))
                try:
                    max_days = int(request.form.get("auto_booking_max_days_ahead", "90"))
                    if not (1 <= max_days <= 365):
                        raise ValueError
                except (ValueError, TypeError):
                    flash("טווח ימים מקסימלי חייב להיות בין 1 ל-365.", "danger")
                    return redirect(url_for("appointments"))
                try:
                    buffer_min = int(request.form.get("auto_booking_buffer_after_event_minutes", "0"))
                    if not (0 <= buffer_min <= 240):
                        raise ValueError
                except (ValueError, TypeError):
                    flash("מרווח אחרי אירוע חייב להיות בין 0 ל-240 דקות.", "danger")
                    return redirect(url_for("appointments"))
                current = db.get_bot_settings()
                db.update_bot_settings(
                    current["tone"], current.get("custom_phrases", ""),
                    auto_booking_mode=mode,
                    auto_booking_max_days_ahead=max_days,
                    auto_booking_buffer_after_event_minutes=buffer_min,
                )
                _audit_log(
                    "appointments",
                    f"auto_booking_mode={mode}, max_days_ahead={max_days}, "
                    f"buffer_min={buffer_min}",
                )
                flash("הגדרות אישור תורים אוטומטי עודכנו בהצלחה!", "success")
            return redirect(url_for("appointments"))

        db.expire_past_appointments()
        appointments_list = db.get_appointments()
        _enrich_pending_with_duration_options(appointments_list)
        cal_ctx = _build_calendar_context(appointments_list)
        settings = db.get_bot_settings()
        # ברירת מחדל לתצוגה — duration של השירות, כדי שהכפתור הראשי יבלוט
        duration_settings = db.get_appointment_duration_settings()

        # סימון התורים שנשלפו כ"נצפו" — אחרי השליפה כדי שהאינדיקטורים על
        # הדף הזה יוצגו ברינדור הנוכחי, ויעלמו ברענון הבא (HTMX polling).
        # מעבירים IDs מפורשים ולא "כולם" כדי למנוע race: תור שנוצר בין
        # get_appointments() להפעולה הזו לא יסומן כנצפה ויוצג ברענון הבא.
        appt_ids_to_mark = [
            a["id"] for a in appointments_list
            if a.get("owner_seen") == 0 and a.get("status") != "cancelled"
        ]
        if appt_ids_to_mark:
            db.mark_appointments_seen(appt_ids_to_mark)

        return render_template(
            "appointments.html",
            appointments=appointments_list,
            settings=settings,
            duration_settings=duration_settings,
            **cal_ctx,
        )
    
    @app.route("/appointments/<int:appt_id>/update", methods=["POST"])
    @login_required
    def update_appointment(appt_id):
        status = request.form.get("status", "confirmed")
        owner_message = request.form.get("owner_message", "").strip()
        # duration_minutes — אופציונלי. כשבעל העסק לוחץ "אשר ב-X דק׳" המספר נשלח בטופס
        # ונשמר ב-confirmed_duration_minutes; משמש את GCal sync ואת הודעת הלקוח.
        duration_raw = (request.form.get("duration_minutes") or "").strip()
        confirmed_duration: int | None = None
        if duration_raw:
            try:
                d = int(duration_raw)
                if 5 <= d <= 24 * 60:
                    confirmed_duration = d
            except ValueError:
                logger.warning("update_appointment: duration_minutes לא תקין: %r", duration_raw)
        if status not in VALID_APPOINTMENT_STATUSES:
            if request.headers.get("HX-Request"):
                resp = app.make_response(("", 422))
                resp.headers["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": "סטטוס לא חוקי.", "type": "danger"}}
                )
                return resp
            flash("סטטוס לא חוקי.", "danger")
            return redirect(url_for("appointments"))
        db.update_appointment_status(appt_id, status, confirmed_duration_minutes=confirmed_duration)

        # שליחת התראת סטטוס אוטומטית ללקוח בטלגרם
        appt = db.get_appointment(appt_id)
        if appt:
            try:
                notify_appointment_status(appt, owner_message=owner_message)
            except Exception:
                logger.error(
                    "Failed to send status notification for appointment #%d",
                    appt_id, exc_info=True,
                )

        # הפעלת מערכת הפניות — כשתור מאושר, בודקים אם הלקוח הגיע דרך הפניה
        if status == "confirmed" and appt:
            user_id = appt["user_id"]
            if db.has_pending_referral(user_id):
                activated = db.complete_referral(user_id)
                if activated:
                    logger.info(
                        "Referral completed for user %s (appointment #%d)",
                        user_id, appt_id,
                    )

            # שליחת קוד הפניה ללקוח אחרי אישור תור
            # try_send_referral_code — לוגיקה משותפת לבוט ולאדמין:
            # generate → mark → send → unmark on failure
            # ניתוב לפי channel של התור — תומך גם ב-Telegram וגם ב-WhatsApp
            appt_channel = appt.get("channel", "telegram") or "telegram"
            try_send_referral_code(
                user_id,
                send_fn=lambda text: send_message_by_channel(user_id, text, appt_channel),
                channel=appt_channel,
            )

        if request.headers.get("HX-Request"):
            if not appt:
                appt = db.get_appointment(appt_id)
            if appt:
                # העשרה לפני render — בלי זה השורה שתוחלף ב-HTMX לא תכיל
                # effective_duration_minutes, ועמודת "משך תור" תוצג ריקה (" דק׳")
                # עד הריענון הבא של הטבלה.
                _enrich_pending_with_duration_options([appt])
                return render_template("partials/appointment_row.html", appt=appt)
            return ""
        flash(f"תור #{appt_id} סומן כ-{status}.", "success")
        return redirect(url_for("appointments"))
    
    # ─── Knowledge Gaps (Unanswered Questions) ─────────────────────────────

    VALID_UNANSWERED_STATUSES = {"open", "resolved", "not_relevant"}

    @app.route("/knowledge-gaps")
    @login_required
    def knowledge_gaps():
        status_filter = request.args.get("status", None)
        questions = db.get_unanswered_questions(status=status_filter)
        open_count = db.count_unanswered_questions(status="open")
        return render_template(
            "knowledge_gaps.html",
            questions=questions,
            current_status=status_filter,
            open_count=open_count,
        )

    @app.route("/knowledge-gaps/<int:question_id>/resolve", methods=["POST"])
    @login_required
    def resolve_question(question_id):
        status = request.form.get("status", "resolved")
        if status not in VALID_UNANSWERED_STATUSES:
            if request.headers.get("HX-Request"):
                resp = app.make_response(("", 422))
                resp.headers["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": "סטטוס לא חוקי.", "type": "danger"}}
                )
                return resp
            flash("סטטוס לא חוקי.", "danger")
            return redirect(url_for("knowledge_gaps"))
        db.update_unanswered_question_status(question_id, status)

        if request.headers.get("HX-Request"):
            q = db.get_unanswered_question(question_id)
            if q:
                return render_template("partials/knowledge_gap_row.html", q=q)
            return ""
        flash(f"שאלה #{question_id} עודכנה.", "success")
        return redirect(url_for("knowledge_gaps"))

    # ─── Business Hours ──────────────────────────────────────────────────

    @app.route("/business-hours")
    @login_required
    def business_hours():
        hours = db.get_all_business_hours()
        special_days = db.get_all_special_days()
        # שנים ייחודיות שמופיעות בטבלת הימים המיוחדים — לכפתורי מחיקה לפי שנה
        holiday_years = sorted({sd["date"][:4] for sd in special_days})
        return render_template(
            "business_hours.html",
            hours=hours,
            special_days=special_days,
            holiday_years=holiday_years,
            day_names=DAY_NAMES_HE,
        )

    @app.route("/business-hours/update", methods=["POST"])
    @login_required
    def business_hours_update():
        # שלב 1: קריאה וולידציה של כל הימים לפני כתיבה ל-DB
        days_data = []
        for day in range(7):
            is_closed = request.form.get(f"closed_{day}") == "on"
            open_time = request.form.get(f"open_{day}", "").strip()
            close_time = request.form.get(f"close_{day}", "").strip()
            if not _is_valid_time(open_time) or not _is_valid_time(close_time):
                day_name = DAY_NAMES_HE.get(day, str(day))
                flash(f"שעה לא תקינה ביום {day_name} — יש להזין בפורמט HH:MM (למשל 09:00).", "danger")
                return redirect(url_for("business_hours"))
            days_data.append((day, open_time, close_time, is_closed))
        # שלב 2: כל הקלטים תקינים — כותבים ל-DB
        for day, open_time, close_time, is_closed in days_data:
            db.upsert_business_hours(day, open_time, close_time, is_closed)
        flash("שעות הפעילות עודכנו בהצלחה!", "success")
        return redirect(url_for("business_hours"))

    @app.route("/business-hours/special-days/add", methods=["POST"])
    @login_required
    def special_day_add():
        date_str = request.form.get("date", "").strip()
        name = request.form.get("name", "").strip()
        is_closed = request.form.get("is_closed") == "on"
        open_time = request.form.get("open_time", "").strip() or None
        close_time = request.form.get("close_time", "").strip() or None
        notes = request.form.get("notes", "").strip()

        if not date_str or not name:
            flash("תאריך ושם הם שדות חובה.", "danger")
            return redirect(url_for("business_hours"))
        if not _is_valid_time(open_time) or not _is_valid_time(close_time):
            flash("שעה לא תקינה — יש להזין בפורמט HH:MM (למשל 09:00).", "danger")
            return redirect(url_for("business_hours"))

        db.add_special_day(date_str, name, is_closed, open_time, close_time, notes)
        flash(f"יום מיוחד '{name}' נוסף בהצלחה!", "success")
        return redirect(url_for("business_hours"))

    @app.route("/business-hours/special-days/<int:sd_id>/edit", methods=["POST"])
    @login_required
    def special_day_edit(sd_id):
        date_str = request.form.get("date", "").strip()
        name = request.form.get("name", "").strip()
        is_closed = request.form.get("is_closed") == "on"
        open_time = request.form.get("open_time", "").strip() or None
        close_time = request.form.get("close_time", "").strip() or None
        notes = request.form.get("notes", "").strip()

        if not date_str or not name:
            flash("תאריך ושם הם שדות חובה.", "danger")
            return redirect(url_for("business_hours"))
        if not _is_valid_time(open_time) or not _is_valid_time(close_time):
            flash("שעה לא תקינה — יש להזין בפורמט HH:MM (למשל 09:00).", "danger")
            return redirect(url_for("business_hours"))

        db.update_special_day(sd_id, date_str, name, is_closed, open_time, close_time, notes)
        flash(f"יום מיוחד '{name}' עודכן בהצלחה!", "success")
        return redirect(url_for("business_hours"))

    @app.route("/business-hours/special-days/<int:sd_id>/delete", methods=["POST"])
    @login_required
    def special_day_delete(sd_id):
        db.delete_special_day(sd_id)
        if request.headers.get("HX-Request"):
            return ""
        flash("יום מיוחד נמחק.", "success")
        return redirect(url_for("business_hours"))

    @app.route("/business-hours/special-days/delete-year/<int:year>", methods=["POST"])
    @login_required
    def special_days_delete_year(year):
        count = db.delete_special_days_by_year(year)
        flash(f"נמחקו {count} ימים מיוחדים של שנת {year}.", "success")
        return redirect(url_for("business_hours"))

    # ─── Services (שירותים — משך תור) ───────────────────────────────────

    @app.route("/services")
    @login_required
    def services():
        all_services = db.get_all_services()
        return render_template(
            "services.html",
            services=all_services,
        )

    @app.route("/services/add", methods=["POST"])
    @login_required
    def services_add():
        name = request.form.get("name", "").strip()
        duration = request.form.get("duration_minutes", "60").strip()
        if not name:
            flash("יש להזין שם שירות.", "error")
            return redirect(url_for("services"))
        try:
            duration_int = int(duration)
            if duration_int < 5:
                raise ValueError
        except ValueError:
            flash("משך תור לא תקין (מינימום 5 דקות).", "error")
            return redirect(url_for("services"))
        try:
            db.add_service(name, duration_int)
            flash(f"השירות '{name}' נוסף בהצלחה.", "success")
        except Exception:
            logger.error("שגיאה בהוספת שירות", exc_info=True)
            flash("שירות עם שם זהה כבר קיים.", "error")
        return redirect(url_for("services"))

    @app.route("/services/<int:service_id>/edit", methods=["POST"])
    @login_required
    def services_edit(service_id):
        name = request.form.get("name", "").strip()
        duration = request.form.get("duration_minutes", "60").strip()
        if not name:
            flash("יש להזין שם שירות.", "error")
            return redirect(url_for("services"))
        try:
            duration_int = int(duration)
            if duration_int < 5:
                raise ValueError
        except ValueError:
            flash("משך תור לא תקין (מינימום 5 דקות).", "error")
            return redirect(url_for("services"))
        try:
            db.update_service(service_id, name, duration_int)
            flash(f"השירות '{name}' עודכן.", "success")
        except Exception:
            logger.error("שגיאה בעדכון שירות", exc_info=True)
            flash("שגיאה בעדכון השירות.", "error")
        return redirect(url_for("services"))

    @app.route("/services/<int:service_id>/delete", methods=["POST"])
    @login_required
    def services_delete(service_id):
        db.delete_service(service_id)
        flash("השירות נמחק.", "success")
        return redirect(url_for("services"))

    # ─── Vacation Mode ─────────────────────────────────────────────────────

    @app.route("/vacation-mode", methods=["GET", "POST"])
    @login_required
    def vacation_mode():
        if request.method == "POST":
            is_active = request.form.get("is_active") == "on"
            vacation_end_date = request.form.get("vacation_end_date", "").strip()
            vacation_message = request.form.get("vacation_message", "").strip()
            db.update_vacation_mode(is_active, vacation_end_date, vacation_message)
            _audit_log("vacation_mode", f"is_active={is_active}")
            if is_active:
                flash("מצב חופשה הופעל!", "success")
            else:
                flash("מצב חופשה כובה.", "info")
            return redirect(url_for("vacation_mode"))

        vacation = db.get_vacation_mode()
        # תצוגה מקדימה — משתמש ב-VacationService כדי שהטקסט תמיד יתאים למה שהלקוח רואה
        preview_booking = VacationService.get_booking_message()
        preview_agent = VacationService.get_agent_message()
        return render_template(
            "vacation_mode.html",
            vacation=vacation,
            preview_booking=preview_booking,
            preview_agent=preview_agent,
        )

    # ─── Google Calendar ─────────────────────────────────────────────────────

    @app.route("/google-calendar")
    @login_required
    def google_calendar():
        """דף הגדרות Google Calendar — חיבור/ניתוק."""
        from ai_chatbot.config import GOOGLE_CLIENT_ID
        cred_data = db.get_google_calendar_credentials()
        is_configured = bool(GOOGLE_CLIENT_ID)
        # auth_invalid — credentials קיימים אבל refresh נכשל (טוקן פג / נמחק).
        # מציגים אזהרה אדומה במקום "פעיל" המטעה.
        auth_invalid = bool(cred_data and cred_data.get("auth_invalid_at"))
        return render_template(
            "google_calendar.html",
            credentials=cred_data,
            is_configured=is_configured,
            auth_invalid=auth_invalid,
        )

    @app.route("/google/connect")
    @login_required
    def google_connect():
        """התחלת OAuth flow לחיבור Google Calendar."""
        from ai_chatbot.config import GOOGLE_CLIENT_ID
        if not GOOGLE_CLIENT_ID:
            flash("Google Calendar לא מוגדר — יש להגדיר GOOGLE_CLIENT_ID ב-.env", "danger")
            return redirect(url_for("google_calendar"))

        try:
            from google_calendar import get_authorization_url
            # שמירת state ב-session למניעת CSRF
            import secrets
            state = secrets.token_urlsafe(32)
            session["google_oauth_state"] = state
            url, code_verifier = get_authorization_url(state=state)
            session["google_oauth_code_verifier"] = code_verifier
            _audit_log("google_calendar", "OAuth flow started")
            return redirect(url)
        except Exception:
            logger.error("שגיאה בהתחלת OAuth flow ל-Google Calendar", exc_info=True)
            flash("שגיאה בהתחלת חיבור Google Calendar.", "danger")
            return redirect(url_for("google_calendar"))

    @app.route("/google/callback")
    @login_required
    def google_callback():
        """OAuth callback — קבלת authorization code ושמירת credentials."""
        error = request.args.get("error")
        if error:
            flash(f"Google Calendar: חיבור בוטל ({error}).", "warning")
            return redirect(url_for("google_calendar"))

        # ולידציית state
        state = request.args.get("state", "")
        expected_state = session.pop("google_oauth_state", "")
        if not state or state != expected_state:
            flash("שגיאת אבטחה — state לא תואם. נסו שוב.", "danger")
            return redirect(url_for("google_calendar"))

        code = request.args.get("code", "")
        if not code:
            flash("לא התקבל authorization code.", "danger")
            return redirect(url_for("google_calendar"))

        try:
            from google_calendar import exchange_code_for_credentials
            code_verifier = session.pop("google_oauth_code_verifier", "")
            result = exchange_code_for_credentials(code, code_verifier=code_verifier)
            # דפוס #7 — לא כותבים email ללוג/אודיט. ה-domain מספיק לאבחון;
            # הכתובת המלאה מוצגת לבעל העסק ב-flash (תצוגה, לא לוג).
            _email = result.get("email", "")
            _domain = _email.split("@", 1)[1] if "@" in _email else "?"
            _audit_log("google_calendar", f"connected: domain={_domain}")
            flash(f"Google Calendar חובר בהצלחה! ({_email})", "success")
        except Exception:
            logger.error("שגיאה בחיבור Google Calendar", exc_info=True)
            flash("שגיאה בחיבור Google Calendar. נסו שוב.", "danger")

        return redirect(url_for("google_calendar"))

    @app.route("/google/disconnect", methods=["POST"])
    @login_required
    def google_disconnect():
        """ניתוק Google Calendar."""
        try:
            from google_calendar import disconnect_calendar
            disconnect_calendar()
            _audit_log("google_calendar", "disconnected")
            flash("Google Calendar נותק בהצלחה.", "success")
        except Exception:
            logger.error("שגיאה בניתוק Google Calendar", exc_info=True)
            flash("שגיאה בניתוק.", "danger")
        return redirect(url_for("google_calendar"))

    # ─── Bot Settings (הגדרות בוט — טון וביטויים) ─────────────────────────

    @app.route("/bot-settings", methods=["GET", "POST"])
    @login_required
    def bot_settings():
        if request.method == "POST":
            # טופס טון — עדכון טון, ביטויים ופרומפט מותאם
            tone = request.form.get("tone", "friendly").strip()
            custom_phrases = request.form.get("custom_phrases", "").strip()
            custom_prompt = request.form.get("custom_prompt", "").strip()
            if tone not in TONE_DEFINITIONS:
                flash("טון לא חוקי.", "danger")
            else:
                db.update_bot_settings(tone, custom_phrases, custom_prompt=custom_prompt)
                _audit_log("bot_settings", f"tone={tone}")
                flash("הגדרות הבוט עודכנו בהצלחה!", "success")
            return redirect(url_for("bot_settings"))

        settings = db.get_bot_settings()
        # הפרומפט שנבנה מהקוד (ברירת מחדל) — חייב לשקף את הערוץ הפעיל בפועל.
        # בלי channel, build_system_prompt נופל ל-"telegram" ומזריק את כללי
        # עיצוב הטלגרם (תגי HTML) גם ללקוח WhatsApp-only. מכיוון שתיבת ה-override
        # מאותחלת עם ה-preview הזה, שמירה תשמור פרומפט טלגרם שנשלח מילה-במילה
        # לשני הערוצים (llm.py) ותשבור עיצוב ב-WhatsApp.
        # סדר הקדימות: ערוץ ה-tenant הנעול (נקבע בחיבור הראשון) → זיהוי
        # פר-tenant (סודות ה-control plane, או env ל-default) → telegram.
        active_channel = (
            feature_flags.get_channel() or _detect_tenant_channel() or "telegram"
        )
        default_prompt = build_system_prompt(
            tone=settings.get("tone", "friendly"),
            custom_phrases=settings.get("custom_phrases", ""),
            follow_up_enabled=FOLLOW_UP_ENABLED,
            custom_prompt=settings.get("custom_prompt", ""),
            channel=active_channel,
        )
        # אם יש override מלא — הוא מה שנשלח למודל
        full_override = settings.get("full_system_prompt", "").strip()
        active_prompt = full_override if full_override else default_prompt
        return render_template(
            "bot_settings.html",
            settings=settings,
            tone_definitions=TONE_DEFINITIONS,
            tone_labels=TONE_LABELS,
            preview_prompt=active_prompt,
            default_prompt=default_prompt,
            has_override=bool(full_override),
        )

    @app.route("/bot-settings/save-full-prompt", methods=["POST"])
    @login_required
    def save_full_prompt():
        """שמירת פרומפט מערכת מלא — override על הפרומפט שנבנה מהקוד."""
        full_prompt = request.form.get("full_system_prompt", "").strip()
        db.update_full_system_prompt(full_prompt)
        _audit_log("bot_settings", "full_system_prompt override saved")
        flash("הפרומפט המלא נשמר בהצלחה! מעכשיו הפרומפט הזה יישלח למודל.", "success")
        return redirect(url_for("bot_settings"))

    @app.route("/bot-settings/reset-prompt", methods=["POST"])
    @login_required
    def reset_full_prompt():
        """איפוס — חזרה לפרומפט שנבנה מהקוד."""
        db.update_full_system_prompt("")
        _audit_log("bot_settings", "full_system_prompt override cleared")
        flash("הפרומפט אופס — חזרה לפרומפט ברירת המחדל מהקוד.", "success")
        return redirect(url_for("bot_settings"))

    @app.route("/business-card", methods=["GET", "POST"])
    @login_required
    def business_card():
        """כרטיס ביקור — טלפון/כתובת/אתר של העסק ל-vCard, ל-ICS ולעמוד
        הציבורי. שם העסק נקבע בהקמה (display_name) ומוצג כאן לקריאה בלבד.
        הערכים נצרכים בזמן-ריצה דרך config.get_business_config().
        """
        if request.method == "POST":
            phone = request.form.get("business_phone", "").strip()[:50]
            address = request.form.get("business_address", "").strip()[:300]
            website = request.form.get("business_website", "").strip()[:300]
            db.update_business_identity(
                phone=phone, address=address, website=website
            )
            # בלי הערכים עצמם בלוג — טלפון/כתובת הם PII של בעל העסק (דפוס #7)
            _audit_log("business_card", "contact details updated")
            flash("כרטיס הביקור עודכן בהצלחה!", "success")
            return redirect(url_for("business_card"))
        return render_template(
            "business_card.html",
            settings=db.get_bot_settings(),
        )

    # ─── Business Profile (שלב 2 של מערכת הזיכרון המתמשך) ───────────────
    # פרופיל עסק שמשמש את ה-fact extractor של מערכת הזיכרון. single-tenant:
    # קבוע 'default' (ENV BUSINESS_ID יתווסף ב-config.py בשלב 3).
    # ראה docs/Customer-memory/claude_code_instructions.md.

    BUSINESS_TYPE_OPTIONS = [
        "מספרה",
        "קליניקת אסתטיקה",
        "חנות אונליין",
        "יועצת תזונה",
        "קליניקה רפואית",
        "מסעדה / בית קפה",
        "סטודיו ספורט / יוגה",
        "אחר",
    ]

    @app.route("/business-profile", methods=["GET", "POST"])
    @login_required
    def business_profile_view():
        """טופס עריכה של פרופיל העסק — סוג, שירותים, ו-what_matters_for_extraction.

        הפרטים משמשים את ה-fact extractor (memory/extractor.py, שלב 3) כקלט
        לפרומפט. כל התוכן נשלח כל פעם ל-LLM — לכן יש אזהרה ב-UI לא לכלול
        PII של לקוחות.
        """
        memory_business_id = "default"  # forward-compat; יוטמע כקבוע מ-config.py בשלב 3

        if request.method == "POST":
            business_type = (request.form.get("business_type") or "").strip()
            biz_name = (request.form.get("business_name_field") or "").strip()
            what_matters = (request.form.get("what_matters_for_extraction") or "").strip()

            # שירותים דינמיים — שלוש רשימות מקבילות מ-getlist
            names = [s.strip() for s in request.form.getlist("service_name")]
            aliases_raw = [s.strip() for s in request.form.getlist("service_aliases")]
            categories = [s.strip() for s in request.form.getlist("service_category")]

            services = []
            for i, nm in enumerate(names):
                if not nm:
                    continue  # שורה ריקה — מדלגים (משתמש לחץ "הוסף" ולא מילא)
                aliases_csv = aliases_raw[i] if i < len(aliases_raw) else ""
                aliases = [a.strip() for a in aliases_csv.split(",") if a.strip()]
                category = categories[i] if i < len(categories) else ""
                services.append({
                    "name": nm,
                    "aliases": aliases,
                    "category": category,
                })

            db.upsert_business_profile({
                "business_id": memory_business_id,
                "business_type": business_type,
                "business_name": biz_name,
                "services_json": json.dumps(services, ensure_ascii=False),
                "what_matters_for_extraction": what_matters,
            })
            _audit_log("business_profile_save", f"services={len(services)} type={business_type[:30]}")
            flash("פרופיל העסק נשמר בהצלחה.", "success")
            return redirect(url_for("business_profile_view"))

        # GET — שליפה ותצוגה
        profile = db.get_business_profile(memory_business_id)
        services = []
        services_json_raw = profile.get("services_json") or "[]"
        try:
            parsed = json.loads(services_json_raw)
            if isinstance(parsed, list):
                services = parsed
        except (json.JSONDecodeError, TypeError):
            logger.error("business_profile_view: כשל בפענוח services_json", exc_info=True)

        return render_template(
            "business_profile.html",
            profile=profile,
            services=services,
            business_type_options=BUSINESS_TYPE_OPTIONS,
        )

    # ─── שלב 7 — פאנל ניהול customer_facts (תור אישור + זיכרון לקוחות) ──

    def _confidence_badge_class(confidence: float) -> str:
        """badge color לפי confidence — אדום/צהוב/ירוק. גם מוגדר ב-Jinja."""
        try:
            c = float(confidence)
        except (TypeError, ValueError):
            return "badge-muted"
        if c < 0.7:
            return "badge-danger"
        if c < 0.85:
            return "badge-warning"
        return "badge-success"

    # רושמים כ-filter כדי שטמפלייטים יוכלו לקרוא לזה
    app.jinja_env.filters["confidence_badge"] = _confidence_badge_class

    @app.route("/pending-facts")
    @login_required
    def pending_facts():
        """מסך תור אישור — כל ה-facts במצב pending_approval.

        total_pending נשלח לטמפלייט כדי שכפתור "אשר הכל" יציג את המספר
        האמיתי (לא facts|length שחסום ל-200). אם total_pending > 200,
        ה-UI מציג הערה.
        """
        facts = db.get_pending_facts(business_id=BUSINESS_ID)
        total_pending = db.get_pending_facts_count(business_id=BUSINESS_ID)
        return render_template(
            "pending_facts.html",
            facts=facts,
            total_pending=total_pending,
        )

    @app.route("/api/pending-facts/rows")
    @login_required
    def api_pending_facts_rows():
        """HTMX auto-refresh: רק <tr>... ב-partial."""
        facts = db.get_pending_facts(business_id=BUSINESS_ID)
        if not facts:
            return ""
        html_parts = [
            render_template("partials/pending_fact_row.html", fact=f)
            for f in facts
        ]
        return "".join(html_parts)

    def _change_fact_status(fact_id: int, new_status: str, state_label: str):
        """משותף ל-approve/reject — atomic compare-and-swap מ-pending_approval.

        rowcount=0 פירושו אחד מהבאים: fact לא קיים, כבר אושר/נדחה (race),
        או שייך לעסק אחר (multi-tenant guard). כל המקרים מקבלים אותה
        תשובה — "לא נמצאה או כבר טופלה" — כי הם בלתי-נבדלים מבחינת UX.

        IntegrityError מטופל בנפרד: ה-UNIQUE partial index
        idx_customer_facts_active_unique מונע 2 facts active עם אותו
        (user_id, business_id, fact_type, content). אישור של pending
        שיש כבר active תאום שלו → IntegrityError. במקום 500, פלאש מובן
        + ה-pending נשאר pending (בעל העסק יכול לדחות ידנית).
        """
        try:
            rowcount = db.transition_customer_fact_status(
                fact_id, BUSINESS_ID, "pending_approval", new_status,
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "approve hit UNIQUE constraint on fact_id=%s — duplicate active fact",
                fact_id,
            )
            if request.headers.get("HX-Request"):
                # HX-Reswap=none כדי שה-tr בטבלה לא יוחלף בטקסט; השגיאה
                # מוצגת כ-toast דרך HX-Trigger (דפוס מ-live_chat_guard,
                # admin/app.py:1709). ה-tr המקורי נשאר במקומו.
                resp = app.make_response(("", 409))
                resp.headers["HX-Reswap"] = "none"
                resp.headers["HX-Trigger"] = json.dumps({
                    "showToast": {
                        "message": "לא ניתן לאשר — כבר קיימת עובדה זהה פעילה ללקוחה.",
                        "type": "warning",
                    },
                })
                return resp
            flash(
                "לא ניתן לאשר — כבר קיימת עובדה זהה פעילה ללקוחה.",
                "warning",
            )
            return redirect(url_for("pending_facts"))

        if rowcount == 0:
            if request.headers.get("HX-Request"):
                return app.make_response(("", 404))
            flash("העובדה לא נמצאה או כבר טופלה.", "warning")
            return redirect(url_for("pending_facts"))

        if request.headers.get("HX-Request"):
            # שולפים את ה-fact המעודכן (כבר לא pending) ל-render עם state
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT cf.*, u.username AS username "
                    "FROM customer_facts cf "
                    "LEFT JOIN users u ON u.user_id = cf.user_id "
                    "WHERE cf.id = ?",
                    (fact_id,),
                ).fetchone()
            if row is None:
                return ""
            return render_template(
                "partials/pending_fact_row.html",
                fact=dict(row),
                state=state_label,
            )
        flash(f"העובדה {state_label}.", "success")
        return redirect(url_for("pending_facts"))

    @app.route("/pending-facts/<int:fact_id>/approve", methods=["POST"])
    @login_required
    def approve_pending_fact(fact_id):
        _audit_log("approve_pending_fact", f"fact_id={fact_id}")
        return _change_fact_status(fact_id, "active", "approved")

    @app.route("/pending-facts/<int:fact_id>/reject", methods=["POST"])
    @login_required
    def reject_pending_fact(fact_id):
        _audit_log("reject_pending_fact", f"fact_id={fact_id}")
        return _change_fact_status(fact_id, "rejected", "rejected")

    @app.route("/pending-facts/approve-all", methods=["POST"])
    @login_required
    def approve_all_pending_facts():
        """Bulk approve — מאשרים את כל ה-pending בעסק.

        UPDATE OR IGNORE: SQLite מדלגת על שורות שמפרות את ה-UNIQUE partial
        index idx_customer_facts_active_unique (active עם אותו
        user_id+business_id+fact_type+content). הן נשארות pending — בעל
        העסק יכול לדחות אותן ידנית או לערוך את ה-content. ללא ה-OR IGNORE,
        כפילות אחת היה מפיל את כל ה-batch.
        """
        with db.get_connection() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM customer_facts "
                "WHERE business_id = ? AND status = 'pending_approval'",
                (BUSINESS_ID,),
            ).fetchone()["c"]
            conn.execute(
                "UPDATE OR IGNORE customer_facts SET status = 'active' "
                "WHERE business_id = ? AND status = 'pending_approval'",
                (BUSINESS_ID,),
            )
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM customer_facts "
                "WHERE business_id = ? AND status = 'pending_approval'",
                (BUSINESS_ID,),
            ).fetchone()["c"]
            count = before - after  # מספר שהצליחו לעבור ל-active
            skipped = after  # נשארו pending (כפילות active קיימת)
        _audit_log(
            "approve_all_pending_facts",
            f"count={count} skipped={skipped}",
        )
        if skipped:
            flash(
                f"אושרו {count} עובדות. {skipped} לא אושרו "
                "כי כבר קיימת עובדה זהה פעילה.",
                "warning",
            )
        else:
            flash(f"אושרו {count} עובדות.", "success")
        return redirect(url_for("pending_facts"))

    @app.route("/customer-memory")
    @login_required
    def customer_memory_list():
        """רשימת לקוחות שיש להם ≥1 fact active/pending."""
        users = db.get_users_with_facts(business_id=BUSINESS_ID)
        return render_template("customer_memory_list.html", users=users)

    @app.route("/customer-memory/<path:user_id>")
    @login_required
    @_validate_user_id
    def customer_memory_detail(user_id):
        """פרטי לקוחה — כל ה-facts מקובצים לפי fact_type.

        `<path:>` (לא `<string:>`) כדי לאפשר BSUID `IL.abc.123` ב-URL.
        `_validate_user_id` קורא ל-`_normalize_user_id` ומחזיר 400 על
        פורמט לא חוקי (גם BSUID נתמך אחרי תיקון admin/app.py:558).
        """
        facts = db.get_customer_facts(user_id, business_id=BUSINESS_ID, status="all")
        # קיבוץ לפי fact_type בסדר קבוע (לפי הסכמה ב-customer_facts CHECK)
        FACT_TYPE_ORDER = [
            "preference", "personal_info", "relationship",
            "open_issue", "vocabulary",
        ]
        groups = {t: [] for t in FACT_TYPE_ORDER}
        for f in facts:
            t = f.get("fact_type")
            if t in groups:
                groups[t].append(f)

        # שם תצוגה — username אם קיים, אחרת user_id מעוצב
        username = ""
        try:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT username FROM users WHERE user_id = ?", (user_id,),
                ).fetchone()
                if row:
                    username = row["username"] or ""
        except Exception:
            logger.exception("customer_memory_detail: username lookup failed")

        return render_template(
            "customer_memory_detail.html",
            user_id=user_id,
            username=username,
            groups=groups,
            fact_type_order=FACT_TYPE_ORDER,
        )

    @app.route(
        "/customer-memory/<path:user_id>/<int:fact_id>/edit", methods=["POST"]
    )
    @login_required
    @_validate_user_id
    def edit_customer_fact(user_id, fact_id):
        """עריכת content של fact. רק content משתנה — last_confirmed_at
        ושאר השדות נשמרים כמו שהם.
        """
        new_content = (request.form.get("content") or "").strip()
        if not new_content:
            if request.headers.get("HX-Request"):
                return app.make_response(("התוכן ריק", 400))
            flash("התוכן ריק.", "warning")
            return redirect(url_for("customer_memory_detail", user_id=user_id))

        # ודאות שה-fact שייך לאותו user_id ולאותו business_id (חוסם URL
        # forgery + multi-tenant leak). שני התנאים ב-SELECT עצמו — אם
        # ה-row לא חוזר, אחד משניהם נכשל.
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM customer_facts "
                "WHERE id = ? AND user_id = ? AND business_id = ?",
                (fact_id, user_id, BUSINESS_ID),
            ).fetchone()
        if row is None:
            if request.headers.get("HX-Request"):
                return app.make_response(("", 404))
            flash("העובדה לא נמצאה.", "warning")
            return redirect(url_for("customer_memory_detail", user_id=user_id))

        # IntegrityError: ה-UNIQUE partial index מונע 2 facts active עם אותו
        # (user_id, business_id, fact_type, content). אם המשתמש עורך fact
        # active כך שתואם fact active אחר → 500. תופסים ומחזירים שגיאה.
        try:
            db.update_customer_fact(fact_id, {"content": new_content})
        except sqlite3.IntegrityError:
            logger.warning(
                "edit hit UNIQUE constraint on fact_id=%s — duplicate active fact",
                fact_id,
            )
            if request.headers.get("HX-Request"):
                # HX-Reswap=none כדי שה-tr בטבלה לא יוחלף בטקסט; ראה הסבר
                # ב-_change_fact_status.
                resp = app.make_response(("", 409))
                resp.headers["HX-Reswap"] = "none"
                resp.headers["HX-Trigger"] = json.dumps({
                    "showToast": {
                        "message": "לא ניתן לשמור — כבר קיימת עובדה זהה פעילה ללקוחה.",
                        "type": "warning",
                    },
                })
                return resp
            flash(
                "לא ניתן לשמור — כבר קיימת עובדה זהה פעילה ללקוחה.",
                "warning",
            )
            return redirect(url_for("customer_memory_detail", user_id=user_id))

        _audit_log("edit_customer_fact", f"fact_id={fact_id} user_id={user_id}")

        if request.headers.get("HX-Request"):
            updated_row = dict(row)
            updated_row["content"] = new_content
            return render_template(
                "partials/customer_fact_row.html",
                fact=updated_row,
                user_id=user_id,
            )
        flash("העובדה עודכנה.", "success")
        return redirect(url_for("customer_memory_detail", user_id=user_id))

    @app.route(
        "/customer-memory/<path:user_id>/<int:fact_id>/delete", methods=["POST"]
    )
    @login_required
    @_validate_user_id
    def delete_customer_fact_route(user_id, fact_id):
        """Hard delete של fact בודד. בדיקת user_id + business_id חוסמת
        URL forgery + multi-tenant leak."""
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM customer_facts "
                "WHERE id = ? AND user_id = ? AND business_id = ?",
                (fact_id, user_id, BUSINESS_ID),
            ).fetchone()
        if row is None:
            if request.headers.get("HX-Request"):
                return app.make_response(("", 404))
            flash("העובדה לא נמצאה.", "warning")
            return redirect(url_for("customer_memory_detail", user_id=user_id))

        db.delete_customer_fact(fact_id)
        _audit_log("delete_customer_fact", f"fact_id={fact_id} user_id={user_id}")

        if request.headers.get("HX-Request"):
            # שורה ריקה — HTMX יחליף את ה-<tr> ב-content ריק → השורה נעלמת
            return ""
        flash("העובדה נמחקה.", "success")
        return redirect(url_for("customer_memory_detail", user_id=user_id))

    # ─── Bot Config (הגדרות תשתית — טוקן, סיסמה וכו') ─────────────────

    def _bot_config_status():
        """סטטוס תצורת הערוצים ל-GET של bot_config, לפי ה-tenant הנוכחי.

        ל-tenant של ברירת המחדל (legacy) — מ-env; לכל tenant אחר —
        מהסודות המוצפנים ב-control plane.
        """
        from tenancy import DEFAULT_TENANT, get_current_tenant
        import ai_chatbot.config as _cfg

        tenant = get_current_tenant()
        if tenant == DEFAULT_TENANT:
            return {
                "has_bot_token": bool(_cfg.TELEGRAM_BOT_TOKEN),
                "telegram_owner_chat_id": _cfg.TELEGRAM_OWNER_CHAT_ID,
                "has_twilio_account_sid": bool(_cfg.TWILIO_ACCOUNT_SID),
                "has_twilio_auth_token": bool(_cfg.TWILIO_AUTH_TOKEN),
                "twilio_whatsapp_number": _cfg.TWILIO_WHATSAPP_NUMBER,
            }
        import control_plane as _cp

        names = set(_cp.list_tenant_secret_names(tenant))
        return {
            "has_bot_token": "telegram_bot_token" in names,
            "telegram_owner_chat_id": _cp.get_tenant_secret(tenant, "telegram_owner_chat_id") or "",
            "has_twilio_account_sid": "twilio_account_sid" in names,
            "has_twilio_auth_token": "twilio_auth_token" in names,
            "twilio_whatsapp_number": _cp.get_tenant_secret(tenant, "twilio_whatsapp_number") or "",
        }

    def _connect_tenant_telegram(tenant):
        """מבטיח מפתח webhook + secret ל-tenant, ומרשם את ה-webhook מול
        טלגרם. מחזיר (ok: bool, note: str). אידמפוטנטי."""
        import ai_chatbot.config as _cfg
        import control_plane as _cp

        key = _cp.get_tenant_route_key(tenant, "telegram_webhook_key")
        if not key:
            key = _cp.generate_route_key()
            _cp.set_route("telegram_webhook_key", key, tenant)
        secret = _cp.get_tenant_secret(tenant, "telegram_webhook_secret")
        if not secret:
            secret = _cp.generate_route_key()
            _cp.set_tenant_secret(tenant, "telegram_webhook_secret", secret)

        base = (_cfg.ADMIN_URL or "").rstrip("/")
        if not base:
            return False, "הטוקן נשמר, אך ADMIN_URL לא מוגדר — ה-webhook לא נרשם מול טלגרם."
        try:
            import asyncio
            from bot_registry import sync_telegram_webhook, reset_tenant

            webhook_url = f"{base}/telegram/webhook/t/{key}"
            bot_username = asyncio.run(sync_telegram_webhook(tenant, webhook_url, secret))
            # שם המשתמש של הבוט (getMe) — נשמר לקישורי QR / widget footer.
            # ולידציית טיפוס: הערך מגיע מ-API חיצוני (טלגרם) — לא סומכים עליו.
            if isinstance(bot_username, str) and bot_username.strip():
                _cp.set_tenant_secret(
                    tenant, "telegram_bot_username", bot_username.strip().lstrip("@")
                )
            reset_tenant(tenant)  # האפליקציה תיבנה מחדש עם הטוקן החדש
            return True, ""
        except Exception:
            logger.error("connect telegram נכשל (tenant=%s)", tenant, exc_info=True)
            return False, "הטוקן נשמר, אך רישום ה-webhook מול טלגרם נכשל — נסו שוב."

    def _set_tenant_channel_after_connect(tenant, channel):
        """נעילת הערוץ אחרי חיבור מוצלח + מחיקת נתוני הערוץ השני.

        מדיניות מעבר ערוץ (החלטת מוצר): ברגע שהלקוח מתחבר לערוץ אחד,
        השני ננעל ונתוניו נמחקים — לא משאירים credentials רדומים. שחרור
        הנעילה נעשה רק ע"י מנהל הפלטפורמה (/platform). כשאין נתונים בצד
        השני זו no-op, ולכן בטוח לקרוא בכל שמירה מחוברת.
        כשל כאן לא מפיל את השמירה עצמה (הסודות כבר נכתבו) — רק לוג.
        """
        import control_plane as _cp

        other = "whatsapp" if channel == "telegram" else "telegram"
        try:
            if feature_flags.get_channel() != channel:
                feature_flags.set_channel(channel)
                _audit_log("bot_config", f"channel set: {channel}")
        except Exception:
            logger.error("set_channel(%s) נכשל (tenant=%s)", channel, tenant, exc_info=True)
        if other == "telegram":
            # ביטול ה-webhook מול טלגרם לפני מחיקת הטוקן (best-effort —
            # אחרי המחיקה אין דרך לבטל, כי הטוקן הוא ההרשאה)
            try:
                import asyncio
                from bot_registry import remove_telegram_webhook, reset_tenant

                if _cp.get_tenant_secret(tenant, "telegram_bot_token"):
                    asyncio.run(remove_telegram_webhook(tenant))
                    reset_tenant(tenant)
            except Exception:
                logger.error(
                    "ביטול webhook טלגרם נכשל במעבר ערוץ (tenant=%s)", tenant,
                    exc_info=True,
                )
        try:
            _cp.delete_tenant_channel_data(tenant, other)
        except Exception:
            logger.error(
                "מחיקת נתוני ערוץ %s נכשלה (tenant=%s)", other, tenant, exc_info=True,
            )

    @app.route("/bot-config", methods=["GET", "POST"])
    @login_required
    def bot_config():
        from tenancy import DEFAULT_TENANT, get_current_tenant
        _tenant = get_current_tenant()
        _is_platform_tenant = _tenant != DEFAULT_TENANT
        from dotenv import set_key as _dotenv_set_key
        from werkzeug.security import generate_password_hash as _gen_hash
        import ai_chatbot.config as _cfg

        # כותבים לדיסק הקבוע (DATA_DIR) כדי ששינויים ישרדו דיפלוי
        env_path = _cfg.DATA_DIR / ".env"

        if request.method == "POST":
            form_type = request.form.get("form_type", "")

            # יצירת קובץ .env אם לא קיים
            if not env_path.exists():
                env_path.touch()

            # נעילת ערוץ פר-tenant — defense in depth מעבר ל-disabled inputs
            # בתבנית. הערוץ נקבע אוטומטית בחיבור המוצלח הראשון (ראה בהמשך);
            # מאותו רגע הערוץ השני נעול עד שחרור ע"י מנהל הפלטפורמה
            # (/platform). ה-tenant של ברירת המחדל (legacy, env) פטור.
            locked_channel = feature_flags.get_channel() if _is_platform_tenant else ""
            if form_type in ("telegram", "whatsapp") and locked_channel and form_type != locked_channel:
                _locked_label = "Telegram" if locked_channel == "telegram" else "WhatsApp"
                _other_label = "WhatsApp" if form_type == "whatsapp" else "Telegram"
                flash(
                    f"העסק מחובר ל-{_locked_label} — הגדרות {_other_label} נעולות. "
                    "להחלפת ערוץ פנו לספק השירות (שחרור הנעילה נעשה ממסך הפלטפורמה).",
                    "danger",
                )
                return redirect(url_for("bot_config"))

            if form_type == "telegram":
                # טופס טלגרם — עדכון רק שדות טלגרם
                # טוקן: write-only — כותבים רק אם הוזן ערך חדש (לא חושפים את הקיים בתבנית)
                token = request.form.get("telegram_bot_token", "").strip()
                chat_id = request.form.get("telegram_owner_chat_id", "").strip()

                if chat_id and not re.match(r'^-?\d+$', chat_id):
                    flash("Chat ID חייב להיות מספר.", "danger")
                    return redirect(url_for("bot_config"))

                changed = []
                if _is_platform_tenant:
                    # tenant בפלטפורמה — סודות מוצפנים ב-control plane, לא env
                    import control_plane as _cp

                    if token:
                        _cp.set_tenant_secret(_tenant, "telegram_bot_token", token)
                        changed.append("telegram_bot_token")
                    _cp.set_tenant_secret(_tenant, "telegram_owner_chat_id", chat_id)
                    if changed:
                        _audit_log("bot_config", f"updated(tenant): {', '.join(changed)}")
                    if token:
                        # חיבור ראשון (או מעבר ערוץ אחרי שחרור נעילה):
                        # הערוץ ננעל ל-telegram ונתוני WhatsApp נמחקים —
                        # לא משאירים credentials של ערוץ שכבר לא בשימוש.
                        _set_tenant_channel_after_connect(_tenant, "telegram")
                        ok, note = _connect_tenant_telegram(_tenant)
                        if ok:
                            flash("הבוט חובר בהצלחה! ההודעות יגיעו לפאנל.", "success")
                        else:
                            flash(note, "warning")
                    else:
                        flash("הגדרות טלגרם נשמרו.", "success")
                    return redirect(url_for("bot_config"))

                # ── legacy (ה-tenant של ברירת המחדל) — env כמו קודם ──
                if token:
                    _dotenv_set_key(str(env_path), "TELEGRAM_BOT_TOKEN", token)
                    os.environ["TELEGRAM_BOT_TOKEN"] = token
                    _cfg.TELEGRAM_BOT_TOKEN = token
                    changed.append("TELEGRAM_BOT_TOKEN")

                # Chat ID — כותבים רק אם הערך השתנה (ערך ריק = ניקוי מכוון, לא סוד)
                if chat_id != _cfg.TELEGRAM_OWNER_CHAT_ID:
                    _dotenv_set_key(str(env_path), "TELEGRAM_OWNER_CHAT_ID", chat_id)
                    os.environ["TELEGRAM_OWNER_CHAT_ID"] = chat_id
                    _cfg.TELEGRAM_OWNER_CHAT_ID = chat_id
                    changed.append("TELEGRAM_OWNER_CHAT_ID")

                if changed:
                    _audit_log("bot_config", f"updated: {', '.join(changed)}")
                flash("הגדרות טלגרם נשמרו בהצלחה! השינויים ייכנסו לתוקף לאחר הפעלה מחדש.", "success")

            elif form_type == "whatsapp":
                # טופס WhatsApp/Twilio — Account SID + Auth Token write-only
                # (סודות, לא חושפים לתבנית). מספר WhatsApp חשוף כי הוא לא סודי.
                account_sid = request.form.get("twilio_account_sid", "").strip()
                auth_token = request.form.get("twilio_auth_token", "").strip()
                wa_number = request.form.get("twilio_whatsapp_number", "").strip()

                # ולידציה: SID חייב להיות AC + 32 hex; Auth Token — 32 hex.
                # אם הוזנו ערכים, חייבים לעבור בפורמט (אחרת ניצור config שבור).
                if account_sid and not re.match(r'^AC[a-fA-F0-9]{32}$', account_sid):
                    flash("Account SID לא תקין — אמור להתחיל ב-AC ולכלול 34 תווים.", "danger")
                    return redirect(url_for("bot_config"))
                if auth_token and not re.match(r'^[a-fA-F0-9]{32}$', auth_token):
                    flash("Auth Token לא תקין — אמור לכלול 32 תווי hex.", "danger")
                    return redirect(url_for("bot_config"))
                # מספר WhatsApp — חייב להיות E.164 (+ קידומת מדינה ומספר). ערך
                # ריק נחשב ניקוי מכוון, לכן הוולידציה רצה רק כשיש ערך.
                if wa_number and not re.match(r'^\+\d{8,15}$', wa_number):
                    flash("מספר WhatsApp חייב להיות בפורמט E.164 (למשל +14155551234).", "danger")
                    return redirect(url_for("bot_config"))

                changed = []
                if _is_platform_tenant:
                    # tenant בפלטפורמה — סודות מוצפנים + מפתח webhook ל-Twilio
                    import control_plane as _cp
                    from messaging.whatsapp_sender import reset_twilio_clients

                    if account_sid:
                        _cp.set_tenant_secret(_tenant, "twilio_account_sid", account_sid)
                        changed.append("twilio_account_sid")
                    if auth_token:
                        _cp.set_tenant_secret(_tenant, "twilio_auth_token", auth_token)
                        changed.append("twilio_auth_token")
                    _cp.set_tenant_secret(_tenant, "twilio_whatsapp_number", wa_number)
                    # מבטיחים מפתח webhook כדי שכתובת הקבלה מ-Twilio תהיה זמינה
                    if not _cp.get_tenant_route_key(_tenant, "twilio_webhook_key"):
                        _cp.set_route("twilio_webhook_key", _cp.generate_route_key(), _tenant)
                    reset_twilio_clients()  # ייבנה מחדש עם ה-credentials החדשים
                    if changed:
                        _audit_log("bot_config", f"updated(tenant): {', '.join(changed)}")
                    # חיבור WhatsApp נחשב מלא כשכל השלישייה קיימת בסודות
                    # (sid + auth token + מספר) — רק אז נועלים את הערוץ
                    # ומוחקים את נתוני הטלגרם (אם נשארו ממעבר ערוץ).
                    _names = set(_cp.list_tenant_secret_names(_tenant))
                    if {"twilio_account_sid", "twilio_auth_token",
                            "twilio_whatsapp_number"} <= _names:
                        _set_tenant_channel_after_connect(_tenant, "whatsapp")
                    flash(
                        "הגדרות WhatsApp נשמרו. את כתובת ה-Webhook להזין ב-Twilio "
                        "Console אפשר לראות במסך ניהול הפלטפורמה.",
                        "success",
                    )
                    return redirect(url_for("bot_config"))

                # ── legacy (ה-tenant של ברירת המחדל) — env כמו קודם ──
                if account_sid:
                    _dotenv_set_key(str(env_path), "TWILIO_ACCOUNT_SID", account_sid)
                    os.environ["TWILIO_ACCOUNT_SID"] = account_sid
                    _cfg.TWILIO_ACCOUNT_SID = account_sid
                    changed.append("TWILIO_ACCOUNT_SID")

                if auth_token:
                    _dotenv_set_key(str(env_path), "TWILIO_AUTH_TOKEN", auth_token)
                    os.environ["TWILIO_AUTH_TOKEN"] = auth_token
                    _cfg.TWILIO_AUTH_TOKEN = auth_token
                    changed.append("TWILIO_AUTH_TOKEN")

                # מספר — נכתב רק כשהערך השתנה (כולל ניקוי לערך ריק)
                if wa_number != _cfg.TWILIO_WHATSAPP_NUMBER:
                    _dotenv_set_key(str(env_path), "TWILIO_WHATSAPP_NUMBER", wa_number)
                    os.environ["TWILIO_WHATSAPP_NUMBER"] = wa_number
                    _cfg.TWILIO_WHATSAPP_NUMBER = wa_number
                    changed.append("TWILIO_WHATSAPP_NUMBER")

                if changed:
                    _audit_log("bot_config", f"updated: {', '.join(changed)}")
                flash("הגדרות WhatsApp נשמרו בהצלחה! השינויים ייכנסו לתוקף לאחר הפעלה מחדש.", "success")

            elif form_type == "admin":
                # טופס אדמין — עדכון שדות גישה
                username = request.form.get("admin_username", "").strip()
                new_password = request.form.get("admin_password", "")
                new_secret_key = request.form.get("admin_secret_key", "")

                # tenant בפלטפורמה: הכניסה שלו היא משתמש admin_users (אימייל),
                # לא env. הטופס משנה את סיסמת ה-**owner של ה-tenant הנוכחי**
                # — לא את המשתמש המחובר. כך גם במצב "פעל-כ" מנהל הפלטפורמה
                # מאפס את סיסמת הלקוח (ולא את שלו עצמו בטעות); כש-owner
                # מחובר בעצמו — זה ממילא אותו משתמש.
                if _is_platform_tenant:
                    if new_password:
                        import control_plane as _cp

                        owner = _cp.get_tenant_owner(_tenant)
                        if not owner:
                            flash(
                                "לא נמצא משתמש בעלים ללקוח הזה — צרו אותו "
                                "במסך הפלטפורמה.",
                                "danger",
                            )
                            return redirect(url_for("bot_config"))
                        try:
                            _cp.set_admin_password(owner["email"], new_password)
                        except ValueError as exc:
                            flash(str(exc), "danger")
                            return redirect(url_for("bot_config"))
                        _audit_log("bot_config", "owner password changed")
                        flash("הסיסמה עודכנה בהצלחה.", "success")
                    else:
                        flash("לא בוצע שינוי.", "info")
                    return redirect(url_for("bot_config"))

                if not username:
                    flash("שם משתמש אדמין לא יכול להיות ריק.", "danger")
                    return redirect(url_for("bot_config"))

                changed = []

                if username != _cfg.ADMIN_USERNAME:
                    _dotenv_set_key(str(env_path), "ADMIN_USERNAME", username)
                    os.environ["ADMIN_USERNAME"] = username
                    _cfg.ADMIN_USERNAME = username
                    changed.append("ADMIN_USERNAME")

                # מפתח סודי — כותבים רק אם הוזן ערך חדש (לא חושפים את הקיים בתבנית)
                if new_secret_key:
                    _dotenv_set_key(str(env_path), "ADMIN_SECRET_KEY", new_secret_key)
                    os.environ["ADMIN_SECRET_KEY"] = new_secret_key
                    _cfg.ADMIN_SECRET_KEY = new_secret_key
                    app.secret_key = new_secret_key
                    changed.append("ADMIN_SECRET_KEY")

                # סיסמה — שומרים כ-hash (לא plaintext) כדי לשמור על מודל האבטחה
                if new_password:
                    pw_hash = _gen_hash(new_password)
                    _dotenv_set_key(str(env_path), "ADMIN_PASSWORD_HASH", pw_hash)
                    _dotenv_set_key(str(env_path), "ADMIN_PASSWORD", "")
                    os.environ["ADMIN_PASSWORD_HASH"] = pw_hash
                    os.environ["ADMIN_PASSWORD"] = ""
                    _cfg.ADMIN_PASSWORD_HASH = pw_hash
                    _cfg.ADMIN_PASSWORD = ""
                    changed.append("ADMIN_PASSWORD_HASH")

                if changed:
                    _audit_log("bot_config", f"updated: {', '.join(changed)}")
                flash("הגדרות גישה נשמרו בהצלחה!", "success")

            else:
                flash("סוג טופס לא מזוהה.", "danger")

            return redirect(url_for("bot_config"))

        # GET — סודות (טוקן, סיסמה, secret key, Twilio credentials) לא
        # מועברים לתבנית; חושפים רק "is configured" וערכים לא-סודיים (chat id, מספר WA).
        # מטא — סטטוס תצורה לכרטיס "Instagram + Messenger" ב-bot_config.
        # מציג כמה עמודים מחוברים, מה ה-URLs להגדיר בקונסול של Meta,
        # ואיזה משתני סביבה חסרים.
        meta_pages = []
        try:
            meta_pages = db.list_meta_credentials()
        except Exception:
            logger.exception("list_meta_credentials נכשל ב-bot_config")
        meta_env_missing = [
            name for name, val in (
                ("META_APP_ID", _cfg.META_APP_ID),
                ("META_APP_SECRET", _cfg.META_APP_SECRET),
                ("META_VERIFY_TOKEN", _cfg.META_VERIFY_TOKEN),
                ("META_OAUTH_REDIRECT_URI", _cfg.META_OAUTH_REDIRECT_URI),
            ) if not val
        ]
        # ה-Webhook URL נגזר מ-ADMIN_URL (הדומיין של הפאנל). אם חסר —
        # נציג placeholder מובהק כדי שהמשתמש ידע להגדיר ADMIN_URL קודם.
        meta_webhook_url = (
            f"{_cfg.ADMIN_URL.rstrip('/')}/webhooks/meta"
            if _cfg.ADMIN_URL else ""
        )

        # סטטוס הערוצים לפי ה-tenant (env ל-default, מוצפן ל-tenant אחר)
        _status = _bot_config_status()
        # שם המשתמש במסך הגישה: ל-tenant בפלטפורמה — האימייל של ה-**owner
        # של ה-tenant** (לא של המשתמש המחובר: במצב "פעל-כ" זה היה מציג
        # בטעות את מנהל הפלטפורמה). "—" כשאין עדיין owner.
        if _is_platform_tenant:
            import control_plane as _cp

            _owner = _cp.get_tenant_owner(_tenant)
            _admin_username = (_owner or {}).get("email") or "—"
        else:
            _admin_username = _cfg.ADMIN_USERNAME
        return render_template(
            "bot_config.html",
            has_bot_token=_status["has_bot_token"],
            telegram_owner_chat_id=_status["telegram_owner_chat_id"],
            has_twilio_account_sid=_status["has_twilio_account_sid"],
            has_twilio_auth_token=_status["has_twilio_auth_token"],
            twilio_whatsapp_number=_status["twilio_whatsapp_number"],
            is_platform_tenant=_is_platform_tenant,
            admin_username=_admin_username,
            has_secret_key=bool(_cfg.ADMIN_SECRET_KEY),
            meta_pages=meta_pages,
            meta_env_missing=meta_env_missing,
            meta_webhook_url=meta_webhook_url,
            meta_verify_token=_cfg.META_VERIFY_TOKEN or "",
            meta_oauth_redirect_uri=_cfg.META_OAUTH_REDIRECT_URI or "",
        )

    # ─── Referrals (מערכת הפניות) ────────────────────────────────────────

    @app.route("/referrals", methods=["GET", "POST"])
    @login_required
    def referrals():
        if request.method == "POST":
            form_type = request.form.get("form_type", "")
            if form_type == "referral":
                # טופס הפניות — הפעלה/כיבוי + אחוז הנחה + תקופת תוקף
                referral_enabled = bool(request.form.get("referral_enabled"))
                try:
                    referral_discount = float(request.form.get("referral_discount", "10"))
                except (ValueError, TypeError):
                    referral_discount = 10.0
                try:
                    referral_validity_days = int(request.form.get("referral_validity_days", "60"))
                except (ValueError, TypeError):
                    referral_validity_days = 60

                if not (0 < referral_discount <= 100):
                    flash("אחוז הנחה חייב להיות בין 1 ל-100.", "danger")
                    return redirect(url_for("referrals"))
                if not (1 <= referral_validity_days <= 730):
                    flash("תקופת תוקף חייבת להיות בין 1 ל-730 ימים.", "danger")
                    return redirect(url_for("referrals"))

                current = db.get_bot_settings()
                db.update_bot_settings(
                    current["tone"], current.get("custom_phrases", ""),
                    referral_enabled=referral_enabled,
                    referral_discount=referral_discount,
                    referral_validity_days=referral_validity_days,
                )
                _audit_log("referrals",
                           f"referral_enabled={referral_enabled}, "
                           f"referral_discount={referral_discount}, "
                           f"referral_validity_days={referral_validity_days}")
                flash("הגדרות הפניות עודכנו בהצלחה!", "success")
            return redirect(url_for("referrals"))

        stats = db.get_referral_stats()
        top_referrers = db.get_top_referrers(limit=10)
        all_referrals = db.get_all_referrals(limit=50)

        # הוספת שמות תצוגה למפנים מובילים
        for ref in top_referrers:
            name = db.get_username_for_user(ref["referrer_id"])
            ref["display_name"] = name or ref["referrer_id"]

        ref_settings = db.get_bot_settings()
        return render_template(
            "referrals.html",
            stats=stats,
            top_referrers=top_referrers,
            all_referrals=all_referrals,
            referral_enabled=ref_settings.get("referral_enabled", 0),
            referral_discount=ref_settings.get("referral_discount", 10.0),
            referral_validity_days=ref_settings.get("referral_validity_days", 60),
        )

    # ─── QR Code ──────────────────────────────────────────────────────────

    def _qr_channel_identity() -> dict:
        """זהות הערוץ של ה-tenant הנוכחי — username / מספר WhatsApp.

        פר-tenant: ל-default מ-env (עדכון ב-/bot-config תופס בלי restart),
        לכל tenant אחר — מהסודות (telegram_bot_username נלכד ב-getMe בעת
        חיבור הטוקן). כך ה-QR של כל לקוח מצביע על הבוט/מספר *שלו*.
        """
        import control_plane as _cp
        from tenancy import get_current_tenant

        return _cp.get_tenant_channel_identity(get_current_tenant())

    def _qr_target_url(channel: str) -> tuple[str, str]:
        """החזרת (url, filename_slug) לפי הערוץ. ("", "") אם לא מוגדר."""
        identity = _qr_channel_identity()
        if channel == "whatsapp":
            from utils.phone import to_wa_me_digits
            digits = to_wa_me_digits(identity["whatsapp_number"])
            if not digits:
                return "", ""
            return f"https://wa.me/{digits}", f"whatsapp_{digits}"
        bot_username = (identity["telegram_bot_username"] or "").lstrip("@")
        if bot_username:
            return f"https://t.me/{bot_username}", bot_username
        return "", ""

    def _generate_qr_png(target_url: str, scale: int, dark_color: str, with_logo: bool) -> io.BytesIO:
        """יצירת PNG bytes של QR Code, אופציונלית עם לוגו עסקי במרכז.

        helper משותף ל-download ו-preview כדי להימנע משכפול לוגיקה
        (CLAUDE.md — "למנוע כפילות לוגיקה בשאילתות").
        """
        import segno
        qr = segno.make(target_url, error="H")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=scale, dark=dark_color, light="#FFFFFF", border=2)
        buf.seek(0)

        if with_logo:
            logo = db.get_business_logo()
            if logo:
                from utils.branding import overlay_logo_on_qr
                try:
                    composed = overlay_logo_on_qr(buf.getvalue(), logo["blob"])
                    buf = io.BytesIO(composed)
                except Exception:
                    # כשל overlay לא צריך להפיל הורדה — נחזיר את ה-QR בלי לוגו
                    logger.error("שגיאה ב-overlay לוגו על QR — מחזיר QR בלי לוגו", exc_info=True)
                    buf.seek(0)
        return buf

    @app.route("/qr-code")
    @login_required
    def qr_code():
        from utils.phone import to_wa_me_digits
        # מעבירים digits נקיים (אותה לוגיקה כמו ב-_qr_target_url) כדי שהקישור
        # שמתחת ל-QR יתאים בדיוק ל-URL שמקודד ב-QR. הצגת המספר המקורי
        # נשמרת רק להצגת המשתמש (whatsapp_number). הזהות פר-tenant.
        identity = _qr_channel_identity()
        return render_template(
            "qr_code.html",
            bot_username=(identity["telegram_bot_username"] or "").lstrip("@"),
            whatsapp_number=identity["whatsapp_number"],
            whatsapp_digits=to_wa_me_digits(identity["whatsapp_number"]),
            has_logo=db.has_business_logo(),
        )

    @app.route("/qr-code/download")
    @login_required
    def qr_code_download():
        """יצירת QR Code כקובץ PNG להורדה. ?channel=telegram|whatsapp&with_logo=1."""
        channel = request.args.get("channel", "telegram")
        if channel not in ("telegram", "whatsapp"):
            channel = "telegram"

        target_url, slug = _qr_target_url(channel)
        if not target_url:
            _ch_label = "WhatsApp" if channel == "whatsapp" else "טלגרם"
            flash(
                f"ערוץ ה-{_ch_label} עוד לא מחובר — חברו אותו במסך "
                "'הגדרות תשתית' וה-QR ייווצר אוטומטית.",
                "danger",
            )
            return redirect(url_for("qr_code"))

        # קריאת פרמטרי עיצוב מה-query string
        dark_color = request.args.get("color", "#000000")
        scale = int(request.args.get("scale", "10"))
        scale = max(1, min(scale, 50))  # הגבלה לטווח סביר
        with_logo = request.args.get("with_logo") == "1"

        buf = _generate_qr_png(target_url, scale, dark_color, with_logo)
        filename = f"qr_{slug}.png"
        return send_file(buf, mimetype="image/png", as_attachment=True, download_name=filename)

    @app.route("/qr-code/preview")
    @login_required
    def qr_code_preview():
        """יצירת תמונת QR Code לתצוגה מקדימה (inline). ?channel=telegram|whatsapp&with_logo=1."""
        channel = request.args.get("channel", "telegram")
        if channel not in ("telegram", "whatsapp"):
            channel = "telegram"

        target_url, _slug = _qr_target_url(channel)
        if not target_url:
            return "", 404

        dark_color = request.args.get("color", "#000000")
        with_logo = request.args.get("with_logo") == "1"

        # scale=15 (במקום 10) — מאפשר ללוגו במרכז לקבל יותר פיקסלים בתצוגה
        # מקדימה. עלות תעבורה זניחה (PNG של QR נשאר תחת 30KB).
        buf = _generate_qr_png(target_url, scale=15, dark_color=dark_color, with_logo=with_logo)
        return send_file(buf, mimetype="image/png")

    # ─── מיתוג עסקי (לוגו) ────────────────────────────────────────────────

    @app.route("/branding")
    @login_required
    def branding():
        """עמוד ניהול מיתוג — העלאה/החלפה/מחיקה של לוגו עסקי."""
        logo = db.get_business_logo()
        return render_template(
            "branding.html",
            has_logo=logo is not None,
            logo_uploaded_at=logo.get("uploaded_at") if logo else "",
        )

    @app.route("/branding/logo", methods=["POST"])
    @login_required
    def branding_logo_upload():
        """העלאת לוגו עסקי (PNG/JPG, מקס' 5MB)."""
        file = request.files.get("logo")
        if not file or not file.filename:
            flash("לא נבחר קובץ.", "danger")
            return redirect(url_for("branding"))

        raw = file.read()
        from utils.branding import process_uploaded_logo, LogoValidationError
        try:
            processed_blob, mime_type = process_uploaded_logo(raw)
        except LogoValidationError as e:
            flash(str(e), "danger")
            return redirect(url_for("branding"))

        try:
            db.set_business_logo(processed_blob, mime_type)
        except Exception:
            logger.error("שגיאה בשמירת לוגו עסקי ל-DB", exc_info=True)
            flash("שמירת הלוגו נכשלה. נסו שוב.", "danger")
            return redirect(url_for("branding"))

        _audit_log("branding_logo_upload", f"size={len(processed_blob)}")
        flash("הלוגו הועלה בהצלחה!", "success")
        return redirect(url_for("branding"))

    @app.route("/branding/logo")
    @login_required
    def branding_logo_serve():
        """שליפת הלוגו השמור (לתצוגה מקדימה ב-UI ולשימושים פנימיים)."""
        logo = db.get_business_logo()
        if not logo:
            return "", 404
        return send_file(
            io.BytesIO(logo["blob"]),
            mimetype=logo["mime_type"],
        )

    @app.route("/branding/logo/delete", methods=["POST"])
    @login_required
    def branding_logo_delete():
        """מחיקת הלוגו העסקי."""
        try:
            db.delete_business_logo()
        except Exception:
            logger.error("שגיאה במחיקת לוגו עסקי", exc_info=True)
            flash("מחיקת הלוגו נכשלה.", "danger")
            return redirect(url_for("branding"))

        _audit_log("branding_logo_delete", "")
        flash("הלוגו נמחק.", "success")
        return redirect(url_for("branding"))

    # ─── Follow-ups (מעקב לידים) ───────────────────────────────────────────

    FOLLOWUP_STATUS_LABELS = {
        "pending": "ממתין",
        "approved": "אושר",
        "sent": "נשלח",
        "replied": "הגיב",
        "converted": "המרה",
        "expired": "פג תוקף",
        "cancelled": "בוטל",
    }

    @app.route("/followups")
    @login_required
    def followups():
        from ai_chatbot.config import FOLLOWUP_ENABLED
        stats = db.get_followup_stats()
        all_followups = db.get_all_followups(limit=100)
        return render_template(
            "followups.html",
            stats=stats,
            followups=all_followups,
            status_labels=FOLLOWUP_STATUS_LABELS,
            followup_enabled=FOLLOWUP_ENABLED,
        )

    # ─── Broadcast (שליחת הודעות יזומות) ──────────────────────────────────

    AUDIENCE_LABELS = {
        "all": "כל הלקוחות",
        "booked": "קבעו תור",
        "recent": "פעילים לאחרונה",
        "custom": "קהל מותאם אישית",
    }

    @app.route("/broadcast")
    @login_required
    def broadcast():
        broadcasts = db.get_all_broadcasts(limit=50)
        recipient_counts = {
            "all": db.count_broadcast_recipients("all"),
            "booked": db.count_broadcast_recipients("booked"),
            "recent": db.count_broadcast_recipients("recent"),
        }
        return render_template(
            "broadcast.html",
            broadcasts=broadcasts,
            recipient_counts=recipient_counts,
            audience_labels=AUDIENCE_LABELS,
        )

    @app.route("/broadcast/count")
    @login_required
    def broadcast_count():
        """HTMX endpoint — מחזיר ספירת נמענים לקהל שנבחר."""
        audience = request.args.get("audience", "all")
        if audience not in ("all", "booked", "recent"):
            audience = "all"
        count = db.count_broadcast_recipients(audience)
        return str(count)

    @app.route("/broadcast/users")
    @login_required
    def broadcast_users():
        """HTMX endpoint — טבלת משתמשים מסוננת לברודקאסט מותאם אישית."""
        inactive_days = request.args.get("inactive_days", type=int)
        search = request.args.get("search", "").strip()
        page = request.args.get("page", 1, type=int)
        per_page = 50
        offset = (page - 1) * per_page

        users = db.get_users_filtered(
            inactive_days=inactive_days,
            search=search,
            limit=per_page,
            offset=offset,
        )
        total = db.count_users_filtered(inactive_days=inactive_days, search=search)
        total_pages = max(1, (total + per_page - 1) // per_page)

        return render_template(
            "partials/broadcast_users_table.html",
            users=users,
            total=total,
            page=page,
            total_pages=total_pages,
            inactive_days=inactive_days,
            search=search,
        )

    @app.route("/broadcast/<int:broadcast_id>/recipients")
    @login_required
    def broadcast_recipients_modal(broadcast_id: int):
        """HTMX endpoint — מחזיר תוכן מודאל עם רשימת לקוחות שנכללו בשידור.

        זמין רק לשידורים מסוג 'custom' שעבורם נשמרה רשימת נמענים בטבלת
        broadcast_message_recipients (תמיד מאז ההוספה של הפיצ'ר; שידורים
        ישנים יותר יציגו רשימה ריקה).
        """
        broadcast = db.get_broadcast(broadcast_id)
        if broadcast is None:
            return ("שידור לא נמצא", 404)

        recipients = db.get_broadcast_recipient_users(broadcast_id)
        return render_template(
            "partials/broadcast_recipients_modal.html",
            broadcast=broadcast,
            recipients=recipients,
        )

    @app.route("/broadcast/send", methods=["POST"])
    @login_required
    def broadcast_send():
        message_text = request.form.get("message_text", "").strip()
        audience = request.form.get("audience", "all")

        if audience not in ("all", "booked", "recent", "custom"):
            flash("סוג קהל לא חוקי.", "danger")
            return redirect(url_for("broadcast"))

        if not message_text:
            flash("לא ניתן לשלוח הודעה ריקה.", "danger")
            return redirect(url_for("broadcast"))

        if len(message_text) > 4096:
            flash("ההודעה ארוכה מדי (מקסימום 4,096 תווים).", "danger")
            return redirect(url_for("broadcast"))

        # שליפת נמענים — לפי סוג קהל
        if audience == "custom":
            # בדיקה: "בחר הכל" על מספר עמודים — שולפים מה-DB לפי פילטר
            select_all = request.form.get("select_all_filtered") == "1"
            if select_all:
                inactive_days = request.form.get("filter_inactive_days", type=int)
                search = request.form.get("filter_search", "").strip()
                all_users = db.get_users_filtered(
                    inactive_days=inactive_days, search=search, limit=100_000,
                )
                selected_ids = [u["user_id"] for u in all_users]
            else:
                selected_ids = request.form.getlist("selected_users")
            if not selected_ids:
                flash("לא נבחרו לקוחות לשליחה.", "warning")
                return redirect(url_for("broadcast"))
            recipients_with_channel = db.get_custom_recipients_with_channel(selected_ids)
        else:
            # סינון ערוצים — לפי בחירת המשתמש בטופס
            send_telegram = request.form.get("channel_telegram") == "1"
            send_whatsapp = request.form.get("channel_whatsapp") == "1"
            if not send_telegram and not send_whatsapp:
                flash("יש לבחור לפחות ערוץ אחד לשליחה.", "danger")
                return redirect(url_for("broadcast"))

            recipients_with_channel = db.get_broadcast_recipients_with_channel(audience)

            # סינון לפי ערוצים נבחרים
            if not (send_telegram and send_whatsapp):
                allowed_channels = set()
                if send_telegram:
                    allowed_channels.add("telegram")
                if send_whatsapp:
                    allowed_channels.add("whatsapp")
                recipients_with_channel = [
                    r for r in recipients_with_channel
                    if r.get("channel", "telegram") in allowed_channels
                ]

        if not recipients_with_channel:
            flash("אין נמענים לשידור בערוצים שנבחרו.", "warning")
            return redirect(url_for("broadcast"))

        # רשימת user_ids שטוחה — לתאימות לאחור עם create_broadcast
        recipients = [r["user_id"] for r in recipients_with_channel]

        # יצירת רשומת broadcast ב-DB. עבור קהל מותאם אישית — שומרים גם את
        # רשימת הנמענים כדי שניתן יהיה לעיין בה בהיסטוריה (לא ניתנת לגזירה
        # מחדש כי המסננים והפעילות של הלקוחות משתנים עם הזמן).
        broadcast_id = db.create_broadcast(
            message_text,
            audience,
            len(recipients),
            recipients=recipients if audience == "custom" else None,
        )

        # הפעלת שליחה ברקע
        from ai_chatbot.bot_state import get_bot, get_loop
        from ai_chatbot.broadcast_service import start_broadcast_task
        from telegram import Bot as TelegramBot

        bot = get_bot()
        loop = get_loop()

        # בדיקה אם יש נמעני Telegram — אם כן, צריך Bot
        has_telegram = any(r["channel"] == "telegram" for r in recipients_with_channel)

        # admin-only mode — יוצרים Bot חדש שיאותחל ע"י ה-worker.
        # הטוקן נפתר לפי ה-tenant הנוכחי (default → env; אחר → הסודות שלו).
        needs_init = False
        if bot is None and has_telegram:
            from bot_registry import resolve_telegram_token

            _tg_token = resolve_telegram_token()
            if _tg_token:
                bot = TelegramBot(token=_tg_token)
                needs_init = True
            else:
                # אין Bot ואין טוקן — אי אפשר לשלוח לנמעני Telegram
                db.fail_broadcast(broadcast_id, 0, len(recipients))
                flash("לא ניתן לשלוח — אין טוקן בוט מוגדר.", "danger")
                return redirect(url_for("broadcast"))

        start_broadcast_task(
            bot, broadcast_id, message_text, recipients, loop,
            needs_init=needs_init,
            recipients_with_channel=recipients_with_channel,
        )
        flash(
            f"ההודעה נכנסה לתור שליחה — {len(recipients)} נמענים. "
            "ניתן לעקוב אחר ההתקדמות בטבלה למטה.",
            "success",
        )
        return redirect(url_for("broadcast"))

    # ─── Broadcast — תבניות WhatsApp מאושרות (Meta HSM) ─────────────────────

    _WA_APPROVAL_LABELS = {
        "approved": "אושרה",
        "pending": "ממתינה לאישור",
        "rejected": "נדחתה",
        "paused": "מושהית",
        "unsubmitted": "לא הוגשה",
    }

    @app.route("/broadcast/templates")
    @login_required
    def broadcast_templates():
        """רשימת תבניות WhatsApp שסונכרנו מ-Twilio + סטטוס אישור Meta.

        מציג רק תבניות broadcast (MARKETING/UTILITY/AUTHENTICATION). תבניות
        שיחה דינמיות (Quick Replies שהקוד יוצר עם hash בשם) מוסתרות כברירת
        מחדל — הן לא מיועדות לקמפיינים. ?show_all=1 חושף הכל לדיבוג.
        """
        from ai_chatbot.database import BROADCAST_TEMPLATE_CATEGORIES

        status_filter = request.args.get("status", "").strip() or None
        language_filter = request.args.get("language", "").strip() or None
        category_filter = request.args.get("category", "").strip().upper() or None
        show_all = request.args.get("show_all", "").strip() in ("1", "true", "yes")

        # אם הוגדרה קטגוריה ספציפית — מסננים אליה. אחרת — כל הקטגוריות
        # המותרות ל-broadcast (סינון "all" עדיין מסיר את האחרות).
        if category_filter and category_filter in BROADCAST_TEMPLATE_CATEGORIES:
            effective_category = category_filter
        elif category_filter == "ALL":
            effective_category = None
            show_all = True
        else:
            effective_category = list(BROADCAST_TEMPLATE_CATEGORIES)

        templates = db.list_whatsapp_templates(
            approval_status=status_filter,
            language=language_filter,
            category=effective_category,
            exclude_internal=not show_all,
        )
        counts = db.count_whatsapp_templates_by_status(
            category=effective_category,
            exclude_internal=not show_all,
        )
        category_counts = db.count_whatsapp_templates_by_category(
            exclude_internal=not show_all,
        )
        return render_template(
            "broadcast_templates.html",
            templates=templates,
            counts=counts,
            category_counts=category_counts,
            status_filter=status_filter,
            language_filter=language_filter,
            category_filter=category_filter,
            broadcast_categories=BROADCAST_TEMPLATE_CATEGORIES,
            show_all=show_all,
            status_labels=_WA_APPROVAL_LABELS,
        )

    # ─── Broadcast — wizard קמפיינים מבוססי-תבנית ───────────────────────────

    @app.route("/broadcast/campaigns")
    @login_required
    def broadcast_campaigns_list():
        """רשימת קמפיינים. שלב 2 של ה-wizard.

        טיוטות שטרם נשמרו במפורש (`last_saved_at IS NULL`) לא מוצגות —
        אלו רשומות שנוצרו בשלב 1 של ה-wizard וסביר שהמשתמש עזב לפני
        שלחץ "שמור טיוטה". מנקים יתומות ישנות לפני השאילתה.
        """
        try:
            removed = db.cleanup_unsaved_draft_campaigns(older_than_minutes=60)
            if removed:
                logger.info("ניקוי %d טיוטות יתומות שלא נשמרו", removed)
        except Exception:
            logger.error("cleanup_unsaved_draft_campaigns: כשל בניקוי", exc_info=True)

        status_filter = request.args.get("status", "").strip() or None
        campaigns = db.list_broadcast_campaigns(status=status_filter, limit=100)
        # מצרפים את שם התבנית לכל קמפיין (UX — נוח יותר מ-SID)
        templates_by_sid = {
            t["content_sid"]: t
            for t in db.list_whatsapp_templates()
        }
        for c in campaigns:
            tpl = templates_by_sid.get(c["template_sid"])
            c["template_name"] = tpl["friendly_name"] if tpl else c["template_sid"]
            c["template_language"] = tpl["language"] if tpl else ""
            c["template_approval_status"] = tpl["approval_status"] if tpl else "unknown"

        return render_template(
            "broadcast_campaigns_list.html",
            campaigns=campaigns,
            status_filter=status_filter,
        )

    @app.route("/broadcast/campaigns/new")
    @login_required
    def broadcast_campaigns_new():
        """שלב 1 של ה-wizard — בחירת תבנית.

        GET חייב להיות idempotent — לא יוצרים draft כאן גם אם ?sid= מועבר
        (prefetch של דפדפן/חזרה-קדימה היו יוצרים כפילויות). אם ?sid=
        קיים — רק מציגים אותו כבחירה מסומנת מראש ב-dropdown; היצירה
        קורית רק בהגשת הטופס (POST).

        הפיקר מציג את כל התבניות (גם pending/rejected/unsubmitted), מחולקות
        ל-2 קבוצות: approved (ניתנות לשליחה מיד) ו-"בהמתנה" (ניתן ליצור
        draft ולהמתין לאישור). שליחת broadcast בפועל נחסמת ב-send_campaign
        אם הסטטוס אינו approved.

        הפיקר מציג רק תבניות broadcast (MARKETING/UTILITY/AUTHENTICATION)
        ומחריג תבניות שיחה דינמיות שנוצרו בקוד.
        """
        from ai_chatbot.database import BROADCAST_TEMPLATE_CATEGORIES

        all_templates = db.list_whatsapp_templates(
            category=list(BROADCAST_TEMPLATE_CATEGORIES),
            exclude_internal=True,
        )
        approved = [t for t in all_templates if t["approval_status"] == "approved"]
        pending = [t for t in all_templates if t["approval_status"] != "approved"]
        pre_selected = request.args.get("sid", "").strip()
        return render_template(
            "broadcast_campaigns_new.html",
            approved_templates=approved,
            pending_templates=pending,
            pre_selected_sid=pre_selected,
        )

    @app.route("/broadcast/campaigns/new", methods=["POST"])
    @login_required
    def broadcast_campaigns_create():
        """יצירת draft מתוך טופס שלב 1 (גם מהפיקר וגם מכפתור בדף התבניות)."""
        content_sid = request.form.get("template_sid", "").strip()
        if not content_sid:
            flash("לא נבחרה תבנית.", "danger")
            return redirect(url_for("broadcast_campaigns_new"))

        tpl = db.get_whatsapp_template(content_sid)
        if not tpl:
            flash("התבנית לא נמצאה.", "danger")
            return redirect(url_for("broadcast_campaigns_new"))

        if tpl["approval_status"] != "approved":
            flash(
                f"התבנית במצב '{tpl['approval_status']}' — ניתן לערוך draft "
                "אבל לא לשלוח עד אישור Meta.",
                "warning",
            )

        campaign_id = db.create_broadcast_campaign(
            template_sid=content_sid,
            title=tpl["friendly_name"],
        )
        return redirect(url_for("broadcast_campaigns_edit", campaign_id=campaign_id))

    @app.route("/broadcast/campaigns/<int:campaign_id>/edit")
    @login_required
    def broadcast_campaigns_edit(campaign_id: int):
        """שלב 2 — טופס מיפוי משתנים + preview חי."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "warning")
            return redirect(url_for("broadcast_campaigns_list"))

        # רק טיוטה ניתנת לעריכה. השמירה (broadcast_campaigns_save) חוסמת
        # סטטוסים אחרים בכל מקרה, אבל בלי הגנה כאן המשתמש היה יכול למלא
        # טופס שלם ולגלות רק אחרי "שמירה" שהשינויים נדחו (אובדן עבודה).
        if campaign["status"] != "draft":
            flash(
                f"לא ניתן לערוך — הקמפיין במצב '{campaign['status']}'. "
                "לעריכה — בטלו תזמון/השהיה תחילה.",
                "warning",
            )
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        template = db.get_whatsapp_template(campaign["template_sid"])
        if not template:
            flash(
                "התבנית של הקמפיין לא קיימת יותר (אולי נמחקה מ-Twilio). "
                "לא ניתן לערוך; מחקו את הקמפיין או סנכרנו שוב תבניות.",
                "danger",
            )
            return redirect(url_for("broadcast_campaigns_list"))

        # רינדור preview ראשוני לפי ה-mapping הקיים. פותרים {{user:field}}
        # מול sample user כדי שהמנהל יראה איך ההודעה תיראה ללקוח אמיתי.
        from messaging.template_renderer import render_preview, substitute_user_fields
        sample_user = _sample_user_for_preview(template, campaign)
        raw_mapping = campaign.get("variable_mapping") or {}
        resolved_mapping = {
            k: substitute_user_fields(str(v or ""), sample_user)
            for k, v in raw_mapping.items()
        }
        preview = render_preview(template, resolved_mapping)

        # ספירת קהל ראשונית — לפי audience_type שנשמר (או opted_in_only default)
        category = template.get("category") or "UTILITY"
        saved_audience_type = campaign.get("audience_type") or "opted_in_only"
        saved_filter = campaign.get("audience_filter") or {}
        inactive_days = saved_filter.get("inactive_days")
        audience_counts = db.count_wa_audience(
            category=category,
            inactive_days=inactive_days,
            require_opt_in=(saved_audience_type == "opted_in_only"),
        )

        return render_template(
            "broadcast_campaigns_edit.html",
            campaign=campaign,
            template=template,
            preview=preview,
            # הפרמטרים שהקטע partials/broadcast_campaign_audience.html מצפה להם.
            # חייבים לזהות שמות עם ה-HTMX endpoint (counts / template_category)
            # כדי שהרינדור הראשוני יציג את אותם ערכים כמו העדכון החי.
            # template_category הוא קטגוריית ה-*תבנית* (להצגת אזהרת MARKETING),
            # לא effective_category שמשמש לחישוב audience.
            counts=audience_counts,
            template_category=category,
            audience_type=saved_audience_type,
            inactive_days=inactive_days,
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/audience", methods=["POST"])
    @login_required
    def broadcast_campaigns_audience_count(campaign_id: int):
        """HTMX endpoint — מחזיר מונים עדכניים של הקהל לפי הבחירות בטופס."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            return "קמפיין לא נמצא", 404
        template = db.get_whatsapp_template(campaign["template_sid"])
        if not template:
            return "תבנית לא נמצאה", 404

        audience_type = request.form.get("audience_type", "opted_in_only").strip()
        inactive_days_raw = request.form.get("inactive_days", "").strip()
        try:
            inactive_days = int(inactive_days_raw) if inactive_days_raw else None
            if inactive_days is not None and inactive_days <= 0:
                inactive_days = None
        except ValueError:
            inactive_days = None

        # MARKETING → תמיד opted_in_only (אכיפה שרתית, לא רק UI).
        # .upper() מגן מקלט lowercase שנכנס ב-SQL עוקף (אותה סמנטיקה
        # כמו sender/scheduler).
        template_category = (template.get("category") or "UTILITY").upper()
        if template_category == "MARKETING":
            audience_type = "opted_in_only"

        # בחירת המשתמש ב-UI מכובדת כ-require_opt_in explicit גם ב-UTILITY
        counts = db.count_wa_audience(
            category=template_category,
            inactive_days=inactive_days,
            require_opt_in=(audience_type == "opted_in_only"),
        )
        return render_template(
            "partials/broadcast_campaign_audience.html",
            counts=counts,
            template_category=template_category,
            audience_type=audience_type,
            inactive_days=inactive_days,
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/preview", methods=["POST"])
    @login_required
    def broadcast_campaigns_preview(campaign_id: int):
        """HTMX endpoint — מחזיר fragment עם preview מעודכן לפי ערכים מהטופס."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            return "קמפיין לא נמצא", 404
        template = db.get_whatsapp_template(campaign["template_sid"])
        if not template:
            return "תבנית לא נמצאה", 404

        # חילוץ ערכי משתנים מהטופס — מפתחות בסגנון var_1, var_2, var_NAME
        values: dict[str, str] = {}
        for field_name, field_value in request.form.items():
            if field_name.startswith("var_"):
                key = field_name[len("var_"):]
                values[key] = field_value

        # Preview של {{user:field}} — מציגים מה שהלקוח הראשון יראה. אם אין
        # נמענים כשירים (טעם בטסט מבלי משתמשי WA), משתמשים בערכי דוגמה
        # כדי שהמנהל יראה את האפקט.
        sample_user = _sample_user_for_preview(template, campaign)
        resolved_values: dict[str, str] = {}
        from messaging.template_renderer import substitute_user_fields, render_preview
        for k, v in values.items():
            resolved_values[k] = substitute_user_fields(v, sample_user)

        preview = render_preview(template, resolved_values)
        return render_template(
            "partials/broadcast_campaign_preview.html",
            preview=preview,
            template=template,
        )

    def _sample_user_for_preview(template: dict, campaign: dict) -> dict:
        """מחזיר user-row לדוגמה שמשמש לפתרון {{user:field}} ב-preview.

        עדיפות: משתמש אמיתי מקהל היעד (מראה שם+טלפון אמיתיים). אם אין
        משתמשי WA בכלל — placeholder שמאפשר למנהל לראות את האפקט.
        """
        try:
            category = (template.get("category") or "UTILITY").upper()
            audience_type = campaign.get("audience_type") or "opted_in_only"
            audience_filter = campaign.get("audience_filter") or {}
            inactive_days = audience_filter.get("inactive_days")
            require_opt_in = audience_type == "opted_in_only"
            ids = db.list_wa_audience_eligible_user_ids(
                category=category,
                inactive_days=inactive_days,
                require_opt_in=require_opt_in,
                limit=1,
            )
            if ids:
                rows = db.get_users_for_broadcast(ids)
                if rows:
                    return rows[0]
        except Exception:
            logger.error(
                "broadcast_campaigns_preview: sample user lookup נכשל",
                exc_info=True,
            )
        return {"user_id": "+972501234567", "username": "דני הלקוח"}

    @app.route("/broadcast/campaigns/<int:campaign_id>", methods=["POST"])
    @login_required
    def broadcast_campaigns_save(campaign_id: int):
        """שמירת draft — title + variable_mapping."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "danger")
            return redirect(url_for("broadcast_campaigns_list"))
        if campaign["status"] != "draft":
            flash("ניתן לערוך רק draft.", "warning")
            return redirect(url_for("broadcast_campaigns_list"))

        title = request.form.get("title", "").strip()
        values: dict[str, str] = {}
        for field_name, field_value in request.form.items():
            if field_name.startswith("var_"):
                key = field_name[len("var_"):]
                values[key] = field_value

        # Audience fields (שלב 3 של ה-wizard)
        audience_type = request.form.get("audience_type", "opted_in_only").strip()
        if audience_type not in ("opted_in_only", "all"):
            audience_type = "opted_in_only"

        # MARKETING תמיד opted_in_only — גם אם ה-UI נעקף.
        # .upper() מגן מקלט lowercase (אותה סמנטיקה כמו sender/scheduler).
        template = db.get_whatsapp_template(campaign["template_sid"])
        if template and (template.get("category") or "").upper() == "MARKETING":
            audience_type = "opted_in_only"

        inactive_days_raw = request.form.get("inactive_days", "").strip()
        try:
            inactive_days = int(inactive_days_raw) if inactive_days_raw else None
        except ValueError:
            inactive_days = None

        audience_filter: dict = {}
        if inactive_days is not None and inactive_days > 0:
            audience_filter["inactive_days"] = inactive_days

        updated = db.update_broadcast_campaign_draft(
            campaign_id=campaign_id,
            variable_mapping=values,
            title=title,
            audience_type=audience_type,
            audience_filter=audience_filter,
        )
        if updated:
            flash("הקמפיין נשמר.", "success")
        else:
            flash("שמירה נכשלה (יתכן שהסטטוס השתנה).", "warning")
        return redirect(url_for("broadcast_campaigns_list"))

    @app.route("/broadcast/campaigns/<int:campaign_id>")
    @login_required
    def broadcast_campaigns_detail(campaign_id: int):
        """דף פירוט קמפיין — מונים, רשימת deliveries, שגיאות (לניטור שליחה)."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "warning")
            return redirect(url_for("broadcast_campaigns_list"))

        template = db.get_whatsapp_template(campaign["template_sid"])
        progress = db.get_campaign_progress(campaign_id)
        # שליחות שנכשלו: failed (Twilio דחה ביצירה) + undelivered (התקבל
        # ב-Twilio אבל לא הגיע למכשיר). שניהם תקלות שצריך debug — מציגים
        # אותם יחד כדי שהמספר בפאנל למעלה (failed+undelivered) יתאים
        # למספר השורות בטבלה הזו.
        failed_deliveries = db.get_deliveries_for_campaign(
            campaign_id, statuses=["failed", "undelivered"], limit=100,
        )
        other_deliveries = db.get_deliveries_for_campaign(campaign_id, limit=500)
        error_breakdown = db.get_error_breakdown(campaign_id)

        return render_template(
            "broadcast_campaigns_detail.html",
            campaign=campaign,
            template=template,
            progress=progress,
            error_breakdown=error_breakdown,
            failed_deliveries=failed_deliveries,
            deliveries=other_deliveries,
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/progress")
    @login_required
    def broadcast_campaigns_progress(campaign_id: int):
        """HTMX endpoint — מחזיר fragment של מוני התקדמות (polling).

        מחזירים HTTP 286 כשהקמפיין ב-terminal status (completed/failed/paused).
        זה מקוד מיוחד של HTMX שמעצור את ה-every-based polling. בלי זה,
        ה-browser היה ממשיך לשלוח בקשות כל 3 שניות גם אחרי שהקמפיין הסתיים.
        """
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            return "", 404
        progress = db.get_campaign_progress(campaign_id)
        html = render_template(
            "partials/broadcast_campaign_progress.html",
            campaign=campaign,
            progress=progress,
        )
        if campaign["status"] in ("completed", "failed", "paused"):
            # HTMX "Stop Polling" response code — מבטל את ה-every polling.
            return html, 286
        return html

    @app.route("/broadcast/campaigns/<int:campaign_id>/send", methods=["POST"])
    @login_required
    def broadcast_campaigns_send(campaign_id: int):
        """שליחת קמפיין עכשיו — מריץ בתהליכון רקע ומחזיר מיד לפירוט."""
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "danger")
            return redirect(url_for("broadcast_campaigns_list"))
        if campaign["status"] != "draft":
            flash(f"לא ניתן לשלוח — הקמפיין במצב '{campaign['status']}'.", "warning")
            return redirect(url_for("broadcast_campaigns_detail", campaign_id=campaign_id))

        template = db.get_whatsapp_template(campaign["template_sid"])
        if not template or template["approval_status"] != "approved":
            flash(
                "לא ניתן לשלוח — התבנית אינה במצב approved.",
                "danger",
            )
            return redirect(url_for("broadcast_campaigns_detail", campaign_id=campaign_id))

        try:
            from messaging.broadcast_sender import start_campaign_send
            # הנעילה (draft→sending) נעשית סינכרונית כאן, לפני ה-redirect,
            # כדי שדף הפירוט יראה status='sending' מיד ויפעיל polling.
            acquired = start_campaign_send(campaign_id)
            if acquired:
                _audit_log("campaign_send", f"campaign_id={campaign_id}")
                flash(
                    "הקמפיין נכנס לתור שליחה. המונים יתעדכנו אוטומטית בדף הפירוט.",
                    "success",
                )
            else:
                flash(
                    "לא ניתן לשלוח — הקמפיין כנראה כבר לא במצב draft.",
                    "warning",
                )
        except Exception:
            logger.error("broadcast_campaigns_send: כשל בהפעלת thread", exc_info=True)
            flash("שליחה נכשלה — ראו לוג לפרטים.", "danger")
        return redirect(url_for("broadcast_campaigns_detail", campaign_id=campaign_id))

    @app.route("/broadcast/campaigns/<int:campaign_id>/schedule", methods=["POST"])
    @login_required
    def broadcast_campaigns_schedule(campaign_id: int):
        """תזמון קמפיין לעתיד. מקבל datetime-local ב-form."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "danger")
            return redirect(url_for("broadcast_campaigns_list"))
        if campaign["status"] != "draft":
            flash(
                f"לא ניתן לתזמן — הקמפיין במצב '{campaign['status']}'.",
                "warning",
            )
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        raw = request.form.get("scheduled_at", "").strip()
        if not raw:
            flash("יש להזין תאריך ושעה.", "danger")
            return redirect(
                url_for("broadcast_campaigns_edit", campaign_id=campaign_id)
            )

        # datetime-local שולח פורמט ISO בלי timezone. מניחים שעון ישראל.
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            flash("פורמט תאריך/שעה לא תקין.", "danger")
            return redirect(
                url_for("broadcast_campaigns_edit", campaign_id=campaign_id)
            )
        il_tz = ZoneInfo("Asia/Jerusalem")
        dt_il = dt.replace(tzinfo=il_tz) if dt.tzinfo is None else dt.astimezone(il_tz)
        now_il = datetime.now(il_tz)
        if dt_il <= now_il:
            flash("הזמן המתוזמן חייב להיות בעתיד.", "warning")
            return redirect(
                url_for("broadcast_campaigns_edit", campaign_id=campaign_id)
            )

        scheduled_str = dt_il.strftime("%Y-%m-%d %H:%M:%S")
        if db.schedule_broadcast_campaign(campaign_id, scheduled_str):
            _audit_log(
                "campaign_schedule",
                f"campaign_id={campaign_id} scheduled_at={scheduled_str}",
            )
            flash(
                f"הקמפיין תוזמן ל-{scheduled_str} (שעון ישראל). "
                "ה-scheduler יבצע אוטומטית.",
                "success",
            )
        else:
            flash("תזמון נכשל — יתכן שהסטטוס השתנה.", "warning")
        return redirect(
            url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/cancel-schedule",
               methods=["POST"])
    @login_required
    def broadcast_campaigns_cancel_schedule(campaign_id: int):
        """ביטול תזמון — מחזיר קמפיין מ-scheduled ל-draft."""
        next_url = request.form.get("next", "").strip()
        success = db.cancel_scheduled_campaign(campaign_id)
        if success:
            _audit_log("campaign_cancel_schedule", f"campaign_id={campaign_id}")
            flash("התזמון בוטל. הקמפיין חזר לטיוטה.", "success")
        else:
            flash("ביטול נכשל (יתכן שלא היה מתוזמן).", "warning")

        # על כשל — לא מנתבים לעמוד עריכה (broadcast_campaigns_edit אין לו
        # status guard, והשמירה תיכשל בשקט כי הסטטוס != draft). חוזרים
        # למקום ממנו הגיעה הבקשה כדי שהמשתמש יראה את ה-flash ויחליט.
        if not success:
            if next_url == "list":
                return redirect(url_for("broadcast_campaigns_list"))
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        if next_url == "list":
            return redirect(url_for("broadcast_campaigns_list"))
        if next_url == "edit":
            return redirect(
                url_for("broadcast_campaigns_edit", campaign_id=campaign_id)
            )
        return redirect(
            url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/export.csv")
    @login_required
    def broadcast_campaigns_export_csv(campaign_id: int):
        """הורדת CSV של כל ה-deliveries בקמפיין לניתוח חיצוני/אודיט.

        BOM של UTF-8 בתחילת הקובץ כדי ש-Excel יזהה קידוד עברי נכון.
        הערכים מוגנים ע"י csv.writer (escaping אוטומטי של פסיקים/ציטוטים).
        """
        import csv
        from io import StringIO
        from flask import Response

        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "danger")
            return redirect(url_for("broadcast_campaigns_list"))

        deliveries = db.get_deliveries_for_campaign(campaign_id, limit=100000)

        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "user_id", "status", "twilio_message_sid",
            "queued_at", "sent_at", "delivered_at", "read_at", "failed_at",
            "error_code", "error_message", "rendered_variables_json",
        ])
        for d in deliveries:
            writer.writerow([
                d.get("user_id") or "",
                d.get("status") or "",
                d.get("twilio_message_sid") or "",
                d.get("queued_at") or "",
                d.get("sent_at") or "",
                d.get("delivered_at") or "",
                d.get("read_at") or "",
                d.get("failed_at") or "",
                d.get("error_code") or "",
                d.get("error_message") or "",
                d.get("rendered_variables_json") or "{}",
            ])

        filename = f"campaign_{campaign_id}_deliveries.csv"
        # BOM נדרש ל-Excel; mimetype עם charset=utf-8 ל-browsers מודרניים.
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.route("/broadcast/analytics")
    @login_required
    def broadcast_analytics():
        """תאימות לאחור: ה-Analytics של broadcast הוטמע ב-/analytics
        הראשי. מפנים לשם כדי שלא יישברו bookmarks/קישורים ישנים."""
        return redirect(url_for("analytics"))

    @app.route("/broadcast/campaigns/<int:campaign_id>/pause", methods=["POST"])
    @login_required
    def broadcast_campaigns_pause(campaign_id: int):
        """השהיית קמפיין sending. ה-thread יזהה בבדיקה המחזורית ויעצור."""
        if db.transition_campaign_status(campaign_id, "sending", "paused"):
            _audit_log("campaign_pause", f"campaign_id={campaign_id}")
            flash(
                "הקמפיין סומן להשהיה. ה-worker יעצור תוך כ-1 שניות לאחר ההודעה האחרונה.",
                "success",
            )
        else:
            flash("לא ניתן להשהות — הקמפיין אינו במצב sending.", "warning")
        return redirect(
            url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/resume", methods=["POST"])
    @login_required
    def broadcast_campaigns_resume(campaign_id: int):
        """חידוש קמפיין מ-paused — thread חדש ממשיך מהנמענים שלא נשלחו עדיין
        (create_delivery_queue מדלג על שורות קיימות, מה שגורם ל-resume טבעי).
        """
        try:
            from messaging.broadcast_sender import start_campaign_send
            acquired = start_campaign_send(campaign_id, from_status="paused")
            if acquired:
                _audit_log("campaign_resume", f"campaign_id={campaign_id}")
                flash("הקמפיין חודש. הנמענים שלא נשלחו עדיין יקבלו עכשיו.", "success")
            else:
                flash("לא ניתן לחדש — הקמפיין אינו במצב paused.", "warning")
        except Exception:
            logger.error("broadcast_campaigns_resume: כשל בהפעלה", exc_info=True)
            flash("חידוש נכשל — ראו לוג לפרטים.", "danger")
        return redirect(
            url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/retry-failed",
               methods=["POST"])
    @login_required
    def broadcast_campaigns_retry_failed(campaign_id: int):
        """ניסיון חוזר על נמענים שנכשלו/undelivered בלבד.

        סדר הפעולות חשוב כדי לא להשאיר יתמים:
          1. ולידציה + pre-count ללא mutation
          2. transition atomic של הקמפיין (completed/failed → sending)
          3. אחרי שהנעילה הצליחה — requeue_failed_deliveries
          4. spawn thread. אם ייכשל — revert את ה-status בחזרה
        הסדר הישן (requeue לפני transition) היה משאיר שורות queued
        יתמות אם ה-transition/thread נכשלו, ואז retry חוזר היה רואה
        0 failed ולא יכל להתאושש.
        """
        campaign = db.get_broadcast_campaign(campaign_id)
        if not campaign:
            flash("הקמפיין לא נמצא.", "danger")
            return redirect(url_for("broadcast_campaigns_list"))
        if campaign["status"] not in ("completed", "failed"):
            flash(
                f"retry-failed זמין רק אחרי שהקמפיין הסתיים. "
                f"מצב נוכחי: '{campaign['status']}'.",
                "warning",
            )
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        # pre-count — שואלים את ה-progress בלי mutation כדי לא להתחיל מעבר סטטוס
        # כאשר אין נמענים לנסות שוב (UX — flash הולם במקום status flip+revert).
        progress = db.get_campaign_progress(campaign_id)
        failed_count = progress["failed"] + progress["undelivered"]
        if failed_count == 0:
            flash("אין נמענים שנכשלו לניסיון חוזר.", "warning")
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        original_status = campaign["status"]

        # 1. atomic transition — אם ה-status השתנה בינתיים (race עם
        #    admin אחר או עם webhook), נצא בלי לעשות mutation.
        if not db.transition_campaign_status(
            campaign_id, original_status, "sending",
        ):
            flash(
                "הקמפיין השתנה — רענן ונסה שוב.",
                "warning",
            )
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        # 2. אחרי שהנעילה שלנו — requeue. שורות queued עכשיו שייכות למחזור
        #    החדש; אם המשך נכשל, שחזור מלא דורש להחזיר אותן ל-failed ואת
        #    ה-status למקור.
        reset_count = db.requeue_failed_deliveries(campaign_id)

        # 3. spawn thread. ה-transition כבר בוצע, לכן משתמשים בפונקציה שלא
        #    עושה transition נוסף.
        try:
            from messaging.broadcast_sender import _spawn_send_thread
            acquired = _spawn_send_thread(campaign_id)
        except Exception:
            logger.error(
                "broadcast_campaigns_retry_failed: spawn thread נכשל",
                exc_info=True,
            )
            acquired = False

        if not acquired:
            # שחזור: מחזירים את ה-status להיות מה שהיה, כך שה-UI יראה את
            # המצב האמיתי. השורות שכבר קיבלו queued — נשארות (תרחיש אפשרי
            # של יצירת thread מכושל הוא נדיר, ה-admin יכול לנסות retry
            # שוב אחרי תיקון הבעיה; requeue_failed_deliveries במחזור הבא
            # לא יעשה כלום כי אין failed — אבל נעביר ל-sending אוטומטית
            # וה-loop יטפל ב-queued הקיימים דרך create_delivery_queue →
            # should_send=True).
            db.transition_campaign_status(
                campaign_id, "sending", original_status,
            )
            flash("התחלת ניסיון חוזר נכשלה — ראו לוג.", "danger")
            return redirect(
                url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
            )

        _audit_log(
            "campaign_retry_failed",
            f"campaign_id={campaign_id} reset={reset_count}",
        )
        flash(
            f"ניסיון חוזר על {reset_count} נמענים שנכשלו.",
            "success",
        )
        return redirect(
            url_for("broadcast_campaigns_detail", campaign_id=campaign_id)
        )

    @app.route("/broadcast/campaigns/<int:campaign_id>/delete", methods=["POST"])
    @login_required
    def broadcast_campaigns_delete(campaign_id: int):
        """מחיקת draft. קמפיינים ששודרו כבר — לא נמחקים (אודיט)."""
        if db.delete_broadcast_campaign(campaign_id):
            flash("הקמפיין נמחק.", "success")
        else:
            flash("לא ניתן למחוק (יתכן ולא draft).", "warning")
        return redirect(url_for("broadcast_campaigns_list"))

    @app.route("/broadcast/templates/<content_sid>/submit")
    @login_required
    def broadcast_templates_submit_form(content_sid: str):
        """טופס שליחה של תבנית לאישור Meta. שלב 1 של רמה A."""
        tpl = db.get_whatsapp_template(content_sid)
        if not tpl:
            flash("התבנית לא נמצאה.", "danger")
            return redirect(url_for("broadcast_templates"))

        # אם כבר ב-pending/approved — אין מה לשלוח שוב
        if tpl["approval_status"] in ("pending", "approved"):
            flash(
                f"התבנית כבר ב-'{tpl['approval_status']}' — אין צורך לשלוח שוב.",
                "warning",
            )
            return redirect(url_for("broadcast_templates"))

        from messaging.whatsapp_templates_submit import sanitize_template_name
        # הערה: 3 הקטגוריות (UTILITY/MARKETING/AUTHENTICATION) + התיאורים
        # שלהן בעברית הן UI copy ומסונגרות ב-broadcast_template_submit.html.
        # ולידציה שרתית של הערך מבוצעת ב-POST handler מול VALID_CATEGORIES.
        return render_template(
            "broadcast_template_submit.html",
            template=tpl,
            suggested_name=sanitize_template_name(tpl["friendly_name"]),
        )

    @app.route("/broadcast/templates/<content_sid>/submit", methods=["POST"])
    @login_required
    def broadcast_templates_submit(content_sid: str):
        """שליחה של תבנית לאישור Meta דרך Twilio."""
        category = request.form.get("category", "").strip().upper()
        name = request.form.get("name", "").strip()

        from messaging.whatsapp_templates_submit import (
            VALID_CATEGORIES,
            submit_template_for_approval,
        )

        if category not in VALID_CATEGORIES:
            flash("קטגוריה לא חוקית.", "danger")
            return redirect(
                url_for("broadcast_templates_submit_form", content_sid=content_sid)
            )
        if not name:
            flash("חובה להזין שם לתבנית.", "danger")
            return redirect(
                url_for("broadcast_templates_submit_form", content_sid=content_sid)
            )

        try:
            result = submit_template_for_approval(
                content_sid=content_sid,
                category=category,
                name=name,
            )
        except ValueError as exc:
            flash(f"שגיאת קלט: {exc}", "danger")
            return redirect(
                url_for("broadcast_templates_submit_form", content_sid=content_sid)
            )
        except Exception:
            logger.error(
                "broadcast_templates_submit: כשל לא צפוי", exc_info=True
            )
            flash("שליחה נכשלה — ראו לוג לפרטים.", "danger")
            return redirect(
                url_for("broadcast_templates_submit_form", content_sid=content_sid)
            )

        if result["success"]:
            flash(
                f"התבנית נשלחה לאישור Meta בשם '{result['name']}' "
                f"(קטגוריה: {result['category']}). תשובה בד\"כ תוך 1-24 שעות.",
                "success",
            )
        else:
            flash(f"שליחה נכשלה: {result['error']}", "danger")

        return redirect(url_for("broadcast_templates"))

    # ─── יצירת תבנית broadcast חדשה (Phase 1: body + Quick Reply) ──────────────

    def _parse_template_form(form) -> dict:
        """מחלץ ערכי טופס לדיקט שניתן להעביר ל-TemplateSpec.

        מרוכז כדי לחלוק בין POST /new לבין HTMX preview.

        חוזה sample_values: ‎sample_values[i-1] תמיד מתייחס ל-{{i}}.
        חשוב: extract_variable_indices מחזיר *סדר הופעה* ב-body, אבל אם
        ה-body כולל "{{2}} ... {{1}}" ניבנה לפי האינדקס המספרי כדי
        ש-derive_twilio_payload (וה-preview וה-DB) יקבלו ערך נכון לכל
        placeholder.
        """
        # ערכי דוגמה מגיעים כ-sample_1, sample_2 וכו'. אוספים רק את אלה
        # שמופיעים ב-body כדי לא להחזיק "חוב" ערכים מ-placeholders שנמחקו.
        body = form.get("body", "")
        from messaging.whatsapp_templates_create import (
            extract_variable_indices,
        )
        indices = extract_variable_indices(body)
        if indices:
            # בונים מערך מאופיין באינדקס: position 0 → {{1}}, position 1 → {{2}}.
            # ממלאים עד max(indices) כדי לתמוך גם בסדר לא-עוקב מבחינת הופעה
            # (אם הוקלד {{2}} לפני {{1}}). placeholders חסרים יישארו "" וייתפסו
            # ע"י _validate_consecutive_indices.
            max_idx = max(indices)
            sample_values = [
                (form.get(f"sample_{i}", "") or "").strip()
                for i in range(1, max_idx + 1)
            ]
        else:
            sample_values = []

        # כפתורים מגיעים כרשימה button_label[] או button_label_1/2/3.
        # תומכים בשני המבנים — getlist קודם, ואז fallback למספור.
        labels = form.getlist("button_label") if hasattr(form, "getlist") else []
        if not labels:
            for i in range(1, 4):
                v = (form.get(f"button_label_{i}", "") or "").strip()
                if v:
                    labels.append(v)
        # ניקוי: מסירים ריקים אבל שומרים על סדר
        labels = [lbl for lbl in (labels or []) if lbl and lbl.strip()]

        # CTA buttons (Phase 2): cta_type_1/2, cta_label_1/2, cta_value_1/2.
        # נוצרים רק אם המשתמש כתב label או value — בחירת סוג בלבד מה-
        # dropdown לא נחשבת "כוונה ליצור CTA". זה מונע שורה ריקה שמטעינה
        # שגיאה מבלבלת של Quick Reply ⇄ CTA mutual exclusion.
        from messaging.whatsapp_templates_create import CTAButton
        cta_buttons = []
        for i in range(1, 3):
            t = (form.get(f"cta_type_{i}", "") or "").strip().upper()
            lbl = (form.get(f"cta_label_{i}", "") or "").strip()
            val = (form.get(f"cta_value_{i}", "") or "").strip()
            if lbl or val:
                cta_buttons.append(CTAButton(type=t, label=lbl, value=val))

        # Phase 3: header media (URL חיצוני). header_kind היא בחירת
        # radio: none/text/image/video/document. רק כש-image/video/document
        # נבחרים — מטפלים ב-URL. text מטופל ע"י השדה הקיים header_text.
        header_kind = (form.get("header_kind", "") or "").strip().lower()
        header_media_type = None
        header_media_url = None
        if header_kind in ("image", "video", "document"):
            header_media_type = header_kind
            header_media_url = (form.get("header_media_url", "") or "").strip() or None
        # אם נבחר "text" או "none" — לא מאכלסים media. אם נבחר text אבל
        # שדה header_text ריק — header_text כבר None וגם media None,
        # תוצאה: header_type='none' (תקין).

        # אכיפת mutual-exclusion ברמת ה-parser: אם המשתמש בחר media
        # אבל גם הקליד header_text — מנקים את ה-header_text כי הבחירה
        # הוויזואלית ב-radio גוברת. validate_spec יזרוק ממילא שגיאה אם
        # לא ננקה, אבל עדיף שלא יראה שגיאה כשהוא בחר ב-radio במפורש.
        header_text = (form.get("header_text", "") or "").strip() or None
        if header_media_type:
            header_text = None
        elif header_kind == "none":
            header_text = None

        return {
            "friendly_name": (form.get("friendly_name", "") or "").strip().lower(),
            "language": (form.get("language", "he") or "he").strip().lower(),
            "category": (form.get("category", "MARKETING") or "MARKETING").strip().upper(),
            "body": body,
            "sample_values": sample_values,
            "quick_reply_buttons": labels,
            # Phase 2:
            "header_text": header_text,
            "footer": (form.get("footer", "") or "").strip() or None,
            "cta_buttons": cta_buttons,
            # Phase 3:
            "header_media_type": header_media_type,
            "header_media_url": header_media_url,
        }

    @app.route("/broadcast/templates/new")
    @login_required
    def broadcast_templates_new_form():
        """טופס יצירת תבנית broadcast חדשה. מציג שדה body + Quick Reply
        אופציונלי. אחרי יצירה — חוזרים לרשימת התבניות עם flash."""
        return _render_new_template_form()

    @app.route("/broadcast/templates/new/preview", methods=["POST"])
    @login_required
    def broadcast_templates_new_preview():
        """HTMX preview — מקבל את ערכי הטופס ומחזיר HTML של תצוגה מקדימה
        + רשימת אזהרות/שגיאות ולידציה."""
        from messaging.whatsapp_templates_create import (
            TemplateSpec, validate_spec, extract_variable_indices,
        )
        data = _parse_template_form(request.form)
        spec = TemplateSpec(**data)
        errors = validate_spec(spec)
        # רינדור body מקומי עם ערכי הדוגמה
        from messaging.template_renderer import substitute_variables
        indices = extract_variable_indices(spec.body)
        # i >= 1 שומר מ-{{0}} שגורם ל-sample_values[-1] (האיבר האחרון)
        # במקום להחזיר ערך ריק. ה-validation היה תופס את זה אבל ה-preview
        # רץ גם אם יש שגיאות, אז ההגנה כפולה.
        var_values = {
            str(i): (spec.sample_values[i - 1]
                    if i >= 1 and i - 1 < len(spec.sample_values) else "")
            for i in indices
        }
        rendered_body = substitute_variables(spec.body, var_values)
        return render_template(
            "_broadcast_template_preview.html",
            spec=spec,
            rendered_body=rendered_body,
            errors=errors,
            indices=indices,
        )

    def _render_new_template_form(form_data=None, editing_sid=None,
                                    rollback_sid=None):
        """Helper לרינדור הטופס. כש-form_data ניתן (אחרי כשל ולידציה),
        הערכים יוחזרו לטופס כך שהמשתמש לא יצטרך למלא הכל מחדש.

        editing_sid: כש-מעבירים — הטופס פועל כעריכה. ה-POST יזהה את
        זה ויסיים בלמחוק את התבנית הישנה אחרי יצירת החדשה (Twilio
        Content API לא תומך ב-edit במקום).

        rollback_sid: כש-rollback אוטומטי הצליח — משמש להסתיר את
        ה-banner של "מצב עריכה" ולהחליף אותו ב-banner שמסביר שהתבנית
        שוחזרה (כדי לא להציג אזהרת SID change כפולה).
        """
        from messaging.whatsapp_templates_create import (
            BROADCAST_CATEGORIES, SUPPORTED_LANGUAGES,
        )
        return render_template(
            "broadcast_template_new.html",
            languages=SUPPORTED_LANGUAGES,
            categories=BROADCAST_CATEGORIES,
            default_category="MARKETING",
            default_language="he",
            form_data=form_data or {},
            editing_sid=editing_sid,
            rollback_sid=rollback_sid,
        )

    def _template_row_to_spec(tpl: dict):
        """ממיר רשומת תבנית מ-DB חזרה ל-TemplateSpec — משמש לrollback
        אחרי כשל יצירה במצב עריכה (אם delete הצליח אבל create נכשל,
        ננסה לשחזר את התבנית המקורית כדי לא לאבד אותה לצמיתות).
        """
        from messaging.whatsapp_templates_create import (
            TemplateSpec, CTAButton,
        )
        ht = tpl.get("header_type") or "none"
        sample_values: list[str] = []
        for var in tpl.get("variables") or []:
            try:
                idx = int(var.get("index", 0))
            except (ValueError, TypeError):
                continue
            if idx >= 1:
                while len(sample_values) < idx:
                    sample_values.append("")
                sample_values[idx - 1] = var.get("example", "") or ""

        qr_buttons: list[str] = []
        cta_buttons: list = []
        for btn in tpl.get("buttons") or []:
            btype = (btn.get("type") or "").lower()
            if btype == "quick_reply":
                qr_buttons.append(btn.get("title", ""))
            elif btype == "call_to_action":
                if btn.get("url"):
                    cta_buttons.append(CTAButton(
                        type="URL", label=btn.get("title", ""),
                        value=btn["url"],
                    ))
                elif btn.get("phone"):
                    cta_buttons.append(CTAButton(
                        type="PHONE", label=btn.get("title", ""),
                        value=btn["phone"],
                    ))

        # category במצב rollback חייב להיות אחד מ-BROADCAST_CATEGORIES
        # (validate_spec דוחה אחרים). אם ה-DB מחזיק UNKNOWN — נופלים
        # ל-UTILITY (בטוח יותר מ-MARKETING שמחייב opt-in לפי תיקון 40).
        # זהה ל-default ב-_template_to_form_data כדי לא לבלבל את המשתמש.
        from messaging.whatsapp_templates_create import BROADCAST_CATEGORIES
        cat = tpl.get("category")
        if cat not in BROADCAST_CATEGORIES:
            cat = "UTILITY"
        return TemplateSpec(
            friendly_name=tpl.get("friendly_name", ""),
            language=tpl.get("language", "he"),
            category=cat,
            body=tpl.get("body_text", ""),
            sample_values=sample_values,
            quick_reply_buttons=qr_buttons,
            header_text=tpl.get("header_text") if ht == "text" else None,
            footer=tpl.get("footer_text") or None,
            cta_buttons=cta_buttons,
            header_media_type=ht if ht in ("image", "video", "document") else None,
            header_media_url=tpl.get("header_media_url")
                if ht in ("image", "video", "document") else None,
        )

    def _template_to_form_data(tpl: dict) -> dict:
        """ממיר רשומת תבנית מ-DB ל-form_data dict שתואם _render_new_template_form.
        משמש בעת עריכה — מאכלס את הטופס בערכים הקיימים."""
        from messaging.whatsapp_templates_create import BROADCAST_CATEGORIES
        # אם הקטגוריה ב-DB אינה ב-BROADCAST_CATEGORIES (למשל "UNKNOWN"
        # שמגיע מ-Twilio כשהקטגוריה לא ידועה), נופלים ל-UTILITY כברירת
        # מחדל בטוחה — לא MARKETING (שיש לו דרישות opt-in מחמירות לפי
        # תיקון 40). המשתמש חייב לבחור במפורש לפני שמירה.
        cat_raw = tpl.get("category")
        cat_form = cat_raw if cat_raw in BROADCAST_CATEGORIES else "UTILITY"
        fd: dict[str, list[str]] = {
            "friendly_name": [tpl.get("friendly_name", "")],
            "language": [tpl.get("language", "he")],
            "category": [cat_form],
            "body": [tpl.get("body_text", "")],
            "footer": [tpl.get("footer_text", "")],
        }
        # header: text או media — לפי header_type שב-DB.
        ht = tpl.get("header_type") or "none"
        if ht == "text":
            fd["header_kind"] = ["text"]
            fd["header_text"] = [tpl.get("header_text", "")]
        elif ht in ("image", "video", "document"):
            fd["header_kind"] = [ht]
            fd["header_media_url"] = [tpl.get("header_media_url", "")]
        else:
            fd["header_kind"] = ["none"]

        # variables: example מועתק ל-sample_N. שומרים על מיון מספרי.
        for var in tpl.get("variables") or []:
            try:
                idx = int(var.get("index", 0))
            except (ValueError, TypeError):
                continue
            if idx >= 1:
                fd[f"sample_{idx}"] = [var.get("example", "")]

        # buttons: quick_reply ו-call_to_action הדדית בלעדיים.
        qr_count, cta_count = 0, 0
        for btn in tpl.get("buttons") or []:
            btype = (btn.get("type") or "").lower()
            if btype == "quick_reply" and qr_count < 3:
                qr_count += 1
                fd[f"button_label_{qr_count}"] = [btn.get("title", "")]
            elif btype == "call_to_action" and cta_count < 2:
                # מדלגים על שורה ללא url/phone — אחרת היינו מציגים
                # למשתמש כפתור עם type='— ללא —' שלא יעבור parse בעת
                # שמירה והנתונים יאבדו שקטה.
                if btn.get("url"):
                    cta_count += 1
                    fd[f"cta_type_{cta_count}"] = ["URL"]
                    fd[f"cta_value_{cta_count}"] = [btn["url"]]
                    fd[f"cta_label_{cta_count}"] = [btn.get("title", "")]
                elif btn.get("phone"):
                    cta_count += 1
                    fd[f"cta_type_{cta_count}"] = ["PHONE"]
                    fd[f"cta_value_{cta_count}"] = [btn["phone"]]
                    fd[f"cta_label_{cta_count}"] = [btn.get("title", "")]
                # אחרת: שורת CTA פגומה (אין url ואין phone) — נדלג.
        return fd

    @app.route("/broadcast/templates/<content_sid>/edit")
    @login_required
    def broadcast_templates_edit_form(content_sid: str):
        """טופס עריכה לתבנית unsubmitted בלבד. מציג את הערכים הקיימים
        ומאפשר לערוך אותם. עריכה ב-Twilio = delete+create (אין endpoint
        edit) — ה-POST מטפל בזה."""
        tpl = db.get_whatsapp_template(content_sid)
        if not tpl:
            flash("התבנית לא נמצאה.", "danger")
            return redirect(url_for("broadcast_templates"))
        if tpl.get("approval_status") != "unsubmitted":
            flash(
                f"לא ניתן לערוך תבנית בסטטוס "
                f"'{tpl.get('approval_status')}'. אפשר רק לערוך תבניות "
                "במצב 'לא הוגשה'.",
                "warning",
            )
            return redirect(url_for("broadcast_templates"))
        # אם הקטגוריה הקיימת אינה broadcast (למשל UNKNOWN מ-Twilio),
        # מציבים UTILITY כברירת מחדל ומזהירים את המשתמש כדי שיבחר במפורש.
        from messaging.whatsapp_templates_create import BROADCAST_CATEGORIES
        if tpl.get("category") not in BROADCAST_CATEGORIES:
            flash(
                f"הקטגוריה הקיימת לתבנית הזו ({tpl.get('category') or 'ריקה'}) "
                "אינה אחת מקטגוריות ה-broadcast התקפות. בחרו קטגוריה "
                "מפורשת (שיווק/שירות/אימות) לפני שמירה.",
                "warning",
            )
        return _render_new_template_form(
            form_data=_template_to_form_data(tpl),
            editing_sid=content_sid,
        )

    @app.route("/broadcast/templates/new", methods=["POST"])
    @login_required
    def broadcast_templates_create_post():
        """יצירת תבנית בפועל — קוראת ל-Twilio Content API ושומרת ל-DB.

        כש-וולידציה נכשלת — מחזיר HTTP 400 עם הטופס + הערכים שמולאו,
        כדי שהמשתמש לא יצטרך למלא מחדש כל הטופס. ה-flash מציג את
        הודעות השגיאה.

        editing_sid: אם נוכח, מצב עריכה — אחרי יצירת התבנית החדשה
        מוחקים את הישנה (Twilio Content API לא תומך ב-edit, רק
        delete+create).
        """
        from messaging.whatsapp_templates_create import (
            TemplateSpec, validate_spec, create_marketing_template,
        )
        # form_data שומר את ה-raw input לרינדור חוזר אם יש כשל
        form_data = request.form.to_dict(flat=False)
        editing_sid = (request.form.get("editing_sid") or "").strip() or None
        data = _parse_template_form(request.form)
        spec = TemplateSpec(**data)
        errors = validate_spec(spec)
        if errors:
            for e in errors:
                flash(e, "danger")
            return _render_new_template_form(form_data, editing_sid), 400

        # מניעת התנגשות שמות. במצב עריכה — מתעלמים מהשם של התבנית הנערכת
        # עצמה (אחרת לא ניתן יהיה לשמור את אותו שם).
        existing = db.list_whatsapp_templates()
        for t in existing:
            if t["friendly_name"] != spec.friendly_name:
                continue
            if editing_sid and t["content_sid"] == editing_sid:
                continue
            flash(
                f"כבר קיימת תבנית בשם '{spec.friendly_name}'. בחרו שם אחר.",
                "danger",
            )
            return _render_new_template_form(form_data, editing_sid), 400

        # מצב עריכה: מטפלים ב-2 תרחישים שונים.
        # - שם שונה: create-new קודם → אם הצליח, delete-old (best effort).
        # - שם זהה: חייבים delete-old קודם (Twilio דוחה כפילות שם), אבל
        #   שומרים את ה-spec המקורי כדי לעשות rollback אם create-new נכשל.
        editing_tpl = None
        editing_original_spec = None
        same_name_edit = False
        if editing_sid:
            editing_tpl = db.get_whatsapp_template(editing_sid)
            if not editing_tpl:
                flash(
                    "התבנית הנערכת לא נמצאה ב-DB. הקלט שמילאת נשמר — "
                    "ניתן לשמור כתבנית חדשה.",
                    "warning",
                )
                # נופלים למצב create כדי שהמשתמש לא ייתקע על SID לא קיים.
                return _render_new_template_form(form_data, None), 200
            if editing_tpl.get("approval_status") != "unsubmitted":
                flash(
                    "התבנית כבר אינה במצב 'לא הוגשה' (כנראה נשלחה לאישור "
                    "במקביל). הקלט שמילאת נשמר — ניתן לשמור כתבנית חדשה.",
                    "warning",
                )
                return _render_new_template_form(form_data, None), 200
            same_name_edit = (editing_tpl["friendly_name"] == spec.friendly_name)
            if same_name_edit:
                # שומרים snapshot של ה-spec המקורי לטובת rollback.
                editing_original_spec = _template_row_to_spec(editing_tpl)
                from messaging.whatsapp_templates import delete_template
                if not delete_template(editing_sid):
                    flash(
                        "מחיקת התבנית הישנה ב-Twilio נכשלה. נסו שוב או "
                        "מחקו ידנית מ-Twilio Console.",
                        "danger",
                    )
                    return _render_new_template_form(form_data, editing_sid), 502
                # מנקים גם ב-DB כדי שלא נראה כפילות אחרי create. עוטפים
                # ב-try/except כדי שכשל ב-DB (busy/IO) לא יחסום את
                # יצירת ההחלפה — Twilio כבר נמחקה, צריך להמשיך. אם
                # ה-DB delete נכשל — לא מאפסים את editing_tpl, כך
                # ש-cleanup הסופי (אחרי create) ינסה שוב למחוק (delete
                # מ-Twilio הוא idempotent עם 404 — בטוח לקרוא שוב).
                db_delete_ok = False
                try:
                    with db.get_connection() as conn:
                        conn.execute(
                            "DELETE FROM whatsapp_templates WHERE content_sid = ?",
                            (editing_sid,),
                        )
                    db_delete_ok = True
                except Exception:
                    logger.error(
                        "broadcast_templates_create_post: כשל בניקוי DB "
                        "אחרי delete מ-Twilio (sid=%s) — ה-cleanup הסופי "
                        "ינסה שוב כדי למנוע שורות כפולות עם friendly_name זהה",
                        editing_sid, exc_info=True,
                    )
                if db_delete_ok:
                    editing_tpl = None  # נוקה לחלוטין — אין צורך לנסות שוב

        def _rollback_on_create_failure(reason: str, status_code: int):
            """אם delete-old הצליח אבל create-new נכשל — מנסים לשחזר
            את התבנית המקורית (best effort). הסיכון: שני כשלים ברצף
            יאבדו את התבנית — ולכן שולחים flash דחוף עם הנתונים
            כדי שהמשתמש יוכל לשחזר ידנית.
            """
            if not editing_original_spec:
                flash(reason, "danger")
                return _render_new_template_form(form_data, editing_sid), status_code
            try:
                restored = create_marketing_template(editing_original_spec)
                logger.warning(
                    "broadcast_templates_create_post: rollback הצליח — "
                    "התבנית המקורית שוחזרה ב-SID חדש %s (היה %s)",
                    restored["content_sid"], editing_sid,
                )
                flash(
                    f"{reason} התבנית המקורית שוחזרה אוטומטית "
                    f"(SID חדש: {restored['content_sid']}).",
                    "warning",
                )
                # מעדכנים את ה-editing_sid ב-form ל-SID החדש כדי שהמשתמש
                # יוכל לתקן את הקלט ולשלוח שוב — אחרת הקריאה הבאה תחפש
                # SID שכבר נמחק ותקבל 404.
                # rollback_sid גם הוא מועבר כדי שה-banner של "מצב עריכה"
                # יוחלף ב-banner של rollback ולא יתבלבל את המשתמש.
                return _render_new_template_form(
                    form_data,
                    editing_sid=restored["content_sid"],
                    rollback_sid=restored["content_sid"],
                ), status_code
            except Exception:
                logger.error(
                    "broadcast_templates_create_post: rollback נכשל גם הוא! "
                    "התבנית הישנה נמחקה מ-Twilio ולא ניתן היה לשחזר. "
                    "spec=%r", editing_original_spec, exc_info=True,
                )
                flash(
                    f"{reason} ניסיון שחזור התבנית הישנה גם נכשל — "
                    "התבנית אבדה. הקלט שמילאת נשמר בטופס; אנא בנה אותה "
                    "מחדש ב-Twilio Console או שלח שוב מכאן.",
                    "danger",
                )
                # ה-editing_sid הישן כבר לא קיים — נחזיר None כדי שהטופס
                # יעבוד במצב יצירה רגיל (POST יחפש שם פנוי).
                return _render_new_template_form(form_data, None), 500

        try:
            tpl = create_marketing_template(spec)
        except ValueError as exc:
            return _rollback_on_create_failure(f"שגיאת ולידציה: {exc}", 400)
        except RuntimeError as exc:
            return _rollback_on_create_failure(f"יצירה נכשלה: {exc}", 502)
        except Exception:
            logger.error(
                "broadcast_templates_create_post: כשל לא צפוי", exc_info=True
            )
            return _rollback_on_create_failure(
                "יצירה נכשלה — ראו לוג לפרטים.", 500,
            )

        # אחרי יצירה מוצלחת במצב עריכה (כשהשם השתנה) — מוחקים את הישנה
        # מ-Twilio + מ-DB. הכשל כאן לא קריטי כי החדשה כבר נשמרה בהצלחה,
        # אבל בודקים את ערך ההחזרה כדי שלא נמחק את ה-DB record בלי לוודא
        # שגם Twilio נמחקה (כי אז סנכרון הבא היה מחזיר אותה כפליל).
        if editing_tpl:
            from messaging.whatsapp_templates import delete_template
            twilio_deleted = False
            try:
                twilio_deleted = bool(delete_template(editing_sid))
            except Exception:
                logger.error(
                    "broadcast_templates_create_post: שגיאה במחיקת ה-template "
                    "הישנה (sid=%s) אחרי עריכה — צריך ניקוי ידני",
                    editing_sid, exc_info=True,
                )
            if twilio_deleted:
                try:
                    with db.get_connection() as conn:
                        conn.execute(
                            "DELETE FROM whatsapp_templates WHERE content_sid = ?",
                            (editing_sid,),
                        )
                except Exception:
                    logger.error(
                        "broadcast_templates_create_post: כשל בניקוי DB "
                        "אחרי delete מ-Twilio של edit-old (sid=%s)",
                        editing_sid, exc_info=True,
                    )
            else:
                # שומרים את ה-DB record כדי שהמשתמש יוכל ללחוץ "מחק ידני"
                # מהפאנל; אחרת היינו מאבדים את הזיהוי של מה שצריך לנקות.
                flash(
                    f"התבנית החדשה נוצרה, אבל מחיקת הישנה ב-Twilio נכשלה "
                    f"(SID ישן: {editing_sid}). יש למחוק אותה ידנית מ-Twilio "
                    "Console או שהסנכרון הבא יחזיר אותה כפליל.",
                    "warning",
                )

        flash(
            f"התבנית '{tpl['friendly_name']}' נוצרה בהצלחה. כעת לחצו "
            "'שלח לאישור' כדי להעביר ל-Meta.",
            "success",
        )
        return redirect(
            url_for("broadcast_templates_submit_form", content_sid=tpl["content_sid"])
        )

    @app.route("/broadcast/templates/<content_sid>/delete", methods=["POST"])
    @login_required
    def broadcast_templates_delete(content_sid: str):
        """מחיקת תבנית מ-Twilio + DB. זמינה בכל הסטטוסים — בעל העסק
        עשוי לרצות לנקות תבנית שאינה אקטואלית גם אם Meta אישרה.

        הגנות:
        - approved: בדיקת קמפיינים פעילים (scheduled/sending/paused)
          שמסתמכים על התבנית — אם יש, חסימה כדי לא לשבור אותם.
        - אם הבדיקה עצמה נכשלת = fail-closed (חסימה).
        - שאר הסטטוסים: ה-confirm dialog ב-UI כבר הזהיר את המשתמש.
        """
        tpl = db.get_whatsapp_template(content_sid)
        if not tpl:
            flash("התבנית לא נמצאה.", "danger")
            return redirect(url_for("broadcast_templates"))

        status = (tpl.get("approval_status") or "").lower()
        # approved ו-paused שניהם עברו אישור Meta בעבר ועשויים להיות
        # להם קמפיינים פעילים שנוצרו כשהיו approved. paused = Meta
        # השהתה זמנית (איכות נמוכה) — קמפיינים שכבר רצים לא בהכרח
        # נעצרו אוטומטית. בודקים את שניהם.
        if status in ("approved", "paused"):
            try:
                active_count = db.count_active_campaigns_for_template(content_sid)
            except Exception:
                logger.error(
                    "broadcast_templates_delete: כשל בבדיקת קמפיינים פעילים",
                    exc_info=True,
                )
                flash(
                    "לא ניתן לוודא שאין קמפיינים פעילים שמסתמכים על "
                    "התבנית (שאילתת DB נכשלה). מחיקה נחסמה כדי לא "
                    "לשבור קמפיינים בטעות. נסו שוב בעוד דקה.",
                    "danger",
                )
                return redirect(url_for("broadcast_templates"))

            if active_count > 0:
                flash(
                    f"לא ניתן למחוק — קיימים {active_count} קמפיינים "
                    "פעילים שמשתמשים בתבנית הזו (scheduled/sending/paused). "
                    "השהו/בטלו אותם תחילה.",
                    "danger",
                )
                return redirect(url_for("broadcast_templates"))

        # מחיקה — Twilio ראשון, אם הצליח אז גם DB. אם Twilio נכשל
        # (חוץ מ-404 שמטופל idempotent), עוצרים בלי לגעת ב-DB.
        from messaging.whatsapp_templates import delete_template
        if not delete_template(content_sid):
            flash(
                "מחיקת התבנית ב-Twilio נכשלה. נסו שוב בעוד דקה או "
                "מחקו ידנית מ-Twilio Console.",
                "danger",
            )
            return redirect(url_for("broadcast_templates"))

        try:
            with db.get_connection() as conn:
                conn.execute(
                    "DELETE FROM whatsapp_templates WHERE content_sid = ?",
                    (content_sid,),
                )
        except Exception:
            logger.error(
                "broadcast_templates_delete: Twilio delete הצליח אבל "
                "DB delete נכשל (sid=%s) — הסנכרון הבא יאחד",
                content_sid, exc_info=True,
            )
            flash(
                "התבנית נמחקה מ-Twilio אבל מחיקה מ-DB נכשלה. הסנכרון "
                "הבא יסיר אותה אוטומטית.",
                "warning",
            )
            return redirect(url_for("broadcast_templates"))

        flash(
            f"התבנית '{tpl.get('friendly_name')}' נמחקה בהצלחה.",
            "success",
        )
        return redirect(url_for("broadcast_templates"))

    @app.route("/broadcast/templates/sync", methods=["POST"])
    @login_required
    def broadcast_templates_sync():
        """טריגר ידני לסנכרון תבניות מ-Twilio. מציג סטטיסטיקות ב-flash."""
        try:
            from messaging.whatsapp_templates_sync import sync_templates_from_twilio
            stats = sync_templates_from_twilio()
            # כשל ב-pagination אומר שלא משכנו את כל הדפים; הימנענו מ-prune
            # ומדווחים למשתמש שהסנכרון חלקי.
            if not stats.get("pagination_complete", True):
                flash(
                    "סנכרון חלקי — חלק מדפי Twilio לא נמשכו. "
                    "נמשכו {fetched}, עודכנו {upserted}, מחיקה דולגה. "
                    "נסו שוב.".format(**stats),
                    "warning",
                )
            elif stats["errors"] == 0:
                flash(
                    "סנכרון הושלם — נמשכו {fetched}, עודכנו {upserted}, "
                    "נמחקו {deleted}.".format(**stats),
                    "success",
                )
            else:
                flash(
                    "סנכרון הושלם עם שגיאות — נמשכו {fetched}, עודכנו {upserted}, "
                    "נמחקו {deleted}, שגיאות {errors}.".format(**stats),
                    "warning",
                )
        except Exception:
            logger.error("broadcast_templates_sync: כשל בסנכרון", exc_info=True)
            flash("סנכרון נכשל — ראו לוג לפרטים.", "danger")
        return redirect(url_for("broadcast_templates"))

    # ─── לקוחות (CRM-lite) ────────────────────────────────────────────────
    # תצוגה מאחדת של מה שיש על כל לקוח: היסטוריה, תגיות אוטומטיות, הערה
    # ידנית. בלי טבלה חדשה — הכל JOIN של users + appointments + user_notes.

    @app.route("/customers")
    @login_required
    def customers():
        """רשימת לקוחות עם חיפוש."""
        search = (request.args.get("q") or "").strip()
        # page query param — fallback ל-1 גם אם הערך לא מספרי (?page=abc).
        try:
            page = max(1, int(request.args.get("page") or 1))
        except (ValueError, TypeError):
            page = 1
        per_page = 50
        offset = (page - 1) * per_page

        customers_list = db.list_customers(
            search=search, limit=per_page, offset=offset,
        )
        total = db.count_customers(search=search)
        total_pages = max(1, (total + per_page - 1) // per_page)

        return render_template(
            "customers.html",
            customers=customers_list,
            search=search,
            page=page,
            total_pages=total_pages,
            total=total,
        )

    @app.route("/customers/export.csv")
    @login_required
    def customers_export_csv():
        """הורדת רשימת לקוחות כ-CSV.

        מכבד את חיפוש (?q=...) מהעמוד הראשי — מייצא בדיוק את מה שמוצג.
        BOM ב-UTF-8 כדי ש-Excel יציג עברית נכון.
        מגבלה 10,000 שורות; אם יש יותר, נחשוב על streaming/pagination.
        """
        import csv
        from io import StringIO
        from datetime import datetime as _dt
        from flask import Response

        search = (request.args.get("q") or "").strip()
        EXPORT_LIMIT = 10_000
        rows = db.list_customers(search=search, limit=EXPORT_LIMIT, offset=0)

        # עוטף ערכים שExcel ממיר לנוטציה מדעית (user_id ארוך, טלפון).
        # הקידומת ="..." היא תחביר של Excel שמכריח לקרוא כטקסט. הקידומת
        # "מתחבאת" בעת תצוגה — המשתמש רואה רק את התוכן.
        def _excel_text(value: str) -> str:
            if not value:
                return ""
            # escape של מרכאות בתוך הערך — לפי תקן CSV ("" מייצג ")
            return f'="{str(value).replace(chr(34), chr(34) * 2)}"'

        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "מזהה", "שם", "ערוץ", "טלפון",
            "תורים שאושרו", "תור אחרון",
            "תגיות אוטומטיות", "תגיות ידניות",
            "יש הערה", "פעיל לאחרונה",
        ])
        for r in rows:
            phone_raw = r.get("phone_number") or ""
            phone_il = _format_il_phone(phone_raw) if phone_raw else ""
            writer.writerow([
                _excel_text(r.get("user_id") or ""),
                r.get("username") or "",
                r.get("channel") or "",
                _excel_text(phone_il),
                r.get("appt_count") or 0,
                r.get("last_appointment_date") or "",
                ", ".join(r.get("auto_tags", []) or []),
                ", ".join(r.get("manual_tags", []) or []),
                "כן" if r.get("has_note") else "",
                r.get("last_active_at") or "",
            ])

        # שם קובץ עם חותמת זמן + suffix אם החיפוש פעיל. סינון תווים מסוכנים
        # (CR/LF, מרכאות, סלאש) מה-search כדי שלא ישברו את ה-Content-Disposition
        # header או ייצרו header injection.
        ts = _dt.now().strftime("%Y-%m-%d_%H%M")
        safe_search = "".join(
            c for c in search[:20] if c.isalnum() or c in ("-", "_")
        )
        suffix = f"_search-{safe_search}" if safe_search else ""
        filename = f"customers_{ts}{suffix}.csv"

        _audit_log("customers", f"exported CSV ({len(rows)} rows, search={search!r})")
        # BOM נדרש ל-Excel
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.route("/customers/<path:user_id>")
    @login_required
    def customer_card(user_id):
        """כרטיס לקוח בודד — היסטוריה, שירותים, הערה, תגיות."""
        normalized = _normalize_user_id(user_id)
        card = db.get_customer_card(normalized)
        if not card:
            flash("לקוח לא נמצא.", "warning")
            return redirect(url_for("customers"))
        return render_template(
            "customer_card.html",
            card=card,
        )

    @app.route("/customers/<path:user_id>/note", methods=["POST"])
    @login_required
    def customer_card_save_note(user_id):
        """שמירת/עדכון הערה ידנית + תגיות ידניות ללקוח."""
        normalized = _normalize_user_id(user_id)
        # existence check קל — לא להפעיל get_customer_card שעולה 5+ שאילתות
        if not db.user_exists(normalized):
            flash("לקוח לא נמצא.", "warning")
            return redirect(url_for("customers"))

        note = (request.form.get("note") or "").strip()
        # תגיות מגיעות כמחרוזת מופרדת בפסיקים — מפצלים, מנקים, dedup
        tags_raw = (request.form.get("tags") or "").strip()
        tags: list[str] = []
        if tags_raw:
            seen = set()
            for t in tags_raw.split(","):
                t = t.strip()
                if t and t not in seen:
                    seen.add(t)
                    tags.append(t)

        # שימור withhold_reason קיים — הטופס לא חושף את השדה (קריאה בלבד),
        # אבל save_user_note דורס ל-"" אם לא מועבר. בלי זה, כל שמירה דרך
        # הטופס הזה הייתה מוחקת את ה-withhold_reason של תיקון 13.
        existing = db.get_user_note_full(normalized)

        db.save_user_note(
            normalized, note,
            tags=tags,
            withhold_reason=existing.get("withhold_reason", ""),
        )
        _audit_log("customers", f"saved note for {normalized}: {len(tags)} tags")
        flash("ההערה נשמרה.", "success")
        return redirect(url_for("customer_card", user_id=normalized))

    # ─── ניהול קהל (חסימת משתמשים) ─────────────────────────────────────────

    @app.route("/audience")
    @login_required
    def audience():
        """עמוד ניהול קהל — רשימת חסומים, חיפוש וחסימה/שחרור."""
        blocked = db.get_blocked_users()
        return render_template(
            "audience.html",
            blocked_users=blocked,
        )

    @app.route("/audience/search")
    @login_required
    def audience_search():
        """HTMX endpoint — חיפוש משתמשים לפי שם או מזהה."""
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return ""
        users = db.get_unique_users()
        blocked_ids = {u["user_id"] for u in db.get_blocked_users()}
        # סינון לפי שם משתמש או מזהה
        results = [
            u for u in users
            if q.lower() in (u.get("username") or "").lower()
            or q in u.get("user_id", "")
        ]
        # הגבלת תוצאות
        results = results[:20]
        if not results:
            return '<div class="text-muted" style="padding:0.5rem;">לא נמצאו תוצאות</div>'
        # קריאה ל-helper משותף לכל שורה — מבטיח זהות מוחלטת בין הרינדור
        # הראשוני לבין ה-outerHTML swap לאחר opt-in/out. blocked_ids נשלף
        # פעם אחת ונמסר בכל איטרציה כדי לחסוך קריאת DB.
        return "\n".join(
            _render_search_result_item(
                u["user_id"],
                u.get("username") or u["user_id"],
                blocked_ids=blocked_ids,
            )
            for u in results
        )

    @app.route("/audience/wa-optin", methods=["POST"])
    @login_required
    def audience_wa_optin():
        """סימון ידני של משתמש כ-opted-in לשיווק WA. מחזיר את כרטיס התוצאה המעודכן."""
        user_id = request.form.get("user_id", "").strip()
        username = request.form.get("username", "").strip()
        if not user_id:
            return "", 400
        try:
            db.set_wa_marketing_opt_in(user_id, source="admin_manual")
        except Exception:
            logger.error("audience_wa_optin: נכשל לעדכן %s", user_id, exc_info=True)
            return "", 500
        _audit_log("wa_optin", f"user_id={user_id} username={username} (manual)")
        # מחזירים את התוצאה כ-HTML יחיד — HTMX מחליף את .search-result-item
        return _render_search_result_item(user_id, username)

    @app.route("/audience/wa-optout", methods=["POST"])
    @login_required
    def audience_wa_optout():
        """סימון ידני של משתמש כ-opted-out מהשיווק WA."""
        user_id = request.form.get("user_id", "").strip()
        username = request.form.get("username", "").strip()
        if not user_id:
            return "", 400
        try:
            db.set_wa_opted_out(user_id)
        except Exception:
            logger.error("audience_wa_optout: נכשל לעדכן %s", user_id, exc_info=True)
            return "", 500
        _audit_log("wa_optout", f"user_id={user_id} username={username} (manual)")
        return _render_search_result_item(user_id, username)

    def _render_search_result_item(
        user_id: str,
        username: str,
        blocked_ids: Optional[set] = None,
    ) -> str:
        """מחזיר HTML של שורת תוצאה — נקרא גם מ-audience_search (לולאה) וגם
        אחרי opt-in/out (outerHTML swap). Single source of truth ל-markup.

        Args:
            blocked_ids: אופציונלי ל-batch use; אם None נשלף ידנית (caller בודד).
        """
        if blocked_ids is None:
            blocked_ids = {u["user_id"] for u in db.get_blocked_users()}
        is_blocked = user_id in blocked_ids
        opt_status = db.get_wa_opt_status(user_id)
        name = username or user_id

        badges = []
        if is_blocked:
            badges.append('<span class="badge badge-danger">חסום</span>')
        if opt_status["opted_out_at"]:
            badges.append('<span class="badge badge-warning" title="סומן opt-out">נוט אאוט</span>')
        elif opt_status["opted_in"]:
            badges.append('<span class="badge badge-success" title="opted-in לשיווק">opt-in</span>')
        badges_html = (" " + " ".join(badges)) if badges else ""

        hx_vals = _html.escape(json.dumps({"user_id": user_id, "username": name}))
        action = "unblock" if is_blocked else "block"
        confirm_name = _html.escape(name)
        confirm_msg = (
            f"לשחרר את {confirm_name} מחסימה?" if is_blocked
            else f"לחסום את {confirm_name}?"
        )

        wa_action = "wa-optout" if opt_status["eligible_for_marketing"] else "wa-optin"
        wa_label = "סמן opt-out" if opt_status["eligible_for_marketing"] else "סמן opt-in"
        wa_btn_class = "btn-warning" if opt_status["eligible_for_marketing"] else "btn-success"
        wa_confirm = (
            f"לסמן את {confirm_name} כ-opted-out מקמפיינים שיווקיים?"
            if opt_status["eligible_for_marketing"]
            else f"לסמן את {confirm_name} כ-opted-in לקמפיינים שיווקיים?"
        )

        return (
            f'<div class="search-result-item" style="display:flex;justify-content:space-between;'
            f'align-items:center;gap:0.5rem;padding:0.5rem;border-bottom:1px solid var(--border-color,#333);">'
            f'<span>{_html.escape(name)} <small class="text-muted">({_html.escape(user_id)})</small>{badges_html}</span>'
            f'<span style="display:flex;gap:0.25rem;">'
            f'<button class="btn btn-sm {wa_btn_class}" '
            f'hx-post="/audience/{wa_action}" '
            f"hx-vals='{hx_vals}' "
            f'hx-target="closest .search-result-item" hx-swap="outerHTML" '
            f'hx-confirm="{wa_confirm}">{wa_label}</button>'
            f'<button class="btn btn-sm {"btn-secondary" if is_blocked else "btn-danger"}" '
            f'hx-post="/audience/{action}" '
            f"hx-vals='{hx_vals}' "
            f'hx-target="#blocked-list" hx-swap="outerHTML" '
            f'hx-confirm="{confirm_msg}">'
            f'{"שחרור" if is_blocked else "חסום"}</button>'
            f'</span>'
            f'</div>'
        )

    @app.route("/audience/block", methods=["POST"])
    @login_required
    def audience_block():
        """חסימת משתמש — מחזיר את רשימת החסומים המעודכנת (HTMX partial)."""
        user_id = request.form.get("user_id", "").strip()
        username = request.form.get("username", "").strip()
        reason = request.form.get("reason", "").strip()
        if not user_id:
            return "", 400
        db.block_user(user_id, username, reason)
        _audit_log("block_user", f"user_id={user_id} username={username} reason={reason}")
        blocked = db.get_blocked_users()
        return render_template("partials/blocked_list.html", blocked_users=blocked)

    @app.route("/audience/unblock", methods=["POST"])
    @login_required
    def audience_unblock():
        """שחרור חסימת משתמש — מחזיר את רשימת החסומים המעודכנת (HTMX partial)."""
        user_id = request.form.get("user_id", "").strip()
        if not user_id:
            return "", 400
        db.unblock_user(user_id)
        _audit_log("unblock_user", f"user_id={user_id}")
        blocked = db.get_blocked_users()
        return render_template("partials/blocked_list.html", blocked_users=blocked)

    # ─── Analytics ──────────────────────────────────────────────────────────

    @app.route("/analytics")
    @login_required
    def analytics():
        # תקופת סינון — ברירת מחדל 30 יום
        days = request.args.get("days", 30, type=int)
        if days not in (7, 30, 90):
            days = 30

        summary = db.get_analytics_summary(days)
        daily = db.get_daily_message_counts(days)
        hourly = db.get_hourly_distribution(days)
        engagement = db.get_user_engagement_stats(days)
        top_unanswered = db.get_top_unanswered_questions(days)
        drop_offs = db.get_conversations_with_drop_off(days)
        popular_sources = db.get_popular_kb_sources(days)

        # Broadcast analytics — אינטגרטיביים לעמוד הראשי במקום עמוד נפרד.
        bcast_summary = db.get_broadcast_analytics_summary()
        bcast_totals = bcast_summary.get("totals") or {}
        bcast_total_sent = bcast_totals.get("total_sent", 0) or 0
        bcast_total_delivered = bcast_totals.get("total_delivered", 0) or 0
        bcast_total_read = bcast_totals.get("total_read", 0) or 0
        bcast_rates = {
            "delivery_rate": (
                100 * bcast_total_delivered / bcast_total_sent
                if bcast_total_sent > 0 else 0
            ),
            "read_rate": (
                100 * bcast_total_read / bcast_total_delivered
                if bcast_total_delivered > 0 else 0
            ),
        }

        return render_template(
            "analytics.html",
            days=days,
            summary=summary,
            daily=daily,
            hourly=hourly,
            engagement=engagement,
            top_unanswered=top_unanswered,
            drop_offs=drop_offs,
            popular_sources=popular_sources,
            bcast_summary=bcast_summary,
            bcast_totals=bcast_totals,
            bcast_rates=bcast_rates,
        )

    # ─── דיווח באג למפתח ────────────────────────────────────────────────

    @app.route("/developer-report", methods=["GET", "POST"])
    @login_required
    def developer_report():
        """עמוד דיווח באגים למפתח — טופס + היסטוריה."""
        from ai_chatbot.developer_report_service import (
            is_configured,
            send_report_to_developer,
            allowed_file,
            MAX_SCREENSHOT_SIZE,
            MAX_SCREENSHOTS,
        )

        if request.method == "POST":
            description_raw = request.form.get("description", "").strip()
            if not description_raw:
                flash("יש למלא תיאור הבעיה.", "danger")
                return redirect(url_for("developer_report"))

            # שכבה 3 (תיקון 13) — סניטיזציה צד-שרת לפני שמירה ולפני שליחה.
            # שני הכיוונים (DB + מייל/טלגרם למפתח) מקבלים את הטקסט הסניטיזי
            # בלבד; המקור הלא-נקי לא נשמר אף-פעם.
            from utils.pii_sanitizer import sanitize_pii
            sanitation = sanitize_pii(description_raw)
            description = sanitation.text
            if sanitation.changed:
                logger.info(
                    "developer_report: PII sanitized — phones=%d emails=%d",
                    sanitation.phones_redacted, sanitation.emails_redacted,
                )

            # עיבוד צילומי מסך
            screenshots: list[tuple[str, bytes]] = []
            files = request.files.getlist("screenshots")
            for f in files[:MAX_SCREENSHOTS]:
                if f and f.filename and allowed_file(f.filename):
                    file_data = f.read()
                    if len(file_data) > MAX_SCREENSHOT_SIZE:
                        flash(f"הקובץ {f.filename} גדול מדי (עד 10MB).", "warning")
                        continue
                    if file_data:
                        screenshots.append((f.filename, file_data))

            # שמירה ב-DB — תיאור סניטיזי בלבד
            report_id = db.save_developer_report(
                description=description,
                screenshot_count=len(screenshots),
            )

            # שליחה לטלגרם / מייל של המפתח — שכבה 4 (תיקון 13).
            # אותו description הסניטיזי שנשמר ב-DB עובר. send_report_to_developer
            # לא מקבל את הטקסט המקורי בכלל — אין דרך לעקוף.
            if is_configured():
                sent = send_report_to_developer(
                    description=description,
                    report_id=report_id,
                    screenshots=screenshots or None,
                )
                if sent:
                    flash("הדיווח נשלח בהצלחה למפתח! 🎯", "success")
                else:
                    flash("הדיווח נשמר, אך השליחה למפתח נכשלה. נסו שוב מאוחר יותר.", "warning")
            else:
                flash("הדיווח נשמר. שליחה אוטומטית לא מוגדרת — פנו למפתח.", "info")

            return redirect(url_for("developer_report"))

        reports = db.get_developer_reports()
        return render_template(
            "developer_report.html",
            reports=reports,
            dev_configured=is_configured(),
        )

    @app.route("/developer-report/<int:report_id>/resolve", methods=["POST"])
    @login_required
    def developer_report_resolve(report_id):
        """סימון דיווח כטופל."""
        db.update_developer_report_status(report_id, "resolved")
        if request.headers.get("HX-Request"):
            reports = db.get_developer_reports()
            return render_template(
                "partials/developer_report_rows.html",
                reports=reports,
            )
        return redirect(url_for("developer_report"))

    @app.route("/developer-report/<int:report_id>/reopen", methods=["POST"])
    @login_required
    def developer_report_reopen(report_id):
        """פתיחה מחדש של דיווח — עדכון סטטוס + שליחת התראה מחדש למפתח."""
        from ai_chatbot.developer_report_service import (
            is_configured,
            send_report_to_developer,
        )

        db.update_developer_report_status(report_id, "open")

        # שליחת התראה חוזרת למפתח
        if is_configured():
            report = db.get_developer_report(report_id)
            if report:
                send_report_to_developer(
                    description=f"[נפתח מחדש] {report['description']}",
                    report_id=report_id,
                )

        if request.headers.get("HX-Request"):
            reports = db.get_developer_reports()
            return render_template(
                "partials/developer_report_rows.html",
                reports=reports,
            )
        return redirect(url_for("developer_report"))

    # ─── API Endpoints (for AJAX) ─────────────────────────────────────────

    @app.route("/api/requests/rows")
    @login_required
    def api_requests_rows():
        """שורות טבלת בקשות נציג — לריענון אוטומטי עם HTMX polling."""
        requests_list = db.get_agent_requests()
        active_live_chats = {lc["user_id"] for lc in LiveChatService.get_all_active()}
        html_parts = []
        for req in requests_list:
            html_parts.append(render_template(
                "partials/request_row.html",
                req=req,
                active_live_chats=active_live_chats,
            ))
        return "".join(html_parts)

    @app.route("/api/appointments/rows")
    @login_required
    def api_appointments_rows():
        """שורות טבלת תורים — לריענון אוטומטי עם HTMX polling."""
        db.expire_past_appointments()
        appointments_list = db.get_appointments()
        _enrich_pending_with_duration_options(appointments_list)
        html_parts = []
        for appt in appointments_list:
            html_parts.append(render_template(
                "partials/appointment_row.html",
                appt=appt,
            ))
        return "".join(html_parts)

    @app.route("/api/appointments/calendar")
    @login_required
    def api_appointments_calendar():
        """לוח שנה חודשי — HTML partial לריענון אוטומטי."""
        db.expire_past_appointments()
        appointments_list = db.get_appointments()
        cal_ctx = _build_calendar_context(appointments_list)
        return render_template("partials/appointments_calendar.html", **cal_ctx)

    @app.route("/api/appointments/data")
    @login_required
    def api_appointments_data():
        """נתוני תורים כ-JSON — לעדכון ה-JS data אחרי ריענון הלוח."""
        db.expire_past_appointments()
        appointments_list = db.get_appointments()
        _enrich_pending_with_duration_options(appointments_list)
        return jsonify(appointments_list)

    @app.route("/api/appointments/<int:appt_id>/ics-preview")
    @login_required
    def api_ics_preview(appt_id):
        """תצוגה מקדימה של שדות קובץ .ics לתור — JSON."""
        appt = db.get_appointment(appt_id)
        if not appt:
            return jsonify({"error": "תור לא נמצא"}), 404
        try:
            from ics_service import build_ics_preview
            # מעדיפים confirmed_duration_minutes שבעל העסק בחר באישור — אחרת
            # נופלים לברירת מחדל של השירות. עקביות עם ה-ICS שנשלח בפועל
            # ועם האירוע ב-Google Calendar (אחרת ה-preview שונה מהקובץ הסופי).
            duration = db.resolve_appointment_duration_minutes(appt)
            preview = build_ics_preview(
                service=appt.get("service", ""),
                preferred_date=appt.get("preferred_date", ""),
                preferred_time=appt.get("preferred_time", ""),
                duration_minutes=duration,
            )
            if not preview:
                return jsonify({"error": "תאריך או שעה לא חוקיים"}), 422
            return jsonify(preview)
        except Exception:
            logger.error("שגיאה ביצירת תצוגה מקדימה ICS לתור #%d", appt_id, exc_info=True)
            return jsonify({"error": "שגיאה ביצירת תצוגה"}), 500

    @app.route("/api/user-note/<user_id>", methods=["GET"])
    @login_required
    def api_get_user_note(user_id):
        """קבלת פתק ללקוח."""
        return jsonify({"user_id": user_id, "note": db.get_user_note(user_id)})

    @app.route("/api/user-note/<user_id>", methods=["POST"])
    @login_required
    def api_save_user_note(user_id):
        """שמירת/עדכון פתק ללקוח.

        save_user_note דורס tags ו-withhold_reason לערכי ברירת מחדל אם לא
        מועברים. כדי לא למחוק נתונים בשקט (תיקון 13), קוראים את הקיים
        ומעבירים אותו חזרה.
        """
        note = request.form.get("note", "").strip()
        existing = db.get_user_note_full(user_id)
        db.save_user_note(
            user_id, note,
            tags=existing.get("tags", []),
            withhold_reason=existing.get("withhold_reason", ""),
        )
        return jsonify({"ok": True, "user_id": user_id, "note": note})

    # ─── Web Push Subscriptions ────────────────────────────────────────────
    # רישום/הסרת מנויי דפדפן של בעל העסק להתראות Web Push כשהדשבורד סגור.
    # ה-Service Worker (/sw.js) מטפל באירוע ה-`push` ומציג notification.

    @app.route("/api/push/vapid-public-key")
    @login_required
    def api_push_vapid_public_key():
        """מחזיר את ה-VAPID public key ל-client לצורך PushManager.subscribe.

        ערך ריק מאותת ל-client שהמנגנון מושבת — ה-JS לא ינסה להירשם.
        """
        from ai_chatbot.config import VAPID_PUBLIC_KEY
        return jsonify({"key": VAPID_PUBLIC_KEY})

    @app.route("/api/push/subscribe", methods=["POST"])
    @login_required
    @csrf.exempt  # CSRF מוגן ע"י login_required + same-origin של PushManager
    def api_push_subscribe():
        """שמירת מנוי Web Push חדש (או דריסת מנוי קיים לאותו endpoint)."""
        data = request.get_json(silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip()
        keys = data.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth:
            return jsonify({"ok": False, "error": "missing subscription fields"}), 400
        user_agent = (request.headers.get("User-Agent") or "")[:255]
        try:
            db.upsert_push_subscription(endpoint, p256dh, auth, user_agent)
        except Exception:
            logger.exception("upsert_push_subscription failed")
            return jsonify({"ok": False, "error": "db error"}), 500
        return jsonify({"ok": True})

    @app.route("/api/push/unsubscribe", methods=["POST"])
    @login_required
    @csrf.exempt
    def api_push_unsubscribe():
        """מחיקת מנוי לפי endpoint — נקרא כשהמשתמש מבטל הרשאת notifications."""
        data = request.get_json(silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "missing endpoint"}), 400
        try:
            db.delete_push_subscription(endpoint)
        except Exception:
            logger.exception("delete_push_subscription failed")
            return jsonify({"ok": False, "error": "db error"}), 500
        return jsonify({"ok": True})

    @app.route("/api/stats")
    @login_required
    def api_stats():
        vacation = db.get_vacation_mode()
        # הודעות אחרונות בשיחות חיות — לצורך התראות בזמן אמת
        live_chat_updates = db.get_live_chat_latest_user_messages()
        # get_dashboard_counts מחזיר pending_requests, pending_appointments,
        # ו-open_knowledge_gaps באותה שאילתה מאוחדת — חוסך 3 round-trips
        # ל-DB ב-endpoint שמתבצע מ-polling כל 5 שניות מהסיידבאר.
        counts = db.get_dashboard_counts()
        # שלב 7 — מונה pending_facts ל-badge בסיידבר. שאילתת COUNT(*) זולה
        # (אינדקס על status). חייב BUSINESS_ID כדי שיתאים ל-/pending-facts
        # ב-multi-tenant. לא len(get_pending_facts()) כי הוא חסום ל-200.
        try:
            pending_facts_count = db.get_pending_facts_count(business_id=BUSINESS_ID)
        except Exception:
            logger.exception("api_stats: pending facts count failed")
            pending_facts_count = 0
        return jsonify({
            "pending_requests": counts.get("pending_requests", 0),
            "pending_appointments": counts.get("pending_appointments", 0),
            "active_live_chats": LiveChatService.count_active(),
            "open_knowledge_gaps": counts.get("open_knowledge_gaps", 0),
            "vacation_active": bool(vacation["is_active"]),
            "live_chat_updates": live_chat_updates,
            "pending_facts": pending_facts_count,
        })

    # ─── Telegram Webhook Endpoint ──────────────────────────────────────────
    # endpoint זה פעיל רק כש-WEBHOOK_URL מוגדר וה-Application מחובר.
    # ה-Application עצמו נשמר ב-app.config["_telegram_app"] ע"י main.py.
    @app.route("/telegram/webhook", methods=["POST"])
    @csrf.exempt  # בקשות מטלגרם — ללא CSRF token
    def telegram_webhook():
        """מקבל עדכונים מטלגרם ומעביר ל-bot Application לעיבוד."""
        telegram_app = app.config.get("_telegram_app")
        if telegram_app is None:
            return "Bot not configured", 503

        # אימות secret token — טלגרם שולח אותו בהדר X-Telegram-Bot-Api-Secret-Token
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(token, WEBHOOK_SECRET):
                logger.warning("Webhook request with invalid secret token from %s", request.remote_addr)
                return "Forbidden", 403

        try:
            from telegram import Update
            update_data = request.get_json(force=True)
            bot_loop = app.config.get("_bot_loop")
            if bot_loop is None:
                return "Bot loop not available", 503

            import asyncio
            update = Update.de_json(update_data, telegram_app.bot)
            future = asyncio.run_coroutine_threadsafe(
                telegram_app.process_update(update),
                bot_loop,
            )
            # callback לזיהוי כשלונות — לא זורקים את ה-Future
            def _on_done(f):
                if f.cancelled():
                    return
                exc = f.exception()
                if exc:
                    logger.error("Error processing webhook update: %s", exc)
            future.add_done_callback(_on_done)

        except Exception:
            logger.exception("Failed to process webhook update")
            return "Internal error", 500

        return "OK", 200

    @app.route("/telegram/webhook/t/<webhook_key>", methods=["POST"])
    @csrf.exempt  # בקשות מטלגרם — ללא CSRF token
    def telegram_tenant_webhook(webhook_key):
        """‏webhook של בוט tenant (multi-tenant שלב 2, spec 6.1).

        המפתח האקראי ב-URL קובע את ה-tenant; ה-secret של טלגרם מאומת מול
        הסוד *של אותו tenant*. העיבוד נשלח ללולאת הבוטים המשותפת, שם
        האפליקציה של ה-tenant נבנית עצלה בהודעה הראשונה (bot_registry).
        """
        from control_plane import resolve_route
        from bot_registry import dispatch_tenant_update, resolve_webhook_secret

        tenant = resolve_route("telegram_webhook_key", webhook_key)
        if tenant is None:
            logger.warning("telegram tenant webhook: unknown key — rejecting")
            return "Not found", 404

        # fail-closed: ל-tenant חייב להיות secret רשום (connect-telegram
        # יוצר אותו). בלי secret אין דרך לאמת שהבקשה באמת מטלגרם.
        secret = resolve_webhook_secret(tenant)
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(header, secret):
            logger.warning(
                "telegram tenant webhook: invalid secret (tenant=%s)", tenant,
            )
            return "Forbidden", 403

        bot_loop = app.config.get("_bot_loop")
        if bot_loop is None:
            # בוטים של tenants דורשים את לולאת ה-webhook המשולבת (main.py
            # במצב webhook). ב-polling/admin-only אין לולאה — 503 עם לוג.
            logger.error(
                "telegram tenant webhook: bot loop not available "
                "(platform requires webhook mode)"
            )
            return "Bot loop not available", 503

        try:
            import asyncio

            update_data = request.get_json(force=True)
            future = asyncio.run_coroutine_threadsafe(
                dispatch_tenant_update(tenant, update_data),
                bot_loop,
            )

            # callback לזיהוי כשלונות — לא זורקים את ה-Future
            def _on_done(f):
                if f.cancelled():
                    return
                exc = f.exception()
                if exc:
                    logger.error(
                        "Error processing tenant webhook update (tenant=%s): %s",
                        tenant, exc,
                    )

            future.add_done_callback(_on_done)
        except Exception:
            logger.exception("Failed to process tenant webhook update")
            return "Internal error", 500

        return "OK", 200

    # ── WhatsApp Webhook Blueprint ───────────────────────────────────────
    # נרשם תמיד (multi-tenant): גם כשה-env לא מגדיר Twilio, ל-tenants
    # יכולים להיות credentials ב-control plane. ההחלטה אם לשרת נעשית
    # פר-בקשה (resolve של ה-tenant + בדיקת הגדרות ⇒ 404/503 בהתאם).
    from messaging.whatsapp_webhook import whatsapp_bp
    csrf.exempt(whatsapp_bp)  # Twilio שולח POST ללא CSRF token
    app.register_blueprint(whatsapp_bp)
    logger.info("WhatsApp webhook blueprint registered at /webhook/whatsapp")

    # ── Meta DM Webhook Blueprint (Instagram + Messenger) ───────────────
    # שלב 1: רק קליטה ולוג. בלי OAuth, בלי שליחה, בלי כתיבה ל-DB.
    # נרשם רק אם META_APP_SECRET ו-META_VERIFY_TOKEN מוגדרים — אחרת
    # אין דרך לאמת חתימה או handshake, וה-endpoint לא רלוונטי.
    from ai_chatbot.config import META_APP_SECRET, META_VERIFY_TOKEN
    if META_APP_SECRET and META_VERIFY_TOKEN:
        from messaging.meta_webhook import meta_bp
        csrf.exempt(meta_bp)  # מטא שולחת POST ללא CSRF token
        app.register_blueprint(meta_bp)
        logger.info("Meta webhook blueprint registered at /webhooks/meta")

    # ─── דפים משפטיים פומביים — מדיניות פרטיות ותנאי שימוש ─────────────────
    # נדרשים בתיקון 13. הקישור אליהם מוצג למשתמש במסך ההסכמה הראשוני בבוט.
    # נטענים מקבצי markdown ב-docs/legal ומוצגים ב-HTML פשוט עם RTL.

    def _render_legal_doc(filename: str, title: str):
        import re
        from pathlib import Path
        from markupsafe import escape as _esc

        repo_root = Path(__file__).resolve().parent.parent
        doc_path = repo_root / "docs" / "legal" / filename
        try:
            content = doc_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"<h1>{_esc(title)}</h1><p>המסמך לא נמצא.</p>", 404

        # אבטחה — חוסמים raw HTML במקור ה-markdown לפני רינדור.
        # ה-route ציבורי (ללא login), ולכן גם אם רק אנחנו מעדכנים את הקבצים,
        # מסירים תגיות גולמיות כדי שלא ייכנסו <script>/<iframe>/וכו'.
        # התוצאה: רק markdown formatting (כותרות, רשימות, טבלאות, bold) הופך
        # ל-HTML — כל תגית גולמית בקובץ הופכת לטקסט רגיל.
        sanitized_content = re.sub(r"<[^>]+>", "", content)

        # רינדור markdown בסיסי בלי תלות חיצונית — שומר על RTL וטיפוגרפיה ברורה
        try:
            from markdown import markdown
            body_html = markdown(sanitized_content, extensions=["tables", "fenced_code"])
        except ImportError:
            # fallback — אם markdown לא מותקן, מציגים pre-formatted
            body_html = f"<pre>{_esc(sanitized_content)}</pre>"

        page = f"""<!doctype html>
<html lang=\"he\" dir=\"rtl\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{_esc(title)}</title>
<style>
body {{ font-family: -apple-system, system-ui, 'Segoe UI', Arial, sans-serif;
       max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.7;
       color: #222; background: #fff; }}
h1, h2, h3 {{ margin-top: 1.6em; }}
table {{ border-collapse: collapse; margin: 1em 0; }}
th, td {{ border: 1px solid #ddd; padding: 0.4em 0.7em; text-align: right; }}
th {{ background: #f3f3f3; }}
code {{ background: #f3f3f3; padding: 0.1em 0.3em; border-radius: 3px; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 2em 0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""
        return page, 200

    @app.route("/legal/terms")
    def legal_terms():
        html, code = _render_legal_doc("terms.md", "תנאי שימוש")
        return html, code

    @app.route("/legal/privacy")
    def legal_privacy():
        html, code = _render_legal_doc("privacy.md", "מדיניות פרטיות")
        return html, code

    return app


def run_admin():
    """Start the Flask admin panel (blocking call)."""
    logger.info("Starting admin panel on %s:%s", ADMIN_HOST, ADMIN_PORT)
    app = create_admin_app()
    app.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=False)
