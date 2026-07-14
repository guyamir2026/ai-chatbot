"""
WhatsApp Opt-out / Opt-in Detection — תיקון 40 לחוק התקשורת.

רקע רגולטורי:
    תיקון 40 לחוק התקשורת (2008) מחייב:
    1. Opt-in מפורש (לא "יחסי לקוח קיימים") לפני שליחת הודעות שיווקיות.
    2. מנגנון הסרה זמין בכל הודעה — הלקוח יכול לענות "הסר" ולצאת.
    3. כיבוד ההסרה תוך זמן סביר (מעשית: מיידי).

    Meta/WhatsApp WABA policy דורשת את אותה התנהגות ברמה גלובלית —
    תגובה לבקשת הסרה היא חלק מחובת ה-Business.

מימוש:
    detect_optout(text) מזהה מילות-מפתח נפוצות בעברית/אנגלית. המסנן
    שמרני — מעדיפים false-positive (להסיר מישהו שלא ביקש) על פני
    false-negative (להחמיץ בקשת הסרה). טעות של false-negative היא הפרה
    רגולטורית; false-positive הוא חוויה פחות טובה אך ניתן לתקן אותה עם
    הודעת opt-in חוזרת.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Opt-out keywords — הודעות נכנסות שמסמנות בקשת הסרה.
# כל הערכים באותיות קטנות; הבדיקה נעשית אחרי lower() + strip().
# הרחבות עתידיות: לא להוסיף מילים דו-משמעיות כמו "עצור" או "מספיק" שיכולות
# להופיע בזרימות אחרות (ביטול תור ספציפי, למשל).
OPTOUT_KEYWORDS = frozenset({
    "הסר",
    "הסרה",
    "להסיר",
    "הסירו",
    "הסר אותי",
    "אל תשלחו",
    "אל תשלחו לי",
    "הפסק",
    "stop",
    "unsubscribe",
    "optout",
    "opt-out",
    "opt out",
    "remove",
    "cancel subscription",
})

# Opt-in keywords — לקוח שרוצה לחזור לקבל קמפיינים אחרי opt-out.
# נדירות יותר; משמשות לרוב כתשובה לשאלת איסוף opt-in מפורשת.
OPTIN_KEYWORDS = frozenset({
    "הסכמה",
    "אני מסכים",
    "אני מסכימה",
    "כן, שלחו",
    "start",
    "subscribe",
    "optin",
    "opt-in",
    "opt in",
})

# הודעת אישור ללקוח אחרי opt-out.
# חייבת להיות ברורה שההסרה בוצעה + לתת דרך לחזור.
OPTOUT_CONFIRMATION = (
    "קיבלנו — הוסרת מרשימת ההודעות השיווקיות. "
    "לא תקבל/י מאיתנו הודעות יזומות חדשות.\n\n"
    "אם זו טעות, או שתרצה/י לחזור בעתיד — השב/י *הסכמה*."
)

OPTIN_CONFIRMATION = (
    "תודה! נרשמת לקבלת עדכונים והצעות. "
    "ניתן להסיר בכל עת ע\"י תגובת *הסר*."
)


def _normalize(text: str) -> str:
    """lower + strip, הסרת סימני פיסוק משני הקצוות (leading + trailing).

    חשוב לנרמל משני הצדדים כדי לתפוס גם "!הסר", "הסר!", או `"הסר"` (בציטוטים).
    לפי עקרון המודול — false-negative בזיהוי opt-out הוא הפרה רגולטורית,
    false-positive הוא אי-נוחות — לכן מנרמלים שמרנית כדי לזהות כמה שיותר.
    """
    if not text:
        return ""
    # סימני פיסוק ומרכאות שיכולים להופיע בקצוות. לא כוללים נקודה פנימית כי
    # היא עלולה להוות חלק מקיצור (למשל שם מקוצר).
    punctuation = "!?.,;:\"'()[]「」״׳ "
    return text.strip().strip(punctuation).strip().lower()


def detect_optout(text: str) -> bool:
    """בדיקה אם טקסט הודעה הוא בקשת opt-out.

    מחזיר True אם ההודעה **כולה** היא מילת/ביטוי הסרה (אחרי נרמול).
    לא מחפש substring כדי למנוע false-positives במשפטים ארוכים (למשל
    "אני לא רוצה להסיר את התור" מכיל "להסיר" אבל לא בקשת opt-out).
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    return normalized in OPTOUT_KEYWORDS


def detect_optin(text: str) -> bool:
    """בדיקה אם טקסט הודעה הוא בקשת opt-in (אחרי opt-out קודם)."""
    normalized = _normalize(text)
    if not normalized:
        return False
    return normalized in OPTIN_KEYWORDS
