"""
OpenAI client בלעדי לרכיב הזיכרון — *נפרד* מ-OPENAI_API_KEY ו-
OPENAI_BASE_URL הראשיים של הבוט.

למה client נפרד?
- ה-spec של מערכת הזיכרון (docs/Customer-memory/) קבע במפורש שימוש
  ב-`gpt-4.1-mini` של OpenAI אמיתי. הפרומפט תוכנן לאופי שהמודלים של
  OpenAI עובדים — ליטרליות בהוראות, שמרנות בחילוץ.
- הבוט הראשי בפרויקט עלול להיות מכוון ל-Gemini דרך OPENAI_BASE_URL
  (שהוא תואם-API). שימוש במודל אחר באמת ירד באיכות ה-extraction.
- כדי לא לקשור את המודל של הזיכרון לבחירת הבוט, פותחים client עצמאי
  עם credentials נפרדים: MEMORY_OPENAI_API_KEY ו-(אופציונלי)
  MEMORY_OPENAI_BASE_URL.

ENV נדרש:
  MEMORY_OPENAI_API_KEY   — OpenAI API key אמיתי (חובה).
  MEMORY_OPENAI_BASE_URL  — אופציונלי, default https://api.openai.com/v1.

הקובץ נקרא ע"י:
- memory/extractor.py    — לקריאת ה-fact extractor (gpt-4.1-mini)
- memory/eval/run_eval.py — לקריאת ה-LLM judge (gpt-4.1-mini)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

logger = logging.getLogger(__name__)

_client: Optional[object] = None
_client_lock = threading.Lock()

# default base URL — OpenAI הרשמי. ENV יכול לדרוס (למשל לטסטים מקומיים
# מול mock proxy), אבל לא יכול ליפול ל-OPENAI_BASE_URL של הבוט.
_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class MemoryOpenAIConfigError(RuntimeError):
    """נזרק כש-MEMORY_OPENAI_API_KEY חסר/ריק. הודעה ברורה למפעיל."""


def get_memory_openai_client():
    """מחזיר client lazily — נוצר ב-call ראשון, שמור ב-singleton.

    Raises:
        MemoryOpenAIConfigError: אם MEMORY_OPENAI_API_KEY לא מוגדר.
        RuntimeError: אם חבילת openai לא מותקנת.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        # double-check — thread אחר עלול היה ליצור בינתיים.
        if _client is not None:
            return _client

        if OpenAI is None:
            raise RuntimeError(
                "OpenAI client is unavailable (openai package not installed)."
            )

        api_key = os.getenv("MEMORY_OPENAI_API_KEY", "").strip()
        if not api_key:
            raise MemoryOpenAIConfigError(
                "MEMORY_OPENAI_API_KEY is required for memory system. "
                "Set this env var with a real OpenAI API key (separate "
                "from the main bot's OPENAI_API_KEY which may point to "
                "a different provider via OPENAI_BASE_URL). "
                "See memory/README.md for details."
            )

        base_url = os.getenv("MEMORY_OPENAI_BASE_URL", "").strip() or _DEFAULT_BASE_URL

        # לוג מאופק — לא חושף את ה-key (OpenAI client לא לוגג אותו), רק
        # את ה-base URL לטובת דיבוג שגיאות תשתית.
        logger.info("memory: creating dedicated OpenAI client (base_url=%s)", base_url)
        _client = OpenAI(api_key=api_key, base_url=base_url)
        return _client


def reset_memory_openai_client() -> None:
    """איפוס ה-singleton — לטסטים בלבד. לא להפעיל ב-runtime."""
    global _client
    with _client_lock:
        _client = None
