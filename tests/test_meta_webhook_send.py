"""
טסטים על שכבת השליחה ב-meta_webhook.py:
- _send_meta_response: בדיקת אורך + ניתוב לעמוד.
- _send_meta_raw: שליפת credentials נכונים לפי channel.

לא קוראים ל-Graph API אמיתי — send_meta_message ממוקה.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patch_send(monkeypatch):
    """מוקה את send_meta_message ומחזיר את ה-MagicMock לבדיקות."""
    import messaging.meta_webhook as mw
    import messaging.meta_sender as ms
    sent = MagicMock(return_value="MID_X")
    # patch גם בתוך meta_sender (במקור) וגם בייבוא local של webhook
    monkeypatch.setattr(ms, "send_meta_message", sent)
    return sent


@pytest.fixture
def patch_db(monkeypatch):
    """מוקה את ai_chatbot.database לקריאות credentials.

    הערה חשובה: `_send_meta_raw` עושה `from ai_chatbot import database as db`
    מבפנים, מה שמתורגם ל-`getattr(ai_chatbot, 'database')` — לא לקריאה
    מ-`sys.modules`. לכן הצבת mock רק ב-sys.modules לא תופסת. צריך לעשות
    setattr על האובייקט של ai_chatbot עצמו (וגם sys.modules למקרה
    שיש קוד אחר שכן קורא משם).
    """
    import sys
    import ai_chatbot
    # מאלץ ייבוא של submodule כדי שאטריביוט 'database' יקיים בunmocked
    # state ראשון — ai_chatbot/__init__.py לא מייצא אותו אוטומטית.
    import ai_chatbot.database  # noqa: F401

    db_mock = MagicMock()
    db_mock.get_meta_credentials_by_page_id.return_value = {
        "page_id": "PAGE_1",
        "access_token": "page-tok-1",
    }
    db_mock.get_meta_credentials_by_ig_account.return_value = {
        "page_id": "PAGE_1",
        "access_token": "page-tok-1",
        "ig_business_account_id": "IGBA_1",
    }
    monkeypatch.setattr(ai_chatbot, "database", db_mock)
    monkeypatch.setitem(sys.modules, "ai_chatbot.database", db_mock)
    return db_mock


class TestSendMetaRawChannelRouting:
    def test_ig_uses_igba_lookup(self, patch_send, patch_db):
        from messaging.meta_webhook import _send_meta_raw
        _send_meta_raw("meta_ig:IGSID_X", "שלום", "IGBA_1")
        patch_db.get_meta_credentials_by_ig_account.assert_called_once_with("IGBA_1")
        patch_db.get_meta_credentials_by_page_id.assert_not_called()
        # send_meta_message נקרא עם ה-recipient הטהור
        args, _ = patch_send.call_args
        assert args[0] == "IGSID_X"  # אחרי to_provider_recipient
        assert args[1] == "שלום"
        assert args[2] == "page-tok-1"

    def test_msg_uses_page_id_lookup(self, patch_send, patch_db):
        from messaging.meta_webhook import _send_meta_raw
        _send_meta_raw("meta_msg:PSID_Y", "hi", "PAGE_1")
        patch_db.get_meta_credentials_by_page_id.assert_called_once_with("PAGE_1")
        patch_db.get_meta_credentials_by_ig_account.assert_not_called()

    def test_strips_html_tags_before_send(self, patch_send, patch_db):
        """תגי HTML (<b>) מוסרים לפני שליחה — Meta DM הוא plain text.
        בלי זה מטא מציגה את התגים גולמיים (הבאג מהפרודקשן)."""
        from messaging.meta_webhook import _send_meta_raw
        _send_meta_raw("meta_msg:PSID", "<b>מחירון</b>: 250₪", "PAGE_1")
        sent_text = patch_send.call_args.args[1]
        assert "<b>" not in sent_text and "</b>" not in sent_text
        assert "מחירון" in sent_text and "250₪" in sent_text

    def test_no_credentials_logs_and_returns(self, patch_send, patch_db, caplog):
        from messaging.meta_webhook import _send_meta_raw
        patch_db.get_meta_credentials_by_page_id.return_value = None
        _send_meta_raw("meta_msg:PSID", "x", "UNKNOWN_PAGE")
        patch_send.assert_not_called()
        assert "אין credentials" in caplog.text

    def test_invalid_user_id_logs_and_returns(self, patch_send, patch_db, caplog):
        from messaging.meta_webhook import _send_meta_raw
        _send_meta_raw("telegram:123", "x", "ASSET")
        patch_send.assert_not_called()
        # לא נשלח כי הערוץ לא מטא
        assert patch_db.get_meta_credentials_by_page_id.call_count == 0


class TestSendMetaResponseLengthCheck:
    def test_short_message_sent_directly(self, patch_send, patch_db, monkeypatch):
        from messaging.meta_webhook import _send_meta_response
        monkeypatch.setattr(
            "ai_chatbot.config.ADMIN_URL", "https://admin.example.com"
        )
        _send_meta_response("meta_ig:U", "קצר", "IGBA_1")
        patch_send.assert_called_once()

    def test_long_message_routed_to_page(self, patch_send, patch_db, monkeypatch):
        """הודעה ארוכה יוצרת עמוד ושולחת קישור קצר במקום."""
        from messaging.meta_webhook import _send_meta_response
        from ai_chatbot.config import META_INSTAGRAM_MAX_LENGTH

        monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "https://admin.example.com")

        # patch generate_page_content ו-create_response_page
        import llm
        monkeypatch.setattr(llm, "generate_page_content",
                            lambda text, **kw: f"<html>{text}</html>")
        patch_db.create_response_page = MagicMock(return_value="PAGE_TOKEN_ABC")

        long_text = "א" * (META_INSTAGRAM_MAX_LENGTH + 100)
        _send_meta_response("meta_ig:U", long_text, "IGBA_1")

        # נשלחה הודעה אחת — הקישור הקצר, לא הטקסט המקורי
        patch_send.assert_called_once()
        args, _ = patch_send.call_args
        sent_text = args[1]
        assert "PAGE_TOKEN_ABC" in sent_text
        assert "https://admin.example.com" in sent_text
        # ה-html של הטקסט הארוך נכנס ל-create_response_page
        patch_db.create_response_page.assert_called_once()
        kwargs = patch_db.create_response_page.call_args.kwargs
        assert kwargs["page_type"] == "meta_fallback"
        assert kwargs["user_id"] == "meta_ig:U"

    def test_long_message_creates_only_one_response_page(self, patch_send, patch_db, monkeypatch):
        """regression — בעבר _send_meta_as_page סיים בקריאה ל-_send_meta_response
        עבור הקישור הקצר, מה שיצר סיכון לרקורסיה (כל סיבוב יוצר response_page
        חדש). חייב להיות בדיוק עמוד אחד לכל הודעה ארוכה."""
        from messaging.meta_webhook import _send_meta_response
        from ai_chatbot.config import META_INSTAGRAM_MAX_LENGTH
        monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "https://admin.example.com")
        import llm
        monkeypatch.setattr(llm, "generate_page_content", lambda t, **k: f"<p>{t}</p>")
        patch_db.create_response_page = MagicMock(return_value="PG_ONE")

        long_text = "ג" * (META_INSTAGRAM_MAX_LENGTH + 200)
        _send_meta_response("meta_ig:U", long_text, "IGBA_1")

        # בדיוק עמוד אחד נוצר, ובדיוק שליחה אחת בוצעה
        assert patch_db.create_response_page.call_count == 1
        assert patch_send.call_count == 1

    def test_long_without_admin_url_falls_to_raw(self, patch_send, patch_db, monkeypatch):
        """בלי ADMIN_URL אין לאן להפנות — נופלים לשליחה רגילה."""
        from messaging.meta_webhook import _send_meta_response
        from ai_chatbot.config import META_MESSENGER_MAX_LENGTH
        monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "")
        long_text = "ב" * (META_MESSENGER_MAX_LENGTH + 50)
        _send_meta_response("meta_msg:U", long_text, "PAGE_1")
        # הוא כן שולח, אבל את הטקסט הארוך כמו שהוא (best-effort)
        patch_send.assert_called_once()
        sent_text = patch_send.call_args.args[1]
        assert len(sent_text) > META_MESSENGER_MAX_LENGTH

    def test_ig_threshold_lower_than_messenger(self, patch_send, patch_db, monkeypatch):
        """הסף של IG (1000) נמוך מהסף של Messenger (2000) — טקסט באמצע
        יוצא לעמוד ב-IG אבל ישיר ב-Messenger."""
        from messaging.meta_webhook import _send_meta_response
        from ai_chatbot.config import (
            META_INSTAGRAM_MAX_LENGTH,
            META_MESSENGER_MAX_LENGTH,
        )
        assert META_INSTAGRAM_MAX_LENGTH < META_MESSENGER_MAX_LENGTH
        monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "https://a.example.com")
        import llm
        monkeypatch.setattr(llm, "generate_page_content", lambda t, **k: f"<p>{t}</p>")
        patch_db.create_response_page = MagicMock(return_value="PG")
        mid_len = (META_INSTAGRAM_MAX_LENGTH + META_MESSENGER_MAX_LENGTH) // 2
        text = "א" * mid_len

        # IG — מעבר לסף, אמור להגיע לעמוד (קישור קצר נשלח)
        _send_meta_response("meta_ig:U_IG", text, "IGBA_1")
        sent_text_ig = patch_send.call_args.args[1]
        assert "PG" in sent_text_ig

        # Messenger — תחת הסף, אמור להישלח כמו שהוא
        patch_send.reset_mock()
        _send_meta_response("meta_msg:U_MSG", text, "PAGE_1")
        sent_text_msg = patch_send.call_args.args[1]
        assert sent_text_msg == text
