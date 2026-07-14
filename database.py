"""
Database module — SQLite storage for knowledge base, conversations, and notifications.
"""

import logging
import re
import sqlite3
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# DB_PATH מיובא לתאימות בלבד (טסטים ישנים עושים patch על database.DB_PATH);
# הנתיב האפקטיבי נקבע ב-get_connection דרך tenancy.tenant_db_path().
from ai_chatbot.config import DB_PATH, TONE_DEFINITIONS  # noqa: F401
from tenancy import tenant_db_path


@contextmanager
def get_connection():
    """Yield a SQLite connection and always close it safely."""
    # הבוט (asyncio) והאדמין (Flask) רצים באותו תהליך. נוצר חיבור חדש לכל
    # פעולה, עם timeout נדיב ו-busy_timeout כדי לצמצם שגיאות "database is locked".
    # check_same_thread=False נדרש כי Flask ו-asyncio משתמשים ב-threads שונים,
    # אבל ה-connection עצמו *אינו* thread-safe — השימוש הבטוח מובטח ע"י
    # context manager שפותח וסוגר חיבור בכל פעולה (ללא שיתוף בין threads).
    #
    # multi-tenant (שלב 1): הנתיב נקבע לפי ה-tenant הנוכחי (tenancy.py).
    # ה-tenant של ברירת המחדל ממופה ל-config.DB_PATH — התנהגות זהה לקודם.
    conn = sqlite3.connect(str(tenant_db_path()), timeout=30, check_same_thread=False)
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


def init_db():
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executescript("""
            -- Knowledge Base entries
            CREATE TABLE IF NOT EXISTS kb_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}',
                is_active   INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            -- Chunked versions for RAG
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id    INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text  TEXT NOT NULL,
                embedding   BLOB,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (entry_id) REFERENCES kb_entries(id) ON DELETE CASCADE
            );

            -- Conversation history
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                message     TEXT NOT NULL,
                sources     TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- Agent transfer notifications
            CREATE TABLE IF NOT EXISTS agent_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                telegram_username TEXT DEFAULT '',
                message     TEXT DEFAULT '',
                status      TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'handled', 'dismissed')),
                created_at  TEXT DEFAULT (datetime('now')),
                handled_at  TEXT
            );

            -- Appointment bookings
            CREATE TABLE IF NOT EXISTS appointments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                telegram_username TEXT DEFAULT '',
                service     TEXT DEFAULT '',
                preferred_date TEXT DEFAULT '',
                preferred_time TEXT DEFAULT '',
                notes       TEXT DEFAULT '',
                status      TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'cancelled', 'passed')),
                reminder_sent INTEGER DEFAULT 0,
                second_reminder_sent INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- Conversation summaries for long-term memory
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                     TEXT NOT NULL,
                summary_text                TEXT NOT NULL,
                message_count               INTEGER NOT NULL DEFAULT 0,
                last_summarized_message_id  INTEGER NOT NULL DEFAULT 0,
                created_at                  TEXT DEFAULT (datetime('now'))
            );

            -- Live chat sessions (business owner takes over a conversation)
            CREATE TABLE IF NOT EXISTS live_chats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                is_active   INTEGER DEFAULT 1,
                started_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now')),
                ended_at    TEXT
            );

            -- Unanswered questions (knowledge gaps)
            CREATE TABLE IF NOT EXISTS unanswered_questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                question    TEXT NOT NULL,
                status      TEXT DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'not_relevant')),
                created_at  TEXT DEFAULT (datetime('now')),
                resolved_at TEXT
            );

            -- Business hours (weekly schedule)
            CREATE TABLE IF NOT EXISTS business_hours (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
                open_time   TEXT,
                close_time  TEXT,
                is_closed   INTEGER DEFAULT 0,
                UNIQUE(day_of_week)
            );

            -- Special days (holidays, one-time closures, custom hours)
            CREATE TABLE IF NOT EXISTS special_days (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL,
                open_time   TEXT,
                close_time  TEXT,
                is_closed   INTEGER DEFAULT 1,
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- Referral codes (קוד הפניה קבוע לכל משתמש)
            CREATE TABLE IF NOT EXISTS referral_codes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL UNIQUE,
                code            TEXT NOT NULL UNIQUE,
                sent            INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            -- Referrals (כל הפניה בודדת)
            CREATE TABLE IF NOT EXISTS referrals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     TEXT NOT NULL,
                referred_id     TEXT NOT NULL UNIQUE,
                code            TEXT NOT NULL,
                status          TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
                created_at      TEXT DEFAULT (datetime('now')),
                completed_at    TEXT
            );

            -- Referral credits (זיכויים מהפניות)
            CREATE TABLE IF NOT EXISTS credits (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                amount          REAL NOT NULL,
                type            TEXT NOT NULL CHECK(type IN ('referrer', 'referred')),
                reason          TEXT DEFAULT '',
                used            INTEGER DEFAULT 0,
                expires_at      TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            -- טבלת משתמשים מרכזית — מאוכלסת אוטומטית בכל אינטראקציה
            CREATE TABLE IF NOT EXISTS users (
                user_id              TEXT PRIMARY KEY,
                username             TEXT DEFAULT '',
                channel              TEXT DEFAULT 'telegram',
                -- ערוצי מטא (Messenger/IG) — מזהה ה-asset (page_id ל-Messenger,
                -- ig_business_account_id ל-IG). ריק עבור Telegram/WhatsApp.
                -- שומר provenance: מאיזה עמוד/חשבון הגיע המשתמש.
                provider_asset_id    TEXT DEFAULT '',
                -- מזהה raw של הספק (PSID/IGSID/chat_id/טלפון). חזרת המפתח
                -- הראשי `user_id` מוגזר ממנו (`meta_ig:<igsid>`), אבל את ה-raw
                -- שומרים נפרד גם בשביל UNIQUE constraint וגם לקריאה ל-Graph API.
                external_user_id     TEXT DEFAULT '',
                first_seen_at   TEXT DEFAULT (datetime('now')),
                last_active_at  TEXT DEFAULT (datetime('now')),
                message_count   INTEGER DEFAULT 0
            );

            -- הערה: ה-UNIQUE INDEX idx_users_provider_identity מוגדר
            -- ב-migrations.py — לא כאן. הסיבה: ב-deployment קיים, ה-CREATE TABLE
            -- IF NOT EXISTS לא מוסיף עמודות לטבלה קיימת, ולכן `external_user_id`
            -- עוד לא קיימת כשה-init_db רץ. ה-migration מטפל בהוספת העמודות
            -- + יצירת ה-index בסדר הנכון. ל-fresh DB ה-index ייווצר במיגרציה
            -- מיד אחרי init_db.

            -- Broadcast messages (הודעות יזומות)
            CREATE TABLE IF NOT EXISTS broadcast_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_text    TEXT NOT NULL,
                audience        TEXT NOT NULL DEFAULT 'all' CHECK(audience IN ('all', 'booked', 'recent', 'custom')),
                total_recipients INTEGER DEFAULT 0,
                sent_count      INTEGER DEFAULT 0,
                failed_count    INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'queued' CHECK(status IN ('queued', 'sending', 'completed', 'failed')),
                created_at      TEXT DEFAULT (datetime('now')),
                completed_at    TEXT
            );

            -- נמעני שידור עבור קהל מותאם אישית — מאפשר תצוגת רשימת לקוחות
            -- שנכללו בשידור גם אחרי שהמסננים השתנו. נשמר רק עבור audience='custom'
            -- (לקהלים סטנדרטיים אפשר לגזור מחדש מתנאי הסינון).
            CREATE TABLE IF NOT EXISTS broadcast_message_recipients (
                broadcast_id    INTEGER NOT NULL,
                user_id         TEXT NOT NULL,
                PRIMARY KEY (broadcast_id, user_id),
                FOREIGN KEY (broadcast_id) REFERENCES broadcast_messages(id) ON DELETE CASCADE
            );

            -- User subscription status (הרשמה/ביטול הרשמה לשידורים)
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                user_id         TEXT NOT NULL PRIMARY KEY,
                is_subscribed   INTEGER DEFAULT 1,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            -- Vacation mode (שורה בודדת — תמיד id=1)
            CREATE TABLE IF NOT EXISTS vacation_mode (
                id                  INTEGER PRIMARY KEY CHECK(id = 1),
                is_active           INTEGER DEFAULT 0,
                vacation_end_date   TEXT DEFAULT '',
                vacation_message    TEXT DEFAULT '',
                updated_at          TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO vacation_mode (id) VALUES (1);

            -- הגדרות בוט — טון תקשורת וביטויים מותאמים (שורה בודדת — תמיד id=1)
            -- עמודות business_phone/address/website — כרטיס הביקור פר-tenant
            -- (ריק = fallback ל-env; ל-DB קיים הן מתווספות ב-migrations.py).
            -- שם העסק אינו כאן — מקורו display_name ב-control plane (הקמה).
            CREATE TABLE IF NOT EXISTS bot_settings (
                id              INTEGER PRIMARY KEY CHECK(id = 1),
                tone            TEXT NOT NULL DEFAULT 'friendly'
                                    CHECK(tone IN ('none', 'friendly', 'formal', 'sales', 'luxury')),
                custom_phrases  TEXT DEFAULT '',
                custom_prompt   TEXT DEFAULT '',
                business_phone  TEXT DEFAULT '',
                business_address TEXT DEFAULT '',
                business_website TEXT DEFAULT '',
                reminder_enabled INTEGER DEFAULT 1,
                reminder_time   TEXT DEFAULT '10:00',
                second_reminder_enabled INTEGER DEFAULT 0,
                second_reminder_hours REAL DEFAULT 2.0,
                auto_booking_mode TEXT NOT NULL DEFAULT 'manual'
                                    CHECK(auto_booking_mode IN ('manual', 'auto_with_check', 'auto_always')),
                auto_booking_max_days_ahead INTEGER NOT NULL DEFAULT 90,
                auto_booking_buffer_after_event_minutes INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO bot_settings (id) VALUES (1);

            -- Google Calendar credentials (שורה בודדת — תמיד id=1)
            CREATE TABLE IF NOT EXISTS google_calendar_credentials (
                id                      INTEGER PRIMARY KEY CHECK(id = 1),
                google_account_email    TEXT DEFAULT '',
                calendar_id             TEXT DEFAULT 'primary',
                refresh_token           TEXT DEFAULT '',
                access_token            TEXT DEFAULT '',
                token_expiry            TEXT DEFAULT '',
                timezone                TEXT DEFAULT 'Asia/Jerusalem',
                updated_at              TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO google_calendar_credentials (id) VALUES (1);

            -- Meta DM credentials (Instagram + Facebook Messenger).
            -- שורה לכל עמוד פייסבוק שחובר ב-OAuth. שלב 2 של מימוש מטא.
            -- access_token_encrypted מוצפן ברמת היישום (utils/crypto.py).
            -- המבנה תומך בכמה עמודים פר deployment כשמדובר באותו עסק
            -- עם בסיס ידע משותף (ראה docs/meta_dm_spec.md).
            CREATE TABLE IF NOT EXISTS meta_credentials (
                page_id                 TEXT PRIMARY KEY,
                ig_business_account_id  TEXT DEFAULT '',
                access_token_encrypted  TEXT NOT NULL,
                page_name               TEXT DEFAULT '',
                ig_username             TEXT DEFAULT '',
                created_at              TEXT DEFAULT (datetime('now')),
                updated_at              TEXT DEFAULT (datetime('now'))
            );
            -- partial UNIQUE על ig_business_account_id — webhook משתמש
            -- בזה כדי לזהות שעמוד IG מחובר (ה-`entry.id` של מטא משדר
            -- את ה-IGBA ב-events של אינסטגרם, לא את ה-page_id).
            -- partial (WHERE ... != '') כדי לאפשר כמה עמודי FB *בלי*
            -- IG מקושר (שדה ריק), אבל לאסור שני עמודים שטוענים שיש
            -- להם את אותו IGBA — מצב לא ייתכן במציאות.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_credentials_ig_account
                ON meta_credentials(ig_business_account_id)
                WHERE ig_business_account_id != '';

            -- הערות על לקוחות (פתקים של בעל העסק).
            -- מבנה לפי המלצת היועץ (תיקון 13):
            --   note            — טקסט חופשי, ייחשף בעיון משתמש לפי ברירת מחדל
            --   note_tags       — JSON של תגיות סגורות, לא נחשפות בעיון
            --   withhold_reason — אם מוגדר, ה-note לא ייחשף; משמש לחריגים
            --                     ספציפיים (סיבה ממשית לסירוב), עם תיעוד פנימי
            CREATE TABLE IF NOT EXISTS user_notes (
                user_id          TEXT NOT NULL PRIMARY KEY,
                note             TEXT NOT NULL DEFAULT '',
                note_tags        TEXT NOT NULL DEFAULT '[]',
                withhold_reason  TEXT NOT NULL DEFAULT '',
                updated_at       TEXT DEFAULT (datetime('now'))
            );

            -- משתמשים חסומים. מבנה לפי המלצת היועץ (תיקון 13):
            --   block_category — enum סגור (abuse/spam/repeated_no_show/manual)
            --                     נחשף בעיון בלי תוכן חופשי
            --   block_reason_internal — טקסט חופשי שבעל העסק כותב לעצמו;
            --                          לא נחשף בעיון בברירת מחדל
            --   appeal_contact_method — איך לפנות אם רוצים לערער; נחשף
            -- ה-row נשאר גם אחרי /forget (כל השאר נמחק) — רק
            -- block_category + blocked_at + appeal_contact מוחזקים כ-hold
            -- צר לאכיפה (אינטרס לגיטימי).
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id                TEXT NOT NULL PRIMARY KEY,
                username               TEXT DEFAULT '',
                reason                 TEXT DEFAULT '',
                block_category         TEXT NOT NULL DEFAULT 'manual'
                                           CHECK(block_category IN
                                               ('abuse', 'spam', 'repeated_no_show', 'manual')),
                block_reason_internal  TEXT NOT NULL DEFAULT '',
                appeal_contact_method  TEXT NOT NULL DEFAULT '',
                blocked_at             TEXT DEFAULT (datetime('now'))
            );

            -- Lead follow-ups (מעקב אחרי לידים שלא השלימו הזמנה)
            CREATE TABLE IF NOT EXISTS lead_followups (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             TEXT NOT NULL,
                username            TEXT DEFAULT '',
                channel             TEXT DEFAULT 'telegram' CHECK(channel IN ('telegram', 'whatsapp')),

                -- ניתוח השיחה
                service_of_interest TEXT DEFAULT '',
                intent_type         TEXT DEFAULT 'unknown',
                lead_temperature    TEXT DEFAULT 'cold' CHECK(lead_temperature IN ('cold', 'warm', 'hot')),
                conversation_summary TEXT DEFAULT '',
                analysis_json       TEXT DEFAULT '{}',

                -- סטטוס follow-up
                status              TEXT DEFAULT 'pending'
                                    CHECK(status IN ('pending', 'approved', 'sent', 'replied', 'converted', 'expired', 'cancelled')),
                template_key        TEXT,
                template_variables  TEXT DEFAULT '{}',

                -- תזמון
                followup_due_at     TEXT NOT NULL,
                followup_sent_at    TEXT,

                -- תוצאה
                user_replied        INTEGER DEFAULT 0,
                user_replied_at     TEXT,
                booking_after_followup INTEGER DEFAULT 0,
                stop_reason         TEXT DEFAULT '',

                -- מטא
                created_at          TEXT DEFAULT (datetime('now'))
            );

            -- דיווחי באגים למפתח (מבעל העסק דרך הפאנל)
            CREATE TABLE IF NOT EXISTS developer_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                description     TEXT NOT NULL,
                screenshot_count INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
                created_at      TEXT DEFAULT (datetime('now')),
                resolved_at     TEXT
            );

            -- זהויות משתמשים — מיפוי BSUID / שם משתמש / מספר טלפון ל-user_id קנוני.
            -- מכין את המערכת לשינוי Meta Cloud API (יוני 2026) שבו BSUID
            -- יחליף את מספר הטלפון כמזהה ברירת מחדל ב-WhatsApp.
            CREATE TABLE IF NOT EXISTS user_identities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                channel         TEXT NOT NULL DEFAULT 'whatsapp'
                                    CHECK(channel IN ('telegram', 'whatsapp')),
                whatsapp_bsuid  TEXT,
                phone_number    TEXT,
                username        TEXT DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(channel, user_id)
            );

            -- עמודי תשובה ציבוריים — לתשובות ארוכות ב-WhatsApp שמוגשות כעמוד HTML
            CREATE TABLE IF NOT EXISTS response_pages (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                title       TEXT DEFAULT '',
                user_id     TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- תבניות WhatsApp שסונכרנו מ-Twilio Content API.
            -- כל שורה = שפה אחת של Content SID (תבנית יכולה להיות
            -- מתורגמת למספר שפות — Twilio מחזיר אותן כ-Contents נפרדים).
            CREATE TABLE IF NOT EXISTS whatsapp_templates (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                content_sid      TEXT NOT NULL UNIQUE,
                friendly_name    TEXT NOT NULL,
                language         TEXT NOT NULL DEFAULT 'he',
                category         TEXT DEFAULT 'UTILITY'
                                     CHECK(category IN ('UTILITY', 'MARKETING', 'AUTHENTICATION', 'UNKNOWN')),
                approval_status  TEXT NOT NULL DEFAULT 'unsubmitted'
                                     CHECK(approval_status IN ('approved', 'pending', 'rejected', 'paused', 'unsubmitted')),
                rejection_reason TEXT,
                header_type      TEXT DEFAULT 'none'
                                     CHECK(header_type IN ('none', 'text', 'image', 'video', 'document', 'location')),
                header_text      TEXT DEFAULT '',
                header_media_url TEXT DEFAULT '',
                body_text        TEXT NOT NULL DEFAULT '',
                footer_text      TEXT DEFAULT '',
                buttons_json     TEXT NOT NULL DEFAULT '[]',
                variables_json   TEXT NOT NULL DEFAULT '[]',
                content_type     TEXT DEFAULT '',
                raw_json         TEXT DEFAULT '',
                last_synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
                created_at       TEXT DEFAULT (datetime('now'))
            );

            -- קמפיינים יזומים מבוססי-תבנית (broadcast עם HSM מאושר של Meta).
            -- draft נשמר כאן עם mapping של המשתנים; שליחה בפועל מתבצעת
            -- דרך broadcast_service בשלב מאוחר יותר.
            CREATE TABLE IF NOT EXISTS broadcast_campaigns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                title            TEXT DEFAULT '',
                template_sid     TEXT NOT NULL,
                variable_mapping_json TEXT NOT NULL DEFAULT '{}',
                status           TEXT NOT NULL DEFAULT 'draft'
                                     CHECK(status IN ('draft', 'scheduled', 'sending', 'completed', 'failed', 'paused')),
                scheduled_at     TEXT,
                total_recipients INTEGER DEFAULT 0,
                sent             INTEGER DEFAULT 0,
                delivered        INTEGER DEFAULT 0,
                read_count       INTEGER DEFAULT 0,
                failed           INTEGER DEFAULT 0,
                created_by       TEXT DEFAULT '',
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            );

            -- פנקס הסכמות פסאודונימי (תיקון 13 + תיקון 40).
            -- שתי קטגוריות באותה טבלה עם retention שונה:
            -- 'consent' — הוכחות הסכמה (5 שנים), 'audit' — תיעוד מימוש
            -- זכויות (24 חודשים). subject_hash הוא HMAC עם pepper נפרד
            -- מה-DB; ראה utils/consent_ledger.py.
            CREATE TABLE IF NOT EXISTS consent_ledger (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_hash    TEXT NOT NULL,
                pepper_version  TEXT NOT NULL DEFAULT 'v1',
                channel         TEXT NOT NULL,
                category        TEXT NOT NULL CHECK(category IN ('consent', 'audit')),
                event_type      TEXT NOT NULL,
                consent_version INTEGER,
                event_at        TEXT NOT NULL DEFAULT (datetime('now')),
                metadata_json   TEXT NOT NULL DEFAULT '{}',
                compromised     INTEGER NOT NULL DEFAULT 0
            );

            -- תור retry לכתיבות ledger שנכשלו (DB locked, pepper חסר וכו').
            -- payload_json מכיל את הקריאה המקורית עם user_id+channel גלויים
            -- (לא hash) כדי לאפשר חישוב hash אחר כך אם ה-pepper חוזר.
            -- job יומי (ב-purge_old_data) מנסה שוב; אחרי 5 ניסיונות מתעד
            -- [LEDGER_RETRY_EXHAUSTED] ב-log לחיפוש ב-Render logs.
            -- זו טבלה בעיקר ריקה — אם יש בה רשומות, יש בעיה לפתור.
            CREATE TABLE IF NOT EXISTS ledger_write_retry (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json    TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT DEFAULT '',
                last_attempt_at TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Web Push subscriptions — מנויי דפדפן של בעל העסק להתראות
            -- כשהדשבורד סגור. endpoint הוא ה-natural key (URL ייחודי לכל
            -- דפדפן). אין PII של משתמש קצה — רק מזהה דפדפן של הבעלים.
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint      TEXT    NOT NULL UNIQUE,
                p256dh        TEXT    NOT NULL,
                auth          TEXT    NOT NULL,
                user_agent    TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at  TIMESTAMP
            );

            -- מיתוג עסקי — לוגו לצריבה על QR Code (ובעתיד עוד מקומות).
            -- שורה בודדת (id=1) — אין צורך באיתור לפי key.
            CREATE TABLE IF NOT EXISTS business_branding (
                id              INTEGER PRIMARY KEY CHECK(id = 1),
                logo_blob       BLOB,
                logo_mime_type  TEXT,
                logo_uploaded_at TEXT
            );
            INSERT OR IGNORE INTO business_branding (id) VALUES (1);

            -- ── Customer Memory System — מערכת זיכרון מתמשך פר-לקוח ──
            -- שלב 1 של מערכת הזיכרון: שיחות מסתיימות → LLM extractor מחלץ
            -- עובדות יציבות → בשיחה הבאה ה-facts מוזרקים ל-context של הבוט.
            -- ראה docs/Customer-memory/claude_code_instructions.md.
            --
            -- business_id: המערכת single-tenant; השדה נשמר forward-compat
            -- ל-multi-tenant עתידי (ראה BUSINESS_ID ב-config.py). ב-runtime
            -- כל הקריאות משתמשות בקבוע 'default'.
            CREATE TABLE IF NOT EXISTS customer_facts (
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
            );

            -- פרופיל עסק לצרכי ה-extractor (סוג העסק, שירותים, "מה חשוב לחלץ").
            -- single-tenant: בפועל יש שורה אחת בעמודה business_id='default'.
            CREATE TABLE IF NOT EXISTS business_profile (
                business_id                     TEXT PRIMARY KEY,
                business_type                   TEXT DEFAULT '',
                business_name                   TEXT DEFAULT '',
                services_json                   TEXT DEFAULT '[]',
                what_matters_for_extraction     TEXT DEFAULT '',
                updated_at                      TEXT DEFAULT (datetime('now'))
            );

            -- audit log של ריצות extraction: כמה הודעות נסרקו, כמה facts יצאו,
            -- tokens, שגיאות. נכתב פעם אחת לכל ריצת background על user.
            CREATE TABLE IF NOT EXISTS extraction_runs (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                 TEXT NOT NULL,
                business_id             TEXT NOT NULL DEFAULT 'default',
                conversation_start      TEXT,
                conversation_end        TEXT,
                messages_count          INTEGER DEFAULT 0,
                extractions_count       INTEGER DEFAULT 0,
                skipped_count           INTEGER DEFAULT 0,
                status                  TEXT NOT NULL DEFAULT 'completed'
                                        CHECK(status IN ('running','completed','failed')),
                error_message           TEXT DEFAULT '',
                tokens_used             INTEGER DEFAULT 0,
                -- שלב 6.2: cursor id-based ל-scheduler. NULLable כדי לא לדרוס
                -- runs ישנים. ראה memory/background.py.
                last_message_id         INTEGER,
                created_at              TEXT DEFAULT (datetime('now'))
            );

            -- Create indexes
            CREATE INDEX IF NOT EXISTS idx_kb_entries_category ON kb_entries(category);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_entry ON kb_chunks(entry_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_user_created ON conversations(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_requests_status ON agent_requests(status);
            CREATE INDEX IF NOT EXISTS idx_conversation_summaries_user ON conversation_summaries(user_id);
            CREATE INDEX IF NOT EXISTS idx_live_chats_user_active ON live_chats(user_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_unanswered_questions_status ON unanswered_questions(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_special_days_date_unique ON special_days(date);
            CREATE INDEX IF NOT EXISTS idx_referral_codes_user ON referral_codes(user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(code);
            CREATE INDEX IF NOT EXISTS idx_credits_user ON credits(user_id);
            CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at);
            CREATE INDEX IF NOT EXISTS idx_broadcast_status ON broadcast_messages(status);
            CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_user
                ON broadcast_message_recipients(user_id);
            CREATE INDEX IF NOT EXISTS idx_lead_followups_status ON lead_followups(status);
            CREATE INDEX IF NOT EXISTS idx_lead_followups_due ON lead_followups(status, followup_due_at);
            CREATE INDEX IF NOT EXISTS idx_lead_followups_user ON lead_followups(user_id);
            CREATE INDEX IF NOT EXISTS idx_developer_reports_status ON developer_reports(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_identities_bsuid
                ON user_identities(whatsapp_bsuid) WHERE whatsapp_bsuid IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_user_identities_phone
                ON user_identities(phone_number) WHERE phone_number IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_wa_tpl_status
                ON whatsapp_templates(approval_status, language);
            CREATE INDEX IF NOT EXISTS idx_wa_tpl_name
                ON whatsapp_templates(friendly_name);
            CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_status
                ON broadcast_campaigns(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_template
                ON broadcast_campaigns(template_sid);
            CREATE INDEX IF NOT EXISTS idx_consent_ledger_subject
                ON consent_ledger(subject_hash, event_at);
            CREATE INDEX IF NOT EXISTS idx_consent_ledger_purge
                ON consent_ledger(category, event_at);

            -- Customer Memory indexes
            CREATE INDEX IF NOT EXISTS idx_customer_facts_user_business
                ON customer_facts(user_id, business_id, status);
            CREATE INDEX IF NOT EXISTS idx_customer_facts_status
                ON customer_facts(status);
            -- partial UNIQUE: dedup ברמת DB לטבלת facts פעילים (safety net
            -- מעל ה-dedup ברמת האפליקציה ב-memory/validator.py).
            -- CLAUDE.md: "לכל טבלה חדשה: לזהות מהו ה-natural key ולהוסיף UNIQUE".
            CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_facts_active_unique
                ON customer_facts(user_id, business_id, fact_type, content)
                WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_extraction_runs_user
                ON extraction_runs(user_id, created_at);
            -- שלב 6.2 — האינדקס idx_extraction_runs_user_msg_id מוגדר
            -- *רק* ב-migrations.py, לא כאן. הסיבה: ה-CREATE INDEX תלוי
            -- בעמודה last_message_id; ב-DB קיים (פרודקשן) שעדיין לא
            -- ביצע את ה-migration, ה-CREATE INDEX יקרוס כי העמודה
            -- אינה קיימת בטבלה הקיימת. ראה CLAUDE.md → "סדר הרצה של
            -- init_db מול migrations" — האינדקס שייך ל-migrations.py.
        """)

        # מיגרציות קלות — הלוגיקה בקובץ נפרד לקריאות טובה יותר
        from migrations import run_migrations
        run_migrations(conn)


def get_user_note(user_id: str) -> str:
    """קבלת הפתק של לקוח לפי user_id. מחזיר מחרוזת ריקה אם אין.

    מחזיר רק את ה-note_text החופשי (לא tags ולא withhold_reason). לקריאה
    מלאה של כל השדות — get_user_note_full.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT note FROM user_notes WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["note"] if row else ""


def get_user_note_full(user_id: str) -> dict:
    """קבלת כל שדות הפתק. מחזיר dict ריק אם אין.

    מבנה: {note: str, tags: list[str], withhold_reason: str, updated_at: str}.
    note_tags נשמר כ-JSON ב-DB; כאן הוא מפושטח לרשימת מחרוזות.
    """
    empty = {"note": "", "tags": [], "withhold_reason": "", "updated_at": ""}
    with get_connection() as conn:
        row = conn.execute(
            "SELECT note, note_tags, withhold_reason, updated_at "
            "FROM user_notes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return empty
        rd = dict(row)
        tags: list[str] = []
        try:
            raw_tags = rd.get("note_tags") or "[]"
            parsed = json.loads(raw_tags) if raw_tags else []
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed if t]
        except Exception:
            logger.error("get_user_note_full: כשל בפענוח tags", exc_info=True)
        return {
            "note": rd.get("note") or "",
            "tags": tags,
            "withhold_reason": rd.get("withhold_reason") or "",
            "updated_at": rd.get("updated_at") or "",
        }


def save_user_note(
    user_id: str,
    note: str,
    tags: list[str] | None = None,
    withhold_reason: str = "",
) -> None:
    """שמירה/עדכון פתק ללקוח.

    tags: רשימת תגיות סגורות (לא נחשפת בעיון משתמש; נשמרת כ-JSON).
    withhold_reason: אם מוגדר, ה-note_text *לא* ייחשף בעיון, רק ציון
    שקיימת הערה ושההיא הוסתרה. ברירת מחדל: ריק → ייחשף.

    אם note ו-tags ו-withhold_reason כולם ריקים — מוחקים את הרשומה.
    """
    note_clean = (note or "").strip()
    tags_list = [t.strip() for t in (tags or []) if t and t.strip()]
    tags_json = json.dumps(tags_list, ensure_ascii=False) if tags_list else "[]"
    withhold_clean = (withhold_reason or "").strip()

    with get_connection() as conn:
        if note_clean or tags_list or withhold_clean:
            conn.execute(
                """INSERT INTO user_notes (user_id, note, note_tags, withhold_reason, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET
                       note = excluded.note,
                       note_tags = excluded.note_tags,
                       withhold_reason = excluded.withhold_reason,
                       updated_at = datetime('now')""",
                (user_id, note_clean, tags_json, withhold_clean),
            )
        else:
            conn.execute("DELETE FROM user_notes WHERE user_id = ?", (user_id,))


# ── כרטיס לקוח (CRM-lite) ────────────────────────────────────────────────────
# הצגה מאחדת של מה שכבר יש ב-DB — בלי טבלה חדשה. מצרפים נתונים מ-users,
# user_identities, user_notes, ו-appointments. תיוג אוטומטי (חדש/חוזר/VIP/רדום)
# מחושב מ-appointments — לא נשמר, כדי שלא ייווצר staleness.


# ספי תיוג — קבועים פנימיים, ניתן להפוך להגדרות מאוחר יותר אם נצטרך
_LIFECYCLE_VIP_THRESHOLD = 10
_LIFECYCLE_RETURNING_THRESHOLD = 2
_DORMANT_DAYS_THRESHOLD = 60


def _compute_auto_tags(appt_count: int, last_visit_iso: str | None) -> list[str]:
    """תגיות אוטומטיות מחושבות מ-appointments (לא נשמרות ב-DB)."""
    tags: list[str] = []
    if appt_count == 0:
        # לקוח שמעולם לא קבע תור — לא מסמנים כ"חדש" בלי ביקור אחד לפחות
        return tags
    if appt_count == 1:
        tags.append("חדש")
    elif appt_count >= _LIFECYCLE_VIP_THRESHOLD:
        tags.append("VIP")
    elif appt_count >= _LIFECYCLE_RETURNING_THRESHOLD:
        tags.append("חוזר")
    if last_visit_iso:
        try:
            from datetime import date as _date
            last = _date.fromisoformat(last_visit_iso[:10])
            days = (_date.today() - last).days
            if days >= _DORMANT_DAYS_THRESHOLD:
                tags.append("רדום")
        except (ValueError, TypeError):
            pass
    return tags


def _customers_where_clause(search: str) -> tuple[str, list]:
    """בונה WHERE + params לסינון רשימת לקוחות. משותף ל-list_customers
    ול-count_customers כדי שלא תיווצר אי-עקביות בעימוד.

    משתמש ב-EXISTS על user_identities (במקום JOIN) כדי שלקוח עם זהויות
    בשני ערוצים (telegram + whatsapp) לא יופיע פעמיים בתוצאות, ולא ינפח
    את הספירה לעימוד.
    """
    where_parts = ["u.user_id IS NOT NULL"]
    params: list = []
    if search:
        like = f"%{search}%"
        where_parts.append(
            "(u.username LIKE ? OR u.user_id LIKE ? OR EXISTS ("
            "SELECT 1 FROM user_identities ui "
            "WHERE ui.user_id = u.user_id AND ui.phone_number LIKE ?))"
        )
        params.extend([like, like, like])
    return " AND ".join(where_parts), params


def list_customers(
    search: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """רשימת לקוחות לעמוד "לקוחות" — JOIN של users + appointments aggregations.

    מחזיר לכל לקוח: user_id, display_name, channel, phone, ספירת תורים,
    תאריך ביקור אחרון, תגיות אוטומטיות, וסימון אם יש הערה ידנית.

    search: חיפוש חופשי בשם/user_id/טלפון (LIKE על שלושת השדות).
    """
    where_sql, params = _customers_where_clause(search)

    with get_connection() as conn:
        # subquery לטלפון: לקוח עם זהויות ב-telegram וב-whatsapp לא יוכפל.
        # עקבי עם get_customer_card שמשתמש באותו דפוס.
        rows = conn.execute(f"""
            SELECT
                u.user_id,
                u.username,
                u.channel,
                u.last_active_at,
                u.first_seen_at,
                (SELECT phone_number FROM user_identities ui
                    WHERE ui.user_id = u.user_id
                    ORDER BY ui.updated_at DESC LIMIT 1) AS phone_number,
                (SELECT COUNT(*) FROM appointments a
                    WHERE a.user_id = u.user_id
                      AND a.status IN ('confirmed', 'passed')) AS appt_count,
                (SELECT MAX(preferred_date) FROM appointments a
                    WHERE a.user_id = u.user_id
                      AND a.status IN ('confirmed', 'passed')) AS last_appointment_date,
                (SELECT note FROM user_notes n WHERE n.user_id = u.user_id AND n.note != '') AS note_text,
                (SELECT note_tags FROM user_notes n WHERE n.user_id = u.user_id) AS manual_tags_json
            FROM users u
            WHERE {where_sql}
            ORDER BY u.last_active_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

    customers: list[dict] = []
    for r in rows:
        d = dict(r)
        # תגיות אוטומטיות + ידניות
        auto = _compute_auto_tags(
            int(d.get("appt_count") or 0), d.get("last_appointment_date"),
        )
        manual: list[str] = []
        try:
            raw = d.get("manual_tags_json") or "[]"
            parsed = json.loads(raw) if raw else []
            if isinstance(parsed, list):
                manual = [str(t) for t in parsed if t]
        except Exception:
            logger.error("list_customers: כשל בפענוח manual_tags", exc_info=True)
        d["auto_tags"] = auto
        d["manual_tags"] = manual
        # note_text מועבר לתבנית כדי שה-modal יציג את התוכן בלי קריאה נוספת.
        d["note_text"] = d.get("note_text") or ""
        d["has_note"] = bool(d["note_text"])
        d.pop("manual_tags_json", None)
        customers.append(d)
    return customers


def count_customers(search: str = "") -> int:
    """ספירה לצורך pagination — עקבי עם list_customers (חולקים את אותו WHERE).

    בלי JOIN ל-user_identities — ה-EXISTS ב-WHERE clause כבר מטפל בחיפוש
    טלפון. JOIN היה מנפח את הספירה אם לקוח קיים בשני ערוצים.
    """
    where_sql, params = _customers_where_clause(search)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT COUNT(*) AS c
            FROM users u
            WHERE {where_sql}
        """, params).fetchone()
        return int(row["c"]) if row else 0


def get_customer_card(user_id: str) -> dict | None:
    """כל המידע על לקוח אחד — לתצוגת "כרטיס לקוח".

    מחזיר None אם המשתמש לא קיים. מצרף: פרטי בסיס, היסטוריית תורים מלאה,
    שירותים שנבחרו (אגרגציה), הערה + תגיות ידניות, ותגיות אוטומטיות.
    """
    with get_connection() as conn:
        u = conn.execute(
            "SELECT user_id, username, channel, first_seen_at, last_active_at, "
            "       message_count, external_user_id "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not u:
            return None

        ident = conn.execute(
            "SELECT phone_number, username AS ident_username "
            "FROM user_identities WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        # @handle של טלגרם — לא נשמר ב-users אלא ב-appointments / agent_requests.
        # נבחר את העדכני ביותר מהשניים (UNION + ORDER) שיש בו ערך לא ריק.
        tg_handle_row = conn.execute(
            """SELECT telegram_username FROM (
                 SELECT telegram_username, created_at FROM appointments WHERE user_id = ?
                 UNION ALL
                 SELECT telegram_username, created_at FROM agent_requests WHERE user_id = ?
               ) WHERE telegram_username IS NOT NULL AND telegram_username != ''
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, user_id),
        ).fetchone()
        telegram_username = tg_handle_row["telegram_username"] if tg_handle_row else ""

        appts = conn.execute(
            """SELECT id, service, preferred_date, preferred_time, status, channel,
                      confirmed_duration_minutes, created_at
               FROM appointments
               WHERE user_id = ?
               ORDER BY preferred_date DESC, preferred_time DESC""",
            (user_id,),
        ).fetchall()

        services = conn.execute(
            """SELECT service, COUNT(*) AS c
               FROM appointments
               WHERE user_id = ? AND status IN ('confirmed', 'passed') AND service != ''
               GROUP BY service ORDER BY c DESC""",
            (user_id,),
        ).fetchall()

    note_full = get_user_note_full(user_id)
    appt_list = [dict(a) for a in appts]
    completed_count = sum(1 for a in appt_list if a["status"] in ("confirmed", "passed"))
    last_visit = next(
        (a["preferred_date"] for a in appt_list if a["status"] in ("confirmed", "passed")),
        None,
    )
    auto_tags = _compute_auto_tags(completed_count, last_visit)

    return {
        "user_id": u["user_id"],
        "display_name": u["username"] or (ident["ident_username"] if ident else "") or u["user_id"],
        "channel": u["channel"],
        "telegram_username": telegram_username,
        # מזהה raw של הספק — PSID ל-Messenger, IGSID ל-Instagram. ריק
        # ל-Telegram/WhatsApp שמשתמשים ב-user_id עצמו כמזהה ספק.
        "external_user_id": u["external_user_id"] or "",
        "first_seen_at": u["first_seen_at"],
        "last_active_at": u["last_active_at"],
        "message_count": u["message_count"],
        "phone_number": ident["phone_number"] if ident else None,
        "appointments": appt_list,
        "appt_count_confirmed": completed_count,
        "last_appointment_date": last_visit,
        "services_summary": [dict(s) for s in services],
        "note": note_full.get("note", ""),
        "manual_tags": note_full.get("tags", []),
        "withhold_reason": note_full.get("withhold_reason", ""),
        "auto_tags": auto_tags,
    }


def get_all_user_notes() -> dict[str, str]:
    """קבלת כל הפתקים כמילון {user_id: note}.

    מחזיר רק note_text — לא tags ולא withhold_reason — כי הקריאות הקיימות
    (UI אדמין) משתמשות בזה לתצוגה מהירה ביד אחת. לקריאה מלאה — לעבור
    דרך get_user_note_full לכל user_id.
    """
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, note FROM user_notes WHERE note != ''").fetchall()
        return {r["user_id"]: r["note"] for r in rows}


# ── חסימת משתמשים ────────────────────────────────────────────────────────────


VALID_BLOCK_CATEGORIES = ("abuse", "spam", "repeated_no_show", "manual")


def block_user(
    user_id: str,
    username: str = "",
    reason: str = "",
    category: str = "manual",
    appeal_contact: str = "",
) -> None:
    """חסימת משתמש. אם כבר חסום — מעדכן סיבה ושם.

    Args:
        category: enum סגור — אחת מ-VALID_BLOCK_CATEGORIES. ערך לא חוקי
                  יוחלף ב-'manual'. נחשף בעיון משתמש לפי תיקון 13 כדי
                  שיוכל לדעת למה הוא חסום ברמת קטגוריה.
        reason: backward-compat — אם נמסר, נשמר גם ב-`reason` הישן
                וגם ב-`block_reason_internal` החדש.
        appeal_contact: דרך לפנייה (מייל / טלפון של בעל העסק) למקרה
                       שהמשתמש רוצה לערער. נחשף בעיון.
    """
    if category not in VALID_BLOCK_CATEGORIES:
        category = "manual"
    reason_internal = reason.strip()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO blocked_users
                   (user_id, username, reason, block_category,
                    block_reason_internal, appeal_contact_method, blocked_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   username = excluded.username,
                   reason = excluded.reason,
                   block_category = excluded.block_category,
                   block_reason_internal = excluded.block_reason_internal,
                   appeal_contact_method = excluded.appeal_contact_method,
                   blocked_at = datetime('now')""",
            (user_id, username, reason_internal, category,
             reason_internal, appeal_contact.strip()),
        )


def unblock_user(user_id: str) -> None:
    """שחרור חסימת משתמש."""
    with get_connection() as conn:
        conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))


def is_user_blocked(user_id: str) -> bool:
    """בדיקה האם משתמש חסום."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def get_block_status_for_user(user_id: str) -> dict | None:
    """מידע על סטטוס חסימה של משתמש לעיון לפי תיקון 13.

    מחזיר רק שדות שניתן לחשוף: blocked_at (ברמת חודש), block_category
    (enum סגור), appeal_contact_method. שדות פנימיים (`reason`,
    `block_reason_internal`, `username`) לא מוחזרים — לא חושפים את מה
    שבעל העסק כתב על המשתמש.

    מחזיר None אם המשתמש לא חסום.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT blocked_at, block_category, appeal_contact_method "
            "FROM blocked_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    rd = dict(row)
    blocked_at = rd.get("blocked_at") or ""
    # רזולוציה לרמת חודש בלבד (YYYY-MM) — לא לחשוף תאריך מדויק
    blocked_month = blocked_at[:7] if blocked_at else ""
    return {
        "blocked_month": blocked_month,
        "block_category": rd.get("block_category") or "manual",
        "appeal_contact_method": rd.get("appeal_contact_method") or "",
    }


def get_blocked_users() -> list[dict]:
    """רשימת כל המשתמשים החסומים — לתצוגת אדמין."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, username, reason, block_category, "
            "block_reason_internal, appeal_contact_method, blocked_at "
            "FROM blocked_users ORDER BY blocked_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def end_expired_live_chats(max_hours: int = 4) -> int:
    """סגירת sessions שלא עודכנו במשך max_hours שעות.

    מחזיר את מספר ה-sessions שנסגרו.
    """
    with get_connection() as conn:
        expired = conn.execute(
            """SELECT COUNT(*) AS cnt FROM live_chats
               WHERE is_active = 1
                 AND datetime(COALESCE(updated_at, started_at), '+' || ? || ' hours') < datetime('now')""",
            (max_hours,),
        ).fetchone()["cnt"]
        if expired:
            conn.execute(
                """UPDATE live_chats SET is_active = 0, ended_at = datetime('now')
                   WHERE is_active = 1
                     AND datetime(COALESCE(updated_at, started_at), '+' || ? || ' hours') < datetime('now')""",
                (max_hours,),
            )
            logger.info("Auto-closed %d expired live chat session(s) (inactive > %d hours).", expired, max_hours)
        return expired


def cleanup_stale_live_chats():
    """Deactivate live chat sessions left over from a previous bot run.

    Called from the bot startup path only — not from init_db() — so that
    a bot-only restart doesn't silently end sessions still managed by
    the admin panel running in a separate process.
    """
    with get_connection() as conn:
        stale = conn.execute(
            "SELECT COUNT(*) AS cnt FROM live_chats WHERE is_active = 1"
        ).fetchone()["cnt"]
        if stale:
            conn.execute(
                "UPDATE live_chats SET is_active = 0, ended_at = datetime('now') WHERE is_active = 1"
            )
            logger.info("Cleaned up %d stale live chat session(s) from previous run.", stale)


# ─── Knowledge Base CRUD ─────────────────────────────────────────────────────

def add_kb_entry(category: str, title: str, content: str, metadata: dict = None) -> int:
    """Add a new knowledge base entry. Returns the entry ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO kb_entries (category, title, content, metadata) VALUES (?, ?, ?, ?)",
            (category, title, content, json.dumps(metadata or {}))
        )
        return cursor.lastrowid


def update_kb_entry(entry_id: int, category: str, title: str, content: str, metadata: dict = None):
    """Update an existing knowledge base entry."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE kb_entries 
               SET category=?, title=?, content=?, metadata=?, updated_at=datetime('now') 
               WHERE id=?""",
            (category, title, content, json.dumps(metadata or {}), entry_id)
        )


def delete_kb_entry(entry_id: int):
    """Delete a knowledge base entry and its chunks."""
    with get_connection() as conn:
        conn.execute("DELETE FROM kb_entries WHERE id=?", (entry_id,))


def get_kb_entry(entry_id: int) -> Optional[dict]:
    """Get a single KB entry by ID."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM kb_entries WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None


def get_all_kb_entries(category: str = None, active_only: bool = True) -> list[dict]:
    """Get all KB entries, optionally filtered by category."""
    with get_connection() as conn:
        query = "SELECT * FROM kb_entries WHERE 1=1"
        params = []
        if active_only:
            query += " AND is_active=1"
        if category:
            query += " AND category=?"
            params.append(category)
        query += " ORDER BY category, title"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_kb_categories() -> list[str]:
    """Get distinct categories from the knowledge base."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM kb_entries WHERE is_active=1 ORDER BY category"
        ).fetchall()
        return [r["category"] for r in rows]


def count_kb_entries(category: str | None = None, active_only: bool = True) -> int:
    """Count KB entries, optionally filtered by category."""
    with get_connection() as conn:
        query = "SELECT COUNT(*) AS count FROM kb_entries WHERE 1=1"
        params: list[object] = []
        if active_only:
            query += " AND is_active=1"
        if category:
            query += " AND category=?"
            params.append(category)
        row = conn.execute(query, params).fetchone()
        return int(row["count"]) if row else 0


def count_kb_categories(active_only: bool = True) -> int:
    """Count distinct KB categories."""
    with get_connection() as conn:
        if active_only:
            row = conn.execute(
                "SELECT COUNT(DISTINCT category) AS count FROM kb_entries WHERE is_active=1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(DISTINCT category) AS count FROM kb_entries"
            ).fetchone()
        return int(row["count"]) if row else 0


# ─── Chunks ──────────────────────────────────────────────────────────────────

def save_chunks(entry_id: int, chunks: list[dict]):
    """Save chunks for a KB entry (replaces existing chunks)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM kb_chunks WHERE entry_id=?", (entry_id,))
        conn.executemany(
            "INSERT INTO kb_chunks (entry_id, chunk_index, chunk_text, embedding) VALUES (?, ?, ?, ?)",
            [(entry_id, c["index"], c["text"], c.get("embedding")) for c in chunks],
        )


def get_all_chunks() -> list[dict]:
    """Get all chunks with their entry info for building the FAISS index."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.entry_id, c.chunk_index, c.chunk_text, c.embedding,
                   e.category, e.title
            FROM kb_chunks c
            JOIN kb_entries e ON c.entry_id = e.id
            WHERE e.is_active = 1
            ORDER BY c.id
        """).fetchall()
        return [dict(r) for r in rows]


def get_chunks_for_entries(entry_ids: list[int]) -> dict[int, list[dict]]:
    """Get existing chunks (with embeddings) grouped by entry_id.

    Only returns chunks whose embedding is not NULL, suitable for reuse
    during incremental index rebuilds.
    """
    if not entry_ids:
        return {}
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in entry_ids)
        rows = conn.execute(
            f"""SELECT c.id, c.entry_id, c.chunk_index, c.chunk_text, c.embedding,
                       e.category, e.title
                FROM kb_chunks c
                JOIN kb_entries e ON c.entry_id = e.id
                WHERE c.entry_id IN ({placeholders}) AND c.embedding IS NOT NULL
                ORDER BY c.entry_id, c.chunk_index""",
            entry_ids,
        ).fetchall()
        result: dict[int, list[dict]] = {}
        for r in rows:
            d = dict(r)
            result.setdefault(d["entry_id"], []).append(d)
        return result


# ─── Conversations ───────────────────────────────────────────────────────────

def save_message(user_id: str, username: str, role: str, message: str, sources: str = "", channel: str = "telegram"):
    """Save a conversation message."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (user_id, username, role, message, sources, channel) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, role, message, sources, channel)
        )


def get_conversation_history(user_id: str, limit: int = 20) -> list[dict]:
    """Get recent conversation history for a user.
    channel נכלל כדי שהתצוגה תוכל לפרסר WhatsApp markdown נכון.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT role, username, message, sources, created_at,
                      COALESCE(channel, 'telegram') AS channel
               FROM conversations WHERE user_id=?
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_all_conversations(limit: int = 100) -> list[dict]:
    """Get all conversations for the admin panel."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT user_id, username, role, message, sources, created_at,
                      COALESCE(channel, 'telegram') AS channel
               FROM conversations ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def user_exists(user_id: str) -> bool:
    """בדיקה קלה האם משתמש קיים — שאילתה אחת פשוטה.

    מיועד למקרים שצריך רק existence check (למשל הגנה ב-handlers שלא נכתוב
    הערות ל-user_id שהומצא), בלי להפעיל get_customer_card שמריץ אגרגציות
    כבדות על appointments / services / notes.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        return row is not None


def upsert_user(
    user_id: str,
    username: str = "",
    channel: str = "telegram",
    provider_asset_id: str = "",
    external_user_id: str = "",
) -> None:
    """עדכון/יצירת רשומת משתמש בטבלת users.

    נקרא בכל אינטראקציה — מעדכן last_active_at, מגדיל מונה הודעות,
    ושומר את שם המשתמש, channel, ופרטי provenance של המשתמש.

    טיעונים:
        user_id: המזהה הפנימי המנורמל (PK). ב-Meta: `meta_ig:<igsid>` /
            `meta_msg:<psid>`. ב-Telegram/WhatsApp: כמו היום (chat_id / טלפון).
        username: שם תצוגה (אופציונלי).
        channel: telegram / whatsapp / meta_ig / meta_msg.
        provider_asset_id: מזהה ה-asset של מטא (page_id ל-Messenger,
            ig_business_account_id ל-IG). ריק לערוצים אחרים.
        external_user_id: מזהה ה-raw של הספק (PSID/IGSID). ריק לערוצים
            שלא צריכים — Telegram/WhatsApp שומרים את ה-raw ב-user_id עצמו.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO users
                   (user_id, username, channel, provider_asset_id,
                    external_user_id, first_seen_at, last_active_at, message_count)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                   channel = excluded.channel,
                   -- שומרים על asset_id/external_id הקיימים אם הקריאה
                   -- החדשה ריקה (למשל קריאה ישנה מ-Telegram עם default '').
                   provider_asset_id = CASE WHEN excluded.provider_asset_id != ''
                                            THEN excluded.provider_asset_id
                                            ELSE users.provider_asset_id END,
                   external_user_id = CASE WHEN excluded.external_user_id != ''
                                           THEN excluded.external_user_id
                                           ELSE users.external_user_id END,
                   last_active_at = datetime('now'),
                   message_count = users.message_count + 1""",
            (user_id, username, channel, provider_asset_id, external_user_id),
        )


# ─── Consent (תיקון 13 לחוק הגנת הפרטיות) ───────────────────────────────────

CURRENT_CONSENT_VERSION = 2
"""גרסת המסמכים הנוכחית. הגדלת ערך זה גורמת להצגת מסך הסכמה מחדש לכל המשתמשים
הקיימים (consent_version בDB יהיה נמוך מהערך הזה ⇒ has_consent יחזיר False).

היסטוריה:
v1 → v2 (2026-05): מסך ההסכמה שודרג עם אישור גיל מפורש (18+) ואזכור
מודגש של עיבוד AI ושל העברה לספקי AI בחו"ל. גרסה זו תואמת את
המלצת היועץ החיצוני על "רמה 1: שירות חובה" שכוללת אימות גיל וכשירות
כחלק מההסכמה לשירות הליבה (לא בנפרד)."""


def has_consent(user_id: str) -> bool:
    """בדיקה אם המשתמש נתן הסכמה תקפה למדיניות הפרטיות הנוכחית.

    מחזיר False אם המשתמש לא ברשומות, או אם הסכמתו היא לגרסה ישנה
    מהמסמכים (לפני עדכון מהותי שדורש הסכמה מחדש).

    כש-CONSENT_SCREEN_ENABLED=false (ברירת מחדל): השער פתוח — מחזירים True
    בלי לכתוב כלום ל-DB. שדה consent_given_at יישאר NULL כדי לא לזייף הסכמה
    שהמשתמש לא נתן בפועל.
    """
    # ייבוא מאוחר — config עלול להיטען אחרי database במקרים מסוימים
    try:
        from ai_chatbot.config import CONSENT_SCREEN_ENABLED
    except ImportError:
        CONSENT_SCREEN_ENABLED = False
    if not CONSENT_SCREEN_ENABLED:
        return True

    with get_connection() as conn:
        row = conn.execute(
            "SELECT consent_given_at, consent_version FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return False
        if not row["consent_given_at"]:
            return False
        return int(row["consent_version"] or 0) >= CURRENT_CONSENT_VERSION


def record_consent(user_id: str, username: str = "", channel: str = "telegram") -> None:
    """תיעוד הסכמה — שומר טיימסטמפ + גרסת המסמכים בשורת המשתמש,
    וכותב הוכחה פסאודונימית ל-consent_ledger (לשמירה אחרי /forget).

    יוצר את שורת המשתמש אם לא קיימת (כי לפעמים ההסכמה היא הפעולה הראשונה).
    """
    with get_connection() as conn:
        # זיהוי האם זו הסכמה ראשונה או superseded (עליית גרסה)
        existing = conn.execute(
            "SELECT consent_version FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        prior_version = int(dict(existing).get("consent_version") or 0) if existing else 0

        conn.execute(
            """INSERT INTO users (user_id, username, channel, first_seen_at, last_active_at,
                                  message_count, consent_given_at, consent_version)
               VALUES (?, ?, ?, datetime('now'), datetime('now'), 0,
                       datetime('now'), ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   consent_given_at = datetime('now'),
                   consent_version = excluded.consent_version""",
            (user_id, username, channel, CURRENT_CONSENT_VERSION),
        )

    # ledger event — אם prior_version > 0 וקטן מהנוכחי, זה superseded;
    # אחרת זו הסכמה ראשונה. הקריאה לא חוסמת את שמירת ה-consent עצמה.
    try:
        from utils.consent_ledger import (
            record_consent_event,
            EVENT_CONSENT_GIVEN,
            EVENT_CONSENT_SUPERSEDED,
        )
        event = (
            EVENT_CONSENT_SUPERSEDED
            if 0 < prior_version < CURRENT_CONSENT_VERSION
            else EVENT_CONSENT_GIVEN
        )
        record_consent_event(
            user_id=user_id,
            channel=channel,
            event_type=event,
            consent_version=CURRENT_CONSENT_VERSION,
            metadata={"prior_version": prior_version} if prior_version else None,
        )
    except Exception:
        logger.error("record_consent: כשל בכתיבה ל-consent_ledger", exc_info=True)


def revoke_consent(user_id: str, channel: str = "") -> None:
    """ביטול הסכמה — נקרא במחיקת מידע משתמש (/forget). הופך has_consent ל-False.
    אינו מוחק את שורת המשתמש כדי לתעד שהיתה הסכמה בעבר ובוטלה.

    כותב הוכחה ל-consent_ledger (event_type=consent_revoked). channel
    אופציונלי — אם לא סופק, נשלף משורת המשתמש (אם קיימת).
    """
    actual_channel = channel
    with get_connection() as conn:
        if not actual_channel:
            row = conn.execute(
                "SELECT channel FROM users WHERE user_id = ?", (user_id,),
            ).fetchone()
            if row:
                actual_channel = dict(row).get("channel") or "telegram"
            else:
                actual_channel = "telegram"  # fallback סביר

        conn.execute(
            "UPDATE users SET consent_given_at = NULL, consent_version = 0 WHERE user_id = ?",
            (user_id,),
        )

    try:
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_REVOKED
        record_consent_event(
            user_id=user_id,
            channel=actual_channel,
            event_type=EVENT_CONSENT_REVOKED,
        )
    except Exception:
        logger.error("revoke_consent: כשל בכתיבה ל-consent_ledger", exc_info=True)


# ─── User Data Rights (זכות עיון + זכות מחיקה) ──────────────────────────────


def get_user_data_summary(user_id: str) -> dict:
    """החזרת סיכום של כל המידע השמור על משתמש — לזכות עיון (/myinfo).

    מחזיר dict עם counts + דוגמאות אחרונות. לא חושף PII של משתמשים אחרים.
    אם המשתמש לא קיים — מחזיר dict ריק עם המבנה הצפוי.
    """
    summary: dict = {
        "user_id": user_id,
        "exists": False,
        "username": "",
        "channel": "",
        "first_seen_at": "",
        "last_active_at": "",
        "message_count": 0,
        "consent_given_at": "",
        "appointments": {"total": 0, "by_status": {}},
        "live_chats_total": 0,
        "agent_requests_total": 0,
        "subscribed": False,
        "has_user_note": False,
        "user_note_text": "",          # תוכן ההערה (אם לא withheld)
        "user_note_withheld": False,   # קיימת הערה אבל לא נחשפה (סיבה פנימית)
        "blocked": False,              # האם המשתמש חסום (חשיפה חלקית)
        "block_status": None,          # אם חסום: dict עם blocked_month, category, appeal_contact
        # נוספו לצורך זכות עיון מלאה לפי תיקון 13 — counts בלבד, ללא חשיפת
        # תוכן חופשי (האם לחשוף תוכן של user_notes / lead_followups עדיין
        # בבדיקה משפטית — ראה docs/privacy_data_matrix.md)
        "conversations_total": 0,
        "conversation_summaries_total": 0,
        "unanswered_questions_total": 0,
        "lead_followups": {"total": 0, "by_status": {}},
        "referrals_as_referrer_total": 0,
        "referrals_as_referred_total": 0,
        "has_referral_code": False,
        "credits": {"total": 0, "active": 0},
        "response_pages_total": 0,
        "broadcast_deliveries_total": 0,
        "identities_total": 0,
    }
    with get_connection() as conn:
        u = conn.execute(
            """SELECT username, channel, first_seen_at, last_active_at,
                      message_count, consent_given_at
               FROM users WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
        if not u:
            return summary
        u_dict = dict(u)
        summary.update({
            "exists": True,
            "username": u_dict.get("username") or "",
            "channel": u_dict.get("channel") or "",
            "first_seen_at": u_dict.get("first_seen_at") or "",
            "last_active_at": u_dict.get("last_active_at") or "",
            "message_count": int(u_dict.get("message_count") or 0),
            "consent_given_at": u_dict.get("consent_given_at") or "",
        })

        # תורים — סיכום לפי סטטוס
        appt_rows = conn.execute(
            """SELECT status, COUNT(*) AS cnt
               FROM appointments WHERE user_id = ? GROUP BY status""",
            (user_id,),
        ).fetchall()
        by_status: dict = {}
        total_appts = 0
        for r in appt_rows:
            cnt = int(dict(r).get("cnt") or 0)
            by_status[dict(r).get("status") or "unknown"] = cnt
            total_appts += cnt
        summary["appointments"] = {"total": total_appts, "by_status": by_status}

        # שיחות נציג, בקשות נציג, מנוי, פתק — counts פשוטים
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM live_chats WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["live_chats_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-live_chats", exc_info=True)

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM agent_requests WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["agent_requests_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-agent_requests", exc_info=True)

        try:
            row = conn.execute(
                "SELECT 1 FROM user_subscriptions WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            summary["subscribed"] = bool(row)
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-user_subscriptions", exc_info=True)

        # user_notes — לפי המלצת היועץ (תיקון 13), ה-note_text נחשף בעיון
        # אלא אם מוגדר withhold_reason. tags סגורות לא נחשפות בכל מקרה.
        # has_user_note נשמר ל-backward compat (משתמש ב-template הישן).
        try:
            row = conn.execute(
                "SELECT note, withhold_reason FROM user_notes WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                rd = dict(row)
                note_text = rd.get("note") or ""
                withhold = rd.get("withhold_reason") or ""
                summary["has_user_note"] = bool(note_text or withhold)
                if note_text and not withhold:
                    summary["user_note_text"] = note_text
                elif withhold:
                    # נחשף שקיימת הערה אבל לא תוכנה — שקיפות בלי חשיפת הסיבה
                    # הפנימית. הסיבה הפנימית בעצמה היא "פנימית" ולא תיחשף.
                    summary["user_note_withheld"] = True
        except Exception:
            # טבלה user_notes עשויה לא להתקיים בכל ה-deployments — לא קריטי
            # ל-summary, אבל עדיין מתעדים כדי לא להפר את כלל "אין except: pass שקט".
            logger.error("get_user_data_summary: שגיאה ב-user_notes", exc_info=True)

        # ─── הרחבה לזכות עיון מלאה (תיקון 13) ────────────────────────────
        # counts לכל הטבלאות הנוספות שמכילות user_id, כדי שמשתמש שמבקש
        # /myinfo יראה שיש מידע נוסף עליו במערכת. תוכן חופשי לא נחשף כאן —
        # רק מטא-נתונים מינימליים.

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM conversations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["conversations_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-conversations", exc_info=True)

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM conversation_summaries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["conversation_summaries_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error(
                "get_user_data_summary: שגיאה ב-conversation_summaries", exc_info=True
            )

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM unanswered_questions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["unanswered_questions_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error(
                "get_user_data_summary: שגיאה ב-unanswered_questions", exc_info=True
            )

        # lead_followups — total + פילוח לפי status. תיקון 13 דורש שקיפות על
        # קבלת החלטות אוטומטית; שדה analysis_json (סיווגי AI) לא נחשף כאן —
        # החשיפה המלאה ממתינה להחלטה משפטית (ראה matrix).
        try:
            lf_rows = conn.execute(
                """SELECT status, COUNT(*) AS cnt FROM lead_followups
                   WHERE user_id = ? GROUP BY status""",
                (user_id,),
            ).fetchall()
            lf_by_status: dict = {}
            lf_total = 0
            for r in lf_rows:
                cnt = int(dict(r).get("cnt") or 0)
                lf_by_status[dict(r).get("status") or "unknown"] = cnt
                lf_total += cnt
            summary["lead_followups"] = {"total": lf_total, "by_status": lf_by_status}
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-lead_followups", exc_info=True)

        # referrals — שני צדדים נספרים בנפרד כדי שהמשתמש יראה גם הפניות
        # שעשה וגם הפניות שדרכן הצטרף.
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ?",
                (user_id,),
            ).fetchone()
            summary["referrals_as_referrer_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error(
                "get_user_data_summary: שגיאה ב-referrals (referrer)", exc_info=True
            )

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM referrals WHERE referred_id = ?",
                (user_id,),
            ).fetchone()
            summary["referrals_as_referred_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error(
                "get_user_data_summary: שגיאה ב-referrals (referred)", exc_info=True
            )

        try:
            row = conn.execute(
                "SELECT 1 FROM referral_codes WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            summary["has_referral_code"] = bool(row)
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-referral_codes", exc_info=True)

        # credits — סך הכל + פעיל (לא expired ולא used). השדה reason הוא
        # טקסט חופשי ולא נחשף כאן.
        try:
            row = conn.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN used = 0
                                 AND (expires_at = '' OR expires_at >= datetime('now'))
                                THEN 1 ELSE 0 END) AS active
                   FROM credits WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            if row:
                rd = dict(row)
                summary["credits"] = {
                    "total": int(rd.get("total") or 0),
                    "active": int(rd.get("active") or 0),
                }
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-credits", exc_info=True)

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM response_pages WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["response_pages_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-response_pages", exc_info=True)

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM broadcast_deliveries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["broadcast_deliveries_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            # broadcast_deliveries אופציונלי בחלק מה-deployments
            logger.error(
                "get_user_data_summary: שגיאה ב-broadcast_deliveries", exc_info=True
            )

        # blocked_users — חשיפה חלקית לפי תיקון 13. למשתמש מותר לדעת שהוא
        # חסום (זכות עיון), אבל לא את ה-reason הפנימי שכתב בעל העסק.
        try:
            block_status = get_block_status_for_user(user_id)
            if block_status:
                summary["blocked"] = True
                summary["block_status"] = block_status
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-blocked_users", exc_info=True)

        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM user_identities WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary["identities_total"] = int(dict(row).get("cnt") or 0) if row else 0
        except Exception:
            logger.error("get_user_data_summary: שגיאה ב-user_identities", exc_info=True)

    return summary


# Idempotency map ל-/forget — מונע double-write ל-ledger כש-Telegram
# מעביר את אותה callback פעמיים בכשל רשת, או כשמשתמש לוחץ פעמיים.
# in-process dict מספיק ל-instance בודד ב-Render; אם נפצל ל-multiple
# workers, להחליף ב-Redis. TTL: 60 שניות (מספיק לחפיפה של callbacks
# כפולים, קצר מספיק שלא יחסום בקשת re-delete לגיטימית).
_DELETION_IDEMPOTENCY_TTL_SECONDS = 60
_DELETION_IDEMPOTENCY_MAX_TRACKED = 5_000
# מפתח: (tenant, user_id) — מחיקה אצל עסק אחד אינה חוסמת מחיקה אצל אחר.
_active_deletions: dict[tuple[str, str], float] = {}
_active_deletions_lock = threading.Lock()


def _deletion_key(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)


def _is_deletion_in_progress(user_id: str) -> bool:
    """בודק אם בקשת /forget עבור user_id הזה פעילה כעת. אם כן — חוסם.
    גם מנקה רשומות ישנות מהמפה (lazy cleanup)."""
    import time
    now = time.time()
    cutoff = now - _DELETION_IDEMPOTENCY_TTL_SECONDS
    key = _deletion_key(user_id)
    with _active_deletions_lock:
        # cleanup רשומות ישנות (על פני כל ה-tenants)
        stale = [k for k, ts in _active_deletions.items() if ts < cutoff]
        for k in stale:
            del _active_deletions[k]
        # בדיקה
        return key in _active_deletions


def _mark_deletion_in_progress(user_id: str) -> None:
    """רושם user_id כמחיקה פעילה. כולל LRU eviction."""
    import time
    key = _deletion_key(user_id)
    with _active_deletions_lock:
        _active_deletions[key] = time.time()
        # LRU eviction אם המפה גדלה מדי
        if len(_active_deletions) > _DELETION_IDEMPOTENCY_MAX_TRACKED:
            oldest = min(_active_deletions.items(), key=lambda kv: kv[1])[0]
            if oldest != key:
                del _active_deletions[oldest]


def _clear_deletion_in_progress(user_id: str) -> None:
    with _active_deletions_lock:
        _active_deletions.pop(_deletion_key(user_id), None)


def delete_user_data(user_id: str) -> dict:
    """מחיקה מלאה של מידע משתמש — לזכות מחיקה (/forget).

    מוחק רשומות מכל הטבלאות הרלוונטיות. תורים שכבר עברו ושסטטוסם 'passed'/'cancelled'
    נמחקים גם — לא נשמרים מסיבה חוקית כשהמשתמש ביקש מחיקה מפורשת.

    מחזיר dict:
    - {"already_in_progress": True} אם בקשה זהה פעילה כעת (idempotency).
    - אחרת dict עם counts לכל טבלה.

    כותב ל-consent_ledger:
    - deletion_requested לפני המחיקה.
    - deletion_completed עם metadata.status=full|partial אם משהו נמחק
      (כולל אם חלק מהטבלאות נכשלו — failed_tables ב-metadata).
    - deletion_failed אם כלום לא נמחק (כל הטבלאות נכשלו או הDB ריק
      ומשהו בכל זאת זרק חריגה).
    אירועי audit (retention 24 חודשים).
    """
    # idempotency check — מונע double-processing של callbacks כפולים
    if _is_deletion_in_progress(user_id):
        logger.info("delete_user_data: user=%s — בקשה כבר בעיבוד, מדלגים", user_id)
        return {"already_in_progress": True}
    _mark_deletion_in_progress(user_id)

    try:
        return _delete_user_data_impl(user_id)
    finally:
        _clear_deletion_in_progress(user_id)


def _delete_user_data_impl(user_id: str) -> dict:
    """המימוש בפועל — מופרד כדי שה-finally של idempotency יעטוף הכול.

    מחזיר dict עם 2 סוגי מפתחות (ראה גם _result_total_count + deletion_status):
      - שמות טבלה (str) → rowcount (int): "users", "conversations", וכד'.
      - מפתחות מטא עם dunder prefix (str) → ערכים מורכבים:
          * "__failed_tables__" → list[str] (רק אם היו כשלים)
          * "__deletion_status__" → "full" / "partial" / "failed"
          * "already_in_progress" → True (idempotency hit, מחזיר רק את זה)

    הפרדת המטא מ-counts הפנימי מבטיחה שה-dict הפנימי נשאר טהור (כל
    הערכים int) ולא נשבר אם מישהו עושה sum() עליו. הקוראים החיצוניים
    חייבים להעביר דרך _result_total_count() כדי לקבל סכום נכון, כי
    ה-return הסופי מערב מטא.
    """
    counts: dict[str, int] = {}  # טהור: שם טבלה → rowcount בלבד
    failed_tables: list[str] = []  # שמות הטבלאות שזרקו חריגה
    errors: list[str] = []  # תיאור הכשלים (ללא PII)

    # שליפת channel *לפני* המחיקה — כי שורת users תימחק בסוף.
    user_channel = "telegram"
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT channel FROM users WHERE user_id = ?", (user_id,),
            ).fetchone()
            if row:
                user_channel = dict(row).get("channel") or "telegram"
    except Exception:
        logger.error("delete_user_data: כשל בשליפת channel", exc_info=True)

    # ledger: deletion_requested (לפני המחיקה — עקבות גם אם נכשל באמצע)
    try:
        from utils.consent_ledger import record_consent_event, EVENT_DELETION_REQUESTED
        record_consent_event(
            user_id=user_id, channel=user_channel,
            event_type=EVENT_DELETION_REQUESTED,
        )
    except Exception:
        logger.error("delete_user_data: כשל ב-deletion_requested", exc_info=True)

    # blocked_users — לפי המלצת היועץ (תיקון 13): שמירה ממוזערת אחרי /forget.
    # ה-row לא נמחק (אינטרס לגיטימי לאכיפה — מניעת עקיפת חסימה ע"י
    # הרשמה מחדש), אבל שדות PII רלוונטיים (`username`, `reason`,
    # `block_reason_internal`) מתאפסים. רק `user_id` + `block_category`
    # + `blocked_at` + `appeal_contact_method` נשארים — מינימום נדרש לאכיפה.
    try:
        with get_connection() as conn:
            conn.execute(
                """UPDATE blocked_users
                   SET username = '', reason = '', block_reason_internal = ''
                   WHERE user_id = ?""",
                (user_id,),
            )
    except Exception:
        logger.error("delete_user_data: minimization של blocked_users נכשל", exc_info=True)

    # רשימת טבלאות שמכילות user_id ומידע אישי. blocked_users מושמט בכוונה —
    # אם משתמש נחסם, מחיקה והרשמה מחדש לא צריכה לעקוף את החסימה.
    tables_user_id = [
        "appointments",
        "conversations",
        "live_chats",
        "agent_requests",
        "user_subscriptions",
        "referral_codes",
        "user_notes",
        "user_identities",
        "conversation_summaries",
        "unanswered_questions",
        "credits",
        "lead_followups",
        "response_pages",
        "broadcast_message_recipients",
        # Customer Memory System (שלב 1) — נגזרות AI על המשתמש, חייבות
        # להימחק ב-/forget (תיקון 13). business_profile לא ברשימה כי
        # היא per-business, לא per-user.
        "customer_facts",
        "extraction_runs",
    ]

    def _safe_delete(label: str, sql: str, params: tuple) -> None:
        """מבצע DELETE עם try/except אחיד; כישלון בטבלה אחת לא חוסם אחרות.
        רושם failed_tables כדי שנדע לסווג את ה-event_type הסופי."""
        try:
            cur = conn.execute(sql, params)
            if cur.rowcount:
                counts[label] = cur.rowcount
        except Exception as exc:
            failed_tables.append(label)
            errors.append(f"{label}: {type(exc).__name__}")
            logger.error("delete_user_data: שגיאה ב-%s", label, exc_info=True)

    with get_connection() as conn:
        for table in tables_user_id:
            _safe_delete(
                table, f"DELETE FROM {table} WHERE user_id = ?", (user_id,),
            )

        # referrals — שני צדדים אפשריים
        _safe_delete(
            "referrals",
            "DELETE FROM referrals WHERE referrer_id = ? OR referred_id = ?",
            (user_id, user_id),
        )

        # broadcast_deliveries — אופציונלי בחלק מה-deployments
        _safe_delete(
            "broadcast_deliveries",
            "DELETE FROM broadcast_deliveries WHERE user_id = ?",
            (user_id,),
        )

        # users עצמו — בסוף, אחרי שכל ההפניות נמחקו
        _safe_delete(
            "users", "DELETE FROM users WHERE user_id = ?", (user_id,),
        )

    logger.info(
        "delete_user_data: user=%s, counts=%s, failed_tables=%s",
        user_id, counts, failed_tables,
    )

    # סיווג event_type סופי לפי תוצאת המחיקה:
    # - deletion_failed: כלום לא נמחק AND היו כשלים → ההוכחה שביצענו לא קיימת,
    #   חייבים event_type שונה כדי שלא יראו בטעות "מחיקה הושלמה".
    # - deletion_completed status=partial: חלק נמחק, חלק נכשל.
    # - deletion_completed status=full: הכול הצליח (גם אם counts ריק כי DB
    #   היה ריק מלכתחילה — זה מצב לגיטימי, לא כשל).
    try:
        from utils.consent_ledger import (
            record_consent_event,
            EVENT_DELETION_COMPLETED,
            EVENT_DELETION_FAILED,
        )
        if not counts and failed_tables:
            # כשל מלא — ה-event_type חייב להבדיל מ-completed
            record_consent_event(
                user_id=user_id, channel=user_channel,
                event_type=EVENT_DELETION_FAILED,
                metadata={
                    "counts": {},
                    "failed_tables": failed_tables,
                    "errors": errors,
                },
            )
        else:
            status = "partial" if failed_tables else "full"
            metadata: dict = {
                "status": status,
                "counts": counts,
                "total": sum(counts.values()),
            }
            if failed_tables:
                metadata["failed_tables"] = failed_tables
                metadata["errors"] = errors
            record_consent_event(
                user_id=user_id, channel=user_channel,
                event_type=EVENT_DELETION_COMPLETED,
                metadata=metadata,
            )
    except Exception:
        logger.error("delete_user_data: כשל ברישום event_type סופי ל-ledger", exc_info=True)

    # בניית ה-dict המוחזר. counts נשאר טהור (dict[str, int] לכל אורך
    # הפונקציה); רק ה-result הסופי מערב מטא מסוג אחר. ההפרדה הזאת
    # מבטיחה שאם מישהו בעתיד יעשה sum(internal_counts.values()) הוא
    # לא יקבל TypeError. כל הקוראים מבחוץ חייבים _result_total_count.
    result: dict[str, object] = dict(counts)
    if failed_tables:
        result["__failed_tables__"] = list(failed_tables)
        result["__deletion_status__"] = "partial" if counts else "failed"

    return result


def _result_total_count(result: dict[str, object]) -> int:
    """API ציבורי לחישוב סך הרשומות שנמחקו מתוצאת delete_user_data.

    הכרחי כי ה-dict מערב 2 סוגי ערכים (int לטבלאות, list/str למטא).
    כל קוד חיצוני שצריך סכום *חייב* להשתמש בזה — sum(result.values())
    יזרוק TypeError אם יש __failed_tables__.

    מתעלם בבירור מ:
      - מפתחות עם dunder prefix (__failed_tables__, __deletion_status__)
      - ערכים שאינם int (גנרי, מגן גם מ-edge cases עתידיים)
      - already_in_progress (boolean)
    """
    return sum(
        v for k, v in result.items()
        if not k.startswith("__")  # dunder prefix משמש למטא
        and isinstance(v, int)
        and not isinstance(v, bool)  # bool הוא subclass של int ב-Python
    )


def deletion_status(result: dict[str, object]) -> str:
    """API ציבורי: 'full' / 'partial' / 'failed' / 'already_in_progress'.

    הכרחי כדי לא להניח דברים על המבנה הפנימי של result.
    """
    if result.get("already_in_progress"):
        return "already_in_progress"
    raw = result.get("__deletion_status__")
    return raw if isinstance(raw, str) else "full"


# ─── Retention purge (מחיקה אוטומטית לפי תקופות שמירה) ───────────────────────


def purge_old_data(
    conversations_months: int = 12,
    closed_appointments_months: int = 36,
    closed_live_chats_months: int = 12,
    agent_requests_months: int = 12,
    unanswered_open_days: int = 90,
    unanswered_resolved_months: int = 6,
    lead_followups_months: int = 6,
    referrals_completed_months: int = 24,
    referrals_pending_months: int = 6,
    credits_expired_months: int = 12,
    credits_used_months: int = 24,
    broadcast_deliveries_months: int = 12,
    broadcast_deliveries_failed_months: int = 18,
    response_pages_days: int = 30,
    ledger_consent_years: int = 5,
    ledger_audit_months: int = 24,
) -> dict:
    """מחיקה אוטומטית של נתונים ישנים — מתבצעת ב-Job יומי.

    תקופות שמירה תפעוליות (לא "מספר רשמי" מהחוק — עיקרון "לא יותר
    מהנדרש למטרה"). תיעוד מלא: docs/privacy_data_matrix.md.

    נמחקים:
    - conversations: 12 חודשים מ-created_at
    - appointments: 36 חודשים — רק passed/cancelled (היסטוריה חשבונאית)
    - live_chats סגורים: 12 חודשים מ-ended_at
    - conversation_summaries: סנכרון עם conversations (אם המקור נמחק,
      גם הסיכום — מסקנה על אדם בלי מקור היא חשיפה מיותרת)
    - agent_requests: 12 חודשים מ-handled_at או created_at אם pending
    - unanswered_questions: open → 90 יום, resolved → 6 חודשים
    - lead_followups: 6 חודשים מהאירוע האחרון (sent/replied/expired)
    - referrals: completed → 24 חודשים, pending → 6 חודשים (expire)
    - credits: expired → 12 חודשים, used → 24 חודשים, active נשארים
    - broadcast_deliveries: רגילים → 12 חודשים, failed → 18 (דיבוג)
    - response_pages: 30 יום (cache לעמוד ציבורי, לא היסטוריה)

    אם משנים מספרים — לעדכן גם privacy.md ו-privacy_data_matrix.md.
    """
    counts: dict[str, int] = {}

    def _safe_purge(table_label: str, sql: str, params: tuple) -> None:
        """מבצע DELETE עם try/except אחיד; כישלון בטבלה אחת לא חוסם אחרות."""
        try:
            cur = conn.execute(sql, params)
            counts[table_label] = cur.rowcount or 0
        except Exception:
            logger.error("purge_old_data: שגיאה ב-%s", table_label, exc_info=True)
            counts[table_label] = 0

    with get_connection() as conn:
        _safe_purge(
            "conversations",
            "DELETE FROM conversations WHERE created_at < datetime('now', ?)",
            (f"-{int(conversations_months)} months",),
        )
        _safe_purge(
            "appointments",
            "DELETE FROM appointments "
            "WHERE status IN ('passed', 'cancelled') "
            "AND preferred_date != '' "
            "AND preferred_date < date('now', ?)",
            (f"-{int(closed_appointments_months)} months",),
        )
        _safe_purge(
            "live_chats",
            "DELETE FROM live_chats "
            "WHERE is_active = 0 AND ended_at IS NOT NULL "
            "AND ended_at < datetime('now', ?)",
            (f"-{int(closed_live_chats_months)} months",),
        )
        _safe_purge(
            "conversation_summaries",
            "DELETE FROM conversation_summaries WHERE created_at < datetime('now', ?)",
            (f"-{int(conversations_months)} months",),
        )

        # agent_requests — handled_at אם קיים, אחרת created_at.
        # NULLIF(field, '') הכרחי כי SQLite COALESCE בודק רק NULL ולא ''.
        # בלעדיו, רשומות עם handled_at='' (legacy / migration) היו נמחקות
        # מיידית כי '' < datetime(...) תמיד true ב-string compare.
        _safe_purge(
            "agent_requests",
            "DELETE FROM agent_requests "
            "WHERE COALESCE(NULLIF(handled_at, ''), created_at) < datetime('now', ?)",
            (f"-{int(agent_requests_months)} months",),
        )

        # unanswered_questions — חוק תנאי לפי status
        _safe_purge(
            "unanswered_questions_open",
            "DELETE FROM unanswered_questions "
            "WHERE status = 'open' AND created_at < datetime('now', ?)",
            (f"-{int(unanswered_open_days)} days",),
        )
        _safe_purge(
            "unanswered_questions_resolved",
            "DELETE FROM unanswered_questions "
            "WHERE status IN ('resolved', 'not_relevant') "
            "AND COALESCE(NULLIF(resolved_at, ''), created_at) < datetime('now', ?)",
            (f"-{int(unanswered_resolved_months)} months",),
        )

        # lead_followups — מבוסס על האירוע האחרון של הרשומה.
        # NULLIF הכרחי לכל השדות כי הם TEXT שיכולים להיות '' (לא רק NULL).
        _safe_purge(
            "lead_followups",
            "DELETE FROM lead_followups "
            "WHERE COALESCE("
            "    NULLIF(user_replied_at, ''),"
            "    NULLIF(followup_sent_at, ''),"
            "    NULLIF(followup_due_at, ''),"
            "    created_at"
            ") < datetime('now', ?)",
            (f"-{int(lead_followups_months)} months",),
        )

        # referrals — completed לפי completed_at, pending נחשב פג אם ישן
        _safe_purge(
            "referrals_completed",
            "DELETE FROM referrals "
            "WHERE status = 'completed' "
            "AND COALESCE(NULLIF(completed_at, ''), created_at) < datetime('now', ?)",
            (f"-{int(referrals_completed_months)} months",),
        )
        _safe_purge(
            "referrals_pending",
            "DELETE FROM referrals "
            "WHERE status = 'pending' AND created_at < datetime('now', ?)",
            (f"-{int(referrals_pending_months)} months",),
        )

        # credits — expired לפי expires_at, used לפי created_at
        _safe_purge(
            "credits_expired",
            "DELETE FROM credits "
            "WHERE used = 0 AND expires_at != '' AND expires_at < datetime('now', ?)",
            (f"-{int(credits_expired_months)} months",),
        )
        _safe_purge(
            "credits_used",
            "DELETE FROM credits "
            "WHERE used = 1 AND created_at < datetime('now', ?)",
            (f"-{int(credits_used_months)} months",),
        )

        # broadcast_deliveries — failed שומרים יותר לדיבוג
        _safe_purge(
            "broadcast_deliveries",
            "DELETE FROM broadcast_deliveries "
            "WHERE status NOT IN ('failed', 'undelivered') "
            "AND queued_at < datetime('now', ?)",
            (f"-{int(broadcast_deliveries_months)} months",),
        )
        _safe_purge(
            "broadcast_deliveries_failed",
            "DELETE FROM broadcast_deliveries "
            "WHERE status IN ('failed', 'undelivered') "
            "AND queued_at < datetime('now', ?)",
            (f"-{int(broadcast_deliveries_failed_months)} months",),
        )

        # response_pages — cache קצר (30 יום default)
        _safe_purge(
            "response_pages",
            "DELETE FROM response_pages WHERE created_at < datetime('now', ?)",
            (f"-{int(response_pages_days)} days",),
        )

        # consent_ledger — שתי קטגוריות עם retention שונה (לפי המלצת היועץ).
        # consent: 5 שנים מהאירוע (5*12 חודשים — SQLite לא תומך ב-'years' ישירות
        # ב-modifier, אבל '-X months' עובד ל-X גדול).
        # audit: 24 חודשים — תיעוד מימוש זכויות, לא ראיה ארוכת טווח.
        _safe_purge(
            "consent_ledger_consent",
            "DELETE FROM consent_ledger "
            "WHERE category = 'consent' AND event_at < datetime('now', ?)",
            (f"-{int(ledger_consent_years) * 12} months",),
        )
        _safe_purge(
            "consent_ledger_audit",
            "DELETE FROM consent_ledger "
            "WHERE category = 'audit' AND event_at < datetime('now', ?)",
            (f"-{int(ledger_audit_months)} months",),
        )

    total = sum(counts.values())
    if total:
        logger.info("purge_old_data: removed %d rows (%s)", total, counts)

    # מעבד את תור ה-retry של ledger באותה הזדמנות (job יומי משותף).
    # רשומות שנכשלו בכתיבה ראשונה מקבלות הזדמנות נוספת. אחרי 5 ניסיונות
    # נכתב [LEDGER_RETRY_EXHAUSTED] ב-log לחיפוש ב-Render. הקריאה רכה —
    # כשל ב-retry לא מבטל את ה-purge.
    try:
        from utils.consent_ledger import process_ledger_retry_queue
        retry_counts = process_ledger_retry_queue()
        if retry_counts.get("total_processed"):
            counts["ledger_retry_processed"] = retry_counts["total_processed"]
            counts["ledger_retry_succeeded"] = retry_counts.get("succeeded", 0)
    except Exception:
        logger.error("purge_old_data: כשל בעיבוד ledger retry queue", exc_info=True)

    return counts


def _users_filter_sql(
    inactive_days: int | None = None,
    search: str = "",
) -> tuple[str, list]:
    """בניית SQL משותף לסינון טבלת users.

    מחזיר (where_clause, params) — משותף ל-get ול-count.
    """
    conditions = []
    params: list = []

    # סינון: לא פעילים מעל X ימים
    if inactive_days is not None and inactive_days > 0:
        conditions.append("u.last_active_at <= datetime('now', ?)")
        params.append(f"-{inactive_days} days")

    # חיפוש חופשי — בשם משתמש או user_id
    if search:
        conditions.append("(u.username LIKE ? OR u.user_id LIKE ?)")
        like_val = f"%{search}%"
        params.extend([like_val, like_val])

    # רק מנויים (לא ביטלו הרשמה)
    conditions.append("COALESCE(us.is_subscribed, 1) = 1")

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


def get_users_filtered(
    inactive_days: int | None = None,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """שליפת משתמשים מטבלת users עם סינון.

    - inactive_days: משתמשים שלא פעילים מעל X ימים (None = ללא סינון)
    - search: חיפוש חופשי בשם/מזהה
    - pagination עם limit/offset
    """
    where, params = _users_filter_sql(inactive_days, search)
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT u.user_id, u.username, u.channel, u.first_seen_at,
                   u.last_active_at, u.message_count
            FROM users u
            LEFT JOIN user_subscriptions us ON u.user_id = us.user_id
            WHERE {where}
            ORDER BY u.last_active_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]


def count_users_filtered(
    inactive_days: int | None = None,
    search: str = "",
) -> int:
    """ספירת משתמשים מסוננים (ללא pagination)."""
    where, params = _users_filter_sql(inactive_days, search)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM users u
            LEFT JOIN user_subscriptions us ON u.user_id = us.user_id
            WHERE {where}
        """, params).fetchone()
        return int(row["cnt"]) if row else 0


def get_unique_users() -> list[dict]:
    """Get list of unique users with their last message time."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, username,
                   MAX(created_at) as last_active,
                   COUNT(*) as message_count
            FROM conversations
            GROUP BY user_id
            ORDER BY last_active DESC
        """).fetchall()
        return [dict(r) for r in rows]


# ── זהויות משתמשים (BSUID / Meta Cloud API 2026) ──────────────────────────


def upsert_user_identity(
    user_id: str,
    channel: str,
    *,
    whatsapp_bsuid: Optional[str] = None,
    whatsapp_parent_bsuid: Optional[str] = None,
    phone_number: Optional[str] = None,
    username: str = "",
) -> None:
    """יצירה/עדכון רשומת זהות למשתמש.

    אם הרשומה קיימת (לפי channel + user_id) — מעדכנת שדות שאינם ריקים.
    אטומי: משתמש ב-INSERT ON CONFLICT כדי למנוע race condition
    בין שתי בקשות webhook מקבילות לאותו משתמש חדש.
    """
    with get_connection() as conn:
        # COALESCE שומר על ערכים קיימים — לא דורס BSUID/phone/username שכבר נשמרו
        conn.execute(
            """INSERT INTO user_identities
                   (user_id, channel, whatsapp_bsuid, whatsapp_parent_bsuid, phone_number, username)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel, user_id) DO UPDATE SET
                   whatsapp_bsuid        = COALESCE(excluded.whatsapp_bsuid, user_identities.whatsapp_bsuid),
                   whatsapp_parent_bsuid = COALESCE(excluded.whatsapp_parent_bsuid, user_identities.whatsapp_parent_bsuid),
                   phone_number          = COALESCE(excluded.phone_number, user_identities.phone_number),
                   username              = CASE WHEN excluded.username != '' THEN excluded.username
                                                ELSE user_identities.username END,
                   updated_at            = datetime('now')
            """,
            (user_id, channel, whatsapp_bsuid, whatsapp_parent_bsuid, phone_number, username),
        )


def lookup_user_id_by_bsuid(bsuid: str) -> Optional[str]:
    """חיפוש user_id לפי BSUID. מחזיר None אם לא נמצא."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM user_identities WHERE whatsapp_bsuid = ?",
            (bsuid,),
        ).fetchone()
        return row["user_id"] if row else None


def lookup_user_id_by_phone(phone_number: str, channel: str = "whatsapp") -> Optional[str]:
    """חיפוש user_id לפי מספר טלפון וערוץ. מחזיר None אם לא נמצא."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM user_identities WHERE phone_number = ? AND channel = ?",
            (phone_number, channel),
        ).fetchone()
        return row["user_id"] if row else None


def get_phone_for_user(user_id: str) -> Optional[str]:
    """חיפוש מספר טלפון לפי user_id (לשליחת הודעות outbound).

    אם ה-user_id עצמו הוא מספר טלפון (מתחיל ב-+), מחזיר אותו ישירות.
    אחרת, מחפש בטבלת user_identities.
    """
    if user_id.startswith("+"):
        return user_id
    with get_connection() as conn:
        row = conn.execute(
            "SELECT phone_number FROM user_identities WHERE user_id = ? AND phone_number IS NOT NULL",
            (user_id,),
        ).fetchone()
        return row["phone_number"] if row else None


def get_username_for_user(user_id: str) -> Optional[str]:
    """Look up the display name for a single user without scanning all users."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT username FROM conversations WHERE user_id = ? AND username != '' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["username"] if row else None


def get_user_provider_info(user_id: str) -> Optional[dict]:
    """שולף provenance של משתמש (channel + asset + external id).

    שימושי לשליחה יזומה מהאדמין לערוצי מטא — כשאין asset_id מועבר
    מבחוץ (כפי שיש ב-webhook), צריך לשלוף אותו לפי user_id.

    מחזיר None אם המשתמש לא נמצא.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT channel, provider_asset_id, external_user_id "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "channel": row["channel"],
            "provider_asset_id": row["provider_asset_id"],
            "external_user_id": row["external_user_id"],
        }


def get_user_channel(user_id: str) -> str:
    """זיהוי ערוץ המשתמש לפי ההודעה האחרונה שלו."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(channel, 'telegram') AS channel FROM conversations "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["channel"] if row else "telegram"


def _last_summarized_message_id(conn, user_id: str) -> int:
    """Return the highest conversation id already covered by a summary (0 if none)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(last_summarized_message_id), 0) AS last_id "
        "FROM conversation_summaries WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return int(row["last_id"])


def get_unsummarized_message_count(user_id: str) -> int:
    """Count messages for a user that haven't been included in any summary yet.

    Uses `last_summarized_message_id` so the count stays correct even when
    older messages are deleted.
    """
    with get_connection() as conn:
        last_id = _last_summarized_message_id(conn, user_id)

        row = conn.execute(
            "SELECT COUNT(*) AS count FROM conversations "
            "WHERE user_id = ? AND id > ?",
            (user_id, last_id),
        ).fetchone()
        return int(row["count"])


def get_messages_for_summarization(user_id: str, limit: int) -> list[dict]:
    """Get the oldest unsummarized messages for a user (to create a summary from).

    Returns up to *limit* messages whose ``id`` is greater than the
    ``last_summarized_message_id`` stored in the latest summary.
    Each returned dict includes the conversation row ``id`` so that
    :func:`save_conversation_summary` can record the new high-water mark.
    """
    with get_connection() as conn:
        last_id = _last_summarized_message_id(conn, user_id)

        rows = conn.execute(
            """SELECT id, role, message, created_at
               FROM conversations WHERE user_id = ? AND id > ?
               ORDER BY id ASC LIMIT ?""",
            (user_id, last_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def save_conversation_summary(
    user_id: str,
    summary_text: str,
    message_count: int,
    last_summarized_message_id: int = 0,
):
    """
    Save a conversation summary for a user.

    Replaces all previous summaries with a single merged summary.
    ``last_summarized_message_id`` is the ``conversations.id`` of the newest
    message included in this summary — subsequent queries use it as a
    high-water mark so that counting stays correct even when rows are deleted.
    ``message_count`` is accumulated for informational / admin-display purposes.
    """
    with get_connection() as conn:
        # Accumulate total message count from existing summaries
        row = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0) AS total FROM conversation_summaries WHERE user_id=?",
            (user_id,)
        ).fetchone()
        total_message_count = int(row["total"]) + message_count

        # If no explicit high-water mark was given, keep the previous one
        if not last_summarized_message_id:
            last_summarized_message_id = _last_summarized_message_id(conn, user_id)

        # Replace all previous summaries with the new merged one
        conn.execute("DELETE FROM conversation_summaries WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO conversation_summaries "
            "(user_id, summary_text, message_count, last_summarized_message_id) "
            "VALUES (?, ?, ?, ?)",
            (user_id, summary_text, total_message_count, last_summarized_message_id),
        )


def get_latest_summary(user_id: str) -> dict | None:
    """Get the latest (single) conversation summary for a user."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT summary_text, message_count, last_summarized_message_id, created_at
               FROM conversation_summaries WHERE user_id=?
               ORDER BY id DESC LIMIT 1""",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None


def count_unique_users() -> int:
    """Count distinct users in conversation history."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS count FROM conversations"
        ).fetchone()
        return int(row["count"]) if row else 0


# ─── Agent Requests ──────────────────────────────────────────────────────────

def create_agent_request(
    user_id: str,
    username: str,
    message: str = "",
    telegram_username: str = "",
    channel: str = "telegram",
) -> int:
    """Create a new agent transfer request."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agent_requests (user_id, username, telegram_username, message, channel) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, telegram_username or "", message, channel)
        )
        return cursor.lastrowid


def _status_filter_query(
    table: str,
    columns: str,
    status: str | None,
    limit: int | None,
    order: str | None = None,
) -> tuple[str, list[object]]:
    """בניית שאילתת SELECT עם סינון סטטוס אופציונלי — helper משותף ל-get/count.

    order=None דולג (מתאים ל-COUNT), order="created_at DESC" ממיין (מתאים ל-SELECT *).
    """
    params: list[object] = []
    query = f"SELECT {columns} FROM {table}"
    if status:
        query += " WHERE status=?"
        params.append(status)
    if order:
        query += f" ORDER BY {order}"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return query, params


def get_agent_requests(status: str | None = None, limit: int | None = None) -> list[dict]:
    """Get agent requests, optionally filtered by status."""
    query, params = _status_filter_query("agent_requests", "*", status, limit, order="created_at DESC")
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count_agent_requests(status: str | None = None) -> int:
    """Count agent requests, optionally filtered by status."""
    query, params = _status_filter_query("agent_requests", "COUNT(*) AS count", status, limit=None)
    with get_connection() as conn:
        row = conn.execute(query, params).fetchone()
        return int(row["count"]) if row else 0


def update_agent_request_status(request_id: int, status: str):
    """Update the status of an agent request."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE agent_requests SET status=?, handled_at=datetime('now') WHERE id=?",
            (status, request_id)
        )


def get_agent_request(request_id: int) -> Optional[dict]:
    """Get a single agent request by ID."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM agent_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None


def handle_pending_requests_for_user(user_id: str) -> int:
    """סגירת כל בקשות הנציג הממתינות עבור משתמש — נקרא כשנכנסים לשיחה חיה."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE agent_requests SET status='handled', handled_at=datetime('now') "
            "WHERE user_id=? AND status='pending'",
            (user_id,),
        )
        return cursor.rowcount


# ─── Appointments ────────────────────────────────────────────────────────────

def create_appointment(
    user_id: str,
    username: str,
    service: str = "",
    preferred_date: str = "",
    preferred_time: str = "",
    notes: str = "",
    telegram_username: str = "",
    channel: str = "telegram",
) -> int:
    """Create a new appointment booking."""
    with get_connection() as conn:
        cursor = conn.cursor()
        # מחיקת תורים ישנים (cancelled/passed) באותו מועד — כדי שה-UNIQUE index
        # לא יחסום קביעת תור חדש אחרי ביטול/עבר.
        if preferred_date and preferred_time:
            cursor.execute(
                """DELETE FROM appointments
                   WHERE user_id = ? AND preferred_date = ? AND preferred_time = ?
                     AND status IN ('cancelled', 'passed')""",
                (user_id, preferred_date, preferred_time),
            )
        cursor.execute(
            """INSERT INTO appointments (user_id, username, telegram_username, service, preferred_date, preferred_time, notes, channel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, telegram_username or "", service, preferred_date, preferred_time, notes, channel)
        )
        return cursor.lastrowid


def get_appointments(status: str | None = None, limit: int | None = None) -> list[dict]:
    """Get appointments, optionally filtered by status."""
    query, params = _status_filter_query("appointments", "*", status, limit, order="created_at DESC")
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_unseen_appointments(limit: int | None = None) -> list[dict]:
    """תורים שטרם נצפו ע"י בעל העסק (owner_seen=0 ולא מבוטלים).

    משמש את הדשבורד להצגת רשימת "תורים חדשים" — חייב להתאים לאותה
    הלוגיקה כמו get_dashboard_counts.pending_appointments, אחרת ייווצר
    mismatch בין הספירה לרשימה.
    """
    query = (
        "SELECT * FROM appointments "
        "WHERE owner_seen = 0 AND status != 'cancelled' "
        "ORDER BY created_at DESC"
    )
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count_appointments(status: str | None = None) -> int:
    """Count appointments, optionally filtered by status."""
    query, params = _status_filter_query("appointments", "COUNT(*) AS count", status, limit=None)
    with get_connection() as conn:
        row = conn.execute(query, params).fetchone()
        return int(row["count"]) if row else 0


def update_appointment_status(
    appt_id: int,
    status: str,
    confirmed_duration_minutes: Optional[int] = None,
):
    """Update appointment status.

    confirmed_duration_minutes — אופציונלי: כשבעל העסק מאשר תור, הוא בוחר
    את משך התור בפועל (יכול להיות שונה מברירת המחדל של השירות). שמירתו
    מאפשרת לסנכרן ל-Google Calendar עם end_dt מדויק.
    """
    with get_connection() as conn:
        if confirmed_duration_minutes is not None:
            conn.execute(
                "UPDATE appointments SET status=?, confirmed_duration_minutes=? WHERE id=?",
                (status, int(confirmed_duration_minutes), appt_id),
            )
        else:
            conn.execute(
                "UPDATE appointments SET status=? WHERE id=?",
                (status, appt_id),
            )


def get_appointment_duration_settings() -> dict:
    """החזרת ההגדרות של אופציות משך תור (default, step, backward, forward).

    default_minutes — ברירת מחדל אחידה לכל השירותים. החליפה את
    duration_minutes הפר-שירות בעקבות ביטול ההגדרה הפרטנית בעמוד "שירותים".

    משתמש ב-`if v is None` ולא ב-`or` כדי לא להחליף 0 (ערך תקין) בברירת מחדל.
    """
    settings = get_bot_settings() or {}
    def _val(key: str, default: int) -> int:
        v = settings.get(key)
        return default if v is None else int(v)
    return {
        "default_minutes": _val("default_appointment_duration_minutes", 60),
        "step_minutes": _val("appointment_duration_step_minutes", 15),
        "steps_backward": _val("appointment_duration_steps_backward", 2),
        "steps_forward": _val("appointment_duration_steps_forward", 4),
    }


def update_appointment_duration_settings(
    step_minutes: int,
    steps_backward: int,
    steps_forward: int,
    default_minutes: Optional[int] = None,
) -> None:
    """עדכון הגדרות אופציות משך תור.

    default_minutes — אופציונלי. אם None, שומרים את הערך הקיים בלי לדרוס.
    קוראים שעודכנו ל-3 שדות בלבד (step/backward/forward) לא יאפסו אותו.
    """
    step_minutes = max(5, min(120, int(step_minutes)))
    steps_backward = max(0, min(10, int(steps_backward)))
    steps_forward = max(0, min(10, int(steps_forward)))
    with get_connection() as conn:
        if default_minutes is None:
            conn.execute(
                """UPDATE bot_settings SET
                       appointment_duration_step_minutes = ?,
                       appointment_duration_steps_backward = ?,
                       appointment_duration_steps_forward = ?,
                       updated_at = datetime('now')
                   WHERE id = 1""",
                (step_minutes, steps_backward, steps_forward),
            )
        else:
            default_minutes = max(5, min(24 * 60, int(default_minutes)))
            conn.execute(
                """UPDATE bot_settings SET
                       default_appointment_duration_minutes = ?,
                       appointment_duration_step_minutes = ?,
                       appointment_duration_steps_backward = ?,
                       appointment_duration_steps_forward = ?,
                       updated_at = datetime('now')
                   WHERE id = 1""",
                (default_minutes, step_minutes, steps_backward, steps_forward),
            )


def _hhmm_to_minutes(value: str) -> Optional[int]:
    """המרת מחרוזת 'HH:MM' למספר דקות מתחילת היום. None אם לא תקין."""
    if not value or not isinstance(value, str):
        return None
    parts = value.split(":")
    try:
        if len(parts) < 2:
            return None
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        return h * 60 + m
    except (ValueError, TypeError):
        return None


_MIN_APPOINTMENT_DURATION_MIN = 5


def _resolve_duration_for_appt_row(appt_row: dict, default_minutes: int) -> int:
    """משך אפקטיבי לתור (לצורך חישוב טווח תפוס): confirmed > default_minutes שהועבר.

    default_minutes הוא ברירת המחדל הגלובלית; הקורא נדרש לשלוף אותה פעם אחת
    מ-get_appointment_duration_settings ולהעביר ללולאה — מונע שאילתה נוספת
    על bot_settings פר-תור (חשוב ל-polling כל 15 שניות).
    """
    confirmed = appt_row.get("confirmed_duration_minutes")
    if confirmed:
        return int(confirmed)
    return int(default_minutes or 60)


def _build_candidates(base_duration: int, settings: dict) -> list[int]:
    """בניית רשימת מועמדים סביב ברירת המחדל (לפי backward/forward/step)."""
    step = int(settings["step_minutes"])
    backward = int(settings["steps_backward"])
    forward = int(settings["steps_forward"])
    candidates = []
    for offset in range(-backward, forward + 1):
        candidate = base_duration + offset * step
        if candidate >= _MIN_APPOINTMENT_DURATION_MIN:
            candidates.append(candidate)
    return candidates


def get_appointments_busy_ranges(
    date_str: str, exclude_appointment_id: Optional[int] = None,
) -> list[tuple[int, int]]:
    """החזרת טווחי דקות תפוסים מתורים pending/confirmed ביום נתון.

    מחזיר list של (start_minute, end_minute) — הדקות מאז חצות היום.
    משך תור: confirmed_duration_minutes אם קיים, אחרת default גלובלי.
    הפונקציה הזו מאפשרת ל-get_available_slots לחסום סלוטים שכבר תפוסים
    ב-DB גם אם לא סונכרנו ל-GCal — באג שגרם לבוט להציע שעות תפוסות.

    exclude_appointment_id — מזהה תור להתעלמות (id != ?). נחוץ להחלטת
    auto-booking שרצה *אחרי* create_appointment: בלי זה התור שזה עתה נוצר
    היה נספר כטווח תפוס וחוסם את השעה של עצמו ⇒ calendar_busy כוזב.
    """
    default_min = 60
    try:
        default_min = int(get_appointment_duration_settings().get("default_minutes") or 60)
    except Exception:
        logger.error("get_appointments_busy_ranges: שגיאה בקריאת default_minutes", exc_info=True)

    query = (
        "SELECT preferred_time, confirmed_duration_minutes, service "
        "FROM appointments "
        "WHERE preferred_date = ? AND status IN ('pending', 'confirmed')"
    )
    params: tuple = (date_str,)
    if exclude_appointment_id is not None:
        query += " AND id != ?"
        params = (date_str, exclude_appointment_id)

    ranges: list[tuple[int, int]] = []
    try:
        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        for r in rows:
            r_dict = dict(r)
            r_start = _hhmm_to_minutes(r_dict.get("preferred_time", ""))
            if r_start is None:
                continue
            r_dur = _resolve_duration_for_appt_row(r_dict, default_min)
            ranges.append((r_start, r_start + r_dur))
    except Exception:
        logger.error("get_appointments_busy_ranges: שגיאה בשליפת תורים", exc_info=True)
    return ranges


def _load_date_context(date_str: str, default_minutes: int) -> dict:
    """טעינת כל הנתונים הקשורים לתאריך נתון — תורים, שעות עבודה, GCal busy.

    מחזיר dict עם:
    - 'other_ranges_by_id': {appt_id: (start_minute, end_minute)} לכל תור pending/confirmed
    - 'close_minute': int או None — סגירת היום העסקי בדקות
    - 'gcal_busy': list[(start_minute, end_minute)] — busy ranges חיצוניים מ-GCal,
      מחולצים לחלון של 24 שעות בתוך היום (מטפל גם באירועי כל-היום וחוצי-חצות)

    default_minutes — ברירת המחדל הגלובלית למשך תור, שמועברת מהקורא כדי להימנע
    משאילתה חוזרת על bot_settings פר-תור (חשוב ל-polling כל 15 שניות).
    """
    from datetime import date as _date_type
    try:
        target_date = _date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return {"other_ranges_by_id": {}, "close_minute": None, "gcal_busy": []}

    # ── תורים אחרים באותו יום (pending/confirmed) ──
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, preferred_time, confirmed_duration_minutes, service
               FROM appointments
               WHERE preferred_date = ? AND status IN ('pending', 'confirmed')""",
            (date_str,),
        ).fetchall()

    other_ranges_by_id: dict[int, tuple[int, int]] = {}
    for r in rows:
        r_dict = dict(r)
        r_start = _hhmm_to_minutes(r_dict.get("preferred_time", ""))
        if r_start is None:
            continue
        r_dur = _resolve_duration_for_appt_row(r_dict, default_minutes)
        other_ranges_by_id[r_dict["id"]] = (r_start, r_start + r_dur)

    # ── סף סגירת היום ──
    close_minute: Optional[int] = None
    try:
        from business_hours import get_status_for_date
        day_status = get_status_for_date(target_date)
        if day_status.get("is_open"):
            close_minute = _hhmm_to_minutes(day_status.get("close_time") or "")
    except Exception:
        logger.error("compute_duration_options: שגיאה בשעות עבודה", exc_info=True)

    # ── GCal busy ranges (clamped ליום) ──
    gcal_busy: list[tuple[int, int]] = []
    try:
        from google_calendar import is_connected, get_busy_slots
        if is_connected():
            from datetime import datetime as _dt, timedelta as _td
            from zoneinfo import ZoneInfo as _ZI
            tz = _ZI("Asia/Jerusalem")
            day_start = _dt.combine(target_date, _dt.min.time(), tzinfo=tz)
            day_end = day_start + _td(days=1)
            for slot in get_busy_slots(day_start, day_end):
                try:
                    bs = _dt.fromisoformat(slot["start"])
                    be = _dt.fromisoformat(slot["end"])
                    if bs.tzinfo is not None:
                        bs = bs.astimezone(tz)
                    else:
                        bs = bs.replace(tzinfo=tz)
                    if be.tzinfo is not None:
                        be = be.astimezone(tz)
                    else:
                        be = be.replace(tzinfo=tz)
                    # חיתוך לטווח היום — אירועי כל-היום וחוצי-חצות מתבטאים נכון
                    if be <= day_start or bs >= day_end:
                        continue
                    bs_clamped = max(bs, day_start)
                    be_clamped = min(be, day_end)
                    bs_min = int((bs_clamped - day_start).total_seconds() // 60)
                    be_min = int((be_clamped - day_start).total_seconds() // 60)
                    if be_min > bs_min:
                        gcal_busy.append((bs_min, be_min))
                except (ValueError, KeyError):
                    continue
    except ImportError:
        pass
    except Exception:
        logger.error("compute_duration_options: שגיאה ב-Google Calendar", exc_info=True)

    return {
        "other_ranges_by_id": other_ranges_by_id,
        "close_minute": close_minute,
        "gcal_busy": gcal_busy,
    }


def _filter_valid_durations(
    start_minute: int,
    candidates: list[int],
    other_busy: list[tuple[int, int]],
    gcal_busy: list[tuple[int, int]],
    close_minute: Optional[int],
) -> list[int]:
    """סינון מועמדים שלא מתנגשים: לא חורגים מסגירה, לא חופפים לתור אחר/GCal."""
    valid = []
    for dur in candidates:
        end_minute = start_minute + dur
        if close_minute is not None and end_minute > close_minute:
            continue
        if any(start_minute < be and end_minute > bs for bs, be in other_busy):
            continue
        if any(start_minute < be and end_minute > bs for bs, be in gcal_busy):
            continue
        valid.append(dur)
    return valid


def compute_duration_options_for_appointment(appt_id: int) -> list[int]:
    """חישוב רשימת אופציות משך אפשריות לתור ממתין (גרסת תור-בודד).

    משתמש ב-_load_date_context לקבלת ההקשר. למספר תורים יחד עדיף
    compute_duration_options_for_pending — עוקף N+1 ע"י קישור לפי תאריך.
    """
    appt = get_appointment(appt_id)
    if not appt:
        return []

    preferred_date = appt.get("preferred_date", "")
    preferred_time = appt.get("preferred_time", "")
    start_minute = _hhmm_to_minutes(preferred_time)
    # שאילתה אחת ל-bot_settings — ולא פר-תור כפי שהיה קודם
    settings = get_appointment_duration_settings()
    default_minutes = int(settings.get("default_minutes") or 60)

    if not preferred_date or start_minute is None:
        # אין תאריך/שעה — מחזירים ברירת מחדל ללא בדיקות
        return [_resolve_duration_for_appt_row(appt, default_minutes)]

    base_duration = _resolve_duration_for_appt_row(appt, default_minutes)
    candidates = _build_candidates(base_duration, settings)

    ctx = _load_date_context(preferred_date, default_minutes)
    # מתעלמים מהתור עצמו ב-other_busy
    other_busy = [
        rng for aid, rng in ctx["other_ranges_by_id"].items() if aid != appt_id
    ]
    valid = _filter_valid_durations(
        start_minute, candidates, other_busy, ctx["gcal_busy"], ctx["close_minute"],
    )
    if not valid:
        valid = [base_duration]
    return valid


def compute_duration_options_for_pending(pending_appts: list[dict]) -> dict[int, list[int]]:
    """חישוב duration_options לרשימת תורים ממתינים — עם batching.

    יעיל ל-polling: מאחד שאילתות לפי תאריך ושומר cache לשירותים. במקום N+M
    קריאות (כש-N=#תורים ו-M=שאילתות פר תור), עושה ~K קריאות (K=#תאריכים שונים).

    מחזיר dict {appt_id: list of valid durations}.
    """
    if not pending_appts:
        return {}

    # שאילתה אחת ל-bot_settings לכל הריצה — לא פר-תור (חשוב ל-polling אדמין)
    settings = get_appointment_duration_settings()
    default_minutes = int(settings.get("default_minutes") or 60)
    # Cache תוצאות per-date — מספיק שאילתה אחת אפילו אם 5 תורים באותו יום
    date_ctx_cache: dict[str, dict] = {}

    results: dict[int, list[int]] = {}
    for appt in pending_appts:
        appt_id = appt.get("id")
        if appt_id is None:
            continue
        preferred_date = appt.get("preferred_date", "")
        preferred_time = appt.get("preferred_time", "")
        start_minute = _hhmm_to_minutes(preferred_time)

        if not preferred_date or start_minute is None:
            results[appt_id] = [_resolve_duration_for_appt_row(appt, default_minutes)]
            continue

        base_duration = _resolve_duration_for_appt_row(appt, default_minutes)
        candidates = _build_candidates(base_duration, settings)

        if preferred_date not in date_ctx_cache:
            date_ctx_cache[preferred_date] = _load_date_context(preferred_date, default_minutes)
        ctx = date_ctx_cache[preferred_date]

        other_busy = [
            rng for aid, rng in ctx["other_ranges_by_id"].items() if aid != appt_id
        ]
        valid = _filter_valid_durations(
            start_minute, candidates, other_busy, ctx["gcal_busy"], ctx["close_minute"],
        )
        results[appt_id] = valid if valid else [base_duration]

    return results


def resolve_appointment_duration_minutes(appt: dict) -> int:
    """החזרת משך התור האפקטיבי לתור נתון.

    סדר עדיפויות:
    1. confirmed_duration_minutes — אם בעל העסק בחר במפורש באישור
    2. ברירת מחדל גלובלית מ-bot_settings (default_appointment_duration_minutes)
    3. 60 דקות — fallback קשיח אם הגדרת ברירת המחדל לא קיימת או לא תקינה

    שדה duration_minutes הפר-שירות בטבלת services לא נקרא יותר — ביטלנו
    הגדרות פר-שירות כשעמוד "שירותים" הוסתר.
    """
    confirmed = appt.get("confirmed_duration_minutes")
    if confirmed:
        return int(confirmed)
    settings = get_appointment_duration_settings()
    return int(settings.get("default_minutes") or 60)


def expire_past_appointments() -> int:
    """סימון תורים ממתינים שהתאריך שלהם עבר כ-'passed'.

    משתמש בשעון ישראל (UTC+2/+3) כי preferred_date מייצג תאריך מקומי.
    מחזיר את מספר התורים שעודכנו.
    """
    from zoneinfo import ZoneInfo
    today_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status='passed' "
            "WHERE status='pending' AND preferred_date != '' "
            "AND preferred_date < ?",
            (today_il,)
        )
        count = cursor.rowcount
        if count:
            logger.info("Marked %d past appointments as 'passed'", count)
        return count


def get_appointments_for_reminder(target_date: str) -> list[dict]:
    """שליפת תורים מאושרים שצריכים תזכורת — רק confirmed (לא pending — עדיין לא סגור)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments "
            "WHERE preferred_date = ? AND status = 'confirmed' "
            "AND reminder_sent = 0",
            (target_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_sent(appt_id: int):
    """סימון שנשלחה תזכורת לתור."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE appointments SET reminder_sent = 1 WHERE id = ?",
            (appt_id,)
        )


def mark_appointments_seen(appt_ids: list[int] | None = None) -> int:
    """סימון תורים כ"נצפו" — מאפס את הבועה בסיידבאר.

    appt_ids — אם מועבר, מסמנים רק את ה-IDs האלה. אחרת מסמנים הכל.
    מומלץ להעביר את ה-IDs מהשליפה שהוצגה ב-UI כדי למנוע race שבו תור
    שנוצר בין fetch ל-mark מסומן כנצפה למרות שלא הוצג עדיין.

    מחזיר את מספר השורות שעודכנו.
    """
    with get_connection() as conn:
        if appt_ids is None:
            cursor = conn.execute(
                "UPDATE appointments SET owner_seen = 1 WHERE owner_seen = 0"
            )
            return cursor.rowcount
        if not appt_ids:
            return 0
        placeholders = ",".join("?" for _ in appt_ids)
        cursor = conn.execute(
            f"UPDATE appointments SET owner_seen = 1 "
            f"WHERE owner_seen = 0 AND id IN ({placeholders})",
            tuple(appt_ids),
        )
        return cursor.rowcount


def get_appointments_for_second_reminder(date_time_ranges: list[tuple[str, str, str]]) -> list[dict]:
    """שליפת תורים מאושרים שצריכים תזכורת שנייה.

    Parameters
    ----------
    date_time_ranges : list of (date, min_time, max_time)
        רשימת טווחים — כל טאפל מגדיר תאריך וחלון שעות.
        תומך במספר טווחים כדי לטפל בחלון שחוצה חצות.
    """
    results: list[dict] = []
    with get_connection() as conn:
        for target_date, min_time, max_time in date_time_ranges:
            rows = conn.execute(
                "SELECT * FROM appointments "
                "WHERE preferred_date = ? AND status = 'confirmed' "
                "AND second_reminder_sent = 0 "
                "AND preferred_time >= ? AND preferred_time < ?",
                (target_date, min_time, max_time)
            ).fetchall()
            results.extend(dict(r) for r in rows)
    return results


def mark_second_reminder_sent(appt_id: int):
    """סימון שנשלחה תזכורת שנייה לתור."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE appointments SET second_reminder_sent = 1 WHERE id = ?",
            (appt_id,)
        )


def get_appointment(appt_id: int) -> Optional[dict]:
    """Get a single appointment by ID."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
        return dict(row) if row else None


def get_pending_appointments_for_user(user_id: str) -> list[dict]:
    """שליפת תורים פעילים (pending או confirmed) של משתמש — אלה שהלקוח יכול לבטל."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE user_id = ? AND status IN ('pending', 'confirmed') "
            "AND preferred_date >= date('now') ORDER BY preferred_date, preferred_time",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_appointment(appt_id: int, user_id: str) -> bool:
    """ביטול תור ע״י הלקוח — pending או confirmed, בתנאי שהתור שייך למשתמש."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status IN ('pending', 'confirmed')",
            (appt_id, user_id)
        )
        return cursor.rowcount > 0


def update_appointment(appt_id: int, user_id: str,
                       preferred_date: str | None = None,
                       preferred_time: str | None = None) -> bool:
    """עדכון תאריך/שעה של תור קיים — pending או confirmed, בתנאי שהתור שייך למשתמש."""
    updates: list[str] = []
    params: list = []
    if preferred_date is not None:
        updates.append("preferred_date = ?")
        params.append(preferred_date)
    if preferred_time is not None:
        updates.append("preferred_time = ?")
        params.append(preferred_time)
    if not updates:
        return True
    params.extend([appt_id, user_id])
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE appointments SET {', '.join(updates)} "
            "WHERE id = ? AND user_id = ? AND status IN ('pending', 'confirmed')",
            params,
        )
        return cursor.rowcount > 0


def update_appointment_and_sync(appt_id: int, user_id: str,
                                preferred_date: str | None = None,
                                preferred_time: str | None = None) -> bool:
    """עדכון תאריך/שעה + סנכרון עם Google Calendar.

    מחזיר True אם התור עודכן (גם אם סנכרון Calendar נכשל).
    """
    if not update_appointment(appt_id, user_id, preferred_date, preferred_time):
        return False

    try:
        from google_calendar import sync_appointment_to_calendar
        updated_appt = get_appointment(appt_id)
        if updated_appt:
            sync_appointment_to_calendar(updated_appt, "updated")
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בסנכרון עדכון תור #%s עם Google Calendar", appt_id, exc_info=True)

    return True


def cancel_appointment_and_sync(appt_id: int, user_id: str) -> bool:
    """ביטול תור ע״י הלקוח + סנכרון עם Google Calendar.

    משלב cancel_appointment עם מחיקת האירוע מ-Google Calendar.
    מחזיר True אם התור בוטל (גם אם סנכרון Calendar נכשל).
    """
    if not cancel_appointment(appt_id, user_id):
        return False

    try:
        from google_calendar import sync_appointment_to_calendar
        updated_appt = get_appointment(appt_id)
        if updated_appt:
            sync_appointment_to_calendar(updated_appt, "cancelled")
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בסנכרון ביטול תור #%s עם Google Calendar", appt_id, exc_info=True)

    return True


def has_confirmed_appointments(user_id: str) -> bool:
    """בדיקה אם למשתמש יש תורים מאושרים עתידיים."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM appointments WHERE user_id = ? AND status = 'confirmed' "
            "AND preferred_date >= date('now') LIMIT 1",
            (user_id,)
        ).fetchone()
        return row is not None


def is_returning_customer(user_id: str) -> bool:
    """בדיקה אם המשתמש לקוח חוזר — יש לו תורים מאושרים שתאריכם כבר עבר.

    passed לבד לא מספיק כי expire_past_appointments מסמן pending כ-passed
    (תורים שמעולם לא אושרו). רק confirmed עם תאריך שעבר = לקוח שבאמת הגיע.
    """
    from zoneinfo import ZoneInfo
    today_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM appointments WHERE user_id = ? "
            "AND status = 'confirmed' AND preferred_date != '' AND preferred_date < ? "
            "LIMIT 1",
            (user_id, today_il)
        ).fetchone()
        return row is not None


# ─── Google Calendar Credentials ─────────────────────────────────────────────


def save_google_calendar_credentials(
    google_account_email: str,
    calendar_id: str,
    refresh_token: str,
    access_token: str,
    token_expiry: str,
    timezone: str,
) -> None:
    """שמירת/עדכון credentials של Google Calendar.

    refresh_token ו-access_token מוצפנים ברמת היישום (Fernet) לפני
    שמירה — כדי שגם אם DB דולף, התוקף לא יקבל גישה ליומן הפרטי של
    בעל העסק. ראה utils/crypto.py.
    """
    from utils.crypto import encrypt_field
    with get_connection() as conn:
        conn.execute(
            """UPDATE google_calendar_credentials
               SET google_account_email = ?, calendar_id = ?,
                   refresh_token = ?, access_token = ?, token_expiry = ?,
                   timezone = ?,
                   auth_invalid_at = NULL, owner_alert_sent_at = NULL,
                   updated_at = datetime('now')
               WHERE id = 1""",
            (google_account_email, calendar_id,
             encrypt_field(refresh_token), encrypt_field(access_token),
             token_expiry, timezone),
        )


def get_google_calendar_credentials() -> Optional[dict]:
    """שליפת credentials של Google Calendar. מחזיר None אם לא מוגדר.

    refresh_token / access_token מפוענחים אוטומטית. ערכים legacy
    בטקסט גלוי (לפני המעבר להצפנה) נתמכים שקופית — decrypt_field
    מחזיר אותם כמו שהם, וכתיבה הבאה תצפין.
    """
    from utils.crypto import decrypt_field
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM google_calendar_credentials WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        try:
            refresh_token_plain = decrypt_field(row["refresh_token"] or "")
        except Exception:
            logger.error(
                "get_google_calendar_credentials: כשל בפענוח refresh_token",
                exc_info=True,
            )
            return None
        if not refresh_token_plain:
            return None
        try:
            access_token_plain = decrypt_field(row["access_token"] or "")
        except Exception:
            logger.error(
                "get_google_calendar_credentials: כשל בפענוח access_token",
                exc_info=True,
            )
            access_token_plain = ""
        result = dict(row)
        result["refresh_token"] = refresh_token_plain
        result["access_token"] = access_token_plain
        return result


def update_google_calendar_token(access_token: str, token_expiry: str) -> None:
    """עדכון access token אחרי refresh — מצפין לפני שמירה."""
    from utils.crypto import encrypt_field
    with get_connection() as conn:
        conn.execute(
            "UPDATE google_calendar_credentials "
            "SET access_token = ?, token_expiry = ?, updated_at = datetime('now') "
            "WHERE id = 1",
            (encrypt_field(access_token), token_expiry),
        )


def delete_google_calendar_credentials() -> None:
    """מחיקת credentials — ניתוק Google Calendar."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE google_calendar_credentials "
            "SET google_account_email = '', calendar_id = 'primary', "
            "    refresh_token = '', access_token = '', token_expiry = '', "
            "    auth_invalid_at = NULL, owner_alert_sent_at = NULL, "
            "    updated_at = datetime('now') "
            "WHERE id = 1"
        )


def set_google_calendar_auth_invalid() -> bool:
    """סימון שהחיבור ל-GCal לא תקף (refresh נכשל). מחזיר True אם זו הפעם
    הראשונה שמסומן (כדי שהקורא ידע אם לשלוח התראה לבעל העסק).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT auth_invalid_at FROM google_calendar_credentials WHERE id = 1"
        ).fetchone()
        already_invalid = bool(row and row["auth_invalid_at"])
        if already_invalid:
            return False
        conn.execute(
            "UPDATE google_calendar_credentials "
            "SET auth_invalid_at = datetime('now') WHERE id = 1"
        )
        return True


def clear_google_calendar_auth_invalid() -> None:
    """איפוס דגל auth_invalid (ה-refresh הצליח / חיבור מחדש)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE google_calendar_credentials "
            "SET auth_invalid_at = NULL, owner_alert_sent_at = NULL "
            "WHERE id = 1"
        )


def is_google_calendar_auth_invalid() -> bool:
    """בדיקה אם החיבור ל-GCal סומן כלא תקף."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT auth_invalid_at FROM google_calendar_credentials WHERE id = 1"
        ).fetchone()
        return bool(row and row["auth_invalid_at"])


def mark_google_calendar_owner_alert_sent() -> None:
    """סימון ששלחנו כבר התראה לבעל העסק על בעיית החיבור."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE google_calendar_credentials "
            "SET owner_alert_sent_at = datetime('now') WHERE id = 1"
        )


def set_appointment_google_event_id(appt_id: int, google_event_id: str) -> None:
    """שמירת מזהה אירוע Google Calendar לתור."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE appointments SET google_event_id = ? WHERE id = ?",
            (google_event_id, appt_id),
        )


# ─── Meta DM Credentials (Instagram + Facebook Messenger) ──────────────────


def upsert_meta_credentials(
    page_id: str,
    access_token: str,
    page_name: str = "",
    ig_business_account_id: str = "",
    ig_username: str = "",
) -> None:
    """שמירת/עדכון credentials של עמוד מטא.

    access_token מוצפן ברמת היישום (Fernet) לפני שמירה. אם העמוד כבר
    קיים — מעדכנים tokens ושמות, שומרים על created_at המקורי.
    """
    from utils.crypto import encrypt_field
    encrypted_token = encrypt_field(access_token)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO meta_credentials
                (page_id, ig_business_account_id, access_token_encrypted,
                 page_name, ig_username, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(page_id) DO UPDATE SET
                -- שומרים על IGBA/ig_username הקיימים אם הקריאה החדשה
                -- ריקה: שליפת IG ב-OAuth יכולה להיכשל באופן זמני
                -- (rate limit / טוקן רגעי), ואסור שדריסה תאבד את הקישור
                -- שכבר היה.
                ig_business_account_id = COALESCE(
                    NULLIF(excluded.ig_business_account_id, ''),
                    meta_credentials.ig_business_account_id
                ),
                ig_username = COALESCE(
                    NULLIF(excluded.ig_username, ''),
                    meta_credentials.ig_username
                ),
                access_token_encrypted = excluded.access_token_encrypted,
                page_name = excluded.page_name,
                updated_at = datetime('now')
            """,
            (page_id, ig_business_account_id, encrypted_token,
             page_name, ig_username),
        )


def get_meta_credentials_by_page_id(page_id: str) -> Optional[dict]:
    """שליפת credentials של עמוד מטא לפי page_id, עם access_token מפוענח.

    מחזיר None אם לא נמצא או אם פענוח נכשל (מפתח הצפנה שונה).
    """
    from utils.crypto import decrypt_field
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM meta_credentials WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["access_token"] = decrypt_field(out["access_token_encrypted"])
        except Exception:
            logger.error(
                "get_meta_credentials_by_page_id: כשל בפענוח access_token "
                "עבור page_id=%s — ייתכן ש-SECRETS_ENCRYPTION_KEY שונה",
                page_id, exc_info=True,
            )
            return None
        out.pop("access_token_encrypted", None)
        return out


def get_meta_credentials_by_ig_account(ig_business_account_id: str) -> Optional[dict]:
    """שליפה לפי IG Business Account ID — שימושי ב-webhook של אינסטגרם
    שמשדר `entry.id` כ-IGBA ולא כ-page_id.
    """
    from utils.crypto import decrypt_field
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM meta_credentials WHERE ig_business_account_id = ?",
            (ig_business_account_id,),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["access_token"] = decrypt_field(out["access_token_encrypted"])
        except Exception:
            logger.error(
                "get_meta_credentials_by_ig_account: כשל בפענוח access_token "
                "עבור ig=%s",
                ig_business_account_id, exc_info=True,
            )
            return None
        out.pop("access_token_encrypted", None)
        return out


def list_meta_credentials() -> list[dict]:
    """רשימת כל העמודים המחוברים — בלי tokens. למסך admin בלבד."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT page_id, ig_business_account_id, page_name, ig_username,
                   created_at, updated_at
            FROM meta_credentials
            ORDER BY created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def is_meta_entry_known(entry_id: str) -> bool:
    """בדיקה מהירה: האם `entry.id` שהגיע מ-webhook של מטא מוכר?

    `entry.id` יכול להיות page_id (Messenger) או IG Business Account ID
    (Instagram). שתי השאלות אחוזות ב-SELECT אחד.
    """
    if not entry_id:
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM meta_credentials
            WHERE page_id = ? OR ig_business_account_id = ?
            LIMIT 1
            """,
            (entry_id, entry_id),
        ).fetchone()
        return row is not None


def delete_meta_credentials(page_id: str) -> bool:
    """מחיקת חיבור עמוד מטא. מחזיר True אם נמחק, False אם לא נמצא."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM meta_credentials WHERE page_id = ?",
            (page_id,),
        )
        return cur.rowcount > 0


# ─── Services (שירותים עם משך תור) ──────────────────────────────────────────


def get_all_services(active_only: bool = False) -> list[dict]:
    """שליפת כל השירותים."""
    with get_connection() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM services WHERE is_active = 1 ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_service_by_name(name: str) -> Optional[dict]:
    """חיפוש שירות לפי שם (case-insensitive)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM services WHERE LOWER(name) = LOWER(?) AND is_active = 1",
            (name.strip(),),
        ).fetchone()
        return dict(row) if row else None


def add_service(name: str, duration_minutes: int = 60) -> int:
    """הוספת שירות חדש. מחזיר את ה-ID."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO services (name, duration_minutes) VALUES (?, ?)",
            (name.strip(), duration_minutes),
        )
        return cur.lastrowid


def update_service(service_id: int, name: str, duration_minutes: int) -> None:
    """עדכון שירות קיים."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE services SET name = ?, duration_minutes = ? WHERE id = ?",
            (name.strip(), duration_minutes, service_id),
        )


def delete_service(service_id: int) -> None:
    """מחיקת שירות."""
    with get_connection() as conn:
        conn.execute("DELETE FROM services WHERE id = ?", (service_id,))


# ─── Live Chats ─────────────────────────────────────────────────────────────

def start_live_chat(user_id: str, username: str = "", channel: str = "telegram") -> int:
    """Start a live chat session for a user. Returns the session ID."""
    with get_connection() as conn:
        # End any existing active session for this user first
        conn.execute(
            "UPDATE live_chats SET is_active=0, ended_at=datetime('now') WHERE user_id=? AND is_active=1",
            (user_id,)
        )
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO live_chats (user_id, username, channel) VALUES (?, ?, ?)",
            (user_id, username, channel)
        )
        return cursor.lastrowid


def touch_live_chat(user_id: str) -> None:
    """עדכון זמן הפעילות האחרונה של שיחה חיה — למניעת timeout על שיחות פעילות."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE live_chats SET updated_at = datetime('now') WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )


def end_live_chat(user_id: str):
    """End the active live chat session for a user."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE live_chats SET is_active=0, ended_at=datetime('now') WHERE user_id=? AND is_active=1",
            (user_id,)
        )


def get_active_live_chat(user_id: str) -> Optional[dict]:
    """Get the active live chat session for a user, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM live_chats WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None


def is_live_chat_active(user_id: str) -> bool:
    """Check if a user has an active live chat session."""
    return get_active_live_chat(user_id) is not None


def get_all_active_live_chats() -> list[dict]:
    """Get all currently active live chat sessions."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM live_chats WHERE is_active=1 ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def count_active_live_chats() -> int:
    """Count currently active live chat sessions."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM live_chats WHERE is_active=1"
        ).fetchone()
        return int(row["count"]) if row else 0


def get_live_chat_latest_user_messages() -> list[dict]:
    """החזרת ההודעה האחרונה מכל לקוח בשיחה חיה פעילה — לצורך התראות באדמין."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT lc.user_id, lc.username,
                      c.message AS last_message, c.created_at AS last_message_at
               FROM live_chats lc
               LEFT JOIN conversations c ON c.id = (
                   SELECT id FROM conversations
                   WHERE user_id = lc.user_id AND role = 'user'
                   ORDER BY id DESC LIMIT 1
               )
               WHERE lc.is_active = 1
               ORDER BY c.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Unanswered Questions (Knowledge Gaps) ──────────────────────────────────

def save_unanswered_question(user_id: str, username: str, question: str,
                              intent: str = "", channel: str = ""):
    """רישום שאלה שהבוט לא הצליח לענות עליה (RAG לא מצא תוצאות).

    intent ו-channel מאפשרים ניתוח וסינון בפאנל האדמין.
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO unanswered_questions (user_id, username, question, intent, channel) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, question, intent, channel),
        )


def get_unanswered_questions(status: str | None = None, limit: int | None = None) -> list[dict]:
    """Get unanswered questions, optionally filtered by status."""
    query, params = _status_filter_query("unanswered_questions", "*", status, limit, order="created_at DESC")
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count_unanswered_questions(status: str | None = None) -> int:
    """Count unanswered questions, optionally filtered by status."""
    query, params = _status_filter_query("unanswered_questions", "COUNT(*) AS count", status, limit=None)
    with get_connection() as conn:
        row = conn.execute(query, params).fetchone()
        return int(row["count"]) if row else 0


def update_unanswered_question_status(question_id: int, status: str):
    """Update the status of an unanswered question."""
    with get_connection() as conn:
        resolved_at = "datetime('now')" if status in ("resolved", "not_relevant") else "NULL"
        conn.execute(
            f"UPDATE unanswered_questions SET status=?, resolved_at={resolved_at} WHERE id=?",
            (status, question_id),
        )


def get_unanswered_question(question_id: int) -> Optional[dict]:
    """Get a single unanswered question by ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM unanswered_questions WHERE id=?", (question_id,)
        ).fetchone()
        return dict(row) if row else None


# ─── Dashboard Batch Query ─────────────────────────────────────────────────

def get_dashboard_counts() -> dict[str, int]:
    """שאילתה מאוחדת לכל מוני הדשבורד — מצמצם שאילתות נפרדות לאחת."""
    # preferred_date הוא תאריך מקומי (ישראל) — חייבים להשוות לתאריך ישראלי
    from zoneinfo import ZoneInfo
    today_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")
    # pending_appointments — מונה תורים *שטרם נצפו* ע"י בעל העסק (לא רק
    # status='pending'), כדי שגם תורים שאושרו אוטומטית ייצרו בועת התראה
    # בסיידבאר עד שבעל העסק יטען את עמוד התורים.
    query = """
        SELECT
            (SELECT COUNT(*) FROM agent_requests WHERE status = 'pending') AS pending_requests,
            (SELECT COUNT(*) FROM appointments
             WHERE owner_seen = 0 AND status != 'cancelled'
            ) AS pending_appointments,
            (SELECT COUNT(*) FROM unanswered_questions WHERE status = 'open') AS open_knowledge_gaps,
            (SELECT COUNT(*) FROM developer_reports WHERE status = 'open') AS open_reports,
            (SELECT COUNT(*) FROM appointments
             WHERE status = 'confirmed'
               AND preferred_date >= ?
            ) AS upcoming_appointments
    """
    with get_connection() as conn:
        row = conn.execute(query, (today_il,)).fetchone()
        return dict(row) if row else {}


# ─── Business Hours ─────────────────────────────────────────────────────────

def get_all_business_hours() -> list[dict]:
    """Get all business hours entries, ordered by day of week."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM business_hours ORDER BY day_of_week"
        ).fetchall()
        return [dict(r) for r in rows]


def get_business_hours_for_day(day_of_week: int) -> Optional[dict]:
    """Get business hours for a specific day of week (0=Sunday .. 6=Saturday)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM business_hours WHERE day_of_week=?",
            (day_of_week,),
        ).fetchone()
        return dict(row) if row else None


def upsert_business_hours(day_of_week: int, open_time: str, close_time: str, is_closed: bool):
    """Insert or update business hours for a day of week."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO business_hours (day_of_week, open_time, close_time, is_closed)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(day_of_week)
               DO UPDATE SET open_time=excluded.open_time,
                             close_time=excluded.close_time,
                             is_closed=excluded.is_closed""",
            (day_of_week, open_time, close_time, int(is_closed)),
        )


def seed_default_business_hours():
    """Populate default business hours if table is empty."""
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM business_hours").fetchone()["c"]
        if count > 0:
            return
        defaults = [
            # day_of_week, open_time, close_time, is_closed
            (0, "09:00", "19:00", 0),  # Sunday
            (1, "09:00", "19:00", 0),  # Monday
            (2, "09:00", "20:00", 0),  # Tuesday
            (3, "09:00", "19:00", 0),  # Wednesday
            (4, "09:00", "19:00", 0),  # Thursday
            (5, "09:00", "14:00", 0),  # Friday
            (6, None, None, 1),        # Saturday — closed
        ]
        conn.executemany(
            "INSERT INTO business_hours (day_of_week, open_time, close_time, is_closed) VALUES (?, ?, ?, ?)",
            defaults,
        )


# ─── Special Days (Holidays & Exceptions) ───────────────────────────────────

def get_all_special_days() -> list[dict]:
    """Get all special days, ordered by date. מסנן ימים שנמחקו ע״י המשתמש."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM special_days WHERE COALESCE(user_removed, 0) = 0 ORDER BY date"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_special_day_dates_including_removed() -> set[str]:
    """מחזיר את כל התאריכים של ימים מיוחדים — כולל מחוקים — לצורך בדיקת seed."""
    with get_connection() as conn:
        rows = conn.execute("SELECT date FROM special_days").fetchall()
        return {r["date"] for r in rows}


def get_special_day_by_date(date_str: str) -> Optional[dict]:
    """Get a special day entry for a given date (YYYY-MM-DD). מתעלם מימים שנמחקו."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM special_days WHERE date=? AND COALESCE(user_removed, 0) = 0",
            (date_str,),
        ).fetchone()
        return dict(row) if row else None


def is_special_day_user_removed(date_str: str) -> bool:
    """בודק אם יום מיוחד/חג נמחק ידנית ע״י המשתמש."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM special_days WHERE date=? AND user_removed = 1",
            (date_str,),
        ).fetchone()
        return row is not None


def add_special_day(
    date_str: str,
    name: str,
    is_closed: bool = True,
    open_time: str = None,
    close_time: str = None,
    notes: str = "",
) -> int:
    """Add or replace a special day for the given date. Returns the entry ID.

    Uses INSERT OR REPLACE so that admin overrides for an existing date
    (e.g. overriding a seeded holiday) take effect instead of silently
    creating a duplicate.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO special_days (date, name, open_time, close_time, is_closed, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (date_str, name, open_time, close_time, int(is_closed), notes),
        )
        return cursor.lastrowid


def update_special_day(
    special_day_id: int,
    date_str: str,
    name: str,
    is_closed: bool = True,
    open_time: str = None,
    close_time: str = None,
    notes: str = "",
):
    """Update an existing special day. מסיר שורות soft-deleted עם אותו תאריך למניעת התנגשות UNIQUE."""
    with get_connection() as conn:
        # הסרת שורה מחוקה (soft-deleted) עם אותו תאריך — כדי שה-UNIQUE לא יתנגש
        conn.execute(
            "DELETE FROM special_days WHERE date=? AND user_removed=1 AND id!=?",
            (date_str, special_day_id),
        )
        conn.execute(
            """UPDATE special_days
               SET date=?, name=?, open_time=?, close_time=?, is_closed=?, notes=?
               WHERE id=?""",
            (date_str, name, open_time, close_time, int(is_closed), notes, special_day_id),
        )


def delete_special_day(special_day_id: int):
    """סימון יום מיוחד כנמחק (soft delete) — לא יוצג ולא ייזרע מחדש."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE special_days SET user_removed = 1 WHERE id=?",
            (special_day_id,),
        )


def delete_special_days_by_year(year: int) -> int:
    """סימון כל הימים המיוחדים של שנה מסוימת כנמחקים. מחזיר את מספר הרשומות שעודכנו."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE special_days SET user_removed = 1 WHERE date LIKE ? AND COALESCE(user_removed, 0) = 0",
            (f"{year}-%",),
        )
        return cursor.rowcount


# ─── Vacation Mode ──────────────────────────────────────────────────────────

def get_vacation_mode() -> dict:
    """קבלת מצב חופשה נוכחי. מחזיר dict עם is_active, vacation_end_date, vacation_message."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM vacation_mode WHERE id = 1").fetchone()
        if row:
            return dict(row)
        # fallback — לא אמור לקרות כי init_db מכניס שורה
        return {"id": 1, "is_active": 0, "vacation_end_date": "", "vacation_message": "", "updated_at": ""}


def update_vacation_mode(is_active: bool, vacation_end_date: str = "", vacation_message: str = ""):
    """עדכון הגדרות מצב חופשה."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE vacation_mode
               SET is_active = ?, vacation_end_date = ?, vacation_message = ?,
                   updated_at = datetime('now')
               WHERE id = 1""",
            (int(is_active), vacation_end_date, vacation_message),
        )


# ─── Bot Settings (הגדרות בוט — טון וביטויים) ─────────────────────────────

# מקור אמת יחיד — נגזר מהגדרות הטון ב-config.py
VALID_TONES = set(TONE_DEFINITIONS.keys())


# ── מיתוג עסקי (לוגו) ────────────────────────────────────────────────────────
# שמירה כ-blob ב-DB ולא ב-filesystem: אטומי עם הגיבוי, פשוט בטסטים, לא תלוי
# במונט filesystem ספציפי. גודל סביר (≤512x512 PNG ≈ 50-200KB) — לא משפיע על ביצועי SQLite.

def get_business_logo() -> dict | None:
    """החזרת לוגו עסקי שמור — None אם לא הועלה."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT logo_blob, logo_mime_type, logo_uploaded_at "
            "FROM business_branding WHERE id = 1"
        ).fetchone()
        if not row or row["logo_blob"] is None:
            return None
        return {
            "blob": bytes(row["logo_blob"]),
            "mime_type": row["logo_mime_type"] or "image/png",
            "uploaded_at": row["logo_uploaded_at"] or "",
        }


def has_business_logo() -> bool:
    """בדיקה זריזה אם יש לוגו (בלי לטעון את ה-blob לזיכרון)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT logo_blob IS NOT NULL AS has_logo "
            "FROM business_branding WHERE id = 1"
        ).fetchone()
        return bool(row and row["has_logo"])


def set_business_logo(blob: bytes, mime_type: str) -> None:
    """שמירת לוגו עסקי (דורס קיים)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE business_branding "
            "SET logo_blob = ?, logo_mime_type = ?, logo_uploaded_at = datetime('now') "
            "WHERE id = 1",
            (blob, mime_type),
        )


def delete_business_logo() -> None:
    """מחיקת הלוגו (ה-row נשאר, רק השדות מתאפסים)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE business_branding "
            "SET logo_blob = NULL, logo_mime_type = NULL, logo_uploaded_at = NULL "
            "WHERE id = 1"
        )


def get_bot_settings() -> dict:
    """קבלת הגדרות הבוט — טון תקשורת וביטויים מותאמים."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM bot_settings WHERE id = 1").fetchone()
        if row:
            return dict(row)
        # fallback — לא אמור לקרות כי init_db מכניס שורה
        return {"id": 1, "tone": "friendly", "custom_phrases": "",
                "custom_prompt": "", "full_system_prompt": "",
                "business_phone": "", "business_address": "",
                "business_website": "",
                "reminder_enabled": 1, "reminder_time": "10:00",
                "second_reminder_enabled": 0,
                "second_reminder_hours": 2.0,
                "referral_enabled": 0, "referral_discount": 10.0,
                "referral_validity_days": 60, "ics_enabled": 1,
                "auto_booking_mode": "manual",
                "auto_booking_max_days_ahead": 90,
                "auto_booking_buffer_after_event_minutes": 0,
                "updated_at": ""}


VALID_AUTO_BOOKING_MODES = {"manual", "auto_with_check", "auto_always"}


def get_auto_booking_buffer_minutes() -> int:
    """החזרת buffer הדקות שמרחיבים אחרי כל אירוע חיצוני ביומן.

    משמש גם בהצגת זמינות ללקוח (calendar keyboard / get_available_slots)
    וגם בהחלטת auto-booking — חשוב שאותו ערך ישמש בכל הנקודות, אחרת
    הלקוח יראה 10:00 כפנוי אבל ההחלטה תדחה אותו.
    """
    s = get_bot_settings() or {}
    try:
        return max(0, int(s.get("auto_booking_buffer_after_event_minutes") or 0))
    except (ValueError, TypeError):
        return 0


def update_bot_settings(
    tone: str,
    custom_phrases: str = "",
    reminder_enabled: bool | None = None,
    reminder_time: str | None = None,
    second_reminder_enabled: bool | None = None,
    second_reminder_hours: float | None = None,
    custom_prompt: str | None = None,
    referral_enabled: bool | None = None,
    referral_discount: float | None = None,
    referral_validity_days: int | None = None,
    ics_enabled: bool | None = None,
    auto_booking_mode: str | None = None,
    auto_booking_max_days_ahead: int | None = None,
    auto_booking_buffer_after_event_minutes: int | None = None,
):
    """עדכון הגדרות הבוט — טון, ביטויים, פרומפט מותאם, תזכורות, הפניות, ICS, ואישור תורים אוטומטי."""
    if tone not in VALID_TONES:
        logger.error("Invalid tone value: %s", tone)
        return
    if auto_booking_mode is not None and auto_booking_mode not in VALID_AUTO_BOOKING_MODES:
        logger.error("Invalid auto_booking_mode: %s", auto_booking_mode)
        return
    if auto_booking_buffer_after_event_minutes is not None and not (
        0 <= int(auto_booking_buffer_after_event_minutes) <= 240
    ):
        logger.error(
            "Invalid auto_booking_buffer_after_event_minutes: %s",
            auto_booking_buffer_after_event_minutes,
        )
        return
    with get_connection() as conn:
        conn.execute(
            """UPDATE bot_settings
               SET tone = ?, custom_phrases = ?,
                   custom_prompt = COALESCE(?, custom_prompt),
                   reminder_enabled = COALESCE(?, reminder_enabled),
                   reminder_time = COALESCE(?, reminder_time),
                   second_reminder_enabled = COALESCE(?, second_reminder_enabled),
                   second_reminder_hours = COALESCE(?, second_reminder_hours),
                   referral_enabled = COALESCE(?, referral_enabled),
                   referral_discount = COALESCE(?, referral_discount),
                   referral_validity_days = COALESCE(?, referral_validity_days),
                   ics_enabled = COALESCE(?, ics_enabled),
                   auto_booking_mode = COALESCE(?, auto_booking_mode),
                   auto_booking_max_days_ahead = COALESCE(?, auto_booking_max_days_ahead),
                   auto_booking_buffer_after_event_minutes = COALESCE(?, auto_booking_buffer_after_event_minutes),
                   updated_at = datetime('now')
               WHERE id = 1""",
            (tone, custom_phrases,
             custom_prompt,
             int(reminder_enabled) if reminder_enabled is not None else None,
             reminder_time,
             int(second_reminder_enabled) if second_reminder_enabled is not None else None,
             second_reminder_hours,
             int(referral_enabled) if referral_enabled is not None else None,
             referral_discount,
             referral_validity_days,
             int(ics_enabled) if ics_enabled is not None else None,
             auto_booking_mode,
             auto_booking_max_days_ahead,
             int(auto_booking_buffer_after_event_minutes) if auto_booking_buffer_after_event_minutes is not None else None),
        )


def update_full_system_prompt(full_system_prompt: str) -> None:
    """שמירת/איפוס פרומפט מערכת מלא (override על הפרומפט שנבנה מהקוד)."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE bot_settings
               SET full_system_prompt = ?, updated_at = datetime('now')
               WHERE id = 1""",
            (full_system_prompt,),
        )


def update_business_identity(
    phone: str | None = None,
    address: str | None = None,
    website: str | None = None,
) -> None:
    """עדכון פרטי כרטיס הביקור של ה-tenant (טלפון/כתובת/אתר).

    None = לא לגעת בשדה; מחרוזת ריקה = ניקוי מפורש (חוזרים ל-fallback של env).
    הערכים נצרכים בזמן-ריצה דרך config.get_business_config(). שם העסק אינו
    כאן — מקורו display_name ב-control plane (נקבע בהקמה).
    """
    with get_connection() as conn:
        conn.execute(
            """UPDATE bot_settings
               SET business_phone = COALESCE(?, business_phone),
                   business_address = COALESCE(?, business_address),
                   business_website = COALESCE(?, business_website),
                   updated_at = datetime('now')
               WHERE id = 1""",
            (phone, address, website),
        )


# ─── Referrals (מערכת הפניות) ────────────────────────────────────────────

def generate_referral_code(user_id: str) -> str:
    """יצירת קוד הפניה ייחודי למשתמש. אם כבר קיים — מחזיר את הקוד הקיים.

    הקוד נשמר ב-referral_codes ונשאר קבוע — ניתן לשימוש חוזר עבור הפניות מרובות.
    """
    import hashlib

    existing = get_user_referral_code(user_id)
    if existing:
        return existing

    raw = f"{user_id}_{datetime.now().isoformat()}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:8].upper()
    code = f"REF_{short_hash}"

    try:
        with get_connection() as conn:
            # וידוא ייחודיות (מקרה קצה נדיר של התנגשות)
            while conn.execute("SELECT 1 FROM referral_codes WHERE code = ?", (code,)).fetchone():
                raw += "_retry"
                short_hash = hashlib.sha256(raw.encode()).hexdigest()[:8].upper()
                code = f"REF_{short_hash}"

            conn.execute(
                "INSERT INTO referral_codes (user_id, code) VALUES (?, ?)",
                (user_id, code),
            )
    except sqlite3.IntegrityError:
        # race condition — תהליך אחר יצר קוד בו-זמנית
        existing = get_user_referral_code(user_id)
        if existing:
            return existing
        logger.error("Failed to generate referral code for user %s", user_id)
        return ""

    return code


def get_referral_by_code(code: str) -> Optional[dict]:
    """חיפוש קוד הפניה ב-referral_codes."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM referral_codes WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def register_referral(code: str, referred_id: str) -> bool:
    """רישום הפניה — יוצר רשומת הפניה חדשה המקשרת את המשתמש לקוד.

    מחזיר True אם הרישום הצליח, False אם הקוד לא קיים,
    המשתמש מנסה להפנות את עצמו, או שכבר הופנה ע"י מישהו.
    """
    with get_connection() as conn:
        # חיפוש המפנה לפי הקוד ב-referral_codes
        code_row = conn.execute(
            "SELECT user_id FROM referral_codes WHERE code = ?", (code,)
        ).fetchone()
        if not code_row:
            return False
        referrer_id = code_row["user_id"]

        # לא מאפשרים הפניה עצמית
        if referrer_id == referred_id:
            return False

        # UNIQUE(referred_id) מבטיח ברמת ה-DB שכל משתמש מופנה רק פעם אחת.
        # INSERT OR IGNORE מחזיר rowcount=0 אם referred_id כבר קיים.
        cursor = conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, code) VALUES (?, ?, ?)",
            (referrer_id, referred_id, code),
        )
        return cursor.rowcount > 0


def complete_referral(referred_id: str) -> bool:
    """הפעלת ההפניה — נקרא לאחר שהלקוח המופנה השלים תור ראשון.

    יוצר זיכויים (credits) לשני הצדדים לפי הגדרות בוט (אחוז הנחה ותקופת תוקף).
    מחזיר True אם הופעל בהצלחה.
    """
    settings = get_bot_settings()
    discount = settings.get("referral_discount", 10.0)
    validity_days = settings.get("referral_validity_days", 60)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM referrals WHERE referred_id = ? AND status = 'pending'",
            (referred_id,),
        ).fetchone()
        if not row:
            return False

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=validity_days)).strftime("%Y-%m-%d %H:%M:%S")

        # סימון אטומי — AND status = 'pending' מונע כפילות בקריאות מקבילות
        cursor = conn.execute(
            "UPDATE referrals SET status = 'completed', completed_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cursor.rowcount == 0:
            return False

        # זיכוי למפנה
        conn.execute(
            "INSERT INTO credits (user_id, amount, type, reason, expires_at) VALUES (?, ?, ?, ?, ?)",
            (row["referrer_id"], discount, "referrer", f"הפניית לקוח חדש (קוד: {row['code']})", expires_at),
        )

        # זיכוי למופנה
        conn.execute(
            "INSERT INTO credits (user_id, amount, type, reason, expires_at) VALUES (?, ?, ?, ?, ?)",
            (referred_id, discount, "referred", f"הצטרפות דרך הפניה (קוד: {row['code']})", expires_at),
        )

        return True


def get_user_referral_code(user_id: str) -> Optional[str]:
    """החזרת קוד ההפניה של משתמש (אם קיים)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["code"] if row else None


def is_referral_code_sent(user_id: str) -> bool:
    """בדיקה האם קוד ההפניה כבר נשלח למשתמש."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT sent FROM referral_codes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row["sent"])


def mark_referral_code_as_sent(user_id: str) -> bool:
    """סימון אטומי שקוד ההפניה נשלח. מחזיר True רק אם הצליח לתפוס את הנעילה.

    משמש למניעת race condition — רק תהליך אחד (בוט או אדמין) מצליח לסמן
    sent=1 כש-sent=0, ורק הוא שולח את ההודעה.
    אם השליחה נכשלת — יש לקרוא ל-unmark_referral_code_sent כדי לאפשר ניסיון חוזר.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE referral_codes SET sent = 1 WHERE user_id = ? AND sent = 0",
            (user_id,),
        )
        return cursor.rowcount > 0


def unmark_referral_code_sent(user_id: str):
    """ביטול דגל השליחה — נקרא כשמשלוח ההודעה נכשל, כדי לאפשר ניסיון חוזר."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE referral_codes SET sent = 0 WHERE user_id = ?",
            (user_id,),
        )


def get_active_credits(user_id: str) -> list[dict]:
    """החזרת זיכויים פעילים (לא נוצלו ולא פגו) של משתמש."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM credits
               WHERE user_id = ? AND used = 0 AND expires_at > datetime('now')
               ORDER BY expires_at ASC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def use_credit(credit_id: int):
    """סימון זיכוי כמנוצל."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE credits SET used = 1 WHERE id = ?",
            (credit_id,),
        )


def count_referrals(user_id: str, status: str | None = None) -> int:
    """ספירת הפניות של משתמש מפנה."""
    with get_connection() as conn:
        if status:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id = ? AND status = ?",
                (user_id, status),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["count"]) if row else 0


def get_referral_stats() -> dict:
    """סטטיסטיקות הפניות לדשבורד האדמין."""
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals"
        ).fetchone()["c"]
        completed = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE status = 'completed'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE status = 'pending'"
        ).fetchone()["c"]
        active_credits = conn.execute(
            "SELECT COUNT(*) AS c FROM credits WHERE used = 0 AND expires_at > datetime('now')"
        ).fetchone()["c"]
        return {
            "total_referrals": total,
            "completed_referrals": completed,
            "pending_referrals": pending,
            "active_credits": active_credits,
        }


def get_top_referrers(limit: int = 10) -> list[dict]:
    """החזרת מפנים מובילים (לפי כמות הפניות שהושלמו)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.referrer_id,
                      COUNT(*) AS total_referrals,
                      SUM(CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END) AS completed_referrals
               FROM referrals r
               GROUP BY r.referrer_id
               ORDER BY completed_referrals DESC, total_referrals DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_referrals(limit: int | None = None) -> list[dict]:
    """החזרת כל ההפניות לפאנל האדמין."""
    with get_connection() as conn:
        query = """SELECT r.*,
                          c_referrer.username AS referrer_name,
                          c_referred.username AS referred_name
                   FROM referrals r
                   LEFT JOIN (SELECT user_id, username FROM conversations WHERE username != ''
                              GROUP BY user_id) c_referrer ON r.referrer_id = c_referrer.user_id
                   LEFT JOIN (SELECT user_id, username FROM conversations WHERE username != ''
                              GROUP BY user_id) c_referred ON r.referred_id = c_referred.user_id
                   ORDER BY r.created_at DESC"""
        params: list[object] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def has_pending_referral(user_id: str) -> bool:
    """בדיקה האם למשתמש יש הפניה ממתינה (נרשם דרך קוד אבל עוד לא השלים תור)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        return row is not None


def has_completed_appointment(user_id: str) -> bool:
    """בדיקה האם למשתמש יש לפחות תור אחד שהושלם (confirmed)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM appointments WHERE user_id = ? AND status = 'confirmed'",
            (user_id,),
        ).fetchone()
        return row is not None


# ─── Broadcast (הודעות יזומות) ─────────────────────────────────────────────

def create_broadcast(
    message_text: str,
    audience: str,
    total_recipients: int,
    recipients: list[str] | None = None,
) -> int:
    """יצירת הודעת שידור חדשה. מחזיר את ה-ID.

    כש-audience='custom' ו-recipients מסופק — שומרים את רשימת ה-user_ids
    בטבלת broadcast_message_recipients, כדי שניתן יהיה להציג בהיסטוריה
    את שמות הלקוחות שנכללו בקהל המותאם אישית.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO broadcast_messages (message_text, audience, total_recipients) "
            "VALUES (?, ?, ?)",
            (message_text, audience, total_recipients),
        )
        broadcast_id = cursor.lastrowid

        # שמירת רשימת נמענים — רק עבור קהל מותאם אישית
        if audience == "custom" and recipients:
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO broadcast_message_recipients "
                    "(broadcast_id, user_id) VALUES (?, ?)",
                    [(broadcast_id, uid) for uid in recipients],
                )
            except Exception:
                logger.error(
                    "create_broadcast: שגיאה בשמירת נמענים לקהל מותאם (broadcast_id=%s)",
                    broadcast_id,
                    exc_info=True,
                )

        return broadcast_id


def get_broadcast_recipient_users(broadcast_id: int) -> list[dict]:
    """שליפת רשימת לקוחות שנכללו בשידור 'קהל מותאם אישית'.

    מחזיר list של dicts עם user_id, username, channel — ממוין לפי שם משתמש.
    LEFT JOIN על users כדי לכלול גם נמענים שנמחקו מטבלת users (יוצגו בלי שם).
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT bmr.user_id,
                   COALESCE(u.username, '') AS username,
                   COALESCE(u.channel, '')  AS channel
            FROM broadcast_message_recipients bmr
            LEFT JOIN users u ON u.user_id = bmr.user_id
            WHERE bmr.broadcast_id = ?
            ORDER BY (CASE WHEN u.username IS NULL OR u.username = '' THEN 1 ELSE 0 END),
                     u.username COLLATE NOCASE,
                     bmr.user_id
            """,
            (broadcast_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_broadcast(broadcast_id: int) -> dict | None:
    """שליפת רשומת שידור בודדת לפי ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM broadcast_messages WHERE id = ?",
            (broadcast_id,),
        ).fetchone()
        return dict(row) if row else None


def get_all_broadcasts(limit: int = 50) -> list[dict]:
    """קבלת כל הודעות השידור, מהחדשה לישנה."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM broadcast_messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_broadcast_progress(broadcast_id: int, sent_count: int, failed_count: int):
    """עדכון התקדמות שליחת שידור."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE broadcast_messages SET sent_count = ?, failed_count = ?, "
            "status = 'sending' WHERE id = ?",
            (sent_count, failed_count, broadcast_id),
        )


def complete_broadcast(broadcast_id: int, sent_count: int, failed_count: int):
    """סיום שידור — סימון כהושלם עם הסטטיסטיקות הסופיות."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE broadcast_messages SET sent_count = ?, failed_count = ?, "
            "status = 'completed', completed_at = datetime('now') WHERE id = ?",
            (sent_count, failed_count, broadcast_id),
        )


def fail_broadcast(broadcast_id: int, sent_count: int | None = None, failed_count: int | None = None):
    """סימון שידור ככישלון.

    אם sent_count/failed_count הם None — שומר על הערכים שכבר ב-DB
    (שנכתבו ע"י update_broadcast_progress במהלך השליחה).
    """
    with get_connection() as conn:
        if sent_count is not None and failed_count is not None:
            conn.execute(
                "UPDATE broadcast_messages SET sent_count = ?, failed_count = ?, "
                "status = 'failed', completed_at = datetime('now') WHERE id = ?",
                (sent_count, failed_count, broadcast_id),
            )
        else:
            conn.execute(
                "UPDATE broadcast_messages SET status = 'failed', "
                "completed_at = datetime('now') WHERE id = ?",
                (broadcast_id,),
            )


def mark_broadcast_sending(broadcast_id: int):
    """סימון שידור כ-sending — נקרא בתחילת השליחה בפועל."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE broadcast_messages SET status = 'sending' WHERE id = ? AND status = 'queued'",
            (broadcast_id,),
        )


# ─── User Subscriptions (הרשמה/ביטול הרשמה) ───────────────────────────────

def ensure_user_subscribed(user_id: str):
    """רישום משתמש כמנוי (נקרא בכל אינטראקציה ראשונה).

    אם המשתמש כבר קיים — לא משנה את הסטטוס שלו (אולי ביטל הרשמה).
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_subscriptions (user_id) VALUES (?)",
            (user_id,),
        )


def get_consecutive_fallbacks(user_id: str) -> int:
    """קבלת מונה fallbacks רצופים למשתמש (לשימוש ב-WhatsApp stateless)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_fallbacks FROM user_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["consecutive_fallbacks"] if row else 0


def set_consecutive_fallbacks(user_id: str, count: int) -> None:
    """עדכון מונה fallbacks רצופים למשתמש."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_subscriptions SET consecutive_fallbacks = ? WHERE user_id = ?",
            (count, user_id),
        )


def unsubscribe_user(user_id: str):
    """ביטול הרשמת משתמש לשידורים."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_subscriptions (user_id, is_subscribed, updated_at) "
            "VALUES (?, 0, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET is_subscribed = 0, updated_at = datetime('now')",
            (user_id,),
        )


def resubscribe_user(user_id: str):
    """החזרת הרשמת משתמש לשידורים."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_subscriptions (user_id, is_subscribed, updated_at) "
            "VALUES (?, 1, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET is_subscribed = 1, updated_at = datetime('now')",
            (user_id,),
        )


def is_user_subscribed(user_id: str) -> bool:
    """בדיקה האם משתמש רשום לקבלת שידורים (ברירת מחדל: כן)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_subscribed FROM user_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        # אם לא קיים — ברירת מחדל רשום
        return bool(row["is_subscribed"]) if row else True


def _broadcast_audience_sql(audience: str) -> tuple[str, str]:
    """בניית חלקי ה-SQL המשותפים לפי סוג קהל.

    מחזיר (join_clause, where_clause) — משותפים ל-get ול-count.
    """
    base_join = "LEFT JOIN user_subscriptions us ON c.user_id = us.user_id"
    base_where = "COALESCE(us.is_subscribed, 1) = 1"

    if audience == "booked":
        join = base_join
        where = f"EXISTS (SELECT 1 FROM appointments a WHERE a.user_id = c.user_id)\n                  AND {base_where}"
    elif audience == "recent":
        join = base_join
        where = f"c.created_at >= datetime('now', '-30 days')\n                  AND {base_where}"
    else:  # all
        join = base_join
        where = base_where

    return join, where


def get_broadcast_recipients(audience: str) -> list[str]:
    """קבלת רשימת user_ids לשידור לפי סוג קהל.

    - all: כל המשתמשים שדיברו עם הבוט (פרט למי שביטל הרשמה)
    - booked: רק מי שקבע תור (אי פעם)
    - recent: רק מי שהיה פעיל ב-30 הימים האחרונים
    """
    join, where = _broadcast_audience_sql(audience)
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT c.user_id
            FROM conversations c
            {join}
            WHERE {where}
        """).fetchall()
        return [r["user_id"] for r in rows]


def get_broadcast_recipients_with_channel(audience: str) -> list[dict]:
    """קבלת רשימת נמענים כולל ערוץ — לשידורים מרובי-ערוצים.

    מחזיר רשימת dicts עם user_id ו-channel.
    הערוץ נלקח מההודעה האחרונה של המשתמש (ההנחה: המשתמש פעיל בערוץ אחד).
    """
    join, where = _broadcast_audience_sql(audience)
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT c.user_id, COALESCE(c.channel, 'telegram') AS channel
            FROM conversations c
            INNER JOIN (
                SELECT user_id, MAX(id) AS max_id FROM conversations GROUP BY user_id
            ) latest ON c.id = latest.max_id
            {join}
            WHERE {where}
        """).fetchall()
        return [{"user_id": r["user_id"], "channel": r["channel"]} for r in rows]


def count_broadcast_recipients(audience: str) -> int:
    """ספירת נמענים פוטנציאליים לשידור (ללא שליחה בפועל).

    משתמש ב-COUNT ברמת ה-SQL במקום לטעון את כל הרשומות לזיכרון.
    """
    join, where = _broadcast_audience_sql(audience)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT COUNT(DISTINCT c.user_id) AS cnt
            FROM conversations c
            {join}
            WHERE {where}
        """).fetchone()
        return int(row["cnt"]) if row else 0


def get_custom_recipients_with_channel(user_ids: list[str]) -> list[dict]:
    """קבלת נמענים לפי רשימת user_ids ספציפית (לברודקאסט custom).

    מחזיר רשימת dicts עם user_id ו-channel מטבלת users.
    מסנן רק מנויים (לא ביטלו הרשמה).
    """
    if not user_ids:
        return []
    placeholders = ",".join("?" * len(user_ids))
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT u.user_id, u.channel
            FROM users u
            LEFT JOIN user_subscriptions us ON u.user_id = us.user_id
            WHERE u.user_id IN ({placeholders})
              AND COALESCE(us.is_subscribed, 1) = 1
        """, user_ids).fetchall()
        return [{"user_id": r["user_id"], "channel": r["channel"]} for r in rows]


# ─── Engagement Queries ──────────────────────────────────────────────────────

def check_high_engagement(user_id: str) -> bool:
    """בדיקת מעורבות גבוהה — האם למשתמש יש 10+ הודעות ב-30 דקות או 20+ ביום.

    שאילתה אחת עם SUM(CASE WHEN ...) למניעת שני סריקות נפרדות.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN created_at >= datetime('now', '-30 minutes') THEN 1 ELSE 0 END) AS cnt_30m,
                SUM(CASE WHEN created_at >= datetime('now', '-1 day') THEN 1 ELSE 0 END) AS cnt_1d
            FROM conversations
            WHERE user_id = ? AND role = 'user'
              AND created_at >= datetime('now', '-1 day')
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return False
        return (int(row["cnt_30m"] or 0) >= 10) or (int(row["cnt_1d"] or 0) >= 20)


# ─── Analytics ──────────────────────────────────────────────────────────────


def get_analytics_summary(days: int = 30) -> dict:
    """נתוני סיכום אנליטיים לתקופה נתונה — שאילתה מאוחדת אחת."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(CASE WHEN role = 'user' THEN 1 END) AS total_user_messages,
                COUNT(CASE WHEN role = 'assistant' THEN 1 END) AS total_bot_messages,
                COUNT(DISTINCT CASE WHEN role = 'user' THEN user_id END) AS unique_users
            FROM conversations
            WHERE created_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchone()
        summary = dict(row) if row else {
            "total_user_messages": 0, "total_bot_messages": 0, "unique_users": 0,
        }

        # פערי ידע ובקשות נציג בתקופה
        counts_row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM unanswered_questions
                 WHERE created_at >= datetime('now', ?)) AS unanswered_count,
                (SELECT COUNT(*) FROM agent_requests
                 WHERE created_at >= datetime('now', ?)) AS agent_request_count
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchone()
        summary.update(dict(counts_row) if counts_row else {})

        # אחוז fallback — הודעות משתמש שגרמו ל-unanswered
        total_user = summary.get("total_user_messages", 0)
        unanswered = summary.get("unanswered_count", 0)
        summary["fallback_rate"] = round(
            (unanswered / total_user * 100) if total_user > 0 else 0, 1
        )
        return summary


def get_daily_message_counts(days: int = 30) -> list[dict]:
    """מספר הודעות לפי יום בשעון ישראל — לגרף טרנד.

    SQLite שומר UTC. ההמרה לשעון ישראל (כולל שעון קיץ) נעשית ב-Python
    כדי שגבולות הימים ישקפו את הפעילות האמיתית של הלקוחות.
    """
    from zoneinfo import ZoneInfo

    israel_tz = ZoneInfo("Asia/Jerusalem")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT created_at, user_id
            FROM conversations
            WHERE role = 'user'
              AND created_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchall()

        # קיבוץ לפי יום בשעון ישראל
        day_data: dict[str, dict] = {}
        for r in rows:
            try:
                utc_dt = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                local_day = utc_dt.astimezone(israel_tz).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                logger.error("שגיאה בפירוש תאריך בספירה יומית: %s",
                             r["created_at"])
                continue

            if local_day not in day_data:
                day_data[local_day] = {"user_messages": 0, "user_ids": set()}
            entry = day_data[local_day]
            entry["user_messages"] += 1
            entry["user_ids"].add(r["user_id"])

        return [
            {
                "day": day,
                "user_messages": d["user_messages"],
                "unique_users": len(d["user_ids"]),
            }
            for day, d in sorted(day_data.items())
        ]


def get_hourly_distribution(days: int = 30) -> list[dict]:
    """התפלגות הודעות לפי שעה ביום בשעון ישראל — לזיהוי שעות עומס.

    SQLite שומר UTC. ההמרה לשעון ישראל (כולל שעון קיץ) נעשית ב-Python
    כדי לשקף את השעות האמיתיות שבהן הלקוחות פעילים.
    """
    from zoneinfo import ZoneInfo

    israel_tz = ZoneInfo("Asia/Jerusalem")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT created_at
            FROM conversations
            WHERE role = 'user'
              AND created_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchall()
        # המרת כל timestamp לשעה מקומית וספירה
        hour_counts: dict[int, int] = {}
        for r in rows:
            try:
                utc_dt = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                local_hour = utc_dt.astimezone(israel_tz).hour
                hour_counts[local_hour] = hour_counts.get(local_hour, 0) + 1
            except (ValueError, TypeError):
                logger.error("שגיאה בפירוש תאריך בהתפלגות שעתית: %s", r["created_at"])
        return [{"hour": h, "message_count": hour_counts.get(h, 0)} for h in range(24)]


def get_top_unanswered_questions(days: int = 30, limit: int = 10) -> list[dict]:
    """שאלות שחוזרות על עצמן בפערי ידע — לזיהוי נושאים חמים."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT question, COUNT(*) AS ask_count,
                   MAX(created_at) AS last_asked,
                   MIN(status) AS status
            FROM unanswered_questions
            WHERE created_at >= datetime('now', ?)
            GROUP BY question
            ORDER BY ask_count DESC, last_asked DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_engagement_stats(days: int = 30) -> dict:
    """סטטיסטיקות מעורבות משתמשים — ממוצע הודעות, שיחות קצרות וחוזרים."""
    with get_connection() as conn:
        # ממוצע הודעות למשתמש
        row = conn.execute(
            """
            SELECT
                AVG(msg_count) AS avg_messages_per_user,
                COUNT(*) AS total_users,
                COUNT(CASE WHEN msg_count = 1 THEN 1 END) AS single_message_users,
                COUNT(CASE WHEN msg_count >= 5 THEN 1 END) AS engaged_users
            FROM (
                SELECT user_id, COUNT(*) AS msg_count
                FROM conversations
                WHERE role = 'user'
                  AND created_at >= datetime('now', ?)
                GROUP BY user_id
            )
            """,
            (f"-{days} days",),
        ).fetchone()
        stats = dict(row) if row else {}
        stats["avg_messages_per_user"] = round(
            float(stats.get("avg_messages_per_user") or 0), 1
        )

        # משתמשים חוזרים — מישהו שהיה פעיל גם לפני התקופה הנוכחית
        returning = conn.execute(
            """
            SELECT COUNT(DISTINCT c1.user_id) AS returning_users
            FROM conversations c1
            WHERE c1.role = 'user'
              AND c1.created_at >= datetime('now', ?)
              AND EXISTS (
                  SELECT 1 FROM conversations c2
                  WHERE c2.user_id = c1.user_id
                    AND c2.role = 'user'
                    AND c2.created_at < datetime('now', ?)
              )
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchone()
        stats["returning_users"] = returning["returning_users"] if returning else 0

        return stats


def get_conversations_with_drop_off(days: int = 30, limit: int = 10) -> list[dict]:
    """שיחות שבהן המשתמש שלח הודעה אחת בלבד ונטש — לאבחון drop-off.

    מחזיר את ההודעה האחרונה של כל משתמש עם הודעה יחידה, כדי לאפשר drill-down.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.user_id, c.username, c.message, c.created_at
            FROM conversations c
            INNER JOIN (
                SELECT user_id
                FROM conversations
                WHERE role = 'user'
                  AND created_at >= datetime('now', ?)
                GROUP BY user_id
                HAVING COUNT(*) = 1
            ) single ON c.user_id = single.user_id
            WHERE c.role = 'user'
              AND c.created_at >= datetime('now', ?)
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (f"-{days} days", f"-{days} days", limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Lead Follow-ups (מעקב לידים) ────────────────────────────────────────


def create_lead_followup(
    user_id: str,
    followup_due_at: str,
    *,
    username: str = "",
    channel: str = "telegram",
    service_of_interest: str = "",
    intent_type: str = "unknown",
    lead_temperature: str = "warm",
    conversation_summary: str = "",
    analysis_json: str = "{}",
    status: str = "pending",
    stop_reason: str = "",
) -> int:
    """יצירת רשומת follow-up חדשה. מחזיר את ה-ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO lead_followups
               (user_id, username, channel, service_of_interest, intent_type,
                lead_temperature, conversation_summary, analysis_json,
                followup_due_at, status, stop_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, channel, service_of_interest, intent_type,
             lead_temperature, conversation_summary, analysis_json,
             followup_due_at, status, stop_reason),
        )
        return cursor.lastrowid


def get_pending_followups(due_before: str | None = None, limit: int = 50) -> list[dict]:
    """שליפת follow-ups בסטטוס pending שהגיע זמנם.

    Args:
        due_before: תאריך-שעה (ISO) — שולפים רק רשומות שה-due_at שלהן לפני הערך הזה.
                    אם None — שולפים הכל ב-pending.
        limit: מגבלת תוצאות.
    """
    with get_connection() as conn:
        if due_before:
            rows = conn.execute(
                "SELECT * FROM lead_followups WHERE status = 'pending' "
                "AND followup_due_at <= ? ORDER BY followup_due_at ASC LIMIT ?",
                (due_before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lead_followups WHERE status = 'pending' "
                "ORDER BY followup_due_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def update_followup_status(
    followup_id: int,
    status: str,
    *,
    template_key: str | None = None,
    template_variables: str | None = None,
    stop_reason: str = "",
) -> None:
    """עדכון סטטוס follow-up (approved/sent/cancelled/expired)."""
    with get_connection() as conn:
        parts = ["status = ?"]
        params: list = [status]
        if status == "sent":
            parts.append("followup_sent_at = datetime('now')")
        if template_key is not None:
            parts.append("template_key = ?")
            params.append(template_key)
        if template_variables is not None:
            parts.append("template_variables = ?")
            params.append(template_variables)
        if stop_reason:
            parts.append("stop_reason = ?")
            params.append(stop_reason)
        params.append(followup_id)
        conn.execute(
            f"UPDATE lead_followups SET {', '.join(parts)} WHERE id = ?",
            params,
        )


def mark_followup_replied(user_id: str) -> bool:
    """סימון שמשתמש הגיב אחרי שקיבל follow-up.

    מחפש follow-up אחרון בסטטוס 'sent' ומעדכן ל-'replied'.
    מחזיר True אם נמצא ועודכן.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM lead_followups WHERE user_id = ? AND status = 'sent' "
            "ORDER BY followup_sent_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE lead_followups SET status = 'replied', user_replied = 1, "
                "user_replied_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            return True
        return False


def mark_followup_converted(user_id: str) -> bool:
    """סימון שמשתמש ביצע הזמנה אחרי follow-up.

    מחפש follow-up בסטטוס 'sent' או 'replied' ומעדכן ל-'converted'.
    משתמש יכול להזמין ישירות מהכפתור בלי לעבור דרך message_handler,
    ולכן הסטטוס עשוי להישאר 'sent' (בלי שלב 'replied' ביניהם).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM lead_followups WHERE user_id = ? AND status IN ('sent', 'replied') "
            "ORDER BY followup_sent_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE lead_followups SET status = 'converted', "
                "booking_after_followup = 1 WHERE id = ?",
                (row["id"],),
            )
            return True
        return False


def has_pending_or_sent_followup(user_id: str) -> bool:
    """בדיקה אם כבר יש follow-up פעיל או שנשלח/בוטל לאחרונה למשתמש.

    כולל גם סטטוסים 'replied' ו-'converted' כדי למנוע לולאת ספאם,
    וגם 'cancelled' מהיממה האחרונה כדי למנוע קריאות LLM חוזרות על
    לידים קרים שלא מייצרים רשומת pending.
    """
    with get_connection() as conn:
        # סטטוסים פעילים — תמיד חוסמים
        row = conn.execute(
            "SELECT 1 FROM lead_followups WHERE user_id = ? "
            "AND status IN ('pending', 'approved', 'sent', 'replied', 'converted') LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return True
        # cancelled/expired — חוסמים רק אם נוצרו ביממה האחרונה
        row = conn.execute(
            "SELECT 1 FROM lead_followups WHERE user_id = ? "
            "AND status IN ('cancelled', 'expired') "
            "AND created_at >= datetime('now', '-24 hours') LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None


def has_recent_booking(user_id: str, hours: int = 48) -> bool:
    """בדיקה אם למשתמש יש תור שנוצר לאחרונה (pending/confirmed)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM appointments WHERE user_id = ? "
            "AND status IN ('pending', 'confirmed') "
            "AND created_at >= datetime('now', ?) LIMIT 1",
            (user_id, f"-{hours} hours"),
        ).fetchone()
        return row is not None


def get_followup_stats() -> dict:
    """סטטיסטיקות KPI של מערכת ה-follow-up."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM lead_followups").fetchone()["c"]
        sent = conn.execute(
            "SELECT COUNT(*) AS c FROM lead_followups WHERE status IN ('sent', 'replied', 'converted')"
        ).fetchone()["c"]
        replied = conn.execute(
            "SELECT COUNT(*) AS c FROM lead_followups WHERE user_replied = 1"
        ).fetchone()["c"]
        converted = conn.execute(
            "SELECT COUNT(*) AS c FROM lead_followups WHERE status = 'converted'"
        ).fetchone()["c"]
        cancelled = conn.execute(
            "SELECT COUNT(*) AS c FROM lead_followups WHERE status = 'cancelled'"
        ).fetchone()["c"]
        return {
            "total": total,
            "sent": sent,
            "replied": replied,
            "converted": converted,
            "cancelled": cancelled,
            "reply_rate": round(replied / sent * 100, 1) if sent else 0,
            "conversion_rate": round(converted / sent * 100, 1) if sent else 0,
        }


def get_all_followups(limit: int = 100) -> list[dict]:
    """שליפת כל ה-follow-ups, מהחדש לישן."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM lead_followups ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def expire_old_followups(max_age_hours: int = 72) -> int:
    """סימון follow-ups ישנים בסטטוס pending כ-expired.

    מחזיר את מספר הרשומות שעודכנו.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE lead_followups SET status = 'expired', "
            "stop_reason = 'expired_timeout' "
            "WHERE status = 'pending' AND followup_due_at < datetime('now', ?)",
            (f"-{max_age_hours} hours",),
        )
        return cursor.rowcount


# ── Developer Reports ────────────────────────────────────────────────────────


def save_developer_report(description: str, screenshot_count: int = 0) -> int:
    """שמירת דיווח באג חדש. מחזיר את ה-ID של הדיווח."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO developer_reports (description, screenshot_count) VALUES (?, ?)",
            (description, screenshot_count),
        )
        return cursor.lastrowid


def get_developer_reports(limit: int = 50) -> list[dict]:
    """שליפת כל הדיווחים, מהחדש לישן."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM developer_reports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_developer_report(report_id: int) -> dict | None:
    """שליפת דיווח בודד לפי ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM developer_reports WHERE id = ?", (report_id,)
        ).fetchone()
        return dict(row) if row else None


def update_developer_report_status(report_id: int, status: str) -> bool:
    """עדכון סטטוס דיווח (open/resolved). מחזיר True אם עודכן."""
    resolved_at = "datetime('now')" if status == "resolved" else "NULL"
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE developer_reports SET status = ?, resolved_at = {resolved_at} "
            "WHERE id = ?",
            (status, report_id),
        )
        return cursor.rowcount > 0


def get_popular_kb_sources(days: int = 30, limit: int = 10) -> list[dict]:
    """מקורות ידע שצוטטו הכי הרבה — לזיהוי תכנים פופולריים.

    מפרק את מחרוזת ה-sources (שמופרדת בפסיקים) למקורות בודדים
    וסופר כל מקור בנפרד, כדי להימנע מכפילויות כשאותו מקור
    מופיע בשילובים שונים.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT sources
            FROM conversations
            WHERE role = 'assistant'
              AND sources IS NOT NULL AND sources != ''
              AND created_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchall()

    # פירוק מחרוזות מופרדות בפסיקים למקורות בודדים וספירה
    from collections import Counter
    counter: Counter[str] = Counter()
    for row in rows:
        for source in row["sources"].split(", "):
            source = source.strip()
            if source:
                counter[source] += 1

    return [
        {"sources": src, "cite_count": cnt}
        for src, cnt in counter.most_common(limit)
    ]


# ── עמודי תשובה ציבוריים (Response Pages) ───────────────────────────────────


def create_response_page(
    content: str,
    title: str = "",
    user_id: str = "",
    *,
    page_type: str = "whatsapp_fallback",
) -> str:
    """יצירת עמוד תשובה ציבורי — מחזיר slug אקראי בן 22 תווים base64url
    (≈128 ביט אנטרופיה).

    page_type — אחד משלושה: 'whatsapp_fallback' (תשתית פנימית, ברירת מחדל),
    'landing' (פיצ'ר Premium — דף נחיתה שיווקי שנוצר ידנית), או 'legacy'
    (רשומות שנוצרו לפני המיגרציה — לא יוצרים חדשות עם ערך זה).
    ראה docs/plans_feature_flags_spec.md סעיף 2.3 ו-docs/privacy_data_matrix.md
    סעיף 16.

    הקודם השתמש ב-uuid.uuid4().hex[:8] (32 ביט בלבד) — נמוך מדי לעמוד
    ציבורי בלי auth שעלול להכיל מידע אישי.
    """
    import secrets
    # secrets.token_urlsafe(16) מחזיר 22 תווים base64url — 128 ביט אנטרופיה.
    page_id = secrets.token_urlsafe(16)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO response_pages (id, content, title, user_id, page_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (page_id, content, title, user_id, page_type),
        )
    return page_id


def get_response_page(page_id: str) -> Optional[dict]:
    """קבלת עמוד תשובה לפי ID. מחזיר None אם לא קיים."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM response_pages WHERE id = ?",
            (page_id,),
        ).fetchone()
        return dict(row) if row else None


# ── WhatsApp Templates (Content API sync) ────────────────────────────────────

# עמודות JSON שנשמרות כמחרוזות — helper לפיצוח לקריאה.
_WA_TEMPLATE_JSON_COLS = ("buttons_json", "variables_json", "raw_json")


def _decode_wa_template_row(row: sqlite3.Row) -> dict:
    """המרה של שורת SQLite למילון עם JSON מפוענח לנוחות ה-UI."""
    data = dict(row)
    for col in _WA_TEMPLATE_JSON_COLS:
        raw = data.get(col)
        if not raw:
            data[col.removesuffix("_json")] = [] if col != "raw_json" else {}
            continue
        try:
            data[col.removesuffix("_json")] = json.loads(raw)
        except (ValueError, TypeError):
            # JSON פגום — לוג ונפילה לערך ריק כדי לא לשבור UI
            logger.error(
                "whatsapp_templates: JSON פגום בעמודה %s עבור content_sid=%s",
                col,
                data.get("content_sid"),
            )
            data[col.removesuffix("_json")] = [] if col != "raw_json" else {}
    return data


def upsert_whatsapp_template(template: dict) -> None:
    """שמירה או עדכון של תבנית WhatsApp לפי content_sid.

    Args:
        template: מילון עם המפתחות:
            content_sid (str, חובה)
            friendly_name (str, חובה)
            language (str)
            category (str)
            approval_status (str)
            rejection_reason (str, אופציונלי)
            header_type (str)
            body_text (str)
            footer_text (str, אופציונלי)
            buttons (list, אופציונלי)
            variables (list, אופציונלי)
            content_type (str, אופציונלי)
            raw (dict, אופציונלי) — JSON גולמי מ-Twilio לצורך debug
    """
    if not template.get("content_sid"):
        raise ValueError("upsert_whatsapp_template: content_sid חובה")
    if not template.get("friendly_name"):
        raise ValueError("upsert_whatsapp_template: friendly_name חובה")

    buttons_json = json.dumps(template.get("buttons") or [], ensure_ascii=False)
    variables_json = json.dumps(template.get("variables") or [], ensure_ascii=False)
    raw_json = json.dumps(template.get("raw") or {}, ensure_ascii=False)

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_templates (
                content_sid, friendly_name, language, category, approval_status,
                rejection_reason, header_type, header_text, header_media_url,
                body_text, footer_text, buttons_json, variables_json,
                content_type, raw_json, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(content_sid) DO UPDATE SET
                friendly_name    = excluded.friendly_name,
                language         = excluded.language,
                category         = excluded.category,
                approval_status  = excluded.approval_status,
                rejection_reason = excluded.rejection_reason,
                header_type      = excluded.header_type,
                -- header_text ו-header_media_url: COALESCE כדי
                -- שcallers שלא מכירים את השדות (submit, sync ישן) לא
                -- ידרסו ערכים קיימים. NULL מהcaller = שמור, ערך
                -- מפורש (כולל מחרוזת ריקה) = עדכן.
                header_text      = COALESCE(excluded.header_text, header_text),
                header_media_url = COALESCE(excluded.header_media_url, header_media_url),
                body_text        = excluded.body_text,
                footer_text      = excluded.footer_text,
                buttons_json     = excluded.buttons_json,
                variables_json   = excluded.variables_json,
                content_type     = excluded.content_type,
                raw_json         = excluded.raw_json,
                last_synced_at   = datetime('now')
            """,
            (
                template["content_sid"],
                template["friendly_name"],
                template.get("language", "he"),
                # category: שכבת הגנה מפני NULL ב-CHECK constraint —
                # אם ה-caller מעביר None נופלים ל-'UTILITY'. אבל זה
                # *לא* מנגנון "preserve" — ב-UPDATE excluded.category
                # ידרוס את הערך הקיים. שמירת קטגוריה קיימת היא אחריות
                # ה-caller (ראה sync_templates_from_twilio שקורא את
                # התבנית הקיימת לפני upsert כש-_fetch_approval_status
                # מחזיר None).
                template.get("category") or "UTILITY",
                template.get("approval_status", "unsubmitted"),
                template.get("rejection_reason"),
                template.get("header_type", "none"),
                # None = שמור קיים (COALESCE), ערך מפורש = עדכן.
                template.get("header_text"),
                template.get("header_media_url"),
                template.get("body_text", ""),
                template.get("footer_text", "") or "",
                buttons_json,
                variables_json,
                template.get("content_type", "") or "",
                raw_json,
            ),
        )


def get_whatsapp_template(content_sid: str) -> Optional[dict]:
    """שליפת תבנית לפי content_sid. מחזיר None אם לא קיימת."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM whatsapp_templates WHERE content_sid = ?",
            (content_sid,),
        ).fetchone()
        return _decode_wa_template_row(row) if row else None


# קטגוריות broadcast לגיטימיות: שיווק, שירות, ואימות. UTILITY נכלל כי תזכורות
# תור ועדכוני הזמנה הם broadcast חוקי בלי דרישת opt-in.
BROADCAST_TEMPLATE_CATEGORIES = ("MARKETING", "UTILITY", "AUTHENTICATION")

# תבניות שיחה דינמיות (ensure_quick_reply / ensure_list_picker) נוצרות בקוד
# בזמן ריצה ושמן הוא "{base}_{16hex}" (16 תווי hex של SHA-256). הן לא
# מיועדות לקמפיינים — מסננים אותן החוצה כשהמשתמש מחפש תבניות broadcast.
_INTERNAL_CONVERSATION_NAME_RE = re.compile(r"_[0-9a-f]{16}$")


def is_internal_conversation_template(friendly_name: Optional[str]) -> bool:
    """True אם השם תואם לתבניות שיחה דינמיות שנוצרו ע"י הקוד."""
    if not friendly_name:
        return False
    return bool(_INTERNAL_CONVERSATION_NAME_RE.search(friendly_name))


def list_whatsapp_templates(
    approval_status: Optional[str] = None,
    language: Optional[str] = None,
    category=None,
    exclude_internal: bool = False,
) -> list[dict]:
    """רשימת תבניות מסוננת לפי סטטוס/שפה/קטגוריה.

    Args:
        category: str יחיד או רצף (tuple/list) של קטגוריות. None = כל הקטגוריות.
        exclude_internal: אם True, מחריג תבניות שיחה דינמיות (`*_<16hex>`)
            שנוצרו ע"י `ensure_quick_reply` / `ensure_list_picker`. שימושי
            ב-UI של broadcast שלא רוצה להציג תבניות פנימיות.
    """
    query = "SELECT * FROM whatsapp_templates WHERE 1=1"
    params: list = []
    if approval_status:
        query += " AND approval_status = ?"
        params.append(approval_status)
    if language:
        query += " AND language = ?"
        params.append(language)
    if category:
        if isinstance(category, str):
            query += " AND category = ?"
            params.append(category)
        else:
            cats = tuple(category)
            if cats:
                placeholders = ",".join("?" for _ in cats)
                query += f" AND category IN ({placeholders})"
                params.extend(cats)
    query += " ORDER BY friendly_name ASC, language ASC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        templates = [_decode_wa_template_row(r) for r in rows]

    if exclude_internal:
        templates = [
            t for t in templates
            if not is_internal_conversation_template(t.get("friendly_name"))
        ]
    return templates


def count_whatsapp_templates_by_status(
    category=None,
    exclude_internal: bool = False,
) -> dict[str, int]:
    """מונה תבניות לפי סטטוס אישור — להצגה ב-badge בממשק האדמין.

    תומך באותם סינונים כמו list_whatsapp_templates כדי שה-counts
    ישקפו את התצוגה בפועל.
    """
    # exclude_internal דורש פילטור ב-Python; קוראים ללוגיקה המאוחדת.
    if exclude_internal or (category and not isinstance(category, str)):
        templates = list_whatsapp_templates(
            category=category, exclude_internal=exclude_internal,
        )
        result: dict[str, int] = {}
        for t in templates:
            status = t.get("approval_status") or ""
            result[status] = result.get(status, 0) + 1
        return result

    query = (
        "SELECT approval_status, COUNT(*) AS c FROM whatsapp_templates "
        "WHERE 1=1"
    )
    params: list = []
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " GROUP BY approval_status"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return {r["approval_status"]: r["c"] for r in rows}


def count_whatsapp_templates_by_category(
    exclude_internal: bool = False,
) -> dict[str, int]:
    """מונה תבניות לפי קטגוריה — להצגה ב-badge filter pills."""
    if exclude_internal:
        templates = list_whatsapp_templates(exclude_internal=True)
        result: dict[str, int] = {}
        for t in templates:
            cat = t.get("category") or ""
            result[cat] = result.get(cat, 0) + 1
        return result

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS c FROM whatsapp_templates "
            "GROUP BY category"
        ).fetchall()
        return {(r["category"] or ""): r["c"] for r in rows}


def delete_whatsapp_templates_not_in(content_sids: list[str]) -> int:
    """מחיקת תבניות שלא מופיעות ברשימה (משמש לסנכרון — תבניות שנמחקו ב-Twilio).

    אם הרשימה ריקה — לא מוחק כלום (הגנה מפני sync שנכשל באמצע ומחזיר ריק).
    מחזיר את מספר השורות שנמחקו.
    """
    if not content_sids:
        logger.warning(
            "delete_whatsapp_templates_not_in: התקבלה רשימה ריקה — מדלג על מחיקה"
        )
        return 0

    placeholders = ",".join("?" for _ in content_sids)
    with get_connection() as conn:
        cur = conn.execute(
            f"DELETE FROM whatsapp_templates WHERE content_sid NOT IN ({placeholders})",
            content_sids,
        )
        return cur.rowcount or 0


# ── Broadcast Campaigns (wizard drafts) ──────────────────────────────────────


def _decode_campaign_row(row: sqlite3.Row) -> dict:
    """המרת שורת campaign לדיקט עם JSON מפוענח (variable_mapping + audience_filter)."""
    data = dict(row)
    # variable_mapping
    raw = data.get("variable_mapping_json") or "{}"
    try:
        data["variable_mapping"] = json.loads(raw)
    except (ValueError, TypeError):
        logger.error(
            "broadcast_campaigns: JSON פגום ב-variable_mapping_json עבור id=%s",
            data.get("id"),
        )
        data["variable_mapping"] = {}
    # audience_filter (אם המיגרציה עוד לא רצה — ייתכן שהעמודה חסרה)
    raw_filter = data.get("audience_filter_json") or "{}"
    try:
        data["audience_filter"] = json.loads(raw_filter)
    except (ValueError, TypeError):
        logger.error(
            "broadcast_campaigns: JSON פגום ב-audience_filter_json עבור id=%s",
            data.get("id"),
        )
        data["audience_filter"] = {}
    return data


def create_broadcast_campaign(
    template_sid: str,
    variable_mapping: Optional[dict] = None,
    title: str = "",
    created_by: str = "",
) -> int:
    """יצירת campaign חדש במצב draft. מחזיר id."""
    if not template_sid:
        raise ValueError("create_broadcast_campaign: template_sid חובה")

    mapping_json = json.dumps(variable_mapping or {}, ensure_ascii=False)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO broadcast_campaigns (
                title, template_sid, variable_mapping_json, status, created_by
            ) VALUES (?, ?, ?, 'draft', ?)
            """,
            (title or "", template_sid, mapping_json, created_by or ""),
        )
        return int(cur.lastrowid)


def get_broadcast_campaign(campaign_id: int) -> Optional[dict]:
    """שליפת campaign לפי id. מחזיר None אם לא קיים."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM broadcast_campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        return _decode_campaign_row(row) if row else None


def update_broadcast_campaign_draft(
    campaign_id: int,
    variable_mapping: dict,
    title: Optional[str] = None,
    audience_type: Optional[str] = None,
    audience_filter: Optional[dict] = None,
) -> bool:
    """עדכון שדות draft — mapping + title + audience. רק אם עדיין draft.

    גם מסמן last_saved_at = NOW() — שמירה מפורשת ע"י המשתמש (להבדיל
    מ-create_broadcast_campaign שלא מסמן). list_broadcast_campaigns
    מסתמך על זה כדי להסתיר טיוטות שטרם נשמרו.

    Args:
        variable_mapping: מילון ערכי משתני תבנית.
        title: כותרת פנימית. None = לא לעדכן.
        audience_type: 'opted_in_only' | 'all' | 'recent'. None = לא לעדכן.
        audience_filter: dict נוסף (inactive_days וכו'). None = לא לעדכן.

    מחזיר True אם נעדכנה שורה, False אחרת (כבר sent/scheduled).
    """
    mapping_json = json.dumps(variable_mapping or {}, ensure_ascii=False)
    # בונים את ה-SET דינמית לפי מה שסופק כדי לא לדרוס ערכים שלא הובאו
    sets: list[str] = [
        "variable_mapping_json = ?",
        "updated_at = datetime('now')",
        "last_saved_at = datetime('now')",
    ]
    params: list = [mapping_json]
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if audience_type is not None:
        sets.append("audience_type = ?")
        params.append(audience_type)
    if audience_filter is not None:
        sets.append("audience_filter_json = ?")
        params.append(json.dumps(audience_filter, ensure_ascii=False))
    params.append(campaign_id)

    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE broadcast_campaigns SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'draft'",
            params,
        )
        return (cur.rowcount or 0) > 0


def count_active_campaigns_for_template(template_sid: str) -> int:
    """מחזיר את מספר הקמפיינים הפעילים (scheduled/sending/paused)
    שמשתמשים בתבנית. שימושי ל-safety check לפני מחיקת תבנית —
    LIMIT=N על list_broadcast_campaigns עלול להחמיץ קמפיינים שמעבר
    לגבול. כאן השאילתה משתמשת באינדקס idx_campaigns_template_sid
    ומונה הכל ללא LIMIT.
    """
    if not template_sid:
        return 0
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM broadcast_campaigns "
            "WHERE template_sid = ? "
            "AND status IN ('scheduled', 'sending', 'paused')",
            (template_sid,),
        ).fetchone()
        return int(row["c"]) if row else 0


def list_broadcast_campaigns(
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """רשימת campaigns ממוינים מהחדש לישן.

    מסנן טיוטות שטרם נשמרו במפורש (last_saved_at IS NULL) — קמפיינים
    כאלה נוצרים בשלב 1 של ה-wizard לפני שהמשתמש לחץ "שמור טיוטה",
    ולא נכון להציגם ברשימה.
    """
    where_clauses: list[str] = []
    params: list = []
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    # טיוטות לא שמורות לא מוצגות (גם אם status='draft' בלבד וגם בלי סינון).
    where_clauses.append("(status != 'draft' OR last_saved_at IS NOT NULL)")

    query = "SELECT * FROM broadcast_campaigns"
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    # מיון משני לפי id DESC כדי שיצירות באותה שניה (datetime('now') קוטע ל-sec)
    # תופענה בסדר הפוך של היצירה.
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_decode_campaign_row(r) for r in rows]


def cleanup_unsaved_draft_campaigns(older_than_minutes: int = 60) -> int:
    """מחיקת טיוטות שטרם נשמרו (last_saved_at IS NULL) שגן ישנות.

    הקמפיין נוצר בשלב 1 של ה-wizard כדי שה-HTMX (preview/audience) יוכל
    לעבוד מול id קיים. אם המשתמש עזב בלי ללחוץ "שמור טיוטה", הרשומה
    נשארת יתומה. הניקיון רץ בעת טעינת רשימת הקמפיינים.

    מחזיר את מספר הרשומות שנמחקו.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM broadcast_campaigns "
            "WHERE status = 'draft' "
            "AND last_saved_at IS NULL "
            "AND datetime(created_at) < datetime('now', ?)",
            (f"-{int(older_than_minutes)} minutes",),
        )
        return int(cur.rowcount or 0)


def get_users_for_broadcast(user_ids: list[str]) -> list[dict]:
    """שליפת מידע בסיסי של משתמשים לצרכי per-user substitution בקמפיינים.

    משמש את broadcast_sender כדי להחליף {{user:username}}, {{user:user_id}},
    {{user:phone}} בתוך ערכי מיפוי variables. batch-fetch במקום N queries
    כדי לא להאט לולאת שליחה של 1000 נמענים.

    Returns:
        רשימת dicts עם user_id + username. משתמשים שלא נמצאו — לא מוחזרים
        (הקורא אחראי על fallback).
    """
    if not user_ids:
        return []
    placeholders = ",".join("?" for _ in user_ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT user_id, username FROM users WHERE user_id IN ({placeholders})",
            user_ids,
        ).fetchall()
        return [dict(r) for r in rows]


def delete_broadcast_campaign(campaign_id: int) -> bool:
    """מחיקת draft או scheduled שטרם נשלח. קמפיין ששודר — לא נמחק (אודיט)."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM broadcast_campaigns WHERE id = ? AND status IN ('draft', 'scheduled')",
            (campaign_id,),
        )
        return (cur.rowcount or 0) > 0


def schedule_broadcast_campaign(campaign_id: int, scheduled_at: str) -> bool:
    """תזמון קמפיין לעתיד — עובר מ-draft ל-scheduled עם scheduled_at שמור.

    Args:
        scheduled_at: datetime כ-string בפורמט 'YYYY-MM-DD HH:MM:SS' שעון ישראל.

    Returns:
        True אם נקבע, False אחרת (למשל הקמפיין אינו draft).
    """
    if not scheduled_at:
        raise ValueError("schedule_broadcast_campaign: scheduled_at חובה")
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE broadcast_campaigns
            SET status = 'scheduled',
                scheduled_at = ?,
                updated_at = datetime('now')
            WHERE id = ? AND status = 'draft'
            """,
            (scheduled_at, campaign_id),
        )
        return (cur.rowcount or 0) > 0


def cancel_scheduled_campaign(campaign_id: int) -> bool:
    """ביטול תזמון — scheduled → draft. רק קמפיינים שעדיין ממתינים ישונו.

    מסמן גם last_saved_at = NOW() — ביטול תזמון זה פעולה מפורשת של
    המשתמש (לא יצירת draft חדשה ע"י wizard). בלי זה, קמפיין שחזר
    מ-scheduled ל-draft היה נופל ב-list filter (עם last_saved_at NULL)
    ונמחק אחרי 60 דק' ע"י cleanup_unsaved_draft_campaigns.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE broadcast_campaigns
            SET status = 'draft',
                scheduled_at = NULL,
                updated_at = datetime('now'),
                last_saved_at = datetime('now')
            WHERE id = ? AND status = 'scheduled'
            """,
            (campaign_id,),
        )
        return (cur.rowcount or 0) > 0


def list_due_scheduled_campaigns(
    now_str: Optional[str] = None, limit: int = 50,
) -> list[dict]:
    """שליפת קמפיינים מתוזמנים שהגיעו לזמן הביצוע שלהם.

    scheduled_at מאוחסן כ-string בפורמט 'YYYY-MM-DD HH:MM:SS' בשעון ישראל.
    הקורא מעביר את הזמן הנוכחי בשעון ישראל לצורך השוואה מדויקת שלא תלויה
    ב-TZ של השרת (ב-Render זה UTC; לא ניתן להסתמך על datetime('now')).

    Args:
        now_str: Current Israel time as 'YYYY-MM-DD HH:MM:SS'. ברירת מחדל —
                 datetime('now', 'localtime') של SQLite (עובד רק אם השרת
                 ב-TZ ישראל; ב-production יש להעביר זמן מפורש).
    """
    with get_connection() as conn:
        if now_str:
            rows = conn.execute(
                """
                SELECT id, template_sid, scheduled_at, audience_type,
                       audience_filter_json
                FROM broadcast_campaigns
                WHERE status = 'scheduled' AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT ?
                """,
                (now_str, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, template_sid, scheduled_at, audience_type,
                       audience_filter_json
                FROM broadcast_campaigns
                WHERE status = 'scheduled'
                  AND scheduled_at <= datetime('now', 'localtime')
                ORDER BY scheduled_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def reschedule_campaign_at(campaign_id: int, new_scheduled_at: str) -> bool:
    """עדכון scheduled_at של קמפיין שכבר scheduled — ללא שינוי סטטוס.

    משמש ל-auto-defer של MARKETING בחלון שבת/חגים: השעון פג אבל
    מדחים למועד מאוחר יותר ומשאירים status='scheduled'.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE broadcast_campaigns
            SET scheduled_at = ?,
                updated_at = datetime('now')
            WHERE id = ? AND status = 'scheduled'
            """,
            (new_scheduled_at, campaign_id),
        )
        return (cur.rowcount or 0) > 0


# ── Opt-in / Opt-out לשיווק ב-WhatsApp (תיקון 40 לחוק התקשורת) ───────────────


def set_wa_marketing_opt_in(user_id: str, source: str = "") -> None:
    """סימון משתמש כ-opted-in לקמפיינים שיווקיים + ניקוי opt-out אם היה.

    כותב הוכחה ל-consent_ledger (event_type=opt_in_marketing) כדי שיהיה
    תיעוד גם אחרי /forget. תיקון 40 דורש להוכיח opt-in אם תלונה תוגש.

    Args:
        user_id: מזהה משתמש (BSUID או טלפון).
        source: מקור ה-opt-in — 'bot_button', 'web_form', 'admin_manual', 'import'.
                מצורף לאודיט ומאפשר להוכיח opt-in במקרה של תלונה רגולטורית.
    """
    if not user_id:
        raise ValueError("set_wa_marketing_opt_in: user_id חובה")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET wa_marketing_opt_in = 1,
                wa_marketing_opt_in_at = datetime('now'),
                wa_marketing_opt_in_source = ?,
                wa_opted_out_at = NULL
            WHERE user_id = ?
            """,
            (source or "", user_id),
        )

    try:
        from utils.consent_ledger import record_consent_event, EVENT_OPT_IN_MARKETING
        record_consent_event(
            user_id=user_id, channel="whatsapp",
            event_type=EVENT_OPT_IN_MARKETING,
            metadata={"source": source} if source else None,
        )
    except Exception:
        logger.error("set_wa_marketing_opt_in: כשל בכתיבה ל-consent_ledger", exc_info=True)


def set_wa_opted_out(user_id: str) -> None:
    """סימון משתמש כ-opted-out. האפקט: הוא לא ייכלל בקמפיינים.

    שומרים גם wa_marketing_opt_in=0 כדי שלא נצטרך לבדוק את שני השדות
    בכל שאילתה — חיפוש לפי opt_in=1 לבד מספיק לסינון בסיסי.

    כותב הוכחה ל-consent_ledger (event_type=opt_out_marketing) — תיקון 40
    דורש להוכיח שכיבדנו את הסירוב מיידית, גם אם המשתמש פתח חשבון מחדש.
    """
    if not user_id:
        raise ValueError("set_wa_opted_out: user_id חובה")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET wa_marketing_opt_in = 0,
                wa_opted_out_at = datetime('now')
            WHERE user_id = ?
            """,
            (user_id,),
        )

    try:
        from utils.consent_ledger import record_consent_event, EVENT_OPT_OUT_MARKETING
        record_consent_event(
            user_id=user_id, channel="whatsapp",
            event_type=EVENT_OPT_OUT_MARKETING,
        )
    except Exception:
        logger.error("set_wa_opted_out: כשל בכתיבה ל-consent_ledger", exc_info=True)


def get_wa_opt_status(user_id: str) -> dict:
    """קבלת סטטוס opt-in/opt-out של משתמש — לתצוגה באדמין.

    Returns:
        dict עם: opted_in (bool), opted_in_at (str|None),
        opted_in_source (str), opted_out_at (str|None),
        eligible_for_marketing (bool).
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT wa_marketing_opt_in, wa_marketing_opt_in_at,
                   wa_marketing_opt_in_source, wa_opted_out_at
            FROM users WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return {
                "opted_in": False, "opted_in_at": None, "opted_in_source": "",
                "opted_out_at": None, "eligible_for_marketing": False,
            }
        opted_in = bool(row["wa_marketing_opt_in"])
        opted_out_at = row["wa_opted_out_at"]
        return {
            "opted_in": opted_in,
            "opted_in_at": row["wa_marketing_opt_in_at"],
            "opted_in_source": row["wa_marketing_opt_in_source"] or "",
            "opted_out_at": opted_out_at,
            "eligible_for_marketing": opted_in and not opted_out_at,
        }


def is_wa_eligible_for_marketing(user_id: str) -> bool:
    """קיצור ל-check מהיר: opted_in=1 AND opted_out IS NULL."""
    return get_wa_opt_status(user_id)["eligible_for_marketing"]


def should_send_opt_in_prompt(user_id: str, min_messages: int = 3) -> bool:
    """האם לשלוח ל-WA user בקשת opt-in פרואקטיבית.

    התנאים:
      1. לא opted-in כבר (אין טעם לבקש)
      2. לא opted-out (הוא ביקש לא לקבל — לא מציקים)
      3. לא נשאל בעבר (wa_opt_in_prompt_sent_at IS NULL)
      4. ביצע לפחות min_messages אינטראקציות (מסנן פניות חד-פעמיות של
         "שלום" שלא יצרו engagement אמיתי)

    Args:
        min_messages: סף engagement — ברירת מחדל 3. מקטין נפיחות של
                      prompt לאנשים שבאו פעם אחת ולא חזרו.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT wa_marketing_opt_in, wa_opted_out_at,
                   wa_opt_in_prompt_sent_at, message_count
            FROM users WHERE user_id = ? AND channel = 'whatsapp'
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return False
        if row["wa_marketing_opt_in"]:
            return False
        if row["wa_opted_out_at"]:
            return False
        if row["wa_opt_in_prompt_sent_at"]:
            return False
        if (row["message_count"] or 0) < min_messages:
            return False
        return True


def mark_opt_in_prompt_sent(user_id: str) -> None:
    """סימון שהמשתמש קיבל את פנייה ה-opt-in — למנוע חזרה עליה.

    נקרא גם כשהמשתמש ענה (כן/לא) וגם כשהוא התעלם — בשני המקרים
    ה-prompt "צרך" את ההזדמנות וכבר לא נחזור עליו."""
    if not user_id:
        return
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET wa_opt_in_prompt_sent_at = datetime('now')
            WHERE user_id = ? AND wa_opt_in_prompt_sent_at IS NULL
            """,
            (user_id,),
        )


def count_wa_audience(
    category: str = "UTILITY",
    inactive_days: Optional[int] = None,
    require_opt_in: Optional[bool] = None,
) -> dict:
    """מונה משתמשי WhatsApp לקמפיין, מפורק לפי שכבות סינון.

    אכיפה: category='MARKETING' תמיד דורשת opt-in (חובה רגולטורית). עבור
    UTILITY/AUTHENTICATION — ברירת המחדל היא לא לסנן לפי opt-in (כל מי
    שלא עשה opt-out), אבל המשתמש יכול לבקש explicit `require_opt_in=True`
    אם רוצה להגביל את הקהל רק לאנשים שהסכימו לתקשורת שיווקית.

    Args:
        category: UTILITY | MARKETING | AUTHENTICATION.
        inactive_days: אם None — אין סינון. אם N — רק משתמשים פעילים
                       ב-N הימים האחרונים.
        require_opt_in: דורש opt-in מפורש. True/False לכפייה/ביטול; None
                       = ברירת מחדל לפי קטגוריה (MARKETING=True, אחרים=False).

    Returns:
        dict עם:
          total_wa_users (int): סך משתמשי WhatsApp ב-DB
          eligible (int): עוברים את כל הפילטרים
          filtered_out_opted_out (int): סומנו opt-out
          filtered_out_never_opted_in (int): לא נתנו opt-in (רק אם נדרש)
          filtered_out_inactive (int): מחוץ לטווח inactive_days (אם הוגדר)
    """
    normalized_category = (category or "UTILITY").upper()
    # MARKETING תמיד דורש opt-in (לא ניתן לעקוף). עבור קטגוריות אחרות
    # הברירת מחדל היא לא לדרוש, אלא אם הקורא ביקש explicit True.
    must_opt_in = normalized_category == "MARKETING" or bool(require_opt_in)
    with get_connection() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE channel = 'whatsapp'"
        ).fetchone()
        total = int(total_row["c"])

        result = {
            "total_wa_users": total,
            "eligible": 0,
            "filtered_out_opted_out": 0,
            "filtered_out_never_opted_in": 0,
            "filtered_out_inactive": 0,
        }

        if must_opt_in:
            # opt-in חובה
            opted_out = conn.execute(
                "SELECT COUNT(*) AS c FROM users "
                "WHERE channel = 'whatsapp' AND wa_opted_out_at IS NOT NULL"
            ).fetchone()["c"]
            never_in = conn.execute(
                "SELECT COUNT(*) AS c FROM users "
                "WHERE channel = 'whatsapp' AND wa_marketing_opt_in = 0 "
                "AND wa_opted_out_at IS NULL"
            ).fetchone()["c"]
            result["filtered_out_opted_out"] = int(opted_out)
            result["filtered_out_never_opted_in"] = int(never_in)

            where = (
                "channel = 'whatsapp' AND wa_marketing_opt_in = 1 "
                "AND wa_opted_out_at IS NULL"
            )
        else:
            # לא דורש opt-in — opted_out בלבד נחסם (כבוד ללקוח)
            opted_out = conn.execute(
                "SELECT COUNT(*) AS c FROM users "
                "WHERE channel = 'whatsapp' AND wa_opted_out_at IS NOT NULL"
            ).fetchone()["c"]
            result["filtered_out_opted_out"] = int(opted_out)
            where = "channel = 'whatsapp' AND wa_opted_out_at IS NULL"

        if inactive_days is not None and inactive_days > 0:
            # active_since = datetime('now', '-N days')
            active_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM users WHERE {where} "
                f"AND last_active_at >= datetime('now', '-' || ? || ' days')",
                (inactive_days,),
            ).fetchone()
            all_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM users WHERE {where}"
            ).fetchone()
            result["eligible"] = int(active_row["c"])
            result["filtered_out_inactive"] = int(all_row["c"]) - result["eligible"]
        else:
            eligible_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM users WHERE {where}"
            ).fetchone()
            result["eligible"] = int(eligible_row["c"])

        return result


# ── Broadcast Deliveries (שלב 4 — מעקב שליחה per-recipient) ─────────────────


def create_delivery_queue(
    campaign_id: int,
    user_id: str,
    rendered_variables: Optional[dict] = None,
) -> tuple[int, bool]:
    """רישום נמען לתור שליחה — או סימון שורה קיימת כמוכנה לשליחה חוזרת.

    UNIQUE(campaign_id, user_id) מבטיח שלא נירשום נמען פעמיים באותו קמפיין.
    אם כבר קיים:
      * status='queued' — השורה טרם נשלחה (ריצה ראשונה שנקטעה ע"י pause,
        או requeue_failed_deliveries אחרי retry, או bulk_create_queued_deliveries
        בתחילת הריצה). מחזירים (id, True) כדי שה-caller ישלח.
        rendered_variables_json מתעדכן לערכים החדשים (מה-caller), כדי
        שהאודיט בשורה ישקף את מה שנשלח בפועל.
      * סטטוס אחר (sent/delivered/read/failed/undelivered) — כבר עבדנו
        על זה. מחזירים (id, False) כדי ש-caller ידלג ולא יכפיל. לא
        נוגעים ב-rendered_variables_json (שימור אודיט היסטורי).

    Returns:
        (delivery_id, should_send)
        - delivery_id: מזהה השורה (חדש או קיים).
        - should_send: True אם הקורא אמור להמשיך לשליחה; False אם יש לדלג.
    """
    vars_json = json.dumps(rendered_variables or {}, ensure_ascii=False)
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO broadcast_deliveries
                    (campaign_id, user_id, rendered_variables_json, status)
                VALUES (?, ?, ?, 'queued')
                """,
                (campaign_id, user_id, vars_json),
            )
            return int(cur.lastrowid), True
        except sqlite3.IntegrityError:
            # כבר קיים — בודקים סטטוס כדי להחליט אם להשלים שליחה
            row = conn.execute(
                "SELECT id, status FROM broadcast_deliveries "
                "WHERE campaign_id = ? AND user_id = ?",
                (campaign_id, user_id),
            ).fetchone()
            if not row:
                return 0, False
            # queued = טרם נשלחה (bulk pre-create / pause / requeue) —
            # מעדכנים את rendered_variables_json כדי שהאודיט ישקף את
            # הערכים שנשלחו בפועל (pre-create שומר '{}' זמנית).
            # WHERE status='queued' מונע דריסת json של שורות שכבר נשלחו
            # במרוץ נדיר.
            should_send = row["status"] == "queued"
            if should_send:
                conn.execute(
                    "UPDATE broadcast_deliveries "
                    "SET rendered_variables_json = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (vars_json, row["id"]),
                )
            return int(row["id"]), should_send


def mark_delivery_sent(delivery_id: int, twilio_message_sid: str) -> None:
    """סימון delivery כ-sent אחרי ש-Twilio קיבלה את הבקשה (HTTP 201)."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE broadcast_deliveries
            SET status = 'sent',
                twilio_message_sid = ?,
                sent_at = datetime('now')
            WHERE id = ? AND status = 'queued'
            """,
            (twilio_message_sid, delivery_id),
        )


def mark_delivery_failed(delivery_id: int, error_code: str = "",
                         error_message: str = "") -> None:
    """סימון delivery ככישלון (שגיאה ב-Twilio request)."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE broadcast_deliveries
            SET status = 'failed',
                error_code = ?,
                error_message = ?,
                failed_at = datetime('now')
            WHERE id = ?
            """,
            (error_code or "", (error_message or "")[:500], delivery_id),
        )


def update_delivery_status_by_twilio_sid(
    twilio_message_sid: str,
    status: str,
    error_code: str = "",
    error_message: str = "",
) -> bool:
    """עדכון סטטוס delivery לפי Twilio MessageSid (מ-status webhook).

    אכיפה מונוטונית: סטטוסים מתקדמים רק קדימה (queued → sent → delivered
    → read) או לצד (sent/delivered → failed/undelivered). לא נסוגים אחורה
    אם Twilio שולחת callbacks out-of-order — מצב אפשרי ברשת רועשת.

    Concurrency: ה-UPDATE כולל WHERE status = ? (compare-and-swap) כדי
    לסגור חלון race בין SELECT ל-UPDATE. במקרה שרדנ thread אחר שינה את
    הסטטוס באמצע, מנסים מחדש עד 3 פעמים עם המצב החדש (כדי שלא נאבד
    update לגיטימי רק בגלל timing).

    Args:
        status: המפתח מ-MessageStatus של Twilio (sent/delivered/read/failed/undelivered).
    מחזיר True אם עודכנה שורה; False אם לא נמצאה, סטטוס לא חוקי, או
    שהמעבר היה מהווה regression.
    """
    if not twilio_message_sid or not status:
        return False
    normalized = _normalize_twilio_status(status)
    if not normalized:
        return False

    # בונים SET דינמית לפי הסטטוס כדי לעדכן גם timestamp מתאים
    timestamp_col = {
        "sent": "sent_at",
        "delivered": "delivered_at",
        "read": "read_at",
        "failed": "failed_at",
        "undelivered": "failed_at",
    }.get(normalized)

    with get_connection() as conn:
        # retry loop: אם CAS נכשל (סטטוס השתנה באמצע ע"י thread אחר), קוראים
        # מחדש את הסטטוס המעודכן ובודקים שוב האם ההתקדמות עדיין לגיטימית.
        # רוב המקרים יסתיימו באיטרציה הראשונה; 3 נסיונות מספיקים למקרה קצה.
        for _attempt in range(3):
            current_row = conn.execute(
                "SELECT status FROM broadcast_deliveries "
                "WHERE twilio_message_sid = ?",
                (twilio_message_sid,),
            ).fetchone()
            if not current_row:
                return False
            current_status = current_row["status"]

            if not _should_advance_status(current_status, normalized):
                # out-of-order/equal/terminal — לא מעדכנים
                logger.info(
                    "update_delivery_status: מדלג %s → %s עבור %s (monotonic guard)",
                    current_status, normalized, twilio_message_sid,
                )
                return False

            # UPDATE עם CAS: רק אם הסטטוס עדיין כמו שקראנו. אם thread אחר
            # כתב בינתיים, rowcount=0 ונחזור על ה-loop עם המצב החדש.
            if timestamp_col:
                cur = conn.execute(
                    f"""
                    UPDATE broadcast_deliveries
                    SET status = ?,
                        {timestamp_col} = datetime('now'),
                        error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
                        error_message = CASE WHEN ? != '' THEN ? ELSE error_message END
                    WHERE twilio_message_sid = ? AND status = ?
                    """,
                    (
                        normalized,
                        error_code, error_code or "",
                        error_message, (error_message or "")[:500],
                        twilio_message_sid, current_status,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE broadcast_deliveries
                    SET status = ?
                    WHERE twilio_message_sid = ? AND status = ?
                    """,
                    (normalized, twilio_message_sid, current_status),
                )

            if (cur.rowcount or 0) > 0:
                return True
            # CAS נכשל — נקרא מחדש בלולאה הבאה ונבדוק שוב

        # אחרי 3 ניסיונות — ויתרנו (תרחיש נדיר מאוד בפרקטיקה)
        logger.warning(
            "update_delivery_status: CAS failed after 3 retries for %s (target=%s)",
            twilio_message_sid, normalized,
        )
        return False


# סדר התקדמות סטטוסי delivery. לא נסוגים אחורה:
# queued → sent → delivered → read (התקדמות לינארית)
# queued/sent/delivered → failed/undelivered (סוף רע) — מותר גם ממצב delivered
# read → anything: לא משנים (terminal success)
# failed/undelivered → anything: לא משנים (terminal fail)
_STATUS_FORWARD_PRIORITY = {
    "queued": 0,
    "sent": 1,
    "delivered": 2,
    "read": 3,
}
_TERMINAL_FAIL_STATUSES = {"failed", "undelivered"}
_TERMINAL_SUCCESS_STATUSES = {"read"}


def _should_advance_status(current: str, new: str) -> bool:
    """מחזיר True אם המעבר current→new הוא התקדמות לגיטימית (לא רגרסיה)."""
    if current == new:
        # אותו סטטוס — no-op, כדי לא לדרוס timestamp עם ערך מאוחר יותר
        return False
    # terminal states — לא משנים אחרי שהגענו
    if current in _TERMINAL_FAIL_STATUSES or current in _TERMINAL_SUCCESS_STATUSES:
        return False
    # terminal fail יכול להגיע מכל מצב לא-terminal (גם אחרי delivered)
    if new in _TERMINAL_FAIL_STATUSES:
        return True
    # התקדמות לינארית — רק קדימה
    return (
        _STATUS_FORWARD_PRIORITY.get(new, -1)
        > _STATUS_FORWARD_PRIORITY.get(current, -1)
    )


def _normalize_twilio_status(status: str) -> Optional[str]:
    """המרה של MessageStatus של Twilio לערכי enum שלנו."""
    mapping = {
        "queued": "queued",
        "sending": "queued",
        "sent": "sent",
        "delivered": "delivered",
        "read": "read",
        "failed": "failed",
        "undelivered": "undelivered",
    }
    return mapping.get((status or "").lower())


def get_campaign_progress(campaign_id: int) -> dict:
    """סיכום התקדמות קמפיין — מונים לפי סטטוס + accepted מצטבר.

    accepted = כל נמען שקיבל twilio_message_sid (כלומר Twilio אישרה את
    הבקשה ב-HTTP 201). זה ה-"sent המצטבר" — לא יורד גם אם מאוחר יותר
    webhook מעדכן ל-failed/undelivered. סטטוס 'failed' ב-DB עצמו יכול
    לנבוע משני מצבים שונים:
      1. Twilio דחתה ביצירה (אין SID) — create-time failure
      2. Twilio קיבלה ואחר כך webhook החזיר status=failed (יש SID)
    הבחנה זו חשובה כדי שמונה 'sent' הקמפיין לא ירד.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM broadcast_deliveries "
            "WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        by_status = {r["status"]: int(r["c"]) for r in rows}
        total = sum(by_status.values())

        accepted_row = conn.execute(
            "SELECT COUNT(*) AS c FROM broadcast_deliveries "
            "WHERE campaign_id = ? AND twilio_message_sid IS NOT NULL",
            (campaign_id,),
        ).fetchone()
        accepted = int(accepted_row["c"]) if accepted_row else 0

        return {
            "total": total,
            "queued": by_status.get("queued", 0),
            "sent": by_status.get("sent", 0),
            "delivered": by_status.get("delivered", 0),
            "read": by_status.get("read", 0),
            "failed": by_status.get("failed", 0),
            "undelivered": by_status.get("undelivered", 0),
            "accepted": accepted,
        }


def get_deliveries_for_campaign(
    campaign_id: int,
    status: Optional[str] = None,
    statuses: Optional[list[str]] = None,
    limit: int = 500,
) -> list[dict]:
    """שליפת רשומות delivery לקמפיין (לתצוגה בעמוד פירוט).

    Args:
        status: סינון לסטטוס יחיד (לאחור-תאימות).
        statuses: סינון למספר סטטוסים (IN). גובר על status אם הוגדרו שניהם.
                  שימוש: failures = [failed, undelivered] (שניהם תקלות).
    """
    query = "SELECT * FROM broadcast_deliveries WHERE campaign_id = ?"
    params: list = [campaign_id]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query += f" AND status IN ({placeholders})"
        params.extend(statuses)
    elif status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def set_campaign_status(campaign_id: int, status: str) -> bool:
    """שינוי סטטוס קמפיין (sending / completed / failed / paused)."""
    if status not in ("draft", "scheduled", "sending", "completed", "failed", "paused"):
        raise ValueError(f"set_campaign_status: סטטוס לא חוקי '{status}'")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE broadcast_campaigns "
            "SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, campaign_id),
        )
        return (cur.rowcount or 0) > 0


def get_campaign_status(campaign_id: int) -> Optional[str]:
    """שליפה קצרה של סטטוס קמפיין בלבד — משמש את send loop לבדיקות
    תכופות של pause/resume/cancel במהלך איטרציה ארוכה (זול יותר מ-
    get_broadcast_campaign שעושה JSON decoding)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM broadcast_campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        return row["status"] if row else None


def requeue_failed_deliveries(campaign_id: int) -> int:
    """איפוס deliveries שנכשלו (failed/undelivered) חזרה ל-queued.

    משמש retry-failed — מריצים את הלולאה רק על נמענים שנכשלו בעבר.
    twilio_message_sid מנוקה כי הריצה הבאה תקבל SID חדש (אחרת status
    callback של ה-SID הישן עדיין עלול להגיע אליהם).
    חשוב לא לאפס נמענים שעברו delivered/read — הם קיבלו.

    מחזיר את מספר השורות שאופסו.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE broadcast_deliveries
            SET status = 'queued',
                twilio_message_sid = NULL,
                error_code = '',
                error_message = '',
                failed_at = NULL
            WHERE campaign_id = ? AND status IN ('failed', 'undelivered')
            """,
            (campaign_id,),
        )
        return cur.rowcount or 0


def bulk_create_queued_deliveries(
    campaign_id: int, user_ids: list[str],
) -> int:
    """יצירת שורות delivery במצב queued לכל user_ids — ב-INSERT OR IGNORE
    אחד (atomic) כדי שפקיחת הקמפיין תהיה efficient גם ל-1000+ נמענים.

    שימוש: בריצה ראשונה של קמפיין, כדי שכל הנמענים יהיו בטבלה *לפני*
    שהלולאה מתחילה. בלי זה, pause באמצע לולאת השליחה היה משאיר נמענים
    לא-מעובדים ללא שורה בכלל — resume היה מוצא רק את אלה שעברו עד
    הנקודה שבה נקטע, ומאבד לנצח את השאר.

    INSERT OR IGNORE מונע כפילויות אם הפונקציה נקראת יותר מפעם אחת
    (שלא אמור לקרות, אבל הגנה).

    Returns:
        מספר השורות שבאמת נוצרו (לא הקיימות מראש).
    """
    if not user_ids:
        return 0
    with get_connection() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM broadcast_deliveries "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()["c"]
        conn.executemany(
            """
            INSERT OR IGNORE INTO broadcast_deliveries
                (campaign_id, user_id, rendered_variables_json, status)
            VALUES (?, ?, '{}', 'queued')
            """,
            [(campaign_id, uid) for uid in user_ids],
        )
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM broadcast_deliveries "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()["c"]
        return after - before


def campaign_has_deliveries(campaign_id: int) -> bool:
    """True אם קיימות כבר שורות delivery לקמפיין (ריצה ראשונה כבר קרתה).

    משמש את _send_campaign_locked כדי להבחין בין ריצה ראשונה (צריך לפתור
    audience) לבין resume/retry (audience כבר קבוע — משתמשים רק בנמענים
    שקיימים בטבלה). בלי הבחנה זו, retry/resume היו מוסיפים נמענים חדשים
    שהצטרפו ל-eligibility אחרי הריצה הראשונה — באג של unintended expansion.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM broadcast_deliveries WHERE campaign_id = ? LIMIT 1",
            (campaign_id,),
        ).fetchone()
        return row is not None


def list_queued_user_ids_for_campaign(campaign_id: int) -> list[str]:
    """נמענים שבסטטוס queued עבור הקמפיין.

    משמש את _send_campaign_locked ב-resume (אחרי pause) וב-retry-failed
    (אחרי requeue_failed_deliveries) — הלולאה רצה רק עליהם, ולא מרחיבה
    audience לכל מי שכשיר עכשיו.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM broadcast_deliveries "
            "WHERE campaign_id = ? AND status = 'queued' ORDER BY id ASC",
            (campaign_id,),
        ).fetchall()
        return [r["user_id"] for r in rows]


def get_error_breakdown(campaign_id: int) -> list[dict]:
    """פירוק שגיאות קמפיין לפי error_code.

    מחזיר רשימת dicts: {error_code, count, sample_message}. error_code
    ריק מסווג כ-UNKNOWN. ממוין מהשכיח ביותר ליורד — עוזר למנהל לזהות
    את הבעיה הנפוצה ביותר בקמפיין.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN error_code IS NULL OR error_code = ''
                        THEN 'UNKNOWN'
                    ELSE error_code
                END AS error_code,
                COUNT(*) AS count,
                MAX(error_message) AS sample_message
            FROM broadcast_deliveries
            WHERE campaign_id = ?
              AND status IN ('failed', 'undelivered')
            GROUP BY 1
            ORDER BY count DESC, error_code ASC
            """,
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_broadcast_analytics_summary() -> dict:
    """סיכום אנליטי חוצה-קמפיינים — משמש דף /broadcast/analytics.

    שם מובחן מ-get_analytics_summary הכללי (עם days param) כדי לא
    להצל על אותו namespace ב-database.py.

    Returns:
        dict עם:
          by_status: {status: count}
          total_campaigns: int
          totals: {total_recipients, sent, delivered, read, failed}
          top_errors: רשימת top-10 error codes חוצי-קמפיינים
    """
    with get_connection() as conn:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM broadcast_campaigns GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: int(r["c"]) for r in status_rows}

        totals_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_recipients), 0) AS total_recipients,
                COALESCE(SUM(sent), 0) AS total_sent,
                COALESCE(SUM(delivered), 0) AS total_delivered,
                COALESCE(SUM(read_count), 0) AS total_read,
                COALESCE(SUM(failed), 0) AS total_failed
            FROM broadcast_campaigns
            WHERE status IN ('completed', 'sending', 'failed', 'paused')
            """
        ).fetchone()

        error_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN error_code IS NULL OR error_code = ''
                        THEN 'UNKNOWN'
                    ELSE error_code
                END AS error_code,
                COUNT(*) AS count
            FROM broadcast_deliveries
            WHERE status IN ('failed', 'undelivered')
            GROUP BY 1
            ORDER BY count DESC
            LIMIT 10
            """
        ).fetchall()

        return {
            "by_status": by_status,
            "total_campaigns": sum(by_status.values()),
            "totals": dict(totals_row) if totals_row else {},
            "top_errors": [dict(e) for e in error_rows],
        }


def transition_campaign_status(
    campaign_id: int, from_status: str, to_status: str,
) -> bool:
    """מעבר סטטוס אטומי (compare-and-swap) — עובר רק אם הסטטוס הנוכחי זהה
    ל-from_status. משמש כנעילה למניעת כפל שליחה בקריאות מקבילות
    (double-click של admin, שני admins באותו זמן).

    Returns:
        True אם המעבר הצליח (השורה נעדכנה), False אחרת.
    """
    valid = ("draft", "scheduled", "sending", "completed", "failed", "paused")
    if from_status not in valid or to_status not in valid:
        raise ValueError(
            f"transition_campaign_status: סטטוס לא חוקי ({from_status} → {to_status})"
        )
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE broadcast_campaigns "
            "SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (to_status, campaign_id, from_status),
        )
        return (cur.rowcount or 0) > 0


def recompute_campaign_counters(campaign_id: int) -> None:
    """חישוב ועדכון מונים של קמפיין ישירות מ-broadcast_deliveries, ב-UPDATE
    יחיד ואטומי.

    ה-subqueries רצים בתוך ה-UPDATE — אין חלון race בין SELECT ל-UPDATE
    (בניגוד ל-set_campaign_counters שמקבלת snapshot ממקור חיצוני). כתיבות
    מקבילות (send-loop סיום + webhook callback) לא יכולות לדרוס זו את זו
    עם snapshot ישן יותר; כל recompute משקף את המצב הנוכחי ב-DB בזמן הכתיבה.

    סמנטיקה (זהה ל-_counters_from_progress):
      - sent: נמענים עם twilio_message_sid (עברו HTTP 201 ב-Twilio).
      - delivered: status IN (delivered, read).
      - read_count: status = read.
      - failed: status IN (failed, undelivered).
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE broadcast_campaigns
            SET
                sent = (
                    SELECT COUNT(*) FROM broadcast_deliveries
                    WHERE campaign_id = ? AND twilio_message_sid IS NOT NULL
                ),
                delivered = (
                    SELECT COUNT(*) FROM broadcast_deliveries
                    WHERE campaign_id = ? AND status IN ('delivered', 'read')
                ),
                read_count = (
                    SELECT COUNT(*) FROM broadcast_deliveries
                    WHERE campaign_id = ? AND status = 'read'
                ),
                failed = (
                    SELECT COUNT(*) FROM broadcast_deliveries
                    WHERE campaign_id = ? AND status IN ('failed', 'undelivered')
                ),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (campaign_id, campaign_id, campaign_id, campaign_id, campaign_id),
        )


def set_campaign_counters(campaign_id: int, counters: dict) -> None:
    """עדכון מונים מרוכזים על הקמפיין (total_recipients/sent/delivered/read/failed).

    משמש בסיום שליחה ובעדכונים מ-status webhook לעדכן את השורה המרכזית
    (לעיון מהיר ברשימה בלי לעשות COUNT על broadcast_deliveries).
    """
    sets: list[str] = []
    params: list = []
    for col in ("total_recipients", "sent", "delivered", "read_count", "failed"):
        key = "read" if col == "read_count" else col
        if key in counters:
            sets.append(f"{col} = ?")
            params.append(int(counters[key]))
    if not sets:
        return
    # updated_at אין לו placeholder — חובה להוסיף אותו ל-sets *לפני*
    # שנוסיף את campaign_id ל-params, אחרת כל שינוי עתידי שיוסיף עמודה
    # parameterized חדשה כאן יגרום ל-misalignment בין sets ל-params.
    sets.append("updated_at = datetime('now')")
    params.append(campaign_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE broadcast_campaigns SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def list_wa_audience_eligible_user_ids(
    category: str = "UTILITY",
    inactive_days: Optional[int] = None,
    limit: int = 10000,
    require_opt_in: Optional[bool] = None,
) -> list[str]:
    """רשימת user_ids שיקבלו את הקמפיין, אחרי אכיפת opt-in/opt-out + פילטרים.

    require_opt_in: True לאכוף opt-in מפורש (גם ב-UTILITY/AUTH), None/False
    לברירת מחדל לפי קטגוריה. MARKETING תמיד אוכף ללא קשר.
    """
    normalized_category = (category or "UTILITY").upper()
    must_opt_in = normalized_category == "MARKETING" or bool(require_opt_in)
    with get_connection() as conn:
        if must_opt_in:
            where = (
                "channel = 'whatsapp' AND wa_marketing_opt_in = 1 "
                "AND wa_opted_out_at IS NULL"
            )
        else:
            where = "channel = 'whatsapp' AND wa_opted_out_at IS NULL"

        if inactive_days is not None and inactive_days > 0:
            rows = conn.execute(
                f"SELECT user_id FROM users WHERE {where} "
                f"AND last_active_at >= datetime('now', '-' || ? || ' days') "
                "LIMIT ?",
                (inactive_days, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT user_id FROM users WHERE {where} LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["user_id"] for r in rows]


# ─── Web Push Subscriptions ─────────────────────────────────────────────────
# מנויי דפדפן של בעל העסק להתראות Web Push. אין PII של משתמש קצה —
# רק endpoint+מפתחות הצפנה של הדפדפן של הבעלים. ראה
# notifications/push_service.py.

def upsert_push_subscription(
    endpoint: str, p256dh: str, auth: str, user_agent: str | None = None
) -> None:
    """שמירה/דריסה של מנוי Web Push. endpoint הוא ה-natural key.

    אותו דפדפן יכול להירשם שוב (למשל אחרי טעינה מחדש) — ON CONFLICT
    מתחזק שורה אחת בלבד לכל endpoint, ומעדכן את ה-keys לערכים האחרונים.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent
            """,
            (endpoint, p256dh, auth, user_agent or ""),
        )


def get_all_push_subscriptions() -> list[dict]:
    """כל מנויי ה-Web Push הפעילים — נקרא מ-notify_live_chat_message."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_push_subscription(endpoint: str) -> None:
    """מחיקת מנוי. נקרא כש-push service מחזיר 404/410 (מנוי פג תוקף)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def touch_push_subscription(endpoint: str) -> None:
    """עדכון last_used_at — נקרא אחרי שליחת push מוצלחת."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE push_subscriptions SET last_used_at = CURRENT_TIMESTAMP WHERE endpoint = ?",
            (endpoint,),
        )


# ──────────────────────────────────────────────────────────────────────────
# ── Customer Memory System — מערכת זיכרון מתמשך פר-לקוח ──────────────────
# ──────────────────────────────────────────────────────────────────────────
# CRUD לטבלאות customer_facts / business_profile / extraction_runs (שלב 1
# של מערכת הזיכרון). נקרא משלב 3 (extractor), שלב 4 (validator) ושלב 8
# (context injection). ראה docs/Customer-memory/claude_code_instructions.md.


def get_customer_facts(
    user_id: str,
    business_id: str = "default",
    status: str = "active",
) -> list[dict]:
    """שולף facts של משתמש עם סינון לפי status.

    status: 'active' / 'pending_approval' / 'rejected' / 'superseded' / 'all'.
    'all' מחזיר את הכל ללא סינון (לצורך מסך פירוט באדמין).
    סדר: confidence DESC, last_confirmed_at DESC, id DESC (tiebreaker יציב).
    """
    with get_connection() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM customer_facts WHERE user_id = ? AND business_id = ? "
                "ORDER BY confidence DESC, last_confirmed_at DESC, id DESC",
                (user_id, business_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM customer_facts WHERE user_id = ? AND business_id = ? "
                "AND status = ? "
                "ORDER BY confidence DESC, last_confirmed_at DESC, id DESC",
                (user_id, business_id, status),
            ).fetchall()
        return [dict(r) for r in rows]


def insert_customer_fact(fact_data: dict) -> int:
    """מוסיף fact חדש. מחזיר id של השורה שנוצרה.

    fact_data חובה: user_id, fact_type, content, confidence, status.
    אופציונליים: business_id (default 'default'), source (default 'inferred'),
    requires_consent (default 0), evidence (default ''), superseded_by_id.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO customer_facts
               (user_id, business_id, fact_type, content, confidence, source,
                requires_consent, status, evidence, superseded_by_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_data["user_id"],
                fact_data.get("business_id", "default"),
                fact_data["fact_type"],
                fact_data["content"],
                float(fact_data["confidence"]),
                fact_data.get("source", "inferred"),
                1 if fact_data.get("requires_consent") else 0,
                fact_data["status"],
                fact_data.get("evidence", ""),
                fact_data.get("superseded_by_id"),
            ),
        )
        return int(cur.lastrowid)


def update_customer_fact(fact_id: int, updates: dict) -> int:
    """מעדכן שדות ב-fact קיים. מחזיר rowcount (1 או 0).

    שדות מותרים: status, content, confidence, requires_consent, evidence,
    superseded_by_id, last_confirmed_at, access_count. שדות אחרים נדחים
    בשקט — שינוי user_id / business_id / fact_type / created_at אסורים.

    נורמליזציה לערכים מסוימים — חייבת להיות תואמת ל-insert_customer_fact
    כדי שטיפוסי העמודות (REAL/INTEGER) יישמרו עקביים. בלי זה, LLM שמחזיר
    confidence="0.9" (string) ישמור TEXT בעמודת REAL ויפיל את ה-ORDER BY
    confidence DESC ב-get_customer_facts.
    """
    allowed = {
        "status", "content", "confidence", "requires_consent", "evidence",
        "superseded_by_id", "last_confirmed_at", "access_count",
    }
    cols = [k for k in updates if k in allowed]
    if not cols:
        return 0

    normalized: dict = {}
    for k in cols:
        v = updates[k]
        if k == "confidence":
            normalized[k] = float(v)
        elif k == "requires_consent":
            normalized[k] = 1 if v else 0
        elif k == "access_count":
            normalized[k] = int(v)
        elif k == "superseded_by_id":
            normalized[k] = int(v) if v is not None else None
        else:
            normalized[k] = v

    set_clause = ", ".join(f"{k} = ?" for k in cols)
    params = tuple(normalized[k] for k in cols) + (fact_id,)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE customer_facts SET {set_clause} WHERE id = ?", params,
        )
        return cur.rowcount


def delete_customer_fact(fact_id: int) -> int:
    """מחיקה קשה של fact בודד (שלב 7 — פאנל admin). מחזיר rowcount.

    בניגוד ל-status='rejected' (soft delete שמשמש את הזרימה האוטומטית),
    זו פעולה ידנית של בעל העסק שמוחקת את השורה לחלוטין מה-DB.
    """
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM customer_facts WHERE id = ?", (fact_id,))
        return cur.rowcount


def transition_customer_fact_status(
    fact_id: int,
    business_id: str,
    from_status: str,
    to_status: str,
) -> int:
    """Atomic compare-and-swap: מעדכן status רק אם הוא שווה ל-from_status
    וה-business_id תואם. מחזיר rowcount (0 = fact לא קיים / כבר טופל /
    שייך לעסק אחר). הדפוס נלקח מ-broadcast_messages (database.py:4579).

    שימוש בשלב 7 (פאנל admin) ב-approve/reject — מונע race condition
    שבה לחיצה כפולה / refresh lag דוחה fact שכבר אושר, או חוצה גבולות
    multi-tenancy.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE customer_facts SET status = ? "
            "WHERE id = ? AND business_id = ? AND status = ?",
            (to_status, fact_id, business_id, from_status),
        )
        return cur.rowcount


def get_pending_facts_count(business_id: str = "default") -> int:
    """COUNT(*) של pending_approval בעסק.

    שאילתה זולה (אינדקס על status). משמש את ה-badge ב-/api/stats —
    לא חסום ל-200 כמו get_pending_facts (שיש לו limit ל-UI).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM customer_facts "
            "WHERE business_id = ? AND status = 'pending_approval'",
            (business_id,),
        ).fetchone()
        return int(row["c"])


# ────────────────────────────────────────────────────────────────────
# Helpers ל-memory/background.py (שלב 6 — Background extraction scheduler)
# ────────────────────────────────────────────────────────────────────


def get_users_active_since(since_iso: str) -> list[str]:
    """SELECT DISTINCT user_id מ-conversations מאז `since_iso` (UTC string
    בפורמט YYYY-MM-DD HH:MM:SS).

    **שים לב (שלב 6.4)**: לא נקרא יותר מ-scheduler — הוחלף ב-
    `get_users_with_pending_messages` שמטפל גם ב-backlog של משתמשים
    נטושים. הפונקציה נשמרת לתאימות אחורה ושימוש אד-הוק.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM conversations "
            "WHERE created_at > ? "
            "ORDER BY user_id",
            (since_iso,),
        ).fetchall()
        return [r["user_id"] for r in rows]


def get_users_with_pending_messages(
    business_id: str = "default",
    lookback_days: int = 7,
) -> list[str]:
    """משתמשים שיש להם הודעות שטרם עובדו ע"י ה-extractor.

    מחזירה DISTINCT user_id משני המקורות (UNION לוגית):
    - **backlog**: יש run קודם (completed עם last_message_id) ויש
      conversations עם `id > last_message_id`. גם אם השיחה ישנה
      מ-`lookback_days`, נמשיך לעבד אותה — backlog לא נושר עם זמן.
    - **new users**: אין run קודם, ויש הודעות בתוך `lookback_days`.
      cap הגיוני שמונע סריקת היסטוריה ארוכה למשתמשים שמעולם לא דיברו
      / נטשו לפני זמן רב.

    הסיבה (שלב 6.4): `get_users_active_since` הישן סינן רק לפי
    `created_at`, כך שמשתמש שנטש לאחר שליחת 80 הודעות, ולא חזר ב-7
    ימים, נפל מהרשימה ו-30 ההודעות שלא נכנסו לcap הראשון נעלמו לעד.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.user_id
            FROM conversations c
            LEFT JOIN (
                SELECT user_id, MAX(last_message_id) AS last_id
                FROM extraction_runs
                WHERE business_id = ?
                  AND status = 'completed'
                  AND last_message_id IS NOT NULL
                GROUP BY user_id
            ) er ON er.user_id = c.user_id
            WHERE
                -- backlog: יש run קודם, יש הודעות אחריו
                (er.last_id IS NOT NULL AND c.id > er.last_id)
                OR
                -- חדש: אין run completed, יש הודעות ב-lookback
                (er.last_id IS NULL AND c.created_at > ?)
            ORDER BY c.user_id
            """,
            (business_id, cutoff),
        ).fetchall()
        return [r["user_id"] for r in rows]


def get_conversation_after(
    user_id: str,
    after_id: int | None,
    since_iso: str | None,
    limit: int,
) -> list[dict]:
    """הודעות של user_id מעובדות **מהישנות לחדשות** (ASC), עם cap.

    - `after_id` (העדפה ראשונה): `WHERE id > after_id`. cursor הראשי.
    - `since_iso` (אם after_id=None): `WHERE created_at > since_iso`.
      fallback למשתמש חדש (אין last_message_id).
    - שניהם None: כל ההודעות, חתוך ל-limit הראשונות.

    **קריטי** (שלב 6.3): סדר ASC, לא DESC. אם backlog > cap (80 הודעות
    חדשות, cap=50): סבב 1 מעבד את 50 הראשונות (ids נמוכים), שומר
    last_message_id = MAX(ids ב-batch). סבב 2 ממשיך מ-`id > last`
    ומעבד את ה-30 הנותרות. לפני התיקון (DESC + LIMIT) — סבב 1 היה
    שולף את ה-50 ה**אחרונות** ושומר MAX → סבב הבא היה מתחיל מ-id יותר
    גבוה, וה-30 הראשונות (ישנות) היו נעלמות לעד.
    """
    with get_connection() as conn:
        base = "SELECT * FROM conversations WHERE user_id = ?"
        params: list = [user_id]
        if after_id is not None:
            base += " AND id > ?"
            params.append(int(after_id))
        elif since_iso is not None:
            base += " AND created_at > ?"
            params.append(since_iso)
        sql = f"{base} ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_user_last_message_time(user_id: str) -> str | None:
    """MAX(created_at) מכל הודעות המשתמש ב-conversations. מחזיר None
    אם אין.

    משמש את idle check ב-scheduler (שלב 6.3): מבוסס על **כל ההודעות**
    של המשתמש, לא רק ה-batch הנוכחי. סיבה: backlog יכול להיות גדול
    מ-cap; ה-batch הנוכחי עלול לכלול הודעות ישנות (חלק מ-backlog)
    גם כש-שיחה פעילה בפועל. בדיקה על MAX הכוללת — האם בכלל יש פעילות
    חדשה מתחת ל-30 דקות.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS m FROM conversations WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["m"] if row and row["m"] else None


def get_last_extraction_end(user_id: str, business_id: str) -> str | None:
    """MAX(conversation_end) מ-extraction_runs ל-(user_id, business_id),
    מסונן ל-status='completed' כדי שכישלון לא ייחשב מסומן.

    שונה מ-get_last_extraction_run (קיים) שלא מסנן status. המשמעות: אם
    run אחרון נכשל ב-LLM error, נחזור על אותו טווח הודעות בסבב הבא.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(conversation_end) AS last_end "
            "FROM extraction_runs "
            "WHERE user_id = ? AND business_id = ? AND status = 'completed'",
            (user_id, business_id),
        ).fetchone()
        return row["last_end"] if row and row["last_end"] else None


def get_pending_facts(business_id: str = "default", limit: int = 200) -> list[dict]:
    """כל ה-facts במצב pending_approval בעסק, עם username מ-users (LEFT JOIN).

    מיון: created_at DESC + id DESC (tiebreaker יציב כי משולב LIMIT).
    cap 200 — UI לא מציג יותר; אם יש יותר זה סימן שצריך לכייל את המודל.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT cf.*, u.username AS username "
            "FROM customer_facts cf "
            "LEFT JOIN users u ON u.user_id = cf.user_id "
            "WHERE cf.business_id = ? AND cf.status = 'pending_approval' "
            "ORDER BY cf.created_at DESC, cf.id DESC "
            "LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_users_with_facts(business_id: str = "default") -> list[dict]:
    """רשימת user_id שיש להם ≥1 fact active/pending בעסק.

    מחזיר user_id, username (מ-users, COALESCE ל-''), fact_count,
    last_update (MAX(created_at)). מיון: last_update DESC, user_id ASC
    (tiebreaker כשאין facts באותו millisecond).
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT cf.user_id, "
            "       COALESCE(u.username, '') AS username, "
            "       COUNT(*) AS fact_count, "
            "       MAX(cf.created_at) AS last_update "
            "FROM customer_facts cf "
            "LEFT JOIN users u ON u.user_id = cf.user_id "
            "WHERE cf.business_id = ? "
            "  AND cf.status IN ('active', 'pending_approval') "
            "GROUP BY cf.user_id, u.username "
            "ORDER BY last_update DESC, cf.user_id ASC",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_business_profile(business_id: str = "default") -> dict:
    """מחזיר את פרופיל העסק, או dict ריק אם לא מוגדר.

    services_json נשאר כ-string ב-DB; הקורא אחראי ל-json.loads אם נדרש.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM business_profile WHERE business_id = ?",
            (business_id,),
        ).fetchone()
        return dict(row) if row else {}


def upsert_business_profile(profile_data: dict) -> None:
    """INSERT OR REPLACE — שמירה או דריסה של פרופיל עסק.

    profile_data חובה: business_id. שאר השדות אופציונליים.
    services_json צריך להגיע כבר כ-string (json.dumps על הצד הקורא).
    """
    business_id = profile_data["business_id"]
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO business_profile
               (business_id, business_type, business_name, services_json,
                what_matters_for_extraction, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(business_id) DO UPDATE SET
                   business_type = excluded.business_type,
                   business_name = excluded.business_name,
                   services_json = excluded.services_json,
                   what_matters_for_extraction = excluded.what_matters_for_extraction,
                   updated_at = datetime('now')""",
            (
                business_id,
                profile_data.get("business_type", ""),
                profile_data.get("business_name", ""),
                profile_data.get("services_json", "[]"),
                profile_data.get("what_matters_for_extraction", ""),
            ),
        )


def log_extraction_run(run_data: dict) -> int:
    """כתיבת שורה ל-extraction_runs בסיום ריצה. מחזיר id.

    חובה: user_id, status.
    אופציונליים: business_id, conversation_start, conversation_end,
    messages_count, extractions_count, skipped_count, error_message,
    tokens_used, last_message_id (cursor id-based — שלב 6.2).
    """
    last_message_id = run_data.get("last_message_id")
    if last_message_id is not None:
        last_message_id = int(last_message_id)
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO extraction_runs
               (user_id, business_id, conversation_start, conversation_end,
                messages_count, extractions_count, skipped_count, status,
                error_message, tokens_used, last_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_data["user_id"],
                run_data.get("business_id", "default"),
                run_data.get("conversation_start"),
                run_data.get("conversation_end"),
                int(run_data.get("messages_count", 0)),
                int(run_data.get("extractions_count", 0)),
                int(run_data.get("skipped_count", 0)),
                run_data["status"],
                run_data.get("error_message", ""),
                int(run_data.get("tokens_used", 0)),
                last_message_id,
            ),
        )
        return int(cur.lastrowid)


def get_last_extraction_message_id(
    user_id: str, business_id: str,
) -> int | None:
    """MAX(last_message_id) ל-(user_id, business_id) מתוך runs מוצלחים
    בלבד. מחזיר None עבור משתמש חדש או runs ישנים בלי last_message_id.

    זה ה-cursor הראשי של ה-scheduler (שלב 6.2) — מבוסס id (monotonic,
    unique, atomic), לא timestamp. עוקף באג same-second.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(last_message_id) AS m FROM extraction_runs "
            "WHERE user_id = ? AND business_id = ? "
            "AND status = 'completed' AND last_message_id IS NOT NULL",
            (user_id, business_id),
        ).fetchone()
        if row and row["m"] is not None:
            return int(row["m"])
        return None


def supersede_customer_fact(old_id: int, new_fact_data: dict) -> int:
    """ATOMIC supersede — INSERT של fact חדש + UPDATE של הישן (status='superseded'
    + superseded_by_id) באותה טרנזקציה. מחזיר id של ה-fact החדש.

    CLAUDE.md — atomicity של linked-field: שינוי status על fact אחד יחד
    עם יצירת fact קשור חייבים להיות בטרנזקציה אחת. אם UPDATE נכשל אחרי
    INSERT מוצלח, ה-rollback של ה-context manager מחזיר את שני השינויים
    כיחידה אחת (אחרת היה נשאר fact חדש active + fact ישן עדיין active —
    שני active facts מתחרים על אותו רעיון).

    שדות נורמליזציה זהים ל-insert_customer_fact (float ל-confidence,
    int 0/1 ל-requires_consent).
    """
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO customer_facts
               (user_id, business_id, fact_type, content, confidence, source,
                requires_consent, status, evidence, superseded_by_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_fact_data["user_id"],
                new_fact_data.get("business_id", "default"),
                new_fact_data["fact_type"],
                new_fact_data["content"],
                float(new_fact_data["confidence"]),
                new_fact_data.get("source", "inferred"),
                1 if new_fact_data.get("requires_consent") else 0,
                new_fact_data["status"],
                new_fact_data.get("evidence", ""),
                new_fact_data.get("superseded_by_id"),
            ),
        )
        new_id = int(cur.lastrowid)
        # UPDATE על הישן באותה טרנזקציה — אם זה נכשל, ה-INSERT מתבטל.
        conn.execute(
            "UPDATE customer_facts SET status = 'superseded', "
            "superseded_by_id = ? WHERE id = ?",
            (new_id, old_id),
        )
        return new_id


def resolve_customer_fact(fact_id: int, evidence: str = "") -> int:
    """סגירת open_issue קיים (action=resolve). מחזיר rowcount (1 או 0).

    UPDATE אטומי יחיד: status='resolved' + resolved_at=datetime('now') +
    resolution_evidence. לא יוצר שורה חדשה — רק מסמן את ה-issue הקיים
    כסגור כך שלא יוזרק יותר ל-context (CLAUDE.md — atomicity של
    linked-field: status + שדות נלווים יחד).
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE customer_facts SET status = 'resolved', "
            "resolved_at = datetime('now'), resolution_evidence = ? "
            "WHERE id = ?",
            (evidence, fact_id),
        )
        return cur.rowcount


def get_last_extraction_run(user_id: str, business_id: str = "default") -> dict:
    """מחזיר את ה-run האחרון של המשתמש (לפי created_at), או dict ריק.

    משמש את ה-background scheduler (שלב 6) כדי לדעת ממתי לקרוא הודעות
    חדשות. tiebreaker על id כדי שיהיה דטרמיניסטי גם בשתי ריצות באותה
    שנייה.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM extraction_runs "
            "WHERE user_id = ? AND business_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id, business_id),
        ).fetchone()
        return dict(row) if row else {}
