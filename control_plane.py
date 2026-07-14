"""
Control Plane — רישום ה-tenants של הפלטפורמה (multi-tenant שלב 2).

ראה docs/multi_tenant_migration_spec.md פרקים 4–6. שכבה זו מחזיקה את
המידע ה*תפעולי* על tenants — לא נתוני ריצה של עסקים:

- `tenants` — רישום ה-tenants ומצבם (active/suspended/migrating).
- `tenant_routes` — מיפוי מפתחות ראוטינג נכנסים → tenant (מספר Twilio,
  page_id של Meta, מפתח webhook של טלגרם, מפתח widget).
- `tenant_secrets` — סודות פר-tenant (טוקן בוט, פרטי Twilio...) מוצפנים
  Fernet **fail-closed**: בלי SECRETS_ENCRYPTION_KEY הכתיבה נחסמת.

ה-DB הוא קובץ SQLite נפרד (`DATA_DIR/platform.db`) שאינו שייך לאף
tenant — ולכן יש לו get_platform_connection משלו שאינו עובר דרך
tenancy. **אסור** לגשת אליו דרך database.get_connection.

נתוני העסק עצמם (שיחות, תורים, KB...) נשארים בקובץ ה-SQLite של כל
tenant — זו הפרדת ה-data plane / control plane של מסלול ב' בספק.
"""

import logging
import re
import secrets as _secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from tenancy import (
    DEFAULT_TENANT,
    InvalidTenantSlug,
    TenancyError,
    tenant_context,
    tenant_db_path,
    validate_tenant_id,
)
from utils.crypto import decrypt_field, encrypt_field_strict

logger = logging.getLogger(__name__)

TENANT_STATUSES = ("active", "suspended", "migrating")

# סוגי ראוטים מוכרים — resolve של הודעה נכנסת לפי (route_type, route_key).
# ה-CHECK בסכימה משקף את אותה רשימה; להוסיף סוג = מיגרציה + עדכון כאן.
ROUTE_TYPES = (
    "telegram_webhook_key",
    "twilio_webhook_key",
    "twilio_number",
    "meta_page_id",
    "meta_ig_account",
    "widget_key",
    "public_slug",
)


class TenantExistsError(TenancyError):
    """ניסיון ליצור tenant עם slug תפוס."""


class UnknownTenantError(TenancyError):
    """פעולה על tenant שאינו רשום."""


def platform_db_path() -> Path:
    """נתיב קובץ ה-platform.db — נגזר דינמית מ-config (מכבד patches)."""
    import config as _config

    return Path(_config.DATA_DIR) / "platform.db"


@contextmanager
def get_platform_connection():
    """חיבור ל-platform.db — **לא** עובר דרך tenancy (הקובץ גלובלי).

    אותם pragmas כמו get_connection של ה-data plane (WAL, busy_timeout,
    foreign_keys) — הקובץ משותף ל-threads של Flask/schedulers.
    """
    conn = sqlite3.connect(str(platform_db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_platform_db() -> None:
    """יצירת סכימת ה-control plane (idempotent — CREATE IF NOT EXISTS)."""
    statuses = ", ".join(f"'{s}'" for s in TENANT_STATUSES)
    route_types = ", ".join(f"'{t}'" for t in ROUTE_TYPES)
    with get_platform_connection() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id    TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active'
                             CHECK(status IN ({statuses})),
                plan         TEXT NOT NULL DEFAULT 'premium',
                notes        TEXT NOT NULL DEFAULT '',
                created_at   TEXT DEFAULT (datetime('now')),
                updated_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tenant_routes (
                route_type TEXT NOT NULL CHECK(route_type IN ({route_types})),
                route_key  TEXT NOT NULL,
                tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id)
                           ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (route_type, route_key)
            );
            CREATE INDEX IF NOT EXISTS idx_tenant_routes_tenant
                ON tenant_routes(tenant_id);

            CREATE TABLE IF NOT EXISTS tenant_secrets (
                tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id)
                           ON DELETE CASCADE,
                name       TEXT NOT NULL,
                value_enc  TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (tenant_id, name)
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                email         TEXT PRIMARY KEY,   -- מנורמל ל-lowercase
                password_hash TEXT NOT NULL,      -- werkzeug, לעולם לא מוחזר החוצה
                display_name  TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL CHECK(role IN ('owner','platform_admin')),
                tenant_id     TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active','disabled')),
                created_at    TEXT DEFAULT (datetime('now')),
                last_login_at TEXT,
                -- owner חייב עסק; platform_admin חוצה-עסקים (tenant_id ריק)
                CHECK((role = 'owner' AND tenant_id IS NOT NULL)
                      OR (role = 'platform_admin' AND tenant_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_admin_users_tenant
                ON admin_users(tenant_id);

            CREATE TABLE IF NOT EXISTS platform_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)


def get_platform_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    """קריאת ערך מטא תפעולי של הפלטפורמה (למשל last-run של job)."""
    if not platform_db_path().exists():
        return default
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT value FROM platform_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_platform_meta(key: str, value: str) -> None:
    """שמירת ערך מטא תפעולי (upsert)."""
    init_platform_db()
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT INTO platform_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, value),
        )


# ─── מחזור חיים של tenant ────────────────────────────────────────────────


def create_tenant(
    tenant_id: str,
    display_name: str,
    plan: str = "premium",
    seed_hours: bool = True,
) -> None:
    """יצירת tenant חדש: רישום + תיקייה + סכימת DB + זריעת שעות פעילות.

    ה-slug 'default' שמור ל-tenant ה-legacy (הקבצים הקיימים) ואינו נרשם
    דרך הפונקציה הזו.
    """
    validate_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT:
        raise InvalidTenantSlug(
            f"'{DEFAULT_TENANT}' שמור ל-tenant ה-legacy ואינו נוצר דרך ה-control plane"
        )

    init_platform_db()
    with get_platform_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if existing:
            raise TenantExistsError(f"tenant כבר קיים: {tenant_id}")
        conn.execute(
            "INSERT INTO tenants (tenant_id, display_name, plan) VALUES (?, ?, ?)",
            (tenant_id, display_name, plan),
        )

    # יצירת ה-data plane של ה-tenant: תיקייה + סכימה מלאה (init_db רץ את
    # אותו executescript + migrations כמו בכל עליית תהליך — לכל קובץ בנפרד)
    db_file = tenant_db_path(tenant_id)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with tenant_context(tenant_id):
        from ai_chatbot import database as db

        db.init_db()
        # ה-plan הוא source-of-truth ב-subscription singleton שבתוך ה-DB
        # של ה-tenant (feature_flags וה-channel-lock קוראים משם). מסנכרנים
        # אליו את ה-plan שנרשם ב-control plane, אחרת feature_flags יראה את
        # ברירת המחדל של ה-migration ולא את מה שביקשנו.
        try:
            import feature_flags as _ff

            _ff.set_plan(plan)
        except Exception:
            logger.error(
                "create_tenant(%s): setting tenant plan failed", tenant_id,
                exc_info=True,
            )
        # שם העסק אינו נזרע ל-tenant DB — get_business_config גוזר אותו
        # ישירות מ-display_name של ה-control plane (מקור אמת יחיד), כך
        # שאין עותק שקופא ולא מסתנכרן אם השם משתנה.
        if seed_hours:
            # זריעת שעות פעילות + חגים — אידמפוטנטית (הדפוס של main.py)
            try:
                from seed_data import _seed_business_hours

                _seed_business_hours()
            except Exception:
                logger.error(
                    "create_tenant(%s): seeding business hours failed", tenant_id,
                    exc_info=True,
                )

    logger.info("tenant created: %s (%s)", tenant_id, display_name)


def get_tenant(tenant_id: str) -> Optional[dict]:
    """שליפת רשומת tenant, או None אם אינו רשום (או שאין platform.db)."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tenants(status: Optional[str] = None) -> list[dict]:
    """כל ה-tenants הרשומים, אופציונלית מסונן לפי סטטוס."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tenants WHERE status = ? ORDER BY tenant_id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tenants ORDER BY tenant_id"
            ).fetchall()
        return [dict(r) for r in rows]


def set_tenant_status(tenant_id: str, status: str) -> None:
    """עדכון סטטוס (active/suspended/migrating) + אינבלידציה של ה-cache."""
    if status not in TENANT_STATUSES:
        raise ValueError(f"סטטוס לא מוכר: {status!r} (מותר: {TENANT_STATUSES})")
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE tenants SET status = ?, updated_at = datetime('now') "
            "WHERE tenant_id = ?",
            (status, tenant_id),
        )
        if cur.rowcount == 0:
            raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    invalidate_status_cache(tenant_id)
    logger.info("tenant %s → status=%s", tenant_id, status)


def list_schedulable_tenant_ids() -> list[str]:
    """ה-tenants שה-jobs המתוזמנים רצים עליהם.

    כשאין רישום (אין platform.db או שאין בו tenants) — התנהגות שלב 1:
    ה-tenant של ברירת המחדל בלבד. ברגע שנרשמו tenants, הרישום הוא מקור
    האמת ורק active נכללים ('default' ה-legacy איננו רשום ולכן יוצא
    מהמחזור — בפלטפורמה אין לו נתונים חיים).
    """
    registered = list_tenants()
    if not registered:
        return [DEFAULT_TENANT]
    return [t["tenant_id"] for t in registered if t["status"] == "active"]


# ─── cache סטטוסים (נצרך ע"י tenancy בכל פתיחת חיבור) ────────────────────

_STATUS_CACHE_TTL = 30.0
_status_cache: dict[str, tuple[float, Optional[str]]] = {}
_status_cache_lock = threading.Lock()


def get_tenant_status_cached(tenant_id: str) -> Optional[str]:
    """סטטוס ה-tenant עם cache קצר (30ש'). None = לא רשום/אין platform.db."""
    import time

    now = time.monotonic()
    with _status_cache_lock:
        hit = _status_cache.get(tenant_id)
        if hit and now - hit[0] < _STATUS_CACHE_TTL:
            return hit[1]
    row = get_tenant(tenant_id)
    status = row["status"] if row else None
    with _status_cache_lock:
        _status_cache[tenant_id] = (now, status)
    return status


def invalidate_status_cache(tenant_id: Optional[str] = None) -> None:
    """אינבלידציה — אחרי שינוי סטטוס (או הכל, בטסטים)."""
    with _status_cache_lock:
        if tenant_id is None:
            _status_cache.clear()
        else:
            _status_cache.pop(tenant_id, None)


# ─── ראוטים (מיפוי מפתח נכנס → tenant) ───────────────────────────────────


def set_route(route_type: str, route_key: str, tenant_id: str) -> None:
    """רישום/עדכון ראוט. INSERT OR REPLACE — מפתח הוא natural key וניתן
    להצביע מחדש (למשל העברת מספר Twilio בין tenants)."""
    if route_type not in ROUTE_TYPES:
        raise ValueError(f"route_type לא מוכר: {route_type!r} (מותר: {ROUTE_TYPES})")
    route_key = (route_key or "").strip()
    if not route_key:
        raise ValueError("route_key ריק")
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tenant_routes (route_type, route_key, tenant_id) "
            "VALUES (?, ?, ?)",
            (route_type, route_key, tenant_id),
        )


def resolve_route(route_type: str, route_key: str) -> Optional[str]:
    """tenant_id של מפתח נכנס, או None אם אינו רשום."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM tenant_routes "
            "WHERE route_type = ? AND route_key = ?",
            (route_type, (route_key or "").strip()),
        ).fetchone()
        return row["tenant_id"] if row else None


def delete_route(route_type: str, route_key: str) -> bool:
    """הסרת ראוט. מחזיר True אם נמחק בפועל."""
    with get_platform_connection() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_routes WHERE route_type = ? AND route_key = ?",
            (route_type, route_key),
        )
        return cur.rowcount > 0


def list_routes(tenant_id: Optional[str] = None) -> list[dict]:
    """הראוטים הרשומים (אופציונלית של tenant מסוים)."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM tenant_routes WHERE tenant_id = ? "
                "ORDER BY route_type, route_key",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tenant_routes ORDER BY tenant_id, route_type"
            ).fetchall()
        return [dict(r) for r in rows]


def get_tenant_route_key(tenant_id: str, route_type: str) -> Optional[str]:
    """ה-route_key הרשום ל-tenant עבור סוג נתון (lookup הפוך ל-resolve).

    משמש לבניית URLs יוצאים (למשל status callback של Twilio שצריך את
    מפתח ה-webhook של ה-tenant). אם רשומים כמה — מחזיר את הראשון.
    """
    if route_type not in ROUTE_TYPES:
        raise ValueError(f"route_type לא מוכר: {route_type!r}")
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT route_key FROM tenant_routes "
            "WHERE tenant_id = ? AND route_type = ? ORDER BY created_at LIMIT 1",
            (tenant_id, route_type),
        ).fetchone()
        return row["route_key"] if row else None


def generate_route_key() -> str:
    """מפתח ראוטינג אקראי בלתי-ניתן-לניחוש (ל-webhook / widget)."""
    return _secrets.token_urlsafe(24)


# ─── סודות פר-tenant (מוצפנים, fail-closed) ──────────────────────────────

# שמות הסודות המוכרים — לתיעוד ולולידציה רכה בלבד (אזהרה, לא חסימה,
# כדי לא לחסום סוד חדש שנוסף בקוד לפני שהרשימה עודכנה).
KNOWN_SECRET_NAMES = (
    "telegram_bot_token",
    "telegram_webhook_secret",
    "telegram_owner_chat_id",
    # שם המשתמש של הבוט (t.me/<username>) — נלכד אוטומטית ב-getMe בעת
    # חיבור הטוקן; לא סוד אמיתי אבל נשמר באותו מנגנון יחד עם שאר נתוני הערוץ
    "telegram_bot_username",
    "twilio_account_sid",
    "twilio_auth_token",
    "twilio_whatsapp_number",
    "owner_whatsapp_number",
)

# נתוני הערוץ פר-tenant — secrets + routes שנמחקים יחד במעבר ערוץ.
# בשימוש delete_tenant_channel_data; להרחיב כשנוסף נתון ערוץ חדש.
_CHANNEL_SECRET_NAMES = {
    "telegram": (
        "telegram_bot_token",
        "telegram_webhook_secret",
        "telegram_owner_chat_id",
        "telegram_bot_username",
    ),
    "whatsapp": (
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_whatsapp_number",
        "owner_whatsapp_number",
    ),
}
_CHANNEL_ROUTE_TYPES = {
    "telegram": ("telegram_webhook_key",),
    "whatsapp": ("twilio_webhook_key", "twilio_number"),
}

_SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def set_tenant_secret(tenant_id: str, name: str, value: str) -> None:
    """שמירת סוד מוצפן. fail-closed: בלי SECRETS_ENCRYPTION_KEY — חריגה.

    ערך ריק מוחק את הסוד (אין טעם לשמור שורות ריקות).
    """
    if not _SECRET_NAME_RE.match(name or ""):
        raise ValueError(f"שם סוד לא חוקי: {name!r} (a-z0-9_ עד 64 תווים)")
    if name not in KNOWN_SECRET_NAMES:
        logger.warning("set_tenant_secret: שם סוד לא ברשימה המוכרת: %s", name)
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")

    if not value:
        with get_platform_connection() as conn:
            conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id = ? AND name = ?",
                (tenant_id, name),
            )
        return

    value_enc = encrypt_field_strict(value)
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT INTO tenant_secrets (tenant_id, name, value_enc) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(tenant_id, name) DO UPDATE SET "
            "value_enc = excluded.value_enc, updated_at = datetime('now')",
            (tenant_id, name, value_enc),
        )


def get_tenant_secret(tenant_id: str, name: str) -> Optional[str]:
    """שליפת סוד מפוענח, או None אם אינו קיים."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT value_enc FROM tenant_secrets WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        ).fetchone()
    if not row:
        return None
    return decrypt_field(row["value_enc"])


def list_tenant_secret_names(tenant_id: str) -> list[str]:
    """שמות הסודות הקיימים ל-tenant — בלי הערכים (לתצוגת סטטוס בלבד)."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM tenant_secrets WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        return [r["name"] for r in rows]


def delete_tenant_channel_data(tenant_id: str, channel: str) -> None:
    """מחיקת נתוני ערוץ של tenant — secrets + routes (מעבר ערוץ).

    נקראת כשה-tenant מתחבר לערוץ החדש: הנתונים של הערוץ הקודם נמחקים
    (החלטת מוצר — לא משאירים credentials רדומים). no-op כשאין נתונים.
    שכבתיות: כאן נתונים בלבד; ביטול webhook מול טלגרם (רשת) באחריות
    הקורא (admin) לפני המחיקה, כי control_plane לא תלוי ב-bot_registry.
    """
    if channel not in _CHANNEL_SECRET_NAMES:
        raise ValueError(f"ערוץ לא מוכר: {channel!r}")
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    for name in _CHANNEL_SECRET_NAMES[channel]:
        # ערך ריק = מחיקת הסוד (התנהגות set_tenant_secret הקיימת)
        set_tenant_secret(tenant_id, name, "")
    for route_type in _CHANNEL_ROUTE_TYPES[channel]:
        key = get_tenant_route_key(tenant_id, route_type)
        if key:
            delete_route(route_type, key)
    logger.info(
        "delete_tenant_channel_data: cleared %s data (tenant=%s)", channel, tenant_id
    )


def get_tenant_channel_identity(tenant_id: str) -> dict:
    """זהות הערוץ הציבורית של tenant — לקישורי QR / widget footer.

    מחזיר {"telegram_bot_username": str, "whatsapp_number": str} (ריק כשאין).
    ל-tenant של ברירת המחדל (legacy) — מ-env; לכל tenant אחר — מהסודות.
    אלה נתוני תצוגה ציבוריים (t.me / wa.me), לא credentials.
    """
    from tenancy import DEFAULT_TENANT

    if tenant_id == DEFAULT_TENANT:
        import config as _config

        return {
            "telegram_bot_username": getattr(_config, "TELEGRAM_BOT_USERNAME", "") or "",
            "whatsapp_number": getattr(_config, "TWILIO_WHATSAPP_NUMBER", "") or "",
        }
    return {
        "telegram_bot_username": get_tenant_secret(tenant_id, "telegram_bot_username") or "",
        "whatsapp_number": get_tenant_secret(tenant_id, "twilio_whatsapp_number") or "",
    }


# ─── משתמשי אדמין (בעלי עסקים + platform admins) ─────────────────────────

# hash דמה — מורץ גם כשה-email לא קיים, כדי שזמן התגובה לא יסגיר האם
# החשבון קיים (timing oracle). נוצר פעם אחת בזמן import.
_DUMMY_PASSWORD_HASH: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        from werkzeug.security import generate_password_hash

        _DUMMY_PASSWORD_HASH = generate_password_hash(_secrets.token_urlsafe(16))
    return _DUMMY_PASSWORD_HASH


def _normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("כתובת אימייל לא תקינה")
    return email


def create_admin_user(
    email: str,
    password: str,
    role: str = "owner",
    tenant_id: Optional[str] = None,
    display_name: str = "",
) -> None:
    """יצירת משתמש אדמין. ‏owner חייב tenant רשום; ‏platform_admin — בלי.

    נוצר אך ורק ע"י מפעיל הפלטפורמה (CLI) — אין self-registration, ולכן
    אין כאן וקטור auto-admin לפי email לא מאומת (דפוס קריטי #3).
    """
    from werkzeug.security import generate_password_hash

    email = _normalize_email(email)
    if role not in ("owner", "platform_admin"):
        raise ValueError(f"role לא מוכר: {role!r}")
    if not password or len(password) < 8:
        raise ValueError("סיסמה קצרה מדי (מינימום 8 תווים)")
    if role == "owner":
        if not tenant_id or get_tenant(tenant_id) is None:
            raise UnknownTenantError(f"owner דורש tenant רשום (קיבלנו: {tenant_id!r})")
    else:
        tenant_id = None

    init_platform_db()
    with get_platform_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM admin_users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            raise ValueError("משתמש עם האימייל הזה כבר קיים")
        conn.execute(
            "INSERT INTO admin_users (email, password_hash, display_name, role, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, generate_password_hash(password), display_name, role, tenant_id),
        )
    logger.info("admin user created: role=%s tenant=%s", role, tenant_id or "-")


def verify_admin_login(email: str, password: str) -> Optional[dict]:
    """אימות התחברות. מחזיר את רשומת המשתמש (בלי ה-hash) או None.

    תמיד מריץ בדיקת סיסמה (גם כשהמשתמש לא קיים / מושבת) — בלי timing
    oracle על קיום החשבון. None אחיד לכל סיבה — הקורא מציג הודעה גנרית
    (דפוס קריטי #10: אין להבחין כלפי חוץ בין 'לא קיים' ל'סיסמה שגויה').
    """
    from werkzeug.security import check_password_hash

    try:
        email = _normalize_email(email)
    except ValueError:
        check_password_hash(_dummy_hash(), password or "")
        return None

    row = None
    if platform_db_path().exists():
        with get_platform_connection() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE email = ?", (email,)
            ).fetchone()

    if row is None:
        check_password_hash(_dummy_hash(), password or "")
        return None

    ok = check_password_hash(row["password_hash"], password or "")
    if not ok or row["status"] != "active":
        return None

    with get_platform_connection() as conn:
        conn.execute(
            "UPDATE admin_users SET last_login_at = datetime('now') WHERE email = ?",
            (email,),
        )
    user = dict(row)
    user.pop("password_hash", None)  # לעולם לא מחזירים hash החוצה (דפוס #6)
    return user


def list_admin_users(tenant_id: Optional[str] = None) -> list[dict]:
    """רשימת משתמשי האדמין — ללא ה-hash."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT email, display_name, role, tenant_id, status, created_at, "
                "last_login_at FROM admin_users WHERE tenant_id = ? ORDER BY email",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT email, display_name, role, tenant_id, status, created_at, "
                "last_login_at FROM admin_users ORDER BY email"
            ).fetchall()
        return [dict(r) for r in rows]


def get_tenant_owner(tenant_id: str) -> Optional[dict]:
    """משתמש ה-owner של tenant — לתצוגה ולשינוי סיסמה במצב "פעל-כ".

    מחזיר dict בלי password_hash (דפוס #6), או None כשאין owner.
    אם רשומים כמה owners — מחזיר את הוותיק (הראשון שנוצר).
    """
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT email, display_name, status FROM admin_users "
            "WHERE tenant_id = ? AND role = 'owner' "
            "ORDER BY created_at, email LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None


def set_admin_user_status(email: str, status: str) -> None:
    """הפעלה/השבתה של משתמש אדמין."""
    if status not in ("active", "disabled"):
        raise ValueError(f"סטטוס לא מוכר: {status!r}")
    email = _normalize_email(email)
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET status = ? WHERE email = ?", (status, email),
        )
        if cur.rowcount == 0:
            raise ValueError("משתמש לא קיים")


def set_admin_password(email: str, new_password: str) -> None:
    """שינוי סיסמת משתמש אדמין (בעל עסק משנה את הסיסמה שלו מהפאנל)."""
    from werkzeug.security import generate_password_hash

    if not new_password or len(new_password) < 8:
        raise ValueError("סיסמה קצרה מדי (מינימום 8 תווים)")
    email = _normalize_email(email)
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET password_hash = ? WHERE email = ?",
            (generate_password_hash(new_password), email),
        )
        if cur.rowcount == 0:
            raise ValueError("משתמש לא קיים")
