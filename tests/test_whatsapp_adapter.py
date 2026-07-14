"""
טסטים עבור WhatsApp Adapter ו-Webhook.
"""

import sys
import types
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ── Mock twilio (לא מותקן בסביבת הטסטים) ─────────────────────────────────
_twilio_mock = types.ModuleType("twilio")
_twilio_rest = types.ModuleType("twilio.rest")
_twilio_rest.Client = MagicMock()
_twilio_validator = types.ModuleType("twilio.request_validator")
_twilio_validator.RequestValidator = MagicMock()
sys.modules.setdefault("twilio", _twilio_mock)
sys.modules.setdefault("twilio.rest", _twilio_rest)
sys.modules.setdefault("twilio.request_validator", _twilio_validator)


class TestWhatsAppAdapter:
    """בדיקות ל-WhatsAppAdapter — mock ל-Twilio Client."""

    @pytest.fixture
    def adapter(self):
        with patch("twilio.rest.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.messages = MagicMock()
            mock_client.messages.create = MagicMock()

            from messaging.whatsapp_adapter import WhatsAppAdapter
            wa = WhatsAppAdapter(
                account_sid="test_sid",
                auth_token="test_token",
                whatsapp_number="+14155551234",
            )
            yield wa, mock_client

    @pytest.mark.asyncio
    async def test_send_text_calls_twilio(self, adapter):
        wa, mock_client = adapter
        await wa.send_text("972501234567", "<b>שלום</b>")
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args
        # וודא ש-format_message נקרא — הטקסט צריך להכיל * במקום <b>
        assert "*שלום*" in call_kwargs.kwargs.get("body", call_kwargs[1].get("body", ""))

    @pytest.mark.asyncio
    async def test_send_text_with_buttons_adds_numbered_list(self, adapter):
        wa, mock_client = adapter
        await wa.send_text("972501234567", "בחרו:", buttons=["תספורת", "צבע", "החלקה"])
        call_kwargs = mock_client.messages.create.call_args
        body = call_kwargs.kwargs.get("body", call_kwargs[1].get("body", ""))
        assert "1. תספורת" in body
        assert "2. צבע" in body
        assert "3. החלקה" in body
        assert "(שלחו את המספר)" in body

    @pytest.mark.asyncio
    async def test_send_contact(self, adapter):
        wa, mock_client = adapter
        await wa.send_contact("972501234567", "דנה", "03-555-0123")
        mock_client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_location(self, adapter):
        wa, mock_client = adapter
        await wa.send_location("972501234567", 32.0853, 34.7818)
        call_kwargs = mock_client.messages.create.call_args
        body = call_kwargs.kwargs.get("body", call_kwargs[1].get("body", ""))
        assert "maps.google.com" in body


flask = pytest.importorskip("flask", reason="Flask not installed — skipping webhook tests")


class TestWhatsAppWebhook:
    """בדיקות ל-webhook endpoint."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        """יוצר Flask test client עם WhatsApp webhook."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
        monkeypatch.setenv("ADMIN_PASSWORD", "test")

        # מאז הראוטינג הרב-tenant, פרטי Twilio נקראים דינמית מ-config
        # (דרך _resolve_twilio_settings) — ה-patch עובר לשם.
        import messaging.whatsapp_webhook as wh_mod
        import ai_chatbot.config as _cfg
        monkeypatch.setattr(_cfg, "TWILIO_ACCOUNT_SID", "test_sid")
        monkeypatch.setattr(_cfg, "TWILIO_AUTH_TOKEN", "test_token")
        monkeypatch.setattr(_cfg, "TWILIO_WHATSAPP_NUMBER", "+14155551234")

        # DB זמני אמיתי עם סכימה — הנתיב נקרא דינמית מ-config (tenancy),
        # כך שה-patch כאן מכסה את כל קריאות ה-DB של ה-webhook. היסטורית
        # הטסטים האלה עבדו על DB שדלף מטסטים קודמים (העותק הקפוא של
        # DB_PATH) — עכשיו הבידוד אמיתי.
        monkeypatch.setattr(_cfg, "DB_PATH", tmp_path / "test.db")
        from database import init_db
        init_db()

        # Patch resolve_whatsapp_user — מחזיר את מספר הטלפון כ-user_id (מצב נוכחי)
        monkeypatch.setattr(
            wh_mod, "resolve_whatsapp_user",
            lambda phone_number, **kw: phone_number,
        )

        # Patch DB functions — הטסטים לא מאתחלים DB אמיתי
        monkeypatch.setattr(wh_mod.db, "ensure_user_subscribed", lambda user_id: None)
        monkeypatch.setattr(wh_mod.db, "get_consecutive_fallbacks", lambda user_id: 0)
        monkeypatch.setattr(wh_mod.db, "set_consecutive_fallbacks", lambda user_id, count: None)

        from flask import Flask
        from messaging.whatsapp_webhook import whatsapp_bp

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(whatsapp_bp)
        return app.test_client()

    def test_valid_request_returns_200(self, client, monkeypatch):
        """בקשה תקינה עם חתימה תקפה מחזירה 200."""
        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        resp = client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:+972501234567",
                                "Body": "שלום",
                            },
                        )
        assert resp.status_code == 200

    def test_valid_request_passes_channel_whatsapp(self, client, monkeypatch):
        """process_incoming_message נקרא עם channel='whatsapp'."""
        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result) as mock_proc:
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:+972501234567",
                                "Body": "שלום",
                            },
                        )
                        mock_proc.assert_called_once()
                        call_kwargs = mock_proc.call_args
                        assert call_kwargs.kwargs.get("channel") == "whatsapp"

    def test_request_agent_creates_db_record(self, client, monkeypatch):
        """כש-action=request_agent — נוצרת בקשת נציג ב-DB."""
        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "נציג יחזור אליכם"
                mock_result.action = "request_agent"
                mock_result.agent_request_message = "הלקוח ביקש נציג"
                mock_result.handoff_reason = ""
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        with patch("messaging.whatsapp_webhook._handle_agent_request") as mock_handle:
                            resp = client.post(
                                "/webhook/whatsapp",
                                data={
                                    "From": "whatsapp:+972501234567",
                                    "Body": "אני רוצה נציג",
                                },
                            )
                            mock_handle.assert_called_once()
        assert resp.status_code == 200

    def test_invalid_signature_returns_403(self, client):
        """חתימה לא תקינה מחזירה 403."""
        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=False):
            resp = client.post(
                "/webhook/whatsapp",
                data={
                    "From": "whatsapp:+972501234567",
                    "Body": "שלום",
                },
            )
        assert resp.status_code == 403

    def test_missing_from_returns_400(self, client):
        """בקשה ללא From מחזירה 400."""
        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            resp = client.post(
                "/webhook/whatsapp",
                data={"Body": "שלום"},
            )
        assert resp.status_code == 400

    def test_bsuid_in_from_field(self, client, monkeypatch):
        """כש-From מכיל BSUID (IL.ABCdef123) — מועבר כ-bsuid, לא כ-phone."""
        import messaging.whatsapp_webhook as wh_mod
        calls = []

        def mock_resolve(phone_number, **kw):
            calls.append({"phone_number": phone_number, **kw})
            return kw.get("bsuid") or phone_number

        monkeypatch.setattr(wh_mod, "resolve_whatsapp_user", mock_resolve)

        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        resp = client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:IL.ABCdef123",
                                "Body": "שלום",
                            },
                        )
        assert resp.status_code == 200
        assert len(calls) == 1
        # BSUID לא אמור להיות מועבר כ-phone_number
        assert calls[0]["phone_number"] == ""
        assert calls[0]["bsuid"] == "IL.ABCdef123"

    def test_bsuid_in_from_with_external_user_id(self, client, monkeypatch):
        """כש-From מכיל BSUID וגם ExternalUserId קיים — ExternalUserId מקבל עדיפות."""
        import messaging.whatsapp_webhook as wh_mod
        calls = []

        def mock_resolve(phone_number, **kw):
            calls.append({"phone_number": phone_number, **kw})
            return kw.get("bsuid") or phone_number

        monkeypatch.setattr(wh_mod, "resolve_whatsapp_user", mock_resolve)

        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        resp = client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:IL.ABCdef123",
                                "Body": "שלום",
                                "ExternalUserId": "IL.ABCdef123",
                            },
                        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0]["phone_number"] == ""
        assert calls[0]["bsuid"] == "IL.ABCdef123"

    def test_webhook_extracts_external_parent_user_id(self, client, monkeypatch):
        """ExternalParentUserId מהפיילוד עובר ל-resolve_whatsapp_user כ-parent_bsuid."""
        import messaging.whatsapp_webhook as wh_mod
        calls = []

        def mock_resolve(phone_number, **kw):
            calls.append({"phone_number": phone_number, **kw})
            return kw.get("bsuid") or phone_number

        monkeypatch.setattr(wh_mod, "resolve_whatsapp_user", mock_resolve)

        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        resp = client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:+972501234567",
                                "Body": "שלום",
                                "ExternalUserId": "IL.ChildC3",
                                "ExternalParentUserId": "IL.ParentXYZ",
                            },
                        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0]["bsuid"] == "IL.ChildC3"
        assert calls[0]["parent_bsuid"] == "IL.ParentXYZ"

    def test_webhook_handles_missing_parent_user_id(self, client, monkeypatch):
        """webhook ללא ExternalParentUserId — parent_bsuid=None, לא מפיל."""
        import messaging.whatsapp_webhook as wh_mod
        calls = []

        def mock_resolve(phone_number, **kw):
            calls.append({"phone_number": phone_number, **kw})
            return kw.get("bsuid") or phone_number

        monkeypatch.setattr(wh_mod, "resolve_whatsapp_user", mock_resolve)

        with patch("messaging.whatsapp_webhook._validate_twilio_signature", return_value=True):
            with patch("ai_chatbot.live_chat_service.LiveChatService.is_active", return_value=False):
                mock_result = MagicMock()
                mock_result.text = "תשובה"
                mock_result.action = "reply"
                with patch("core.message_processor.process_incoming_message", return_value=mock_result):
                    with patch("messaging.whatsapp_webhook._send_whatsapp_response"):
                        resp = client.post(
                            "/webhook/whatsapp",
                            data={
                                "From": "whatsapp:+972501234567",
                                "Body": "שלום",
                                "ExternalUserId": "IL.ChildD4",
                            },
                        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0]["parent_bsuid"] is None
