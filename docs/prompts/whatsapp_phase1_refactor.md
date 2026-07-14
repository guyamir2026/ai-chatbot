# שיחה 1 — ריפקטור: חילוץ לוגיקה גנרית מ-handlers.py

## מטרה

להפריד את הלוגיקה העסקית (intent detection, RAG pipeline, LLM, rate limiting) מקוד ה-Telegram ב-`bot/handlers.py`, כך שתשב במודול גנרי `core/message_processor.py` שכל ערוץ הודעות יוכל לקרוא לו.

**זה ריפקטור בלבד — אפס שינוי בהתנהגות.** אחרי הריפקטור, הבוט חייב לעבוד בדיוק כמו קודם.

---

## הקשר — למה עושים את זה

זה שלב 1 מתוך 3 להוספת תמיכה ב-WhatsApp (Twilio). כרגע הלוגיקה העסקית (80% מהקוד) צמודה ל-Telegram API ב-`bot/handlers.py`. כדי לא לשכפל לוגיקה כשנוסיף ערוץ שני, צריך קודם לחלץ אותה למקום גנרי.

## קבצים לקריאה לפני שמתחילים

קרא את כל הקבצים האלה לפני שאתה כותב קוד:

1. `bot/handlers.py` — **הקובץ המרכזי**. כאן יושבת כל הלוגיקה שצריך לחלץ
2. `bot/telegram_bot.py` — רישום handlers ו-ConversationHandler
3. `live_chat_service.py` — מנגנון live chat + decorators (`@live_chat_guard`)
4. `rate_limiter.py` — `@rate_limit_guard`, `check_rate_limit`, `record_message`
5. `bot_state.py` — שמירת reference ל-bot ו-loop (משמש broadcast_service)
6. `broadcast_service.py` — שידורים — משתמש ב-`bot_state.py` לשליחת הודעות
7. `intent.py` — זיהוי כוונות (regex + LLM)
8. `llm.py` — קריאה ל-LLM
9. `rag/engine.py` — RAG pipeline
10. `config.py` — system prompt, הגדרות
11. `CLAUDE.md` — כללי פיתוח (חובה לעקוב)

## מה לחלץ ל-`core/message_processor.py`

פונקציה מרכזית `process_incoming_message(user_id, text, user_info)` שמכילה:

1. **Rate limiting** — `check_rate_limit` + `record_message`
2. **Intent detection** — `detect_intent` (regex + LLM)
3. **ניתוב לפי intent** — greeting, business_hours, complaint, booking, location, contact, וכו'
4. **RAG pipeline** — `_handle_rag_query` (retrieve → build context → LLM → quality check)
5. **שאלות המשך** — חילוץ `[שאלות_המשך: ...]` מהתשובה
6. **זיכרון שיחה** — conversation memory (load/save)

הפונקציה מחזירה אובייקט תוצאה (dataclass או dict) שמכיל:
- `text` — טקסט התשובה (ב-HTML של טלגרם, כרגע)
- `follow_up_questions` — רשימת שאלות המשך (אם יש)
- `intent` — ה-intent שזוהה
- `action` — פעולה מיוחדת אם נדרשת (request_agent, start_booking, send_location, send_contact, וכו')

## מה נשאר ב-`bot/handlers.py`

- קבלת `update` מ-Telegram ושליפת `user_id`, `text`, `chat_id`
- קריאה ל-`process_incoming_message`
- תרגום התוצאה לפעולות Telegram (שליחת הודעה, כפתורים, InlineKeyboard)
- ConversationHandler לתהליך תורים (נשאר ספציפי לטלגרם כרגע)
- `_reply_html_safe` — נשאר בטלגרם
- `_notify_owner` — נשאר בטלגרם

## כללים קריטיים

### Guards — חובה על כל handler
`@rate_limit_guard` ו-`@live_chat_guard` חייבים להמשיך לעטוף כל handler ב-Telegram. ב-`message_processor` ה-rate limiting יהיה חלק מהלוגיקה הפנימית (לא decorator), כי ב-WhatsApp אין את אותו מבנה של handlers.

### user_id — string בכל מקום
ב-Telegram ה-user_id הוא מספרי, ב-WhatsApp זה מספר טלפון. ה-DB כולו בנוי על `user_id` כ-string אז זה בסדר, אבל וודא שב-`message_processor` ה-user_id מטופל כ-string ולא כ-int.

### HTML formatting
כרגע התשובות מה-LLM מגיעות ב-HTML (טלגרם). בשלב הזה **אל תשנה את זה** — ההמרה לפורמטים אחרים תיעשה בשלב 2. ה-processor מחזיר HTML כמו שהוא.

### broadcast_service
`broadcast_service.py` משתמש ב-`bot_state.py` (שמחזיק reference ל-Bot object ול-event loop). בשלב הזה **אל תשנה אותו**. ההתאמה תהיה בשלב 2.

### _handle_rag_query — צינור אחד בלבד
לפי CLAUDE.md: "כל נתיב שמפעיל את צינור ה-RAG חייב לעבור דרך `_handle_rag_query`". וודא שאחרי הריפקטור עדיין יש נקודת כניסה אחת ל-RAG.

### Exceptions — תמיד ללוג
לפי CLAUDE.md: `except Exception: pass` אסור. תמיד `logger.error(...)`.

## מבנה קבצים צפוי

```
core/
├── __init__.py
└── message_processor.py    ← הלוגיקה הגנרית
ai_chatbot/core/
├── __init__.py
└── message_processor.py    ← wrapper (כמו שאר המודולים ב-ai_chatbot/)
```

## טסטים

- **יש ~500 טסטים שעוברים כרגע.** הרץ `python -m pytest tests/ -v` אחרי כל שינוי משמעותי.
- 42 כשלונות async הם pre-existing (בעיית pytest-asyncio), תתעלם מהם.
- **הוסף טסטים ל-`message_processor.py`** — ב-`tests/test_message_processor.py`. Mock ל-LLM ו-RAG. לפחות:
  - greeting intent → תשובת ברכה
  - general question → עובר דרך RAG
  - rate limit exceeded → הודעת חסימה
  - complaint intent → action=request_agent

## מה לא לעשות

- לא לשנות את ה-DB schema
- לא לשנות את ה-admin panel
- לא להוסיף WhatsApp — זה שלב 2
- לא לשנות את `broadcast_service.py`
- לא לשבור את ממשק ה-handlers הקיים — הם רק צריכים לקרוא ל-processor
