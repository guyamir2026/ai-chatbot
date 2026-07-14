"""
Telegram Bot Runner — sets up and starts the Telegram bot with all handlers.
"""

import asyncio
import logging
import re
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from ai_chatbot.config import TELEGRAM_BOT_TOKEN
from ai_chatbot.bot_state import set_bot
from ai_chatbot.live_chat_service import LiveChatService
from ai_chatbot.appointment_notifications import send_appointment_reminders, send_second_reminders
from ai_chatbot.bot.handlers import (
    start_command,
    help_command,
    stop_command,
    subscribe_command,
    message_handler,
    booking_start,
    booking_service,
    booking_date,
    booking_time,
    booking_confirm,
    booking_cancel,
    booking_button_interrupt,
    calendar_navigate_callback,
    calendar_ignore_callback,
    calendar_select_callback,
    cancel_appointment_callback,
    reschedule_appointment_callback,
    follow_up_callback,
    referral_command,
    error_handler,
    myinfo_command,
    forget_command,
    forget_callback,
    consent_callback,
    talk_to_agent_handler,
    BOOKING_SERVICE,
    BOOKING_DATE,
    BOOKING_TIME,
    BOOKING_CONFIRM,
    CB_FORGET_CONFIRM,
    CB_FORGET_CANCEL,
    CB_CONSENT_ACCEPT,
    CB_CONSENT_DECLINE,
    ALL_BUTTON_TEXTS,
    BUTTON_BOOKING,
    FOLLOW_UP_CB_PREFIX,
)
from ai_chatbot.bot.calendar_keyboard import (
    CB_CALENDAR_SELECT,
    CB_CALENDAR_PREV,
    CB_CALENDAR_NEXT,
    CB_CALENDAR_IGNORE,
)
# save_contact_handler ו-BUTTON_SAVE_CONTACT מטופלים דרך message_handler
# ו-booking_button_interrupt — אין צורך ברישום ישיר.

logger = logging.getLogger(__name__)


def create_bot_application():
    """
    Create and configure the Telegram bot application with all handlers.
    
    Returns:
        Configured Application instance ready to run.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is not set. "
            "Please set it in your .env file or environment variables."
        )
    
    def _with_default_tenant(job_func):
        """עוטף job callback ב-tenant context של ברירת המחדל.

        הבוט היחיד של התהליך משרת את ה-tenant ה-legacy. כשהבוטים יהפכו
        פר-tenant (ראוטינג webhooks, שלב 2 המשך) — ה-jobs האלה יעברו
        ל-scheduler הפלטפורמתי שמאתר על פני כל ה-tenants.
        """
        from functools import wraps

        from tenancy import DEFAULT_TENANT, tenant_context

        @wraps(job_func)
        async def wrapped(context):
            with tenant_context(DEFAULT_TENANT):
                return await job_func(context)

        return wrapped

    # שמירת רפרנס לבוט ול-event loop — משמש את broadcast_service לשליחת הודעות
    async def _post_init(application: Application) -> None:
        loop = asyncio.get_running_loop()
        set_bot(application.bot, loop)

        # סגירת sessions ישנים באופן תקופתי — כל 30 דקות
        async def _cleanup_expired_job(context) -> None:
            try:
                closed = LiveChatService.cleanup_expired()
                if closed:
                    logger.info("Periodic cleanup: closed %d expired live chat session(s)", closed)
            except Exception as e:
                logger.error("Periodic live chat cleanup failed: %s", e)

        application.job_queue.run_repeating(
            _with_default_tenant(_cleanup_expired_job),
            interval=1800,  # 30 דקות
            first=60,       # ריצה ראשונה אחרי דקה (לא מיד ב-startup)
            name="live_chat_cleanup",
        )

        # תזכורות תורים — בדיקה כל 30 דקות אם הגיע זמן לשלוח
        async def _appointment_reminders_job(context) -> None:
            try:
                result = send_appointment_reminders()
                if result["sent"]:
                    logger.info("Appointment reminders job: sent %d", result["sent"])
            except Exception as e:
                logger.error("Appointment reminders job failed: %s", e)

        application.job_queue.run_repeating(
            _with_default_tenant(_appointment_reminders_job),
            interval=1800,  # 30 דקות
            first=120,      # ריצה ראשונה אחרי 2 דקות
            name="appointment_reminders",
        )

        # תזכורת שנייה — שעתיים לפני התור, בדיקה כל 30 דקות
        async def _second_reminders_job(context) -> None:
            try:
                result = send_second_reminders()
                if result["sent"]:
                    logger.info("Second reminders job: sent %d", result["sent"])
            except Exception as e:
                logger.error("Second reminders job failed: %s", e)

        application.job_queue.run_repeating(
            _with_default_tenant(_second_reminders_job),
            interval=1800,  # 30 דקות
            first=180,      # ריצה ראשונה אחרי 3 דקות
            name="second_reminders",
        )

        # Retention purge — מחיקה אוטומטית של מידע ישן לפי תקופות השמירה המוצהרות
        # במדיניות הפרטיות (12 חודשים לשיחות, 36 לתורים שעברו). רץ פעם ביום.
        async def _retention_purge_job(context) -> None:
            try:
                import database as _db
                counts = _db.purge_old_data()
                total = sum(counts.values()) if counts else 0
                if total:
                    logger.info("Retention purge: removed %d rows (%s)", total, counts)
            except Exception as e:
                logger.error("Retention purge job failed: %s", e)

        application.job_queue.run_repeating(
            _with_default_tenant(_retention_purge_job),
            interval=24 * 3600,  # פעם ביממה
            first=300,           # ריצה ראשונה אחרי 5 דקות
            name="retention_purge",
        )

        # Follow-up לידים — בדיקה תקופתית ושליחת follow-up ללידים שלא קבעו תור
        from ai_chatbot.config import FOLLOWUP_ENABLED, FOLLOWUP_CHECK_INTERVAL_MINUTES
        if FOLLOWUP_ENABLED:
            from ai_chatbot.followup_service import process_pending_followups

            application.job_queue.run_repeating(
                _with_default_tenant(process_pending_followups),
                interval=FOLLOWUP_CHECK_INTERVAL_MINUTES * 60,
                first=240,  # ריצה ראשונה אחרי 4 דקות
                name="followup_leads",
            )
            logger.info(
                "Follow-up scheduler started — checking every %d minutes",
                FOLLOWUP_CHECK_INTERVAL_MINUTES,
            )

    # Build the application
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    _register_handlers(app)
    logger.info("Telegram bot application configured successfully")
    return app


def create_tenant_bot_application(token: str) -> Application:
    """אפליקציית בוט ל-tenant (multi-tenant שלב 2 — ריבוי בוטים בתהליך).

    זהה לאפליקציה הרגילה בכל ה-handlers, אבל:
    - הטוקן מגיע מהסודות של ה-tenant (לא מ-env).
    - בלי post_init: אין רישום ל-bot_state הגלובלי (הרישום נעשה
      ב-bot_registry פר-tenant) ואין JobQueue jobs — העבודות המתוזמנות
      רצות ב-schedulers הפלטפורמתיים שמאתרים על פני כל ה-tenants.
    """
    if not token:
        raise ValueError("tenant bot token is empty")
    app = ApplicationBuilder().token(token).build()
    _register_handlers(app)
    return app


def _register_handlers(app: Application) -> None:
    """רישום כל ה-handlers — משותף לבוט ה-legacy ולבוטים פר-tenant."""
    # ─── Conversation handler for appointment booking ─────────────────────
    # Filter that matches any main-menu button text — used to let button
    # clicks break out of an active booking conversation.
    button_filter = filters.TEXT & filters.Regex(
        r"^(" + "|".join(re.escape(t) for t in ALL_BUTTON_TEXTS) + r")$"
    )

    booking_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^" + re.escape(BUTTON_BOOKING) + r"$"), booking_start),
            CommandHandler("book", booking_start),
        ],
        states={
            BOOKING_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~button_filter, booking_service)],
            BOOKING_DATE: [
                CallbackQueryHandler(calendar_select_callback, pattern=r"^" + re.escape(CB_CALENDAR_SELECT)),
                CallbackQueryHandler(calendar_navigate_callback, pattern=r"^(" + re.escape(CB_CALENDAR_PREV) + "|" + re.escape(CB_CALENDAR_NEXT) + ")"),
                CallbackQueryHandler(calendar_ignore_callback, pattern=r"^" + re.escape(CB_CALENDAR_IGNORE) + r"$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~button_filter, booking_date),
            ],
            BOOKING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~button_filter, booking_time)],
            BOOKING_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~button_filter, booking_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", booking_cancel),
            MessageHandler(button_filter, booking_button_interrupt),
        ],
    )
    
    # ─── Register handlers (order matters!) ───────────────────────────────
    
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stop", stop_command))
    # /unsubscribe — alias ל-/stop כדי להתאים למה שמוצהר במדיניות הפרטיות
    app.add_handler(CommandHandler("unsubscribe", stop_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("referral", referral_command))
    # זכויות נושאי מידע (תיקון 13)
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("forget", forget_command))
    # /agent — alias שמפעיל את אותה לוגיקה כמו כפתור "דברו עם נציג"
    app.add_handler(CommandHandler("agent", talk_to_agent_handler))

    # Booking conversation (must be before the general message handler)
    app.add_handler(booking_handler)

    # Cancellation confirmation (inline keyboard callback)
    app.add_handler(CallbackQueryHandler(cancel_appointment_callback, pattern=r"^cancel_(appt|select)_"))

    # Reschedule appointment (inline keyboard callback)
    app.add_handler(CallbackQueryHandler(reschedule_appointment_callback, pattern=r"^reschedule_(select|confirm|no)"))

    # שאלות המשך (inline keyboard callback)
    app.add_handler(CallbackQueryHandler(follow_up_callback, pattern=rf"^{re.escape(FOLLOW_UP_CB_PREFIX)}"))

    # מסך הסכמה + מחיקת מידע (תיקון 13)
    app.add_handler(CallbackQueryHandler(
        consent_callback,
        pattern=rf"^({re.escape(CB_CONSENT_ACCEPT)}|{re.escape(CB_CONSENT_DECLINE)})$",
    ))
    app.add_handler(CallbackQueryHandler(
        forget_callback,
        pattern=rf"^({re.escape(CB_FORGET_CONFIRM)}|{re.escape(CB_FORGET_CANCEL)})$",
    ))

    # General text messages (catch-all)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Error handler
    app.add_error_handler(error_handler)


def run_bot():
    """Start the Telegram bot (blocking call).

    מצב ההפעלה נקבע אוטומטית:
    - אם WEBHOOK_URL מוגדר → webhook mode (שימושי כש-bot רץ לבד עם --bot)
    - אחרת → polling mode (ברירת מחדל)
    """
    from ai_chatbot.config import WEBHOOK_URL, WEBHOOK_SECRET

    app = create_bot_application()

    if WEBHOOK_URL:
        # מצב webhook — הבוט מאזין לבקשות נכנסות מטלגרם
        from urllib.parse import urlparse
        parsed = urlparse(WEBHOOK_URL)
        webhook_path = parsed.path or "/telegram/webhook"
        # אם ה-URL לא כלל path — מוסיפים אותו גם ל-URL שנרשם בטלגרם
        effective_webhook_url = WEBHOOK_URL
        if not parsed.path or parsed.path == "/":
            effective_webhook_url = WEBHOOK_URL.rstrip("/") + "/telegram/webhook"
        # פורט 443 כברירת מחדל — טלגרם תומך בפורטים 443, 80, 88, 8443
        listen_port = int(parsed.port or 8443)

        logger.info("Starting Telegram bot in WEBHOOK mode at %s", effective_webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=listen_port,
            url_path=webhook_path.lstrip("/"),
            webhook_url=effective_webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting Telegram bot in POLLING mode...")
        app.run_polling(drop_pending_updates=True)


async def setup_webhook_via_flask(app: Application, webhook_url: str,
                                  secret_token: str | None = None) -> None:
    """אתחול האפליקציה ורישום webhook בטלגרם — לשימוש כשהבוט משולב עם Flask.

    נקרא מ-main.py כשהבוט עובד במצב webhook דרך ה-Flask server הקיים.
    run_polling/run_webhook קוראים ל-post_init אוטומטית, אבל initialize() לבד לא.
    לכן קוראים ידנית כדי לרשום job queue tasks ולהגדיר set_bot().
    """
    await app.initialize()
    # post_init — רישום jobs (cleanup, reminders) + set_bot()
    if app.post_init:
        await app.post_init(app)
    await app.bot.set_webhook(
        url=webhook_url,
        secret_token=secret_token or None,
        drop_pending_updates=True,
    )
    await app.start()
    logger.info("Webhook registered at %s", webhook_url)


async def shutdown_webhook_app(app: Application) -> None:
    """ניקוי — מסיר webhook ומכבה את האפליקציה."""
    try:
        await app.bot.delete_webhook()
    except Exception as e:
        logger.error("Failed to delete webhook: %s", e)
    try:
        await app.stop()
        await app.shutdown()
    except Exception as e:
        logger.error("Failed to shutdown bot application: %s", e)
