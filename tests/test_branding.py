"""
טסטים למודול utils/branding.py — עיבוד לוגו, אחסון ב-DB, ושילוב ב-QR.
מכסים:
- ולידציה (פורמט, גודל, קובץ ריק/לא תקין)
- pad לריבוע אם המקור מלבני
- resize ל-2048x2048 אם גדול
- שמירה/קריאה/מחיקה ב-DB
- overlay על QR (תוצאה PNG תקין, גודל לוגו ~22%)
- routes של admin: /branding GET/POST upload/delete + serve
- שילוב לוגו ב-/qr-code/download עם ?with_logo=1
"""

import importlib
import io
import os
from unittest.mock import patch

import pytest
from PIL import Image


# ── עזרי בנייה של תמונות מבחן ──────────────────────────────────────────────

def _make_png_bytes(size: tuple[int, int], color=(255, 0, 0, 255)) -> bytes:
    """בונה PNG פשוט בגודל ובצבע הנתונים."""
    img = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(size: tuple[int, int]) -> bytes:
    img = Image.new("RGB", size, (0, 128, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ── process_uploaded_logo ──────────────────────────────────────────────────


class TestProcessUploadedLogo:
    def test_square_png_returned_as_png(self):
        from utils.branding import process_uploaded_logo
        raw = _make_png_bytes((300, 300))
        out, mime = process_uploaded_logo(raw)
        assert mime == "image/png"
        assert out[:8] == b"\x89PNG\r\n\x1a\n"
        # גודל זהה (לא חורג מ-MAX_LOGO_DIM, לא צריך resize)
        result = Image.open(io.BytesIO(out))
        assert result.size == (300, 300)

    def test_jpeg_converted_to_png(self):
        from utils.branding import process_uploaded_logo
        raw = _make_jpeg_bytes((200, 200))
        out, mime = process_uploaded_logo(raw)
        assert mime == "image/png"  # תמיד PNG בפלט
        assert out[:8] == b"\x89PNG\r\n\x1a\n"

    def test_rectangular_padded_to_square(self):
        """לוגו מלבני 400x200 → ריבוע 400x400 עם רקע שקוף בצדדים."""
        from utils.branding import process_uploaded_logo
        raw = _make_png_bytes((400, 200))
        out, _ = process_uploaded_logo(raw)
        result = Image.open(io.BytesIO(out))
        assert result.width == result.height
        assert result.size == (400, 400)  # מתחת ל-MAX_LOGO_DIM אז לא יורד
        # פיקסל בפינה (שהיה מחוץ למקור) צריך להיות שקוף
        result = result.convert("RGBA")
        top_left_alpha = result.getpixel((0, 0))[3]
        assert top_left_alpha == 0

    def test_large_rectangular_padded_keeps_full_resolution(self):
        """1500x800 → pad ל-1500x1500 (מתחת ל-MAX_LOGO_DIM=2048, לא יורד)."""
        from utils.branding import process_uploaded_logo
        raw = _make_png_bytes((1500, 800))
        out, _ = process_uploaded_logo(raw)
        result = Image.open(io.BytesIO(out))
        assert result.size == (1500, 1500)

    def test_oversized_resized_to_max(self):
        """לוגו 4000x4000 → 2048x2048 (cap)."""
        from utils.branding import process_uploaded_logo, MAX_LOGO_DIM
        raw = _make_png_bytes((4000, 4000))
        out, _ = process_uploaded_logo(raw)
        result = Image.open(io.BytesIO(out))
        assert result.size == (MAX_LOGO_DIM, MAX_LOGO_DIM)

    def test_empty_bytes_rejected(self):
        from utils.branding import process_uploaded_logo, LogoValidationError
        with pytest.raises(LogoValidationError, match="ריק"):
            process_uploaded_logo(b"")

    def test_oversized_file_rejected(self):
        """קובץ מעל 5MB נדחה לפני שאפילו פותחים אותו."""
        from utils.branding import process_uploaded_logo, LogoValidationError, MAX_UPLOAD_BYTES
        raw = b"x" * (MAX_UPLOAD_BYTES + 1)
        with pytest.raises(LogoValidationError, match="גדול"):
            process_uploaded_logo(raw)

    def test_invalid_image_bytes_rejected(self):
        from utils.branding import process_uploaded_logo, LogoValidationError
        with pytest.raises(LogoValidationError, match="תמונה"):
            process_uploaded_logo(b"not an image at all")

    def test_unsupported_format_rejected(self, tmp_path):
        """פורמט לא נתמך (BMP) נדחה."""
        from utils.branding import process_uploaded_logo, LogoValidationError
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        with pytest.raises(LogoValidationError, match="פורמט"):
            process_uploaded_logo(buf.getvalue())


# ── overlay_logo_on_qr ─────────────────────────────────────────────────────


class TestOverlayLogoOnQr:
    def test_overlay_produces_valid_png(self):
        """תוצאת ה-overlay היא PNG תקין באותו גודל כמו ה-QR."""
        import segno
        from utils.branding import overlay_logo_on_qr

        qr = segno.make("https://example.com", error="H")
        qr_buf = io.BytesIO()
        qr.save(qr_buf, kind="png", scale=10, border=2)
        qr_bytes = qr_buf.getvalue()

        logo_bytes = _make_png_bytes((200, 200), color=(0, 0, 0, 255))

        out = overlay_logo_on_qr(qr_bytes, logo_bytes)
        assert out[:8] == b"\x89PNG\r\n\x1a\n"

        # שומר את המידות של ה-QR המקורי
        original = Image.open(io.BytesIO(qr_bytes))
        composed = Image.open(io.BytesIO(out))
        assert composed.size == original.size

    def test_logo_appears_in_center(self):
        """הפיקסל במרכז התמונה הסופית הוא הצבע של הלוגו (אדום), לא של ה-QR."""
        import segno
        from utils.branding import overlay_logo_on_qr

        qr = segno.make("https://example.com", error="H")
        qr_buf = io.BytesIO()
        qr.save(qr_buf, kind="png", scale=10, border=2)
        qr_bytes = qr_buf.getvalue()

        # לוגו אדום במלוא הריבוע
        logo_bytes = _make_png_bytes((200, 200), color=(255, 0, 0, 255))

        out = overlay_logo_on_qr(qr_bytes, logo_bytes)
        result = Image.open(io.BytesIO(out)).convert("RGB")
        cx, cy = result.width // 2, result.height // 2
        center_pixel = result.getpixel((cx, cy))
        assert center_pixel == (255, 0, 0)


# ── DB helpers ─────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path):
    """אתחול DB טהור לכל טסט (משתמש ב-DB_PATH ייעודי)."""
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    with patch("ai_chatbot.config.DB_PATH", tmp_path / "test.db"):
        from database import init_db
        init_db()
        yield


class TestDBLogoStorage:
    def test_no_logo_initially(self, fresh_db):
        from database import get_business_logo, has_business_logo
        assert get_business_logo() is None
        assert has_business_logo() is False

    def test_set_and_get(self, fresh_db):
        from database import set_business_logo, get_business_logo, has_business_logo
        blob = _make_png_bytes((100, 100))
        set_business_logo(blob, "image/png")

        assert has_business_logo() is True
        retrieved = get_business_logo()
        assert retrieved is not None
        assert retrieved["blob"] == blob
        assert retrieved["mime_type"] == "image/png"
        assert retrieved["uploaded_at"]  # nonempty

    def test_overwrite(self, fresh_db):
        """העלאה שנייה דורסת את הראשונה."""
        from database import set_business_logo, get_business_logo
        first = _make_png_bytes((100, 100), color=(255, 0, 0, 255))
        second = _make_png_bytes((100, 100), color=(0, 255, 0, 255))

        set_business_logo(first, "image/png")
        set_business_logo(second, "image/png")

        retrieved = get_business_logo()
        assert retrieved["blob"] == second
        assert retrieved["blob"] != first

    def test_delete(self, fresh_db):
        from database import set_business_logo, delete_business_logo, get_business_logo, has_business_logo
        set_business_logo(_make_png_bytes((50, 50)), "image/png")
        assert has_business_logo() is True

        delete_business_logo()
        assert has_business_logo() is False
        assert get_business_logo() is None


# ── Admin routes ───────────────────────────────────────────────────────────


@pytest.fixture
def admin_client_with_branding(tmp_path, monkeypatch):
    """fixture דומה ל-admin_client של test_qr_code, עם DB ריק."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-branding")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "MyTestBot")
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155551234")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")

    import config as _root_config; importlib.reload(_root_config)
    import ai_chatbot.config; importlib.reload(ai_chatbot.config)
    import database; importlib.reload(database); database.init_db()
    import admin.app as _admin_app; importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as c:
        c.post("/login", data={"username": "admin", "password": "testpass123"})
        yield c


class TestBrandingRoutes:
    def test_branding_page_empty_state(self, admin_client_with_branding):
        resp = admin_client_with_branding.get("/branding")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "לוגו" in body
        # empty state — אין thumbnail
        assert "branding/logo?t=" not in body

    def test_upload_valid_png(self, admin_client_with_branding):
        png = _make_png_bytes((300, 300))
        resp = admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert resp.status_code == 302  # redirect חזרה ל-/branding

        # אחרי העלאה, has_logo=True ב-page
        resp = admin_client_with_branding.get("/branding")
        body = resp.data.decode("utf-8")
        assert 'src="/branding/logo' in body  # thumbnail מופיע

    def test_upload_no_file(self, admin_client_with_branding):
        resp = admin_client_with_branding.post(
            "/branding/logo",
            data={},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "לא נבחר".encode("utf-8") in resp.data

    def test_upload_invalid_image(self, admin_client_with_branding):
        resp = admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(b"not an image"), "fake.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # הודעת שגיאה מופיעה
        assert "תמונה".encode("utf-8") in resp.data

    def test_serve_logo_after_upload(self, admin_client_with_branding):
        png = _make_png_bytes((100, 100))
        admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data",
        )
        resp = admin_client_with_branding.get("/branding/logo")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_serve_logo_when_none(self, admin_client_with_branding):
        resp = admin_client_with_branding.get("/branding/logo")
        assert resp.status_code == 404

    def test_delete_logo(self, admin_client_with_branding):
        png = _make_png_bytes((100, 100))
        admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data",
        )
        resp = admin_client_with_branding.post(
            "/branding/logo/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # לאחר מחיקה — 404 על serve
        resp = admin_client_with_branding.get("/branding/logo")
        assert resp.status_code == 404


class TestQRWithLogo:
    """אינטגרציה: ?with_logo=1 משלב את הלוגו ב-QR."""

    def test_download_without_logo_param_unaffected(self, admin_client_with_branding):
        resp = admin_client_with_branding.get("/qr-code/download?channel=telegram")
        assert resp.status_code == 200
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_download_with_logo_when_uploaded(self, admin_client_with_branding):
        png = _make_png_bytes((300, 300), color=(255, 0, 0, 255))
        admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data",
        )

        resp = admin_client_with_branding.get(
            "/qr-code/download?channel=telegram&with_logo=1"
        )
        assert resp.status_code == 200
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

        # פיקסל במרכז = אדום (הלוגו), לא שחור (QR) ולא לבן (רקע)
        result = Image.open(io.BytesIO(resp.data)).convert("RGB")
        cx, cy = result.width // 2, result.height // 2
        assert result.getpixel((cx, cy)) == (255, 0, 0)

    def test_download_with_logo_when_no_logo_uploaded_falls_back(
        self, admin_client_with_branding,
    ):
        """?with_logo=1 בלי לוגו שמור — מחזיר QR רגיל ללא שגיאה."""
        resp = admin_client_with_branding.get(
            "/qr-code/download?channel=telegram&with_logo=1"
        )
        assert resp.status_code == 200
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_qr_page_disables_checkbox_when_no_logo(self, admin_client_with_branding):
        resp = admin_client_with_branding.get("/qr-code")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # ה-checkbox קיים אבל disabled
        assert 'id="with_logo"' in body
        assert "disabled" in body
        # יש לינק לעמוד מיתוג
        assert "/branding" in body

    def test_qr_page_enables_checkbox_when_logo_exists(
        self, admin_client_with_branding,
    ):
        png = _make_png_bytes((100, 100))
        admin_client_with_branding.post(
            "/branding/logo",
            data={"logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data",
        )
        resp = admin_client_with_branding.get("/qr-code")
        body = resp.data.decode("utf-8")
        # ה-checkbox קיים ולא disabled — אפשר לבדוק שהמילה "disabled" לא נמצאת
        # בתוך תג ה-input של with_logo
        idx = body.find('id="with_logo"')
        assert idx != -1
        # עוטפים את התג
        snippet = body[idx:idx + 200]
        assert "disabled" not in snippet
