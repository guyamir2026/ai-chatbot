"""
טסטים למודול ics_service — יצירת קבצי יומן .ics.
"""

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("OPENAI_API_KEY", "fake-key")

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_config():
    """מוק להגדרות config — שם עסק וכתובת."""
    with patch("ics_service.BUSINESS_NAME", "מספרת דנה"), \
         patch("ics_service.BUSINESS_ADDRESS", "דיזנגוף 50, תל אביב"):
        yield


class TestGenerateIcs:
    """טסטים ליצירת תוכן קובץ .ics."""

    def test_basic_ics_content(self):
        """קובץ .ics מכיל את כל השדות הנדרשים."""
        from ics_service import generate_ics

        result = generate_ics(
            service="תספורת גברים",
            preferred_date="2026-02-25",
            preferred_time="14:00",
        )

        assert isinstance(result, bytes)
        text = result.decode("utf-8")

        # בדיקת מבנה בסיסי
        assert "BEGIN:VCALENDAR" in text
        assert "END:VCALENDAR" in text
        assert "BEGIN:VEVENT" in text
        assert "END:VEVENT" in text

        # בדיקת שדות
        assert "SUMMARY:" in text
        assert "תספורת גברים" in text
        assert "מספרת דנה" in text
        assert "DTSTART;TZID=Asia/Jerusalem:20260225T140000" in text
        assert "DTEND;TZID=Asia/Jerusalem:20260225T150000" in text  # 60 דקות ברירת מחדל
        assert "LOCATION:" in text
        assert "דיזנגוף 50" in text

    def test_ics_alarm(self):
        """קובץ .ics מכיל תזכורת (VALARM)."""
        from ics_service import generate_ics

        result = generate_ics(
            service="מניקור",
            preferred_date="2026-03-10",
            preferred_time="09:30",
        )
        text = result.decode("utf-8")

        assert "BEGIN:VALARM" in text
        assert "TRIGGER:-PT60M" in text
        assert "END:VALARM" in text

    def test_custom_duration(self):
        """משך תור מותאם משנה את DTEND."""
        from ics_service import generate_ics

        result = generate_ics(
            service="צביעה",
            preferred_date="2026-04-01",
            preferred_time="10:00",
            duration_minutes=120,
        )
        text = result.decode("utf-8")

        assert "DTSTART;TZID=Asia/Jerusalem:20260401T100000" in text
        assert "DTEND;TZID=Asia/Jerusalem:20260401T120000" in text  # +120 דקות

    def test_crlf_line_endings(self):
        """קובץ .ics משתמש ב-CRLF כמפריד שורות (RFC 5545)."""
        from ics_service import generate_ics

        result = generate_ics(
            service="תספורת",
            preferred_date="2026-01-01",
            preferred_time="12:00",
        )
        text = result.decode("utf-8")
        assert "\r\n" in text

    def test_vtimezone_included(self):
        """קובץ .ics מכיל VTIMEZONE component (RFC 5545 §3.2.19)."""
        from ics_service import generate_ics

        result = generate_ics(
            service="תספורת",
            preferred_date="2026-02-25",
            preferred_time="14:00",
        )
        text = result.decode("utf-8")
        assert "BEGIN:VTIMEZONE" in text
        assert "TZID:Asia/Jerusalem" in text
        assert "END:VTIMEZONE" in text
        assert "BEGIN:STANDARD" in text
        assert "BEGIN:DAYLIGHT" in text

    def test_no_address(self):
        """כשאין כתובת — אין שדה LOCATION באירוע (רק X-LIC-LOCATION ב-VTIMEZONE)."""
        from ics_service import generate_ics

        with patch("ics_service.BUSINESS_ADDRESS", ""):
            result = generate_ics(
                service="ייעוץ",
                preferred_date="2026-05-15",
                preferred_time="16:00",
            )
        text = result.decode("utf-8")
        # VEVENT לא אמור להכיל LOCATION (רק VTIMEZONE מכיל X-LIC-LOCATION)
        vevent = text.split("BEGIN:VEVENT")[1].split("END:VEVENT")[0]
        assert "LOCATION:" not in vevent

    def test_invalid_date_raises(self):
        """תאריך לא חוקי מעלה שגיאה."""
        from ics_service import generate_ics

        with pytest.raises(ValueError):
            generate_ics(
                service="תספורת",
                preferred_date="not-a-date",
                preferred_time="10:00",
            )

    def test_description_included(self):
        """תיאור אופציונלי מופיע בקובץ."""
        from ics_service import generate_ics

        result = generate_ics(
            service="תספורת",
            preferred_date="2026-06-01",
            preferred_time="11:00",
            description="תור במספרת דנה",
        )
        text = result.decode("utf-8")
        assert "DESCRIPTION:" in text
        assert "תור במספרת דנה" in text


class TestGenerateIcsFilename:
    """טסטים ליצירת שם קובץ .ics."""

    def test_filename_format(self):
        from ics_service import generate_ics_filename

        name = generate_ics_filename("2026-02-25")
        assert name == "appointment_2026-02-25.ics"
        assert name.endswith(".ics")


class TestBuildIcsPreview:
    """טסטים לתצוגה מקדימה של שדות .ics."""

    def test_preview_fields(self):
        from ics_service import build_ics_preview

        preview = build_ics_preview(
            service="תספורת גברים",
            preferred_date="2026-02-25",
            preferred_time="14:00",
        )

        assert preview["summary"] == "תספורת גברים — מספרת דנה"
        assert preview["dtstart"] == "25/02/2026 14:00"
        assert preview["dtend"] == "25/02/2026 15:00"
        assert "דיזנגוף 50" in preview["location"]
        assert "דקות" in preview["reminder"]

    def test_preview_empty_on_bad_date(self):
        from ics_service import build_ics_preview

        preview = build_ics_preview(
            service="תספורת",
            preferred_date="bad",
            preferred_time="10:00",
        )
        assert preview == {}


class TestIcsEscape:
    """טסטים ל-escape של תווים מיוחדים."""

    def test_escapes_special_chars(self):
        from ics_service import _ics_escape

        assert _ics_escape("hello;world") == "hello\\;world"
        assert _ics_escape("a,b") == "a\\,b"
        assert _ics_escape("line1\nline2") == "line1\\nline2"
        assert _ics_escape("back\\slash") == "back\\\\slash"
