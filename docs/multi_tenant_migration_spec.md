# מסמך תכנון: הגירה מ-Repo-per-Client ל-Multi-Tenant

> **סטטוס:** מסמך תכנון (spec) בלבד — אין במסמך זה מימוש. נכתב על סמך חקירת עומק של הקוד (יולי 2026).
> **מטרה:** מפה מלאה למעבר מ"ריפו + Render instance + SQLite לכל לקוח" לפלטפורמה אחת מרובת-לקוחות, כולל פתרון שורש לבעיית Google OAuth.

---

## תוכן עניינים

1. [תקציר מנהלים](#0-תקציר-מנהלים)
2. [ממצאי החקירה — המצב בפועל](#1-ממצאי-החקירה--המצב-בפועל)
3. [הכרעת מסלול ה-DB: ניתוח והמלצה](#2-הכרעת-מסלול-ה-db-ניתוח-והמלצה)
4. [ארכיטקטורת Google OAuth מרכזית](#3-ארכיטקטורת-google-oauth-מרכזית)
5. [ניהול סודות מוצפן פר-tenant](#4-ניהול-סודות-מוצפן-פר-tenant)
6. [Tenant Isolation](#5-tenant-isolation)
7. [ראוטינג הודעות נכנסות](#6-ראוטינג-הודעות-נכנסות)
8. [תוכנית הגירה מדורגת](#7-תוכנית-הגירה-מדורגת)
9. [ניתוח סיכונים](#8-ניתוח-סיכונים)
10. [פתרון ביניים לניתוק כל 7 ימים](#9-פתרון-ביניים-לניתוק-כל-7-ימים--אפשר-לבצע-כבר-השבוע)
11. [שאלות פתוחות והחלטות שלך](#10-שאלות-פתוחות-והחלטות-שלך)

---

## 0. תקציר מנהלים

**ההמלצה: מסלול ב' — database-per-tenant על SQLite, בתוספת "control plane" קטן ומרכזי.** לא Postgres, לפחות לא עכשיו. הנימוק המלא בפרק 2, אבל בקצרה: שכבת ה-DB היא ‎7,400 שורות של SQL גולמי (290 פונקציות, 335 קריאות `execute`) שאף אחת מהן לא מכירה tenant, עם 5 טבלאות singleton (`CHECK(id = 1)`) ו-idioms של SQLite בכל מקום. במסלול Postgres+tenant_id כמעט כל פונקציה נוגעת בשינוי + נדרש ETL של כל לקוח קיים + ניתוח מחדש של כל constraint. במסלול SQLite-פר-tenant, נקודת השינוי המרכזית היא פונקציה אחת (`get_connection`), הגירת לקוח קיים היא **העתקת קובץ**, והבידוד הפיזי — הנכס הכי טוב של הארכיטקטורה הנוכחית — נשמר.

**תובנת השורש המרכזית מהחקירה:** המחלה האמיתית היא לא SQLite ולא Google — היא ש**זהות העסק חיה בתשתית (env vars, ריפו, אינסטנס) במקום בנתונים**. העבודה הגדולה באמת — בניית "tenant context" שזורם מכל נקודת כניסה עד כל שאילתה, העברת קונפיג עסקי מ-env ל-DB, ומיפתוח כל ה-state שבזיכרון לפי tenant — **זהה בשני המסלולים**. לכן הבחירה במסלול ב' לא "שורפת" עבודה: אם בעוד שנתיים תגיע לסקייל שמצדיק Postgres, כל עבודת ה-tenant context תשרת אותך כמו שהיא.

**ממצא שמשנה את תמונת ה-OAuth:** אימות דומיין מול Google הוא היום **בלתי אפשרי עקרונית** בארכיטקטורה הנוכחית — הפאנלים יושבים על `*.onrender.com`, דומיין של Render שאתה לא יכול לאמת ב-Search Console. כלומר verification מלא של אפליקציית Google מחייב ממילא דומיין בבעלותך — עוד טיעון לארכיטקטורת הדומיין המרכזי.

**פתרון ביניים זמין מיד** (פרק 9): איחוד כל הלקוחות לפרויקט Google Cloud אחד + העברת ה-consent screen ל-In Production (גם בלי verification). זה מעלים את תפוגת 7 הימים באותו יום, במחיר מסך אזהרה חד-פעמי ותקרת 100 משתמשים — סביר לחלוטין בסקייל הנוכחי.

**סדר מוצע:** פתרון ביניים (שבוע זה) → שלב 1: יסודות בקוד הקיים בלי שינוי התנהגות (ימים בודדים) → שלב 2: בניית הפלטפורמה (הליבה, 2–4 שבועות) → פיילוט על tenant חדש → הגירת לקוחות קיימים בגלים (העתקת קבצים + החלפת webhooks, ~שעה ללקוח).

---

## 1. ממצאי החקירה — המצב בפועל

### 1.1 מודל התהליך והפריסה

- **תהליך אחד לכל לקוח** על Render web service (plan: starter), עם דיסק מתמיד ‎1GB ב-`/var/data` שמחזיק את SQLite ואת אינדקס ה-FAISS (`render.yaml:16-20`). נקודת הכניסה: `python -m main`.
- **שתי טופולוגיות ריצה** (`main.py:143`): במצב webhook — Flask ב-main thread ולולאת asyncio של הבוט ב-daemon thread (`main.py:176-188`); במצב polling — הפוך. הגשר היחיד בין ה-threads: `asyncio.run_coroutine_threadsafe` (`admin/app.py:5914`, `broadcast_service.py:281`).
- ⚠️ **הפרודקשן רץ על שרת הפיתוח של Flask** — `flask_app.run(...)` ב-`main.py:56`. gunicorn נמצא ב-`requirements.txt` וקיים `ai_chatbot/admin/wsgi.py`, אבל `render.yaml` לא משתמש בהם. אין `ProxyFix` (יש עקיפה ידנית ל-`X-Forwarded-Proto` רק באימות חתימת Twilio, `messaging/whatsapp_webhook.py:59-60`).
- ⚠️ **צינור ה-RAG+LLM של WhatsApp/Meta רץ סינכרונית בתוך thread הבקשה של Flask** — `process_incoming_message` נקרא ישירות מה-webhook (`messaging/whatsapp_webhook.py:609`, `messaging/meta_webhook.py:306`). בעומס multi-tenant זה צוואר בקבוק מובנה.
- **Jobs מתוזמנים בשני מנגנונים:** JobQueue של python-telegram-bot (תזכורות תורים, follow-up לידים, purge יומי, ניקוי live chat — `bot/telegram_bot.py:86-171`) + שני threads עצמאיים (broadcast scheduler ו-memory extraction — `main.py:158-169`). כולם עובדים מול DB יחיד ו-`BUSINESS_ID` יחיד.
- **אין שום מנגנון גיבוי בקוד** — אין route להורדת DB, אין dump מתוזמן. העמידות = הדיסק של Render בלבד.

### 1.2 שכבת ה-DB

- **SQLite גולמי, בלי ORM.** `database.py` — ‎7,409 שורות, ‎290 פונקציות, ‎335 קריאות `execute`, הכול פרמטריזציה תקינה עם `?`.
- **נקודת חנק אחת ויחידה:** `get_connection()` (`database.py:20-40`) — context manager שפותח חיבור חדש לכל פעולה מ-`DB_PATH` יחיד, עם WAL, `busy_timeout=30000`, `check_same_thread=False`. **אין** חיבור גלובלי ואין thread-local. זו נקודת ההזרקה הטבעית של ניתוב פר-tenant.
- **~40 טבלאות.** רק שלוש (`customer_facts`, `business_profile`, `extraction_runs` — תת-מערכת הזיכרון) נושאות `business_id`. כל השאר — בלי מימד tenant.
- **חמש טבלאות singleton קשיח** `id INTEGER PRIMARY KEY CHECK(id = 1)`: ‏`bot_settings`, `vacation_mode`, `google_calendar_credentials`, `business_branding`, `subscription`. בנוסף `business_profile` עם שורת `'default'` יחידה.
- **user_id הוא TEXT תלוי-ערוץ** (chat_id טלגרם / טלפון / BSUID / `meta_ig:*` / `meta_msg:*`) ומשמש PK ב-`users` ובעוד ~17 טבלאות. **אותו אדם שפונה לשני עסקים = אותו user_id** — התנגשות מובטחת ב-DB משותף.
- **Idioms של SQLite שיקשו על פורט ל-Postgres:** ‏130 מופעי `datetime('now')`‏, 34 `AUTOINCREMENT`‏, 14 `cursor.lastrowid`‏, `INSERT OR IGNORE/REPLACE`‏, `executescript` לכל הסכימה, `LIKE` לא-רגיש-רישיות, שני BLOBs (`kb_chunks.embedding`, `business_branding.logo_blob`). מנגד — אין JSON1, אין FTS, אין triggers, וה-upserts כבר בתחביר `ON CONFLICT` תואם-PG.
- **מנגנון המיגרציות הוא idempotent-by-inspection בלי version ledger** — `run_migrations` רץ בכל עליית תהליך מתוך `init_db` (`database.py:649-650`), עם `_ensure_column` ו-rebuilds בסגנון SQLite (`PRAGMA foreign_keys=OFF` + RENAME+COPY). התכונה הזו דווקא **נכס** במסלול ב': כל קובץ tenant "מתעדכן מעצמו" בפתיחה.
- **RAG בשלוש שכבות:** embeddings ב-BLOB ב-`kb_chunks` (מקור אמת), אינדקס FAISS על דיסק (`FAISS_INDEX_PATH`), ו-singleton בזיכרון `_store` (`rag/vector_store.py:188`) + query cache גלובלי שהמפתח שלו הוא `(query, top_k)` **בלי tenant** (`rag/engine.py:43`).

### 1.3 ניהול סודות היום

| סוד | איפה חי היום | סיווג ב-multi-tenant |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET`, `TELEGRAM_OWNER_CHAT_ID` | env (`config.py:37-45`) | **פר-tenant** |
| `TWILIO_ACCOUNT_SID/AUTH_TOKEN/WHATSAPP_NUMBER` | env, וניתן לעדכון מהפאנל | **פר-tenant** (או subaccounts) |
| `OPENAI_API_KEY` (+`MEMORY_OPENAI_API_KEY`) | env | פלטפורמה (עם אופציה לפר-tenant בהמשך) |
| `GOOGLE_CLIENT_ID/SECRET` | env (`config.py:243-244`) | **פלטפורמה** |
| טוקני Google (refresh/access) | **DB**, טבלת singleton, **מוצפנים Fernet** (`database.py:3338-3400`) | פר-tenant (כבר בנוי) |
| `META_APP_ID/SECRET/VERIFY_TOKEN` | env — **כבר משותף לכל הפריסות** (`config.py:198-205`) | **פלטפורמה** |
| Page tokens של Meta | **DB**, `meta_credentials` לפי `page_id`, **מוצפנים** (`database.py:3487-3559`) | פר-tenant (כבר בנוי) |
| `ADMIN_PASSWORD(_HASH)`, `ADMIN_SECRET_KEY` | env | פר-tenant (משתמש) / פלטפורמה (session key) |
| `SECRETS_ENCRYPTION_KEY`, `LEDGER_PEPPER_V1` | env | פלטפורמה |
| VAPID, SMTP, Sentry, `DEVELOPER_*` | env | פלטפורמה |

- **תשתית הצפנה כבר קיימת:** `utils/crypto.py` — Fernet עם ciphertext מגורסן `v1:<b64>` שמאפשר רוטציית מפתחות עתידית, ומיגרציית backfill שהצפינה טוקנים ישנים (`migrations.py:761-796`).
- ⚠️ **ההצפנה היא opt-in:** בלי `SECRETS_ENCRYPTION_KEY` הקוד שומר **plaintext** עם אזהרה בלבד (`utils/crypto.py:107-119`). בפלטפורמה זה חייב להפוך ל-fail-closed.
- ⚠️ **"הגדרות תשתית" בפאנל שומרות סודות בכתיבה ל-`.env` שעל הדיסק** (plaintext) + monkey-patching של `os.environ` ואטריביוטים של מודול הקונפיג בזמן ריצה (`admin/app.py:3060-3196`). זה עובד רק כי יש תהליך אחד ועסק אחד — ובפלטפורמה חייב להתחלף באחסון המוצפן.

### 1.4 Google Calendar OAuth היום

- **ספרייה ו-scope:** `google-auth-oauthlib` עם PKCE; scope יחיד — `https://www.googleapis.com/auth/calendar` (קריאה+כתיבה, `google_calendar.py:35`).
- **Flow:** ‏`GET /google/connect` (עם `state` CSRF ב-session) → Google → ‏`GET /google/callback` → החלפת code, קריאת calendar ‏`primary` לצורך email+timezone, ושמירה (`admin/app.py:2519-2574`, `google_calendar.py:104-167`).
- **קונפיג מ-env:** ‏`GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI` — ה-redirect הוא env ייעודי פר-פריסה, לא נגזר מ-`ADMIN_URL`.
- **אחסון:** טבלת singleton ‏`google_calendar_credentials` (‏`CHECK(id=1)`) עם `refresh_token`/`access_token` מוצפנים, `token_expiry`, `timezone`, ודגלי בריאות `auth_invalid_at`/`owner_alert_sent_at`.
- **רענון:** lazy — בכל פעולת יומן, אם פג ה-access token הספרייה מרעננת וכותבת חזרה (`google_calendar.py:179-250`). אין רענון יזום ברקע. על `RefreshError` (זה מה שקורה כל 7 ימים במצב Testing) — סימון `auth_invalid` + התראה חד-פעמית לבעל העסק בטלגרם/וואטסאפ.
- **שימושי API בפועל:** ‏`freebusy().query`‏, `events().insert`‏, `events().delete`‏, `calendars().get('primary')`. תמיד מול היומן `primary` (hardcoded).

### 1.5 חיבורי הערוצים ונקודות הכניסה

| ערוץ | נקודת כניסה | איך מזוהה העסק היום | אימות |
|---|---|---|---|
| Telegram | polling, או ‏`POST /telegram/webhook` (`admin/app.py:5890`) | **משתמע** — טוקן הבוט של התהליך | `X-Telegram-Bot-Api-Secret-Token` מול `WEBHOOK_SECRET` |
| WhatsApp (Twilio) | ‏`POST /webhook/whatsapp` (`messaging/whatsapp_webhook.py:221`) | **משתמע** — ⚠️ שדה `To` בכלל לא נקרא | חתימת Twilio מול ה-`TWILIO_AUTH_TOKEN` הגלובלי |
| Messenger/Instagram | ‏`POST /webhooks/meta` (`messaging/meta_webhook.py:171`) | **כמעט-tenant**: ‏`entry.id` נבדק מול `meta_credentials` המקומית (`db.is_meta_entry_known`) | HMAC ‏`X-Hub-Signature-256` מול `META_APP_SECRET` המשותף |
| Widget אתר | ‏`POST /widget/api/chat` (`admin/widget.py:406`) | **משתמע** — אין מפתח tenant ב-snippet | CORS allowlist + rate limit פר-IP בזיכרון |
| פאנל אדמין | Flask session (`session["logged_in"]=True`) | משתמע — זהות בוליאנית, בלי מזהה משתמש/עסק (`admin/app.py:836-846`) | סיסמה יחידה מ-env |

- **URLs ציבוריים** — הכול נבנה מ-`ADMIN_URL` יחיד: עמודי `/p/<page_id>` לתשובות ארוכות, `/ics/<page_id>`, ‏`/widget/embed.js`, לינקים עמוקים לפאנל בהתראות. אף path לא נושא מזהה tenant.

### 1.6 קטלוג הנחות single-tenant (מה יישבר בתהליך משותף)

**זהות עסקית קפואה ב-import:** ‏`BUSINESS_NAME/PHONE/ADDRESS/WEBSITE` (‏`config.py:178-181`) מיובאים by-value ל-~15 מודולים (`llm.py`, `bot/handlers.py`, `ics_service.py`, `appointment_notifications.py`, `messaging/whatsapp_webhook.py` ועוד) ומוזרקים ל-~50 `render_template` בפאנל. ‏`build_system_prompt` קורא את `BUSINESS_NAME` מהגלובל — **שם העסק אינו פרמטר** של בניית הפרומפט (`config.py:508-626`).

**State ברמת מודול שיהפוך ל-state חוצה-לקוחות:**

| קטגוריה | דוגמאות (file:line) | סיכון |
|---|---|---|
| דליפת מידע ישירה | ‏FAISS ‏`_store` יחיד (`rag/vector_store.py:188`); query cache בלי מפתח tenant (`rag/engine.py:43`); cache חופשה (`vacation_service.py:29`) | **קריטי** — תשובות של עסק א' יוגשו לעסק ב' |
| מפתוח לפי user_id בלבד | rate limiter (`rate_limiter.py:37`); מכונת מצבים של booking ‏(`messaging/conversation_state.py:25`); follow-up store; pending-deletes; מנעולי סיכום (`llm.py:36`) | אותו אדם מול שני עסקים = התנגשות |
| Singletons של חשבון | ‏`_twilio_client` (`messaging/whatsapp_sender.py:17`); ‏`bot_state._bot/_loop`; клиенты OpenAI | חשבון אחד לכל התהליך |
| מוני אבטחה פר-IP בזיכרון | ‏`_login_attempts` וכו' (`admin/app.py:443-470`); widget rate limit (`admin/widget.py:52` — הקוד עצמו מעיר "ל-multi-client חייבים Redis") | תקרות משותפות, איפוס ב-restart |

**כפילות namespace של `ai_chatbot/`:** ‏`database.py:17` עושה `from ai_chatbot.config import DB_PATH` בעוד מודולים אחרים מייבאים `config`/`from config import X` — **שני אובייקטי מודול חיים לאותו קובץ**, עם עותקי ערכים נפרדים. ה-conftest כבר נאלץ לעשות double-patching בגלל זה (`tests/conftest.py:102-122`). אי-אפשר להזריק קונפיג פר-tenant בצורה אמינה לפני שמנטרלים את הכפילות.

**Seed ופלאגים:** ‏`seed_data.py` מניח KB ריק אחד לקובץ; החבילה (`subscription`) והפיצ'רים הם שורת singleton; ‏`main.py:125-130` עושה auto-seed לפי ספירה גלובלית.

### 1.7 ממצאים מפתיעים / משני-תמונה

1. **ההכנה ל-multi-tenant כבר התחילה בקוד:** ‏`config.py:74-76` מגדיר `BUSINESS_ID` עם הערה מפורשת "forward-compat ל-multi-tenant", ותת-מערכת הזיכרון (`customer_facts`, `business_profile`, `extraction_runs`) כבר בנויה עם `business_id` בסכימה ובשאילתות. הדפוס קיים — צריך להשלים אותו.
2. **Meta היא אב-טיפוס עובד של ארכיטקטורת היעד:** אפליקציית Meta **אחת משותפת לכל הפריסות** (`config.py:198-202`), webhook אחד, וניתוב לפי `entry.id` מול טבלת credentials מוצפנת. זה בדיוק המודל שצריך להעתיק ל-Google ולערוצים האחרים.
3. **אימות דומיין ל-Google בלתי אפשרי היום בכל מקרה:** הדומיינים הם `*.onrender.com` — שייכים ל-Render, לא לך, ולא ניתנים לאימות ב-Search Console. כלומר "לאמת דומיין לכל לקוח" לא היה קשה — הוא היה **חסום**. verification מחייב דומיין בבעלותך, וזה מתיישב רק עם דומיין מרכזי (או סאב-דומיינים שלו).
4. **שרת הפרודקשן הוא Flask dev server** בלי ProxyFix, וה-LLM רץ בתוך thread ה-webhook. עובד ללקוח אחד; בפלטפורמה זה חייב תיקון שורש (gunicorn + threads) — לא טלאי.
5. **אין גיבויים בכלל.** ההגירה היא הזדמנות, אבל זה פער שצריך לסגור עוד קודם.
6. **הפאנל כותב סודות ל-`.env` על הדיסק ועושה monkey-patching לקונפיג** (`admin/app.py:3060-3196`) — מנגנון שעובד רק בתהליך-יחיד ומדגים למה קונפיג חייב לעבור ל-DB.
7. **חתימת WhatsApp נכנסת מאומתת, אבל `To` לא נקרא** — אין שום עוגן בנתונים לזיהוי העסק בהודעת Twilio נכנסת. כדאי להתחיל לשמור אותו כבר עכשיו (עוזר גם לדיבוג וגם להגירה).

---

## 2. הכרעת מסלול ה-DB: ניתוח והמלצה

### 2.1 קודם — מה זהה בשני המסלולים (וזו העבודה האמיתית)

לפני השוואה, חשוב לקבע: **החלק הקשה של ההגירה אינו מנוע ה-DB.** בשני המסלולים חייבים:

1. **Tenant context** שנקבע בכל נקודת כניסה (webhook / session / job / CLI) וזורם עד שכבת ה-DB.
2. **קונפיג עסקי עובר מ-env ל-DB** — ‏`BUSINESS_NAME` וחבריו, טוקן הבוט, מספרי Twilio, `TELEGRAM_OWNER_CHAT_ID`.
3. **מיפתוח כל ה-state בזיכרון לפי tenant** — FAISS, caches, rate limiter, מכונות מצבים.
4. **ראוטינג ערוצים** — טוקן/מספר/page_id → tenant (פרק 6).
5. **OAuth מרכזי + סודות פר-tenant** (פרקים 3–4).
6. **Jobs שמאתרים על פני tenants** במקום לרוץ פעם אחת.
7. **איחוד ה-namespace הכפול** של `ai_chatbot/`.

ההבדל בין המסלולים מצטמצם לשאלה אחת: **מה עושים עם 335 השאילתות ו-40 הטבלאות.**

### 2.2 מסלול א' — Postgres משותף עם `tenant_id`

מה נדרש בפועל, על סמך הקוד:

- **ניתוח סכימה מחדש לכל טבלה:** ‏`users.user_id` הוא PK — חייב להפוך ל-`(tenant_id, user_id)`, וזה גורר את כל ~17 הטבלאות שמפנות ל-user_id, את כל ה-UNIQUE constraints (‏`referral_codes.user_id`, ‏`user_identities` partial uniques על phone/bsuid, ‏`idx_appointments_user_datetime`...) ואת חמש טבלאות ה-singleton שהופכות ל-`WHERE tenant_id = ?`.
- **נגיעה ב-~290 פונקציות** — או הוספת tenant_id לכל שאילתה, או Postgres RLS עם `SET app.tenant_id` על כל חיבור (RLS מקטין את השינוי בשאילתות אבל לא פוטר מפורט הדיאלקט ומניתוח הסכימה).
- **פורט דיאלקט:** ‏130 `datetime('now')`‏, 34 `AUTOINCREMENT`→IDENTITY‏, 14 `lastrowid`→`RETURNING`‏, `INSERT OR REPLACE/IGNORE`‏, `executescript`‏, רגישות-רישיות של `LIKE`‏, BLOB→BYTEA, והחלפת כל מנגנון המיגרציות (ה-rebuild-dance עם `PRAGMA foreign_keys` לא קיים ב-PG; צריך Alembic או שווה-ערך).
- **ETL להגירת כל לקוח קיים:** קריאת קובץ SQLite, רימאפינג מפתחות, טעינה ל-PG, ואימות — פר לקוח, עם חלון השבתה או סנכרון כפול.
- **RAG:** או pgvector (עוד פורט) או להשאיר FAISS פר-tenant על דיסק (ואז ממילא יש state פר-tenant על קבצים).
- **הבידוד הופך ללוגי:** שורה בלי סינון = דליפה. RLS מגן היטב — אבל רק אחרי שכל הפורט הושלם נכון.

**יתרונות אמיתיים של המסלול:** DB מנוהל (Supabase: גיבויים, PITR, dashboard), שאילתות רוחביות לכל הלקוחות (אנליטיקות, חיפוש תקלות), אפשרות ל-scale אופקי של האפליקציה (כמה אינסטנסים מול DB אחד), אין ניהול קבצים, כלים בוגרים (Alembic, pooling).

### 2.3 מסלול ב' — SQLite פר-tenant על שרת מרכזי אחד

- **שינוי הליבה:** ‏`get_connection()` קורא את ה-tenant מ-context ופותח `DATA_DIR/tenants/<tenant_id>/chatbot.db`. ‏290 הפונקציות, הסכימה, ה-constraints, ה-singletons (`CHECK(id=1)`) ומנגנון המיגרציות — **נשארים כמות שהם**, כי כל קובץ הוא עדיין "עולם של עסק אחד". ‏`run_migrations` האידמפוטנטי רץ פר-קובץ בפתיחה/onboarding — בדיוק כמו היום.
- **Control plane קטן ומרכזי** (קובץ `platform.db` נפרד או Postgres קטן): רשימת tenants, מפתחות ראוטינג (bot token → tenant, מספר Twilio → tenant, page_id → tenant, widget key → tenant), משתמשי אדמין, חבילות, וסודות פר-tenant מוצפנים. עשרות שורות, לא נתוני ריצה.
- **הגירת לקוח קיים = העתקת `chatbot.db` + תיקיית `faiss_index`** ורישום שורת tenant. אפס ETL, אפס סיכון המרה, אימות ב-checksum.
- **בידוד:** נשאר פיזי ברמת הקובץ. שאילתה בלי סינון tenant *לא קיימת כמושג* — החיבור רואה רק קובץ אחד. משטח התקיפה מצטמצם מ"335 שאילתות ממושמעות" ל"נקודת בחירת קובץ אחת" שאפשר להקיף בבדיקות.
- **חסרונות אמיתיים:** גיבוי = הרבה קבצים (פתיר: job לילי שמעתיק snapshot לכל tenant ל-object storage, או Litestream); אנליטיקה רוחבית דורשת איטרציה על קבצים (בסקייל הנוכחי — לולאה פשוטה, ואפשר לשקף מטריקות ל-control plane); תקרת סקייל של מכונה אחת (אנכי; מאות עסקים קטנים בעומס הקיים — ריאלי בנוחות, כי כל tenant הוא low-traffic ו-WAL הוא פר-קובץ כך שאין תחרות כתיבה בין לקוחות); דיסק אחד = SPOF (ממותן בגיבויים; זהה למצב היום עם N דיסקים בלי גיבוי).

### 2.4 טבלת השוואה

| קריטריון | א' — Postgres+tenant_id | ב' — SQLite פר-tenant |
|---|---|---|
| שינוי בשכבת ה-queries | ~290 פונקציות + פורט דיאלקט + סכימה | ~פונקציה אחת (`get_connection`) + בדיקות |
| הגירת לקוח קיים | ETL מלא פר-לקוח + רימאפינג PK | העתקת קבצים + checksum |
| בידוד נתונים | לוגי — משמעת/RLS על כל שאילתה | פיזי — קובץ פר-tenant (כמו היום) |
| כשל בידוד אופייני | שאילתה אחת בלי סינון → דליפה שקטה | בחירת tenant שגויה בכניסה → נקודת בקרה אחת |
| מיגרציות סכימה | מסגרת חדשה (Alembic) | הקיים עובד פר-קובץ, ללא שינוי |
| גיבויים | מנוהל (Supabase/PITR) — יתרון ברור | דורש בנייה (job לילי ל-object storage) |
| אנליטיקה רוחבית | SQL ישיר | איטרציה/שיקוף ל-control plane |
| תקרת סקייל | גבוהה (scale-out) | מכונה אחת, מאות tenants בעומס הנוכחי |
| סיכון רגרסיה בהגירה | גבוה (כל השכבה משתכתבת) | נמוך (הקוד המוכח נשאר) |
| זמן עד לקוח ראשון על הפלטפורמה | חודשים | שבועות |

### 2.5 ההמלצה

**מסלול ב', עם control plane קטן — וזו המלצה מטעמי שורש, לא מטעמי נוחות:**

1. **הנכס המרכזי של הארכיטקטורה הנוכחית הוא הבידוד הפיזי.** מסלול ב' משמר אותו; מסלול א' מחליף אותו במשמעת שאילתות — ירידה בביטחון דווקא בפרויקט שכל מהותו רגישות לפרטיות (תיקון 13, מטריצת הפרטיות).
2. **הבעיה שאתה פותר היא תפעולית** (N ריפואים, N פריסות, N אפליקציות OAuth) — לא בעיית מנוע DB. מסלול ב' פותר אותה במלואה: deploy אחד, קוד אחד, לקוח חדש = שורה ב-control plane + קובץ.
3. **הסיכון א-סימטרי.** במסלול א' הרגרסיות אורבות ב-7,400 שורות SQL משוכתבות; במסלול ב' הקוד שנבדק בפרודקשן ממשיך לרוץ אות-באות מול אותו פורמט קובץ.
4. **שום דבר לא נזרק.** אם תגיע לנקודה שבה Postgres מוצדק (אלפי tenants, צורך ב-scale-out, אנליטיקות כבדות) — עבודת ה-tenant context, הראוטינג, הסודות וה-OAuth עוברות כמו שהן, וה-port יתבצע אז כפרויקט נפרד ממוקד. אפשר אף לעשות אותו מדורג: להתחיל בזה שה-**control plane** עצמו יעלה ל-Supabase (הוא קטן וחדש — שם זה זול) ולהשאיר את נתוני ה-tenants ב-SQLite.

**מתי הייתי הופך את ההמלצה:** אם בטווח 12–18 חודשים אתה צופה >~500 לקוחות פעילים, צורך במספר אינסטנסים של אפליקציה במקביל, או דרישות דוחות רוחביים בזמן-אמת — התחל ישר במסלול א' ותתמחר את הפורט המלא (סדר גודל של פי 3–5 עבודה, רובה בשכבת ה-DB ובבדיקות).

---

## 3. ארכיטקטורת Google OAuth מרכזית

### 3.1 העיקרון

אפליקציית OAuth **אחת** של הפלטפורמה (פרויקט Google Cloud אחד, client_id אחד), על **דומיין אחד בבעלותך**. כל בעל עסק מתחבר אליה כמשתמש — בדיוק "Sign in with Google" — והטוקנים שלו נשמרים אצלך פר-tenant. אין יותר אפליקציה/דומיין/אימות פר לקוח.

### 3.2 מבנה

- **פרויקט GCP אחד** עם Calendar API מופעל; מיתוג ה-consent screen = המותג של הפלטפורמה (שם, לוגו, support email).
- **דומיין:** ‏`app.<הדומיין-שלך>` (הפאנל של הפלטפורמה). ‏Authorized domain = ‏`<הדומיין-שלך>`, מאומת פעם אחת ב-Search Console. עמודי privacy/terms כבר קיימים בקוד (`/legal/privacy`, `/legal/terms`) — יוגשו מהדומיין הזה.
- **Redirect URI יחיד:** ‏`https://app.<domain>/google/callback`. נעלמת התחזוקה של "להוסיף redirect לכל לקוח" מה-checklist.
- **ה-flow הקיים כמעט לא משתנה:** בעל העסק מחובר לפאנל (ה-session כבר יישא `tenant_id` — פרק 5) → ‏`/google/connect` עם `state` CSRF כמו היום → callback קושר את הטוקנים ל-tenant **מה-session המאומת** (לא מ-state, כדי שלא ניתן יהיה לשתול טוקן אצל tenant זר).
- **Scopes:** להשאיר `https://www.googleapis.com/auth/calendar` (מכסה את ארבעת השימושים בפועל: freebusy, insert, delete, ‏calendars.get). ל-verification אפשר לשקול צמצום ל-`calendar.events`+`calendar.freebusy` — מקל על ה-review אבל דורש התאמה קטנה של שליפת email/timezone; החלטה בזמן הגשת ה-verification, לא לפני.

### 3.3 אחסון ורענון טוקנים

- **במסלול ב' אין כמעט שינוי:** ‏`google_calendar_credentials` נשארת singleton — **בתוך קובץ ה-tenant** — עם ההצפנה הקיימת. משתנה רק מקור client_id/secret (env של הפלטפורמה) וה-redirect.
- **רענון:** המנגנון ה-lazy הקיים (`google_calendar.py:179-250`) נשאר; ברגע שהאפליקציה In Production הטוקן פשוט לא פג כל 7 ימים. מוסיפים **job פלטפורמה שבועי** שמרענן טוקן לכל tenant מחובר — גם keep-alive (טוקן שלא בשימוש ~6 חודשים נפסל) וגם גילוי מוקדם של ניתוקים במקום לגלות בזמן קביעת תור. מנגנון ההתראה על `auth_invalid` כבר קיים ועובד.

### 3.4 Verification — הסרת האזהרה והתקרה

‏Calendar הוא scope בסיווג **sensitive** (לא restricted — אין צורך בביקורת אבטחה CASA). הגשת verification דורשת: אימות בעלות על ה-authorized domain ב-Search Console, homepage + privacy policy עקביים, הצהרת scopes עם הצדקה, וסרטון דמו של ה-flow. אורך התהליך: ימים עד שבועות. התוצאה: נעלם מסך "unverified app", נעלמת תקרת ‎100 המשתמשים. עד אישור ה-verification הפלטפורמה עובדת במצב In-Production-unverified (ראה פרק 9 — אותו מצב בדיוק כמו פתרון הביניים).

### 3.5 איך זה סוגר את שתי הבעיות

- **ניתוק כל 7 ימים** — נעלם עם publishing (סטטוס In Production ⇒ refresh tokens ללא תפוגה קבועה).
- **אימות דומיין** — נדרש פעם אחת, על דומיין אחד שבבעלותך, לאפליקציה אחת. הבעיה של "דומיין לכל לקוח" לא נפתרת — היא **מתבטלת כקטגוריה**.

---

## 4. ניהול סודות מוצפן פר-tenant

### 4.1 חלוקת אחריות

- **env של הפלטפורמה (תהליך אחד):** ‏`GOOGLE_CLIENT_ID/SECRET`, ‏`META_APP_ID/SECRET/VERIFY_TOKEN`, ‏`OPENAI_API_KEY`, ‏`SECRETS_ENCRYPTION_KEY`, ‏`LEDGER_PEPPER_V1`, ‏VAPID, ‏SMTP, ‏Sentry, ‏`DEVELOPER_*`, ‏`ADMIN_SECRET_KEY` (מפתח session אחד לפלטפורמה).
- **Control plane, מוצפן:** לכל tenant — טוקן בוט טלגרם + webhook secret, פרטי Twilio (SID/token/מספר), מזהי נכסים (page_id, ig_account, מספר וואטסאפ) לראוטינג, פרטי בעל העסק להתראות.
- **בתוך קובץ ה-tenant (כמו היום):** טוקני Google ו-page tokens של Meta — כבר מוצפנים, אין סיבה להזיז.
- **לא סוד אלא אימות:** סיסמאות אדמין הופכות למשתמשים ב-control plane עם hash (‏Werkzeug כמו היום), לא להצפנה.

### 4.2 עיצוב האחסון (control plane)

סקיצת סכימה (להמחשה — לא מימוש):

```sql
tenants(
  tenant_id TEXT PRIMARY KEY,          -- slug קצר [a-z0-9-], משמש גם כשם תיקייה
  display_name TEXT NOT NULL,
  status TEXT CHECK(status IN ('active','suspended','migrating')) NOT NULL,
  plan TEXT NOT NULL,
  created_at ...
)

tenant_secrets(
  tenant_id TEXT NOT NULL REFERENCES tenants,
  name TEXT NOT NULL,                  -- 'telegram_bot_token' | 'twilio_auth_token' | ...
  value_enc TEXT NOT NULL,             -- Fernet, פורמט v-prefix הקיים
  updated_at ...,
  PRIMARY KEY (tenant_id, name)
)

tenant_routes(
  route_type TEXT NOT NULL,            -- 'telegram_webhook_key' | 'twilio_number' | 'meta_page_id' | 'widget_key' | 'public_slug'
  route_key TEXT NOT NULL,             -- הערך הנכנס (או fingerprint שלו)
  tenant_id TEXT NOT NULL REFERENCES tenants,
  PRIMARY KEY (route_type, route_key)
)

admin_users(
  email TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  tenant_id TEXT NOT NULL REFERENCES tenants,
  role TEXT CHECK(role IN ('owner','platform_admin')) NOT NULL
)
```

- **הצפנה:** אותו `utils/crypto.py` — הפורמט המגורסן `v1:` כבר תומך ברוטציה. שדרוג מוצע: גרסה `v2` עם מפתח נגזר פר-tenant — ‏HKDF(master_key, tenant_id) — כך שדליפת ciphertext של tenant אחד לא שקולה לדליפת כולם, והמפתח הראשי נשאר יחיד ב-env.
- **רוטציה:** הוספת `SECRETS_ENCRYPTION_KEY_V2` (המנגנון כבר מוכן ב-`utils/crypto.py:78`), הצפנה-מחדש ברקע של כל הרשומות, החלפת `CURRENT_KEY_VERSION`.

### 4.3 הקשחה — משנה מדיניות, לא רק מיקום

1. **fail-closed:** במצב פלטפורמה, היעדר `SECRETS_ENCRYPTION_KEY` = סירוב עלייה. מסלול ה-fallback לplaintext (`utils/crypto.py:107-119`) מבוטל.
2. **סוף לכתיבת סודות ל-`.env` מהפאנל** — עמוד "הגדרות תשתית" עובר לכתוב ל-`tenant_secrets`; אין יותר `dotenv.set_key` + monkey-patching (`admin/app.py:3060-3196`).
3. **סודות לעולם לא בלוגים/תבניות** — נשמר הכלל הקיים; ה-DTO של עמודי אדמין מציג רק "מוגדר/לא מוגדר" וארבע ספרות אחרונות.

---

## 5. Tenant Isolation

### 5.1 החוזה: TenantContext אחד, ללא ברירת מחדל

- ‏`contextvars.ContextVar("current_tenant")` **בלי default**. גישה כשהוא לא נקבע ⇒ חריגה מיידית (fail-loud). זה החוזה שכל השכבות נשענות עליו.
- נקבע ב-**ארבע משפחות כניסה בלבד**, וכולן עוברות דרך פונקציה אחת `set_current_tenant(...)`:
  1. **בקשת אדמין** — middleware שקורא `tenant_id` מה-session אחרי login (ה-session נחתם ב-`ADMIN_SECRET_KEY` של הפלטפורמה).
  2. **webhook ערוץ** — אחרי resolve לפי מפתח הראוטינג (פרק 6).
  3. **job מתוזמן** — לולאה מפורשת `for tenant in list_active_tenants(): with tenant_context(tenant): ...`.
  4. **CLI/תחזוקה** — דגל `--tenant` מפורש.
- ‏asyncio ו-threads: ‏contextvars זורמים לתוך tasks באופן טבעי; בהעברות בין threads (למשל `run_coroutine_threadsafe`, ‏`asyncio.to_thread`) ה-tenant מועבר מפורשות כפרמטר ונקבע מחדש בצד השני — כלל קוד, נאכף ב-review ובבדיקות.

### 5.2 בידוד קבצים (מסלול ב')

- פריסה: ‏`DATA_DIR/platform.db` + ‏`DATA_DIR/tenants/<tenant_id>/{chatbot.db, faiss_index/}`.
- ‏`get_connection()` גוזר את הנתיב מ-`current_tenant` **בלבד**. ולידציית slug ‏(`^[a-z0-9-]{1,32}$`) + אימות שהנתיב המוחלט בתוך שורש ה-tenants (הגנת path traversal) — פעם אחת, בנקודה אחת.
- ‏tenant במצב `suspended`/`migrating` ⇒ סירוב חיבור (מונע כתיבה בזמן הגירה/עזיבה).
- מחיקת לקוח (זכות מחיקה עסקית) = מחיקת תיקייה + שורות control plane — פשוטה וניתנת להוכחה, יתרון ישיר של הבידוד הפיזי.

### 5.3 בידוד state בזיכרון (החזית האמיתית של הדליפות)

| היום | היעד |
|---|---|
| ‏`_store` FAISS יחיד (`rag/vector_store.py:188`) | Registry ‏`tenant → VectorStore` עם LRU (טעינה עצלה, תקרת אינדקסים חמים בזיכרון) |
| query cache ‏`(query, top_k)` (`rag/engine.py:43`) | מפתח ‏`(tenant, query, top_k)` — או ביטול ה-cache (רווח קטן, סיכון גדול) |
| cache חופשה יחיד (`vacation_service.py:29`) | ‏dict פר-tenant או קריאת DB ישירה (זולה) |
| ‏dicts לפי `user_id` (rate limiter, booking FSM, follow-up, pending-deletes) | מפתח ‏`(tenant, user_id)`; תקרות גודל פר-tenant |
| ‏`_twilio_client`, ‏`bot_state._bot` יחידים | Registry לקוחות/בוטים פר-tenant (טעינה עצלה מה-control plane) |
| מוני login/widget פר-IP | מפתח ‏`(tenant, ip)`; נשארים בזיכרון בתהליך-יחיד (Redis רק אם יתרבו workers) |

### 5.4 שכבות הגנה נוספות

- **לוגים:** ‏logging record factory שמזריק `tenant=<id>` לכל שורה; tag ב-Sentry. בלי זה אי-אפשר לחקור תקלות בפלטפורמה.
- **בדיקות חובה בשלב 2:** (א) קריאת DB בלי tenant context ⇒ חריגה; (ב) שני tenants עם אותם user_id/שאלה — אין זליגת cache/RAG; (ג) session של tenant א' לא ניגש לנתוני ב' (בדיקת HTTP על הפאנל); (ד) job שמאתר — כל tenant רואה רק את עצמו.
- **עיקרון "resolve פעם אחת":** ה-tenant נקבע רק בכניסות המוגדרות; שום קוד עמוק לא "מנחש" tenant מנתונים (למשל ממספר טלפון) — מונע בלבול בין זהות המשתמש לזהות העסק.

---

## 6. ראוטינג הודעות נכנסות

עיקרון משותף: לכל ערוץ יש **מפתח ראוטינג בלתי-ניתן-לניחוש** שממופה ל-tenant ב-`tenant_routes`, וה-resolve קורה פעם אחת בשולי המערכת.

### 6.1 Telegram

- **מצב הפלטפורמה: webhook בלבד** (polling של N בוטים בתהליך אחד לא סקיילבילי; polling נשאר לפיתוח מקומי).
- לכל tenant נרשם ‏`setWebhook` ל-‏`POST /telegram/webhook/<webhook_key>` — מפתח אקראי פר-tenant — עם `secret_token` פר-tenant (נתמך כבר היום ב-flow הקיים, `bot/telegram_bot.py:302`, `admin/app.py:5899-5903`).
- ‏resolve: ‏`webhook_key → tenant` ⇒ קביעת context ⇒ ‏`process_update` על אובייקט ‏`Application` של אותו tenant מתוך registry (בנייה עצלה, ‏JobQueue פר-אפליקציה **כבוי** — ה-jobs עוברים ל-scheduler פלטפורמתי, ראה 6.5).
- ‏deep links, QR ולינקי הפניה ממשיכים לעבוד ללא שינוי — הם תלויי bot username, לא דומיין.

### 6.2 WhatsApp (Twilio)

- ‏URL ה-webhook שמוגדר ב-Twilio Console הופך ל-‏`POST /webhook/whatsapp/<webhook_key>` פר-tenant.
- סדר עיבוד: ‏resolve ‏`webhook_key → tenant` ⇒ שליפת `twilio_auth_token` של ה-tenant ⇒ **אימות חתימה עם הטוקן של אותו tenant** ⇒ קביעת context. (resolve-לפני-אימות הכרחי כי החתימה תלוית-tenant; ה-key האקראי ב-URL מונע שימוש כ-oracle.)
- שדה `To` נשמר ומוצלב כ-sanity check מול המספר הרשום ל-tenant (התראת mismatch למפתח — מנגנון `developer_alerts` הקיים).
- **טופולוגיית חשבונות:** שתי אפשרויות נתמכות באותו עיצוב — ‏BYO-Twilio (כמו היום, כל לקוח חשבונו) או **subaccounts תחת חשבון פלטפורמה** (מומלץ ללקוחות חדשים: auth token נפרד פר-tenant, הפרדת עלויות, השעיה נקודתית). ההחלטה עסקית — פרק 10.

### 6.3 Meta (Messenger/Instagram)

- הדפוס הנכון **כבר קיים**: אפליקציה משותפת אחת + resolve לפי `entry.id`. השינוי: ‏`is_meta_entry_known` עובר מטבלת ה-tenant ל-lookup ב-`tenant_routes` ‏(`meta_page_id → tenant`), והשורה המוצפנת של ה-page token נשארת בקובץ ה-tenant.
- ⚠️ **נקודת cutover רגישה:** ‏callback URL של webhook הוא הגדרה **ברמת האפליקציה** — רגע ההחלפה מזיז את התעבורה של *כל* העמודים בבת אחת. לכן לקוחות Meta מהגרים **בגל אחד מתואם**, או שהפלטפורמה מעבירה (forward) אירועי עמודים לא-מוכרים לאינסטנסים הישנים בתקופת המעבר.

### 6.4 Widget ועמודים ציבוריים

- ‏snippet ההטמעה מקבל ‏`data-key="<widget_key>"` (מפתח ציבורי אקראי פר-tenant); ‏`/widget/api/chat` עושה resolve לפיו. ‏CORS allowlist הופך להגדרת tenant (מהפאנל) במקום env. ‏rate limit במפתח ‏`(tenant, ip)`.
- עמודים ציבוריים ו-ICS עוברים לנתיב נושא-tenant: ‏`/p/<tenant_slug>/<page_id>`, ‏`/ics/<tenant_slug>/<page_id>` — resolve חד-משמעי בלי lookup גלובלי. הלינקים הישנים ממילא מתים עם הדומיינים הישנים; העמודים הם ephemeral.
- לינקים עמוקים בהתראות לבעל עסק נבנים מ-`PLATFORM_URL` אחיד (יורש `ADMIN_URL`).

### 6.5 Jobs — מסגרת אחת במקום שתיים

ה-scheduler הפלטפורמתי (thread יחיד + לולאת tenants, מחליף גם את JobQueue וגם את שני ה-threads הקיימים): תזכורות תורים, follow-up לידים, ‏retention purge, שידורים מתוזמנים, ניקוי live chat, רענון שבועי של טוקני Google, memory extraction. כל ריצה עוטפת tenant אחד ב-context משלו, עם try/except פר-tenant (כשל אצל לקוח אחד לא עוצר את השאר — בהתאם לכלל הקיים ב-CLAUDE.md על לולאות I/O).

---

## 7. תוכנית הגירה מדורגת

> עיקרון: כל שלב ניתן לפריסה בפני עצמו, בלי לשבור את הלקוחות הקיימים. השלבים 1–2 נשלחים כקוד לריפו הקיים (והלקוחות הקיימים מקבלים אותם כ-deploy רגיל) — כך הקוד המשותף נבדק בפרודקשן אמיתי עוד לפני שהפלטפורמה קיימת.

### שלב 0 — פתרון ביניים Google (השבוע; פרק 9)

עצמאי לחלוטין מההגירה. מבטל את כאב 7 הימים מיד.

### שלב 1 — יסודות בקוד הקיים, אפס שינוי התנהגות (סדר גודל: ימים בודדים)

1. **איחוד ה-namespace:** ‏`ai_chatbot/*` הופך ל-alias אמיתי (רישום אותם אובייקטי מודול ב-`sys.modules`) במקום `from X import *` — מעלים את בעיית שני-העותקים; הטסטים מפסיקים לעשות double-patching.
2. **קונפיג עסקי דרך פונקציות:** ‏`get_business_config()` שקורא DB-first (הרחבת `bot_settings`/`business_profile` בשדות שם/טלפון/כתובת/אתר) עם fallback ל-env. המודולים מפסיקים לייבא `BUSINESS_NAME` by-value; ‏`build_system_prompt` מקבל את שם העסק כפרמטר.
3. **TenantContext נטוע עם ערך קבוע `"default"`** בכל נקודות הכניסה — התשתית נכנסת, ההתנהגות זהה. ‏`get_connection` מתחיל לקרוא את הנתיב דרך ה-context.
4. **תיקוני שורש תפעוליים:** מעבר ל-gunicorn (threads) + ‏ProxyFix; שמירת `To` ב-webhook של WhatsApp; **גיבוי לילי** של `chatbot.db` (‏`sqlite3 .backup`) + ‏`faiss_index` ל-object storage — נפרס לכל הלקוחות הקיימים מיד.
5. הרחבת בדיקות סביב הנקודות שנגעו בהן.

**Definition of done:** כל הלקוחות הקיימים רצים על הקוד הזה בפרודקשן ללא שינוי נראה.

### שלב 2 — בניית הפלטפורמה (סדר גודל: 2–4 שבועות, הליבה של הפרויקט)

1. ‏Control plane (‏`platform.db`): ‏tenants, ‏routes, ‏secrets, ‏admin_users + CLI ל-onboarding (`create-tenant`: יצירת תיקייה, ‏init_db, ‏seed, רישום ראוטים).
2. ניתוב ‏`get_connection` פר-tenant + ‏registries פר-tenant (FAISS, בוטים, לקוחות Twilio) + מיפתוח כל ה-caches (סעיף 5.3).
3. ראוטינג ערוצים (פרק 6) + ‏scheduler פלטפורמתי (6.5).
4. פאנל אדמין רב-משתמשים: login ‏email+password ⇒ ‏session עם `tenant_id`; מסך platform-admin (מחליף את `/dev`) עם רשימת tenants, יצירה, השעיה, מעבר-הקשר.
5. ‏OAuth מרכזי: דומיין, ‏Search Console, ‏redirect יחיד, הגשת verification (פרק 3) + ‏fail-closed לסודות (פרק 4).
6. חבילת בדיקות הבידוד (5.4) — תנאי מעבר לשלב 3.

### שלב 3 — פיילוט (שבוע)

- הקמת סביבת הפלטפורמה ב-Render (service חדש, דיסק גדול יותר, דומיין `app.<domain>`).
- ‏tenant ראשון: עסק הדמו/לקוח חדש אמיתי. ריצה מלאה של צ'ק-ליסט ההקלטה מול הפלטפורמה. הלקוחות הקיימים לא מושפעים — שתי המערכות רצות במקביל.

### שלב 4 — הגירת לקוחות קיימים, בגלים (סדר גודל: ~שעה ללקוח + חלון השגחה)

‏runbook ללקוח בודד:

1. הודעה מראש ללקוח; בחירת שעת שפל. העמדת האינסטנס הישן על readonly (עצירת הבוט).
2. העתקת ‏`chatbot.db` (עם ‏`.backup` עקבי) + ‏`faiss_index` לפלטפורמה; אימות checksum; ‏`create-tenant --import`.
3. יבוא ההגדרות שהיו ב-env: זהות עסקית ⇒ קובץ ה-tenant; טוקנים ⇒ ‏`tenant_secrets`; רישום ‏routes.
4. החלפת ‏Telegram ‏`setWebhook` ל-URL הפלטפורמה (אטומי — מרגע זה ההודעות זורמות לפלטפורמה); עדכון webhook של Twilio ב-console; ‏widget — עדכון snippet באתר הלקוח (או redirect מהדומיין הישן).
5. בעל העסק מתחבר לפאנל החדש; **מחבר מחדש Google Calendar בלחיצה אחת** (client_id חדש ⇒ הטוקנים הישנים לא עבירים; על האפליקציה המפורסמת זו הפעם האחרונה).
6. השגחה 24–48 שעות; האינסטנס הישן מושעה (לא נמחק!) עם הדיסק שלו כ-rollback למשך 14 יום; ‏rollback = ‏setWebhook חזרה + הפעלת האינסטנס.
7. גל Meta (אם יש כמה לקוחות עם Messenger/Instagram): מתואם יחד בגלל ה-callback המשותף (6.3).

### שלב 5 — סגירה

מחיקת אינסטנסים וריפואים משוכפלים; העברת מסמכי DPA/פרטיות לנוסח פלטפורמה (עדכון "היכן מאוחסן המידע"); עדכון ‏`docs/client_checklist.md` לתהליך onboarding החדש (שורה ב-control plane במקום ריפו); דוח סיכום עלויות (N אינסטנסים ⇒ 1–2).

---

## 8. ניתוח סיכונים

| # | סיכון | מסלול/שלב | חומרה | מיטיגציה |
|---|---|---|---|---|
| 1 | **דליפה בין tenants דרך state בזיכרון** (RAG cache, FAISS, חופשה) | ב', שלב 2 | קריטית | הקטלוג בסעיף 5.3 מטופל כרשימת חובה; בדיקות בידוד כתנאי-שער; ביטול caches שערכם שולי |
| 2 | ‏tenant context שגוי/חסר בנקודת כניסה | שניהם | קריטית | ‏ContextVar בלי default (fail-loud); resolve רק ב-4 כניסות; מפתחות ראוטינג אקראיים; sanity check על `To`/page_id |
| 3 | **blast radius**: באג אחד מפיל את כל הלקוחות (היום — לקוח אחד) | שניהם | גבוהה | ‏tenant קנרי + סביבת staging; ‏feature flags פר-tenant (התשתית קיימת — `subscription.features_json`); ‏deploy מדורג; התחייבות ל-rollback מהיר |
| 4 | אובדן נתונים בהעתקת קבצים בהגירה | ב', שלב 4 | גבוהה | ‏`.backup` עקבי (לא cp על קובץ חי), ‏checksum, אינסטנס ישן נשמר 14 יום, גיבוי לילי כבר מש��ב 1 |
| 5 | צוואר בקבוק ביצועים: LLM סינכרוני בתהליך אחד לכל הלקוחות | שניהם | בינונית–גבוהה | ‏gunicorn threads (שלב 1); ניטור latency פר-tenant; בהמשך — תור עבודות אם הסקייל ידרוש |
| 6 | ‏SPOF: מכונה/דיסק אחד לכולם | ב' | בינונית | גיבוי לילי + נוהל restore מתורגל; זה שיפור לעומת היום (N דיסקים בלי גיבוי כלל) |
| 7 | דליפת מפתח הצפנה יחיד = כל הסודות | שניהם | בינונית | ‏HKDF פר-tenant (v2); מפתח רק ב-env של Render; נוהל רוטציה מתועד (התשתית קיימת) |
| 8 | ‏cutover של Meta מזיז את כולם בבת אחת | שלב 4 | בינונית | גל מתואם או forwarding לאינסטנסים ישנים; חלון קצר |
| 9 | תקרת 100 משתמשים על אפליקציה לא-מאומתת (ביניים) | שלב 0 | נמוכה בסקייל הנוכחי | הגשת verification בשלב 2; התקרה היא על *מעניקי הרשאה* (בעלי עסק), לא על לקוחות קצה |
| 10 | ציות (תיקון 13): המידע עובר לשרת משותף | שלב 4–5 | בינונית | עדכון DPA/מדיניות פרטיות לפני ההגירה; הבידוד הפיזי פר-קובץ מקל על ההוכחה; ‏delete/export פר-tenant נשארים פשוטים |
| 11 | סחף בין קבצי tenants (סכימה לא אחידה) | ב', שוטף | נמוכה | ‏run_migrations האידמפוטנטי רץ לכל קובץ בפתיחה/עלייה — הדפוס הקיים בדיוק |
| 12 | רגרסיות מהשכתוב — הסיכון המרכזי של מסלול א' | א' | גבוהה (אם ייבחר) | הסיבה המרכזית להמלצה על ב' |

**החולשות המבניות של כל מסלול, בכנות:** מסלול ב' דוחה את שאלת הסקייל האופקי ואת האנליטיקה הרוחבית, ומטיל עליך את הגיבויים; מסלול א' קונה את אלה במחיר שכתוב שכבת הנתונים כולה, ETL פר-לקוח, ובידוד שהופך תלוי-משמעת. בסקייל של עשרות עד מאות עסקים קטנים — העסקה של ב' עדיפה בבירור.

---

## 9. פתרון ביניים לניתוק כל 7 ימים — אפשר לבצע כבר השבוע

**עצמאי לחלוטין מההגירה, ומבטל את הכאב המרכזי מיד.**

### 9.1 הצעד

1. **איחוד לפרויקט Google Cloud אחד:** אם ללקוחות קיימים יש פרויקטים נפרדים — בוחרים פרויקט אחד, ומוסיפים בו את כל ה-redirect URIs של הלקוחות (`https://<admin-של-כל-לקוח>/google/callback`). ה-checklist כבר מתעד את התצורה הזו (`docs/client_checklist.md`, סעיף 4.5). מעדכנים `GOOGLE_CLIENT_ID/SECRET` בכל אינסטנס שעבר פרויקט.
2. **Publish ל-Production:** ‏OAuth consent screen ⇒ ‏Publishing status ⇒ ‏"In production". לא נדרש verification כדי לפרסם.
3. **חיבור מחדש חד-פעמי:** כל בעל עסק לוחץ שוב "חבר Google Calendar" (טוקנים שהונפקו תחת Testing/client אחר אינם משודרגים). מסך ההרשאות יציג אזהרת "unverified app" — ממשיכים דרך Advanced. **מכאן והלאה ה-refresh token לא פג אחרי 7 ימים.**

### 9.2 מה המצב הזה כן ולא נותן

- ✅ סוף לניתוק השבועי (תפוגת 7 הימים היא תכונה של סטטוס Testing בלבד; ב-Production טוקן נפסל רק ב-revoke או אי-שימוש ~6 חודשים — והשימוש השוטף של הבוט מרחיק את זה).
- ⚠️ מסך אזהרה חד-פעמי בעת חיבור (לבעל העסק בלבד; לקוחות הקצה לא נוגעים ב-Google).
- ⚠️ תקרת ‎100 הענקות-הרשאה לכל חיי הפרויקט לאפליקציה לא מאומתת עם sensitive scope — רלוונטי רק למספר בעלי העסק; בסקייל הנוכחי לא מגביל, אבל **לא ניתן לאיפוס** — עוד סיבה לא לבזבז הענקות על ניסויים בפרויקט הזה.
- ❌ הסרת האזהרה והתקרה דורשת verification — וזה **חסום היום** כי הדומיינים הם `*.onrender.com` (סעיף 1.7.3). ה-verification יוגש בשלב 2 על הדומיין המרכזי.

### 9.3 וריאנט "ביניים+" (לשלמות התמונה, לא מומלץ כמסלול)

אפשר לפתור את Google *לחלוטין* גם בלי multi-tenant: לרכוש דומיין אחד, לתת לכל לקוח קיים custom domain ‏(`client1.yourdomain.com`) על ה-Render שלו, לאמת את הדומיין פעם אחת ולהגיש verification על האפליקציה המשותפת. זה מוכיח את הטענה שלך שהבעיה של גוגל היא תסמין: היא נפתרת בשכבת הדומיין. **אבל** זה משאיר את המחלה — N ריפואים, N פריסות, עלות אינסטנס פר לקוח — ולכן נכון רק אם ההגירה נדחית משמעותית.

מקורות למדיניות Google (נבדקו יולי 2026):
- [Google — Manage App Audience (publishing status, user cap)](https://support.google.com/cloud/answer/15549945)
- [Google — Sensitive scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)
- [Google — Using OAuth 2.0 (refresh token expiry in Testing)](https://developers.google.com/identity/protocols/oauth2)
- [Unipile — Google OAuth refresh token 7-day limit](https://www.unipile.com/google-oauth-refresh-token/)
- [Nylas — Google OAuth verification: costs & timelines](https://www.nylas.com/blog/google-oauth-app-verification/)

---

## 10. שאלות פתוחות והחלטות שלך

| # | שאלה | ברירת המחדל המוצעת |
|---|---|---|
| 1 | **דומיין הפלטפורמה** — צריך לרכוש/לבחור דומיין. משפיע על OAuth, widget, עמודים ציבוריים | ‏`app.<domain>` אחד לכל הפאנלים; בלי סאב-דומיין פר-tenant (מיותר בשלב זה) |
| 2 | **Twilio topology** — ‏BYO פר לקוח (כמו היום) או subaccounts תחת חשבון פלטפורמה? | קיימים נשארים BYO; חדשים על subaccounts |
| 3 | **עלויות OpenAI** — מפתח פלטפורמה משותף מחייב מדידת שימוש פר-tenant לבילינג | מפתח משותף + רישום צריכת טוקנים פר-tenant ל-control plane (שדה בכל קריאת LLM) |
| 4 | **Control plane על SQLite או Supabase?** | להתחיל SQLite (`platform.db`, אפס תשתית חדשה); המעבר ל-Supabase זול כי הסכימה קטנה וחדשה |
| 5 | **כמה לקוחות פעילים יש כרגע ומה תחזית 12 חודשים?** | קובע את תכנון הגלים בשלב 4 ואת בדיקת הסף של סעיף 2.5 |
| 6 | ‏**VAPID / Web Push** — זוג מפתחות פלטפורמה אחד או פר-tenant? | פלטפורמה אחד (ה-subscription ממילא פר-דפדפן של בעל העסק) |
| 7 | **מדיניות דמו** — ‏`DEMO_MODE` הופך ל-tenant דמו ייעודי בפלטפורמה | ‏tenant ‏`demo` עם דגל read-only במקום env גלובלי |
| 8 | **צמצום scopes של Google בעת ה-verification** (סעיף 3.2) | להחליט מול טופס ה-verification; לא חוסם שום שלב מוקדם |

---

*נכתב על בסיס חקירת קוד מלאה: ‏`database.py`, ‏`migrations.py`, ‏`config.py`, ‏`main.py`, ‏`admin/app.py`, ‏`admin/meta_oauth.py`, ‏`admin/widget.py`, ‏`google_calendar.py`, ‏`bot/telegram_bot.py`, ‏`messaging/*`, ‏`rag/*`, ‏`memory/*`, ‏`utils/crypto.py`, ‏`render.yaml`, ‏`docs/client_checklist.md`, ‏`docs/privacy_data_matrix.md`, וסריקת כלל ה-state הגלובלי בריפו. כל הפניות file:line נכונות ל-HEAD של main בזמן הכתיבה (bbb5218).*
