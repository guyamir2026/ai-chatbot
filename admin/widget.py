"""Widget ציבורי להטמעה באתר חיצוני.

המודול מספק שלוש routes:
- ``GET  /widget/embed.js``   — סקריפט IIFE עצמאי שיוצר launcher + חלון צ'אט
- ``POST /widget/api/chat``   — endpoint אנונימי לשאילתות LLM (CORS + rate limit)
- ``GET  /widget/demo``       — עמוד הדגמה ציבורי

עיצוב:
- אין ``user_id``, אין שמירת שיחה ב-DB, אין live chat.
- היסטוריית השיחה מוחזקת ב-localStorage של הדפדפן (עד 20 הודעות) ונשלחת חזרה.
- משתמש בצינור RAG הקיים דרך ``generate_answer(... channel="widget")``.

הקוד הזה רץ בתוך אפליקציית האדמין (Flask). שלוש ה-routes הציבוריות פטורות
מ-``login_required`` ומ-CSRF, עם CORS allowlist ו-rate limit פר-IP.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from typing import Optional

from flask import Flask, Response, abort, jsonify, request

import config
from llm import (
    generate_answer,
    strip_source_citation,
    strip_telegram_html_tags,
    strip_whatsapp_markdown,
)
from core.message_processor import (
    extract_lead_from_response,
    strip_handoff_marker,
    strip_lead_marker,
)

logger = logging.getLogger(__name__)

__all__ = [
    "register_widget_routes",
    "register_widget_admin_routes",
]


# ─── Rate limiting ───────────────────────────────────────────────────────────
# dict בזיכרון פר-תהליך — לא משותף בין workers (תהליך יחיד היום). המפתח:
# (tenant, ip) — מכסה נפרדת לכל עסק (multi-tenant שלב 2).
_widget_message_log: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_WIDGET_LOG_MAX_IPS = 1000
_WIDGET_WINDOW_SECONDS = 3600

_WIDGET_MAX_MESSAGE_LENGTH = 1000
_WIDGET_MAX_HISTORY = 20
_WIDGET_FALLBACK_ANSWER = (
    "אני מצטער, נתקלתי בבעיה זמנית. אפשר לנסות שוב בעוד רגע, "
    "או לפנות אלינו דרך הערוצים האחרים שמופיעים באתר."
)


def _get_max_messages_per_window() -> int:
    """תקרת הודעות פר-IP בחלון של שעה."""
    return _WIDGET_MAX_MESSAGES


_WIDGET_MAX_MESSAGES = int(os.getenv("WIDGET_RATE_LIMIT_PER_HOUR", "30") or "30")


def _rl_key(ip: str) -> tuple[str, str]:
    """מפתח ה-rate limit: (tenant, ip) — מכסה נפרדת לכל עסק."""
    from tenancy import get_current_tenant

    return (get_current_tenant(), ip)


def _check_widget_rate_limit(ip: str) -> bool:
    """מחזיר ``True`` אם ה-IP חרג מהמכסה — חוסמים את הבקשה.

    שים לב: אסור לעשות short-circuit על ``timestamps is None`` בלי לבדוק
    את המגבלה. אם מגדירים ``WIDGET_RATE_LIMIT_PER_HOUR=0`` (כיבוי מוחלט
    של ה-API), ההודעה הראשונה מכל IP תיעקוף את ההגבלה — באג שדווח.
    """
    now = time.time()
    cutoff = now - _WIDGET_WINDOW_SECONDS
    key = _rl_key(ip)
    timestamps = _widget_message_log.get(key) or []
    # ניקוי timestamps ישנים בכל בדיקה
    fresh = [t for t in timestamps if t >= cutoff]
    if fresh:
        _widget_message_log[key] = fresh
    elif key in _widget_message_log:
        _widget_message_log.pop(key, None)
    return len(fresh) >= _get_max_messages_per_window()


def _record_widget_message(ip: str) -> None:
    """מתעד הודעה חדשה מ-IP נתון, עם LRU eviction."""
    now = time.time()
    key = _rl_key(ip)
    timestamps = _widget_message_log.get(key)
    if timestamps is None:
        timestamps = []
    timestamps.append(now)
    _widget_message_log[key] = timestamps
    _widget_message_log.move_to_end(key)
    while len(_widget_message_log) > _WIDGET_LOG_MAX_IPS:
        _widget_message_log.popitem(last=False)


def _resolve_widget_tenant(widget_key):
    """resolve של ה-tenant לפי מפתח ה-widget (spec 6.4).

    בלי מפתח — ה-tenant של ברירת המחדל (התנהגות legacy). מפתח לא רשום
    מחזיר None והקורא עונה 404.
    """
    if not widget_key:
        from tenancy import DEFAULT_TENANT

        return DEFAULT_TENANT
    from control_plane import resolve_route

    return resolve_route("widget_key", widget_key)


# ─── CORS ────────────────────────────────────────────────────────────────────
def _get_allowed_origins() -> list[str]:
    """רשימת origins מורשים מ-``WIDGET_ALLOWED_ORIGINS`` (פסיקים).

    ריק = ``*`` (allow-all). בייצור עדיף להגדיר רשימה מפורשת.
    """
    raw = os.getenv("WIDGET_ALLOWED_ORIGINS", "") or ""
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _origin_allowed(origin: Optional[str]) -> Optional[str]:
    """מחזיר את ה-origin להחזרה ב-header, או ``None`` אם נדחה."""
    allowed = _get_allowed_origins()
    if not allowed:
        # אין allowlist — פתוח לכולם
        return origin or "*"
    if origin and origin.rstrip("/") in allowed:
        return origin.rstrip("/")
    return None


def _apply_cors(resp: Response) -> Response:
    """מוסיף CORS headers ל-response רק אם ה-origin מורשה."""
    origin = request.headers.get("Origin")
    allowed = _origin_allowed(origin)
    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = allowed
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _client_ip() -> str:
    """מחזיר את ה-IP של הלקוח, גם מאחורי proxy אמין כמו Render.

    ⚠️ אבטחה: ``X-Forwarded-For`` הוא ``client_ip, proxy1, proxy2, ...``.
    הערך הראשון נכתב על ידי הלקוח עצמו וניתן לזיוף — אסור להשתמש בו
    ל-rate limiting. ב-Render (וברוב ה-proxies האמינים) ה-proxy שלנו
    מוסיף את ה-IP שהוא ראה ב-TCP בסוף השרשרת. לכן אנחנו לוקחים את
    הערך האחרון, שהוא בלתי-ניתן-לזיוף תחת ההנחה שיש hop proxy אמין יחיד.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "unknown"


def _sanitize_history(raw) -> list[dict]:
    """מסנן רשימת היסטוריה לפורמט שצינור ה-RAG מצפה לו.

    מקבל רק רשומות עם ``role ∈ {'user', 'assistant'}`` ו-``message`` כמחרוזת.
    זורק כל ניסיון להזריק ``role: 'system'`` או שדות נוספים.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for item in raw[-_WIDGET_MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        message = item.get("message")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(message, str) or not message.strip():
            continue
        cleaned.append({
            "role": role,
            "message": message[:_WIDGET_MAX_MESSAGE_LENGTH],
        })
    return cleaned


def _clean_widget_answer(answer: str) -> str:
    """מנקה תשובת LLM לפני שליחה ל-widget.

    1. מסיר ``[HANDOFF]`` אם איכשהו ה-LLM הוסיף אותו (הפרומפט אומר במפורש לא).
    2. מסיר ``[LEAD]`` ואת בלוק השדות שאחריו — הטוקן והפרטים הם סיגנל
       פנימי לשרת ואסור שיגיעו ללקוח.
    3. מסיר ציטוטי מקור — גם ``Source:``/``מקור:`` וגם ``[Category — description]``.
       מאצילים ל-``llm.strip_source_citation`` שמטפל בשני הפורמטים, במקום
       לשכפל רגקס שיכול לפספס פורמט.
    4. מסיר תגי HTML של טלגרם — הצד-לקוח משתמש ב-textContent.
    5. מסיר מרקדאון של WhatsApp — אותו טעם.
    """
    text = strip_handoff_marker(answer or "")
    text = strip_lead_marker(text)
    text = strip_source_citation(text)
    text = strip_telegram_html_tags(text)
    text = strip_whatsapp_markdown(text)
    return text.strip()


def _capture_widget_lead(lead: dict, history: list[dict], current_user_message: str) -> None:
    """שומר ליד ב-agent_requests ושולח התראה לבעל העסק.

    user_id מבוסס על הטלפון עם prefix ``widget:`` כדי שיהיה uniqueness
    סביר (ליד אחד ל-phone) ולא יתנגש עם user_ids של טלגרם/וואטסאפ.

    **חוזה:** הפונקציה לעולם לא זורקת חריגה. כל כשל (DB, build summary,
    התראה) נבלע ומתועד בלוג. הסיבה — היא נקראת באותו try-block של
    תשובת ה-LLM, וכל חריגה שתזלוג תגרום ללקוח לקבל ``_WIDGET_FALLBACK_ANSWER``
    במקום התשובה האמיתית. עוטפים את כל הגוף ב-try כדי שגם כשלים
    לא-צפויים (מעט סבירים, אבל קיימים) לא יחרגו.
    """
    try:
        try:
            import database as db
        except Exception:
            logger.error("widget lead capture: failed to import database", exc_info=True)
            return

        summary = _build_lead_summary(history, current_user_message, lead)
        user_id = f"widget:{lead['phone']}"
        try:
            request_id = db.create_agent_request(
                user_id=user_id,
                username=lead["name"],
                message=summary,
                telegram_username="",
                channel="widget",
            )
        except Exception:
            logger.error("widget lead capture: DB write failed", exc_info=True)
            return

        try:
            _notify_owner_widget_lead(lead, request_id)
        except Exception:
            logger.error("widget lead capture: owner notify failed", exc_info=True)
    except Exception:
        # רשת ביטחון אחרונה — קריסה לא צפויה (ב-_build_lead_summary או
        # בכל מקום אחר שלא עוטפנו במפורש). אסור להפיל את התשובה ללקוח.
        logger.error("widget lead capture: unexpected failure", exc_info=True)


def _build_lead_summary(history: list[dict], current_user_message: str, lead: dict) -> str:
    """בונה תקציר טקסטואלי של השיחה לשמירה ב-agent_requests.message.

    כולל את שם המבקר, הטלפון, ואחרונות 6 הודעות מהשיחה כדי שבעל העסק
    יבין על מה הפנייה לפני שהוא מתקשר חזרה.
    """
    lines = [
        f"שם: {lead['name']}",
        f"טלפון: {lead['phone']}",
        "",
        "תקציר השיחה (אחרונות):",
    ]
    recent = list(history[-6:]) + [{"role": "user", "message": current_user_message}]
    for h in recent:
        prefix = "מבקר" if h.get("role") == "user" else "בוט"
        msg = (h.get("message") or "").strip()
        if not msg:
            continue
        # חיתוך הודעה ארוכה כדי שהתקציר לא יתפוצץ
        if len(msg) > 250:
            msg = msg[:250] + "…"
        lines.append(f"  {prefix}: {msg}")
    return "\n".join(lines)


def _notify_owner_widget_lead(lead: dict, request_id: int) -> None:
    """שולח לבעל העסק התראה על ליד חדש שהגיע מה-widget.

    בוחר ערוץ אוטומטית: טלגרם אם ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_OWNER_CHAT_ID``
    מוגדרים, אחרת WhatsApp אם ``OWNER_WHATSAPP_NUMBER`` מוגדר. אם שני
    הערוצים לא מוגדרים — לוג ויציאה שקטה (הליד עדיין נשמר ב-DB).

    כשל בשליחה לא מפיל את הזרימה — הליד כבר נשמר ב-DB וגם בלי התראה
    בעל העסק יוכל לראות אותו ב-/requests.
    """
    panel_link = ""
    admin_url = (getattr(config, "ADMIN_URL", "") or "").rstrip("/")
    if admin_url:
        panel_link = f"\n\n🔗 {admin_url}/requests"

    text = (
        f"📩 בקשת נציג חדשה #{request_id} — מהאתר (Widget)\n"
        f"לקוח: {lead['name']}\n"
        f"טלפון: {lead['phone']}"
        f"{panel_link}"
    )

    try:
        from live_chat_service import (
            send_telegram_message,
            send_whatsapp_message,
        )
    except Exception:
        logger.error("widget lead notify: failed to import senders", exc_info=True)
        return

    telegram_owner = getattr(config, "TELEGRAM_OWNER_CHAT_ID", "") or ""
    telegram_token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
    owner_whatsapp = getattr(config, "OWNER_WHATSAPP_NUMBER", "") or ""

    # עוקבים אילו ערוצים ניסינו, כדי שהלוג בסוף יבחין בין
    # "אין שום ערוץ מוגדר" (operator לא הגדיר כלום) לבין
    # "ניסינו ונכשלנו" (תקלה זמנית — כדאי לבדוק).
    attempted: list[str] = []

    if telegram_token and telegram_owner:
        attempted.append("telegram")
        if send_telegram_message(telegram_owner, text):
            return
        logger.warning("widget lead notify: telegram failed")

    if owner_whatsapp:
        attempted.append("whatsapp")
        if send_whatsapp_message(owner_whatsapp, text):
            return
        logger.warning("widget lead notify: whatsapp failed")

    if not attempted:
        logger.warning(
            "widget lead notify: no notification channel configured "
            "(set TELEGRAM_OWNER_CHAT_ID or OWNER_WHATSAPP_NUMBER)"
        )
    else:
        logger.warning(
            "widget lead notify: all attempted channels failed: %s",
            ", ".join(attempted),
        )


def _build_widget_config() -> dict:
    """מחזיר קונפיג שיוזרק ל-``embed.js`` כ-JSON.

    לא חושף סודות — רק שדות תצוגה. ``footer_*`` בנוי דינמית: אם הוגדר
    שם משתמש לבוט טלגרם נחזיר קישור ל-t.me; אחרת אם יש מספר WhatsApp
    נחזיר קישור ל-wa.me.

    הפונקציה רצה בתוך tenant_context (embed.js / demo), ולכן השם וזהות
    הערוץ נשלפים בזמן-ריצה פר-tenant — לא מ-env הגלובלי.
    """
    from tenancy import get_current_tenant
    from control_plane import get_tenant_channel_identity

    business_name = config.get_business_config().name or "העסק"
    identity = get_tenant_channel_identity(get_current_tenant())
    telegram_username = (identity.get("telegram_bot_username") or "").lstrip("@")
    whatsapp_number = (identity.get("whatsapp_number") or "").strip()

    footer_label = ""
    footer_url = ""
    if telegram_username:
        footer_label = "המשך בטלגרם"
        footer_url = f"https://telegram.me/{telegram_username}"
    elif whatsapp_number:
        # פורמט נפוץ: "whatsapp:+972..." → ניקוי לקבלת מספר נטו עבור wa.me
        digits = "".join(ch for ch in whatsapp_number if ch.isdigit())
        if digits:
            footer_label = "המשך ב-WhatsApp"
            footer_url = f"https://wa.me/{digits}"

    return {
        "businessName": business_name,
        "greeting": f"שלום! אני העוזר הדיגיטלי של {business_name}. איך אפשר לעזור?",
        "footerLabel": footer_label,
        "footerUrl": footer_url,
    }


def _widget_feature_enabled() -> bool:
    """האם הפיצ'ר 'widget' פעיל ב-subscription הנוכחי?

    ה-widget הוא חלק מחבילת "מקצועי" (premium). אם הפיצ'ר כבוי — ה-routes
    הציבוריות מחזירות 404 (כדי לא לחשוף את קיומה של היכולת ללקוחות שלא
    משלמים), והעמוד הפנימי בפאנל מציג מסך "feature locked" כמו broadcast.

    בדיקה דרך ``feature_flags.has_feature`` מאוחדת — אם הקריאה נכשלת,
    מגינים בצד הזהיר ומחזירים False.
    """
    try:
        import feature_flags
        return bool(feature_flags.has_feature("widget"))
    except Exception:
        logger.error("widget feature check failed", exc_info=True)
        return False


# ─── Routes ──────────────────────────────────────────────────────────────────
def register_widget_routes(app: Flask, csrf) -> None:
    """רישום שלוש ה-routes הציבוריות של ה-widget.

    יש לקרוא מתוך ``create_admin_app`` אחרי ש-``csrf.init_app(app)`` רץ.
    ``csrf`` חייב להיות אובייקט ``CSRFProtect`` כדי שנוכל להחיל ``@csrf.exempt``.

    כל ה-routes מחזירות 404 כשפיצ'ר ה-widget כבוי (לקוחות basic/advanced),
    כדי להסתיר את היכולת מי שלא בחבילת "מקצועי".
    """

    @app.route("/widget/embed.js", methods=["GET"])
    def widget_embed_js():
        from tenancy import tenant_context

        widget_key = (request.args.get("k") or "").strip() or None
        tenant = _resolve_widget_tenant(widget_key)
        if tenant is None:
            return Response("", status=404)
        with tenant_context(tenant):
            if not _widget_feature_enabled():
                return Response("", status=404)
            widget_cfg = _build_widget_config()
            if widget_key:
                # המפתח חוזר בכל קריאת chat — כך ה-API יודע לאיזה tenant
                widget_cfg["widgetKey"] = widget_key
            config_json = json.dumps(widget_cfg, ensure_ascii=False)
        body = _WIDGET_JS_TEMPLATE.replace("__CONFIG_JSON__", config_json)
        resp = Response(body, mimetype="application/javascript; charset=utf-8")
        resp.headers["Cache-Control"] = "public, max-age=300"
        # הסקריפט נטען מאתרים זרים — חייב CORS פתוח לקריאת GET.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.route("/widget/api/chat", methods=["POST", "OPTIONS"])
    @csrf.exempt
    def widget_api_chat():
        from tenancy import tenant_context

        # Preflight — ה-JS מוסיף ?k=<key> ל-URL של ה-fetch, כך שגם OPTIONS
        # (שאין לו payload) יודע לאיזה tenant לבדוק את שער הפיצ'ר. פיצ'ר
        # כבוי ⇒ 404 גם ל-OPTIONS — לא מסגירים את קיום ה-endpoint ללקוחות
        # בלתי-משלמים (ההתנהגות המקורית, עכשיו פר-tenant).
        if request.method == "OPTIONS":
            pre_key = (request.args.get("k") or "").strip() or None
            pre_tenant = _resolve_widget_tenant(pre_key)
            if pre_tenant is None:
                return Response("", status=404)
            with tenant_context(pre_tenant):
                if not _widget_feature_enabled():
                    return Response("", status=404)
            resp = Response(status=204)
            return _apply_cors(resp)

        payload = request.get_json(silent=True) or {}
        widget_key = (
            (payload.get("key") if isinstance(payload.get("key"), str) else "")
            or request.args.get("k", "")
        ).strip() or None
        tenant = _resolve_widget_tenant(widget_key)
        if tenant is None:
            return Response("", status=404)
        with tenant_context(tenant):
            return _widget_api_chat_impl(payload)

    def _widget_api_chat_impl(payload):
        # פיצ'ר כבוי (לא בחבילת "מקצועי") — מחזירים 404 כדי לא לאשר
        # ללקוחות בלתי-משלמים שה-API קיים בכלל. נבדק תחת ה-tenant context
        # — החבילה של ה-tenant עצמו.
        if not _widget_feature_enabled():
            return Response("", status=404)

        # אכיפת CORS allowlist על POST: אם הוגדרה רשימה וה-origin לא בתוכה,
        # חוסמים לפני שמגיעים ל-LLM (מונע שימוש לרעה במשאבי OpenAI).
        origin = request.headers.get("Origin")
        if _get_allowed_origins() and not _origin_allowed(origin):
            logger.warning(
                "widget_api_chat: origin %r לא מורשה — חוסמים", origin,
            )
            return _apply_cors(jsonify({"error": "origin_not_allowed"})), 403

        ip = _client_ip()
        if _check_widget_rate_limit(ip):
            logger.warning("widget_api_chat: rate limit ל-IP %s", ip)
            resp = jsonify({
                "answer": "יותר מדי הודעות בזמן קצר — נסו שוב בעוד כמה דקות.",
                "sources": [],
                "rate_limited": True,
            })
            return _apply_cors(resp), 429

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return _apply_cors(jsonify({"error": "empty_message"})), 400
        message = message.strip()[:_WIDGET_MAX_MESSAGE_LENGTH]

        history = _sanitize_history(payload.get("history"))

        # רישום ההודעה לפני קריאת ה-LLM כדי שגם בקשות שכושלות יספרו במכסה.
        _record_widget_message(ip)

        try:
            result = generate_answer(
                user_query=message,
                conversation_history=history,
                user_id=None,
                username=None,
                channel="widget",
            )
            raw_answer = result.get("answer") or ""
            sources = result.get("sources") or []

            # חילוץ ליד אם ה-LLM סימן בטוקן [LEAD] עם name+phone תקינים.
            # _capture_widget_lead שומר ב-agent_requests ושולח התראה.
            # שתי שכבות של try/except: גם בתוך הפונקציה וגם כאן —
            # ככה גם אם משהו ב-extract_lead_from_response עצמו זורק
            # (שלא אמור) או ב-capture, התשובה ללקוח לא נפגעת.
            try:
                lead = extract_lead_from_response(raw_answer)
                if lead:
                    _capture_widget_lead(lead, history, message)
            except Exception:
                logger.error("widget lead extraction/capture failed", exc_info=True)

            answer = _clean_widget_answer(raw_answer)
            if not answer:
                answer = _WIDGET_FALLBACK_ANSWER
        except Exception:
            logger.error("widget_api_chat: כשל בקריאת generate_answer", exc_info=True)
            answer = _WIDGET_FALLBACK_ANSWER
            sources = []

        return _apply_cors(jsonify({"answer": answer, "sources": sources}))

    @app.route("/widget/demo", methods=["GET"])
    def widget_demo():
        from tenancy import tenant_context

        # ?k=<widget_key> — כמו ב-embed.js: בלי מפתח → default (legacy);
        # מפתח לא רשום → 404. כך הדמו של כל tenant מציג את השם/ההגדרות שלו.
        widget_key = (request.args.get("k") or "").strip() or None
        tenant = _resolve_widget_tenant(widget_key)
        if tenant is None:
            return Response("", status=404)
        # אסור ב-render_template_string — context processor של האדמין
        # ניגש ל-DB ב-_inject_globals ויקרוס אם DB לא מאותחל. במקום
        # זה: HTML קבוע עם .format() (סוגריים מסולסלים ב-CSS = {{...}}).
        # ⚠️ אבטחה: שם העסק מגיע מבעל-העסק/admin, אבל הדף הזה ציבורי.
        # סניטציה דרך html.escape מבטיחה שכל תו מיוחד יוצג כטקסט ולא
        # כ-HTML/script.
        # ⚠️ פרוטוקול: מעבירים URL יחסי (/widget/embed.js) ולא absolute.
        # אחרת אם הדף נטען ב-HTTPS אבל request.host_url החזיר http:// (מצב
        # נפוץ ב-Render כש-ProxyFix לא מוגדר), הדפדפן חוסם את ה-script
        # כ-mixed-content בשקט והכפתור הצף פשוט לא מופיע.
        import html as _html
        from urllib.parse import quote as _quote

        with tenant_context(tenant):
            if not _widget_feature_enabled():
                return Response("", status=404)
            widget_cfg = _build_widget_config()
        embed_src = "/widget/embed.js"
        if widget_key:
            embed_src += f"?k={_quote(widget_key)}"
        body = _WIDGET_DEMO_TEMPLATE.format(
            business_name=_html.escape(widget_cfg["businessName"], quote=True),
            embed_url=embed_src,
        )
        return Response(body, mimetype="text/html; charset=utf-8")


def register_widget_admin_routes(app: Flask, login_required) -> None:
    """רושם את עמוד הוראות ההטמעה הפנימי לבעל העסק.

    מופיע ב-sidebar ככניסה ייעודית. דורש ``login_required`` (closure
    מתוך ``create_admin_app``) כי הוא חי תחת מסך כניסת האדמין.
    """
    from flask import render_template

    @app.route("/widget-embed", methods=["GET"], endpoint="widget_embed_admin")
    @login_required
    def widget_embed_admin():
        from urllib.parse import quote as _quote
        from tenancy import DEFAULT_TENANT, get_current_tenant

        host = request.host_url.rstrip("/")
        embed_url = f"{host}/widget/embed.js"
        demo_url = f"{host}/widget/demo"

        # פר-tenant: קטע ההטמעה חייב לכלול ?k=<widget_key>, אחרת ה-widget
        # באתר הלקוח ידבר עם ה-tenant של ברירת המחדל. המפתח נוצר כאן
        # בביקור הראשון (אותו דפוס auto-provision כמו מפתחות ה-webhook).
        # ה-default נשאר בלי מפתח — התנהגות legacy.
        tenant = get_current_tenant()
        if tenant != DEFAULT_TENANT:
            import control_plane as _cp

            widget_key = _cp.get_tenant_route_key(tenant, "widget_key")
            if not widget_key:
                widget_key = _cp.generate_route_key()
                _cp.set_route("widget_key", widget_key, tenant)
            embed_url += f"?k={_quote(widget_key)}"
            demo_url += f"?k={_quote(widget_key)}"

        widget_cfg = _build_widget_config()
        return render_template(
            "widget_embed.html",
            embed_url=embed_url,
            demo_url=demo_url,
            widget_config=widget_cfg,
        )


# ─── ה-JavaScript של ה-widget ────────────────────────────────────────────────
# כתוב כ-IIFE עצמאי. ``__CONFIG_JSON__`` מוחלף בעת ההגשה ב-JSON אמיתי.
# הערות פנימיות בעברית מתועדות ב-CLAUDE.md.
_WIDGET_JS_TEMPLATE = r"""
(function() {
  'use strict';
  if (window.__AI_CHATBOT_WIDGET_LOADED__) return;
  window.__AI_CHATBOT_WIDGET_LOADED__ = true;

  var CONFIG = __CONFIG_JSON__;

  // ── זיהוי ה-origin של הסקריפט ──
  // currentScript זמין רק במהלך הטעינה הסינכרונית; שומרים אותו מיד.
  var thisScript = document.currentScript;
  var apiBase = (function() {
    try {
      if (thisScript && thisScript.src) {
        var u = new URL(thisScript.src);
        return u.origin;
      }
    } catch (e) {}
    return '';
  })();

  // ── סניטציה של ערכי data-* ──
  function clamp(n, min, max) {
    n = parseInt(n, 10);
    if (isNaN(n)) return null;
    return Math.max(min, Math.min(max, n));
  }
  function safeColor(v) {
    if (typeof v !== 'string') return null;
    v = v.trim();
    if (!v) return null;
    if (/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(v)) return v;
    if (/^rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(,\s*[\d.]+\s*)?\)$/.test(v)) return v;
    if (/^[a-zA-Z]{3,20}$/.test(v)) return v;
    return null;
  }
  function safeFont(v) {
    if (typeof v !== 'string') return null;
    if (v.length > 100) return null;
    if (!/^[a-zA-Z0-9֐-׿\s\-_.,'"]+$/.test(v)) return null;
    return v;
  }
  function safeText(v, max) {
    if (typeof v !== 'string') return null;
    v = v.replace(/[<>]/g, '').trim();
    if (!v) return null;
    return v.slice(0, max);
  }
  function darken(hex) {
    if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
    var r = parseInt(hex.slice(1,3),16),
        g = parseInt(hex.slice(3,5),16),
        b = parseInt(hex.slice(5,7),16);
    r = Math.max(0, Math.round(r * 0.88));
    g = Math.max(0, Math.round(g * 0.88));
    b = Math.max(0, Math.round(b * 0.88));
    return '#' + [r,g,b].map(function(x){var s=x.toString(16);return s.length===1?'0'+s:s;}).join('');
  }

  // ── קריאת data-attributes מתוך תג הסקריפט ──
  var ds = (thisScript && thisScript.dataset) || {};
  var POSITION  = ['bottom-right','bottom-left','top-right','top-left'].indexOf(ds.position) >= 0 ? ds.position : 'bottom-right';
  var COLOR     = safeColor(ds.color)  || '#2563eb';
  var COLOR_HOVER = darken(COLOR);
  var BG        = safeColor(ds.bg)     || '#ffffff';
  var TEXT      = safeColor(ds.text)   || '#1f2937';
  var FONT      = safeFont(ds.font)    || 'system-ui, -apple-system, "Segoe UI", Heebo, Arial, sans-serif';
  var RADIUS    = clamp(ds.radius, 0, 32);  if (RADIUS === null) RADIUS = 16;
  var WIDTH     = clamp(ds.width, 280, 600); if (WIDTH === null) WIDTH = 360;
  var HEIGHT    = clamp(ds.height, 360, 800); if (HEIGHT === null) HEIGHT = 520;
  var ICON      = safeText(ds.icon, 8) || '\u{1F4AC}';
  var TITLE     = safeText(ds.title, 60) || CONFIG.businessName;
  var SUBTITLE  = safeText(ds.subtitle, 80) || 'נשמח לעזור';
  var AUTO_OPEN = ds.autoOpen === 'true';

  // ── יצירת style block עם CSS variables ──
  var style = document.createElement('style');
  style.textContent = [
    ':root {',
    '  --aicb-color: ' + COLOR + ';',
    '  --aicb-color-hover: ' + COLOR_HOVER + ';',
    '  --aicb-bg: ' + BG + ';',
    '  --aicb-text: ' + TEXT + ';',
    '  --aicb-font: ' + FONT + ';',
    '  --aicb-radius: ' + RADIUS + 'px;',
    '  --aicb-radius-sm: ' + Math.round(RADIUS * 0.6) + 'px;',
    '  --aicb-w: ' + WIDTH + 'px;',
    '  --aicb-h: ' + HEIGHT + 'px;',
    '}',
    '.aicb-launcher{position:fixed;width:60px;height:60px;border-radius:50%;background:var(--aicb-color);color:#fff;border:none;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.18);font-size:28px;display:flex;align-items:center;justify-content:center;z-index:2147483646;transition:transform .15s ease, background .15s ease;}',
    '.aicb-launcher:hover{background:var(--aicb-color-hover);transform:scale(1.05);}',
    '.aicb-window{position:fixed;width:var(--aicb-w);max-width:calc(100vw - 24px);height:var(--aicb-h);max-height:calc(100vh - 100px);background:var(--aicb-bg);color:var(--aicb-text);border-radius:var(--aicb-radius);box-shadow:0 12px 40px rgba(0,0,0,.22);display:none;flex-direction:column;overflow:hidden;font-family:var(--aicb-font);direction:rtl;z-index:2147483647;}',
    '.aicb-window.aicb-open{display:flex;}',
    '.aicb-header{background:var(--aicb-color);color:#fff;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;}',
    '.aicb-header-title{font-weight:700;font-size:15px;line-height:1.2;}',
    '.aicb-header-subtitle{font-size:12px;opacity:.85;margin-top:2px;}',
    '.aicb-close{background:transparent;border:none;color:#fff;font-size:22px;cursor:pointer;padding:0 4px;line-height:1;}',
    '.aicb-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:var(--aicb-bg);}',
    '.aicb-msg{max-width:85%;padding:9px 13px;border-radius:var(--aicb-radius-sm);font-size:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;}',
    '.aicb-msg.user{align-self:flex-start;background:var(--aicb-color);color:#fff;}',
    '.aicb-msg.assistant{align-self:flex-end;background:rgba(0,0,0,.06);color:var(--aicb-text);}',
    '.aicb-typing{align-self:flex-end;font-size:12px;color:#6b7280;padding:6px 10px;}',
    '.aicb-input-row{display:flex;gap:8px;padding:10px;border-top:1px solid rgba(0,0,0,.08);background:var(--aicb-bg);}',
    '.aicb-input{flex:1;border:1px solid rgba(0,0,0,.15);border-radius:var(--aicb-radius-sm);padding:9px 12px;font-family:inherit;font-size:14px;outline:none;background:#fff;color:#1f2937;}',
    '.aicb-input:focus{border-color:var(--aicb-color);}',
    '.aicb-send{background:var(--aicb-color);color:#fff;border:none;border-radius:var(--aicb-radius-sm);padding:0 16px;font-weight:600;font-size:14px;cursor:pointer;}',
    '.aicb-send:hover{background:var(--aicb-color-hover);}',
    '.aicb-send:disabled{opacity:.6;cursor:not-allowed;}',
    '.aicb-footer{font-size:11px;color:#6b7280;text-align:center;padding:6px 10px;border-top:1px solid rgba(0,0,0,.06);background:var(--aicb-bg);}',
    '.aicb-footer a{color:var(--aicb-color);text-decoration:none;}',
    '.aicb-footer a:hover{text-decoration:underline;}',
    '@media (max-width: 480px){',
    '  .aicb-window{width:calc(100vw - 16px);height:calc(100vh - 88px);max-height:calc(100vh - 88px);}',
    '}'
  ].join('\n');
  document.head.appendChild(style);

  // ── מיקום inline (לא class) — מאפשר להחיל על launcher ועל window נפרד ──
  function applyPosition(el, isLauncher) {
    var offsetMain = isLauncher ? '20px' : '90px';
    var offsetSide = '20px';
    if (POSITION === 'bottom-right') { el.style.bottom = offsetMain; el.style.right = offsetSide; el.style.top=''; el.style.left=''; }
    else if (POSITION === 'bottom-left') { el.style.bottom = offsetMain; el.style.left = offsetSide; el.style.top=''; el.style.right=''; }
    else if (POSITION === 'top-right') { el.style.top = offsetMain; el.style.right = offsetSide; el.style.bottom=''; el.style.left=''; }
    else if (POSITION === 'top-left') { el.style.top = offsetMain; el.style.left = offsetSide; el.style.bottom=''; el.style.right=''; }
  }

  // ── יצירת DOM ──
  var launcher = document.createElement('button');
  launcher.className = 'aicb-launcher';
  launcher.setAttribute('aria-label', 'פתח צ\'אט');
  launcher.textContent = ICON;
  applyPosition(launcher, true);

  var win = document.createElement('div');
  win.className = 'aicb-window';
  applyPosition(win, false);

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  var headerHtml = ''
    + '<div>'
    + '  <div class="aicb-header-title">' + escapeHtml(TITLE) + '</div>'
    + '  <div class="aicb-header-subtitle">' + escapeHtml(SUBTITLE) + '</div>'
    + '</div>'
    + '<button class="aicb-close" type="button" aria-label="סגור">×</button>';

  var header = document.createElement('div');
  header.className = 'aicb-header';
  header.innerHTML = headerHtml;

  var messagesEl = document.createElement('div');
  messagesEl.className = 'aicb-messages';

  var inputRow = document.createElement('div');
  inputRow.className = 'aicb-input-row';
  var inputEl = document.createElement('input');
  inputEl.className = 'aicb-input';
  inputEl.type = 'text';
  inputEl.placeholder = 'הקלד/י הודעה...';
  inputEl.maxLength = 1000;
  var sendBtn = document.createElement('button');
  sendBtn.className = 'aicb-send';
  sendBtn.type = 'button';
  sendBtn.textContent = 'שלח';
  inputRow.appendChild(inputEl);
  inputRow.appendChild(sendBtn);

  win.appendChild(header);
  win.appendChild(messagesEl);
  win.appendChild(inputRow);

  if (CONFIG.footerLabel && CONFIG.footerUrl) {
    var footer = document.createElement('div');
    footer.className = 'aicb-footer';
    var a = document.createElement('a');
    a.href = CONFIG.footerUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = CONFIG.footerLabel;
    footer.appendChild(a);
    win.appendChild(footer);
  }

  document.body.appendChild(launcher);
  document.body.appendChild(win);

  // ── ניהול state ──
  var STORAGE_KEY = 'aicb_history_v1';
  var SESSION_KEY = 'aicb_session_v1';
  var history = [];
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved) history = JSON.parse(saved) || [];
    if (!Array.isArray(history)) history = [];
  } catch (e) { history = []; }

  var sessionId = '';
  try {
    sessionId = localStorage.getItem(SESSION_KEY) || '';
    if (!sessionId) {
      sessionId = 'sess_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(SESSION_KEY, sessionId);
    }
  } catch (e) { sessionId = 'sess_' + Date.now(); }

  var resetGen = 0;
  var greetShown = false;

  function persistHistory() {
    try {
      // שומרים עד 20 פריטים אחרונים
      var trimmed = history.slice(-20);
      history = trimmed;
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    } catch (e) { /* localStorage חסום או מלא */ }
  }

  function fireEvent(name, detail) {
    try {
      window.dispatchEvent(new CustomEvent('aichatbot:' + name, { detail: detail || {} }));
    } catch (e) { /* ignore */ }
  }

  function renderMessage(role, text) {
    var div = document.createElement('div');
    div.className = 'aicb-msg ' + (role === 'user' ? 'user' : 'assistant');
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function renderHistoryFromStorage() {
    messagesEl.innerHTML = '';
    if (history.length === 0 && !greetShown && CONFIG.greeting) {
      renderMessage('assistant', CONFIG.greeting);
      greetShown = true;
    } else {
      for (var i = 0; i < history.length; i++) {
        renderMessage(history[i].role, history[i].message);
      }
    }
  }

  function open() {
    if (win.classList.contains('aicb-open')) return;
    win.classList.add('aicb-open');
    if (messagesEl.children.length === 0) renderHistoryFromStorage();
    setTimeout(function(){ inputEl.focus(); }, 50);
    fireEvent('open');
  }
  function close() {
    if (!win.classList.contains('aicb-open')) return;
    win.classList.remove('aicb-open');
    fireEvent('close');
  }
  function toggle() {
    if (win.classList.contains('aicb-open')) close(); else open();
  }
  function reset() {
    resetGen++;
    history = [];
    greetShown = false;
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
    messagesEl.innerHTML = '';
    if (CONFIG.greeting) {
      renderMessage('assistant', CONFIG.greeting);
      greetShown = true;
    }
    fireEvent('reset');
  }

  function sendMessage(externalText) {
    var fromExternal = arguments.length > 0;
    var text = fromExternal ? externalText : inputEl.value;
    text = (typeof text === 'string') ? text.trim() : '';
    if (!text) return;

    open();

    var userMsg = { role: 'user', message: text };
    history.push(userMsg);
    persistHistory();
    renderMessage('user', text);

    // typing נוצר *לפני* fireEvent כדי שאם listener קורא reset() —
    // ה-innerHTML='' של reset יחסל גם את typing. אחרת typing נשאר יתום.
    var typing = document.createElement('div');
    typing.className = 'aicb-typing';
    typing.textContent = '...';
    messagesEl.appendChild(typing);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    fireEvent('message', { role: 'user', text: text });

    if (!fromExternal) inputEl.value = '';
    sendBtn.disabled = true;
    inputEl.disabled = true;

    var myGen = resetGen;
    var payload = {
      message: text,
      history: history.slice(0, -1).slice(-20),
      session_id: sessionId,
      key: CONFIG.widgetKey || null,
    };

    var chatUrl = apiBase + '/widget/api/chat' +
      (CONFIG.widgetKey ? ('?k=' + encodeURIComponent(CONFIG.widgetKey)) : '');
    fetch(chatUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function(r) {
      return r.json().then(function(data) { return { ok: r.ok, status: r.status, data: data }; });
    }).then(function(res) {
      // remove() על אלמנט מנותק = no-op, לכן בטוח להפעיל לפני בדיקת הדור
      if (typing && typing.parentNode) typing.remove();
      if (myGen !== resetGen) return;
      sendBtn.disabled = false;
      inputEl.disabled = false;
      inputEl.focus();
      if (!res.ok) {
        fireEvent('error', { reason: 'http', status: res.status });
        renderMessage('assistant', (res.data && res.data.answer) || 'שגיאה זמנית. נסו שוב בעוד רגע.');
        return;
      }
      var answer = (res.data && res.data.answer) || '';
      if (!answer) return;
      var asst = { role: 'assistant', message: answer };
      history.push(asst);
      persistHistory();
      renderMessage('assistant', answer);
      fireEvent('message', { role: 'assistant', text: answer });
    }).catch(function(err) {
      if (typing && typing.parentNode) typing.remove();
      if (myGen !== resetGen) return;
      sendBtn.disabled = false;
      inputEl.disabled = false;
      fireEvent('error', { reason: 'network', status: 0 });
      renderMessage('assistant', 'תקלת רשת. בדקו את החיבור ונסו שוב.');
    });
  }

  // ── event listeners ──
  launcher.addEventListener('click', toggle);
  header.querySelector('.aicb-close').addEventListener('click', close);
  sendBtn.addEventListener('click', function() { sendMessage(); });
  inputEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // ── API גלובלי ──
  window.AIChatbot = {
    open: open,
    close: close,
    toggle: toggle,
    send: function(text) { sendMessage(text); },
    reset: reset,
    isOpen: function() { return win.classList.contains('aicb-open'); },
    version: '1.0',
  };

  fireEvent('ready');
  if (AUTO_OPEN) open();
})();
"""


# ─── עמוד ההדגמה הציבורי ─────────────────────────────────────────────────────
# דף נקי לבעל העסק לבדוק שה-widget אכן עובד — ללא תיעוד טכני (התיעוד
# נמצא בעמוד הפנימי /widget-embed). מציג רק כותרת קצרה + כפתור צף.
# שימו לב — מחרוזת רגילה (לא f-string) ולכן ``{{...}}`` ב-CSS עם זוג בודד.
_WIDGET_DEMO_TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>הדגמת ה-widget — {business_name}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <style>
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, Heebo, Arial, sans-serif;
      background: linear-gradient(180deg, #f1f5f9 0%, #e2e8f0 100%);
      color: #1f2937;
      line-height: 1.6;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      box-sizing: border-box;
    }}
    .hero {{
      max-width: 560px;
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 8px 32px rgba(15, 23, 42, .08);
      padding: 36px 32px;
      text-align: center;
    }}
    .hero .icon {{
      font-size: 48px;
      line-height: 1;
      margin-bottom: 12px;
    }}
    .hero h1 {{
      margin: 0 0 12px;
      font-size: 22px;
      color: #0f172a;
    }}
    .hero p {{
      margin: 0 0 10px;
      color: #475569;
      font-size: 15px;
    }}
    .hero .arrow {{
      margin-top: 18px;
      color: #94a3b8;
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="hero">
    <div class="icon">💬</div>
    <h1>כך ייראה ה-widget באתר של {business_name}</h1>
    <p>הכפתור הצף בפינה הוא ה-widget. לחצו עליו, שלחו הודעה, וודאו שהתשובות תקינות.</p>
    <p class="arrow">↘ הכפתור נמצא בפינה הימנית-תחתונה של הדף</p>
  </div>

  <script src="{embed_url}" async></script>
</body>
</html>
"""
