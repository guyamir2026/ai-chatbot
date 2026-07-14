"""
טסטים לפיצ'רי תיקון 13 — הסכמה, זכות עיון, זכות מחיקה, ומחיקה אוטומטית
לפי תקופות שמירה.
"""

import secrets
from unittest.mock import patch

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """מאתחל DB בקובץ זמני ומחזיר את מודול database.

    מגדיר אוטומטית LEDGER_PEPPER_V1 לטסטים — אחרת record_consent_event
    תיכשל רכות ולא ניתן יהיה לבדוק את ה-ledger. ה-pepper שונה בין טסטים
    (tmp_path) — אבל בתוך אותו טסט הוא קבוע.
    """
    monkeypatch.setenv("LEDGER_PEPPER_V1", secrets.token_urlsafe(32))
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ─── Consent ──────────────────────────────────────────────────────────────────


class TestConsent:
    """טסטי הסכמה — חייבים להפעיל את הפלאג CONSENT_SCREEN_ENABLED כי
    ברירת המחדל היא false (מסך הסכמה כבוי) ואז has_consent תמיד מחזיר True.
    """

    @pytest.fixture(autouse=True)
    def _enable_consent_flag(self, monkeypatch):
        monkeypatch.setattr("ai_chatbot.config.CONSENT_SCREEN_ENABLED", True)

    def test_no_consent_for_new_user(self, db):
        """משתמש חדש שלא נתן הסכמה — has_consent מחזיר False."""
        assert db.has_consent("999") is False

    def test_record_consent_marks_user_as_consenting(self, db):
        db.record_consent("100", username="alice")
        assert db.has_consent("100") is True

    def test_record_consent_creates_user_if_missing(self, db):
        """record_consent יוצר את שורת המשתמש גם אם לא קיים."""
        db.record_consent("200", username="bob", channel="whatsapp")
        summary = db.get_user_data_summary("200")
        assert summary["exists"] is True
        assert summary["channel"] == "whatsapp"

    def test_record_consent_idempotent(self, db):
        """קריאה כפולה ל-record_consent לא יוצרת שורות כפולות — ON CONFLICT."""
        db.record_consent("300")
        db.record_consent("300")
        # אין כשל = הצלחה. וודא שיש בדיוק רשומה אחת
        with db.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE user_id = ?", ("300",)).fetchone()
            assert dict(row)["c"] == 1

    def test_revoke_consent_clears_consent(self, db):
        db.record_consent("400")
        assert db.has_consent("400") is True
        db.revoke_consent("400")
        assert db.has_consent("400") is False

    def test_outdated_consent_version_invalidates(self, db, monkeypatch):
        """אם consent_version שלי נמוך מהגרסה הנוכחית — has_consent חוזר False."""
        db.record_consent("500")
        # מעלים את הגרסה הנוכחית — המשתמש "מאחור"
        monkeypatch.setattr(db, "CURRENT_CONSENT_VERSION", db.CURRENT_CONSENT_VERSION + 1)
        assert db.has_consent("500") is False


class TestConsentFlagBypass:
    """כש-CONSENT_SCREEN_ENABLED=false (ברירת מחדל החדשה): השער פתוח —
    has_consent מחזיר True לכולם, אבל consent_given_at *לא* נכתב ל-DB
    כדי לא לזייף הסכמה שלא ניתנה בפועל.
    """

    @pytest.fixture(autouse=True)
    def _disable_consent_flag(self, monkeypatch):
        monkeypatch.setattr("ai_chatbot.config.CONSENT_SCREEN_ENABLED", False)

    def test_unknown_user_passes_when_flag_off(self, db):
        """המשתמש לא קיים ב-DB — עדיין True כי הפלאג כבוי."""
        assert db.has_consent("never_seen_user_zzz") is True

    def test_user_without_consent_passes_when_flag_off(self, db):
        """משתמש קיים שלא לחץ "מסכים" — עדיין True כשהפלאג כבוי."""
        db.upsert_user("flag_off_user_1")
        assert db.has_consent("flag_off_user_1") is True

    def test_consent_given_at_remains_null_when_flag_off(self, db):
        """הפלאג כבוי לא כותב consent_given_at אוטומטית — אין זיוף."""
        db.upsert_user("flag_off_user_2")
        # has_consent מחזיר True אבל לא כותב
        assert db.has_consent("flag_off_user_2") is True
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT consent_given_at FROM users WHERE user_id = ?",
                ("flag_off_user_2",),
            ).fetchone()
            assert row["consent_given_at"] is None


# ─── User Data Summary (זכות עיון, /myinfo) ───────────────────────────────────


class TestGetUserDataSummary:
    def test_nonexistent_user_returns_empty_summary(self, db):
        summary = db.get_user_data_summary("nonexistent")
        assert summary["exists"] is False
        assert summary["message_count"] == 0
        assert summary["appointments"]["total"] == 0

    def test_existing_user_summary(self, db):
        db.upsert_user("u1", username="dana", channel="telegram")
        summary = db.get_user_data_summary("u1")
        assert summary["exists"] is True
        assert summary["username"] == "dana"
        assert summary["channel"] == "telegram"
        assert summary["message_count"] >= 0

    def test_appointments_count_by_status(self, db):
        db.upsert_user("u2", username="moshe")
        db.create_appointment(
            user_id="u2", username="moshe", service="ייעוץ",
            preferred_date="2099-01-05", preferred_time="10:00",
        )
        summary = db.get_user_data_summary("u2")
        assert summary["appointments"]["total"] == 1
        assert summary["appointments"]["by_status"].get("pending") == 1

    def test_summary_includes_all_pii_tables(self, db):
        """רגרסיה לתיקון 13 — זכות עיון מלאה: ה-summary חייב לכלול counts
        לכל הטבלאות שמכילות user_id, לא רק appointments + live_chats."""
        db.upsert_user("u3")
        summary = db.get_user_data_summary("u3")
        # שדות שנוספו לזכות עיון מורחבת — חייבים להופיע גם כשהמשתמש ריק
        for field in (
            "conversations_total",
            "conversation_summaries_total",
            "unanswered_questions_total",
            "lead_followups",
            "referrals_as_referrer_total",
            "referrals_as_referred_total",
            "has_referral_code",
            "credits",
            "response_pages_total",
            "broadcast_deliveries_total",
            "identities_total",
        ):
            assert field in summary, f"שדה {field} חסר ב-summary — חוסר בזכות עיון"

    def test_summary_counts_lead_followups(self, db):
        """lead_followups מכיל סיווגי AI על המשתמש — תיקון 13 דורש שקיפות."""
        db.upsert_user("u4")
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, channel, intent_type, lead_temperature, status,
                        followup_due_at)
                   VALUES (?, 'telegram', 'booking_intent', 'hot', 'pending',
                           datetime('now', '+1 day'))""",
                ("u4",),
            )
        summary = db.get_user_data_summary("u4")
        assert summary["lead_followups"]["total"] == 1
        assert summary["lead_followups"]["by_status"].get("pending") == 1

    def test_summary_counts_conversations_and_referrals(self, db):
        """conversations + referrals (שני צדדים) חייבים להיספר בנפרד."""
        db.upsert_user("u5")
        db.upsert_user("u6")
        db.save_message("u5", "x", "user", "hi")
        db.save_message("u5", "x", "assistant", "hello")
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO referrals (referrer_id, referred_id, code, status)
                   VALUES (?, ?, 'CODE1', 'completed')""",
                ("u5", "u6"),
            )
        s5 = db.get_user_data_summary("u5")
        s6 = db.get_user_data_summary("u6")
        assert s5["conversations_total"] == 2
        assert s5["referrals_as_referrer_total"] == 1
        assert s5["referrals_as_referred_total"] == 0
        assert s6["referrals_as_referrer_total"] == 0
        assert s6["referrals_as_referred_total"] == 1


# ─── Delete User Data (זכות מחיקה, /forget) ───────────────────────────────────


class TestDeleteUserData:
    def test_delete_removes_user_row(self, db):
        db.upsert_user("d1")
        counts = db.delete_user_data("d1")
        assert counts.get("users", 0) >= 1
        assert db.get_user_data_summary("d1")["exists"] is False

    def test_delete_removes_appointments(self, db):
        db.upsert_user("d2", username="lina")
        db.create_appointment(
            user_id="d2", username="lina", service="טיפול",
            preferred_date="2099-02-10", preferred_time="14:00",
        )
        counts = db.delete_user_data("d2")
        assert counts.get("appointments", 0) == 1
        assert db.get_user_data_summary("d2")["appointments"]["total"] == 0

    def test_delete_other_users_data_is_preserved(self, db):
        db.upsert_user("keep_me", username="A")
        db.upsert_user("delete_me", username="B")
        db.create_appointment(
            user_id="keep_me", username="A", service="x",
            preferred_date="2099-03-01", preferred_time="09:00",
        )
        db.create_appointment(
            user_id="delete_me", username="B", service="y",
            preferred_date="2099-03-01", preferred_time="10:00",
        )
        db.delete_user_data("delete_me")
        # המשתמש שלא מחק עדיין קיים עם התור שלו
        assert db.get_user_data_summary("keep_me")["exists"] is True
        assert db.get_user_data_summary("keep_me")["appointments"]["total"] == 1

    def test_delete_nonexistent_user_does_not_crash(self, db):
        counts = db.delete_user_data("never_existed")
        assert isinstance(counts, dict)

    def test_delete_removes_unanswered_questions(self, db):
        """רגרסיה: שאלות ללא תשובה (PII — user_id, username, question)
        חייבות להימחק עם /forget. בלי זה הזכות להישכח לא מקיימת את עצמה."""
        db.upsert_user("u_unanswered", username="alice")
        db.save_unanswered_question(
            "u_unanswered", "alice", "מה המחיר לטיפול ?", channel="telegram",
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM unanswered_questions WHERE user_id = ?",
                ("u_unanswered",),
            ).fetchone()
            assert dict(row)["c"] == 1
        db.delete_user_data("u_unanswered")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM unanswered_questions WHERE user_id = ?",
                ("u_unanswered",),
            ).fetchone()
            assert dict(row)["c"] == 0

    def test_delete_user_data_for_bsuid_only_user(self, db):
        """תיקון 13: /forget של משתמש BSUID-only מנקה גם user_identities."""
        from utils.user_identity import resolve_whatsapp_user
        bsuid_uid = resolve_whatsapp_user(
            "",
            bsuid="IL.PrivacyBsuid9",
            parent_bsuid="IL.PrivacyParent",
        )
        assert bsuid_uid == "IL.PrivacyBsuid9"

        # ווידוא קיום השורה לפני המחיקה
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT whatsapp_bsuid, whatsapp_parent_bsuid FROM user_identities "
                "WHERE user_id = ?", (bsuid_uid,),
            ).fetchone()
            assert row is not None
            assert dict(row)["whatsapp_bsuid"] == "IL.PrivacyBsuid9"

        counts = db.delete_user_data(bsuid_uid)
        assert counts.get("user_identities", 0) >= 1

        # ווידוא — אין שורה בכלל ב-user_identities
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_identities WHERE user_id = ?",
                (bsuid_uid,),
            ).fetchone()
            assert dict(row)["c"] == 0

    def test_summary_counts_bsuid_only_user_identity(self, db):
        """get_user_data_summary סופר את שורת ה-BSUID-only בטבלת user_identities."""
        from utils.user_identity import resolve_whatsapp_user
        bsuid_uid = resolve_whatsapp_user(
            "",
            bsuid="IL.SummaryBsuid10",
            parent_bsuid="IL.SummaryParent",
        )
        # ה-user נוצר רק ב-user_identities; ב-flow אמיתי גם נכתב ל-users
        # בעקבות upsert_user מה-handler. מדמים זאת כדי שזכות עיון תכלול אותו.
        db.upsert_user(bsuid_uid, username="bsuid_user", channel="whatsapp")

        summary = db.get_user_data_summary(bsuid_uid)
        assert summary["exists"] is True
        assert summary["identities_total"] >= 1

        # ווידוא ישיר ש-BSUID + parent_bsuid אכן נשמרו (זמינים ל-export עתידי)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT whatsapp_bsuid, whatsapp_parent_bsuid FROM user_identities "
                "WHERE user_id = ?", (bsuid_uid,),
            ).fetchone()
            assert dict(row)["whatsapp_bsuid"] == "IL.SummaryBsuid10"
            assert dict(row)["whatsapp_parent_bsuid"] == "IL.SummaryParent"

    def test_delete_removes_lead_followups(self, db):
        """רגרסיה: lead_followups מכיל user_id + username + סיכום שיחה (PII)."""
        db.upsert_user("u_lead", username="bob")
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, username, channel, conversation_summary,
                        followup_due_at)
                   VALUES (?, ?, 'telegram', ?, datetime('now', '+1 day'))""",
                ("u_lead", "bob", "summary"),
            )
        db.delete_user_data("u_lead")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM lead_followups WHERE user_id = ?",
                ("u_lead",),
            ).fetchone()
            assert dict(row)["c"] == 0


# ─── Retention purge (מחיקה אוטומטית לפי תקופות שמירה) ─────────────────────


class TestRetentionPurge:
    def test_purge_old_conversations(self, db):
        """שיחה ישנה (יותר מ-12 חודשים) נמחקת; חדשה — נשמרת."""
        # מוסיפים ידנית שיחות עם תאריכים מסוימים
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, username, role, message, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-13 months'))",
                ("u_old", "old_user", "user", "הודעה ישנה"),
            )
            conn.execute(
                "INSERT INTO conversations (user_id, username, role, message, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-1 month'))",
                ("u_new", "new_user", "user", "הודעה חדשה"),
            )
        counts = db.purge_old_data(conversations_months=12)
        assert counts["conversations"] >= 1
        # רשומה חדשה עדיין קיימת
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE user_id = ?", ("u_new",),
            ).fetchone()
            assert dict(row)["c"] == 1
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE user_id = ?", ("u_old",),
            ).fetchone()
            assert dict(row)["c"] == 0

    def test_purge_only_closed_appointments(self, db):
        """פירג מוחק רק תורים פסיביים (passed/cancelled), לא אקטיביים."""
        # תור pending ישן — לא יימחק (עדיין פעיל לוגית)
        # תור cancelled ישן — יימחק
        # תור pending חדש — לא יימחק
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, status) "
                "VALUES (?, ?, ?, date('now', '-40 months'), 'pending')",
                ("u1", "x", "old_pending"),
            )
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, status) "
                "VALUES (?, ?, ?, date('now', '-40 months'), 'cancelled')",
                ("u2", "y", "old_cancelled"),
            )
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, status) "
                "VALUES (?, ?, ?, date('now', '-40 months'), 'passed')",
                ("u3", "z", "old_passed"),
            )
            conn.execute(
                "INSERT INTO appointments (user_id, username, service, preferred_date, status) "
                "VALUES (?, ?, ?, date('now', '+10 days'), 'pending')",
                ("u4", "w", "future_pending"),
            )
        counts = db.purge_old_data(closed_appointments_months=36)
        # cancelled ו-passed הישנים נמחקו = 2
        assert counts["appointments"] == 2

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT service FROM appointments ORDER BY id"
            ).fetchall()
            services = [dict(r)["service"] for r in row]
            assert "old_pending" in services       # pending ישן נשמר
            assert "future_pending" in services    # עתידי נשמר
            assert "old_cancelled" not in services # cancelled ישן נמחק
            assert "old_passed" not in services    # passed ישן נמחק

    def test_purge_returns_zero_when_nothing_to_delete(self, db):
        """DB ריק — counts אפסיים, ללא קריסה."""
        counts = db.purge_old_data()
        assert all(v == 0 for v in counts.values())

    def test_purge_response_pages_after_30_days(self, db):
        """response_pages — slug ציבורי, חי 30 יום בלבד."""
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO response_pages (id, content, title, user_id, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-31 days'))",
                ("old_slug_aaaaaaaaaaaaaa", "old content", "old", "u1"),
            )
            conn.execute(
                "INSERT INTO response_pages (id, content, title, user_id, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-1 day'))",
                ("new_slug_bbbbbbbbbbbbbb", "new content", "new", "u2"),
            )
        counts = db.purge_old_data()
        assert counts["response_pages"] >= 1
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM response_pages"
            ).fetchone()
            assert dict(row)["c"] == 1  # רק החדש שרד

    def test_purge_agent_requests(self, db):
        """agent_requests — 12 חודשים מ-handled_at או created_at."""
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO agent_requests (user_id, message, status, created_at, handled_at) "
                "VALUES (?, ?, 'handled', datetime('now', '-13 months'), datetime('now', '-13 months'))",
                ("u1", "old"),
            )
            conn.execute(
                "INSERT INTO agent_requests (user_id, message, status, created_at) "
                "VALUES (?, ?, 'pending', datetime('now', '-1 month'))",
                ("u2", "new"),
            )
        counts = db.purge_old_data()
        assert counts["agent_requests"] >= 1
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM agent_requests"
            ).fetchall()
            users = [dict(r)["user_id"] for r in row]
            assert "u1" not in users
            assert "u2" in users

    def test_purge_unanswered_questions_split_by_status(self, db):
        """open → 90 יום, resolved → 6 חודשים. שתי תקופות שונות."""
        with db.get_connection() as conn:
            # open ישן (91 יום) — יימחק
            conn.execute(
                "INSERT INTO unanswered_questions (user_id, question, status, created_at) "
                "VALUES (?, ?, 'open', datetime('now', '-91 days'))",
                ("u_old_open", "שאלה ישנה פתוחה"),
            )
            # open חדש (30 יום) — נשאר
            conn.execute(
                "INSERT INTO unanswered_questions (user_id, question, status, created_at) "
                "VALUES (?, ?, 'open', datetime('now', '-30 days'))",
                ("u_new_open", "שאלה חדשה"),
            )
            # resolved ישן (7 חודשים) — יימחק
            conn.execute(
                "INSERT INTO unanswered_questions (user_id, question, status, created_at, resolved_at) "
                "VALUES (?, ?, 'resolved', datetime('now', '-8 months'), datetime('now', '-7 months'))",
                ("u_resolved_old", "פתורה ישנה"),
            )
            # resolved חדש (3 חודשים) — נשאר
            conn.execute(
                "INSERT INTO unanswered_questions (user_id, question, status, created_at, resolved_at) "
                "VALUES (?, ?, 'resolved', datetime('now', '-4 months'), datetime('now', '-3 months'))",
                ("u_resolved_new", "פתורה חדשה"),
            )
        db.purge_old_data()
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM unanswered_questions"
            ).fetchall()
            users = [dict(r)["user_id"] for r in row]
            assert "u_old_open" not in users
            assert "u_resolved_old" not in users
            assert "u_new_open" in users
            assert "u_resolved_new" in users

    def test_purge_lead_followups(self, db):
        """lead_followups — 6 חודשים מהאירוע האחרון."""
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, channel, status, followup_due_at, created_at, followup_sent_at)
                   VALUES (?, 'telegram', 'sent', datetime('now', '-7 months'),
                           datetime('now', '-8 months'), datetime('now', '-7 months'))""",
                ("lf_old",),
            )
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, channel, status, followup_due_at, created_at)
                   VALUES (?, 'telegram', 'pending', datetime('now', '+1 day'),
                           datetime('now', '-1 day'))""",
                ("lf_new",),
            )
        counts = db.purge_old_data()
        assert counts["lead_followups"] >= 1
        with db.get_connection() as conn:
            row = conn.execute("SELECT user_id FROM lead_followups").fetchall()
            users = [dict(r)["user_id"] for r in row]
            assert "lf_old" not in users
            assert "lf_new" in users


class TestResponsePageSecurity:
    """אבטחת עמודי תשובה ציבוריים — slug ארוך + entropy גבוהה."""

    def test_slug_is_long_enough(self, db):
        """slug חייב להיות לפחות 22 תווים (≈128 ביט base64url)."""
        page_id = db.create_response_page("test content", title="t", user_id="u")
        assert len(page_id) >= 22, f"slug קצר מדי: {len(page_id)} תווים — צריך 22+"

    def test_slugs_are_unique_and_random(self, db):
        """100 קריאות צריכות להחזיר 100 slugs שונים."""
        slugs = {db.create_response_page(f"content {i}") for i in range(100)}
        assert len(slugs) == 100, "התנגשות slug — אנטרופיה לא מספיקה"

    def test_slug_uses_url_safe_alphabet(self, db):
        """slug חייב להיות בטוח ל-URL — base64url alphabet (אותיות, ספרות, _ -)."""
        import re
        page_id = db.create_response_page("test", user_id="u")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", page_id), f"slug מכיל תווים לא בטוחים: {page_id}"


class TestSecretsEncryption:
    """הצפנת שדות סודיים — refresh_token / access_token של Google Calendar."""

    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        """encrypt → decrypt חייב להחזיר את הטקסט המקורי."""
        from cryptography.fernet import Fernet
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        # נקה cache כי הטסט הקודם אולי טען מפתח שונה
        from utils import crypto
        crypto._fernet_cache.clear()
        from utils.crypto import encrypt_field, decrypt_field
        original = "ya29.a0AfH6SMC..."
        encrypted = encrypt_field(original)
        assert encrypted != original
        assert encrypted.startswith("v1:")
        assert decrypt_field(encrypted) == original

    def test_decrypt_handles_legacy_plaintext(self, monkeypatch):
        """ערך בלי prefix 'v1:' = legacy — מחזיר כמו שהוא."""
        from cryptography.fernet import Fernet
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        from utils import crypto
        crypto._fernet_cache.clear()
        from utils.crypto import decrypt_field
        assert decrypt_field("legacy_plain_token") == "legacy_plain_token"
        assert decrypt_field("") == ""

    def test_encrypt_empty_returns_empty(self, monkeypatch):
        """שדה ריק לא מוצפן — מאפשר זיהוי 'אין טוקן' בלי פענוח."""
        from cryptography.fernet import Fernet
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        from utils import crypto
        crypto._fernet_cache.clear()
        from utils.crypto import encrypt_field
        assert encrypt_field("") == ""

    def test_is_encrypted_detects_prefix(self):
        from utils.crypto import is_encrypted
        assert is_encrypted("v1:abc") is True
        assert is_encrypted("v2:abc") is True
        assert is_encrypted("plain_text") is False
        assert is_encrypted("") is False
        assert is_encrypted("ya29.a0Af") is False  # OAuth token format

    def test_google_calendar_credentials_stored_encrypted(self, db, monkeypatch):
        """integration: tokens נשמרים מוצפנים ב-DB ומפוענחים בקריאה."""
        from cryptography.fernet import Fernet
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        from utils import crypto
        crypto._fernet_cache.clear()

        db.save_google_calendar_credentials(
            google_account_email="biz@example.com",
            calendar_id="primary",
            refresh_token="REFRESH_SECRET",
            access_token="ACCESS_SECRET",
            token_expiry="2026-01-01T00:00:00Z",
            timezone="Asia/Jerusalem",
        )

        # ב-DB עצמו — מוצפן
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT refresh_token, access_token FROM google_calendar_credentials WHERE id = 1"
            ).fetchone()
            assert dict(row)["refresh_token"].startswith("v1:")
            assert dict(row)["access_token"].startswith("v1:")
            assert "REFRESH_SECRET" not in dict(row)["refresh_token"]

        # קריאה דרך API — מפוענח
        creds = db.get_google_calendar_credentials()
        assert creds is not None
        assert creds["refresh_token"] == "REFRESH_SECRET"
        assert creds["access_token"] == "ACCESS_SECRET"


class TestConsentLedger:
    """consent_ledger — תיקון 13: הוכחה פסאודונימית להסכמה/ביטול/מחיקה.

    מאמתים: דטרמיניסטיות (אותו user_id → אותו hash), קישור היסטוריה
    אחרי /forget + re-consent (אופציה א), הפרדת קטגוריות consent/audit,
    retention שונה לכל קטגוריה, וכתיבת אירועים בכל מסלולי ההסכמה.
    """

    def _events(self, db, user_id, channel="telegram", event_type=None):
        from utils import consent_ledger
        # cache reset כדי שה-pepper מ-monkeypatch ייטען מחדש
        return consent_ledger.get_events_for_subject(user_id, channel, event_type)

    def test_record_consent_writes_consent_given(self, db):
        db.record_consent("led1", username="x", channel="telegram")
        events = self._events(db, "led1")
        assert len(events) == 1
        assert events[0]["event_type"] == "consent_given"
        assert events[0]["category"] == "consent"
        assert events[0]["consent_version"] == db.CURRENT_CONSENT_VERSION
        assert events[0]["pepper_version"] == "v1"

    def test_subject_hash_is_deterministic(self, db):
        """אותו user_id+channel נותן אותו hash. אופציה א של היועץ."""
        db.record_consent("led2", channel="telegram")
        db.record_consent("led2", channel="telegram")  # שוב — אותו hash
        events = self._events(db, "led2")
        assert len(events) == 2
        assert events[0]["subject_hash"] == events[1]["subject_hash"]

    def test_subject_hash_differs_by_channel(self, db):
        """אותו user_id בערוצים שונים → hashes שונים (לא אמור לקרות בפועל,
        אבל הגנה תקפה)."""
        from utils.consent_ledger import _subject_hash
        h1 = _subject_hash("u", "telegram")
        h2 = _subject_hash("u", "whatsapp")
        assert h1 != h2

    def test_revoke_consent_writes_consent_revoked(self, db):
        db.record_consent("led3", channel="telegram")
        db.revoke_consent("led3")
        events = self._events(db, "led3")
        types = [e["event_type"] for e in events]
        assert "consent_given" in types
        assert "consent_revoked" in types

    def test_delete_user_data_writes_audit_pair(self, db):
        """deletion_requested לפני, deletion_completed אחרי — עם counts."""
        db.record_consent("led4", channel="telegram")
        db.upsert_user("led4", channel="telegram")
        db.delete_user_data("led4")
        events = self._events(db, "led4")
        types = [e["event_type"] for e in events]
        assert "deletion_requested" in types
        assert "deletion_completed" in types
        # ה-completed חייב להיות אחרי ה-requested
        idx_req = types.index("deletion_requested")
        idx_comp = types.index("deletion_completed")
        assert idx_req < idx_comp
        # metadata של completed כולל counts
        completed = events[idx_comp]
        import json
        meta = json.loads(completed["metadata_json"])
        assert "counts" in meta
        assert "total" in meta

    def test_forget_then_reconsent_links_history(self, db):
        """אופציה א של היועץ: אותו אדם נמחק וחוזר → אותו subject_hash.
        זה תכונה (קישור היסטוריה), לא באג. דורש שקיפות במדיניות."""
        db.record_consent("led5", channel="telegram")
        db.delete_user_data("led5")
        db.record_consent("led5", channel="telegram")
        events = self._events(db, "led5")
        types = [e["event_type"] for e in events]
        # 5 אירועים: consent_given, deletion_requested, deletion_completed,
        # ואז consent_given נוסף
        assert "consent_given" in types
        assert "deletion_completed" in types
        assert types.count("consent_given") == 2  # ראשון + חזרה
        # כל ה-hashes זהים — קישור היסטוריה
        hashes = {e["subject_hash"] for e in events}
        assert len(hashes) == 1, "כל הרשומות חייבות שיהיה להן אותו hash"

    def test_wa_opt_in_writes_marketing_event(self, db):
        db.upsert_user("wa1", channel="whatsapp")
        db.set_wa_marketing_opt_in("wa1", source="bot_button")
        events = self._events(db, "wa1", channel="whatsapp", event_type="opt_in_marketing")
        assert len(events) == 1
        import json
        meta = json.loads(events[0]["metadata_json"])
        assert meta.get("source") == "bot_button"

    def test_wa_opt_out_writes_marketing_event(self, db):
        db.upsert_user("wa2", channel="whatsapp")
        db.set_wa_marketing_opt_in("wa2")
        db.set_wa_opted_out("wa2")
        events = self._events(db, "wa2", channel="whatsapp")
        types = [e["event_type"] for e in events]
        assert "opt_in_marketing" in types
        assert "opt_out_marketing" in types

    def test_consent_superseded_on_version_bump(self, db, monkeypatch):
        """עליית CURRENT_CONSENT_VERSION → consent הבא נרשם כ-superseded."""
        db.record_consent("led6", channel="telegram")
        # מעלים את הגרסה הנוכחית
        monkeypatch.setattr(db, "CURRENT_CONSENT_VERSION", db.CURRENT_CONSENT_VERSION + 1)
        db.record_consent("led6", channel="telegram")
        events = self._events(db, "led6")
        types = [e["event_type"] for e in events]
        assert "consent_given" in types
        assert "consent_superseded" in types

    def test_ledger_purge_categories_have_different_retention(self, db):
        """consent: 5 שנים, audit: 24 חודשים. consent ישן מ-24 חודשים אך פחות
        מ-5 שנים — לא נמחק. audit באותו גיל — כן נמחק."""
        with db.get_connection() as conn:
            # consent בן 25 חודשים — נשאר (כי 5 שנים = 60 חודשים)
            conn.execute(
                "INSERT INTO consent_ledger (subject_hash, pepper_version, channel, "
                "category, event_type, event_at) "
                "VALUES (?, 'v1', 'telegram', 'consent', 'consent_given', "
                "datetime('now', '-25 months'))",
                ("hash_consent_keep",),
            )
            # audit בן 25 חודשים — נמחק
            conn.execute(
                "INSERT INTO consent_ledger (subject_hash, pepper_version, channel, "
                "category, event_type, event_at) "
                "VALUES (?, 'v1', 'telegram', 'audit', 'access_requested', "
                "datetime('now', '-25 months'))",
                ("hash_audit_purge",),
            )
            # consent עתיק (6 שנים) — נמחק
            conn.execute(
                "INSERT INTO consent_ledger (subject_hash, pepper_version, channel, "
                "category, event_type, event_at) "
                "VALUES (?, 'v1', 'telegram', 'consent', 'consent_given', "
                "datetime('now', '-72 months'))",
                ("hash_consent_old",),
            )
        db.purge_old_data()
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT subject_hash, category FROM consent_ledger"
            ).fetchall()
            remaining = {dict(r)["subject_hash"]: dict(r)["category"] for r in rows}
        assert "hash_consent_keep" in remaining       # 25 חודשים < 5 שנים → נשאר
        assert "hash_audit_purge" not in remaining    # 25 חודשים > 24 → נמחק
        assert "hash_consent_old" not in remaining    # 6 שנים > 5 → נמחק

    def test_ledger_silent_when_pepper_missing(self, db, monkeypatch):
        """אם LEDGER_PEPPER_V1 לא מוגדר — record_consent_event מחזיר False
        ולא זורק. הזכות עצמה (record_consent ב-DB) חייבת להמשיך לעבוד."""
        monkeypatch.delenv("LEDGER_PEPPER_V1", raising=False)
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_GIVEN
        result = record_consent_event(
            user_id="x", channel="telegram", event_type=EVENT_CONSENT_GIVEN,
        )
        assert result is False
        # ה-DB עצמו עדיין עובד
        db.record_consent("led7", channel="telegram")
        assert db.has_consent("led7")

    def test_mark_pepper_compromised_flags_records(self, db):
        """אחרי דליפת pepper — סימון compromised על כל הרשומות בגרסה הדלופה."""
        db.record_consent("led8", channel="telegram")
        from utils.consent_ledger import mark_pepper_compromised
        marked = mark_pepper_compromised("v1")
        assert marked >= 1
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT compromised FROM consent_ledger LIMIT 1"
            ).fetchone()
            assert dict(row)["compromised"] == 1


class TestDeletionFailureSemantics:
    """המלצת היועץ: deletion_completed עם status=full/partial, ו-event_type
    נפרד deletion_failed כשכלום לא נמחק. הפרדה כדי ששאילתה אינטואיטיבית
    `WHERE event_type='deletion_completed'` לא תיתן 100% הצלחה כשבפועל
    יש כשלים."""

    def _ledger_event(self, db, user_id, event_type):
        from utils.consent_ledger import get_events_for_subject
        events = get_events_for_subject(user_id, "telegram", event_type)
        return events[-1] if events else None

    def test_full_success_writes_status_full(self, db):
        db.upsert_user("del_full", channel="telegram")
        db.record_consent("del_full", channel="telegram")
        db.delete_user_data("del_full")
        evt = self._ledger_event(db, "del_full", "deletion_completed")
        assert evt is not None
        import json
        meta = json.loads(evt["metadata_json"])
        assert meta.get("status") == "full"

    def test_partial_failure_writes_status_partial(self, db):
        """כשל באמצע: חלק מהטבלאות נמחק, חלק נכשל — status=partial,
        failed_tables ב-metadata. מדמים כשל ע"י DROP TABLE."""
        db.upsert_user("del_partial", channel="telegram")
        db.record_consent("del_partial", channel="telegram")
        # מכניסים נתונים ל-conversations שיימחקו בהצלחה
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, role, message) VALUES (?, 'user', 'hi')",
                ("del_partial",),
            )
            # מפילים live_chats — DELETE יזרוק "no such table"
            conn.execute("DROP TABLE live_chats")

        db.delete_user_data("del_partial")
        evt = self._ledger_event(db, "del_partial", "deletion_completed")
        assert evt is not None
        import json
        meta = json.loads(evt["metadata_json"])
        assert meta.get("status") == "partial"
        assert "live_chats" in meta.get("failed_tables", [])
        # conversations כן נמחקה
        assert meta.get("counts", {}).get("conversations", 0) >= 1

    def test_total_failure_writes_deletion_failed_event(self, db):
        """כל ה-DELETEs נכשלים → event_type=deletion_failed (לא completed).
        מדמים ע"י DROP של כל הטבלאות שיש להן user_id."""
        db.upsert_user("del_fail", channel="telegram")
        db.record_consent("del_fail", channel="telegram")
        tables_to_drop = [
            "appointments", "conversations", "live_chats", "agent_requests",
            "user_subscriptions", "referral_codes", "user_notes",
            "user_identities", "conversation_summaries", "unanswered_questions",
            "credits", "lead_followups", "response_pages",
            "broadcast_message_recipients", "referrals",
            "broadcast_deliveries", "users",
        ]
        with db.get_connection() as conn:
            for t in tables_to_drop:
                try:
                    conn.execute(f"DROP TABLE {t}")
                except Exception:
                    pass  # אופציונלי — חלק מהטבלאות עשויות לא להתקיים

        db.delete_user_data("del_fail")

        # אין deletion_completed
        completed = self._ledger_event(db, "del_fail", "deletion_completed")
        assert completed is None
        # יש deletion_failed עם errors
        failed = self._ledger_event(db, "del_fail", "deletion_failed")
        assert failed is not None
        import json
        meta = json.loads(failed["metadata_json"])
        assert meta.get("counts") == {}
        assert meta.get("failed_tables")
        assert meta.get("errors")


class TestDeletionIdempotency:
    """idempotency check: שתי קריאות מקבילות ל-delete_user_data לא ייצרו
    שתי רשומות deletion_requested + שתי deletion_completed. השנייה
    מקבלת {'already_in_progress': True}."""

    def test_second_call_returns_already_in_progress(self, db):
        """כשקריאה ראשונה עדיין ב-cache, השנייה חוזרת מיד."""
        db.upsert_user("idem1", channel="telegram")
        # סימון ידני שהמחיקה בעיבוד (מדמה race)
        db._mark_deletion_in_progress("idem1")
        try:
            result = db.delete_user_data("idem1")
            assert result == {"already_in_progress": True}
        finally:
            db._clear_deletion_in_progress("idem1")

    def test_after_clear_second_call_works(self, db):
        """אחרי שהראשונה הסתיימה, קריאה חדשה כן עוברת."""
        db.upsert_user("idem2", channel="telegram")
        db.record_consent("idem2", channel="telegram")
        result1 = db.delete_user_data("idem2")
        assert "already_in_progress" not in result1
        # שנייה — הראשונה הסתיימה (cleared ב-finally), כן עוברת
        result2 = db.delete_user_data("idem2")
        assert "already_in_progress" not in result2

    def test_idempotency_ttl_expires(self, db, monkeypatch):
        """TTL פג → user_id ישן יוסר אוטומטית במקרה מפה גדולה."""
        import time
        # סימון ישן (מעבר ל-TTL)
        db._active_deletions["expired_user"] = time.time() - 120
        # _is_deletion_in_progress מנקה במהלך הבדיקה
        assert db._is_deletion_in_progress("expired_user") is False
        # ה-cleanup הסיר אותו
        assert "expired_user" not in db._active_deletions


class TestLedgerWriteRetry:
    """ledger_write_retry — טבלה אחת שמשרתת 3 תרחישים: pepper חסר,
    כשל DB בכתיבת ledger, וכשל מתוך delete_user_data. job יומי
    מנסה שוב; אחרי 5 ניסיונות נכתב [LEDGER_RETRY_EXHAUSTED] ל-log."""

    def _retry_count(self, db) -> int:
        with db.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM ledger_write_retry").fetchone()
            return int(dict(row)["c"])

    def test_missing_pepper_enqueues_to_retry(self, db, monkeypatch):
        """pepper חסר → רשומה נכנסת ל-retry במקום להיעלם."""
        monkeypatch.delenv("LEDGER_PEPPER_V1", raising=False)
        from utils import consent_ledger
        
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_GIVEN
        result = record_consent_event(
            user_id="retry1", channel="telegram", event_type=EVENT_CONSENT_GIVEN,
        )
        assert result is False
        assert self._retry_count(db) == 1

    def test_retry_succeeds_when_pepper_returns(self, db, monkeypatch):
        """pepper חסר → enqueue. pepper חוזר → process_ledger_retry_queue
        מצליח, מעביר ל-ledger ומוחק מה-retry."""
        # שלב 1: pepper חסר, נכשל
        monkeypatch.delenv("LEDGER_PEPPER_V1", raising=False)
        from utils import consent_ledger
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_GIVEN
        record_consent_event(
            user_id="retry2", channel="telegram", event_type=EVENT_CONSENT_GIVEN,
            consent_version=1,
        )
        assert self._retry_count(db) == 1

        # שלב 2: pepper חוזר, retry מצליח
        import secrets as _secrets
        monkeypatch.setenv("LEDGER_PEPPER_V1", _secrets.token_urlsafe(32))
        
        from utils.consent_ledger import process_ledger_retry_queue
        result = process_ledger_retry_queue()
        assert result["succeeded"] == 1
        assert self._retry_count(db) == 0  # נמחק מהתור

        # ליתר ביטחון — האירוע אכן נכתב ל-ledger
        events = consent_ledger.get_events_for_subject("retry2", "telegram")
        assert len(events) == 1
        assert events[0]["event_type"] == "consent_given"

    def test_retry_pepper_still_missing_does_not_increment_attempts(self, db, monkeypatch):
        """pepper עדיין חסר → לא מגדילים attempts (אחרת ניצול מהיר של 5 ניסיונות)."""
        monkeypatch.delenv("LEDGER_PEPPER_V1", raising=False)
        from utils import consent_ledger
        
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_GIVEN
        record_consent_event(
            user_id="retry3", channel="telegram", event_type=EVENT_CONSENT_GIVEN,
        )
        # שלוש פעמים — לא אמור להגדיל attempts
        from utils.consent_ledger import process_ledger_retry_queue
        for _ in range(3):
            process_ledger_retry_queue()
        with db.get_connection() as conn:
            row = conn.execute("SELECT attempts FROM ledger_write_retry").fetchone()
            assert dict(row)["attempts"] == 0

    def test_5_failures_marks_exhausted_with_log(self, db, monkeypatch, caplog):
        """כשל קבוע (לא pepper) → אחרי 5 ניסיונות, log [LEDGER_RETRY_EXHAUSTED]."""
        # מכניסים רשומה ידנית עם payload שיכשל (event_type לא קיים בסכמה
        # היא לא דרך טובה כי אנחנו בודקים EVENT_TYPE_CATEGORIES בכניסה).
        # במקום זה: נשבור את consent_ledger table כדי שכל ה-INSERT יכשלו.
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO ledger_write_retry (payload_json, attempts)
                   VALUES (?, 0)""",
                ('{"user_id":"x","channel":"telegram","event_type":"consent_given","consent_version":1,"metadata":{},"event_at":"2026-01-01 00:00:00"}',),
            )
            conn.execute("DROP TABLE consent_ledger")

        from utils.consent_ledger import process_ledger_retry_queue
        import logging
        with caplog.at_level(logging.ERROR):
            for _ in range(LEDGER_RETRY_MAX_ATTEMPTS_LOCAL := 5):
                process_ledger_retry_queue()
        # אחרי 5 ניסיונות — attempts = 5, ויש log עם prefix המובחן
        with db.get_connection() as conn:
            row = conn.execute("SELECT attempts FROM ledger_write_retry").fetchone()
            assert dict(row)["attempts"] >= 5
        assert any("[LEDGER_RETRY_EXHAUSTED]" in rec.message for rec in caplog.records)

    def test_retry_processed_in_purge_old_data(self, db, monkeypatch):
        """ה-job של retry רץ אוטומטית מתוך purge_old_data היומי."""
        # יצירת רשומת retry שתצליח (pepper תקין)
        monkeypatch.delenv("LEDGER_PEPPER_V1", raising=False)
        from utils import consent_ledger
        
        from utils.consent_ledger import record_consent_event, EVENT_CONSENT_GIVEN
        record_consent_event(
            user_id="retry5", channel="telegram", event_type=EVENT_CONSENT_GIVEN,
            consent_version=1,
        )
        assert self._retry_count(db) == 1

        # pepper חוזר, ואז קוראים ל-purge — הוא אמור גם להפעיל את ה-retry
        import secrets as _secrets
        monkeypatch.setenv("LEDGER_PEPPER_V1", _secrets.token_urlsafe(32))
        
        counts = db.purge_old_data()
        assert counts.get("ledger_retry_succeeded") == 1
        assert self._retry_count(db) == 0


class TestBlockedUsersAccessRights:
    """blocked_users restructure (תיקון 13): חשיפה חלקית בעיון —
    block_category + blocked_month + appeal_contact_method.
    block_reason_internal לא נחשף. /forget שומר רק מינימום לאכיפה."""

    def test_block_with_category_and_appeal(self, db):
        db.block_user(
            "blk_u1", username="Alice",
            reason="התנהגות פוגענית",
            category="abuse",
            appeal_contact="biz@example.com",
        )
        status = db.get_block_status_for_user("blk_u1")
        assert status is not None
        assert status["block_category"] == "abuse"
        assert status["appeal_contact_method"] == "biz@example.com"
        # blocked_month ברמת חודש בלבד (YYYY-MM, 7 תווים)
        assert len(status["blocked_month"]) == 7

    def test_block_status_does_not_expose_internal_reason(self, db):
        """get_block_status_for_user לא מחזיר את reason הפנימי."""
        db.block_user(
            "blk_u2", username="Bob",
            reason="הערות פנימיות שאסור לחשוף",
            category="manual",
        )
        status = db.get_block_status_for_user("blk_u2")
        # אין שדה reason / username / block_reason_internal בעיון
        assert "reason" not in status
        assert "username" not in status
        assert "block_reason_internal" not in status
        # ולוודא שהמחרוזת הפנימית לא דלפה דרך שדה אחר
        assert "אסור לחשוף" not in str(status)

    def test_invalid_category_falls_back_to_manual(self, db):
        """category לא חוקי (typo, ערך זדוני) → 'manual' אוטומטית."""
        db.block_user("blk_u3", category="not_a_real_category")
        status = db.get_block_status_for_user("blk_u3")
        assert status["block_category"] == "manual"

    def test_summary_exposes_block_status(self, db):
        """get_user_data_summary חושף blocked + block_status."""
        db.upsert_user("blk_u4")
        db.block_user(
            "blk_u4", category="spam",
            appeal_contact="appeal@biz.com",
        )
        summary = db.get_user_data_summary("blk_u4")
        assert summary["blocked"] is True
        assert summary["block_status"]["block_category"] == "spam"
        assert summary["block_status"]["appeal_contact_method"] == "appeal@biz.com"

    def test_summary_when_not_blocked(self, db):
        """משתמש לא חסום → blocked=False, block_status=None."""
        db.upsert_user("blk_u5")
        summary = db.get_user_data_summary("blk_u5")
        assert summary["blocked"] is False
        assert summary["block_status"] is None

    def test_forget_keeps_minimal_block_record(self, db):
        """/forget לא מוחק blocked_users — אבל מאפס PII (username, reason,
        block_reason_internal). category + blocked_at + appeal_contact נשמרים."""
        db.upsert_user("blk_u6", username="Charlie")
        db.block_user(
            "blk_u6", username="Charlie",
            reason="פרטים פנימיים פרטיים",
            category="abuse",
            appeal_contact="contact@biz.com",
        )
        db.delete_user_data("blk_u6")

        # ה-row עוד קיים
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM blocked_users WHERE user_id = ?",
                ("blk_u6",),
            ).fetchone()
            assert row is not None
            rd = dict(row)
            # PII אופס
            assert rd["username"] == ""
            assert rd["reason"] == ""
            assert rd["block_reason_internal"] == ""
            # שדות אכיפה נשארו
            assert rd["block_category"] == "abuse"
            assert rd["blocked_at"]
            assert rd["appeal_contact_method"] == "contact@biz.com"

        # החסימה עצמה עדיין פעילה — מנגנון אכיפה
        assert db.is_user_blocked("blk_u6") is True


class TestBugbotRegressions:
    """רגרסיות לבאגים שדווחו ע"י Cursor Bugbot. כל טסט מתעד באג ספציפי
    ומוודא שהתיקון מחזיק. אל תמחקו את הטסטים האלה גם אם נראה שהקוד
    נקי — הם הגנה מ-regression בעתיד."""

    # ─── Bug 1 (HIGH): encrypt_field crashes when key missing ───────────

    def test_encrypt_field_falls_back_to_plaintext_when_key_missing(self, monkeypatch):
        """ללא SECRETS_ENCRYPTION_KEY — encrypt_field מחזיר plaintext
        בלי prefix, לא קורס. תאימות עם הבטחת .env.example של legacy mode.
        """
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        from utils import crypto
        crypto._fernet_cache.clear()
        crypto._legacy_warning_logged = False  # reset כדי לראות את ה-warning
        from utils.crypto import encrypt_field, decrypt_field
        result = encrypt_field("plaintext_value")
        assert result == "plaintext_value"  # ללא prefix
        assert not result.startswith("v1:")
        # decrypt_field יודע לטפל בזה
        assert decrypt_field(result) == "plaintext_value"

    def test_save_calendar_credentials_works_without_encryption_key(self, db, monkeypatch):
        """save_google_calendar_credentials לא קורס בלי המפתח —
        רק שומר plaintext. אחרת deployments קיימים נשברים בעדכון."""
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        from utils import crypto
        crypto._fernet_cache.clear()

        db.save_google_calendar_credentials(
            google_account_email="biz@example.com",
            calendar_id="primary",
            refresh_token="REFRESH_TOKEN_PLAIN",
            access_token="ACCESS_TOKEN_PLAIN",
            token_expiry="2026-01-01T00:00:00Z",
            timezone="Asia/Jerusalem",
        )
        creds = db.get_google_calendar_credentials()
        assert creds is not None
        assert creds["refresh_token"] == "REFRESH_TOKEN_PLAIN"
        assert creds["access_token"] == "ACCESS_TOKEN_PLAIN"

    # ─── Bug 2 (HIGH): COALESCE with empty string causes premature delete ─

    def test_purge_does_not_delete_recent_records_with_empty_handled_at(self, db):
        """agent_request עם handled_at='' (נפוץ ב-legacy או ב-INSERT עם
        DEFAULT '') לא נמחק אם created_at חדש. בלי NULLIF, '' < datetime
        תמיד true ב-string compare — והרשומה נמחקת בטעות."""
        with db.get_connection() as conn:
            # רשומה חדשה (3 חודשים), עם handled_at='' (לא NULL)
            conn.execute(
                "INSERT INTO agent_requests (user_id, message, status, created_at, handled_at) "
                "VALUES (?, ?, 'pending', datetime('now', '-3 months'), '')",
                ("recent_empty_handled", "msg"),
            )
            # רשומה ישנה (15 חודשים) — צריכה להימחק
            conn.execute(
                "INSERT INTO agent_requests (user_id, message, status, created_at, handled_at) "
                "VALUES (?, ?, 'handled', datetime('now', '-15 months'), datetime('now', '-15 months'))",
                ("old_handled", "msg"),
            )
        db.purge_old_data()
        with db.get_connection() as conn:
            users = [
                dict(r)["user_id"]
                for r in conn.execute("SELECT user_id FROM agent_requests").fetchall()
            ]
        assert "recent_empty_handled" in users  # ❗ לא להמחק למרות handled_at=''
        assert "old_handled" not in users

    def test_purge_lead_followups_handles_empty_dates(self, db):
        """אותו עיקרון על lead_followups — ערכים '' לא יגרמו למחיקה
        מוקדמת של רשומות חדשות."""
        with db.get_connection() as conn:
            # חדש: כל ה-timestamps ריקים חוץ מ-created_at
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, channel, status, followup_due_at,
                        user_replied_at, followup_sent_at, created_at)
                   VALUES (?, 'telegram', 'pending', '', '', '',
                           datetime('now', '-2 months'))""",
                ("recent_lf",),
            )
            # ישן: created_at לפני 8 חודשים
            conn.execute(
                """INSERT INTO lead_followups
                       (user_id, channel, status, followup_due_at,
                        user_replied_at, followup_sent_at, created_at)
                   VALUES (?, 'telegram', 'expired',
                           datetime('now', '-8 months'),
                           '', '', datetime('now', '-8 months'))""",
                ("old_lf",),
            )
        db.purge_old_data()
        with db.get_connection() as conn:
            users = [
                dict(r)["user_id"]
                for r in conn.execute("SELECT user_id FROM lead_followups").fetchall()
            ]
        assert "recent_lf" in users
        assert "old_lf" not in users

    # ─── Bug 3 (MEDIUM): WhatsApp delete writes new PII before deletion ─

    def test_delete_result_does_not_collide_with_table_keys(self, db):
        """המפתחות שחושפים מטא (__failed_tables__, __deletion_status__)
        משתמשים ב-dunder prefix כדי לא להתנגש עם שמות טבלה (שמות בלי
        underscore כפול). _result_total_count אמור להתעלם מהם.
        """
        db.upsert_user("dunder_test", channel="telegram")
        db.record_consent("dunder_test", channel="telegram")
        result = db.delete_user_data("dunder_test")
        # _result_total_count לא מתפוצץ על dunder values שאינן int
        assert isinstance(db._result_total_count(result), int)
        # status helper מחזיר string תקף
        assert db.deletion_status(result) in {"full", "partial", "failed", "already_in_progress"}

    # ─── Bug 4 (MEDIUM): Partial failure detection ──────────────────────

    def test_partial_failure_exposed_in_return_value(self, db):
        """delete_user_data מחזיר __failed_tables__ במקרה של partial.
        בלי זה, ה-handler ב-WhatsApp לא יכל להבחין בין full ל-partial."""
        db.upsert_user("partial_test", channel="telegram")
        db.record_consent("partial_test", channel="telegram")
        # יוצרים נתונים אמיתיים בטבלה אחת + מפילים טבלה אחרת
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, role, message) VALUES (?, 'user', 'hi')",
                ("partial_test",),
            )
            conn.execute("DROP TABLE live_chats")

        result = db.delete_user_data("partial_test")
        assert "__failed_tables__" in result
        assert "live_chats" in result["__failed_tables__"]
        assert db.deletion_status(result) == "partial"

    def test_total_count_excludes_meta_keys(self, db):
        """_result_total_count מתעלם ממפתחות שמתחילים ב-__ ומערכים
        שאינם int. אחרת sum היה זורק TypeError על list."""
        # סימולציה ידנית של תוצאה עם מטא ושדות תקינים
        fake_result = {
            "users": 1,
            "conversations": 5,
            "__failed_tables__": ["live_chats"],
            "__deletion_status__": "partial",
        }
        assert db._result_total_count(fake_result) == 6

    # ─── Bug 5 (LOW): ICS rate-limit headers (covered indirectly) ───────

    def test_ics_rate_limit_response_applies_security_headers(self):
        """Bug 5 רגרסיה: מסלול /ics/ ה-429 קורא ל-_apply_public_page_security_headers.
        בדיקה ברמת source (Flask לא זמין בסביבת הטסטים).

        הקוד הקודם החזיר `app.make_response(("Too Many Requests", 429))`
        בלי להעביר דרך ה-helper, וזה הסיר את כל ה-headers (Cache-Control,
        X-Robots-Tag וכו'). התיקון מעביר את התשובה דרך helper משותף.
        """
        import os
        import re
        # שימוש בנתיב יחסי לקובץ הטסט (פועל גם ב-CI עם /home/runner/...)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        admin_app_path = os.path.join(repo_root, "admin", "app.py")
        with open(admin_app_path, encoding="utf-8") as f:
            source = f.read()

        # מוצאים את הבלוק של public_ics + ה-429 שלו
        match = re.search(
            r"def public_ics\(.*?\):(.*?)(?=\n    @app\.route|\Z)",
            source, re.DOTALL,
        )
        assert match is not None, "public_ics route לא נמצא"
        body = match.group(1)
        # ה-429 חייב להיות מועבר דרך ה-helper. בלוק רלוונטי מתחיל
        # אחרי `if _check_public_page_rate_limit`
        rate_limit_block = re.search(
            r"_check_public_page_rate_limit.*?return\s+(.*?)\n",
            body, re.DOTALL,
        )
        assert rate_limit_block is not None, "rate-limit branch לא נמצא"
        # ה-return צריך להיות עטוף ב-_apply_public_page_security_headers
        assert "_apply_public_page_security_headers" in rate_limit_block.group(0), (
            "Bug 5: 429 של /ics/ חייב לעבור דרך _apply_public_page_security_headers"
        )

    # ─── Bug 6 (MEDIUM): full failure shown as "no data exists" ─────────

    def test_total_failure_does_not_falsely_confirm_deletion(self, db):
        """באג: כשכל ה-DELETEs נכשלים → total=0 + status='failed'.
        הקוד הקודם נכנס ל-elif total==0 → הציג "אין מידע השמור עליך"
        (אישור שקרי שהמידע לא קיים). זו הפרת ציות.
        """
        db.upsert_user("fail_msg_test", channel="telegram")
        db.record_consent("fail_msg_test", channel="telegram")
        # מפילים את כל הטבלאות שhould להימחק → כל ה-DELETEs יכשלו
        tables_to_drop = [
            "appointments", "conversations", "live_chats", "agent_requests",
            "user_subscriptions", "referral_codes", "user_notes",
            "user_identities", "conversation_summaries", "unanswered_questions",
            "credits", "lead_followups", "response_pages",
            "broadcast_message_recipients", "referrals",
            "broadcast_deliveries", "users",
        ]
        with db.get_connection() as conn:
            for t in tables_to_drop:
                try:
                    conn.execute(f"DROP TABLE {t}")
                except Exception:
                    pass

        result = db.delete_user_data("fail_msg_test")
        # _result_total_count החזיר 0 (כלום לא נמחק)
        assert db._result_total_count(result) == 0
        # אבל status הוא "failed" — לא "full"!
        assert db.deletion_status(result) == "failed"
        # כל handler חיצוני (Telegram + WhatsApp) חייב להבדיל בין
        # status=failed (תקלה) ל-total==0+status=full (DB ריק לגיטימית).
        # לא לסמוך על total==0 לבד.

    def test_status_failed_takes_precedence_over_empty_count(self):
        """deletion_status מחזיר 'failed' גם אם counts ריק לחלוטין —
        כל קוד תצוגה חייב לבדוק status לפני total==0."""
        from database import deletion_status, _result_total_count
        # סימולציה של תוצאת כשל מלא: counts ריקים + מטא של failed
        fake_failed = {
            "__failed_tables__": ["users", "conversations"],
            "__deletion_status__": "failed",
        }
        assert deletion_status(fake_failed) == "failed"
        assert _result_total_count(fake_failed) == 0
        # סימולציה של DB ריק לגיטימית (counts=0, no failures)
        fake_empty = {}
        assert deletion_status(fake_empty) == "full"
        assert _result_total_count(fake_empty) == 0
        # שני המקרים מחזירים total=0, אבל deletion_status שונה — וזה
        # מה שמאפשר להבחין ביניהם בהודעת המשתמש.


class TestPIISanitizer:
    """utils/pii_sanitizer — שכבה 3 ב-developer_reports (תיקון 13).
    מסנן דפוסי טלפון ישראלי + מייל לפני שמירה ולפני שליחה למפתח."""

    def test_sanitize_israeli_mobile_with_dash(self):
        from utils.pii_sanitizer import sanitize_pii, PHONE_REDACTION
        result = sanitize_pii("התקשר אליי 050-1234567 בבקשה")
        assert PHONE_REDACTION in result.text
        assert "050-1234567" not in result.text
        assert result.phones_redacted == 1

    def test_sanitize_israeli_mobile_no_dash(self):
        from utils.pii_sanitizer import sanitize_pii, PHONE_REDACTION
        result = sanitize_pii("מספר 0501234567")
        assert PHONE_REDACTION in result.text
        assert result.phones_redacted == 1

    def test_sanitize_international_format(self):
        from utils.pii_sanitizer import sanitize_pii, PHONE_REDACTION
        result = sanitize_pii("WhatsApp: +972501234567 או +972-50-1234567")
        assert "+972501234567" not in result.text
        assert PHONE_REDACTION in result.text
        assert result.phones_redacted == 2

    def test_sanitize_email(self):
        from utils.pii_sanitizer import sanitize_pii, EMAIL_REDACTION
        result = sanitize_pii("פנו אליי alice@example.com")
        assert EMAIL_REDACTION in result.text
        assert "alice@example.com" not in result.text
        assert result.emails_redacted == 1

    def test_sanitize_multiple_pii_types(self):
        from utils.pii_sanitizer import sanitize_pii
        result = sanitize_pii(
            "הלקוח 0501234567 שלח מייל ל-bob@example.com עם 03-1234567"
        )
        assert result.phones_redacted == 2  # mobile + landline
        assert result.emails_redacted == 1
        assert result.changed

    def test_sanitize_preserves_clean_text(self):
        """טקסט בלי PII לא משתנה — לא false positives."""
        from utils.pii_sanitizer import sanitize_pii
        clean = "לחיצה על כפתור אישור התור לא עובדת בדף /appointments"
        result = sanitize_pii(clean)
        assert result.text == clean
        assert result.changed is False

    def test_has_pii_indicators_detects_phone(self):
        from utils.pii_sanitizer import has_pii_indicators
        assert has_pii_indicators("0501234567") is True
        assert has_pii_indicators("050-1234567") is True
        assert has_pii_indicators("+972501234567") is True

    def test_has_pii_indicators_detects_email(self):
        from utils.pii_sanitizer import has_pii_indicators
        assert has_pii_indicators("user@example.com") is True

    def test_has_pii_indicators_clean(self):
        from utils.pii_sanitizer import has_pii_indicators
        assert has_pii_indicators("בעיה בכפתור") is False
        assert has_pii_indicators("error 500 in /api/users") is False


class TestUserNotesAccessRights:
    """user_notes — לפי המלצת היועץ (תיקון 13). note_text נחשף בעיון
    משתמש לפי ברירת מחדל; tags סגורות לא נחשפות; withhold_reason מאפשר
    חריג נקודתי."""

    def test_save_and_get_note_full_structure(self, db):
        db.save_user_note("note_u1", "הופיע 10 דקות באיחור", tags=["late", "first_visit"])
        full = db.get_user_note_full("note_u1")
        assert full["note"] == "הופיע 10 דקות באיחור"
        assert "late" in full["tags"]
        assert "first_visit" in full["tags"]
        assert full["withhold_reason"] == ""

    def test_get_user_note_backward_compat(self, db):
        """get_user_note הישן עדיין מחזיר רק את הטקסט (string)."""
        db.save_user_note("note_u2", "פתק קצר", tags=["x"])
        assert db.get_user_note("note_u2") == "פתק קצר"

    def test_summary_exposes_note_text_by_default(self, db):
        """ברירת מחדל: ה-note_text נחשף ב-/myinfo."""
        db.upsert_user("note_u3")
        db.save_user_note("note_u3", "פתק לעיון")
        summary = db.get_user_data_summary("note_u3")
        assert summary["has_user_note"] is True
        assert summary["user_note_text"] == "פתק לעיון"
        assert summary.get("user_note_withheld") is False

    def test_summary_hides_note_when_withheld(self, db):
        """withhold_reason → המשתמש רואה רק שיש הערה, לא את התוכן."""
        db.upsert_user("note_u4")
        db.save_user_note(
            "note_u4", "פתק רגיש",
            withhold_reason="בקשה משפטית של עו\"ד",
        )
        summary = db.get_user_data_summary("note_u4")
        assert summary["has_user_note"] is True
        assert summary["user_note_text"] == ""  # לא נחשף
        assert summary["user_note_withheld"] is True

    def test_tags_never_exposed_in_summary(self, db):
        """tags הן מטא סגור — לא צריכות להופיע ב-summary בכלל."""
        db.upsert_user("note_u5")
        db.save_user_note("note_u5", "טקסט", tags=["סודי", "internal"])
        summary = db.get_user_data_summary("note_u5")
        # אין שדה tags ב-summary
        assert "tags" not in summary
        assert "user_note_tags" not in summary
        # והטקסטים הסודיים לא דולפים
        assert "סודי" not in summary.get("user_note_text", "")

    def test_save_empty_note_deletes_row(self, db):
        db.save_user_note("note_u6", "ראשוני")
        assert db.get_user_note("note_u6") == "ראשוני"
        db.save_user_note("note_u6", "")
        assert db.get_user_note("note_u6") == ""
        # הרשומה אכן נמחקה
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_notes WHERE user_id = ?", ("note_u6",)
            ).fetchone()
            assert row is None


class TestConsentDisclosures:
    """v2 של מסך ההסכמה — אזכור מפורש של עיבוד AI, העברה לחו"ל, וגיל 18+
    כתנאי להסכמה לשירות (לפי המלצת היועץ — 'רמה 1' כוללת אימות גיל)."""

    @pytest.fixture(autouse=True)
    def _enable_consent_flag(self, monkeypatch):
        """מפעיל את הפלאג כדי לבחון התנהגות הסכמה אמיתית — ברירת המחדל
        החדשה היא false ואז has_consent תמיד True."""
        monkeypatch.setattr("ai_chatbot.config.CONSENT_SCREEN_ENABLED", True)

    def test_consent_text_mentions_ai_processing(self, db):
        """ההסכמה חייבת להזכיר במפורש שהשיחה מעובדת ב-AI."""
        # mock של config — _consent_message_text דורש BUSINESS_NAME ו-ADMIN_URL
        from unittest.mock import patch
        with patch("config.BUSINESS_NAME", "TestBiz"), patch("config.ADMIN_URL", "https://example.com"):
            from bot.handlers import _consent_message_text
            text = _consent_message_text()
        assert "AI" in text or "בינה מלאכותית" in text
        assert "OpenAI" in text or "Google" in text

    def test_consent_text_mentions_us_servers(self, db):
        """ההסכמה חייבת להזכיר שהמידע עובר לשרתי AI בארה"ב."""
        from unittest.mock import patch
        with patch("config.BUSINESS_NAME", "TestBiz"), patch("config.ADMIN_URL", "https://example.com"):
            from bot.handlers import _consent_message_text
            text = _consent_message_text()
        assert "ארה" in text  # תופס "ארה\"ב"

    def test_consent_text_mentions_age_18(self, db):
        """גיל 18+ חייב להופיע במפורש."""
        from unittest.mock import patch
        with patch("config.BUSINESS_NAME", "TestBiz"), patch("config.ADMIN_URL", "https://example.com"):
            from bot.handlers import _consent_message_text
            text = _consent_message_text()
        assert "18" in text

    def test_consent_button_includes_age_attestation(self, db):
        """כפתור האישור חייב לכלול אישור גיל בטקסט (אקטיבי, לא פסיבי).
        ב-environment של הטסטים `telegram` ממוקה, אז בודקים את ה-call_args
        של InlineKeyboardButton במקום את האובייקט עצמו."""
        import telegram
        telegram.InlineKeyboardButton.reset_mock()
        from bot.handlers import _build_consent_keyboard
        _build_consent_keyboard()
        # אחת הקריאות חייבת לכלול "18" + "מסכים" בטקסט הכפתור
        calls = telegram.InlineKeyboardButton.call_args_list
        button_texts = [
            (call.args[0] if call.args else call.kwargs.get("text", ""))
            for call in calls
        ]
        joined = " | ".join(button_texts)
        assert "18" in joined, f"לא נמצא '18' בטקסט הכפתורים: {joined}"
        assert "מסכים" in joined

    def test_consent_version_bumped_to_2(self, db):
        """עליית גרסה כדי לאלץ re-prompt למשתמשים קיימים."""
        assert db.CURRENT_CONSENT_VERSION >= 2

    def test_existing_v1_users_must_reconsent(self, db):
        """משתמש שהסכים לגרסה 1 — has_consent יחזיר False אחרי bump."""
        # משתמש מדומה עם consent_version=1
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO users (user_id, consent_given_at, consent_version)
                   VALUES (?, datetime('now'), 1)""",
                ("v1_user",),
            )
        # אם הגרסה הנוכחית היא 2+, has_consent חייב להחזיר False
        assert db.has_consent("v1_user") is False

    def test_consent_v2_writes_superseded_event(self, db):
        """כש-v1 user נותן consent מחדש לגרסה 2 — נכתב consent_superseded ל-ledger
        (אופציה א של היועץ — קישור היסטוריה)."""
        # יוצרים משתמש עם consent v1
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO users (user_id, channel, consent_given_at, consent_version)
                   VALUES (?, 'telegram', datetime('now'), 1)""",
                ("supersede_user",),
            )
        # נותן consent מחדש (לגרסה הנוכחית, שהיא v2+)
        db.record_consent("supersede_user", channel="telegram")
        from utils.consent_ledger import get_events_for_subject
        events = get_events_for_subject("supersede_user", "telegram")
        types = [e["event_type"] for e in events]
        assert "consent_superseded" in types


class TestWhatsAppPrivacyRouter:
    """WhatsApp privacy router (תיקון 13). הפרדה ברורה משני סוגי ביטול:
        הסר → opt_out marketing בלבד (תיקון 40, מטופל ב-whatsapp_optout)
        מחק אותי → consent_revoked + delete_user_data + 2-step confirmation
        המידע שלי → access_requested + access_delivered

    כל הזיהוי לפני LLM/RAG. שלילה ("אל תמחק אותי") לא נחשבת בקשה.
    """

    def test_detect_delete_exact_match(self):
        from messaging.whatsapp_privacy import detect_delete_request
        assert detect_delete_request("מחק אותי") is True
        assert detect_delete_request("ביטול הסכמה") is True
        assert detect_delete_request("Delete me") is True

    def test_detect_delete_with_punctuation(self):
        from messaging.whatsapp_privacy import detect_delete_request
        assert detect_delete_request("!מחק אותי") is True
        assert detect_delete_request("מחק אותי!") is True
        assert detect_delete_request('"מחק אותי"') is True

    def test_detect_delete_substring_long_keyword(self):
        """keyword ארוך (>=8 תווים) זוהה גם כ-substring."""
        from messaging.whatsapp_privacy import detect_delete_request
        # "מחק את המידע שלי" מופיע בתוך משפט ארוך יותר
        assert detect_delete_request("בבקשה מחק את המידע שלי כבר") is True

    def test_detect_delete_negation_rejected(self):
        """'אל תמחק אותי' לא נחשב בקשת מחיקה."""
        from messaging.whatsapp_privacy import detect_delete_request
        # שלב 1: match מדויק לא יקרה (משפט מורחב)
        # שלב 2: substring יקרה ל-keyword "מחק את המידע שלי" — אבל
        # נבדוק שהשלילה תופסת על keywords אחרים
        assert detect_delete_request("אל תמחק את המידע שלי") is False

    def test_detect_delete_short_keyword_in_middle_rejected(self):
        """keyword קצר ('מחק') לא יקפוץ באמצע משפט (לא substring)."""
        from messaging.whatsapp_privacy import detect_delete_request
        # 'מחק' כקיצור מופיע בכל "תמחק לי את התור" — אסור שזה ייחשב delete
        assert detect_delete_request("תמחק לי את התור של מחר") is False

    def test_detect_access_exact_match(self):
        from messaging.whatsapp_privacy import detect_access_request
        assert detect_access_request("המידע שלי") is True
        assert detect_access_request("מה אתם יודעים עליי") is True

    def test_detect_delete_confirmation_exact_only(self):
        """ביטוי האישור חייב להיות מדויק — שלא ייחשב באמצע משפט."""
        from messaging.whatsapp_privacy import detect_delete_confirmation
        assert detect_delete_confirmation("אישור מחיקה") is True
        assert detect_delete_confirmation("אישור מחיקה ") is True  # רווח בסוף — strip
        assert detect_delete_confirmation("רוצה אישור מחיקה") is False
        assert detect_delete_confirmation("מחק") is False

    def test_pending_delete_register_and_check(self):
        from messaging import whatsapp_privacy as wp
        # ניקוי לפני בדיקה (cache משותף בין טסטים)
        wp.clear_pending_delete("wa_user1")
        assert wp.is_pending_delete("wa_user1") is False
        wp.register_pending_delete("wa_user1")
        assert wp.is_pending_delete("wa_user1") is True
        wp.clear_pending_delete("wa_user1")
        assert wp.is_pending_delete("wa_user1") is False

    def test_pending_delete_ttl_expires(self):
        """TTL פג → cleanup אוטומטי ב-is_pending_delete."""
        from messaging import whatsapp_privacy as wp
        import time
        # הוספה ידנית עם timestamp ישן (מעבר ל-TTL)
        with wp._pending_deletes_lock:
            wp._pending_deletes["expired_wa"] = time.time() - 700  # > 600
        assert wp.is_pending_delete("expired_wa") is False
        # ה-cleanup הסיר אותו
        with wp._pending_deletes_lock:
            assert "expired_wa" not in wp._pending_deletes

    def test_format_access_summary_no_data(self):
        from messaging.whatsapp_privacy import format_access_summary
        text = format_access_summary({"exists": False})
        assert "לא מצאנו מידע" in text

    def test_format_access_summary_with_data(self):
        from messaging.whatsapp_privacy import format_access_summary
        text = format_access_summary({
            "exists": True,
            "username": "Alice",
            "first_seen_at": "2026-01-01",
            "consent_given_at": "2026-01-01",
            "appointments": {"total": 3, "by_status": {}},
            "conversations_total": 10,
            "lead_followups": {"total": 2, "by_status": {}},
            "subscribed": True,
            "broadcast_deliveries_total": 1,
            "has_user_note": False,
        })
        assert "Alice" in text
        assert "תורים: 3" in text
        assert "הודעות בשיחות: 10" in text
        assert "ניתוחי AI" in text
        assert "מחק אותי" in text  # הוראת מחיקה בסוף

    def test_build_delete_warning_with_link(self):
        from messaging.whatsapp_privacy import build_delete_warning
        text = build_delete_warning("https://example.com/legal/privacy")
        assert "5 שנים" in text  # תקופת השמירה
        assert "https://example.com/legal/privacy" in text
        assert "זיהוי טכני סגור" in text  # פסאודונימיזציה במונחים פשוטים

    def test_build_delete_warning_without_link(self):
        from messaging.whatsapp_privacy import build_delete_warning
        text = build_delete_warning("")
        assert "5 שנים" in text
        assert "https" not in text  # אין קישור


class TestLegalPagesSanitization:
    """רגרסיה: דפי /legal/terms ו-/legal/privacy ציבוריים — לא ניתן להזריק
    דרכם raw HTML גם אם משהו נדבק לקבצי docs/legal."""

    def _render_with_content(self, content: str) -> str:
        """מדמה את הלוגיקה של _render_legal_doc עם קלט נתון."""
        import re
        sanitized = re.sub(r"<[^>]+>", "", content)
        try:
            from markdown import markdown
            return markdown(sanitized, extensions=["tables", "fenced_code"])
        except ImportError:
            from markupsafe import escape
            return f"<pre>{escape(sanitized)}</pre>"

    def test_script_tag_in_markdown_is_stripped(self):
        """תגיות script לא יוצאות לפלט — גם אם יש אותן ב-md, הן הופכות לטקסט."""
        content = "# כותרת\n\nטקסט רגיל <script>alert('xss')</script> וסיום."
        out = self._render_with_content(content)
        # הקריטי: אין תגית script (ולכן אין הרצה של JS)
        assert "<script>" not in out
        assert "</script>" not in out
        # התוכן הפנימי ("alert('xss')") עשוי להישאר כטקסט — זה לא הרצה,
        # רק תצוגה תמימה של מחרוזת.

    def test_iframe_and_event_handlers_stripped(self):
        content = '<iframe src="evil"></iframe><img src=x onerror=alert(1)>'
        out = self._render_with_content(content)
        assert "<iframe" not in out
        assert "onerror" not in out

    def test_legitimate_markdown_still_works(self):
        """אם markdown מותקן — header/list/bold הופכים ל-HTML תקין.
        אם לא — מדלגים על הטסט (production: markdown ב-requirements.txt)."""
        pytest.importorskip("markdown")
        content = "# כותרת\n\n**bold** וטקסט.\n\n- פריט אחד\n- פריט שני"
        out = self._render_with_content(content)
        assert "<h1>" in out
        assert "<strong>bold</strong>" in out
        assert "<li>" in out
