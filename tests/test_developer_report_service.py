"""
טסטים למודול developer_report_service — דיווחי באגים למפתח (טלגרם + מייל).
"""

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("OPENAI_API_KEY", "fake-key")

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_config():
    """מוק להגדרות config — טלגרם + מייל."""
    with patch("developer_report_service.DEVELOPER_BOT_TOKEN", "fake-dev-token"), \
         patch("developer_report_service.DEVELOPER_CHAT_ID", "123456"), \
         patch("developer_report_service.DEVELOPER_EMAIL", ""), \
         patch("developer_report_service.SMTP_HOST", ""), \
         patch("developer_report_service.SMTP_PORT", 465), \
         patch("developer_report_service.SMTP_USER", ""), \
         patch("developer_report_service.SMTP_PASSWORD", ""), \
         patch("developer_report_service.BUSINESS_NAME", "מספרת דנה"):
        yield


class TestIsConfigured:
    """בדיקת פונקציית is_configured."""

    def test_configured_with_telegram(self):
        from developer_report_service import is_configured
        assert is_configured() is True

    def test_configured_with_email_only(self):
        from developer_report_service import is_configured
        with patch("developer_report_service.DEVELOPER_BOT_TOKEN", ""), \
             patch("developer_report_service.DEVELOPER_CHAT_ID", ""), \
             patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass"):
            assert is_configured() is True

    def test_not_configured_when_nothing_set(self):
        from developer_report_service import is_configured
        with patch("developer_report_service.DEVELOPER_BOT_TOKEN", ""), \
             patch("developer_report_service.DEVELOPER_CHAT_ID", ""):
            assert is_configured() is False

    def test_telegram_not_configured_when_token_missing(self):
        from developer_report_service import _telegram_configured
        with patch("developer_report_service.DEVELOPER_BOT_TOKEN", ""):
            assert _telegram_configured() is False

    def test_email_not_configured_when_email_missing(self):
        from developer_report_service import _email_configured
        assert _email_configured() is False

    def test_email_configured_when_all_set(self):
        from developer_report_service import _email_configured
        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass"):
            assert _email_configured() is True


class TestAllowedFile:
    """בדיקת סינון סיומות קבצים."""

    def test_allowed_extensions(self):
        from developer_report_service import allowed_file
        assert allowed_file("screenshot.png") is True
        assert allowed_file("photo.jpg") is True
        assert allowed_file("image.jpeg") is True
        assert allowed_file("anim.gif") is True
        assert allowed_file("modern.webp") is True

    def test_rejected_extensions(self):
        from developer_report_service import allowed_file
        assert allowed_file("virus.exe") is False
        assert allowed_file("script.py") is False
        assert allowed_file("doc.pdf") is False

    def test_no_extension(self):
        from developer_report_service import allowed_file
        assert allowed_file("noextension") is False


class TestEscapeMarkdown:
    """בדיקת escape לתווים מיוחדים ב-MarkdownV2."""

    def test_special_chars_escaped(self):
        from developer_report_service import _escape_markdown
        result = _escape_markdown("הבוט *לא* עובד! (בעיה #1)")
        assert "\\*" in result
        assert "\\!" in result
        assert "\\(" in result
        assert "\\#" in result

    def test_plain_text_unchanged(self):
        from developer_report_service import _escape_markdown
        result = _escape_markdown("טקסט רגיל בעברית")
        assert result == "טקסט רגיל בעברית"

    def test_backslash_escaped(self):
        from developer_report_service import _escape_markdown
        result = _escape_markdown(r"path\to\file")
        assert r"path\\to\\file" == result


class TestMimeFromFilename:
    """בדיקת הסקת MIME type מסיומת קובץ."""

    def test_png(self):
        from developer_report_service import _mime_from_filename
        assert _mime_from_filename("screenshot.png") == "image/png"

    def test_jpg(self):
        from developer_report_service import _mime_from_filename
        assert _mime_from_filename("photo.jpg") == "image/jpeg"

    def test_gif(self):
        from developer_report_service import _mime_from_filename
        assert _mime_from_filename("anim.gif") == "image/gif"

    def test_webp(self):
        from developer_report_service import _mime_from_filename
        assert _mime_from_filename("modern.webp") == "image/webp"

    def test_unknown_defaults_to_jpeg(self):
        from developer_report_service import _mime_from_filename
        assert _mime_from_filename("noextension") == "image/jpeg"


class TestSendTelegram:
    """בדיקות שליחת דיווח לטלגרם."""

    @patch("developer_report_service.http_requests.post")
    def test_sends_text_message(self, mock_post):
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=True)
        result = send_report_to_developer("הבוט לא עונה", report_id=42)

        assert result is True
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["chat_id"] == "123456"
        assert "42" in call_kwargs.kwargs["json"]["text"]

    @patch("developer_report_service.http_requests.post")
    def test_sends_with_screenshots(self, mock_post):
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=True)

        screenshots = [
            ("screen1.png", b"fake-png-data"),
            ("screen2.jpg", b"fake-jpg-data"),
        ]
        result = send_report_to_developer(
            "בעיה בתצוגה", report_id=7, screenshots=screenshots
        )

        assert result is True
        # קריאה ראשונה — הודעת טקסט, שתיים נוספות — תמונות
        assert mock_post.call_count == 3

    @patch("developer_report_service.http_requests.post")
    def test_returns_false_on_failure(self, mock_post):
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=False, text="Unauthorized")
        result = send_report_to_developer("באג", report_id=1)
        assert result is False

    def test_skips_when_not_configured(self):
        from developer_report_service import send_report_to_developer

        with patch("developer_report_service.DEVELOPER_BOT_TOKEN", ""), \
             patch("developer_report_service.DEVELOPER_CHAT_ID", ""):
            result = send_report_to_developer("באג", report_id=1)
            assert result is False

    @patch("developer_report_service.http_requests.post")
    def test_handles_network_error(self, mock_post):
        from developer_report_service import send_report_to_developer

        mock_post.side_effect = Exception("Connection timeout")
        result = send_report_to_developer("באג", report_id=1)
        assert result is False

    @patch("developer_report_service.http_requests.post")
    def test_business_name_in_message(self, mock_post):
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=True)
        send_report_to_developer("בעיה", report_id=1)
        text = mock_post.call_args.kwargs["json"]["text"]
        assert "מספרת דנה" in text


class TestSendEmail:
    """בדיקות שליחת דיווח למייל."""

    @patch("developer_report_service.smtplib.SMTP_SSL")
    def test_sends_email_successfully(self, mock_smtp_class):
        """שליחת מייל בסיסית עם SMTP."""
        from developer_report_service import _send_email

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass123"):

            result = _send_email("באג בתצוגה", report_id=5)

        assert result is True
        mock_server.login.assert_called_once()
        mock_server.send_message.assert_called_once()

        # בדיקת תוכן המייל
        sent_msg = mock_server.send_message.call_args[0][0]
        assert "5" in sent_msg["Subject"]
        assert "מספרת דנה" in sent_msg["Subject"]
        assert sent_msg["To"] == "dev@test.com"

    @patch("developer_report_service.smtplib.SMTP_SSL")
    def test_sends_email_with_attachments(self, mock_smtp_class):
        """מייל עם צילומי מסך כצרופות."""
        from developer_report_service import _send_email

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        screenshots = [("bug.png", b"fake-png")]

        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass123"):

            result = _send_email("באג", report_id=1, screenshots=screenshots)

        assert result is True
        sent_msg = mock_server.send_message.call_args[0][0]
        # בדיקה שיש צרופה — iter_attachments מחזיר generator
        attachments = list(sent_msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "bug.png"

    @patch("developer_report_service.smtplib.SMTP_SSL")
    def test_returns_false_on_smtp_error(self, mock_smtp_class):
        """מחזיר False כשה-SMTP נכשל."""
        from developer_report_service import _send_email

        mock_smtp_class.side_effect = Exception("SMTP connection refused")

        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass123"):

            result = _send_email("באג", report_id=1)

        assert result is False


class TestDualChannel:
    """בדיקות שליחה משולבת — טלגרם + מייל."""

    @patch("developer_report_service.smtplib.SMTP_SSL")
    @patch("developer_report_service.http_requests.post")
    def test_sends_to_both_channels(self, mock_post, mock_smtp_class):
        """כששניהם מוגדרים — שולח לשניהם."""
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=True)
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass123"):

            result = send_report_to_developer("באג", report_id=1)

        assert result is True
        mock_post.assert_called_once()  # טלגרם
        mock_server.send_message.assert_called_once()  # מייל

    @patch("developer_report_service.smtplib.SMTP_SSL")
    @patch("developer_report_service.http_requests.post")
    def test_succeeds_if_only_email_works(self, mock_post, mock_smtp_class):
        """מצליח גם כשטלגרם נכשל אבל מייל עובד."""
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=False, text="Telegram error")
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        with patch("developer_report_service.DEVELOPER_EMAIL", "dev@test.com"), \
             patch("developer_report_service.SMTP_HOST", "smtp.gmail.com"), \
             patch("developer_report_service.SMTP_PASSWORD", "pass123"):

            result = send_report_to_developer("באג", report_id=1)

        assert result is True  # מייל הציל

    @patch("developer_report_service.http_requests.post")
    def test_succeeds_if_only_telegram_works(self, mock_post):
        """מצליח כשמייל לא מוגדר אבל טלגרם עובד."""
        from developer_report_service import send_report_to_developer

        mock_post.return_value = MagicMock(ok=True)
        result = send_report_to_developer("באג", report_id=1)
        assert result is True


class TestDatabaseFunctions:
    """בדיקות פונקציות DB לדיווחים (דורשות DB זמני)."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with patch("ai_chatbot.config.DB_PATH", db_path), \
             patch("database.DB_PATH", db_path):
            from database import init_db
            init_db()
            yield

    def test_save_and_get_reports(self):
        from database import save_developer_report, get_developer_reports

        report_id = save_developer_report("בעיה ראשונה", screenshot_count=2)
        assert report_id is not None

        reports = get_developer_reports()
        assert len(reports) >= 1
        assert reports[0]["description"] == "בעיה ראשונה"
        assert reports[0]["screenshot_count"] == 2
        assert reports[0]["status"] == "open"

    def test_update_status_to_resolved(self):
        from database import (
            save_developer_report,
            get_developer_reports,
            update_developer_report_status,
        )

        report_id = save_developer_report("באג")
        result = update_developer_report_status(report_id, "resolved")
        assert result is True

        reports = get_developer_reports()
        assert reports[0]["status"] == "resolved"
        assert reports[0]["resolved_at"] is not None

    def test_reopen_report(self):
        from database import (
            save_developer_report,
            get_developer_reports,
            update_developer_report_status,
        )

        report_id = save_developer_report("באג")
        update_developer_report_status(report_id, "resolved")
        update_developer_report_status(report_id, "open")

        reports = get_developer_reports()
        assert reports[0]["status"] == "open"

    def test_reports_ordered_newest_first(self):
        from database import save_developer_report, get_developer_reports

        save_developer_report("ראשון")
        save_developer_report("שני")
        save_developer_report("שלישי")

        reports = get_developer_reports()
        assert reports[0]["description"] == "שלישי"
        assert reports[2]["description"] == "ראשון"
