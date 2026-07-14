"""
Broadcast Campaign Sender — מנוע שליחת קמפיין מבוסס-תבנית ל-WhatsApp.

זרימה:
    1. טוענים את הקמפיין; חייב להיות במצב draft.
    2. טוענים את התבנית; חייבת להיות במצב approved.
    3. ממירים סטטוס ל-sending (נעילה — לא ניתן לערוך draft מרגע זה).
    4. פותרים את רשימת הנמענים דרך list_wa_audience_eligible_user_ids
       (אכיפה חוזרת של opt-in ע"פ קטגוריה).
    5. לכל נמען: רישום delivery בתור + שליחה דרך Twilio Content API + עדכון
       סטטוס. כל קריאת I/O עטופה ב-try/except כך שכשל בודד לא עוצר את כל
       הקמפיין (CLAUDE.md — לולאות I/O ארוכות).
    6. בסיום: עדכון מונים מרוכזים ו-status=completed.

רינדור משתנים:
    בשלב 4 תומכים רק בערכים סטטיים (אותו ערך לכל הנמענים). per-user
    substitution (למשל {{1}} → user.first_name) יתווסף בשלב עתידי ע"י
    הרחבת render_variables_for_user().

הרצה ברקע:
    start_campaign_send() מפעילה thread — Flask handler מחזיר מיד עם 202
    והקמפיין ממשיך להישלח ברקע.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── הגדרות rate limiting ─────────────────────────────────────────────────────
# Twilio WABA מוגבל ל-~80 msg/sec ברירת מחדל. אנחנו שומרים מרווח זהיר —
# 10 msg/sec (100ms בין הודעות) — כדי להימנע מ-429. קמפיין של 1000 נמענים
# ייקח ~100 שניות. אם צריך מהר יותר — להקטין את הקבוע (אך לא מתחת ל-13ms).
_PACE_SLEEP_SECONDS = 0.1


def render_variables_for_user(
    template_variables: list,
    static_mapping: dict,
    user_id: str,
    user_row: Optional[dict] = None,
) -> dict:
    """המרת mapping ל-dict של variables לפי index, עם החלפת {{user:field}}.

    Twilio Content API דורש content_variables ב-JSON של {"1": "...", "2": "..."}.
    המיפוי יכול לכלול placeholders בסגנון {{user:username}} בתוך ערכים
    (למשל "היי {{user:username}}, תודה!"); הם מוחלפים per-recipient.

    Args:
        user_row: dict עם username / user_id כמפתחות. אם None — נבנה
                  minimal fallback שכולל רק user_id (בלי username).
                  הקורא (broadcast_sender) עושה batch-fetch לפני הלולאה
                  כדי לא לעשות N queries.
    """
    if user_row is None:
        user_row = {"user_id": user_id, "username": ""}

    result: dict[str, str] = {}
    for var in template_variables or []:
        idx = str(var.get("index", "") or "")
        if not idx:
            continue
        value = static_mapping.get(idx, "")
        if value is None:
            value = ""
        # substitute_user_fields עושה pass-through אם אין {{user:...}} בטקסט
        from messaging.template_renderer import substitute_user_fields
        result[idx] = substitute_user_fields(str(value), user_row)
    return result


def _send_to_one(
    content_sid: str,
    to_user_id: str,
    variables: dict,
    status_callback_url: Optional[str],
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """שליחה לנמען בודד דרך Twilio.

    Returns:
        (success, twilio_message_sid, error_code, error_message)
    """
    try:
        from ai_chatbot.config import TWILIO_WHATSAPP_NUMBER
        from messaging.whatsapp_sender import _get_twilio_client, _is_phone_number
        client = _get_twilio_client()

        # reverse lookup: אם ה-user_id הוא BSUID, מביאים טלפון מ-user_identities
        send_to = to_user_id
        if not _is_phone_number(to_user_id):
            try:
                from utils.user_identity import get_whatsapp_send_address
                resolved = get_whatsapp_send_address(to_user_id)
                if resolved:
                    send_to = resolved
            except Exception:
                logger.error(
                    "broadcast_sender: reverse lookup נכשל עבור %s", to_user_id,
                    exc_info=True,
                )

        # ולידציית פורמט ישראלי תקף לפני שליחה ל-Twilio — חוסך error codes
        # מבלבלים (21211/21408) על מספרים שגויים. BSUID לא נבדק (לא תקף
        # כמספר טלפון מראש; Twilio יכולה לטפל בפורמט זה בנפרד).
        if _is_phone_number(send_to):
            from utils.phone import is_valid_israeli_e164
            if not is_valid_israeli_e164(send_to):
                logger.warning(
                    "broadcast_sender: מספר לא תקף %s — מדלגים על שליחה",
                    send_to,
                )
                return (
                    False, None,
                    "INVALID_PHONE",
                    f"מספר {send_to} אינו +972XXXXXXXXX תקף",
                )

        kwargs = {
            "content_sid": content_sid,
            "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            "to": f"whatsapp:{send_to}",
        }
        if variables:
            kwargs["content_variables"] = json.dumps(variables, ensure_ascii=False)
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url

        message = client.messages.create(**kwargs)
        return True, message.sid, None, None
    except Exception as exc:
        # Twilio SDK זורק TwilioRestException עם code ו-msg; נכסה הכל ב-except כללי.
        error_code = getattr(exc, "code", None)
        error_message = str(exc)
        logger.error(
            "broadcast_sender: שליחה ל-%s נכשלה (code=%s): %s",
            to_user_id, error_code, error_message,
        )
        # המרה מפורשת של None → "" (ולא truthy-check דרך `or`), כי error_code
        # יכול להיות int 0 — ערך falsy אך תקף שהיינו רוצים לשמר.
        code_str = str(error_code) if error_code is not None else ""
        return False, None, code_str, error_message


def send_campaign(campaign_id: int) -> dict:
    """שליחה סינכרונית של קמפיין שלם. מוחזר dict עם סטטיסטיקות.

    לשימוש ישיר (טסטים/scripts סינכרוניים). בפרודקשן start_campaign_send
    מבצעת את הנעילה לפני הרקע, ואז קוראת ל-_send_campaign_locked.

    Concurrency: המעבר draft → sending מתבצע אטומית (compare-and-swap ב-DB)
    לפני ולידציה ופתרון נמענים.
    """
    from ai_chatbot import database as db

    stats = {"total": 0, "sent": 0, "failed": 0, "skipped": 0}

    # נעילה אטומית — אם כשל, מישהו אחר מטפל בקמפיין או שהוא לא draft.
    if not db.transition_campaign_status(campaign_id, "draft", "sending"):
        current = db.get_broadcast_campaign(campaign_id)
        logger.warning(
            "send_campaign: לא ניתן לנעול קמפיין %s (status=%s) — מדלג",
            campaign_id,
            current["status"] if current else "missing",
        )
        return stats

    return _run_locked_send_safely(campaign_id, stats)


def _run_locked_send_safely(campaign_id: int, stats: dict) -> dict:
    """עטיפת _send_campaign_locked ב-try/except שמסמנת failed אם הכל נפל.

    single source of recovery — משמשת גם ע"י send_campaign (סינכרוני) וגם
    ע"י ה-thread ב-start_campaign_send. בלי זה, exception לא-צפויה בתוך
    _send_campaign_locked היתה משאירה את הקמפיין תקוע ב-sending לנצח,
    ורק הנתיב האסינכרוני הכיל fallback.
    """
    try:
        return _send_campaign_locked(campaign_id)
    except Exception:
        logger.error(
            "broadcast_sender: _send_campaign_locked קרס עבור קמפיין %s",
            campaign_id, exc_info=True,
        )
        try:
            from ai_chatbot import database as db
            # אטומי: רק אם ה-status עדיין sending. אם המשתמש לחץ pause
            # (status=paused) וה-exception קרה בסיום הלולאה, אסור לדרוס
            # ל-failed — ה-admin ציפה שהקמפיין יחכה ל-resume.
            db.transition_campaign_status(campaign_id, "sending", "failed")
        except Exception:
            logger.error(
                "broadcast_sender: גם עדכון failed נכשל עבור %s",
                campaign_id, exc_info=True,
            )
        return stats


def _send_campaign_locked(campaign_id: int) -> dict:
    """מניח שהקמפיין כבר ב-'sending' (הנעילה כבר בוצעה ע"י הקורא).

    משמש גם ע"י send_campaign (סינכרוני — אחרי transition עצמי) וגם ע"י
    ה-thread שמופעל מ-start_campaign_send (הנעילה בוצעה במערך ה-HTTP
    כדי שדף הפירוט יראה status=sending מיד וההתקדמות תתחיל ב-polling).
    """
    from ai_chatbot import database as db

    stats = {"total": 0, "sent": 0, "failed": 0, "skipped": 0}

    # מרגע זה הקמפיין ב-sending. כל כישלון בהמשך משאיר אותו ב-failed.
    campaign = db.get_broadcast_campaign(campaign_id)
    if not campaign:
        # edge case נדיר — השורה נעלמה בין ה-lock ל-get. בלי set_campaign_status
        # הקמפיין היה נשאר תקוע ב-sending לנצח. קוראים בכל זאת (אם השורה לא
        # קיימת זה no-op, ואם היא כן קיימת הסטטוס יוחלף ל-failed).
        logger.error("send_campaign: קמפיין %s נעלם אחרי הנעילה", campaign_id)
        db.set_campaign_status(campaign_id, "failed")
        return stats

    template = db.get_whatsapp_template(campaign["template_sid"])
    if not template:
        db.set_campaign_status(campaign_id, "failed")
        logger.error("send_campaign: תבנית %s לא נמצאה — הקמפיין סומן כ-failed",
                     campaign["template_sid"])
        return stats

    if template["approval_status"] != "approved":
        db.set_campaign_status(campaign_id, "failed")
        logger.error(
            "send_campaign: תבנית %s במצב %s — לא ניתן לשלוח broadcast",
            template["content_sid"], template["approval_status"],
        )
        return stats

    # Pre-flight של חלון שבת/חגים — רק ל-MARKETING. אם הקמפיין מופעל מיד
    # ("שלח עכשיו") בתוך חלון חסום, נסמן failed ונבקש מהמנהל לתזמן
    # למוצ"ש/אחרי החג. תזמון אוטומטי מגיע בשלב 5b (scheduler).
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from messaging.shabbat_window import is_blocked_for_marketing

    # המרת category ל-upper פעם אחת בראש הפונקציה; גם ה-DB constraint
    # שומר uppercase אבל הגנה הזו מונעת false-negative אם רשומה נכנסה
    # בצורה עוקפת (admin manual SQL, migration חלקית). אותה סמנטיקה
    # כמו scheduler.
    category = (template.get("category") or "UTILITY").upper()
    if category == "MARKETING":
        now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
        blocked, reason = is_blocked_for_marketing(now_il)
        if blocked:
            db.set_campaign_status(campaign_id, "failed")
            logger.warning(
                "send_campaign: קמפיין MARKETING %s נחסם — %s",
                campaign_id, reason,
            )
            return stats

    # פתרון קהל יעד — אכיפה חוזרת של opt-in לפי קטגוריה + UI choice.
    # MARKETING תמיד דורש opt-in (חובה רגולטורית + belt-and-braces אם draft
    # נרשם שגוי). עבור UTILITY/AUTH — בחירת המשתמש ב-UI (opted_in_only/all)
    # מכובדת כפרמטר explicit require_opt_in.
    audience_type = campaign.get("audience_type") or "opted_in_only"
    if category == "MARKETING":
        audience_type = "opted_in_only"
    require_opt_in = audience_type == "opted_in_only"

    audience_filter = campaign.get("audience_filter") or {}
    inactive_days = audience_filter.get("inactive_days")

    # ריצה ראשונה vs. resume/retry: אם כבר קיימות שורות delivery לקמפיין,
    # זו ריצה חוזרת (resume אחרי pause או retry-failed אחרי requeue).
    # בריצה חוזרת: משתמשים רק ב-queued הקיימים (לא מוסיפים משתמשים חדשים
    # שהצטרפו אחרי הריצה הראשונה).
    # בריצה ראשונה: פותרים audience + יוצרים את כל שורות ה-delivery מראש
    # ב-queued, כך ש-pause באמצע לא יאבד נמענים לא-מעובדים (כולם כבר
    # יושבים ב-queued, הלולאה פשוט תמשיך מהיכן שנעצרה).
    is_rerun = db.campaign_has_deliveries(campaign_id)
    if is_rerun:
        recipients = db.list_queued_user_ids_for_campaign(campaign_id)
        stats["total"] = len(recipients)
        if not recipients:
            # אין שום מה לשלוח (למשל retry בלי failed). ממשיכים ל-finalize
            # כדי שה-end-of-loop יסדר את הסטטוס לפי progress. לא נוגעים
            # ב-total_recipients שנכתב בריצה הראשונה.
            logger.info(
                "send_campaign: קמפיין %s re-run ללא queued — ממשיכים ל-finalize",
                campaign_id,
            )
    else:
        all_eligible = db.list_wa_audience_eligible_user_ids(
            category=category,
            inactive_days=inactive_days,
            require_opt_in=require_opt_in,
        )
        stats["total"] = len(all_eligible)

        if not all_eligible:
            db.set_campaign_status(campaign_id, "failed")
            db.set_campaign_counters(campaign_id, {"total_recipients": 0})
            logger.warning(
                "send_campaign: קמפיין %s ללא נמענים כשירים", campaign_id,
            )
            return stats

        db.set_campaign_counters(
            campaign_id, {"total_recipients": stats["total"]},
        )
        # Pre-create של כל שורות ה-delivery ב-queued — קריטי לנכונות
        # pause/resume: אם pause קורה באמצע הלולאה, כל מי שלא נשלח עדיין
        # כבר יושב ב-queued ו-resume ימשיך מהם. אחרת היו נשלפים רק אלה
        # שעברו את create_delivery_queue, והשאר היו נעלמים.
        db.bulk_create_queued_deliveries(campaign_id, all_eligible)
        recipients = all_eligible

    # URL ל-status callback — Twilio ישלח עדכוני סטטוס לכאן
    status_callback_url: Optional[str] = None
    try:
        from public_urls import whatsapp_status_callback_url
        status_callback_url = whatsapp_status_callback_url()
    except Exception:
        # ייבוא של config עלול לזרוק בעת startup חריג (missing dep / syntax
        # error). רושמים traceback מלא כדי לא להסתיר באגים אמיתיים מאחורי
        # ההודעה הגנרית על ADMIN_URL.
        logger.error(
            "broadcast_sender: שגיאה בייבוא ADMIN_URL — status callback מבוטל",
            exc_info=True,
        )

    template_variables = template.get("variables") or []
    static_mapping = campaign.get("variable_mapping") or {}

    # Batch-fetch של מידע על הנמענים — חוסך N queries בלולאה של שליחה.
    # משמש ל-per-user substitution ({{user:username}} וכו' בתוך ערכי mapping).
    try:
        users_info = db.get_users_for_broadcast(recipients)
        user_info_map = {u["user_id"]: u for u in users_info}
    except Exception:
        logger.error(
            "broadcast_sender: batch-fetch של משתמשים נכשל — ממשיכים בלי user-fields",
            exc_info=True,
        )
        user_info_map = {}

    # תדירות בדיקת pause/cancel — כל N איטרציות, למנוע query מיותר
    # לכל נמען. הערך נמוך מספיק כדי שה-admin יראה תגובה תוך 1-2 שניות
    # (10 נמענים * 100ms rate-limit ≈ 1 שנייה לבדיקה הבאה).
    _PAUSE_CHECK_EVERY = 10

    # לולאת שליחה — כל איטרציה עטופה כדי שכשל בודד לא יעצור את השאר.
    for i, user_id in enumerate(recipients):
        # בדיקה מחזורית: אם המנהל לחץ pause/cancel, עוצרים בלי לשלוח
        # הודעות נוספות. הסטטוס כבר עודכן ב-DB ע"י ה-route.
        if i > 0 and i % _PAUSE_CHECK_EVERY == 0:
            try:
                current_status = db.get_campaign_status(campaign_id)
            except Exception:
                current_status = "sending"  # fail-open — ממשיכים
            if current_status != "sending":
                logger.info(
                    "send loop: קמפיין %s במצב %s — עוצר (נשלחו %d מתוך %d)",
                    campaign_id, current_status, stats["sent"], len(recipients),
                )
                break

        delivery_id: Optional[int] = None
        twilio_sid: Optional[str] = None
        try:
            user_row = user_info_map.get(
                user_id, {"user_id": user_id, "username": ""},
            )
            rendered = render_variables_for_user(
                template_variables, static_mapping, user_id, user_row=user_row,
            )
            delivery_id, should_send = db.create_delivery_queue(
                campaign_id, user_id, rendered_variables=rendered,
            )
            if not should_send:
                # שורה קיימת כבר עברה שליחה (sent/delivered/read) או כשל
                # שלא אופס. מדלגים כדי לא לשלוח duplicate. רק queued
                # (ריצה חדשה, resume אחרי pause, או retry-failed) מתקדם.
                stats["skipped"] += 1
                continue

            success, msg_sid, error_code, error_msg = _send_to_one(
                content_sid=template["content_sid"],
                to_user_id=user_id,
                variables=rendered,
                status_callback_url=status_callback_url,
            )
            twilio_sid = msg_sid
            if success and msg_sid:
                db.mark_delivery_sent(delivery_id, msg_sid)
                stats["sent"] += 1
            else:
                db.mark_delivery_failed(delivery_id, error_code or "", error_msg or "")
                stats["failed"] += 1
        except Exception as exc:
            logger.error(
                "broadcast_sender: שגיאה לא צפויה באיטרציה של %s "
                "(delivery_id=%s, twilio_sid=%s)",
                user_id, delivery_id, twilio_sid,
                exc_info=True,
            )
            stats["failed"] += 1
            # הגנה מ-orphaned queued: אם כבר יצרנו שורת delivery, לא מניחים
            # אותה ב-queued לנצח — מסמנים מצב סיום מתאים.
            if delivery_id:
                try:
                    if twilio_sid:
                        # Twilio קיבלה את ההודעה (יש SID), אבל פעולת DB
                        # אחרי כן נכשלה. שומרים את ה-SID כדי שה-status
                        # callback העתידי מ-Twilio ימצא את השורה ויעדכן סטטוס.
                        db.mark_delivery_sent(delivery_id, twilio_sid)
                    else:
                        # אין SID — ההודעה לא הגיעה ל-Twilio (או נכשלה ברינדור).
                        db.mark_delivery_failed(
                            delivery_id, "local_error", str(exc)[:500],
                        )
                except Exception:
                    logger.error(
                        "broadcast_sender: גם finalize של delivery %s נכשל",
                        delivery_id, exc_info=True,
                    )

        # rate limiting — לא בתיעוד האחרון כדי לא לעכב את הסיכום.
        if i + 1 < len(recipients):
            time.sleep(_PACE_SLEEP_SECONDS)

    # עדכון סופי של מונים + סטטוס.
    # חשוב: קוראים ל-get_campaign_progress במקום להסתמך על stats הלוקליים,
    # כי webhooks עשויים כבר להיות עודכנו deliveries במהלך הלולאה (הודעה
    # שנשלחה בהתחלה כבר קיבלה delivered/failed עדכון). stats["sent"]/["failed"]
    # יודעים רק על תוצאות create-time, לא על webhook updates —
    # שימוש בהם כאן היה דורס את המונים שה-webhook עדכן.
    # recompute אטומי מה-DB — מונע race עם webhooks שעיבדו סטטוסים במהלך
    # הלולאה ועל התחלת הסיום. כל הקריאות ל-set_campaign_counters שתלויות
    # ב-snapshot נפרד של get_campaign_progress יכלו להתעדכן בסדר לא עקבי.
    db.recompute_campaign_counters(campaign_id)
    # לקביעת הסטטוס הסופי עדיין צריכים accepted>0 → קריאה קצרה שלא מעדכנת כלום.
    final_progress = db.get_campaign_progress(campaign_id)
    final_status = "completed" if final_progress["accepted"] > 0 else "failed"

    # מעבר אטומי sending → final — TOCTOU-safe. אם ה-admin לחץ pause/resume
    # בזמן שרצנו או אם thread חדש שודרג ל-sending, transition_campaign_status
    # יחזיר False (ה-status השתנה מ-sending) ונשאיר אותו כמו שהוא.
    # בלי זה היה מרוץ: get_status ראה sending → set_status("completed") דרס
    # את מה שה-resume החדש יצר.
    transitioned = db.transition_campaign_status(
        campaign_id, "sending", final_status,
    )
    if not transitioned:
        # הסטטוס כבר שונה ע"י גורם אחר (paused/resume) — לא נוגעים בו.
        current_status = db.get_campaign_status(campaign_id)
        final_status = current_status or final_status

    logger.info(
        "broadcast_sender: קמפיין %s סיים — total=%d sent=%d failed=%d skipped=%d accepted=%d",
        campaign_id, stats["total"], stats["sent"], stats["failed"], stats["skipped"],
        final_progress["accepted"],
    )
    return stats


def _spawn_send_thread(campaign_id: int) -> bool:
    """הפעלת thread שמריץ _run_locked_send_safely עבור הקמפיין.

    מניח שהקורא כבר ביצע transition ל-sending (אטומי). משמש נתיבי retry
    ו-resume שצריכים לעשות עבודה נוספת בין הנעילה לכתיבת ה-thread (למשל
    requeue_failed_deliveries). הפרדה זו מאפשרת:
      1. transition → sending (atomic)
      2. mutation של deliveries (requeue / cleanup)
      3. spawn thread דרך הפונקציה הזו

    Returns:
        True אם ה-thread התחיל; False אם יצירת ה-thread נכשלה (במקרה כזה
        הקורא צריך לשחזר את הסטטוס המקורי).
    """
    def _run():
        _run_locked_send_safely(campaign_id, stats={
            "total": 0, "sent": 0, "failed": 0, "skipped": 0,
        })

    try:
        thread = threading.Thread(
            target=_run, daemon=True, name=f"campaign-send-{campaign_id}",
        )
        thread.start()
        return True
    except Exception:
        logger.error(
            "broadcast_sender: יצירת thread עבור קמפיין %s נכשלה",
            campaign_id, exc_info=True,
        )
        return False


def start_campaign_send(campaign_id: int, *, from_status: str = "draft") -> bool:
    """הפעלת שליחת קמפיין ברקע — לא חוסם את ה-HTTP request.

    הנעילה האטומית מתבצעת כאן (במערך ה-HTTP, לא ב-thread) כדי שדף
    הפירוט שנטען מיד אחרי ה-redirect יראה status='sending' ויפעיל
    את ה-HTMX polling של ההתקדמות. אם נעלנו ב-thread בלבד, היה מרוץ:
    ה-thread יכול עוד לא להגיע ל-transition, והדף היה טוען עם 'draft',
    ה-polling לא היה מופעל, והמנהל היה רואה תצוגה תקועה עד רענון ידני.

    Args:
        from_status: סטטוס הקמפיין הנוכחי שממנו עוברים ל-sending. ברירת
                     מחדל 'draft' (שליחה ידנית מהאדמין). ה-scheduler מעביר
                     'scheduled' כשהגיע הזמן המתוזמן, ו-resume מעביר 'paused'.

    Returns:
        True אם הנעילה הצליחה ו-thread הופעל; False אם הקמפיין אינו
        ב-from_status המצופה (כבר נשלח, נכשל, וכו') ולכן אין מה לעשות.
    """
    from ai_chatbot import database as db

    if not db.transition_campaign_status(campaign_id, from_status, "sending"):
        logger.warning(
            "start_campaign_send: לא ניתן לנעול קמפיין %s (from=%s)",
            campaign_id, from_status,
        )
        return False

    # ה-nail-down של ה-thread הוא אחריות של _spawn_send_thread — עוטף את
    # ה-try/except סביב threading.Thread.start(). אם נכשל, נשחזר את
    # ה-status כדי לא להישאר תקועים ב-sending בלי worker.
    if _spawn_send_thread(campaign_id):
        return True

    try:
        db.set_campaign_status(campaign_id, "failed")
    except Exception:
        logger.error(
            "start_campaign_send: גם עדכון failed נכשל עבור %s",
            campaign_id, exc_info=True,
        )
    return False


# ── Twilio status callback handling ──────────────────────────────────────────


def handle_status_callback(
    message_sid: str,
    message_status: str,
    error_code: str = "",
    error_message: str = "",
) -> bool:
    """טיפול בעדכון סטטוס מ-Twilio status webhook.

    מעדכן את ה-delivery המתאים ואת המונים המרוכזים של הקמפיין.
    מוחזר True אם ה-SID נמצא ועובד (גם אם הסטטוס עצמו לא שונה כי
    הוא כבר terminal או זהה למה ששמרנו). False אם ה-SID לא נמצא.
    """
    from ai_chatbot import database as db

    if not message_sid:
        return False

    # בדיקת קיום SID בנפרד מנסיון העדכון — כדי לא להתריע WARNING שגוי
    # כשהסטטוס פשוט לא מתקדם (duplicate callback, monotonic guard).
    # Twilio שולחת "sent" callback בעקבות מה שכבר סימנו מתגובת ה-API —
    # זה תרחיש נורמלי, לא באג.
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT campaign_id FROM broadcast_deliveries "
            "WHERE twilio_message_sid = ? LIMIT 1",
            (message_sid,),
        ).fetchone()
    if not row:
        logger.warning(
            "broadcast_sender: MessageSid %s לא נמצא ב-broadcast_deliveries",
            message_sid,
        )
        return False

    # מנסים לעדכן; False כאן = monotonic guard חסם (תרחיש תקין), לא באג.
    db.update_delivery_status_by_twilio_sid(
        message_sid, message_status, error_code, error_message,
    )

    # עדכון מונים מרוכזים של הקמפיין לקריאה מהירה ברשימה. גם אם הסטטוס
    # של ה-delivery לא השתנה, חישוב חוזר של המונים לא מזיק (אותם ערכים).
    try:
        campaign_id = int(row["campaign_id"])
        # recompute אטומי — מונע race עם עדכון סיום של send-loop ועם webhooks
        # מקבילים. הכל מחושב בתוך UPDATE אחד, אין חלון בין read ל-write.
        db.recompute_campaign_counters(campaign_id)
    except Exception:
        logger.error(
            "broadcast_sender: עדכון מונים נכשל עבור %s", message_sid,
            exc_info=True,
        )

    return True
