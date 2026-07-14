"""
Shabbat + Jewish Holiday Window — לוגיקה לדחיית קמפיינים שיווקיים ישראליים.

רקע:
    * תיקון 40 לחוק התקשורת לא אוסר שליחה בשבת/חג, אך זה תקן ה-best-practice
      ב-B2C ישראלי — הודעה שיווקית בשבת בבוקר נתפסת כלא-מקצועית ומעלה את
      שיעור ההסרות.
    * הלוגיקה הזו רלוונטית רק ל-MARKETING. UTILITY (אישור תור, התראה שלוחית)
      ו-AUTHENTICATION (OTP) עוברות תמיד — הן *שירותיות* ולא *שיווקיות*.

חלונות חסומים ל-MARKETING:
    * שישי מ-14:00 עד מוצ"ש (שבת 20:00) — שבת קלאסית.
    * חגים יהודיים (ראש השנה, יום כיפור, סוכות, פסח וכו') — כל היום.

מימוש:
    * שבת נקבעת לפי יום-בשבוע ושעת-יום בשעון ישראל (Asia/Jerusalem).
    * חגים משתמשים בספרייה `holidays` (כבר מותקנת בפרויקט דרך business_hours).
    * next_allowed_time דוחה קדימה עד 14 ימים — הגנה מ-infinite loop אם
      יש איחוד של חג+שבת שממלא שבוע שלם.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# חלון שבת. שישי 14:00 = שעה בטוחה לפני כניסת שבת גם בקיץ הקצר.
# שבת 20:00 = אחרי צאת השבת לכל השנה (בקיץ: ~20:00, בחורף: ~18:00 —
# אנחנו שמרנים). טווח זה רלוונטי למדיה שיווקית, לא ל-utility.
_SHABBAT_START_HOUR = 14
_SHABBAT_END_HOUR = 20
# Python weekday(): Monday=0 ... Sunday=6. שישי=4, שבת=5.
_FRIDAY = 4
_SATURDAY = 5

# הקטגוריה היחידה שחסומה בחלון
_BLOCKED_CATEGORY = "MARKETING"


def _to_israel(dt: datetime) -> datetime:
    """המרת datetime ל-Asia/Jerusalem. תומך גם ב-naive (מניחים שעון ישראל)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ISRAEL_TZ)
    return dt.astimezone(_ISRAEL_TZ)


def is_in_shabbat_window(dt: datetime) -> bool:
    """האם ה-datetime נופל בחלון שבת (שישי 14:00 → שבת 20:00 שעון ישראל)."""
    il = _to_israel(dt)
    if il.weekday() == _FRIDAY and il.hour >= _SHABBAT_START_HOUR:
        return True
    if il.weekday() == _SATURDAY and il.hour < _SHABBAT_END_HOUR:
        return True
    return False


def is_jewish_holiday(dt: datetime) -> tuple[bool, Optional[str]]:
    """בדיקה אם ה-datetime נופל על חג יהודי בישראל.

    Returns:
        (True, <שם החג>) אם כן; (False, None) אחרת.
    משתמש ב-cache של business_hours._get_israeli_holidays.
    """
    il = _to_israel(dt)
    d = il.date()
    try:
        from business_hours import _get_israeli_holidays
        holidays_map = _get_israeli_holidays(d.year)
    except Exception:
        logger.error("shabbat_window: שליפת חגים נכשלה", exc_info=True)
        return False, None
    name = holidays_map.get(d)
    return (name is not None, name)


def is_blocked_for_marketing(dt: datetime) -> tuple[bool, Optional[str]]:
    """צירוף חלונות שבת + חג. True אם נחסם, ומחרוזת סיבה להצגה ב-UI/לוג."""
    if is_in_shabbat_window(dt):
        return True, "חלון שבת (שישי 14:00 → שבת 20:00)"
    is_hol, name = is_jewish_holiday(dt)
    if is_hol:
        return True, f"חג: {name}"
    return False, None


def next_allowed_time(
    dt: datetime,
    *,
    category: str,
) -> datetime:
    """מחזיר את הזמן הבא בו מותר לשלוח קמפיין בקטגוריה הנתונה.

    Args:
        dt: הזמן המבוקש לשליחה (לרוב scheduled_at או "now").
        category: UTILITY / MARKETING / AUTHENTICATION / UNKNOWN.

    Returns:
        אותו dt *כפי שהוא* (אותו tzinfo) אם אין חסימה. אחרת — datetime
        בשעון ישראל עם tzinfo=_ISRAEL_TZ של המועד הפנוי הבא (מוצ"ש /
        אחרי חג). הקורא שמעביר dt עם tzinfo אחר יקבל אותו בחזרה רק אם
        לא היה חסום — אם נדרשה דחייה, ההחזרה ב-Israel TZ (ההקשר הטבעי).

    מוגבל ל-14 ימים של חיפוש קדימה כדי למנוע לולאה אינסופית.
    """
    if (category or "").upper() != _BLOCKED_CATEGORY:
        return dt

    # בדיקה ראשונה ב-Israel TZ, אבל אם לא חסום מחזירים את ה-dt המקורי
    # ללא המרה — כדי לשמור על ההבטחה שב-docstring של "אותו dt".
    il = _to_israel(dt)
    blocked, _ = is_blocked_for_marketing(il)
    if not blocked:
        return dt

    for _ in range(14):
        # דחייה ממוקדת — אם שבת, ל-שבת 20:00; אם חג, למחר 09:00.
        if is_in_shabbat_window(il):
            sat = il if il.weekday() == _SATURDAY else il + timedelta(days=1)
            il = sat.replace(
                hour=_SHABBAT_END_HOUR, minute=0, second=0, microsecond=0,
            )
        else:
            # חג (או שבת+חג) — דוחים ליום הבא בשעה 09:00
            il = (il + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0,
            )

        blocked, _ = is_blocked_for_marketing(il)
        if not blocked:
            return il

    logger.warning(
        "shabbat_window.next_allowed_time: לא נמצא חלון פנוי אחרי 14 ימים — "
        "מחזירים %s כפי שהוא.", il,
    )
    return il
