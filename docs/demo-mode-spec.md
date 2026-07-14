# Demo Mode — ספק טכני (V3)

> **סטטוס:** מאושר למימוש. החלפת V2 שהציעה דפלוימנט נפרד — overkill ל-$7/חודש ותחזוקה כפולה.

## החלטות מסוכמות

1. **אותו service, אותו DB.** בלי $7/חודש נוספים.
2. **הגנה ב-3 שכבות:** session flag + middleware שחוסם כתיבה + stubs ליציאות חיצוניות.
3. **Tests** שאוכפים שאי-אפשר לכתוב כ-demo.
4. **בוט חי דרך Telegram** עם 30 הודעות/יום per chat_id.

## הקשר ארכיטקטוני שצריך להבין

המערכת היא **single-tenant per deployment** (לפי CLAUDE.md). אין טבלת `businesses` שמפרידה לקוחות. ה-deployment הזה (`ai-business-chatbot-kjm4.onrender.com`) **הוא הדמו** — כל הנתונים בו הם seed של "מכון היופי של דנה", לא לקוח אמיתי. לקוח אמיתי יקבל deployment משלו.

מסקנות:
- אין דאגת PII של "לקוחות אמיתיים" — אין כאלה ב-deployment הזה.
- אין צורך ב-`is_demo_business` flag — כל ה-deployment הוא הדמו.
- ה-`is_demo` שייך לשכבת ה-**session** של פאנל הניהול, לא לטבלת `users` (ש-aam מכילה משתמשי קצה של הבוט, לא אדמינים).

## הגנה ב-3 שכבות

### שכבה 1 — session flag + auto-login

ב-`admin/app.py`:

```python
@app.route("/demo")
def demo_entry():
    session.clear()
    session["logged_in"] = True
    session["demo"] = True
    return redirect(url_for("dashboard"))
```

ה-login_required הקיים ימשיך לעבוד (`session["logged_in"] = True`). הדגל `session["demo"]` מבדיל בין אדמין רגיל לגולש דמו.

הקמפיין הממומן מוביל ישירות ל-`/demo`.

### שכבה 2 — middleware שחוסם כתיבה

`before_request` חדש (אחרי `_enforce_feature_flags` ב-`admin/app.py:780`):

```python
DEMO_WRITE_ALLOWLIST = {
    "/logout",
    "/demo/track",  # analytics בלבד
}

@app.before_request
def _enforce_demo_readonly():
    if not session.get("demo"):
        return None
    if request.method == "GET":
        return None
    if request.path in DEMO_WRITE_ALLOWLIST:
        return None
    if request.path.startswith("/static/"):
        return None
    # ── שתי תגובות לפי סוג הבקשה ──
    # HTMX (form עם hx-post וכו') → 200 + fragment toast + HX-Retarget
    # ל-#demo-toast-container; HTMX יחליף שם בלי לשבור את הטופס.
    # POST רגיל (form classic) → 302 redirect חזרה לדף המקור עם flash,
    # כי החזרת fragment HTML כעמוד שלם תוצג כעמוד שבור.
    if request.headers.get("HX-Request"):
        html = render_template("_partials/demo_blocked_toast.html")
        resp = make_response((html, 200))
        resp.headers["HX-Retarget"] = "#demo-toast-container"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    flash("במצב דמו השינויים לא נשמרים.", "info")
    return redirect(_safe_redirect_back(url_for("dashboard")))
```

ה-toast יוצג ב-`#demo-toast-container` גלובלי שיתווסף ל-`base.html`. HTMX יחליף את התוכן שם בלי לשבור טפסים.

### שכבה 3 — stubs ליציאות חיצוניות

ב-DEMO_MODE (env var), היציאות החוצה הופכות ל-no-op:

| מודול | נקודת הכניסה | התנהגות |
|---|---|---|
| Twilio (WhatsApp) | `messaging/whatsapp_sender.py:send_whatsapp` | `if DEMO_MODE: log + return success-stub` |
| Broadcasts | `broadcast_service.py:_send_one` | `if DEMO_MODE: log + return success-stub` |
| Google Calendar writes | מקומות שעושים `events().insert/update/delete` | `if DEMO_MODE: log + skip` |

ה-stubs מגנים מפני שני תרחישים:
- bug ב-middleware שמאפשר POST.
- background jobs שלא עוברים דרך request (cron של broadcast, follow-ups).

**OpenAI calls נשארים אמיתיים** — בלעדיהם הדמו לא משכנע. אבל מוגבלים ב-rate limit חזק (ראו "בוט חי").

## הבוט החי — Telegram, 30/יום

- בוט Telegram אחד (אותו `TELEGRAM_BOT_TOKEN` של ה-deployment).
- Banner ב-dashboard עם לינק `https://t.me/<bot_username>?start=demo`.
- כתב הסבר: "WhatsApp עובד בדיוק אותו דבר — אותו קוד, אותו RAG."
- **Rate limit:** 30 הודעות/יום per chat_id (במקום ברירת המחדל 100/יום ב-`config.py:96`). מספיק לסיור משכנע, לא יקר.
- consent screen — `CONSENT_SCREEN_ENABLED=false` (גולש דמו לא צריך להיתקע).
- כתב ויתור בכתיבה ראשונה לבוט: "זו סביבת דמו. אל תשלח פרטים אישיים."

## env vars חדשים

```bash
# מצב דמו — מפעיל את ה-/demo route ואת ה-stubs
DEMO_MODE=true

# rate limit מחמיר יותר בדמו (ברירת מחדל 100)
RATE_LIMIT_PER_DAY=30

# CTA שאליו הגולשים פונים לרכישה
DEMO_CTA_WHATSAPP="+972501234567"
```

## UI

### Banner sticky (`base.html`, מתחת ל-navbar, רק כש-`session.demo`)
```
🎬 מצב דמו — הנתונים הם דוגמה. השינויים שלך לא יישמרו.
[רוצה אחד משלך? דבר איתנו →]
```

### CTA floating (bottom-left, רק כש-`session.demo`)
```
💬 רוצה כזה לעסק שלך?
   [WhatsApp]
```

### Card ב-dashboard
```
🤖 דבר עם הבוט עכשיו
   הבוט שאתה רואה כאן מחובר ל-KB.
   [פתח בטלגרם]
   * WhatsApp עובד בדיוק אותו דבר.
```

## Routes שמושפעים

| route | התנהגות ב-`session.demo` |
|---|---|
| `/dev/*` | חסום (302 ל-`/`). אין צורך לחשוף מסך מפתח לדמו. |
| `/integrations/*/connect` | GET עובד, POST חסום (toast). |
| `/billing`, `/my-plan` | תצוגת "Pro" קבועה. |
| `/kb/rebuild` | חסום עם toast. |
| `/api/*` שכותבים | חסומים. קוראים — פתוחים. |
| `/demo/track` | פתוח לכתיבה (analytics). |

## Tests חובה

ב-`tests/test_demo_mode.py`:

1. `test_demo_session_blocks_post` — POST ל-`/kb/add` עם `session.demo=True` מחזיר את ה-toast, לא יוצר רשומה.
2. `test_demo_session_blocks_delete` — `DELETE` חסום.
3. `test_demo_session_allows_get` — `/dashboard`, `/conversations` נגישים.
4. `test_demo_entry_creates_session` — `GET /demo` מציב את הדגלים הנכונים ומפנה.
5. `test_demo_blocks_dev_routes` — `/dev/login` חסום.
6. `test_demo_stub_whatsapp` — קריאה ל-`send_whatsapp` ב-DEMO_MODE לא קוראת ל-Twilio.
7. `test_normal_admin_unaffected` — login רגיל (לא דמו) עדיין יכול לכתוב.

## Analytics — minimal

טבלה חדשה `demo_events(id, event_type, path, ts, session_id)`. POST ל-`/demo/track` נכתב אליה (זה ה-write היחיד שמותר בדמו). שאילתת אגרגציה ב-`/dev/demo-stats` (זמין רק לאדמין רגיל, לא לדמו).

אירועים: `page_view`, `cta_click`, `live_bot_click`, `blocked_action`.

## Rollout

### Phase 1 — Core (יום)
1. `DEMO_MODE` ב-`config.py`.
2. `/demo` route + `session["demo"]` ב-`admin/app.py`.
3. `_enforce_demo_readonly` middleware.
4. `_partials/demo_blocked_toast.html` + `#toast-container` ב-`base.html`.
5. Banner + CTA floating ב-`base.html` מותנים ב-`session.demo`.

### Phase 2 — Stubs + Live Bot (יום)
6. בדיקת DEMO_MODE ב-`whatsapp_sender.py`, `broadcast_service.py`.
7. Card "פתח בטלגרם" ב-dashboard, מותנה ב-`session.demo`.
8. Rate limit מותאם (30/יום) — דרך env var, אין שינוי קוד.

### Phase 3 — Tests + Seed enrichment (יום)
9. `tests/test_demo_mode.py` (7 הטסטים לעיל).
10. הרחבת `seed_data.py` ל-60+ שיחות, 30+ לקוחות, 20+ פגישות (אם חסר).

### Phase 4 — Analytics (חצי יום, אופציונלי ל-V1)
11. טבלת `demo_events` + `/demo/track`.

### Phase 5 — Launch
12. **Launch gate (חובה):** השלמת המיטיגציה לסיכון השיחות הטלגרמיות (ראו "Launch Gate" למטה).
13. הפעלת `DEMO_MODE=true` ב-Render env vars.
14. קמפיין פייסבוק → `https://ai-business-chatbot-kjm4.onrender.com/demo`.

## Launch Gate — חובה לפני Phase 5

**הסיכון:** `/conversations` חושף את כל היסטוריית הבוט. אם בבוט הטלגרם הקיים יש שיחות של משתמשים אמיתיים — הן ייחשפו לכל גולש דמו מהקמפיין. זה דליפת PII, לא הערה ויזואלית.

**Acceptance criteria:** `/conversations` חייב להציג **רק** שיחות seed (פיקטיביות) או שיחות שמשתמשים מודיעים כלגיטימיות לחשיפה. אין דרך חוקית להעלות לאוויר בלי שאחת מהבאות מתקיימת ומתועדת:

| מיטיגציה | מה צריך לעשות | תיעוד נדרש |
|---|---|---|
| **A.** טוקן בוט טלגרם נפרד לדמו | יצירת `@<name>DemoBot` חדש ב-BotFather, הצבת הטוקן ב-`TELEGRAM_BOT_TOKEN`, **לא** ה-token של בוט קיים שיש לו משתמשים. | מסמך עם שם הבוט החדש, חתימת בעלים, תאריך. |
| **B.** Purge מאומת של היסטוריית שיחות | `DELETE FROM conversations; DELETE FROM users; DELETE FROM appointments WHERE ...` לפני launch, ואז `--seed`. וידוא ידני ש-`/conversations` ריק חוץ מ-seed. | תאריך ה-purge, מי הריץ, מספר רשומות שנמחקו, צילום מסך של `/conversations` אחרי. |

אסור לעלות עם "אופציה" כי שתי הדרכים שוות, או "נחליט אחר כך". **לפני Phase 5** — בחירה מתועדת ובוצעה.
