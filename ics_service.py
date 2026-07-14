"""
ics_service — יצירת קבצי יומן .ics (iCalendar) לתורים.

יוצר קובץ .ics בזיכרון עם פרטי התור: שם שירות, תאריך, שעה,
כתובת העסק, ותזכורת שעה לפני. הקובץ נשלח ללקוח כמסמך בטלגרם.

ראה: https://github.com/amirbiron/ai-business-bot/issues/173
"""

import logging
import uuid
from datetime import datetime, timedelta

from config import get_business_config
import database as db

logger = logging.getLogger(__name__)

# משך ברירת מחדל לתור (בדקות) — כשאין משך מוגדר לשירות
DEFAULT_DURATION_MINUTES = 60

# זמן תזכורת לפני התור (בדקות)
REMINDER_MINUTES_BEFORE = 60

# הגדרת VTIMEZONE לאזור הזמן Asia/Jerusalem (RFC 5545 §3.2.19).
# כללי DST של ישראל נקבעים בחקיקה ולא ניתנים לייצוג מדויק ב-RRULE
# (הכלל האמיתי: "יום שישי לפני יום ראשון האחרון של מרץ" — אין RRULE שמתאר זאת).
# ה-RRULE כאן הוא קירוב (זהה לזה ש-Google Calendar משתמש בו).
# X-LIC-LOCATION מאפשר ללקוחות מודרניים להשתמש במסד נתוני IANA המעודכן.
_VTIMEZONE_JERUSALEM = (
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Asia/Jerusalem\r\n"
    "X-LIC-LOCATION:Asia/Jerusalem\r\n"
    "BEGIN:STANDARD\r\n"
    "DTSTART:19701025T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
    "TZOFFSETFROM:+0300\r\n"
    "TZOFFSETTO:+0200\r\n"
    "TZNAME:IST\r\n"
    "END:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "DTSTART:19700327T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1FR\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0300\r\n"
    "TZNAME:IDT\r\n"
    "END:DAYLIGHT\r\n"
    "END:VTIMEZONE"
)


def generate_ics(
    service: str,
    preferred_date: str,
    preferred_time: str,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    description: str = "",
) -> bytes:
    """יצירת קובץ .ics בזיכרון עם פרטי התור.

    Parameters
    ----------
    service : str
        שם השירות (למשל "תספורת גברים").
    preferred_date : str
        תאריך בפורמט YYYY-MM-DD.
    preferred_time : str
        שעה בפורמט HH:MM.
    duration_minutes : int
        משך התור בדקות (ברירת מחדל 60).
    description : str
        תיאור נוסף (אופציונלי).

    Returns
    -------
    bytes
        תוכן קובץ .ics מוכן לשליחה.
    """
    # פרסור תאריך ושעה
    try:
        dt_start = datetime.strptime(
            f"{preferred_date} {preferred_time}", "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        logger.error(
            "לא ניתן לפרסר תאריך/שעה לקובץ ICS: date=%s time=%s",
            preferred_date, preferred_time,
        )
        raise

    dt_end = dt_start + timedelta(minutes=duration_minutes)

    # פורמט iCalendar לתאריכים (TZID=Asia/Jerusalem)
    fmt = "%Y%m%dT%H%M%S"
    dtstart_str = dt_start.strftime(fmt)
    dtend_str = dt_end.strftime(fmt)

    _biz = get_business_config()
    summary = f"{service} — {_biz.name}"
    uid = f"{uuid.uuid4()}@{_biz.name.replace(' ', '-')}"
    dtstamp = datetime.utcnow().strftime(fmt) + "Z"

    location = _biz.address or ""

    # בניית תוכן הקובץ ידנית — ללא תלות בספרייה חיצונית
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI-Business-Bot//Appointment//HE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _VTIMEZONE_JERUSALEM,
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=Asia/Jerusalem:{dtstart_str}",
        f"DTEND;TZID=Asia/Jerusalem:{dtend_str}",
        f"SUMMARY:{_ics_escape(summary)}",
    ]

    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")

    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")

    # תזכורת שעה לפני
    lines += [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:-PT{REMINDER_MINUTES_BEFORE}M",
        f"DESCRIPTION:תזכורת: יש לך תור בעוד {REMINDER_MINUTES_BEFORE} דקות!",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]

    # iCalendar דורש CRLF כמפריד שורות
    content = "\r\n".join(lines) + "\r\n"
    return content.encode("utf-8")


def generate_ics_filename(preferred_date: str) -> str:
    """יצירת שם קובץ .ics ידידותי.

    Returns
    -------
    str
        שם הקובץ (למשל "appointment_2026-02-25.ics").
    """
    return f"appointment_{preferred_date}.ics"


def get_service_duration(service_name: str) -> int:
    """קבלת משך השירות בדקות מטבלת services. ברירת מחדל 60."""
    try:
        service = db.get_service_by_name(service_name) if hasattr(db, "get_service_by_name") else None
        if service:
            return service.get("duration_minutes", DEFAULT_DURATION_MINUTES)
    except Exception:
        logger.error("שגיאה בקבלת משך שירות '%s'", service_name, exc_info=True)
    return DEFAULT_DURATION_MINUTES


def build_ics_preview(
    service: str,
    preferred_date: str,
    preferred_time: str,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
) -> dict:
    """בניית תצוגה מקדימה של שדות ה-.ics לפאנל הניהול.

    Returns
    -------
    dict
        מילון עם השדות שיוצגו בפאנל: summary, dtstart, dtend, location, reminder.
    """
    try:
        dt_start = datetime.strptime(
            f"{preferred_date} {preferred_time}", "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        return {}

    dt_end = dt_start + timedelta(minutes=duration_minutes)

    _biz = get_business_config()
    return {
        "summary": f"{service} — {_biz.name}",
        "dtstart": dt_start.strftime("%d/%m/%Y %H:%M"),
        "dtend": dt_end.strftime("%d/%m/%Y %H:%M"),
        "location": _biz.address or "(לא הוגדרה כתובת)",
        "reminder": f"{REMINDER_MINUTES_BEFORE} דקות לפני",
    }


def _ics_escape(text: str) -> str:
    """Escape של תווים מיוחדים בפורמט iCalendar (RFC 5545)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )
