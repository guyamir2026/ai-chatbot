"""
Configuration module for the AI Business Chatbot.
Loads settings from environment variables with sensible defaults.
"""

import os
import re
from pathlib import Path
from dotenv import load_dotenv

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
# Render-friendly storage configuration:
# - Render provides a dynamic `PORT` env var for web services.
# - For persistence you can mount a disk and set `DATA_DIR` to the mount path.
_DATA_DIR_DEFAULT = str(BASE_DIR / "data")

# Load .env file if it exists
# טוענים קודם את .env בשורש (הגדרות בסיסיות כמו DATA_DIR),
# ואז את DATA_DIR/.env עם override — כדי שהגדרות שנשמרו לדיסק הקבוע
# (למשל ב-Render) ידרסו את ברירות המחדל.
load_dotenv()
_persistent_env = Path(os.getenv("DATA_DIR", _DATA_DIR_DEFAULT)).resolve() / ".env"
if _persistent_env.exists():
    load_dotenv(_persistent_env, override=True)

DATA_DIR = Path(os.getenv("DATA_DIR", _DATA_DIR_DEFAULT)).resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "chatbot.db"))).resolve()
FAISS_INDEX_PATH = Path(os.getenv("FAISS_INDEX_PATH", str(DATA_DIR / "faiss_index"))).resolve()

# Ensure data directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
FAISS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)

# ─── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_OWNER_CHAT_ID = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")

# ─── Webhook ─────────────────────────────────────────────────────────────────
# כשמוגדר WEBHOOK_URL — הבוט עובר ממצב polling למצב webhook.
# הכתובת חייבת להיות HTTPS נגישה מהאינטרנט (למשל https://your-domain.com/telegram/webhook).
# WEBHOOK_SECRET משמש לאימות שהבקשות מגיעות מטלגרם בלבד.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ─── OpenAI / LLM ───────────────────────────────────────────────────────────
# ניתן לשנות את המודל דרך משתנה סביבה OPENAI_MODEL.
# לספקים חיצוניים (כמו Google Gemini דרך OpenAI-compatible API) — להגדיר גם OPENAI_BASE_URL.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# ─── LLM Intent Detection ──────────────────────────────────────────────────
# מודל קל לזיהוי כוונות — ברירת מחדל gpt-4.1-nano (זול ומהיר לסיווג).
# כשמופעל, הודעות שה-regex לא מצליח לסווג עוברות ל-LLM לפני שמסווגות כ-GENERAL.
LLM_INTENT_ENABLED = os.getenv("LLM_INTENT_ENABLED", "true").lower() in ("true", "1", "yes")
INTENT_MODEL = os.getenv("INTENT_MODEL", "gpt-4.1-nano")

# ─── RAG Settings ────────────────────────────────────────────────────────────
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "10"))
RAG_MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.3"))
CHUNK_MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "300"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))

# ─── Conversation Memory Settings ─────────────────────────────────────────
CONTEXT_WINDOW_SIZE = int(os.getenv("CONTEXT_WINDOW_SIZE", "10"))
SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "10"))

# ─── Customer Memory System (פר-לקוח, שלב 1-8) ────────────────────────────
# מערכת זיכרון מתמשך — extractor שמחלץ עובדות יציבות משיחות שהסתיימו
# ומזריק אותן ל-context של הבוט בשיחות הבאות. ראה
# docs/Customer-memory/claude_code_instructions.md.
#
# single-tenant: BUSINESS_ID משמש כקבוע בכל קריאה ל-CRUD של memory.
# forward-compat ל-multi-tenant — הסכימה תומכת ב-business_id כעמודה.
BUSINESS_ID = os.getenv("BUSINESS_ID", "default")
# מודלי ה-LLM של רכיב הזיכרון — *קבועים*, לא ENV. ה-spec של מערכת
# הזיכרון (docs/Customer-memory/) דורש gpt-4.1-mini במפורש, והפרומפט
# תוכנן ל-OpenAI ספציפית. ה-client הבלעדי ב-memory/openai_client.py
# מבטיח שזה ירוץ מול OpenAI אמיתי גם אם הבוט הראשי מכוון ל-Gemini.
MEMORY_EXTRACTION_MODEL = "gpt-4.1"
MEMORY_JUDGE_MODEL = "gpt-4.1-mini"
# Embedding model ל-pre-filter של existing_facts. *קבוע* — לא יורש
# מ-EMBEDDING_MODEL הראשי שעשוי להיות מכוון לספק אחר (Gemini למשל).
# חייב להיות מודל של OpenAI כי ה-client של memory עובד רק מולם.
MEMORY_EMBEDDING_MODEL = "text-embedding-3-small"
# קאפ על facts קיימים שנשלחים ל-prompt (מעל זה — pre-filter סמנטי).
MEMORY_EXISTING_FACTS_CAP = int(os.getenv("MEMORY_EXISTING_FACTS_CAP", "12"))
# קאפ על הודעות בשיחה שנשלחות ל-LLM (בעיות נפוצות #5 ב-spec).
MEMORY_CONVERSATION_CAP = int(os.getenv("MEMORY_CONVERSATION_CAP", "50"))
# האם להפעיל את ה-background worker (שלב 6 — memory/background.py).
MEMORY_BACKGROUND_ENABLED = os.getenv("MEMORY_BACKGROUND_ENABLED", "true").lower() in ("true", "1", "yes")
# סף "שיחה הסתיימה" — אם ההודעה האחרונה של המשתמש לפני יותר מזה,
# ה-scheduler יחלץ facts. אחרת ידלג ויחזור בסבב הבא.
MEMORY_IDLE_MINUTES = int(os.getenv("MEMORY_IDLE_MINUTES", "30"))
# חלון סריקה לאחור — scheduler בודק רק משתמשים שדיברו ב-X הימים האחרונים.
MEMORY_LOOKBACK_DAYS = int(os.getenv("MEMORY_LOOKBACK_DAYS", "7"))
# האם להזריק facts ל-context של הבוט (שלב 8). כיבוי לא עוצר extraction —
# רק מונע הזרקה. שימושי לבדיקת השפעה על איכות התשובות.
# parsing עקבי עם שאר ה-toggles בקובץ (true/1/yes — case-insensitive).
MEMORY_INJECTION_ENABLED = os.getenv("MEMORY_INJECTION_ENABLED", "true").lower() in ("true", "1", "yes")
# סף יישנות ל-facts (בימים). fact שלא אומת/נאמר מעבר לסף יסומן
# "ייתכן שלא רלוונטי" ב-context, והבוט יודע לטפל בו בזהירות.
MEMORY_STALENESS_DAYS = int(os.getenv("MEMORY_STALENESS_DAYS", "90"))

# ─── Lead Follow-up ─────────────────────────────────────────────────────────
# פיצ'ר follow-up אוטומטי — שולח הודעה ללידים שלא השלימו הזמנה.
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "false").lower() in ("true", "1", "yes")
# מודל LLM למנוע ההחלטה (Gemini Flash מומלץ — זול ומהיר).
FOLLOWUP_MODEL = os.getenv("FOLLOWUP_MODEL", "gemini-3.0-flash")
# שעות המתנה לפני שליחת follow-up (ברירת מחדל: 24 שעות).
FOLLOWUP_DELAY_HOURS = float(os.getenv("FOLLOWUP_DELAY_HOURS", "24"))
# סף ביטחון מינימלי (0–100) — מתחתיו לא שולחים אוטומטית.
FOLLOWUP_MIN_CONFIDENCE = int(os.getenv("FOLLOWUP_MIN_CONFIDENCE", "60"))
# תדירות בדיקת לידים זכאיים (בדקות).
FOLLOWUP_CHECK_INTERVAL_MINUTES = int(os.getenv("FOLLOWUP_CHECK_INTERVAL_MINUTES", "15"))
# מרווח בטיחות ל-WhatsApp לפני סגירת חלון השיחה של Twilio (24 שעות מאז
# ההודעה האחרונה של הלקוח). אחרי החלון, ההודעה הופכת ל-template עם
# תמחור גבוה. שליחה מעט לפני החלון שומרת על תמחור session — ברירת מחדל
# 15 דקות, ולכן due_at לוואטסאפ = (24h - 15min) = 23h45m.
FOLLOWUP_WHATSAPP_BUFFER_MINUTES = int(os.getenv("FOLLOWUP_WHATSAPP_BUFFER_MINUTES", "15"))

# ─── מסך הסכמה ראשוני (תיקון 13) ────────────────────────────────────────────
# כשהפלאג OFF (ברירת מחדל): המשתמש לא רואה את מסך ההסכמה הגדול בכניסה הראשונה,
# וה-handlers לא חוסמים אותו. שדה consent_given_at ב-DB *לא* נכתב באופן אוטומטי
# כדי לא לזייף הסכמה. הפקודות /myinfo, /forget, /stop, ודפי /legal/* ממשיכים
# לעבוד. הפעלה (true) מחזירה את ההתנהגות המלאה של תיקון 13.
CONSENT_SCREEN_ENABLED = os.getenv("CONSENT_SCREEN_ENABLED", "false").lower() in ("true", "1", "yes")

# ─── Rate Limiting ───────────────────────────────────────────────────────────
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "50"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "100"))

# ─── Admin Panel ─────────────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
#
# Security note:
# - Do not embed default secrets in code.
# - These are intentionally empty by default and must be provided via environment.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")
ADMIN_HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.getenv("ADMIN_PORT") or os.getenv("PORT") or "5000")
# כתובת הפאנל הציבורית — משמשת לקישורים בהתראות לבעל העסק.
# דוגמה: https://my-bot-admin.onrender.com
ADMIN_URL = os.getenv("ADMIN_URL", "").rstrip("/")

# ─── Developer-only access (Plans + Feature Flags) ──────────────────────────
# סיסמה נפרדת לאיזור /dev/* שבו המפתח (ספק ה-SaaS) משנה את החבילה של
# הלקוח. מכוון להפריד מכניסת ה-admin הרגילה של בעל העסק. אם לא מוגדר —
# כל הראוטים תחת /dev/* יחזירו 404 (כדי לא לחשוף את קיומם).
DEVELOPER_PASSWORD = os.getenv("DEVELOPER_PASSWORD", "")
# chat_id של המפתח להתראות אקטיביות (mismatch של channel ב-startup וכו').
# אם לא מוגדר — נשלח רק לוג, ללא הודעת טלגרם.
DEVELOPER_TELEGRAM_CHAT_ID = os.getenv("DEVELOPER_TELEGRAM_CHAT_ID", "")
# שם פריסה לזיהוי בהתראות (איזה לקוח שלח). אם לא מוגדר — fallback ל-
# BUSINESS_NAME, ואז ל-RENDER_SERVICE_NAME (Render מספק אוטומטית), ואז HOSTNAME.
DEPLOYMENT_NAME = (
    os.getenv("DEPLOYMENT_NAME")
    or os.getenv("RENDER_SERVICE_NAME")
    or ""
).strip()

# ─── Demo Mode ───────────────────────────────────────────────────────────────
# מצב דמו לקמפיין שיווקי: גולש מהמודעה נכנס ל-/demo, מקבל session מבודדת
# שמאפשרת קריאה בלבד, ולא יכול לבצע POST/PUT/DELETE/PATCH. ראה
# docs/demo-mode-spec.md לפרטים. כשמכובה — /demo מחזיר 404.
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("true", "1", "yes")
# קישור WhatsApp לרכישה — מופיע ב-CTA הצף ובבאנר הדמו. בלעדיו ה-CTA מוסתר.
DEMO_CTA_WHATSAPP = os.getenv("DEMO_CTA_WHATSAPP", "").strip()
# קישור לבוט הטלגרם החי (https://t.me/<bot>?start=demo) — מופיע בכרטיס
# "דבר עם הבוט" ב-dashboard. בלעדיו הכרטיס מוסתר.
DEMO_LIVE_BOT_URL = os.getenv("DEMO_LIVE_BOT_URL", "").strip()

# ─── Business Info (defaults for demo) ───────────────────────────────────────
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Dana's Beauty Salon")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "")
BUSINESS_ADDRESS = os.getenv("BUSINESS_ADDRESS", "")
BUSINESS_WEBSITE = os.getenv("BUSINESS_WEBSITE", "")

# ─── Telegram Bot Username (for QR code generation) ─────────────────────────
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")

# ─── WhatsApp / Twilio ──────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
# מספר WhatsApp של בעל העסק — לקבלת התראות על תורים ובקשות נציג.
# פורמט E.164: +972XXXXXXXXX (עם + וללא 0 מוביל).
OWNER_WHATSAPP_NUMBER = os.getenv("OWNER_WHATSAPP_NUMBER", "")

# ─── WhatsApp Response Pages ───────────────────────────────────────────────
# מגבלת תווים להודעת WhatsApp רגילה — מעבר לסף הזה, התשובה תוגש כעמוד HTML.
WHATSAPP_MAX_LENGTH = int(os.getenv("WHATSAPP_MAX_LENGTH", "1600"))

# ─── Meta (Instagram + Facebook Messenger DM) ───────────────────────────────
# פרטי האפליקציה ב-Meta for Developers. כל ה-deployment משתף את אותה
# אפליקציה — credentials ספציפיים לעמוד נשמרים ב-meta_credentials ב-DB
# (יבוצע בשלב 2 של המימוש).
META_APP_ID = os.getenv("META_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
# Token חופשי שאני בוחר — מטא משווה אליו ב-handshake של ה-webhook.
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
# גרסת Graph API. עדכון תקופתי לפי https://developers.facebook.com/docs/graph-api/changelog
META_GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v21.0")
# OAuth redirect URI — חייב להתאים בדיוק למה שמוגדר ב-Meta App Dashboard.
# למשל: https://your-domain.com/admin/meta/callback
META_OAUTH_REDIRECT_URI = os.getenv("META_OAUTH_REDIRECT_URI", "")
# תקרות אורך הודעה — מעבר אליהן יוצא לעמוד HTML ציבורי (`/p/<page_id>`).
# Messenger: 2000 תווים. Instagram DM: 1000 תווים (קצר משמעותית מ-WhatsApp).
META_MESSENGER_MAX_LENGTH = int(os.getenv("META_MESSENGER_MAX_LENGTH", "2000"))
META_INSTAGRAM_MAX_LENGTH = int(os.getenv("META_INSTAGRAM_MAX_LENGTH", "1000"))

# ─── Web Push Notifications (VAPID) ────────────────────────────────────────
# התראות לבעל העסק כשלשונית הדשבורד סגורה — דרך תקן Web Push (RFC 8030).
# מפתחות VAPID מזהים את השרת לשירות ה-push של הדפדפן (FCM, Mozilla, Apple).
# יצירת זוג מפתחות חד-פעמית:
#   python -m utils.vapid_keygen
# (או דרך py-vapid CLI). ה-private נשאר רק בשרת; ה-public נחשף ל-client.
# בלי שלושת הערכים — המנגנון מושבת בשקט (לוג WARNING חד-פעמי).
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
# subject חייב להיות mailto:... או URL — מזהה את הבעלים של השרת מול ה-push service.
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "")

# ─── Developer Notifications ───────────────────────────────────────────────
# בוט טלגרם נפרד של המפתח — לקבלת דיווחי באגים מבעלי עסקים.
# יוצרים בוט אחד ב-BotFather ומשתמשים בו בכל ה-deployments.
DEVELOPER_BOT_TOKEN = os.getenv("DEVELOPER_BOT_TOKEN", "")
DEVELOPER_CHAT_ID = os.getenv("DEVELOPER_CHAT_ID", "")
# מייל כגיבוי/ערוץ נוסף — שולח לשניהם אם שניהם מוגדרים.
DEVELOPER_EMAIL = os.getenv("DEVELOPER_EMAIL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")  # אם ריק — משתמש ב-DEVELOPER_EMAIL
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# ─── Google Calendar OAuth ──────────────────────────────────────────────────
# יצירת credentials ב-Google Cloud Console:
# https://console.cloud.google.com/apis/credentials
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")

# ─── System Prompt (Layer A) ────────────────────────────────────────────────

# מבנה data-driven אחד לכל טון — כל המאפיינים במקום אחד לתחזוקה קלה.
# כדי להוסיף טון חדש: להוסיף מפתח אחד ל-TONE_PROFILES עם כל השדות.
TONE_PROFILES: dict[str, dict[str, str]] = {
    "none": {
        "label": "ללא בחירה",
        "definition": "הטון ייקבע לפי הפרומפט העסקי או הגדרות ה-Seed בלבד.",
        "identity": "המטרה שלך היא לספק מידע מדויק ומועיל.",
        "descriptor": "נציג דיגיטלי של העסק",
        "guidelines": (
            '- הימנע לחלוטין מביטויים רובוטיים כמו "אני מודל שפה", "כבינה מלאכותית".\n'
            '- השתמש ב"אנחנו" (צוות העסק) במקום ב"אני".\n'
            "- השתמש ברשימות עם תבליטים (•) כשיש מספר פריטים. כשנשאל על מחירון או רשימת שירותים — פרט את הכל, בלי לקצר או לסכם."
        ),
        # ריק בכוונה — טון "none" לא מציג קטע "מבנה התשובה" (המבנה נגזר
        # מהפרומפט העסקי). ראה build_system_prompt.
        "response_structure": "",
    },
    "friendly": {
        "label": "ידידותי",
        "definition": (
            "אתה נציג שירות לקוחות ידידותי, חם ומסביר פנים. "
            "דבר בטון חברי, אישי וקליל. "
            "מותר להשתמש באימוג'ים בזהירות (😊, ✨, 👋) — בלי להגזים. "
            "תרגיש כמו חבר טוב שעובד בעסק."
        ),
        "identity": (
            'אתה הלב הדיגיטלי של העסק. אתה לא "בינה מלאכותית" – אתה נציג שירות אנושי, מקצועי ומיומן.\n'
            "צור אינטראקציה עם לקוחות בצורה שמרגישה 100% אנושית וטבעית. "
            "המטרה שלך היא לספק מידע מדויק ומועיל תוך בניית קשר אישי. "
            "בדיוק כמו שבעל עסק קטן היה עושה."
        ),
        "descriptor": "חבר צוות מקצועי, חם ומיומן שמייצג את קול המותג",
        "guidelines": (
            '- שפה טבעית: השתמש בעברית שיחתית וחמה. הימנע לחלוטין מביטויים רובוטיים כמו "אני מודל שפה", "כבינה מלאכותית" או "במה אוכל לסייע לך היום?".\n'
            "- אמפתיה תחילה: אם לקוח מתוסכל, הבע צער כנציג אנושי. אם הוא נרגש, שמח בשמחו.\n"
            '- אווירת עסק קטן: השתמש ב"אנחנו" (צוות העסק) במקום ב"אני".\n'
            "- זרימה ובהירות: השתמש ברשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים. כשנשאל על מחירון או רשימת שירותים — פרט את הכל, בלי לקצר או לסכם."
        ),
        "response_structure": (
            '1. פתיחה חמה (תלוית הקשר — בוקר טוב / ערב טוב / לקוח חוזר).\n'
            '2. תשובה מקיפה ואנושית.\n'
            '3. סגירה טבעית (למשל: "אם צריך עוד משהו, אנחנו כאן!").'
        ),
    },
    "formal": {
        "label": "רשמי",
        "definition": (
            "אתה נציג שירות לקוחות מקצועי ומכובד. "
            "דבר בטון רשמי, אדיב ומנומס. "
            "הימנע מסלנג, קיצורים ואימוג'ים לחלוטין. "
            "שמור על ניסוח ענייני ומכבד."
        ),
        "identity": (
            'אתה הנציג הדיגיטלי של העסק. אתה לא "בינה מלאכותית" – אתה נציג שירות מקצועי ומיומן.\n'
            "צור אינטראקציה עם לקוחות בצורה מכבדת ואמינה. "
            "המטרה שלך היא לספק מידע מדויק ומועיל תוך שמירה על רמת שירות גבוהה."
        ),
        "descriptor": "נציג מקצועי ומיומן שמייצג את העסק בסטנדרט הגבוה ביותר",
        "guidelines": (
            '- שפה מכובדת: השתמש בעברית תקינה ורשמית. הימנע לחלוטין מביטויים רובוטיים כמו "אני מודל שפה", "כבינה מלאכותית" או "במה אוכל לסייע לך היום?".\n'
            "- אמפתיה מקצועית: אם לקוח מתוסכל, הבע הזדהות מקצועית ומכבדת. אם הוא שבע רצון, הערך את אמונו.\n"
            '- מקצועיות: השתמש ב"אנחנו" (צוות העסק) במקום ב"אני".\n'
            "- זרימה ובהירות: השתמש ברשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים. כשנשאל על מחירון או רשימת שירותים — פרט את הכל, בלי לקצר או לסכם."
        ),
        "response_structure": (
            '1. פתיחה מנומסת (תלוית הקשר — בוקר טוב / ערב טוב / פנייה מכבדת).\n'
            '2. תשובה מקיפה ומקצועית.\n'
            '3. סגירה אדיבה (למשל: "נשמח לעמוד לרשותכם בכל שאלה נוספת.").'
        ),
    },
    "sales": {
        "label": "מכירתי",
        "definition": (
            "אתה נציג שירות לקוחות שירותי ומוכוון-מכירות. "
            "כוון את הלקוח באלגנטיות לשלב הבא — בין אם זה קביעת תור, "
            "ניסיון מוצר חדש או מבצע. "
            "השתמש בשפה חיובית ומזמינה שמעודדת פעולה, "
            "והצע שירותים רלוונטיים כשזה מתאים טבעית לשיחה."
        ),
        "identity": (
            'אתה הלב הדיגיטלי של העסק. אתה לא "בינה מלאכותית" – אתה נציג שירות אנושי, מקצועי ומיומן.\n'
            "צור אינטראקציה עם לקוחות בצורה שמרגישה 100% אנושית וטבעית. "
            "המטרה שלך היא לספק מידע מדויק ומועיל תוך הובלת הלקוח לשלב הבא. "
            "בדיוק כמו שבעל עסק קטן היה עושה."
        ),
        "descriptor": "נציג מקצועי, שירותי ומיומן שמייצג את קול המותג",
        "guidelines": (
            '- שפה מזמינה: השתמש בעברית חיובית ומזמינה. הימנע לחלוטין מביטויים רובוטיים כמו "אני מודל שפה", "כבינה מלאכותית" או "במה אוכל לסייע לך היום?".\n'
            "- אמפתיה תחילה: אם לקוח מתוסכל, הבע צער כנציג אנושי. אם הוא נרגש, שמח בשמחו.\n"
            '- אווירת עסק קטן: השתמש ב"אנחנו" (צוות העסק) במקום ב"אני".\n'
            "- זרימה ובהירות: השתמש ברשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים. כשנשאל על מחירון או רשימת שירותים — פרט את הכל, בלי לקצר או לסכם."
        ),
        "response_structure": (
            '1. פתיחה מזמינה (תלוית הקשר — בוקר טוב / ערב טוב / לקוח חוזר).\n'
            '2. תשובה מקיפה עם הצעת ערך.\n'
            '3. סגירה שמעודדת פעולה (למשל: "תרצו לקבוע תור כדי להתנסות?").'
        ),
    },
    "luxury": {
        "label": "יוקרתי",
        "definition": (
            "אתה נציג שירות לקוחות בסגנון יוקרתי ומעודן. "
            "דבר בביטויים מנומסים כמו \"בוודאי\", \"בשמחה\", \"נשמח לארח\". "
            "הקרן שקט, איכות ותשומת לב לפרטים. "
            "ללא סימני קריאה מרובים או אימוג'ים."
        ),
        "identity": (
            'אתה הנציג הדיגיטלי של העסק. אתה לא "בינה מלאכותית" – אתה נציג שירות מעודן ומיומן.\n'
            "צור אינטראקציה עם לקוחות בצורה מלוטשת ואיכותית. "
            "המטרה שלך היא לספק מידע מדויק ומועיל תוך הקרנת איכות ותשומת לב לפרטים."
        ),
        "descriptor": "נציג מעודן ומיומן שמייצג את העסק ברמה הגבוהה ביותר",
        "guidelines": (
            '- שפה מעודנת: השתמש בעברית תקינה ומלוטשת. הימנע לחלוטין מביטויים רובוטיים כמו "אני מודל שפה", "כבינה מלאכותית" או "במה אוכל לסייע לך היום?".\n'
            "- אמפתיה עדינה: אם לקוח מתוסכל, הבע הזדהות מעודנת ומכבדת. אם הוא שבע רצון, שמח לארח.\n"
            '- נוכחות מעודנת: השתמש ב"אנחנו" (צוות העסק) במקום ב"אני".\n'
            "- זרימה ובהירות: השתמש ברשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים. כשנשאל על מחירון או רשימת שירותים — פרט את הכל, בלי לקצר או לסכם."
        ),
        "response_structure": (
            '1. פתיחה מעודנת (תלוית הקשר — בוקר טוב / ערב טוב / לקוח חוזר).\n'
            '2. תשובה מקיפה ואיכותית.\n'
            '3. סגירה מכבדת (למשל: "נשמח לארח אתכם בכל עת.").'
        ),
    },
}

# תאימות לאחור — נגזרות מ-TONE_PROFILES למקומות שמייבאים את השמות הישנים
TONE_DEFINITIONS: dict[str, str] = {k: v["definition"] for k, v in TONE_PROFILES.items()}
TONE_LABELS: dict[str, str] = {k: v["label"] for k, v in TONE_PROFILES.items()}
_AGENT_IDENTITY: dict[str, str] = {k: v["identity"] for k, v in TONE_PROFILES.items()}
_AGENT_DESCRIPTOR: dict[str, str] = {k: v["descriptor"] for k, v in TONE_PROFILES.items()}
_CONVERSATION_GUIDELINES: dict[str, str] = {k: v["guidelines"] for k, v in TONE_PROFILES.items()}
_RESPONSE_STRUCTURE: dict[str, str] = {k: v["response_structure"] for k, v in TONE_PROFILES.items()}


# תווים מותרים בביטויים מותאמים אישית — אותיות (כל שפה), ספרות, רווחים,
# סימני פיסוק בסיסיים, ותווים עסקיים נפוצים (מטבעות, אחוזים, לוכסן וכו').
# חוסם תווים שעלולים לשמש ל-prompt injection (כמו מפרידי סקשנים ── או הנחיות מערכת).
# en-dash (–) ו-em-dash (—) חסומים — LLMs מפרשים רצפי מקפים כמפרידי סקשנים.
_CUSTOM_PHRASES_PATTERN = re.compile(
    r"[^\w\s\u0590-\u05FF\u0600-\u06FF.,!?;:'\"\-()•·\n%₪$€/+#&@]",
    re.UNICODE,
)
# אורך מקסימלי לביטויים מותאמים — הגנה מפני הצפת פרומפט
_CUSTOM_PHRASES_MAX_LENGTH = 500


def _sanitize_custom_phrases(text: str) -> str:
    """סניטציה של ביטויים מותאמים אישית — מסיר תווים חשודים ומגביל אורך."""
    cleaned = _CUSTOM_PHRASES_PATTERN.sub("", text).strip()
    if len(cleaned) > _CUSTOM_PHRASES_MAX_LENGTH:
        # חותך בגבול מילה כדי לא לשבור טקסט באמצע
        cleaned = cleaned[:_CUSTOM_PHRASES_MAX_LENGTH].rsplit(" ", 1)[0]
    return cleaned


def _build_formatting_rules(channel: str) -> str:
    """בניית הנחיות עיצוב טקסט בהתאם לערוץ."""
    if channel == "whatsapp":
        return (
            "חוק ברזל: השתמש אך ורק בעיצוב WhatsApp. אסור להשתמש בתגי HTML.\n"
            "עיצוב מותר:\n"
            "- *טקסט מודגש* — לכותרות, שמות קטגוריות ושמות שירותים\n"
            "- _טקסט נטוי_ — להערות משניות, הבהרות ותנאים\n"
            "- ~טקסט קו חוצה~ — למחירים ישנים/מבצעים\n"
            "- רשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים\n"
            "- רווח ברור בין פסקאות ונושאים שונים\n"
            "דוגמה נכונה: *תספורת נשים* — _45 דקות_ — 99 ש\"ח\n"
            "דוגמה שגויה: <b>תספורת נשים</b> — <i>45 דקות</i>"
        )
    if channel == "widget":
        # ערוץ widget באתר חיצוני — טקסט נקי בלבד. אסור HTML, אסור Markdown,
        # כי הצד-לקוח מציג את התשובה דרך textContent ולא מפענח שום פורמט.
        return (
            "חוק ברזל: כתוב בטקסט רגיל בלבד. אסור להשתמש בתגי HTML "
            "(כמו <b>, <i>, <u>) ואסור בתחביר Markdown "
            "(כוכביות, קווים תחתונים, סולמיות, ~ או backticks).\n"
            "עיצוב מותר:\n"
            "- שורות חדשות ופסקאות נפרדות לקריאות\n"
            "- רשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים\n"
            "- רווח ברור בין פסקאות ונושאים שונים\n"
            "דוגמה נכונה: תספורת נשים — 45 דקות — 99 ש\"ח\n"
            "דוגמה שגויה: <b>תספורת נשים</b> — *45 דקות* — _99 ש\"ח_"
        )
    # ברירת מחדל — טלגרם
    return (
        "חוק ברזל: השתמש אך ורק בתגי HTML של טלגרם. אסור בהחלט להשתמש בתחביר Markdown "
        "(כוכביות, קווים תחתונים, סולמיות).\n"
        "תגים מותרים:\n"
        "- תג b (פתיחה וסגירה) — לכותרות, שמות קטגוריות ושמות שירותים\n"
        "- תג i (פתיחה וסגירה) — להערות משניות, הבהרות ותנאים\n"
        "- תג u (פתיחה וסגירה) — להדגשת פרטים חשובים כמו מחיר מבצע או משך טיפול\n"
        "- לא יותר מ-3 קווים תחתונים (קו תחתון ע\"י תגי u כמובן) בתשובה אחת\n"
        "- רשימות עם תבליטים (•) כשיש מספר פריטים, שירותים או מחירים\n"
        "- רווח ברור בין פסקאות ונושאים שונים\n"
        "דוגמה נכונה: <b>תספורת נשים</b> — <i>45 דקות</i> — <u>מבצע 99 ש\"ח</u>\n"
        "דוגמה שגויה: **תספורת נשים** — *45 דקות* — __מבצע 99 ש\"ח__\n"
        "אם תשתמש בכוכביות (*) או בקווים תחתונים (_) להדגשה — התשובה תיחשב שגויה."
    )


def _build_channel_rules(channel: str) -> str:
    """בניית כללים 5-7 בהתאם לערוץ — טלגרם (כפתורים) או WhatsApp (טקסט)."""
    # ── כלל ההעברה לנציג ──
    # מנגנון "טוקן סמן": ה-LLM פותח את התשובה ב-HANDOFF_MARKER ואחריו את
    # הטקסט הקבוע. הפרסר במשק ב-core/message_processor.py מזהה את הטוקן,
    # מסיר אותו, ומפעיל את צינור בקשת הנציג. שיטה דטרמיניסטית — בלי
    # fuzzy matching שיוצר false positives/negatives.
    handoff_rule = (
        f"7. אם הלקוח מתוסכל או מבקש לדבר עם אדם, התחל את התשובה בדיוק במחרוזת "
        f"{HANDOFF_MARKER} (כולל הסוגריים המרובעים, ללא רווחים לפניה, בשורה משלה), "
        f"ואז שורה ריקה ואז ענה בדיוק את הטקסט: \"{FALLBACK_RESPONSE}\".\n"
        f"   חשוב: אל תוסיף תגיות אחרות, אל תשנה את הטקסט הקבוע, ואל תכתוב {HANDOFF_MARKER} "
        f"בשום מצב אחר — הוא נשמר אך ורק לבקשת העברה."
    )

    if channel == "whatsapp":
        return (
            "5. אם הלקוח רוצה לקבוע תור, בקש ממנו לכתוב את השירות שהוא מעוניין בו ותסייע לו בקביעה.\n"
            "6. אם הלקוח שואל על המיקום, שלח לו את כתובת העסק ישירות בתשובה.\n"
            f"{handoff_rule}\n"
            "חשוב: אתה משוחח עם הלקוח ב-WhatsApp. אל תזכיר כפתורים, בוט טלגרם, או ממשקי טלגרם — הם לא קיימים כאן."
        )
    if channel == "widget":
        # ערוץ widget — אין כפתורים, אין שיתוף מיקום, אין handoff בנוסח
        # הרגיל (HANDOFF_MARKER), אבל **כן** קיים מסלול ליד באמצעות
        # LEAD_MARKER: כשהמבקר מבקש שיחזרו אליו, הבוט אוסף ממנו שם
        # וטלפון, ואז מסמן את התשובה ב-LEAD_MARKER עם השדות המובנים.
        # הצד שרת מנתח את הטוקן, פותח בקשת נציג ב-DB, ושולח התראה
        # לבעל העסק. בלי הטוקן — הליד נשרף.
        return (
            "5. אם המבקר רוצה לקבוע תור, לדבר עם נציג, או שיחזרו אליו "
            "(שאלה כמו 'תוכלו לחזור אליי?', 'איך פונים?', 'בעל העסק יכול "
            "לדבר איתי?'), בקש ממנו במשפט אחד שם וטלפון. כשקיבלת את "
            f"שניהם — פתח את התשובה בדיוק במחרוזת {LEAD_MARKER} (כולל "
            "הסוגריים המרובעים, בשורה משלה), אחריה שתי שורות במבנה:\n"
            "name: <השם של המבקר>\n"
            "phone: <מספר הטלפון של המבקר>\n"
            "(שורה ריקה)\n"
            "ענה את הטקסט: 'מצוין! פנייתך התקבלה ובעל העסק יחזור אליך "
            "בהקדם.'\n"
            "   חשוב: אל תשתמש בטוקן הזה לפני שיש לך גם שם וגם טלפון "
            "תקין. אם המבקר נתן רק שם או רק טלפון — בקש בנימוס את הפרט "
            "החסר, ואל תכתוב את הטוקן עדיין.\n"
            "6. אם המבקר שואל על המיקום, ענה את כתובת העסק ישירות "
            "בתשובה כטקסט.\n"
            f"7. אל תכתוב לעולם את המחרוזת {HANDOFF_MARKER} — היא לא "
            f"רלוונטית בערוץ הזה. השתמש רק ב-{LEAD_MARKER} כפי שתואר בכלל 5.\n"
            "חשוב: אתה משוחח עם המבקר באתר האינטרנט של העסק. אל תזכיר "
            "כפתורי טלגרם, ממשקי בוט, או הוראות שתלויות באפליקציה ספציפית "
            "— המבקר רואה תיבת צ'אט באתר בלבד."
        )
    return (
        "5. אם הלקוח רוצה לקבוע תור, הנחה אותו להשתמש בכפתור בקשת התור.\n"
        "6. אם הלקוח שואל על המיקום, הצע להשתמש בכפתור שליחת המיקום.\n"
        f"{handoff_rule}"
    )


def build_system_prompt(
    tone: str = "friendly",
    custom_phrases: str = "",
    follow_up_enabled: bool = False,
    custom_prompt: str = "",
    channel: str = "telegram",
) -> str:
    """בניית פרומפט מערכת משופר המשלב הנחיות טון, DNA עסקי וכללי התנהגות.

    משלב את הפרומפט המשופר (אנושי, מותאם טון) עם עשרת הכללים המקוריים.
    כשהפיצ'ר שאלות המשך פעיל — כלל 11 מוזרק לאחר כלל 10, לפני סקשן המגבלות.
    custom_prompt — הנחיות מותאמות אישית מבעל העסק, מוזרקות לפני סקשן הכללים.
    """
    effective_tone = tone if tone in TONE_PROFILES else "friendly"
    profile = TONE_PROFILES[effective_tone]
    tone_text = profile["definition"]
    agent_desc = profile["descriptor"]
    conv_guidelines = profile["guidelines"]
    resp_structure = profile["response_structure"]
    identity = profile["identity"]

    # קטע "טון תקשורת" מוזרק רק כשנבחר טון בפועל. בטון "none" (ללא בחירה)
    # אין טקסט טון להזריק — הטון נקבע מהפרומפט העסקי / הביטויים — ולכן
    # מדלגים על הקטע כדי לא להציג כותרת עם שורת placeholder בפרומפט.
    # ה-definition של "none" עדיין משמש כתיאור בבורר הטונים בפאנל — הוא נשאר.
    tone_section = ""
    if effective_tone != "none" and tone_text and tone_text.strip():
        tone_section = f"\n── טון תקשורת ──\n{tone_text}\n"

    # קטע "מבנה התשובה" מוזרק רק כשהטון מגדיר מבנה. טון "none" מגדיר מבנה
    # ריק ולכן הקטע מושמט — המבנה נקבע מהפרומפט העסקי.
    structure_section = ""
    if resp_structure and resp_structure.strip():
        structure_section = f"\n── מבנה התשובה ──\n{resp_structure}\n"

    # ביטויים מותאמים אישית (DNA עסקי) — עם סניטציה נגד prompt injection
    dna_section = ""
    if custom_phrases and custom_phrases.strip():
        safe_phrases = _sanitize_custom_phrases(custom_phrases)
        if safe_phrases:
            dna_section = (
                "\nביטויים אופייניים לעסק (השתמש בהם באופן טבעי בשיחה):\n"
                f"{safe_phrases}\n"
            )

    # כלל 10 — שאלות המשך (מוזרק רק כשהפיצ'ר פעיל, מיד אחרי כלל 9)
    follow_up_rule = ""
    if follow_up_enabled:
        follow_up_rule = (
            "\n10. בסוף כל תשובה, הוסף בדיוק 2-3 שאלות המשך רלוונטיות "
            "שהלקוח עשוי לרצות לשאול, "
            "בפורמט הבא (בשורה נפרדת בסוף התשובה, אחרי ציון המקור):\n"
            "[שאלות_המשך: שאלה ראשונה | שאלה שנייה | שאלה שלישית]\n"
            "חוק ברזל: הצע <b>אך ורק</b> שאלות שהתשובה עליהן מופיעה "
            "במפורש בקטעי המידע שסופקו לך בפנייה זו, "
            "או שאלות שמניעות לפעולות מערכת ידועות "
            "(למשל: \"אפשר לקבוע תור?\", \"לבטל תור\", \"לדבר עם נציג\"). "
            "השאלות צריכות להיות קצרות (עד 5 מילים). "
            "אל תציע שאלות שכבר נענו בשיחה הנוכחית, "
            "ואל תציע על נושאים שאינם מופיעים בקטעי המידע שקיבלת."
        )

    # פרומפט עסקי מותאם אישית — הנחיות מבעל העסק (ללא סניטציה — רק אדמין שולט)
    custom_prompt_section = ""
    if custom_prompt and custom_prompt.strip():
        custom_prompt_section = (
            "\n── הנחיות עסקיות מותאמות אישית ──\n"
            f"{custom_prompt.strip()}\n"
        )

    # שלב 8 — הוראות שימוש ב-facts על הלקוח. נכלל רק כאשר ההזרקה
    # פעילה (MEMORY_INJECTION_ENABLED), אחרת המודל מקבל הוראה
    # שמתייחסת לבלוק שלא יוזרק → מבלבל ובזבוז tokens. נוסף בסוף
    # ה-prompt לאפקט recency גבוה — המודל קורא את ההוראות האחרונות
    # לפני הצריכה של ה-facts עצמם.
    memory_usage_section = ""
    if MEMORY_INJECTION_ENABLED:
        memory_usage_section = """

── שימוש במידע על הלקוח ──
לפני התשובה שלך תקבל בלוק "מה שאתה יודע על הלקוח" עם facts מהשיחות הקודמות. השתמש בהם בטבעיות, לא צריך לציין אותם במפורש (אל תגיד "ראיתי שאת אלרגית לאגוזים" — פשוט תתחשב בזה).

**מידע רגיש:** facts מסומנים "מידע רגיש" — תהיה דיסקרטי. אל תזכיר אותם בקול רם ללא צורך.

**ייתכן שלא רלוונטי:** facts מסומנים "ייתכן שלא רלוונטי" הם מידע ישן (מעל מספר חודשים מאז שאומת). אל תניח שהם עדיין נכונים. אם זה רלוונטי לשיחה — שאל את הלקוח לוודא. למשל אם רשום "בהריון" ועברו חודשים — תוכל לשאול "איך את מרגישה? כשדיברנו לאחרונה היית בהריון, איך זה התקדם?".

**open_issue פתוחים:** אם יש fact מסוג open_issue (החזר, תלונה, וכו'), זה אומר שיש משהו שעוד לא נסגר. תוכל להזכיר את זה אם רלוונטי, או לבדוק אם הוא נפתר."""

    return f"""אתה העוזר הדיגיטלי של {BUSINESS_NAME} — {agent_desc}.
{identity}

── מגדר לשוני ──
דבר תמיד על עצמך בלשון זכר בעברית. לדוגמה: "אני יכול", "אני שמח לעזור", "אשמח לסייע".
לעולם אל תשתמש בלשון נקבה כשאתה מתייחס לעצמך (לא "אני יכולה", לא "אני שמחה").
{tone_section}
── הנחיות לשיחה ──
{conv_guidelines}
{dna_section}{custom_prompt_section}
── עיצוב טקסט (חובה!) ──
{_build_formatting_rules(channel)}

── כללים — יש לעקוב אחריהם בקפידה ──
1. ענה רק על סמך המידע שסופק בהקשר. לעולם אל תמציא מידע.
2. אם ההקשר לא מכיל מספיק מידע כדי לענות, התחל את התשובה בדיוק במחרוזת {HANDOFF_MARKER} (כולל הסוגריים המרובעים, ללא רווחים לפניה, בשורה משלה), ואז שורה ריקה ואז ענה בדיוק את הטקסט: "{FALLBACK_RESPONSE}"
3. תמיד ציין את המקור בסוף התשובה בפורמט: מקור: [שם הקטגוריה או כותרת המסמך]
4. פעל בהתאם להנחיות הטון שלמעלה. היה מועיל ומקיף.
{_build_channel_rules(channel)}
8. הצע פעולות רלוונטיות בהתאם (לדוגמה, "האם תרצו לבקש תור?").
9. ענה באותה שפה שבה הלקוח פונה.{follow_up_rule}

── מגבלות ──
- לעולם אל תצא מהדמות. אם ישאלו אותך "אתה בוט?", ענה: "אני העוזר הדיגיטלי של {BUSINESS_NAME}, אני כאן כדי לוודא שאתה מקבל שירות מעולה! איך אני יכול לעזור?"
- בלי ז'רגון תאגידי. דבר כמו בן אדם, לא כמו ספר הוראות.
- היצמד אך ורק לתחומי העסק על סמך המידע שסופק.
{structure_section}
── ברכה לפי שעה ──
בחר ברכת פתיחה לפי השעה הנוכחית (מתוך מידע שעות הפעילות):
06:00–11:59 → בוקר טוב | 12:00–16:59 → צהריים טובים | 17:00–20:59 → ערב טוב | 21:00–05:59 → לילה טוב.
אם אין צורך בברכה (למשל, שאלת המשך באמצע שיחה) — דלג עליה.{memory_usage_section}"""

# ─── Follow-up Questions (Premium Feature) ──────────────────────────────────
# שאלות המשך חכמות — הצגת 2-3 שאלות המשך רלוונטיות אחרי כל תשובה
# הטקסט עצמו מוזרק כ-rule 11 בתוך build_system_prompt() כשהפיצ'ר פעיל.
FOLLOW_UP_ENABLED = os.getenv("FOLLOW_UP_ENABLED", "false").lower() in ("true", "1", "yes")

# ─── Quality Check (Layer C) ────────────────────────────────────────────────
SOURCE_CITATION_PATTERN = r"([Ss]ource|מקור):\s*.+"
FALLBACK_RESPONSE = (
    "אין לי את המידע הזה כרגע. "
    "תנו לי להעביר את הפנייה לבעל העסק שיוכל לעזור. "
    "בעל העסק יחזור אליכם בקרוב!"
)

# טוקן סמן ש-LLM שם בתחילת תשובה כדי לסמן "אני מבקש להעביר לבעל העסק".
# שימוש בטוקן (במקום fuzzy text matching) הופך את הזיהוי לדטרמיניסטי —
# אין false positives ואין false negatives. הפרסר ב-message_processor
# מזהה את הטוקן, מסיר אותו מהתשובה, ומפעיל את צינור בקשת הנציג.
HANDOFF_MARKER = "[HANDOFF]"

# טוקן סמן ש-LLM שם בתחילת תשובה כדי לסמן "המבקר ב-widget מסר פרטי
# קשר ורוצה שיחזרו אליו". בשונה מ-HANDOFF_MARKER, אחריו מצורפות
# שורות מובנות עם name/phone (ראה widget channel rules ב-_build_channel_rules).
# ערוץ widget הוא היחיד שמותר להוציא את הטוקן הזה.
LEAD_MARKER = "[LEAD]"


def validate_config(*, require_bot: bool = False, require_admin: bool = False) -> list[str]:
    """בדיקת תקינות משתני סביבה קריטיים בהתאם למצב ההרצה.

    מחזיר רשימת שגיאות. רשימה ריקה = הכל תקין.
    """
    errors: list[str] = []
    if require_bot:
        if not TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN לא מוגדר — הבוט לא יוכל להתחבר לטלגרם")
        if WEBHOOK_URL and not WEBHOOK_SECRET:
            errors.append("WEBHOOK_SECRET לא מוגדר — מומלץ להגדיר סוד לאימות בקשות webhook")
    if require_admin:
        if not ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
            errors.append("ADMIN_PASSWORD / ADMIN_PASSWORD_HASH לא מוגדרים — לא ניתן להתחבר לפאנל האדמין")
        if not ADMIN_SECRET_KEY:
            errors.append("ADMIN_SECRET_KEY לא מוגדר — sessions לא מאובטחים")

    # WhatsApp / Twilio — אזהרה אם חלק מה-credentials מוגדרים וחלק לא
    twilio_vars = {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_WHATSAPP_NUMBER": TWILIO_WHATSAPP_NUMBER,
    }
    defined = {k for k, v in twilio_vars.items() if v}
    if defined and defined != set(twilio_vars.keys()):
        missing = set(twilio_vars.keys()) - defined
        errors.append(
            f"WhatsApp credentials חלקיים — חסרים: {', '.join(sorted(missing))}. "
            "יש להגדיר את כולם או לא להגדיר אף אחד."
        )

    return errors
