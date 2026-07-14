"""
Booking decision — פונקציה טהורה שקובעת האם תור חדש יהיה
pending (אישור ידני), confirmed (אישור אוטומטי) או rejected (דחייה).

זה שכבת הלוגיקה — בלי DB ובלי רשת. הקריאות ל-DB / Google Calendar
מתבצעות בצד ה-handler וההחלטה מועברת לפונקציה הזו כפרמטרים.
מבנה זה מאפשר לכסות את כל מקרי הקצה בטסטים בלי mocks.

ראה טבלת התרחישים ב-PR description (#auto-calendar-booking).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional


VALID_MODES = {"manual", "auto_with_check", "auto_always"}

Decision = Literal["confirmed", "pending", "rejected"]


@dataclass(frozen=True)
class BookingDecisionInput:
    """כל הקלט הדרוש להחלטה — פרמטרים בלבד, בלי תלויות חיצוניות."""

    # מצב המוגדר ע״י בעל העסק
    mode: str

    # פרטי הסלוט המבוקש
    slot_date: date
    slot_time: time
    duration_minutes: int

    # הקשר זמני
    now_il: datetime  # חייב להיות עם tzinfo של Asia/Jerusalem
    max_days_ahead: int = 90

    # הקשר עסקי — מועברים מ-business_hours / vacation_service / DB
    business_hours_status: Optional[dict] = None  # פלט get_status_for_date
    vacation_active: bool = False
    has_pending_or_confirmed_conflict: bool = False  # תור קיים בסלוט הזה
    user_has_appointment_same_day: bool = False  # תור אחר של אותו לקוח באותו יום

    # הקשר Google Calendar
    calendar_connected: bool = False
    calendar_check_failed: bool = False  # True אם הניסיון לקרוא ל-GCal נכשל
    available_slots: Optional[list[str]] = None  # פלט get_available_slots(target)


@dataclass(frozen=True)
class BookingDecisionResult:
    decision: Decision
    reason: str  # מחרוזת קצרה לאנגלית — לוג + UI


def _slot_end(d: date, t: time, duration_minutes: int) -> datetime:
    return datetime.combine(d, t) + timedelta(minutes=duration_minutes)


def _slot_start(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def _normalize_time(t: time | str) -> time:
    if isinstance(t, time):
        return t
    # תמיכה ב-"HH:MM" וב-"HH:MM:SS"
    parts = t.split(":")
    return time(int(parts[0]), int(parts[1]))


def decide_appointment_status(inp: BookingDecisionInput) -> BookingDecisionResult:
    """
    מחזיר החלטה לפי המצב והקשר. ראה טבלת התרחישים.

    כללי הברירת מחדל:
        - mode=manual               ⇒ תמיד pending (חוץ מ-rejected אם הסלוט בעבר/רחוק).
        - mode=auto_with_check      ⇒ confirmed רק אם כל הבדיקות עוברות.
                                       כשל בדיקה ⇒ rejected (בעל העסק מקבל דחייה ומציעים שעה אחרת).
                                       חוסר ודאות (calendar_check_failed,
                                       calendar לא מחובר, אין duration) ⇒ fallback ל-pending.
        - mode=auto_always          ⇒ confirmed אלא אם יש בעיה קשה (סלוט בעבר,
                                       קונפליקט עם תור קיים, יום מנוחה ידוע).
    """
    if inp.mode not in VALID_MODES:
        # ערך לא תקין ⇒ fallback בטוח
        return BookingDecisionResult("pending", "invalid_mode_fallback")

    slot_start = _slot_start(inp.slot_date, _normalize_time(inp.slot_time))
    slot_end = _slot_end(inp.slot_date, _normalize_time(inp.slot_time), inp.duration_minutes)
    now_naive = inp.now_il.replace(tzinfo=None)

    # ─── בדיקות גלובליות (מתבצעות לכל ה-modes) ───────────────────────────

    # סלוט בעבר — תמיד דחייה
    if slot_start <= now_naive:
        return BookingDecisionResult("rejected", "slot_in_past")

    # סלוט רחוק מדי — דחייה (גם ב-manual; שמירה על שפיות)
    days_ahead = (inp.slot_date - inp.now_il.date()).days
    if days_ahead > inp.max_days_ahead:
        return BookingDecisionResult("rejected", "slot_too_far_ahead")

    # תור קיים פעיל באותו סלוט (pending/confirmed) — דחייה
    if inp.has_pending_or_confirmed_conflict:
        return BookingDecisionResult("rejected", "slot_already_taken")

    # סלוט חוצה חצות — דחייה (לא תומכים)
    if slot_end.date() != inp.slot_date:
        return BookingDecisionResult("rejected", "slot_crosses_midnight")

    # משך לא תקין
    if inp.duration_minutes <= 0:
        return BookingDecisionResult("rejected", "invalid_duration")

    # ─── manual ────────────────────────────────────────────────────────
    if inp.mode == "manual":
        return BookingDecisionResult("pending", "manual_mode")

    # ─── auto_always — דרוש מינימום בדיקות בטיחות ──────────────────────
    if inp.mode == "auto_always":
        # גם ב-auto_always לא נאשר אם בעל העסק במצב חופשה (זה כבוד בסיסי
        # להגדרה שהוא הפעיל). לפעמים auto_always = "תמיד פתוח" אבל חופשה
        # היא שכבה ידנית של בעל העסק וצריכה לגבור.
        if inp.vacation_active:
            return BookingDecisionResult("rejected", "vacation_active")
        return BookingDecisionResult("confirmed", "auto_always")

    # ─── auto_with_check ──────────────────────────────────────────────
    # מכאן והלאה inp.mode == "auto_with_check"

    if inp.vacation_active:
        return BookingDecisionResult("rejected", "vacation_active")

    # business_hours_status חסר ⇒ get_status_for_date נכשל ב-orchestrator.
    # חוסר ודאות = fallback ל-pending (לא לדחות על סמך מצב לא ידוע, ולא לאשר).
    if inp.business_hours_status is None:
        return BookingDecisionResult("pending", "business_hours_unknown")

    bh = inp.business_hours_status
    if not bh.get("is_open", False):
        # special_day סגור / חג / יום קבוע סגור / שבת
        source = bh.get("source", "regular")
        return BookingDecisionResult("rejected", f"closed_{source}")

    # סלוט חייב להיכנס בתוך שעות העבודה — גם start וגם end
    open_str = bh.get("open_time") or "00:00"
    close_str = bh.get("close_time") or "23:59"
    try:
        open_t = _normalize_time(open_str)
        close_t = _normalize_time(close_str)
    except (ValueError, TypeError):
        # שעות עבודה לא תקינות — fallback ל-pending (בעל עסק יחליט)
        return BookingDecisionResult("pending", "invalid_business_hours")

    slot_t = _normalize_time(inp.slot_time)
    end_t = (datetime.combine(date(2000, 1, 1), slot_t) + timedelta(minutes=inp.duration_minutes)).time()

    if slot_t < open_t:
        return BookingDecisionResult("rejected", "before_business_hours")
    if end_t > close_t:
        return BookingDecisionResult("rejected", "exceeds_closing_time")

    # תור אחר באותו יום — לא חוסם אוטומטית, אבל מורידים ל-pending כי
    # זה דפוס חשוד (לקוח מנסה כמה זמנים). בעל עסק יחליט.
    if inp.user_has_appointment_same_day:
        return BookingDecisionResult("pending", "user_other_appointment_same_day")

    # Google Calendar — חוסר ודאות = fallback ל-pending (לא confirmed על סמך מצב לא ידוע)
    if not inp.calendar_connected:
        return BookingDecisionResult("pending", "calendar_not_connected")
    if inp.calendar_check_failed:
        return BookingDecisionResult("pending", "calendar_check_failed")
    if inp.available_slots is None:
        return BookingDecisionResult("pending", "calendar_slots_unknown")

    slot_str = slot_t.strftime("%H:%M")
    if slot_str not in inp.available_slots:
        return BookingDecisionResult("rejected", "calendar_busy")

    return BookingDecisionResult("confirmed", "auto_with_check_ok")


def gather_and_decide(
    *,
    user_id: str,
    slot_date_str: str,
    slot_time_str: str,
    duration_minutes: int | None = None,
    exclude_appointment_id: int | None = None,
) -> BookingDecisionResult:
    """
    Orchestrator — אוסף את הקשר מ-DB / business_hours / vacation / Google Calendar
    ומפעיל את decide_appointment_status. נקרא מתוך handlers.

    מחזיר BookingDecisionResult. אין כתיבה ל-DB כאן — הקוראים אחראים לעדכן את
    סטטוס התור לפי ההחלטה.

    exclude_appointment_id — מזהה התור שההחלטה מתקבלת עבורו. הקוראים יוצרים את
    התור ב-DB *לפני* הקריאה (סדר בטוח מפני race), ולכן חייבים להעביר את ה-id
    כדי שבדיקת הזמינות מול היומן תתעלם מהתור עצמו. בלי זה הוא נספר כטווח תפוס
    וחוסם את השעה של עצמו ⇒ כל שעה תיראה תפוסה (calendar_busy). זהו הפיצוי
    המקביל לבדיקת הקונפליקט ב-DB שסופרת c >= 2 כדי לדלג על שורת התור-עצמו.
    """
    import logging
    from datetime import date as _date, datetime as _dt
    from zoneinfo import ZoneInfo

    logger = logging.getLogger(__name__)

    # ייבוא מאוחר כדי לא לחייב את database/business_hours בטעינת המודול
    # (שומר על המודול בר-טסטינג בלי תלויות).
    from ai_chatbot import database as db  # type: ignore

    settings = db.get_bot_settings() or {}
    mode = settings.get("auto_booking_mode", "manual")
    max_days_ahead = int(settings.get("auto_booking_max_days_ahead", 90) or 90)

    # זמן בישראל
    il_tz = ZoneInfo("Asia/Jerusalem")
    now_il = _dt.now(il_tz)

    # parsing של הסלוט
    try:
        slot_date_obj = _date.fromisoformat(slot_date_str)
    except (ValueError, TypeError):
        logger.warning("gather_and_decide: slot_date לא תקין: %r", slot_date_str)
        return BookingDecisionResult("pending", "invalid_slot_date")

    try:
        slot_time_obj = _normalize_time(slot_time_str)
    except (ValueError, TypeError, IndexError):
        logger.warning("gather_and_decide: slot_time לא תקין: %r", slot_time_str)
        return BookingDecisionResult("pending", "invalid_slot_time")

    # משך — אם לא נשלח, ניקח מהקונפיג הגלובלי
    if duration_minutes is None:
        duration_settings = db.get_appointment_duration_settings()
        duration_minutes = int(duration_settings.get("default_minutes") or 60)

    # business_hours status — נחוץ רק ל-auto_with_check (manual מחזיר pending מיד
    # אחרי הבדיקות הגלובליות, בלי לקרוא ל-business_hours_status).
    bh_status: dict | None = None
    if mode == "auto_with_check":
        try:
            from ai_chatbot import business_hours  # type: ignore
            bh_status = business_hours.get_status_for_date(slot_date_obj)
        except Exception:
            logger.error("gather_and_decide: כשל בשליפת business_hours", exc_info=True)
            bh_status = None

    # vacation — רלוונטי לכל המצבים האוטומטיים
    vacation_active = False
    try:
        vac = db.get_vacation_mode() or {}
        vacation_active = bool(vac.get("is_active"))
    except Exception:
        logger.error("gather_and_decide: כשל בשליפת vacation_mode", exc_info=True)

    # קונפליקטים — כל תור pending/confirmed באותו סלוט (כל לקוח שהוא)
    has_conflict = False
    user_other_same_day = False
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM appointments "
                "WHERE preferred_date = ? AND preferred_time = ? "
                "AND status IN ('pending', 'confirmed')",
                (slot_date_str, slot_time_str),
            ).fetchone()
            # הערה: אם התור החדש כבר נוצר (קוראים אחרי create_appointment),
            # הוא ייכלל בספירה ⇒ נחשב conflict רק כשיש לפחות 2.
            has_conflict = bool(row and row["c"] >= 2)

            row2 = conn.execute(
                "SELECT COUNT(*) AS c FROM appointments "
                "WHERE user_id = ? AND preferred_date = ? "
                "AND status IN ('pending', 'confirmed') "
                "AND preferred_time != ?",
                (user_id, slot_date_str, slot_time_str),
            ).fetchone()
            user_other_same_day = bool(row2 and row2["c"] > 0)
    except Exception:
        logger.error("gather_and_decide: כשל בבדיקת קונפליקטים ב-DB", exc_info=True)

    # Google Calendar — רק עבור auto_with_check
    calendar_connected = False
    calendar_check_failed = False
    available_slots: list[str] | None = None
    if mode == "auto_with_check":
        try:
            from google_calendar import is_connected, get_available_slots
            calendar_connected = is_connected()
            if calendar_connected:
                buffer_min = db.get_auto_booking_buffer_minutes()
                try:
                    available_slots = get_available_slots(
                        slot_date_obj,
                        service_duration_minutes=duration_minutes,
                        buffer_after_event_minutes=buffer_min,
                        exclude_appointment_id=exclude_appointment_id,
                    )
                except Exception:
                    logger.error(
                        "gather_and_decide: get_available_slots נכשל", exc_info=True,
                    )
                    calendar_check_failed = True
        except ImportError:
            calendar_connected = False
        except Exception:
            logger.error(
                "gather_and_decide: שגיאה כללית מול Google Calendar", exc_info=True,
            )
            calendar_check_failed = True

    inp = BookingDecisionInput(
        mode=mode,
        slot_date=slot_date_obj,
        slot_time=slot_time_obj,
        duration_minutes=duration_minutes,
        now_il=now_il,
        max_days_ahead=max_days_ahead,
        business_hours_status=bh_status,
        vacation_active=vacation_active,
        has_pending_or_confirmed_conflict=has_conflict,
        user_has_appointment_same_day=user_other_same_day,
        calendar_connected=calendar_connected,
        calendar_check_failed=calendar_check_failed,
        available_slots=available_slots,
    )
    result = decide_appointment_status(inp)
    logger.info(
        "auto_booking decision: user=%s date=%s time=%s mode=%s ⇒ %s (%s)",
        user_id, slot_date_str, slot_time_str, mode, result.decision, result.reason,
    )
    return result


# מיפוי סיבת דחייה ⇒ הודעה בעברית ללקוח.
# נשמר כאן ולא ב-handlers כדי שכל הצינורות (Telegram, WhatsApp, וכו') ישתפו טקסט אחיד.
_REJECTION_MESSAGES = {
    "slot_in_past": "השעה שבחרתם כבר עברה. אנא בחרו זמן עתידי.",
    "slot_too_far_ahead": "התאריך שביקשתם רחוק מדי. אנא בחרו תאריך קרוב יותר.",
    "slot_already_taken": "השעה הזו כבר תפוסה. אנא בחרו שעה אחרת.",
    "slot_crosses_midnight": "אורך התור חוצה את חצות. אנא בחרו שעה מוקדמת יותר.",
    "invalid_duration": "אירעה שגיאה במשך התור. אנא נסו שוב.",
    "vacation_active": "כרגע אנחנו בחופשה ולא ניתן לקבוע תורים חדשים.",
    "closed_regular": "התאריך שבחרתם הוא יום סגור אצלנו. אנא בחרו יום אחר.",
    "closed_holiday": "התאריך שבחרתם הוא חג. אנא בחרו תאריך אחר.",
    "closed_special_day": "התאריך שבחרתם סגור אצלנו (יום מיוחד). אנא בחרו תאריך אחר.",
    "before_business_hours": "השעה שבחרתם לפני שעות הפתיחה. אנא בחרו שעה מאוחרת יותר.",
    "exceeds_closing_time": "השעה שבחרתם חורגת משעות הסגירה. אנא בחרו שעה מוקדמת יותר.",
    "calendar_busy": "השעה הזו כבר תפוסה ביומן. אנא בחרו שעה אחרת.",
}


def get_rejection_message(reason: str) -> str:
    """החזרת הודעה ללקוח עבור סיבת דחייה. fallback להודעה כללית."""
    return _REJECTION_MESSAGES.get(
        reason,
        "לא הצלחנו לקבוע את התור. אנא נסו שעה או תאריך אחרים.",
    )


__all__ = [
    "BookingDecisionInput",
    "BookingDecisionResult",
    "Decision",
    "VALID_MODES",
    "decide_appointment_status",
    "gather_and_decide",
    "get_rejection_message",
]
