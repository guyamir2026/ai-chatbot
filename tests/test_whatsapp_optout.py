"""
טסטים ל-messaging/whatsapp_optout.py + DB helpers של opt-in/out +
count_wa_audience (אכיפת תיקון 40).
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── detect_optout / detect_optin ─────────────────────────────────────────────


class TestOptoutDetection:
    def test_hebrew_keywords(self):
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("הסר") is True
        assert detect_optout("הסרה") is True
        assert detect_optout("להסיר") is True
        assert detect_optout("הפסק") is True
        assert detect_optout("אל תשלחו לי") is True

    def test_english_keywords(self):
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("STOP") is True
        assert detect_optout("stop") is True
        assert detect_optout("unsubscribe") is True
        assert detect_optout("OPT-OUT") is True

    def test_with_punctuation(self):
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("הסר!") is True
        assert detect_optout("הסר.") is True
        assert detect_optout("  STOP.  ") is True

    def test_leading_punctuation(self):
        """Regression: פיסוק בתחילת ההודעה לא אמור למנוע זיהוי opt-out
        (למשל "!הסר" מהדגשה, או "...הסר")."""
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("!הסר") is True
        assert detect_optout("!stop") is True
        assert detect_optout("...הסר") is True

    def test_quoted_keyword(self):
        """Regression: משתמשים לעיתים מצטטים את המילה בבקשה חוזרת."""
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout('"הסר"') is True
        assert detect_optout("'STOP'") is True

    def test_not_optout_in_long_sentence(self):
        """Regression: משפט שמכיל "להסיר" כחלק ממנו אינו opt-out.

        חשוב למנוע false-positives — מי שכותב "אני רוצה להסיר את התור"
        לא ביקש opt-out מהשיווק.
        """
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("אני רוצה להסיר את התור") is False
        assert detect_optout("תודה על ההודעה, הסר כבר לא בא לי") is False
        assert detect_optout("please stop the appointment") is False

    def test_empty_and_none(self):
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("") is False
        assert detect_optout(None) is False

    def test_booking_flow_keywords_not_optout(self):
        """Regression: מילים נפוצות בזרימות אחרות לא נחשבות ל-opt-out."""
        from messaging.whatsapp_optout import detect_optout
        assert detect_optout("ביטול") is False  # ביטול תור, לא opt-out
        assert detect_optout("עצור") is False  # עצור לא ברשימה בכוונה
        assert detect_optout("מספיק") is False


class TestOptinDetection:
    def test_hebrew_keywords(self):
        from messaging.whatsapp_optout import detect_optin
        assert detect_optin("הסכמה") is True
        assert detect_optin("אני מסכים") is True

    def test_english_keywords(self):
        from messaging.whatsapp_optout import detect_optin
        assert detect_optin("subscribe") is True
        assert detect_optin("START") is True
        assert detect_optin("opt-in") is True


# ── DB helpers: opt-in/out ───────────────────────────────────────────────────


def _insert_wa_user(db, user_id: str):
    """עזר: רישום משתמש WhatsApp בטבלת users."""
    db.upsert_user(user_id, username=user_id, channel="whatsapp")


class TestOptStatus:
    def test_new_user_not_opted_in(self, db):
        _insert_wa_user(db, "+972501234567")
        status = db.get_wa_opt_status("+972501234567")
        assert status["opted_in"] is False
        assert status["opted_out_at"] is None
        assert status["eligible_for_marketing"] is False

    def test_opt_in_sets_flag_and_source(self, db):
        _insert_wa_user(db, "+972501234567")
        db.set_wa_marketing_opt_in("+972501234567", source="bot_button")
        status = db.get_wa_opt_status("+972501234567")
        assert status["opted_in"] is True
        assert status["opted_in_source"] == "bot_button"
        assert status["opted_in_at"] is not None
        assert status["eligible_for_marketing"] is True

    def test_opt_out_clears_eligibility(self, db):
        _insert_wa_user(db, "+972501234567")
        db.set_wa_marketing_opt_in("+972501234567", source="bot_button")
        db.set_wa_opted_out("+972501234567")
        status = db.get_wa_opt_status("+972501234567")
        assert status["opted_out_at"] is not None
        assert status["eligible_for_marketing"] is False

    def test_opt_in_after_opt_out_restores_eligibility(self, db):
        """עלול לקרות אחרי שלקוח ענה 'הסכמה' חזרה."""
        _insert_wa_user(db, "+972501234567")
        db.set_wa_opted_out("+972501234567")
        db.set_wa_marketing_opt_in("+972501234567", source="bot_reply")
        status = db.get_wa_opt_status("+972501234567")
        assert status["opted_in"] is True
        assert status["opted_out_at"] is None
        assert status["eligible_for_marketing"] is True

    def test_is_eligible_shortcut(self, db):
        _insert_wa_user(db, "+972501234567")
        assert db.is_wa_eligible_for_marketing("+972501234567") is False
        db.set_wa_marketing_opt_in("+972501234567", source="x")
        assert db.is_wa_eligible_for_marketing("+972501234567") is True

    def test_missing_user_returns_false(self, db):
        status = db.get_wa_opt_status("+972999999999")
        assert status["opted_in"] is False
        assert status["eligible_for_marketing"] is False

    def test_raises_on_empty_user_id(self, db):
        with pytest.raises(ValueError):
            db.set_wa_marketing_opt_in("", source="x")
        with pytest.raises(ValueError):
            db.set_wa_opted_out("")


# ── count_wa_audience — אכיפת תיקון 40 ───────────────────────────────────────


class TestAudienceCounts:
    def _setup_mixed_users(self, db):
        """3 משתמשי WA: אחד opted-in, אחד opted-out, אחד שלא בחר."""
        _insert_wa_user(db, "+972501000001")  # opted-in
        _insert_wa_user(db, "+972501000002")  # opted-out
        _insert_wa_user(db, "+972501000003")  # neutral (כברירת מחדל)

        db.set_wa_marketing_opt_in("+972501000001", source="test")
        db.set_wa_opted_out("+972501000002")

    def test_marketing_requires_opt_in(self, db):
        """עבור MARKETING — רק opted-in נספרים ל-eligible."""
        self._setup_mixed_users(db)
        counts = db.count_wa_audience(category="MARKETING")
        assert counts["total_wa_users"] == 3
        assert counts["eligible"] == 1
        assert counts["filtered_out_opted_out"] == 1
        assert counts["filtered_out_never_opted_in"] == 1

    def test_utility_excludes_only_opted_out(self, db):
        """UTILITY: מותר לשלוח לכל מי שלא opted-out, גם ללא opt-in מפורש."""
        self._setup_mixed_users(db)
        counts = db.count_wa_audience(category="UTILITY")
        assert counts["eligible"] == 2  # opt-in + neutral
        assert counts["filtered_out_opted_out"] == 1
        # UTILITY לא סופר never_opted_in כ-filter (לא רלוונטי)
        assert counts["filtered_out_never_opted_in"] == 0

    def test_authentication_same_as_utility(self, db):
        """AUTHENTICATION מתנהג כמו UTILITY (OTP לא דורש opt-in שיווקי)."""
        self._setup_mixed_users(db)
        counts = db.count_wa_audience(category="AUTHENTICATION")
        assert counts["eligible"] == 2

    def test_inactive_days_filter(self, db):
        """סינון לפי פעילות — משתמש ישן מחוץ לטווח."""
        _insert_wa_user(db, "+972501000001")
        _insert_wa_user(db, "+972501000002")
        db.set_wa_marketing_opt_in("+972501000001", source="test")
        db.set_wa_marketing_opt_in("+972501000002", source="test")

        # מעדכנים ידנית last_active_at ל-"+972501000002" שיהיה בעבר הרחוק
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_active_at = datetime('now', '-180 days') "
                "WHERE user_id = ?",
                ("+972501000002",),
            )

        counts = db.count_wa_audience(category="MARKETING", inactive_days=30)
        assert counts["eligible"] == 1
        assert counts["filtered_out_inactive"] == 1

    def test_no_inactive_filter_counts_everyone(self, db):
        """inactive_days=None — כולם נכללים ללא קשר לפעילות."""
        _insert_wa_user(db, "+972501000001")
        _insert_wa_user(db, "+972501000002")
        db.set_wa_marketing_opt_in("+972501000001", source="test")
        db.set_wa_marketing_opt_in("+972501000002", source="test")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_active_at = datetime('now', '-180 days') "
                "WHERE user_id = ?",
                ("+972501000002",),
            )
        counts = db.count_wa_audience(category="MARKETING", inactive_days=None)
        assert counts["eligible"] == 2

    def test_telegram_users_not_counted(self, db):
        """משתמשי טלגרם לא נכללים ב-WhatsApp audience."""
        db.upsert_user("tg_user_1", username="tg_user_1", channel="telegram")
        counts = db.count_wa_audience(category="UTILITY")
        assert counts["total_wa_users"] == 0

    def test_list_eligible_user_ids(self, db):
        self._setup_mixed_users(db)
        ids = db.list_wa_audience_eligible_user_ids(category="MARKETING")
        assert ids == ["+972501000001"]

        ids_utility = sorted(db.list_wa_audience_eligible_user_ids(category="UTILITY"))
        assert ids_utility == sorted(["+972501000001", "+972501000003"])

    def test_empty_db_returns_zero_counts(self, db):
        counts = db.count_wa_audience(category="MARKETING")
        assert counts["total_wa_users"] == 0
        assert counts["eligible"] == 0
