"""
טסטים לפיצ'ר חסימת משתמשים — database + block_guard decorator.
"""

import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


@pytest.fixture
def db(tmp_path):
    """מאתחל DB בקובץ זמני ומחזיר את מודול database מוכן לשימוש."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


class TestBlockedUsersDB:
    """טסטים לפונקציות DB של חסימת משתמשים."""

    def test_block_user(self, db):
        db.block_user("123", "אבי", "ספאם")
        assert db.is_user_blocked("123") is True

    def test_unblock_user(self, db):
        db.block_user("123", "אבי", "ספאם")
        db.unblock_user("123")
        assert db.is_user_blocked("123") is False

    def test_is_user_blocked_returns_false_for_unknown(self, db):
        assert db.is_user_blocked("999") is False

    def test_get_blocked_users_empty(self, db):
        assert db.get_blocked_users() == []

    def test_get_blocked_users_returns_all(self, db):
        db.block_user("111", "א", "סיבה 1")
        db.block_user("222", "ב", "סיבה 2")
        blocked = db.get_blocked_users()
        assert len(blocked) == 2
        ids = {u["user_id"] for u in blocked}
        assert ids == {"111", "222"}

    def test_block_user_updates_on_conflict(self, db):
        """חסימה חוזרת מעדכנת סיבה ושם."""
        db.block_user("123", "שם ישן", "סיבה ישנה")
        db.block_user("123", "שם חדש", "סיבה חדשה")
        blocked = db.get_blocked_users()
        assert len(blocked) == 1
        assert blocked[0]["username"] == "שם חדש"
        assert blocked[0]["reason"] == "סיבה חדשה"

    def test_unblock_nonexistent_user_no_error(self, db):
        """שחרור משתמש שלא חסום לא זורק שגיאה."""
        db.unblock_user("nonexistent")

    def test_blocked_users_table_created(self, db):
        """וידוא שטבלת blocked_users נוצרת ב-init_db."""
        from database import get_connection
        with get_connection() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='blocked_users'"
            ).fetchall()
            assert len(tables) == 1


class TestBlockGuardDecorator:
    """טסטים ל-block_guard ו-block_guard_booking."""

    @pytest.fixture(autouse=True)
    def setup_db(self, db):
        self.db = db

    @pytest.mark.asyncio
    async def test_block_guard_allows_unblocked_user(self, db):
        from rate_limiter import block_guard

        inner = AsyncMock()
        decorated = block_guard(inner)

        update = MagicMock()
        update.effective_user.id = 123
        update.message = MagicMock()
        context = MagicMock()

        await decorated(update, context)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_block_guard_blocks_user(self, db):
        from rate_limiter import block_guard

        db.block_user("456", "יוזר חסום", "ספאם")

        inner = AsyncMock()
        decorated = block_guard(inner)

        update = MagicMock()
        update.effective_user.id = 456
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await decorated(update, context)
        inner.assert_not_awaited()
        update.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_block_guard_booking_returns_end(self, db):
        from rate_limiter import block_guard_booking

        db.block_user("789", "יוזר", "")

        inner = AsyncMock()
        decorated = block_guard_booking(inner)

        update = MagicMock()
        update.effective_user.id = 789
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        result = await decorated(update, context)
        inner.assert_not_awaited()
        # ConversationHandler.END = -1 (מוגדר ב-conftest)
        assert result == -1

    @pytest.mark.asyncio
    async def test_block_guard_no_user_passes_through(self, db):
        """אם אין effective_user — לא בודק חסימה, מעביר הלאה."""
        from rate_limiter import block_guard

        inner = AsyncMock()
        decorated = block_guard(inner)

        update = MagicMock()
        update.effective_user = None
        context = MagicMock()

        await decorated(update, context)
        inner.assert_awaited_once()
