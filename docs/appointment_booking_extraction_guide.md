# מדריך חילוץ — בוט תורים לפי יומן Google (הצעת slots פנויים + אישור)

מסמך זה מחלץ מהריפו `ai-business-bot` את הפיצ'ר המלא של **הצעת תורים לליד לפי מקומות פנויים ביומן Google של בעל העסק**, על כל שלביו: חיבור היומן, חישוב slots פנויים, ה-flow של בחירת תור, מנוע ההחלטה (אישור אוטומטי/ידני), סנכרון ליומן, ותזכורות.

זה הפיצ'ר הכי "מכירתי" — בוט שסוגר תורים לבד 24/7. הוא משלים את המדריך הקודם (`whatsapp_messaging_extraction_guide.md`): ה-templates שם הם ה-UI להצגת ה-slots (List Picker).

---

## 0. סקירה — הזרימה המלאה

```
  בעל העסק (פעם אחת)                      ליד (כל פנייה)
  ┌────────────────┐              ┌──────────────────────────────┐
  │ OAuth → יומן    │              │ "אני רוצה תור"               │
  │ Google מחובר    │              └──────────────┬───────────────┘
  │ (token מוצפן)   │                             ▼
  └────────────────┘              ① בחירת שירות (List Picker)
         │                                       ▼
         │  freebusy                ② בחירת תאריך (List Picker)
         ▼                                       ▼
  ┌──────────────────────────────────────────────────────────┐
  │ get_available_slots(date, duration):                      │  ← הליבה (§2)
  │   business_hours  ∩  NOT(calendar busy)  ∩  NOT(DB appts)  │
  │   = ["09:00","09:30","11:30",...]                          │
  └──────────────────────────────────────────┬───────────────┘
                                              ▼
                            ③ בחירת שעה  →  ④ אישור
                                              ▼
                   ┌──────────────────────────────────────┐
                   │ gather_and_decide()                   │  ← מנוע החלטה (§4)
                   │   manual → pending (בעל העסק מאשר)     │
                   │   auto_with_check → confirmed/rejected │
                   └──────────────────┬───────────────────┘
                                      ▼ confirmed
              create_appointment(DB) → create_event(GCal) → אישור לליד + ICS
                                      ▼
                          תזכורות (24h + X שעות לפני)  ← scheduler (§7)
```

**עקרונות-על:**
1. **שלושה מקורות busy מצטלבים** — שעות פעילות, יומן Google, ותורים ב-DB. ה-DB הוא source of truth שמונע double-booking גם כש-GCal מנותק.
2. **מנוע החלטה מופרד** — לוגיקת "לאשר/לדחות/להמתין" יושבת בפונקציה טהורה אחת, נפרדת מה-flow ומהערוץ.
3. **idempotent reminders** — polling כל 30 דק' + דגלי DB, לא scheduling מדויק.

---

## 1. Google Calendar — חיבור, אחסון מוצפן, refresh, בריאות

### OAuth flow (`google_calendar.py`)

```python
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_oauth_flow() -> Flow:
    client_config = {"web": {
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [GOOGLE_REDIRECT_URI],
    }}
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


def get_authorization_url(state: str = "") -> tuple[str, str]:
    flow = get_oauth_flow()
    url, _ = flow.authorization_url(
        access_type="offline",      # ← קריטי: מבטיח refresh_token
        prompt="consent",           # ← מאלץ אישור → refresh_token חדש
        state=state,                # ← CSRF
    )
    return url, flow.code_verifier  # PKCE


def exchange_code_for_credentials(code: str, code_verifier: str = "") -> dict:
    flow = get_oauth_flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    # שולפים email + timezone של היומן
    service = build("calendar", "v3", credentials=creds)
    info = service.calendars().get(calendarId="primary").execute()
    db.save_google_calendar_credentials(
        google_account_email=info.get("id", "primary"), calendar_id="primary",
        refresh_token=creds.refresh_token or "", access_token=creds.token or "",
        token_expiry=creds.expiry.isoformat() if creds.expiry else "",
        timezone=info.get("timeZone", "Asia/Jerusalem"),
    )
    return {"email": info.get("id"), "timezone": info.get("timeZone")}
```

Routes באדמין: `GET /google/connect` (שומר `state`+`code_verifier` ב-session) → `GET /google/callback` (מאמת state, מחליף code) → `POST /google/disconnect`.

### אחסון מוצפן

טבלה עם **שורה יחידה** (`CHECK(id = 1)`) — יש בעל-עסק אחד:

```sql
CREATE TABLE IF NOT EXISTS google_calendar_credentials (
    id                   INTEGER PRIMARY KEY CHECK(id = 1),
    google_account_email TEXT DEFAULT '',
    calendar_id          TEXT DEFAULT 'primary',
    refresh_token        TEXT DEFAULT '',       -- מוצפן (Fernet)
    access_token         TEXT DEFAULT '',       -- מוצפן
    token_expiry         TEXT DEFAULT '',
    timezone             TEXT DEFAULT 'Asia/Jerusalem',
    auth_invalid_at      TEXT DEFAULT NULL,     -- בריאות: refresh נכשל
    owner_alert_sent_at  TEXT DEFAULT NULL,     -- בריאות: התראה נשלחה
    updated_at           TEXT DEFAULT (datetime('now'))
);
```

```python
# הצפנה לפני שמירה (utils/crypto.py — Fernet, AES-128-CBC+HMAC)
def save_google_calendar_credentials(..., refresh_token, access_token, ...):
    from utils.crypto import encrypt_field
    conn.execute("UPDATE google_calendar_credentials SET refresh_token=?, access_token=?, "
                 "auth_invalid_at=NULL, owner_alert_sent_at=NULL, ... WHERE id=1",
                 (encrypt_field(refresh_token), encrypt_field(access_token), ...))
```

`encrypt_field` מחזיר `v1:<base64>` (prefix לרוטציית מפתח). מחרוזת ריקה `''` **לא** מוצפנת — מאפשר לבדוק "אין token" בלי פענוח.

### Token refresh + ניטור בריאות (`_get_credentials`)

הלקח הכי חשוב מהריפו: refresh_token יכול להישלל (משתמש מנתק את ההרשאה). הקוד מזהה את זה, מסמן דגל, ומתריע לבעל העסק **פעם אחת**:

```python
def _get_credentials() -> Credentials | None:
    cred_data = db.get_google_calendar_credentials()
    if not cred_data or not cred_data.get("refresh_token"):
        return None
    creds = Credentials(token=cred_data.get("access_token"), refresh_token=cred_data["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET, expiry=expiry)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            db.update_google_calendar_token(creds.token, creds.expiry.isoformat())
            if db.is_google_calendar_auth_invalid():
                db.clear_google_calendar_auth_invalid()      # התאושש → איפוס דגל
        except RefreshError:                                  # token נשלל!
            if db.set_google_calendar_auth_invalid():        # מחזיר True רק בפעם הראשונה
                _notify_owner_calendar_disconnected()        # ← התראה אחת, לא spam
            return None
    return creds


def is_connected() -> bool:
    """מחובר *ובריא* — refresh_token קיים ו-auth_invalid_at לא מסומן."""
    cred_data = db.get_google_calendar_credentials()
    if not (cred_data and cred_data.get("refresh_token")):
        return False
    return not cred_data.get("auth_invalid_at")
```

**נקודות מפתח:**
- **`access_type="offline"` + `prompt="consent"`** — בלי שניהם לא תקבל `refresh_token` ולא תוכל לגשת ליומן אחרי שה-access token פג (שעה).
- **שורה יחידה (`CHECK(id=1)`)** — תבנית נקייה ל-singleton config. בפרויקט מרובה-עסקים, החלף ב-`business_id` PK.
- **הצפנת refresh_token** — זה credential ארוך-טווח. דליפה = גישה ליומן של הלקוח. Fernet עם `SECRETS_ENCRYPTION_KEY`.
- **`set_..._auth_invalid()` מחזיר True רק פעם ראשונה** — דפוס anti-spam: התראה אחת לכל אירוע ניתוק, לא בכל retry.
- **`is_connected()` בודק בריאות, לא רק קיום** — מונע להציע slots שלא ניתן לכתוב אליהם.

---

## 2. חישוב Slots פנויים ⭐ (הליבה)

### הדפוס: חיתוך שלושה מקורות

```
   slots פנויים = [גריד 30 דק' בתוך שעות הפעילות]
                  MINUS  [טווחים תפוסים ביומן Google]   (+ buffer אחרי אירוע)
                  MINUS  [תורים pending/confirmed ב-DB] (+ buffer בין תורים)
```

ה-`get_busy_slots` קורא את ה-FreeBusy API:

```python
def get_busy_slots(time_min, time_max, timezone="Asia/Jerusalem") -> list[dict]:
    service = _get_calendar_service()
    if not service:
        raise CalendarUnavailable("auth/connection")   # ← מובחן מ-"אין תורים"
    body = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
            "timeZone": timezone, "items": [{"id": calendar_id}]}
    result = service.freebusy().query(body=body).execute()
    return result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    # → [{"start": "...T14:00:00Z", "end": "...T15:00:00Z"}, ...]
```

### האלגוריתם (`get_available_slots`)

```python
def get_available_slots(target_date, service_duration_minutes=60,
                        buffer_after_minutes=0, buffer_after_event_minutes=0) -> list[str]:
    # ① שעות פעילות — יום סגור = אין slots
    day_status = get_status_for_date(target_date)        # §8
    if not day_status.get("is_open"):
        return []
    tz = ZoneInfo("Asia/Jerusalem")
    day_start = datetime.combine(target_date, open_time, tzinfo=tz)
    day_end   = datetime.combine(target_date, close_time, tzinfo=tz)

    # ② היום? לעגל למעלה ל-slot הבא (לא להציע שעות שעברו)
    now = datetime.now(tz)
    if target_date == now.date() and now > day_start:
        next_slot = ((now.hour*60 + now.minute)//30 + 1) * 30
        if next_slot >= 24*60:
            return []
        day_start = now.replace(hour=next_slot//60, minute=next_slot%60, second=0, microsecond=0)

    # ③ busy מהיומן (+ buffer אחרי כל אירוע)
    busy_ranges = []
    event_buffer = timedelta(minutes=max(0, buffer_after_event_minutes))
    for slot in get_busy_slots(day_start, day_end):
        start = datetime.fromisoformat(slot["start"]).astimezone(tz)
        end   = datetime.fromisoformat(slot["end"]).astimezone(tz) + event_buffer
        busy_ranges.append((start, end))

    # ④ busy מה-DB (תורים pending/confirmed) — source of truth!
    for start_min, end_min in db.get_appointments_busy_ranges(target_date.isoformat()):
        db_start = datetime.combine(target_date, time(0,0), tzinfo=tz) + timedelta(minutes=start_min)
        db_end   = datetime.combine(target_date, time(0,0), tzinfo=tz) + timedelta(minutes=end_min) + event_buffer
        busy_ranges.append((db_start, db_end))

    # ⑤ גריד 30 דק' — slot פנוי אם לא חופף לאף טווח תפוס
    slot_duration = timedelta(minutes=service_duration_minutes + buffer_after_minutes)
    available, current = [], day_start
    while current + timedelta(minutes=service_duration_minutes) <= day_end:
        slot_end = current + slot_duration
        # חפיפה: slot_start < busy_end  AND  slot_end > busy_start
        is_free = not any(current < be and slot_end > bs for bs, be in busy_ranges)
        if is_free:
            available.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)
    return available
```

ה-busy מה-DB (`get_appointments_busy_ranges`) — דקות מחצות, כולל ה-duration לכל תור:

```python
def get_appointments_busy_ranges(date_str: str) -> list[tuple[int, int]]:
    default_min = db.get_appointment_duration_settings().get("default_minutes", 60)
    rows = conn.execute("SELECT preferred_time, confirmed_duration_minutes FROM appointments "
                        "WHERE preferred_date=? AND status IN ('pending','confirmed')", (date_str,))
    ranges = []
    for r in rows:
        h, m = map(int, r["preferred_time"].split(":"))
        start = h*60 + m
        ranges.append((start, start + int(r["confirmed_duration_minutes"] or default_min)))
    return ranges
```

**נקודות מפתח:**
- **חפיפה: `start < busy_end AND end > busy_start`** — הנוסחה הקנונית לבדיקת overlap בין שני טווחים. שווה לשנן.
- **DB busy ranges = source of truth** — תורים `pending`/`confirmed` חוסמים slots **גם אם GCal לא מסונכרן או מנותק**. זה מה שמונע double-booking בפועל; ה-FreeBusy הוא תוספת.
- **שלושה סוגי buffer:** `service_duration` (משך השירות), `buffer_after_minutes` (מרווח בין תורים), `buffer_after_event_minutes` (מרווח אחרי אירוע יומן — למשל זמן נסיעה). כל אחד מרחיב busy בצורה שונה.
- **גריד 30 דק'** — offsets עקביים (09:00, 09:30...). השירות יכול להיות 60 דק' אבל ה-slots מתחילים כל 30.
- **עיגול ל-slot הבא היום** — לא להציע 09:00 בשעה 09:15.
- **`CalendarUnavailable` מובחן מ-"אין busy"** — אם ה-API נכשל, זה לא אומר "הכל פנוי". מנוע ההחלטה (§4) מתייחס לזה כ-`calendar_check_failed` → pending, לא confirmed.

---

## 3. Booking Flow (state machine, שני ערוצים)

### הדפוס

ארבעה שלבים: **service → date → time → confirm**. Telegram משתמש ב-`ConversationHandler` (state ב-`context.user_data`); WhatsApp ב-state machine ידני in-memory (אין ConversationHandler). אותם שלבים, UI שונה.

```python
# Telegram (bot/handlers.py)
BOOKING_SERVICE, BOOKING_DATE, BOOKING_TIME, BOOKING_CONFIRM = range(4)

# WhatsApp (conversation_state.py) — state בזיכרון, timeout 30 דק'
STATE_BOOKING_SERVICE = "booking_service"
STATE_BOOKING_DATE = "booking_date"
STATE_BOOKING_TIME = "booking_time"
STATE_BOOKING_CONFIRM = "booking_confirm"
```

### הצגת slots לפי ערוץ

| שלב | WhatsApp | Telegram |
|---|---|---|
| שירות | **List Picker** (≤10) או רשימה ממוספרת | טקסט חופשי |
| תאריך | **List Picker** (≤10 + pagination) | **לוח שנה inline** (גריד חודשי) |
| שעה | טקסט (slots מוצעים בטקסט) | טקסט |
| אישור | **Quick Reply** (2 כפתורים) | טקסט "כן/לא" |

ה-List Picker ב-WhatsApp נשען על Twilio Content templates (מהמדריך הקודם):

```python
# whatsapp_booking.py — בחירת תאריך כ-List Picker
def _send_date_list_picker(user_id, service_name, dates, page=0):
    # dates = [{"id": "date_2026-06-08", "title": "ראשון 08/06"}, ...] (≤10)
    body = f"✅ שירות: *{service_name}*\n\n📆 בחרו תאריך מהרשימה:"
    # ensure_list_picker(...) + send_with_template(...) — נפילה לטקסט אם API נכשל
```

ה-callback מ-List Picker חוזר כ-`ListId` (למשל `date_2026-06-08`), וה-handler מפענח:

```python
def _handle_date_input(user_id, text):
    if text.startswith("date_"):                    # List Picker callback
        date_iso = text[len("date_"):]
    elif text.startswith("date_more_"):             # pagination
        return _send_date_list_picker(user_id, ..., page=int(text.split("_")[-1]))
    else:
        date_iso = normalize_date(text)             # שפה טבעית: "מחר", "15/03"
    slots = get_available_slots(date.fromisoformat(date_iso), duration)   # §2
    set_session_data(user_id, "booking_date", date_iso)
    set_state(user_id, STATE_BOOKING_TIME)
    return f"השעות הפנויות ל-{date_iso}:\n" + ", ".join(slots) + "\n\nאיזו שעה?"
```

**נקודות מפתח:**
- **אותם 4 שלבים, UI לפי ערוץ** — הלוגיקה (חישוב slots, ולידציה, יצירה) משותפת; רק ההצגה משתנה. WhatsApp List Picker / Telegram calendar.
- **תמיד fallback לטקסט** — אם Twilio Content API נכשל, המשתמש מקבל רשימה ממוספרת. אסור שכשל UI יחסום הזמנה.
- **WhatsApp state בזיכרון עם timeout** — אין ConversationHandler. dict גלובלי `{user_id: {state, data, ts}}` עם ניקוי אחרי 30 דק'. בפרודקשן multi-instance → Redis.
- **callbacks מקודדים בערך** (`date_<iso>`, `svc_<id>`) — ה-id נושא את המידע, אין צורך ב-state נוסף.

---

## 4. מנוע ההחלטה (`core/booking_decision.py`)

### הדפוס: פונקציה טהורה אחת

כל לוגיקת "לאשר / לדחות / להמתין" יושבת בפונקציה **טהורה** אחת שמקבלת context מלא ומחזירה החלטה. ה-flow רק אוסף את ה-context וקורא לה. זה מאפשר לבדוק את כל מקרי הקצה ביוניט-טסט בלי DB/רשת.

```python
Decision = Literal["confirmed", "pending", "rejected"]

@dataclass
class BookingDecisionInput:
    mode: str                              # "manual" | "auto_with_check" | "auto_always"
    slot_date: date; slot_time: str; duration_minutes: int
    now_il: datetime
    business_hours_status: dict            # מ-get_status_for_date()
    vacation_active: bool
    has_pending_or_confirmed_conflict: bool
    user_has_appointment_same_day: bool
    calendar_connected: bool
    calendar_check_failed: bool
    available_slots: list[str]             # מ-get_available_slots()
```

עץ ההחלטה (`decide_appointment_status`):

| תנאי | החלטה | reason |
|---|---|---|
| השעה עברה | **rejected** | `slot_in_past` |
| > 90 יום קדימה | **rejected** | `slot_too_far_ahead` |
| חופף לתור קיים | **rejected** | `slot_already_taken` |
| `mode=manual` (אחרי בדיקות גלובליות) | **pending** | בעל העסק מאשר ידנית |
| `auto_always` + חופשה | **rejected** | `vacation_active` |
| `auto_always` (אחרת) | **confirmed** | מתעלם מהיומן |
| `auto_with_check` + חופשה / יום סגור / מחוץ לשעות | **rejected** | `vacation`/`closed_*`/`*_hours` |
| `auto_with_check` + יומן מנותק / בדיקה נכשלה | **pending** | אי-ודאות → לא לאשר עיוור |
| `auto_with_check` + slot לא ב-`available_slots` | **rejected** | `calendar_busy` |
| `auto_with_check` + הכל עבר | **confirmed** | `auto_with_check_ok` |

ה-orchestrator אוסף הכל עם graceful fallback:

```python
def gather_and_decide(user_id, slot_date_str, slot_time_str, duration_minutes=None):
    mode = db.get_bot_settings().get("auto_booking_mode", "manual")
    inp = BookingDecisionInput(
        mode=mode, ...,
        business_hours_status=_safe(get_status_for_date, ...),
        vacation_active=_safe(db.get_vacation_mode, ...),
        calendar_connected=is_connected(),
        calendar_check_failed=False,
        available_slots=_safe(get_available_slots, ...) ,   # כשל → calendar_check_failed=True
    )
    return decide_appointment_status(inp)
```

ה-reasons ממופים להודעות עבריות ללקוח:

```python
_REJECTION_MESSAGES = {
    "slot_in_past":       "השעה שבחרתם כבר עברה...",
    "slot_already_taken": "השעה הזו כבר תפוסה...",
    "calendar_busy":      "השעה הזו כבר תפוסה ביומן...",
    "closed_regular":     "התאריך שבחרתם הוא יום סגור...",
    # ... 12 בסך הכל
}
```

**נקודות מפתח:**
- **פונקציה טהורה = testable** — כל ה-context מגיע כפרמטרים. אפס I/O בתוך `decide_*`. בודקים 12 מקרי קצה בלי mock.
- **שלושה modes** — `manual` (תמיד pending, בעל העסק שולט), `auto_with_check` (מאשר רק אם הכל ברור), `auto_always` (מאשר תמיד מלבד חופשה/עבר). זה הכפתור המרכזי של "כמה לסמוך על הבוט".
- **אי-ודאות → pending, לא confirmed** — יומן מנותק / בדיקה נכשלה → לא לאשר עיוור. עדיף שבעל העסק יראה מאשר double-booking.
- **reason קוד → הודעה** — הפרדה בין הלוגיקה (קוד אנגלי) לתצוגה (עברית). קל לתרגם/להחליף.

---

## 5. סכמת DB + מניעת Double-Booking

```sql
CREATE TABLE IF NOT EXISTS appointments (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                    TEXT NOT NULL,
    username                   TEXT DEFAULT '',
    service                    TEXT DEFAULT '',
    preferred_date             TEXT DEFAULT '',      -- YYYY-MM-DD
    preferred_time             TEXT DEFAULT '',      -- HH:MM
    status                     TEXT DEFAULT 'pending'
                                 CHECK(status IN ('pending','confirmed','cancelled','passed')),
    reminder_sent              INTEGER DEFAULT 0,
    second_reminder_sent       INTEGER DEFAULT 0,
    google_event_id            TEXT DEFAULT '',      -- מזהה האירוע ביומן
    channel                    TEXT NOT NULL DEFAULT 'telegram',
    confirmed_duration_minutes INTEGER,              -- נקבע באישור (לסנכרון יומן)
    created_at                 TEXT DEFAULT (datetime('now'))
);
```

**שלוש שכבות הגנה מפני double-booking:**

```python
# שכבה 1 — race check לפני INSERT (ב-flow)
existing = [a for a in db.get_pending_appointments_for_user(user_id)
            if a["preferred_date"] == date and a["preferred_time"] == time]
if existing:
    return "כבר יש לך בקשה לשעה הזו"

# שכבה 2 — UNIQUE index חלקי (DB-level, תופס race conditions)
CREATE UNIQUE INDEX idx_appointments_user_datetime
ON appointments(user_id, preferred_date, preferred_time)
WHERE status IN ('pending', 'confirmed');     -- מאפשר rebooking של slot שבוטל

# שכבה 3 — ניקוי שורות מבוטלות/עברו לפני INSERT (מונע התנגשות עם ה-UNIQUE)
DELETE FROM appointments
WHERE user_id=? AND preferred_date=? AND preferred_time=?
  AND status IN ('cancelled', 'passed');
```

**נקודות מפתח:**
- **UNIQUE חלקי (`WHERE status IN (...)`)** — מאפשר לאותו משתמש לקבוע שוב slot שביטל בעבר, אבל חוסם כפילות של תור פעיל. דפוס SQLite/Postgres קלאסי.
- **שלוש שכבות** — race check (UX מהיר) + UNIQUE (תופס concurrency אמיתי) + cleanup (מונע false-positive על slot שבוטל). השכבה 2 היא הביטחון האמיתי.
- **`confirmed_duration_minutes` נפרד מהשירות** — בעל העסק יכול לקבוע משך מותאם באישור (תור 90 דק' במקום 60). זה מה שנכנס ל-busy ranges וליומן.

---

## 6. אישור, סנכרון יומן, ICS (`appointment_notifications.py`)

```python
def notify_appointment_status(appt: dict, owner_message: str = "") -> bool:
    """נקרא כשבעל העסק משנה סטטוס (pending→confirmed / →cancelled)."""
    status = appt["status"]
    msg = _build_confirmed_message(appt, owner_message) if status == "confirmed" \
          else _build_cancelled_message(appt, owner_message)
    # שליחה לפי ערוץ (Telegram HTML / WhatsApp מומר)
    if appt["channel"] == "whatsapp":
        send_message_by_channel(appt["user_id"], msg, "whatsapp")
    else:
        send_telegram_message(appt["user_id"], msg, parse_mode="HTML")
    # סנכרון יומן Google
    sync_appointment_to_calendar(appt, status)
    if status == "confirmed" and db.get_bot_settings().get("ics_enabled"):
        _send_ics_file(appt)          # קובץ .ics להוספה ליומן הלקוח
```

הסנכרון ליומן (`sync_appointment_to_calendar`):

```python
def sync_appointment_to_calendar(appt, status):
    if not is_connected():
        return                                       # יומן מנותק — דילוג שקט
    if status == "confirmed":
        start_dt = datetime(*map(int, appt["preferred_date"].split("-")),
                            *map(int, appt["preferred_time"].split(":")), tzinfo=IL_TZ)
        end_dt = start_dt + timedelta(minutes=db.resolve_appointment_duration_minutes(appt))
        if appt.get("google_event_id"):
            delete_event(appt["google_event_id"])    # reschedule: מחק ישן
        create_event(appt["id"], appt["service"], appt["username"], start_dt, end_dt)
    elif status == "cancelled":
        if appt.get("google_event_id"):
            delete_event(appt["google_event_id"])
            db.set_appointment_google_event_id(appt["id"], "")
```

יצירת אירוע (`create_event`) — שומר `bookingId=appt_<id>` ב-description כדי לקשר חזרה, ושומר את ה-`event_id` ב-DB. מחיקה מטפלת ב-`410 Gone` כהצלחה (כבר נמחק).

**ICS ל-WhatsApp** — Twilio לא תומכת ב-attachment של `.ics`, אז יוצרים עמוד ציבורי (`/ics/<id>`) עם `Content-Disposition: attachment` ושולחים קישור. (אותו דפוס page-fallback מהמדריך הקודם.)

**נקודות מפתח:**
- **reschedule = delete + create** — אין update; פשוט יותר ועמיד יותר (אם הזמן השתנה, אירוע חדש נקי).
- **`410 Gone` = הצלחה** במחיקה — אם האירוע כבר נמחק ידנית ביומן, זה לא שגיאה.
- **`bookingId` ב-description** — קישור דו-כיווני בין התור ב-DB לאירוע ביומן.
- **סנכרון אחרי השמירה ב-DB** — ה-DB הוא source of truth; היומן הוא שיקוף. אם הסנכרון נכשל, התור עדיין קיים (ו-busy ranges מה-DB מגנים).

---

## 6.5. סביב התור (WhatsApp): התראת בעל העסק, ביטול/שינוי, ובחירת תאריך

שלושה דברים שקורים מסביב לתור עצמו — כולם דרך WhatsApp.

### א. התראת בעל העסק על תור pending (`_notify_owner_booking`)

ברגע שהליד מאשר שעה, בעל העסק מקבל **התראת push בערוץ שלו** (WhatsApp, עם fallback ל-Telegram) — לא רק רשומה בפאנל — עם קישור ישיר לעמוד התורים:

```python
def _notify_owner_booking(user_id, appt_id, service, date_display, preferred_time, auto_confirmed=False):
    display_name = db.get_username_for_user(user_id) or user_id
    phone_display = _format_phone(user_id)
    panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""    # ← קישור לפאנל
    header = (f"✅ תור חדש אושר אוטומטית #{appt_id} (WhatsApp)" if auto_confirmed
              else f"📅 בקשת תור חדשה #{appt_id} (WhatsApp)")             # ← pending vs auto
    notification = (f"{header}\n\nלקוח: {display_name}\nטלפון: {phone_display}\n"
                    f"שירות: {service}\nתאריך: {date_display}\nשעה: {preferred_time}{panel_link}")
    try:
        from messaging.whatsapp_sender import notify_owner_whatsapp
        notify_owner_whatsapp(notification)          # → OWNER_WHATSAPP_NUMBER
    except Exception as e:
        logger.error("Failed to notify owner (WhatsApp): %s", e)
    # fallback: אם מוגדר TELEGRAM_OWNER_CHAT_ID — שולח גם שם
```

ה-header מבחין בין `pending` ("בקשת תור חדשה" — דורש אישור) ל-`auto_confirmed` ("אושר אוטומטית" — ליידוע בלבד). הקישור `{ADMIN_URL}/appointments` פותח את עמוד התורים בפאנל לאישור/דחייה בלחיצה.

**נקודה:** הפאנל הוא ל*פעולה*; ההתראה ב-WhatsApp היא ה-*push* שמושך את בעל העסק לפאנל. שילוב של שניהם = בעל העסק לא מפספס בקשה.

### ב. ביטול ושינוי ע"י הליד — flow מלא ב-WhatsApp

כן — הליד מבטל ומשנה **דרך WhatsApp עצמו**, לא "צרו קשר". ה-NLU (`message_processor`) מזהה כוונה ("אני לא יכול בסוף", "אפשר להזיז?") ומחזיר `action`:

```python
# whatsapp_webhook.py — ביטול
elif result.action == "cancel_appointment":
    pending = db.get_pending_appointments_for_user(from_number)
    if not pending:
        _send_whatsapp_response(from_number, "לא רשום אצלנו תור על שמך... להעביר לבעל העסק?")  # handoff
    elif len(pending) == 1:
        set_state(from_number, STATE_CANCEL_CONFIRM, {"appt_id": appt["id"]})
        _send_cancel_confirmation_buttons(from_number, confirm_text)   # Quick Reply: ✅ כן / ❌ לא
    else:
        set_state(from_number, STATE_CANCEL_SELECT, {"appt_ids": appt_ids})   # כמה תורים → בחירה
```

ובאישור — פעולה אטומית שמשחררת את ה-slot ומודיעה לבעל העסק:

```python
def _handle_cancel_confirmation(from_number, text):
    if normalized in {"cancel_appt_yes", "כן", "1", ...}:
        appt_id = session["data"]["appt_id"]
        cancelled = db.cancel_appointment_and_sync(appt_id, from_number)   # DB→cancelled + מחיקת אירוע יומן
        _notify_owner_cancellation(from_number, appt_id, ...)              # מתריע לבעל העסק
        return "התור שלך בוטל בהצלחה ✅..."
```

ה-**reschedule** הוא flow ארבע-שלבי משל עצמו (כמו booking), עם states נפרדים:

```python
# conversation_state.py
STATE_RESCHEDULE_SELECT, STATE_RESCHEDULE_DATE, STATE_RESCHEDULE_TIME, STATE_RESCHEDULE_CONFIRM
# whatsapp_webhook.py: _handle_reschedule_step() מנתב select→date→time→confirm,
# ובסיום _notify_owner_reschedule() מתריע לבעל העסק על השינוי.
```

**נקודות מפתח:**
- **NLU → action → flow** — הליד לא צריך פקודה. שפה טבעית ("לבטל") היא ה-entry point. ה-intent מנתב ל-`cancel_appointment` / `reschedule_appointment`.
- **`cancel_appointment_and_sync` אטומי** — סטטוס → `cancelled` + מחיקת אירוע יומן + שחרור ה-slot (busy ranges בודק רק `status IN (pending,confirmed)`, אז ביטול מפנה מיד).
- **התראת owner על *כל* שינוי** — חדש / ביטול / reschedule — שלושתם מודיעים לבעל העסק. הוא תמיד מסונכרן.
- **handoff כ-fallback** — אין תור על שם הליד? מציע להעביר לבעל העסק. לא מבוי סתום.

### ג. הצגת בחירת התאריך ב-WhatsApp (Date List Picker)

התאריכים הפנויים מוצגים כ-**List Picker אינטראקטיבי** (לא טקסט) — הליד בוחר מרשימה:

```python
def _send_date_list_picker(user_id, service_name, service_duration, page=0):
    dates, has_more = _get_available_dates(service_duration, page)   # רק תאריכים פנויים
    items = []
    for d in dates:
        day_name = _HEBREW_DAYS[d.weekday()]                        # "ראשון", "שני"...
        items.append({"title": f"יום {day_name} {d.strftime('%d/%m')}",
                      "id": f"date_{d.isoformat()}"})               # ← ה-id נושא את התאריך
    if has_more:                                                    # pagination (≤10 פריטים)
        items = items[:9]                                           # פינוי מקום לכפתור
        items.append({"title": "▶ עוד ימים...", "id": f"date_more_{page+1}"})
    content_sid = ensure_list_picker(friendly_name="booking_dates",
        body=f"✅ שירות: *{service_name}*\n\n📆 בחרו תאריך מהרשימה:",
        button_text="בחרו תאריך", items=items)
    send_with_template(user_id, content_sid)
```

חישוב התאריכים הפנויים סורק עד 60 יום קדימה ומסנן לפי זמינות (יומן + שעות פעילות), עם cache לכל חודש:

```python
def _get_available_dates(service_duration, page=0):
    available = []
    for offset in range(60):
        d = today + timedelta(days=offset)
        avail = get_month_availability(d.year, d.month, service_duration, ...)   # cached per-month
        if avail.get(d.day, {}).get("available"):
            available.append(d)
    start = page * 10
    return available[start:start + 10], len(available) > start + 10   # 10/עמוד (מגבלת List Picker)
```

ה-callback חוזר כ-`ListId` (למשל `date_2026-06-08` או `date_more_1`) וה-handler מפענח (ראה §3).

**נקודות מפתח:**
- **List Picker ≤ 10** (מגבלת WhatsApp) → pagination, כשהכפתור "▶ עוד ימים..." הוא עצמו פריט ברשימה (`date_more_<page>`).
- **רק תאריכים פנויים מוצגים** — הסינון קורה מראש (`_get_available_dates`). הליד לא רואה ימים סגורים/מלאים בכלל. UX נקי.
- **ה-id נושא את הערך** (`date_<iso>`) — אין צורך ב-state נוסף; ה-callback מכיל הכל.
- **תמיד fallback לטקסט** — אם Content API נכשל, `_date_prompt` יורד לבקשת תאריך חופשי ("מחר / 15/03").

---

## 7. תזכורות + Scheduler (polling)

### הדפוס: polling idempotent, לא scheduling מדויק

במקום לתזמן job לכל תור (שביר — restart מאבד jobs), רצים **כל 30 דק'** ושולחים מה שצריך, עם דגלי DB שמונעים כפילות.

```python
# bot/telegram_bot.py — job queue (APScheduler דרך python-telegram-bot)
application.job_queue.run_repeating(_appointment_reminders_job, interval=1800, first=120,
                                    name="appointment_reminders")     # תזכורת 1 (24h)
application.job_queue.run_repeating(_second_reminders_job, interval=1800, first=180,
                                    name="second_reminders")          # תזכורת 2 (X שעות)
```

```python
def send_appointment_reminders() -> dict:
    settings = db.get_bot_settings()
    if not settings.get("reminder_enabled", 1):
        return {"sent": 0, "skipped": "disabled"}
    # שולחים רק אחרי שעת התזכורת היומית (ברירת מחדל 10:00)
    reminder_h, reminder_m = map(int, settings.get("reminder_time", "10:00").split(":"))
    now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    if (now_il.hour, now_il.minute) < (reminder_h, reminder_m):
        return {"sent": 0, "skipped": "too_early"}
    tomorrow = (now_il.date() + timedelta(days=1)).isoformat()
    for appt in db.get_appointments_for_reminder(tomorrow):    # confirmed + reminder_sent=0
        send_message_by_channel(appt["user_id"], _build_reminder_message(appt), appt["channel"])
        db.mark_reminder_sent(appt["id"])                       # ← דגל idempotency
```

תזכורת שנייה (X שעות לפני) משתמשת בחלון של 30 דק' שתואם ל-polling, עם טיפול ב-חציית חצות:

```python
def send_second_reminders() -> dict:
    hours = float(db.get_bot_settings().get("second_reminder_hours", 2.0))
    window_start = now_il + timedelta(hours=hours)
    window_end = window_start + timedelta(minutes=30)            # = interval ה-polling
    if window_start.date() != window_end.date():                # חוצה חצות → שני טווחים
        ranges = [(start_date, start_time, "24:00"), (end_date, "00:00", end_time)]
    # ... שליחה + mark_second_reminder_sent
```

**נקודות מפתח:**
- **polling + דגל DB = idempotent ועמיד ל-restart** — אין state בזיכרון; אם השרת קם מחדש, ה-polling הבא ממשיך. דגל `reminder_sent` מונע כפילות.
- **חלון = interval ה-polling** (30 דק') — התזכורת השנייה מחפשת תורים בחלון [now+X, now+X+30] כך שכל תור נתפס בדיוק פעם אחת.
- **בדיקת `reminder_time` בכל ריצה** — ה-job רץ כל 30 דק', אבל שולח רק אחרי השעה שנקבעה. הדגל מבטיח פעם-ביום.
- **חציית חצות** — תזכורת שעתיים לפני תור ב-01:00 צריכה לרוץ ב-23:00 של היום הקודם. פיצול לשני טווחי תאריך.
- **כל timezone ב-`Asia/Jerusalem`** — קריטי. כל ההשוואות ב-IL time.

---

## 8. Business Hours (`business_hours.py`)

### הדפוס: רזולוציה לפי עדיפות

```python
def get_status_for_date(target_date) -> dict:
    # סדר עדיפות (גבוה→נמוך):
    # 1. special_days table   — בעל העסק הגדיר ידנית (חופשה/שעות מיוחדות)
    # 2. לוח חגים ישראלי       — holidays_lib.Israel(), אלא אם המשתמש הסיר ידנית
    # 3. ערב חג                — אם מחר חג והיום פתוח → דגל (שעות מקוצרות)
    # 4. שעות שבועיות רגילות   — get_business_hours_for_day (0=ראשון...6=שבת)
    return {"is_open": ..., "open_time": ..., "close_time": ..., "source": ..., "day_name": ...}
```

טבלת תרחישים (כלל ברזל בריפו — לכתוב לפני קוד תלוי-זמן):

| תרחיש | טיפול |
|---|---|
| יום סגור רגיל | `is_open=False`, source=`regular` |
| חג ישראלי | אוטומטי מ-`holidays` lib, אלא אם הוסר ידנית |
| ערב חג | דגל `erev_chag` — לא חוסם, מתריע על שעות מקוצרות |
| משמרת לילה (22:00–02:00) | `is_overnight = close <= open`; בדיקת `current >= open` בלבד |
| חופשה (vacation mode) | דורס הכל — מונע מה-LLM להציע תורים |
| יום מיוחד עם שעות מותאמות | מ-`special_days`, עדיפות עליונה |

**נקודות מפתח:**
- **רזולוציה לפי עדיפות** — special_days דורס חגים דורס רגיל. שכבה אחת מנצחת, לא מיזוג.
- **לוח חגים אוטומטי** (`holidays` lib) עם override ידני — הבוט יודע על פסח בלי הגדרה, אבל בעל העסק יכול לפתוח בכל זאת.
- **ספירת ימים ישראלית** (0=ראשון) ≠ Python (0=שני) — פונקציית המרה. מלכודת קלאסית.
- **טבלת תרחישים לפני קוד** — מעבר יום, ערב חג, משמרת לילה, גבול שנה. כלל מה-CLAUDE.md.

---

## 9. מה לא לחלץ / להחליף בפרויקט החדש

| רכיב | למה / מה להחליף |
|---|---|
| `ConversationHandler` של Telegram | אם הפרויקט WhatsApp-only — קח את ה-state machine של `whatsapp_booking.py` בלבד |
| לוח שנה inline (Telegram) | לא רלוונטי ל-WhatsApp — List Picker מספיק |
| ספציפיקציות עסק יופי (שירותים, מחירים) | seed data משלך |
| חגים ישראליים | אם שוק אחר — להחליף `holidays.Israel` במדינה שלך, או להסיר |
| `BUSINESS_NAME` וכו' | config משלך |

**מה כן ליבה (להעתיק כמעט as-is):** `get_available_slots` (§2), `decide_appointment_status` (§4), מניעת double-booking (§5), polling reminders (§7).

---

## 10. טבלת קבצים + env vars

| נושא | קובץ | פונקציות מפתח |
|---|---|---|
| OAuth + tokens + refresh | `google_calendar.py` | `get_authorization_url`, `exchange_code_for_credentials`, `_get_credentials`, `is_connected` |
| FreeBusy + slots | `google_calendar.py` | `get_busy_slots`, `get_available_slots` |
| Event create/delete/sync | `google_calendar.py` | `create_event`, `delete_event`, `sync_appointment_to_calendar` |
| מנוע החלטה | `core/booking_decision.py` | `decide_appointment_status`, `gather_and_decide` |
| Booking flow (Telegram) | `bot/handlers.py` | `booking_start/service/date/time/confirm` |
| Booking flow (WhatsApp) | `messaging/whatsapp_booking.py` | `start_booking`, `handle_booking_step`, `_send_date_list_picker`, `_notify_owner_booking` |
| ביטול/שינוי (WhatsApp) | `messaging/whatsapp_webhook.py` | `_handle_cancel_confirmation`, `_handle_reschedule_step`, `_notify_owner_cancellation/reschedule` |
| State (WhatsApp) | `messaging/conversation_state.py` | `set_state`, `get_session_data`, timeout |
| לוח שנה (Telegram) | `bot/calendar_keyboard.py` | `build_calendar_keyboard` |
| שעות פעילות | `business_hours.py` | `get_status_for_date`, `is_currently_open` |
| התראות + תזכורות | `appointment_notifications.py` | `notify_appointment_status`, `send_appointment_reminders`, `send_second_reminders` |
| Scheduler | `bot/telegram_bot.py` | `run_repeating` (job queue) |
| DB — appointments | `database.py` | `create_appointment`, `get_appointments_busy_ranges`, `update_appointment_status` |
| DB — credentials | `database.py` | `save/get/update_google_calendar_credentials`, `set/clear_auth_invalid` |
| הצפנה | `utils/crypto.py` | `encrypt_field`, `decrypt_field` |

```bash
# Google Calendar OAuth
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxx
GOOGLE_REDIRECT_URI=https://your-domain/google/callback
SECRETS_ENCRYPTION_KEY=xxx     # Fernet — הצפנת refresh_token (חובה!)

# הגדרות (ב-bot_settings DB, לא env):
#   auto_booking_mode          = manual | auto_with_check | auto_always
#   auto_booking_max_days_ahead = 90
#   default_appointment_duration_minutes = 60
#   reminder_enabled=1, reminder_time="10:00"
#   second_reminder_enabled=0, second_reminder_hours=2.0
```

---

## סיכום — סדר היישום המומלץ

1. **Google Calendar OAuth + אחסון מוצפן** (§1) — תשתית. בלי זה אין free/busy.
2. **`get_available_slots`** (§2) — הליבה. החיתוך של 3 המקורות. השקע כאן הכי הרבה.
3. **DB appointments + double-booking** (§5) — לפני ה-flow, כי ה-flow כותב לכאן.
4. **מנוע ההחלטה** (§4) — פונקציה טהורה, קל לבדוק, מנותק מהערוץ.
5. **Booking flow** (§3) — חוט אותם יחד. WhatsApp List Picker מהמדריך הקודם.
6. **אישור + סנכרון יומן** (§6).
7. **תזכורות** (§7) — אחרון. polling פשוט.
