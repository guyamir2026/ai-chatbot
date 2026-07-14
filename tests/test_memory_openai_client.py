"""
טסטים ל-memory/openai_client.py — client בלעדי לרכיב הזיכרון.

מאמת:
- MEMORY_OPENAI_API_KEY חסר/ריק → MemoryOpenAIConfigError ברור.
- API key תקין → client נוצר עם הפרמטרים הנכונים.
- ה-client נפרד לחלוטין מ-OPENAI_API_KEY ו-OPENAI_BASE_URL הראשיים
  (אסור שגלישה מ-ENV של הבוט תשפיע).
- singleton — קריאה שנייה מחזירה את אותו instance.
- reset לטסטים.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memory import openai_client


@pytest.fixture(autouse=True)
def _reset_singleton():
    """מאפסים את ה-singleton בין טסטים כדי שכל אחד יקבל מצב נקי."""
    openai_client.reset_memory_openai_client()
    yield
    openai_client.reset_memory_openai_client()


class TestApiKeyRequired:
    def test_missing_key_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("MEMORY_OPENAI_API_KEY", raising=False)
        with pytest.raises(openai_client.MemoryOpenAIConfigError) as exc_info:
            openai_client.get_memory_openai_client()
        msg = str(exc_info.value)
        # ההודעה ברורה ומסבירה למה נדרש המפתח
        assert "MEMORY_OPENAI_API_KEY" in msg
        assert "memory system" in msg

    def test_empty_key_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "")
        with pytest.raises(openai_client.MemoryOpenAIConfigError):
            openai_client.get_memory_openai_client()

    def test_whitespace_only_key_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "   ")
        with pytest.raises(openai_client.MemoryOpenAIConfigError):
            openai_client.get_memory_openai_client()


class TestClientCreation:
    def test_creates_client_with_key(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "sk-test123")
        monkeypatch.delenv("MEMORY_OPENAI_BASE_URL", raising=False)

        fake_openai_cls = MagicMock()
        fake_instance = MagicMock()
        fake_openai_cls.return_value = fake_instance

        with patch.object(openai_client, "OpenAI", fake_openai_cls):
            client = openai_client.get_memory_openai_client()

        assert client is fake_instance
        # ה-client נוצר עם ה-key הנכון ו-base_url ברירת מחדל (api.openai.com)
        fake_openai_cls.assert_called_once_with(
            api_key="sk-test123",
            base_url="https://api.openai.com/v1",
        )

    def test_uses_custom_base_url_when_set(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("MEMORY_OPENAI_BASE_URL", "https://my-proxy.example.com/v1")

        fake_openai_cls = MagicMock()
        with patch.object(openai_client, "OpenAI", fake_openai_cls):
            openai_client.get_memory_openai_client()

        call_kwargs = fake_openai_cls.call_args.kwargs
        assert call_kwargs["base_url"] == "https://my-proxy.example.com/v1"

    def test_ignores_main_openai_env_vars(self, monkeypatch):
        """OPENAI_API_KEY ו-OPENAI_BASE_URL של הבוט הראשי לא משפיעים
        על ה-client של memory. זאת המהות של ההפרדה."""
        # מוגדר ENV של הבוט (כאילו הוא Gemini)
        monkeypatch.setenv("OPENAI_API_KEY", "main-bot-key-irrelevant")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
        # אבל לא ENV של memory
        monkeypatch.delenv("MEMORY_OPENAI_API_KEY", raising=False)

        # ה-client של memory חייב לזרוק שגיאה, *גם אם* OPENAI_API_KEY של
        # הבוט מוגדר. אסור שייפול עליו ב-fallback.
        with pytest.raises(openai_client.MemoryOpenAIConfigError):
            openai_client.get_memory_openai_client()

    def test_does_not_inherit_base_url_from_main_bot(self, monkeypatch):
        """אם OPENAI_BASE_URL מכוון ל-Gemini אבל MEMORY_OPENAI_BASE_URL
        לא מוגדר — נופלים ל-OpenAI הרשמי, *לא* ל-Gemini."""
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "sk-real-openai-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
        monkeypatch.delenv("MEMORY_OPENAI_BASE_URL", raising=False)

        fake_openai_cls = MagicMock()
        with patch.object(openai_client, "OpenAI", fake_openai_cls):
            openai_client.get_memory_openai_client()

        assert fake_openai_cls.call_args.kwargs["base_url"] == "https://api.openai.com/v1"


class TestSingleton:
    def test_returns_same_instance_on_repeated_calls(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "sk-test")

        fake_openai_cls = MagicMock()
        fake_openai_cls.return_value = MagicMock()
        with patch.object(openai_client, "OpenAI", fake_openai_cls):
            c1 = openai_client.get_memory_openai_client()
            c2 = openai_client.get_memory_openai_client()

        assert c1 is c2
        # OpenAI constructor נקרא פעם אחת בלבד (singleton)
        assert fake_openai_cls.call_count == 1

    def test_reset_creates_new_client(self, monkeypatch):
        monkeypatch.setenv("MEMORY_OPENAI_API_KEY", "sk-test")
        fake_openai_cls = MagicMock()
        fake_openai_cls.side_effect = [MagicMock(name="first"), MagicMock(name="second")]
        with patch.object(openai_client, "OpenAI", fake_openai_cls):
            c1 = openai_client.get_memory_openai_client()
            openai_client.reset_memory_openai_client()
            c2 = openai_client.get_memory_openai_client()

        assert c1 is not c2
        assert fake_openai_cls.call_count == 2
