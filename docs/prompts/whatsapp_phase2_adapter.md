# שיחה 2 — Messaging Adapter + WhatsApp Integration

## מטרה

להוסיף שכבת הפשטה (Messaging Adapter) ואינטגרציה עם WhatsApp דרך Twilio. אחרי השלב הזה, המערכת תומכת בשני ערוצים — Telegram ו-WhatsApp — עם לוגיקה עסקית משותפת.

**דרישת קדם:** שלב 1 (ריפקטור) הושלם — קיים `core/message_processor.py` עם הלוגיקה הגנרית.

---

## הקשר

זה שלב 2 מתוך 3. בשלב 1 חילצנו את הלוגיקה העסקית ל-`core/message_processor.py`. עכשיו צריך:
1. ליצור ממשק adapter אחיד
2. לממש adapter ל-WhatsApp (Twilio)
3. להוסיף webhook endpoint לקבלת הודעות
4. להוסיף פורמטינג טקסט שמתאים לערוץ

## קבצים לקריאה לפני שמתחילים

1. `core/message_processor.py` — הלוגיקה הגנרית (מהשלב הקודם)
2. `bot/handlers.py` — ה-handlers של Telegram (לראות איך קוראים ל-processor)
3. `admin/app.py` — ה-Flask app (כאן נוסיף webhook endpoint)
4. `config.py` — משתני סביבה
5. `broadcast_service.py` — שידורים (צריך התאמה)
6. `bot_state.py` — שמירת reference לבוט
7. `live_chat_service.py` — שליחת הודעות ב-live chat (`send_telegram_message`)
8. `.env.example` — תיעוד משתנים
9. `docs/client_checklist.md` — צ'ק ליסט הקלטה
10. `CLAUDE.md` — כללי פיתוח (חובה)

## 1. Config — משתני סביבה

ב-`config.py` הוסף:

```python
# ─── WhatsApp / Twilio ──────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
```

הוסף ולידציה ב-`validate_config` — אם WhatsApp credentials חלקיים (חלק מוגדרים וחלק לא), להזהיר.

## 2. Messaging Adapter — שכבת הפשטה

```
messaging/
├── __init__.py
├── base.py              ← MessageAdapter (abstract base)
├── telegram_adapter.py  ← עוטף את python-telegram-bot
├── whatsapp_adapter.py  ← עוטף את Twilio SDK
└── formatter.py         ← format_message(html_text, channel) — המרת HTML לפורמט הערוץ
```

### base.py — ממשק

```python
class MessageAdapter(ABC):
    async def send_text(self, chat_id: str, text: str, buttons=None) -> None: ...
    async def send_contact(self, chat_id: str, name: str, phone: str) -> None: ...
    async def send_location(self, chat_id: str, lat: float, lon: float) -> None: ...
    async def send_file(self, chat_id: str, file_data: bytes, filename: str) -> None: ...
```

### formatter.py — המרת טקסט

כמעט כל מקום ב-handlers.py משתמש ב-HTML parse_mode של טלגרם (`<b>`, `<i>`, `<u>`). ה-LLM מייצר HTML כי ה-system prompt מנחה אותו. הפונקציה:

```python
def format_message(html_text: str, channel: str) -> str:
    """ממיר HTML של טלגרם לפורמט הערוץ המבוקש."""
```

| תג | Telegram | WhatsApp |
|---|---|---|
| `<b>text</b>` | נשאר | `*text*` |
| `<i>text</i>` | נשאר | `_text_` |
| `<u>text</u>` | נשאר | הסרת התג (WhatsApp לא תומך) |
| `<a href="url">text</a>` | נשאר | `text (url)` |
| `<code>text</code>` | נשאר | `` `text` `` |

### whatsapp_adapter.py — Twilio SDK

```python
from twilio.rest import Client

class WhatsAppAdapter(MessageAdapter):
    def __init__(self):
        self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        self.from_number = f"whatsapp:{TWILIO_WHATSAPP_NUMBER}"
    
    async def send_text(self, chat_id, text, buttons=None):
        formatted = format_message(text, "whatsapp")
        # Twilio SDK הוא סינכרוני — לעטוף ב-asyncio.to_thread
        await asyncio.to_thread(
            self.client.messages.create,
            body=formatted,
            from_=self.from_number,
            to=f"whatsapp:{chat_id}",
        )
```

### כפתורים — ההבדל בין הערוצים

| | Telegram | WhatsApp (Twilio) |
|---|---|---|
| כפתורים בהודעה | InlineKeyboard (ללא הגבלה) | אין תמיכה ב-API הרגיל* |
| Reply keyboard | ReplyKeyboardMarkup | לא קיים |
| תפריט בחירה | — | List message (עד 10, דרך Twilio Content Templates) |

*Twilio תומך בכפתורים דרך Content Templates (pre-approved). בשלב הזה — **fallback לטקסט מספרי:**
```
בחרו שירות:
1. תספורת
2. צבע
3. החלקה

(שלחו את המספר)
```

## 3. Webhook endpoint — קבלת הודעות מ-Twilio

ב-`admin/app.py` (או קובץ נפרד `messaging/whatsapp_webhook.py` שנרשם כ-Blueprint):

```python
@app.route("/webhook/whatsapp", methods=["POST"])
@csrf.exempt  # Twilio שולח POST ללא CSRF token
def whatsapp_webhook():
    # אימות חתימה של Twilio (X-Twilio-Signature)
    # ...
    from_number = request.form["From"].replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    
    # קריאה ל-message_processor (אותה לוגיקה כמו Telegram)
    result = process_incoming_message(
        user_id=from_number,
        text=body,
        user_info={"first_name": "", "username": from_number},
    )
    
    # שליחת תשובה דרך WhatsApp adapter
    whatsapp_adapter.send_text(from_number, result.text)
```

### אימות חתימה (חשוב לאבטחה!)

Twilio שולח header `X-Twilio-Signature` שצריך לאמת עם `TWILIO_AUTH_TOKEN`. להשתמש ב:
```python
from twilio.request_validator import RequestValidator
validator = RequestValidator(TWILIO_AUTH_TOKEN)
validator.validate(url, request.form, signature)
```

## 4. התאמות בקוד קיים

### user_id — string בכל מקום
ב-Telegram: `"123456789"` (מספרי). ב-WhatsApp: `"972501234567"` (מספר טלפון).
ה-DB כבר עובד עם string. **וודא שאין מקום שעושה `int(user_id)`.**

### live_chat_service.py
הפונקציה `send_telegram_message` צריכה להפוך לגנרית — לדעת באיזה ערוץ המשתמש נמצא ולשלוח בהתאם. הוסף עמודה `channel` לטבלת `live_chat_sessions` (או ל-`users`), כדי שכשבעל העסק עונה מהפאנל, המערכת תדע לאן לשלוח.

### broadcast_service.py
שידורים צריכים לדעת באיזה ערוץ כל user רשום. אפשרות:
- עמודה `channel` בטבלת `subscribers` (או `users`)
- ברירת מחדל: `"telegram"` (backward compatible)
- בלולאת השידור — בחירת adapter לפי channel

### הגדרות ב-Admin Panel
הוסף עמוד/סקשן ב-admin panel להגדרת Twilio credentials (כמו שיש כיום ל-Telegram token, אם יש). לפחות:
- שדות להזנת `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`
- כפתור "בדוק חיבור" — שולח הודעת test

## טסטים

- הרץ `python -m pytest tests/ -v` אחרי כל שינוי.
- 42 כשלונות async הם pre-existing — תתעלם מהם.
- הוסף טסטים ב-`tests/test_whatsapp_adapter.py`:
  - Mock ל-Twilio Client
  - send_text — וודא format_message נקרא
  - webhook endpoint — request תקין מחזיר 200
  - webhook endpoint — חתימה לא תקינה מחזירה 403
- הוסף טסטים ב-`tests/test_formatter.py`:
  - HTML → WhatsApp: `<b>bold</b>` → `*bold*`
  - HTML → WhatsApp: `<i>italic</i>` → `_italic_`
  - HTML → Telegram: ללא שינוי
  - תגים מקוננים

## עדכון docs

- `.env.example` — הוסף משתני Twilio
- `docs/client_checklist.md` — הוסף שלב הגדרת WhatsApp (Twilio credentials, webhook URL)

## מה לא לעשות

- לא לשנות את תהליך קביעת התורים — זה שלב 3
- לא להוסיף Content Templates של Twilio — fallback לטקסט מספרי
- לא לשנות את ה-system prompt — ה-LLM ממשיך לייצר HTML, ה-formatter ממיר
