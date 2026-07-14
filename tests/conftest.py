"""
Shared fixtures — DB in-memory, מוקים לתלויות חיצוניות.
"""

import os
import sqlite3
import sys
import tempfile
import types
from unittest.mock import patch, MagicMock

import pytest

# ── Mock telegram (לא מותקן בסביבת הטסטים) ────────────────────────────────
# חייב להתרחש לפני כל ייבוא של מודולים שתלויים ב-telegram.
if "telegram" not in sys.modules:
    _telegram = types.ModuleType("telegram")
    _telegram.Bot = MagicMock()
    _telegram.Update = MagicMock()
    _telegram.ReplyKeyboardMarkup = MagicMock()
    _telegram.KeyboardButton = MagicMock()
    _telegram.InlineKeyboardButton = MagicMock()
    _telegram.InlineKeyboardMarkup = MagicMock()
    sys.modules["telegram"] = _telegram

    _error = types.ModuleType("telegram.error")

    class _Forbidden(Exception):
        pass

    class _RetryAfter(Exception):
        def __init__(self, retry_after=0):
            self.retry_after = retry_after
            super().__init__(f"RetryAfter: {retry_after}")

    class _TimedOut(Exception):
        pass

    class _BadRequest(Exception):
        pass

    class _NetworkError(Exception):
        pass

    _error.Forbidden = _Forbidden
    _error.RetryAfter = _RetryAfter
    _error.TimedOut = _TimedOut
    _error.BadRequest = _BadRequest
    _error.NetworkError = _NetworkError
    sys.modules["telegram.error"] = _error
    _telegram.error = _error

    _ext = types.ModuleType("telegram.ext")
    _ext.ContextTypes = MagicMock()
    _ext.ConversationHandler = MagicMock()
    _ext.ConversationHandler.END = -1
    _ext.ApplicationBuilder = MagicMock()
    _ext.Application = MagicMock()
    _ext.CommandHandler = MagicMock()
    _ext.MessageHandler = MagicMock()
    _ext.CallbackQueryHandler = MagicMock()
    _ext.filters = MagicMock()
    sys.modules["telegram.ext"] = _ext
    _telegram.ext = _ext

# ── Mock requests (לא צריך HTTP אמיתי בטסטים) ─────────────────────────────
if "requests" not in sys.modules:
    _requests = types.ModuleType("requests")
    _requests.post = MagicMock()
    _requests.get = MagicMock()
    _requests.delete = MagicMock()

    # סטאב ל-RequestException — נדרש ע"י מודולים שתופסים שגיאות רשת
    # (למשל whatsapp_templates_sync ו-whatsapp_templates_submit).
    class _RequestException(Exception):
        pass

    _requests.RequestException = _RequestException
    _requests.exceptions = types.ModuleType("requests.exceptions")
    _requests.exceptions.RequestException = _RequestException
    sys.modules["requests"] = _requests
    sys.modules["requests.exceptions"] = _requests.exceptions


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """מגדיר משתני סביבה בטוחים כך שייבוא config לא ייצור קבצים אמיתיים."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("FAISS_INDEX_PATH", str(tmp_path / "faiss"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    # מפתח Fernet קבוע לטסטים — utils/crypto.py קורא ממשתנה הסביבה. בלי
    # זה, טסטים שכותבים שדות מוצפנים (Google Calendar / Meta credentials)
    # זורקים EncryptionConfigError בעת encrypt_field.
    monkeypatch.setenv(
        "SECRETS_ENCRYPTION_KEY",
        "qV5Bw4Yw3VgX9V0E9-zZ_T1xX5sQqM4hCgL9pZsK5oI=",
    )


@pytest.fixture
def db_conn(tmp_path):
    """מחזיר חיבור SQLite אמיתי לקובץ זמני, עם הסכימה המלאה.

    חשוב — patch גם על `database.DB_PATH` ולא רק על
    `ai_chatbot.config.DB_PATH`: ה-database.py עושה
    `from ai_chatbot.config import DB_PATH` בייבוא, ולכן יש
    לו reference משלו לערך הישן. בלי patch על שני המקומות,
    `get_connection` ימשיך לכתוב ל-DB הגלובלי בין טסטים
    ושורות ידלפו ביניהם.
    """
    db_path = tmp_path / "test.db"
    os.environ["DB_PATH"] = str(db_path)

    with patch("ai_chatbot.config.DB_PATH", db_path):
        from database import init_db, get_connection
        import database as _db_mod
        with patch.object(_db_mod, "DB_PATH", db_path):
            init_db()
            with get_connection() as conn:
                yield conn
