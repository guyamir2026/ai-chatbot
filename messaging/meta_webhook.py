"""
Meta DM Webhook — Flask Blueprint לקבלת הודעות נכנסות
מ-Instagram DM ו-Facebook Messenger.

שלב 1 של המימוש (ראה docs/meta_dm_spec.md):
1. אימות handshake של מטא ב-GET (`hub.challenge` + `hub.verify_token`).
2. אימות חתימת `X-Hub-Signature-256` ב-POST.
3. פענוח payload, זיהוי הערוץ (`ig` / `messenger`), ולוג של ההודעה.

**שלב זה לא שולח תשובות ולא כותב ל-DB.** מטרתו לוודא שצינור הקליטה
מקצה לקצה עובד מול Meta — verification ו-handshake תקינים, ושאנחנו
מקבלים את ההודעות במבנה הצפוי.

הקוד נרשם כ-Blueprint ב-admin/app.py רק אם `META_APP_SECRET` ו-
`META_VERIFY_TOKEN` מוגדרים.
"""

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from flask import Blueprint, Response, abort, request

from ai_chatbot.config import META_APP_SECRET, META_VERIFY_TOKEN

logger = logging.getLogger(__name__)

meta_bp = Blueprint("meta", __name__)


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    אימות חתימת `X-Hub-Signature-256` של מטא.

    מטא חותמת את ה-body עם HMAC-SHA256 ו-`META_APP_SECRET`,
    בפורמט `sha256=<hex>`. בלי אימות זה, כל אחד יכול לשלוח
    POSTים מזויפים ל-webhook.
    """
    if not META_APP_SECRET:
        logger.error("META_APP_SECRET לא מוגדר — לא ניתן לאמת חתימה")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header.split("=", 1)[1]
    computed = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    # compare_digest כדי להימנע מ-timing attack
    return hmac.compare_digest(expected_sig, computed)


def _channel_from_object(obj: str) -> Optional[str]:
    """
    `object` ב-payload מציין מאיזה ערוץ הגיעה ההודעה:
    - `page` ⇒ Facebook Messenger
    - `instagram` ⇒ Instagram DM

    מחזיר את ה-canonical channel names: `meta_msg` / `meta_ig`. אלה
    אותם שמות שמשמשים בכל הצינור — DB.users.channel, adapter, sender,
    _max_length_for_channel — כדי שלא ייווצרו פערים בין inbound ל-outbound.
    שאר הערכים לא מעניינים אותנו (page subscription וכו').
    """
    if obj == "page":
        return "meta_msg"
    if obj == "instagram":
        return "meta_ig"
    return None


def _extract_inbound_messages(payload: dict) -> list[dict]:
    """
    חילוץ הודעות נכנסות מ-payload של מטא.

    מבנה ה-payload (זהה ל-IG ול-Messenger):
        {
          "object": "page" | "instagram",
          "entry": [
            {
              "id": "<page_or_ig_id>",
              "messaging": [
                {
                  "sender": {"id": "<psid_or_igsid>"},
                  "recipient": {"id": "<page_id>"},
                  "timestamp": <ms>,
                  "message": {"mid": "...", "text": "..."}
                }
              ]
            }
          ]
        }

    מחזיר רשימה מנורמלת של הודעות לטיפול במעלה הזרם. עוטף כל יצירת
    פריט ב-try כדי שהודעה אחת תקולקלת לא תפיל את הצינור עבור היתר.

    הגנה דיפנסיבית מול payload פגום (JSON שאינו אובייקט, `entry` שאינו
    רשימה, וכו'). חייב להחזיר רשימה ריקה ולא לזרוק — הקורא מסתמך
    על זה כדי לעמוד בדרישת מטא להחזיר 200.
    """
    if not isinstance(payload, dict):
        return []
    channel = _channel_from_object(payload.get("object", ""))
    if channel is None:
        return []

    out: list[dict] = []
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return []
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                continue
            messaging = entry.get("messaging", []) or []
            if not isinstance(messaging, list):
                continue
            for event in messaging:
                if not isinstance(event, dict):
                    continue
                msg = event.get("message")
                if not isinstance(msg, dict) or msg.get("is_echo"):
                    # is_echo = הודעה שיצאה מאיתנו וחזרה כ-echo, מתעלמים.
                    continue
                out.append({
                    "channel": channel,
                    "page_or_ig_id": entry.get("id"),
                    "sender_id": (event.get("sender") or {}).get("id"),
                    "recipient_id": (event.get("recipient") or {}).get("id"),
                    "timestamp_ms": event.get("timestamp"),
                    "mid": msg.get("mid"),
                    "text": msg.get("text"),
                    "has_attachments": bool(msg.get("attachments")),
                })
        except Exception:
            logger.exception("שגיאה בפענוח entry של webhook מטא — מדלגים")
    return out


@meta_bp.route("/webhooks/meta", methods=["GET"])
def meta_verify():
    """
    handshake ראשוני של מטא. כשמוסיפים webhook ב-Meta Developers Portal,
    מטא שולחת GET עם:
        ?hub.mode=subscribe
        &hub.verify_token=<מה שהגדרנו ב-portal>
        &hub.challenge=<מחרוזת לאקו>

    אם ה-token תואם — מחזירים את ה-challenge בגוף עם 200.
    אחרת 403.
    """
    if not META_VERIFY_TOKEN:
        logger.error("META_VERIFY_TOKEN לא מוגדר — handshake לא יכול לעבור")
        abort(500)

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Meta webhook handshake הצליח")
        return Response(challenge, status=200, mimetype="text/plain")

    logger.warning("Meta webhook handshake נכשל: mode=%s token_match=%s",
                   mode, token == META_VERIFY_TOKEN)
    abort(403)


@meta_bp.route("/webhooks/meta", methods=["POST"])
def meta_inbound():
    """
    קבלת הודעות נכנסות מ-IG ו-Messenger.

    הזרימה:
    1. אימות חתימה (HMAC SHA-256 עם META_APP_SECRET).
    2. פענוח JSON.
    3. חילוץ הודעות מנורמלות.
    4. סינון entry לא מוכר (לא חיברנו דרך OAuth).
    5. עבור כל הודעה: לוג PII-safe + טיפול דרך `_handle_meta_message`
       (live_chat guard + RAG + שליחת תשובה).

    מטא דורשת תשובה מהירה (200) — אחרת מנסה שוב ובסוף מסמנת את ה-webhook
    כפגום. לכן כל עיבוד נכנס ל-try/except: כשל בהודעה אחת לא מונע 200,
    ולא עוצר את שאר ההודעות באותו payload.
    """
    raw_body = request.get_data(cache=True)
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_signature(raw_body, signature):
        logger.warning("Meta webhook: חתימה לא תקפה — דוחים")
        abort(403)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        logger.exception("Meta webhook: payload לא JSON תקין")
        return Response("OK", status=200)

    from tenancy import tenant_context

    messages = _extract_inbound_messages(payload)
    for m in messages:
        tenant = _resolve_entry_tenant(m["page_or_ig_id"])
        if tenant is None:
            logger.warning(
                "Meta inbound: entry לא מוכר — מתעלמים. channel=%s entry_hash=%s",
                m["channel"], _short_hash(m["page_or_ig_id"]),
            )
            continue

        # PII redaction: sender → hash; text → אורך בלבד.
        logger.info(
            "Meta inbound: channel=%s tenant=%s sender_hash=%s text_len=%s has_attachments=%s",
            m["channel"],
            tenant,
            _short_hash(m["sender_id"]),
            _safe_len(m["text"]),
            m["has_attachments"],
        )

        try:
            # כל entry מעובד תחת ה-tenant שלו — ה-credentials, ההיסטוריה
            # וה-RAG נקראים מקובץ ה-DB הנכון.
            with tenant_context(tenant):
                _handle_meta_message(m)
        except Exception:
            # לולאת I/O ארוכה — כשל בהודעה אחת לא עוצר את היתר
            # (כלל ה-CLAUDE.md על "לולאות I/O ארוכות").
            logger.exception(
                "_handle_meta_message נכשל ל-channel=%s sender_hash=%s",
                m["channel"], _short_hash(m["sender_id"]),
            )

    return Response("OK", status=200)


def _handle_meta_message(m: dict) -> None:
    """מטפל בהודעת מטא בודדת: בונה user_id מנורמל, מריץ צינור RAG,
    שולח תשובה. נקרא רק אחרי `_is_known_entry`.

    ה-payload המנורמל (m) מגיע מ-`_extract_inbound_messages`. שדות נדרשים:
        channel, sender_id, page_or_ig_id, text.

    אם text ריק (סטיקר, attachment בלבד) — מתעלם בשקט; אין מה לענות לו
    מבחינת RAG, וזה לא שגיאה.
    """
    from ai_chatbot import database as db
    from messaging.meta_adapter import to_internal_user_id

    channel = m["channel"]
    sender_id = m["sender_id"]
    text = m["text"] or ""
    asset_id = m["page_or_ig_id"]

    if not sender_id or not asset_id:
        logger.warning(
            "Meta inbound: שדות חסרים — sender_hash=%s entry_hash=%s",
            _short_hash(sender_id), _short_hash(asset_id),
        )
        return

    if not text.strip():
        # סטיקר/attachment בלבד — אין טקסט לעבד. שלב 5 יוסיף תשובה
        # סטנדרטית ("שלחו טקסט בבקשה"); כרגע פשוט מתעלמים.
        return

    user_id = to_internal_user_id(channel, sender_id)

    # ── live_chat guard ──────────────────────────────────────────────────
    # אם בעל העסק לוקח את השיחה ידנית, הבוט לא יגיב על RAG. שומרים
    # את ההודעה ב-DB כדי שהיסטוריה תהיה שלמה ויוצאים. (User אישר
    # שאין מסך הסכמה אז לא בודקים consent.)
    from ai_chatbot.live_chat_service import LiveChatService
    if LiveChatService.is_active(user_id):
        db.save_message(user_id, sender_id, "user", text, channel=channel)
        db.touch_live_chat(user_id)
        # התראת Web Push לבעל העסק — עובד גם כשהדשבורד סגור.
        try:
            from notifications.push_service import notify_live_chat_message
            notify_live_chat_message(user_id, sender_id, text)
        except Exception:
            logger.exception("notify_live_chat_message failed (Meta DM live chat)")
        return

    # ── רישום משתמש + מנוי ──────────────────────────────────────────────
    # provider_asset_id ו-external_user_id חיוניים — בלעדיהם UNIQUE
    # constraint לא נאכף ושליחת תגובה לא תמצא credentials.
    db.upsert_user(
        user_id=user_id,
        username=sender_id,  # אין לנו display_name ב-payload; נשפר ב-User Profile API בעתיד
        channel=channel,
        provider_asset_id=asset_id,
        external_user_id=sender_id,
    )
    db.ensure_user_subscribed(user_id)
    try:
        fallbacks = db.get_consecutive_fallbacks(user_id)
    except Exception:
        fallbacks = 0
        logger.exception("get_consecutive_fallbacks נכשל ל-user_hash=%s",
                         _short_hash(user_id))

    # ── עיבוד הודעה דרך הצינור המאוחד ───────────────────────────────────
    # rate_limit, intent detection, RAG, HANDOFF stripping — הכל בתוך
    # process_incoming_message. אותה לוגיקה כמו Telegram/WhatsApp.
    from core.message_processor import process_incoming_message
    user_info = {
        "display_name": sender_id,
        "telegram_username": "",
    }
    result = process_incoming_message(
        user_id=user_id,
        text=text,
        user_info=user_info,
        channel=channel,
        consecutive_fallbacks=fallbacks,
    )

    # שמירת fallbacks המעודכן ל-DB (מטא stateless כמו WhatsApp)
    if result.consecutive_fallbacks != fallbacks:
        try:
            db.set_consecutive_fallbacks(user_id, result.consecutive_fallbacks)
        except Exception:
            logger.exception("set_consecutive_fallbacks נכשל")

    # ── שליחת התשובה ──────────────────────────────────────────────────
    if result.text:
        _send_meta_response(user_id, result.text, asset_id)

    # ── handoff / agent request ─────────────────────────────────────────
    # אחרי שהלקוח קיבל את התשובה הרכה, צריך להודיע לבעל העסק שיש פנייה
    # שמחכה לו (אחרת היא נופלת לבור שחור). הצינור זהה ל-WhatsApp:
    # יצירת agent_request ב-DB + שליחת notification לבעל העסק.
    if result.action in ("request_agent", "handoff_to_human"):
        try:
            _handle_meta_agent_request(user_id, result, channel=channel)
        except Exception:
            logger.exception(
                "_handle_meta_agent_request נכשל ל-user_hash=%s",
                _short_hash(user_id),
            )


def _handle_meta_agent_request(user_id: str, result, channel: str) -> None:
    """יצירת בקשת נציג ב-DB + התראה לבעל העסק (Telegram).

    מקבילה של `_handle_agent_request` של WhatsApp, אבל ל-Meta:
    - אין notify_owner_whatsapp; ההתראה היחידה היא ב-Telegram (אם
      מוגדר TELEGRAM_OWNER_CHAT_ID). זה תואם להנחה ש-deployment הוא
      single-channel — אם הלקוחות פונים ב-IG/Messenger, בעל העסק
      צופה מ-Telegram.
    - הקישור באדמין מוביל ל-/requests, אותו עמוד שמרכז את כל
      בקשות הנציג מכל הערוצים.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ai_chatbot import database as db
    from ai_chatbot.config import ADMIN_URL, TELEGRAM_OWNER_CHAT_ID

    display = db.get_username_for_user(user_id) or user_id
    message = (
        getattr(result, "agent_request_message", "")
        or getattr(result, "handoff_reason", "")
        or ""
    )
    request_id = db.create_agent_request(
        user_id=user_id,
        username=display,
        message=message,
        channel=channel,
    )

    now_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
    panel_link = f"\n\n🔗 {ADMIN_URL}/requests" if ADMIN_URL else ""
    channel_label = "Instagram DM" if channel == "meta_ig" else "Facebook Messenger"
    notification = (
        f"🔔 בקשת נציג #{request_id} ({channel_label})\n\n"
        f"לקוח: {display}\n"
        f"זמן: {now_il}\n\n"
        f"{message}"
        f"{panel_link}"
    )

    if TELEGRAM_OWNER_CHAT_ID:
        try:
            _notify_owner_telegram(str(TELEGRAM_OWNER_CHAT_ID), notification)
        except Exception:
            logger.exception("שליחת התראת handoff מטא לבעל העסק נכשלה")
    else:
        logger.warning(
            "agent_request #%s נוצר ב-Meta אבל אין TELEGRAM_OWNER_CHAT_ID — "
            "בעל העסק לא יקבל התראה",
            request_id,
        )


def _notify_owner_telegram(chat_id: str, text: str) -> bool:
    """עטיפה רזה ל-send_telegram_message — נקודת patch יחידה לטסטים.

    בלי העטיפה הזו, הטסטים צריכים לעקוף את הdouble-binding של
    `ai_chatbot.live_chat_service` (wrapper שעושה `import *`) ושל
    `live_chat_service` (source) — בעייתי וגורר test pollution.
    """
    from ai_chatbot.live_chat_service import send_telegram_message
    return send_telegram_message(chat_id, text)


def _is_known_entry(entry_id: Optional[str]) -> bool:
    """האם ה-entry.id (page_id או IG Business Account ID) מוכר?

    בודק מול `meta_credentials` של ה-tenant הנוכחי. אם ה-DB עוד לא קיים
    (טסטים מסוימים) — מחזיר False כדי שלא נטפל באירועים בלי credentials.
    """
    if not entry_id:
        return False
    try:
        from ai_chatbot import database as db
        return db.is_meta_entry_known(entry_id)
    except Exception:
        logger.exception("is_meta_entry_known נכשל — מתייחסים כ-unknown")
        return False


def _resolve_entry_tenant(entry_id: Optional[str]) -> Optional[str]:
    """resolve של ה-tenant לפי entry.id (spec 6.3).

    ה-webhook של מטא משותף לכל הפלטפורמה (callback אחד ברמת האפליקציה),
    ולכן ה-entry.id הוא מפתח הראוטינג: קודם lookup ב-control plane
    (meta_page_id / meta_ig_account), ואם אין — fallback לבדיקה בטבלת
    ה-credentials של ה-tenant של ברירת המחדל (התנהגות legacy). None =
    לא מוכר לאף אחד ⇒ מתעלמים מהאירוע.
    """
    if not entry_id:
        return None
    try:
        from control_plane import resolve_route

        tenant = (
            resolve_route("meta_page_id", entry_id)
            or resolve_route("meta_ig_account", entry_id)
        )
        if tenant:
            return tenant
    except Exception:
        logger.exception("resolve_entry_tenant: control plane lookup נכשל")
    from tenancy import DEFAULT_TENANT

    return DEFAULT_TENANT if _is_known_entry(entry_id) else None


def _short_hash(value: Any) -> str:
    """SHA-256 קצר (10 hex) של מזהה — לקישור בין אירועים בלי לחשוף PII."""
    if not value:
        return "none"
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:10]


def _safe_len(text: Any) -> str:
    """אורך טקסט להודעה — לוג מציין נוכחות בלי לחשוף תוכן."""
    if text is None:
        return "none"
    if not isinstance(text, str):
        return "non-str"
    return str(len(text))


# ─── שליחת תשובות החוצה ──────────────────────────────────────────────────
# גישת השליחה זהה ב-Messenger וב-IG: Graph API נקודת קצה /me/messages,
# עם page access token. כאן יושב safety net לאורך הודעות (זהה לדפוס
# WhatsApp): אם הטקסט עובר את הסף — מסלול עמוד HTML ציבורי במקום שליחה
# שתיחתך באמצע.

# תקרות אורך נקראות מ-config. מטא דוחה הודעות שעוברות את המגבלות:
# Messenger = 2000 תווים, Instagram = 1000 (קצר משמעותית). ערוצי מטא
# מבדילים — IG מקבל סף נמוך יותר.
from ai_chatbot.config import (
    META_INSTAGRAM_MAX_LENGTH,
    META_MESSENGER_MAX_LENGTH,
)


def _max_length_for_channel(channel: str) -> int:
    """תקרת אורך תווים לפי ערוץ. מעבר אליה ⇒ עמוד HTML."""
    if channel == "meta_ig":
        return META_INSTAGRAM_MAX_LENGTH
    return META_MESSENGER_MAX_LENGTH


def _send_meta_response(
    internal_user_id: str,
    text: str,
    provider_asset_id: str,
) -> None:
    """שער השליחה היחיד החוצה למטא — בודק אורך, מנתב לעמוד אם צריך.

    טיעונים:
        internal_user_id: `meta_ig:<igsid>` או `meta_msg:<psid>`.
        text: גוף ההודעה.
        provider_asset_id: page_id (Messenger) או IGBA (IG). חייב להגיע
            מהקורא — בזרימת inbound הוא יורד מ-`entry.id` של ה-webhook;
            ב-flows עתידיים שלא מקבלים אותו אוטומטית, יוסיפו lookup
            מטבלת users.

    אם > תקרת הערוץ ויש ADMIN_URL — יוצר עמוד HTML ושולח קישור קצר.
    אחרת — שליחה ישירה.
    """
    from ai_chatbot.config import ADMIN_URL
    from messaging.meta_adapter import InvalidUserIdError, parse_channel

    try:
        channel = parse_channel(internal_user_id)
    except InvalidUserIdError:
        # user_id לא של מטא — שגיאת תכנות. ננפיק לוג ונפסיק כדי
        # שלא נגיע לשליחה אמיתית עם נתונים שגויים.
        logger.error(
            "_send_meta_response נקרא עם user_id לא-מטא=%s",
            _short_hash(internal_user_id),
        )
        return

    # בודקים אורך על הטקסט **המפורמט** (plain, בלי תגי HTML) — זה מה
    # שיישלח בפועל ל-DM. הטקסט המקורי (עם HTML) עובר לעמוד אם ארוך, כדי
    # שה-formatting יישמר בעמוד; _send_meta_raw מפרמט בעצמו לפני שליחה.
    from messaging.formatter import format_message
    max_len = _max_length_for_channel(channel)
    if len(format_message(text, channel)) > max_len and ADMIN_URL:
        try:
            _send_meta_as_page(internal_user_id, text, provider_asset_id)
            return
        except Exception:
            logger.error(
                "כשל בהמרת הודעה ארוכה לעמוד HTML — נופלים לשליחה רגילה "
                "(מטא תדחה אם זה ארוך מדי)",
                exc_info=True,
            )
    _send_meta_raw(internal_user_id, text, provider_asset_id)


def _send_meta_raw(
    internal_user_id: str,
    text: str,
    provider_asset_id: str,
) -> None:
    """שליחה ישירה דרך Graph API — *ללא* בדיקת אורך.

    לשימוש פנימי בלבד (fallback paths של `_send_meta_as_page` ומשם בלבד,
    כדי לא ליצור recursion עם בדיקת האורך). handlers חיצוניים *חייבים*
    לעבור דרך `_send_meta_response`.
    """
    from messaging.meta_adapter import parse_channel, to_provider_recipient
    from messaging.meta_sender import send_meta_message
    from ai_chatbot import database as db

    try:
        channel = parse_channel(internal_user_id)
        recipient = to_provider_recipient(internal_user_id)
    except Exception:
        logger.error(
            "לא ניתן לפרסר user_id=%s כ-meta channel",
            _short_hash(internal_user_id),
            exc_info=True,
        )
        return

    # פרמוט סופי לפי הערוץ — Messenger/IG = plain text (הסרת תגי HTML).
    # זו השכבה האחרונה לפני השליחה, מקבילה ל-whatsapp_sender שמפרמט לפני
    # messages.create. כל שליחת raw עוברת כאן, כולל הקישור הקצר מ-as_page
    # (שהוא plain ולכן לא מושפע). בלי זה, מטא מציגה תגי HTML גולמיים.
    from messaging.formatter import format_message
    text = format_message(text, channel)

    # שליפת page token לפי הערוץ: IG משתמש ב-IGBA, Messenger ב-page_id.
    # שניהם נשמרים תחת אותה רשומה ב-meta_credentials של עמוד הפייסבוק
    # (IG account מחובר לעמוד פייסבוק, ולכן page_token משמש לשניהם).
    try:
        if channel == "meta_ig":
            creds = db.get_meta_credentials_by_ig_account(provider_asset_id)
        else:
            creds = db.get_meta_credentials_by_page_id(provider_asset_id)
    except Exception:
        logger.error(
            "שליפת credentials נכשלה ל-asset=%s",
            _short_hash(provider_asset_id),
            exc_info=True,
        )
        return

    if not creds:
        logger.error(
            "אין credentials לערוץ=%s asset=%s — לא ניתן לשלוח תשובה",
            channel, _short_hash(provider_asset_id),
        )
        return

    try:
        send_meta_message(recipient, text, creds["access_token"])
    except Exception:
        logger.error(
            "send_meta_message נכשל ל-channel=%s recipient_hash=%s len=%s",
            channel, _short_hash(recipient), _safe_len(text),
            exc_info=True,
        )


def _send_meta_as_page(
    internal_user_id: str,
    text: str,
    provider_asset_id: str,
) -> None:
    """יוצר עמוד HTML ציבורי ושולח קישור — לתשובות שעוברות את התקרה.

    זהה לדפוס WhatsApp (`_send_as_page`). אין recursion: ההודעה הקצרה
    עם הקישור עוברת דרך `_send_meta_response` שוב, אבל היא קצרה מהסף
    ולכן עוברת ישירות ל-raw.
    """
    from ai_chatbot.config import ADMIN_URL
    from ai_chatbot import database as db

    try:
        from llm import generate_page_content
        page_html = generate_page_content(text, title="מידע", rag_context="")
    except Exception:
        logger.error("יצירת תוכן עמוד למטא נכשלה", exc_info=True)
        # נופלים לשליחה רגילה — מטא אולי תדחה, אבל לפחות יש ניסיון.
        _send_meta_raw(internal_user_id, text, provider_asset_id)
        return

    try:
        # page_type='meta_fallback' — מבדיל מ-whatsapp_fallback כדי לאפשר
        # אנליטיקה לפי ערוץ.
        page_id = db.create_response_page(
            content=page_html,
            title="מידע",
            user_id=internal_user_id,
            page_type="meta_fallback",
        )
    except Exception:
        logger.error("שמירת עמוד תשובה למטא נכשלה", exc_info=True)
        _send_meta_raw(internal_user_id, text, provider_asset_id)
        return

    from public_urls import public_page_url
    page_url = public_page_url(page_id)
    short_msg = f"הכנתי עבורכם את כל המידע בעמוד נוח לקריאה:\n{page_url}"
    # קוראים ל-raw ישירות, לא ל-_send_meta_response, כדי למנוע recursion
    # תיאורטי אם השילוב ADMIN_URL+page_id יעבור את הסף בעתיד (כל סיבוב
    # היה יוצר עוד response_page ל-DB ⇒ stack overflow + זיהום DB).
    # ההודעה כאן היא URL קצר ולא צריכה length-gating.
    _send_meta_raw(internal_user_id, short_msg, provider_asset_id)
