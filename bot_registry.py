"""
Bot Registry — ניהול אפליקציות טלגרם פר-tenant בתהליך אחד (שלב 2).

ראה docs/multi_tenant_migration_spec.md סעיף 6.1. העקרונות:

- כל ה-Applications חיות על **אותו event loop** (ה-bot loop שנוצר
  ב-main.py במצב webhook). בנייה ואתחול נעשים עצלה — בהודעה הראשונה
  של tenant — כך ש-tenant שנרשם בזמן ריצה עובד בלי restart.
- הטוקן של כל tenant מגיע מהסודות המוצפנים ב-control plane; ה-tenant
  של ברירת המחדל ממשיך על TELEGRAM_BOT_TOKEN מה-env (הבוט ה-legacy,
  שמנוהל ע"י main.py ולא כאן).
- אין JobQueue לבוטים של tenants — העבודות המתוזמנות רצות ב-schedulers
  הפלטפורמתיים (broadcast / memory) שמאתרים על פני כל ה-tenants.

הפונקציות האסינכרוניות כאן רצות **על ה-bot loop בלבד** (נשלחות אליו
דרך run_coroutine_threadsafe מה-route של Flask).
"""

import asyncio
import logging
from typing import Optional

from tenancy import DEFAULT_TENANT, get_current_tenant, tenant_context

logger = logging.getLogger(__name__)

# tenant → Application מאותחל. מנוהל אך ורק מתוך ה-bot loop —
# ולכן אין צורך במנעול threads, רק במנעולי asyncio לאתחול כפול.
_apps: dict = {}
_init_locks: dict = {}


def resolve_telegram_token(tenant_id: Optional[str] = None) -> str:
    """טוקן הבוט של ה-tenant: ‏default → env (דינמי); אחר → tenant_secrets.

    מחזיר '' כשאין טוקן — הקוראים מטפלים (אין שליחה / אין אפליקציה).
    לעולם לא נופלים לטוקן ה-env עבור tenant אחר — זו זהות של עסק אחר.
    """
    tenant = tenant_id or get_current_tenant()
    if tenant == DEFAULT_TENANT:
        import ai_chatbot.config as _cfg

        return getattr(_cfg, "TELEGRAM_BOT_TOKEN", "") or ""
    try:
        from control_plane import get_tenant_secret

        return get_tenant_secret(tenant, "telegram_bot_token") or ""
    except Exception:
        logger.error(
            "resolve_telegram_token failed (tenant=%s)", tenant, exc_info=True,
        )
        return ""


def resolve_webhook_secret(tenant_id: str) -> str:
    """ה-secret לאימות ה-header של טלגרם עבור ה-tenant ('' אם לא הוגדר)."""
    try:
        from control_plane import get_tenant_secret

        return get_tenant_secret(tenant_id, "telegram_webhook_secret") or ""
    except Exception:
        logger.error(
            "resolve_webhook_secret failed (tenant=%s)", tenant_id, exc_info=True,
        )
        return ""


async def ensure_tenant_application(tenant_id: str):
    """ה-Application של ה-tenant — בנייה+אתחול עצלים בקריאה הראשונה.

    רץ על ה-bot loop. מחזיר None אם ל-tenant אין טוקן רשום (ההודעה
    תיזרק עם לוג — זו קונפיגורציה חסרה, לא באג).
    """
    app = _apps.get(tenant_id)
    if app is not None:
        return app

    lock = _init_locks.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _init_locks[tenant_id] = lock

    async with lock:
        app = _apps.get(tenant_id)
        if app is not None:
            return app

        token = resolve_telegram_token(tenant_id)
        if not token:
            logger.warning(
                "tenant %s: no telegram_bot_token registered — dropping update",
                tenant_id,
            )
            return None

        from bot.telegram_bot import create_tenant_bot_application

        app = create_tenant_bot_application(token)
        await app.initialize()

        from ai_chatbot import bot_state

        bot_state.register_tenant_bot(tenant_id, app.bot)
        _apps[tenant_id] = app
        logger.info("tenant bot initialized: %s", tenant_id)
        return app


async def dispatch_tenant_update(tenant_id: str, update_data: dict) -> None:
    """עיבוד update של tenant — תחת ה-context שלו, על האפליקציה שלו."""
    with tenant_context(tenant_id):
        app = await ensure_tenant_application(tenant_id)
        if app is None:
            return
        from telegram import Update

        update = Update.de_json(update_data, app.bot)
        await app.process_update(update)


async def shutdown_tenant_applications() -> None:
    """כיבוי נקי של כל אפליקציות ה-tenants (נקרא מ-atexit של main)."""
    from ai_chatbot import bot_state

    for tenant_id, app in list(_apps.items()):
        try:
            await app.shutdown()
        except Exception:
            # כיבוי של אחד לא עוצר את השאר (כלל cleanup ב-finally)
            logger.error(
                "tenant bot shutdown failed (tenant=%s)", tenant_id, exc_info=True,
            )
        bot_state.unregister_tenant_bot(tenant_id)
    _apps.clear()
    _init_locks.clear()


def reset_registry() -> None:
    """איפוס ל-tests בלבד — לא מכבה אפליקציות (הן mocks בטסטים)."""
    _apps.clear()
    _init_locks.clear()


def reset_tenant(tenant_id: str) -> None:
    """הסרת האפליקציה המטומנת של tenant — תיבנה מחדש בהודעה הבאה.

    נקרא כשהטוקן של ה-tenant מתחלף (עדכון מהפאנל) כדי שהאפליקציה
    תיבנה מחדש עם הטוקן החדש. לא מבצע shutdown אסינכרוני (רץ ב-thread
    של Flask, בלי הלולאה) — האובייקט הישן פשוט מוחלף; ה-webhook נרשם
    מחדש לאותו URL כך שההודעות ממשיכות לזרום.
    """
    _apps.pop(tenant_id, None)
    _init_locks.pop(tenant_id, None)
    from ai_chatbot import bot_state

    bot_state.unregister_tenant_bot(tenant_id)


async def sync_telegram_webhook(tenant_id: str, webhook_url: str, secret: str) -> str:
    """רישום ה-webhook של tenant מול טלגרם (משמש את ה-CLI ואת הפאנל).

    מחזיר את שם המשתמש של הבוט (getMe) — הקורא שומר אותו לקישורי QR
    (t.me/<username>); ריק אם לא הוחזר. Bot עצמאי (מחוץ ל-Application) —
    חובה initialize לפני ו-shutdown אחרי (python-telegram-bot v20+,
    ראה כלל asyncio ב-CLAUDE.md).
    """
    token = resolve_telegram_token(tenant_id)
    if not token:
        raise RuntimeError(
            f"ל-tenant '{tenant_id}' אין telegram_bot_token — "
            "יש להגדיר דרך: python -m platform_cli set-secret"
        )
    from telegram import Bot

    bot = Bot(token=token)
    await bot.initialize()
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=secret or None,
            drop_pending_updates=True,
        )
        # שם המשתמש נלכד באותו חיבור — כשל בו לא מפיל את רישום ה-webhook
        try:
            me = await bot.get_me()
            return (me.username or "") if me else ""
        except Exception:
            logger.error(
                "getMe failed after set_webhook (tenant=%s)", tenant_id, exc_info=True
            )
            return ""
    finally:
        try:
            await bot.shutdown()
        except Exception:
            # כשל ב-cleanup לא דורס את תוצאת הפעולה העיקרית
            logger.error("bot.shutdown failed after set_webhook", exc_info=True)


async def remove_telegram_webhook(tenant_id: str) -> None:
    """ביטול ה-webhook של tenant מול טלגרם — לפני מחיקת הטוקן במעבר ערוץ.

    אחרי שהטוקן נמחק אין דרך לבטל (הטוקן הוא ההרשאה), לכן הקורא מפעיל
    את זה קודם. best-effort — כשל נבלע אצל הקורא עם לוג.
    """
    token = resolve_telegram_token(tenant_id)
    if not token:
        return
    from telegram import Bot

    bot = Bot(token=token)
    await bot.initialize()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    finally:
        try:
            await bot.shutdown()
        except Exception:
            logger.error("bot.shutdown failed after delete_webhook", exc_info=True)
