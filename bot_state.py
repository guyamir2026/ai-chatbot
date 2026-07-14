"""
Bot State — רפרנסים לאובייקטי ה-Bot ול-event loop המשותף.

multi-tenant שלב 2: במקום Bot יחיד — registry ‏tenant → Bot. הבוט
ה-legacy (main.py / polling) נרשם תחת ה-tenant של ברירת המחדל דרך
set_bot; בוטים של tenants נרשמים ע"י bot_registry עם האתחול העצל.

ה-event loop אחד לכולם — הלולאה שבה חיות כל האפליקציות (bot loop).
פאנל האדמין (שרץ ב-thread נפרד) משתמש ב-get_bot()+get_loop() לשליחת
broadcast דרך run_coroutine_threadsafe.
"""

import asyncio
import threading
from typing import Optional

from telegram import Bot

from tenancy import DEFAULT_TENANT, get_current_tenant

_bots: dict[str, Bot] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None
_guard = threading.Lock()


def set_bot(bot: Bot, loop: asyncio.AbstractEventLoop) -> None:
    """רישום הבוט ה-legacy (ברירת המחדל) + ה-event loop המשותף.

    נקרא מ-post_init של הבוט הראשי — שומר על החתימה ההיסטורית.
    """
    global _loop
    with _guard:
        _bots[DEFAULT_TENANT] = bot
        _loop = loop


def register_tenant_bot(tenant_id: str, bot: Bot) -> None:
    """רישום Bot של tenant (נקרא מ-bot_registry על ה-bot loop)."""
    with _guard:
        _bots[tenant_id] = bot


def unregister_tenant_bot(tenant_id: str) -> None:
    with _guard:
        _bots.pop(tenant_id, None)


def get_bot() -> Optional[Bot]:
    """ה-Bot של ה-tenant הנוכחי (או None אם לא רשום / לא אותחל עדיין).

    אין fallback לבוט של ברירת המחדל עבור tenant אחר — שליחה בזהות
    בוט של עסק אחר אסורה.
    """
    with _guard:
        return _bots.get(get_current_tenant())


def get_loop() -> Optional[asyncio.AbstractEventLoop]:
    """ה-event loop המשותף של הבוטים (או None אם אף בוט לא פעיל)."""
    return _loop


def reset_state() -> None:
    """איפוס ל-tests בלבד."""
    global _loop
    with _guard:
        _bots.clear()
        _loop = None
