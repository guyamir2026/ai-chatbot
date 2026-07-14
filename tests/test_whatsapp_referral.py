"""
טסטים לזיהוי קוד הפניה (REF_XXX) ב-WhatsApp webhook.

מקבילה ל-Telegram /start REF_XXX deep-link. הקוד מגיע כטקסט מוכן
מתוך wa.me link וצריך להירשם דרך register_referral.
"""

from unittest.mock import patch, MagicMock

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


def _import_handler(db_module):
    """Import the handler with database patched to the test DB module."""
    with patch("messaging.whatsapp_webhook.db", db_module):
        from messaging.whatsapp_webhook import _maybe_handle_referral_code
        return _maybe_handle_referral_code


class TestReferralCodeDetection:
    def test_non_ref_text_returns_false(self, db):
        from messaging import whatsapp_webhook
        with patch.object(whatsapp_webhook, "db", db):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                "+972500000001", "Alice", "שלום, רוצה תור",
            )
        assert handled is False

    def test_empty_body_returns_false(self, db):
        from messaging import whatsapp_webhook
        with patch.object(whatsapp_webhook, "db", db):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                "+972500000001", "Alice", "",
            )
        assert handled is False

    def test_ref_in_middle_ignored(self, db):
        """REF_ באמצע טקסט (לא בתחילתו) — לא קוד הפניה."""
        from messaging import whatsapp_webhook
        with patch.object(whatsapp_webhook, "db", db):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                "+972500000001", "Alice", "אני מעוניין ב REF_XXX לקבל",
            )
        assert handled is False

    def test_valid_ref_registers_referral(self, db):
        """קוד תקף → רישום + הודעת ברוכים הבאים עם בונוס + הרשמה לשידורים."""
        # יצירת מפנה (referrer) עם קוד
        referrer_id = "+972500000999"
        db.upsert_user(referrer_id, "Bob", channel="whatsapp")
        code = db.generate_referral_code(referrer_id)
        assert code.startswith("REF_")

        from messaging import whatsapp_webhook
        sent = []
        with patch.object(whatsapp_webhook, "db", db), \
             patch.object(whatsapp_webhook, "_send_whatsapp_response",
                          side_effect=lambda to, text: sent.append((to, text))):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                "+972500000001", "Alice", code,
            )

        assert handled is True
        assert len(sent) == 1
        to, text = sent[0]
        assert to == "+972500000001"
        assert "הגעתם דרך הפניה" in text
        # רשומת ההפניה נוצרה
        ref_row = db.get_referral_by_code(code)
        assert ref_row is not None
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT referred_id FROM referrals WHERE code = ?", (code,)
            ).fetchone()
            assert row["referred_id"] == "+972500000001"
            # הרשמה לשידורים — מקבילה ל-/start בטלגרם
            sub_row = conn.execute(
                "SELECT user_id FROM user_subscriptions WHERE user_id = ?",
                ("+972500000001",),
            ).fetchone()
            assert sub_row is not None

    def test_invalid_code_sends_generic_welcome(self, db):
        """קוד שלא קיים — לא קורסים, שולחים welcome גנרי."""
        from messaging import whatsapp_webhook
        sent = []
        with patch.object(whatsapp_webhook, "db", db), \
             patch.object(whatsapp_webhook, "_send_whatsapp_response",
                          side_effect=lambda to, text: sent.append((to, text))):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                "+972500000001", "Alice", "REF_DOESNOTEXIST",
            )

        assert handled is True
        assert len(sent) == 1
        _to, text = sent[0]
        assert "ברוכים הבאים" in text
        assert "הגעתם דרך הפניה" not in text  # לא להבטיח בונוס שלא קיים

    def test_self_referral_blocked(self, db):
        """משתמש לא יכול להפנות את עצמו — register_referral מחזיר False."""
        user_id = "+972500000001"
        db.upsert_user(user_id, "Alice", channel="whatsapp")
        code = db.generate_referral_code(user_id)

        from messaging import whatsapp_webhook
        sent = []
        with patch.object(whatsapp_webhook, "db", db), \
             patch.object(whatsapp_webhook, "_send_whatsapp_response",
                          side_effect=lambda to, text: sent.append((to, text))):
            handled = whatsapp_webhook._maybe_handle_referral_code(
                user_id, "Alice", code,
            )

        assert handled is True
        # הודעת welcome גנרית — לא מבטיחים בונוס
        assert "הגעתם דרך הפניה" not in sent[0][1]
