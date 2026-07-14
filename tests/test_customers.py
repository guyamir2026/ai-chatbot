"""
טסטים ל-CRM-lite (database.list_customers / get_customer_card / _compute_auto_tags).
"""

from datetime import date, timedelta

import pytest


@pytest.fixture
def db_module(db_conn):
    """גישה למודול database עם DB מאותחל."""
    import database as db
    return db


# ─── _compute_auto_tags ──────────────────────────────────────────────────
class TestAutoTags:
    def test_no_appointments_no_tags(self, db_module):
        assert db_module._compute_auto_tags(0, None) == []

    def test_first_visit_marks_new(self, db_module):
        assert "חדש" in db_module._compute_auto_tags(1, date.today().isoformat())

    def test_two_visits_marks_returning(self, db_module):
        tags = db_module._compute_auto_tags(2, date.today().isoformat())
        assert "חוזר" in tags
        assert "חדש" not in tags

    def test_ten_visits_marks_vip(self, db_module):
        tags = db_module._compute_auto_tags(10, date.today().isoformat())
        assert "VIP" in tags
        assert "חוזר" not in tags  # VIP גובר על חוזר

    def test_old_visit_marks_dormant(self, db_module):
        old = (date.today() - timedelta(days=90)).isoformat()
        tags = db_module._compute_auto_tags(3, old)
        assert "רדום" in tags
        assert "חוזר" in tags  # אפשר להיות גם רדום וגם חוזר

    def test_recent_visit_not_dormant(self, db_module):
        recent = (date.today() - timedelta(days=10)).isoformat()
        tags = db_module._compute_auto_tags(3, recent)
        assert "רדום" not in tags

    def test_invalid_date_handled_gracefully(self, db_module):
        # תאריך לא חוקי לא יזרוק exception
        tags = db_module._compute_auto_tags(1, "not-a-date")
        assert "חדש" in tags  # התגיות הלא-תלויות בתאריך עדיין עובדות


# ─── list_customers + count_customers ────────────────────────────────────
class TestListCustomers:
    def _seed_user(self, db_module, user_id, username, channel="telegram"):
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username, channel) VALUES (?, ?, ?)",
                (user_id, username, channel),
            )

    def _seed_appointment(
        self, db_module, user_id, date_str, status="confirmed", time_str="10:00",
    ):
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, "
                "preferred_time, status) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, "x", "תספורת", date_str, time_str, status),
            )

    def test_basic_listing(self, db_module):
        self._seed_user(db_module, "list_basic_100", "Alice_basic")
        self._seed_user(db_module, "list_basic_200", "Bob_basic")
        results = db_module.list_customers(search="basic")
        names = {r["username"] for r in results}
        assert "Alice_basic" in names
        assert "Bob_basic" in names

    def test_search_by_username(self, db_module):
        self._seed_user(db_module, "list_search_100", "Charlie_search")
        self._seed_user(db_module, "list_search_200", "Dana_search")
        results = db_module.list_customers(search="Charlie_search")
        assert len(results) == 1
        assert results[0]["username"] == "Charlie_search"

    def test_appt_count_aggregated(self, db_module):
        uid = "list_appt_100"
        self._seed_user(db_module, uid, "Eve_appt")
        self._seed_appointment(db_module, uid, "2026-01-01", "confirmed", "09:00")
        self._seed_appointment(db_module, uid, "2026-02-01", "passed", "10:00")
        self._seed_appointment(db_module, uid, "2026-03-01", "cancelled", "11:00")
        self._seed_appointment(db_module, uid, "2026-04-01", "pending", "12:00")
        results = db_module.list_customers(search="Eve_appt")
        e = next(r for r in results if r["user_id"] == uid)
        # רק confirmed + passed נספרים (ביקורים שקרו / יקרו). pending עדיין
        # לא ביקור, cancelled בוטל. עקבי עם get_customer_card.appt_count_confirmed.
        assert e["appt_count"] == 2

    def test_list_and_card_agree_on_tags(self, db_module):
        """באג קודם: list ספר pending → "חוזר", card לא → "חדש". עכשיו עקבי."""
        uid = "consistency_test_uid"
        self._seed_user(db_module, uid, "Consistency_user")
        self._seed_appointment(db_module, uid, "2026-04-01", "confirmed", "09:00")
        self._seed_appointment(db_module, uid, "2026-04-02", "pending", "10:00")

        list_row = next(
            r for r in db_module.list_customers(search="Consistency_user")
            if r["user_id"] == uid
        )
        card = db_module.get_customer_card(uid)

        assert list_row["auto_tags"] == card["auto_tags"]
        # ביקור confirmed יחיד ⇒ "חדש" בשני המקומות (pending לא נספר)
        assert "חדש" in list_row["auto_tags"]

    def test_auto_tags_computed_per_customer(self, db_module):
        # לקוח חדש (תור 1) — תאריך עתידי שלא יסומן רדום
        new_uid = "list_tags_new"
        vip_uid = "list_tags_vip"
        self._seed_user(db_module, new_uid, "NewCustomer_tags")
        self._seed_appointment(
            db_module, new_uid, date.today().isoformat(), "confirmed", "09:00",
        )
        # VIP (10 תורים) — שעות שונות כדי לא לפגוע ב-UNIQUE constraint
        self._seed_user(db_module, vip_uid, "VipCustomer_tags")
        for i in range(10):
            self._seed_appointment(
                db_module, vip_uid, "2026-01-01", "confirmed", f"{8 + i:02d}:00",
            )

        results = db_module.list_customers(search="tags")
        new_c = next(r for r in results if r["user_id"] == new_uid)
        vip = next(r for r in results if r["user_id"] == vip_uid)
        assert "חדש" in new_c["auto_tags"]
        assert "VIP" in vip["auto_tags"]


# ─── get_customer_card ───────────────────────────────────────────────────
class TestCustomerCard:
    def test_unknown_user_returns_none(self, db_module):
        assert db_module.get_customer_card("nobody_xxx") is None

    def test_full_card(self, db_module):
        uid = "card_test_alice"
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username, channel) VALUES (?, ?, ?)",
                (uid, "Alice_card", "telegram"),
            )
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, "
                "preferred_time, status) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, "Alice_card", "תספורת", "2026-04-01", "10:00", "confirmed"),
            )
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, "
                "preferred_time, status) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, "Alice_card", "צביעה", "2026-05-01", "11:00", "passed"),
            )

        db_module.save_user_note(uid, "VIP — מעדיף בוקר", tags=["מעדיף בוקר"])

        card = db_module.get_customer_card(uid)
        assert card is not None
        assert card["display_name"] == "Alice_card"
        assert card["channel"] == "telegram"
        assert len(card["appointments"]) == 2
        assert card["appt_count_confirmed"] == 2
        assert card["note"] == "VIP — מעדיף בוקר"
        assert "מעדיף בוקר" in card["manual_tags"]
        services = {s["service"]: s["c"] for s in card["services_summary"]}
        assert services == {"תספורת": 1, "צביעה": 1}


# ─── Regression: handler לא דורס withhold_reason ──────────────────────────
# Bugbot זיהה: customer_card_save_note היה קורא ל-save_user_note בלי withhold_reason,
# וברירת המחדל "" של הפונקציה דרסה את הערך הקיים. תיקון: ה-handler קורא קודם
# get_user_note_full ומעביר את ה-withhold_reason הקיים. הטסט מוודא שהדפוס הזה
# (read-then-write) משמר את הערך.
class TestHandlerPreservesWithholdReason:
    def test_save_with_withhold_then_update_preserves(self, db_module):
        uid = "withhold_preserve_test"
        # רושמים hold_reason ב-DB ישירות
        db_module.save_user_note(
            uid, "פתק רגיש", tags=["legal"],
            withhold_reason="בקשה משפטית",
        )

        # מדמים מה ש-handler עושה: קורא את הקיים, מעביר אותו חזרה
        existing = db_module.get_user_note_full(uid)
        assert existing["withhold_reason"] == "בקשה משפטית"  # sanity

        db_module.save_user_note(
            uid, "פתק מעודכן", tags=["legal", "updated"],
            withhold_reason=existing["withhold_reason"],  # ⇐ הפתרון לבאג
        )

        after = db_module.get_user_note_full(uid)
        assert after["note"] == "פתק מעודכן"
        assert "updated" in after["tags"]
        # החשוב — withhold_reason לא נדרס:
        assert after["withhold_reason"] == "בקשה משפטית"

    def test_save_without_withhold_overrides_to_empty(self, db_module):
        """תיעוד התנהגות ה-API: אם לא מעבירים withhold_reason — הוא נדרס.
        זה הבאג שתוקן בנקודת הקריאה (handler), לא ב-API עצמו."""
        uid = "withhold_overwrite_test"
        db_module.save_user_note(uid, "פתק", withhold_reason="חסוי")
        # קריאה בלי withhold_reason — דורסת ל-""
        db_module.save_user_note(uid, "פתק חדש", tags=["x"])
        assert db_module.get_user_note_full(uid)["withhold_reason"] == ""


# ─── user_exists ─────────────────────────────────────────────────────────
class TestUserExists:
    def test_returns_true_for_existing_user(self, db_module):
        uid = "exists_test_yes"
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                (uid, "Exists"),
            )
        assert db_module.user_exists(uid) is True

    def test_returns_false_for_missing_user(self, db_module):
        assert db_module.user_exists("definitely_not_a_real_user_zzz") is False


# ─── _customers_where_clause (helper משותף) ──────────────────────────────
class TestCustomersWhereClause:
    def test_no_search_returns_baseline(self, db_module):
        sql, params = db_module._customers_where_clause("")
        assert "u.user_id IS NOT NULL" in sql
        assert params == []

    def test_search_adds_like_filter(self, db_module):
        sql, params = db_module._customers_where_clause("alice")
        assert "LIKE ?" in sql
        # שלוש פעמים LIKE — username, user_id, phone_number
        assert sql.count("LIKE ?") == 3
        assert params == ["%alice%", "%alice%", "%alice%"]

    def test_list_and_count_consistent(self, db_module):
        """אסמכתא לבאג שתוקן: אם list ו-count לא משתמשים באותו WHERE,
        ה-pagination נשבר. הטסט מאמת התאמה אחרי seed."""
        for i in range(5):
            uid = f"consistency_where_{i}"
            with db_module.get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                    (uid, f"WhereTest_{i}"),
                )

        rows = db_module.list_customers(search="WhereTest_", limit=100)
        count = db_module.count_customers(search="WhereTest_")
        assert len(rows) == count == 5

    def test_dual_channel_user_not_duplicated(self, db_module):
        """Bugbot regression: לקוח עם זהויות בשני ערוצים (telegram + whatsapp)
        לא צריך להופיע פעמיים ברשימה ולא לנפח את ה-count.
        הבעיה הקודמת: LEFT JOIN על user_identities הכפיל שורות.
        """
        uid = "dual_channel_user_zzz"
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                (uid, "DualChannelUser"),
            )
            # שתי זהויות: telegram + whatsapp (UNIQUE(channel, user_id) מאפשר זאת)
            conn.execute(
                "INSERT OR REPLACE INTO user_identities (user_id, channel, phone_number) "
                "VALUES (?, ?, ?)",
                (uid, "telegram", "0501111111"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO user_identities (user_id, channel, phone_number) "
                "VALUES (?, ?, ?)",
                (uid, "whatsapp", "0502222222"),
            )

        rows = db_module.list_customers(search="DualChannelUser")
        count = db_module.count_customers(search="DualChannelUser")
        assert len(rows) == 1
        assert count == 1
        # החיפוש על טלפון עדיין עובד גם עם EXISTS
        rows_phone = db_module.list_customers(search="0502222222")
        assert any(r["user_id"] == uid for r in rows_phone)
