"""
טסטים ל-CRUD של meta_credentials ב-database.py.

מכסה:
- upsert ראשון (insert) + שני (update).
- שליפה לפי page_id + IG account.
- list_meta_credentials מחזיר בלי tokens.
- is_meta_entry_known עובד גם עבור page_id וגם עבור IGBA.
- delete_meta_credentials.
- access_token מוצפן ב-DB ומפוענח בקריאה.
"""

import pytest


@pytest.fixture
def db_with_meta(db_conn):
    """אתחול DB ריק (מ-`db_conn` ב-conftest.py).

    `db_conn` מספק SECRETS_ENCRYPTION_KEY, DB_PATH זמני (גם על
    `ai_chatbot.config` וגם על `database` עצמו) — מבטיח שכל טסט
    מקבל DB נקי לחלוטין בלי דליפת שורות.
    """
    import database as db
    return db


class TestUpsert:
    def test_first_insert(self, db_with_meta):
        db = db_with_meta
        db.upsert_meta_credentials(
            page_id="PAGE_1",
            access_token="raw-token-abc",
            page_name="העסק של דנה",
            ig_business_account_id="IG_1",
            ig_username="dana_biz",
        )
        cred = db.get_meta_credentials_by_page_id("PAGE_1")
        assert cred is not None
        assert cred["page_id"] == "PAGE_1"
        assert cred["page_name"] == "העסק של דנה"
        assert cred["ig_business_account_id"] == "IG_1"
        assert cred["ig_username"] == "dana_biz"
        # access_token מפוענח חזרה בקריאה
        assert cred["access_token"] == "raw-token-abc"
        # access_token_encrypted לא דולף החוצה
        assert "access_token_encrypted" not in cred

    def test_update_existing_page(self, db_with_meta):
        """upsert שני על אותו page_id ⇒ עדכון, לא duplicate."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "old-token", page_name="old name")
        db.upsert_meta_credentials("P1", "new-token", page_name="new name")
        cred = db.get_meta_credentials_by_page_id("P1")
        assert cred["access_token"] == "new-token"
        assert cred["page_name"] == "new name"
        # רק שורה אחת קיימת
        assert len(db.list_meta_credentials()) == 1

    def test_multiple_pages_coexist(self, db_with_meta):
        """כמה עמודים שונים מאוחסנים במקביל (תמיכה ב-multi-page)."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "t1", page_name="page 1")
        db.upsert_meta_credentials("P2", "t2", page_name="page 2")
        db.upsert_meta_credentials("P3", "t3", page_name="page 3")
        assert len(db.list_meta_credentials()) == 3

    def test_empty_igba_on_update_preserves_existing(self, db_with_meta):
        """כשליפה חוזרת לא מצליחה להחזיר IGBA — שומרים את הקיים."""
        db = db_with_meta
        db.upsert_meta_credentials(
            "P1", "tok1",
            ig_business_account_id="IG_REAL",
            ig_username="biz_user",
        )
        # עדכון שני בלי IGBA (שליפה נכשלה / זמני)
        db.upsert_meta_credentials("P1", "tok2", page_name="updated")
        cred = db.get_meta_credentials_by_page_id("P1")
        # ה-IGBA הקיים נשמר, לא נמחק
        assert cred["ig_business_account_id"] == "IG_REAL"
        assert cred["ig_username"] == "biz_user"
        # שאר השדות עודכנו
        assert cred["access_token"] == "tok2"
        assert cred["page_name"] == "updated"

    def test_explicit_igba_update_overwrites(self, db_with_meta):
        """אם החדש *לא ריק*, הוא דורס — זה לא preserve כשמספקים ערך אמיתי."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "tok", ig_business_account_id="IG_OLD")
        db.upsert_meta_credentials("P1", "tok", ig_business_account_id="IG_NEW")
        cred = db.get_meta_credentials_by_page_id("P1")
        assert cred["ig_business_account_id"] == "IG_NEW"

    def test_duplicate_igba_rejected(self, db_with_meta):
        """partial UNIQUE על ig_business_account_id (כשלא ריק) חוסם כפילות."""
        import sqlite3
        db = db_with_meta
        db.upsert_meta_credentials("P1", "t1", ig_business_account_id="IG_X")
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_meta_credentials("P2", "t2", ig_business_account_id="IG_X")

    def test_multiple_empty_igba_allowed(self, db_with_meta):
        """כמה עמודים בלי IG מקושר — מותר (partial UNIQUE מתעלם מ-'')."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "t1")  # IGBA ברירת מחדל ''
        db.upsert_meta_credentials("P2", "t2")
        db.upsert_meta_credentials("P3", "t3")
        # אם זה נופל בגלל UNIQUE, ה-test ייכשל אוטומטית
        assert len(db.list_meta_credentials()) == 3


class TestEncryption:
    def test_token_is_encrypted_in_db(self, db_with_meta, tmp_path):
        """ה-token לא נשמר בטקסט גלוי ב-DB."""
        db = db_with_meta
        db.upsert_meta_credentials("P_SECRET", "super-secret-token-xyz")

        # קריאה ישירה מ-DB — לא דרך get_*
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT access_token_encrypted FROM meta_credentials WHERE page_id = ?",
                ("P_SECRET",),
            ).fetchone()
        raw = row["access_token_encrypted"]
        assert "super-secret-token-xyz" not in raw
        # פורמט utils/crypto.py: v1:<ciphertext>
        assert raw.startswith("v1:")


class TestLookups:
    def test_get_by_ig_account(self, db_with_meta):
        db = db_with_meta
        db.upsert_meta_credentials(
            "P1", "tok", ig_business_account_id="IG_777", ig_username="biz",
        )
        cred = db.get_meta_credentials_by_ig_account("IG_777")
        assert cred is not None
        assert cred["page_id"] == "P1"
        assert cred["access_token"] == "tok"

    def test_get_by_ig_account_returns_none_when_missing(self, db_with_meta):
        db = db_with_meta
        assert db.get_meta_credentials_by_ig_account("IG_DOES_NOT_EXIST") is None

    def test_get_by_page_id_returns_none_when_missing(self, db_with_meta):
        db = db_with_meta
        assert db.get_meta_credentials_by_page_id("NO_SUCH_PAGE") is None

    def test_is_meta_entry_known_by_page_id(self, db_with_meta):
        db = db_with_meta
        db.upsert_meta_credentials("PAGE_A", "t")
        assert db.is_meta_entry_known("PAGE_A") is True
        assert db.is_meta_entry_known("PAGE_X") is False

    def test_is_meta_entry_known_by_ig_account(self, db_with_meta):
        """ה-webhook של IG מקבל entry.id כ-IGBA — חייב להיות מוכר גם הוא."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "t", ig_business_account_id="IGBA_99")
        assert db.is_meta_entry_known("IGBA_99") is True

    def test_is_meta_entry_known_empty(self, db_with_meta):
        db = db_with_meta
        assert db.is_meta_entry_known("") is False
        assert db.is_meta_entry_known(None) is False


class TestList:
    def test_list_does_not_contain_tokens(self, db_with_meta):
        """list_meta_credentials לא חושף tokens (אפילו לא מוצפנים)."""
        db = db_with_meta
        db.upsert_meta_credentials("P1", "tok", page_name="A")
        rows = db.list_meta_credentials()
        assert len(rows) == 1
        assert "access_token" not in rows[0]
        assert "access_token_encrypted" not in rows[0]

    def test_list_ordered_by_created_desc(self, db_with_meta):
        """החדש ביותר ראשון."""
        import time
        db = db_with_meta
        db.upsert_meta_credentials("P_OLD", "t")
        time.sleep(1.05)  # datetime('now') ברזולוציית שניות
        db.upsert_meta_credentials("P_NEW", "t")
        rows = db.list_meta_credentials()
        assert rows[0]["page_id"] == "P_NEW"
        assert rows[1]["page_id"] == "P_OLD"


class TestDelete:
    def test_delete_existing(self, db_with_meta):
        db = db_with_meta
        db.upsert_meta_credentials("P1", "t")
        assert db.delete_meta_credentials("P1") is True
        assert db.get_meta_credentials_by_page_id("P1") is None

    def test_delete_nonexistent_returns_false(self, db_with_meta):
        db = db_with_meta
        assert db.delete_meta_credentials("NEVER_EXISTED") is False
