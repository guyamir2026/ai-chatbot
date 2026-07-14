"""
טסטים ל-messaging/meta_webhook.py — שלב 1 של מימוש Meta DM.

מכסה:
- handshake (GET) — token תקין / לא תקין / לא מוגדר.
- POST — חתימה תקינה / לא תקינה / חסרה.
- פענוח payload של Instagram ו-Messenger.
- התעלמות מ-echo messages.
- payload לא תקין מחזיר 200 (אחרת מטא תסמן את ה-webhook כפגום).
"""

import hashlib
import hmac
import json

import pytest
from flask import Flask


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    """יוצר ערך X-Hub-Signature-256 תקף."""
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def client(monkeypatch):
    """Flask client עם meta_bp רשום, וקונפיג שמוזרק במקום env vars.

    כברירת מחדל, `_is_known_entry` מוחלף ל-True כדי שהבדיקות
    הקיימות ימשיכו לעבוד בלי להתעסק עם meta_credentials. טסטים
    שבודקים את ההתנהגות של הסינון מבטלים את ה-patch הזה.
    """
    import messaging.meta_webhook as mw

    monkeypatch.setattr(mw, "META_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(mw, "META_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setattr(mw, "_is_known_entry", lambda _eid: True)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mw.meta_bp)
    return app.test_client()


@pytest.fixture
def client_strict(monkeypatch):
    """כמו client אבל בלי patch של _is_known_entry — בודק את הסינון."""
    import messaging.meta_webhook as mw

    monkeypatch.setattr(mw, "META_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(mw, "META_VERIFY_TOKEN", VERIFY_TOKEN)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mw.meta_bp)
    return app.test_client()


# ── handshake (GET) ────────────────────────────────────────────────────────

class TestVerify:
    def test_valid_token_echoes_challenge(self, client):
        resp = client.get(
            "/webhooks/meta",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1234567890",
            },
        )
        assert resp.status_code == 200
        assert resp.data == b"1234567890"

    def test_wrong_token_returns_403(self, client):
        resp = client.get(
            "/webhooks/meta",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "x",
            },
        )
        assert resp.status_code == 403

    def test_wrong_mode_returns_403(self, client):
        resp = client.get(
            "/webhooks/meta",
            query_string={
                "hub.mode": "unsubscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "x",
            },
        )
        assert resp.status_code == 403

    def test_missing_verify_token_config_returns_500(self, client, monkeypatch):
        import messaging.meta_webhook as mw
        monkeypatch.setattr(mw, "META_VERIFY_TOKEN", "")
        resp = client.get(
            "/webhooks/meta",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "anything",
                "hub.challenge": "x",
            },
        )
        assert resp.status_code == 500


# ── חתימה (POST) ───────────────────────────────────────────────────────────

class TestSignature:
    def test_valid_signature_returns_200(self, client):
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200

    def test_invalid_signature_returns_403(self, client):
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )
        assert resp.status_code == 403

    def test_missing_signature_header_returns_403(self, client):
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_signature_without_prefix_returns_403(self, client):
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        digest = hmac.new(
            APP_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": digest,  # בלי "sha256=" prefix
            },
        )
        assert resp.status_code == 403

    def test_missing_app_secret_config_blocks_request(self, client, monkeypatch):
        import messaging.meta_webhook as mw
        monkeypatch.setattr(mw, "META_APP_SECRET", "")
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 403


# ── פענוח payload ─────────────────────────────────────────────────────────

class TestExtractMessages:
    """בודק את הלוגיקה של _extract_inbound_messages ישירות."""

    def test_messenger_text_message(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "page",
            "entry": [{
                "id": "PAGE_123",
                "messaging": [{
                    "sender": {"id": "PSID_1"},
                    "recipient": {"id": "PAGE_123"},
                    "timestamp": 1700000000000,
                    "message": {"mid": "m_1", "text": "שלום"},
                }],
            }],
        }
        msgs = _extract_inbound_messages(payload)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["channel"] == "meta_msg"
        assert m["sender_id"] == "PSID_1"
        assert m["page_or_ig_id"] == "PAGE_123"
        assert m["text"] == "שלום"
        assert m["has_attachments"] is False

    def test_instagram_text_message(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG_BIZ_1",
                "messaging": [{
                    "sender": {"id": "IGSID_1"},
                    "recipient": {"id": "IG_BIZ_1"},
                    "timestamp": 1700000000000,
                    "message": {"mid": "m_2", "text": "hi"},
                }],
            }],
        }
        msgs = _extract_inbound_messages(payload)
        assert len(msgs) == 1
        assert msgs[0]["channel"] == "meta_ig"
        assert msgs[0]["sender_id"] == "IGSID_1"
        assert msgs[0]["text"] == "hi"

    def test_echo_messages_are_ignored(self):
        """is_echo = הודעה שיצאה מאיתנו וחזרה. מתעלמים כדי לא לטפל בה כנכנסת."""
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "page",
            "entry": [{
                "id": "PAGE_123",
                "messaging": [{
                    "sender": {"id": "PAGE_123"},
                    "recipient": {"id": "PSID_1"},
                    "message": {"mid": "m_3", "text": "תשובה שלנו", "is_echo": True},
                }],
            }],
        }
        assert _extract_inbound_messages(payload) == []

    def test_unknown_object_returns_empty(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {"object": "whatsapp_business_account", "entry": []}
        assert _extract_inbound_messages(payload) == []

    def test_attachments_detected(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG_BIZ_1",
                "messaging": [{
                    "sender": {"id": "IGSID_1"},
                    "recipient": {"id": "IG_BIZ_1"},
                    "message": {
                        "mid": "m_4",
                        "attachments": [{"type": "image", "payload": {"url": "..."}}],
                    },
                }],
            }],
        }
        msgs = _extract_inbound_messages(payload)
        assert len(msgs) == 1
        assert msgs[0]["has_attachments"] is True
        assert msgs[0]["text"] is None

    def test_multiple_entries_and_events(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_A",
                    "messaging": [
                        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE_A"},
                         "message": {"mid": "m_a", "text": "1"}},
                        {"sender": {"id": "U2"}, "recipient": {"id": "PAGE_A"},
                         "message": {"mid": "m_b", "text": "2"}},
                    ],
                },
                {
                    "id": "PAGE_B",
                    "messaging": [
                        {"sender": {"id": "U3"}, "recipient": {"id": "PAGE_B"},
                         "message": {"mid": "m_c", "text": "3"}},
                    ],
                },
            ],
        }
        msgs = _extract_inbound_messages(payload)
        assert [m["text"] for m in msgs] == ["1", "2", "3"]


# ── תרחישי קצה ב-POST ───────────────────────────────────────────────────────

class TestPostEdgeCases:
    def test_malformed_json_returns_200(self, client):
        """מטא דורשת 200; אחרת תסמן את ה-webhook כפגום. JSON שבור הוא לוג בלבד."""
        body = b"this is not json"
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200

    def test_empty_entry_returns_200(self, client):
        body = json.dumps({"object": "page", "entry": []}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200

    def test_corrupted_entry_does_not_break_others(self, monkeypatch):
        """כשל בפענוח entry אחד לא מפיל את עיבוד היתר."""
        from messaging.meta_webhook import _extract_inbound_messages
        # entry ראשון תקין, שני עם מבנה שיגרום ל-AttributeError בלולאה הפנימית.
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "P1",
                    "messaging": [{
                        "sender": {"id": "U1"}, "recipient": {"id": "P1"},
                        "message": {"mid": "m1", "text": "ok"},
                    }],
                },
                {
                    "id": "P2",
                    "messaging": "not-a-list",  # ידלג בגלל isinstance check
                },
                {
                    "id": "P3",
                    "messaging": [{
                        "sender": {"id": "U3"}, "recipient": {"id": "P3"},
                        "message": {"mid": "m3", "text": "still works"},
                    }],
                },
            ],
        }
        msgs = _extract_inbound_messages(payload)
        # 2 ההודעות התקינות עוברות, השבורה מדלגת.
        assert [m["text"] for m in msgs] == ["ok", "still works"]


# ── הגנה דיפנסיבית מול payload פגום (cursor-bot) ────────────────────────────

class TestMalformedPayloadDefenses:
    """payloads תקני-JSON אבל מבנה לא צפוי לא צריכים לזרוק."""

    def test_payload_is_list_not_dict(self):
        from messaging.meta_webhook import _extract_inbound_messages
        # JSON תקין שאינו אובייקט — לא היה אמור להגיע, אבל אסור לזרוק.
        assert _extract_inbound_messages([1, 2, 3]) == []  # type: ignore[arg-type]

    def test_payload_is_string(self):
        from messaging.meta_webhook import _extract_inbound_messages
        assert _extract_inbound_messages("not a dict") == []  # type: ignore[arg-type]

    def test_payload_is_none(self):
        from messaging.meta_webhook import _extract_inbound_messages
        assert _extract_inbound_messages(None) == []  # type: ignore[arg-type]

    def test_entry_is_non_iterable_int(self):
        """`entry: 42` היה גורם ל-TypeError ב-`for entry in 42`."""
        from messaging.meta_webhook import _extract_inbound_messages
        assert _extract_inbound_messages({"object": "page", "entry": 42}) == []

    def test_entry_is_string(self):
        from messaging.meta_webhook import _extract_inbound_messages
        assert _extract_inbound_messages({"object": "page", "entry": "x"}) == []

    def test_entry_is_dict_not_list(self):
        from messaging.meta_webhook import _extract_inbound_messages
        assert _extract_inbound_messages({"object": "page", "entry": {"id": "P1"}}) == []

    def test_event_is_not_dict(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "page",
            "entry": [{"id": "P1", "messaging": ["not-a-dict", 42, None]}],
        }
        assert _extract_inbound_messages(payload) == []

    def test_message_is_not_dict(self):
        from messaging.meta_webhook import _extract_inbound_messages
        payload = {
            "object": "page",
            "entry": [{
                "id": "P1",
                "messaging": [{
                    "sender": {"id": "U1"},
                    "recipient": {"id": "P1"},
                    "message": "not-a-dict",
                }],
            }],
        }
        assert _extract_inbound_messages(payload) == []

    def test_post_with_list_payload_returns_200(self, client):
        """ה-route חייב להחזיר 200 גם ל-payload פגום (אחרי אימות חתימה)."""
        body = json.dumps([1, 2, 3]).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200

    def test_post_with_int_entry_returns_200(self, client):
        body = json.dumps({"object": "page", "entry": 42}).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200


# ── PII redaction בלוגים ───────────────────────────────────────────────────

class TestPIIRedaction:
    """מזהי משתמשים וטקסט הודעה לא צריכים להיכתב גולמיים ללוג."""

    def test_short_hash_obscures_sender_id(self):
        from messaging.meta_webhook import _short_hash
        h = _short_hash("PSID_12345678")
        assert "PSID" not in h
        assert "12345678" not in h
        assert len(h) == 10

    def test_short_hash_stable(self):
        """אותו מזהה ⇒ אותו hash, כדי שאפשר לקשר בין אירועים."""
        from messaging.meta_webhook import _short_hash
        assert _short_hash("PSID_X") == _short_hash("PSID_X")

    def test_short_hash_handles_none(self):
        from messaging.meta_webhook import _short_hash
        assert _short_hash(None) == "none"
        assert _short_hash("") == "none"

    def test_safe_len_returns_length(self):
        from messaging.meta_webhook import _safe_len
        assert _safe_len("hello") == "5"

    def test_safe_len_handles_none(self):
        from messaging.meta_webhook import _safe_len
        assert _safe_len(None) == "none"

    def test_inbound_log_does_not_contain_pii(self, client, caplog):
        """log של הודעה נכנסת לא צריך להכיל sender_id מלא או טקסט."""
        import logging as _logging
        caplog.set_level(_logging.INFO, logger="messaging.meta_webhook")
        body = json.dumps({
            "object": "page",
            "entry": [{
                "id": "PAGE_123",
                "messaging": [{
                    "sender": {"id": "PSID_SECRET_USER_12345"},
                    "recipient": {"id": "PAGE_123"},
                    "message": {"mid": "m1", "text": "מספר טלפון פרטי: 0501234567"},
                }],
            }],
        }).encode("utf-8")
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200
        # ה-PII לא נכתב; רק metadata אגרגטיבית.
        joined = "\n".join(r.message for r in caplog.records)
        assert "PSID_SECRET_USER_12345" not in joined
        assert "0501234567" not in joined
        assert "מספר טלפון" not in joined
        # ויש לפחות אורך טקסט ו-hash sender (לאיתור באגים).
        assert "text_len=" in joined
        assert "sender_hash=" in joined


# ── סינון עמוד לא מוכר ──────────────────────────────────────────────────────

class TestUnknownEntryFilter:
    """events מ-entry שלא חיברנו דרך OAuth נדחים בשקט."""

    def test_unknown_entry_is_not_logged(self, client_strict, caplog, monkeypatch):
        """`_is_known_entry` מחזיר False ⇒ אין לוג של ההודעה."""
        import messaging.meta_webhook as mw
        monkeypatch.setattr(mw, "_is_known_entry", lambda _eid: False)

        import logging as _logging
        caplog.set_level(_logging.INFO, logger="messaging.meta_webhook")

        body = json.dumps({
            "object": "page",
            "entry": [{
                "id": "UNKNOWN_PAGE",
                "messaging": [{
                    "sender": {"id": "PSID_1"},
                    "recipient": {"id": "UNKNOWN_PAGE"},
                    "message": {"mid": "m1", "text": "שלום"},
                }],
            }],
        }).encode("utf-8")
        resp = client_strict.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        # עדיין 200 — מטא דורשת זאת
        assert resp.status_code == 200
        joined = "\n".join(r.message for r in caplog.records)
        # WARNING על entry לא מוכר נכתב, אבל INFO של ההודעה לא
        assert "Meta inbound: entry לא מוכר" in joined
        assert "text_len=" not in joined

    def test_known_entry_is_processed(self, client_strict, caplog, monkeypatch):
        """`_is_known_entry` מחזיר True ⇒ ההודעה מטופלת."""
        import messaging.meta_webhook as mw
        monkeypatch.setattr(mw, "_is_known_entry", lambda _eid: True)

        import logging as _logging
        caplog.set_level(_logging.INFO, logger="messaging.meta_webhook")

        body = json.dumps({
            "object": "page",
            "entry": [{
                "id": "KNOWN_PAGE",
                "messaging": [{
                    "sender": {"id": "PSID_1"},
                    "recipient": {"id": "KNOWN_PAGE"},
                    "message": {"mid": "m1", "text": "שלום"},
                }],
            }],
        }).encode("utf-8")
        resp = client_strict.post(
            "/webhooks/meta",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 200
        joined = "\n".join(r.message for r in caplog.records)
        assert "Meta inbound: channel=" in joined
        assert "text_len=" in joined

    def test_is_known_entry_db_failure_returns_false(self, monkeypatch):
        """אם הקריאה ל-DB נכשלת — מתייחסים כ-unknown ולא קורסים."""
        from messaging.meta_webhook import _is_known_entry

        def _broken_db_check(_eid):
            raise RuntimeError("DB closed")

        # פוגעים ב-db.is_meta_entry_known דרך הייבוא הפנימי
        import ai_chatbot.database as db_mod
        monkeypatch.setattr(db_mod, "is_meta_entry_known", _broken_db_check)
        assert _is_known_entry("any-id") is False

    def test_is_known_entry_empty_id_returns_false(self):
        from messaging.meta_webhook import _is_known_entry
        assert _is_known_entry(None) is False
        assert _is_known_entry("") is False
