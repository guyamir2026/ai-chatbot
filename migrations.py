"""
מיגרציות קלות ל-SQLite — נקראות מ-init_db() בכל הפעלה.

SQLite תומך רק ב-ADD COLUMN, כך שמיגרציות מורכבות יותר (כמו שינוי
UNIQUE constraint או מעבר סכימה) דורשות CREATE TABLE + INSERT + DROP.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


def _ensure_column(conn, table: str, column: str, ddl_suffix: str) -> None:
    """הוספת עמודה אם לא קיימת (SQLite ADD COLUMN בלבד)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(r["name"] == column for r in cols):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")


def run_migrations(conn) -> None:
    """הפעלת כל המיגרציות הקלות — נקראת מתוך init_db() עם חיבור פתוח."""

    # ─── ADD COLUMN מיגרציות ───────────────────────────────────────────────
    _ensure_column(conn, "agent_requests", "telegram_username", "TEXT DEFAULT ''")
    _ensure_column(conn, "appointments", "telegram_username", "TEXT DEFAULT ''")
    _ensure_column(
        conn,
        "conversation_summaries",
        "last_summarized_message_id",
        "INTEGER NOT NULL DEFAULT 0",
    )

    # ─── Back-fill last_summarized_message_id ─────────────────────────────
    # שורות ישנות שמיגרו מהסכימה הקודמת (COUNT-based offset) — מחשבים את
    # ה-high-water mark מהיסטוריית השיחות.
    rows = conn.execute(
        "SELECT id, user_id, message_count FROM conversation_summaries "
        "WHERE last_summarized_message_id = 0 AND message_count > 0"
    ).fetchall()
    for row in rows:
        last_msg = conn.execute(
            "SELECT id FROM conversations WHERE user_id = ? "
            "ORDER BY id ASC LIMIT 1 OFFSET ?",
            (row["user_id"], row["message_count"] - 1),
        ).fetchone()
        if last_msg:
            conn.execute(
                "UPDATE conversation_summaries SET last_summarized_message_id = ? WHERE id = ?",
                (last_msg["id"], row["id"]),
            )

    # ─── special_days: כפילויות + UNIQUE index ────────────────────────────
    existing_indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='special_days' AND name='idx_special_days_date_unique'"
    ).fetchone()
    if not existing_indexes:
        # מחיקת כפילויות — שומר רק את הרשומה האחרונה לכל תאריך
        dup_cursor = conn.execute("""
            SELECT COUNT(*) AS cnt FROM special_days WHERE id NOT IN (
                SELECT MAX(id) FROM special_days GROUP BY date
            )
        """)
        dup_count = dup_cursor.fetchone()["cnt"]
        if dup_count:
            logger.warning("Removing %d duplicate special_days entries during migration", dup_count)
        conn.execute("""
            DELETE FROM special_days WHERE id NOT IN (
                SELECT MAX(id) FROM special_days GROUP BY date
            )
        """)
        conn.execute("DROP INDEX IF EXISTS idx_special_days_date")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_special_days_date_unique ON special_days(date)"
        )

    # ─── referrals: מודל הפניה-בודדת → ריבוי-הפניות ──────────────────────
    referral_cols = {
        c["name"]: c
        for c in conn.execute("PRAGMA table_info(referrals)").fetchall()
    }
    referred_id_col = referral_cols.get("referred_id")
    if referred_id_col and not referred_id_col["notnull"]:
        # סכימה ישנה — referred_id nullable → צריך מיגרציה
        conn.execute("""
            INSERT OR IGNORE INTO referral_codes (user_id, code, created_at)
            SELECT referrer_id, code, MIN(created_at)
            FROM referrals GROUP BY referrer_id
        """)
        conn.execute("ALTER TABLE referrals RENAME TO _referrals_old")
        conn.execute("""
            CREATE TABLE referrals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     TEXT NOT NULL,
                referred_id     TEXT NOT NULL UNIQUE,
                code            TEXT NOT NULL,
                status          TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
                created_at      TEXT DEFAULT (datetime('now')),
                completed_at    TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO referrals
                (referrer_id, referred_id, code, status, created_at, completed_at)
            SELECT referrer_id, referred_id, code, status, created_at, completed_at
            FROM _referrals_old WHERE referred_id IS NOT NULL
        """)
        conn.execute("DROP TABLE _referrals_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(code)")
        logger.info("Migrated referrals table to multi-referral schema")

    _ensure_column(conn, "referral_codes", "sent", "INTEGER DEFAULT 0")

    # ─── bot_settings: עמודות תזכורת תורים ────────────────────────────────
    _ensure_column(conn, "bot_settings", "reminder_enabled", "INTEGER DEFAULT 1")
    _ensure_column(conn, "bot_settings", "reminder_time", "TEXT DEFAULT '10:00'")
    _ensure_column(conn, "bot_settings", "custom_prompt", "TEXT DEFAULT ''")

    # ─── bot_settings: הוספת טון 'none' ל-CHECK constraint ───────────────
    bs_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bot_settings'"
    ).fetchone()
    if bs_sql and "'none'" not in (bs_sql["sql"] or ""):
        row = conn.execute("SELECT * FROM bot_settings WHERE id = 1").fetchone()
        conn.execute("ALTER TABLE bot_settings RENAME TO _bot_settings_old")
        conn.execute("""
            CREATE TABLE bot_settings (
                id               INTEGER PRIMARY KEY CHECK(id = 1),
                tone             TEXT NOT NULL DEFAULT 'friendly'
                                     CHECK(tone IN ('none', 'friendly', 'formal', 'sales', 'luxury')),
                custom_phrases   TEXT DEFAULT '',
                custom_prompt    TEXT DEFAULT '',
                reminder_enabled INTEGER DEFAULT 1,
                reminder_time    TEXT DEFAULT '10:00',
                updated_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        if row:
            conn.execute(
                """INSERT INTO bot_settings
                       (id, tone, custom_phrases, custom_prompt, reminder_enabled, reminder_time, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (1, dict(row)["tone"], dict(row).get("custom_phrases", ""),
                 dict(row).get("custom_prompt", ""),
                 dict(row).get("reminder_enabled", 1),
                 dict(row).get("reminder_time", "10:00"),
                 dict(row).get("updated_at", "")),
            )
        else:
            conn.execute("INSERT INTO bot_settings (id) VALUES (1)")
        conn.execute("DROP TABLE _bot_settings_old")
        logger.info("Migrated bot_settings: added 'none' tone and custom_prompt column")

    # ─── bot_settings: הגדרות מערכת הפניות ──────────────────────────────
    _ensure_column(conn, "bot_settings", "referral_enabled", "INTEGER DEFAULT 0")
    _ensure_column(conn, "bot_settings", "referral_discount", "REAL DEFAULT 10.0")
    _ensure_column(conn, "bot_settings", "referral_validity_days", "INTEGER DEFAULT 60")

    # ─── bot_settings: הפעלה/כיבוי קביעת תורים פר-עסק ────────────────────
    # ברירת מחדל 1 = פעיל (תאימות לאחור). כבוי = העסק לא מתאם תורים
    # אונליין; בקשות תור/פגישה מופנות לנציג, והכפתור/ה-flow מוסתרים.
    _ensure_column(conn, "bot_settings", "booking_enabled", "INTEGER DEFAULT 1")

    # ─── bot_settings: פרומפט מערכת מלא (override) ─────────────────────────
    _ensure_column(conn, "bot_settings", "full_system_prompt", "TEXT DEFAULT ''")

    # ─── bot_settings: כרטיס ביקור פר-tenant (טלפון/כתובת/אתר) ────────────
    # ריק = fallback ל-env (legacy). נצרך דרך config.get_business_config().
    # שם העסק אינו כאן — מקורו display_name ב-control plane (הקמת ה-tenant).
    _ensure_column(conn, "bot_settings", "business_phone", "TEXT DEFAULT ''")
    _ensure_column(conn, "bot_settings", "business_address", "TEXT DEFAULT ''")
    _ensure_column(conn, "bot_settings", "business_website", "TEXT DEFAULT ''")

    # ─── special_days: סימון מחיקה ע"י משתמש (soft delete) כדי שהסידור לא יחזיר חגים שנמחקו
    _ensure_column(conn, "special_days", "user_removed", "INTEGER DEFAULT 0")

    # ─── appointments: סימון תזכורת שנשלחה ────────────────────────────────
    _ensure_column(conn, "appointments", "reminder_sent", "INTEGER DEFAULT 0")

    # ─── תזכורת שנייה (שעתיים לפני התור) ─────────────────────────────────
    _ensure_column(conn, "bot_settings", "second_reminder_enabled", "INTEGER DEFAULT 0")
    _ensure_column(conn, "bot_settings", "second_reminder_hours", "REAL DEFAULT 2.0")
    _ensure_column(conn, "appointments", "second_reminder_sent", "INTEGER DEFAULT 0")

    # ─── live_chats: עמודת updated_at למעקב אחר פעילות אחרונה ─────────────
    _ensure_column(conn, "live_chats", "updated_at", "TEXT DEFAULT ''")
    # Back-fill: שורות קיימות מקבלות את started_at כ-updated_at
    conn.execute(
        "UPDATE live_chats SET updated_at = started_at WHERE updated_at IS NULL OR updated_at = ''"
    )

    # ─── referrals: UNIQUE(referrer_id, referred_id) → UNIQUE(referred_id)
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='referrals'"
    ).fetchone()
    if create_sql and "UNIQUE(referrer_id, referred_id)" in (create_sql["sql"] or ""):
        conn.execute("ALTER TABLE referrals RENAME TO _referrals_old2")
        conn.execute("""
            CREATE TABLE referrals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     TEXT NOT NULL,
                referred_id     TEXT NOT NULL UNIQUE,
                code            TEXT NOT NULL,
                status          TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
                created_at      TEXT DEFAULT (datetime('now')),
                completed_at    TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO referrals
                (referrer_id, referred_id, code, status, created_at, completed_at)
            SELECT referrer_id, referred_id, code, status, created_at, completed_at
            FROM _referrals_old2
        """)
        conn.execute("DROP TABLE _referrals_old2")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(code)")
        logger.info("Migrated referrals: UNIQUE(referrer_id, referred_id) → UNIQUE(referred_id)")

    # ─── appointments: הוספת סטטוס 'passed' ל-CHECK constraint ─────────────
    appt_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='appointments'"
    ).fetchone()
    if appt_sql and "'passed'" not in (appt_sql["sql"] or ""):
        conn.execute("ALTER TABLE appointments RENAME TO _appointments_old")
        conn.execute("""
            CREATE TABLE appointments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                telegram_username TEXT DEFAULT '',
                service     TEXT DEFAULT '',
                preferred_date TEXT DEFAULT '',
                preferred_time TEXT DEFAULT '',
                notes       TEXT DEFAULT '',
                status      TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'cancelled', 'passed')),
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO appointments
                (id, user_id, username, telegram_username, service,
                 preferred_date, preferred_time, notes, status, created_at)
            SELECT id, user_id, username, telegram_username, service,
                   preferred_date, preferred_time, notes, status, created_at
            FROM _appointments_old
        """)
        conn.execute("DROP TABLE _appointments_old")
        logger.info("Migrated appointments: added 'passed' to CHECK constraint")

    # ─── appointments: UNIQUE partial index למניעת תורים כפולים ────────────
    existing_appt_idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='appointments' AND name='idx_appointments_user_datetime'"
    ).fetchone()
    if not existing_appt_idx:
        # מחיקת כפילויות (שומר רק את הרשומה האחרונה לכל user+date+time)
        dup_cursor = conn.execute("""
            SELECT COUNT(*) AS cnt FROM appointments
            WHERE preferred_date != '' AND preferred_time != ''
              AND id NOT IN (
                  SELECT MAX(id) FROM appointments
                  WHERE preferred_date != '' AND preferred_time != ''
                  GROUP BY user_id, preferred_date, preferred_time
              )
        """)
        dup_count = dup_cursor.fetchone()["cnt"]
        if dup_count:
            logger.warning("Removing %d duplicate appointments during migration", dup_count)
            conn.execute("""
                DELETE FROM appointments
                WHERE preferred_date != '' AND preferred_time != ''
                  AND id NOT IN (
                      SELECT MAX(id) FROM appointments
                      WHERE preferred_date != '' AND preferred_time != ''
                      GROUP BY user_id, preferred_date, preferred_time
                  )
            """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_user_datetime
                ON appointments(user_id, preferred_date, preferred_time)
                WHERE preferred_date != '' AND preferred_time != ''
        """)

    # ─── appointments: עמודת google_event_id לסנכרון עם Google Calendar ────
    _ensure_column(conn, "appointments", "google_event_id", "TEXT DEFAULT ''")

    # ─── google_calendar_credentials: יצירת טבלה אם לא קיימת (DB ישנים) ────
    existing_gcal = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='google_calendar_credentials'"
    ).fetchone()
    if not existing_gcal:
        conn.execute("""
            CREATE TABLE google_calendar_credentials (
                id                      INTEGER PRIMARY KEY CHECK(id = 1),
                google_account_email    TEXT DEFAULT '',
                calendar_id             TEXT DEFAULT 'primary',
                refresh_token           TEXT DEFAULT '',
                access_token            TEXT DEFAULT '',
                token_expiry            TEXT DEFAULT '',
                timezone                TEXT DEFAULT 'Asia/Jerusalem',
                updated_at              TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT OR IGNORE INTO google_calendar_credentials (id) VALUES (1)")
        logger.info("Created google_calendar_credentials table via migration")

    # ─── services: טבלת שירותים עם משך תור ──────────────────────────────
    existing_services = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='services'"
    ).fetchone()
    if not existing_services:
        conn.execute("""
            CREATE TABLE services (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL UNIQUE,
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                is_active        INTEGER DEFAULT 1,
                created_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        logger.info("Created services table via migration")

    # ─── appointments: נרמול תאריכים ישנים בפורמט עברי → YYYY-MM-DD ──────
    # תורים ישנים שנשמרו עם "מחר" / "יום שני" / "15/3" לפני שנוסף normalize_date.
    # נרמול לפי created_at כ-reference date (היום שבו המשתמש הזמין).
    # UPDATE OR IGNORE — אם הנרמול יוצר כפילות (אותו user+date+time),
    # שומרים על הרשומה הקיימת ומוחקים את הישנה כדי לא לקרוס.
    non_iso_rows = conn.execute(
        "SELECT id, preferred_date, created_at FROM appointments "
        "WHERE preferred_date != '' AND preferred_date NOT LIKE '____-__-__'"
    ).fetchall()
    if non_iso_rows:
        from entity_extraction import normalize_date  # noqa: E402 — lazy import
        migrated = 0
        for row in non_iso_rows:
            ref = None
            try:
                ref = date.fromisoformat(row["created_at"][:10])
            except (ValueError, TypeError):
                pass
            normalized = normalize_date(row["preferred_date"], ref_date=ref)
            if normalized:
                result = conn.execute(
                    "UPDATE OR IGNORE appointments SET preferred_date = ? WHERE id = ?",
                    (normalized, row["id"]),
                )
                if result.rowcount:
                    migrated += 1
                else:
                    # כפילות UNIQUE — מוחקים את הרשומה הישנה (כבר קיימת רשומה מנורמלת)
                    conn.execute("DELETE FROM appointments WHERE id = ?", (row["id"],))
                    logger.info("Deleted duplicate appointment id=%s after normalization", row["id"])
            else:
                logger.warning(
                    "Could not normalize appointment date id=%s: %r",
                    row["id"], row["preferred_date"],
                )
        if migrated:
            logger.info("Normalized %d/%d old appointment dates to YYYY-MM-DD", migrated, len(non_iso_rows))

    # ─── channel: עמודת ערוץ הודעות (telegram/whatsapp) ──────────────────────
    _ensure_column(conn, "conversations", "channel", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "live_chats", "channel", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "user_subscriptions", "channel", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "agent_requests", "channel", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "appointments", "channel", "TEXT NOT NULL DEFAULT 'telegram'")

    # ─── consecutive_fallbacks: מונה fallbacks לשימוש ב-WhatsApp (stateless) ──
    _ensure_column(conn, "user_subscriptions", "consecutive_fallbacks", "INTEGER NOT NULL DEFAULT 0")

    # ─── unanswered_questions: הוספת סטטוס 'not_relevant' ל-CHECK constraint ─
    uq_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='unanswered_questions'"
    ).fetchone()
    if uq_sql and "'not_relevant'" not in (uq_sql["sql"] or ""):
        conn.execute("ALTER TABLE unanswered_questions RENAME TO _unanswered_questions_old")
        conn.execute("""
            CREATE TABLE unanswered_questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                question    TEXT NOT NULL,
                status      TEXT DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'not_relevant')),
                created_at  TEXT DEFAULT (datetime('now')),
                resolved_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO unanswered_questions
                (id, user_id, username, question, status, created_at, resolved_at)
            SELECT id, user_id, username, question, status, created_at, resolved_at
            FROM _unanswered_questions_old
        """)
        conn.execute("DROP TABLE _unanswered_questions_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_unanswered_questions_status "
            "ON unanswered_questions(status)"
        )
        logger.info("Migrated unanswered_questions: added 'not_relevant' to CHECK constraint")

    # ─── unanswered_questions: intent + channel לניתוח פערי ידע ─────────────
    _ensure_column(conn, "unanswered_questions", "intent", "TEXT DEFAULT ''")
    _ensure_column(conn, "unanswered_questions", "channel", "TEXT DEFAULT ''")

    # ─── bot_settings: הפעלת/כיבוי קובץ יומן .ics לאישור תור ──────────────
    _ensure_column(conn, "bot_settings", "ics_enabled", "INTEGER DEFAULT 1")

    # ─── user_identities: טבלת זהויות (BSUID / Meta Cloud API 2026) ──────
    existing_ui = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_identities'"
    ).fetchone()
    if not existing_ui:
        conn.execute("""
            CREATE TABLE user_identities (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                TEXT NOT NULL,
                channel                TEXT NOT NULL DEFAULT 'whatsapp'
                                           CHECK(channel IN ('telegram', 'whatsapp')),
                whatsapp_bsuid         TEXT,
                whatsapp_parent_bsuid  TEXT,
                phone_number           TEXT,
                username               TEXT DEFAULT '',
                created_at             TEXT DEFAULT (datetime('now')),
                updated_at             TEXT DEFAULT (datetime('now')),
                UNIQUE(channel, user_id)
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_identities_bsuid
                ON user_identities(whatsapp_bsuid) WHERE whatsapp_bsuid IS NOT NULL
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_identities_phone
                ON user_identities(phone_number) WHERE phone_number IS NOT NULL
        """)
        logger.info("Created user_identities table via migration")

    # ─── user_identities: Parent BSUID (forward-compat ל-Meta-managed portfolios)
    # Parent BSUID יכול להיות משותף בין משתמשים — לכן ללא UNIQUE וללא אינדקס.
    _ensure_column(conn, "user_identities", "whatsapp_parent_bsuid", "TEXT")

    # ─── users: טבלת משתמשים מרכזית + אכלוס מ-conversations ─────────────
    existing_users_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not existing_users_table:
        conn.execute("""
            CREATE TABLE users (
                user_id         TEXT PRIMARY KEY,
                username        TEXT DEFAULT '',
                channel         TEXT DEFAULT 'telegram',
                first_seen_at   TEXT DEFAULT (datetime('now')),
                last_active_at  TEXT DEFAULT (datetime('now')),
                message_count   INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at)"
        )
        logger.info("Created users table via migration")

    # אכלוס ראשוני מ-conversations (רק אם הטבלה ריקה)
    user_count = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
    if user_count == 0:
        backfilled = conn.execute("""
            INSERT OR IGNORE INTO users (user_id, username, channel, first_seen_at, last_active_at, message_count)
            SELECT
                c.user_id,
                c.username,
                COALESCE(c.channel, 'telegram'),
                MIN(c.created_at),
                MAX(c.created_at),
                COUNT(*)
            FROM conversations c
            WHERE c.role = 'user'
            GROUP BY c.user_id
        """).rowcount
        if backfilled:
            logger.info("Backfilled %d users from conversations into users table", backfilled)

    # ─── broadcast_messages: הוספת 'custom' ל-CHECK constraint ────────────
    bm_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='broadcast_messages'"
    ).fetchone()
    if bm_sql and "'custom'" not in (bm_sql["sql"] or ""):
        conn.execute("ALTER TABLE broadcast_messages RENAME TO _broadcast_messages_old")
        conn.execute("""
            CREATE TABLE broadcast_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_text    TEXT NOT NULL,
                audience        TEXT NOT NULL DEFAULT 'all'
                                    CHECK(audience IN ('all', 'booked', 'recent', 'custom')),
                total_recipients INTEGER DEFAULT 0,
                sent_count      INTEGER DEFAULT 0,
                failed_count    INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'queued'
                                    CHECK(status IN ('queued', 'sending', 'completed', 'failed')),
                created_at      TEXT DEFAULT (datetime('now')),
                completed_at    TEXT
            )
        """)
        conn.execute("""
            INSERT INTO broadcast_messages
                (id, message_text, audience, total_recipients, sent_count, failed_count, status, created_at, completed_at)
            SELECT id, message_text, audience, total_recipients, sent_count, failed_count, status, created_at, completed_at
            FROM _broadcast_messages_old
        """)
        conn.execute("DROP TABLE _broadcast_messages_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_broadcast_status ON broadcast_messages(status)"
        )
        logger.info("Migrated broadcast_messages: added 'custom' audience type")

    # ─── users: opt-in / opt-out לשיווק ב-WhatsApp (תיקון 40 + חוקי WABA) ───
    # לקוחות חייבים לתת opt-in מפורש כדי לקבל קמפיינים שיווקיים, ולקבל דרך
    # לצאת בכל עת (opt-out). opted_out_at הוא soft-delete: המשתמש נשאר ב-DB
    # אבל לא ייכלל ב-audience של קמפיינים.
    # ─── users: provenance של מזהה הספק (Meta IG/Messenger) ─────────────────
    # ערוצי מטא — PSID (Messenger) / IGSID (IG) הם page/account-scoped:
    # נשמרים גם ב-user_id (עם prefix) וגם ב-external_user_id (raw, לקריאות
    # ל-Graph API). provider_asset_id שומר את ה-page_id / IGBA — provenance
    # שמאפשר למחוק לפי asset, לדבג, ולתמוך ב-multi-asset deployments בעתיד.
    # אחרי backfill יש שכבת הגנה: UNIQUE(channel, provider_asset_id, external_user_id).
    _ensure_column(conn, "users", "provider_asset_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "users", "external_user_id", "TEXT DEFAULT ''")
    # backfill: למשתמשי Telegram/WhatsApp קיימים, ה-user_id עצמו הוא ה-raw id.
    # מעתיקים פעם אחת כדי שה-UNIQUE החדש יחול גם עליהם בלי לשבור שורות ישנות.
    conn.execute(
        """UPDATE users SET external_user_id = user_id
           WHERE external_user_id = '' AND channel IN ('telegram', 'whatsapp')"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_provider_identity "
        "ON users(channel, provider_asset_id, external_user_id) "
        "WHERE external_user_id != ''"
    )

    _ensure_column(conn, "users", "wa_marketing_opt_in", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "wa_marketing_opt_in_at", "TEXT")
    _ensure_column(conn, "users", "wa_marketing_opt_in_source", "TEXT DEFAULT ''")
    _ensure_column(conn, "users", "wa_opted_out_at", "TEXT")
    # Timestamp של פנייה פרואקטיבית לבקשת opt-in — מונע להציק לאותו משתמש
    # שוב ושוב עם אותה בקשה. NULL = עוד לא נשאל.
    _ensure_column(conn, "users", "wa_opt_in_prompt_sent_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_wa_marketing "
        "ON users(wa_marketing_opt_in, wa_opted_out_at)"
    )

    # ─── broadcast_campaigns: audience filter (wizard step 3) ────────────
    _ensure_column(
        conn, "broadcast_campaigns", "audience_type",
        "TEXT NOT NULL DEFAULT 'opted_in_only'",
    )
    _ensure_column(
        conn, "broadcast_campaigns", "audience_filter_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    # ─── broadcast_campaigns: last_saved_at — דיכוי טיוטות שלא נשמרו ──────
    # יצירת draft קורית בשלב 1 של ה-wizard (לפני שהמשתמש לחץ "שמור טיוטה")
    # כדי לאפשר preview/audience HTMX. כדי שלא יוצגו ברשימה — ה-last_saved_at
    # מתעדכן רק כשהמשתמש לוחץ "שמור טיוטה" בטופס. רשומות ישנות (לפני
    # המיגרציה) נחשבות שמורות — backfill מקבל את ה-created_at שלהן.
    #
    # ⚠ ה-backfill חייב לרוץ פעם אחת בלבד — בריצה הראשונה שבה העמודה
    # נוספת. אם נריץ אותו בכל startup, טיוטות יתומות (last_saved_at=NULL,
    # שנוצרו ע"י step-1 wizard ועדיין לא נשמרו) ייצבעו כשמורות ויתגלו
    # ברשימה — בדיוק הבאג שנפתר. לכן בודקים את קיום העמודה לפני ההוספה.
    _bc_cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(broadcast_campaigns)").fetchall()
    }
    if "last_saved_at" not in _bc_cols:
        conn.execute(
            "ALTER TABLE broadcast_campaigns ADD COLUMN last_saved_at TEXT"
        )
        # backfill לכל הקמפיינים הקיימים, לא רק drafts. הסיבה: ביטול
        # תזמון (cancel_scheduled_campaign) מחזיר scheduled → draft.
        # אם backfill הוגבל ל-status='draft', קמפיינים scheduled שהיו
        # קיימים לפני המיגרציה היו נשארים עם last_saved_at=NULL,
        # ובעת ביטול תזמון הם היו נעלמים מהרשימה ונמחקים תוך 60 דק'
        # ע"י cleanup_unsaved_draft_campaigns. אובדן נתונים.
        conn.execute(
            "UPDATE broadcast_campaigns "
            "SET last_saved_at = COALESCE(updated_at, created_at)"
        )

    # ─── broadcast_deliveries: מעקב אחר שליחה לכל נמען בקמפיין (שלב 4) ───
    # טבלה אחת לנמען — מאפשרת audit מדויק, retry של כישלונות נקודתיים,
    # ועדכון סטטוסים מ-Twilio callback (sent/delivered/read/failed).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_deliveries (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id             INTEGER NOT NULL,
            user_id                 TEXT NOT NULL,
            rendered_variables_json TEXT DEFAULT '{}',
            twilio_message_sid      TEXT,
            status                  TEXT NOT NULL DEFAULT 'queued'
                                        CHECK(status IN ('queued', 'sent', 'delivered',
                                                         'read', 'failed', 'undelivered')),
            error_code              TEXT,
            error_message           TEXT,
            queued_at               TEXT DEFAULT (datetime('now')),
            sent_at                 TEXT,
            delivered_at            TEXT,
            read_at                 TEXT,
            failed_at               TEXT,
            UNIQUE(campaign_id, user_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_campaign "
        "ON broadcast_deliveries(campaign_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_twilio_sid "
        "ON broadcast_deliveries(twilio_message_sid)"
    )

    # ─── whatsapp_templates: header_text (Phase 2 של יצירת תבנית) ──────────
    # תוכן הכותרת שנשלחת ל-Meta — מאוחסן בנפרד מ-header_type כי ה-type
    # יכול להיות 'text' אבל גם 'image'/'video' (עם media URL בעתיד).
    _ensure_column(conn, "whatsapp_templates", "header_text", "TEXT DEFAULT ''")
    # Phase 3: URL חיצוני ל-header media (image/video/document).
    _ensure_column(
        conn, "whatsapp_templates", "header_media_url", "TEXT DEFAULT ''"
    )

    # ─── בחירת משך תור באישור: הגדרות גלובליות + עמודה לתור ─────────────
    # בעל העסק בוחר את משך התור בלחיצה על "אשר" — מציגים לו טווח אופציות
    # סביב ברירת המחדל הגלובלית, מסוננות לפי קונפליקטים ביומן.
    # default_appointment_duration_minutes — ברירת מחדל אחידה לכל השירותים
    # (החליפה את duration_minutes הפר-שירות; טבלת services נשארת לרישום בלבד)
    # appointment_duration_step_minutes — גודל קפיצה (15 דק' ברירת מחדל)
    # appointment_duration_steps_backward — כמה אופציות קצרות יותר להציג
    # appointment_duration_steps_forward — כמה אופציות ארוכות יותר להציג
    _ensure_column(
        conn, "bot_settings", "default_appointment_duration_minutes",
        "INTEGER NOT NULL DEFAULT 60",
    )
    _ensure_column(
        conn, "bot_settings", "appointment_duration_step_minutes",
        "INTEGER NOT NULL DEFAULT 15",
    )
    _ensure_column(
        conn, "bot_settings", "appointment_duration_steps_backward",
        "INTEGER NOT NULL DEFAULT 2",
    )
    _ensure_column(
        conn, "bot_settings", "appointment_duration_steps_forward",
        "INTEGER NOT NULL DEFAULT 4",
    )
    # confirmed_duration_minutes — המשך שבעל העסק בחר באישור התור.
    # NULL = לא אושר עדיין, או נופלים על ברירת המחדל של השירות.
    _ensure_column(
        conn, "appointments", "confirmed_duration_minutes",
        "INTEGER DEFAULT NULL",
    )

    # ─── אישור תורים אוטומטי ─────────────────────────────────────────────
    # auto_booking_mode — קובע מה קורה כשלקוח קובע תור דרך הבוט:
    #   manual (ברירת מחדל)  — תור נוצר כ-pending, בעל עסק מאשר ידנית
    #   auto_with_check      — אם הסלוט פנוי (שעות עבודה + חופשה + GCal busy)
    #                          ⇒ confirmed אוטומטית. אחרת נשאר pending.
    #   auto_always          — תמיד confirmed (שימוש על אחריות בעל העסק).
    # auto_booking_max_days_ahead — חוסם בקשות רחוקות מדי (90 יום ברירת מחדל).
    _ensure_column(
        conn, "bot_settings", "auto_booking_mode",
        "TEXT NOT NULL DEFAULT 'manual'",
    )
    _ensure_column(
        conn, "bot_settings", "auto_booking_max_days_ahead",
        "INTEGER NOT NULL DEFAULT 90",
    )
    # auto_booking_buffer_after_event_minutes — דקות שמרחיבים אחרי כל
    # אירוע ביומן Google. נועד למקרה שתור קודם נמשך יותר מהמתוכנן.
    # רלוונטי גם ל-display של slots ללקוח (calendar keyboard) וגם ל-decision.
    _ensure_column(
        conn, "bot_settings", "auto_booking_buffer_after_event_minutes",
        "INTEGER NOT NULL DEFAULT 0",
    )

    # ─── סימון "תור חדש שטרם נצפה" — להצגת בועת התראה בסיידבאר וביומן ─────
    # owner_seen=0 פירושו תור שעדיין לא נראה ע"י בעל העסק. נכתב 0 בעת יצירת
    # תור חדש (גם בידני וגם באישור אוטומטי), ועובר ל-1 כשבעל העסק טוען את
    # עמוד התורים. הבועה בסיידבאר סופרת `owner_seen=0 AND status != cancelled`,
    # ולכן תור שאושר אוטומטית עדיין מייצר התראה — זה מה ששובר את הקישור
    # שהיה קודם בין pending ל-"חדש".
    # backfill: בהתקנה ראשונה של העמודה, מסמנים את כל התורים הקיימים
    # כ"נצפו" כדי לא להציף את בעל העסק בעשרות התראות על תורים היסטוריים.
    cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()}
    _ensure_column(
        conn, "appointments", "owner_seen",
        "INTEGER NOT NULL DEFAULT 0",
    )
    if "owner_seen" not in cols_before:
        # העמודה נוספה כעת — backfill חד-פעמי לכל הרשומות הקיימות
        cursor = conn.execute("UPDATE appointments SET owner_seen = 1 WHERE owner_seen = 0")
        if cursor.rowcount:
            logger.info("Backfilled owner_seen=1 on %d existing appointments", cursor.rowcount)

    # ─── Google Calendar — מעקב אחרי בריאות החיבור ──────────────────────
    # auth_invalid_at — timestamp של כשל refresh אחרון (invalid_grant וכד').
    # NULL = החיבור תקין. ערך = הטוקן פג/נמחק ויש לחבר מחדש.
    # is_connected() מחזיר False כשהדגל הזה מוגדר, כדי שה-decision logic לא
    # יקבל "פעיל" שגוי ויתפוס סלוטים מבלי שהאירוע באמת ייכתב ל-GCal.
    # owner_alert_sent_at — מסמן ששלחנו כבר התראה לבעל העסק על הבעיה,
    # כדי לא לשלוח שוב כל refresh attempt.
    _ensure_column(
        conn, "google_calendar_credentials", "auth_invalid_at",
        "TEXT DEFAULT NULL",
    )
    _ensure_column(
        conn, "google_calendar_credentials", "owner_alert_sent_at",
        "TEXT DEFAULT NULL",
    )

    # ─── הסכמת משתמש למדיניות פרטיות (תיקון 13 לחוק הגנת הפרטיות) ────────
    # consent_given_at — תאריך/שעה שבה המשתמש לחץ "מסכים" במסך ההסכמה הראשוני.
    # NULL = לא נתן הסכמה עדיין; כל handler שדורש consent יציג את מסך ההסכמה.
    # consent_version — גרסת המסמכים שהמשתמש הסכים אליה. שינוי גרסה מוביל
    # להצגת מסך הסכמה מחדש (למשל אחרי עדכון משמעותי במדיניות).
    _ensure_column(conn, "users", "consent_given_at", "TEXT DEFAULT NULL")
    _ensure_column(conn, "users", "consent_version", "INTEGER NOT NULL DEFAULT 0")

    # ─── broadcast_message_recipients — רשימת נמענים לקהל מותאם אישית ─────
    # נשמר כדי לאפשר תצוגת רשימת לקוחות שנכללו בשידור גם אחרי שהמסננים
    # השתנו (למשל לקוח שכבר לא עומד בקריטריון "לא פעיל מעל X ימים").
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_message_recipients (
            broadcast_id    INTEGER NOT NULL,
            user_id         TEXT NOT NULL,
            PRIMARY KEY (broadcast_id, user_id),
            FOREIGN KEY (broadcast_id) REFERENCES broadcast_messages(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_user "
        "ON broadcast_message_recipients(user_id)"
    )

    # ─── blocked_users — block_category + appeal_contact (תיקון 13) ───────
    # פיצול ל-2 שדות: category enum נחשף בעיון, internal_reason לא.
    # לעמודות חדשות יש DEFAULT, אז משתמשים קיימים יקבלו 'manual' אוטומטית.
    _ensure_column(conn, "blocked_users", "block_category",
                   "TEXT NOT NULL DEFAULT 'manual'")
    _ensure_column(conn, "blocked_users", "block_reason_internal",
                   "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "blocked_users", "appeal_contact_method",
                   "TEXT NOT NULL DEFAULT ''")
    # backfill: 'reason' הישן נשמר ב-block_reason_internal (כי הוא היה
    # פנימי בפועל — לא היה נחשף לאף אחד), כדי לא לאבד תוכן קיים.
    # רק שורות שעוד לא קיבלו internal_reason (היה ריק).
    try:
        conn.execute(
            "UPDATE blocked_users SET block_reason_internal = reason "
            "WHERE block_reason_internal = '' AND reason != ''"
        )
    except Exception:
        logger.error("Migration: backfill blocked_users internal_reason failed", exc_info=True)

    # ─── user_notes — תוספת note_tags + withhold_reason (תיקון 13) ────────
    # פיצול לפי המלצת היועץ: note ייחשף בעיון לפי ברירת מחדל, tags סגורות
    # לא נחשפות, withhold_reason מאפשר חריג נקודתי לפתק שלא ייחשף.
    _ensure_column(conn, "user_notes", "note_tags", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "user_notes", "withhold_reason", "TEXT NOT NULL DEFAULT ''")

    # ─── הצפנת google_calendar_credentials (טוקנים קיימים בטקסט גלוי) ──────
    # Migration חד-פעמי: tokens שנשמרו לפני ההצפנה — מצפינים אותם עכשיו.
    # זוהה עם is_encrypted; אם כבר מוצפן (יש prefix v1:) — לא נוגעים.
    # רץ רק אם יש מפתח הצפנה מוגדר; אחרת מדלגים בשקט (תקין בסביבת dev
    # שעוד לא הגדירה SECRETS_ENCRYPTION_KEY — ההצפנה תקרה בכתיבה הבאה).
    try:
        from utils.crypto import is_encryption_configured, is_encrypted, encrypt_field
        if is_encryption_configured():
            row = conn.execute(
                "SELECT refresh_token, access_token FROM google_calendar_credentials WHERE id = 1"
            ).fetchone()
            if row:
                rt = row["refresh_token"] or ""
                at = row["access_token"] or ""
                updates: list = []
                params: list = []
                if rt and not is_encrypted(rt):
                    updates.append("refresh_token = ?")
                    params.append(encrypt_field(rt))
                if at and not is_encrypted(at):
                    updates.append("access_token = ?")
                    params.append(encrypt_field(at))
                if updates:
                    conn.execute(
                        f"UPDATE google_calendar_credentials SET {', '.join(updates)} WHERE id = 1",
                        params,
                    )
                    logger.info(
                        "Migrated google_calendar_credentials: encrypted %d legacy token field(s)",
                        len(updates),
                    )
    except Exception:
        # לא קריטי — המערכת תמשיך לעבוד עם tokens בטקסט גלוי עד שיוחלפו
        # בכתיבה הבאה. log ולא חוסם startup.
        logger.error(
            "Migration: כשל בהצפנת google_calendar_credentials legacy tokens",
            exc_info=True,
        )

    # ─── Plans + Feature Flags — מערכת חבילות SaaS (subscription + plan_history) ──
    # singleton table (id=1 בלבד) שמחזיק את חבילת הלקוח SaaS (בעל העסק) ואת
    # הפיצ'רים הפעילים. ראה plans_config.py + feature_flags.py.
    # המוצר חד-שכבתי: כל פריסה (עותק ריפו ללקוח) מקבלת `premium` כברירת מחדל
    # כדי שכל הפיצ'רים דלוקים בלי טיפול ידני בחבילות. המנגנון נשאר קיים —
    # ניתן להוריד לקוח ל-basic/advanced ידנית דרך /dev/subscription אם אי-פעם צריך.
    existing_subscription = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subscription'"
    ).fetchone()
    if not existing_subscription:
        conn.execute("""
            CREATE TABLE subscription (
                id                INTEGER PRIMARY KEY CHECK(id = 1),
                plan              TEXT NOT NULL DEFAULT 'premium'
                                      CHECK(plan IN ('basic', 'advanced', 'premium')),
                channel           TEXT NOT NULL DEFAULT ''
                                      CHECK(channel IN ('', 'telegram', 'whatsapp')),
                features_json     TEXT NOT NULL DEFAULT '{}',
                plan_started_at   TEXT NOT NULL DEFAULT (datetime('now')),
                plan_ends_at      TEXT,
                grace_period_days INTEGER NOT NULL DEFAULT 30,
                notes             TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT OR IGNORE INTO subscription (id) VALUES (1)")
        logger.info("Created subscription table via migration (default plan=premium)")

    # ─── subscription: ערוץ פר-tenant (multi-tenant) ──────────────────────
    # '' = טרם נקבע (שני מקטעי הערוצים פתוחים); נקבע אוטומטית בחיבור
    # הערוץ הראשון וננעל עד שחרור ע"י מנהל הפלטפורמה. ראה feature_flags.
    _ensure_column(
        conn, "subscription", "channel",
        "TEXT NOT NULL DEFAULT '' CHECK(channel IN ('', 'telegram', 'whatsapp'))",
    )

    # plan_history — audit trail לכל שינוי חבילה / override פיצ'ר.
    # נשמר לתמיד (לא נמחק ב-delete_user_data כי זה לא PII).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_history (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            changed_at               TEXT NOT NULL DEFAULT (datetime('now')),
            previous_plan            TEXT,
            new_plan                 TEXT NOT NULL,
            previous_features_json   TEXT,
            new_features_json        TEXT,
            reason                   TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_history_changed_at "
        "ON plan_history(changed_at DESC)"
    )

    # ─── response_pages: page_type — הבחנה בין סוגי דפים ציבוריים.
    # שלושה ערכים אפשריים:
    #   'legacy'           — רשומות שנוצרו לפני המיגרציה הזו (תוכן היסטורי
    #                        מעורב; חלקן fallback של WhatsApp). שורות קיימות
    #                        מקבלות את הערך הזה אוטומטית כברירת מחדל. נחסם
    #                        מ-feature_flag כי מערבים מקורות.
    #   'whatsapp_fallback' — נכתב **רק** ע"י `_send_as_page` (תשתית פנימית
    #                         לעקיפת תקרת 1600 התווים של Twilio). תמיד פעיל,
    #                         לא נחסם ע"י feature_flag.
    #   'landing'          — נכתב **רק** ע"י הראוט החדש בפאנל ליצירת דפי
    #                         נחיתה שיווקיים. נחסם ע"י has_feature("landing_page").
    #
    # ה-DEFAULT הוא 'legacy' — עוטף שורות קיימות ב-tag נפרד כדי שהקוד החדש
    # לא יערב נתונים היסטוריים עם נתונים שנוצרו אחרי המעבר. כל קוד חדש
    # שכותב ל-response_pages חייב לפסוק page_type מפורש.
    _ensure_column(
        conn, "response_pages", "page_type",
        "TEXT NOT NULL DEFAULT 'legacy'",
    )

    # ─── customer_facts: status 'resolved' + resolved_at/resolution_evidence ─
    # action=resolve (פרומפט v2.2) סוגר open_issue → status חדש 'resolved'.
    # שינוי ה-CHECK constraint ב-SQLite דורש table-rebuild (CHECK הוא חלק
    # מ-CREATE TABLE). idempotency: רק אם resolved_at עדיין לא קיים — ב-DB
    # חדש init_db כבר יצר את הטבלה עם הסכמה החדשה, אז ה-migration ידלג.
    cf_cols = {
        c["name"]
        for c in conn.execute("PRAGMA table_info(customer_facts)").fetchall()
    }
    if cf_cols and "resolved_at" not in cf_cols:
        # FK חייב OFF ל-rebuild: customer_facts_new מפנה ל-customer_facts
        # (self-FK ב-superseded_by_id), וה-DROP של הישן יפר את ה-FK אחרת.
        # commit לפני ה-PRAGMA כדי שלא יהיה no-op בתוך טרנזקציה פתוחה.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""
            CREATE TABLE customer_facts_new (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             TEXT NOT NULL,
                business_id         TEXT NOT NULL DEFAULT 'default',
                fact_type           TEXT NOT NULL
                                    CHECK(fact_type IN ('preference','personal_info','relationship','vocabulary','open_issue')),
                content             TEXT NOT NULL,
                confidence          REAL NOT NULL,
                source              TEXT NOT NULL DEFAULT 'inferred'
                                    CHECK(source IN ('inferred','business_owner')),
                requires_consent    INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL
                                    CHECK(status IN ('active','pending_approval','rejected','superseded','resolved')),
                evidence            TEXT DEFAULT '',
                superseded_by_id    INTEGER REFERENCES customer_facts(id) ON DELETE SET NULL,
                created_at          TEXT DEFAULT (datetime('now')),
                last_confirmed_at   TEXT DEFAULT (datetime('now')),
                access_count        INTEGER DEFAULT 0,
                resolved_at         TEXT,
                resolution_evidence TEXT
            )
        """)
        conn.execute("""
            INSERT INTO customer_facts_new
                (id, user_id, business_id, fact_type, content, confidence, source,
                 requires_consent, status, evidence, superseded_by_id, created_at,
                 last_confirmed_at, access_count)
            SELECT id, user_id, business_id, fact_type, content, confidence, source,
                   requires_consent, status, evidence, superseded_by_id, created_at,
                   last_confirmed_at, access_count
            FROM customer_facts
        """)
        conn.execute("DROP TABLE customer_facts")
        conn.execute("ALTER TABLE customer_facts_new RENAME TO customer_facts")
        # recreate indexes — נמחקו יחד עם הטבלה הישנה
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_facts_user_business "
            "ON customer_facts(user_id, business_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_facts_status "
            "ON customer_facts(status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_facts_active_unique "
            "ON customer_facts(user_id, business_id, fact_type, content) "
            "WHERE status = 'active'"
        )
        # בודקים שאין הפרות FK שהשתחזרו (לפי החלטה: לוג אם יש; לא מקריס —
        # נדיר במיוחד, כי superseded_by_id היה מצביע לאותה טבלה ש-rebuilt
        # עם אותם IDs).
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            logger.error(
                "customer_facts migration: foreign_key_check violations: %s",
                fk_violations,
            )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info(
            "Migrated customer_facts: +resolved status, "
            "+resolved_at/resolution_evidence columns",
        )

    # ─── שלב 6.2 — id-based cursor ב-extraction_runs ────────────────
    # Cursor הקיים (conversation_end timestamp) מפספס הודעות באותה שנייה
    # כש-cap חתך באמצע. עוברים ל-id-based: ה-cursor הוא MAX(conversations.id)
    # שה-extractor עבד עליו. עמודה NULLable כדי שruns ישנים יישארו תקפים
    # (helper get_last_extraction_message_id מסנן IS NOT NULL).
    _ensure_column(conn, "extraction_runs", "last_message_id", "INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_extraction_runs_user_msg_id "
        "ON extraction_runs(user_id, business_id, status, last_message_id)"
    )
