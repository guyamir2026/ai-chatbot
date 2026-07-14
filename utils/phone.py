"""פונקציות עזר לפירמוט מספרי טלפון."""

import re

# מספר ישראלי תקף ב-E.164: +972 + ספרה לא-אפס + 8 ספרות.
# ה-[1-9] אוכף את הכלל שאחרי 972 אסור אפס מוביל (הייצוג המקומי 0XX
# חייב להיות מוסר לפני המרה ל-E.164 — +9720XXXXXXXX אינו תקף).
# מרכז את פורמט המספרים שנשלחים ל-Twilio WhatsApp — מספרים לא תקפים
# ידחו ברמת API עם error codes מבלבלים. עדיף לסנן מראש.
_IL_E164_RE = re.compile(r"^\+972[1-9]\d{8}$")


def format_phone(value: str) -> str:
    """פירמוט מספר טלפון ישראלי: +972XXXXXXXXX → 0XXXXXXXXX.

    תומך גם בפורמטים שעלולים להופיע אחרי URL decoding:
    - " 972..." (רווח מוביל מהמרת `+` ב-URL ל-space)
    - "972..." (בלי + בכלל)
    - "+972..." (פורמט E.164 הסטנדרטי)
    אם הערך לא מספר טלפון ישראלי (למשל Telegram user_id) — מחזיר כמו שהוא.
    בשני הענפים (עם + ובלי) הספרה אחרי "972" חייבת להיות לא-אפס; אחרת
    מדובר ב-9720XXX מספק שמשרשר 972 עם פורמט מקומי, וההמרה הייתה יוצרת
    "00XXX" חסר משמעות.
    """
    if not isinstance(value, str):
        return value
    # רווח מוביל מ-URL decode (`+` → space) — מנקים לפני בדיקת קידומת
    cleaned = value.lstrip()
    if cleaned.startswith("+972") and len(cleaned) >= 13 and cleaned[4] != "0":
        return "0" + cleaned[4:]
    # 972XXXXXXXXX ללא קידומת `+` — אחרי שאיכשהו ה-+ אבד.
    # הספרה אחרי "972" חייבת להיות לא-אפס לפי הכלל של מספרים ישראליים.
    if cleaned.startswith("972") and len(cleaned) >= 12 and cleaned[3] != "0":
        return "0" + cleaned[3:]
    return value


def is_valid_israeli_e164(value: str) -> bool:
    """בדיקה אם ערך הוא מספר טלפון ישראלי תקף בפורמט E.164 (+972 + 9 ספרות).

    שמרני: אם המספר לא מתחיל ב-+972 בדיוק מחזיר False. זה מיועד לווידציה
    לפני שליחה ב-Twilio — מספרים זרים או BSUID יוחזרו False ויטופלו בנפרד.
    """
    if not isinstance(value, str):
        return False
    return bool(_IL_E164_RE.match(value))


def to_wa_me_digits(number: str) -> str:
    """ניקוי מספר WhatsApp ל-digits בלבד — wa.me דורש בלי + או רווחים.

    דוגמה: '+972 50-123-4567' → '972501234567'.
    """
    return "".join(ch for ch in (number or "") if ch.isdigit())

