"""
טסטים ל-messaging/whatsapp_templates_submit.py.

מוקים מחליפים את Twilio HTTP — אין קריאות אמיתיות.
"""

from unittest.mock import MagicMock, patch

import pytest


# ── fixture: DB נקי ─────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── sanitize_template_name ───────────────────────────────────────────────────


class TestSanitizeTemplateName:
    def test_lowercase_alphanumeric_passes_through(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("order_update_v2") == "order_update_v2"

    def test_uppercase_downcased(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("Order_Update_V2") == "order_update_v2"

    def test_spaces_become_underscores(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("Order Update v2") == "order_update_v2"

    def test_special_chars_replaced(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("welcome!@#$msg") == "welcome_msg"

    def test_consecutive_specials_collapsed(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("a...b---c") == "a_b_c"

    def test_leading_trailing_underscores_stripped(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        assert sanitize_template_name("___hello___") == "hello"
        assert sanitize_template_name("  spaces  ") == "spaces"

    def test_empty_falls_back_to_timestamp(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        result = sanitize_template_name("")
        assert result.startswith("template_")
        assert len(result) > len("template_")

    def test_only_special_chars_falls_back(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        result = sanitize_template_name("!!!@@@")
        assert result.startswith("template_")

    def test_hebrew_input_treated_as_special(self):
        """שמות בעברית לא חוקיים ב-Meta; sanitize יזרוק אותם ויחזיר fallback."""
        from messaging.whatsapp_templates_submit import sanitize_template_name
        result = sanitize_template_name("תזכורת תור")
        # כל התווים העבריים הוחלפו בקווים תחתונים, שבוטלו בשוליים → fallback
        assert result.startswith("template_") or result == ""

    def test_max_length_enforced(self):
        from messaging.whatsapp_templates_submit import sanitize_template_name
        very_long = "a" * 1000
        assert len(sanitize_template_name(very_long)) <= 512


# ── submit_template_for_approval — ולידציה ──────────────────────────────────


class TestSubmitValidation:
    def test_rejects_empty_content_sid(self):
        from messaging.whatsapp_templates_submit import submit_template_for_approval
        with pytest.raises(ValueError, match="content_sid"):
            submit_template_for_approval("", "UTILITY", "name")

    def test_rejects_invalid_category(self):
        from messaging.whatsapp_templates_submit import submit_template_for_approval
        with pytest.raises(ValueError, match="category"):
            submit_template_for_approval("HX123", "INVALID", "name")

    def test_rejects_empty_name(self):
        from messaging.whatsapp_templates_submit import submit_template_for_approval
        with pytest.raises(ValueError, match="name"):
            submit_template_for_approval("HX123", "UTILITY", "")

    def test_accepts_all_three_valid_categories(self, db, monkeypatch):
        """UTILITY / MARKETING / AUTHENTICATION — כל אחת מהן תקינה."""
        from messaging import whatsapp_templates_submit as submit_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_OK", "friendly_name": "ok",
            "approval_status": "unsubmitted",
        })

        def fake_post(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"status": "received"}
            return resp

        monkeypatch.setattr(submit_mod.requests, "post", fake_post)

        for cat in ("UTILITY", "MARKETING", "AUTHENTICATION"):
            result = submit_mod.submit_template_for_approval(
                "HX_OK", cat, "ok_name",
            )
            assert result["success"] is True
            assert result["category"] == cat


# ── submit_template_for_approval — תקשורת עם Twilio ─────────────────────────


class TestSubmitHttp:
    def test_successful_submission_updates_db_to_pending(self, db, monkeypatch):
        """שליחה מוצלחת → upsert מסמן approval_status=pending."""
        from messaging import whatsapp_templates_submit as submit_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_NEW",
            "friendly_name": "new_tpl",
            "approval_status": "unsubmitted",
            "category": "UNKNOWN",
            "body_text": "היי {{1}}",
            "language": "he",
        })

        fake_resp = MagicMock()
        fake_resp.status_code = 201
        fake_resp.json.return_value = {"status": "received", "category": "UTILITY"}
        monkeypatch.setattr(
            submit_mod.requests, "post", MagicMock(return_value=fake_resp)
        )

        result = submit_mod.submit_template_for_approval(
            content_sid="HX_NEW",
            category="UTILITY",
            name="new_tpl_v1",
        )

        assert result["success"] is True
        assert result["approval_status"] == "pending"
        assert result["category"] == "UTILITY"
        assert result["error"] is None

        tpl = db.get_whatsapp_template("HX_NEW")
        assert tpl["approval_status"] == "pending"
        assert tpl["category"] == "UTILITY"

    def test_non_2xx_response_is_failure(self, db, monkeypatch):
        from messaging import whatsapp_templates_submit as submit_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_BAD", "friendly_name": "bad",
            "approval_status": "unsubmitted",
        })

        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.text = '{"code":20001,"message":"Name already exists"}'
        monkeypatch.setattr(
            submit_mod.requests, "post", MagicMock(return_value=fake_resp)
        )

        result = submit_mod.submit_template_for_approval(
            "HX_BAD", "UTILITY", "duplicate_name"
        )

        assert result["success"] is False
        assert "HTTP 400" in result["error"]
        assert "already exists" in result["error"]
        # לא התבצע עדכון מקומי
        assert db.get_whatsapp_template("HX_BAD")["approval_status"] == "unsubmitted"

    def test_network_error_returns_failure_not_raise(self, db, monkeypatch):
        """שגיאת רשת לא מפילה את ה-handler — מוחזרת כ-success=False."""
        from messaging import whatsapp_templates_submit as submit_mod
        import requests as requests_lib

        def fake_post_raises(*args, **kwargs):
            raise requests_lib.RequestException("connection refused")

        monkeypatch.setattr(submit_mod.requests, "post", fake_post_raises)

        result = submit_mod.submit_template_for_approval(
            "HX_X", "UTILITY", "any_name"
        )
        assert result["success"] is False
        assert "שגיאת רשת" in result["error"]

    def test_name_is_sanitized_before_sending(self, db, monkeypatch):
        """גם אם המשתמש הזין שם 'מכוער', הוא נשלח ל-Twilio sanitized."""
        from messaging import whatsapp_templates_submit as submit_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_SAN", "friendly_name": "san",
            "approval_status": "unsubmitted",
        })

        captured = {}

        def fake_post(url, json=None, data=None, auth=None, timeout=None):
            # Twilio דורש JSON, לא form-encoded — מאמתים שאנחנו בכלל
            # שולחים json= ולא data= (שגרם ל-HTTP 415 בפרודקשן).
            captured["json"] = json
            captured["data"] = data
            captured["url"] = url
            resp = MagicMock()
            resp.status_code = 201
            return resp

        monkeypatch.setattr(submit_mod.requests, "post", fake_post)

        submit_mod.submit_template_for_approval(
            content_sid="HX_SAN",
            category="UTILITY",
            name="Order Update!!! v2",
        )

        # שדות lowercase + JSON (לא form/PascalCase)
        assert captured["data"] is None
        assert captured["json"]["name"] == "order_update_v2"
        assert captured["json"]["category"] == "UTILITY"
        assert "HX_SAN" in captured["url"]
        assert "/ApprovalRequests/whatsapp" in captured["url"]

    def test_success_without_local_template_still_reports_success(self, db, monkeypatch):
        """אם מסיבה כלשהי התבנית לא ב-DB המקומי, Twilio עדיין מצליחה.
        הסנכרון הבא ימלא את ה-DB."""
        from messaging import whatsapp_templates_submit as submit_mod

        fake_resp = MagicMock()
        fake_resp.status_code = 201
        monkeypatch.setattr(
            submit_mod.requests, "post", MagicMock(return_value=fake_resp)
        )

        # הערה: לא upsert-טנו כלום ל-DB
        result = submit_mod.submit_template_for_approval(
            "HX_NOT_IN_DB", "UTILITY", "any_name"
        )
        assert result["success"] is True
        # לא יצרנו רשומה חדשה (upsert רק מעדכן קיימות)
        assert db.get_whatsapp_template("HX_NOT_IN_DB") is None
