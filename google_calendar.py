"""
Google Calendar Service — סנכרון תורים עם Google Calendar של בעל העסק.

תכונות:
- חיבור OAuth 2.0 עם offline access (refresh token)
- בדיקת זמינות דרך FreeBusy API
- יצירת/עדכון/מחיקת אירועים ביומן כשתור מאושר/משתנה/מבוטל
- חישוב slots פנויים לפי שעות עבודה + באפרים + עומס ביומן

ראה: https://developers.google.com/workspace/calendar/api/guides/overview
"""

import logging
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ai_chatbot.config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    BUSINESS_NAME,
)
import database as db

logger = logging.getLogger(__name__)

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# scopes נדרשים — קריאה+כתיבה ליומן
SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarUnavailable(Exception):
    """החיבור ל-Google Calendar לא תקין (טוקן פג, ניתוק, וכו').
    מובחן מ-"אין סלוטים תפוסים" — קוראים שמטפלים בזמינות חייבים להבחין
    בין "ריק" ל-"לא ניתן לבדוק" כדי לא לקבל החלטות אוטומטיות שגויות.
    """
    pass


def _notify_owner_calendar_disconnected() -> None:
    """שליחת התראה חד-פעמית לבעל העסק על בעיית חיבור GCal.
    בוחר בערוץ לפי הקיים (Telegram/WhatsApp) — בפרודקשן רק אחד מהם פעיל.
    """
    panel_url = ""
    try:
        from ai_chatbot.config import ADMIN_URL
        if ADMIN_URL:
            panel_url = f"\n\n🔗 חבר מחדש: {ADMIN_URL}/google-calendar"
    except Exception:
        pass

    text = (
        "⚠️ החיבור ל-Google Calendar פג תוקף.\n\n"
        "תורים חדשים שנקבעים דרך הבוט לא יסונכרנו ליומן עד שתחברו מחדש."
        f"{panel_url}"
    )

    # מסתמכים על ערכי החזר בוליאניים — אסור לסמן owner_alert_sent_at אם
    # לא נשלח בפועל לאף ערוץ, אחרת set_google_calendar_auth_invalid לא יקרא
    # שוב (מכבה התראות חוזרות) ובעל העסק יפספס אותה לתמיד.
    sent_any = False
    # Telegram
    try:
        from ai_chatbot.config import TELEGRAM_OWNER_CHAT_ID
        if TELEGRAM_OWNER_CHAT_ID:
            from live_chat_service import send_telegram_message
            if send_telegram_message(str(TELEGRAM_OWNER_CHAT_ID), text):
                sent_any = True
    except Exception:
        logger.error("נכשל בשליחת התראת GCal-disconnect לטלגרם", exc_info=True)
    # WhatsApp
    try:
        from messaging.whatsapp_sender import notify_owner_whatsapp
        if notify_owner_whatsapp(text):
            sent_any = True
    except Exception:
        logger.error("נכשל בשליחת התראת GCal-disconnect ל-WhatsApp", exc_info=True)

    if sent_any:
        try:
            db.mark_google_calendar_owner_alert_sent()
        except Exception:
            logger.error("נכשל בסימון owner_alert_sent_at", exc_info=True)
    else:
        # לא נשלח לאף ערוץ — לא מסמנים, כדי שהניסיון הבא של refresh
        # שייכשל יוכל להפעיל שוב את set_google_calendar_auth_invalid
        # (שמחזיר True רק כשהדגל NULL) ולנסות שליחה חוזרת.
        logger.warning(
            "GCal-disconnect: לא נשלחה התראה לאף ערוץ "
            "(TELEGRAM_OWNER_CHAT_ID/OWNER_WHATSAPP_NUMBER לא מוגדר או כשל). "
            "בעל העסק לא יקבל התראה — יש לבדוק קונפיגורציה.",
        )


# ── OAuth Flow ─────────────────────────────────────────────────────────────


def get_oauth_flow() -> Flow:
    """יצירת OAuth flow לחיבור Google Calendar."""
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


def get_authorization_url(state: str = "") -> tuple[str, str]:
    """יצירת URL להתחברות OAuth עם offline access.

    state — ערך אופציונלי שיוחזר ב-callback (למניעת CSRF).
    מחזיר (url, code_verifier) — יש לשמור את code_verifier ב-session לצורך ה-callback.
    """
    flow = get_oauth_flow()
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    # flow.code_verifier נוצר אוטומטית ע"י google_auth_oauthlib (PKCE)
    return url, flow.code_verifier


def exchange_code_for_credentials(code: str, code_verifier: str = "") -> dict:
    """המרת authorization code ל-credentials ושמירה ב-DB.

    code_verifier — ה-PKCE verifier שנוצר ב-get_authorization_url.
    מחזיר dict עם פרטי החשבון (email, calendar_id).
    """
    flow = get_oauth_flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials

    # שליפת כתובת האימייל של החשבון המחובר
    service = build("calendar", "v3", credentials=creds)
    calendar_info = service.calendars().get(calendarId="primary").execute()
    email = calendar_info.get("id", "primary")
    timezone = calendar_info.get("timeZone", "Asia/Jerusalem")

    db.save_google_calendar_credentials(
        google_account_email=email,
        calendar_id="primary",
        refresh_token=creds.refresh_token or "",
        access_token=creds.token or "",
        token_expiry=creds.expiry.isoformat() if creds.expiry else "",
        timezone=timezone,
    )

    logger.info("Google Calendar connected: %s", email)
    return {
        "email": email,
        "calendar_id": "primary",
        "timezone": timezone,
    }


def disconnect_calendar() -> None:
    """ניתוק Google Calendar — מחיקת credentials מה-DB."""
    db.delete_google_calendar_credentials()
    logger.info("Google Calendar disconnected")


# ── Calendar Service Helpers ───────────────────────────────────────────────


def _get_credentials() -> Credentials | None:
    """טעינת credentials מה-DB ויצירת אובייקט Credentials.

    מחזיר None אם אין חיבור פעיל.
    """
    cred_data = db.get_google_calendar_credentials()
    if not cred_data or not cred_data.get("refresh_token"):
        return None

    # פירוק token_expiry ל-datetime כדי שהספרייה תזהה מתי הטוקן פג
    expiry = None
    token_expiry_str = cred_data.get("token_expiry", "")
    if token_expiry_str:
        try:
            expiry = datetime.fromisoformat(token_expiry_str)
            # google-auth דורש naive UTC datetime
            if expiry.tzinfo is not None:
                from datetime import timezone as _tz
                expiry = expiry.astimezone(_tz.utc).replace(tzinfo=None)
        except (ValueError, TypeError):
            logger.warning("token_expiry לא תקין: %r", token_expiry_str)

    creds = Credentials(
        token=cred_data.get("access_token", ""),
        refresh_token=cred_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        expiry=expiry,
    )

    # עדכון access_token ב-DB אם חודש
    if creds.expired and creds.refresh_token:
        # google-auth הוא תלות חובה — הוא נטען בראש הקובץ. אסור fallback
        # ל-RefreshError=Exception כי זה היה הופך את מסלול "שגיאת רשת חולפת"
        # למת — כל timeout היה מסמן auth_invalid ושולח התראה כוזבת.
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
            db.update_google_calendar_token(
                access_token=creds.token,
                token_expiry=creds.expiry.isoformat() if creds.expiry else "",
            )
            # refresh הצליח — מאפסים דגל בריאות אם היה מסומן
            try:
                if db.is_google_calendar_auth_invalid():
                    db.clear_google_calendar_auth_invalid()
                    logger.info("Google Calendar refresh הצליח — דגל auth_invalid אופס")
            except Exception:
                logger.error("נכשל באיפוס דגל auth_invalid", exc_info=True)
        except RefreshError:
            logger.error("נכשל חידוש access token ל-Google Calendar", exc_info=True)
            # invalid_grant / token revoked / expired — מסמנים את החיבור כשבור
            # ושולחים התראה לבעל העסק. הטריגר הוא "טרם נשלחה" ולא "פעם
            # ראשונה" — אחרת כשל-שליחה ראשון (Twilio down, רשת) היה גורם
            # לבעל העסק לפספס את ההתראה לעולם. הניסיון מסתיים ברגע
            # שאחת השליחות הצליחה — owner_alert_sent_at מוגדר ולא נכנסים שוב.
            try:
                db.set_google_calendar_auth_invalid()
                creds_after = db.get_google_calendar_credentials()
                if creds_after and not creds_after.get("owner_alert_sent_at"):
                    _notify_owner_calendar_disconnected()
            except Exception:
                logger.error("נכשל בסימון auth_invalid", exc_info=True)
            return None
        except Exception:
            # שגיאות אחרות (רשת/timeout) — לא לסמן invalid כדי שננסה שוב בריצה הבאה
            logger.error("נכשל חידוש access token ל-Google Calendar", exc_info=True)
            return None

    return creds


def _get_calendar_service():
    """יצירת Google Calendar API service.

    מחזיר None אם אין חיבור פעיל.
    """
    creds = _get_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def is_connected() -> bool:
    """בדיקה אם Google Calendar מחובר *ובריא*.

    מחזיר False אם:
    - אין refresh_token ב-DB (לא חובר אף פעם / נותק)
    - יש דגל auth_invalid_at (refresh נכשל בעבר ולא תוקן עדיין)

    ההפרדה הזו קריטית: לפני התיקון הפונקציה הזו החזירה True גם כשטוקן
    שבור, וה-decision logic של אישור אוטומטי קיבל "מחובר" שגוי, חישב
    סלוטים על בסיס שעות עבודה בלבד, ואישר תורים שלא יכלו להיכתב ליומן.

    בדיקת auth_invalid ב-UI/דשבורד נעשית inline על cred_data.get("auth_invalid_at")
    כדי לחסוך שאילתת DB נוספת — לכן אין כאן helper ייעודי.
    """
    cred_data = db.get_google_calendar_credentials()
    if not (cred_data and cred_data.get("refresh_token")):
        return False
    if cred_data.get("auth_invalid_at"):
        return False
    return True


def get_connection_info() -> dict | None:
    """החזרת פרטי החיבור הנוכחי, או None אם לא מחובר."""
    return db.get_google_calendar_credentials()


# ── FreeBusy — בדיקת זמינות ────────────────────────────────────────────────


def get_busy_slots(
    time_min: datetime,
    time_max: datetime,
    timezone: str = "Asia/Jerusalem",
) -> list[dict]:
    """שליפת טווחי זמן תפוסים מ-Google Calendar FreeBusy API.

    מחזיר רשימת dicts עם 'start' ו-'end' (ISO strings).
    זורק CalendarUnavailable אם השירות לא זמין (טוקן שבור, ניתוק, שגיאת
    רשת/HttpError) — להבדיל מתוצאה ריקה לגיטימית. הקוראים חייבים להבחין
    כדי לא לקבל "אין busy slots" שגוי כשבעצם אי-אפשר לבדוק.
    """
    service = _get_calendar_service()
    if not service:
        raise CalendarUnavailable("Google Calendar service unavailable (auth/connection)")

    cred_data = db.get_google_calendar_credentials()
    calendar_id = cred_data.get("calendar_id", "primary") if cred_data else "primary"

    try:
        body = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "timeZone": timezone,
            "items": [{"id": calendar_id}],
        }
        result = service.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return busy
    except HttpError as e:
        logger.error("שגיאה בשליפת FreeBusy מ-Google Calendar", exc_info=True)
        raise CalendarUnavailable("FreeBusy query failed") from e


def get_available_slots(
    target_date: date,
    service_duration_minutes: int = 60,
    buffer_after_minutes: int = 0,
    buffer_after_event_minutes: int = 0,
    exclude_appointment_id: int | None = None,
) -> list[str]:
    """חישוב שעות פנויות ביום נתון.

    משלב שעות עבודה מה-DB + עומס מ-Google Calendar.
    מחזיר רשימת שעות בפורמט "HH:MM".

    buffer_after_minutes — מרחיב את גודל הסלוט עצמו (השירות + buffer).
    buffer_after_event_minutes — מרחיב את הסיום של כל אירוע *חיצוני* ביומן,
        כדי לחסום סלוטים שעלולים להיתקל בתור קודם שגלש (לא יודעים את משכו האמיתי).
    exclude_appointment_id — מזהה תור להתעלמות בשליפת busy ranges מ-DB. נחוץ
        להחלטת auto-booking שרצה אחרי create_appointment, כדי שהתור החדש לא
        יחסום את השעה של עצמו (calendar_busy כוזב).
    """
    from business_hours import get_status_for_date

    # בדיקת שעות עבודה
    day_status = get_status_for_date(target_date)
    if not day_status.get("is_open"):
        return []

    open_time_str = day_status.get("open_time") or "00:00"
    close_time_str = day_status.get("close_time") or "23:59"

    try:
        open_time = time.fromisoformat(open_time_str)
        close_time = time.fromisoformat(close_time_str)
    except (ValueError, TypeError):
        # שעות לא תקינות (למשל "-") — fallback לפתוח כל היום
        logger.warning("שעות עבודה לא תקינות: %s-%s — fallback ל-00:00-23:59", open_time_str, close_time_str)
        open_time = time(0, 0)
        close_time = time(23, 59)

    # חלון הבדיקה — כל היום העסקי, אבל אם זה היום — מתחילים מעכשיו
    tz = ZoneInfo("Asia/Jerusalem")
    day_start = datetime.combine(target_date, open_time, tzinfo=tz)
    day_end = datetime.combine(target_date, close_time, tzinfo=tz)

    now = datetime.now(tz)
    if target_date == now.date() and now > day_start:
        # עיגול למעלה לגבול 30 דקות הבא — כדי שה-slots יישארו על הגריד (09:00, 09:30, ...)
        minutes_since_midnight = now.hour * 60 + now.minute
        next_slot_minutes = ((minutes_since_midnight // 30) + 1) * 30
        if next_slot_minutes >= 24 * 60:
            return []
        day_start = now.replace(hour=next_slot_minutes // 60, minute=next_slot_minutes % 60, second=0, microsecond=0)
        if day_start >= day_end:
            return []

    # שליפת טווחים תפוסים מגוגל
    busy_slots = get_busy_slots(day_start, day_end)
    logger.info(
        "get_available_slots(%s): day_start=%s, day_end=%s, busy_slots=%d",
        target_date, day_start.isoformat(), day_end.isoformat(), len(busy_slots),
    )

    # המרת busy slots ל-datetime
    event_buffer = timedelta(minutes=max(0, int(buffer_after_event_minutes or 0)))
    busy_ranges = []
    for slot in busy_slots:
        try:
            start = datetime.fromisoformat(slot["start"])
            end = datetime.fromisoformat(slot["end"])
            # המרה ל-timezone מקומי
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            else:
                start = start.astimezone(tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            else:
                end = end.astimezone(tz)
            # הרחבת אירועים חיצוניים — סופגים גלישה אפשרית של תור קודם
            if event_buffer:
                end = end + event_buffer
            busy_ranges.append((start, end))
        except (ValueError, KeyError):
            logger.warning("busy slot לא תקין: %s", slot)

    # ── תורים מ-DB (pending/confirmed) — סלוטים שכבר תפוסים גם אם לא
    # סונכרנו ל-GCal. קריטי: בלי זה הבוט מציע שעות שכבר נתפסו ב-panel.
    try:
        db_ranges = db.get_appointments_busy_ranges(
            target_date.isoformat(), exclude_appointment_id=exclude_appointment_id,
        )
        for start_min, end_min in db_ranges:
            db_start = datetime.combine(target_date, time(0, 0), tzinfo=tz) + timedelta(minutes=start_min)
            db_end = datetime.combine(target_date, time(0, 0), tzinfo=tz) + timedelta(minutes=end_min)
            if event_buffer:
                db_end = db_end + event_buffer
            busy_ranges.append((db_start, db_end))
        logger.info(
            "get_available_slots(%s): db_busy_ranges=%d (added to %d gcal)",
            target_date, len(db_ranges), len(busy_slots),
        )
    except Exception:
        logger.error("get_available_slots: שגיאה בשליפת תורים מ-DB", exc_info=True)

    # יצירת slots לפי משך השירות + באפר
    slot_duration = timedelta(minutes=service_duration_minutes + buffer_after_minutes)
    available = []
    current = day_start

    while current + timedelta(minutes=service_duration_minutes) <= day_end:
        slot_end = current + slot_duration
        # בדיקה שה-slot לא חופף עם אף טווח תפוס
        is_free = True
        for busy_start, busy_end in busy_ranges:
            # חפיפה: slot מתחיל לפני שהתפוס נגמר ונגמר אחרי שהתפוס מתחיל
            if current < busy_end and slot_end > busy_start:
                is_free = False
                break

        if is_free:
            available.append(current.strftime("%H:%M"))

        # קפיצה של 30 דקות (ברירת מחדל ל-slot grid)
        current += timedelta(minutes=30)

    logger.info("get_available_slots(%s): %d available slots found", target_date, len(available))
    return available


# ── אירועים — יצירה/מחיקה ─────────────────────────────────────────────────


def create_event(
    appt_id: int,
    service: str,
    customer_name: str,
    start_dt: datetime,
    end_dt: datetime,
    phone: str = "",
    location: str = "",
) -> str | None:
    """יצירת אירוע ב-Google Calendar לתור מאושר.

    מחזיר google_event_id אם הצליח, None אם נכשל.
    """
    service_api = _get_calendar_service()
    if not service_api:
        logger.warning("לא ניתן ליצור אירוע — Google Calendar לא מחובר")
        return None

    cred_data = db.get_google_calendar_credentials()
    calendar_id = cred_data.get("calendar_id", "primary") if cred_data else "primary"
    cal_timezone = cred_data.get("timezone", "Asia/Jerusalem") if cred_data else "Asia/Jerusalem"

    description_lines = [f"bookingId=appt_{appt_id}"]
    if phone:
        description_lines.append(f"טלפון: {phone}")
    description_lines.append("נוצר אוטומטית מצ'אטבוט")

    event_body = {
        "summary": f"תור: {service} - {customer_name}",
        "description": "\n".join(description_lines),
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": cal_timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": cal_timezone,
        },
    }
    if location:
        event_body["location"] = location

    try:
        event = service_api.events().insert(
            calendarId=calendar_id,
            body=event_body,
        ).execute()

        event_id = event.get("id", "")
        if event_id:
            db.set_appointment_google_event_id(appt_id, event_id)
            logger.info(
                "נוצר אירוע ביומן Google לתור #%d: event_id=%s",
                appt_id, event_id,
            )
        return event_id

    except HttpError:
        logger.error(
            "שגיאה ביצירת אירוע Google Calendar לתור #%d",
            appt_id, exc_info=True,
        )
        return None


def delete_event(google_event_id: str) -> bool:
    """מחיקת אירוע מ-Google Calendar (כשתור מבוטל).

    מחזיר True אם הצליח.
    """
    service_api = _get_calendar_service()
    if not service_api or not google_event_id:
        return False

    cred_data = db.get_google_calendar_credentials()
    calendar_id = cred_data.get("calendar_id", "primary") if cred_data else "primary"

    try:
        service_api.events().delete(
            calendarId=calendar_id,
            eventId=google_event_id,
        ).execute()
        logger.info("נמחק אירוע Google Calendar: %s", google_event_id)
        return True

    except HttpError as e:
        if e.resp.status == 410:
            # אירוע כבר נמחק — לא שגיאה אמיתית
            logger.info("אירוע Google Calendar כבר נמחק: %s", google_event_id)
            return True
        logger.error(
            "שגיאה במחיקת אירוע Google Calendar: %s",
            google_event_id, exc_info=True,
        )
        return False


# ── סנכרון תור ← יומן ────────────────────────────────────────────────────


def sync_appointment_to_calendar(appt: dict, status: str) -> None:
    """סנכרון שינוי סטטוס תור עם Google Calendar.

    נקרא מ-appointment_notifications.py כשבעל העסק מאשר/מבטל תור.
    """
    if not is_connected():
        return

    appt_id = appt.get("id")
    google_event_id = appt.get("google_event_id", "")

    if status == "confirmed":
        # יצירת אירוע חדש ביומן
        preferred_date = appt.get("preferred_date", "")
        preferred_time = appt.get("preferred_time", "")

        if not preferred_date or not preferred_time:
            logger.warning("תור #%s — חסר תאריך/שעה, דילוג על סנכרון יומן", appt_id)
            return

        try:
            tz = ZoneInfo("Asia/Jerusalem")
            # נרמול שעה — תמיכה ב"14:00" וגם "14:00:00"
            time_parts = preferred_time.split(":")
            hour = int(time_parts[0])
            minute = int(time_parts[1]) if len(time_parts) > 1 else 0

            start_dt = datetime(
                *map(int, preferred_date.split("-")),
                hour, minute,
                tzinfo=tz,
            )
            # משך התור — confirmed_duration_minutes (אם בעל העסק בחר באישור),
            # אחרת ברירת המחדל של השירות (60 דק׳ אם לא נמצא).
            duration_min = db.resolve_appointment_duration_minutes(appt)
            end_dt = start_dt + timedelta(minutes=duration_min)
        except (ValueError, IndexError):
            logger.error("שגיאה בפירוק תאריך/שעה לתור #%s", appt_id, exc_info=True)
            return

        # אם כבר יש אירוע — מוחקים ויוצרים חדש (למקרה שהשעה השתנתה)
        if google_event_id:
            delete_event(google_event_id)

        create_event(
            appt_id=appt_id,
            service=appt.get("service", ""),
            customer_name=appt.get("username", ""),
            start_dt=start_dt,
            end_dt=end_dt,
        )

    elif status == "cancelled":
        # מחיקת אירוע מהיומן
        if google_event_id:
            if delete_event(google_event_id):
                db.set_appointment_google_event_id(appt_id, "")
