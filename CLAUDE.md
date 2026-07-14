# CLAUDE.md — הנחיות פיתוח לפרויקט ai-business-bot

## תהליך עבודה

1. **קודם מתכננים** – לפני כל מימוש, יש להציג תוכנית עבודה ברורה (עם הסברים בשפה פשוטה ומובנת לכל)
2. **אחר כך מממשים** – המימוש מתחיל רק לאחר אישור התוכנית.

## כלל חשוב: 

אם נמצאו באגים כלשהם בריפו - תמיד נחפש פיתרונות שורשיים לבעיה, ולא פיתרונות "טלאי".

## שפה

- סיכומי PR, תיאורי commit, והודעות סשן — **בעברית**
- הערות בקוד (comments) — **בעברית**
- שמות משתנים, פונקציות, וטבלאות — באנגלית (כמקובל)

## ארכיטקטורה

- **מבנה מודולים:** קוד המקור בשורש הריפו (`config.py`, `database.py`, וכו'). חבילת `ai_chatbot/` היא **namespace של aliases** — meta-path finder ב-`ai_chatbot/__init__.py` ממפה כל `ai_chatbot.X` לאותו אובייקט מודול של `X` בשורש. **אין ליצור קבצי wrapper ידניים** — מודול חדש בשורש זמין אוטומטית גם כ-`ai_chatbot.<שם>`. אסור לחזור לדפוס `from X import *` בתוך `ai_chatbot/` (יוצר שני עותקים של module state).
- **בסיס נתונים:** SQLite עם WAL mode. סכימה של DB חדש ב-`init_db()` (CREATE TABLE IF NOT EXISTS בלבד). מיגרציות לטבלאות קיימות — ADD COLUMN דרך `_ensure_column`, אינדקסים תלויי-עמודה — ב-`migrations.py` בלבד, מורצים מ-`init_db` דרך `run_migrations()` אחרי ה-executescript. ראה הסעיף "DB — סדר הרצה של init_db מול migrations" למטה.
- **Admin:** Flask + HTMX + Jinja2. RTL עברית. תבניות ב-`admin/templates/`.
- **בוט:** python-telegram-bot (async). Handlers ב-`bot/handlers.py`.
- **ערוצים:** בפרודקשן כל לקוח עובד על ערוץ אחד בלבד — או Telegram או WhatsApp, לא שניהם במקביל. הקוד תומך בשניהם אבל deployment הוא ערוץ-יחיד.
- **LLM:** שלוש שכבות — A (system prompt), B (RAG context), C (quality check עם regex).
- **Multi-tenant (שלב 1 — תשתית):** ראה `docs/multi_tenant_migration_spec.md` ו-`tenancy.py`. הכללים המחייבים כבר עכשיו:
  - **זהות עסקית** (שם/טלפון/כתובת/אתר) — אך ורק דרך `config.get_business_config()` בזמן-ריצה. אסור `from config import BUSINESS_NAME` (הערך קופא ב-import ולא ניתן להחלפה פר-tenant). `build_system_prompt` מקבל `business_name` כפרמטר אופציונלי.
  - **נתיב DB** — `get_connection()` פותח את הקובץ לפי ה-tenant הנוכחי (`tenancy.tenant_db_path()`). אסור לפתוח `sqlite3.connect` ישירות מול `DB_PATH`.
  - **קביעת tenant** — רק בנקודות כניסה (Flask `before_request`, לולאות schedulers, CLI) דרך `tenant_context()` / `set_current_tenant()`. קוד עמוק קורא `get_current_tenant()` ולא מנחש. במעבר בין threads ה-context לא עובר אוטומטית — להעביר את ה-tenant כפרמטר ולקבוע מחדש.
  - **state חדש ברמת מודול** (cache/dict/singleton שמחזיק נתוני ריצה) — חייב מפתח tenant מהיום הראשון, או להיצמד ל-DB.
  - `TENANCY_STRICT=true` (env) הופך גישה בלי context לחריגה — יודלק בפלטפורמה בשלב 2; טסטים יכולים להשתמש בו לאיתור נתיבים לא מכוסים.

## כללי פיתוח

### DB — אילוצים מהרגע הראשון
- לכל טבלה חדשה: לזהות מהו ה-natural key ולהוסיף `UNIQUE` constraint.
- אם יש seed data שמשתמש יכול לדרוס — להשתמש ב-`INSERT OR REPLACE` ולא `INSERT`.

### DB — סדר הרצה של init_db מול migrations
- `init_db()` רץ `executescript` תחילה, ורק אחר-כך קורא ל-`run_migrations()`. זה אומר ש-DB **קיים** (פרודקשן) לא יקבל עמודות חדשות מתוך ה-`CREATE TABLE IF NOT EXISTS` ב-init_db — רק migration רץ עם `_ensure_column` יוסיף אותן.
- **כשמוסיפים עמודה חדשה לטבלה קיימת + אינדקס שתלוי בה**: ה-`ADD COLUMN` וה-`CREATE INDEX` חייבים להיות **שניהם ב-migrations.py בלבד**. אם תוסיף את ה-`CREATE INDEX` ב-init_db's executescript, ב-DB קיים הוא יקרוס כי העמודה עוד לא קיימת (migration רץ אחרי). ב-CI/dev (DB חדש) זה יעבוד — והבאג ייתפס רק ב-deploy לפרודקשן.
- ב-`init_db`'s `CREATE TABLE` אפשר עדיין להגדיר את העמודה לטבלאות **חדשות** (DB ריק); הטבלה לא תיווצר ב-DB קיים אז זה לא רלוונטי שם. רק אינדקסים/constraints שתלויים בעמודה נכנסים ל-migrations.
- **Multi-tenant — המיגרציות רצות על כל ה-tenants בעליית התהליך.** `main.py` קורא ל-`control_plane.migrate_all_tenants()` אחרי ה-`init_db` של ברירת-המחדל; הוא מריץ `init_db` (executescript + migrations) על ה-DB של **כל tenant פעיל**. בלי זה, ה-data-plane DB של כל tenant היה עובר מיגרציה רק פעם אחת (ב-`create_tenant`), ועמודה חדשה שנוספת אחר-כך הייתה חסרה מ-tenant DBs קיימים → `no such column` בכל כתיבה שמפנה אליה. **מסקנה:** עמודה/סכימה חדשה זמינה ל-tenants קיימים רק אחרי deploy/restart (שמריץ את הלולאה). אל תניח ש-tenant DB נמצא בגרסת סכימה מסוימת בלי שהמיגרציה רצה עליו.

### LLM Prompts — לקרוא כשלם
- כשמזריקים תוכן חדש ל-prompt — לקרוא את כל ההודעות יחד ולוודא שאין הוראות סותרות (למשל "השתמש **רק** במידע X" ואז מידע Y בהודעה נפרדת).

### HTMX — DOM consistency
- כש-HTMX מוחק/מחליף אלמנט, לוודא שכל האלמנטים הקשורים (כמו טופס עריכה מוסתר) נמחקים יחד. לעטוף קבוצות קשורות בקונטיינר משותף שה-target מכוון אליו.

### Routes — לא dead code
- לכל route חדש — לוודא שיש UI שקורא לו באותו commit. לא להוסיף endpoint בלי caller.

### Templates — תצוגת תאריך/שעה
- ערכי datetime מה-DB (פורמט UTC `YYYY-MM-DD HH:MM:SS`) **חייבים** לעבור דרך פילטר Jinja לפני הצגה למשתמש. אסור `{{ value }}` חשוף.
- תאריך+שעה: `{{ value | il_datetime }}` ⇒ `DD/MM/YYYY HH:MM` (יום-חודש-שנה, בלי שניות, בשעון ישראל).
- תאריך בלבד: `{{ value | il_date }}` ⇒ `DD/MM/YYYY`.
- ערך שכבר בשעון ישראל (לא UTC): `{{ value | il_datetime_local }}`.
- הפילטרים רשומים ב-`admin/app.py` (חיפוש `il_datetime`).

### לוגיקת זמן — טבלת תרחישים
- לפני כתיבת לוגיקה שתלויה בזמן/תאריך — לכתוב טבלת תרחישים עם כל מקרי הקצה (שעות לילה, מעבר יום, ערבי חג על ימים סגורים, גבולות שנה).

### Exceptions — תמיד לרשום ללוג
- `except Exception: pass` אסור. תמיד `logger.error(...)` כדי שבאגים לא ייעלמו בשקט.

### Handlers — דקורטורים על כל handler
- כל handler חדש (command, message, callback) חייב לעבור דרך `@rate_limit_guard` ו-`@live_chat_guard`. בלי `@live_chat_guard` — ה-handler יגיב ישירות למשתמש במהלך live chat ויפר את הזרימה.

### לולאות I/O ארוכות — עמידות בפני כשלים
- בלולאה שמבצעת I/O (רשת, DB) על רשימת פריטים: לעטוף **כל** קריאת I/O בתוך הלולאה ב-`try/except` עם לוג. כשל בפריט אחד לא צריך לעצור את עיבוד שאר הפריטים. דוגמה: `broadcast_service.py` — כשל DB בהודעה 10 לא עוצר 990 הודעות שנותרו.

### asyncio — ניהול lifecycle ו-futures
- **Bot standalone**: `Bot(token=...)` שנוצר מחוץ ל-`Application` דורש `await bot.initialize()` לפני שימוש ו-`await bot.shutdown()` בסיום (python-telegram-bot v20+).
- **Futures**: כש-`run_coroutine_threadsafe` מחזיר `Future` — לא לזרוק אותו. להוסיף `add_done_callback` שמטפל בכשלון. לבדוק `future.cancelled()` **לפני** `future.exception()`.
- **Cleanup ב-finally**: אם `shutdown()` / `close()` יכול להיכשל — לעטוף ב-`try/except` נפרד כדי שלא ידרוס את התוצאה של הפעולה העיקרית (למשל סטטוס `completed` שכבר נכתב ל-DB).

### DB — לא לדרוס התקדמות ב-error paths
- כשפונקציית כישלון (כמו `fail_broadcast`) נקראת ב-error handler — לא לדרוס מונים (sent/failed) עם 0 אם כבר נכתבה התקדמות ל-DB. לתמוך בקריאה ללא מונים שמעדכנת רק סטטוס.

### DB — למנוע כפילות לוגיקה בשאילתות
- כשיש שתי פונקציות שחולקות לוגיקת סינון (למשל `get_X` ו-`count_X`) — לחלץ helper פנימי משותף. שכפול WHERE/JOIN בין פונקציות מזמין סטייה שקטה כשמעדכנים רק אחת מהן.

### Handlers — צינור RAG אחד בלבד
- כל נתיב שמפעיל את צינור ה-RAG (כולל callback queries) חייב לעבור דרך `_handle_rag_query` ולא לשכפל את הלוגיקה. לצורך callbacks בלי `update.message` — להעביר `chat_id`.

### Handlers — rate limit על כל קריאת LLM
- כל נתיב שמגיע ל-LLM (הודעות, callbacks, שאלות המשך) חייב לעבור בדיקת `check_rate_limit` + `record_message`. ללא זה משתמש יכול לעקוף את מגבלות הקצב.

### Handlers — שימוש ב-helpers קיימים
- לחילוץ פרטי משתמש — `_get_user_info(update)`. לא לשכפל את הלוגיקה ידנית.

### WhatsApp — BSUID (Business-Scoped User ID)
- מאז אפריל 2026 (Meta) / מאי 2026 (Twilio), כל הודעת WhatsApp נכנסת כוללת BSUID בפורמט `CC.alphanumeric` (לדוגמה `IL.abc123XYZ`). Twilio חושף אותו ב-`ExternalUserId`, ואת ה-Parent BSUID (אם קיים) ב-`ExternalParentUserId`.
- כל נתיב שמקבל מזהה משתמש מ-WhatsApp webhook חייב לעבור דרך `utils/user_identity.resolve_whatsapp_user()`. לא לבנות `user_id` ידנית.
- `user_id` יכול להיות מספר טלפון (`+972...`) או BSUID (`IL.abc...`). אסור להניח regex של טלפון — להשתמש ב-`messaging.whatsapp_sender._is_phone_number` להבחנה.
- בשליחה יוצאת — `get_whatsapp_send_address()` מחזיר טלפון כשיש (המלצת Meta להעדיף טלפון). אחרת — שולחים ישירות ל-BSUID כ-`to=whatsapp:IL.abc...` (נתמך ב-Twilio).
- **אסור reverse-lookup** מ-BSUID לטלפון — אין API כזה ב-Meta. הטלפון נשמר רק כשמגיע ב-webhook עצמו.
- `whatsapp_parent_bsuid` נשמר ב-`user_identities` ל-forward-compat (Meta-managed portfolios). הוא **משותף בין משתמשים** ולכן לעולם לא משמש כ-`user_id` ולא קיים עליו UNIQUE.
- **WhatsApp עובר רק דרך Twilio** בפרויקט. `messaging/meta_webhook.py` מטפל ב-Messenger/Instagram DM ולא ב-WhatsApp Cloud API.

### WhatsApp — תקרת אורך הודעה (1600 תווים)
- Twilio קוצץ הודעות WhatsApp שעוברות 1600 תווים **בשקט**, באמצע משפט. זו אחת הבעיות שחזרו פעמיים בפרויקט הזה.
- כל יציאה ב-WhatsApp **חייבת** לעבור דרך `_send_whatsapp_response` (ב-`messaging/whatsapp_webhook.py`). הוא בודק `len(text) > WHATSAPP_MAX_LENGTH` ומעביר אוטומטית למסלול עמוד HTML ציבורי (`/p/<page_id>`) במקום לסכן קציצה.
- **אסור לקרוא ישירות ל-`send_whatsapp` מ-`messaging/whatsapp_sender.py`** מ-handler חיצוני (booking, follow-up, agent וכו'). תמיד דרך `_send_whatsapp_response` כדי שהצ'ק יקרה.
- הצ'ק הוא safety net מרכזי. ייתכנו צ'קים מוקדמים יותר עם הקשר נוסף (intent + rag_context) שעוברים ישירות ל-`_send_as_page` עם פרמטרים מועשרים — זה תוספת, לא תחליף.
- אין recursion: `_send_as_page` שולח קישור קצר חזרה דרך `_send_whatsapp_response`, אבל הקישור קצר מהסף ועובר ישירות.
- מנגנון העמודים תלוי ב-`ADMIN_URL`. בלעדיו אין לאן להפנות; הקוד נופל לשליחה רגילה (Twilio יקצוץ אבל לפחות מתעד).

### LLM — JSON output חייב מבנה מפורש בפרומפט
- כשמשתמשים ב-`response_format={"type": "json_object"}` בלבד, ה-LLM יוצר JSON עם שדות *משלו* (לא תואמים לסכמה). הקוד עושה `dict.get("field", default)` ומקבל ברירת מחדל ⇒ באג שקט.
- חובה לכלול בפרומפט: (1) **רשימת השדות הנדרשים בשמותיהם המדויקים**, (2) **לפחות דוגמה אחת** של JSON תקין מלא. LLMים עוקבים אחרי דוגמאות קונקרטיות יותר טוב מתיאורים מילוליים.
- אם מוסיפים שדה לסכמה — לעדכן גם את הפרומפט (השמות + הדוגמה). אם מוסיפים הוראת התנהגות לפרומפט שמזכירה שדה — לוודא שהוא בסכמה. סטיות יוצרות JSON עם שדות שלא בקוד.
- ראה: `followup_config.py:FOLLOWUP_DECISION_PROMPT` ו-`tests/test_followup_service.py:TestFollowupDecisionPrompt` — טסטים שאוכפים את ההתאמה.

### תיקון 13 — אין כתיבת PII לפני הסכמה
- כל handler שמעבד PII (booking, talk_to_agent, referral, subscribe) חייב `@consent_guard` (או `@consent_guard_booking` ל-ConversationHandler entry points).
- **סדר ה-decorators**: `block → rate_limit → vacation → live_chat → consent`. consent חייב להיות **אחרי** rate_limit (אחרת אפשר לשפם את מסך ההסכמה) **ואחרי** live_chat (אחרת מסך הסכמה פורץ שיחה חיה).
- ב-`start_command` וב-`message_handler` ה-consent check ידני (לא דקורטור) כי צריך לעבד deep-link args לפני החסימה. שם, REF_ נשמר ב-`context.user_data["pending_referral_code"]` ועובר ל-`consent_callback`.
- `db.upsert_user` ו-`db.ensure_user_subscribed` חייבים להיקרא **רק אחרי** `db.has_consent(user_id)`. אסור לכתוב שורת users / לרשום לשידורים לפני שהמשתמש אישר.
- כשמוסיפים טבלה חדשה עם `user_id` — חובה לעדכן את `delete_user_data` ב-`database.py` (זכות מחיקה).
- **כשמוסיפים טבלה חדשה (כל טבלה, לא רק עם `user_id`)** — חובה להוסיף שורה ב-`docs/privacy_data_matrix.md` באותו commit. המטריצה היא המקור היחיד שמיפוי מלא של מה כל טבלה מכילה ואיך מתייחסים אליה (export / delete / retention / sens_risk). הוספת טבלה בלי שורה מנתקת את התיעוד מהקוד והופכת את הציות ל-best-effort. אם הטבלה היא config / ידע עסקי בלי PII של משתמש קצה — לפחות שורה קצרה בחלק "טבלאות ללא PII" עם שיקול אבטחה (האם תוכן עובר ל-LLM? האם יש סודות?).

### URL params — `+` מתפרש כ-space
- ב-application/x-www-form-urlencoded, התו `+` הוא קוד ל-space. כש-user_id של WhatsApp `+972...` נכנס ל-URL בלי `urlencode`, הוא חוזר כ-` 972...` (עם רווח מוביל) ולא תואם ל-DB.
- **בכל לינק** (path או query) ל-route שמקבל user_id — להשתמש ב-`{{ user_id|urlencode }}` ב-Jinja.
- ב-routes שמקבלים user_id — להריץ דרך `_normalize_user_id` (`admin/app.py`) שמטפל ב-` 972...` / `972...` / `+972...` ומחזיר תמיד `+972...`.

### Handoff — מנגנון `[HANDOFF]` token (לא fuzzy matching)
- אם ה-LLM רוצה להעביר לבעל העסק, הוא **חייב** לפתוח את התשובה ב-`HANDOFF_MARKER = "[HANDOFF]"` (`config.py`). הפרסר ב-`core/message_processor.py:should_handoff_to_human` בודק `startswith` בלבד — בלי fuzzy matching.
- `strip_handoff_marker` מסיר את הטוקן לפני שליחה ללקוח. **כל קורא** ל-`generate_answer` שמחזיר ללקוח (process_rag_query, _booking_start_core, whatsapp_booking) חייב להפעיל את הפרסר ולהסיר את הטוקן.
- אסור לחזור ל-fuzzy detection (חיפוש "אעביר את הפנייה" וכו') — זה מייצר false positives שחוסמים תשובות תמימות.

### צ'ק ליסט הקלטת לקוח — לעדכן בכל שינוי רלוונטי
- המסמך `docs/client_checklist.md` מתאר את תהליך ההקלטה ללקוח חדש.
- בכל שינוי ב-`seed_data.py` (קטגוריות, שדות, מבנה), `config.py` (משתני סביבה, system prompt), `.env.example`, או פיצ'רים בבוט/אדמין — **יש לעדכן גם את הצ'ק ליסט** כדי שישקף את המצב הנוכחי של הקוד.

### טסטים — כיסוי ותחזוקה
- **הרצה:** `python -m pytest tests/ -v`
- **מבנה:** קובץ טסט לכל מודול — `tests/test_<module>.py`. fixtures משותפים ב-`tests/conftest.py`.
- **DB בטסטים:** כל טסט מקבל DB זמני נפרד (tmp_path). לעולם לא לגעת ב-DB אמיתי. השתמש ב-`db_conn` fixture מ-`tests/conftest.py` (מגדיר גם `SECRETS_ENCRYPTION_KEY` אוטומטית, כך שטסטים על שדות מוצפנים עובדים). **אל תקרא ל-`importlib.reload()` על `database`/`config`** — זה שובר את ה-binding של `cryptography` (C extension) ויפיל כל טסט שתלוי ב-`init_db` שמייבא `utils/crypto`. הדפוס הנכון: `with patch("ai_chatbot.config.DB_PATH", ...)` ו-`from database import init_db` בלי reload.
- **תלויות חיצוניות:** מודולים שתלויים ב-telegram / OpenAI — mock לפני ייבוא. לא לקרוא ל-API בטסטים.
- **כשמוסיפים לוגיקה חדשה:** להוסיף טסט באותו commit. עדיפות למודולים עם לוגיקה טהורה (intent, chunker, rate_limiter, business_hours).

## דפוסים קריטיים — security / privacy / data-loss (חל תמיד)

1. **OAuth password takeover.** endpoints של "set password" / "register" / "link account" שמקבלים email חייבים לדחות את הבקשה אם חשבון קיים דרך OAuth, אלא אם מאומת כאותו משתמש או הוכח דרך קישור one-time מאומת שנשלח ל-email.

2. **XFF spoofing ב-rate limiter.** לעולם אל תקרא `X-Forwarded-For` ישירות להחלטות security. הגדר middleware של trusted-proxy (`ProxyHeadersMiddleware`, `app.set('trust proxy', N)`) — ואז קרא `request.client.host`.

3. **Auto-admin לפי email לא מאומת.** הענקת תפקיד admin / staff / owner קורית רק אחרי email verification, או דרך קישור invite + token מ-admin קיים. לעולם אל תסמוך על `request.body.email == OWNER_EMAIL`.

4. **XSS via innerHTML.** ברירת מחדל ל-`textContent` / טקסט ב-JSX. `innerHTML` / `dangerouslySetInnerHTML` / `v-html` עם ערכים ממקור חיצוני דורש `DOMPurify` (או שווה ערך) באותה שורה של ההשמה. "Admin only" אינה הגנה.

5. **פאנל admin חשוף לרשת.** שרת שנקשר ל-`0.0.0.0` דורש middleware של אימות *לפני* שכל route רץ, או firewall. אחרת קשור `127.0.0.1`.

6. **Secret ב-response.** Serialization של משתמש עובר דרך DTO / response model מפורש. שמות שדות אסורים בכל מקום ב-response של API: `password`, `passwordHash`, `salt`, `refresh_token`, `access_token`, `api_key`, `secret`. הודעות exception בגבולות API לא יכולות לכלול IDs פנימיים / סיבות heuristic.

7. **PII בלוגים.** אסור ב-`logger.*` וב-`HTTPException.detail`: `email`, `phone`, `from_email`, `to_email`, שמות, body של הודעות, tokens, API keys. החלף ב-domain של email בלבד, hash, או user_id משלך. הודעת שגיאה מתורגמת גנרית לפרטים שמופיעים למשתמש.

8. **LIKE wildcard injection.** prefix של משתמש ב-`LIKE` חייב לברוח מ-`_` ו-`%`, או להשתמש ב-`.startswith(value, autoescape=True)`, או `=` מדויק.

9. **שליחת credential לפני storage.** התמד ב-OTP / token / link (Redis SET / DB INSERT) *לפני* השליחה. אם ההתמדה נכשלת, אל תשלח. אין נתיב fallback "דלג על verification" בכשל storage — fail closed.

10. **500 ≠ "invalid credentials".** טיפול בשגיאת login מסתעף: 401/403 → "Invalid credentials"; 5xx → "Service unavailable, try again"; 429 → "Too many attempts". לעולם אל תאחד 5xx ל-auth error.

ראה `CRITICAL-PATTERNS.md` להגיון מלא וכללי זיהוי.

## דפוסים אוניברסליים (חל על כל פרויקט)

1. **Async race / TOCTOU.** read-then-write דורש UNIQUE constraint, advisory lock, או CAS. `INSERT` מקומי **לפני** כל קריאה חיצונית בלתי הפיכה. כל `async def` שנקראת בהקשר בוליאני (`if foo():`) חייבת `await`. CAS על עמודה nullable: הסתעפות `is None` → השתמש ב-`IS NULL`.

2. **סנכרון state ב-React.** `useState(props.X)` מקומי ישן כש-`props.X` משתנה — השתמש ב-`key={id}`, `useEffect([X], () => setState(X))`, או derived state. כל ה-hooks לפני כל early return. deps של `useCallback`/`useMemo` חייבים לכלול כל prop/state בגוף. `setState` אחרי `await` בודק cancellation flag.

3. **ולידציה של external input.** לפני `.get()` / `.append()` / `.strip()` / iteration על נתון חיצוני: `isinstance(...)` guard. מספרים: `isfinite()` + טווח. regex על טקסט חיצוני: raw strings + word boundaries; עדיף `json.JSONDecoder().raw_decode()` על regex. קריאות SDK: תפוס base class (`anthropic.APIError`), סדר subclass-לפני-superclass. אתחול SDK ב-startup: try/except + ולידציית פורמט — הורד את הפיצ'ר, לעולם אל תקרוס את ה-boot.

4. **SQL/Postgres edges.** `col = NULL` הוא NULL, לא TRUE — הסתעפות `IS NULL`. `VARCHAR(N)` ≥ ערך enum הארוך ביותר (טסט CI). Telegram / IDs חיצוניים: `BigInteger`. כל `ORDER BY` משולב עם `LIMIT`/`OFFSET` דורש tiebreaker `, id`. `LIKE` עם קלט משתמש: `.startswith(value, autoescape=True)` או escape ל-`_`/`%`.

5. **atomicity של linked-field.** שינויי status מעדכנים את כל השדות המקושרים (`status` + `last_outbound_at` + `last_activity_type` + סגירת task קשור + activity-log) בטרנזקציה אחת. קריאה חיצונית: כתוב שורה מקומית עם `UNIQUE` קודם; או כתוב audit log עם `metadata.applied=false` בכשל. Token pairs (access + refresh) נשמרים/מתעדכנים יחד.

6. **סטיית migration.** כל `Index`/`CheckConstraint`/`UniqueConstraint` ב-migration חייב לשקף ב-`__table_args__` של המודל. revision id של Alembic ≤ 30 chars. פרויקטי MySQL: אסור `ADD COLUMN IF NOT EXISTS`. `DROP COLUMN` הוא migration נפרד אחרי שהקוד הפסיק להשתמש בשדה.

ראה `BY-STACK/*.md` לדוגמאות קוד ו-`bugbot-rules/*.md` ל-prompts של כללים עצמאיים.

## פקודות

```bash
# הרצת הפרויקט (בוט + אדמין)
python main.py

# בוט בלבד
python main.py --bot

# אדמין בלבד
python main.py --admin

# Seed data
python main.py --seed

# טסטים
python -m pytest tests/ -v
```
