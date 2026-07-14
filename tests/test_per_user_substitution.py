"""
טסטים ל-per-user substitution (שלב 6) — substitute_user_fields +
render_variables_for_user + integration ב-_send_campaign_locked.
"""

from unittest.mock import MagicMock, patch

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


# ── substitute_user_fields ───────────────────────────────────────────────────


class TestSubstituteUserFields:
    def test_username_replaced(self):
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields("היי {{user:username}}", {"username": "דני"})
        assert out == "היי דני"

    def test_user_id_replaced(self):
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "id={{user:user_id}}", {"user_id": "+972501234567"},
        )
        assert out == "id=+972501234567"

    def test_phone_formatted_to_local(self):
        """user:phone משתמש ב-format_phone — +972XXX → 0XXX."""
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "טלפון: {{user:phone}}", {"user_id": "+972501234567"},
        )
        assert out == "טלפון: 0501234567"

    def test_multiple_fields(self):
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "{{user:username}} ({{user:phone}})",
            {"username": "אבי", "user_id": "+972521112222"},
        )
        assert out == "אבי (0521112222)"

    def test_unknown_field_kept_literal(self):
        """שדה לא מוכר — ה-placeholder נשאר literal כדי שהמנהל יראה בעיה."""
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "hi {{user:email}}", {"username": "דני"},
        )
        assert "{{user:email}}" in out

    def test_missing_value_becomes_empty(self):
        """שדה מוכר אך חסר ב-user_row — empty string."""
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields("היי {{user:username}}", {"user_id": "x"})
        assert out == "היי "

    def test_no_placeholder_passes_through(self):
        from messaging.template_renderer import substitute_user_fields
        assert substitute_user_fields("טקסט רגיל", {}) == "טקסט רגיל"

    def test_empty_text(self):
        from messaging.template_renderer import substitute_user_fields
        assert substitute_user_fields("", {}) == ""
        assert substitute_user_fields(None, {}) == ""

    def test_bsuid_phone_returns_raw(self):
        """BSUID (לא מספר טלפון) — format_phone מחזיר כמו שהוא."""
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "id={{user:phone}}", {"user_id": "IL.ABC123"},
        )
        assert out == "id=IL.ABC123"

    def test_whitespace_in_placeholder(self):
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "{{ user:username }}", {"username": "דני"},
        )
        assert out == "דני"

    def test_html_content_preserved(self):
        """אין escaping ב-substitute_user_fields (זה תפקיד של wa_markdown_to_html
        מאוחר יותר). השדות נכנסים raw."""
        from messaging.template_renderer import substitute_user_fields
        out = substitute_user_fields(
            "שם: {{user:username}}", {"username": "<b>תגית</b>"},
        )
        assert out == "שם: <b>תגית</b>"


# ── render_variables_for_user — אינטגרציה ─────────────────────────────────────


class TestRenderVariablesForUser:
    def test_resolves_user_fields_in_mapping_values(self):
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user(
            template_variables=[{"index": "1"}, {"index": "2"}],
            static_mapping={
                "1": "היי {{user:username}}",
                "2": "המספר שלך: {{user:phone}}",
            },
            user_id="+972501234567",
            user_row={"user_id": "+972501234567", "username": "שרה"},
        )
        assert result == {
            "1": "היי שרה",
            "2": "המספר שלך: 0501234567",
        }

    def test_fallback_user_row_when_none(self):
        """אם user_row=None, fallback עם user_id בלבד. {{user:username}} → ריק."""
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user(
            template_variables=[{"index": "1"}, {"index": "2"}],
            static_mapping={
                "1": "{{user:username}}",
                "2": "id={{user:user_id}}",
            },
            user_id="+972501234567",
            user_row=None,
        )
        assert result["1"] == ""
        assert result["2"] == "id=+972501234567"

    def test_static_values_unchanged(self):
        from messaging.broadcast_sender import render_variables_for_user
        result = render_variables_for_user(
            template_variables=[{"index": "1"}],
            static_mapping={"1": "קבוע בלי placeholders"},
            user_id="+972501234567",
            user_row={"user_id": "+972501234567", "username": "x"},
        )
        assert result["1"] == "קבוע בלי placeholders"


# ── get_users_for_broadcast — DB helper ──────────────────────────────────────


class TestGetUsersForBroadcast:
    def test_batch_fetch_returns_dicts(self, db):
        db.upsert_user("+972501111111", username="דני", channel="whatsapp")
        db.upsert_user("+972502222222", username="רונית", channel="whatsapp")
        db.upsert_user("tg_u1", username="טלגרם", channel="telegram")

        rows = db.get_users_for_broadcast(
            ["+972501111111", "+972502222222", "tg_u1"],
        )
        assert len(rows) == 3
        by_id = {r["user_id"]: r for r in rows}
        assert by_id["+972501111111"]["username"] == "דני"
        assert by_id["+972502222222"]["username"] == "רונית"

    def test_missing_users_not_returned(self, db):
        db.upsert_user("+972501111111", username="דני", channel="whatsapp")
        rows = db.get_users_for_broadcast(["+972501111111", "+972509999999"])
        assert len(rows) == 1
        assert rows[0]["user_id"] == "+972501111111"

    def test_empty_list_returns_empty(self, db):
        assert db.get_users_for_broadcast([]) == []


# ── Integration: send_campaign משתמש ב-batch-fetch ─────────────────────────────


class TestSendCampaignWithUserFields:
    def test_campaign_personalizes_per_recipient(self, db, monkeypatch):
        """שני נמענים עם שמות שונים; mapping עם {{user:username}} → כל אחד
        מקבל את שמו."""
        from messaging import broadcast_sender as sender_mod

        # יצירת תבנית + משתמשים
        db.upsert_whatsapp_template({
            "content_sid": "HX_T1", "friendly_name": "t",
            "language": "he", "category": "UTILITY",
            "approval_status": "approved", "body_text": "היי {{1}}",
            "variables": [{"index": "1", "name": "name"}],
        })
        db.upsert_user("+972501111111", username="דני", channel="whatsapp")
        db.upsert_user("+972502222222", username="רונית", channel="whatsapp")
        db.set_wa_marketing_opt_in("+972501111111", source="test")
        db.set_wa_marketing_opt_in("+972502222222", source="test")

        cid = db.create_broadcast_campaign(
            template_sid="HX_T1",
            variable_mapping={"1": "היי {{user:username}}"},
        )

        # Twilio mock — לוכד את content_variables שנשלחים לכל recipient
        sent_variables: list[dict] = []

        def fake_create(**kwargs):
            import json as _json
            cvars = kwargs.get("content_variables") or "{}"
            sent_variables.append(_json.loads(cvars))
            return MagicMock(sid=f"SM_{len(sent_variables)}")

        mock_client = MagicMock()
        mock_client.messages.create = fake_create
        monkeypatch.setattr(
            "messaging.whatsapp_sender._get_twilio_client", lambda: mock_client,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_sender._is_phone_number", lambda x: True,
        )
        monkeypatch.setattr(sender_mod, "_PACE_SLEEP_SECONDS", 0)
        monkeypatch.setattr(
            "messaging.shabbat_window.is_blocked_for_marketing",
            lambda _dt: (False, None),
        )

        sender_mod.send_campaign(cid)

        # שני recipients, כל אחד קיבל את שמו
        assert len(sent_variables) == 2
        messages_sent = {v["1"] for v in sent_variables}
        assert messages_sent == {"היי דני", "היי רונית"}
