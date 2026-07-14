# Customer Memory System

מערכת זיכרון מתמשך פר-לקוח-קצה. בסיום שיחה, LLM extractor מנתח את השיחה
ושומר עובדות יציבות; בשיחות הבאות העובדות מוזרקות ל-context של הבוט.

ראה `docs/Customer-memory/claude_code_instructions.md` למפרט המלא.

---

## חשוב — OpenAI client נפרד מהבוט הראשי

רכיב הזיכרון משתמש ב-**OpenAI אמיתי באופן בלעדי**, נפרד מ-`OPENAI_API_KEY`
ו-`OPENAI_BASE_URL` של הבוט הראשי.

### למה?

ה-spec של המערכת קבע במפורש שימוש ב-`gpt-4.1-mini`. הפרומפט תוכנן ל-
OpenAI ספציפית — ליטרליות בהוראות ושמרנות בחילוץ. מודלים אחרים (Gemini
דרך OpenAI-compat layer, או אחרים) נוטים להיות אגרסיביים יותר ולחלץ
עובדות שהפרומפט אומר במפורש לא לחלץ. ההפרדה מבטיחה שאיכות ה-extraction
לא תיפגע גם אם הבוט הראשי יעבור לספק אחר.

### ENV נדרש

| משתנה | חובה? | ברירת מחדל | תיאור |
|---|---|---|---|
| `MEMORY_OPENAI_API_KEY` | כן | — | OpenAI API key אמיתי. נפרד מ-`OPENAI_API_KEY` של הבוט. |
| `MEMORY_OPENAI_BASE_URL` | לא | `https://api.openai.com/v1` | רק לטסטים מקומיים מול mock proxy. אל תכוון לספק אחר ב-production. |

אם `MEMORY_OPENAI_API_KEY` חסר/ריק, המערכת תיכשל בעלייה עם הודעה ברורה:
`MemoryOpenAIConfigError: MEMORY_OPENAI_API_KEY is required ...`

### עלות צפויה

- ~$1-5 לחודש בקנה מידה הנוכחי (extraction לכל שיחה שהסתיימה + judge ב-eval).
- ה-eval runner: ~60 קריאות לריצה מלאה (30 cases × extractor + judge),
  עלות חד-פעמית של כמה אגורות.

---

## מבנה החבילה

```
memory/
├── README.md             ← הקובץ הזה
├── __init__.py
├── openai_client.py      ← client בלעדי (MEMORY_OPENAI_API_KEY)
├── extractor.py          ← שלב 3 — extract_facts()
├── validator.py          ← שלב 4 — validate + save + run_extraction_for_user
├── context.py            ← שלב 8 — הזרקת facts ל-LLM של הבוט (TODO)
├── background.py         ← שלב 6 — scheduler ל-extraction אוטומטי (TODO)
├── prompts/
│   └── fact_extractor.txt
├── schemas/
│   └── extractor_schema.py
└── eval/
    └── run_eval.py        ← שלב 5 — eval runner מול scorecard
```

---

## הרצת ה-eval

```bash
# הרצה מלאה (30 cases) + judge LLM:
python -m memory.eval.run_eval --report /tmp/eval_results.md

# case יחיד לדיבוג:
python -m memory.eval.run_eval --case-id pii_01

# N הראשונים בלבד:
python -m memory.eval.run_eval --limit 5
```

ה-runner יחזיר exit code 0 אם **כל** 6 המטריקות עוברות את הרפים
ב-`docs/Customer-memory/scorecard.md`. אחרת exit code 1 + רשימת
המטריקות שנכשלו.

---

## מודלים — קבועים, לא ENV

| תפקיד | מודל |
|---|---|
| Fact extractor | `gpt-4.1-mini` |
| Eval LLM judge | `gpt-4.1-mini` |
| Embeddings (pre-filter) | `text-embedding-3-small` |

המודלים מקובעים בקוד (`ai_chatbot/config.py`) ולא ב-ENV — ה-spec דורש
אותם ספציפית והפרומפט תוכנן עבורם. שינוי מודל דורש re-validation של
ה-eval לאיכות.

---

## ENV נוספים (אופציונליים)

| משתנה | ברירת מחדל | תיאור |
|---|---|---|
| `BUSINESS_ID` | `default` | single-tenant constant; forward-compat ל-multi-tenant. |
| `MEMORY_EXISTING_FACTS_CAP` | `12` | cap לפני pre-filter סמנטי. |
| `MEMORY_CONVERSATION_CAP` | `50` | cap על הודעות לכל שיחה ב-extraction (בעיות נפוצות #5 ב-spec). |
| `MEMORY_BACKGROUND_ENABLED` | `true` | האם להפעיל את ה-scheduler (שלב 6, עוד לא הוטמע). |

---

## סטיות מאושרות מה-spec

ה-spec הורה "להעתיק את הפרומפט 1:1". הסטיות הבאות אושרו במפורש לאחר
שה-eval חשף בעיות:

### action `resolve` — action רביעי (לא ב-spec המקורי, מיושם מלא)
פרומפט v2.2 הוסיף action `resolve` — סגירת `open_issue` קיים (content=null,
resolves_id מצביע ל-issue) במקום יצירת fact חדש. **מיושם end-to-end**:
- schema: action enum כולל `resolve`, content nullable, `resolves_id`.
- validator: validate_extraction (resolves_id חובה + מצביע ל-open_issue
  קיים, content=null, fact_type=open_issue, confirms/supersedes null);
  save_extractions קורא ל-`db.resolve_customer_fact` (לא יוצר שורה חדשה,
  רק מסמן את ה-issue הקיים).
- DB: status חמישי `'resolved'` ל-customer_facts (**לא ב-spec המקורי**) +
  עמודות `resolved_at`, `resolution_evidence`. נוסף דרך migration עם
  table-rebuild (migrations.py) כי ה-CHECK constraint כבר בפרודקשן.

### פרומפט ה-extractor — ריכוך דיכוי-יתר (H3)
ה-eval המלא גילה שב-3/4 cases של PII רגיש ("בהריון בחודש חמישי",
"התגרשתי לאחרונה", "סוכרת סוג 2") המודל סירב לחלץ מידע מפורש לחלוטין
עם reason "לא נאמר במפורש". חקירה הראתה שהפרומפט מוטה מבנית לדיכוי
(triad סוגר "אל תנחש / אל תחלץ ליתר ביטחון / אם יש ספק — דלג").

**הערה קריטית**: ה-prompt של ה-spec סתר את ה-eval של ה-spec — ה-eval
מצפה לחלץ את ה-PII הזה. ה-eval צודק (אלה עובדות יציבות ושימושיות).

שני שינויים ב-`prompts/fact_extractor.txt`:
1. **שער הקבלה** — תנאי הרלוונטיות מנוסח לפי משך הרלוונטיות לעסק
   (3+ חודשים) במקום "תקף מעבר לשיחה", עם דוגמת ההריון.
2. **ה-triad הסוגר** — הוחלף ב"מבחן כפול": אל תחלץ פרשנות, אבל כן חלץ
   הצהרות מפורשות על מאפיינים יציבים גם בתוך משפט מורכב.

המטרה: לרכך דיכוי-יתר בלי לאבד שמרנות (no_extraction נשאר 10/10).

---

## שלב 6 — Background extraction scheduler (מבוצע)

Thread רקע ב-`memory/background.py` שמופעל אוטומטית מ-`main.py`
(בשני המצבים — webhook ו-polling). כל 5 דקות סורק את `conversations`,
מזהה משתמשים שדיברו ב-`MEMORY_LOOKBACK_DAYS` האחרונים, ומפעיל
`run_extraction_for_user` על שיחות שהסתיימו.

### לוגיקה (`_process_due_users`)

1. `db.get_users_active_since(now - MEMORY_LOOKBACK_DAYS days)` — DISTINCT
   user_id מ-conversations.
2. לכל user_id:
   - **Idle check ראשון** (שלב 6.3): `db.get_user_last_message_time(user_id)`
     — MAX(created_at) של **כל הודעות המשתמש** (לא ה-batch). אם <
     `MEMORY_IDLE_MINUTES` → דלג (`skipped_active`). שאילתה זולה, חוסכת
     LLM call כשהשיחה פעילה. הבדיקה על כל ההודעות (לא ה-batch) חשובה כי
     ה-batch עלול להיות backlog ישן, אך השיחה הכוללת עדיין פעילה.
   - `last_id = db.get_last_extraction_message_id(user_id, BUSINESS_ID)` —
     MAX(last_message_id) מ-extraction_runs.completed (**cursor id-based**).
   - `messages = db.get_conversation_after(user_id, after_id=last_id,
     since_iso=lookback if last_id is None else None, limit=50)` —
     **ASC + LIMIT** (לא DESC). מעבדים מהישנות לחדשות.
   - אם `len(messages) < 2` → דלג (`skipped_no_new_messages`).
   - אחרת → `run_extraction_for_user(user_id, BUSINESS_ID, messages)`.
     ה-validator מחשב `max_message_id = max(m["id"] for m in messages)`
     ומעביר ל-log_extraction_run.
3. לוג cycle מסכם:
   `memory.background cycle: scanned=X extracted=Y skipped_active=Z ...`

### Discovery של משתמשים נטושים (שלב 6.4)

ה-scheduler מחפש משתמשים מ-2 מקורות (UNION) דרך
`db.get_users_with_pending_messages`:

- **Backlog**: יש `extraction_runs.completed` עם `last_message_id`,
  ויש `conversations.id > last_message_id`. ה-cursor הוא id
  (monotonic), לא תאריך — backlog נשאר זמין לנצח.
- **New users**: אין run קודם, ויש הודעות ב-`MEMORY_LOOKBACK_DAYS`
  האחרונים. cap סביר כדי לא לסרוק את כל ההיסטוריה.

דוגמה: משתמש שולח 80 הודעות → cycle 1 מעבד 50 (id 1-50). המשתמש
נעלם 8 ימים. cycle 2 (אחרי 8 ימים) **עדיין רואה אותו** דרך backlog
ומעבד 51-80. לפני שלב 6.4 (`get_users_active_since` שסינן רק לפי
`created_at`), המשתמש היה נופל מהרשימה אחרי 7 ימים וה-30 הנותרות
היו נעלמות לעד.

### Backlog > cap (שלב 6.3)

אם משתמש שולח 80 הודעות בבת אחת ו-cap=50:
- **לפני התיקון** (DESC + LIMIT): cycle 1 שלף את ה-50 ה**אחרונות** (ids
  גבוהים), שמר MAX → cycle 2 חיפש `id > MAX` ולא מצא דבר. 30 ההודעות
  הראשונות (ids נמוכים) **נעלמו לעד**.
- **אחרי התיקון** (ASC + LIMIT): cycle 1 מעבד את 50 הראשונות, שומר
  MAX. cycle 2 ממשיך מ-`id > MAX` ומעבד את ה-30 הנותרות. אחרי שני
  cycles הכל מעובד.

### Idle check על כל ההודעות (שלב 6.3)

ה-idle check **לא** מבוסס יותר על `messages[-1].created_at` של ה-batch.
הסיבה: ה-batch הוא ASC, כך ש-`messages[-1]` הוא ה-50th-oldest מתוך
backlog ארוך — נראה "ישן" אבל למעשה השיחה עדיין פעילה. הבדיקה הנכונה:
MAX(created_at) של כל הודעות המשתמש ב-DB.

### Cursor id-based (שלב 6.2)

הסיבה: cursor מבוסס timestamp מפספס הודעות באותה שנייה כש-cap חתך
באמצע. cursor מבוסס id (monotonic + unique + atomic) פותר את זה לכל
הוריאציות.

- ה-cursor הוא **`conversations.id`** (INTEGER PRIMARY KEY AUTOINCREMENT,
  global monotonic).
- **גם cap וגם cursor על אותה מטריקה** (id) — אין סדק אפשרי.
- fallback ל-`since_iso` (created_at) רק כש-`last_id is None` — קורה
  פעם אחת למשתמש חדש או runs ישנים בלי last_message_id.

### log_extraction_run failure (שלב 6.2)

אם `save_extractions` עבר ו-`log_extraction_run` נכשל (DB locked,
disk full):
- ה-facts ב-DB ✅.
- אין שורה ב-extraction_runs → cursor לא מתקדם.
- `run_extraction_for_user` מחזיר **`status='failed'`** → ה-scheduler
  לא יספור כ-extracted (`counts["errors"] += 1`).
- בסבב הבא: אותן הודעות, ה-LLM ייצור facts (דומים), `_is_active_dup`
  + UNIQUE partial index ימנעו duplicates.
- עלות: קריאת LLM אחת מיותרת ב-edge case נדיר. אין פגיעה בעקביות.

### Lock פנימי

`_in_progress: set[str]` ברמת מודול עם `_lock` מונע double-extraction
על אותו user_id באותו cycle. process-local — מספיק כי deployment הוא
single-process. ל-multi-worker עתידי: להחליף ב-DB lock.

### ENV vars

```bash
MEMORY_BACKGROUND_ENABLED=true   # default. false → scheduler לא מופעל.
MEMORY_IDLE_MINUTES=30           # default. סף "שיחה נגמרה".
MEMORY_LOOKBACK_DAYS=7           # default. חלון סריקה לאחור.
MEMORY_CONVERSATION_CAP=50       # default (קיים). cap הודעות לסבב.
```

הכיבוי דרך `MEMORY_BACKGROUND_ENABLED=false` לא עוצר את הקריאה
מ-`main.py` — `start_scheduler()` בעצמו מחזיר False וה-thread לא נוצר.

### בדיקה ב-Render

לוגים שיאשרו ש-scheduler פעיל:
1. הפעלה: `memory.background: scheduler started (poll=300s, idle=30min, lookback=7d)`.
2. כל 5 דקות: `memory.background cycle: scanned=N extracted=M ...`.
3. אחרי 30 דקות שקט אצל משתמש פעיל: `extracted` עולה ב-1 ו-
   `customer_facts` שלו מתעדכן.

תשאול לבדיקת מצב אחרי הפעלה:
```sql
SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 5;
SELECT user_id, COUNT(*) FROM customer_facts GROUP BY user_id;
```

---

## שלב 7 — פאנל admin לניהול facts (מבוצע)

3 מסכים חדשים בפאנל לבעל העסק:

### 1. `/pending-facts` — תור אישור עובדות

מציג את כל ה-facts במצב `pending_approval` בעסק. fact נכנס למצב זה כש-
`requires_consent=true` (PII רגיש — בריאות/פיננסי/משפחתי/דתי/מיני) או
כש-`0.60 ≤ confidence < 0.85` (`memory/validator.py:_determine_status`).

**מתג "אישור אוטומטי" (`memory_auto_approve` ב-`bot_settings`, פר-עסק,
ברירת מחדל כבוי):** כשדלוק, facts לא-רגישים בביטחון בינוני
(`0.60 ≤ confidence < 0.85`) עוברים ישר ל-`active` בלי להמתין בתור.
מידע רגיש (`requires_consent=true`) **תמיד** נשאר `pending_approval` —
שער הפרטיות אינו נעקף (`_determine_status` מחזיר `pending_approval`
עבור requires_consent לפני שהוא בכלל בודק את `auto_approve`).

**פעולות:**
- "אשר" → `status='active'` (ה-fact מוזרק לבוט בשיחות הבאות).
- "דחה" → `status='rejected'` (לא מוזרק; נשמר ל-audit).
- "אשר הכל" → bulk UPDATE (טרנזקציה אחת, ללא race conditions).
- מתג "אישור אוטומטי" → `POST /pending-facts/settings` (מעדכן
  `memory_auto_approve`). משפיע על חילוצים **עתידיים** בלבד.

Auto-refresh כל 15 שניות דרך `/api/pending-facts/rows` (HTMX). badge
בסיידבר מציג את המספר (`/api/stats` עכשיו מחזיר `pending_facts`).

### 2. `/customer-memory` — רשימת לקוחות

GROUP BY user_id מתוך `customer_facts` לכל מי שיש לו ≥1 fact ב-
`status IN ('active','pending_approval')`. מציג username (אם זמין דרך
LEFT JOIN ל-users), fact_count, last_update.

### 3. `/customer-memory/<user_id>` — פרטי לקוחה

כל ה-facts (status='all') מקובצים לפי `fact_type` ב-5 קטגוריות (סדר קבוע:
preference → personal_info → relationship → open_issue → vocabulary).
facts במצב `superseded`/`resolved`/`rejected` ברקע אפור.

**פעולות:**
- **ערוך** — שינוי content בלבד. `last_confirmed_at` ושאר השדות
  לא משתנים (זה לא confirm — זה תיקון ידני).
- **מחק** — **hard delete** מ-DB (לא soft delete ל-`rejected`). נועד
  למחיקת facts שגויים שלא היו צריכים להיווצר מלכתחילה.

תמיכה מלאה ב-BSUID (Meta WhatsApp): URL `/customer-memory/IL.abc.123`
עובר דרך `<path:user_id>` ו-`_validate_user_id` הותאם מהסבב הקודם.

### CRUD DB חדש (database.py)

- `delete_customer_fact(fact_id)` — hard delete.
- `get_pending_facts(business_id, limit=200)` — JOIN ל-users עבור username,
  מיון `created_at DESC, id DESC`.
- `get_users_with_facts(business_id)` — GROUP BY, COUNT, MAX(created_at).

`delete_user_data` כבר מטפל ב-`customer_facts` (database.py:1894), כך
שמחיקת משתמש מנקה גם facts.

---

## שלב 8 — הזרקה ל-context של הבוט (מבוצע)

`memory/context.py` שולף active facts של המשתמש ומזריק אותם ל-system
message ב-`llm.py:_build_messages` לפני `summary_section`. הבלוק:

```
תאריך נוכחי: DD/MM/YYYY

מה שאתה יודע על הלקוח:
- בהריון בחודש חמישי (מידע רגיש, נאמר 15/03/2026, ייתכן שלא רלוונטי)
- מעדיפה תורים בבוקר (נאמר 12/02/2026, אומת שוב 15/04/2026)
- אלרגית לאגוזים (מידע רגיש, נאמר 10/01/2026, ייתכן שלא רלוונטי)
- ממתינה להחזר על הזמנה 4587 (נאמר 20/05/2026)
```

לוגיקת השליפה (`get_relevant_facts_for_context`):
- רק `status='active'` (לא resolved/superseded/rejected/pending_approval).
- `preference` / `personal_info` / `relationship` / `open_issue` — תמיד.
- `vocabulary` — רק כש-`current_message` מכיל את ה-content (case-insensitive
  substring). מונע זיהום של ה-prompt בכינויים לא קשורים.
- מיון: `confidence DESC, last_confirmed_at DESC, id DESC` (tiebreaker יציב).
- Cap: 10 facts.
- `access_count++` ב-UPDATE batch לכל fact שנכלל.

לוגיקת הפורמט (`format_facts_block`):
- בלוק ריק אם אין facts (None → אין הזרקה).
- "מידע רגיש" — תמיד ראשון ב-tags של facts עם `requires_consent`.
- "נאמר DD/MM/YYYY" מ-`created_at`.
- "אומת שוב DD/MM/YYYY" רק אם `last_confirmed_at` שונה מ-`created_at`
  ביום או יותר (אחרת זה רק רעש — שני השדות זהים מאז INSERT).
- "ייתכן שלא רלוונטי" — נוסף כש-fact ישן מעבר ל-`MEMORY_STALENESS_DAYS`
  ימים (default 90) מ-`last_confirmed_at` (או `created_at` אם אין confirm).
  הבוט מקבל ב-system prompt הוראות נפרדות איך לטפל ב-flag הזה
  (אל לסמוך עיוורת; שאל את הלקוח לוודא אם רלוונטי).

### הוראות שימוש ל-LLM ב-system prompt

`build_system_prompt` ב-`config.py` מוסיף אוטומטית בסוף ה-prompt סעיף
"## שימוש במידע על הלקוח" שמסביר ל-LLM:
- להתחשב ב-facts בטבעיות (לא לצטט אותם).
- להיות דיסקרטי עם "מידע רגיש".
- לטפל ב"ייתכן שלא רלוונטי" בזהירות — לשאול את הלקוח לוודא.
- להזכיר open_issue אם רלוונטי.

הסעיף נכלל **רק כש-`MEMORY_INJECTION_ENABLED=true`** — אחרת בזבוז tokens
על הוראה שמתייחסת לבלוק שלא יוזרק. `full_system_prompt` override מהפאנל
לא מושפע: אם בעל העסק רוצה memory facts תחת override, הוא יכלול את
ההוראות בעצמו.

### Feature toggles

```bash
MEMORY_INJECTION_ENABLED=true    # default. כיבוי לא עוצר extraction.
MEMORY_STALENESS_DAYS=90         # default. סף יישנות ל-"ייתכן שלא רלוונטי".
```

שניהם נקראים כ-string env (true/1/yes — case-insensitive) או int.

### בדיקה ידנית

1. צור fact לבד: `INSERT INTO customer_facts (user_id, fact_type,
   content, confidence, status) VALUES ('<uid>', 'preference',
   'מעדיפה בקרים', 0.92, 'active')`.
2. שלח הודעה לבוט מהמשתמש הזה.
3. בלוגי `llm.py` תראה את ה-system message כולל "מה שאתה יודע על הלקוח"
   + הסעיף "## שימוש במידע על הלקוח".
4. בדיקת staleness: עדכן `last_confirmed_at` ל-`2025-01-01` →
   ה-bullet יכלול "ייתכן שלא רלוונטי".
5. כיבוי: `MEMORY_INJECTION_ENABLED=false` + restart → הבלוק נעלם, וגם
   ההוראות ב-system prompt נעלמות.
