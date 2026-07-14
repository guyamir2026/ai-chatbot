# הוראות מימוש: מערכת זיכרון מתמשך לצ'אטבוט - שלב 1

## רקע

אנחנו מוסיפים למערכת הצ'אטבוט הקיימת שכבת זיכרון פר-לקוח-קצה. בסיום שיחה, מערכת חילוץ עובדות (LLM) מנתחת את השיחה ושומרת עובדות יציבות על הלקוח. בשיחה הבאה, העובדות מוזרקות ל-context של הבוט.

**מה לא כלול בשלב הזה:** decay, business_facts, consolidation, active recall, multi-channel sync.

---

## קבצים שצורפו לך

1. **`extractor_eval_set.json`** - 30 שיחות סינתטיות לבדיקת ה-extractor
2. **`scorecard.md`** - הגדרת מטריקות הצלחה
3. **`json_schema.md`** - הסכמה ל-Structured Outputs
4. **המסמך הזה** - הוראות מימוש מלאות

---

## עקרונות עבודה

1. **לא לקפוץ ל-end-to-end.** המימוש הוא בשלבים. כל שלב חייב לעבור בדיקה לפני המעבר הבא.

2. **לעצור ולהתייעץ.** אם משהו לא ברור בקוד הקיים, או אם יש החלטה משמעותית שהאפיון לא מכסה - לעצור ולשאול. לא לאלתר.

3. **לא להגע בלוגיקה קיימת של הבוט בשלב 1-6.** כל המימוש הוא בקבצים חדשים. רק בשלב 7 נוגעים ב-context builder הקיים.

4. **לבדוק את ה-DB schema הקיים לפני שמוסיפים טבלאות.** ייתכן שיש מוסכמות שמירה (snake_case, indexים, etc) - לעקוב אחריהן.

---

## שלב 1: הקמת תשתית DB

### מטרה
ליצור את הטבלאות החדשות + סקריפט migration.

### משימות

1. צור קובץ `migrations/001_memory_system.sql` (או לפי המוסכמה במערכת) עם:

```sql
CREATE TABLE customer_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  business_id TEXT NOT NULL,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('preference','personal_info','relationship','vocabulary','open_issue')),
  content TEXT NOT NULL,
  confidence REAL NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('inferred','business_owner')),
  requires_consent INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('active','pending_approval','rejected','superseded')),
  evidence TEXT,
  superseded_by_id INTEGER REFERENCES customer_facts(id),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  access_count INTEGER DEFAULT 0,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX idx_customer_facts_user_business ON customer_facts(user_id, business_id, status);
CREATE INDEX idx_customer_facts_status ON customer_facts(status);

CREATE TABLE business_profile (
  business_id TEXT PRIMARY KEY,
  business_type TEXT,
  business_name TEXT,
  services_json TEXT,
  what_matters_for_extraction TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE extraction_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  business_id TEXT NOT NULL,
  conversation_start TIMESTAMP,
  conversation_end TIMESTAMP,
  messages_count INTEGER,
  extractions_count INTEGER,
  skipped_count INTEGER,
  status TEXT,
  error_message TEXT,
  tokens_used INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_extraction_runs_user ON extraction_runs(user_id, created_at);
```

2. הוסף ל-`database.py` (או הקובץ המקביל) פונקציות CRUD בסיסיות:
   - `get_customer_facts(user_id, business_id, status='active')`
   - `insert_customer_fact(fact_data)`
   - `update_customer_fact(fact_id, updates)`
   - `get_business_profile(business_id)`
   - `upsert_business_profile(profile_data)`
   - `log_extraction_run(run_data)`
   - `get_last_extraction_run(user_id, business_id)`

3. הרץ את ה-migration על DB מקומי לבדיקה.

### בדיקת השלב

- כל הטבלאות נוצרו בלי שגיאות
- בדיקה ידנית: insert + select של customer_fact אחד עובד
- כל הפונקציות עוברות בדיקה בסיסית (יחידה אחת לכל אחת)

**עצור כאן והודע שסיימת לפני שעוברים לשלב 2.**

---

## שלב 2: פאנל - מסך "פרופיל עסק"

### מטרה
מסך בפאנל שמאפשר לבעל העסק להזין business_profile. **חייב להיות לפני ה-extractor**, כי בלי זה אין מה להזין כקלט.

### משימות

1. צור route חדש ב-Flask: `/admin/business-profile` (או לפי המוסכמה).

2. ה-UI כולל טופס עם:
   - `business_type` - dropdown/select עם ערכים נפוצים + אופציית "אחר"
   - `business_name` - text input
   - `services` - UI דינמי להוספת/עריכת שירותים. כל שירות: name, aliases (רשימה), category
   - `what_matters_for_extraction` - textarea גדול עם placeholder/דוגמה שמסבירה מה לכתוב

3. שמירה ל-`business_profile`. `services` נשמר כ-JSON ב-`services_json`.

4. הוסף seed data לעסקים קיימים במערכת. אם יש רק business_id אחד פעיל - אפשר להזין ידנית.

### בדיקת השלב

- אפשר להזין/לערוך פרופיל עסק
- ה-JSON של services תקין
- הנתונים נשמרים ב-DB

**עצור כאן והודע שסיימת.**

---

## שלב 3: ה-Fact Extractor (פונקציית הליבה)

### מטרה
לבנות את פונקציית `extract_facts()` שמקבלת שיחה ומחזירה עובדות. **בלי background task עדיין** - רק הפונקציה.

### משימות

1. צור מודול חדש: `memory/extractor.py` (או לפי מבנה הפרויקט).

2. שמור את הפרומפט בקובץ נפרד: `memory/prompts/fact_extractor.txt`. הפרומפט המלא מצורף בסוף המסמך הזה. **חשוב: להעתיק אותו 1:1 בלי שינויים.**

3. שמור את ה-JSON Schema בקובץ נפרד: `memory/schemas/extractor_schema.py`. הסכמה המלאה ב-`json_schema.md`.

4. הפונקציה הראשית:

```python
def extract_facts(
    user_id: str,
    business_id: str,
    conversation: list[dict],
    business_profile: dict,
    existing_facts: list[dict]
) -> dict:
    """
    Returns: {
        'extractions': [...],
        'skipped': [...],
        'tokens_used': int,
        'success': bool,
        'error': str | None
    }
    """
```

5. הפונקציה עושה:
   - בונה input dict לפי הפורמט המוגדר (`<business_context>`, `<existing_facts>`, `<conversation>`)
   - קוראת ל-`gpt-4.1-mini` עם temperature=0.1 ו-Structured Outputs
   - retry פעם אחת אחרי 5 שניות במקרה של שגיאה
   - מחזירה את התוצאה הגולמית של ה-LLM + tokens_used + success/error

6. Pre-filter ל-existing_facts (כשיש >8):
   - כל `open_issue` נכלל
   - top-K לפי FAISS embedding similarity לטקסט השיחה (השתמש ב-FAISS הקיים)
   - cap: 12 facts סה"כ

7. הוסף בדיקה: אם conversation ריק או < 2 הודעות - להחזיר success=True עם extractions=[].

### בדיקת השלב

- קריאה ידנית לפונקציה עם דאטה לדוגמה מה-eval set
- בדיקה שהפלט תואם ל-schema
- בדיקה ש-retry עובד (אפשר לדמות שגיאה)

**עצור כאן והודע שסיימת + הצג פלט אחד לדוגמה.**

---

## שלב 4: Post-validation וכתיבה ל-DB

### מטרה
לאמת את הפלט של ה-extractor ולשמור ל-DB עם הסטטוס הנכון.

### משימות

1. הוסף פונקציה `validate_extraction(ext, existing_facts) -> tuple[bool, str | None]`:
   - action vs ids consistency:
     - `confirm` חייב `confirms_id` ולא יכול `supersedes_id`
     - `supersede` חייב `supersedes_id` ולא יכול `confirms_id`
     - `add` שניהם חייבים להיות null
   - `confirms_id` / `supersedes_id` חייבים להתאים ל-fact קיים ב-existing_facts
   - `content` עד 20 מילים (15 + שוליים)
   - `confidence` >= 0.6
   - מחזיר (True, None) או (False, reason)

2. הוסף פונקציה `save_extractions(extractions, user_id, business_id)`:
   - לכל extraction תקין:
     - קביעת status לפי הטבלה:
       | confidence | requires_consent | status |
       |---|---|---|
       | >= 0.85 | false | `active` |
       | >= 0.85 | true | `pending_approval` |
       | 0.60-0.84 | any | `pending_approval` |
     - `action=add` → INSERT
     - `action=confirm` → UPDATE על last_confirmed_at של ה-fact הקיים, לא לשמור fact חדש
     - `action=supersede` → INSERT חדש + UPDATE לישן (status=superseded, superseded_by_id)
   - dedup: אם נשמר `add` ויש כבר fact זהה (content + status=active) - לדחות

3. תקין: כל extraction שנכשל ב-validation נרשם ב-log אבל לא ב-DB.

4. הוסף `run_extraction_for_user(user_id, business_id, conversation)` שמחבר הכל:
   - שולף business_profile ו-existing_facts
   - קורא ל-`extract_facts`
   - validate + save
   - רושם ל-`extraction_runs`

### בדיקת השלב

- בדיקה ידנית: שיחה לדוגמה → extractions נשמרים נכון
- בדיקת edge case: confirm על fact שלא קיים → נדחה
- בדיקת dedup: שני adds זהים → רק אחד נשמר

**עצור והודע שסיימת.**

---

## שלב 5: סקריפט הרצת eval

### מטרה
סקריפט שמריץ את כל 30 הקייסים ב-eval set ומייצר דוח. **קריטי לפני שמחברים background task.**

### משימות

1. צור סקריפט `memory/eval/run_eval.py`:
   - טוען את `extractor_eval_set.json`
   - לכל case:
     - בונה business_profile mock מ-`business_profiles` בקובץ
     - קורא ל-`extract_facts` (לא ל-`run_extraction_for_user` - אנחנו לא רוצים לכתוב ל-DB מה-eval)
     - משווה לתוצאה ב-`expected`
   - מייצר דוח markdown

2. כתוב פונקציית comparison לפי `scorecard.md`:
   - extraction match (action + fact_type + content סמנטית + requires_consent + confidence bucket + ids)
   - false positives
   - false negatives
   - skipped quality

3. השוואת `content` סמנטית - השתמש ב-LLM-judge:
   ```python
   def is_semantic_match(actual: str, expected: str) -> bool:
       # קריאה ל-gpt-4.1-mini עם temperature=0
       # פרומפט: "האם שני המשפטים מבטאים את אותה משמעות עסקית? ענה כן/לא בלבד."
   ```

4. הדוח כולל:
   - טבלת מטריקות אגרגטיביות עם status (לפי הרפים ב-scorecard.md)
   - רשימת cases שנכשלו עם פירוט
   - actual vs expected לכל case שנכשל

5. הסקריפט מחזיר exit code 0 אם כל המטריקות עוברות, אחרת 1.

### בדיקת השלב

- הרץ את ה-eval. הצג את הדוח.
- **אם יש fails:** נתח אותם. ייתכן שהפרומפט צריך חידוד או שהקייסים צריכים תיקון. תתייעץ לפני שינויים.
- **רק כשכל המטריקות עוברות** - ממשיכים לשלב 6.

**עצור כאן והצג את הדוח המלא.**

---

## שלב 6: Background task

### מטרה
טריגר אוטומטי שמריץ extraction על שיחות שהסתיימו.

### משימות

1. צור `memory/background.py` עם פונקציה `extraction_worker()`:
   - רץ כל 5 דקות (השתמש ב-mechanism הקיים במערכת - APScheduler, cron, או דומה)
   - שולף משתמשים עם `last_active` שעבר 30 דקות
   - לכל אחד: בודק אם יש `extraction_run` שמכסה את השיחה האחרונה
   - אם לא: מריץ `run_extraction_for_user`

2. Conversation Segmentation:
   - שולף הודעות מ-`conversations` של המשתמש מאז ה-`extraction_run.conversation_end` האחרון, או 7 ימים אחורה (הראשון מהשניים)
   - אם הפער בין ההודעה האחרונה לעכשיו > 30 דקות → שיחה נסגרה, מריץ extraction
   - אחרת → דוחה

3. הוסף לוגיקה למנוע concurrent runs על אותו user (lock פשוט בטבלה או in-memory).

4. רישום ל-log: כמה משתמשים נסרקו, כמה extractions יצאו, זמני ריצה.

### בדיקת השלב

- הפעל ידנית לבדיקה (לא להמתין 5 דקות)
- בדוק שמשתמש שעבר את הסף עובר extraction
- בדוק שאין double-run
- בדוק ש-logs מקיפים

**עצור והודע שסיימת.**

---

## שלב 7: פאנל - מסכי לקוחות ותור אישור

### מטרה
ממשק שבעל העסק רואה ויכול לאשר/לדחות עובדות.

### משימות

1. **מסך "תור אישור"** ב-`/admin/pending-facts`:
   - שולף את כל ה-facts עם `status='pending_approval'`
   - לכל אחד מציג: content, fact_type, evidence (הציטוט), confidence, requires_consent, שם המשתמש, תאריך
   - כפתורי ✓ אשר (→ status=active) / ✗ דחה (→ status=rejected)
   - אופציה bulk: אשר/דחה הכל בעמוד
   - מציין באופן ברור (אייקון/צבע) אילו facts הם PII רגיש

2. **מסך "לקוחות"** ב-`/admin/customers`:
   - רשימת כל המשתמשים שיש להם facts
   - לחיצה על משתמש → מסך פירוט
   - מסך פירוט: כל ה-facts של המשתמש מקובצים לפי fact_type
   - לכל fact: אפשרויות ערוך content / שנה ל-rejected / מחק לחלוטין
   - הצגת evidence ו-confidence לכל fact
   - הצגה ויזואלית של facts ב-status=superseded (אפור, סימון "מוחלף")

3. הוסף תפריט ניווט ראשי לפאנל אם עוד אין.

### בדיקת השלב

- הפאנל עובד end-to-end
- אישור fact → status משתנה ל-active
- דחייה → status משתנה ל-rejected
- עריכת content → ה-fact מתעדכן

**עצור והודע שסיימת.**

---

## שלב 8: הזרקה ל-context של הבוט

### מטרה
לחבר את הכל - הבוט מקבל את ה-facts ב-context בכל שיחה.

### משימות

1. אתר את הפונקציה שבונה את ה-context של הבוט. כנראה משהו כמו `build_chat_context()` או `prepare_messages()`.

2. הוסף פונקציה `get_relevant_facts_for_context(user_id, business_id, current_message)`:
   - שולפת כל `preference` + `personal_info` + `relationship` + `open_issue` עם `status='active'`
   - שולפת `vocabulary` רק אם מילים מה-content מופיעות ב-current_message (lexical match בסיסי)
   - Cap: 10 facts, מסודרים לפי `confidence DESC, last_confirmed_at DESC`
   - מעדכנת `access_count += 1` לכל fact שנשלף

3. הוסף את ה-facts ל-context בפורמט הזה, **לפני** סיכום השיחה:

```
מה שאתה יודע על המשתמש:
- מעדיפה תורים בשעות הבוקר
- רגישה לאגוזים (מידע רגיש)
- 'הטיפול הקבוע' = מניקור ג'ל
```

4. אם אין facts - לא להוסיף את הבלוק בכלל (לא לכתוב "אין מידע").

5. PII רגיש - הוסף את התיוג `(מידע רגיש)` ליד ה-content.

### בדיקת השלב

- שיחה עם משתמש שיש לו facts → הבוט מתייחס אליהם
- שיחה עם משתמש חדש → אין בלוק facts ב-context
- בדיקה שהבוט לא מזכיר את ה-facts ישירות (אלא משתמש בהם)

**עצור והודע שסיימת. זה השלב האחרון של שלב 1.**

---

## בעיות נפוצות - מה לשים לב

1. **SQLite locks** - הבוט והפאנל קוראים לאותו DB. ודא ש-WAL mode פעיל ושהבוט לא חוסם את הפאנל בשעת extraction.

2. **עלות tokens** - לכל extraction יש עלות. רשום ב-`extraction_runs.tokens_used` כדי לעקוב. אם אתה רואה ש-extraction רץ שוב על אותה שיחה - יש באג.

3. **Empty business_profile** - אם המשתמש לא הזין business_profile, ה-extractor יקבל context חלקי. אל תריץ extraction עד שיש business_profile.

4. **Race conditions ב-background** - אם הסקרייפר רץ פעמיים במקביל על אותו משתמש, אתה יכול לקבל extractions כפולים. השתמש בלוק.

5. **Conversation ארוכה מאוד** - אם משתמש דיבר 100 הודעות בלי הפסקה של 30 דקות - אל תשלח את כולן ל-LLM. cap ב-50 הודעות אחרונות, או חתוך לפי conversation_summaries אם קיים.

---

## הפרומפט המלא ל-Fact Extractor

```text
אתה Fact Extractor שמרני מאוד עבור צ'אטבוט שירות עסקי.

המטרה שלך:
לחלץ רק עובדות יציבות, שימושיות וחד-משמעיות על לקוח קצה, מתוך שיחה שכבר הסתיימה.
המטרה הראשית היא למנוע false positives.
עדיף להחמיץ עובדה אמיתית מאשר לשמור עובדה לא יציבה או לא רלוונטית.

## כללי על
- החזר רק נתונים שמתאימים בדיוק לסכמת הפלט.
- אל תכתוב הסברים מחוץ לשדות המותרים.
- אם אין עובדות טובות מספיק, החזר `extractions: []`.
- ברוב השיחות התקינות יהיו 0 עד 2 extractions. יותר מ-3 הוא חריג ודורש evidence חזק מאוד.

## שער קבלה קשיח
מותר לחלץ עובדה רק אם כל ארבעת התנאים מתקיימים:
1. היא נאמרה במפורש, או חזרה לפחות פעמיים בהקשרים שונים.
2. היא צפויה להיות תקפה מעבר לשיחה הנוכחית.
3. היא שימושית לאינטראקציות שירות עתידיות.
4. אין דו-משמעות לגבי הסובייקט או הטענה.

אם אחד מהתנאים לא מתקיים:
- אל תחזיר extraction על זה.
- אפשר להוסיף אותו ל-`skipped` עם reason מתאים.

## מה לא לחלץ
אל תחלץ:
- פרטים רגעיים או חד-פעמיים מהשיחה הנוכחית בלבד.
- כוונות זמניות או ספקולטיביות.
- ניחושים, פרשנויות, או inference שלא נאמר במפורש.
- רגשות משוערים.
- עובדות על צד שלישי אם לא ברור שהן על הלקוח עצמו.
- פרטים כלליים שלא יועילו לשירות עתידי.
- מידע מעומעם, סותר, או תלוי הקשר רגעי בלבד.

## סוגי fact מותרים
- `preference` — העדפה יציבה של הלקוח.
- `personal_info` — מידע אישי שימושי לשירות עתידי.
- `relationship` — יחס מתמשך לעסק, היסטוריה או סטטוס רלוונטי.
- `vocabulary` — איך הלקוח קורא לשירות/מוצר/תהליך של העסק.
- `open_issue` — בעיה לא פתורה שכדאי לזכור ולעקוב אחריה.

## כללים לכל סוג
### preference
חלץ רק אם מדובר בהעדפה יציבה יחסית, לא רצון רגעי.
דוגמאות טובות: שעות מועדפות, ערוץ תקשורת מועדף, סוג שירות מועדף.
לא טוב: "היום נוח לי ב-16:00".

### personal_info
חלץ רק אם המידע שימושי עתידית לשירות.
לא לחלץ פרטים ביוגרפיים כלליים שלא מוסיפים ערך תפעולי.

### relationship
חלץ רק אם יש לזה משמעות מתמשכת מול העסק.
למשל: לקוח קבוע, מחכה להחזר, מחזיק מנוי, חזר אחרי תקופה.

### vocabulary
אם הלקוח משתמש בכינוי עקבי לשירות/מוצר, או במונח שלא תואם בדיוק לרשימת השירותים אך מתייחס אליהם בבירור, מותר לחלץ vocabulary fact.
ה-fact צריך לנסח מיפוי ברור וקצר.

### open_issue
חלץ רק בעיה שעדיין פתוחה וצפויה להיות רלוונטית בשיחה עתידית.
אם הבעיה נפתרה בתוך השיחה — אל תחלץ.

## רגישות ו-consent
אם העובדה כוללת מידע רגיש, סמן `requires_consent=true`.

מידע רגיש כולל:
- בריאות
- מידע פיננסי
- מידע משפחתי
- מידע דתי
- מידע מיני

אם יש ספק אם המידע רגיש — סמן `requires_consent=true`.

## שימוש ב-business context
העדף עובדות שמתיישרות עם:
- סוג העסק
- השירותים/המוצרים שלו
- `what_matters_for_extraction`

התעלם מעובדות שאינן רלוונטיות לשירות העתידי בעסק הזה.

אם הלקוח משתמש בביטוי שלא מופיע בדיוק ב-`services.name` או `aliases`,
אבל ברור שהוא מתייחס לשירות/מוצר מהרשימה,
אפשר לחלץ `vocabulary`.

## השוואה ל-existing_facts
עליך להשוות כל מועמד רק מול העובדות שסופקו ב-`existing_facts`.

בחר action אחד בלבד לכל extraction:
- `add` — עובדה חדשה.
- `confirm` — אותה משמעות כמו fact קיים.
- `supersede` — סותרת fact קיים על אותו ציר.

### כללי confirm
בחר `confirm` רק אם המשמעות בפועל זהה, גם אם הניסוח שונה.
במקרה כזה:
- מלא `confirms_id`
- `supersedes_id` חייב להיות null

### כללי supersede
בחר `supersede` רק אם יש סתירה אמיתית על אותו ציר.
במקרה כזה:
- מלא `supersedes_id`
- `confirms_id` חייב להיות null
- ה-content החדש צריך לנסח את העובדה המעודכנת בלבד

### כללי add
בחר `add` אם העובדה לא זהה ולא סותרת fact קיים רלוונטי.
במקרה כזה:
- `confirms_id` = null
- `supersedes_id` = null

אל תיצור גם `add` וגם `confirm` עבור אותו רעיון.
אל תחזיר שני extractions חופפים מאוד באותה קריאה.

## ניסוח content
- בעברית
- גוף שלישי
- עד 15 מילים
- קצר, קונקרטי, בלי הסברים
- בלי לצטט את הלקוח מילה במילה אם לא צריך
- בלי סימני קריאה
- בלי ספקולציה

דוגמאות סגנון:
- "מעדיפה תורים בשעות הבוקר"
- "ממתינה להחזר על הזמנה קודמת"
- "'הטיפול הקבוע' = מניקור ג'ל"

## evidence
- ציטוט קצר או paraphrase צמוד מאוד למה שנאמר בשיחה
- evidence חייב לבסס את העובדה בפועל
- אם אין evidence טוב, אל תחלץ

## rubric ל-confidence
השתמש בטווחים הבאים:

- 0.95–1.00  
  נאמר במפורש, ברור מאוד, יציב, שימושי עתידית, ללא עמימות.

- 0.85–0.94  
  נאמר במפורש ויש evidence טוב, אך יש חולשה קטנה אחת בלבד.

- 0.70–0.84  
  יש אינדיקציה טובה, אבל קיימת אי-בהירות מסוימת לגבי יציבות / שימושיות / סובייקט / ניסוח.

- 0.60–0.69  
  מועמד חלש או גבולי. רק אם בכל זאת מחלצים לבדיקה אנושית.

- מתחת ל-0.60  
  אל תחזיר extraction.

כללים נוספים:
- השתמש ב-confidence מעל 0.94 רק כאשר evidence ישיר ומפורש קיים בשיחה ואין שום עמימות.
- אם התלבטת בין שני ציונים, בחר את הנמוך.
- אם מדובר ב-inference ולא באמירה מפורשת, בדרך כלל confidence צריך להיות נמוך מ-0.85.

## reasons מומלצים ל-skipped
השתמש בסיבות קצרות וברורות, למשל:
- "לא נאמר במפורש"
- "לא יציב מעבר לשיחה"
- "לא שימושי לעתיד"
- "סובייקט לא חד-משמעי"
- "פרט רגעי או חד-פעמי"
- "כבר מכוסה על ידי fact קיים"
- "אין evidence מספיק"
- "בעיה נפתרה בתוך השיחה"
- "לא רלוונטי לסוג העסק"

## דוגמאות

### דוגמה 1 — extraction חיובי
קלט:
הלקוחה אומרת: "כמו תמיד, הכי טוב לי בבוקר, אחרי 9."
אין fact קיים רלוונטי.

פלט רצוי:
{
  "extractions": [
    {
      "action": "add",
      "fact_type": "preference",
      "content": "מעדיפה תורים בשעות הבוקר",
      "requires_consent": false,
      "confidence": 0.93,
      "evidence": "הכי טוב לי בבוקר, אחרי 9",
      "supersedes_id": null,
      "confirms_id": null
    }
  ],
  "skipped": []
}

### דוגמה 2 — דחייה נכונה
קלט:
הלקוח אומר: "היום אני פנוי רק ב-17:30."
פלט רצוי:
{
  "extractions": [],
  "skipped": [
    {
      "candidate": "פנוי ב-17:30",
      "reason": "פרט רגעי או חד-פעמי"
    }
  ]
}

### דוגמה 3 — confirm
קלט:
existing_facts כולל:
{id: 12, fact_type: "preference", content: "מעדיפה תורים בשעות הבוקר"}

בשיחה:
"בוקר תמיד הכי נוח לי."

פלט רצוי:
{
  "extractions": [
    {
      "action": "confirm",
      "fact_type": "preference",
      "content": "מעדיפה תורים בשעות הבוקר",
      "requires_consent": false,
      "confidence": 0.91,
      "evidence": "בוקר תמיד הכי נוח לי",
      "supersedes_id": null,
      "confirms_id": 12
    }
  ],
  "skipped": []
}

### דוגמה 4 — supersede
קלט:
existing_facts כולל:
{id: 21, fact_type: "preference", content: "מעדיפה תורים בשעות הבוקר"}

בשיחה:
"מאז שהתחלתי עבודה חדשה, רק ערבים מתאימים לי."

פלט רצוי:
{
  "extractions": [
    {
      "action": "supersede",
      "fact_type": "preference",
      "content": "מעדיפה תורים בשעות הערב",
      "requires_consent": false,
      "confidence": 0.9,
      "evidence": "מאז שהתחלתי עבודה חדשה, רק ערבים מתאימים לי",
      "supersedes_id": 21,
      "confirms_id": null
    }
  ],
  "skipped": []
}

### דוגמה 5 — requires_consent
קלט:
הלקוחה אומרת: "אני לא יכולה טיפול עם אגוזים כי יש לי אלרגיה."
פלט רצוי:
{
  "extractions": [
    {
      "action": "add",
      "fact_type": "personal_info",
      "content": "רגישה לאגוזים",
      "requires_consent": true,
      "confidence": 0.9,
      "evidence": "יש לי אלרגיה",
      "supersedes_id": null,
      "confirms_id": null
    }
  ],
  "skipped": []
}

### דוגמה 6 — vocabulary
קלט:
ברשימת השירותים מופיע "מניקור ג'ל".
בשיחה הלקוחה אומרת: "אני רוצה שוב את הטיפול הקבוע שלי."

פלט רצוי:
{
  "extractions": [
    {
      "action": "add",
      "fact_type": "vocabulary",
      "content": "'הטיפול הקבוע' = מניקור ג'ל",
      "requires_consent": false,
      "confidence": 0.82,
      "evidence": "אני רוצה שוב את הטיפול הקבוע שלי",
      "supersedes_id": null,
      "confirms_id": null
    }
  ],
  "skipped": []
}

## בדיקה עצמית לפני החזרה
לפני שאתה מחזיר תשובה, עבור על כל extraction ובדוק:
1. האם כל ארבעת תנאי שער הקבלה מתקיימים?
2. האם ה-content קצר, ברור, בעברית ובגוף שלישי?
3. האם ה-evidence באמת תומך בעובדה?
4. האם action נבחר נכון מול existing_facts?
5. האם requires_consent נכון?
6. האם ה-confidence שמרני ולא מנופח?

אם אחת הבדיקות נכשלת:
- תקן את ה-extraction
- או העבר אותו ל-`skipped`
- או מחק אותו

כעת נתח את הקלט הבא בלבד.

<business_context>
{{business_context_json}}
</business_context>

<existing_facts>
{{existing_facts_json}}
</existing_facts>

<conversation>
{{conversation_json}}
</conversation>

תזכורת אחרונה:
אל תנחש.
אל תחלץ ליתר ביטחון.
אם יש ספק ממשי — דלג.
```

---

## סיכום סדר העבודה

| שלב | מה עושים | בדיקה לפני המשך |
|---|---|---|
| 1 | טבלאות + CRUD | טבלאות נוצרות, פונקציות בסיסיות עובדות |
| 2 | פאנל - פרופיל עסק | אפשר להזין business_profile |
| 3 | פונקציית extract_facts | קריאה ידנית מחזירה JSON תקין |
| 4 | Validation + שמירה ל-DB | extractions נשמרים עם status נכון |
| 5 | סקריפט eval | **כל המטריקות עוברות** |
| 6 | Background task | טריגר אוטומטי עובד |
| 7 | פאנל - תור אישור ולקוחות | UI מלא לבעל העסק |
| 8 | הזרקה ל-context של הבוט | הבוט משתמש ב-facts |

---

## הנחיה כללית

אם בכל שלב יש לך ספק - **עצור ושאל**. עדיף להתעכב 5 דקות מאשר לבנות 3 שלבים על בסיס הנחה שגויה. אני זמין לכל שאלה.

בהצלחה!
