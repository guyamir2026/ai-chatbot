"""
Meta Graph API client — קריאות OAuth ו-management עבור עמודי
Instagram/Messenger DM.

מצולם מקריאות שצריך לבצע בזרימת ההתקנה (admin/meta_oauth.py):

1. **exchange_code_for_user_token** — לוקח את ה-`code` שמטא מחזירה
   ב-OAuth callback, מחליף ב-short-lived user token.
2. **exchange_for_long_lived_user_token** — short → long-lived (60 יום).
3. **list_user_pages** — מחזיר את כל העמודים שהמשתמש מנהל, עם
   page access tokens שלהם.
4. **get_ig_business_account_id** — מקשר page → IG Business Account
   (אם קיים אחד מקושר).
5. **subscribe_page_to_webhook** — מוסיף את העמוד ל-webhook של
   האפליקציה, כדי שהודעות יגיעו אלינו. בלי זה — OAuth מאומת אבל
   הודעות לא יגיעו.
6. **unsubscribe_page_from_webhook** — לניתוק עמוד.

עיצוב:
- כל קריאה עוטפת `requests` עם timeout מפורש.
- כל שגיאה זורקת `MetaGraphError` עם פירוט.
- אין caching ואין retry — תפקיד הקורא להתמודד עם כשלים.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from ai_chatbot.config import (
    META_APP_ID,
    META_APP_SECRET,
    META_GRAPH_API_VERSION,
)

logger = logging.getLogger(__name__)

# Timeout נדיב — Graph API לפעמים איטי, במיוחד לקריאות subscription.
_TIMEOUT_SECONDS = 15


class MetaGraphError(RuntimeError):
    """שגיאה מקריאת Graph API. message מכיל את ההסבר של מטא."""


def _graph_url(path: str) -> str:
    """בונה URL מלא ל-Graph API לפי גרסה מוגדרת."""
    version = META_GRAPH_API_VERSION or "v21.0"
    return f"https://graph.facebook.com/{version}/{path.lstrip('/')}"


def _raise_for_graph_error(resp: requests.Response, context: str) -> None:
    """בודק תגובה מ-Graph API וזורק עם הקשר מפורט.

    מטא מחזירה שגיאות כ-JSON עם שדה `error.message` — שווה לחלץ אותו
    כדי שהלוג יספר משהו שימושי במקום סטטוס HTTP יבש.
    """
    if resp.ok:
        return
    detail = ""
    try:
        err = resp.json().get("error", {})
        detail = f"{err.get('type', '')}: {err.get('message', '')} (code={err.get('code', '')})"
    except Exception:
        detail = resp.text[:300]
    raise MetaGraphError(f"{context} נכשל ({resp.status_code}): {detail}")


# ─── OAuth token exchange ─────────────────────────────────────────────────


def exchange_code_for_user_token(code: str, redirect_uri: str) -> str:
    """short-lived user token (חיים כשעה).

    `redirect_uri` חייב להתאים *בדיוק* לזה שהוגדר ב-Meta App Dashboard
    ושנשלח ב-authorize URL — מטא מוודאת התאמה.
    """
    if not META_APP_ID or not META_APP_SECRET:
        raise MetaGraphError("META_APP_ID או META_APP_SECRET לא מוגדרים")
    resp = requests.get(
        _graph_url("oauth/access_token"),
        params={
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "exchange_code_for_user_token")
    data = resp.json()
    token = data.get("access_token", "")
    if not token:
        raise MetaGraphError("exchange_code_for_user_token: לא הוחזר access_token")
    return token


def exchange_for_long_lived_user_token(short_token: str) -> str:
    """short-lived → long-lived user token (60 יום).

    אחרי 60 יום, page tokens שנגזרו ממנו עדיין יעבדו (מטא לא פוגגת
    page tokens שהונפקו מ-user token חי). אבל אם המשתמש מסיר הרשאות
    באמצע — כל ה-tokens מוצאים מהתוקף.
    """
    resp = requests.get(
        _graph_url("oauth/access_token"),
        params={
            "grant_type": "fb_exchange_token",
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "fb_exchange_token": short_token,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "exchange_for_long_lived_user_token")
    token = resp.json().get("access_token", "")
    if not token:
        raise MetaGraphError(
            "exchange_for_long_lived_user_token: לא הוחזר access_token"
        )
    return token


# ─── Page & IG metadata ───────────────────────────────────────────────────


def list_user_pages(user_token: str) -> list[dict]:
    """מחזיר עמודי FB שהמשתמש מנהל + page access tokens שלהם.

    כל פריט: `{id, name, access_token, tasks}`. אם המשתמש לא מנהל
    אף עמוד — רשימה ריקה.
    """
    resp = requests.get(
        _graph_url("me/accounts"),
        params={
            "access_token": user_token,
            "fields": "id,name,access_token,tasks",
            "limit": 100,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "list_user_pages")
    return resp.json().get("data", []) or []


def get_user_info(user_token: str) -> dict:
    """דיאגנוסטיקה: שולף פרטי בסיס של משתמש דרך /me.

    שימושי לוודא שטוקן תקף בכלל ושמטא מזהה את המשתמש. מחזיר
    ``{"id": "...", "name": "..."}`` בהצלחה, ו-``{}`` בכל כשל
    (כולל טוקן פג, רשת, JSON פגום). לא זורק — מיועד לזרימת
    onboarding שאסור לה לקרוס בגלל דיאגנוסטיקה.
    """
    try:
        resp = requests.get(
            _graph_url("me"),
            params={"access_token": user_token, "fields": "id,name"},
            timeout=_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            # error body של מטא קצר וברור בד"כ ("OAuthException: ...") —
            # שווה ללוג כדי שאבחנה לא תהיה שקטה. truncate ל-200 כי לא
            # אמורות להופיע סודות אבל ליתר ביטחון.
            logger.warning(
                "get_user_info: HTTP %s — מחזיר dict ריק. detail=%s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return {}
        body = resp.json()
        return {"id": str(body.get("id", "")), "name": str(body.get("name", ""))}
    except Exception:
        logger.exception("get_user_info: כשל לא צפוי")
        return {}


def list_user_pages_nested(user_token: str) -> list[dict]:
    """fallback: ניסיון לשליפת עמודים דרך nested expansion ב-/me.

    `/me/accounts` סטנדרטי לפעמים מחזיר ריק כשמטא מסתבכת בFB Login for
    Business (גם כשהרשאות granted ועמודים הוצגו ב-asset picker). הדפוס
    ``/me?fields=accounts{...}`` עוקף את ה-edge הזה לפעמים — מטא
    מתייחסת אליו דרך מסלול אחר.

    מחזיר רשימה זהה במבנה ל-list_user_pages, או רשימה ריקה בכשל.
    """
    try:
        resp = requests.get(
            _graph_url("me"),
            params={
                "access_token": user_token,
                "fields": "accounts.limit(100){id,name,access_token,tasks}",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            logger.warning(
                "list_user_pages_nested: HTTP %s. detail=%s",
                resp.status_code, (resp.text or "")[:200],
            )
            return []
        accounts = resp.json().get("accounts", {})
        # accounts אמור להיות dict עם data:[...] אבל אם מטא תחזיר
        # מבנה מעוות, אסור שנקרוס downstream על .get(). מוודאים
        # שזה list ושכל פריט בו dict — אחרת מסננים.
        if not isinstance(accounts, dict):
            return []
        data = accounts.get("data", [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]
    except Exception:
        logger.exception("list_user_pages_nested: כשל לא צפוי")
        return []


def get_user_businesses(user_token: str) -> list[dict]:
    """דיאגנוסטיקה: שולף Business Portfolios של המשתמש.

    אם הדפים של המשתמש מתחת ל-Business Portfolio, הם לא יחזרו
    דרך /me/accounts ויש לשלוף אותם דרך /{business_id}/owned_pages.
    מחזיר רשימת ``{"id": "...", "name": "..."}``, או רשימה ריקה
    בכשל. לא זורק.
    """
    try:
        resp = requests.get(
            _graph_url("me/businesses"),
            params={"access_token": user_token, "fields": "id,name"},
            timeout=_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            logger.warning(
                "get_user_businesses: HTTP %s — מחזיר רשימה ריקה. detail=%s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return []
        return [
            {"id": str(b.get("id", "")), "name": str(b.get("name", ""))}
            for b in resp.json().get("data", [])
            if b.get("id")
        ]
    except Exception:
        logger.exception("get_user_businesses: כשל לא צפוי")
        return []


def merge_pages_by_id(*page_lists: list[dict]) -> list[dict]:
    """ממזג רשימות עמודים עם dedup לפי page id, שומר על סדר ההופעה.

    כשעמוד מופיע ביותר מרשימה אחת — owned_pages מול client_pages, או
    /me/accounts מול עמודי תיק עסקי — ואחת הגרסאות עם access_token והשנייה
    בלי, מעדיפים את הגרסה **עם** הטוקן. בלי ההעדפה הזו עמוד עסקי עלול
    להישאר בלי token (אם הגרסה הראשונה שנראתה הייתה חסרת token) ולא להופיע
    לבחירה — למרות שמטא כן החזירה לו token במקור אחר. פריט שאינו dict /
    בלי id מסונן.

    זהו ה-helper היחיד למיזוג עמודים בפרויקט — list_business_pages וזרימת
    ה-OAuth callback שניהם משתמשים בו, כדי שלוגיקת ה-dedup לא תשוכפל ותסטה.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for pages in page_lists:
        for page in pages:
            if not isinstance(page, dict):
                continue
            pid = page.get("id")
            if not pid:
                continue
            if pid not in by_id:
                by_id[pid] = page
                order.append(pid)
            elif not by_id[pid].get("access_token") and page.get("access_token"):
                by_id[pid] = page  # שדרוג: גרסה בלי token → גרסה עם token
    return [by_id[pid] for pid in order]


def list_business_pages(business_id: str, user_token: str) -> list[dict]:
    """שולף עמודים שמנוהלים תחת תיק עסקי (Business Portfolio).

    `/me/accounts` **לא** מחזיר עמודים שמשויכים ל-Business Portfolio —
    מטא דורשת לשלוף אותם ישירות דרך `/{business_id}/owned_pages`
    (עמודים בבעלות התיק) ו-`/{business_id}/client_pages` (עמודים
    ששותפו לתיק מגורם אחר). שתי הקריאות דורשות הרשאת `business_management`.

    מחזיר רשימה במבנה זהה ל-`list_user_pages`: `{id, name, access_token,
    tasks}`, ללא כפילויות. עמוד שמופיע גם ב-owned וגם ב-client ממוזג דרך
    `merge_pages_by_id` שמעדיף את הגרסה **עם** token — כך עמוד שחזר בלי
    token ב-owned_pages לא מסתיר גרסה עם token שחזרה ב-client_pages (או
    להפך). עמוד שאין לו token באף מקור — הקורא מסנן בהמשך.

    כל קריאה עטופה ב-try/except נפרד: כשל ב-edge אחד לא מונע מהשני
    להחזיר תוצאות, ובוודאי לא מפיל את זרימת ה-OAuth. מחזיר רשימה ריקה
    בכשל מלא — לא זורק.
    """
    edge_results: list[list[dict]] = []
    for edge in ("owned_pages", "client_pages"):
        try:
            resp = requests.get(
                _graph_url(f"{business_id}/{edge}"),
                params={
                    "access_token": user_token,
                    "fields": "id,name,access_token,tasks",
                    "limit": 100,
                },
                timeout=_TIMEOUT_SECONDS,
            )
            if not resp.ok:
                logger.warning(
                    "list_business_pages: %s HTTP %s. detail=%s",
                    edge, resp.status_code, (resp.text or "")[:200],
                )
                continue
            data = resp.json().get("data", [])
            # הגנה מפני מבנה לא צפוי — אסור לקרוס downstream על .get()
            if isinstance(data, list):
                edge_results.append(data)
        except Exception:
            logger.exception("list_business_pages: כשל לא צפוי ב-%s", edge)
    # dedup עם העדפת-token בין owned ל-client (עמוד יכול להופיע בשניהם,
    # ולעיתים רק לאחד מהם יש token). merge_pages_by_id גם מסנן non-dict/idless.
    return merge_pages_by_id(*edge_results)


def get_user_permissions(user_token: str) -> dict:
    """שולף את רשימת ההרשאות שנגרנטו/נדחו ל-user token.

    מחזיר ``{"granted": [...], "declined": [...]}``. שימושי
    לדיאגנוסטיקה כשנדמה ש-OAuth הצליח אבל קריאות הבאות מחזירות
    ריק — אם ההרשאה הרלוונטית ב-`declined` או שאינה ברשימה,
    זו אינדיקציה ברורה שהמשתמש לא אישר אותה.

    אם הקריאה נכשלת מטעם כלשהו (טוקן פג, אין קישוריות) —
    מחזיר dict ריק עם רשימות ריקות במקום לזרוק. בהקשר
    דיאגנוסטיקה, חוסר מידע עדיף על קריסה.
    """
    try:
        resp = requests.get(
            _graph_url("me/permissions"),
            params={"access_token": user_token},
            timeout=_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            logger.warning(
                "get_user_permissions: HTTP %s — מחזיר רשימות ריקות. detail=%s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return {"granted": [], "declined": []}
        data = resp.json().get("data", [])
        # .get("permission") במקום ["permission"] כדי לעמוד בחוזה ה-docstring
        # ("לעולם לא זורק") גם אם מטא תחזיר entry בלי שדה permission —
        # אנחנו מסננים None וממשיכים.
        return {
            "granted": [
                p.get("permission") for p in data
                if p.get("status") == "granted" and p.get("permission")
            ],
            "declined": [
                p.get("permission") for p in data
                if p.get("status") == "declined" and p.get("permission")
            ],
        }
    except Exception:
        logger.exception("get_user_permissions: כשל לא צפוי")
        return {"granted": [], "declined": []}


def get_ig_business_account(page_id: str, page_token: str) -> Optional[dict]:
    """שולף את חשבון ה-IG (Professional) המקושר לעמוד, אם קיים.

    מחזיר `{id, username}` או None. עמוד פייסבוק יכול להיות בלי חשבון
    IG מקושר — זה תקין, פשוט אין תמיכת אינסטגרם לעמוד הזה.

    שולף שני שדות ומעדיף את הראשון:
    - `instagram_business_account` — הקישור הסטנדרטי (IG Professional
      מקושר לעמוד דרך הגדרות העמוד). המקור המועדף.
    - `connected_instagram_account` — קישור דרך Account Center. fallback
      לקישורים מהסוג החדש, ולחלק מחשבונות Creator שלא תמיד מאוכלסים
      ב-business_account.

    מלוגג איזה שדה אוכלס (id בלבד; username לא נחשף — PII) כדי לאבחן
    מצב שבו נדמה שיש IG מקושר אבל הוא לא נמצא. אם השליפה המלאה נכשלת
    (למשל השדה connected אינו נתמך בהקשר), נופלים לשליפת business_account
    בלבד כדי לא לאבד את המקרה הנפוץ.
    """
    fields_full = (
        "instagram_business_account{id,username},"
        "connected_instagram_account{id,username}"
    )
    try:
        resp = requests.get(
            _graph_url(page_id),
            params={"access_token": page_token, "fields": fields_full},
            timeout=_TIMEOUT_SECONDS,
        )
        _raise_for_graph_error(resp, "get_ig_business_account")
        body = resp.json()
    except MetaGraphError:
        logger.warning(
            "get_ig_business_account: שליפה מלאה נכשלה — מנסה business_account בלבד"
        )
        resp = requests.get(
            _graph_url(page_id),
            params={
                "access_token": page_token,
                "fields": "instagram_business_account{id,username}",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        _raise_for_graph_error(resp, "get_ig_business_account")
        body = resp.json()

    business = body.get("instagram_business_account") or {}
    connected = body.get("connected_instagram_account") or {}
    logger.info(
        "get_ig_business_account(page=%s): business=%s connected=%s",
        str(page_id)[:6] + "...",
        bool(business.get("id")),
        bool(connected.get("id")),
    )
    ig = business if business.get("id") else connected
    if not ig or not ig.get("id"):
        return None
    return {"id": ig.get("id"), "username": ig.get("username", "")}


# ─── Webhook subscription ─────────────────────────────────────────────────

# ה-fields שאליהם נרשמים. ל-Messenger ול-IG אותם שמות שדות — מטא
# מאחדת את ה-subscription תחת page subscription אחד.
_WEBHOOK_FIELDS = "messages,messaging_postbacks,message_reads"


def _require_success_body(resp: requests.Response, context: str) -> None:
    """מוודא שגוף התגובה מכיל `success: true`.

    `/subscribed_apps` של מטא יכולה להחזיר HTTP 200 עם `{"success": false}`
    במקרים מסוימים (הרשאה חלקית, רישום שלא נשמר). בלי הבדיקה הזו, הקוד
    היה מניח שהרישום הצליח. כל ערך falsy / חסר ⇒ שגיאה.
    """
    try:
        body = resp.json()
    except Exception:
        raise MetaGraphError(f"{context}: תגובת מטא אינה JSON תקין")
    if not isinstance(body, dict) or not body.get("success"):
        raise MetaGraphError(
            f"{context}: מטא החזירה success={body.get('success') if isinstance(body, dict) else body!r}"
        )


def subscribe_page_to_webhook(page_id: str, page_token: str) -> None:
    """רושם את העמוד ל-webhook של האפליקציה.

    בלי זה — מטא לא תשלח אלינו events מהעמוד, גם אחרי OAuth מוצלח.
    זוהי דרישה מטא: כל עמוד נרשם בנפרד.
    """
    resp = requests.post(
        _graph_url(f"{page_id}/subscribed_apps"),
        params={
            "access_token": page_token,
            "subscribed_fields": _WEBHOOK_FIELDS,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "subscribe_page_to_webhook")
    _require_success_body(resp, "subscribe_page_to_webhook")


def unsubscribe_page_from_webhook(page_id: str, page_token: str) -> None:
    """מסיר את העמוד מה-webhook של האפליקציה. לניתוק."""
    resp = requests.delete(
        _graph_url(f"{page_id}/subscribed_apps"),
        params={"access_token": page_token},
        timeout=_TIMEOUT_SECONDS,
    )
    _raise_for_graph_error(resp, "unsubscribe_page_from_webhook")
    _require_success_body(resp, "unsubscribe_page_from_webhook")
