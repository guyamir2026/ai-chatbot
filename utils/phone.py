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


def to_israeli_e164(value: str) -> str:
    """נרמול מספר ישראלי לפורמט E.164 (+972...).

    מקבל פורמט מקומי (0501234567), 972501234567, או +972501234567 —
    עם או בלי רווחים / מקפים / סוגריים / נקודות. מחזיר +972XXXXXXXXX.
    הפוך ל-format_phone (שממיר +972 → 0X).

    אם הערך אינו נראה כמספר ישראלי (מספר זר, ריק, טקסט) — מוחזר כמו
    שהוא, בלי לדחות, כי שדה הטלפון בכרטיס הביקור חופשי (למשל מספר בחו"ל).
    """
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return raw
    # ניקוי מפרידי-תצוגה נפוצים לפני בדיקת הקידומת
    digits = re.sub(r"[\s\-().]", "", raw)
    if digits.startswith("+972"):
        rest = digits[4:]
    elif digits.startswith("972"):
        rest = digits[3:]
    elif digits.startswith("0"):
        rest = digits[1:]
    else:
        return value  # לא נראה ישראלי — משאירים כמו שהוא
    # החלק אחרי הקידומת חייב להיות 8–9 ספרות שמתחילות בלא-אפס
    # (מובייל ישראלי 9, קווי 8). אחרת — לא תבנית ישראלית תקפה, לא נוגעים.
    if re.fullmatch(r"[1-9]\d{7,8}", rest):
        return "+972" + rest
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

