"""
טסטים על העמודות החדשות בטבלת users (provider_asset_id, external_user_id)
ועל ה-UNIQUE constraint שמגן מפני זהויות כפולות בתוך אותו asset.
"""

import sqlite3

import pytest


@pytest.fixture
def db(db_conn):
    import database as _db
    return _db


class TestUpsertUserMetaFields:
    def test_meta_ig_user_stores_asset_and_external_id(self, db):
        db.upsert_user(
            user_id="meta_ig:IGSID_1",
            username="dana",
            channel="meta_ig",
            provider_asset_id="IGBA_1",
            external_user_id="IGSID_1",
        )
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT channel, provider_asset_id, external_user_id "
                "FROM users WHERE user_id = ?",
                ("meta_ig:IGSID_1",),
            ).fetchone()
        assert row["channel"] == "meta_ig"
        assert row["provider_asset_id"] == "IGBA_1"
        assert row["external_user_id"] == "IGSID_1"

    def test_telegram_user_has_empty_meta_fields(self, db):
        """משתמשי טלגרם — provider_asset_id ריק (לא breaking)."""
        db.upsert_user("123456", "user_tg", channel="telegram")
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT channel, provider_asset_id, external_user_id "
                "FROM users WHERE user_id = ?",
                ("123456",),
            ).fetchone()
        assert row["channel"] == "telegram"
        assert row["provider_asset_id"] == ""
        assert row["external_user_id"] == ""

    def test_existing_meta_fields_preserved_on_partial_update(self, db):
        """upsert חוזר בלי provider_asset_id ⇒ ה-asset הקיים נשמר."""
        db.upsert_user(
            user_id="meta_ig:X",
            channel="meta_ig",
            provider_asset_id="IGBA_FIRST",
            external_user_id="X",
        )
        # עדכון חוזר בלי לעדכן את שדות המטא (למשל ההודעה השנייה — אין צורך)
        db.upsert_user(user_id="meta_ig:X", channel="meta_ig")
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT provider_asset_id, external_user_id, message_count "
                "FROM users WHERE user_id = ?",
                ("meta_ig:X",),
            ).fetchone()
        # נשמר ה-IGBA הראשון, ו-message_count עלה
        assert row["provider_asset_id"] == "IGBA_FIRST"
        assert row["external_user_id"] == "X"
        assert row["message_count"] == 2


class TestProviderIdentityUniqueConstraint:
    def test_same_asset_and_external_id_blocked(self, db):
        """אסור להכניס שני user_ids שונים על אותו (channel, asset, external)."""
        db.upsert_user(
            user_id="meta_ig:A",
            channel="meta_ig",
            provider_asset_id="IGBA_1",
            external_user_id="EXT_1",
        )
        # ניסיון להכניס שורה אחרת עם אותו tuple ⇒ UNIQUE INDEX זורק
        from database import get_connection
        with pytest.raises(sqlite3.IntegrityError):
            with get_connection() as conn:
                conn.execute(
                    """INSERT INTO users
                       (user_id, channel, provider_asset_id, external_user_id)
                       VALUES (?, ?, ?, ?)""",
                    ("meta_ig:B_DIFFERENT_PK", "meta_ig", "IGBA_1", "EXT_1"),
                )

    def test_same_external_id_different_assets_allowed(self, db):
        """אותו external_id משני assets שונים — מותר (זהויות נפרדות)."""
        db.upsert_user(
            user_id="meta_ig:user_at_acc1",
            channel="meta_ig",
            provider_asset_id="IGBA_1",
            external_user_id="SAME_RAW",
        )
        # PK שונה כדי לעקוף את ה-PRIMARY KEY של user_id
        db.upsert_user(
            user_id="meta_ig:user_at_acc2",
            channel="meta_ig",
            provider_asset_id="IGBA_2",
            external_user_id="SAME_RAW",
        )
        # שתי שורות נכנסו בהצלחה
        from database import get_connection
        with get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM users WHERE external_user_id = ?",
                ("SAME_RAW",),
            ).fetchone()[0]
        assert cnt == 2

    def test_telegram_users_not_blocked_by_index(self, db):
        """UNIQUE החדש partial (WHERE != '') — לא חוסם משתמשי טלגרם
        ישנים שטרם עברו backfill."""
        db.upsert_user("123", channel="telegram")
        db.upsert_user("456", channel="telegram")
        # שורתיים נכנסו, external_user_id ריק לשתיהן ⇒ partial UNIQUE
        # מתעלם
        from database import get_connection
        with get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM users WHERE channel = 'telegram'"
            ).fetchone()[0]
        assert cnt >= 2
