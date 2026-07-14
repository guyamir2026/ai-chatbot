# שיחה 3 — התאמות ספציפיות: Booking, Live Chat, Broadcasts

## מטרה

להשלים את תמיכת WhatsApp עם כל הפיצ'רים שדורשים התאמה ספציפית לערוץ: תהליך קביעת תור, live chat דו-ערוצי, שידורים, ושליחת קבצים (VCF, מיקום).

**דרישות קדם:** שלב 1 (ריפקטור) ושלב 2 (adapter + WhatsApp בסיסי) הושלמו.

---

## הקשר

זה שלב 3 (אחרון) מתוך 3. בשלב 1 חילצנו לוגיקה ל-`core/message_processor.py`. בשלב 2 יצרנו messaging adapter ו-WhatsApp webhook בסיסי. עכשיו צריך להשלים את הפיצ'רים שדורשים התאמה לערוץ.

## קבצים לקריאה לפני שמתחילים

1. `core/message_processor.py` — הלוגיקה הגנרית
2. `messaging/base.py` — ממשק adapter
3. `messaging/whatsapp_adapter.py` — adapter קיים
4. `messaging/formatter.py` — המרת טקסט
5. `bot/handlers.py` — handlers של Telegram (ConversationHandler, booking flow)
6. `bot/telegram_bot.py` — רישום handlers
7. `live_chat_service.py` — שירות live chat
8. `broadcast_service.py` — שידורים
9. `admin/app.py` — פאנל אדמין (live chat UI, broadcasts)
10. `appointment_notifications.py` — התראות תורים
11. `database.py` — סכימת DB (לראות טבלאות users, subscribers, appointments)
12. `CLAUDE.md` — כללי פיתוח (חובה)

## 1. Booking Flow ב-WhatsApp

### הבעיה
ב-Telegram יש `ConversationHandler` עם states (BOOKING_SERVICE, BOOKING_DATE, BOOKING_TIME, BOOKING_CONFIRM). ב-WhatsApp אין מנגנון מובנה ל-state machine.

### הפתרון
ניהול state ב-DB (או in-memory dict). הוסף ל-`core/` (או ל-`messaging/`):

```python
# מנגנון state לשיחות WhatsApp
# כל user_id → { "state": "booking_service", "data": {...} }
```

כש-webhook מקבל הודעה מ-WhatsApp:
1. בדוק אם יש state פתוח ל-user
2. אם כן — העבר ל-handler של ה-state הנוכחי
3. אם לא — עבד כהודעה רגילה

### כפתורים → טקסט מספרי
ב-Telegram הבחירה היא דרך InlineKeyboard. ב-WhatsApp — fallback לטקסט:

```
בחרו שירות:
1. תספורת גברים — ₪80
2. תספורת נשים — ₪150
3. צבע — ₪250

(שלחו את המספר)
```

המשתמש שולח "1" והמערכת מזהה את הבחירה.

### Google Calendar
בדיקת זמינות ויצירת אירועים — עובדים כמו שהם (הלוגיקה ב-DB/Google API, לא תלויה בערוץ).

## 2. Live Chat דו-ערוצי

### הבעיה
כשבעל העסק עונה מהפאנל, צריך לדעת לאן לשלוח — Telegram או WhatsApp.

### הפתרון

1. **עמודה `channel` בטבלת `live_chat_sessions`** (או ב-`users`) — `"telegram"` / `"whatsapp"`
2. כשנפתחת session — לשמור את הערוץ
3. ב-`send_telegram_message` (שצריך שינוי שם → `send_live_chat_message`):
   - בדוק את ה-channel של ה-session
   - שלח דרך ה-adapter המתאים

### admin/app.py — UI
בממשק ה-live chat, הוסף אינדיקציה לערוץ (אייקון Telegram / WhatsApp ליד שם המשתמש).

## 3. שידורים דו-ערוציים

### הבעיה
`broadcast_service.py` משתמש ב-`bot_state.py` (Telegram Bot object) לשליחת הודעות. צריך לתמוך בשליחה ל-WhatsApp.

### הפתרון

1. **עמודה `channel` בטבלת `subscribers`** — ברירת מחדל `"telegram"`
2. בלולאת השידור (`send_broadcast`):
   - קבץ נמענים לפי channel
   - שלח לכל קבוצה דרך ה-adapter המתאים
3. **format_message** — המרת HTML לפי הערוץ לפני שליחה

### admin/app.py — UI שידורים
בטופס השידור, הוסף אפשרות לבחור ערוץ יעד:
- [ ] Telegram
- [ ] WhatsApp
- [ ] שניהם

## 4. שליחת קבצים

### כרטיס VCF (שמור איש קשר)
- **Telegram:** `send_contact` או `send_document` עם קובץ .vcf
- **WhatsApp:** `client.messages.create(media_url=..., content_type="text/vcard")`

### מיקום
- **Telegram:** `send_location(lat, lon)`
- **WhatsApp:** Twilio לא תומך בשליחת מיקום ישירה → שלח Google Maps link בטקסט

### QR Code / תמונות
- **Telegram:** `send_photo`
- **WhatsApp:** `client.messages.create(media_url=...)`

## 5. התראות לבעל העסק

`appointment_notifications.py` ו-`_notify_owner` שולחים הודעות לבעל העסק דרך Telegram. כרגע **להשאיר כך** — בעל העסק מקבל התראות בטלגרם תמיד. (אפשר להוסיף בהמשך אפשרות לקבל גם ב-WhatsApp.)

## 6. Guards ב-WhatsApp

### rate_limit_guard
ב-Telegram זה decorator על handler. ב-WhatsApp ה-rate limiting כבר מטופל בתוך `message_processor`. וודא שה-webhook endpoint לא עוקף את ה-rate limiting.

### live_chat_guard
ב-Telegram: ה-decorator חוסם handlers כש-live chat פעיל. ב-WhatsApp: ה-webhook צריך לבדוק `LiveChatService.is_active(user_id)` ולהעביר הודעות ישירות לפאנל (בלי לעבד דרך processor).

## DB Migrations

הוסף ב-`init_db()` (מיגרציות קלות כמקובל בפרויקט):

```sql
-- ערוץ ל-subscribers
ALTER TABLE subscribers ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram';

-- ערוץ ל-live chat sessions
ALTER TABLE live_chat_sessions ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram';

-- ערוץ ל-users (אם קיימת טבלה)
ALTER TABLE users ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram';
```

## טסטים

- הרץ `python -m pytest tests/ -v` אחרי כל שינוי.
- 42 כשלונות async הם pre-existing — תתעלם מהם.
- הוסף/עדכן טסטים:
  - `tests/test_whatsapp_booking.py` — state machine, בחירה מספרית, flow מלא
  - `tests/test_broadcast_service.py` — שידור דו-ערוצי (mock ל-Twilio)
  - `tests/test_live_chat_service.py` — live chat עם channel whatsapp
  - `tests/test_formatter.py` — הוסף edge cases אם חסרים

## עדכון docs

- `docs/client_checklist.md` — הוסף בדיקות WhatsApp (booking flow, live chat, broadcasts)
- `.env.example` — וודא שמשתני Twilio מתועדים

## מה לא לעשות

- לא לשנות את ה-system prompt — ה-LLM ממשיך לייצר HTML
- לא לשבור backward compatibility — Telegram חייב להמשיך לעבוד בדיוק כמו קודם
- לא להוסיף Twilio Content Templates — זה שיפור עתידי
- לא להוסיף Instagram/SMS — זה שיפור עתידי
