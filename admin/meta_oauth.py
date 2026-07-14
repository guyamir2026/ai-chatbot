"""
Admin OAuth flow לחיבור עמוד מטא (Facebook + Instagram).

מבנה: `register_meta_oauth_routes(app, login_required)` נרשם
מ-`admin/app.py` בתוך ה-closure שיוצרת את `login_required` ו-`csrf`.

זרימה:
1. בעל העסק נכנס ל-`/admin/meta/setup` — רואה עמודים מחוברים.
2. לוחץ "חבר חשבון" → `/admin/meta/connect` בונה URL ל-Facebook
   OAuth ומפנה. שומר `state` ב-session להגנת CSRF.
3. מטא מחזירה ל-`/admin/meta/callback` עם `code` ו-`state`.
4. ה-callback מחליף code → user token → long-lived user token,
   מציג למשתמש רשימת עמודים שהוא מנהל (אם יותר מאחד), מאפשר
   לבחור עמוד אחד, ואז:
   - שולף IG Business Account (אם קיים).
   - רושם את העמוד ל-webhook.
   - שומר ל-DB עם access_token מוצפן.
5. `/admin/meta/disconnect/<page_id>` מבטל subscription ומוחק.
"""

from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlencode

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import database as db
from ai_chatbot.config import (
    META_APP_ID,
    META_APP_SECRET,
    META_OAUTH_REDIRECT_URI,
)
from messaging.meta_graph_client import (
    MetaGraphError,
    exchange_code_for_user_token,
    exchange_for_long_lived_user_token,
    get_ig_business_account,
    get_user_businesses,
    get_user_info,
    get_user_permissions,
    list_business_pages,
    list_user_pages,
    list_user_pages_nested,
    merge_pages_by_id,
    subscribe_page_to_webhook,
    unsubscribe_page_from_webhook,
)

logger = logging.getLogger(__name__)

# הרשאות OAuth — הקובץ הזה מבקש את המינימום הנדרש ל-DM:
#   pages_show_list           — לראות את העמודים של המשתמש
#   pages_manage_metadata     — לרשום אותם ל-webhook
#   pages_read_engagement     — קריטי: נדרש לקריאת מטא-דאטה של העמוד, כולל
#                               השדה instagram_business_account. בלי זה
#                               get_ig_business_account נכשל ב-(#100) "requires
#                               the pages_read_engagement permission" וה-IG
#                               לא מתחבר אוטומטית למרות שהוא מקושר לעמוד.
#   pages_messaging           — לשלוח/לקבל הודעות ב-Messenger
#   instagram_basic           — לקבל פרטי חשבון IG מקושר
#   instagram_manage_messages — לשלוח/לקבל הודעות ב-IG DM
#   business_management       — קריטי: בלי זה /me/accounts לא מחזיר עמודים
#                               שמנוהלים תחת Business Portfolio (תיק עסקי),
#                               וגם /me/businesses נכשל ב-"Missing Permission".
#                               זה התרחיש הנפוץ של "המשתמש לא מנהל עמוד עסקי"
#                               למרות שיש לו עמוד. admin של האפליקציה מקבל
#                               אותה מיד; לקוחות אחרים — אחרי App Review.
_OAUTH_SCOPES = (
    "pages_show_list,"
    "pages_manage_metadata,"
    "pages_read_engagement,"
    "pages_messaging,"
    "instagram_basic,"
    "instagram_manage_messages,"
    "business_management"
)

_FB_OAUTH_BASE = "https://www.facebook.com/v21.0/dialog/oauth"

# ── Server-side cache ל-page tokens במהלך OAuth ──────────────────────────
# Flask session ברירת מחדל = signed cookie (לא מוצפן). אסור לשמור שם
# page access tokens — הקוד הקודם שעשה זאת חשף אותם בצד הלקוח. כאן
# שומרים tokens ב-cache פנים-תהליכי הזה (חי כל זמן ה-worker), עם
# nonce שמשמש כמפתח. בצד ה-session הולך רק ה-nonce + מטא-דאטה
# לא רגישה (id, name) לרינדור ה-UI.
#
# חיים קצרים (TTL = 10 דקות): אם המשתמש לא בחר עמוד תוך 10 דקות,
# כל מה שנשמר נמחק. כל קריאה ל-cache מנקה ערכים ישנים.
_PENDING_OAUTH_TTL_SEC = 600
_pending_oauth_cache: dict[str, dict] = {}


def _cache_pending_pages(pages_with_tokens: list[dict]) -> str:
    """שומר את העמודים ב-cache פנים-תהליכי, מחזיר nonce לקישור."""
    _gc_pending_oauth()
    nonce = secrets.token_urlsafe(24)
    _pending_oauth_cache[nonce] = {
        "stored_at": time.time(),
        "pages": pages_with_tokens,
    }
    return nonce


def _get_pending_page(nonce: str, page_id: str) -> dict | None:
    """שולף עמוד ספציפי (עם token) מה-cache לפי nonce + page_id."""
    _gc_pending_oauth()
    entry = _pending_oauth_cache.get(nonce)
    if not entry:
        return None
    for p in entry["pages"]:
        if p.get("id") == page_id:
            return p
    return None


def _drop_pending(nonce: str) -> None:
    """מסיר entry מה-cache (אחרי שהושלמה הבחירה / בוטלה)."""
    _pending_oauth_cache.pop(nonce, None)


def _gc_pending_oauth() -> None:
    """מנקה ערכים שעברו TTL. נקרא לפני כל read/write."""
    cutoff = time.time() - _PENDING_OAUTH_TTL_SEC
    expired = [k for k, v in _pending_oauth_cache.items() if v["stored_at"] < cutoff]
    for k in expired:
        _pending_oauth_cache.pop(k, None)


def _any_page_with_token(pages: list[dict]) -> bool:
    """האם יש לפחות עמוד אחד עם access_token לשמירה?

    זה התנאי שקובע אם הזרימה יכולה להמשיך — עמוד בלי token לא שמיש.
    משמש כתנאי ל-fallback (במקום "האם הרשימה ריקה"), כי /me/accounts
    יכול להחזיר עמודים *בלי* token ועדיין להשאיר אותנו בלי מה לחבר.
    """
    return any(p.get("access_token") for p in pages)


def register_meta_oauth_routes(app: Flask, login_required) -> None:
    """רושם את routes של OAuth מטא תחת login_required.

    נקרא מ-`admin/app.py` בתוך ה-closure של create_admin_app, אחרי
    הגדרת `login_required` ו-CSRF.
    """

    def _is_configured() -> bool:
        """כל המשתנים הנדרשים מוגדרים?"""
        return bool(META_APP_ID and META_APP_SECRET and META_OAUTH_REDIRECT_URI)

    @app.route("/admin/meta/setup", methods=["GET"])
    @login_required
    def meta_setup():
        """דף סטטוס: עמודים מחוברים + כפתור חיבור."""
        # pending_pages מכיל רק id+name (בלי tokens) — tokens חיים
        # ב-_pending_oauth_cache פנים-תהליכי בלבד.
        pending_pages = session.get("meta_oauth_pending_pages") or []
        return render_template(
            "meta_setup.html",
            is_configured=_is_configured(),
            connected_pages=db.list_meta_credentials(),
            pending_pages=pending_pages,
            redirect_uri=META_OAUTH_REDIRECT_URI,
        )

    @app.route("/admin/meta/connect", methods=["GET"])
    @login_required
    def meta_connect():
        """בונה URL ל-Facebook OAuth ומפנה."""
        if not _is_configured():
            flash(
                "Meta OAuth לא מוגדר. הגדר META_APP_ID, META_APP_SECRET "
                "ו-META_OAUTH_REDIRECT_URI ב-env לפני שמתחילים.",
                "danger",
            )
            return redirect(url_for("meta_setup"))

        state = secrets.token_urlsafe(32)
        session["meta_oauth_state"] = state
        params = {
            "client_id": META_APP_ID,
            "redirect_uri": META_OAUTH_REDIRECT_URI,
            "state": state,
            "scope": _OAUTH_SCOPES,
            "response_type": "code",
            # auth_type=rerequest: מטא תציג מסך בחירת הרשאות + עמודים
            # **בכל פעם**, גם אם המשתמש כבר אישר את האפליקציה בעבר.
            # בלי זה, מטא לפעמים "חוסכת זמן" ומדלגת על שלב בחירת
            # העמודים אחרי אישור ראשון — והקוד שלנו מקבל /me/accounts
            # ריק כי לא הוקצו עמודים בסשן הנוכחי. ראה דיון:
            # https://developers.facebook.com/docs/facebook-login/guides/permissions/request-revoke
            "auth_type": "rerequest",
        }
        return redirect(f"{_FB_OAUTH_BASE}?{urlencode(params)}")

    @app.route("/admin/meta/callback", methods=["GET"])
    @login_required
    def meta_callback():
        """OAuth callback — מטפל ב-code, מציג למשתמש עמודים לבחירה."""
        error = request.args.get("error")
        if error:
            err_desc = request.args.get("error_description", error)
            flash(f"Meta OAuth בוטל: {err_desc}", "warning")
            return redirect(url_for("meta_setup"))

        state = request.args.get("state", "")
        expected_state = session.pop("meta_oauth_state", "")
        if not state or state != expected_state:
            flash("שגיאת אבטחה — state לא תואם. נסו שוב.", "danger")
            return redirect(url_for("meta_setup"))

        code = request.args.get("code", "")
        if not code:
            flash("לא התקבל authorization code ממטא.", "danger")
            return redirect(url_for("meta_setup"))

        try:
            short_token = exchange_code_for_user_token(
                code, META_OAUTH_REDIRECT_URI
            )
            long_token = exchange_for_long_lived_user_token(short_token)
            pages = list_user_pages(long_token)
        except MetaGraphError as e:
            logger.error("Meta OAuth callback נכשל: %s", e, exc_info=True)
            flash(f"שגיאה בחיבור מטא: {e}", "danger")
            return redirect(url_for("meta_setup"))

        # logging מפורט לדיאגנוסטיקה — חשוב במיוחד בזמן onboarding ראשוני
        # כשמטא מחזירה רשימה ריקה ולא ברור למה. PII redaction:
        # page_id → 6 תווים, page_name לא נחשף.
        logger.info(
            "Meta OAuth: list_user_pages returned %d page(s); "
            "with_tokens=%d; ids_preview=%s",
            len(pages),
            sum(1 for p in pages if p.get("access_token")),
            [str(p.get("id", ""))[:6] + "..." for p in pages[:5]],
        )

        # עמודים שתחת תיק עסקי (Business Portfolio) *לעולם* לא חוזרים דרך
        # /me/accounts (התנהגות מתועדת של Graph API). לכן שולפים אותם
        # וממזגים **תמיד** כשיש תיק עסקי — לא רק כש-/me/accounts ריק. זה
        # קריטי כי /me/accounts יכול להחזיר עמוד אישי בלבד, או עמודים בלי
        # access_token, בעוד העמוד העסקי שהמשתמש רוצה חסר. get_user_businesses
        # מחזיר [] מהר למשתמש בלי תיק, כך שאין עומס מיותר על המקרה הפשוט.
        businesses = get_user_businesses(long_token)
        if businesses:
            logger.info(
                "Meta OAuth /me/businesses: count=%d ids=%s",
                len(businesses),
                [b["id"][:6] + "..." for b in businesses[:5]],
            )
            # כל תיק נשלף בנפרד עם try/except כך שתיק תקול לא יעצור את השאר.
            # merge_pages_by_id עושה dedup לפי id (חוצה-תיקים וגם מול /me/accounts)
            # ומעדיף גרסה עם token כשיש כפילות.
            for biz in businesses:
                try:
                    pages = merge_pages_by_id(
                        pages, list_business_pages(biz["id"], long_token)
                    )
                except Exception:
                    logger.exception(
                        "Meta OAuth business-pages fallback נכשל לתיק %s",
                        biz["id"][:6] + "...",
                    )
            logger.info(
                "Meta OAuth after business merge: total=%d; with_tokens=%d",
                len(pages),
                sum(1 for p in pages if p.get("access_token")),
            )

        # אם אחרי הכל אין עמוד **עם token** (לא רק "אין עמוד") — מצב כשל.
        # שולפים דיאגנוסטיקה כדי לדעת למה: הרשאות granted/declined ותקפות
        # הטוקן (/me). בלי זה לא ברור אם הבעיה היא הרשאה שנדחתה, טוקן פגום,
        # או משהו אחר. ומנסים fallback אחרון — nested expansion, שלפעמים
        # עוקף edge שקט של /me/accounts ב-FB Login for Business.
        if not _any_page_with_token(pages):
            perms = get_user_permissions(long_token)
            logger.info(
                "Meta OAuth permissions inspection: granted=%s; declined=%s",
                perms["granted"], perms["declined"],
            )
            user = get_user_info(long_token)
            if user.get("id"):
                logger.info(
                    "Meta OAuth /me check: user_id_prefix=%s",
                    user["id"][:6] + "...",
                )
            else:
                logger.warning(
                    "Meta OAuth /me check: הטוקן לא מחזיר user — בעיה בטוקן"
                )

            nested_pages = list_user_pages_nested(long_token)
            logger.info(
                "Meta OAuth /me?fields=accounts fallback: count=%d",
                len(nested_pages),
            )
            pages = merge_pages_by_id(pages, nested_pages)

        if not pages:
            # שים לב: אין כאן ודאות *למה* הרשימה ריקה (אין עמוד / עמוד תחת
            # תיק עסקי בלי הרשאה / הרשאה שנדחתה). ההודעה מכוונת לתרחיש
            # הנפוץ (עמוד תחת Business Portfolio) בלי לטעון שקר ("אינך מנהל
            # עמוד"). האבחון המדויק נמצא בלוגים (granted/declined, businesses,
            # fallback counts) שנכתבו למעלה.
            flash(
                "לא נמצאו עמודים לחיבור. אם העמוד שלך מנוהל תחת תיק עסקי "
                "(Business Portfolio / Meta Business Suite), ודא שבמסך "
                "ההרשאות של פייסבוק אישרת גישה לתיק העסקי ולעמוד שבתוכו. "
                "אם רק התנתקת והתחברת מחדש — נסה להסיר את האפליקציה ב-"
                "facebook.com/settings?tab=business_tools ולחבר שוב. "
                "אם הבעיה נמשכת, בדוק בלוגים את שורות 'Meta OAuth' לאבחון.",
                "warning",
            )
            return redirect(url_for("meta_setup"))

        # רק עמודים עם page tokens תקפים
        pages_with_tokens = [p for p in pages if p.get("access_token")]
        if not pages_with_tokens:
            flash(
                "מטא לא החזירה page access tokens. ייתכן שחסרות הרשאות "
                "(pages_show_list / pages_manage_metadata / business_management) "
                "או שלא אישרת את הגישה לעמוד במסך ההרשאות.",
                "danger",
            )
            return redirect(url_for("meta_setup"))

        # tokens נשמרים ב-cache פנים-תהליכי (לא בעוגייה). בצד ה-session
        # שומרים רק nonce + id+name לרינדור הרשימה.
        nonce = _cache_pending_pages(pages_with_tokens)
        session["meta_oauth_pending_nonce"] = nonce
        session["meta_oauth_pending_pages"] = [
            {"id": p.get("id"), "name": p.get("name", "")}
            for p in pages_with_tokens
        ]

        return redirect(url_for("meta_setup"))

    @app.route("/admin/meta/select-page", methods=["POST"])
    @login_required
    def meta_select_page():
        """המשתמש בחר עמוד מתוך הרשימה שהוצגה אחרי callback.

        כאן מתבצעת השמירה הסופית: שליפת IG מקושר, רישום ל-webhook,
        כתיבה ל-DB.
        """
        page_id = request.form.get("page_id", "")
        nonce = session.get("meta_oauth_pending_nonce", "")
        match = _get_pending_page(nonce, page_id) if nonce else None
        if not match:
            flash(
                "העמוד שנבחר לא נמצא או שתוקף הבקשה פג. נסו שוב.",
                "danger",
            )
            return redirect(url_for("meta_setup"))

        page_token = match["access_token"]
        page_name = match.get("name", "")

        try:
            ig = get_ig_business_account(page_id, page_token)
        except MetaGraphError as e:
            # IG אופציונלי — אם השליפה נכשלה, ממשיכים בלי
            logger.warning("get_ig_business_account נכשל (ממשיכים): %s", e)
            ig = None

        try:
            subscribe_page_to_webhook(page_id, page_token)
        except MetaGraphError as e:
            logger.error("subscribe_page_to_webhook נכשל: %s", e, exc_info=True)
            flash(
                f"חיבור OAuth הצליח אבל רישום webhook נכשל: {e}. "
                "הודעות לא יגיעו עד שתחבר מחדש.",
                "danger",
            )
            return redirect(url_for("meta_setup"))

        db.upsert_meta_credentials(
            page_id=page_id,
            access_token=page_token,
            page_name=page_name,
            ig_business_account_id=(ig or {}).get("id", ""),
            ig_username=(ig or {}).get("username", ""),
        )
        # ניקוי session + cache
        _drop_pending(nonce)
        session.pop("meta_oauth_pending_pages", None)
        session.pop("meta_oauth_pending_nonce", None)

        # ig['username'] לפעמים ריק (חשבון IG מקושר אבל המשתמש לא חשף
        # username דרך ה-API). מציגים פרטי IG רק אם יש username אמיתי
        # להציג; אחרת מציינים שיש חיבור IG בלי לקרוא לו בשם.
        ig_username = (ig or {}).get("username", "") if ig else ""
        if ig and ig_username:
            ig_note = f" + Instagram (@{ig_username})"
        elif ig:
            ig_note = " + Instagram"
        else:
            ig_note = ""
        flash(
            f"עמוד {page_name} חובר בהצלחה{ig_note}.",
            "success",
        )
        return redirect(url_for("meta_setup"))

    @app.route("/admin/meta/cancel", methods=["POST"])
    @login_required
    def meta_cancel_pending():
        """ביטול בחירת עמוד שטרם הושלמה."""
        nonce = session.pop("meta_oauth_pending_nonce", "")
        if nonce:
            _drop_pending(nonce)
        session.pop("meta_oauth_pending_pages", None)
        flash("בחירת העמוד בוטלה.", "info")
        return redirect(url_for("meta_setup"))

    @app.route("/admin/meta/disconnect/<page_id>", methods=["POST"])
    @login_required
    def meta_disconnect(page_id: str):
        """ניתוק עמוד — מבטל subscription + מוחק מ-DB."""
        cred = db.get_meta_credentials_by_page_id(page_id)
        if not cred:
            flash("העמוד לא נמצא ברשימה המחוברים.", "warning")
            return redirect(url_for("meta_setup"))

        # מנסים לבטל subscription מול מטא — אם נכשל (token פג, וכו'),
        # ממשיכים למחיקה לוקאלית. עדיף DB נקי גם אם מטא לא מסונכרנת.
        try:
            unsubscribe_page_from_webhook(page_id, cred["access_token"])
        except MetaGraphError as e:
            logger.warning(
                "unsubscribe_page_from_webhook נכשל (ממשיכים למחיקה לוקאלית): %s", e
            )

        db.delete_meta_credentials(page_id)
        flash(f"עמוד {cred.get('page_name') or page_id} נותק.", "success")
        return redirect(url_for("meta_setup"))
