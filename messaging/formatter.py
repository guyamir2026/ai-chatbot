"""
formatter — המרת טקסט HTML (פורמט Telegram) לפורמט הערוץ המבוקש.

ה-LLM מייצר HTML (כי ה-system prompt מנחה אותו). הפונקציה format_message
ממירה את ה-HTML לפורמט המתאים לערוץ היעד.

| תג                          | Telegram | WhatsApp       | Meta (Messenger/IG) |
|-----------------------------|----------|----------------|---------------------|
| <b>text</b>                 | נשאר     | *text*         | text (הסרת התג)     |
| <i>text</i>                 | נשאר     | _text_         | text (הסרת התג)     |
| <u>text</u>                 | נשאר     | הסרת התג       | text (הסרת התג)     |
| <a href="url">text</a>      | נשאר     | text (url)     | text (url)          |
| <code>text</code>           | נשאר     | `text`         | text (הסרת התג)     |

Meta DM (Messenger ו-Instagram) הם **plain text בלבד** — אין תמיכה ב-bold/
italic/markdown. (ולעברית אין גם Unicode bold כתחליף.) לכן מסירים את כל
התגים ומשאירים טקסט נקי, אבל שומרים URL מתוך <a> כדי שלא יאבד.
"""

import html as _html_mod
import re

# ביטויי regex מקומפלים — מחושבים פעם אחת בזמן import
_BOLD_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)
_ITALIC_RE = re.compile(r"<i>(.*?)</i>", re.DOTALL)
_UNDERLINE_RE = re.compile(r"<u>(.*?)</u>", re.DOTALL)
_CODE_RE = re.compile(r"<code>(.*?)</code>", re.DOTALL)
_LINK_RE = re.compile(r'<a\s+href=["\']([^"\']*)["\']>(.*?)</a>', re.DOTALL)
# תגים כלליים שנשארו — הסרה (למקרה שיש תגים לא ידועים)
_REMAINING_TAGS_RE = re.compile(r"<[^>]+>")

# ערוצי Meta DM — plain text בלבד (Messenger + Instagram).
_META_CHANNELS = ("meta_msg", "meta_ig")


def format_message(html_text: str, channel: str) -> str:
    """ממיר HTML של טלגרם לפורמט הערוץ המבוקש.

    Args:
        html_text: טקסט עם תגי HTML (מהמודל).
        channel: שם הערוץ — "telegram" / "whatsapp" / "meta_msg" / "meta_ig".

    Returns:
        טקסט מעוצב בפורמט הערוץ.
    """
    if channel == "telegram":
        # טלגרם תומך ב-HTML ישירות — ללא שינוי
        return html_text

    if channel == "whatsapp":
        return _html_to_whatsapp(html_text)

    if channel in _META_CHANNELS:
        # Messenger/Instagram DM — plain text. אם נשלח HTML גולמי, מטא
        # מציגה את התגים (<b> וכו') כטקסט במקום לעצב.
        return _html_to_plain(html_text)

    # ערוץ לא מוכר — הסרת כל התגים כ-fallback בטוח
    return _html_to_plain(html_text)


def _html_to_plain(text: str) -> str:
    """המרת HTML ל-plain text — לערוצים בלי עיצוב (Messenger/Instagram).

    קישורים נשמרים כ-"text (url)" כדי שה-URL לא יאבד; שאר התגים מוסרים
    ו-HTML entities מפוענחים (טקסט רגיל, לא HTML).
    """
    text = _LINK_RE.sub(r"\2 (\1)", text)
    text = _REMAINING_TAGS_RE.sub("", text)
    text = _html_mod.unescape(text)
    return text


def _html_to_whatsapp(text: str) -> str:
    """המרת HTML לפורמט WhatsApp (Markdown-like)."""
    # סדר חשוב — קישורים לפני הסרת תגים כלליים
    text = _LINK_RE.sub(r"\2 (\1)", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _ITALIC_RE.sub(r"_\1_", text)
    text = _CODE_RE.sub(r"`\1`", text)
    # underline — WhatsApp לא תומך, מסירים את התג בלבד
    text = _UNDERLINE_RE.sub(r"\1", text)
    # הסרת תגים שנותרו (למשל <pre>, <s>)
    text = _REMAINING_TAGS_RE.sub("", text)
    # פענוח HTML entities — WhatsApp מקבל טקסט רגיל, לא HTML
    text = _html_mod.unescape(text)
    return text
