"""
טסטים לתשתית DB של מערכת הזיכרון המתמשך (שלב 1).

מכסה: יצירת טבלאות, CRUD בסיסי, partial UNIQUE לדדאפ,
ו-delete_user_data שכולל את הטבלאות החדשות (תיקון 13).
"""

import json
import sqlite3

import pytest


# ייבוא ברמת המודול: ה-autouse fixture `_isolate_env` ב-conftest.py מגדיר
# DB_PATH ל-tmp_path *לפני* ייבוא database. ה-`db_conn` fixture מאתחל את
# הסכימה דרך init_db() ומחזיק patch על DB_PATH לאורך הטסט, כך שכל קריאה
# פנימית ל-get_connection() תיפול לאותו tmp DB.
from database import (
    delete_user_data,
    get_business_profile,
    get_customer_facts,
    get_last_extraction_run,
    insert_customer_fact,
    log_extraction_run,
    update_customer_fact,
    upsert_business_profile,
)


class TestSchema:
    def test_init_creates_memory_tables(self, db_conn):
        """init_db צריך ליצור את 3 הטבלאות החדשות."""
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('customer_facts', 'business_profile', 'extraction_runs')"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert names == {"customer_facts", "business_profile", "extraction_runs"}

    def test_init_creates_memory_indexes(self, db_conn):
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_customer_facts_user_business', 'idx_customer_facts_status', "
            "'idx_customer_facts_active_unique', 'idx_extraction_runs_user')"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert names == {
            "idx_customer_facts_user_business",
            "idx_customer_facts_status",
            "idx_customer_facts_active_unique",
            "idx_extraction_runs_user",
        }

    def test_check_constraints_reject_invalid_fact_type(self, db_conn):
        """CHECK constraint על fact_type — ערך לא חוקי נדחה."""
        with pytest.raises(sqlite3.IntegrityError):
            insert_customer_fact({
                "user_id": "u1",
                "fact_type": "bogus_type",
                "content": "x",
                "confidence": 0.9,
                "status": "active",
            })


class TestCustomerFactsCRUD:
    def test_insert_and_get(self, db_conn):
        fact_id = insert_customer_fact({
            "user_id": "u1",
            "fact_type": "preference",
            "content": "מעדיפה תורים בשעות הבוקר",
            "confidence": 0.92,
            "status": "active",
            "evidence": "הכי טוב לי בבוקר",
        })
        assert fact_id > 0
        facts = get_customer_facts("u1", status="active")
        assert len(facts) == 1
        assert facts[0]["content"] == "מעדיפה תורים בשעות הבוקר"
        assert facts[0]["confidence"] == pytest.approx(0.92)
        assert facts[0]["requires_consent"] == 0
        assert facts[0]["business_id"] == "default"
        assert facts[0]["source"] == "inferred"

    def test_requires_consent_persisted_as_1(self, db_conn):
        insert_customer_fact({
            "user_id": "u1",
            "fact_type": "personal_info",
            "content": "רגישה לאגוזים",
            "confidence": 0.9,
            "status": "pending_approval",
            "requires_consent": True,
        })
        facts = get_customer_facts("u1", status="pending_approval")
        assert facts[0]["requires_consent"] == 1

    def test_get_filters_by_status(self, db_conn):
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "f1", "confidence": 0.9, "status": "active",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "f2", "confidence": 0.7, "status": "pending_approval",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "personal_info",
            "content": "f3", "confidence": 0.8, "status": "rejected",
        })

        assert len(get_customer_facts("u1", status="active")) == 1
        assert len(get_customer_facts("u1", status="pending_approval")) == 1
        assert len(get_customer_facts("u1", status="all")) == 3

    def test_get_orders_by_confidence_desc(self, db_conn):
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "low", "confidence": 0.7, "status": "active",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "high", "confidence": 0.95, "status": "active",
        })
        facts = get_customer_facts("u1", status="active")
        assert facts[0]["content"] == "high"
        assert facts[1]["content"] == "low"

    def test_business_id_isolates_facts(self, db_conn):
        """facts של business_id אחר לא מגיעים בשליפה."""
        insert_customer_fact({
            "user_id": "u1", "business_id": "default",
            "fact_type": "preference",
            "content": "a", "confidence": 0.9, "status": "active",
        })
        insert_customer_fact({
            "user_id": "u1", "business_id": "other",
            "fact_type": "preference",
            "content": "b", "confidence": 0.9, "status": "active",
        })
        assert len(get_customer_facts("u1", "default", "active")) == 1
        assert len(get_customer_facts("u1", "other", "active")) == 1

    def test_update_allowed_fields(self, db_conn):
        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.9, "status": "pending_approval",
        })
        n = update_customer_fact(fact_id, {"status": "active", "access_count": 5})
        assert n == 1
        facts = get_customer_facts("u1", status="active")
        assert facts[0]["access_count"] == 5

    def test_update_rejects_disallowed_fields(self, db_conn):
        """user_id / business_id / fact_type / created_at לא ניתנים לעדכון."""
        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        n = update_customer_fact(fact_id, {
            "user_id": "hacker", "fact_type": "open_issue",
            "created_at": "1999-01-01",
        })
        assert n == 0
        facts = get_customer_facts("u1", status="active")
        assert facts[0]["user_id"] == "u1"
        assert facts[0]["fact_type"] == "preference"

    def test_update_no_rows(self, db_conn):
        assert update_customer_fact(99999, {"status": "active"}) == 0

    def test_active_dedup_unique_index(self, db_conn):
        """ה-partial UNIQUE על (user, business, fact_type, content)
        WHERE status='active' מונע שני facts פעילים זהים — safety net
        מעל הדדאפ ברמת האפליקציה."""
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "same content", "confidence": 0.9, "status": "active",
        })
        with pytest.raises(sqlite3.IntegrityError):
            insert_customer_fact({
                "user_id": "u1", "fact_type": "preference",
                "content": "same content", "confidence": 0.95, "status": "active",
            })

    def test_dedup_index_allows_non_active(self, db_conn):
        """ה-partial UNIQUE לא חוסם status אחר — superseded יכול לחיות
        בצד active חדש, ולקיים שני pending_approval זהים מותר (האפליקציה
        תטפל בזה)."""
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "same", "confidence": 0.9, "status": "active",
        })
        # superseded — מותר
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "same", "confidence": 0.9, "status": "superseded",
        })
        # שני rejected — מותר (partial UNIQUE רק על status='active')
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "same", "confidence": 0.9, "status": "rejected",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "same", "confidence": 0.9, "status": "rejected",
        })
        assert len(get_customer_facts("u1", status="all")) == 4


class TestUpdateNormalization:
    """update_customer_fact חייב לנרמל ערכים כמו insert_customer_fact —
    אחרת LLM שמחזיר confidence="0.9" (string) או requires_consent=True
    (bool במקום 0/1) ישבור את ה-ORDER BY confidence DESC בעמודת REAL."""

    def test_confidence_string_normalized_to_float(self, db_conn):
        from database import insert_customer_fact, update_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.5, "status": "active",
        })
        update_customer_fact(fact_id, {"confidence": "0.92"})

        row = db_conn.execute(
            "SELECT confidence, typeof(confidence) AS t "
            "FROM customer_facts WHERE id = ?", (fact_id,),
        ).fetchone()
        assert row["t"] == "real"
        assert row["confidence"] == pytest.approx(0.92)

    def test_requires_consent_bool_normalized_to_int(self, db_conn):
        from database import insert_customer_fact, update_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "personal_info",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        update_customer_fact(fact_id, {"requires_consent": True})

        row = db_conn.execute(
            "SELECT requires_consent, typeof(requires_consent) AS t "
            "FROM customer_facts WHERE id = ?", (fact_id,),
        ).fetchone()
        assert row["t"] == "integer"
        assert row["requires_consent"] == 1

    def test_access_count_string_normalized_to_int(self, db_conn):
        from database import insert_customer_fact, update_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        update_customer_fact(fact_id, {"access_count": "5"})

        row = db_conn.execute(
            "SELECT access_count, typeof(access_count) AS t "
            "FROM customer_facts WHERE id = ?", (fact_id,),
        ).fetchone()
        assert row["t"] == "integer"
        assert row["access_count"] == 5

    def test_superseded_by_id_none_stays_null(self, db_conn):
        """superseded_by_id=None שומר NULL בעמודה."""
        from database import insert_customer_fact, update_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        update_customer_fact(fact_id, {"superseded_by_id": None})
        row = db_conn.execute(
            "SELECT superseded_by_id FROM customer_facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        assert row["superseded_by_id"] is None


class TestSupersedeAtomic:
    """supersede_customer_fact: INSERT + UPDATE באותה טרנזקציה.
    הפונקציה תוקנה ב-PR #288 בעקבות סקירה (Medium: linked-field atomicity)."""

    def test_creates_new_and_marks_old_in_one_call(self, db_conn):
        from database import (
            get_customer_facts, insert_customer_fact, supersede_customer_fact,
        )

        old_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה בקרים", "confidence": 0.9, "status": "active",
        })
        new_id = supersede_customer_fact(old_id, {
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה ערבים", "confidence": 0.91, "status": "active",
            "requires_consent": False, "evidence": "ערבים נוח לי",
        })
        assert new_id != old_id

        all_facts = get_customer_facts("u1", status="all")
        assert len(all_facts) == 2
        old = next(f for f in all_facts if f["id"] == old_id)
        new = next(f for f in all_facts if f["id"] == new_id)
        assert old["status"] == "superseded"
        assert old["superseded_by_id"] == new_id
        assert new["status"] == "active"

    def test_rollback_when_update_fails(self, db_conn):
        """אם ה-UPDATE נכשל (למשל constraint violation אקזוטי), גם ה-INSERT
        מתבטל — לא נשארים עם new active + old active (שני מתחרים).

        נדמה כשל ע"י הוספה ידנית של old_id לא חוקי שלא קיים — UPDATE עם
        WHERE id=X לא יזרוק שגיאה (יחזיר rowcount=0). לכן נבדוק תרחיש שכן
        זורק: insert עם content שיוצר IntegrityError (כפילות active שכבר
        קיים) — חייב להחזיר rollback מלא ולא לדלוף active נוסף.
        """
        import sqlite3

        from database import (
            get_customer_facts, insert_customer_fact, supersede_customer_fact,
        )

        # קיים active "blocker" עם content X
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "blocker active", "confidence": 0.9, "status": "active",
        })
        old_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "something else", "confidence": 0.9, "status": "active",
        })

        # ננסה supersede שיוצר content "blocker active" → partial UNIQUE מפיל
        with pytest.raises(sqlite3.IntegrityError):
            supersede_customer_fact(old_id, {
                "user_id": "u1", "fact_type": "preference",
                "content": "blocker active",  # מתנגש ב-partial UNIQUE
                "confidence": 0.9, "status": "active",
                "requires_consent": False, "evidence": "ev",
            })

        # ה-old חייב להישאר active (כי ה-INSERT התבטל ולכן גם ה-UPDATE
        # נכשל באטומיות — שום שינוי לא נשמר).
        all_facts = get_customer_facts("u1", status="all")
        old = next(f for f in all_facts if f["id"] == old_id)
        assert old["status"] == "active"
        assert old["superseded_by_id"] is None
        # שום fact חדש לא נוצר (rollback מלא)
        contents = [f["content"] for f in all_facts]
        assert contents.count("blocker active") == 1  # רק ה-blocker המקורי


class TestBusinessProfile:
    def test_upsert_then_get(self, db_conn):
        upsert_business_profile({
            "business_id": "default",
            "business_type": "מספרה",
            "business_name": "סטודיו לירון",
            "services_json": json.dumps([
                {"name": "תספורת", "aliases": ["גזירה"], "category": "תספורות"},
            ]),
            "what_matters_for_extraction": "סוג שיער, אורך מועדף",
        })
        prof = get_business_profile("default")
        assert prof["business_name"] == "סטודיו לירון"
        services = json.loads(prof["services_json"])
        assert services[0]["name"] == "תספורת"
        assert services[0]["aliases"] == ["גזירה"]

    def test_upsert_replaces_existing(self, db_conn):
        upsert_business_profile({
            "business_id": "default",
            "business_name": "old name",
        })
        upsert_business_profile({
            "business_id": "default",
            "business_name": "new name",
        })
        assert get_business_profile("default")["business_name"] == "new name"

    def test_get_empty_returns_empty_dict(self, db_conn):
        assert get_business_profile("nonexistent") == {}


class TestExtractionRuns:
    def test_log_and_get_last(self, db_conn):
        rid = log_extraction_run({
            "user_id": "u1",
            "status": "completed",
            "messages_count": 5,
            "extractions_count": 2,
            "skipped_count": 1,
            "tokens_used": 1234,
        })
        assert rid > 0
        last = get_last_extraction_run("u1")
        assert last["status"] == "completed"
        assert last["tokens_used"] == 1234
        assert last["extractions_count"] == 2

    def test_get_last_returns_newest(self, db_conn):
        log_extraction_run({"user_id": "u1", "status": "completed"})
        log_extraction_run({
            "user_id": "u1", "status": "failed",
            "error_message": "API timeout",
        })
        last = get_last_extraction_run("u1")
        assert last["status"] == "failed"
        assert last["error_message"] == "API timeout"

    def test_get_last_empty(self, db_conn):
        assert get_last_extraction_run("nonexistent") == {}

    def test_running_status_allowed(self, db_conn):
        """status='running' חוקי — משמש כ-lock בשלב 6."""
        rid = log_extraction_run({"user_id": "u1", "status": "running"})
        assert rid > 0


class TestDeleteUserData:
    def test_purges_customer_facts(self, db_conn):
        """delete_user_data חייב למחוק customer_facts (תיקון 13 — מחיקת
        נגזרות AI). business_profile אינו per-user ולכן לא נמחק."""
        # יצירת שורת users כדי שמחיקת users עצמה לא תיכשל
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)",
            ("u1", "telegram"),
        )
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)",
            ("u2", "telegram"),
        )
        db_conn.commit()

        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "fact u1", "confidence": 0.9, "status": "active",
        })
        insert_customer_fact({
            "user_id": "u2", "fact_type": "preference",
            "content": "fact u2", "confidence": 0.9, "status": "active",
        })

        delete_user_data("u1")

        # u1 נמחק; u2 נשאר
        assert get_customer_facts("u1", status="all") == []
        assert len(get_customer_facts("u2", status="all")) == 1

    def test_purges_extraction_runs(self, db_conn):
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)",
            ("u1", "telegram"),
        )
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)",
            ("u2", "telegram"),
        )
        db_conn.commit()

        log_extraction_run({"user_id": "u1", "status": "completed"})
        log_extraction_run({"user_id": "u2", "status": "completed"})

        delete_user_data("u1")

        assert get_last_extraction_run("u1") == {}
        assert get_last_extraction_run("u2")["status"] == "completed"

    def test_business_profile_survives_user_deletion(self, db_conn):
        """business_profile אינו per-user ואסור שיימחק."""
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)",
            ("u1", "telegram"),
        )
        db_conn.commit()
        upsert_business_profile({
            "business_id": "default",
            "business_name": "Survives Test",
        })

        delete_user_data("u1")

        assert get_business_profile("default")["business_name"] == "Survives Test"


class TestResolveCustomerFact:
    """resolve_customer_fact + status 'resolved' (action=resolve, פרומפט v2.2)."""

    def test_resolve_marks_fact_resolved(self, db_conn):
        from database import (
            get_customer_facts, insert_customer_fact, resolve_customer_fact,
        )
        fid = insert_customer_fact({
            "user_id": "u1", "fact_type": "open_issue",
            "content": "ממתינה להחזר", "confidence": 0.9, "status": "active",
        })
        n = resolve_customer_fact(fid, "ההחזר הגיע")
        assert n == 1
        row = db_conn.execute(
            "SELECT status, resolved_at, resolution_evidence "
            "FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "resolved"
        assert row["resolved_at"]
        assert row["resolution_evidence"] == "ההחזר הגיע"
        # resolved כבר לא מופיע ב-active
        assert get_customer_facts("u1", status="active") == []
        assert get_customer_facts("u1", status="resolved")[0]["id"] == fid

    def test_resolve_missing_fact_returns_zero(self, db_conn):
        from database import resolve_customer_fact
        assert resolve_customer_fact(999999, "x") == 0

    def test_resolved_status_accepted_by_check(self, db_conn):
        from database import insert_customer_fact, update_customer_fact, get_customer_facts
        fid = insert_customer_fact({
            "user_id": "u1", "fact_type": "open_issue",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        # CHECK constraint מתיר 'resolved' (אחרת זה היה זורק IntegrityError)
        update_customer_fact(fid, {"status": "resolved"})
        assert get_customer_facts("u1", status="resolved")[0]["id"] == fid

    def test_delete_user_data_removes_resolved(self, db_conn):
        from database import (
            delete_user_data, get_customer_facts, insert_customer_fact,
            resolve_customer_fact,
        )
        db_conn.execute(
            "INSERT INTO users (user_id, channel) VALUES (?, ?)", ("u1", "telegram"),
        )
        db_conn.commit()
        fid = insert_customer_fact({
            "user_id": "u1", "fact_type": "open_issue",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        resolve_customer_fact(fid, "done")
        delete_user_data("u1")
        assert get_customer_facts("u1", status="all") == []


class TestCustomerFactsMigration:
    """Regression: בעבר היה באג שבו ה-migration ל-customer_facts לא נכתב
    ל-migrations.py בכלל. init_db יוצר את הסכמה החדשה רק על DB ריק
    (`CREATE TABLE IF NOT EXISTS`), אז על DB קיים שורות resolve היו
    קורסות עם OperationalError (חסרות עמודות + CHECK לא מתיר 'resolved').

    הגישה: ה-`db_conn` fixture נותן DB מלא. אנחנו DROP-ים את customer_facts
    ומחזירים אותו לסכמה הישנה + שורות, ואז מריצים את `run_migrations`
    שוב. הטבלאות האחרות כבר קיימות וה-`_ensure_column` calls שלהן הופכים
    ל-no-op — אז אנחנו בודקים את ה-migration של customer_facts בלי לבנות
    DB מאפס.
    """

    @pytest.fixture
    def old_schema_cf(self, db_conn):
        """מוריד את customer_facts ומחזיר אותו לסכמה הישנה (בלי 'resolved'
        וללא 2 העמודות החדשות) עם שורות מייצגות כולל self-FK chain."""
        db_conn.execute("PRAGMA foreign_keys=OFF")
        db_conn.execute("DROP TABLE IF EXISTS customer_facts")
        db_conn.execute("""
            CREATE TABLE customer_facts (
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
                                    CHECK(status IN ('active','pending_approval','rejected','superseded')),
                evidence            TEXT DEFAULT '',
                superseded_by_id    INTEGER REFERENCES customer_facts(id) ON DELETE SET NULL,
                created_at          TEXT DEFAULT (datetime('now')),
                last_confirmed_at   TEXT DEFAULT (datetime('now')),
                access_count        INTEGER DEFAULT 0
            )
        """)
        db_conn.execute(
            "CREATE INDEX idx_customer_facts_user_business "
            "ON customer_facts(user_id, business_id, status)"
        )
        db_conn.execute(
            "CREATE INDEX idx_customer_facts_status ON customer_facts(status)"
        )
        db_conn.execute(
            "CREATE UNIQUE INDEX idx_customer_facts_active_unique "
            "ON customer_facts(user_id, business_id, fact_type, content) "
            "WHERE status = 'active'"
        )
        # שורות מייצגות + self-FK chain (1 → 2)
        db_conn.execute(
            "INSERT INTO customer_facts "
            "(id, user_id, fact_type, content, confidence, status) "
            "VALUES (1, 'u', 'preference', 'old pref', 0.9, 'superseded')"
        )
        db_conn.execute(
            "INSERT INTO customer_facts "
            "(id, user_id, fact_type, content, confidence, status, superseded_by_id) "
            "VALUES (2, 'u', 'preference', 'new pref', 0.9, 'active', NULL)"
        )
        db_conn.execute("UPDATE customer_facts SET superseded_by_id=2 WHERE id=1")
        db_conn.execute(
            "INSERT INTO customer_facts "
            "(id, user_id, fact_type, content, confidence, status) "
            "VALUES (3, 'u', 'open_issue', 'pending issue', 0.9, 'active')"
        )
        db_conn.execute("PRAGMA foreign_keys=ON")
        db_conn.commit()
        return db_conn

    def test_migration_adds_resolved_status_and_columns(self, old_schema_cf):
        """ה-migration רץ → 'resolved' מתווסף ל-CHECK + 2 עמודות חדשות.
        זה הטסט שהיה תופס את הבאג של cursor."""
        conn = old_schema_cf
        sql_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='customer_facts'"
        ).fetchone()["sql"]
        assert "'resolved'" not in sql_before
        cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(customer_facts)")}
        assert "resolved_at" not in cols_before
        assert "resolution_evidence" not in cols_before

        from migrations import run_migrations
        run_migrations(conn)

        sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='customer_facts'"
        ).fetchone()["sql"]
        assert "'resolved'" in sql_after
        cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(customer_facts)")}
        assert "resolved_at" in cols_after
        assert "resolution_evidence" in cols_after

    def test_migration_preserves_rows_and_self_fk(self, old_schema_cf):
        """ה-rebuild לא מאבד נתונים ולא שובר את ה-self-FK (superseded_by_id)."""
        from migrations import run_migrations
        run_migrations(old_schema_cf)
        rows = old_schema_cf.execute(
            "SELECT id, content, status, superseded_by_id "
            "FROM customer_facts ORDER BY id"
        ).fetchall()
        assert [(r["id"], r["content"], r["status"], r["superseded_by_id"])
                for r in rows] == [
            (1, "old pref", "superseded", 2),
            (2, "new pref", "active", None),
            (3, "pending issue", "active", None),
        ]

    def test_migration_recreates_indexes(self, old_schema_cf):
        """3 האינדקסים נמחקים יחד עם הטבלה ב-rebuild ונוצרים מחדש."""
        from migrations import run_migrations
        run_migrations(old_schema_cf)
        idx = {
            r["name"] for r in old_schema_cf.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='customer_facts'"
            )
        }
        assert {
            "idx_customer_facts_user_business",
            "idx_customer_facts_status",
            "idx_customer_facts_active_unique",
        }.issubset(idx)

    def test_migration_enables_resolve_writes(self, old_schema_cf):
        """אחרי ה-migration, UPDATE עם status='resolved' + הכתיבה לעמודות
        החדשות עובדת — בדיוק התרחיש שהיה קורס לפני התיקון."""
        from migrations import run_migrations
        run_migrations(old_schema_cf)
        old_schema_cf.execute(
            "UPDATE customer_facts SET status='resolved', "
            "resolved_at=datetime('now'), resolution_evidence=? WHERE id=3",
            ("done",),
        )
        row = old_schema_cf.execute(
            "SELECT status, resolved_at, resolution_evidence "
            "FROM customer_facts WHERE id=3"
        ).fetchone()
        assert row["status"] == "resolved"
        assert row["resolved_at"]
        assert row["resolution_evidence"] == "done"

    def test_migration_is_idempotent(self, old_schema_cf):
        """הרצה שנייה לא קורסת ולא משנה כלום (resolved_at קיים → guard מדלג)."""
        from migrations import run_migrations
        run_migrations(old_schema_cf)
        cols_after_first = {
            r["name"] for r in old_schema_cf.execute("PRAGMA table_info(customer_facts)")
        }
        assert "resolved_at" in cols_after_first

        run_migrations(old_schema_cf)
        cols_after_second = {
            r["name"] for r in old_schema_cf.execute("PRAGMA table_info(customer_facts)")
        }
        assert cols_after_second == cols_after_first

    def test_migration_no_fk_violations(self, old_schema_cf):
        """PRAGMA foreign_key_check נקי אחרי ה-rebuild — self-FK 1→2 נשמר."""
        from migrations import run_migrations
        run_migrations(old_schema_cf)
        violations = old_schema_cf.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []


# ────────────────────────────────────────────────────────────────────
# שלב 7 — CRUD חדש לפאנל admin
# ────────────────────────────────────────────────────────────────────


def _seed_fact_for_admin(**overrides):
    """גרסה מינימלית ל-tests של שלב 7. ברירת מחדל: active, default business."""
    from database import insert_customer_fact
    base = {
        "user_id": "u1", "business_id": "default",
        "fact_type": "preference", "content": "default",
        "confidence": 0.9, "status": "active",
    }
    base.update(overrides)
    return insert_customer_fact(base)


class TestGetPendingFacts:
    def test_returns_only_pending(self, db_conn):
        _seed_fact_for_admin(user_id="u1", content="active1", status="active")
        _seed_fact_for_admin(user_id="u1", content="pending1", status="pending_approval")
        _seed_fact_for_admin(user_id="u2", content="rejected1", status="rejected")
        _seed_fact_for_admin(user_id="u3", content="pending2",
                             status="pending_approval", fact_type="personal_info")
        from database import get_pending_facts
        out = get_pending_facts()
        contents = sorted(f["content"] for f in out)
        assert contents == ["pending1", "pending2"]

    def test_business_id_filter(self, db_conn):
        _seed_fact_for_admin(user_id="u1", business_id="default",
                             content="here", status="pending_approval")
        _seed_fact_for_admin(user_id="u1", business_id="other",
                             content="there", status="pending_approval")
        from database import get_pending_facts
        assert [f["content"] for f in get_pending_facts("default")] == ["here"]
        assert [f["content"] for f in get_pending_facts("other")] == ["there"]

    def test_orders_by_created_at_desc(self, db_conn):
        """מיון: created_at DESC + id DESC (tiebreaker)."""
        id1 = _seed_fact_for_admin(user_id="u1", content="first",
                                   status="pending_approval")
        id2 = _seed_fact_for_admin(user_id="u1", content="second",
                                   status="pending_approval",
                                   fact_type="personal_info")
        from database import get_pending_facts
        out = get_pending_facts()
        # אותו created_at (datetime('now') באותה שניה) → tiebreaker id DESC
        # → id2 (חדש יותר) ראשון
        assert out[0]["id"] == id2
        assert out[1]["id"] == id1

    def test_empty_when_no_pending(self, db_conn):
        _seed_fact_for_admin(user_id="u1", content="a", status="active")
        from database import get_pending_facts
        assert get_pending_facts() == []


class TestGetUsersWithFacts:
    def test_groups_by_user_id_with_count(self, db_conn):
        _seed_fact_for_admin(user_id="u1", content="a", status="active")
        _seed_fact_for_admin(user_id="u1", content="b", status="pending_approval",
                             fact_type="personal_info")
        _seed_fact_for_admin(user_id="u2", content="c", status="active",
                             fact_type="relationship")
        from database import get_users_with_facts
        out = {u["user_id"]: u["fact_count"] for u in get_users_with_facts()}
        assert out == {"u1": 2, "u2": 1}

    def test_excludes_users_without_active_or_pending(self, db_conn):
        """user שיש לו רק facts ב-rejected/superseded/resolved לא נכלל."""
        _seed_fact_for_admin(user_id="u_inactive", content="x", status="rejected")
        _seed_fact_for_admin(user_id="u_active", content="y", status="active")
        from database import get_users_with_facts
        ids = [u["user_id"] for u in get_users_with_facts()]
        assert "u_inactive" not in ids
        assert "u_active" in ids

    def test_username_from_users_table(self, db_conn):
        _seed_fact_for_admin(user_id="u_with_name", content="x", status="active")
        # יצירת רשומת users עם username
        with db_conn:
            db_conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                ("u_with_name", "Alice"),
            )
        from database import get_users_with_facts
        out = {u["user_id"]: u["username"] for u in get_users_with_facts()}
        assert out["u_with_name"] == "Alice"

    def test_username_empty_when_no_users_row(self, db_conn):
        """LEFT JOIN — user_id ללא שורה ב-users → username = '' (COALESCE)."""
        _seed_fact_for_admin(user_id="u_orphan", content="x", status="active")
        from database import get_users_with_facts
        out = {u["user_id"]: u["username"] for u in get_users_with_facts()}
        assert out["u_orphan"] == ""


class TestDeleteCustomerFact:
    def test_delete_removes_row(self, db_conn):
        fid = _seed_fact_for_admin(user_id="u1", content="x")
        from database import delete_customer_fact, get_customer_facts
        assert delete_customer_fact(fid) == 1
        assert get_customer_facts("u1", status="all") == []

    def test_delete_nonexistent_returns_zero(self, db_conn):
        from database import delete_customer_fact
        assert delete_customer_fact(99999) == 0


# ────────────────────────────────────────────────────────────────────
# שלב 7.1 — תיקוני Cursor bot: compare-and-swap + COUNT אמיתי
# ────────────────────────────────────────────────────────────────────


class TestTransitionCustomerFactStatus:
    """Atomic compare-and-swap — מעדכן רק אם status+business_id תואמים."""

    def test_pending_to_active_succeeds(self, db_conn):
        fid = _seed_fact_for_admin(content="x", status="pending_approval")
        from database import transition_customer_fact_status
        assert transition_customer_fact_status(
            fid, "default", "pending_approval", "active",
        ) == 1
        # ה-status באמת השתנה
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "active"

    def test_already_active_returns_zero(self, db_conn):
        """fact במצב active — approve שני לא משנה כלום (race-safe)."""
        fid = _seed_fact_for_admin(content="x", status="active")
        from database import transition_customer_fact_status
        assert transition_customer_fact_status(
            fid, "default", "pending_approval", "active",
        ) == 0
        # ה-status לא השתנה (נשאר active)
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "active"

    def test_wrong_business_id_returns_zero(self, db_conn):
        """fact בעסק אחר — approve לא משפיע (multi-tenant guard)."""
        fid = _seed_fact_for_admin(
            content="x", status="pending_approval", business_id="other",
        )
        from database import transition_customer_fact_status
        assert transition_customer_fact_status(
            fid, "default", "pending_approval", "active",
        ) == 0
        # ה-status לא השתנה
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "pending_approval"

    def test_nonexistent_fact_returns_zero(self, db_conn):
        from database import transition_customer_fact_status
        assert transition_customer_fact_status(
            99999, "default", "pending_approval", "active",
        ) == 0


class TestGetPendingFactsCount:
    def test_counts_only_pending_in_business(self, db_conn):
        _seed_fact_for_admin(content="a", status="active")
        _seed_fact_for_admin(content="p1", status="pending_approval")
        _seed_fact_for_admin(content="p2", status="pending_approval",
                             fact_type="personal_info")
        _seed_fact_for_admin(content="rej", status="rejected",
                             fact_type="vocabulary")
        # pending בעסק אחר — לא נכלל
        _seed_fact_for_admin(content="other", status="pending_approval",
                             business_id="other_biz")
        from database import get_pending_facts_count
        assert get_pending_facts_count("default") == 2
        assert get_pending_facts_count("other_biz") == 1
        assert get_pending_facts_count("none") == 0

    def test_not_capped_at_200(self, db_conn):
        """get_pending_facts חסום ל-200; get_pending_facts_count לא."""
        for i in range(205):
            _seed_fact_for_admin(content=f"p{i}", status="pending_approval",
                                 fact_type="vocabulary")
        from database import get_pending_facts_count, get_pending_facts
        assert get_pending_facts_count() == 205
        # get_pending_facts עדיין מחזיר עד 200
        assert len(get_pending_facts()) == 200


# ────────────────────────────────────────────────────────────────────
# שלב 6 — Helpers ל-background scheduler
# ────────────────────────────────────────────────────────────────────


def _seed_conversation(db_conn, user_id, role, message, created_at=None):
    """seed הודעה ב-conversations. אם created_at לא מועבר → datetime('now')."""
    if created_at:
        db_conn.execute(
            "INSERT INTO conversations (user_id, role, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, role, message, created_at),
        )
    else:
        db_conn.execute(
            "INSERT INTO conversations (user_id, role, message) "
            "VALUES (?, ?, ?)",
            (user_id, role, message),
        )
    db_conn.commit()


class TestGetUsersActiveSince:
    def test_returns_distinct_user_ids(self, db_conn):
        _seed_conversation(db_conn, "u1", "user", "a", "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "assistant", "b", "2026-05-01 10:01:00")
        _seed_conversation(db_conn, "u2", "user", "c", "2026-05-01 12:00:00")
        from database import get_users_active_since
        out = get_users_active_since("2026-04-30 00:00:00")
        assert sorted(out) == ["u1", "u2"]

    def test_filters_by_since(self, db_conn):
        _seed_conversation(db_conn, "old", "user", "x", "2025-01-01 10:00:00")
        _seed_conversation(db_conn, "recent", "user", "y", "2026-05-01 10:00:00")
        from database import get_users_active_since
        assert get_users_active_since("2026-01-01 00:00:00") == ["recent"]

    def test_empty_when_no_users(self, db_conn):
        from database import get_users_active_since
        assert get_users_active_since("2026-01-01 00:00:00") == []


class TestGetConversationAfter:
    """שלב 6.2 — cursor מבוסס id. cap על id, cursor על id, אין same-second."""

    def test_no_after_no_since_returns_all_asc(self, db_conn):
        _seed_conversation(db_conn, "u1", "user", "first", "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "assistant", "second",
                           "2026-05-01 10:01:00")
        from database import get_conversation_after
        out = get_conversation_after("u1", after_id=None, since_iso=None, limit=10)
        assert [m["message"] for m in out] == ["first", "second"]

    def test_after_id_filters_by_id(self, db_conn):
        """cursor הראשי — WHERE id > N. monotonic, יציב."""
        _seed_conversation(db_conn, "u1", "user", "m1", "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "assistant", "m2", "2026-05-01 10:01:00")
        _seed_conversation(db_conn, "u1", "user", "m3", "2026-05-01 10:02:00")
        # נשלוף את ה-id של m1 כדי לקבוע after_id
        row = db_conn.execute(
            "SELECT id FROM conversations WHERE message='m1'"
        ).fetchone()
        first_id = row["id"]
        from database import get_conversation_after
        out = get_conversation_after("u1", after_id=first_id, since_iso=None, limit=10)
        # רק m2 ו-m3 נכללים (id > first_id)
        assert [m["message"] for m in out] == ["m2", "m3"]

    def test_since_iso_fallback_when_no_after_id(self, db_conn):
        """fallback למשתמש חדש בלי last_message_id — since_iso."""
        _seed_conversation(db_conn, "u1", "user", "old",
                           "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "assistant", "new",
                           "2026-05-01 11:00:00")
        from database import get_conversation_after
        out = get_conversation_after(
            "u1", after_id=None, since_iso="2026-05-01 10:30:00", limit=10,
        )
        assert [m["message"] for m in out] == ["new"]

    def test_after_id_takes_priority_over_since_iso(self, db_conn):
        """אם שניהם מועברים — after_id מנצח."""
        _seed_conversation(db_conn, "u1", "user", "m1", "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "user", "m2", "2026-05-01 10:01:00")
        first_id = db_conn.execute(
            "SELECT id FROM conversations WHERE message='m1'"
        ).fetchone()["id"]
        from database import get_conversation_after
        # since_iso רחב, after_id מצומצם — צריך לקבל רק m2
        out = get_conversation_after(
            "u1", after_id=first_id, since_iso="2020-01-01 00:00:00", limit=10,
        )
        assert [m["message"] for m in out] == ["m2"]

    def test_same_second_two_messages_both_processed_across_cycles(self, db_conn):
        """Regression critical: 2 הודעות באותה שנייה, cap=1.
        cycle 1 → msg_a (id קטן יותר, ASC). cursor=id_of_msg_a.
        cycle 2 → msg_b. אף הודעה לא אובדת.

        (לפני שלב 6.2 — cursor timestamp היה מפספס את ה-2nd לעד.
        לפני שלב 6.3 — DESC + LIMIT היה לוקח את msg_b קודם, cursor
        היה MAX → msg_a אובדת.)
        """
        _seed_conversation(db_conn, "u1", "user", "msg_a",
                           "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "user", "msg_b",
                           "2026-05-01 10:00:00")  # אותה שנייה
        from database import get_conversation_after

        # cycle 1: msg_a (ASC — id קטן יותר נטען קודם)
        out1 = get_conversation_after("u1", after_id=None,
                                      since_iso=None, limit=1)
        assert [m["message"] for m in out1] == ["msg_a"]
        cursor = int(out1[-1]["id"])

        # cycle 2: msg_b (id > cursor)
        out2 = get_conversation_after("u1", after_id=cursor,
                                      since_iso=None, limit=10)
        assert [m["message"] for m in out2] == ["msg_b"]


class TestGetLastExtractionMessageId:
    """שלב 6.2 — cursor id-based."""

    def _seed_run(self, db_conn, user_id, biz, last_id, status="completed"):
        db_conn.execute(
            "INSERT INTO extraction_runs (user_id, business_id, "
            "last_message_id, status) VALUES (?, ?, ?, ?)",
            (user_id, biz, last_id, status),
        )
        db_conn.commit()

    def test_returns_max_for_completed(self, db_conn):
        self._seed_run(db_conn, "u1", "default", 100)
        self._seed_run(db_conn, "u1", "default", 200)
        from database import get_last_extraction_message_id
        assert get_last_extraction_message_id("u1", "default") == 200

    def test_excludes_failed_runs(self, db_conn):
        """run שנכשל לא נחשב cursor — נחזור אליו."""
        self._seed_run(db_conn, "u1", "default", 100, status="completed")
        self._seed_run(db_conn, "u1", "default", 200, status="failed")
        from database import get_last_extraction_message_id
        assert get_last_extraction_message_id("u1", "default") == 100

    def test_excludes_null_last_message_id(self, db_conn):
        """runs ישנים (לפני שלב 6.2) עם NULL — לא נחשבים."""
        self._seed_run(db_conn, "u1", "default", None, status="completed")
        self._seed_run(db_conn, "u1", "default", 50, status="completed")
        from database import get_last_extraction_message_id
        assert get_last_extraction_message_id("u1", "default") == 50

    def test_none_when_no_runs(self, db_conn):
        from database import get_last_extraction_message_id
        assert get_last_extraction_message_id("u_no_runs", "default") is None

    def test_filters_by_business_id(self, db_conn):
        self._seed_run(db_conn, "u1", "biz_a", 100)
        self._seed_run(db_conn, "u1", "biz_b", 200)
        from database import get_last_extraction_message_id
        assert get_last_extraction_message_id("u1", "biz_a") == 100
        assert get_last_extraction_message_id("u1", "biz_b") == 200


class TestLogExtractionRunPersistsLastMessageId:
    """שלב 6.2 — log_extraction_run שומר last_message_id (cursor id-based)."""

    def test_persists_provided_id(self, db_conn):
        from database import log_extraction_run
        run_id = log_extraction_run({
            "user_id": "u1", "status": "completed",
            "last_message_id": 12345,
        })
        row = db_conn.execute(
            "SELECT last_message_id FROM extraction_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert row["last_message_id"] == 12345

    def test_none_when_not_provided(self, db_conn):
        """ערכים ישנים שלא העבירו last_message_id — נכתב NULL."""
        from database import log_extraction_run
        run_id = log_extraction_run({
            "user_id": "u1", "status": "completed",
        })
        row = db_conn.execute(
            "SELECT last_message_id FROM extraction_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert row["last_message_id"] is None


class TestGetUserLastMessageTime:
    """שלב 6.3 — idle check מבוסס על MAX(created_at) של כל הודעות
    המשתמש, לא ה-batch הנוכחי. כך גם backlog לא יעובד אם השיחה
    הכוללת עדיין פעילה.
    """

    def test_returns_max_created_at(self, db_conn):
        _seed_conversation(db_conn, "u1", "user", "old",
                           "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u1", "user", "newer",
                           "2026-05-01 11:00:00")
        _seed_conversation(db_conn, "u1", "assistant", "newest",
                           "2026-05-01 12:00:00")
        from database import get_user_last_message_time
        assert get_user_last_message_time("u1") == "2026-05-01 12:00:00"

    def test_none_when_no_messages(self, db_conn):
        from database import get_user_last_message_time
        assert get_user_last_message_time("u_nobody") is None

    def test_isolated_per_user(self, db_conn):
        _seed_conversation(db_conn, "u1", "user", "a",
                           "2026-05-01 10:00:00")
        _seed_conversation(db_conn, "u2", "user", "b",
                           "2026-05-02 10:00:00")
        from database import get_user_last_message_time
        assert get_user_last_message_time("u1") == "2026-05-01 10:00:00"
        assert get_user_last_message_time("u2") == "2026-05-02 10:00:00"


class TestGetConversationAfterAscOrder:
    """שלב 6.3 — ASC + LIMIT (לא DESC + subquery). backlog > cap:
    cycle 1 לוקח את ה-cap ה**ראשונות** (ids נמוכים), שומר MAX, cycle 2
    ממשיך."""

    def test_returns_oldest_first_when_capped(self, db_conn):
        """5 הודעות, cap=3 → מחזיר את 3 הראשונות (m0, m1, m2)."""
        for i in range(5):
            _seed_conversation(db_conn, "u1", "user", f"m{i}",
                               f"2026-05-01 10:0{i}:00")
        from database import get_conversation_after
        out = get_conversation_after("u1", after_id=None,
                                     since_iso=None, limit=3)
        assert [m["message"] for m in out] == ["m0", "m1", "m2"]

    def test_second_cycle_continues_from_cursor(self, db_conn):
        """5 הודעות. cycle 1: cap=3 → m0,m1,m2. cursor=id_of_m2.
        cycle 2: m3,m4. אין הודעות אבודות."""
        ids = []
        for i in range(5):
            cur = db_conn.execute(
                "INSERT INTO conversations (user_id, role, message, "
                "created_at) VALUES (?, ?, ?, ?)",
                ("u1", "user", f"m{i}", f"2026-05-01 10:0{i}:00"),
            )
            ids.append(int(cur.lastrowid))
        db_conn.commit()

        from database import get_conversation_after

        # cycle 1
        out1 = get_conversation_after("u1", after_id=None,
                                      since_iso=None, limit=3)
        assert [m["message"] for m in out1] == ["m0", "m1", "m2"]
        cursor = max(int(m["id"]) for m in out1)

        # cycle 2 — ממשיך מ-cursor
        out2 = get_conversation_after("u1", after_id=cursor,
                                      since_iso=None, limit=3)
        assert [m["message"] for m in out2] == ["m3", "m4"]


class TestGetUsersWithPendingMessages:
    """שלב 6.4 — discovery עם UNION של backlog + new users.
    מחליף את get_users_active_since בbackground.py.
    """

    def _seed_msg(self, db_conn, user_id, ts="2026-05-01 10:00:00"):
        """seed הודעה אחת. מחזיר id."""
        cur = db_conn.execute(
            "INSERT INTO conversations (user_id, role, message, created_at) "
            "VALUES (?, 'user', 'x', ?)",
            (user_id, ts),
        )
        db_conn.commit()
        return int(cur.lastrowid)

    def _seed_run(self, db_conn, user_id, business_id, last_id,
                  status="completed"):
        db_conn.execute(
            "INSERT INTO extraction_runs (user_id, business_id, "
            "last_message_id, status) VALUES (?, ?, ?, ?)",
            (user_id, business_id, last_id, status),
        )
        db_conn.commit()

    def test_user_with_backlog_after_lookback_still_included(self, db_conn):
        """80 הודעות לפני 10 ימים, run אחד מ-id=מסוים → backlog → מוחזר.
        Regression critical של שלב 6.4."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        ids = [self._seed_msg(db_conn, "u_backlog", old_ts) for _ in range(80)]
        # run עיבד את ה-50 הראשונים
        self._seed_run(db_conn, "u_backlog", "default", ids[49])

        from database import get_users_with_pending_messages
        out = get_users_with_pending_messages("default", lookback_days=7)
        assert "u_backlog" in out  # backlog: ids[50..79] > last_id

    def test_new_user_in_lookback_included(self, db_conn):
        """משתמש בלי run, הודעה לפני 5 ימים → בתוך lookback → מוחזר."""
        from datetime import datetime, timedelta, timezone
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)
                     ).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_msg(db_conn, "u_new", recent_ts)
        from database import get_users_with_pending_messages
        assert "u_new" in get_users_with_pending_messages("default", 7)

    def test_new_user_outside_lookback_excluded(self, db_conn):
        """משתמש בלי run, הודעה לפני 10 ימים → מחוץ ל-lookback → לא מוחזר."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_msg(db_conn, "u_ancient", old_ts)
        from database import get_users_with_pending_messages
        assert "u_ancient" not in get_users_with_pending_messages("default", 7)

    def test_user_fully_processed_excluded(self, db_conn):
        """5 הודעות, run עם last_message_id = MAX(ids) → אין backlog, לא חדש."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        ids = [self._seed_msg(db_conn, "u_done", old_ts) for _ in range(5)]
        self._seed_run(db_conn, "u_done", "default", max(ids))
        from database import get_users_with_pending_messages
        assert "u_done" not in get_users_with_pending_messages("default", 7)

    def test_failed_run_does_not_count_as_completed(self, db_conn):
        """run יחיד status='failed' (גם עם last_message_id) → המשתמש
        נחשב כ"אין run", lookback חל. הודעה ישנה → לא מוחזר."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        mid = self._seed_msg(db_conn, "u_failed_only", old_ts)
        self._seed_run(db_conn, "u_failed_only", "default", mid,
                       status="failed")
        from database import get_users_with_pending_messages
        # אין completed → נחשב חדש → lookback חל → הודעה ישנה → לא מוחזר
        assert "u_failed_only" not in get_users_with_pending_messages(
            "default", 7,
        )

    def test_business_id_isolation(self, db_conn):
        """backlog בעסק אחר לא משפיע — extraction_runs מסונן ל-business_id
        של הקריאה."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        ids = [self._seed_msg(db_conn, "u_cross", old_ts) for _ in range(5)]
        # run בעסק "other" — לא רלוונטי ל-"default"
        self._seed_run(db_conn, "u_cross", "other", max(ids))
        from database import get_users_with_pending_messages
        # מ-עיני "default": אין run בכלל → נחשב חדש → lookback חל →
        # הודעות ישנות (10 ימים) → לא מוחזר. (הודעות שלו מ-2 עסקים?
        # ב-single-tenant לא רלוונטי, אבל הפילטר על er.business_id
        # מוודא שגם אם היה — אין דליפה.)
        assert "u_cross" not in get_users_with_pending_messages("default", 7)

    def test_returns_distinct_user_ids(self, db_conn):
        """כפילות לא רצויה — DISTINCT."""
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=2)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        # 3 הודעות לאותו user
        for _ in range(3):
            self._seed_msg(db_conn, "u_multi", recent)
        from database import get_users_with_pending_messages
        out = get_users_with_pending_messages("default", 7)
        assert out.count("u_multi") == 1


# ════════════════════════════════════════════════════════════════
# שלב 6 — Migration safety net (מונע חזרה של באג 7)
# ════════════════════════════════════════════════════════════════


class TestExtractionRunsMigration:
    """מדמה DB ישן (לפני שלב 6) שמכיל extraction_runs **בלי**
    last_message_id. מאמת ש-run_migrations מוסיף את העמודה ואת
    האינדקס בלי לקרוס.

    זה הטסט שהיה תופס את באג 7 (קריסת init_db בפרודקשן):
    לפני התיקון, ה-CREATE INDEX היה ב-init_db's executescript ותלוי
    בעמודה שעוד לא נוספה — קרס על DB קיים. אחרי התיקון, ה-CREATE
    INDEX נמצא רק ב-migrations.py, אחרי ה-_ensure_column.
    """

    @pytest.fixture
    def old_extraction_runs(self, db_conn):
        """ממליץ את extraction_runs לסכמה הישנה של שלב 5 — בלי
        last_message_id ובלי האינדקס שתלוי בה. מדמה את המצב המדויק
        של פרודקשן ערב upgrade ל-שלב 6.

        החשיבות: אנחנו לא מוחקים ויוצרים מחדש את הטבלה רק כדי לאמת
        את ה-migration; אנחנו מדמים את ה-DB **כפי שהוא בפרודקשן**.
        """
        # מבטלים את האינדקס החדש (אם init_db יצר אותו) ויוצרים מחדש
        # את הטבלה בסכמה הישנה
        db_conn.execute("DROP INDEX IF EXISTS idx_extraction_runs_user_msg_id")
        db_conn.execute("DROP TABLE IF EXISTS extraction_runs")
        db_conn.execute("""
            CREATE TABLE extraction_runs (
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
                created_at              TEXT DEFAULT (datetime('now'))
            )
        """)
        # שורה ישנה (נתונים קיימים בפרודקשן)
        db_conn.execute(
            "INSERT INTO extraction_runs (user_id, business_id, status, "
            "conversation_end, messages_count, extractions_count) "
            "VALUES ('u_legacy', 'default', 'completed', "
            "'2026-05-01 10:00:00', 5, 2)"
        )
        db_conn.commit()
        return db_conn

    def test_migration_adds_last_message_id_column(self, old_extraction_runs):
        """אחרי run_migrations — last_message_id קיימת."""
        from migrations import run_migrations
        run_migrations(old_extraction_runs)
        cols = {
            r["name"] for r in
            old_extraction_runs.execute("PRAGMA table_info(extraction_runs)")
        }
        assert "last_message_id" in cols

    def test_migration_creates_index_after_column(self, old_extraction_runs):
        """ה-CREATE INDEX רץ *אחרי* ה-ADD COLUMN, לא קורס."""
        from migrations import run_migrations
        run_migrations(old_extraction_runs)
        idx = old_extraction_runs.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_extraction_runs_user_msg_id'"
        ).fetchone()
        assert idx is not None

    def test_migration_preserves_legacy_rows(self, old_extraction_runs):
        """שורות ישנות נשמרות; last_message_id = NULL (NOT NULL לא מוטל)."""
        from migrations import run_migrations
        run_migrations(old_extraction_runs)
        row = old_extraction_runs.execute(
            "SELECT user_id, last_message_id, messages_count "
            "FROM extraction_runs WHERE user_id='u_legacy'"
        ).fetchone()
        assert row["user_id"] == "u_legacy"
        assert row["last_message_id"] is None  # NULL ל-runs ישנים — תקין
        assert row["messages_count"] == 5  # נתון ישן נשמר

    def test_migration_idempotent(self, old_extraction_runs):
        """run_migrations רץ פעמיים → לא קורס. _ensure_column בודק PRAGMA
        ודולג; CREATE INDEX IF NOT EXISTS — no-op."""
        from migrations import run_migrations
        run_migrations(old_extraction_runs)
        run_migrations(old_extraction_runs)  # שוב — לא קורס
        cols = {
            r["name"] for r in
            old_extraction_runs.execute("PRAGMA table_info(extraction_runs)")
        }
        assert "last_message_id" in cols

    def test_init_db_on_existing_db_without_column_does_not_crash(
        self, db_conn, tmp_path, monkeypatch,
    ):
        """E2E: מדמה במדויק את הקריסה בפרודקשן —
        DB קיים עם extraction_runs ישנה. init_db נקרא (כפי שקורה
        בכל startup ב-Render). חייב לעבור בלי OperationalError.

        זה הטסט שהיה תופס את באג 7 לפני פרודקשן. הוא לא משתמש
        ב-db_conn שכבר אותחל; הוא יוצר DB חדש ומדמה את המצב.
        """
        import sqlite3
        # יוצרים DB ידני בסכמה ישנה (כמו פרודקשן ערב upgrade)
        legacy_db = str(tmp_path / "legacy_prod.db")
        c = sqlite3.connect(legacy_db)
        c.executescript("""
            CREATE TABLE extraction_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                business_id TEXT NOT NULL DEFAULT 'default',
                conversation_start TEXT,
                conversation_end TEXT,
                messages_count INTEGER DEFAULT 0,
                extractions_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'completed'
                       CHECK(status IN ('running','completed','failed')),
                error_message TEXT DEFAULT '',
                tokens_used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO extraction_runs (user_id, status)
            VALUES ('legacy_user', 'completed');
        """)
        c.commit()
        c.close()

        # מפנים את database.DB_PATH ל-DB הישן ומריצים init_db
        import database
        import ai_chatbot.config
        monkeypatch.setattr(database, "DB_PATH", legacy_db)
        monkeypatch.setattr(ai_chatbot.config, "DB_PATH", legacy_db)

        # זה היה זורק sqlite3.OperationalError: no such column: last_message_id
        # לפני התיקון. אחרי התיקון — עובר חלק.
        database.init_db()

        # אימותים סופיים
        c = sqlite3.connect(legacy_db)
        cols = [r[1] for r in c.execute("PRAGMA table_info(extraction_runs)")]
        assert "last_message_id" in cols, (
            f"last_message_id missing after init_db — migration didn't run! "
            f"cols={cols}"
        )
        idx = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name='idx_extraction_runs_user_msg_id'"
        ).fetchone()
        assert idx is not None, "Index missing after init_db"
        # השורה הישנה נשמרת
        row = c.execute(
            "SELECT user_id, last_message_id FROM extraction_runs "
            "WHERE user_id='legacy_user'"
        ).fetchone()
        assert row == ("legacy_user", None)
        c.close()
