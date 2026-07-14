"""
WhatsApp Webhook — Flask Blueprint לקבלת הודעות נכנסות מ-Twilio.

נרשם כ-Blueprint ב-admin/app.py. מטפל ב:
1. אימות חתימת Twilio (X-Twilio-Signature)
2. חילוץ הודעה ומספר שולח
3. קריאה ל-message_processor (אותה לוגיקה כמו Telegram)
4. שליחת תשובה דרך WhatsApp adapter
"""

import asyncio
import logging

from flask import Blueprint, request, abort, has_request_context

from ai_chatbot.config import TELEGRAM_OWNER_CHAT_ID, ADMIN_URL, WHATSAPP_MAX_LENGTH, get_business_config
from tenancy import DEFAULT_TENANT, tenant_context
from ai_chatbot import database as db

logger = logging.getLogger(__name__)


from utils.phone import format_phone as _format_phone
from utils.user_identity import resolve_whatsapp_user

# אחסון שאלות follow-up per-user — שומרים את הטקסט המלא כי ב-Quick Reply יש מגבלת 20 תווים
# מפתח: user_id, ערך: רשימת שאלות. נדרס בכל תשובה חדשה (רק השאלות האחרונות רלוונטיות)
# שאלות ההמשך האחרונות פר-משתמש. המפתח: (tenant, user_id).
_follow_up_store: dict[tuple[str, str], list[str]] = {}


def _follow_up_key(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)

# מיפוי interactive_id (מכפתורי Quick Reply / List Picker) לטקסט תפריט
_INTERACTIVE_MAP = {
    "menu_price": "מחירון",
    "menu_booking": "בקשת תור",
    "menu_location": "מיקום העסק",
    "menu_agent": "דברו עם נציג",
}
# מיפוי מספרי טקסט (fallback — כשאין כפתורים)
_MENU_MAP = {
    "1": "מחירון",
    "2": "בקשת תור",
    "3": "מיקום העסק",
    "4": "דברו עם נציג",
}

whatsapp_bp = Blueprint("whatsapp", __name__)


def _resolve_webhook_tenant(webhook_key):
    """resolve של ה-tenant מנתיב ה-webhook.

    בלי מפתח (הנתיב ה-legacy /webhook/whatsapp) — ה-tenant של ברירת
    המחדל (env). עם מפתח — lookup ב-control plane; מפתח לא רשום מחזיר
    None והקורא עונה 404 (המפתח אקראי ובלתי-ניתן-לניחוש — אין oracle).
    """
    if webhook_key is None:
        return DEFAULT_TENANT
    from control_plane import resolve_route

    return resolve_route("twilio_webhook_key", webhook_key)


def _tenant_twilio_settings():
    """(sid, token, number) של ה-tenant הנוכחי, או None אם לא מוגדר."""
    try:
        from messaging.whatsapp_sender import _resolve_twilio_settings

        sid, token, number = _resolve_twilio_settings()
        if sid and token and number:
            return sid, token, number
    except Exception:
        logger.error("whatsapp webhook: resolving Twilio settings failed", exc_info=True)
    return None


def _validate_twilio_signature() -> bool:
    """אימות חתימת Twilio — עם ה-auth token של ה-tenant הנוכחי.

    ה-resolve של ה-tenant קורה לפני האימות (המפתח ב-URL קובע את הטוקן);
    fail-closed — בלי טוקן אין אימות ואין עיבוד.
    """
    settings = _tenant_twilio_settings()
    if settings is None:
        logger.error("Twilio auth token לא זמין ל-tenant הנוכחי — לא ניתן לאמת חתימה")
        return False
    _, auth_token, _ = settings
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(auth_token)
        signature = request.headers.get("X-Twilio-Signature", "")
        # בניית URL מלא — Twilio משתמש ב-URL כפי שנשלח (כולל פרוטוקול)
        url = request.url
        # אם מאחורי proxy עם HTTPS — להשתמש ב-X-Forwarded-Proto
        if request.headers.get("X-Forwarded-Proto") == "https":
            url = url.replace("http://", "https://", 1)
        return validator.validate(url, request.form.to_dict(), signature)
    except Exception as e:
        logger.error("שגיאה באימות חתימת Twilio: %s", e)
        return False


# כפתורי opt-in פרואקטיבי — מזוהים ב-interactive handling כדי להירשם/להסיר
# בלי לעבור דרך LLM. אותה מחלקה כמו _INTERACTIVE_MAP אבל פעולה ייחודית.
_OPTIN_BUTTON_YES = "optin_yes"
_OPTIN_BUTTON_NO = "optin_no"


def _maybe_send_opt_in_prompt(to_number: str) -> None:
    """שליחת בקשת opt-in פרואקטיבית אם המשתמש עומד בתנאים (ראה DB helper).

    הטקסט כולל את החובות של תיקון 40 — הסכמה מפורשת + אזכור של אפשרות
    הסרה. בלי אזכור זה ה-opt-in אינו חוקי גם אם המשתמש לחץ "כן".

    שולח Quick Reply בתוך session (לא דורש אישור Meta כי זה תשובה
    להודעה שנכנסה). Fallback טקסטואלי אם Quick Reply נכשל.
    """
    if not db.should_send_opt_in_prompt(to_number):
        return

    # שני טקסטים נפרדים: אחד לכפתור Quick Reply ("בסימון *כן*") ואחד
    # ל-fallback טקסטואלי ("השב/י *הסכמה*"). חשוב לא לערבב — משתמש
    # שרואה "בסימון *כן*" ומגיב "כן" לא ייכנס ל-OPTIN_KEYWORDS (שם יש רק
    # "כן, שלחו"), וזו הפרה פוטנציאלית של תיקון 40 (הסכמה שלא נקלטה).
    intro = f"📬 רוצים לקבל מאיתנו עדכונים והטבות ב-WhatsApp?"
    legal_tail = (
        f"אני מסכים/ה לקבל הודעות שיווקיות מ-{get_business_config().name}. "
        "ניתן להסיר בכל עת ע\"י השבת *הסר*."
    )
    button_body = f"{intro}\n\nבסימון *כן* {legal_tail}"
    try:
        from messaging.whatsapp_templates import ensure_quick_reply, send_with_template
        content_sid = ensure_quick_reply(
            friendly_name="opt_in_prompt",
            body=button_body,
            buttons=[
                ("✅ כן, אני מסכים/ה", _OPTIN_BUTTON_YES),
                ("❌ לא, תודה", _OPTIN_BUTTON_NO),
            ],
        )
        send_with_template(to_number, content_sid)
    except Exception:
        logger.warning(
            "Quick Reply opt-in prompt נכשל, חוזרים לטקסט", exc_info=True,
        )
        # ב-fallback אין כפתור "כן", אז לא מזכירים אותו. מדריכים להשיב
        # *הסכמה* (מופיע ב-OPTIN_KEYWORDS). שתיקה = סירוב אוטומטי
        # (mark_opt_in_prompt_sent כבר נקרא — לא נטריד שוב).
        fallback = (
            f"{intro}\n\nבהשבת *הסכמה* {legal_tail}\n\n"
            "להתעלם — פשוט אל תגיבו לבקשה זו; לא נטריד שוב."
        )
        _send_whatsapp_response(to_number, fallback)

    # תמיד מסמנים sent — גם אם השליחה עצמה נכשלה, לא נציק שוב. ניסיון
    # חוזר אוטומטי עלול לאתר באג בשליחה ולהמשיך להטריד את המשתמש.
    db.mark_opt_in_prompt_sent(to_number)
    db.save_message(
        to_number, to_number, "assistant",
        "[בקשת הסכמה לעדכונים שיווקיים נשלחה]",
        channel="whatsapp",
    )


def _handle_opt_in_button(
    from_number: str, profile_name: str, interactive_id: str,
) -> bool:
    """טיפול בלחיצה על כפתור opt-in prompt. מחזיר True אם טופל (וה-caller
    צריך לעצור עיבוד רגיל)."""
    if interactive_id == _OPTIN_BUTTON_YES:
        db.set_wa_marketing_opt_in(from_number, source="bot_button")
        db.save_message(
            from_number, profile_name or from_number, "user",
            "[כן, אני מסכים/ה לעדכונים שיווקיים]", channel="whatsapp",
        )
        reply = (
            "תודה! נרשמת לקבלת עדכונים ✅\n"
            "ניתן להסרה בכל עת ע\"י תגובת *הסר*."
        )
        _send_whatsapp_response(from_number, reply)
        db.save_message(
            from_number, profile_name or from_number, "assistant",
            reply, channel="whatsapp",
        )
        return True
    if interactive_id == _OPTIN_BUTTON_NO:
        # "לא" אינו opt-out מלא — סתם דוחה את הבקשה. לא נציק שוב כי
        # prompt_sent_at כבר עודכן כשה-prompt נשלח.
        db.save_message(
            from_number, profile_name or from_number, "user",
            "[לא, לא מעוניין/ת בעדכונים]", channel="whatsapp",
        )
        reply = "בסדר גמור, לא תקבלו מאיתנו הודעות שיווקיות. 🙂"
        _send_whatsapp_response(from_number, reply)
        db.save_message(
            from_number, profile_name or from_number, "assistant",
            reply, channel="whatsapp",
        )
        return True
    return False


@whatsapp_bp.route("/webhook/whatsapp/status", methods=["POST"])
@whatsapp_bp.route("/webhook/whatsapp/t/<webhook_key>/status", methods=["POST"])
def whatsapp_status_webhook(webhook_key=None):
    """נקודת קבלה ל-status callbacks של Twilio עבור הודעות broadcast.

    Twilio שולחת עדכוני סטטוס (sent → delivered → read או failed) ב-POST
    עם השדות: MessageSid, MessageStatus, ErrorCode (אם יש), ErrorMessage.
    אנו מעדכנים את broadcast_deliveries + מונים של הקמפיין.

    מחזירים 200 תמיד (גם בשגיאה) — 5xx היה גורם ל-Twilio retry storm.
    4xx (403) על חתימה לא תקפה בסדר כי Twilio לא מנסים שוב אחרי זה.
    """
    tenant = _resolve_webhook_tenant(webhook_key)
    if tenant is None:
        abort(404)
    with tenant_context(tenant):
        return _whatsapp_status_impl()


def _whatsapp_status_impl():
    if _tenant_twilio_settings() is None:
        # Token חסר אחרי deploy — לא מנסים לאמת ולא מעבדים. 200 כדי
        # ש-Twilio לא ינסה שוב באופן שוטף; הבעיה היא פנימית ותטופל בנפרד.
        logger.warning(
            "whatsapp_status_webhook: פרטי Twilio לא זמינים — מתעלמים מה-callback"
        )
        return "", 200

    # אימות חתימה — חובה כדי שלא יוזרקו עדכוני סטטוס מזויפים
    if not _validate_twilio_signature():
        logger.warning(
            "whatsapp_status_webhook: חתימת Twilio לא תקפה — דוחה"
        )
        abort(403)

    message_sid = request.form.get("MessageSid", "").strip()
    message_status = request.form.get("MessageStatus", "").strip()
    error_code = request.form.get("ErrorCode", "").strip()
    error_message = request.form.get("ErrorMessage", "").strip()

    if not message_sid or not message_status:
        logger.warning(
            "whatsapp_status_webhook: חסרים MessageSid/MessageStatus"
        )
        return "", 200

    try:
        from messaging.broadcast_sender import handle_status_callback
        handle_status_callback(
            message_sid=message_sid,
            message_status=message_status,
            error_code=error_code,
            error_message=error_message,
        )
    except Exception:
        logger.error(
            "whatsapp_status_webhook: עיבוד עדכון סטטוס נכשל עבור %s",
            message_sid, exc_info=True,
        )

    return "", 200


def _current_to_number() -> str:
    """מספר ה-WhatsApp העסקי שאליו נשלחה ההודעה (שדה To של Twilio).

    multi-tenant שלב 1: נאסף ונשמר כ-provider_asset_id על שורת המשתמש —
    העוגן שדרכו הפלטפורמה תדע לאיזה עסק שייכת הודעה נכנסת (בשלב 2 ה-resolve
    יקרה לפי מפתח ראוטינג ב-URL, וה-To ישמש cross-check). כשל כאן לא מפיל
    את הבקשה — מחזיר מחרוזת ריקה ו-upsert_user שומר את הערך הקיים.
    """
    # קריאה ישירה מחוץ ל-request (טסטים / שימוש עתידי) — אין To, וזה תקין
    if not has_request_context():
        return ""
    try:
        return request.form.get("To", "").replace("whatsapp:", "").strip()
    except Exception:
        logger.error("whatsapp webhook: failed reading To field", exc_info=True)
        return ""


def _upsert_whatsapp_user(from_number: str, profile_name: str) -> None:
    """Upsert אחיד למשתמש WhatsApp — כולל שמירת מספר העסק (To)."""
    db.upsert_user(
        from_number,
        profile_name or from_number,
        channel="whatsapp",
        provider_asset_id=_current_to_number(),
    )


@whatsapp_bp.route("/webhook/whatsapp", methods=["POST"])
@whatsapp_bp.route("/webhook/whatsapp/t/<webhook_key>", methods=["POST"])
def whatsapp_webhook(webhook_key=None):
    """נקודת כניסה להודעות נכנסות מ-WhatsApp דרך Twilio.

    שני נתיבים: ה-legacy (/webhook/whatsapp) משרת את ה-tenant של ברירת
    המחדל; הנתיב עם המפתח (/webhook/whatsapp/t/<key>) עושה resolve מול
    ה-control plane — זה הנתיב שמוגדר ב-Twilio Console לכל tenant.
    """
    tenant = _resolve_webhook_tenant(webhook_key)
    if tenant is None:
        logger.warning("WhatsApp webhook: unknown webhook key — rejecting")
        abort(404)
    with tenant_context(tenant):
        return _whatsapp_webhook_impl()


def _whatsapp_webhook_impl():
    # בדיקת הגדרות — אם Twilio לא מוגדר ל-tenant, מחזיר 503
    if _tenant_twilio_settings() is None:
        logger.warning("WhatsApp webhook called but Twilio credentials not configured")
        abort(503)

    # אימות חתימה (אבטחה!)
    if not _validate_twilio_signature():
        logger.warning("WhatsApp webhook: invalid Twilio signature — rejecting request")
        abort(403)

    # cross-check: ה-To של ההודעה מול המספר הרשום ל-tenant. mismatch לא
    # חוסם (החתימה כבר אומתה מול הטוקן של ה-tenant) אבל מרמז על ראוט
    # שגוי ב-Twilio Console — מתעדים בבירור.
    _settings = _tenant_twilio_settings()
    _to = _current_to_number()
    if _settings and _to and _to != _settings[2]:
        logger.warning(
            "WhatsApp webhook: To mismatch — inbound=%s registered=%s "
            "(בדקו את הגדרת ה-webhook ב-Twilio Console)", _to, _settings[2],
        )

    # חילוץ נתונים מהבקשה
    from_raw = request.form.get("From", "").replace("whatsapp:", "").strip()
    body = request.form.get("Body", "").strip()
    # שם הפרופיל מ-WhatsApp — Twilio מעביר אותו בשדה ProfileName
    profile_name = request.form.get("ProfileName", "").strip()

    # חילוץ נתוני כפתור — כש-Twilio שולח תגובה ללחיצה על Quick Reply / List Picker
    button_payload = request.form.get("ButtonPayload", "").strip()
    button_text = request.form.get("ButtonText", "").strip()
    # ListId מגיע כשמשתמש בוחר מתוך List Picker
    list_id = request.form.get("ListId", "").strip()

    # BSUID — מזהה משתמש חדש מ-Meta Cloud API (יוני 2026).
    # Twilio חושפים אותו בשדה ExternalUserId (אושר באינטל 19-04-2026).
    bsuid = request.form.get("ExternalUserId", "").strip() or None
    # Parent BSUID — רלוונטי ל-Meta-managed portfolios (forward-compat).
    # אם השדה חסר — נשארים None ולא מפילים את ה-webhook.
    parent_bsuid = request.form.get("ExternalParentUserId", "").strip() or None
    # Twilio לא חושפים שדה username ב-webhook (אושר 19-04-2026).
    # ProfileName כבר נשלף למעלה — אם Meta יוסיפו username בעתיד, הקוד יעודכן.
    wa_username = None

    # From יכול להכיל מספר טלפון (+972...) או BSUID (IL.ABCdef123).
    # מפרידים כדי לא להעביר BSUID כ-phone_number ל-resolve_whatsapp_user.
    from messaging.whatsapp_sender import _is_phone_number
    if from_raw and _is_phone_number(from_raw):
        from_number = from_raw
    else:
        from_number = ""
        # אם ExternalUserId חסר אבל From מכיל BSUID — משתמשים בו
        if not bsuid and from_raw:
            bsuid = from_raw

    if not from_number and not bsuid:
        logger.warning("WhatsApp webhook: missing From number and BSUID")
        abort(400)

    # תרגום לזהות קנונית — שומר BSUID/טלפון/שם משתמש בטבלת user_identities
    from_number = resolve_whatsapp_user(
        phone_number=from_number,
        bsuid=bsuid,
        parent_bsuid=parent_bsuid,
        wa_username=wa_username,
    )

    if not body and not button_payload and not list_id:
        # הודעות מדיה (תמונות, קבצים) — לא נתמכות כרגע
        logger.info("WhatsApp webhook: empty body from %s (possibly media message)", from_number)
        return "", 200

    # ── Opt-out / Opt-in detection (תיקון 40) ────────────────────────────
    # מוקדם ככל האפשר — לפני live-chat, booking, cancel וכל flow אחר.
    # הסרה רגולטורית מנצחת כל state אחר.
    try:
        from messaging.whatsapp_optout import (
            detect_optout, detect_optin,
            OPTOUT_CONFIRMATION, OPTIN_CONFIRMATION,
        )
        if detect_optout(body):
            _upsert_whatsapp_user(from_number, profile_name)
            db.set_wa_opted_out(from_number)
            db.save_message(from_number, profile_name or from_number, "user", body, channel="whatsapp")
            _send_whatsapp_response(from_number, OPTOUT_CONFIRMATION)
            db.save_message(from_number, profile_name or from_number, "assistant", OPTOUT_CONFIRMATION, channel="whatsapp")
            logger.info("WhatsApp opt-out registered for %s", from_number)
            return "", 200
        if detect_optin(body):
            _upsert_whatsapp_user(from_number, profile_name)
            db.set_wa_marketing_opt_in(from_number, source="bot_reply")
            db.save_message(from_number, profile_name or from_number, "user", body, channel="whatsapp")
            _send_whatsapp_response(from_number, OPTIN_CONFIRMATION)
            db.save_message(from_number, profile_name or from_number, "assistant", OPTIN_CONFIRMATION, channel="whatsapp")
            logger.info("WhatsApp opt-in registered for %s", from_number)
            return "", 200
    except Exception:
        # זיהוי opt-out לא אמור להפיל את ה-webhook; אם נכשל, ממשיכים לזרימה רגילה.
        # אבל חייבים ללוג — הפרה רגולטורית לא אמורה להתפספס בשקט.
        logger.error("WhatsApp opt-out/in detection failed for %s", from_number, exc_info=True)

    # ── Privacy router (תיקון 13) ────────────────────────────────────────
    # מטפל בבקשות מחיקה ועיון שונות מ-opt-out (תיקון 40 לעיל).
    # מחיקה דורשת אישור דו-שלבי כדי למנוע false-positives.
    # רץ לפני LLM/RAG כדי שהמודל לא יענה משהו כמו "אעביר את הבקשה" בלי לבצע.
    try:
        from messaging.whatsapp_privacy import (
            detect_delete_request,
            detect_access_request,
            detect_delete_confirmation,
            register_pending_delete,
            is_pending_delete,
            clear_pending_delete,
            build_delete_warning,
            format_access_summary,
            DELETE_CONFIRMATION_PROMPT,
            DELETE_COMPLETED_TEMPLATE,
            DELETE_NO_DATA_MESSAGE,
            DELETE_ALREADY_IN_PROGRESS,
            DELETE_PARTIAL_FAILURE,
            DELETE_FAILED_MESSAGE,
        )
        from config import ADMIN_URL

        # שלב 1: אם יש pending delete + המשתמש שלח את אישור המחיקה — מבצעים.
        # *לא* קוראים ל-upsert_user / save_message כאן — אנחנו עומדים
        # למחוק, אין סיבה ליצור PII חדש שיצטרך להימחק מיד או ישרוד אם
        # המחיקה תיכשל חלקית. ה-deletion_requested ב-ledger מתעד שהבקשה
        # הגיעה.
        if is_pending_delete(from_number) and detect_delete_confirmation(body):
            counts = db.delete_user_data(from_number)
            clear_pending_delete(from_number)
            if counts.get("already_in_progress"):
                _send_whatsapp_response(from_number, DELETE_ALREADY_IN_PROGRESS)
                return "", 200
            # _result_total_count מתעלם ממפתחות dunder (__failed_tables__
            # וכד') כדי לא לחבר רשימות/מחרוזות בטעות.
            total = db._result_total_count(counts)
            status = db.deletion_status(counts)
            # סדר חשוב: failed לפני total==0, אחרת כשל מלא יוצג כ-
            # "אין מידע" (false confirmation, הפרת ציות).
            if status == "failed":
                msg = DELETE_FAILED_MESSAGE
            elif status == "partial":
                msg = DELETE_PARTIAL_FAILURE.format(total=total)
            elif total == 0:
                msg = DELETE_NO_DATA_MESSAGE
            else:
                msg = DELETE_COMPLETED_TEMPLATE.format(total=total)
            _send_whatsapp_response(from_number, msg)
            logger.info(
                "WhatsApp privacy: delete %s for %s, total=%d",
                status, from_number, total,
            )
            return "", 200

        # שלב 2: בקשת מחיקה חדשה — שולחים 2 הודעות (אזהרה + הוראת אישור)
        if detect_delete_request(body):
            _upsert_whatsapp_user(from_number, profile_name)
            db.save_message(
                from_number, profile_name or from_number, "user", body, channel="whatsapp",
            )
            base = (ADMIN_URL or "").rstrip("/")
            privacy_link = f"{base}/legal/privacy" if base else ""
            warning = build_delete_warning(privacy_link)
            _send_whatsapp_response(from_number, warning)
            _send_whatsapp_response(from_number, DELETE_CONFIRMATION_PROMPT)
            register_pending_delete(from_number)
            logger.info("WhatsApp privacy: delete request from %s, awaiting confirmation", from_number)
            return "", 200

        # שלב 3: בקשת עיון
        if detect_access_request(body):
            _upsert_whatsapp_user(from_number, profile_name)
            db.save_message(
                from_number, profile_name or from_number, "user", body, channel="whatsapp",
            )
            # ייבוא ledger helpers מחוץ ל-try כדי שכשל בייבוא לא יגרום
            # ל-NameError ב-try השני (שיוסתר מאחורי error log מטעה).
            try:
                from utils.consent_ledger import (
                    record_consent_event,
                    EVENT_ACCESS_REQUESTED,
                    EVENT_ACCESS_DELIVERED,
                )
                ledger_available = True
            except Exception:
                logger.error(
                    "WhatsApp privacy: כשל ב-import של consent_ledger", exc_info=True,
                )
                ledger_available = False

            # ledger: access_requested
            if ledger_available:
                try:
                    record_consent_event(
                        user_id=from_number, channel="whatsapp",
                        event_type=EVENT_ACCESS_REQUESTED,
                    )
                except Exception:
                    logger.error(
                        "WhatsApp privacy: כשל ב-access_requested ל-ledger", exc_info=True,
                    )
            summary = db.get_user_data_summary(from_number)
            text = format_access_summary(summary)
            _send_whatsapp_response(from_number, text)
            if ledger_available:
                try:
                    record_consent_event(
                        user_id=from_number, channel="whatsapp",
                        event_type=EVENT_ACCESS_DELIVERED,
                    )
                except Exception:
                    logger.error(
                        "WhatsApp privacy: כשל ב-access_delivered ל-ledger", exc_info=True,
                    )
            logger.info("WhatsApp privacy: access summary delivered to %s", from_number)
            return "", 200
    except Exception:
        # privacy router לא אמור להפיל את ה-webhook — אם נכשל, ממשיכים
        # לזרימה רגילה. אבל חייבים ללוג כי זה הפרת זכות פוטנציאלית.
        logger.error(
            "WhatsApp privacy router failed for %s", from_number, exc_info=True,
        )

    # ── Referral code detection (REF_XXXXXXXX) ──────────────────────────
    # מקבילה ל-Telegram /start REF_XXX deep-link. הקוד מגיע כטקסט מוכן
    # מתוך wa.me link (מוגדר ב-build_referral_link). מקצרים את הזרימה ולא
    # מעבירים ל-LLM/booking — זו פעולת רישום, לא שאלת תוכן.
    try:
        if _maybe_handle_referral_code(from_number, profile_name, body):
            return "", 200
    except Exception:
        logger.error(
            "WhatsApp referral code handling failed for %s", from_number, exc_info=True,
        )

    # אם יש button payload — משתמשים בו כ-body (הוא ה-id הייחודי של הכפתור)
    interactive_id = button_payload or list_id

    # Opt-in prompt buttons — מנותבים לפני כל לוגיקה אחרת כי הם פעולת חשבון
    # (registration), לא שאלת content. לא מעבירים ל-LLM/booking.
    # חשוב: יוצאים תמיד (גם בכשל וגם כש-handler מחזיר False) — אחרת ה-id נופל
    # אל fallback של "סשן פג תוקף" כי אינו ב-_INTERACTIVE_MAP ואין body.
    if interactive_id in (_OPTIN_BUTTON_YES, _OPTIN_BUTTON_NO):
        try:
            _handle_opt_in_button(from_number, profile_name, interactive_id)
        except Exception:
            logger.error(
                "WhatsApp opt-in button handling failed for %s",
                from_number, exc_info=True,
            )
        return "", 200
    if interactive_id:
        logger.info("WhatsApp interactive from %s: payload=%s text=%s", from_number, interactive_id, button_text)
    else:
        logger.info("WhatsApp message from %s: %s", from_number, body[:100])

    # עיבוד ההודעה דרך message_processor — אותה לוגיקה כמו Telegram
    try:
        # בדיקת live chat — אם פעיל, שומרים את ההודעה ולא מפעילים את ה-processor
        from ai_chatbot.live_chat_service import LiveChatService
        if LiveChatService.is_active(from_number):
            live_chat_text = body or button_text or interactive_id
            db.save_message(from_number, profile_name or from_number, "user", live_chat_text, channel="whatsapp")
            db.touch_live_chat(from_number)
            # התראת Web Push לבעל העסק — עובד גם כשהדשבורד סגור.
            try:
                from notifications.push_service import notify_live_chat_message
                notify_live_chat_message(from_number, profile_name or from_number, live_chat_text)
            except Exception:
                logger.exception("notify_live_chat_message failed (WhatsApp live chat)")
            return "", 200

        # ניקוי booking sessions שפג תוקפם — זול (in-memory dict), מונע דליפת זיכרון
        from messaging.conversation_state import cleanup_expired as _cleanup_booking
        _cleanup_booking()

        # בדיקת cancel flow פתוח — בחירת תור או אישור ביטול
        from messaging.conversation_state import (
            get_state as _get_conv_state, set_state as _set_conv_state,
            STATE_CANCEL_CONFIRM, STATE_CANCEL_SELECT,
        )
        cancel_session = _get_conv_state(from_number)
        cancel_state = cancel_session.get("state") if cancel_session else None

        if cancel_state == STATE_CANCEL_SELECT:
            # שלב בחירת תור מרשימה — שמירת הודעת המשתמש לפני עיבוד
            db.save_message(from_number, profile_name or from_number, "user", button_text or body, channel="whatsapp")
            cancel_response = _handle_cancel_selection(from_number, interactive_id or body, cancel_session)
            if cancel_response:
                _send_whatsapp_response(from_number, cancel_response)
                db.save_message(from_number, profile_name or from_number, "assistant", cancel_response, channel="whatsapp")
            return "", 200

        if cancel_state == STATE_CANCEL_CONFIRM:
            cancel_response = _handle_cancel_confirmation(from_number, interactive_id or body)
            if cancel_response is not None:
                db.save_message(from_number, profile_name or from_number, "user", button_text or body, channel="whatsapp")
                _send_whatsapp_response(from_number, cancel_response)
                db.save_message(from_number, profile_name or from_number, "assistant", cancel_response, channel="whatsapp")
                return "", 200
            # קלט לא מזוהה — שולחים שוב את כפתורי האישור
            _set_conv_state(from_number, STATE_CANCEL_CONFIRM)
            db.save_message(from_number, profile_name or from_number, "user", button_text or body, channel="whatsapp")
            retry_msg = "לא הבנתי — האם לבטל את התור?"
            _send_cancel_confirmation_buttons(from_number, retry_msg)
            db.save_message(from_number, profile_name or from_number, "assistant", retry_msg, channel="whatsapp")
            return "", 200

        # בדיקת reschedule flow פתוח — שינוי תאריך/שעה של תור
        from messaging.conversation_state import (
            STATE_RESCHEDULE_SELECT, STATE_RESCHEDULE_DATE,
            STATE_RESCHEDULE_TIME, STATE_RESCHEDULE_CONFIRM,
        )
        reschedule_session = _get_conv_state(from_number)
        reschedule_state = reschedule_session.get("state") if reschedule_session else None

        if reschedule_state in (STATE_RESCHEDULE_SELECT, STATE_RESCHEDULE_DATE,
                                STATE_RESCHEDULE_TIME, STATE_RESCHEDULE_CONFIRM):
            reschedule_response = _handle_reschedule_step(
                from_number, interactive_id or body, reschedule_session,
            )
            db.save_message(from_number, profile_name or from_number, "user", button_text or body, channel="whatsapp")
            if reschedule_response:
                _send_whatsapp_response(from_number, reschedule_response)
                db.save_message(from_number, profile_name or from_number, "assistant", reschedule_response, channel="whatsapp")
            return "", 200

        # בדיקת booking flow פתוח — אם יש state, ממשיכים את ה-flow
        # חשוב: לפני תרגום מספרי תפריט, כדי לא לדרוס בחירת שירות ממוספרת
        from messaging.whatsapp_booking import handle_booking_step
        # ב-booking flow, interactive_id מקבל עדיפות (למשל list_id של שירות שנבחר)
        booking_input = interactive_id or body
        booking_response = handle_booking_step(from_number, booking_input)
        if booking_response is not None:
            db.save_message(from_number, profile_name or from_number, "user", button_text or body, channel="whatsapp")
            if booking_response:  # מחרוזת לא ריקה = טקסט לשליחה
                _send_whatsapp_response(from_number, booking_response)
            else:
                # מחרוזת ריקה = כבר נשלח אינטראקטיבית — שומרים placeholder בהיסטוריה
                db.save_message(from_number, profile_name or from_number, "assistant", "[הודעה אינטראקטיבית נשלחה]", channel="whatsapp")
            return "", 200

        # תרגום לחיצת כפתור / מספרי תפריט לטקסט מתאים
        if interactive_id and interactive_id in _INTERACTIVE_MAP:
            body = _INTERACTIVE_MAP[interactive_id]
        elif interactive_id and interactive_id.startswith("followup_"):
            # לחיצה על כפתור שאלת המשך — שולפים את הטקסט המלא מהמאגר
            try:
                idx = int(interactive_id.split("_")[1])
                stored = _follow_up_store.get(_follow_up_key(from_number), [])
                if idx < len(stored):
                    body = stored[idx]
                else:
                    body = button_text or interactive_id  # fallback — טקסט הכפתור עצמו
            except (ValueError, IndexError):
                body = button_text or interactive_id
        elif interactive_id and not body.strip():
            # לחיצה על כפתור booking (confirm_yes, svc_5 וכו') אחרי שפג תוקף הסשן
            # interactive_id לא בתפריט ואין body — שולחים הודעה ידידותית
            expired_msg = "⏰ הסשן פג תוקף. אפשר להתחיל מחדש — כתבו *תור* או *בקשת תור*."
            db.save_message(from_number, profile_name or from_number, "user", f"[כפתור: {interactive_id}]", channel="whatsapp")
            _send_whatsapp_response(from_number, expired_msg)
            db.save_message(from_number, profile_name or from_number, "assistant", expired_msg, channel="whatsapp")
            return "", 200
        elif body.strip() in _MENU_MAP:
            body = _MENU_MAP[body.strip()]

        # process_incoming_message מטפל ב:
        # - rate limiting (פנימית)
        # - שמירת הודעות user/assistant ב-DB
        # לכן אין צורך לשמור/לבדוק כאן בנפרד
        from core.message_processor import process_incoming_message

        # רישום משתמש כמנוי (אם עוד לא קיים) + עדכון טבלת users + קריאת מונה fallbacks
        db.ensure_user_subscribed(from_number)
        _upsert_whatsapp_user(from_number, profile_name)
        fallbacks = db.get_consecutive_fallbacks(from_number)

        # הודעת פתיחה למשתמש חדש — אם אין היסטוריית שיחה, שולחים ברכה + תפריט
        history = db.get_conversation_history(from_number, limit=1)
        sent_welcome = False
        if not history:
            _send_welcome_message(from_number)
            sent_welcome = True
            db.save_message(
                from_number, profile_name or from_number, "assistant",
                f"שלחתי הודעת ברוכים הבאים ותפריט ראשי ללקוח ב-WhatsApp.",
                channel="whatsapp",
            )

        user_info = {
            "display_name": profile_name or from_number,
            "telegram_username": "",
        }

        result = process_incoming_message(
            user_id=from_number,
            text=body,
            user_info=user_info,
            channel="whatsapp",
            consecutive_fallbacks=fallbacks,
        )

        # שמירת מונה fallbacks מעודכן ב-DB (WhatsApp הוא stateless)
        if result.consecutive_fallbacks != fallbacks:
            db.set_consecutive_fallbacks(from_number, result.consecutive_fallbacks)

        # טיפול בפעולות מיוחדות — בקשת נציג / העברה לנציג
        if result.action in ("request_agent", "handoff_to_human"):
            _handle_agent_request(from_number, result, profile_name=profile_name)

        # greeting — שליחת תפריט כפתורים (אם לא נשלח כבר כ-welcome למשתמש חדש)
        # farewell לא כלול — "ביי" / "תודה" צריכים לקבל תשובת פרידה, לא welcome
        if result.intent and result.intent.value == "greeting" and not sent_welcome:
            _send_welcome_message(from_number)
            # process_incoming_message כבר שמר תשובת ברכה ב-DB, אבל שלחנו welcome במקום.
            # שומרים placeholder כדי שההיסטוריה תשקף את מה שהמשתמש באמת קיבל.
            db.save_message(from_number, profile_name or from_number, "assistant",
                            "[תפריט ראשי עם כפתורים נשלח]", channel="whatsapp")
        elif result.action == "cancel_appointment":
            from messaging.conversation_state import set_state, STATE_CANCEL_CONFIRM, STATE_CANCEL_SELECT
            from messaging.whatsapp_booking import _format_date_display
            pending = db.get_pending_appointments_for_user(from_number)
            if not pending:
                no_appt_msg = "לא רשום אצלנו תור על שמך. 🤔\nתרצו שאעביר את הבקשה לבעל העסק כדי לברר?"
                _send_whatsapp_response(from_number, no_appt_msg)
                db.save_message(from_number, profile_name or from_number, "assistant", no_appt_msg, channel="whatsapp")
            elif len(pending) == 1:
                # תור יחיד — ישר לאישור
                appt = pending[0]
                date_display = _format_date_display(appt.get("preferred_date", ""))
                confirm_text = (
                    f"האם לבטל את התור הזה?\n\n"
                    f"📋 *שירות:* {appt.get('service', '')}\n"
                    f"📅 *תאריך:* {date_display}\n"
                    f"🕐 *שעה:* {appt.get('preferred_time', '')}"
                )
                set_state(from_number, STATE_CANCEL_CONFIRM, {"appt_id": appt["id"]})
                _send_cancel_confirmation_buttons(from_number, confirm_text)
            else:
                # מספר תורים — שלב בחירה
                lines = ["איזה תור תרצו לבטל?\n"]
                appt_ids = []
                for i, appt in enumerate(pending, 1):
                    date_display = _format_date_display(appt.get("preferred_date", ""))
                    lines.append(f"{i}. {appt.get('service', '')} | {date_display} | {appt.get('preferred_time', '')}")
                    appt_ids.append(appt["id"])
                lines.append("\n_(שלחו את המספר)_")
                select_msg = "\n".join(lines)
                set_state(from_number, STATE_CANCEL_SELECT, {"appt_ids": appt_ids})
                _send_whatsapp_response(from_number, select_msg)
                db.save_message(from_number, profile_name or from_number, "assistant", select_msg, channel="whatsapp")
        elif result.action == "reschedule_appointment":
            from messaging.conversation_state import set_state as _set_state
            from messaging.conversation_state import STATE_RESCHEDULE_SELECT, STATE_RESCHEDULE_DATE
            from messaging.whatsapp_booking import _format_date_display
            pending = db.get_pending_appointments_for_user(from_number)
            if not pending:
                no_appt_msg = "לא רשום אצלנו תור על שמך. 🤔\nתרצו שאעביר את הבקשה לבעל העסק כדי לברר?"
                _send_whatsapp_response(from_number, no_appt_msg)
                db.save_message(from_number, profile_name or from_number, "assistant", no_appt_msg, channel="whatsapp")
            elif len(pending) == 1:
                # תור יחיד — ישר לבחירת תאריך
                appt = pending[0]
                date_display = _format_date_display(appt.get("preferred_date", ""))
                reschedule_msg = (
                    f"🔄 שינוי תור:\n\n"
                    f"📋 *שירות:* {appt.get('service', '')}\n"
                    f"📅 *תאריך נוכחי:* {date_display}\n"
                    f"🕐 *שעה נוכחית:* {appt.get('preferred_time', '')}\n\n"
                    f"📅 מה *התאריך החדש* שמתאים לכם?\n"
                    f"(למשל: מחר, יום ראשון, 15/03)\n\n"
                    f"_(שלחו *ביטול* לביטול)_"
                )
                # מעדיפים את המשך שאושר בפועל (אם קיים), אחרת ברירת מחדל גלובלית
                svc_duration = db.resolve_appointment_duration_minutes(appt)
                _set_state(from_number, STATE_RESCHEDULE_DATE, {
                    "appt_id": appt["id"],
                    "service": appt.get("service", ""),
                    "service_duration": svc_duration,
                })
                _send_whatsapp_response(from_number, reschedule_msg)
                db.save_message(from_number, profile_name or from_number, "assistant", reschedule_msg, channel="whatsapp")
            else:
                # מספר תורים — שלב בחירה
                lines = ["איזה תור תרצו לשנות?\n"]
                appt_ids = []
                for i, appt in enumerate(pending, 1):
                    date_display = _format_date_display(appt.get("preferred_date", ""))
                    lines.append(f"{i}. {appt.get('service', '')} | {date_display} | {appt.get('preferred_time', '')}")
                    appt_ids.append(appt["id"])
                lines.append("\n_(שלחו את המספר, או *ביטול* לביטול)_")
                select_msg = "\n".join(lines)
                _set_state(from_number, STATE_RESCHEDULE_SELECT, {"appt_ids": appt_ids})
                _send_whatsapp_response(from_number, select_msg)
                db.save_message(from_number, profile_name or from_number, "assistant", select_msg, channel="whatsapp")

        elif result.action == "start_booking":
            from messaging.whatsapp_booking import start_booking
            booking_msg = start_booking(from_number)
            if booking_msg:
                # fallback טקסטואלי — שומרים ושולחים
                db.save_message(from_number, profile_name or from_number, "assistant", booking_msg, channel="whatsapp")
                _send_whatsapp_response(from_number, booking_msg)
            else:
                # List Picker כבר נשלח ישירות
                db.save_message(from_number, profile_name or from_number, "assistant", "[רשימת שירותים אינטראקטיבית]", channel="whatsapp")
        elif result.text:
            # בדיקת אורך תשובה — אם ארוכה מדי ל-WhatsApp, יוצרים עמוד HTML ציבורי
            from messaging.formatter import format_message
            formatted_text = format_message(result.text, "whatsapp")
            formatted_len = len(formatted_text)
            logger.info(
                "WhatsApp response length: html_len=%d, formatted_len=%d, limit=%d",
                len(result.text), formatted_len, WHATSAPP_MAX_LENGTH,
            )
            if formatted_len > WHATSAPP_MAX_LENGTH and ADMIN_URL:
                _send_as_page(from_number, result.text, result.intent,
                             rag_context=getattr(result, "rag_context", ""))
            else:
                _send_whatsapp_response(from_number, result.text)

        # שאלות המשך — Quick Reply עם עד 3 כפתורים (כמו inline keyboard בטלגרם)
        from ai_chatbot.config import FOLLOW_UP_ENABLED
        if FOLLOW_UP_ENABLED and result.follow_up_questions:
            _send_follow_up_buttons(from_number, result.follow_up_questions)

        # Opt-in פרואקטיבי (תיקון 40) — אם המשתמש כבר engage'ד עם הבוט
        # (3+ הודעות) ולא נשאל בעבר, נשלח לו בקשת הסכמה חד-פעמית. לא
        # מציקים יותר מפעם אחת, בלי קשר לתשובה.
        try:
            _maybe_send_opt_in_prompt(from_number)
        except Exception:
            logger.error(
                "WhatsApp opt-in prompt נכשל עבור %s", from_number, exc_info=True,
            )

    except Exception as e:
        logger.error("WhatsApp webhook processing error for %s: %s", from_number, e)

    # Twilio מצפה ל-200 OK (אפילו בשגיאה — כדי לא לגרום ל-retry מיותר)
    return "", 200


def _send_welcome_message(to_number: str) -> None:
    """שליחת הודעת פתיחה עם Quick Reply כפתורים (או fallback טקסטואלי).

    Quick Reply ב-session מוגבל ל-3 כפתורים — מציגים את השלושה הפופולריים.
    """
    returning = db.is_returning_customer(to_number)
    if returning:
        body = (
            f"😊 שמחים לראות אותך שוב ב-{get_business_config().name}!\n"
            "איך אפשר לעזור הפעם?"
        )
    else:
        body = (
            f"👋 ברוכים הבאים ל-{get_business_config().name}!\n\n"
            "אני העוזר הווירטואלי שלכם. אני יכול לעזור עם:\n"
            "• מידע על השירותים והמחירים\n"
            "• בקשת תורים\n"
            "• מענה על שאלות\n"
            "• חיבור לבעל העסק\n\n"
            "אפשר לכתוב כל שאלה, או לבחור:"
        )

    # ניסיון לשלוח Quick Reply עם 3 כפתורים (מגבלת session)
    try:
        from messaging.whatsapp_templates import ensure_quick_reply, send_with_template

        # friendly_name שונה ללקוח חוזר/חדש — כי ה-body שונה וה-template נקאש
        template_name = "welcome_menu_returning" if returning else "welcome_menu_new"
        content_sid = ensure_quick_reply(
            friendly_name=template_name,
            body=body,
            buttons=[
                ("📋 מחירון", "menu_price"),
                ("📅 בקשת תור", "menu_booking"),
                ("👤 דברו עם נציג", "menu_agent"),
            ],
        )
        send_with_template(to_number, content_sid)
        return
    except Exception:
        logger.warning("Quick Reply welcome נכשל, חוזרים לטקסט", exc_info=True)

    # fallback — טקסט ממוספר
    fallback = (
        body + "\n\n"
        "1. 📋 מחירון\n"
        "2. 📅 בקשת תור\n"
        "3. 📍 מיקום העסק\n"
        "4. 👤 דברו עם נציג\n\n"
        "_(שלחו את המספר או כתבו את הבקשה שלכם)_"
    )
    _send_whatsapp_response(to_number, fallback)


def _send_follow_up_buttons(to_number: str, questions: list[str]) -> None:
    """שליחת שאלות המשך כ-Quick Reply (עד 3 כפתורים) או fallback טקסטואלי.

    השאלות נשמרות ב-_follow_up_store כדי לשלוף את הטקסט המלא כשהמשתמש לוחץ
    (כפתור Quick Reply מוגבל ל-20 תווים — חותכים עם ...).
    """
    if not questions:
        return

    # שומרים עד 3 שאלות (מגבלת Quick Reply)
    trimmed = questions[:3]
    _follow_up_store[_follow_up_key(to_number)] = trimmed

    try:
        from messaging.whatsapp_templates import ensure_quick_reply, send_with_template

        # חיתוך כותרות ל-20 תווים (מגבלת Twilio Quick Reply title)
        # בלי emoji — ממקסם מקום לטקסט השאלה
        buttons = []
        for i, q in enumerate(trimmed):
            label = q if len(q) <= 20 else q[:17] + "..."
            buttons.append((label, f"followup_{i}"))

        content_sid = ensure_quick_reply(
            friendly_name="follow_up",
            body="💡 *אולי תרצו גם לשאול:*",
            buttons=buttons,
        )
        send_with_template(to_number, content_sid)
        return
    except Exception:
        logger.warning("Quick Reply follow-up נכשל, חוזרים לטקסט", exc_info=True)

    # fallback — טקסט עם bullets (לא מספרים! 1-4 תפוסים ע"י _MENU_MAP)
    lines = ["💡 *אולי תרצו גם לשאול:*\n"]
    for q in trimmed:
        lines.append(f"• {q}")
    lines.append("\n_(העתיקו את השאלה ושלחו)_")
    _send_whatsapp_response(to_number, "\n".join(lines))


def _handle_agent_request(from_number: str, result, *, profile_name: str = "") -> None:
    """יצירת בקשת נציג ב-DB + התראה לבעל העסק (WhatsApp או Telegram)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        display = profile_name or from_number
        message = result.agent_request_message or result.handoff_reason or ""
        request_id = db.create_agent_request(
            user_id=from_number,
            username=display,
            message=message,
            channel="whatsapp",
        )

        now_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
        panel_link = f"\n\n🔗 {ADMIN_URL}/requests" if ADMIN_URL else ""
        phone_display = _format_phone(from_number)
        notification = (
            f"🔔 בקשת נציג #{request_id} (WhatsApp)\n\n"
            f"לקוח: {display}\n"
            f"טלפון: {phone_display}\n"
            f"זמן: {now_il}\n\n"
            f"{message}"
            f"{panel_link}"
        )

        # התראה לבעל העסק ב-WhatsApp
        try:
            from messaging.whatsapp_sender import notify_owner_whatsapp
            notify_owner_whatsapp(notification)
        except Exception as e:
            logger.error("Failed to notify owner (WhatsApp) about agent request: %s", e)

        # fallback — אם מוגדר גם טלגרם, שולח גם שם
        if TELEGRAM_OWNER_CHAT_ID:
            try:
                from ai_chatbot.live_chat_service import send_telegram_message
                send_telegram_message(str(TELEGRAM_OWNER_CHAT_ID), notification)
            except Exception as e:
                logger.error("Failed to notify owner (Telegram) about agent request: %s", e)
    except Exception as e:
        logger.error("Failed to create agent request for WhatsApp user %s: %s", from_number, e)


def _send_cancel_confirmation_buttons(to_number: str, text: str) -> None:
    """שליחת כפתורי אישור ביטול תור (Quick Reply) או fallback טקסטואלי."""
    try:
        from messaging.whatsapp_templates import ensure_quick_reply, send_with_template
        content_sid = ensure_quick_reply(
            friendly_name="cancel_confirm",
            body=text,
            buttons=[
                ("✅ כן, לבטל", "cancel_appt_yes"),
                ("❌ לא, להשאיר", "cancel_appt_no"),
            ],
        )
        send_with_template(to_number, content_sid)
        return
    except Exception:
        logger.warning("Quick Reply cancel confirm נכשל, חוזרים לטקסט", exc_info=True)

    # fallback — טקסט ממוספר
    fallback = (
        f"{text}\n\n"
        "1. ✅ כן, לבטל\n"
        "2. ❌ לא, להשאיר\n\n"
        "_(שלחו את המספר)_"
    )
    _send_whatsapp_response(to_number, fallback)


def _handle_cancel_selection(from_number: str, text: str, session: dict) -> str | None:
    """טיפול בבחירת תור מרשימה (כשיש יותר מאחד).

    מחזיר הודעה אם הקלט לא תקין, או None אם עברנו לשלב אישור (כפתורים נשלחו).
    """
    from messaging.conversation_state import set_state, clear_state, STATE_CANCEL_CONFIRM
    from messaging.whatsapp_booking import _format_date_display
    normalized = text.strip()
    appt_ids = session.get("data", {}).get("appt_ids", [])

    # בדיקה אם המשתמש בחר מספר תקין
    try:
        choice = int(normalized)
    except ValueError:
        if normalized.lower() in {"ביטול", "לא", "no"}:
            clear_state(from_number)
            return "בסדר, לא מבטלים. 👍\nאיך עוד אפשר לעזור?"
        return f"שלחו מספר בין 1 ל-{len(appt_ids)}."

    if choice < 1 or choice > len(appt_ids):
        return f"שלחו מספר בין 1 ל-{len(appt_ids)}."

    appt_id = appt_ids[choice - 1]
    appt = db.get_appointment(appt_id)
    if not appt or appt["user_id"] != from_number:
        clear_state(from_number)
        return "התור לא נמצא. 🤔"

    # מעבר לשלב אישור עם ID ספציפי
    date_display = _format_date_display(appt.get("preferred_date", ""))
    confirm_text = (
        f"האם לבטל את התור הזה?\n\n"
        f"📋 *שירות:* {appt.get('service', '')}\n"
        f"📅 *תאריך:* {date_display}\n"
        f"🕐 *שעה:* {appt.get('preferred_time', '')}"
    )
    set_state(from_number, STATE_CANCEL_CONFIRM, {"appt_id": appt_id})
    _send_cancel_confirmation_buttons(from_number, confirm_text)
    # שמירת הודעת האישור בהיסטוריה — ההודעה נשלחת ישירות כאן ולא דרך הקוד הקורא
    db.save_message(from_number, from_number, "assistant", confirm_text, channel="whatsapp")
    return None


def _handle_cancel_confirmation(from_number: str, text: str) -> str | None:
    """טיפול בתשובת לקוח לאישור ביטול תור.

    מחזיר טקסט תשובה, או None אם הקלט לא מתאים לשום אפשרות.
    """
    from messaging.conversation_state import get_state, clear_state
    normalized = text.strip().rstrip("!?.").strip().lower()

    # מיפוי כפתור / מספר / טקסט חופשי → כן/לא
    yes_inputs = {"cancel_appt_yes", "1", "כן", "yes", "כן, לבטל", "✅ כן, לבטל"}
    no_inputs = {"cancel_appt_no", "2", "לא", "no", "לא, להשאיר", "❌ לא, להשאיר"}

    if normalized in yes_inputs:
        # שליפת appt_id מה-state (אם נשמר בשלב בחירה או תור יחיד)
        session = get_state(from_number)
        stored_appt_id = session.get("data", {}).get("appt_id") if session else None
        clear_state(from_number)

        if stored_appt_id:
            appt = db.get_appointment(stored_appt_id)
            if not appt or appt["user_id"] != from_number:
                return "התור לא נמצא. 🤔"
        else:
            # תאימות לאחור — בלי ID, לוקח את הראשון
            pending = db.get_pending_appointments_for_user(from_number)
            if not pending:
                return "לא רשום אצלנו תור על שמך. 🤔\nתרצו שאעביר את הבקשה לבעל העסק כדי לברר?"
            appt = pending[0]

        cancelled = db.cancel_appointment_and_sync(appt["id"], from_number)
        if not cancelled:
            return "לא הצלחנו לבטל את התור — ייתכן שהסטטוס שלו השתנה. 🤔"

        date_str = appt.get("preferred_date", "")
        time_str = appt.get("preferred_time", "")
        service = appt.get("service", "")
        # פורמט תאריך ידידותי — DD/MM/YYYY במקום YYYY-MM-DD
        from messaging.whatsapp_booking import _format_date_display
        date_display = _format_date_display(date_str)

        # התראה לבעל העסק על ביטול — כשל בהתראה לא צריך לשבור את התשובה ללקוח
        try:
            _notify_owner_cancellation(from_number, appt["id"], service, date_display, time_str)
        except Exception as e:
            logger.error("Failed to notify owner about cancellation #%s: %s", appt["id"], e)

        return (
            f"התור שלך בוטל בהצלחה ✅\n\n"
            f"📋 *שירות:* {service}\n"
            f"📅 *תאריך:* {date_display}\n"
            f"🕐 *שעה:* {time_str}\n\n"
            f"לקביעת תור חדש, שלחו *בקשת תור*."
        )

    if normalized in no_inputs:
        clear_state(from_number)
        return "בסדר גמור, התור נשאר! 👍\nאיך עוד אפשר לעזור?"

    # קלט לא מזוהה — שואלים שוב
    return None


def _notify_owner_cancellation(
    user_id: str, appt_id: int, service: str, date_display: str, time_str: str
) -> None:
    """התראה לבעל העסק על ביטול תור מ-WhatsApp."""
    display_name = db.get_username_for_user(user_id) or user_id
    phone_display = _format_phone(user_id)
    panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
    notification = (
        f"❌ ביטול תור #{appt_id} (WhatsApp)\n\n"
        f"לקוח: {display_name}\n"
        f"טלפון: {phone_display}\n"
        f"שירות: {service}\n"
        f"תאריך: {date_display}\n"
        f"שעה: {time_str}"
        f"{panel_link}"
    )
    try:
        from messaging.whatsapp_sender import notify_owner_whatsapp
        notify_owner_whatsapp(notification)
    except Exception as e:
        logger.error("Failed to notify owner (WhatsApp) about cancellation: %s", e)
    if TELEGRAM_OWNER_CHAT_ID:
        try:
            from live_chat_service import send_telegram_message
            send_telegram_message(str(TELEGRAM_OWNER_CHAT_ID), notification)
        except Exception as e:
            logger.error("Failed to notify owner (Telegram) about cancellation: %s", e)


def _handle_reschedule_step(from_number: str, text: str, session: dict) -> str | None:
    """ניתוב שלבי reschedule flow לפי ה-state הנוכחי."""
    from messaging.conversation_state import clear_state
    state = session.get("state", "")

    # ביטול — בכל שלב
    if text.strip().lower() in ("ביטול", "cancel", "בטל"):
        clear_state(from_number)
        return "בסדר, התור נשאר ללא שינוי! 👍\nאיך עוד אפשר לעזור?"

    if state == "reschedule_select":
        return _handle_reschedule_selection(from_number, text, session)
    elif state == "reschedule_date":
        return _handle_reschedule_date(from_number, text, session)
    elif state == "reschedule_time":
        return _handle_reschedule_time(from_number, text, session)
    elif state == "reschedule_confirm":
        return _handle_reschedule_confirmation(from_number, text, session)

    clear_state(from_number)
    return None


def _handle_reschedule_selection(from_number: str, text: str, session: dict) -> str | None:
    """בחירת תור מרשימה ממוספרת → מעבר לשלב בחירת תאריך."""
    from messaging.conversation_state import set_state, clear_state
    from messaging.conversation_state import STATE_RESCHEDULE_DATE
    from messaging.whatsapp_booking import _format_date_display

    appt_ids = session.get("data", {}).get("appt_ids", [])
    try:
        choice = int(text.strip())
    except ValueError:
        return f"שלחו מספר בין 1 ל-{len(appt_ids)}."

    if choice < 1 or choice > len(appt_ids):
        return f"שלחו מספר בין 1 ל-{len(appt_ids)}."

    appt_id = appt_ids[choice - 1]
    appt = db.get_appointment(appt_id)
    if not appt or appt["user_id"] != from_number:
        clear_state(from_number)
        return "התור לא נמצא. 🤔"

    date_display = _format_date_display(appt.get("preferred_date", ""))
    # מעדיפים את המשך שאושר בפועל (אם קיים), אחרת ברירת מחדל גלובלית
    svc_duration = db.resolve_appointment_duration_minutes(appt)

    set_state(from_number, STATE_RESCHEDULE_DATE, {
        "appt_id": appt_id,
        "service": appt.get("service", ""),
        "service_duration": svc_duration,
    })

    return (
        f"🔄 שינוי תור:\n\n"
        f"📋 *שירות:* {appt.get('service', '')}\n"
        f"📅 *תאריך נוכחי:* {date_display}\n"
        f"🕐 *שעה נוכחית:* {appt.get('preferred_time', '')}\n\n"
        f"📅 מה *התאריך החדש* שמתאים לכם?\n"
        f"(למשל: מחר, יום ראשון, 15/03)\n\n"
        f"_(שלחו *ביטול* לביטול)_"
    )


def _handle_reschedule_date(from_number: str, text: str, session: dict) -> str | None:
    """קלט תאריך חדש → בדיקת זמינות → מעבר לשלב בחירת שעה."""
    from messaging.conversation_state import set_state, get_session_data
    from messaging.conversation_state import STATE_RESCHEDULE_TIME
    from messaging.whatsapp_booking import _format_date_display
    from entity_extraction import normalize_date

    # תמיכה ב-date_YYYY-MM-DD (מ-list picker)
    if text.startswith("date_"):
        iso_part = text[5:]
        try:
            from datetime import date as _date_type
            _date_type.fromisoformat(iso_part)
            normalized = iso_part
        except ValueError:
            normalized = normalize_date(text)
    else:
        normalized = normalize_date(text)

    if normalized is None:
        return (
            "🤔 לא הצלחתי לזהות תאריך.\n\n"
            "אפשר לכתוב למשל:\n"
            "• מחר / מחרתיים\n"
            "• יום ראשון / ביום שלישי\n"
            "• 15/03 / 14 במרץ\n\n"
            "_(שלחו *ביטול* לביטול)_"
        )

    service_duration = session.get("data", {}).get("service_duration", 60)

    # בדיקת זמינות ביומן Google
    available_slots_text = ""
    no_slots = False
    try:
        from google_calendar import is_connected, get_available_slots
        if is_connected():
            from datetime import date as _date_type
            target = _date_type.fromisoformat(normalized)
            buf_min = db.get_auto_booking_buffer_minutes()
            slots = get_available_slots(
                target, service_duration_minutes=service_duration,
                buffer_after_event_minutes=buf_min,
            )
            if slots:
                slots_str = " | ".join(f"*{s}*" for s in slots)
                available_slots_text = f"\n\n🟢 שעות פנויות: {slots_str}"
            else:
                no_slots = True
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בבדיקת זמינות Google Calendar (reschedule WhatsApp)", exc_info=True)

    if no_slots:
        return (
            f"📅 תאריך: *{_format_date_display(normalized)}*\n\n"
            "🔴 אין שעות פנויות בתאריך זה.\n"
            "אנא כתבו *תאריך אחר*.\n\n"
            "_(שלחו *ביטול* לביטול)_"
        )

    set_state(from_number, STATE_RESCHEDULE_TIME, {"reschedule_date": normalized})

    return (
        f"📅 תאריך חדש: *{_format_date_display(normalized)}*{available_slots_text}\n\n"
        "🕐 איזו *שעה* מתאימה לכם?\n"
        "(לדוגמה: 10:00, אחר הצהריים, 14:00)\n\n"
        "_(שלחו *ביטול* לביטול)_"
    )


def _handle_reschedule_time(from_number: str, text: str, session: dict) -> str | None:
    """קלט שעה חדשה → הצגת סיכום לאישור."""
    from messaging.conversation_state import set_state, get_session_data
    from messaging.conversation_state import STATE_RESCHEDULE_CONFIRM
    from messaging.whatsapp_booking import _format_date_display

    new_time = text.strip()
    reschedule_date = session.get("data", {}).get("reschedule_date", "")
    service = get_session_data(from_number, "service", "")
    date_display = _format_date_display(reschedule_date)

    set_state(from_number, STATE_RESCHEDULE_CONFIRM, {"reschedule_time": new_time})

    confirm_msg = (
        f"🔄 *סיכום שינוי תור:*\n\n"
        f"📋 שירות: {service}\n"
        f"📅 תאריך חדש: {date_display}\n"
        f"🕐 שעה חדשה: {new_time}\n\n"
        f"לאשר את השינוי?\n"
        f"כתבו *כן* או *לא*:"
    )
    return confirm_msg


def _handle_reschedule_confirmation(from_number: str, text: str, session: dict) -> str | None:
    """אישור או ביטול שינוי התור."""
    from messaging.conversation_state import get_state, get_session_data, clear_state
    from messaging.whatsapp_booking import _format_date_display

    normalized = text.strip().rstrip("!?.").strip().lower()
    yes_inputs = {"1", "כן", "yes", "אישור"}
    no_inputs = {"2", "לא", "no"}

    if normalized in yes_inputs:
        appt_id = get_session_data(from_number, "appt_id")
        new_date = get_session_data(from_number, "reschedule_date", "")
        new_time = get_session_data(from_number, "reschedule_time", "")
        service = get_session_data(from_number, "service", "")
        clear_state(from_number)

        if not appt_id:
            return "שגיאה — התור לא נמצא. 🤔 נסו שוב."

        updated = db.update_appointment_and_sync(
            appt_id, from_number,
            preferred_date=new_date,
            preferred_time=new_time,
        )
        if not updated:
            return "לא הצלחנו לעדכן את התור — ייתכן שהסטטוס שלו השתנה. 🤔"

        date_display = _format_date_display(new_date)

        # התראה לבעל העסק
        try:
            _notify_owner_reschedule(from_number, appt_id, service, date_display, new_time)
        except Exception:
            logger.error("Failed to notify owner about reschedule #%s", appt_id, exc_info=True)

        return (
            f"התור עודכן בהצלחה ✅\n\n"
            f"📋 *שירות:* {service}\n"
            f"📅 *תאריך חדש:* {date_display}\n"
            f"🕐 *שעה חדשה:* {new_time}"
        )

    if normalized in no_inputs:
        clear_state(from_number)
        return "בסדר, התור נשאר ללא שינוי! 👍\nאיך עוד אפשר לעזור?"

    return "לא הבנתי — כתבו *כן* לאישור או *לא* לביטול."


def _notify_owner_reschedule(
    user_id: str, appt_id: int, service: str, date_display: str, time_str: str,
) -> None:
    """התראה לבעל העסק על שינוי תור מ-WhatsApp."""
    display_name = db.get_username_for_user(user_id) or user_id
    phone_display = _format_phone(user_id)
    panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
    notification = (
        f"🔄 שינוי תור #{appt_id} (WhatsApp)\n\n"
        f"לקוח: {display_name}\n"
        f"טלפון: {phone_display}\n"
        f"שירות: {service}\n"
        f"תאריך חדש: {date_display}\n"
        f"שעה חדשה: {time_str}"
        f"{panel_link}"
    )
    try:
        from messaging.whatsapp_sender import notify_owner_whatsapp
        notify_owner_whatsapp(notification)
    except Exception as e:
        logger.error("Failed to notify owner (WhatsApp) about reschedule: %s", e)
    if TELEGRAM_OWNER_CHAT_ID:
        try:
            from live_chat_service import send_telegram_message
            send_telegram_message(str(TELEGRAM_OWNER_CHAT_ID), notification)
        except Exception as e:
            logger.error("Failed to notify owner (Telegram) about reschedule: %s", e)


# מיפוי intent לכותרת עמוד עסקי
_INTENT_PAGE_TITLES = {
    "pricing": "מחירון",
    "location": "מיקום והגעה",
    "general": "מידע",
}


def _send_as_page(to_number: str, text: str, intent=None, rag_context: str = "") -> None:
    """יצירת עמוד HTML ציבורי ושליחת קישור ללקוח — לתשובות ארוכות מדי ל-WhatsApp.

    1. קריאת LLM שנייה שמייצרת תוכן עסקי נקי (ללא טון צ'אט).
    2. שמירה בטבלת response_pages.
    3. שליחת הודעה קצרה עם קישור לעמוד.
    """
    intent_val = intent.value if intent else "general"
    title = _INTENT_PAGE_TITLES.get(intent_val, "מידע")

    try:
        from llm import generate_page_content
        page_html = generate_page_content(text, title=title, rag_context=rag_context)
    except Exception as e:
        logger.error("שגיאה ביצירת תוכן עמוד עסקי: %s", e)
        # fallback — שולחים את ההודעה המקורית ישירות (Twilio יקצוץ).
        # חייבים _send_whatsapp_raw כדי לעקוף את בדיקת האורך — אחרת
        # _send_whatsapp_response → _send_as_page → fallback → ... infinite recursion.
        _send_whatsapp_raw(to_number, text)
        return

    try:
        # page_type='whatsapp_fallback' — מציין מפורש שזו תשתית פנימית
        # (עקיפת תקרת 1600 התווים של Twilio), לא דף landing פיצ'רי.
        page_id = db.create_response_page(
            content=page_html,
            title=title,
            user_id=to_number,
            page_type="whatsapp_fallback",
        )
    except Exception as e:
        logger.error("שגיאה בשמירת עמוד תשובה: %s", e)
        # fallback — אותו טעם כמו למעלה: _send_whatsapp_raw, לא דרך הצ'ק.
        _send_whatsapp_raw(to_number, text)
        return

    from public_urls import public_page_url
    page_url = public_page_url(page_id)
    short_msg = f"הכנתי עבורכם את כל המידע בעמוד נוח לקריאה:\n{page_url}"
    _send_whatsapp_response(to_number, short_msg)


def _send_whatsapp_raw(to_number: str, text: str) -> None:
    """שליחת WhatsApp ישירה דרך Twilio — *ללא* בדיקת אורך.

    משמש את ה-fallback paths של _send_as_page כשהמסלול דרך עמוד HTML נכשל
    (LLM error / DB error) — שם חייבים לשלוח את הטקסט המקורי גם אם הוא
    ארוך, כדי שהלקוח יקבל לפחות משהו (Twilio יקצוץ — best effort).

    אסור לקרוא מ-handler חיצוני! handlers חיצוניים (booking, RAG וכו')
    *חייבים* לעבור דרך _send_whatsapp_response שמבצע את בדיקת האורך.
    """
    try:
        from messaging.whatsapp_sender import send_whatsapp
        send_whatsapp(to_number, text)
    except Exception as e:
        logger.error("Failed to send WhatsApp response to %s: %s", to_number, e)


def _send_whatsapp_response(to_number: str, text: str) -> None:
    """שליחת תשובה ללקוח דרך Twilio WhatsApp API.

    אם ההודעה ארוכה מ-WHATSAPP_MAX_LENGTH (תקרה של Twilio שמעבר לה הודעות
    נחתכות בשקט) — מעבירים אוטומטית למסלול עמוד HTML ציבורי במקום לסכן
    קצירה באמצע משפט. הצ'ק כאן מהווה safety net אחרון; קוראים ספציפיים
    (כמו תגובת RAG) עשויים לבצע צ'ק מוקדם יותר עם הקשר נוסף.

    אין recursion: _send_as_page שולח את הקישור הקצר דרך פונקציה זו (ההודעה
    הקצרה מתחת לסף ⇒ עוברת ישירות), וה-fallback paths של _send_as_page
    משתמשים ב-_send_whatsapp_raw כדי לא לחזור לכאן עם הטקסט הארוך.
    """
    if len(text) > WHATSAPP_MAX_LENGTH and ADMIN_URL:
        try:
            _send_as_page(to_number, text)
            return
        except Exception:
            logger.error(
                "Failed to convert long message to HTML page (falling back to truncated send)",
                exc_info=True,
            )
            # נופלים לשליחה רגילה — Twilio יקצוץ אבל לפחות הלקוח יקבל משהו
    _send_whatsapp_raw(to_number, text)


def _maybe_handle_referral_code(from_number: str, profile_name: str, body: str) -> bool:
    """זיהוי הודעת קוד הפניה (REF_XXXXXXXX) ורישום ההפניה.

    מקבילה ל-Telegram /start REF_XXX. מחזיר True אם הקוד טופל וה-webhook
    צריך לעצור עיבוד נוסף; False אחרת.
    """
    text = (body or "").strip()
    # הודעה צריכה להתחיל ב-REF_ (טוקן יחיד); מתעלמים מטקסט חופשי שמכיל REF_ באמצע
    if not text.startswith("REF_"):
        return False
    code = text.split()[0]

    _upsert_whatsapp_user(from_number, profile_name)
    # רישום מנוי שידורים — מקבילה ל-ensure_user_subscribed שנקרא ב-/start
    # (bot/handlers.py:378) וב-process flow (whatsapp_webhook.py:453). בלי זה
    # משתמשים שנכנסים דרך לינק הפניה לא יקבלו שידורים עד ההודעה הבאה שלהם.
    db.ensure_user_subscribed(from_number)
    db.save_message(
        from_number, profile_name or from_number, "user", code, channel="whatsapp",
    )

    registered = db.register_referral(code, from_number)
    if registered:
        logger.info("Referral registered (WhatsApp): user %s via code %s", from_number, code)
        from ai_chatbot.referral_service import (
            format_referral_discount, format_referral_period,
        )
        settings = db.get_bot_settings()
        d_str = format_referral_discount(settings.get("referral_discount", 10.0))
        p_str = format_referral_period(settings.get("referral_validity_days", 60))
        reply = (
            f"👋 ברוכים הבאים ל-{get_business_config().name}!\n\n"
            "🎁 *הגעתם דרך הפניה!*\n"
            "לאחר שתקבעו ותשלימו את התור הראשון שלכם — "
            f"גם אתם וגם החבר/ה שהפנה אתכם תקבלו *{d_str} הנחה {p_str}!*\n\n"
            "כדי להתחיל, שלחו *בקשת תור* או כל שאלה."
        )
    else:
        # קוד לא קיים, הפניה עצמית, או שכבר רשום מהפניה אחרת — לא נחשוף את
        # הסיבה המדויקת. שולחים welcome רגיל כדי שלא יתקעו.
        reply = (
            f"👋 ברוכים הבאים ל-{get_business_config().name}!\n\n"
            "כדי להתחיל, שלחו *בקשת תור* או כל שאלה."
        )

    _send_whatsapp_response(from_number, reply)
    db.save_message(
        from_number, profile_name or from_number, "assistant", reply, channel="whatsapp",
    )
    return True
