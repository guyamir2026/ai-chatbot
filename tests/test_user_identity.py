"""
טסטים למודול utils/user_identity.py — שכבת resolution ל-BSUID / מספר טלפון.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def identity_db(tmp_path):
    """מאתחל DB זמני עם סכימה מלאה ומחזיר connection helper."""
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    with patch("ai_chatbot.config.DB_PATH", tmp_path / "test.db"):
        from database import init_db, get_connection
        init_db()
        yield get_connection


class TestResolveWhatsappUser:
    """בדיקות ל-resolve_whatsapp_user — לוגיקת תרגום מספר/BSUID ל-user_id."""

    def test_new_user_phone_only(self, identity_db):
        """משתמש חדש עם מספר טלפון בלבד — user_id = מספר הטלפון."""
        from utils.user_identity import resolve_whatsapp_user

        user_id = resolve_whatsapp_user("+972501234567")

        assert user_id == "+972501234567"
        # ווידוא שנשמרה רשומה בטבלת user_identities
        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("+972501234567",)
            ).fetchone()
            assert row is not None
            assert row["channel"] == "whatsapp"
            assert row["phone_number"] == "+972501234567"
            assert row["whatsapp_bsuid"] is None

    def test_returning_user_phone_only(self, identity_db):
        """משתמש חוזר — מזוהה לפי מספר הטלפון, מחזיר אותו user_id."""
        from utils.user_identity import resolve_whatsapp_user

        first = resolve_whatsapp_user("+972501234567")
        second = resolve_whatsapp_user("+972501234567")

        assert first == second == "+972501234567"

    def test_new_user_with_bsuid_and_phone(self, identity_db):
        """משתמש חדש עם BSUID + טלפון — user_id = מספר הטלפון (תאימות לאחור)."""
        from utils.user_identity import resolve_whatsapp_user

        user_id = resolve_whatsapp_user(
            "+972501234567",
            bsuid="IL.ABCdef123",
        )

        assert user_id == "+972501234567"
        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("+972501234567",)
            ).fetchone()
            assert row["whatsapp_bsuid"] == "IL.ABCdef123"
            assert row["phone_number"] == "+972501234567"

    def test_gradual_migration_adds_bsuid(self, identity_db):
        """מיגרציה הדרגתית — משתמש ותיק (טלפון בלבד) שולח עכשיו עם BSUID."""
        from utils.user_identity import resolve_whatsapp_user

        # פעם ראשונה — ללא BSUID
        first = resolve_whatsapp_user("+972501234567")
        assert first == "+972501234567"

        # פעם שנייה — עם BSUID
        second = resolve_whatsapp_user(
            "+972501234567",
            bsuid="IL.XYZ789abc",
        )

        # user_id נשאר אותו דבר — ההיסטוריה נשמרת
        assert second == "+972501234567"
        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("+972501234567",)
            ).fetchone()
            assert row["whatsapp_bsuid"] == "IL.XYZ789abc"

    def test_bsuid_lookup_returns_existing_user(self, identity_db):
        """חיפוש לפי BSUID — כשכבר יש רשומה, מחזיר את ה-user_id הקיים."""
        from utils.user_identity import resolve_whatsapp_user

        # יצירת משתמש עם BSUID
        resolve_whatsapp_user("+972501234567", bsuid="IL.ABCdef123")

        # הודעה חדשה עם אותו BSUID אבל ללא טלפון
        user_id = resolve_whatsapp_user("", bsuid="IL.ABCdef123")

        assert user_id == "+972501234567"

    def test_bsuid_only_no_phone(self, identity_db):
        """משתמש חדש עם BSUID בלבד (ללא טלפון) — user_id = BSUID."""
        from utils.user_identity import resolve_whatsapp_user

        user_id = resolve_whatsapp_user("", bsuid="IL.NoPhoneUser99")

        assert user_id == "IL.NoPhoneUser99"
        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("IL.NoPhoneUser99",)
            ).fetchone()
            assert row is not None
            assert row["whatsapp_bsuid"] == "IL.NoPhoneUser99"
            assert row["phone_number"] is None

    def test_username_saved(self, identity_db):
        """שם משתמש WhatsApp נשמר ברשומת הזהות."""
        from utils.user_identity import resolve_whatsapp_user

        resolve_whatsapp_user(
            "+972501234567",
            wa_username="dana_beauty",
        )

        with identity_db() as conn:
            row = conn.execute(
                "SELECT username FROM user_identities WHERE user_id = ?",
                ("+972501234567",),
            ).fetchone()
            assert row["username"] == "dana_beauty"

    def test_username_updated_on_return(self, identity_db):
        """שם משתמש מתעדכן כשמשתמש חוזר עם שם חדש."""
        from utils.user_identity import resolve_whatsapp_user

        resolve_whatsapp_user("+972501234567", wa_username="old_name")
        resolve_whatsapp_user("+972501234567", wa_username="new_name")

        with identity_db() as conn:
            row = conn.execute(
                "SELECT username FROM user_identities WHERE user_id = ?",
                ("+972501234567",),
            ).fetchone()
            assert row["username"] == "new_name"

    def test_no_phone_no_bsuid_raises(self, identity_db):
        """ללא טלפון וללא BSUID — שגיאה."""
        from utils.user_identity import resolve_whatsapp_user

        with pytest.raises(ValueError, match="phone_number.*bsuid"):
            resolve_whatsapp_user("")

    def test_resolve_with_parent_bsuid_stores_correctly(self, identity_db):
        """parent_bsuid נשמר ברשומת user_identities בכל אחד משלושת המסלולים."""
        from utils.user_identity import resolve_whatsapp_user

        # מסלול 3 — משתמש חדש
        user_id = resolve_whatsapp_user(
            "+972501111111",
            bsuid="IL.ChildA1",
            parent_bsuid="IL.ParentRoot",
        )
        assert user_id == "+972501111111"
        with identity_db() as conn:
            row = conn.execute(
                "SELECT whatsapp_bsuid, whatsapp_parent_bsuid FROM user_identities "
                "WHERE user_id = ?", ("+972501111111",)
            ).fetchone()
            assert row["whatsapp_bsuid"] == "IL.ChildA1"
            assert row["whatsapp_parent_bsuid"] == "IL.ParentRoot"

        # מסלול 1 — חיפוש לפי BSUID, parent_bsuid מתעדכן
        resolve_whatsapp_user(
            "",
            bsuid="IL.ChildA1",
            parent_bsuid="IL.ParentUpdated",
        )
        with identity_db() as conn:
            row = conn.execute(
                "SELECT whatsapp_parent_bsuid FROM user_identities WHERE user_id = ?",
                ("+972501111111",),
            ).fetchone()
            assert row["whatsapp_parent_bsuid"] == "IL.ParentUpdated"

    def test_resolve_preserves_existing_parent_bsuid_on_update(self, identity_db):
        """COALESCE — קריאה ללא parent_bsuid לא דורסת ערך קיים."""
        from utils.user_identity import resolve_whatsapp_user

        # יצירה עם parent_bsuid
        resolve_whatsapp_user(
            "+972502222222",
            bsuid="IL.ChildB2",
            parent_bsuid="IL.ParentKept",
        )

        # עדכון בלי parent_bsuid (None) — לא אמור לדרוס
        resolve_whatsapp_user(
            "+972502222222",
            bsuid="IL.ChildB2",
            parent_bsuid=None,
        )

        with identity_db() as conn:
            row = conn.execute(
                "SELECT whatsapp_parent_bsuid FROM user_identities WHERE user_id = ?",
                ("+972502222222",),
            ).fetchone()
            assert row["whatsapp_parent_bsuid"] == "IL.ParentKept"


class TestGetWhatsappSendAddress:
    """בדיקות ל-get_whatsapp_send_address — reverse lookup לשליחת הודעות."""

    def test_phone_user_returns_phone(self, identity_db):
        """user_id שהוא מספר טלפון — מחזיר את המספר ישירות."""
        from utils.user_identity import get_whatsapp_send_address

        result = get_whatsapp_send_address("+972501234567")

        assert result == "+972501234567"

    def test_bsuid_user_returns_stored_phone(self, identity_db):
        """user_id שהוא BSUID — מחזיר את מספר הטלפון מהטבלה."""
        from utils.user_identity import resolve_whatsapp_user, get_whatsapp_send_address

        # יצירת משתמש עם BSUID + טלפון → user_id = טלפון
        resolve_whatsapp_user("+972501234567", bsuid="IL.ABCdef123")

        # BSUID-only user — user_id = bsuid
        resolve_whatsapp_user("", bsuid="IL.OnlyBsuid99")

        # עבור user_id שהוא BSUID, אין טלפון שמור → None
        result = get_whatsapp_send_address("IL.OnlyBsuid99")
        assert result is None

    def test_unknown_user_returns_none(self, identity_db):
        """user_id לא ידוע ושלא מתחיל ב-+ — מחזיר None."""
        from utils.user_identity import get_whatsapp_send_address

        result = get_whatsapp_send_address("IL.UnknownUser999")

        assert result is None


class TestIsPhoneNumber:
    """בדיקות ל-_is_phone_number — הבחנה בין מספר טלפון ל-BSUID."""

    def test_e164_phone(self):
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("+972501234567") is True

    def test_digits_only_phone(self):
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("972501234567") is True

    def test_short_digits_not_phone(self):
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("12345678") is False

    def test_bsuid_iso_format_il(self):
        """BSUID בפורמט ISO alpha-2: IL.ABCdef123 — לא מספר טלפון."""
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("IL.ABCdef123xyz") is False

    def test_bsuid_iso_format_us(self):
        """BSUID בפורמט ISO alpha-2: US.13491208655302741918."""
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("US.13491208655302741918") is False

    def test_bsuid_string(self):
        from messaging.whatsapp_sender import _is_phone_number
        assert _is_phone_number("bsuid_abc123") is False


class TestDatabaseIdentityFunctions:
    """בדיקות לפונקציות DB של user_identities."""

    def test_upsert_creates_new_record(self, identity_db):
        """upsert_user_identity יוצרת רשומה חדשה."""
        from database import upsert_user_identity

        upsert_user_identity(
            "+972501234567", "whatsapp",
            phone_number="+972501234567",
        )

        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("+972501234567",)
            ).fetchone()
            assert row is not None
            assert row["channel"] == "whatsapp"

    def test_upsert_updates_existing(self, identity_db):
        """upsert_user_identity מעדכנת רשומה קיימת בלי לדרוס ערכים."""
        from database import upsert_user_identity

        # יצירה
        upsert_user_identity(
            "+972501234567", "whatsapp",
            phone_number="+972501234567",
            username="first_name",
        )
        # עדכון — מוסיפים BSUID, לא דורסים שם
        upsert_user_identity(
            "+972501234567", "whatsapp",
            whatsapp_bsuid="IL.ABCdef123",
        )

        with identity_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_identities WHERE user_id = ?", ("+972501234567",)
            ).fetchone()
            assert row["whatsapp_bsuid"] == "IL.ABCdef123"
            assert row["username"] == "first_name"  # לא נדרס

    def test_lookup_by_bsuid(self, identity_db):
        """lookup_user_id_by_bsuid מוצאת את ה-user_id הנכון."""
        from database import upsert_user_identity, lookup_user_id_by_bsuid

        upsert_user_identity(
            "+972501234567", "whatsapp",
            whatsapp_bsuid="IL.XYZ789abc",
        )

        result = lookup_user_id_by_bsuid("IL.XYZ789abc")
        assert result == "+972501234567"

    def test_lookup_by_bsuid_not_found(self, identity_db):
        """lookup_user_id_by_bsuid מחזירה None אם לא נמצא."""
        from database import lookup_user_id_by_bsuid

        result = lookup_user_id_by_bsuid("nonexistent_bsuid")
        assert result is None

    def test_lookup_by_phone(self, identity_db):
        """lookup_user_id_by_phone מוצאת את ה-user_id הנכון."""
        from database import upsert_user_identity, lookup_user_id_by_phone

        upsert_user_identity(
            "+972501234567", "whatsapp",
            phone_number="+972501234567",
        )

        result = lookup_user_id_by_phone("+972501234567")
        assert result == "+972501234567"

    def test_get_phone_for_user_with_phone_id(self, identity_db):
        """get_phone_for_user — user_id שמתחיל ב-+ מחזיר ישירות."""
        from database import get_phone_for_user

        result = get_phone_for_user("+972501234567")
        assert result == "+972501234567"

    def test_get_phone_for_user_with_bsuid(self, identity_db):
        """get_phone_for_user — חיפוש טלפון לפי BSUID."""
        from database import upsert_user_identity, get_phone_for_user

        upsert_user_identity(
            "IL.ABCdef123", "whatsapp",
            whatsapp_bsuid="IL.ABCdef123",
            phone_number="+972501234567",
        )

        result = get_phone_for_user("IL.ABCdef123")
        assert result == "+972501234567"
