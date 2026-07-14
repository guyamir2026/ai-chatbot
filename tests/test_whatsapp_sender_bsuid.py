"""
טסטים ל-messaging/whatsapp_sender.py — שליחה ל-BSUID ול-טלפון.

מטרה: לוודא שמשתמש BSUID-only (ללא טלפון ב-DB) מקבל הודעה דרך
to=whatsapp:CC.BSUID, ושמשתמש עם טלפון מקבל קודם דרך הטלפון
(לפי המלצת Meta).
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def sender_db(db_conn, monkeypatch):
    """db_conn + ניטרול DEMO_MODE כדי שהשליחה תגיע לשלב Twilio."""
    import messaging.whatsapp_sender as sender_mod
    # DEMO_MODE נקרא בתוך הפונקציה דרך import של config — patch על המקור.
    monkeypatch.setattr("ai_chatbot.config.DEMO_MODE", False, raising=False)
    monkeypatch.setattr("config.DEMO_MODE", False, raising=False)
    monkeypatch.setattr("ai_chatbot.config.TWILIO_WHATSAPP_NUMBER", "+14155551234", raising=False)
    monkeypatch.setattr("config.TWILIO_WHATSAPP_NUMBER", "+14155551234", raising=False)
    # איפוס ה-singleton של Twilio Client כדי לא לזלוג בין טסטים
    sender_mod._twilio_client = None
    yield db_conn
    sender_mod._twilio_client = None


def _mock_twilio_client():
    """יוצר Twilio Client mock שמתעד את הקריאה ל-messages.create."""
    client = MagicMock()
    sent_message = MagicMock()
    sent_message.sid = "SM_test_sid"
    sent_message.status = "queued"
    sent_message.num_segments = "1"
    client.messages.create.return_value = sent_message
    return client


class TestSendWhatsappBSUID:
    """שליחת WhatsApp עבור משתמשי BSUID — לפי הכלל "BSUID-only fallback"."""

    def test_send_to_bsuid_only_user(self, sender_db):
        """משתמש BSUID-only (אין טלפון ב-DB) — נשלח אל to=whatsapp:IL.abc..."""
        from messaging.whatsapp_sender import send_whatsapp
        from utils.user_identity import resolve_whatsapp_user

        # יצירת משתמש BSUID-only
        user_id = resolve_whatsapp_user("", bsuid="IL.OnlyBsuidUser1")
        assert user_id == "IL.OnlyBsuidUser1"

        mock_client = _mock_twilio_client()
        with patch("messaging.whatsapp_sender._get_twilio_client", return_value=mock_client):
            send_whatsapp(user_id, "שלום")

        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["to"] == "whatsapp:IL.OnlyBsuidUser1"
        assert call_kwargs["from_"] == "whatsapp:+14155551234"

    def test_send_to_phone_user_with_bsuid_attached(self, sender_db):
        """משתמש עם טלפון + BSUID — נשלח אל הטלפון (העדפה ל-phone)."""
        from messaging.whatsapp_sender import send_whatsapp
        from utils.user_identity import resolve_whatsapp_user

        # יצירת משתמש עם שניהם — user_id = phone (תאימות לאחור)
        user_id = resolve_whatsapp_user("+972503334444", bsuid="IL.AttachedBsuid2")
        assert user_id == "+972503334444"

        mock_client = _mock_twilio_client()
        with patch("messaging.whatsapp_sender._get_twilio_client", return_value=mock_client):
            send_whatsapp(user_id, "היי")

        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["to"] == "whatsapp:+972503334444"

    def test_send_to_bsuid_user_id_with_phone_in_db(self, sender_db):
        """user_id הוא BSUID, אבל יש phone ב-user_identities → reverse lookup לטלפון."""
        from messaging.whatsapp_sender import send_whatsapp
        from database import upsert_user_identity

        # מצב נדיר אבל אפשרי — user_id נשמר כ-BSUID אבל יש לנו טלפון מאוחר יותר
        upsert_user_identity(
            "IL.BsuidWithPhone3", "whatsapp",
            whatsapp_bsuid="IL.BsuidWithPhone3",
            phone_number="+972505556666",
        )

        mock_client = _mock_twilio_client()
        with patch("messaging.whatsapp_sender._get_twilio_client", return_value=mock_client):
            send_whatsapp("IL.BsuidWithPhone3", "בדיקה")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        # reverse lookup מצא טלפון — שולחים אליו ולא ל-BSUID
        assert call_kwargs["to"] == "whatsapp:+972505556666"
