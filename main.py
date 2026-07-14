"""
Main Entry Point — Starts both the Telegram bot and the Admin panel.

Usage:
    python -m ai_chatbot.main              # Start both bot and admin
    python -m ai_chatbot.main --bot        # Start only the Telegram bot
    python -m ai_chatbot.main --admin      # Start only the admin panel
    python -m ai_chatbot.main --seed       # Seed database and build index
"""

import argparse
import logging
import os
import threading
import sys

import sentry_sdk

from ai_chatbot import database as db
from ai_chatbot.config import TELEGRAM_BOT_TOKEN, ADMIN_HOST, ADMIN_PORT, WEBHOOK_URL, WEBHOOK_SECRET, validate_config

# ─── Sentry — ניטור שגיאות בפרודקשן ──────────────────────────────────────────
_sentry_dsn = os.getenv("SENTRY_DSN", "")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.2,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


def run_seed():
    """Seed the database with demo data and build the RAG index."""
    from ai_chatbot.seed_data import seed_and_index
    seed_and_index()


def run_admin_panel(flask_app=None):
    """Start the Flask admin panel in a thread.

    אם flask_app מסופק — משתמש בו (למשל כש-webhook מוגדר).
    """
    if flask_app is None:
        from ai_chatbot.admin.app import create_admin_app
        flask_app = create_admin_app()
    logger.info("Starting Admin Panel at http://%s:%s", ADMIN_HOST, ADMIN_PORT)
    flask_app.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=False)


def run_telegram_bot():
    """Start the Telegram bot (polling or webhook standalone)."""
    from ai_chatbot.bot.telegram_bot import run_bot

    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set! "
            "Please set it in your .env file. "
            "Starting admin panel only..."
        )
        return

    # Clean up live chat sessions from a previous bot run so users aren't
    # permanently silenced.  Done here (not in init_db) so an admin-only
    # restart doesn't kill sessions that are still actively managed.
    from ai_chatbot.live_chat_service import LiveChatService
    LiveChatService.cleanup_stale()

    logger.info("Starting Telegram Bot...")
    run_bot()


def main():
    parser = argparse.ArgumentParser(description="AI Business Chatbot")
    parser.add_argument("--bot", action="store_true", help="Start only the Telegram bot")
    parser.add_argument("--admin", action="store_true", help="Start only the admin panel")
    parser.add_argument("--seed", action="store_true", help="Seed database and build RAG index")
    args = parser.parse_args()
    
    # Always initialize the database
    logger.info("Initializing database...")
    db.init_db()

    # תמיד לזרוע שעות פעילות וחגים — הפונקציה אידמפוטנטית ולא דורסת נתונים קיימים
    try:
        from seed_data import _seed_business_hours
        _seed_business_hours()
    except Exception:
        logger.exception("Failed to seed business hours / holidays.")

    if args.seed:
        run_seed()
        return

    # ולידציה של משתני סביבה קריטיים בהתאם למצב ההרצה
    require_bot = args.bot or (not args.bot and not args.admin)
    require_admin = args.admin or (not args.bot and not args.admin)
    config_errors = validate_config(require_bot=require_bot, require_admin=require_admin)
    for err in config_errors:
        logger.warning("⚠ תצורה: %s", err)

    # Phase 3 — startup mismatch check: אם הערוץ הפעיל לא תואם לחבילה,
    # שולח התראת טלגרם למפתח (DEVELOPER_TELEGRAM_CHAT_ID). לא חוסם הפעלה
    # — רק התראה. ראה developer_alerts.py + docs/plans_feature_flags_spec.md 2.4.
    try:
        from ai_chatbot.developer_alerts import check_and_alert_channel_mismatch
        check_and_alert_channel_mismatch()
    except Exception:
        logger.error(
            "Failed to run developer channel-mismatch check at startup",
            exc_info=True,
        )

    # Auto-seed on first run: if the knowledge base is empty, populate it with
    # demo data and build the FAISS index so the bot can answer questions
    # immediately without requiring a manual --seed step.
    if db.count_kb_entries(active_only=False) == 0:
        logger.info("Knowledge base is empty — auto-seeding with demo data...")
        try:
            run_seed()
        except Exception:
            logger.exception("Auto-seed failed. Continuing without demo data.")

    if args.bot:
        run_telegram_bot()
        return

    if args.admin:
        run_admin_panel()
        return
    
    # Default: run both
    logger.info("Starting AI Business Chatbot (Bot + Admin Panel)...")

    if WEBHOOK_URL and TELEGRAM_BOT_TOKEN:
        # ─── מצב Webhook — הבוט מקבל עדכונים דרך ה-Flask server ─────────
        # asyncio loop ב-thread נפרד מריץ את ה-bot Application,
        # Flask מקבל POST מטלגרם ומעביר ל-loop.
        import asyncio
        from ai_chatbot.admin.app import create_admin_app
        from ai_chatbot.bot.telegram_bot import (
            create_bot_application, setup_webhook_via_flask, shutdown_webhook_app,
        )
        from ai_chatbot.live_chat_service import LiveChatService
        LiveChatService.cleanup_stale()

        # Broadcast scheduler — thread ברקע שמבצע קמפיינים מתוזמנים.
        # פועל מסביבת production; אפשר לכבות עם BROADCAST_SCHEDULER_ENABLED=0.
        try:
            from ai_chatbot.messaging.broadcast_scheduler import start_scheduler
            start_scheduler()
        except Exception:
            logger.error("Failed to start broadcast scheduler", exc_info=True)

        # Memory extraction scheduler (שלב 6) — thread ברקע שמחלץ facts
        # משיחות שהסתיימו. כיבוי: MEMORY_BACKGROUND_ENABLED=false.
        try:
            from memory.background import start_scheduler as start_memory_scheduler
            start_memory_scheduler()
        except Exception:
            logger.error("Failed to start memory background scheduler", exc_info=True)

        bot_app = create_bot_application()
        flask_app = create_admin_app()
        # שיתוף ה-Application עם ה-Flask endpoint
        flask_app.config["_telegram_app"] = bot_app

        bot_loop = asyncio.new_event_loop()
        flask_app.config["_bot_loop"] = bot_loop

        def _run_bot_loop():
            """מריץ asyncio loop ל-bot application ב-thread נפרד."""
            asyncio.set_event_loop(bot_loop)
            bot_loop.run_until_complete(
                setup_webhook_via_flask(bot_app, WEBHOOK_URL, WEBHOOK_SECRET)
            )
            bot_loop.run_forever()

        bot_thread = threading.Thread(target=_run_bot_loop, daemon=True, name="bot-loop")
        bot_thread.start()
        logger.info("Bot webhook loop started in background thread")

        # Flask ב-main thread — מאזין ל-admin + webhook endpoint
        # atexit — ממתין לסיום ה-cleanup בפועל כדי שה-webhook יימחק מטלגרם
        import atexit

        def _shutdown_bot():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    shutdown_webhook_app(bot_app), bot_loop
                )
                future.result(timeout=10)  # ממתין עד 10 שניות לסיום
            except Exception as e:
                logger.error("Bot shutdown during atexit failed: %s", e)
            finally:
                bot_loop.call_soon_threadsafe(bot_loop.stop)

        atexit.register(_shutdown_bot)

        logger.info(
            "Starting Flask (admin + webhook) at http://%s:%s",
            ADMIN_HOST, ADMIN_PORT,
        )
        run_admin_panel(flask_app)

    elif TELEGRAM_BOT_TOKEN:
        # ─── מצב Polling — ללא webhook ──────────────────────────────────
        try:
            from ai_chatbot.messaging.broadcast_scheduler import start_scheduler
            start_scheduler()
        except Exception:
            logger.error("Failed to start broadcast scheduler", exc_info=True)
        # Memory extraction scheduler (שלב 6) — שני אז גם ב-polling mode.
        try:
            from memory.background import start_scheduler as start_memory_scheduler
            start_memory_scheduler()
        except Exception:
            logger.error("Failed to start memory background scheduler", exc_info=True)
        # Start admin panel in a background thread
        admin_thread = threading.Thread(target=run_admin_panel, daemon=True)
        admin_thread.start()
        logger.info("Admin panel started at http://%s:%s", ADMIN_HOST, ADMIN_PORT)
        run_telegram_bot()
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set. Running admin panel only. "
            "Set TELEGRAM_BOT_TOKEN in .env to enable the Telegram bot."
        )
        # Keep the main thread alive for the admin panel
        run_admin_panel()


if __name__ == "__main__":
    main()
