"""
טסטים לחיבור inbound של מטא ל-RAG (`_handle_meta_message`).

מה נבדק:
- live_chat guard — אם פעיל, ההודעה נשמרת אבל לא נשלחת תשובה.
- בנייה נכונה של internal_user_id (`meta_ig:<igsid>`).
- upsert_user מקבל provider_asset_id + external_user_id.
- process_incoming_message מקבל את ה-user_id המנורמל ואת ה-channel.
- result.text נשלח דרך _send_meta_response (עם asset_id).
- טקסט ריק / attachment בלבד — לא שולחים כלום.
- כשל בהודעה אחת לא קורס את ה-webhook (התנהגות לולאת I/O).
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patch_pipeline(monkeypatch):
    """מוקה את כל התלויות של _handle_meta_message."""
    import sys
    import ai_chatbot
    import ai_chatbot.database  # מאלץ סאב-מודול להירשם כאטריביוט (ראה test_meta_webhook_send.py)

    db_mock = MagicMock()
    db_mock.get_consecutive_fallbacks.return_value = 0
    monkeypatch.setattr(ai_chatbot, "database", db_mock)
    monkeypatch.setitem(sys.modules, "ai_chatbot.database", db_mock)

    # live_chat — ברירת מחדל: לא פעיל
    import ai_chatbot.live_chat_service as lcs
    monkeypatch.setattr(lcs.LiveChatService, "is_active", MagicMock(return_value=False))

    # process_incoming_message — מוקה כדי לא להפעיל LLM אמיתי
    import core.message_processor as mp
    fake_result = MagicMock()
    fake_result.text = "תשובה מ-RAG"
    fake_result.intent = None
    fake_result.action = ""
    fake_result.consecutive_fallbacks = 0
    process_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr(mp, "process_incoming_message", process_mock)

    # _send_meta_response — מוקה כדי לאמת שנקרא נכון
    import messaging.meta_webhook as mw
    send_mock = MagicMock()
    monkeypatch.setattr(mw, "_send_meta_response", send_mock)

    return {
        "db": db_mock,
        "live_chat": lcs.LiveChatService.is_active,
        "process": process_mock,
        "send": send_mock,
        "result": fake_result,
    }


def _make_msg(channel="meta_ig", sender="IGSID_A", entry="IGBA_X", text="שלום"):
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


class TestHappyPath:
    def test_ig_message_processed_and_response_sent(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg())

        # upsert_user נקרא עם user_id מנורמל + שדות מטא
        patch_pipeline["db"].upsert_user.assert_called_once()
        kwargs = patch_pipeline["db"].upsert_user.call_args.kwargs
        assert kwargs["user_id"] == "meta_ig:IGSID_A"
        assert kwargs["channel"] == "meta_ig"
        assert kwargs["provider_asset_id"] == "IGBA_X"
        assert kwargs["external_user_id"] == "IGSID_A"

        # process_incoming_message קיבל את ה-user_id המנורמל
        patch_pipeline["process"].assert_called_once()
        proc_kwargs = patch_pipeline["process"].call_args.kwargs
        assert proc_kwargs["user_id"] == "meta_ig:IGSID_A"
        assert proc_kwargs["channel"] == "meta_ig"
        assert proc_kwargs["text"] == "שלום"

        # _send_meta_response נקרא עם הטקסט והאסט
        patch_pipeline["send"].assert_called_once_with(
            "meta_ig:IGSID_A", "תשובה מ-RAG", "IGBA_X"
        )

    def test_messenger_uses_meta_msg_prefix(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg(channel="meta_msg", sender="PSID_B", entry="PAGE_1"))

        proc_kwargs = patch_pipeline["process"].call_args.kwargs
        assert proc_kwargs["user_id"] == "meta_msg:PSID_B"
        assert proc_kwargs["channel"] == "meta_msg"
        patch_pipeline["send"].assert_called_once_with(
            "meta_msg:PSID_B", "תשובה מ-RAG", "PAGE_1"
        )


class TestLiveChatGuard:
    def test_active_live_chat_blocks_response(self, patch_pipeline, monkeypatch):
        """live_chat פעיל ⇒ ההודעה נשמרת, אבל RAG/שליחה לא קורות."""
        import ai_chatbot.live_chat_service as lcs
        monkeypatch.setattr(lcs.LiveChatService, "is_active", MagicMock(return_value=True))

        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg())

        # ההודעה נשמרת + touch_live_chat
        patch_pipeline["db"].save_message.assert_called_once()
        patch_pipeline["db"].touch_live_chat.assert_called_once_with("meta_ig:IGSID_A")
        # אבל RAG לא רץ ולא נשלחה תשובה
        patch_pipeline["process"].assert_not_called()
        patch_pipeline["send"].assert_not_called()


class TestEmptyOrIncompleteMessages:
    def test_empty_text_skipped(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg(text=""))
        patch_pipeline["process"].assert_not_called()
        patch_pipeline["send"].assert_not_called()

    def test_whitespace_only_skipped(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg(text="   \n  "))
        patch_pipeline["process"].assert_not_called()
        patch_pipeline["send"].assert_not_called()

    def test_missing_sender_skipped(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg(sender=None))
        patch_pipeline["process"].assert_not_called()

    def test_missing_entry_skipped(self, patch_pipeline):
        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg(entry=None))
        patch_pipeline["process"].assert_not_called()


class TestFallbacksRoundtrip:
    def test_consecutive_fallbacks_persisted_when_changed(self, patch_pipeline):
        """אם processor החזיר fallback count שונה, נשמר ל-DB."""
        patch_pipeline["db"].get_consecutive_fallbacks.return_value = 0
        patch_pipeline["result"].consecutive_fallbacks = 2

        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg())
        patch_pipeline["db"].set_consecutive_fallbacks.assert_called_once_with(
            "meta_ig:IGSID_A", 2
        )

    def test_no_db_write_when_unchanged(self, patch_pipeline):
        patch_pipeline["db"].get_consecutive_fallbacks.return_value = 0
        patch_pipeline["result"].consecutive_fallbacks = 0

        from messaging.meta_webhook import _handle_meta_message
        _handle_meta_message(_make_msg())
        patch_pipeline["db"].set_consecutive_fallbacks.assert_not_called()


class TestInboundLoopResilience:
    def test_one_bad_message_does_not_stop_others(
        self, patch_pipeline, monkeypatch
    ):
        """כשל ב-_handle_meta_message להודעה אחת ⇒ webhook ממשיך,
        עונה 200, מטפל ב-good messages הנותרים."""
        from messaging import meta_webhook as mw

        # extract מחזיר 3 הודעות, כל ה-entries מוכרים
        messages = [
            _make_msg(sender="A"),
            _make_msg(sender="B"),
            _make_msg(sender="C"),
        ]
        monkeypatch.setattr(mw, "_extract_inbound_messages", lambda p: messages)
        monkeypatch.setattr(mw, "_is_known_entry", lambda _e: True)
        monkeypatch.setattr(mw, "_verify_signature", lambda *_a: True)

        # handle נכשל באמצע (B), אחרים עוברים
        original = mw._handle_meta_message
        calls = []

        def flaky_handle(m):
            calls.append(m["sender_id"])
            if m["sender_id"] == "B":
                raise RuntimeError("simulated DB failure")
            original(m)

        monkeypatch.setattr(mw, "_handle_meta_message", flaky_handle)

        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(mw.meta_bp)
        with app.test_client() as client:
            resp = client.post(
                "/webhooks/meta",
                data=b"{}",
                content_type="application/json",
                headers={"X-Hub-Signature-256": "sha256=fake"},
            )
        # 200 למרות הכשל; כל 3 ההודעות עברו דרך handle
        assert resp.status_code == 200
        assert calls == ["A", "B", "C"]
