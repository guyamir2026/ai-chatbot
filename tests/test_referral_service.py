"""
טסטים ל-referral_service — תמיכה בשני ערוצים (Telegram + WhatsApp).

מכסים:
- build_referral_link: לפי ערוץ + fallback כשהקונפיג חסר
- get_referral_message_text: הלינק הנכון לכל ערוץ
- try_send_referral_code: מעביר channel ל-text builder ול-send_fn
"""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def db(tmp_path):
    """DB זמני — גם ל-init וגם לפעולות referral."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        # מערכת ההפניות חייבת להיות פעילה ל-try_send_referral_code
        database.update_bot_settings(
            tone="friendly",
            referral_enabled=1,
            referral_discount=10.0,
            referral_validity_days=60,
        )
        yield database


# ── build_referral_link ──────────────────────────────────────────────────────


class TestBuildReferralLink:
    def test_telegram_returns_deep_link(self):
        import referral_service
        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", "MyBot"):
            link = referral_service.build_referral_link("REF_ABC123", channel="telegram")
        assert link == "https://t.me/MyBot?start=REF_ABC123"

    def test_telegram_default_channel(self):
        """ברירת מחדל ללא channel — Telegram (תאימות לאחור)."""
        import referral_service
        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", "MyBot"):
            link = referral_service.build_referral_link("REF_ABC123")
        assert link == "https://t.me/MyBot?start=REF_ABC123"

    def test_telegram_fallback_to_code_when_username_missing(self):
        import referral_service
        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", ""):
            link = referral_service.build_referral_link("REF_ABC123", channel="telegram")
        assert link == "REF_ABC123"

    def test_whatsapp_returns_wa_me_with_code(self):
        import referral_service
        with patch.object(referral_service, "TWILIO_WHATSAPP_NUMBER", "+972501234567"):
            link = referral_service.build_referral_link("REF_ABC123", channel="whatsapp")
        # wa.me דורש digits בלבד; הקוד url-encoded
        assert link == "https://wa.me/972501234567?text=REF_ABC123"

    def test_whatsapp_strips_non_digits(self):
        """המספר עשוי להכיל +, רווחים או מקפים — wa.me דורש digits בלבד."""
        import referral_service
        with patch.object(referral_service, "TWILIO_WHATSAPP_NUMBER", "+972 50-123-4567"):
            link = referral_service.build_referral_link("REF_ABC123", channel="whatsapp")
        assert link == "https://wa.me/972501234567?text=REF_ABC123"

    def test_whatsapp_fallback_to_code_when_number_missing(self):
        import referral_service
        with patch.object(referral_service, "TWILIO_WHATSAPP_NUMBER", ""):
            link = referral_service.build_referral_link("REF_ABC123", channel="whatsapp")
        assert link == "REF_ABC123"


# ── get_referral_message_text ────────────────────────────────────────────────


class TestGetReferralMessageText:
    def test_telegram_includes_t_me_link(self, db):
        import referral_service
        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", "MyBot"):
            text = referral_service.get_referral_message_text("REF_X", channel="telegram")
        assert "https://t.me/MyBot?start=REF_X" in text

    def test_whatsapp_includes_wa_me_link(self, db):
        import referral_service
        with patch.object(referral_service, "TWILIO_WHATSAPP_NUMBER", "+972501234567"):
            text = referral_service.get_referral_message_text("REF_X", channel="whatsapp")
        assert "https://wa.me/972501234567?text=REF_X" in text
        # ושאין דליפה של לינק טלגרם
        assert "t.me/" not in text

    def test_text_includes_discount_and_period(self, db):
        import referral_service
        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", "MyBot"):
            text = referral_service.get_referral_message_text("REF_X", channel="telegram")
        assert "10%" in text
        assert "לחודשיים" in text  # 60 ימים → "לחודשיים"


# ── try_send_referral_code ───────────────────────────────────────────────────


class TestTrySendReferralCode:
    def test_telegram_send_fn_receives_t_me_text(self, db):
        import referral_service
        captured: dict = {}

        def fake_send(text):
            captured["text"] = text
            return True

        with patch.object(referral_service, "TELEGRAM_BOT_USERNAME", "MyBot"):
            ok = referral_service.try_send_referral_code(
                "user_tg", send_fn=fake_send, channel="telegram",
            )
        assert ok is True
        assert "https://t.me/MyBot?start=REF_" in captured["text"]

    def test_whatsapp_send_fn_receives_wa_me_text(self, db):
        import referral_service
        captured: dict = {}

        def fake_send(text):
            captured["text"] = text
            return True

        with patch.object(referral_service, "TWILIO_WHATSAPP_NUMBER", "+972501234567"):
            ok = referral_service.try_send_referral_code(
                "user_wa", send_fn=fake_send, channel="whatsapp",
            )
        assert ok is True
        assert "https://wa.me/972501234567?text=REF_" in captured["text"]
        assert "t.me/" not in captured["text"]

    def test_unmark_on_send_failure(self, db):
        """כשלון שליחה — הדגל מתאפס לניסיון חוזר."""
        import referral_service
        ok = referral_service.try_send_referral_code(
            "user_wa", send_fn=lambda _t: False, channel="whatsapp",
        )
        assert ok is False
        # ניסיון חוזר אמור להצליח (הדגל התאפס)
        ok2 = referral_service.try_send_referral_code(
            "user_wa", send_fn=lambda _t: True, channel="whatsapp",
        )
        assert ok2 is True

    def test_disabled_returns_false(self, db):
        import referral_service
        db.update_bot_settings(tone="friendly", referral_enabled=0)
        ok = referral_service.try_send_referral_code(
            "u", send_fn=lambda _t: True, channel="whatsapp",
        )
        assert ok is False
