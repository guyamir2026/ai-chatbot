"""
טסטים ל-שלב 9 — opt-in prompt פרואקטיבי (should_send_opt_in_prompt,
mark_opt_in_prompt_sent, וזרימת הכפתור).
"""

from unittest.mock import patch

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


def _make_wa_user(db, uid, message_count=5):
    """יוצר משתמש WA עם message_count מוגדר (דרך UPDATE ישיר — upsert_user
    תמיד מעלה ב-1 בכל קריאה)."""
    db.upsert_user(uid, username=uid, channel="whatsapp")
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE users SET message_count = ? WHERE user_id = ?",
            (message_count, uid),
        )


# ── should_send_opt_in_prompt ────────────────────────────────────────────────


class TestShouldSendPrompt:
    def test_eligible_user_returns_true(self, db):
        _make_wa_user(db, "+972501000001", message_count=5)
        assert db.should_send_opt_in_prompt("+972501000001") is True

    def test_already_opted_in_returns_false(self, db):
        _make_wa_user(db, "+972501000001", message_count=5)
        db.set_wa_marketing_opt_in("+972501000001", source="test")
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_opted_out_returns_false(self, db):
        """מי שביקש הסרה — לא מטרידים שוב."""
        _make_wa_user(db, "+972501000001", message_count=5)
        db.set_wa_opted_out("+972501000001")
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_already_prompted_returns_false(self, db):
        """Regression: אחרי ששלחנו prompt — לא חוזרים על זה, גם אם המשתמש
        לא ענה."""
        _make_wa_user(db, "+972501000001", message_count=5)
        db.mark_opt_in_prompt_sent("+972501000001")
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_low_engagement_returns_false(self, db):
        """משתמש שכתב רק 1-2 הודעות — מוקדם מדי לשאול על opt-in."""
        _make_wa_user(db, "+972501000001", message_count=1)
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_configurable_threshold(self, db):
        _make_wa_user(db, "+972501000001", message_count=2)
        assert db.should_send_opt_in_prompt(
            "+972501000001", min_messages=5,
        ) is False
        assert db.should_send_opt_in_prompt(
            "+972501000001", min_messages=1,
        ) is True

    def test_missing_user_returns_false(self, db):
        assert db.should_send_opt_in_prompt("+972501999999") is False

    def test_telegram_user_returns_false(self, db):
        """אופצ׳ן שיווק WhatsApp לא רלוונטי ל-telegram."""
        db.upsert_user("tg_u1", username="tg_u1", channel="telegram")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET message_count = 10 WHERE user_id = ?",
                ("tg_u1",),
            )
        assert db.should_send_opt_in_prompt("tg_u1") is False


# ── mark_opt_in_prompt_sent ──────────────────────────────────────────────────


class TestMarkPromptSent:
    def test_sets_timestamp(self, db):
        _make_wa_user(db, "+972501000001", message_count=5)
        assert db.should_send_opt_in_prompt("+972501000001") is True
        db.mark_opt_in_prompt_sent("+972501000001")
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_idempotent(self, db):
        """קריאה חוזרת — לא דורסת timestamp קיים (WHERE IS NULL)."""
        _make_wa_user(db, "+972501000001", message_count=5)
        db.mark_opt_in_prompt_sent("+972501000001")

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT wa_opt_in_prompt_sent_at FROM users WHERE user_id = ?",
                ("+972501000001",),
            ).fetchone()
            first_ts = row["wa_opt_in_prompt_sent_at"]

        # קריאה שנייה לא משנה
        db.mark_opt_in_prompt_sent("+972501000001")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT wa_opt_in_prompt_sent_at FROM users WHERE user_id = ?",
                ("+972501000001",),
            ).fetchone()
            assert row["wa_opt_in_prompt_sent_at"] == first_ts

    def test_empty_user_id_noop(self, db):
        # לא זורק — פשוט לא עושה כלום
        db.mark_opt_in_prompt_sent("")


# ── Integration: button handling via webhook functions ──────────────────────


class TestOptInButtonHandling:
    """ה-handler של הכפתור קורא ל-DB helpers ישירות + שולח תשובה. מוק
    על _send_whatsapp_response כדי לא לקרוא ל-Twilio."""

    def test_yes_button_opts_user_in(self, db, monkeypatch):
        from messaging import whatsapp_webhook as webhook

        _make_wa_user(db, "+972501000001", message_count=5)

        replies = []
        monkeypatch.setattr(
            webhook, "_send_whatsapp_response",
            lambda to, text: replies.append((to, text)),
        )

        handled = webhook._handle_opt_in_button(
            "+972501000001", "דני", webhook._OPTIN_BUTTON_YES,
        )
        assert handled is True

        status = db.get_wa_opt_status("+972501000001")
        assert status["opted_in"] is True
        assert status["opted_in_source"] == "bot_button"
        assert len(replies) == 1
        assert "תודה" in replies[0][1]

    def test_no_button_does_not_opt_out(self, db, monkeypatch):
        """לחיצה על "לא" דוחה את הבקשה — אין opt-out מלא (המשתמש לא ביקש
        הסרה, רק דחה את השיווק כרגע)."""
        from messaging import whatsapp_webhook as webhook

        _make_wa_user(db, "+972501000001", message_count=5)

        replies = []
        monkeypatch.setattr(
            webhook, "_send_whatsapp_response",
            lambda to, text: replies.append((to, text)),
        )

        handled = webhook._handle_opt_in_button(
            "+972501000001", "דני", webhook._OPTIN_BUTTON_NO,
        )
        assert handled is True

        status = db.get_wa_opt_status("+972501000001")
        assert status["opted_in"] is False
        # לא opted_out כי לא נלחץ "הסר"
        assert status["opted_out_at"] is None
        assert len(replies) == 1

    def test_unknown_button_not_handled(self, db, monkeypatch):
        from messaging import whatsapp_webhook as webhook
        _make_wa_user(db, "+972501000001", message_count=5)

        handled = webhook._handle_opt_in_button(
            "+972501000001", "דני", "some_other_button",
        )
        assert handled is False


# ── Regression: כשל ב-handler לא ייפול ל-fallback של "סשן פג תוקף" ────────────


class TestOptInButtonExceptionFallthrough:
    """Regression bugbot: אם _handle_opt_in_button זרק חריגה, הזרימה לא צריכה
    ליפול ל-elif שמודיע "⏰ הסשן פג תוקף" — זה היה מבלבל את המשתמש."""

    def _make_client(self, monkeypatch):
        from flask import Flask
        from messaging import whatsapp_webhook as wh_mod

        # פרטי Twilio נקראים דינמית מ-config (multi-tenant) — patch שם
        import ai_chatbot.config as _cfg
        monkeypatch.setattr(_cfg, "TWILIO_ACCOUNT_SID", "test_sid")
        monkeypatch.setattr(_cfg, "TWILIO_AUTH_TOKEN", "test_token")
        monkeypatch.setattr(_cfg, "TWILIO_WHATSAPP_NUMBER", "+14155551234")
        monkeypatch.setattr(
            wh_mod, "resolve_whatsapp_user",
            lambda phone_number, **kw: phone_number,
        )
        monkeypatch.setattr(
            wh_mod, "_validate_twilio_signature", lambda *a, **kw: True,
        )

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(wh_mod.whatsapp_bp)
        return app.test_client(), wh_mod

    def test_exception_does_not_send_session_expired(self, db, monkeypatch):
        client, wh_mod = self._make_client(monkeypatch)

        # מאלצים חריגה ב-handler
        def _boom(*a, **kw):
            raise RuntimeError("simulated handler failure")
        monkeypatch.setattr(wh_mod, "_handle_opt_in_button", _boom)

        sent = []
        monkeypatch.setattr(
            wh_mod, "_send_whatsapp_response",
            lambda to, text: sent.append((to, text)),
        )

        resp = client.post(
            "/webhook/whatsapp",
            data={
                "From": "whatsapp:+972501234567",
                "Body": "",
                "ButtonPayload": wh_mod._OPTIN_BUTTON_YES,
            },
        )
        assert resp.status_code == 200
        # אסור שתישלח הודעת "סשן פג תוקף" או כל הודעה אחרת
        for _to, text in sent:
            assert "פג תוקף" not in text, (
                f"לא אמורה להישלח הודעת session-expired בכשל opt-in: {text}"
            )


# ── _maybe_send_opt_in_prompt ────────────────────────────────────────────────


class TestMaybeSendPrompt:
    def test_not_sent_if_user_not_eligible(self, db, monkeypatch):
        """משתמש שלא עומד בתנאים — לא שולחים."""
        from messaging import whatsapp_webhook as webhook

        _make_wa_user(db, "+972501000001", message_count=1)  # engagement נמוך
        sent_calls = []
        monkeypatch.setattr(
            webhook, "_send_whatsapp_response",
            lambda to, text: sent_calls.append((to, text)),
        )

        webhook._maybe_send_opt_in_prompt("+972501000001")
        assert sent_calls == []  # לא נשלח כלום

    def test_sent_and_marked_for_eligible(self, db, monkeypatch):
        from messaging import whatsapp_webhook as webhook

        _make_wa_user(db, "+972501000001", message_count=5)

        # Quick Reply mock — נזרוק כדי להגיע ל-fallback טקסטואלי
        from messaging import whatsapp_templates as wa_tpl
        monkeypatch.setattr(
            wa_tpl, "ensure_quick_reply",
            lambda *a, **kw: (_ for _ in ()).throw(
                RuntimeError("no quick reply in tests"),
            ),
        )
        sent_calls = []
        monkeypatch.setattr(
            webhook, "_send_whatsapp_response",
            lambda to, text: sent_calls.append((to, text)),
        )

        webhook._maybe_send_opt_in_prompt("+972501000001")
        assert len(sent_calls) == 1  # נשלח הודעה אחת

        # prompt_sent_at סומן
        assert db.should_send_opt_in_prompt("+972501000001") is False

    def test_not_sent_twice(self, db, monkeypatch):
        """Regression: אחרי prompt אחד — לא שולחים שוב גם בקריאה חוזרת."""
        from messaging import whatsapp_webhook as webhook

        _make_wa_user(db, "+972501000001", message_count=5)
        db.mark_opt_in_prompt_sent("+972501000001")  # כאילו כבר נשלח קודם

        sent_calls = []
        monkeypatch.setattr(
            webhook, "_send_whatsapp_response",
            lambda to, text: sent_calls.append((to, text)),
        )

        webhook._maybe_send_opt_in_prompt("+972501000001")
        assert sent_calls == []
