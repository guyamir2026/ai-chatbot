# אפיון: מערכת חבילות (Plans) ופיצ'רים מבוססי Feature Flags

**סטטוס**: טיוטה לאישור — לפני מימוש.
**מטרה**: לאפשר למפתח (אני) להגדיר לכל לקוח SaaS (בעל עסק) חבילה אחת מתוך שלוש (בסיסי / מתקדם / כוללת), ולשלוט על אילו פיצ'רים פעילים בפריסה שלו, באמצעות feature flags גמישים שניתן לעקוף ידנית.

---

## 0. הקשר ארכיטקטוני קריטי

הפרויקט הוא **single-tenant**: כל deployment שייך ללקוח SaaS אחד (בעל עסק אחד), והטבלאות `bot_settings`, `google_calendar_credentials` הן singletons עם שורה יחידה (id=1).

**משמעות עבור החבילות**:

| מושג | פירוש כאן |
|---|---|
| "לקוח" שמקבל חבילה | בעל העסק שמשלם לי (developer) — **לא** לקוחות הקצה שמדברים עם הבוט |
| מקום החבילה ב-DB | טבלה חדשה עם שורה יחידה (singleton), בסגנון `bot_settings` |
| לקוחות הקצה (טבלת `users`) | **לא מושפעים** מהחבילה — הם תמיד מקבלים את אותו השירות |
| "Frontend" | פאנל ה-Flask/Jinja/HTMX הקיים ב-`admin/` |
| גישה לשינוי חבילה | למפתח בלבד, דרך מסך אדמין מוגן (לא לבעל העסק) |

זה משנה לחלוטין את התכנון מול ההצעה המקורית של "שדה JSON בטבלת users" — אין צורך בשדה per-user; כל הקונפיגורציה גלובלית לפריסה.

---

## 1. מבנה מסד נתונים (SQLite)

### 1.1 טבלה חדשה: `subscription`

טבלה singleton (שורה אחת בלבד, id=1), בדומה ל-`bot_settings` (`database.py:240-263`) ו-`google_calendar_credentials` (`database.py:266-276`).

**שדות מוצעים**:

| שדה | טיפוס | תיאור |
|---|---|---|
| `id` | INTEGER PRIMARY KEY CHECK(id=1) | אכיפת singleton |
| `plan` | TEXT NOT NULL CHECK(plan IN ('basic','advanced','premium')) | החבילה הנוכחית |
| `features_json` | TEXT NOT NULL DEFAULT '{}' | feature flags ידניים שעוקפים את ברירת המחדל של החבילה |
| `plan_started_at` | TEXT NOT NULL DEFAULT (datetime('now')) | מועד תחילת החבילה — לחישוב תקופת חסד |
| `plan_ends_at` | TEXT | תאריך סיום מתוכנן (אופציונלי, אם יש חוזה לטווח קצוב) |
| `grace_period_days` | INTEGER NOT NULL DEFAULT 15 | ימי חסד לשינויים — נגזר מהחבילה אבל ניתן לדריסה |
| `notes` | TEXT DEFAULT '' | הערות פנימיות של המפתח (לא מוצג ללקוח) |
| `updated_at` | TEXT NOT NULL DEFAULT (datetime('now')) | עדכון אחרון |

**מבנה `features_json`** (דוגמה):

```json
{
  "broadcast": true,
  "followup_24h": false,
  "landing_page": true,
  "calendar_sync": true,
  "scenarios_max": null
}
```

ערכים אפשריים:
- `true` / `false` — דריסה ידנית של ברירת המחדל
- חסר מהמפה — נופל לברירת המחדל של החבילה (מוגדר ב-`plans_config.py`, ראה סעיף 2.1)
- `null` — מציין "ללא הגבלה" עבור פיצ'רים מספריים

**למה JSON ולא עמודות בוליאניות נפרדות?**
הגמישות שביקשת — להוסיף פיצ'רים בעתיד בלי מיגרציה. החיסרון: אין אינדקס/CHECK ב-DB. כיוון שזו טבלת singleton (שורה אחת), אין בעיית ביצועים, וה-validation יעשה ברמת קוד ב-`feature_flags.py` (ראה 2.2).

### 1.2 טבלה חדשה: `plan_history` (אופציונלי, מומלץ)

לתיעוד שינויי חבילה (audit). חשוב מאוד עבור חיובים ומחלוקות עם לקוחות.

| שדה | טיפוס |
|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT |
| `changed_at` | TEXT NOT NULL DEFAULT (datetime('now')) |
| `previous_plan` | TEXT |
| `new_plan` | TEXT NOT NULL |
| `previous_features_json` | TEXT |
| `new_features_json` | TEXT |
| `reason` | TEXT — סיבת השינוי (שדרוג, downgrade, חסד וכו') |

### 1.3 טבלת `users` — אין שינוי

**חשוב**: לא מוסיפים `plan` או `features_json` ל-`users` (`database.py:197-204`). טבלת `users` מתעדת את **לקוחות הקצה** של העסק (מי שמדבר עם הבוט), והחבילה לא רלוונטית להם.

### 1.4 מיגרציה

ב-`migrations.py`, להוסיף פונקציה `_migrate_subscription_table(conn)`:

1. אם הטבלה `subscription` קיימת — לדלג.
2. אם לא — `CREATE TABLE` + `INSERT OR IGNORE` של שורה יחידה עם `plan='basic'`, `features_json='{}'` (ברירת מחדל בטוחה לכל לקוח קיים).
3. לקוחות קיימים ימצאו את עצמם ב-`basic` כברירת מחדל. לאחר deploy, **המפתח יעדכן ידנית** את החבילה הנכונה דרך הפאנל.

הקריאה ל-migration תיכנס לתוך `init_db()` ב-`database.py` (סביב שורה 43+) או ב-`migrations.py` בסגנון הקיים (`_ensure_column` ב-`migrations.py:14`).

### 1.5 עדכון `docs/privacy_data_matrix.md`

לפי כלל הפיתוח ב-`CLAUDE.md` (סעיף "DB — אילוצים מהרגע הראשון" + "תיקון 13"), כל טבלה חדשה חייבת שורה במטריצה.

הטבלאות החדשות:
- `subscription` — **ללא PII של לקוחות קצה**. נכנסת לקטגוריית "טבלאות ללא PII / קונפיגורציה". שיקול אבטחה: אין פה תוכן רגיש, אין סודות, התוכן לא מועבר ל-LLM.
- `plan_history` — אותו דין.

---

## 2. שינויים ב-Backend (Python)

### 2.1 קובץ קונפיגורציה חדש: `plans_config.py` (בשורש)

מקום מרכזי לכל ההגדרות של 3 החבילות. דומה בסגנונו ל-`followup_config.py` הקיים. גם wrapper ב-`ai_chatbot/plans_config.py` (לפי הנחיית הארכיטקטורה ב-`CLAUDE.md`).

**מבנה מוצע**:

```python
# הגדרת תבניות החבילות — ברירות מחדל בלבד, ניתן לדרוס פר לקוח דרך features_json
PLANS = {
    "basic": {
        "display_name": "בסיסי",
        "channel": "telegram",
        "grace_period_days": 15,
        "features": {
            "calendar_sync": True,
            "followup_24h": False,
            "broadcast": False,
            "landing_page": False,
            "scenarios_max": None,  # None = ללא הגבלה (בפועל)
        },
    },
    "advanced": {
        "display_name": "מתקדם",
        "channel": "whatsapp",
        "grace_period_days": 15,
        "features": {
            "calendar_sync": True,
            "followup_24h": True,
            "broadcast": False,
            "landing_page": False,
            "scenarios_max": None,
        },
    },
    "premium": {
        "display_name": "כוללת",
        "channel": "whatsapp",
        "grace_period_days": 30,
        "features": {
            "calendar_sync": True,
            "followup_24h": True,
            "broadcast": True,
            "landing_page": True,
            "scenarios_max": 5,  # 5 לפרסום השיווקי בלבד; אכיפה — ראה סעיף 5
        },
    },
}

# רשימת כל הפיצ'רים האפשריים — לבדיקת תקינות features_json
ALL_FEATURES = {"calendar_sync", "followup_24h", "broadcast", "landing_page", "scenarios_max"}
```

**יתרון מבנה זה**: הוספת חבילה רביעית בעתיד = הוספת מפתח חדש למילון, ללא שינוי קוד עזר.

### 2.2 מודול עזר חדש: `feature_flags.py` (בשורש)

נקודה מרכזית יחידה (single source of truth) לכל בדיקות הפיצ'רים. גם wrapper ב-`ai_chatbot/`.

**API מוצע**:

```python
def get_current_plan() -> str: ...
def get_plan_config() -> dict: ...
def has_feature(feature_name: str) -> bool: ...
def get_feature_value(feature_name: str, default=None): ...
def is_in_grace_period() -> bool: ...
def days_remaining_in_grace() -> int: ...
def set_plan(plan: str, reason: str = "") -> None: ...
def override_feature(feature_name: str, value) -> None: ...
def reset_feature_to_plan_default(feature_name: str) -> None: ...
```

**לוגיקת `has_feature(name)`**:
1. קוראים את שורת `subscription` (cache קצר ב-memory, invalidation ב-`set_plan`/`override_feature`).
2. בודקים אם `name` מופיע ב-`features_json`. אם כן — מחזירים את הערך הזה (override).
3. אחרת — מחזירים את ברירת המחדל מ-`PLANS[plan]["features"][name]`.
4. אם הפיצ'ר לא קיים בכלל — מחזירים `False` + לוג warning (לא exception, לפי הכלל "Exceptions — תמיד לרשום ללוג").

**שאלה פתוחה**: האם לשמור cache בזיכרון? יתרון: ביצועים. חיסרון: דורש invalidation ב-multi-process (gunicorn workers). הצעה: cache עם TTL של 30 שניות, או בלי cache בכלל — שאילתת SELECT על שורה אחת זולה.

### 2.3 מקומות אכיפה ב-Backend

טבלת אכיפה — היכן `has_feature(...)` נכנס:

| פיצ'ר | מקום הקריאה הקיים | מקום בדיקה מומלץ |
|---|---|---|
| `broadcast` | `admin/app.py:2226` (`POST /broadcast/send`), `admin/app.py:2150+` (`/broadcast/*`), `broadcast_service.py:50` (`send_broadcast`), `broadcast_service.py:181` (`start_broadcast_task`), broadcast_scheduler | בכניסה לכל route ב-`admin/app.py` + בכניסה ל-`send_broadcast()` עצמו (defense in depth) |
| `followup_24h` | `followup_service.py:112` (`analyze_lead`), `process_pending_followups`, `admin/app.py:2127` (`/followups`) | בתחילת `analyze_lead()` ובתחילת `process_pending_followups()` — אם כבוי, return early עם לוג |
| `landing_page` | `admin/app.py:584-612` (`/p/<page_id>`), `messaging/whatsapp_webhook.py:_send_as_page` | **ראה אזהרה למטה** ⚠️ |
| `calendar_sync` | `google_calendar.py`, `admin/app.py` (routes של `/google-calendar`) | בכניסה ל-routes הניהוליים בלבד; sync שכבר רץ בפועל לא נחסם באמצע |

**⚠️ הבחנה קריטית לגבי `landing_page`** (אושר):

הראוט `/p/<page_id>` משמש גם כ-fallback ל-WhatsApp כשהודעה ארוכה מ-1600 תווים (`CLAUDE.md` סעיף "WhatsApp — תקרת אורך הודעה"). זה safety net תשתיתי שתמיד פעיל, ללא קשר לחבילה.

**הפיצ'ר Premium "דף נחיתה"** = דפים שיווקיים שהמפתח יוצר ללקוח כחלק מההקמה: URL מותאם, עיצוב, טופס לידים. **לא** קשור ל-fallback.

**מימוש**:
- הוספת עמודה `page_type` ב-`response_pages` (`database.py:374-383`) עם ערכים `'whatsapp_fallback'` (ברירת מחדל למיגרציה של נתונים קיימים) או `'landing'`.
- בדיקת `has_feature("landing_page")` תופעל **רק** ביצירה של `page_type='landing'` — דרך route חדש בפאנל.
- ה-fallback של WhatsApp ב-`messaging/whatsapp_webhook.py:_send_as_page` ימשיך ליצור רשומות עם `page_type='whatsapp_fallback'` ללא בדיקת חבילה.
- ה-route הציבורי `/p/<page_id>` (`admin/app.py:584-612`) לא נחסם — הוא רק מציג את מה שכבר קיים.
- עדכון `docs/privacy_data_matrix.md` עבור השינוי בטבלה `response_pages`.

### 2.4 בדיקת ערוץ (Telegram vs WhatsApp) + התראת מפתח (אושר)

החבילה קובעת ערוץ אבל **לא** אוכפת אותו ברמת קוד. הסיבה: בחירת הערוץ בפועל היא env-var ב-startup (`messaging/telegram_adapter.py` או `whatsapp_adapter.py`). אם זוהה mismatch — זה לא ייחסם, אבל **נשלחת התראה אקטיבית למפתח** כדי שלא נפספס.

**מנגנון**:

1. ב-startup ב-`main.py` (סביב שורות 131-212), לאחר טעינת ה-subscription וזיהוי הערוץ הפעיל:
   ```
   plan = get_current_plan()
   active_channel = detect_active_channel()  # מבוסס ENV / מצב adapter
   expected_channel = PLANS[plan]["channel"]
   if active_channel != expected_channel:
       _notify_developer_mismatch(plan, active_channel, expected_channel)
       logger.warning(...)
   ```

2. **לוג warning** — קריטי לתיעוד היסטורי, אבל לא מספיק.

3. **התראת טלגרם למפתח**:
   - משתנה env חדש: `DEVELOPER_TELEGRAM_CHAT_ID` — chat_id של המפתח. אם לא מוגדר, ההתראה מדולגת בשקט (לוג בלבד).
   - שליחה דרך `Bot(token=TELEGRAM_BOT_TOKEN)` — אותו טוקן של הבוט הקיים. **חשוב**: ה-bot הסטנדאלוני דורש `await bot.initialize()` לפני שימוש ו-`await bot.shutdown()` בסיום (לפי כלל "asyncio — ניהול lifecycle" ב-`CLAUDE.md`).
   - תוכן ההודעה (HTML פשוט):
     ```
     ⚠️ Channel mismatch detected on startup
     Deployment: <שם הפריסה>
     Configured plan: <plan> (expected channel: <expected>)
     Actual channel: <active>
     Action: בדוק שה-plan ב-/dev/subscription מתאים, או עדכן את ה-env vars של הערוץ.
     ```
   - **שם הפריסה**: עדיפות ראשונה ל-env var חדש `DEPLOYMENT_NAME`. אם לא קיים — fallback ל-`BUSINESS_NAME` (`config.py:114`). אם גם זה לא — `RENDER_SERVICE_NAME` (Render מספק אוטומטית) או `HOSTNAME`.

4. **טיפול בכשלים בשליחת ההתראה**: שליחת ההתראה עטופה ב-`try/except` עם `logger.error` — כשל בהתראה לא יחסום הפעלת הבוט (defensive). לא מתאים `except: pass` (לפי הכלל ב-`CLAUDE.md` סעיף "Exceptions").

5. **תדירות**: רק ב-startup. אין polling. אם ה-mismatch ממשיך — ההתראה תישלח שוב רק בהפעלה הבאה.

**משתני env חדשים** (יתועדו ב-`.env.example` ובצ'ק ליסט הלקוח `docs/client_checklist.md`):
- `DEVELOPER_TELEGRAM_CHAT_ID` — chat_id למפתח להתראות mismatch
- `DEPLOYMENT_NAME` (אופציונלי) — שם פריסה זיהוי בהתראה
- `DEVELOPER_PASSWORD` — ראה סעיף 3.5

### 2.5 דקורטור חדש לroutes באדמין: `@require_feature(name)`

לקלות שימוש בקוד הפאנל:

```python
@app.route("/broadcast/send", methods=["POST"])
@require_login
@require_feature("broadcast")
def broadcast_send():
    ...
```

ההתנהגות אם הפיצ'ר כבוי:
- בקשת HTML רגילה → redirect ל-`/upgrade?feature=broadcast` עם הסבר
- בקשת HTMX (`HX-Request: true`) → להחזיר 403 עם partial של מודאל "שדרג"
- בקשת JSON/API → 403 + `{"error": "feature_not_available", "feature": "broadcast", "plan": "basic"}`

---

## 3. שינויים ב-Frontend (admin/templates/)

### 3.1 רכיב Jinja משותף: macro `feature_lock`

קובץ חדש: `admin/templates/_macros/feature_lock.html`

```jinja
{% macro feature_button(feature, label, url, icon='') %}
  {% if has_feature(feature) %}
    <a href="{{ url }}" class="btn btn-primary">{{ icon }} {{ label }}</a>
  {% else %}
    <button class="btn btn-disabled" data-locked-feature="{{ feature }}"
            onclick="showUpgradeModal('{{ feature }}')">
      🔒 {{ label }}
    </button>
  {% endif %}
{% endmacro %}
```

ה-context processor של Flask ידאג שש-`has_feature` יהיה זמין בכל template (ב-`admin/app.py`, סביב מקום רישום הפילטרים `il_datetime`).

### 3.2 הצגת חסימה בפועל

**עיקרון**: לעולם לא להסתיר את הפיצ'ר לחלוטין — תמיד להציג אותו עם מנעול. זה **שיווקי** (היוזר רואה מה הוא מפסיד) ו**עקבי** (אותו לעיצוב UI).

מסכים בפאנל שדורשים שינוי:

| מסך | נתיב | פיצ'ר | פעולה |
|---|---|---|---|
| Dashboard | `/` | broadcast, followup_24h, landing_page | "Cards" של פיצ'רים נעולים מסומנים |
| Broadcast list | `/broadcast` | broadcast | כפתור "צור broadcast" עם מנעול אם כבוי |
| Broadcast send | `/broadcast/send` | broadcast | חסימת UI + חסימת backend |
| Broadcast campaigns | `/broadcast/campaigns/*` | broadcast | חסימה |
| Followups queue | `/followups` (`admin/app.py:2127`) | followup_24h | banner "פיצ'ר זה כבוי בחבילה שלך" |
| Landing pages list | חדש או חלק מ-`/kb` | landing_page | חסימה |

### 3.3 מודאל "שדרג"

קובץ: `admin/templates/_partials/upgrade_modal.html`

תוכן: מציג את הפיצ'ר המבוקש, החבילה הנוכחית, החבילה המינימלית הנדרשת, ופרטי קשר ליצירת קשר עם המפתח (כי המפתח הוא מי שמשנה חבילה — ראה 3.5).

### 3.4 מסך "החבילה שלי" (לבעל העסק)

נתיב חדש: `/my-plan` (כל admin רגיל יכול לגשת).

תוכן:
- שם החבילה, ערוץ
- רשימת הפיצ'רים הפעילים / הלא פעילים (עם סימון ברור)
- תאריך תחילת החבילה
- תקופת חסד (כמה ימים נותרו, אם רלוונטי)
- כפתור "ליצירת קשר לשדרוג" (פותח email/whatsapp/טופס)

**הערה**: למסך זה אין כפתורים שמשנים חבילה — הבעלים לא משנים בעצמם.

### 3.5 מסך מפתח: `/dev/subscription`

נתיב חדש, מוגן בנפרד מהאדמין הרגיל.

**אבטחה — אפשרות ב' (אושר)**:

env var חדש `DEVELOPER_PASSWORD` שדורש לוגין נוסף ספציפי ל-routes תחת `/dev/*`. נפרד מ-`ADMIN_USERNAME`/`ADMIN_PASSWORD` הקיים (`config.py:99-103`).

**מימוש**:
- middleware/decorator חדש `@require_developer` — בודק session flag נפרד `dev_authenticated`.
- אם לא מאומת: redirect ל-`/dev/login` עם טופס סיסמה (POST → השוואה מול `DEVELOPER_PASSWORD`, set session flag).
- אם `DEVELOPER_PASSWORD` ריק/לא מוגדר → המסך **לא נגיש בכלל** (return 404), כדי לא לחשוף קיום של נתיב dev בפריסות שלא הגדירו סיסמה.
- session timeout נפרד וקצר יותר (למשל 30 דקות) משל admin רגיל.
- **לא** דרך header (אם הוצעו): טופס login רגיל — עקבי עם הפאנל הקיים, פשוט יותר לדפדפן.

תוכן המסך:
- בחירת חבילה (radio: basic/advanced/premium)
- כפתור "החל חבילה" — מבצע `set_plan()` ומעדכן `plan_started_at`
- טבלת feature flags (כל פיצ'ר אפשרי, עם 3 מצבים: ברירת מחדל מהחבילה / מופעל ידנית / מבוטל ידנית)
- שדה "סיבת שינוי" → נשמר ב-`plan_history.reason`
- היסטוריית שינויים (מ-`plan_history`)

---

## 4. אכיפה דו-שכבתית

### 4.1 מודל האיומים

ההנחה: **בעל העסק (admin רגיל)** הוא האקטור היחיד; הוא לא "Attacker" אלא משתמש שמנסה למקסם את מה שיש לו. אבל יש שלוש דרכים שבהן הוא יכול לעקוף UI:

1. שינוי כתובת URL ידני (`/broadcast/send` ב-POST דרך curl)
2. הזרקת בקשת HTMX ידנית
3. שימוש ב-API endpoints שלא חסומים ב-UI

### 4.2 שכבה 1 — Frontend (UX, לא אבטחה)

- macros עם מנעול (3.1)
- הסתרת/הצגת תפריטים בנאבים
- מודאלים מסבירים

המטרה: שהמשתמש **יבין** מה זמין לו. אין הסתמכות על זה לאכיפה.

### 4.3 שכבה 2 — Backend (אבטחה אמיתית)

- כל route ב-`admin/app.py` שמשרת פיצ'ר חבילה — מקבל `@require_feature(...)` (2.5)
- כל פונקציית שירות שמבצעת את הפיצ'ר (`broadcast_service.send_broadcast`, `followup_service.analyze_lead`) — בודקת `has_feature()` בתחילתה. אם כבוי → לוג warning + return early.
- בדיקה כפולה (defense in depth) — גם אם ה-route הוסיף בעתיד caller שלא עבר דרך הדקורטור.

### 4.4 בדיקות אבטחה ידניות לכל פיצ'ר

לכל פיצ'ר חסום, להריץ ידנית:
- `curl -X POST .../<endpoint>` עם cookie של admin רגיל בחבילה בסיסי → לוודא 403/redirect
- שליחת בקשת HTMX אל endpoint חסום → לוודא partial של 403
- בדיקת ה-callable הפנימי (`broadcast_scheduler`) — האם הוא קורא ל-DB ובודק חבילה?

---

## 5. מערכת תרחישים (Scenarios)

### 5.1 מצב נוכחי בקוד

המילה "תרחיש" בקוד הקיים = **תרחיש בדיקה** (test scenario, design scenario), לא ישות runtime. דוגמאות:
- `CLAUDE.md` סעיף "לוגיקת זמן — טבלת תרחישים"
- `core/booking_decision.py:9` — "ראה טבלת התרחישים ב-PR description"

**אין מערכת runtime של "תרחישים" שניתן להפעיל/לכבות**.

### 5.2 הגדרה מוצעת

לפי האפיון שלך: **תרחיש = מודול שניתן להפעיל/לכבות פר לקוח**.

תרחיש בעצם = קבוצת לוגיקה עסקית. דוגמאות:
- "יומן" — אינטגרציית Google Calendar
- "פולואפ 24h"
- "Broadcast"
- "הזמנת תור" (booking)
- "שיחת אדם" (talk_to_agent)

זה **חופף 100%** למושג Feature Flags. ההצעה: **לא להפריד** בין "תרחישים" ל-"פיצ'רים" — אלה אותו דבר ברמת המימוש, ושמות שונים בשיווק.

### 5.3 הגבלת "5 תרחישים" בחבילת Premium

הטבלה אומרת: "עד 5 תרחישים (מפורסם)". המילה "מפורסם" קריטית:
- **שיווקית**: בדף הנחיתה כתוב "עד 5 תרחישים".
- **בפועל**: אין הגבלה אכיפה — אי אפשר לחסום אצל לקוח שכבר משתמש ביותר.

**הצעה**: השדה `scenarios_max` (סעיף 2.1) הוא **מטא-נתון מוצג** בלבד. לא נכנס ללוגיקת חסימה. כשנוסיף בעתיד "תרחישים מותאמים" אמיתיים (custom scenarios שהבעלים מגדיר), אז נחזור לשדה הזה ונאכוף אותו.

### 5.4 רישום תרחיש חדש בעתיד

נוהל מוצע (לתעד ב-`CLAUDE.md` בסבב הבא):

1. הוספת המפתח ל-`ALL_FEATURES` ב-`plans_config.py`.
2. הוספת ערך ב-`features` של כל אחת מ-3 החבילות (ברירת מחדל).
3. עיטוף נקודת הכניסה לקוד התרחיש ב-`if not has_feature("..."): return`.
4. עיטוף ה-route המתאים ב-`@require_feature("...")`.
5. הוספת כפתור/קישור עם macro `feature_button` ב-template.
6. בדיקה ב-`/dev/subscription` שניתן לדרוס ידנית.

---

## 6. תקופת חסד לשינויים

### 6.1 לוגיקה

- שדה `plan_started_at` נכתב ב-`set_plan()` (גם בחבילה ראשונה וגם בכל שדרוג).
- `grace_period_days` נשלף מהחבילה (מ-`plans_config.PLANS[plan]["grace_period_days"]`), אבל ניתן לדרוס ידנית בשדה ב-`subscription`.
- חישוב: `is_in_grace_period()` = `(now - plan_started_at) < grace_period_days`.

### 6.2 משמעות "חסד" (אושר)

ה"חסד" הוא **אדמיניסטרטיבי בלבד** — נוגע לעבודה ידנית של המפתח לבקשת הלקוח (שינויי תוכן, הוספת FAQ, שינוי תרחיש קיים, וכו'). **לא משפיע על קוד הבוט** — הבוט פועל זהה בכל מצב.

לפיכך, **אין endpoint שדורש בדיקת `is_in_grace_period`**. זה רק תאריך שמוצג בפאנל.

### 6.3 אינדיקציה בפאנל

ב-`/dev/subscription`:
- שורה תמיד מוצגת: `plan_started_at`, `grace_period_days`, תאריך סיום חסד מחושב, `is_in_grace_period` (כן/לא), `days_remaining_in_grace`.

ב-פאנל הראשי (לבעל העסק) — **banner אינפורמטיבי גלובלי** (לא רק במסך `/my-plan`):
- אם בחסד: לא להציג כלום (לא להציק).
- אם נותרו 3 ימים או פחות: banner צהוב "תקופת החסד מסתיימת בעוד X ימים — שינויים שתבקש מעבר לתאריך זה יחויבו 200 ₪/שעה".
- אם הסתיים: banner אפור עדין "תקופת החסד הסתיימה — שינויים יחויבו 200 ₪/שעה. ליצירת קשר: ...".
- מיקום ה-banner: בתבנית base/layout משותפת של האדמין, מעל התוכן הראשי. ב-`/my-plan` (3.4) — תמיד להציג את הסטטוס המלא.

### 6.4 שדרוג / downgrade ↔ איפוס חסד (אושר)

- **שדרוג** (basic→advanced, advanced→premium, basic→premium): `plan_started_at = now()` — חסד מתאפס.
- **Downgrade** (premium→advanced, advanced→basic): `plan_started_at` נשמר על המקורי; `grace_period_days` משתנה לפי החבילה החדשה (פחות).
- **שינוי באותה חבילה** (רק override של feature flags): `plan_started_at` לא משתנה.

הלוגיקה תיושם ב-`set_plan(new_plan, ...)` ב-`feature_flags.py`, ותועד ב-`plan_history` עם `reason` שכולל סוג השינוי (`upgrade` / `downgrade` / `override_only`).

---

## 7. שעות פעילות לתמיכה

### 7.1 ניתוח

הטבלה השוואתית אומרת "תמיכה: שעות הפעילות שלנו" — זה אומר שאני (המפתח) זמין לתמיכה רק בשעות מסוימות. **זה לא משפיע על קוד הבוט**, כי הבוט פועל 24/7 ללא קשר.

### 7.2 הבחנה חשובה

יש בקוד `business_hours` (`admin/app.py:1389`, `business_hours.py`) — **אבל זה שעות הפעילות של בעל העסק**, לא של התמיכה שלי.

**הצעה**: לא להוסיף קוד חדש לכך. זה כתב על דף הנחיתה השיווקי של ה-SaaS שלי, לא חלק מהמוצר.

**אם** רוצים בעתיד להוסיף "התראות לבעל העסק רק בשעות התמיכה שלי" — זה פיצ'ר נפרד שלא קשור לחבילות.

---

## 8. סיכום סדר ביצוע מומלץ

### שלב 1 — תשתית (סיכון נמוך, לא משנה התנהגות קיימת)

1. יצירת `plans_config.py` + wrapper ב-`ai_chatbot/`
2. יצירת `feature_flags.py` עם `has_feature`, `get_current_plan` (ללא `set_plan` עדיין)
3. הוספת טבלאות `subscription` + `plan_history` ב-`migrations.py` (עם seed: `plan='basic'`, features ריק)
4. עדכון `docs/privacy_data_matrix.md` באותו commit
5. רישום `has_feature` כ-context processor ב-`admin/app.py`

**בנקודה זו**: הקוד עדיין לא חוסם כלום. כל הלקוחות הקיימים = `basic`, אבל אף route לא בודק. שום שבירה אפשרית.

### שלב 2 — מסך מפתח (גישה למפתח בלבד)

6. יצירת `/dev/subscription` עם auth נפרד
7. מימוש `set_plan()`, `override_feature()` עם כתיבה ל-`plan_history`
8. בדיקה ידנית: שינוי חבילה דרך ה-UI עובד, נשמר ב-DB

**בנקודה זו**: אני יכול לעדכן ידנית את החבילה של כל לקוח שכבר רץ. עדיין לא חוסם כלום בקוד הקיים.

### שלב 3 — אכיפה ב-Backend (סיכון בינוני — עלול לחסום פיצ'רים אצל לקוחות קיימים אם החבילה שלהם הוגדרה לא נכון)

9. הוספת `@require_feature("broadcast")` ל-routes ב-`admin/app.py:2150+` ו-`admin/app.py:2226`
10. בדיקה בתחילת `broadcast_service.send_broadcast()` ב-`broadcast_service.py:50`
11. הוספת `has_feature("followup_24h")` בתחילת `followup_service.analyze_lead()`
12. בדיקת startup ב-`main.py` עבור channel mismatch — לוג + **התראת טלגרם למפתח** (סעיף 2.4)
13. הוספת עמודה `page_type` ב-`response_pages` + הבחנה ב-`messaging/whatsapp_webhook.py` בין fallback ל-landing

**אזהרה**: לפני deploy של שלב זה — לוודא ב-DB שכל לקוח קיים מיוחס לחבילה הנכונה (דרך `/dev/subscription`). אחרת, ביום ה-deploy, לקוח שמשתמש ב-broadcast ייחסם.

### שלב 4 — חסימת UI

14. macro `feature_lock` ב-templates
15. החלפת כפתורים רלוונטיים ב-templates של broadcast/followup
16. מודאל "שדרג"

### שלב 5 — מסך "החבילה שלי" + תקופת חסד + Banner גלובלי

17. יצירת `/my-plan`
18. לוגיקת `is_in_grace_period`, `days_remaining_in_grace`
19. הצגת badge מלא ב-`/my-plan` + banner גלובלי בlayout הראשי (3 ימים לסיום / לאחר סיום)
20. עדכון `docs/client_checklist.md` עם משתני env חדשים (`DEVELOPER_PASSWORD`, `DEVELOPER_TELEGRAM_CHAT_ID`, `DEPLOYMENT_NAME`)

### שלב 6 — נושאים שאושרו (לתיעוד בלבד)

כל ששת הנושאים שהיו פתוחים אושרו (ראה סעיף 9). אין החלטות נוספות פתוחות לפני התחלת מימוש.

### דברים שאפשר לדחות לסבב הבא

- מערכת רב-טננטית אמיתית (לא נדרש כרגע — חבילה אחת לפריסה)
- אכיפה אמיתית של `scenarios_max` (5.3 — כיום שיווקי בלבד)
- audit log מפורט יותר (לחבר את `plan_history` למסך אדמין)
- self-service שדרוג (כפתור שלוקח ישר לתשלום) — דורש אינטגרציית תשלומים
- קונפיגורציית שעות תמיכה (7.2 — מחוץ לסקופ הפיצ'ר)

---

## 9. החלטות שאושרו (סיכום)

1. **`landing_page` כפיצ'ר**: דפי נחיתה שיווקיים שהמפתח יוצר בהקמה (URL מותאם, עיצוב, טופס לידים). שונים מ-fallback של WhatsApp. **מימוש**: הוספת `page_type` ב-`response_pages`. ← *סעיף 2.3*
2. **חסד לשינויים**: אדמיניסטרטיבי בלבד (עבודה ידנית של המפתח), לא משפיע על קוד הבוט. תאריך מוצג ב-`/dev/subscription` + banner אינפורמטיבי בפאנל הראשי כשנגמר. ← *סעיף 6.2-6.3*
3. **שדרוג ↔ איפוס חסד**: שדרוג מאפס `plan_started_at`, downgrade שומר. ← *סעיף 6.4*
4. **Auth למסך מפתח**: env var `DEVELOPER_PASSWORD` + טופס login נפרד ב-`/dev/login`. אם לא מוגדר → 404. ← *סעיף 3.5*
5. **`scenarios_max`**: מטא-נתון להצגה בלבד. אכיפה אמיתית רק כשנבנה custom scenarios בעתיד. ← *סעיף 5.3*
6. **Cache של `has_feature`**: ללא cache בסבב הראשון. נוסיף רק אם נראה איטיות בפרודקשן. ← *סעיף 2.2*
7. **Channel mismatch**: לוג warning **+ התראת טלגרם** למפתח ב-startup (env vars: `DEVELOPER_TELEGRAM_CHAT_ID`, `DEPLOYMENT_NAME`). ← *סעיף 2.4*

## 10. סיכום משתני env חדשים

| משתנה | חובה? | תיאור |
|---|---|---|
| `DEVELOPER_PASSWORD` | אופציונלי (אם חסר → `/dev/*` לא נגיש) | סיסמה ל-`/dev/subscription` |
| `DEVELOPER_TELEGRAM_CHAT_ID` | אופציונלי (אם חסר → רק לוג, ללא התראה) | chat_id למפתח להתראות mismatch |
| `DEPLOYMENT_NAME` | אופציונלי | שם הפריסה להצגה בהתראה (fallback: `BUSINESS_NAME`/`RENDER_SERVICE_NAME`/`HOSTNAME`) |

יתועדו ב-`.env.example` וב-`docs/client_checklist.md`.

---

**סיום מסמך**. כל הנושאים אושרו — מוכן למעבר למימוש לפי סדר השלבים בסעיף 8.
