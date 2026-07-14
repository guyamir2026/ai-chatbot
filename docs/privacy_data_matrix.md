# מטריצת מיפוי מידע אישי — ai-business-bot

מיפוי כל הטבלאות במערכת לצורך ציות לחוק הגנת הפרטיות (תיקון 13). נבנה לפי תבנית
ההתייעצות החיצונית.

**סטטוס**: מילוי ראשוני אוטומטי על בסיס `database.py` + `migrations.py`. שורות
מסומנות `🟡 לבירור` דורשות החלטה אנושית לפני שננעל את ה-policy.

**הנחיה למילוי**: לכל טבלה ממלאים לפי המצב בפועל, לא לפי הכוונה המקורית של
המפתח. אם שדה יכול לזהות אדם, להסיק עליו משהו, או להיכלל בייצוא/מחיקה — הוא
נכנס למטריצה. אם המידע נגזר ע"י AI, נשמר ב-log, summary, cache או analytics —
הוא עדיין נחשב למיפוי.

**כלל פיתוח (להוסיף ל-CLAUDE.md אחרי שהמטריצה אושרה)**: כל טבלה חדשה — להוסיף
שורה במטריצה באותו commit שמוסיף את הטבלה.

---

## סיכום מהיר — מה מצאנו

- **32 טבלאות** במערכת.
- **17 טבלאות** מכילות `user_id` של משתמש קצה ↔ דורשות טיפול.
- **11 טבלאות** הן config / ידע עסקי / תוכן ללא PII של משתמש קצה — לא דורשות
  טיפול ב-delete/export אבל כן בסקירה אבטחתית (למשל credentials, חבילת SaaS).
- **4 טבלאות** הן "נגזרות" — embeddings, summaries, cache של תשובות AI, סיכומי
  follow-up. מי שמוחק רק את ה-source ומשאיר אותן — לא באמת מחק.

### פערים נצפים לעומת הקוד הקיים

| תופעה | מה קיים | מה חסר |
|---|---|---|
| `delete_user_data` | מכסה את כל 14 הטבלאות עם `user_id` + referrals (שני כיוונים) + broadcast_deliveries + users | ✅ הכיסוי שלם. `blocked_users` מושמט בכוונה (legal hold — ראה שורה במטריצה). `developer_reports` אין user_id ולכן לא רלוונטי. |
| `get_user_data_summary` | (תוקן) מחזיר counts לכל הטבלאות הרלוונטיות | ✅ תוקן: נוספו counts ל-conversations, conversation_summaries, lead_followups (עם פילוח status), referrals (שני כיוונים), referral_codes, credits, unanswered_questions, response_pages, broadcast_deliveries, user_identities. תוכן חופשי (user_notes.note, lead_followups.analysis_json) עדיין לא נחשף — ממתין להחלטה משפטית. |
| `purge_old_data` | מכסה: conversations, appointments, live_chats, conversation_summaries | ❗️לא מכסה: `lead_followups`, `agent_requests`, `unanswered_questions`, `response_pages`, `broadcast_deliveries`, `referrals` (completed), `credits` (expired). |
| `consent_ledger` | לא קיים | ❗️consent נשמר רק ב-`users.consent_given_at` ונמחק יחד עם המשתמש ב-`/forget`. כנ"ל `users.disclaimer_sent_at` (סימון timestamp שהודעת הפתיחה המשפטית נשלחה — implied consent; לא PII רגיש) — נמחק עם שורת המשתמש. |
| `admin_audit_log` | רק stdout (`logger.info("AUDIT \| ...")`) | ❗️זה הולך ל-Render logs ול-Sentry. אין retention משלנו עליו. |

---

## טבלת תקציר — אילו טבלאות במטריצה

| # | table_name | subject | חיוני לסקירה? | חמורות מבחינת רגישות |
|---|---|---|---|---|
| 1 | `users` | end_user | ✅ | medium — anchor |
| 2 | `conversations` | end_user | ✅ | **high** — free text |
| 3 | `conversation_summaries` | end_user (derived) | ✅ | **high** — AI מסיק על האדם |
| 4 | `appointments` | end_user | ✅ | medium עד high (קליניקות) |
| 5 | `agent_requests` | end_user | ✅ | medium — סיבת הפנייה |
| 6 | `live_chats` | end_user | ✅ | low (metadata) |
| 7 | `unanswered_questions` | end_user | ✅ | medium — שאלת המשתמש בטקסט |
| 8 | `lead_followups` | end_user (AI-derived) | ✅ | **high** — סיווגי AI |
| 9 | `user_notes` | end_user (admin-written) | ✅ | **high** — טקסט חופשי של בעל העסק |
| 10 | `blocked_users` | end_user | ✅ | low — אבל מושמט מ-delete בכוונה |
| 11 | `referral_codes` | end_user | ✅ | low |
| 12 | `referrals` | end_user (2 צדדים) | ✅ | low |
| 13 | `credits` | end_user | ✅ | low |
| 14 | `user_subscriptions` | end_user | ✅ | low |
| 15 | `user_identities` | end_user | ✅ | medium — phone+BSUID |
| 16 | `response_pages` | end_user (AI cache) | ✅ | medium — תוכן AI על משתמש |
| 17 | `broadcast_message_recipients` | end_user | ✅ | low |
| 18 | `broadcast_deliveries` | end_user | ✅ | low |
| 19 | `kb_entries` | business_owner content | סקירה אבטחתית | low |
| 20 | `kb_chunks` | derived from kb_entries | סקירה אבטחתית | low |
| 21 | `business_hours` | config | — | none |
| 22 | `special_days` | config | — | none |
| 23 | `vacation_mode` | config | — | none |
| 24 | `bot_settings` | config (system_prompt) + זהות עסקית | סקירה אבטחתית | low — אבל יכול להכיל מידע עסקי |
| 25 | `google_calendar_credentials` | business_owner | סקירה אבטחתית | **high** — refresh_token |
| 26 | `business_branding` | config | — | none |
| 27 | `broadcast_messages` | broadcast content | — | low (תוכן יזום) |
| 28 | `broadcast_campaigns` | broadcast content | — | low |
| 29 | `whatsapp_templates` | template content | — | none |
| 30 | `developer_reports` | business_owner reports | סקירה | low (אבל יכול לכלול PII בתיאור חופשי 🟡) |
| 31 | `subscription` | saas_customer (business_owner) | — | none (config; אין סודות, אין PII של end_user) |
| 32 | `plan_history` | saas_customer (business_owner) | — | low (`reason` שדה חופשי — הוראת UI לא לכלול PII) |
| 33 | `meta_credentials` | business_owner | סקירה אבטחתית | **high** — page access tokens |
| 34 | `push_subscriptions` | business_owner (browser) | — | none (endpoint דפדפן בלבד) |
| 35 | `customer_facts` | end_user (AI-derived) | ✅ | **high** — מסקנות AI על האדם |
| 36 | `extraction_runs` | end_user (audit) | ✅ | low — metadata בלבד |
| 37 | `business_profile` | config | סקירה | low — תוכן עסקי כללי |

---

## חלק א' — טבלאות עם PII של משתמש קצה (17 טבלאות)

לכל טבלה, 17 העמודות לפי התבנית. עמודות מקוצרות לטובת קריאות:
`tbl`, `purpose`, `subject`, `lookup`, `direct_id`, `indirect_id`, `free_text`,
`sens_risk`, `source`, `external`, `export`, `delete`, `retention`, `purge_trig`,
`hold`, `sec_lvl`, `notes`.

---

### 1. `users`

| | |
|---|---|
| **purpose** | טבלת anchor — זיהוי משתמש קצה, סטטוס הסכמה, ערוץ, מונה הודעות, opt-in WhatsApp |
| **subject** | end_user |
| **lookup** | `user_id` (PK) — Telegram numeric ID או `+972...` ל-WhatsApp |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `channel`, `first_seen_at`, `last_active_at`, `wa_marketing_opt_in_source` |
| **free_text** | none |
| **sens_risk** | `possible` — לא בגלל השדות, אלא כי השתייכות לטננט (קליניקה) יכולה להסגיר. גם `username` יכול להיות שם פרטי אמיתי. |
| **source** | webhook, system_generated |
| **external** | Telegram/WhatsApp (לא נשלח ל-OpenAI/Sentry באופן רגיל) |
| **export** | `yes` — כל המידע על המשתמש כולל סטטוסי הסכמה והעדפות |
| **delete** | `hard_delete` (כיום מכוסה ב-`delete_user_data`) — אבל הוכחת הסכמה צריכה לעבור ל-`consent_ledger` חדש לפני המחיקה |
| **retention** | כל עוד המשתמש פעיל; purge לדורמנט (לא קיים היום) |
| **purge_trig** | `last_active_at` 🟡 לבירור — לא מוגדר policy |
| **hold** | abuse/security — `blocked_users` נשמרת בנפרד גם אחרי `/forget` |
| **sec_lvl** | medium — anchor table |
| **notes** | `consent_given_at` ו-`consent_version` כאן הם הוכחת ההסכמה היחידה. עם מחיקה, ההוכחה אובדת ⇒ מצדיק `consent_ledger` נפרד. |

---

### 2. `conversations`

| | |
|---|---|
| **purpose** | תיעוד היסטוריית שיחות (user + assistant), context ל-RAG/LLM, איכות שירות |
| **subject** | end_user |
| **lookup** | `user_id`, `id`, joins ל-`conversation_summaries` |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `channel`, `created_at`, `role`, `sources` (רשימת kb entries) |
| **free_text** | **`free_text`** — `message` הוא טקסט חופשי ללא הגבלה |
| **sens_risk** | **`high`** — תוכן השיחה יכול להכיל מידע רפואי, אישי, מיקום, פרטי משפחה, תשלומים, תלונות. בקליניקה — כמעט תמיד. |
| **source** | user_input, llm_generated (תשובות assistant) |
| **external** | OpenAI/Gemini (כל הודעה עוברת לעיבוד), Telegram/WhatsApp, Sentry **לא רצוי** — לבדוק אם message נכנס ל-error payload 🟡 |
| **export** | `yes` — כולל transcript מלא |
| **delete** | `hard_delete` (מכוסה) + ❗️**מחיקת נגזרות**: `conversation_summaries`, `response_pages`, `lead_followups.conversation_summary` |
| **retention** | 12 חודשים מ-`created_at` (מכוסה ב-`purge_old_data`) — בקליניקות לשקול 6 חודשים 🟡 |
| **purge_trig** | `created_at` |
| **hold** | dispute, abuse investigation, security incident |
| **sec_lvl** | **high** — הטבלה שמשנה את כל ניתוח רמת האבטחה של הפלטפורמה |
| **notes** | תשובות assistant (`role='assistant'`) הן גם מידע אישי כי הן "מסקנות על האדם" (תיקון 13). אסור להתייחס אליהן כ-metadata. |

---

### 3. `conversation_summaries`

| | |
|---|---|
| **purpose** | סיכום אוטומטי של שיחות ארוכות לחיסכון ב-tokens. נגזר ע"י LLM. |
| **subject** | end_user (derived) |
| **lookup** | `user_id`, `id`, `last_summarized_message_id` → `conversations.id` |
| **direct_id** | `user_id` |
| **indirect_id** | `created_at`, `message_count` |
| **free_text** | **`free_text`** — `summary_text` הוא תוצר LLM שמסכם את השיחה |
| **sens_risk** | **`high`** — סיכום של שיחה רגישה הוא עדיין רגיש; הרשות הדגישה שגם **מסקנות AI על האדם** הן מידע אישי |
| **source** | llm_generated |
| **external** | OpenAI/Gemini (יוצרים את הסיכום), עשוי לחזור ל-LLM כ-context בשיחות עתידיות |
| **export** | `yes` — חלק ממה שהמערכת "יודעת על האדם" |
| **delete** | `hard_delete` (מכוסה) — צריך להישאר synced עם מחיקת `conversations` |
| **retention** | מכוסה ב-`purge_old_data` עם אותו טווח של conversations (12 חודשים). **הוחלט (לפי המלצת היועץ): סיכום של שיחה שנמחקה הוא בדיוק "מסקנה על אדם" שאמורה להימחק.** אסור להחזיק נגזרת בלי המקור. |
| **purge_trig** | `created_at` |
| **hold** | dispute |
| **sec_lvl** | high |
| **notes** | זו טבלה **נגזרת** ראשונה — derived from `conversations`. retention זהה למקור (לא ארוך יותר). |

---

### 4. `appointments`

| | |
|---|---|
| **purpose** | ניהול תורים, סטטוסים, תזכורות, סנכרון ל-Google Calendar |
| **subject** | end_user |
| **lookup** | `id`, `user_id`, `google_event_id` |
| **direct_id** | `user_id`, `username`, `telegram_username` |
| **indirect_id** | `service`, `preferred_date`, `preferred_time`, `status`, `created_at`, `confirmed_duration_minutes`, `google_event_id` |
| **free_text** | `free_text` — `notes` שדה הערות חופשי שכותב המשתמש |
| **sens_risk** | `possible` (רגיל) עד **`high`** (קליניקה) — `service` בקליניקה מסגיר מצב; `notes` יכול להכיל כל דבר |
| **source** | user_input, admin_input |
| **external** | Google Calendar (אם מחובר), WhatsApp/Telegram לתזכורות |
| **export** | `yes` — כל היסטוריית התורים |
| **delete** | `hard_delete` (מכוסה). ❗️**צריך גם למחוק את האירוע ב-Google Calendar** — לבדוק אם יש 🟡 |
| **retention** | 36 חודשים מ-`preferred_date` ל-passed/cancelled (מכוסה). 🟡 לבירור: בקליניקות לשקול תקופה קצרה יותר; לחשבונאות יש דרישה נפרדת. |
| **purge_trig** | `preferred_date` + `status` (compound) |
| **hold** | accounting (חוק שמירת ספרים), dispute, no-show claim |
| **sec_lvl** | medium עד high לפי וורטיקל |
| **notes** | `service_type` "תמים" כמו "ייעוץ" יכול להסגיר תחום רפואי. בקליניקות — לעולם לא תמים. שדה `confirmed_duration_minutes` הוא indirect_id חזק (מאפשר join ל-calendar). |

---

### 5. `agent_requests`

| | |
|---|---|
| **purpose** | בקשות של משתמש קצה לדבר עם נציג אנושי |
| **subject** | end_user |
| **lookup** | `user_id`, `id` |
| **direct_id** | `user_id`, `username`, `telegram_username` |
| **indirect_id** | `status`, `channel`, `created_at`, `handled_at` |
| **free_text** | `free_text` — `message` הוא סיבת הפנייה כפי שהמשתמש כתב |
| **sens_risk** | `possible` עד `high` — סיבת הפנייה יכולה להיות "אני בלחץ נפשי", "יש לי כאב חמור" |
| **source** | user_input, system_generated |
| **external** | היעד הטבעי הוא human_agent (בעל העסק) דרך Telegram/WhatsApp |
| **export** | `yes` (count נוסף ל-`get_user_data_summary`) |
| **delete** | `hard_delete` — ✅ ברשימה ב-`delete_user_data` |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`** — צריך policy. הצעה: 12 חודשים מ-`handled_at`. |
| **purge_trig** | `handled_at` או `created_at` |
| **hold** | abuse investigation |
| **sec_lvl** | medium |
| **notes** | באג פתוח אחד: לא ב-purge. (קודם הייתה לי טעות — בדיקה חוזרת מצאה שהיא כן ברשימת המחיקה.) <br><br> **`channel='widget'` (חבילת מקצועי)** — לידים אנונימיים מה-widget באתר נשמרים פה: `user_id="widget:<phone>"`, `username=<שם המבקר>`, `message=<תקציר 6 הודעות אחרונות + שם + טלפון>`. ה-widget לא יוצר session של live chat — UI מסתיר את כפתור "כנס לשיחה" ב-/requests. בעל העסק חוזר טלפונית. **אין IP** של המבקר ב-DB; rate limit פר-IP מוחזק רק בזיכרון התהליך. |

---

### 6. `live_chats`

| | |
|---|---|
| **purpose** | סשן של שיחה ישירה בין בעל העסק למשתמש (הבוט שקט) |
| **subject** | end_user |
| **lookup** | `user_id`, `id`, `is_active` |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `channel`, `started_at`, `updated_at`, `ended_at`, `is_active` |
| **free_text** | none — תוכן השיחה הולך ל-`conversations` |
| **sens_risk** | low (metadata בלבד) |
| **source** | system_generated, admin_input |
| **external** | none ישירות |
| **export** | `yes` — chronology |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | 12 חודשים מ-`ended_at` (מכוסה) |
| **purge_trig** | `ended_at` |
| **hold** | dispute |
| **sec_lvl** | low |
| **notes** | התוכן עצמו ב-`conversations` — לא לשכוח לסנכרן retention ביניהן. |

---

### 7. `unanswered_questions`

| | |
|---|---|
| **purpose** | תיעוד שאלות שהבוט לא ידע לענות עליהן (knowledge gaps) |
| **subject** | end_user |
| **lookup** | `user_id`, `id` |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `intent`, `channel`, `status`, `created_at`, `resolved_at` |
| **free_text** | **`free_text`** — `question` הוא טקסט חופשי של המשתמש |
| **sens_risk** | `possible` — שאלה יכולה להיות "האם הטיפול שלכם מסייע ב-X" כשהיא חושפת מצב |
| **source** | user_input, system_generated |
| **external** | אם מוצג בפאנל אדמין, נחשף לבעל העסק |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`**. הצעה: 12 חודשים מ-`resolved_at`, או 6 חודשים מ-`created_at` ל-status=`open`. |
| **purge_trig** | `resolved_at` או `created_at` |
| **hold** | none |
| **sec_lvl** | medium |
| **notes** | אם יש flow של "מנהל המערכת מסמן כ-resolved", השאלה נשארת ב-DB ללא retention. |

---

### 8. `lead_followups`

| | |
|---|---|
| **purpose** | מעקב אחרי לידים — מי לא הזמין, מתי לחזור אליו, סיווגי AI על הכוונה |
| **subject** | end_user (AI-derived) |
| **lookup** | `user_id`, `id` |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `channel`, `service_of_interest`, `intent_type`, `lead_temperature`, `template_key`, `template_variables`, `followup_due_at`, `followup_sent_at`, `user_replied_at`, `booking_after_followup`, `stop_reason`, `created_at` |
| **free_text** | **`free_text`** — `conversation_summary`, `analysis_json`, `template_variables`, `stop_reason` |
| **sens_risk** | **`high`** — `analysis_json` מכיל סיווגי AI על המשתמש (intent, temperature, service_of_interest). זה בדיוק מה שהרשות מתייחסת אליו: **מסקנות AI = מידע אישי**. |
| **source** | llm_generated, system_generated |
| **external** | OpenAI/Gemini (יוצרים את ה-analysis_json), אולי Sentry בכשלים 🟡 |
| **export** | ❗️**`yes` — אבל לא מכוסה ב-`get_user_data_summary` היום**. זכות עיון לפי תיקון 13 דורשת שמשתמש יוכל לראות את הסיווגים ש-AI עשה עליו. |
| **delete** | `hard_delete` (מכוסה ב-`delete_user_data`) |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`**. הצעה: 6 חודשים מ-`followup_sent_at`/`user_replied_at`/`expired_at`. |
| **purge_trig** | `followup_sent_at` / `user_replied_at` |
| **hold** | none |
| **sec_lvl** | high |
| **notes** | זו הטבלה החזקה ביותר בהיבט "שקיפות AI" של תיקון 13. לוג ההחלטה (`followup_decisions`) שדיברנו עליו אמור להזין את הטבלה הזו. |

---

### 9. `user_notes`

| | |
|---|---|
| **purpose** | פתקים פנימיים שבעל העסק כותב על משתמש |
| **subject** | end_user (admin-written) |
| **lookup** | `user_id` (PK) |
| **direct_id** | `user_id` |
| **indirect_id** | `updated_at` |
| **free_text** | **`free_text`** — `note` הוא טקסט חופשי שכותב בעל העסק |
| **sens_risk** | **`high`** — בעל העסק יכול לכתוב כל דבר: "מסרבן", "בקליניקה — סובל מ-X", "חשוד באבק". זה בדיוק מסוג השדות שהיועץ הזהיר עליהם. |
| **source** | admin_input |
| **external** | none |
| **export** | ❗️**זה אחד המקרים העדינים** — תיקון 13 כן מחייב לכלול ב-זכות עיון, **גם אם הכוונה הייתה "פנימי"**. הרשות הדגישה זאת במפורש. 🟡 לבירור משפטי. |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`**. צריך policy: למחוק יחד עם המשתמש? לתת לבעל העסק לבחור? |
| **purge_trig** | `updated_at` או deletion של המשתמש |
| **hold** | none |
| **sec_lvl** | high |
| **notes** | חשיפה משפטית גבוהה. אם משתמש מבקש עיון ובעל העסק כתב משהו פוגעני — זה נחשף. |

---

### 10. `blocked_users`

| | |
|---|---|
| **purpose** | משתמשים שבעל העסק חסם |
| **subject** | end_user |
| **lookup** | `user_id` (PK) |
| **direct_id** | `user_id`, `username` |
| **indirect_id** | `blocked_at` |
| **free_text** | `free_text` — `reason` |
| **sens_risk** | medium — `reason` יכול להכיל מידע, השם בטבלה עצמו אומר "האדם הזה נחסם" |
| **source** | admin_input |
| **external** | none |
| **export** | 🟡 **לבירור משפטי** — מצד אחד זה מידע על המשתמש, מצד שני חשיפת המנגנון פוגעת ביכולת ההגנה |
| **delete** | ❗️**מושמט בכוונה מ-`delete_user_data`** (לפי הערה בקוד: כדי שמחיקה והרשמה מחדש לא יעקפו את החסימה). הגיוני, אבל **דורש בסיס חוקי מתועד** = "אינטרס לגיטימי". |
| **retention** | 🟡 ללא הגבלה היום. לשקול: למחוק חסימות מעל 24 חודשים אם המשתמש לא ניסה לחזור. |
| **purge_trig** | `blocked_at` |
| **hold** | זה ה-hold עצמו |
| **sec_lvl** | low |
| **notes** | חשוב לתעד באופן מפורש בשם הקובץ או ב-CLAUDE.md שזה "legal hold by design". |

---

### 11. `referral_codes`

| | |
|---|---|
| **purpose** | קוד הפניה ייחודי לכל משתמש |
| **subject** | end_user |
| **lookup** | `user_id` (UNIQUE), `code` (UNIQUE) |
| **direct_id** | `user_id`, `code` |
| **indirect_id** | `created_at`, `sent` |
| **free_text** | none |
| **sens_risk** | low |
| **source** | system_generated |
| **external** | `code` נשלח למשתמש דרך Telegram/WhatsApp |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | אין retention עצמאי — חי כל עוד המשתמש קיים |
| **purge_trig** | מחיקת המשתמש |
| **hold** | none |
| **sec_lvl** | low |
| **notes** | `code` הוא indirect_id — אם דולף, אפשר לקשר לאדם. |

---

### 12. `referrals`

| | |
|---|---|
| **purpose** | תיעוד הפניה ספציפית בין שני משתמשים |
| **subject** | end_user (שני צדדים) |
| **lookup** | `id`, `referrer_id`, `referred_id` (UNIQUE), `code` |
| **direct_id** | `referrer_id`, `referred_id` |
| **indirect_id** | `code`, `status`, `created_at`, `completed_at` |
| **free_text** | none |
| **sens_risk** | low — אבל מקשר בין שני אנשים; מחיקה של אחד צריכה להישמר חלקית עבור השני |
| **source** | system_generated |
| **external** | none |
| **export** | `yes` — אבל **רק הצד של המבקש** (לא לחשוף את הצד השני בעיון) |
| **delete** | `hard_delete` של רשומות בשני הכיוונים (מכוסה) |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`**. הצעה: completed → 24 חודשים; pending מעל 6 חודשים → expire |
| **purge_trig** | `completed_at` או `created_at` |
| **hold** | dispute על בונוס |
| **sec_lvl** | low |
| **notes** | מחיקה של מבקש ה-/forget לא צריכה לחשוף את הצד השני. בייצוא עיון — להחזיר רק רשומות שהמבקש בהן צד. |

---

### 13. `credits`

| | |
|---|---|
| **purpose** | זיכויים מהפניות (מטבעות וירטואליים) |
| **subject** | end_user |
| **lookup** | `user_id`, `id` |
| **direct_id** | `user_id` |
| **indirect_id** | `amount`, `type`, `expires_at`, `created_at`, `used` |
| **free_text** | `free_text` — `reason` |
| **sens_risk** | low |
| **source** | system_generated, admin_input |
| **external** | none |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | 🟡 **לא מכוסה**. הצעה: expired credits → 12 חודשים; used → 24 חודשים (לחשבונאות) |
| **purge_trig** | `expires_at` או `created_at` + `used` |
| **hold** | accounting |
| **sec_lvl** | low |
| **notes** | אם אי פעם ימופה לכסף אמיתי — נכנס לדרישות חשבונאיות חזקות יותר. |

---

### 14. `user_subscriptions`

| | |
|---|---|
| **purpose** | סטטוס הסכמה לשידורים יזומים (broadcasts) |
| **subject** | end_user |
| **lookup** | `user_id` (PK) |
| **direct_id** | `user_id` |
| **indirect_id** | `is_subscribed`, `channel`, `consecutive_fallbacks`, `updated_at` |
| **free_text** | none |
| **sens_risk** | low |
| **source** | user_input (`/stop`, `/start`), system_generated |
| **external** | none |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) — אבל ❗️**יחד עם זה נמחקת הוכחת ה-opt-out** |
| **retention** | אין retention עצמאי |
| **purge_trig** | מחיקת המשתמש |
| **hold** | **important** — הוכחת opt-out נדרשת מצד תיקון 40 (חוק התקשורת — ספאם). |
| **sec_lvl** | medium |
| **notes** | זו רשומה שצריכה לעבור ל-`consent_ledger` לפני מחיקה (proof of opt-out). **כשנבנה את ה-ledger: `event_type=opt_out_proof` בעת קריאה ל-`/forget` או `/stop`, עם hash של user_id + ערוץ + timestamp.** לא לשכוח. |

---

### 15. `user_identities`

| | |
|---|---|
| **purpose** | מיפוי בין מזהי ערוץ (BSUID, phone, telegram_id) ל-user_id קנוני |
| **subject** | end_user |
| **lookup** | `id`, `user_id` (UNIQUE עם channel), `whatsapp_bsuid` (UNIQUE), `phone_number` |
| **direct_id** | `user_id`, **`whatsapp_bsuid`** (לפי "מאמץ סביר" של תיקון 13 — טוקן יציב של אותו אדם בכל ערוץ Meta = מזהה ישיר), **`whatsapp_parent_bsuid`** (Parent BSUID של Meta-managed portfolios — מזהה ישיר משותף בין משתמשים), `phone_number`, `username` |
| **indirect_id** | `channel`, `created_at`, `updated_at` |
| **free_text** | none |
| **sens_risk** | medium — phone_number הוא PII מובהק; BSUID/parent_bsuid הם identifiers יציבים **ולכן מזהים ישירים**, לא רק עקיפים |
| **source** | webhook, system_generated |
| **external** | WhatsApp Cloud API |
| **export** | `yes` — חלק ממיפוי הזהות |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | אין retention עצמאי — חי כל עוד המשתמש קיים |
| **purge_trig** | מחיקת המשתמש |
| **hold** | none |
| **sec_lvl** | medium |
| **notes** | חשובה במיוחד למעבר Meta ל-BSUID (אפריל–אוגוסט 2026). `whatsapp_parent_bsuid` נשמר forward-compat ל-Meta-managed portfolios — לא משמש כ-user_id (משותף בין משתמשים). מחיקה כאן בלי מחיקה ב-users עלולה להשאיר orphan. |

---

### 16. `response_pages`

| | |
|---|---|
| **purpose** | עמודי HTML ציבוריים. שלושה סוגים מובחנים בעמודה `page_type`: (1) `'legacy'` = רשומות היסטוריות שנוצרו לפני מיגרציית מערכת החבילות. תוכן מעורב — בעיקר fallback של WhatsApp אך אין אבחנה דאוגה. (2) `'whatsapp_fallback'` = תשובות ארוכות חדשות שעוקפות תקרת 1600 התווים של Twilio (תשתית פנימית, תמיד פעילה). נכתב רק ע"י `_send_as_page`. (3) `'landing'` = דפי נחיתה שיווקיים שהמפתח יוצר ידנית בראוט החדש בפאנל (פיצ'ר Premium — נחסם ע"י `has_feature("landing_page")`). |
| **subject** | end_user (תוכן AI-generated על משתמש) — רלוונטי ל-`whatsapp_fallback` ולחלק מה-`legacy`. ל-`landing` התוכן הוא תוכן שיווקי כללי. |
| **lookup** | `id` (slug), `user_id`, `page_type` |
| **direct_id** | `user_id` |
| **indirect_id** | `created_at`, `title`, `id` (slug ציבורי שמופיע בקישור), `page_type` |
| **free_text** | **`free_text`** — `content` הוא תשובת LLM מלאה (ב-fallback / legacy) או תוכן שיווקי (ב-landing) |
| **sens_risk** | medium עד high (fallback / legacy) / low (landing — תוכן ציבורי שיווקי) |
| **source** | llm_generated (fallback / legacy) / admin_manual (landing) |
| **external** | **❗️ציבורי דרך URL** — `/p/<page_id>`. מי שיש לו את הקישור רואה. אין auth. |
| **export** | `yes` (רק רשומות `whatsapp_fallback` או `legacy` עם `user_id`) |
| **delete** | `hard_delete` (מכוסה — בכל ה-page_types השייכים ל-user) |
| **retention** | 🟡 **לא מכוסה ב-`purge_old_data`**. הצעה: `legacy` + `whatsapp_fallback` — 30—90 יום; `landing` — ללא retention אוטומטי (עד שהמפתח מסיר). |
| **purge_trig** | `created_at` |
| **hold** | none |
| **sec_lvl** | medium |
| **notes** | ❗️**הסיכון הכי לא מטופל**: עמודים ציבוריים בלי תוקף. אם slug ניתן לניחוש או דולף — מידע אישי נחשף לכל. גם דפי landing — אם slug דולף, התוכן השיווקי גלוי (פחות חמור אבל לא רצוי). השדה `page_type` נוסף במיגרציה של מערכת החבילות (ראה sections 31—32 בחלק ב'). DEFAULT='legacy' — חוצץ ברור בין נתונים היסטוריים לחדשים, וכל קוד חדש שכותב ל-response_pages חייב לפסוק page_type מפורש כדי שלא ייצור 'legacy' חדש בטעות. |

---

### 17. `broadcast_message_recipients`

| | |
|---|---|
| **purpose** | רשימת נמענים בקהל מותאם אישית (audience='custom') |
| **subject** | end_user |
| **lookup** | `(broadcast_id, user_id)` PK |
| **direct_id** | `user_id` |
| **indirect_id** | `broadcast_id` (קישור לתוכן השידור) |
| **free_text** | none |
| **sens_risk** | low — אבל ההצטרפות לקבוצת היעד יכולה להסגיר משהו |
| **source** | system_generated |
| **external** | none |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | תלוי ב-`broadcast_messages` (FK CASCADE) |
| **purge_trig** | מחיקת ה-broadcast |
| **hold** | none |
| **sec_lvl** | low |

---

### 18. `broadcast_deliveries`

| | |
|---|---|
| **purpose** | מעקב אחר שליחה לכל נמען בקמפיין WhatsApp |
| **subject** | end_user |
| **lookup** | `id`, `(campaign_id, user_id)` UNIQUE, `twilio_message_sid` |
| **direct_id** | `user_id`, `twilio_message_sid` |
| **indirect_id** | `campaign_id`, `status`, `queued_at`, `sent_at`, `delivered_at`, `read_at`, `failed_at`, `error_code` |
| **free_text** | `free_text` — `error_message`, `rendered_variables_json` |
| **sens_risk** | medium — `rendered_variables_json` עלול להכיל את התוכן המלא ששלחנו לאדם (כולל שם, פרטים אישיים בתבנית) |
| **source** | system_generated, webhook |
| **external** | Twilio |
| **export** | `yes` |
| **delete** | `hard_delete` (מכוסה) |
| **retention** | 🟡 **לא מכוסה**. הצעה: 12 חודשים מ-`queued_at`. |
| **purge_trig** | `queued_at` |
| **hold** | dispute על שליחה לא מאושרת |
| **sec_lvl** | medium |
| **notes** | `rendered_variables_json` הוא שדה "תמים" שכבר ראינו שיכול להכיל הרבה — לוודא ב-export שלא נחשף תוכן של משתמש אחר. |

---

### 18.5 `consent_ledger` (חדש — תיקון 13 + תיקון 40)

| | |
|---|---|
| **purpose** | פנקס פסאודונימי של אירועי הסכמה / ביטול / מחיקה / עיון. שורד את `/forget` כדי להוכיח חוקיות לשעבר. |
| **subject** | end_user (פסאודונימי בלבד) |
| **lookup** | `subject_hash` = HMAC-SHA256(user_id\|\|channel, pepper). דטרמיניסטי: אותו אדם → אותו hash גם אחרי /forget וחזרה. |
| **direct_id** | אין — `subject_hash` עם pepper נפרד הוא **פסאודונימי**, לא מזהה ישיר |
| **indirect_id** | `subject_hash`, `channel`, `consent_version`, `event_at` |
| **free_text** | `metadata_json` (counts של מחיקה, source של opt-in וכו' — לא תוכן חופשי של משתמש) |
| **sens_risk** | `low` בודד; `medium` אם ה-pepper דולף (מאפשר reidentification ב-bruteforce על מרחב טלפונים ישראליים) |
| **source** | system_generated (מ-`record_consent_event`) |
| **external** | none |
| **export** | **`partial`** — לא בעיון רגיל. כלי מנהלים בלבד (`get_events_for_subject`). הסיבה: ledger פסאודונימי לא מיועד לחשיפה ישירה למשתמש; ה-`/myinfo` כבר מציג את `consent_given_at` משורת users. |
| **delete** | **`retain_minimal_proof`** — לא נמחק ב-`/forget`. זו התכלית. |
| **retention** | category=consent: **5 שנים מהאירוע** (תקופת התיישנות אזרחית גמישה). category=audit: **24 חודשים**. שניהם מיושמים ב-`purge_old_data`. |
| **purge_trig** | `event_at` + `category` |
| **hold** | `compromised=1` במקרה דליפת pepper — לא מוחקים את הראיה אבל מסמנים שלא ניתן להישען על אנונימיות |
| **sec_lvl** | medium |
| **notes** | אופציה א של היועץ: לקשר היסטוריה ולהיות שקופים במדיניות הפרטיות. ה-`pepper_version` מאפשר rotation forward-only. `LEDGER_PEPPER_V1` ב-env, נפרד מ-`SECRETS_ENCRYPTION_KEY`. |

---

### 19. `customer_facts` (שלב 1 — מערכת זיכרון מתמשך)

| | |
|---|---|
| **purpose** | עובדות יציבות שחולצו ע"י LLM משיחות הלקוח, להזרקה ל-context בשיחות עתידיות (preference / personal_info / relationship / vocabulary / open_issue). |
| **subject** | end_user (AI-derived) |
| **lookup** | `user_id` + `business_id`, `id`, `status` |
| **direct_id** | `user_id` |
| **indirect_id** | `business_id`, `fact_type`, `status`, `confidence`, `created_at`, `last_confirmed_at`, `access_count` |
| **free_text** | **`free_text`** — `content` (ניסוח העובדה) + `evidence` (ציטוט מהשיחה) |
| **sens_risk** | **`high`** — מסקנות AI על האדם בדיוק הקטגוריה שתיקון 13 מכוון אליה. כש-`requires_consent=true` (בריאות/פיננסי/משפחתי/דתי/מיני) — רגישות מרבית. |
| **source** | llm_generated (`source='inferred'`) או admin_input (`source='business_owner'`, עתידי) |
| **external** | OpenAI/Gemini (יוצר את ה-fact), חוזר ל-LLM כ-context בשיחות עתידיות |
| **export** | `yes` — חלק ממה שהמערכת "יודעת" על המשתמש. זכות עיון תיקון 13. |
| **delete** | `hard_delete` — מכוסה ב-`delete_user_data` (שלב 1). |
| **retention** | 🟡 לא מכוסה ב-`purge_old_data` היום. הצעה: 24 חודשים מ-`last_confirmed_at`, או deletion יחד עם `conversations` המקור. |
| **purge_trig** | `last_confirmed_at` |
| **hold** | none |
| **sec_lvl** | high |
| **notes** | `requires_consent=true` עוצר ב-`status='pending_approval'` עד אישור בעל העסק (לא חשיפה אוטומטית ל-LLM). partial UNIQUE על `(user_id, business_id, fact_type, content) WHERE status='active'` מונע כפילות. **gate של בעל העסק על PII רגיש**, לא של המשתמש — ה-`@consent_guard` הקיים של data processing מספיק. |

---

### 20. `extraction_runs` (שלב 1 — מערכת זיכרון מתמשך)

| | |
|---|---|
| **purpose** | audit log של ריצות extraction — כמה הודעות נסרקו, כמה facts יצאו, tokens שנצרכו, שגיאות. |
| **subject** | end_user (audit) |
| **lookup** | `user_id` + `business_id`, `created_at` |
| **direct_id** | `user_id` |
| **indirect_id** | `business_id`, `conversation_start`, `conversation_end`, `status`, `created_at` |
| **free_text** | `error_message` (קצר, ללא תוכן שיחה) |
| **sens_risk** | low — metadata בלבד; אין content של השיחה, רק counts ו-tokens. |
| **source** | system_generated |
| **external** | none |
| **export** | `yes` — counts בלבד ב-summary, מעניין למשתמש לדעת כמה פעמים AI ניתח אותו. |
| **delete** | `hard_delete` — מכוסה ב-`delete_user_data` (שלב 1). |
| **retention** | 🟡 הצעה: 12 חודשים מ-`created_at`. |
| **purge_trig** | `created_at` |
| **hold** | none |
| **sec_lvl** | low |
| **notes** | `error_message` חייב להישאר ללא PII — אסור לכתוב שם תוכן שיחה. |

---

## חלק ב' — טבלאות ללא PII של משתמש קצה (סקירה אבטחתית בלבד)

### 19. `kb_entries` + 20. `kb_chunks`

ידע עסקי שבעל העסק מזין. לא PII של משתמש קצה. **שיקול אבטחה**: הטקסט שבעל העסק
מזין הולך ל-OpenAI/Gemini ל-embeddings. אם יש שם בטעות PII (שמות לקוחות, טלפונים) —
זו זליגה. **toggle**: אין כיום ולידציה. 🟡 לבירור אם להוסיף בדיקה.

### 21—22. `business_hours`, `special_days`

config של ימי פעילות. אין PII.

### 23. `vacation_mode`

טקסט הודעת חופשה (`vacation_message`). אין PII.

### 24. `bot_settings`

הגדרות גלובליות של הבוט: tone, custom_phrases, custom_prompt, full_system_prompt.
**שיקול**: `custom_prompt` ו-`full_system_prompt` עלולים להכיל מידע עסקי רגיש
(אסטרטגיות, מחירים) — לא PII של משתמש קצה אבל סוד מסחרי. נשלח כל פעם ל-LLM.

**כרטיס ביקור (multi-tenant)**: העמודות `business_phone` / `business_address`
/ `business_website` מחזיקות את פרטי הקשר של העסק פר-tenant (נצרכות דרך
`config.get_business_config()`; ריק ⇒ fallback ל-env), ונערכות במסך "כרטיס
ביקור". זהו PII של **בעל העסק**, לא של משתמש קצה — מוצג ללקוחות ב-vCard/ICS,
מידע שהעסק בחר לפרסם. **שם העסק אינו כאן** — מקורו `display_name` ב-control
plane (נקבע בהקמה, מקור-אמת יחיד). לא נכלל ב-export/delete של משתמש קצה;
נמחק יחד עם קובץ ה-DB של ה-tenant.

### 25. `google_calendar_credentials`

❗️**רגיש מבחינת אבטחה**: `refresh_token`, `access_token` של בעל העסק. אם מסד
הנתונים דולף — תוקף יכול לקרוא את היומן הפרטי של בעל העסק. **🟡 חזק לדעתי
לשקול הצפנת השדות האלה ברמת היישום (Fernet עם key נפרד) גם בלי SQLCipher**. זה
תיקון נקודתי שלא דורש מיגרציה גדולה.

### 26. `business_branding`

לוגו (BLOB). אין PII.

### 27. `broadcast_messages`

תוכן השידור היזום. אין PII בתוכן (פר-משתמש זה ב-`broadcast_message_recipients`).

### 28. `broadcast_campaigns`

קמפיינים מבוססי תבנית. `variable_mapping_json`, `audience_filter_json` —
מטא-data על איך הוגדר הקמפיין. אין PII ספציפי, אבל יכול לחשוף כיצד מסננים
לקוחות.

### 29. `whatsapp_templates`

תבניות מאושרות מ-Twilio. אין PII.

### 30. `developer_reports`

דיווחי באגים מבעל העסק למפתח. **🟡 שדה `description` חופשי** — בעל העסק עלול
לכתוב "המשתמש +97250... קיבל תשובה לא נכונה". אם זה מועבר במייל למפתח — זו
העברה של PII מחוץ למסד הנתונים. צריך לבדוק את הזרימה (`developer_report_service`)
ולהוסיף הוראת UI: "אנא אל תכלול פרטים אישיים".

### 31. `subscription`

טבלה singleton (id=1) שמחזיקה את חבילת ה-SaaS של בעל העסק (basic / advanced /
premium), feature flags ידניים (JSON), `plan_started_at` לתקופת חסד, ו-grace
period days. **אין PII של משתמש קצה** — זה נתון על בעל העסק (הלקוח של המפתח).
**שיקול אבטחה**: אין סודות (API keys/tokens), התוכן לא נשלח ל-LLM. הגישה
לשינוי החבילה מוגנת ב-`/dev/subscription` עם `DEVELOPER_PASSWORD` נפרד מהאדמין
הרגיל. **לא נכלל ב-`delete_user_data`** כי לא מתייחס למשתמש קצה.

### 32. `plan_history`

audit trail לכל שינוי חבילה / override של פיצ'ר. שדות: `previous_plan`,
`new_plan`, JSON של feature flags לפני/אחרי, `reason` (טקסט חופשי שהמפתח
מקליד). **אין PII של משתמש קצה**. **שיקול אבטחה**: שדה `reason` חופשי — אם
המפתח כותב שם פרטי לקוח (לא מומלץ אבל אפשרי), יש כאן זליגה תאורטית. הצעה:
הוראה במסך `/dev/subscription` "אל תכלול שמות לקוחות". נשמר לתמיד (אין retention
אוטומטי) — הקובץ קטן ובעל ערך לאודיט חיובים.

### 33. `meta_credentials`

❗️**רגיש מבחינת אבטחה**: `access_token_encrypted` של בעל העסק לעמודי
פייסבוק/אינסטגרם. אם המפתח דולף — תוקף יכול לשלוח ולקרוא הודעות DM
בעמוד של בעל העסק. **השדה מוצפן ברמת היישום** (`utils/crypto.py`, Fernet
עם `SECRETS_ENCRYPTION_KEY`) — אותה תשתית כמו `google_calendar_credentials`.
שדות נוספים (`page_name`, `ig_username`) הם מטא-דאטה ציבורית של בעל העסק,
לא PII של משתמש קצה. **אין `user_id` של משתמש קצה** — לכן לא נכנס
ל-`delete_user_data` או ל-`get_user_data_summary`. retention: לתמיד עד
שהמשתמש מנתק יזום ב-`/admin/meta/setup` או מסיר הרשאות באפליקציה במטא.

### 35. `business_profile` (שלב 1 — מערכת זיכרון מתמשך)

טבלת singleton (במצב single-tenant: `business_id='default'`). מחזיקה את
פרופיל העסק לצרכי ה-fact extractor: `business_type`, `business_name`,
`services_json` (רשימת שירותים + aliases + categories), ו-
`what_matters_for_extraction` (טקסט חופשי שבעל העסק כותב — איזה סוגי מידע
חשובים לחילוץ פר הוורטיקל). **אין PII של משתמש קצה.** **שיקול אבטחה**:
התוכן (במיוחד `what_matters_for_extraction`) נשלח כל פעם ל-OpenAI/Gemini
כחלק מ-prompt ה-extractor — אם בעל העסק יכתוב שם בטעות מידע על לקוח, זה
ייצא ל-LLM. הוראת UI במסך הטופס: "אל תכלול שמות/טלפונים של לקוחות".
**לא נכלל ב-`delete_user_data`** כי אינו per-user. forward-compat ל-multi-
tenant: ה-PK הוא `business_id`, אבל בפועל יש שורה אחת.

---

### 34. `push_subscriptions`

מנויי Web Push של דפדפן בעל העסק — לקבלת התראות מערכת הפעלה על הודעות
חדשות בשיחה חיה כשלשונית הדשבורד סגורה. שדות: `endpoint` (URL של push
service של הדפדפן, ייחודי לכל דפדפן/מכשיר), `p256dh` + `auth` (מפתחות
הצפנת payload לפי תקן Web Push). **אין PII של משתמש קצה** — endpoint
מזהה דפדפן של הבעלים, לא לקוח. **שיקול אבטחה**: אם מישהו יחטוף את ה-DB
ויהיו לו גם VAPID private + endpoint, הוא יוכל לשלוח התראות מזויפות
ספציפיות לבעל העסק (מטרד, לא חמור). VAPID private נשמר במשתנה סביבה
(`VAPID_PRIVATE_KEY`), לא ב-DB. **לא נכלל ב-`delete_user_data`** —
המנוי מתבטל אוטומטית כש-push service מחזיר 404/410 (משתמש ביטל הרשאה
בדפדפן). הקוד מוחק בעצמו כשמקבל את הסטטוס הזה. retention: לתמיד עד
ביטול יזום.

---

### 38—40. טבלאות ה-Control Plane — `tenants`, `tenant_routes`, `tenant_secrets` (platform.db)

> **הקשר**: multi-tenant שלב 2 (ראה `docs/multi_tenant_migration_spec.md`).
> שלוש הטבלאות יושבות בקובץ SQLite **נפרד** (`DATA_DIR/platform.db`) ששייך
> למפעיל הפלטפורמה — לא לאף עסק — ומנוהל ב-`control_plane.py` בלבד.

**38. `tenants`** — רישום העסקים על הפלטפורמה: `tenant_id` (slug), `display_name`,
`status`, `plan`. **אין PII של משתמש קצה**; `display_name` הוא שם עסק (פומבי).
לא נשלח ל-LLM. גישה: CLI של המפעיל בלבד (`platform_cli`).

**39. `tenant_routes`** — מיפוי מפתחות ראוטינג נכנסים → tenant (מספר Twilio,
page_id, מפתחות webhook/widget אקראיים). **אין PII של משתמש קצה**. **שיקול
אבטחה**: מפתחות ה-webhook האקראיים הם de-facto סוד תפעולי (מי שמנחש אותם יכול
לשלוח בקשות מזויפות ל-endpoint, שעדיין מאומתות חתימה) — נשמרים ב-plaintext כי
הם משמשים lookup, אבל אקראיים (token_urlsafe 24 בייט) ולא ניתנים לניחוש.

**40. `tenant_secrets`** — ❗️**רגיש מבחינת אבטחה**: טוקני בוט טלגרם, פרטי
Twilio וכו' פר-tenant. **מוצפן Fernet חובה (fail-closed)** — בניגוד לשדות
ה-legacy, כאן `encrypt_field_strict` זורק חריגה אם `SECRETS_ENCRYPTION_KEY`
לא מוגדר, כך שסוד פלטפורמה לעולם לא נכתב בטקסט גלוי. ערכים לעולם לא מודפסים
(CLI מציג שמות בלבד). לא נשלח ל-LLM. retention: עד מחיקת ה-tenant
(`ON DELETE CASCADE`).

**42. `platform_meta` (platform.db)** — key-value תפעולי של הפלטפורמה
(‏last-run של job הגיבוי וה-keep-alive). **אין PII**. לא נשלח ל-LLM.
retention: לתמיד (מספר שורות קבוע). **גיבויים** (`backup_service`):
העתקים עקביים של קבצי ה-tenants + `platform.db` תחת `BACKUP_DIR`. הם
מכילים את **כל** ה-PII של קבצי המקור — ולכן יורשים את אותה רמת רגישות:
דורשים אותה הגנת-גישה כמו ה-DB החי (הרשאות קובץ, ובעת העלאה ל-object
storage — bucket פרטי + הצפנה בצד השרת). ה-retention שלהם
(`BACKUP_RETENTION_DAYS`, ברירת מחדל 14) נכנס לחישוב מדיניות השמירה.

**41. `admin_users` (platform.db)** — **PII של בעלי עסקים (לקוחות ה-SaaS),
לא של משתמשי קצה**: `email` (מזהה ישיר), `display_name`, `password_hash`
(‏werkzeug), `role`, `tenant_id`, `last_login_at`. **שיקולי אבטחה**:
(א) ה-hash לעולם לא מוחזר מ-API — ‏`verify_admin_login` ו-`list_admin_users`
מסירים אותו לפני החזרה (דפוס קריטי #6); (ב) אימות מריץ בדיקת סיסמה גם
כשה-email לא קיים — בלי timing oracle על קיום חשבון; (ג) אין self-registration
— משתמשים נוצרים רק ע"י מפעיל הפלטפורמה דרך ה-CLI (אין וקטור auto-admin,
דפוס קריטי #3); (ד) ‏email לא נכתב ללוגים — האודיט מתעד role+tenant בלבד
(דפוס #7). לא נשלח ל-LLM. retention: עד מחיקה יזומה ע"י המפעיל או מחיקת
ה-tenant (‏`ON DELETE CASCADE` על owner). זכויות עיון/מחיקה של בעל העסק —
מול המפעיל ישירות (יחסי ספק-לקוח, מוסדר ב-DPA).

> **מחיקת tenant מלאה (decommission)** — `control_plane.delete_tenant`
> (מעמוד "ניהול פלטפורמה" או `platform_cli delete-tenant`) מבצע מחיקה
> שורשית של לקוח: (א) מוחק את שורת `tenants`, ואיתה דרך `ON DELETE CASCADE`
> את `tenant_routes`, `tenant_secrets` ומשתמשי ה-`owner` ב-`admin_users`
> (‏`platform_admin` עם `tenant_id=NULL` אינו מושפע); (ב) מוחק מהדיסק את
> **כל** ה-data plane של העסק (`chatbot.db` + אינדקס FAISS) — כלומר כל
> ה-PII של משתמשי הקצה של אותו עסק (מ-`conversations` ועד `customer_facts`)
> נמחק יחד; (ג) שומר גיבוי אחרון תחת `BACKUP_DIR` (‏`deleted-<stamp>/`)
> לחלון שחזור. ביטול ה-webhook מול טלגרם רץ *לפני* מחיקת הטוקן. זהו מסלול
> מחיקה **ברמת-עסק** — משלים את `delete_user_data` שפועל על משתמש-קצה בודד
> בתוך ה-data plane של עסק.

---

## חלק ג' — מקרי קצה שכדאי לעקוב אחריהם

### 1. שדות "תמימים" שהם indirect_id

- `appointments.google_event_id` — מקשר ל-Google Calendar; מי שיש לו אותו יכול לקשר.
- `broadcast_deliveries.twilio_message_sid` — מאפשר join ל-Twilio.
- `referrals.code` — מי שיודע את הקוד יודע מי שלח.
- `response_pages.id` — slug ציבורי. אם קצר/ניחוש — דליפת מידע.

### 2. טקסט חופשי "שובר" סיווג

- `conversations.message`
- `appointments.notes`
- `agent_requests.message`
- `unanswered_questions.question`
- `lead_followups.conversation_summary`, `analysis_json`, `stop_reason`
- `user_notes.note`
- `response_pages.content`
- `broadcast_deliveries.error_message`, `rendered_variables_json`
- `developer_reports.description`

### 3. retention שתלוי בשדה אחר

- `appointments` — לפי `status` (passed/cancelled vs confirmed).
- `broadcast_deliveries` — אולי לפי `status` (failed לשמור יותר לדיבוג).
- `lead_followups` — לפי `status` (sent/replied/expired/cancelled).
- `credits` — לפי `used` ו-`expires_at`.

### 4. למחיקה צריך לכלול גם נגזרות

| source table | derived/cascade |
|---|---|
| `conversations` | `conversation_summaries`, `lead_followups.conversation_summary`, `response_pages` שמתבססים על שיחה, embeddings בזיכרון של RAG |
| `users` | `user_identities`, `user_subscriptions`, `user_notes`, `referral_codes`, ועוד 14 |
| `appointments` | אירוע ב-Google Calendar (חיצוני!) |
| `kb_entries` | `kb_chunks` (FK CASCADE — מכוסה) |

### 5. `tenant_id` כמזהה עקיף

המערכת רב-טננטית (כל לקוח DB נפרד). שם ה-DB / שם הטננט עצמו ("clinic_xyz")
יכול להסגיר. **🟡 לבירור**: איפה שם הטננט נשמר ב-logs / Sentry / Render.

### 6. שדות שהולכים ל-Sentry

🟡 לבדוק האם יש Sentry breadcrumbs / extras שמכילים `message` של conversations,
`note` של user_notes, או `description` של developer_reports. כיום אין `before_send` —
זה השלב הבא ברשימה.

---

## הצעדים הבאים — מהמטריצה לקוד

לפי מה שמצאנו, רשימת המשימות (לפי הסדר שהוצע קודם):

### בוצע ✅

**גל 1 — זכות עיון מורחבת:**
- `get_user_data_summary` הורחב ל-counts מלאים לכל הטבלאות עם user_id (conversations, conversation_summaries, lead_followups, referrals × 2, referral_codes, credits, unanswered_questions, response_pages, broadcast_deliveries, user_identities). הצגה ב-`/myinfo` עודכנה.

**גל 8ג — blocked_users restructure (חשיפה חלקית בעיון לפי תיקון 13):**
- 3 עמודות חדשות בטבלת `blocked_users`: `block_category` (enum סגור: abuse/spam/repeated_no_show/manual), `block_reason_internal` (טקסט פנימי, לא נחשף), `appeal_contact_method` (איך לערער, נחשף).
- migration backfill: `reason` הישן מועתק ל-`block_reason_internal` כדי לא לאבד תוכן.
- `block_user` קיבל פרמטרים `category` ו-`appeal_contact`. validation: ערך לא חוקי ל-category נופל ל-'manual'.
- `get_block_status_for_user` חדש — מחזיר רק שדות שניתן לחשוף בעיון: `blocked_month` (YYYY-MM ברזולוציית חודש, לא תאריך מדויק), `block_category`, `appeal_contact_method`. **לא** מחזיר reason, internal_reason, username.
- `get_user_data_summary` חושף `blocked: bool` + `block_status` (dict חלקי). `/myinfo` ב-Telegram ו-WhatsApp מציגים: סטטוס + קטגוריה (מתורגמת לעברית) + מועד + מסלול ערעור.
- `delete_user_data` minimization: ה-row לא נמחק (אינטרס לגיטימי לאכיפה — מניעת עקיפת חסימה ע"י הרשמה מחדש), אבל `username`, `reason`, `block_reason_internal` מתאפסים. רק `user_id` + `block_category` + `blocked_at` + `appeal_contact_method` נשארים.
- 6 טסטים חדשים. **100/100 עוברים.**

**גל 8ב — developer_reports 4 שכבות סניטיזציה (תיקון 13):**
- `utils/pii_sanitizer.py` חדש — `sanitize_pii(text)` מחליף דפוסי טלפון ישראלי (4 פורמטים: `+972`, `00972`, `05X-XXXXXXX`, `05XXXXXXXX` רצוף, קווי `0X-XXXXXXX`) ומיילים ב-`[REDACTED_PHONE]` / `[REDACTED_EMAIL]`. מחזיר NamedTuple עם counts.
- שכבה 1 (UI hint): ב-`developer_report.html` הוסף `<small>` מתחת לתיאור — "אנא אל תכלול/י מספרי טלפון, שמות לקוחות או תוכן שיחה".
- שכבה 2 (client-side warning): JS לפני submit — אם זוהה דפוס טלפון/מייל, מציג `confirm()` עם הסבר. לא חוסם, רק מבקש אישור.
- שכבה 3 (server sanitation): ב-`admin/app.py:developer_report` POST — `sanitize_pii(description_raw)` לפני `save_developer_report`. רק טקסט מסונן נשמר ב-DB.
- שכבה 4 (email/Telegram gating): אותו טקסט מסונן עובר ל-`send_report_to_developer` — הטקסט המקורי לא מועבר אף-פעם.
- 9 טסטים חדשים (פורמטים שונים, multiple PII, clean text). **94/94 עוברים.**

**גל 8א — user_notes restructure (תיקון 13, פתקי בעל העסק):**
- שתי עמודות חדשות בטבלת user_notes: `note_tags` (JSON של תגיות סגורות) ו-`withhold_reason` (חריג נקודתי לאי-חשיפה).
- `save_user_note` קיבל פרמטרים אופציונליים `tags=[]` ו-`withhold_reason=""`. ברירת המחדל היא להציג את ה-note בעיון.
- `get_user_note_full` חדש — מחזיר את כל השדות. `get_user_note` נשאר ב-API הישן (string only) ל-backward compat.
- `get_user_data_summary` חושף `user_note_text` כברירת מחדל; אם `withhold_reason` מוגדר → רק `user_note_withheld=True`. tags לא נחשפות אף פעם.
- `/myinfo` ב-Telegram מציג את התוכן המלא של ההערה (HTML escaped). WhatsApp `format_access_summary` מציג plain text. במקרה withhold — מודיע "חסויה — לפנות במייל לפירוט".
- UI banner צהוב בפופאובר ההערות ב-admin: "ההערה עשויה להיחשף ללקוח אם יבקש עיון. כתוב/י עובדות תפעוליות, לא דעות אישיות." ניסוח של היועץ.
- 6 טסטים חדשים. **85/85 עוברים.**

**גל 7 — מסך הסכמה v2 (אזכור AI/חו"ל מודגש + אימות גיל אקטיבי):**
- `bot/handlers.py:_consent_message_text`: שכתוב מלא של מסך ההסכמה ב-Telegram. עיבוד AI ו-OpenAI/Google בארה"ב הועלו לסעיף ייעודי מודגש (לא bullet אחד). דרישת גיל 18+ קיבלה כותרת משלה. הוסף `/stop` כפעולה אופציונלית.
- `bot/handlers.py:_build_consent_keyboard`: טקסט כפתור האישור הוחלף ל-"✅ אני מסכים/ה ובן/בת 18+" — אישור הגיל מוצמד לפעולת ההסכמה עצמה (Telegram inline keyboards לא תומכים ב-toggle נפרד).
- `database.py:CURRENT_CONSENT_VERSION` הועלה מ-1 ל-2 — `has_consent` יחזיר False למשתמשי v1 קיימים, מה שיציג להם את מסך ההסכמה החדש בפנייה הבאה. כשיאשרו, ייכתב `consent_superseded` ל-ledger (אופציה א של היועץ — קישור היסטוריה).
- `messaging/whatsapp_webhook.py:_send_welcome_message`: הוסף disclosure block למשתמשים חדשים ב-WhatsApp עם 18+, AI processing בארה"ב, וקיצור דרך ל-`מחק אותי`/`המידע שלי`. בלי אקטיבי-checkbox כי WhatsApp Quick Reply לא תומך — disclosure בלבד הוא הקו המינימלי לעמוד מולו תחת תיקון 13.
- `docs/legal/privacy.md` סעיף 11: עודכן להבהיר שאישור ההסכמה בכל ערוץ מהווה גם הצהרת גיל.
- 7 טסטים חדשים. 79/79 עוברים.

**גל 6 — WhatsApp privacy router (תיקון 13 ב-WhatsApp):**
- `messaging/whatsapp_privacy.py` חדש — מטפל בבקשות מחיקה ועיון לפני ה-LLM/RAG.
- הפרדה ברורה משני סוגי ביטול: `הסר`/`stop` → opt_out marketing בלבד (תיקון 40, ב-`whatsapp_optout.py` הקיים); `מחק אותי`/`ביטול הסכמה` → מחיקה מלאה.
- אישור דו-שלבי למחיקה: הודעה 1 (אזהרה + הסבר על ledger + קישור ל-`/legal/privacy`), הודעה 2 (הוראת אישור עם `אישור מחיקה`). cache 10 דקות ב-`_pending_deletes` עם `threading.Lock`.
- זיהוי שלילה: `אל תמחק אותי` לא נחשב מחיקה (word-boundary match + token check לפני ה-keyword).
- בקשת עיון (`המידע שלי` / `מה אתם יודעים עליי`): מתעד `access_requested` + `access_delivered` ל-ledger, מציג summary בעברית פשוטה (plain text + `*bold*`, לא HTML).
- 13 טסטים חדשים. **72/72 עוברים.**

**גל 5 — partial failures + retry queue (לפי המלצות היועץ על delete_user_data):**
- `delete_user_data` סיווג event_type סופי לפי תוצאה: `deletion_completed` עם `metadata.status=full|partial`, או `deletion_failed` (חדש) כשכלום לא נמחק. מונע באג שבו שאילתה אינטואיטיבית `WHERE event_type='deletion_completed'` תיתן 100% הצלחה כשבפועל יש partial.
- Idempotency check ברמת command: `_active_deletions: dict` עם TTL 60s + `threading.Lock`. שתי קריאות מקבילות → השנייה מחזירה `{"already_in_progress": True}`. `forget_callback` מטפל בתגובה.
- `ledger_write_retry` טבלה חדשה — משרתת 3 תרחישים בטבלה אחת (pepper חסר, DB write failure, deletion partial). `record_consent_event` במקרה כשל מכניס payload (עם user_id+channel גלויים — כדי שניתן יהיה לחשב hash אחר כך אם pepper חוזר). `process_ledger_retry_queue()` רץ אוטומטית מתוך `purge_old_data` היומי. `pepper_still_missing` לא מגדיל attempts (מונע ניצול מהיר של 5 ניסיונות). אחרי 5 כשלים → log `[LEDGER_RETRY_EXHAUSTED]` לחיפוש ב-Render logs.
- 11 טסטים חדשים (6 ל-failure semantics + idempotency, 5 ל-retry queue). 59/59 עוברים.

**גל 4 — שקיפות מדיניות פרטיות (ניסוח של היועץ):**
- `docs/legal/privacy.md` — סעיף חדש 6.1 "שמירת הוכחת הסכמה לאחר מחיקה" (גרסה מלאה: מה נמחק, מה נשאר, פסאודונימיזציה במונחים פשוטים, מסגרות זמן לפי 4 סוגי רשומות, מקרה של חשבון חדש, מה קורה בדליפת מפתח).
- `bot/handlers.py:forget_command` — טקסט אישור המחיקה הוחלף לגרסה הקצרה של היועץ (שקיפות לגבי מה שנשאר, קישור לסעיף המלא דרך `ADMIN_URL/legal/privacy`).

**גל 3 — consent_ledger (לפי סקיצת היועץ):**
- טבלה חדשה `consent_ledger` עם 2 קטגוריות (consent / audit), `pepper_version`, `compromised` flag.
- `utils/consent_ledger.py` עם 9 event_types, HMAC-SHA256 + `LEDGER_PEPPER_V1` (env נפרד).
- אינטגרציה: `record_consent` → `consent_given` / `consent_superseded`. `revoke_consent` → `consent_revoked`. `delete_user_data` → `deletion_requested` (לפני) + `deletion_completed` (אחרי, עם counts). `set_wa_marketing_opt_in/out` → `opt_in_marketing` / `opt_out_marketing` (תיקון 40). `/myinfo` → `access_requested` + `access_delivered`.
- retention ב-`purge_old_data`: 5 שנים ל-consent, 24 חודשים ל-audit.
- `mark_pepper_compromised(version)` להגנה על קייס דליפה.
- 12 טסטים חדשים: דטרמיניסטיות, no-fragmentation אחרי `/forget`+re-consent, retention מותנה קטגוריה, silent failure בלי pepper, sigh-up superseded, opt-in/out, mark_compromised. **48/48 עוברים.**

**גל 2 — אחרי תשובות היועץ:**
- **`response_pages` slug** — `secrets.token_urlsafe(16)` = 22 תווים base64url (128 ביט אנטרופיה). היה `uuid.uuid4().hex[:8]` (32 ביט).
- **`response_pages` headers + rate limit** — `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. rate limit 60 req/min/IP על `/p/<slug>` ו-`/ics/<slug>` (כולל 404, חוסם ניחוש מסיבי).
- **`response_pages` TTL** — 30 יום ב-`purge_old_data` (default; קונפיגורבילי).
- **הצפנת `google_calendar_credentials`** — `utils/crypto.py` עם Fernet. `refresh_token` ו-`access_token` נשמרים `v1:<ciphertext>`. מפתח ב-`SECRETS_ENCRYPTION_KEY` env var (נפרד מה-DB). תומך ב-key rotation (prefix `vN:`) וב-legacy plaintext (תקופת מעבר). migration אוטומטי במעבר ראשון אם המפתח מוגדר.
- **הרחבת `purge_old_data`** ל-7 טבלאות נוספות עם המספרים שהיועץ נתן: agent_requests (12 חודשים), unanswered (open: 90 יום, resolved: 6 חודשים), lead_followups (6 חודשים), referrals (completed: 24, pending: 6), credits (expired: 12, used: 24), broadcast_deliveries (12 חודשים, failed: 18), response_pages (30 יום).
- **תיקוני מטריצה לפי הערות היועץ**: conversation_summaries retention זהה למקור; user_subscriptions עם הערה ל-`event_type=opt_out_proof` ב-ledger העתידי; whatsapp_bsuid עבר ל-direct_id.
- 13 טסטים חדשים (5 retention + 3 slug security + 5 encryption). 36/36 עוברים.

### לדיון

1. **רעיון: `followup_decisions` log** — לתמיכה ב"שקיפות AI" של תיקון 13.
2. **`appointments` 36 חודשים** — היועץ ממליץ להוריד ל-18-24 כללי, או לפי tenant vertical. דורש החלטה עסקית.

### דוחה (לא רלוונטי)

- ~~Sentry `before_send`~~ — Sentry לא מחובר בפועל.

---

## פתקים אחרונים

- ה-`admin_audit_log` שדאגנו לו אינו DB table — הוא `logger.info("AUDIT \| ...")`
  שהולך ל-stdout (Render logs) ול-Sentry. זה מוריד מטלת retention DB אבל מעלה
  שאלה: מה ה-retention של Render logs? של Sentry? 🟡 לבירור.
- `kb_chunks.embedding` הוא BLOB. לא PII של משתמש קצה, אבל אם בעל העסק שם בטעות
  PII בידע — ה-embedding "יודע" עליו.
- כל הטבלאות עם `BLOB` (`kb_chunks.embedding`, `business_branding.logo_blob`)
  לא נחשפות בייצוא טקסטואלי רגיל — לוודא שלא מועברות בטעות ב-CSV/JSON של
  זכות עיון.
