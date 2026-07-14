# מדריך חילוץ — תשתית הודעות לבוט WhatsApp / מכירות

מסמך זה מחלץ מהריפו `ai-business-bot` את **שכבת ההודעות** (channel layer): templates, אימות webhooks, adapter pattern, שליחה יוצאת עם retry, טיפול באורך, ו-formatting — כך שתוכל להעתיק אותם לפרויקט אחר (בוט WhatsApp מכירות 24/7).

הוצאו במכוון: הצינור (`process_incoming_message`), intent detection, rate-limiting, decorators, ו-conversation memory — ראה סעיף "מה לא לחלץ".

> **שני דפוסי שליחה בריפו** — שווה להכיר את שניהם:
> - **Twilio** (WhatsApp): שליחה דרך Twilio Content API. הדרך היחידה שעבדה טוב לעסקים קטנים בארץ. **כל מנגנון ה-templates בנוי סביב Twilio.**
> - **Graph API ישיר** (Messenger/Instagram DM): POST ל-`/me/messages` עם page token. **זה הדפוס הקרוב יותר ל-Meta Cloud API** שאתה מתכנן.
>
> המדריך מצטט את שניהם; בחר את המתאים לערוץ שלך. ה-*patterns* זהים — רק קריאת ה-API משתנה.

---

## 0. סקירה — מפת השכבה

```
        הודעה נכנסת (webhook POST)
                  │
                  ▼  ① אימות חתימה (verify-before-process)  ← סעיף 2
       ┌──────────────────────┐
       │  Channel Adapter      │  ② נרמול ל-(user_id, text, channel)  ← סעיף 3
       └──────────┬───────────┘
                  ▼
          process_incoming_message()   ← הליבה (לא במדריך הזה)
                  │
                  ▼  result.text (HTML מה-LLM)
       ┌──────────────────────┐
       │  שער שליחה יחיד       │  ③ format לפי ערוץ            ← סעיף 6
       │  (_send_*_response)   │  ④ בדיקת אורך → עמוד HTML?     ← סעיף 5
       └──────────┬───────────┘  ⑤ retry + error classification ← סעיף 4
                  ▼
        Provider API (Twilio / Graph)

  ─── מסלול proactive (לידים שלא ענו) ───
       Template (pre-approved) → render {{vars}} → broadcast send  ← סעיף 1
```

**עיקרון-על:** כל יציאה לערוץ עוברת דרך **שער שליחה יחיד**. אסור לקרוא ל-API של הספק ישירות מ-handlers שונים — אחרת בדיקות האורך וה-formatting נעקפות. זה הלקח המרכזי מהריפו.

| רכיב | קובץ מקור | ספק |
|---|---|---|
| Templates lifecycle | `messaging/whatsapp_templates_*.py`, `template_renderer.py` | Twilio |
| Webhook signature | `messaging/whatsapp_webhook.py`, `messaging/meta_webhook.py` | Twilio + Meta |
| Adapter pattern | `messaging/base.py`, `*_adapter.py` | כולם |
| Outbound + retry | `messaging/whatsapp_sender.py`, `broadcast_sender.py`, `meta_sender.py` | Twilio + Graph |
| Length + page fallback | `messaging/whatsapp_webhook.py`, `meta_webhook.py` | כולם |
| Formatting | `messaging/formatter.py` | כולם |

---

## 1. WhatsApp Templates ⭐ (קריטי — החיסכון הכי גדול)

### הדפוס

Meta דורשת template **pre-approved** לכל הודעה proactive (יזומה) — כלומר כל פנייה ללקוח **מחוץ לחלון 24 השעות** מאז ההודעה האחרונה שלו. follow-up לליד שלא ענה = הודעה יזומה = **חייב template מאושר**. זה לא אופציונלי; Meta חוסמת אחרת.

ה-lifecycle של template:

```
   create (admin)          submit              poll (sync)         send
  ┌──────────┐  HX-SID   ┌──────────┐ pending ┌──────────┐ approved ┌──────────┐
  │ Twilio   │ ───────►  │ Meta      │ ──────► │ status   │ ───────► │ broadcast │
  │ Content  │           │ Approval  │         │ sync→DB  │          │ + render  │
  └──────────┘           └──────────┘         └──────────┘          └──────────┘
   unsubmitted            pending             approved/rejected      {{1}}→value
```

**ארבעה שלבים, כל אחד endpoint נפרד ב-Twilio Content API:**
1. **Create** — `POST /v1/Content` → מקבל `content_sid` (HXxxx). מצב `unsubmitted`.
2. **Submit** — `POST /v1/Content/{sid}/ApprovalRequests/whatsapp` → מצב `pending`.
3. **Sync** — `GET /v1/Content/{sid}/ApprovalRequests` → קורא `approved`/`rejected`/`paused` חזרה ל-DB.
4. **Send** — `messages.create(content_sid=..., content_variables=...)` — Twilio מבצעת את החלפת ה-`{{N}}` בצד שלה.

### סכמת DB

```sql
CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    content_sid      TEXT NOT NULL UNIQUE,          -- Twilio SID (HXxxx) = ה-natural key
    friendly_name    TEXT NOT NULL,
    language         TEXT NOT NULL DEFAULT 'he',
    category         TEXT DEFAULT 'UTILITY'
                       CHECK(category IN ('UTILITY','MARKETING','AUTHENTICATION','UNKNOWN')),
    approval_status  TEXT NOT NULL DEFAULT 'unsubmitted'
                       CHECK(approval_status IN ('approved','pending','rejected','paused','unsubmitted')),
    rejection_reason TEXT,                          -- למה Meta דחתה
    header_type      TEXT DEFAULT 'none'
                       CHECK(header_type IN ('none','text','image','video','document','location')),
    header_text      TEXT DEFAULT '',
    header_media_url TEXT DEFAULT '',
    body_text        TEXT NOT NULL DEFAULT '',      -- "הזמנה {{1}} מוכנה ב-{{2}}"
    footer_text      TEXT DEFAULT '',
    buttons_json     TEXT NOT NULL DEFAULT '[]',    -- [{type,title,id,url/phone}]
    variables_json   TEXT NOT NULL DEFAULT '[]',    -- [{index,name,example}]
    content_type     TEXT DEFAULT '',               -- twilio/quick-reply, twilio/text...
    raw_json         TEXT DEFAULT '',               -- payload גולמי (debug)
    last_synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
    created_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_wa_tpl_status ON whatsapp_templates(approval_status, language);
```

`broadcast_deliveries` — שורת מעקב לכל נמען, עם `UNIQUE(campaign_id, user_id)` (מונע שליחה כפולה) ו-`twilio_message_sid` (לקישור עם status callbacks):

```sql
CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id             INTEGER NOT NULL,
    user_id                 TEXT NOT NULL,
    rendered_variables_json TEXT DEFAULT '{}',      -- {"1":"ערך מרונדר"} לכל נמען
    twilio_message_sid      TEXT,
    status                  TEXT NOT NULL DEFAULT 'queued'
                              CHECK(status IN ('queued','sent','delivered','read','failed','undelivered')),
    error_code              TEXT,                   -- קוד Twilio (21211...)
    error_message           TEXT,
    sent_at TEXT, delivered_at TEXT, read_at TEXT, failed_at TEXT,
    UNIQUE(campaign_id, user_id)
);
```

### Submission ל-Meta (`whatsapp_templates_submit.py`)

```python
_VALID_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
# Meta: name = lowercase, ספרות, קו תחתון בלבד. מקס ~512 תווים.
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def sanitize_template_name(name: str) -> str:
    """'Order Update v2' → 'order_update_v2'. Meta דוחה כל פורמט אחר."""
    cleaned = _NAME_SANITIZE_RE.sub("_", (name or "").lower().strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")     # קיבוץ + חיתוך שוליים
    if not cleaned:
        cleaned = f"template_{int(time.time())}"
    return cleaned[:512]


def submit_template_for_approval(content_sid, category, name, timeout=15) -> dict:
    if not content_sid:
        raise ValueError("content_sid חובה")
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"category חייבת להיות אחת מ-{sorted(_VALID_CATEGORIES)}")
    sanitized_name = sanitize_template_name(name)

    try:
        # ⚠️ Twilio Approval endpoint דורש JSON (לא form-encoded → HTTP 415).
        # השדות lowercase (name/category), לא PascalCase.
        resp = requests.post(
            content_api_url(f"{content_sid}/ApprovalRequests/whatsapp"),
            json={"name": sanitized_name, "category": category},
            auth=get_auth(),          # (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"success": False, "approval_status": "unsubmitted", "error": f"שגיאת רשת: {exc}"}

    if resp.status_code not in (200, 201, 202):
        return {"success": False, "approval_status": "unsubmitted",
                "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}

    # עדכון אופטימי ל-DB: pending מיד (הסטטוס הסופי יגיע בסנכרון הבא)
    db.upsert_whatsapp_template({**tpl, "approval_status": "pending", "category": category})
    return {"success": True, "approval_status": "pending", "category": category,
            "name": sanitized_name, "error": None}
```

### Parameter Substitution — שתי שכבות

זה החלק המתוחכם. יש **שתי** רמות של placeholders:

1. **`{{1}}`, `{{2}}`** — משתני ה-template הסטנדרטיים של Twilio/Meta. ב-broadcast, **Twilio** מבצעת את ההחלפה הסופית (אתה שולח `content_variables='{"1":"ערך"}'`).
2. **`{{user:field}}`** — שכבה משלנו **מעל** השכבה הראשונה, ל-personalization per-recipient. נפתרת **אצלנו** לפני השליחה.

```python
# template_renderer.py
_VAR_RE = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")          # {{1}}, {{name}}
_USER_FIELD_RE = re.compile(r"\{\{\s*user:(\w+)\s*\}\}")    # {{user:username}}
ALLOWED_USER_FIELDS = frozenset({"username", "user_id", "phone"})   # allow-list!


def substitute_variables(text: str, values: dict) -> str:
    """{{N}} → ערך. משתנה חסר נשאר {{N}} (ה-UI מסמן 'חסר')."""
    if not text:
        return ""
    normalized = {str(k): v for k, v in (values or {}).items()}   # תומך {1:..} ו-{"1":..}

    def _replace(match):
        key = match.group(1)
        if key in normalized:
            val = normalized[key]
            if val is None or str(val).strip() == "":
                return match.group(0)        # ריק → השאר placeholder
            return str(val)
        return match.group(0)

    return _VAR_RE.sub(_replace, text)


def substitute_user_fields(text: str, user_row: dict) -> str:
    """{{user:username}} → ערך אמיתי. שדה לא ב-allow-list → נשאר literal."""
    if not text or "{{" not in text or "user:" not in text:
        return text or ""                    # fast path: אין מה להחליף

    def _replace(match):
        field = match.group(1)
        if field not in ALLOWED_USER_FIELDS:  # ← הגנה: רק שדות מותרים
            return match.group(0)
        if field == "phone":
            from utils.phone import format_phone   # +972XX → 0XX
            return str(format_phone(user_row.get("user_id", "") or ""))
        return str(user_row.get(field, "") or "")

    return _USER_FIELD_RE.sub(_replace, text)
```

ובזמן ה-broadcast, מרכיבים את ה-`content_variables` לכל נמען:

```python
# broadcast_sender.py
def render_variables_for_user(template_variables, static_mapping, user_id, user_row=None) -> dict:
    """{"1": "היי {{user:username}}"} + user_row → {"1": "היי דני"}.
    ה-user_row מגיע מ-batch-fetch לפני הלולאה — לא N queries."""
    if user_row is None:
        user_row = {"user_id": user_id, "username": ""}
    result = {}
    for var in template_variables or []:
        idx = str(var.get("index", "") or "")
        if not idx:
            continue
        value = static_mapping.get(idx, "") or ""
        result[idx] = substitute_user_fields(str(value), user_row)   # pass-through אם אין {{user:}}
    return result
```

**נקודות מפתח:**
- **`content_sid` הוא ה-natural key** (`UNIQUE`). העדכון ל-DB הוא upsert לפי SID.
- **עדכון אופטימי ל-`pending`** מיד אחרי submission מוצלח — ה-UI מראה תוצאה מיידית, והסטטוס הסופי מגיע ב-sync. אם עדכון ה-DB נכשל, הסנכרון הבא יתקן.
- **`{{user:field}}` עם allow-list** — לעולם לא לאפשר שדה שרירותי (דליפת PII / injection). שדה לא מוכר נשאר literal וה-UI מסמן.
- **`category=UNKNOWN`** ב-enum — fallback ל-קטגוריות חדשות של Meta כדי שה-sync לא יקרוס.
- **pagination robustness ב-sync**: אם עמוד נכשל באמצע — לבטל את ה-prune (מחיקת templates שלא נמצאו), אחרת תמחק templates שפשוט לא נקראו.
- **לא ניתן לערוך template אחרי submission** — נדחה? שולחים גרסה חדשה (content_sid חדש). הריפו עושה versioning לפי hash של הגוף.

> **התאמה ל-Meta Cloud API ישיר** (בלי Twilio): ה-lifecycle זהה, אבל ה-endpoints הם של Meta: `POST /{waba-id}/message_templates` ליצירה+submission (שלב אחד), ו-`GET /{waba-id}/message_templates` לסטטוס. השליחה: `POST /{phone-id}/messages` עם `type:"template"` ו-`components[].parameters`. שכבת ה-`{{user:field}}` שלך נשארת זהה — אתה פותר אותה לפני שאתה בונה את ה-`parameters`.

---

## 2. Webhook Signature Verification

### הדפוס: verify-before-process

**שורה ראשונה בכל handler של POST נכנס** — לפני פענוח, לפני DB, לפני הכל. כל אחד יכול לזייף POST ל-webhook ציבורי; החתימה מוכיחה שזה הספק. כשל → `abort(403)` מיד.

### Meta — HMAC SHA-256 ידני (`meta_webhook.py`)

זה הדפוס הקרוב ל-Meta Cloud (Cloud API חותם זהה ב-`X-Hub-Signature-256`):

```python
def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """מטא חותמת את ה-body ב-HMAC-SHA256 עם META_APP_SECRET, פורמט sha256=<hex>."""
    if not META_APP_SECRET:
        logger.error("META_APP_SECRET לא מוגדר — לא ניתן לאמת חתימה")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header.split("=", 1)[1]
    computed = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body,                       # ← raw bytes, לא decoded! קריטי.
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, computed)   # ← constant-time (anti timing-attack)
```

קריאה ב-handler:

```python
@meta_bp.route("/webhooks/meta", methods=["POST"])
def meta_inbound():
    raw_body = request.get_data(cache=True)             # ← cache=True: גם הקוד הבא קורא את ה-body
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(raw_body, signature):
        logger.warning("Meta webhook: חתימה לא תקפה — דוחים")
        abort(403)
    # ... רק עכשיו מפענחים JSON ומעבדים
```

ול-handshake הראשוני (GET) של Meta — השוואת verify token:

```python
@meta_bp.route("/webhooks/meta", methods=["GET"])
def meta_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return Response(challenge, status=200, mimetype="text/plain")
    abort(403)
```

### Twilio — RequestValidator (`whatsapp_webhook.py`)

ל-WhatsApp דרך Twilio, ה-SDK עושה את ה-HMAC (SHA-1 על ה-URL+params). הנקודה החשובה: **טיפול ב-proxy**:

```python
def _validate_twilio_signature() -> bool:
    if not TWILIO_AUTH_TOKEN:
        return False
    from twilio.request_validator import RequestValidator
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    # ⚠️ מאחורי proxy עם HTTPS — Twilio חתמה על https:// אבל Flask רואה http://
    if request.headers.get("X-Forwarded-Proto") == "https":
        url = url.replace("http://", "https://", 1)     # replacement יחיד
    return validator.validate(url, request.form.to_dict(), signature)
```

**נקודות מפתח:**
- **`raw_body` ולא `request.json`** — ה-HMAC חייב לרוץ על ה-bytes **המקוריים** בדיוק כפי שהגיעו. אם תפענח ל-JSON ותסריאלז מחדש, החתימה לא תתאים (סדר מפתחות, whitespace).
- **`hmac.compare_digest`** ולא `==` — השוואה בזמן קבוע מונעת timing attack שמדליף את החתימה בייט-בייט.
- **`X-Forwarded-Proto`** — מאחורי reverse proxy (Render/nginx), ה-URL שה-framework רואה הוא `http://` אבל הספק חתם על `https://`. בלי התיקון, **כל** החתימות נכשלות בפרודקשן. (זה גם הקשר ל-XFF spoofing: קרא את ה-header רק כשאתה מאחורי proxy מהימן.)
- **fail closed** — חסר secret? → `return False` (דוחה), לא `True`.

---

## 3. Channel Adapter Pattern

### הדפוס: adapters דקים מעל ליבה משותפת

הליבה (`process_incoming_message`) **לא יודעת** דרך איזה ערוץ ההודעה הגיעה. כל ערוץ הוא adapter דק שעושה שני דברים: (א) מנרמל webhook נכנס ל-`(user_id, text, channel)`, (ב) שולח את ה-response חזרה. אפס שכפול לוגיקה בין ערוצים.

### הממשק (`base.py`)

```python
class MessageAdapter(ABC):
    """ממשק אחיד לשליחת הודעות — כל ערוץ ממש את המתודות."""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str,
                        buttons: Optional[list[str]] = None) -> None: ...

    @abstractmethod
    async def send_contact(self, chat_id: str, name: str, phone: str) -> None: ...

    @abstractmethod
    async def send_location(self, chat_id: str, lat: float, lon: float) -> None: ...

    @abstractmethod
    async def send_file(self, chat_id: str, file_data: bytes, filename: str) -> None: ...
```

### מימוש WhatsApp (`whatsapp_adapter.py`)

```python
class WhatsAppAdapter(MessageAdapter):
    def __init__(self, account_sid, auth_token, whatsapp_number):
        from twilio.rest import Client
        self.client = Client(account_sid, auth_token)
        self.from_number = f"whatsapp:{whatsapp_number}"

    async def send_text(self, chat_id, text, buttons=None):
        formatted = format_message(text, "whatsapp")    # ← HTML→WhatsApp (סעיף 6)
        if buttons:
            # Twilio ב-API הרגיל לא תומך בכפתורים — fallback לטקסט ממוספר
            lines = [formatted, ""]
            for i, label in enumerate(buttons, 1):
                lines.append(f"{i}. {label}")
            lines += ["", "(שלחו את המספר)"]
            formatted = "\n".join(lines)
        send_to = self._resolve_send_to(chat_id)
        # ⚠️ Twilio SDK סינכרוני — עוטפים ב-to_thread כדי לא לחסום את ה-event loop
        await asyncio.to_thread(
            self.client.messages.create,
            body=formatted, from_=self.from_number, to=send_to,
        )

    async def send_location(self, chat_id, lat, lon):
        # WhatsApp לא תומך ב-location מובנה דרך ה-API — קישור Google Maps
        url = f"https://maps.google.com/maps?q={lat},{lon}"
        await asyncio.to_thread(self.client.messages.create,
            body=f"📍 מיקום: {url}", from_=self.from_number,
            to=self._resolve_send_to(chat_id))
```

### נרמול מזהים (`meta_adapter.py`) — לערוצים עם prefix

כשיש כמה תת-ערוצים (Messenger + Instagram) שחולקים תשתית, מנרמלים את ה-id ל-`channel:external_id`. כל הקוד הפנימי משתמש בפורמט הזה; רק קריאת ה-API מפשיטה אותו:

```python
def to_internal_user_id(channel: str, external_id: str) -> str:
    """('meta_ig', '178401') → 'meta_ig:178401'."""
    return f"{channel}:{external_id}"

def to_provider_recipient(internal_user_id: str) -> str:
    """'meta_ig:178401' → '178401' (מה ש-Graph API מצפה לו)."""
    # ... split on ':' עם ולידציה

def parse_channel(internal_user_id: str) -> str:
    """'meta_ig:178401' → 'meta_ig'."""
```

**נקודות מפתח:**
- **`asyncio.to_thread`** — ה-SDK של Twilio סינכרוני. אם תקרא לו ישירות מ-`async def`, תחסום את ה-event loop וכל הבוט ייתקע. תמיד `to_thread`.
- **כפתורים = fallback לטקסט** ב-WhatsApp דרך Twilio הרגיל (כפתורים אמיתיים דורשים templates עם Quick Reply). location → קישור Maps. contact → טקסט. **כל ערוץ מתרגם לעשיר-ביותר שהוא תומך בו.**
- **נרמול id עם prefix** — אם הפרויקט שלך מטפל בכמה sub-channels, ה-`channel:id` חוסך שדה channel נפרד בכל מקום ומונע התנגשויות id בין ערוצים.
- **ה-adapter לא מכיל לוגיקה עסקית** — רק תרגום פורמט ושליחה. כל "מה לענות" קורה בליבה.

---

## 4. Outbound Sending + Retry + Error Classification

### הדפוס

שליחה יוצאת היא נקודת הכשל הכי שכיחה (נמען חסם, מספר לא תקין, rate limit, שרת נפל). שני עקרונות:
1. **שער יחיד** שמרכז את כל השליחות (format + diagnostics במקום אחד).
2. **סיווג שגיאות** — לא כל שגיאה שווה. חסימה → להסיר מנוי. rate limit → לחכות ולנסות שוב. מספר לא תקין → לדלג לפני שליחה.

### השער ל-WhatsApp (`whatsapp_sender.py`)

```python
_twilio_client = None   # singleton — נוצר פעם אחת

def _get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def send_whatsapp(to_number: str, text: str, media_url: str | None = None) -> None:
    """שער השליחה היחיד ל-WhatsApp. webhook + broadcast + live_chat — כולם דרכו."""
    if DEMO_MODE:                                       # מצב דמו — לא שולחים בפועל
        logger.info("DEMO_MODE: skipping WhatsApp send"); return

    send_to = to_number                                # reverse lookup ל-BSUID אם צריך
    if not _is_phone_number(to_number):
        from utils.user_identity import get_whatsapp_send_address
        send_to = get_whatsapp_send_address(to_number) or to_number

    formatted = format_message(text, "whatsapp")
    kwargs = {"body": formatted, "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
              "to": f"whatsapp:{send_to}"}
    if media_url:
        kwargs["media_url"] = [media_url]

    # ⚠️ לוג אבחון bytes — עברית UTF-8 = 2 בייט/תו. Twilio קוצץ לפי bytes,
    # אז chars<1600 לא מבטיח שלא ייחתך. (ראה סעיף 5.)
    logger.info("WA send diag: chars=%d utf8_bytes=%d",
                len(formatted), len(formatted.encode("utf-8")))
    message = _get_twilio_client().messages.create(**kwargs)
    logger.info("WA send response: sid=%s status=%s num_segments=%s",
                message.sid, message.status, message.num_segments)
```

### השער ל-Graph API ישיר (`meta_sender.py`) — קרוב ל-Meta Cloud

```python
def send_meta_message(recipient_external_id: str, text: str, page_token: str) -> str:
    """POST /me/messages. נקודת קצה זהה ל-Messenger ול-IG; ההבדל הוא הטוקן."""
    payload = {
        "recipient": {"id": recipient_external_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",          # תשובה להודעת לקוח (מותר בלי תיוג)
    }
    resp = requests.post(_graph_url("me/messages"),
                         params={"access_token": page_token},
                         json=payload, timeout=15)
    _raise_for_graph_error(resp, "send_meta_message")
    message_id = resp.json().get("message_id", "")
    if not message_id:                          # 200 בלי message_id = כשל לוגי
        raise MetaGraphError(f"send_meta_message: לא חזר message_id")
    return message_id
```

### סיווג שגיאות בשליחה לנמען בודד (`broadcast_sender.py`)

```python
def _send_to_one(content_sid, to_user_id, variables, status_callback_url
                ) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """מחזיר (success, twilio_sid, error_code, error_message)."""
    try:
        send_to = to_user_id
        if not _is_phone_number(to_user_id):
            send_to = get_whatsapp_send_address(to_user_id) or to_user_id

        # ① ולידציה לפני שליחה — חוסך error codes מבלבלים על מספרים שגויים
        if _is_phone_number(send_to):
            from utils.phone import is_valid_israeli_e164
            if not is_valid_israeli_e164(send_to):
                return (False, None, "INVALID_PHONE", f"מספר {send_to} לא תקף")

        kwargs = {"content_sid": content_sid,
                  "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                  "to": f"whatsapp:{send_to}"}
        if variables:
            kwargs["content_variables"] = json.dumps(variables, ensure_ascii=False)
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url      # delivery tracking

        message = client.messages.create(**kwargs)
        return True, message.sid, None, None
    except Exception as exc:
        # ② Twilio זורק TwilioRestException עם .code; שומרים אותו לסיווג/אנליטיקה
        error_code = getattr(exc, "code", None)
        # ⚠️ None→"" מפורש (לא `or`): code יכול להיות int 0 — falsy אך תקף
        code_str = str(error_code) if error_code is not None else ""
        return False, None, code_str, str(exc)
```

### retry ב-broadcast (הדפוס מ-`broadcast_service.py`, Telegram — אותו עיקרון)

```python
try:
    await bot.send_message(chat_id=int(user_id), text=message_text)
    sent += 1
except RetryAfter as e:                  # rate limit (429) — הספק אומר כמה לחכות
    await asyncio.sleep(e.retry_after)
    await bot.send_message(chat_id=int(user_id), text=message_text)   # retry פעם אחת
except Forbidden:                        # הנמען חסם את הבוט
    _safe_unsubscribe(user_id)           # ← להסיר מנוי, לא לנסות שוב
    failed += 1
except (TimedOut, BadRequest):           # רשת / בקשה שגויה
    failed += 1
except Exception:
    logger.exception("...")              # אסור pass שקט
    failed += 1
```

| שגיאה | סיווג | פעולה |
|---|---|---|
| `RetryAfter` / 429 | rate limit | `sleep(retry_after)` + retry **פעם אחת** |
| `Forbidden` / 21408 | נמען חסם | להסיר מנוי, לסמן failed, **לא** לנסות שוב |
| `INVALID_PHONE` / 21211 | מספר לא תקין | ולידציה **לפני** שליחה → דלג |
| `TimedOut` / 5xx | שרת/רשת | לסמן failed (כאן בלי backoff; שווה להוסיף) |

**נקודות מפתח:**
- **client singleton** — לא ליצור Twilio Client בכל שליחה (overhead + connection churn).
- **ולידציה לפני שליחה** — לבדוק E.164 תקין מקומית חוסך round-trip ו-error codes מבלבלים. בפרויקט שלך, התאם ל-פורמט המספרים של השוק.
- **pacing** — `_PACE_SLEEP_SECONDS = 0.1` (10 msg/sec). Twilio WABA מוגבל ל-~80/sec; שמרנות מונעת 429. 1000 לידים ≈ 100 שניות.
- **כל שליחה ב-try/except נפרד בתוך הלולאה** — כשל בנמען 10 לא עוצר 990 נותרים. (כלל ברזל ללולאות I/O.)
- **`None → ""` מפורש** ב-error_code — `or` היה הופך `0` ל-`""` (0 הוא falsy אבל קוד תקף).
- **status callbacks** — Twilio שולחת webhook נפרד (`sent→delivered→read`) עם monotonic guard (אסור לחזור אחורה). זה מה שמזין את ה-`broadcast_deliveries.status`.

> **התאמה ל-Meta Cloud:** קודי השגיאה שונים (`131026` recipient unavailable, `131047` re-engagement נדרש מחוץ ל-24h, `130429` rate limit). הסיווג זהה: 24h-window error → צריך template; rate limit → backoff; invalid → דלג.

---

## 5. Length Handling + Page Fallback

### הדפוס: gate → page → short link

Twilio קוצץ הודעות WhatsApp מעל ~1600 תווים **בשקט, באמצע משפט** (Meta Cloud נדיב יותר — 4096). זה גרם לבאגים חוזרים בריפו. הפתרון: אם תשובה ארוכה מהסף → לייצר **עמוד HTML ציבורי** ולשלוח קישור קצר במקום הטקסט. בונוס: תשובות מורכבות (מחירונים, רשימות) נקראות יפה יותר בעמוד.

```
   response.text
        │
        ▼  len(formatted) > LIMIT ?
   ┌────┴────┐
  כן         לא
   │          └──► שליחה ישירה (raw)
   ▼
  generate_page_content (LLM שני) → response_pages (DB) → "קישור: /p/<id>"
   │
   └──► הקישור קצר מהסף → נשלח ישירות (אין recursion)
```

### השער (`whatsapp_webhook.py` / `meta_webhook.py`)

```python
def _send_whatsapp_response(to_number: str, text: str) -> None:
    """השער היחיד החוצה. כל handler חיצוני חייב לעבור דרכו (לא send_whatsapp ישיר)."""
    if len(text) > WHATSAPP_MAX_LENGTH and ADMIN_URL:
        try:
            _send_as_page(to_number, text)
            return
        except Exception:
            logger.error("המרה לעמוד נכשלה — נופלים לשליחה רגילה", exc_info=True)
    _send_whatsapp_raw(to_number, text)
```

הגרסה ל-Meta בודקת אורך על הטקסט ה**מפורמט** (אחרי הסרת HTML), כי זה מה שנשלח בפועל, ויש סף שונה לכל ערוץ:

```python
def _max_length_for_channel(channel: str) -> int:
    return META_INSTAGRAM_MAX_LENGTH if channel == "meta_ig" else META_MESSENGER_MAX_LENGTH  # 1000 / 2000

def _send_meta_response(internal_user_id, text, provider_asset_id):
    channel = parse_channel(internal_user_id)
    if len(format_message(text, channel)) > _max_length_for_channel(channel) and ADMIN_URL:
        _send_meta_as_page(internal_user_id, text, provider_asset_id); return
    _send_meta_raw(internal_user_id, text, provider_asset_id)
```

### יצירת העמוד

```python
def _send_as_page(to_number, text, intent=None, rag_context=""):
    title = _INTENT_PAGE_TITLES.get(intent.value if intent else "general", "מידע")
    try:
        from llm import generate_page_content
        page_html = generate_page_content(text, title=title, rag_context=rag_context)
    except Exception:
        _send_whatsapp_raw(to_number, text); return        # נכשל? לפחות נסה לשלוח

    page_id = db.create_response_page(content=page_html, title=title,
                                      user_id=to_number, page_type="whatsapp_fallback")
    page_url = f"{ADMIN_URL}/p/{page_id}"
    short_msg = f"הכנתי עבורכם את כל המידע בעמוד נוח לקריאה:\n{page_url}"
    _send_whatsapp_response(to_number, short_msg)           # קצר מהסף → נשלח ישירות
```

ה-`page_id` הוא **128 ביט אנטרופיה** (לא ניתן לניחוש), והעמוד הציבורי מוגן:

```python
page_id = secrets.token_urlsafe(16)     # 22 תווים base64url = 128 ביט

@app.route("/p/<page_id>")
def public_page(page_id):
    if _check_public_page_rate_limit(request.remote_addr):   # נגד brute-force של slugs
        return ("Too Many Requests", 429)
    page = db.get_response_page(page_id)
    resp = render_template("public_page.html", ...)
    # security headers — לא לאינדוקס, לא cache
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp
```

**נקודות מפתח:**
- **בדיקת אורך על הטקסט שנשלח בפועל** — ל-WhatsApp זה ה-markdown, ל-Meta זה ה-plain (אחרי `format_message`). לבדוק את ה-HTML הגולמי = טעות (התגים מנפחים).
- **אין recursion** — הקישור הקצר נשלח שוב דרך השער, אבל הוא קצר מהסף ועובר ישר ל-raw. (ב-Meta זה מפורש: `_send_meta_as_page` קורא ל-`_raw` ישירות.)
- **fallback בתוך fallback** — אם יצירת העמוד נכשלת (LLM/DB), נופלים לשליחה רגילה. עדיף הודעה חתוכה מאשר כלום.
- **`page_id` עם 128 ביט** — העמוד ציבורי (אין auth — הלקוח לא מחובר). entropy גבוה + rate-limit + `noindex` מונעים דליפה. **אל תשים שם PII רגיש.**
- **TTL** — `response_pages` נמחק אחרי 30 יום (cache, לא אחסון קבוע).
- **bytes מול chars** — עברית = 2 בייט/תו ב-UTF-8. אם הספק קוצץ לפי bytes, סף ה-chars מטעה. הריפו מלוגג את שניהם.

---

## 6. Markdown / Formatting per Channel

### הדפוס

ה-LLM מייצר פלט אחיד (HTML — `<b>`, `<i>`, `<a>`). כל ערוץ מתרגם אותו לפורמט שלו **בשכבת השליחה**, פעם אחת, במקום מרכזי. אסור לפזר `if channel ==` בכל מקום.

| תג | Telegram | WhatsApp | Meta DM (Messenger/IG) |
|---|---|---|---|
| `<b>x</b>` | נשאר | `*x*` | `x` (הסרה — plain) |
| `<i>x</i>` | נשאר | `_x_` | `x` |
| `<a href="u">t</a>` | נשאר | `t (u)` | `t (u)` |
| `<code>x</code>` | נשאר | `` `x` `` | `x` |

### המימוש (`formatter.py`)

```python
_BOLD_RE   = re.compile(r"<b>(.*?)</b>", re.DOTALL)
_ITALIC_RE = re.compile(r"<i>(.*?)</i>", re.DOTALL)
_LINK_RE   = re.compile(r'<a\s+href=["\']([^"\']*)["\']>(.*?)</a>', re.DOTALL)
_REMAINING_TAGS_RE = re.compile(r"<[^>]+>")
_META_CHANNELS = ("meta_msg", "meta_ig")


def format_message(html_text: str, channel: str) -> str:
    if channel == "telegram":
        return html_text                       # Telegram תומך HTML ישירות
    if channel == "whatsapp":
        return _html_to_whatsapp(html_text)
    if channel in _META_CHANNELS:
        return _html_to_plain(html_text)        # Messenger/IG = plain text בלבד
    return _html_to_plain(html_text)            # fallback בטוח


def _html_to_whatsapp(text: str) -> str:
    text = _LINK_RE.sub(r"\2 (\1)", text)       # קישורים לפני הסרת תגים!
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _ITALIC_RE.sub(r"_\1_", text)
    text = _REMAINING_TAGS_RE.sub("", text)     # תגים לא ידועים — הסרה
    return html.unescape(text)                  # &amp; → &  (WhatsApp = טקסט רגיל)


def _html_to_plain(text: str) -> str:
    """Messenger/Instagram — plain text. (לעברית אין Unicode bold כתחליף.)"""
    text = _LINK_RE.sub(r"\2 (\1)", text)       # שומרים URL מתוך <a>
    text = _REMAINING_TAGS_RE.sub("", text)
    return html.unescape(text)
```

**נקודות מפתח:**
- **קישורים לפני הסרת תגים** — אחרת `<a href="u">t</a>` הופך ל-`t` וה-URL אובד. הסדר קריטי.
- **`html.unescape`** — הפלט נשלח כטקסט רגיל, אז `&amp;` חייב לחזור ל-`&`.
- **WhatsApp markdown ב-word boundary** — ב-template renderer יש regex עדין יותר (`(?<!\w)\*...\*(?!\w)`) כך ש-`5*3=15` לא נהפך ל-bold אבל `*דני*` כן. `\w` ב-Unicode כדי שעברית תיחשב תו-מילה.
- **ל-Meta DM — plain בלבד**: Messenger/IG לא תומכים ב-bold כלל, **ולעברית אין Unicode bold** (יש לאנגלית: 𝐛𝐨𝐥𝐝). אז הסרת תגים. (זה היה באג אמיתי — תגי `<b>` הוצגו גולמיים.)

> **Meta Cloud API WhatsApp** תומך ב-markdown של WhatsApp (`*bold*`), אז שם תשתמש ב-`_html_to_whatsapp`, לא ב-plain.

---

## 7. Media Handling (אינבאונד) — מצב ופאטרן

### מצב נוכחי בריפו: **לא ממומש** (מדיה נכנסת נזרקת)

חשוב לדעת מראש — אין כאן קוד מוכן לחלץ. הריפו **מזהה** מדיה אבל לא מעבד:

```python
# whatsapp_webhook.py — הודעה בלי body (= מדיה בלבד) נזרקת
if not body and not button_payload and not list_id:
    logger.info("WhatsApp webhook: empty body (possibly media message)")
    return "", 200

# meta_webhook.py — attachments מזוהים כ-flag בלבד, לא מורדים
"has_attachments": bool(msg.get("attachments")),
# ...
if not text.strip():
    return        # סטיקר/attachment בלבד — אין טקסט לעבד
```

### הפאטרן להוספה (אם הליד שולח תמונה/הקלטה)

הנקודות שבהן הקוד כבר חושף את ה-hooks:
1. **חילוץ** — Twilio שולח `NumMedia`, `MediaUrl0`, `MediaContentType0` ב-form. Meta שולח `message.attachments[].payload.url`. הוסף אותם ל-normalization ב-webhook.
2. **הורדה** — `requests.get(media_url, auth=(SID, TOKEN))` (Twilio media דורש auth!), שמירה ל-storage או buffer.
3. **עיבוד** — אודיו → transcription (Whisper); תמונה → vision model. ואז להזרים את הטקסט המתומלל ל-`process_incoming_message` כאילו זו הודעת טקסט.

```python
# סקיצה לחילוץ ב-webhook (לא קיים בריפו — להוספה):
num_media = int(request.form.get("NumMedia", 0))
if num_media > 0:
    media_url = request.form["MediaUrl0"]
    content_type = request.form["MediaContentType0"]    # image/jpeg, audio/ogg
    resp = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    # → transcribe / vision → text → process_incoming_message(text=...)
```

**נקודת מפתח:** Twilio media URL דורש **auth** (לא ציבורי). Meta media URL הוא חתום וזמני (פג תוקף). הורד מיד, אל תשמור URLs.

---

## 8. מה לא לחלץ (חוסך זמן)

| רכיב | למה לדלג | מקור (לרפרנס בלבד) |
|---|---|---|
| `process_incoming_message` | הצינור שלך outbound-first, לא inbound-first | `core/message_processor.py` |
| Intent detection | אצלך 3-4 כוונות, לא 11 — regex פשוט יספיק | `intent.py` |
| Rate limiting | אצלך per-lead + per-customer, לא per-user יחיד | `rate_limiter.py` |
| Decorators chain | block/vacation/live_chat/consent — לא רלוונטי למודל שלך | `bot/decorators.py` |
| Conversation memory | תטמיע אחר (chatbot_build_guide + adaptation) | `core/`, `memory_*` |

---

## 9. טבלת קבצים מקוריים

| נושא | קובץ | פונקציות מפתח |
|---|---|---|
| Templates — submit | `messaging/whatsapp_templates_submit.py` | `submit_template_for_approval`, `sanitize_template_name` |
| Templates — sync | `messaging/whatsapp_templates_sync.py` | `sync_templates_from_twilio` |
| Templates — render | `messaging/template_renderer.py` | `substitute_variables`, `substitute_user_fields`, `render_preview` |
| Broadcast send | `messaging/broadcast_sender.py` | `_send_to_one`, `render_variables_for_user`, `send_campaign` |
| Signature (Meta) | `messaging/meta_webhook.py` | `_verify_signature` (קווים 33–53) |
| Signature (Twilio) | `messaging/whatsapp_webhook.py` | `_validate_twilio_signature` (קווים 47–64) |
| Adapter interface | `messaging/base.py` | `MessageAdapter` |
| Adapter — WhatsApp | `messaging/whatsapp_adapter.py` | `WhatsAppAdapter` |
| ID normalization | `messaging/meta_adapter.py` | `to_internal_user_id`, `to_provider_recipient`, `parse_channel` |
| Outbound — Twilio | `messaging/whatsapp_sender.py` | `send_whatsapp`, `_get_twilio_client`, `_is_phone_number` |
| Outbound — Graph | `messaging/meta_sender.py` | `send_meta_message` |
| Retry (broadcast) | `broadcast_service.py` | `send_broadcast` (קווים 168–197) |
| Length + page (WA) | `messaging/whatsapp_webhook.py` | `_send_whatsapp_response`, `_send_as_page` |
| Length + page (Meta) | `messaging/meta_webhook.py` | `_send_meta_response`, `_send_meta_as_page` |
| Public page route | `admin/app.py` | `public_page` (`/p/<id>`) |
| Formatting | `messaging/formatter.py` | `format_message`, `_html_to_whatsapp`, `_html_to_plain` |

---

## 10. משתני סביבה

```bash
# Twilio (WhatsApp)
TWILIO_ACCOUNT_SID=ACxxxxx
TWILIO_AUTH_TOKEN=xxxxx           # גם חתימת webhook (RequestValidator)
TWILIO_WHATSAPP_NUMBER=+9721234567

# Meta (אם Graph API ישיר / Cloud)
META_APP_SECRET=xxxxx             # חתימת X-Hub-Signature-256
META_VERIFY_TOKEN=xxxxx           # handshake GET

# תשתית משותפת
ADMIN_URL=https://your-domain     # בלעדיו אין page fallback (סעיף 5)
WHATSAPP_MAX_LENGTH=1600          # סף קציצה של Twilio
META_MESSENGER_MAX_LENGTH=2000
META_INSTAGRAM_MAX_LENGTH=1000
```

---

## סיכום — סדר היישום המומלץ

1. **Signature verification** (סעיף 2) — תשתית אבטחה, מהיר, עצמאי.
2. **Adapter + שער שליחה** (סעיפים 3-4) — העמוד השדרה. הכל עובר דרכו.
3. **Formatting + length/page** (סעיפים 5-6) — נתלים על השער.
4. **Templates** (סעיף 1) — הכי הרבה עבודה, אבל זה ה-core של "מכירות 24/7" (follow-up יזום). השאר אחרון כי הוא תלוי בכל השאר.
5. **Media** (סעיף 7) — רק אם ה-MVP צריך. לא קריטי לבוט מכירות טקסטואלי.
