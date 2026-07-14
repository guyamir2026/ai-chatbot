"""
טסטים ללכידת שדה To ב-webhook של WhatsApp (multi-tenant שלב 1).

ה-To (מספר ה-WhatsApp העסקי) נשמר כ-provider_asset_id על שורת המשתמש —
העוגן העתידי לזיהוי ה-tenant של הודעה נכנסת. ראה
docs/multi_tenant_migration_spec.md סעיף 6.2.
"""

from unittest.mock import patch, MagicMock

from flask import Flask


def _make_app() -> Flask:
    return Flask(__name__)


class TestCurrentToNumber:
    def test_reads_and_strips_whatsapp_prefix(self):
        from messaging import whatsapp_webhook as wh

        app = _make_app()
        with app.test_request_context(
            "/webhook/whatsapp",
            method="POST",
            data={"To": "whatsapp:+14155551234"},
        ):
            assert wh._current_to_number() == "+14155551234"

    def test_missing_to_returns_empty(self):
        from messaging import whatsapp_webhook as wh

        app = _make_app()
        with app.test_request_context("/webhook/whatsapp", method="POST", data={}):
            assert wh._current_to_number() == ""

    def test_outside_request_context_returns_empty(self):
        """קריאה ישירה (טסטים / קוד עתידי) בלי request — לא זורק ולא מתעד error."""
        from messaging import whatsapp_webhook as wh

        assert wh._current_to_number() == ""


class TestUpsertWhatsappUser:
    def test_passes_to_number_as_provider_asset_id(self):
        from messaging import whatsapp_webhook as wh

        fake_db = MagicMock()
        app = _make_app()
        with patch.object(wh, "db", fake_db):
            with app.test_request_context(
                "/webhook/whatsapp",
                method="POST",
                data={"To": "whatsapp:+14155551234"},
            ):
                wh._upsert_whatsapp_user("+972500000001", "Alice")

        fake_db.upsert_user.assert_called_once_with(
            "+972500000001",
            "Alice",
            channel="whatsapp",
            provider_asset_id="+14155551234",
        )

    def test_empty_profile_falls_back_to_number(self):
        from messaging import whatsapp_webhook as wh

        fake_db = MagicMock()
        with patch.object(wh, "db", fake_db):
            wh._upsert_whatsapp_user("+972500000001", "")

        fake_db.upsert_user.assert_called_once_with(
            "+972500000001",
            "+972500000001",
            channel="whatsapp",
            provider_asset_id="",
        )
