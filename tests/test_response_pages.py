"""
טסטים לעמודי תשובה ציבוריים — response_pages.

מכסה: יצירה, קריאה, סניטציה, ואינטגרציה עם ה-webhook.
"""

import os
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def db_module(tmp_path):
    """מודול database עם DB זמני."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path), \
         patch("database.DB_PATH", db_path):
        import database as db_mod
        db_mod.init_db()
        yield db_mod


class TestResponsePagesCRUD:
    """טסטים ליצירה וקריאה של עמודי תשובה."""

    def test_create_and_get(self, db_module):
        """יצירת עמוד ושליפה לפי ID. slug חייב להיות לפחות 22 תווים
        base64url (128 ביט אנטרופיה) — ראה תיקון אבטחה ב-database.py.
        """
        page_id = db_module.create_response_page(
            content="<h2>מחירון</h2><p>תספורת - 80 ש\"ח</p>",
            title="מחירון",
            user_id="+972501234567",
        )
        # secrets.token_urlsafe(16) → 22 תווים. בעבר היה 8 (uuid.hex[:8])
        # אבל זה היה רק 32 ביט אנטרופיה — לא מספיק לעמוד ציבורי.
        assert len(page_id) >= 22

        page = db_module.get_response_page(page_id)
        assert page is not None
        assert page["title"] == "מחירון"
        assert "מחירון" in page["content"]
        assert page["user_id"] == "+972501234567"

    def test_get_nonexistent_returns_none(self, db_module):
        """שליפת עמוד שלא קיים מחזירה None."""
        assert db_module.get_response_page("notexist") is None

    def test_page_persists(self, db_module):
        """עמוד נשמר לצמיתות — אין תפוגה."""
        page_id = db_module.create_response_page(content="test", title="test")
        page = db_module.get_response_page(page_id)
        assert page is not None
        assert page["content"] == "test"

    def test_unique_ids(self, db_module):
        """כל עמוד מקבל ID ייחודי."""
        ids = set()
        for _ in range(20):
            page_id = db_module.create_response_page(content="test", title="test")
            ids.add(page_id)
        assert len(ids) == 20


class TestSendAsPageIntegration:
    """טסטים ללוגיקת _send_as_page ב-webhook."""

    def test_long_response_triggers_page(self, db_module):
        """תשובה ארוכה מ-WHATSAPP_MAX_LENGTH גורמת ליצירת עמוד."""
        long_text = "מחירון מלא: " + "• שירות X — 100 ש\"ח\n" * 200  # >1600 תווים

        with patch("ai_chatbot.config.WHATSAPP_MAX_LENGTH", 1600), \
             patch("ai_chatbot.config.ADMIN_URL", "https://example.com"):
            from messaging.formatter import format_message
            formatted = format_message(long_text, "whatsapp")
            assert len(formatted) > 1600

    def test_short_response_no_page(self, db_module):
        """תשובה קצרה לא יוצרת עמוד."""
        short_text = "מחירון: תספורת 80 ש\"ח"
        from messaging.formatter import format_message
        formatted = format_message(short_text, "whatsapp")
        assert len(formatted) <= 1600


class TestSanitizePageHtml:
    """טסטים לסניטציית HTML — מניעת XSS בעמודים ציבוריים."""

    def test_allowed_tags_preserved(self):
        """תגים מותרים נשמרים."""
        from llm import _sanitize_page_html
        html = '<h2 class="page-title">מחירון</h2><p>תיאור</p><table><tr><td>שירות</td></tr></table>'
        result = _sanitize_page_html(html)
        assert '<h2 class="page-title">' in result
        assert "<p>" in result
        assert "<table>" in result
        assert "<tr>" in result
        assert "<td>" in result

    def test_script_tags_stripped(self):
        """תגי script מוסרים לחלוטין."""
        from llm import _sanitize_page_html
        html = '<h2>כותרת</h2><script>alert("xss")</script><p>תוכן</p>'
        result = _sanitize_page_html(html)
        assert "<script>" not in result
        assert "alert" not in result or "<script>" not in result
        assert "<h2>" in result
        assert "<p>" in result

    def test_event_handlers_stripped(self):
        """תכונות on* (event handlers) מוסרות."""
        from llm import _sanitize_page_html
        html = '<p onclick="alert(1)" onmouseover="hack()">טקסט</p>'
        result = _sanitize_page_html(html)
        assert "onclick" not in result
        assert "onmouseover" not in result
        assert "<p>" in result
        assert "טקסט" in result

    def test_style_attribute_stripped(self):
        """תכונת style מוסרת."""
        from llm import _sanitize_page_html
        html = '<p style="background:url(evil)">טקסט</p>'
        result = _sanitize_page_html(html)
        assert "style" not in result
        assert "<p>" in result

    def test_iframe_stripped(self):
        """תגי iframe מוסרים."""
        from llm import _sanitize_page_html
        html = '<div>תוכן</div><iframe src="evil.com"></iframe>'
        result = _sanitize_page_html(html)
        assert "<iframe" not in result
        assert "<div>" in result

    def test_dir_attribute_preserved(self):
        """תכונת dir נשמרת (חשוב ל-RTL)."""
        from llm import _sanitize_page_html
        html = '<div dir="rtl">תוכן</div>'
        result = _sanitize_page_html(html)
        assert 'dir="rtl"' in result

    def test_nested_tag_bypass_blocked(self):
        """תגים מקוננים (<<script>script>) לא עוקפים את הסניטציה."""
        from llm import _sanitize_page_html
        html = '<<script>script>alert(1)</script>'
        result = _sanitize_page_html(html)
        assert "<script" not in result
        assert "alert(1)" not in result or "<script" not in result

    def test_double_angle_bracket_bypass(self):
        """עקיפה ע"י כפילות סוגריים — <<img>img onerror=alert(1)>."""
        from llm import _sanitize_page_html
        html = '<<img>img onerror=alert(1)//>'
        result = _sanitize_page_html(html)
        assert "<img" not in result
        assert "onerror" not in result


class TestGeneratePageContent:
    """טסטים לפונקציית generate_page_content."""

    @patch("llm.get_openai_client")
    def test_generates_html(self, mock_client):
        """הפונקציה מחזירה HTML מה-LLM."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "<h2>מחירון</h2><table><tr><td>תספורת</td><td>80₪</td></tr></table>"
        mock_client.return_value.chat.completions.create.return_value = mock_response

        from llm import generate_page_content
        result = generate_page_content("הנה המחירון שלנו! תספורת 80 ש\"ח", title="מחירון")
        assert "<h2>" in result
        assert "מחירון" in result

    @patch("llm.get_openai_client")
    def test_strips_markdown_code_fences(self, mock_client):
        """הסרת code fences של markdown שמודלים עוטפים בהם HTML."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '```html\n<h2>מחירון</h2>\n<p>תוכן</p>\n```'
        mock_client.return_value.chat.completions.create.return_value = mock_response

        from llm import generate_page_content
        result = generate_page_content("מחירון", title="מחירון")
        assert "```" not in result
        assert "<h2>" in result
        assert "<p>" in result

    @patch("llm.get_openai_client")
    def test_fallback_on_error(self, mock_client):
        """בכשל LLM — מחזירה את התוכן המקורי ב-div."""
        mock_client.return_value.chat.completions.create.side_effect = Exception("API error")

        from llm import generate_page_content
        result = generate_page_content("תוכן כלשהו", title="test")
        assert "תוכן כלשהו" in result
        assert "dir=\"rtl\"" in result


class TestSendWhatsAppResponseLengthGuard:
    """רגרסיה: הודעות ארוכות (כמו תגובת booking שמכילה מחירון) חייבות לעבור
    דרך מסלול עמוד HTML גם אם נשלחות מ-flow שלא ביצע בדיקת אורך מוקדמת
    (whatsapp_booking.py / agent / וכל קורא ל-_send_whatsapp_response).
    בלי הצ'ק המרכזי הזה — Twilio קוצץ את ההודעה בשקט באמצע משפט.
    """

    @patch("messaging.whatsapp_webhook._send_as_page")
    @patch("messaging.whatsapp_sender.send_whatsapp")
    def test_long_message_routes_to_page(self, mock_send, mock_send_as_page, monkeypatch):
        from messaging.whatsapp_webhook import _send_whatsapp_response
        # מבטיח ש-ADMIN_URL לא ריק כדי שהמסלול יופעל
        monkeypatch.setattr("messaging.whatsapp_webhook.ADMIN_URL", "https://example.com")
        long_text = "א" * 2000  # > 1600 = WHATSAPP_MAX_LENGTH ברירת מחדל

        _send_whatsapp_response("+972500000000", long_text)

        mock_send_as_page.assert_called_once()
        mock_send.assert_not_called()

    @patch("messaging.whatsapp_webhook._send_as_page")
    @patch("messaging.whatsapp_sender.send_whatsapp")
    def test_short_message_sends_directly(self, mock_send, mock_send_as_page, monkeypatch):
        from messaging.whatsapp_webhook import _send_whatsapp_response
        monkeypatch.setattr("messaging.whatsapp_webhook.ADMIN_URL", "https://example.com")

        _send_whatsapp_response("+972500000000", "הודעה קצרה")

        mock_send.assert_called_once()
        mock_send_as_page.assert_not_called()

    @patch("messaging.whatsapp_webhook._send_as_page")
    @patch("messaging.whatsapp_sender.send_whatsapp")
    def test_long_without_admin_url_falls_back(self, mock_send, mock_send_as_page, monkeypatch):
        """ללא ADMIN_URL — אין לאן להפנות לעמוד, נופל לשליחה רגילה."""
        from messaging.whatsapp_webhook import _send_whatsapp_response
        monkeypatch.setattr("messaging.whatsapp_webhook.ADMIN_URL", "")
        long_text = "א" * 2000

        _send_whatsapp_response("+972500000000", long_text)

        mock_send.assert_called_once()
        mock_send_as_page.assert_not_called()

    @patch("llm.generate_page_content")
    @patch("messaging.whatsapp_sender.send_whatsapp")
    def test_long_message_llm_failure_no_recursion(
        self, mock_send, mock_generate, monkeypatch,
    ):
        """רגרסיה ל-recursion: כש-_send_as_page נכשל ב-LLM, הוא מנסה לשלוח
        את הטקסט המקורי. לפני התיקון זה קרא חזרה ל-_send_whatsapp_response
        עם טקסט ארוך → נכנס שוב למסלול עמוד → recursion → stack overflow.
        כעת ה-fallback עובר דרך _send_whatsapp_raw שלא בודק אורך.
        """
        from messaging.whatsapp_webhook import _send_whatsapp_response
        monkeypatch.setattr("messaging.whatsapp_webhook.ADMIN_URL", "https://example.com")
        mock_generate.side_effect = Exception("LLM API error")
        long_text = "א" * 2000

        # אם יש recursion הטסט נכשל ב-RecursionError; אחרת עובר ב-call יחיד
        _send_whatsapp_response("+972500000000", long_text)

        # send_whatsapp נקרא פעם אחת בדיוק (fallback ב-_send_as_page)
        assert mock_send.call_count == 1
        assert mock_send.call_args[0][1] == long_text  # הטקסט המקורי

    @patch("llm.generate_page_content")
    @patch("messaging.whatsapp_sender.send_whatsapp")
    def test_long_message_db_failure_no_recursion(
        self, mock_send, mock_generate, monkeypatch,
    ):
        """רגרסיה: כשל DB ב-create_response_page → fallback → לא recursion.

        webhook משתמש ב-`from ai_chatbot import database as db`, ולכן הפץ' חייב
        להיות על האטריבוט של ה-wrapper, לא של database הגולמי.
        """
        from messaging.whatsapp_webhook import _send_whatsapp_response
        monkeypatch.setattr("messaging.whatsapp_webhook.ADMIN_URL", "https://example.com")
        mock_generate.return_value = "<h1>page</h1>"
        # פץ' על ה-wrapper שב-webhook משתמש ב-`db.create_response_page`
        monkeypatch.setattr(
            "ai_chatbot.database.create_response_page",
            MagicMock(side_effect=Exception("DB locked")),
        )
        long_text = "א" * 2000

        _send_whatsapp_response("+972500000000", long_text)

        assert mock_send.call_count == 1
        assert mock_send.call_args[0][1] == long_text
