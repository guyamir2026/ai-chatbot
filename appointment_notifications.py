"""
appointment_notifications — התראות סטטוס אוטומטיות לתורים.

שולח הודעת טלגרם ללקוח כשבעל העסק משנה סטטוס תור
(pending → confirmed / cancelled) דרך פאנל הניהול.
כולל גם תזכורות אוטומטיות יום לפני התור.

ראה: https://github.com/amirbiron/ai-business-bot/issues/80
"""

import logging
from datetime import datetime, timedelta
from html import escape as _esc

from live_chat_service import send_telegram_message, send_telegram_document, send_message_by_channel
from messaging.formatter import format_message
from config import get_business_config
import database as db

logger = logging.getLogger(__name__)


def _format_date_short(iso_date: str) -> str:
    """המרת YYYY-MM-DD לפורמט DD/MM/YYYY."""
    try:
        parts = iso_date.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (IndexError, AttributeError):
        return iso_date


def _add_minutes_to_hhmm(hhmm: str, minutes: int) -> str:
    """הוספת דקות לשעה בפורמט HH:MM. אם השעה גולשת ליום הבא, מצרפים '(למחרת)'
    כדי שהלקוח לא יראה '23:00–00:30' שנראה כאילו התור נגמר לפני שהתחיל.
    אם הקלט לא תקין — מחזיר את הקלט כמו שהוא.
    """
    try:
        parts = hhmm.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        total = h * 60 + m + int(minutes)
        day_minutes = 24 * 60
        if total >= day_minutes:
            # גלישה ליום הבא — מציגים את השעה המקורית ומסמנים מפורשות
            wrapped = total % day_minutes
            return f"{wrapped // 60:02d}:{wrapped % 60:02d} (למחרת)"
        return f"{total // 60:02d}:{total % 60:02d}"
    except (ValueError, IndexError, TypeError):
        return hhmm


def _build_confirmed_message(
    service: str,
    date: str,
    time: str,
    owner_message: str = "",
    duration_minutes: int | None = None,
) -> str:
    """בניית הודעת אישור תור.

    duration_minutes — אם מצוין, מציגים גם את שעת הסיום ("10:00–11:30, ~90 דק׳").
    """
    date_display = _format_date_short(date)
    if duration_minutes:
        end_time = _add_minutes_to_hhmm(time, duration_minutes)
        time_line = f"🕐 <b>שעה:</b> {_esc(time)}–{_esc(end_time)} (כ-{int(duration_minutes)} דק׳)"
    else:
        time_line = f"🕐 <b>שעה:</b> {_esc(time)}"

    lines = [
        f"התור שלך ב{_esc(get_business_config().name)} אושר ✅",
        "",
        f"📋 <b>שירות:</b> {_esc(service)}",
        f"📅 <b>תאריך:</b> {_esc(date_display)}",
        time_line,
    ]
    if owner_message:
        lines += ["", f"💬 {_esc(owner_message)}"]
    lines += ["", "נתראה! 😊"]
    return "\n".join(lines)


def _build_cancelled_message(
    service: str,
    date: str,
    time: str,
    owner_message: str = "",
) -> str:
    """בניית הודעת ביטול תור."""
    date_display = _format_date_short(date)
    lines = [
        f"😑 התור שלך ב{_esc(get_business_config().name)} בוטל",
        "",
        f"📋 <b>שירות:</b> {_esc(service)}",
        f"📅 <b>תאריך:</b> {_esc(date_display)}",
        f"🕐 <b>שעה:</b> {_esc(time)}",
    ]
    if owner_message:
        lines += ["", f"💬 {_esc(owner_message)}"]
    lines += ["", "לקביעת תור חדש, שלחו /book"]
    return "\n".join(lines)


# מיפוי סטטוס → פונקציית בניית הודעה
_MESSAGE_BUILDERS = {
    "confirmed": _build_confirmed_message,
    "cancelled": _build_cancelled_message,
}


def notify_appointment_status(appt: dict, owner_message: str = "") -> bool:
    """שליחת התראת סטטוס תור ללקוח בטלגרם.

    Parameters
    ----------
    appt : dict
        רשומת התור מה-DB (חייבת לכלול user_id, status, service,
        preferred_date, preferred_time).
    owner_message : str, optional
        הודעה אישית מבעל העסק שתצורף להתראה.

    Returns
    -------
    bool
        True אם ההודעה נשלחה בהצלחה, False אחרת.
    """
    status = appt.get("status", "")
    builder = _MESSAGE_BUILDERS.get(status)
    if builder is None:
        # אין התראה לסטטוס pending — רק לשינויים
        logger.debug(
            "Skipping notification for appointment #%s — status '%s' has no template",
            appt.get("id"), status,
        )
        return False

    user_id = appt.get("user_id")
    if not user_id:
        logger.warning(
            "Cannot notify — appointment #%s has no user_id", appt.get("id"),
        )
        return False

    # המשך מועבר רק להודעת אישור — בהודעת ביטול לא רלוונטי.
    builder_kwargs = dict(
        service=appt.get("service", ""),
        date=appt.get("preferred_date", ""),
        time=appt.get("preferred_time", ""),
        owner_message=owner_message.strip(),
    )
    if status == "confirmed":
        builder_kwargs["duration_minutes"] = db.resolve_appointment_duration_minutes(appt)
    text = builder(**builder_kwargs)

    # קביעת ערוץ — לפי עמודת channel בתור, או לפי ערוץ אחרון ידוע של המשתמש
    channel = appt.get("channel") or db.get_user_channel(user_id)

    if channel == "whatsapp":
        # WhatsApp — המרת HTML לפורמט WhatsApp ושליחה ללא parse_mode
        formatted = format_message(text, "whatsapp")
        success = send_message_by_channel(user_id, formatted, channel="whatsapp")
    else:
        # Telegram — שליחה עם HTML parse_mode
        success = send_telegram_message(user_id, text, parse_mode="HTML")

    if success:
        logger.info(
            "Sent %s notification to user %s (channel=%s) for appointment #%s",
            status, user_id, channel, appt.get("id"),
        )
    else:
        logger.error(
            "Failed to send %s notification to user %s (channel=%s) for appointment #%s",
            status, user_id, channel, appt.get("id"),
        )

    # שליחת קובץ יומן .ics — רק באישור תור וכשהפיצ'ר מופעל
    if status == "confirmed" and success:
        _send_ics_file(appt, channel)

    # סנכרון עם Google Calendar — יצירת/מחיקת אירוע
    try:
        from google_calendar import sync_appointment_to_calendar
        sync_appointment_to_calendar(appt, status)
    except ImportError:
        # google-api-python-client לא מותקן — דילוג שקט
        pass
    except Exception:
        logger.error(
            "שגיאה בסנכרון תור #%s עם Google Calendar",
            appt.get("id"), exc_info=True,
        )

    return success


# ── קובץ יומן .ics ──────────────────────────────────────────────────────────


def _send_ics_file(appt: dict, channel: str) -> None:
    """שליחת קובץ .ics ללקוח אחרי אישור תור (אם הפיצ'ר מופעל)."""
    settings = db.get_bot_settings()
    if not settings.get("ics_enabled", 1):
        return

    user_id = appt.get("user_id", "")
    service = appt.get("service", "")
    preferred_date = appt.get("preferred_date", "")
    preferred_time = appt.get("preferred_time", "")

    if not all([user_id, preferred_date, preferred_time]):
        logger.warning(
            "לא ניתן לשלוח קובץ ICS לתור #%s — חסרים שדות תאריך/שעה",
            appt.get("id"),
        )
        return

    try:
        from ics_service import generate_ics, generate_ics_filename

        # מעדיפים confirmed_duration_minutes שבעל העסק בחר באישור — אחרת
        # נופלים ל-duration_minutes של השירות. עקביות עם הודעת הטקסט שמציגה
        # ללקוח את שעת הסיום ועם האירוע ב-Google Calendar.
        duration = db.resolve_appointment_duration_minutes(appt)
        ics_data = generate_ics(
            service=service,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            duration_minutes=duration,
            description=f"תור ב{get_business_config().name}",
        )
        filename = generate_ics_filename(preferred_date)
        caption = "📅 לחצו על הקובץ כדי להוסיף את התור ליומן שלכם"

        if channel == "whatsapp":
            # WhatsApp: לא תומך ב-text/calendar כסוג media — Twilio שולחת אבל
            # WhatsApp משמיט את הקובץ ומציג רק את הטקסט (סוגי המסמכים
            # הנתמכים: PDF/DOCX/XLSX וכו', לא ICS). לכן שומרים את ה-ICS
            # ב-response_pages ושולחים URL כקישור לחיץ בגוף ההודעה.
            # כשהלקוח לוחץ, הדפדפן מוריד עם Content-Disposition: attachment
            # ומערכת ההפעלה מעבירה את הקובץ לאפליקציית היומן.
            from ai_chatbot.config import ADMIN_URL
            if not ADMIN_URL:
                logger.warning(
                    "ICS לא נשלח ב-WhatsApp לתור #%s — ADMIN_URL לא מוגדר",
                    appt.get("id"),
                )
                return
            # page_type='whatsapp_fallback' — קובץ ICS שמופץ דרך עמוד ציבורי
            # ב-WhatsApp הוא חלק מתשתית שליחת הודעות, לא פיצ'ר landing.
            page_id = db.create_response_page(
                content=ics_data.decode("utf-8"),
                title=filename.removesuffix(".ics"),
                user_id=user_id,
                page_type="whatsapp_fallback",
            )
            from public_urls import public_ics_url
            page_url = public_ics_url(page_id)
            body = f"📅 להוספת התור ליומן שלכם:\n{page_url}"
            from messaging.whatsapp_sender import send_whatsapp
            send_whatsapp(user_id, body)
            logger.info(
                "Sent ICS link via WhatsApp to user %s for appointment #%s",
                user_id, appt.get("id"),
            )
            return

        # טלגרם — שליחת הקובץ כמסמך
        ok = send_telegram_document(
            chat_id=user_id,
            file_data=ics_data,
            filename=filename,
            caption=caption,
        )
        if ok:
            logger.info("Sent ICS file to user %s for appointment #%s", user_id, appt.get("id"))
        else:
            logger.error("Failed to send ICS file to user %s for appointment #%s", user_id, appt.get("id"))
    except Exception:
        logger.error(
            "שגיאה ביצירת/שליחת קובץ ICS לתור #%s",
            appt.get("id"), exc_info=True,
        )


# ── תזכורות אוטומטיות ──────────────────────────────────────────────────────


def _format_hours_display(hours: float) -> str:
    """המרת מספר שעות לטקסט תצוגה בעברית."""
    if hours == 1:
        return "שעה"
    if hours == 2:
        return "שעתיים"
    if hours == int(hours):
        return f"{int(hours)} שעות"
    # חצאי שעות: 1.5 → "שעה וחצי", 2.5 → "שעתיים וחצי"
    whole = int(hours)
    if hours - whole == 0.5:
        if whole == 0:
            return "חצי שעה"
        if whole == 1:
            return "שעה וחצי"
        if whole == 2:
            return "שעתיים וחצי"
        return f"{whole} שעות וחצי"
    return f"{hours} שעות"


def _build_reminder_message(
    service: str,
    date: str,
    time: str,
    hours_before_display: str | None = None,
) -> str:
    """בניית הודעת תזכורת לתור. hours_before_display=None → תזכורת יום לפני, אחרת טקסט זמן לפני."""
    if hours_before_display is not None:
        header = f"🔔 תזכורת: יש לך תור עוד {hours_before_display} ב{_esc(get_business_config().name)}!"
    else:
        header = f"🔔 תזכורת: יש לך תור מחר ב{_esc(get_business_config().name)}!"
    date_display = _format_date_short(date)
    lines = [
        header,
        "",
        f"📋 <b>שירות:</b> {_esc(service)}",
        f"📅 <b>תאריך:</b> {_esc(date_display)}",
        f"🕐 <b>שעה:</b> {_esc(time)}",
        "",
        "נתראה! 😊",
    ]
    return "\n".join(lines)


def send_appointment_reminders() -> dict:
    """שליחת תזכורות לתורים מאושרים של מחר.

    בודק הגדרות (enabled/time) ושולח רק אם:
    - תזכורות מופעלות
    - השעה הנוכחית (ישראל) >= שעת השליחה המוגדרת
    - לתור לא נשלחה תזכורת עדיין

    Returns: {"sent": int, "failed": int, "skipped": str | None}
    """
    from zoneinfo import ZoneInfo
    israel_tz = ZoneInfo("Asia/Jerusalem")

    settings = db.get_bot_settings()
    if not settings.get("reminder_enabled", 1):
        return {"sent": 0, "failed": 0, "skipped": "disabled"}

    now_il = datetime.now(israel_tz)
    reminder_time_str = settings.get("reminder_time", "10:00")
    try:
        reminder_hour, reminder_minute = map(int, reminder_time_str.split(":"))
    except (ValueError, AttributeError):
        reminder_hour, reminder_minute = 10, 0

    # שולחים רק אחרי השעה המוגדרת
    if now_il.hour < reminder_hour or (now_il.hour == reminder_hour and now_il.minute < reminder_minute):
        return {"sent": 0, "failed": 0, "skipped": "not_yet"}

    # תורים של מחר (לפי שעון ישראל)
    tomorrow = (now_il + timedelta(days=1)).strftime("%Y-%m-%d")
    appointments = db.get_appointments_for_reminder(tomorrow)

    sent = 0
    failed = 0
    for appt in appointments:
        try:
            text = _build_reminder_message(
                service=appt.get("service", ""),
                date=appt.get("preferred_date", ""),
                time=appt.get("preferred_time", ""),
            )
            channel = appt.get("channel") or db.get_user_channel(appt["user_id"])
            if channel == "whatsapp":
                formatted = format_message(text, "whatsapp")
                success = send_message_by_channel(appt["user_id"], formatted, channel="whatsapp")
            else:
                success = send_telegram_message(appt["user_id"], text, parse_mode="HTML")
            if success:
                db.mark_reminder_sent(appt["id"])
                sent += 1
                logger.info("Sent reminder to user %s (channel=%s) for appointment #%s", appt["user_id"], channel, appt["id"])
            else:
                failed += 1
                logger.error("Failed to send reminder to user %s (channel=%s) for appointment #%s", appt["user_id"], channel, appt["id"])
        except Exception:
            failed += 1
            logger.error("Error sending reminder for appointment #%s", appt["id"], exc_info=True)

    if sent or failed:
        logger.info("Appointment reminders: %d sent, %d failed (target date: %s)", sent, failed, tomorrow)
    return {"sent": sent, "failed": failed, "skipped": None}


def send_second_reminders() -> dict:
    """שליחת תזכורת שנייה — X שעות לפני התור (ברירת מחדל 2).

    בודק הגדרות (second_reminder_enabled, second_reminder_hours) ושולח
    רק לתורים שהשעה שלהם בחלון של 30 דקות מהזמן המוגדר לפני התור
    (מותאם לריצת הסקדיולר כל 30 דקות).

    Returns: {"sent": int, "failed": int, "skipped": str | None}
    """
    from zoneinfo import ZoneInfo
    israel_tz = ZoneInfo("Asia/Jerusalem")

    settings = db.get_bot_settings()
    if not settings.get("second_reminder_enabled", 0):
        return {"sent": 0, "failed": 0, "skipped": "disabled"}

    hours_before = float(settings.get("second_reminder_hours", 2.0))
    now_il = datetime.now(israel_tz)

    # חלון: תורים שמתחילים בעוד hours_before עד hours_before+0.5 שעות
    window_start = now_il + timedelta(hours=hours_before)
    window_end = now_il + timedelta(hours=hours_before, minutes=30)

    # אם החלון חוצה חצות — מפצלים לשני טווחים (לפני ואחרי חצות)
    if window_start.date() == window_end.date():
        ranges = [(
            window_start.strftime("%Y-%m-%d"),
            window_start.strftime("%H:%M"),
            window_end.strftime("%H:%M"),
        )]
    else:
        # חלק ראשון: מ-window_start עד 23:59 ביום הראשון
        ranges = [
            (window_start.strftime("%Y-%m-%d"), window_start.strftime("%H:%M"), "24:00"),
            (window_end.strftime("%Y-%m-%d"), "00:00", window_end.strftime("%H:%M")),
        ]

    appointments = db.get_appointments_for_second_reminder(ranges)

    # טקסט תצוגה: "שעתיים" / "שעה" / "3 שעות" / "שעה וחצי" וכו'
    hours_display = _format_hours_display(hours_before)

    sent = 0
    failed = 0
    for appt in appointments:
        try:
            text = _build_reminder_message(
                service=appt.get("service", ""),
                date=appt.get("preferred_date", ""),
                time=appt.get("preferred_time", ""),
                hours_before_display=hours_display,
            )
            channel = appt.get("channel") or db.get_user_channel(appt["user_id"])
            if channel == "whatsapp":
                formatted = format_message(text, "whatsapp")
                success = send_message_by_channel(appt["user_id"], formatted, channel="whatsapp")
            else:
                success = send_telegram_message(appt["user_id"], text, parse_mode="HTML")
            if success:
                db.mark_second_reminder_sent(appt["id"])
                sent += 1
                logger.info("Sent %sh reminder to user %s (channel=%s) for appointment #%s", hours_before, appt["user_id"], channel, appt["id"])
            else:
                failed += 1
                logger.error("Failed to send %sh reminder to user %s (channel=%s) for appointment #%s", hours_before, appt["user_id"], channel, appt["id"])
        except Exception:
            failed += 1
            logger.error("Error sending %sh reminder for appointment #%s", hours_before, appt["id"], exc_info=True)

    if sent or failed:
        logger.info("Second reminders: %d sent, %d failed (ranges: %s)", sent, failed, ranges)
    return {"sent": sent, "failed": failed, "skipped": None}
