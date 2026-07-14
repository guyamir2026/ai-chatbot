"""
טסטים על נתיב ההתראה לבעל העסק כשמתבצע handoff בערוצי מטא.

מכסה:
- result.action='request_agent' → יצירת agent_request + התראה בטלגרם.
- result.action='handoff_to_human' → אותו דבר.
- שאר actions → אין התראה.
- channel_label תקין (Instagram DM / Facebook Messenger).
- אין TELEGRAM_OWNER_CHAT_ID → לוג אזהרה, לא שגיאה.
- send_message_by_channel — ניתוב נכון של ערוצי מטא.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patch_pipeline(monkeypatch):
    """מוקה את התלויות של _handle_meta_message + _handle_meta_agent_request."""
    import sys
    import ai_chatbot
    import ai_chatbot.database

    db_mock = MagicMock()
    db_mock.get_consecutive_fallbacks.return_value = 0
    db_mock.create_agent_request.return_value = 42
    db_mock.get_username_for_user.return_value = "דנה"
    monkeypatch.setattr(ai_chatbot, "database", db_mock)
    monkeypatch.setitem(sys.modules, "ai_chatbot.database", db_mock)

    import live_chat_service as lcs
    monkeypatch.setattr(lcs.LiveChatService, "is_active", MagicMock(return_value=False))

    # ההתראה לבעל העסק עוברת דרך helper יחיד ב-meta_webhook —
    # נקודת patch אחת, אין צורך לעקוף wrapper של ai_chatbot.live_chat_service.
    import messaging.meta_webhook as mw
    send_tg = MagicMock(return_value=True)
    monkeypatch.setattr(mw, "_notify_owner_telegram", send_tg)

    import core.message_processor as mp
    fake_result = MagicMock()
    fake_result.text = "אעביר את הפנייה לבעל העסק"
    fake_result.intent = None
    fake_result.action = "request_agent"
    fake_result.agent_request_message = "לקוח שואל על מחיר מיוחד"
    fake_result.handoff_reason = ""
    fake_result.consecutive_fallbacks = 0
    monkeypatch.setattr(mp, "process_incoming_message", MagicMock(return_value=fake_result))

    import messaging.meta_webhook as mw
    monkeypatch.setattr(mw, "_send_meta_response", MagicMock())

    # מוודאים שיש TELEGRAM_OWNER_CHAT_ID כברירת מחדל
    monkeypatch.setattr("ai_chatbot.config.TELEGRAM_OWNER_CHAT_ID", "999000")
    monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "https://admin.example.com")

    return {"db": db_mock, "send_tg": send_tg, "result": fake_result}


def _msg(channel="meta_ig", sender="IGSID_A", entry="IGBA_X", text="שלום"):
    return {
        "channel": channel,
        "sender_id": sender,
        "page_or_ig_id": entry,
        "recipient_id": "REC",
        "timestamp_ms": 1700000000000,
        "mid": "MID_1",
        "text": text,
        "has_attachments": False,
    }


class TestHandoffNotification:
    def test_request_agent_creates_db_entry_and_notifies_telegram(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_msg())

        # agent_request נוצר עם channel נכון ומסר נכון
        patch_pipeline["db"].create_agent_request.assert_called_once()
        kwargs = patch_pipeline["db"].create_agent_request.call_args.kwargs
        assert kwargs["channel"] == "meta_ig"
        assert kwargs["user_id"] == "meta_ig:IGSID_A"
        assert "לקוח שואל על מחיר מיוחד" in kwargs["message"]

        # ההתראה נשלחת לבעל העסק בטלגרם
        patch_pipeline["send_tg"].assert_called_once()
        args = patch_pipeline["send_tg"].call_args.args
        assert args[0] == "999000"
        notification = args[1]
        assert "Instagram DM" in notification
        assert "#42" in notification
        assert "https://admin.example.com/requests" in notification

    def test_handoff_to_human_also_triggers_notification(self, patch_pipeline):
        patch_pipeline["result"].action = "handoff_to_human"
        patch_pipeline["result"].agent_request_message = ""
        patch_pipeline["result"].handoff_reason = "שאלה מורכבת"

        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_msg())

        patch_pipeline["db"].create_agent_request.assert_called_once()
        kwargs = patch_pipeline["db"].create_agent_request.call_args.kwargs
        assert "שאלה מורכבת" in kwargs["message"]
        patch_pipeline["send_tg"].assert_called_once()

    def test_messenger_channel_label(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_msg(channel="meta_msg", sender="PSID_B", entry="PAGE_1"))
        notif = patch_pipeline["send_tg"].call_args.args[1]
        assert "Facebook Messenger" in notif
        assert "Instagram" not in notif

    def test_no_action_no_notification(self, patch_pipeline):
        """ברירת מחדל — RAG עבר חלק, אין handoff, אין התראה."""
        patch_pipeline["result"].action = ""
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_msg())
        patch_pipeline["db"].create_agent_request.assert_not_called()
        patch_pipeline["send_tg"].assert_not_called()

    def test_no_telegram_owner_logs_warning(self, patch_pipeline, monkeypatch, caplog):
        """בלי TELEGRAM_OWNER_CHAT_ID — agent_request נוצר, אבל אין שליחה.
        זו לא שגיאה אלא warning (deployment חסר תצורה)."""
        monkeypatch.setattr("ai_chatbot.config.TELEGRAM_OWNER_CHAT_ID", "")
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_msg())
        patch_pipeline["db"].create_agent_request.assert_called_once()
        patch_pipeline["send_tg"].assert_not_called()
        assert "לא יקבל התראה" in caplog.text


class TestSendMessageByChannelRouting:
    """send_message_by_channel חייב לנתב נכון לפי channel."""

    def test_meta_ig_routes_to_meta_sender(self, monkeypatch):
        import live_chat_service as lcs
        meta_send = MagicMock(return_value=True)
        monkeypatch.setattr(lcs, "send_meta_message_by_user_id", meta_send)
        tg_send = MagicMock()
        wa_send = MagicMock()
        monkeypatch.setattr(lcs, "send_telegram_message", tg_send)
        monkeypatch.setattr(lcs, "send_whatsapp_message", wa_send)

        assert lcs.send_message_by_channel("meta_ig:X", "hi", channel="meta_ig")
        meta_send.assert_called_once_with("meta_ig:X", "hi")
        tg_send.assert_not_called()
        wa_send.assert_not_called()

    def test_meta_msg_routes_to_meta_sender(self, monkeypatch):
        import live_chat_service as lcs
        meta_send = MagicMock(return_value=True)
        monkeypatch.setattr(lcs, "send_meta_message_by_user_id", meta_send)
        lcs.send_message_by_channel("meta_msg:Y", "x", channel="meta_msg")
        meta_send.assert_called_once_with("meta_msg:Y", "x")

    def test_telegram_unchanged(self, monkeypatch):
        import live_chat_service as lcs
        tg_send = MagicMock(return_value=True)
        monkeypatch.setattr(lcs, "send_telegram_message", tg_send)
        lcs.send_message_by_channel("12345", "x", channel="telegram")
        tg_send.assert_called_once_with("12345", "x")

    def test_whatsapp_unchanged(self, monkeypatch):
        import live_chat_service as lcs
        wa_send = MagicMock(return_value=True)
        monkeypatch.setattr(lcs, "send_whatsapp_message", wa_send)
        lcs.send_message_by_channel("+972501234567", "x", channel="whatsapp")
        wa_send.assert_called_once_with("+972501234567", "x")


class TestSendMetaMessageByUserId:
    """send_meta_message_by_user_id שולף asset/credentials נכון."""

    @pytest.fixture
    def patch_meta_send(self, monkeypatch):
        import sys
        import ai_chatbot
        import ai_chatbot.database
        db_mock = MagicMock()
        db_mock.get_user_provider_info.return_value = {
            "channel": "meta_ig",
            "provider_asset_id": "IGBA_1",
            "external_user_id": "IGSID_X",
        }
        db_mock.get_meta_credentials_by_ig_account.return_value = {
            "page_id": "PAGE_1",
            "access_token": "tok-1",
            "ig_business_account_id": "IGBA_1",
        }
        db_mock.get_meta_credentials_by_page_id.return_value = {
            "page_id": "PAGE_1",
            "access_token": "tok-1",
        }
        monkeypatch.setattr(ai_chatbot, "database", db_mock)
        monkeypatch.setitem(sys.modules, "ai_chatbot.database", db_mock)

        import messaging.meta_sender as ms
        send = MagicMock(return_value="MID")
        monkeypatch.setattr(ms, "send_meta_message", send)
        return {"db": db_mock, "send": send}

    def test_ig_uses_igba_lookup(self, patch_meta_send):
        import live_chat_service as lcs
        # database fixture נטען דרך ai_chatbot, אבל lcs מייבא ב-top
        # level דרך `from ai_chatbot import database as db`. הוא כבר
        # ייובא — צריך לעקוף גם אותו.
        import live_chat_service as lcs_mod
        from unittest.mock import patch as mock_patch
        with mock_patch.object(lcs_mod, "db", patch_meta_send["db"]):
            ok = lcs_mod.send_meta_message_by_user_id("meta_ig:IGSID_X", "שלום")
        assert ok is True
        patch_meta_send["db"].get_meta_credentials_by_ig_account.assert_called_once_with("IGBA_1")
        # send_meta_message קיבל את ה-recipient הטהור
        args = patch_meta_send["send"].call_args.args
        assert args[0] == "IGSID_X"
        assert args[2] == "tok-1"

    def test_missing_user_returns_false(self, patch_meta_send):
        patch_meta_send["db"].get_user_provider_info.return_value = None
        import live_chat_service as lcs_mod
        from unittest.mock import patch as mock_patch
        with mock_patch.object(lcs_mod, "db", patch_meta_send["db"]):
            ok = lcs_mod.send_meta_message_by_user_id("meta_ig:NEW", "x")
        assert ok is False
        patch_meta_send["send"].assert_not_called()

    def test_missing_asset_id_returns_false(self, patch_meta_send):
        patch_meta_send["db"].get_user_provider_info.return_value = {
            "channel": "meta_ig", "provider_asset_id": "", "external_user_id": "X",
        }
        import live_chat_service as lcs_mod
        from unittest.mock import patch as mock_patch
        with mock_patch.object(lcs_mod, "db", patch_meta_send["db"]):
            ok = lcs_mod.send_meta_message_by_user_id("meta_ig:X", "x")
        assert ok is False

    def test_invalid_user_id_returns_false(self, patch_meta_send):
        import live_chat_service as lcs_mod
        from unittest.mock import patch as mock_patch
        with mock_patch.object(lcs_mod, "db", patch_meta_send["db"]):
            ok = lcs_mod.send_meta_message_by_user_id("telegram:123", "x")
        assert ok is False
