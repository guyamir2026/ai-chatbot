# חקירת באג קציצת תשובות LLM (WhatsApp) — מסמך תיעוד

**סטטוס:** ✅ **נפתר** (2026-05-05). שורש הבעיה: Gemini 2.5 thinking tokens.
**עודכן לאחרונה:** 2026-05-05

## 🎯 התשובה — מה שמצאנו

הלוגים האבחוניים שהוספנו חשפו את השורש מיד:
```
[INFO] llm: LLM diag: model=gemini-2.5-flash finish_reason=length
prompt_tokens=4331 completion_tokens=81 max_tokens=2048 chars=178 utf8_bytes=311
```

`completion_tokens=81` אבל `finish_reason=length` עם `max_tokens=2048` — פיזית
בלתי אפשרי, אלא אם...

**Gemini 2.5 Flash הוא thinking model.** הוא משתמש ב-tokens פנימיים של "חשיבה"
שנספרים אל `max_tokens` אבל **לא נחשפים ב-`completion_tokens`**:

| מטריקה | ערך |
|--------|-----|
| max_tokens budget | 2048 |
| Thinking tokens (לא נחשפים) | ~1967 |
| Visible output tokens | 81 |
| Sum | 2048 ⇒ `finish_reason=length` |

מתועד בגוגל: https://ai.google.dev/gemini-api/docs/thinking

### התיקון
ב-`llm.py`, כש-`OPENAI_MODEL.startswith("gemini-2.5")`, מעבירים
`extra_body={"extra_body": {"google": {"thinking_config": {"thinking_budget": 0}}}}`
ל-`client.chat.completions.create`. זה מבטל את ה-thinking לחלוטין —
לבוט שירות לקוחות לא נדרש reasoning עמוק. כל ה-2048 tokens מוקדשים
לתשובה האמיתית.

הוחל על שלוש הקריאות ב-`llm.py`: `generate_answer`,
`generate_page_content`, ו-`_generate_summary`.

---

## תיעוד היסטורי — מה ניסינו לפני שמצאנו

מסמך זה מתעד את כל מה שנבדק/נוסה עד היום, במטרה למנוע חקירה כפולה
ולתת לחוקר הבא נקודת התחלה. אם פתרת — עדכן כאן.

---

## תיאור הבעיה

הבוט (בעיקר ב-WhatsApp) שולח תשובה עם רשימה ארוכה (למשל מחירון/רשימת
שירותים) **שנחתכת באמצע מילה** ולא בגבול שמשמעותי לעין.

**דוגמה אחרונה (2026-05-05):**
```
📅 *בקשת תור*

בשמחה! במכון היופי של דנה אנחנו מציעים מגוון רחב של טיפולים מקצועיים, הנה פירוט קצר:

*שירותי שיער*
•   *תספורות*:
    •   *תספורת נשים ועיצוב*: כולל ייעוץ, חפיפה, ת

אנא כתבו את *השירות* שתרצו להזמין:
(או שלחו *ביטול* לביטול)
```

**מאפיינים:**
- ה-prefix וה-suffix של ה-handler (`📅 בקשת תור` / `אנא כתבו...`) נשארים שלמים
- רק תוכן ה-LLM באמצע נחתך
- חיתוך מתרחש לעיתים מתחת ל-1600 תווים (לא תקרת Twilio הידועה)
- אורך בעייתי קבוע לא זוהה — לפעמים מאות תווים, לפעמים אלפים

**הצהרת בעל המוצר:**
> "**לא מדובר במגבלת תווים** מכמה סיבות. אם תרצה אפרט אותם."

---

## תיקונים שנוסו בעבר ועזרו חלקית / לא פתרו

### 1. WhatsApp 1600-char Twilio cut (commit `383e597`, 2026-04-27)
**הניחוש:** Twilio קוצץ הודעות מעל 1600 תווים בשקט.
**הפתרון:** הוספת safety net ב-`_send_whatsapp_response` שמעבירה
הודעות ארוכות לעמוד HTML (`/p/<id>`).
**תוצאה:** עזר למחירונים מאוד ארוכים. לא פותר את המקרה הנוכחי שמתרחש
**מתחת** ל-1600 תווים.

### 2. WhatsApp formatter HTML→Markdown (commit `8ba4aae`)
לא פתר.

### 3. ICS דרך URL ב-WhatsApp במקום media_url (commit `de337ed`)
שחרר נושא אחר. לא קשור.

### 4. זיהוי `finish_reason='length'` ב-`generate_answer` (commit `4e1f779`, 2026-05-05)
**הניחוש:** OpenAI חותך את התשובה ב-`max_tokens`.
**הפתרון:** בדיקת `finish_reason` והוספת `_trim_to_last_sentence`.
**תוצאה:** מטפל בתסמין בלבד — מסתיר את החיתוך באמצע מילה ע"י החלפה
בנקודות-נקודות, **לא פותר** את הסיבה השורשית. בעל המוצר ציין במפורש שזה
לא קביל כי הלקוח רואה רק חצי מחירון.

---

## מה נבדק ונשלל

### `messaging/formatter.py` — ה-HTML→Markdown
מבצע רק החלפות regex של תגי HTML. אין חיתוך באמצע מחרוזת. ✗

### `messaging/whatsapp_sender.py:send_whatsapp`
קריאה ישירה ל-`twilio.rest.client.messages.create(body=...)`. בלי ניתוח/
חיתוך פנימי. ✗

### `_send_whatsapp_response` (whatsapp_webhook.py)
בודק `len(text) > WHATSAPP_MAX_LENGTH=1600` ועובר לעמוד HTML. אם תחת
הסף — קורא ל-`_send_whatsapp_raw` ישירות. ✗

### סכמת DB (`conversations.message`)
`TEXT NOT NULL` — אין מגבלת אורך אינהרנטית ב-SQLite. ✗

### `rag/chunker.py:chunk_text`
מפצל לפסקאות ⇒ משפטים ⇒ מילים. **לעולם לא חותך באמצע מילה**.
`CHUNK_MAX_TOKENS=300` משפיע על אורך chunks ב-RAG retrieval, **לא על
התשובה הסופית**. ✗

### `rag/engine.py:format_context`
פשוט מחבר chunks עם `---`. ✗

### `llm.py:strip_source_citation`
regex ב-MULTILINE שמסיר שורות שמתחילות ב-"מקור:" או "Source:" וגם תבנית
`[Category — desc]`. הרגקס מעוגן ל-`^...$` ובסוף שורה — לא יחתוך
באמצע מילה. ✗

### `llm.py:strip_handoff_marker`
מסיר רק את `[HANDOFF]` מתחילת המחרוזת. ✗

### `llm.py:strip_follow_up_questions`
מסיר רק את `[שאלות_המשך: ...]` בסוף. ✗

### `_get_calendar_service` / טוקן GCal פג
לא קשור — הבעיה תוצג גם כשאין שימוש ב-GCal.

### Twilio API
לפי תיעוד, מגבלה אופיציאלית של 1,600 תווים. הבאג קורה גם **מתחת** לסף.

### גודל system prompt + RAG context
לא זוהה כפוגע ב-output budget, כי `gpt-4.1-mini` תומך ב-128K context
ו-`max_tokens=2048` (output) הוא הגבול שיצרנו, רחוק מתפוס.

---

## תאוריות שלא נבדקו — רעיונות לחוקר הבא

1. **Twilio API חותך לפי bytes ולא chars**:
   עברית ב-UTF-8 = 2 בייטים לתו. 800 chars = ~1600 bytes. אם Twilio
   באמת בודק bytes, הסף האפקטיבי הוא חצי ממה שאנחנו חושבים.
   **בדיקה**: לוג של `len(text.encode("utf-8"))` ב-`_send_whatsapp_raw`
   והשוואה ל-WHATSAPP_MAX_LENGTH.

2. **Twilio segments**: הודעת WhatsApp עשויה להישלח ב-segments. אם
   segment שני נופל (rate limit / network), הלקוח רואה רק את הראשון.
   **בדיקה**: לוג של `message.num_segments` מתגובת Twilio.

3. **OpenAI API מחזיר תשובה חלקית עם `finish_reason='stop'`**:
   המודל "החליט" שזה הסוף — אבל באמצע מילה עברית? אולי בעיה ב-
   tokenizer של עברית.
   **בדיקה**: לוג של `response.choices[0].finish_reason` ו-`raw_answer`
   המלא לפני כל post-processing.

4. **stop_sequences נסתרים**: אם הוגדר stop ב-API call (לא ראינו) או
   המודל פגש token שמתפרש כסטופ.
   **בדיקה**: ודא ש-`client.chat.completions.create` נקרא בלי `stop=`.

5. **גרסת openai SDK**: שינויים ב-SDK עלולים לעבד את התשובה אחרת.
   **בדיקה**: `pip show openai` ושינויים ב-`requirements.txt`.

6. **Hebrew RTL/LTR handling**: כיוון ה-RTL יוצר אילוזיה ויזואלית
   שהטקסט "נחתך" כשבעצם ה-rendering לא תקין.
   **בדיקה**: dump bytes גולמי של `formatted` (אחרי format_message)
   והשוואה למה שהלקוח רואה.

7. **WhatsApp client / מכשיר ספציפי**: יתכן שהבאג קורה רק בגרסת
   WhatsApp מסוימת או באנדרואיד מסוים.
   **בדיקה**: בקש מהלקוח ל-export את הצ'אט (`Settings → צ'אט → שתף`)
   ולוודא שזה מה שבאמת הגיע למכשיר.

8. **DB encoding bug ב-`save_message`**: אולי שמירה ושליפה מ-DB מאבדים
   bytes. אבל ההודעה שנשלחת ללקוח לא עוברת דרך DB read אחרי השמירה,
   אז זה לא צריך להיות.

9. **Race condition ב-RAG retrieval**: קצב גבוה של בקשות מוביל
   ל-context cropping. לא ראיתי קוד כזה, אבל שווה לבדוק.

10. **gpt-4.1-mini באג**: המודל הזה יחסית חדש (אפריל 2025). אולי בעיה
    ספציפית למודל. נסה להחליף ל-`gpt-4o` או `gpt-4-turbo`:
    `OPENAI_MODEL=gpt-4o python main.py --bot`.

---

## כלי אבחון מומלצים להוסיף

1. **לוג מלא של תשובת LLM גולמית** ב-`generate_answer`:
   ```python
   logger.info(
       "LLM raw response: model=%s prompt_tokens=%d completion_tokens=%d "
       "finish_reason=%s response_len=%d response=%r",
       OPENAI_MODEL,
       response.usage.prompt_tokens,
       response.usage.completion_tokens,
       response.choices[0].finish_reason,
       len(raw_answer),
       raw_answer,
   )
   ```

2. **לוג של `len(text.encode("utf-8"))`** ב-`_send_whatsapp_raw` לפני
   קריאה ל-Twilio:
   ```python
   logger.info(
       "WA send: chars=%d, utf8_bytes=%d, to=%s",
       len(text), len(text.encode("utf-8")), to_number,
   )
   ```

3. **לוג של תגובת Twilio** (משאיר עקבות לחיתוך ב-API):
   ```python
   message = client.messages.create(**kwargs)
   logger.info(
       "Twilio response: sid=%s status=%s num_segments=%s body_len=%d",
       message.sid, message.status, message.num_segments, len(formatted),
   )
   ```

---

## איך לשחזר

1. תקנפג את הבוט עם DB ריק (אין שירותים פעילים) → start_booking יפול
   ל-fallback של RAG.
2. ב-RAG הזן מחירון ארוך (10+ שירותים, כל אחד עם תיאור).
3. שלח "אפשר לקבוע תור?" ב-WhatsApp.
4. בדוק את ההודעה שמתקבלת — אם זה משחזר, אסוף את הלוגים החדשים מסעיף
   "כלי אבחון" כדי לאפיין.

---

## מסקנה והמלצה

נכון להיום, **אין לנו כלי אבחון מספיק טוב כדי לאפיין את שורש הבעיה**.
הצעדים הבאים המומלצים:

1. הוספת הלוגים מסעיף "כלי אבחון" ל-production (ללא מידע אישי של
   לקוחות).
2. כשהבאג חוזר — לאסוף לוג עם המידע המדויק.
3. עם המידע, לאפיין: האם זה max_tokens? bytes? Twilio segments?
   tokenizer עברית?

עד אז, הסיומת `…` שהוספנו ב-`_trim_to_last_sentence` היא **טלאי בלבד**
שמסתיר את החיתוך באמצע מילה — זה לא פתרון.
