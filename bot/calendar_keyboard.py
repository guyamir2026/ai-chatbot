"""
Calendar Keyboard — לוח שנה ויזואלי inline לבחירת תאריך תור.

בונה InlineKeyboardMarkup עם ימי החודש, ניווט בין חודשים,
וסימון ימים סגורים/תפוסים לפי business_hours, special_days,
vacation_service ו-Google Calendar.
"""

import calendar
import logging
from datetime import date, datetime, time as _time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ai_chatbot.business_hours import get_status_for_date
from ai_chatbot.vacation_service import VacationService

logger = logging.getLogger(__name__)

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _today_israel() -> date:
    """תאריך נוכחי לפי אזור הזמן של ישראל — עקבי עם business_hours.py."""
    return datetime.now(ISRAEL_TZ).date()

# קידומות callback_data ללוח השנה
CB_CALENDAR_SELECT = "cal_sel_"      # בחירת יום: cal_sel_2026-04-15
CB_CALENDAR_PREV = "cal_prev_"      # חודש קודם: cal_prev_2026_3
CB_CALENDAR_NEXT = "cal_next_"      # חודש הבא: cal_next_2026_5
CB_CALENDAR_IGNORE = "cal_ignore"    # כפתור לא לחיץ (ימים סגורים, כותרות)

# מגבלת טווח — כמה חודשים קדימה להציג
MAX_MONTHS_AHEAD = 3

# שמות ימים בעברית (ראשון עד שבת)
WEEKDAY_HEADERS = ["א׳", "ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳"]

# שמות חודשים בעברית
MONTH_NAMES_HE = {
    1: "ינואר", 2: "פברואר", 3: "מרץ", 4: "אפריל",
    5: "מאי", 6: "יוני", 7: "יולי", 8: "אוגוסט",
    9: "ספטמבר", 10: "אוקטובר", 11: "נובמבר", 12: "דצמבר",
}


def get_month_availability(
    year: int,
    month: int,
    service_duration: int = 60,
    buffer_after_event_minutes: int = 0,
) -> dict[int, dict]:
    """בדיקת זמינות לכל ימי החודש.

    שולף busy ranges מ-Google Calendar לחודש שלם בקריאה אחת (ביצועים),
    ואז בודק כל יום בנפרד מול business_hours ו-special_days.

    Args:
        year: שנה
        month: חודש
        service_duration: משך השירות בדקות (ברירת מחדל: 60)
        buffer_after_event_minutes: מרווח אחרי אירוע חיצוני ביומן (סופג גלישה
            של תור קודם שנמשך מעבר למתוכנן)

    מחזיר dict: day_number -> {available: bool, reason: str}
    """
    today = _today_israel()
    _, days_in_month = calendar.monthrange(year, month)
    result = {}

    # בדיקת חופשה — אם פעילה, כל הימים חסומים
    vacation_active = VacationService.is_active()

    # שליפת busy slots מ-Google Calendar לחודש שלם בקריאה אחת
    gcal_connected = False
    month_busy_ranges = []
    try:
        from google_calendar import is_connected, get_busy_slots

        gcal_connected = is_connected()
        if gcal_connected:
            tz = ZoneInfo("Asia/Jerusalem")
            month_start = datetime.combine(date(year, month, 1), _time(0, 0), tzinfo=tz)
            last_day = date(year, month, days_in_month)
            month_end = datetime.combine(last_day, _time(23, 59, 59), tzinfo=tz)
            month_busy_ranges = get_busy_slots(month_start, month_end)
    except ImportError:
        pass
    except Exception:
        gcal_connected = False
        logger.error("שגיאה בשליפת busy slots לחודש %d/%d", month, year, exc_info=True)

    for day in range(1, days_in_month + 1):
        target = date(year, month, day)

        # ימים שעברו
        if target < today:
            result[day] = {"available": False, "reason": "עבר"}
            continue

        # חופשה
        if vacation_active:
            result[day] = {"available": False, "reason": "חופשה"}
            continue

        # שעות פעילות + ימים מיוחדים + חגים
        status = get_status_for_date(target)
        if not status.get("is_open"):
            result[day] = {"available": False, "reason": status.get("reason", "סגור")}
            continue

        # אם היום — בדיקה שיש מספיק זמן עד סגירה גם ללא GCal
        if target == today:
            tz = ZoneInfo("Asia/Jerusalem")
            now = datetime.now(tz)
            close_str = status.get("close_time") or "23:59"
            try:
                close_t = _time.fromisoformat(close_str)
            except (ValueError, TypeError):
                close_t = _time(23, 59)
            day_end = datetime.combine(target, close_t, tzinfo=tz)
            # הסלוט הבא אחרי עכשיו (עיגול ל-30 דקות)
            mins = now.hour * 60 + now.minute
            next_slot_mins = ((mins // 30) + 1) * 30
            if next_slot_mins >= 24 * 60 or now.replace(
                hour=next_slot_mins // 60, minute=next_slot_mins % 60,
                second=0, microsecond=0,
            ) + timedelta(minutes=service_duration) > day_end:
                result[day] = {"available": False, "reason": "עבר שעת סגירה"}
                continue

        # בדיקת תפוסות מול Google Calendar — חישוב slots לפי busy ranges של אותו יום
        if gcal_connected:
            day_slots = _calculate_day_slots(
                target, status, month_busy_ranges, service_duration,
                buffer_after_event_minutes=buffer_after_event_minutes,
            )
            if not day_slots:
                result[day] = {"available": False, "reason": "תפוס"}
                continue

        result[day] = {"available": True, "reason": ""}

    return result


def _calculate_day_slots(
    target_date: date,
    day_status: dict,
    month_busy_ranges: list[dict],
    service_duration: int,
    buffer_after_event_minutes: int = 0,
) -> list[str]:
    """חישוב slots פנויים ליום אחד מתוך busy ranges של חודש שלם.

    לוגיקה דומה ל-get_available_slots אבל עובדת עם busy ranges שכבר נשלפו.
    """
    tz = ZoneInfo("Asia/Jerusalem")

    open_time_str = day_status.get("open_time") or "00:00"
    close_time_str = day_status.get("close_time") or "23:59"

    try:
        open_time = _time.fromisoformat(open_time_str)
        close_time = _time.fromisoformat(close_time_str)
    except (ValueError, TypeError):
        open_time = _time(0, 0)
        close_time = _time(23, 59)

    day_start = datetime.combine(target_date, open_time, tzinfo=tz)
    day_end = datetime.combine(target_date, close_time, tzinfo=tz)

    # אם היום — מתחילים מהסלוט הבא אחרי עכשיו
    now = datetime.now(tz)
    if target_date == now.date() and now > day_start:
        minutes_since_midnight = now.hour * 60 + now.minute
        next_slot_minutes = ((minutes_since_midnight // 30) + 1) * 30
        if next_slot_minutes >= 24 * 60:
            return []
        day_start = now.replace(
            hour=next_slot_minutes // 60,
            minute=next_slot_minutes % 60,
            second=0, microsecond=0,
        )
        if day_start >= day_end:
            return []

    # סינון busy ranges רלוונטיים ליום הזה בלבד
    event_buffer = timedelta(minutes=max(0, int(buffer_after_event_minutes or 0)))
    busy_ranges = []
    for slot in month_busy_ranges:
        try:
            start = datetime.fromisoformat(slot["start"])
            end = datetime.fromisoformat(slot["end"])
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
            # חפיפה עם היום
            if start.date() <= target_date and end.date() >= target_date:
                busy_ranges.append((start, end))
        except (ValueError, KeyError):
            continue

    # חישוב slots פנויים
    slot_duration = timedelta(minutes=service_duration)
    available = []
    current = day_start

    while current + timedelta(minutes=service_duration) <= day_end:
        slot_end = current + slot_duration
        is_free = True
        for busy_start, busy_end in busy_ranges:
            if current < busy_end and slot_end > busy_start:
                is_free = False
                break
        if is_free:
            available.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)

    return available


def build_calendar_keyboard(
    year: int,
    month: int,
    service_duration: int = 60,
    buffer_after_event_minutes: int = 0,
) -> InlineKeyboardMarkup:
    """בניית לוח שנה ויזואלי כ-InlineKeyboardMarkup.

    Args:
        year: שנה
        month: חודש
        service_duration: משך השירות בדקות — מועבר ל-get_month_availability
        buffer_after_event_minutes: מרווח אחרי אירוע חיצוני ביומן

    מחזיר מקלדת inline עם:
    - שורת כותרת (חודש + ניווט)
    - שורת ימות השבוע
    - שורות ימים עם סימון זמינות
    """
    today = _today_israel()
    availability = get_month_availability(
        year, month, service_duration,
        buffer_after_event_minutes=buffer_after_event_minutes,
    )
    _, days_in_month = calendar.monthrange(year, month)

    keyboard = []

    # ── שורת כותרת עם ניווט ──
    nav_row = []

    # כפתור חודש קודם — רק אם זה לא לפני החודש הנוכחי
    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1
    if date(prev_year, prev_month, 1) >= date(today.year, today.month, 1):
        nav_row.append(InlineKeyboardButton(
            "◀",
            callback_data=f"{CB_CALENDAR_PREV}{prev_year}_{prev_month}",
        ))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data=CB_CALENDAR_IGNORE))

    # כותרת חודש
    month_name = MONTH_NAMES_HE.get(month, str(month))
    nav_row.append(InlineKeyboardButton(
        f"{month_name} {year}",
        callback_data=CB_CALENDAR_IGNORE,
    ))

    # כפתור חודש הבא — רק אם בטווח המותר
    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1
    max_date = date(today.year, today.month, 1) + timedelta(days=MAX_MONTHS_AHEAD * 31)
    if date(next_year, next_month, 1) <= max_date:
        nav_row.append(InlineKeyboardButton(
            "▶",
            callback_data=f"{CB_CALENDAR_NEXT}{next_year}_{next_month}",
        ))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data=CB_CALENDAR_IGNORE))

    keyboard.append(nav_row)

    # ── שורת ימות השבוע ──
    header_row = [
        InlineKeyboardButton(day_name, callback_data=CB_CALENDAR_IGNORE)
        for day_name in WEEKDAY_HEADERS
    ]
    keyboard.append(header_row)

    # ── שורות ימים ──
    # היום הראשון בחודש — ממופה לאינדקס בשבוע הישראלי (0=ראשון)
    first_day = date(year, month, 1)
    # Python weekday: 0=Mon..6=Sun → Israeli: 0=Sun..6=Sat
    first_day_israeli = (first_day.weekday() + 1) % 7

    week_row = []
    # ריפוד ימים ריקים בתחילת החודש
    for _ in range(first_day_israeli):
        week_row.append(InlineKeyboardButton(" ", callback_data=CB_CALENDAR_IGNORE))

    for day in range(1, days_in_month + 1):
        day_info = availability.get(day, {"available": False, "reason": ""})

        if day_info["available"]:
            # יום פנוי — לחיץ
            target = date(year, month, day)
            week_row.append(InlineKeyboardButton(
                str(day),
                callback_data=f"{CB_CALENDAR_SELECT}{target.isoformat()}",
            ))
        else:
            # יום סגור/תפוס — לא לחיץ, עם סימון
            week_row.append(InlineKeyboardButton(
                f"{day}❌",
                callback_data=CB_CALENDAR_IGNORE,
            ))

        # סוף שבוע — שורה חדשה
        if len(week_row) == 7:
            keyboard.append(week_row)
            week_row = []

    # שורה אחרונה (אם לא מלאה)
    if week_row:
        while len(week_row) < 7:
            week_row.append(InlineKeyboardButton(" ", callback_data=CB_CALENDAR_IGNORE))
        keyboard.append(week_row)

    return InlineKeyboardMarkup(keyboard)


def parse_calendar_callback(callback_data: str) -> dict:
    """פירוק callback_data של לוח השנה.

    מחזיר dict עם:
    - action: "select" | "prev" | "next" | "ignore"
    - date: str (ISO) — רק ב-select
    - year: int — רק ב-prev/next
    - month: int — רק ב-prev/next
    """
    if callback_data == CB_CALENDAR_IGNORE:
        return {"action": "ignore"}

    if callback_data.startswith(CB_CALENDAR_SELECT):
        # cal_sel_2026-04-15
        date_str = callback_data[len(CB_CALENDAR_SELECT):]
        return {"action": "select", "date": date_str}

    if callback_data.startswith(CB_CALENDAR_PREV):
        # cal_prev_2026_3
        parts = callback_data[len(CB_CALENDAR_PREV):].split("_")
        try:
            return {
                "action": "prev",
                "year": int(parts[0]),
                "month": int(parts[1]),
            }
        except (ValueError, IndexError):
            logger.error("callback data לא תקין: %s", callback_data)
            return {"action": "ignore"}

    if callback_data.startswith(CB_CALENDAR_NEXT):
        # cal_next_2026_5
        parts = callback_data[len(CB_CALENDAR_NEXT):].split("_")
        try:
            return {
                "action": "next",
                "year": int(parts[0]),
                "month": int(parts[1]),
            }
        except (ValueError, IndexError):
            logger.error("callback data לא תקין: %s", callback_data)
            return {"action": "ignore"}

    return {"action": "ignore"}
