# Meta DM — ספק טכני (Instagram + Facebook Messenger)

> **סטטוס:** תוכנית מאושרת לתכנון. טרם החל מימוש.
> מטרה: הוספת ערוצי Instagram DM ו-Facebook Messenger ל-ai-business-bot, כדי שעסקים שהקהל שלהם נמצא במטא יוכלו להפעיל את אותו בוט גם שם.

---

## עקרון מנחה

ערוצי מטא (IG + Messenger) הם **שני ערוצים נפרדים מבחינה לוגית**, אך חולקים את אותה תשתית טכנית: OAuth אחד, webhook אחד, Graph API אחד. בקוד הם יבואו לידי ביטוי כשני adapters דקים מעל מודול sender משותף.

**אין כפיית קבוצות.** לקוח יכול להפעיל כל שילוב של ערוצים: רק IG, רק Messenger, שניהם, או בשילוב עם Telegram/WhatsApp. הכלל ב-`CLAUDE.md` שאומר "כל לקוח עובד על ערוץ אחד בלבד — או Telegram או WhatsApp" הוא **ברירת מחדל עסקית** (Telegram ו-WhatsApp מתחרים בדרך כלל על אותו לקוח), לא אילוץ טכני. כשנדרש — מותר להפעיל מספר ערוצים במקביל.

הסעיף ב-CLAUDE.md יעודכן בהתאם כדי שיהיה ברור: ערוצים נפרדים — היכן שיש היגיון עסקי, נספק. אין יותר הנחה ש-deployment = ערוץ יחיד.

---

## מיפוי פיצ'רים — איפה כל דבר יושב

### 1. שכבת קליטה (webhook)

**חדש:** `messaging/meta_webhook.py`

- מקביל ל-`messaging/whatsapp_webhook.py` הקיים.
- חושף שני endpoints ב-Flask:
  - `GET /webhooks/meta` — verification של מטא (echo של `hub.challenge`).
  - `POST /webhooks/meta` — קליטת הודעות נכנסות מ-IG ו-Messenger.
- מאמת חתימה `X-Hub-Signature-256` עם `META_APP_SECRET`.
- מפענח את ה-payload (מטא משתמשת באותו פורמט ל-IG ול-Messenger; נבדל ב-`object` ברמה העליונה: `instagram` או `page`), מזהה את הערוץ, ומפנה ל-`_send_meta_response` המקביל ל-`_send_whatsapp_response`.

### 2. שכבת שליחה (Graph API)

**חדש:** `messaging/meta_sender.py`

- `send_meta_message(recipient_id, text, channel)` עם `channel ∈ {ig, messenger}`.
- שתי הקריאות זהות מבנית (POST ל-`/me/messages` עם access_token של העמוד), נבדלות רק ב-endpoint וב-IDs.

**חדש:** `messaging/meta_adapter.py`

- שני מימושים של `MessageAdapter` (אחד ל-IG, אחד ל-Messenger), עוטפים את `meta_sender`.
- שאר הקוד (`broadcast_service`, `followup_service`, `live_chat_service`) כבר מדבר מול `MessageAdapter` מופשט — יעבוד אוטומטית.

### 3. תקרת אורך הודעה ועמוד ציבורי

המנגנון של `_send_whatsapp_response` שמעביר הודעות ארוכות לעמוד `/p/<page_id>` כבר עובד — נשכפל את הרעיון:

- **חדש:** `_send_meta_response(...)` בתוך `meta_webhook.py` שיבדוק תקרת אורך ויפנה לאותו מנגנון `/p/<page_id>` קיים.
- **תקרות מטא:**
  - Messenger: 2000 תווים
  - Instagram DM: **1000 תווים** (פחות מ-WhatsApp! קל לעבור מבלי לשים לב.)
- **לעדכן ב-CLAUDE.md** — להוסיף את התקרות החדשות לסעיף "WhatsApp — תקרת אורך הודעה" (או להפוך אותו לסעיף "ערוצים — תקרות אורך").

### 4. OAuth של Facebook Login

**חדש:** `admin/meta_oauth.py` (Blueprint ב-Flask)

- מסך התקנה חד-פעמי: בעל העסק לוחץ "חבר חשבון מטא" → redirect ל-Facebook OAuth → callback → קבלת page access token (long-lived) → שמירה מוצפנת ב-DB.
- **הצפנה:** `cryptography.fernet` עם מפתח מ-`META_TOKEN_ENCRYPTION_KEY` ב-env.
- **רענון tokens:** long-lived page tokens של מטא לא פגים (60 יום ל-user token, אבל page token שנוצר ממנו לא פג). עדיין שווה לוג כל שימוש כדי לזהות נפילה.

### 5. צינור LLM ו-RAG

**אפס שינוי.** כל הקוד ב-`core/message_processor.py`, `llm.py`, `intent.py`, `entity_extraction.py` כבר ערוץ-אגנוסטי. הודעה נכנסת מ-IG עוברת בדיוק את אותה דרך כמו הודעה מטלגרם.

### 6. Knowledge Base

**אפס שינוי בסכמה.** משאירים את הפיצול הקיים: `business_hours`, `categories`, `services`, FAQ. הקוד החדש רק קורא אותם.

### 7. Handoff ו-live chat

**שימוש חוזר מלא:**

- `[HANDOFF]` token מהמודל → `should_handoff_to_human` הקיים.
- ההתראה לבעל העסק תצא דרך **ערוץ ההתראות שכבר מוגדר אצלו** (Telegram אישי / WhatsApp אישי) — אין צורך בערוץ התראה חדש.
- `live_chat_guard` יעבוד כמו שהוא — כל handler חדש ב-Meta חייב לעבור דרכו (כלל קיים).

### 8. חלון 24 השעות של מטא

האילוץ הקשה ביותר. מטא אוסרת לשלוח הודעות יזומות ב-DM יותר מ-24 שעות אחרי ההודעה האחרונה של המשתמש, אלא אם משתמשים ב-`MESSAGE_TAG` מסוים.

**חדש:** `messaging/meta_window.py` — מודול קטן שעוקב אחרי `last_inbound_at` לכל שיחה ומחזיר `is_within_24h_window(conversation_id) -> bool`.

- כל שליחה יוצאת ב-`meta_sender` בודקת את החלון. מחוץ לחלון: זורקת חריגה אם אין `MESSAGE_TAG` ב-payload.
- ל-handoff/escalation שמגיע אחרי 24 שעות — לא ניתן לחזור ב-DM. במקום זה: ההתראה לבעל העסק כוללת לינק ישיר לשיחה ב-IG/Messenger inbox, והוא חוזר ידנית.

### 9. Admin UI

**חדש:** `admin/templates/meta_setup.html` — מסך OAuth + סטטוס חיבור (page name, IG account name, token age, מצב webhook).

**שימוש חוזר:** דף לוג השיחות, ניהול KB, התראות — אותם דפים, רק סינון נוסף לפי `channel ∈ {meta_ig, meta_messenger}`.

### 10. Consent ו-PII (תיקון 13)

ב-IG/Messenger אין `/start` כמו בטלגרם — המשתמש פשוט שולח הודעה ראשונה. הסכמה תוצג כתשובה הראשונה של הבוט (כפתור inline / quick reply עם טקסט "אישור").

- `@consent_guard` הקיים יעבוד, רק עם adapter שיודע לשלוח quick replies של מטא.
- כל הכללים הקיימים על סדר decorators (`block → rate_limit → vacation → live_chat → consent`) ועל אי-כתיבת PII לפני הסכמה — נשמרים.

### 11. טסטים

- `tests/test_meta_webhook.py` — חתימה תקפה / לא תקפה / payload של IG / payload של Messenger.
- `tests/test_meta_sender.py` — mock ל-Graph API, בדיקת חלון 24 שעות, שגיאות tokens.
- `tests/test_meta_adapter.py` — אינטגרציה עם `MessageAdapter`.
- mocks ל-Facebook Graph API — לא קוראים ל-API אמיתי בטסטים (כלל קיים).

---

## טבלאות DB חדשות

```sql
-- credentials של חיבור מטא (אחד פר deploy בשלב הזה)
CREATE TABLE meta_credentials (
  id INTEGER PRIMARY KEY,
  page_id TEXT NOT NULL UNIQUE,
  ig_business_account_id TEXT,
  access_token_encrypted TEXT NOT NULL,
  page_name TEXT,
  ig_username TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- חלון 24 השעות + מטא-דאטה לשיחה
CREATE TABLE meta_conversations (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,        -- 'ig' או 'messenger'
  recipient_id TEXT NOT NULL,   -- PSID של פייסבוק / IGSID של אינסטגרם
  customer_name TEXT,
  last_inbound_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'bot',  -- bot/human/escalated
  UNIQUE(channel, recipient_id)
);
```

לפי הכלל ב-CLAUDE.md — שתי הטבלאות ייכנסו גם ל-`docs/privacy_data_matrix.md` באותו commit, ול-`delete_user_data` ב-`database.py`.

---

## משתני סביבה חדשים (`.env.example`)

```env
META_APP_ID=
META_APP_SECRET=
META_VERIFY_TOKEN=                    # token שאני בוחר ל-webhook verification
META_TOKEN_ENCRYPTION_KEY=            # fernet key להצפנת page tokens
META_GRAPH_API_VERSION=v21.0
```

---

## מה צריך לעדכן ב-CLAUDE.md

1. **סעיף "ערוצים"** — להבהיר שאין כפיית קבוצות. כל שילוב מותר טכנית; ההפרדה בין Telegram ל-WhatsApp היא ברירת מחדל עסקית בלבד.
2. **סעיף "WhatsApp — תקרת אורך הודעה"** — להרחיב לכלל הערוצים: Messenger 2000, Instagram 1000, WhatsApp 1600. להפנות ל-`_send_meta_response`.
3. **סעיף חדש: "Meta — חלון 24 שעות"** — אסור לשלוח הודעות יזומות ב-DM אחרי 24 שעות בלי `MESSAGE_TAG`. השלכות על broadcasts ו-followups עתידיים.
4. **צ'ק ליסט הקלטת לקוח** (`docs/client_checklist.md`) — להוסיף שלב "חיבור OAuth של מטא" כאשר הלקוח בוחר ערוץ DM.

---

## שלבי מימוש מומלצים

1. **תשתית מטא בלבד** — webhook + verification + לוג של הודעות נכנסות (בלי תשובה). וידוא שהחיבור עובד מקצה לקצה.
2. **OAuth + שמירת token** — מסך admin להתקנה, הצפנה.
3. **שליחת הודעה ראשונה** — adapter + sender, hello world ל-IG ול-Messenger.
4. **חיבור ל-`message_processor`** — הודעה נכנסת → RAG → תשובה יוצאת. כאן הפיצ'ר חי לראשונה.
5. **חלון 24 שעות + handoff + live chat** — חוקיות מטא ושילוב עם הקוד הקיים.
6. **Admin UI מלא** — סטטוס, לוגים, ניהול.
7. **טסטים + עדכון CLAUDE.md + `privacy_data_matrix.md` + `client_checklist.md`**.

---

## דחיות מודעות (לא ב-MVP)

- **הודעות לא טקסטואליות** (תמונות, קוליות, סטיקרים) — escalation לבעל העסק או transcription של קוליות עם Whisper. **דחוי** — נחזור לזה אחרי שה-MVP יציב.
- **broadcast_service ב-DM** — שליחת broadcasts ב-Messenger/IG מוגבלת לחלון 24 שעות + `MESSAGE_TAG`. **דחוי** — לא מבטלים את broadcasts הקיימים בטלגרם/וואטסאפ, פשוט לא מוסיפים תמיכת מטא ב-broadcast עכשיו.
- **followup_service ב-DM** — אותו אילוץ של חלון 24 שעות. **דחוי**.

---

## סיכונים ושאלות פתוחות

- **App Review של מטא** — לפני הפקה אצל לקוחות אמיתיים, צריך לעבור App Review + Business Verification. לפיילוט עד 25 משתמשים-בודקים אפשר ב-Development mode.
- **המרת PSID ל-display name** — Graph API נותן `first_name + last_name` רק אם המשתמש לחץ "Get Started" או יש לעמוד הרשאה מתאימה. אחרת רק PSID אטום. צריך להחליט מה רואים בלוג השיחות במקרה הזה.
- **הצפנת tokens** — אם `META_TOKEN_ENCRYPTION_KEY` הולך לאיבוד, אין דרך לפענח את ה-tokens השמורים. צריך לתעד שלב גיבוי של המפתח בצ'ק ליסט ההקלטה.
