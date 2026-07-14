"""
BroadcastService — שירות לשליחת הודעות יזומות (broadcast) ללקוחות.

השירות מקבל הודעה ורשימת נמענים, ושולח ברקע עם delay בין הודעות
כדי לעמוד במגבלות Telegram (rate limit).

ארכיטקטורה:
- הפאנל יוצר broadcast_messages ומפעיל את ה-worker דרך asyncio.
- ה-worker שולח הודעה-הודעה עם השהייה, מעדכן את ה-DB בהתקדמות,
  ומטפל ב-RetryAfter / Forbidden בצורה גמישה.
- תומך במספר ערוצים (Telegram, WhatsApp) — הערוץ נקבע לפי המשתמש.
"""

import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TimedOut, BadRequest

from ai_chatbot import database as db

logger = logging.getLogger(__name__)

# השהייה בין הודעות — 0.05 שניות (20 הודעות/שנייה).
# מגבלת טלגרם: 30 הודעות/שנייה לבוטים רגילים, כך שיש מרווח של ~33%.
_SEND_DELAY = 0.05

# אורך מקסימלי של הודעת טלגרם
_MAX_MESSAGE_LENGTH = 4096

# עדכון התקדמות ב-DB כל N הודעות (לא כל הודעה — חוסך עומס על ה-DB)
_PROGRESS_UPDATE_INTERVAL = 10


def _safe_unsubscribe(broadcast_id: int, user_id: str) -> None:
    """ביטול הרשמת משתמש עם הגנה מפני כשל DB — לא עוצר את לולאת השליחה."""
    try:
        db.unsubscribe_user(user_id)
    except Exception as e:
        logger.error("Broadcast %d: failed to unsubscribe user %s: %s", broadcast_id, user_id, e)


async def _send_whatsapp_broadcast_message(message_text: str, user_id: str) -> None:
    """שליחת הודעת broadcast יחידה דרך WhatsApp (Twilio)."""
    from messaging.whatsapp_sender import send_whatsapp
    await asyncio.to_thread(send_whatsapp, user_id, message_text)


async def send_broadcast(
    bot: Bot,
    broadcast_id: int,
    message_text: str,
    recipients: list[str],
    *,
    needs_init: bool = False,
    recipients_with_channel: Optional[list[dict]] = None,
) -> None:
    """שליחת הודעת שידור לרשימת נמענים ברקע.

    מעדכן את ה-DB בהתקדמות ובסיום. מטפל ב-RetryAfter (429) ו-Forbidden (חסום).
    אם needs_init=True (admin-only mode), מאתחל את ה-Bot לפני השליחה וסוגר בסוף.

    אם recipients_with_channel מסופק — משתמש בערוץ המתאים לכל נמען.
    אחרת — שולח דרך Telegram (תאימות לאחור).

    ⚠ feature gate (Phase 3): לא מתבצע אם החבילה הנוכחית לא כוללת broadcast.
    זה defense in depth — הראוט באדמין כבר חוסם, אבל אם השירות נקרא ממקום
    אחר (scheduler / API פנימי), חייב לעצור כאן.
    """
    # ── מצב דמו — לא שולחים שידורים בפועל ──
    # שכבה נוספת מעל ה-middleware של admin: גם cron / scheduler לא ישלח
    # שידור המוני ב-deployment דמו. מסמנים את השידור כ-completed (לא failed)
    # כדי שה-UI יציג סטטוס נורמלי לגולש הדמו. ראה docs/demo-mode-spec.md.
    # fail-closed: אם הקונפיגורציה לא נטענת — מתייחסים כאל דמו (לא שולחים)
    # ולא כאל פרודקשן. עדיף שגולש דמו יראה "השידור נשלח" כשבעצם לא נשלח,
    # מאשר להציף לקוחות אמיתיים אם משהו נשבר בקונפיג.
    try:
        from ai_chatbot.config import DEMO_MODE
    except ImportError:
        logger.error("broadcast_service: failed to import DEMO_MODE — failing closed (treating as demo)")
        DEMO_MODE = True
    if DEMO_MODE:
        # ספירת נמענים נכונה — אם recipients_with_channel סופק, הוא ה-list
        # הסמכותי (משמש בנתיב הרגיל למטה ב-recipient_list). אחרת — recipients.
        total_recipients = (
            len(recipients_with_channel) if recipients_with_channel else len(recipients)
        )
        logger.info(
            "DEMO_MODE: skipping broadcast_id=%d (recipients=%d, chars=%d)",
            broadcast_id, total_recipients, len(message_text),
        )
        try:
            db.complete_broadcast(broadcast_id, total_recipients, 0)
        except Exception:
            logger.error(
                "DEMO_MODE: failed to mark broadcast %d completed",
                broadcast_id, exc_info=True,
            )
        return

    # ── feature gate ──────────────────────────────────────────────────
    try:
        from ai_chatbot import feature_flags
        if not feature_flags.has_feature("broadcast"):
            logger.warning(
                "send_broadcast: blocked broadcast_id=%d — feature 'broadcast' "
                "is not active in current plan (%s)",
                broadcast_id, feature_flags.get_current_plan(),
            )
            try:
                db.fail_broadcast(broadcast_id)
            except Exception:
                logger.error(
                    "send_broadcast: failed to mark broadcast %d as failed",
                    broadcast_id, exc_info=True,
                )
            return
    except Exception:
        # אם feature_flags עצמו זרק (לא אמור — bullet-proof) — לא חוסמים
        # שליחה לגיטימית. ממשיכים אבל מלוגים.
        logger.error(
            "send_broadcast: feature_flags check raised — proceeding anyway",
            exc_info=True,
        )
    # בניית רשימת נמענים עם ערוץ
    if recipients_with_channel:
        recipient_list = recipients_with_channel
    else:
        recipient_list = [{"user_id": uid, "channel": "telegram"} for uid in recipients]

    total = len(recipient_list)

    # ולידציה — אורך הודעה (BC2)
    if len(message_text) > _MAX_MESSAGE_LENGTH:
        logger.error(
            "Broadcast %d: message too long (%d chars, max %d)",
            broadcast_id, len(message_text), _MAX_MESSAGE_LENGTH,
        )
        db.fail_broadcast(broadcast_id)
        return

    # אתחול Bot שנוצר מחוץ ל-Application (admin-only mode)
    if needs_init and bot is not None:
        await bot.initialize()

    sent = 0
    failed = 0

    try:
        # סימון מיידי כ-sending — גם לרשימות קטנות מ-PROGRESS_UPDATE_INTERVAL
        db.mark_broadcast_sending(broadcast_id)

        for i, recipient in enumerate(recipient_list):
            user_id = recipient["user_id"]
            channel = recipient.get("channel", "telegram")

            try:
                if channel == "whatsapp":
                    await _send_whatsapp_broadcast_message(message_text, user_id)
                elif bot is not None:
                    await bot.send_message(chat_id=int(user_id), text=message_text)
                else:
                    logger.error("Broadcast %d: no bot for Telegram user %s", broadcast_id, user_id)
                    failed += 1
                    continue
                sent += 1
            except Forbidden:
                # המשתמש חסם את הבוט — מסמנים כלא-מנוי (Telegram בלבד)
                logger.info("Broadcast %d: user %s blocked the bot, unsubscribing", broadcast_id, user_id)
                _safe_unsubscribe(broadcast_id, user_id)
                failed += 1
            except RetryAfter as e:
                # טלגרם מבקש להמתין — מכבדים ומנסים שוב (Telegram בלבד)
                logger.warning("Broadcast %d: rate limited, waiting %s seconds", broadcast_id, e.retry_after)
                await asyncio.sleep(e.retry_after)
                try:
                    if channel == "whatsapp":
                        await _send_whatsapp_broadcast_message(message_text, user_id)
                    elif bot is not None:
                        await bot.send_message(chat_id=int(user_id), text=message_text)
                    else:
                        raise RuntimeError("No bot available for Telegram retry")
                    sent += 1
                except Forbidden:
                    logger.info("Broadcast %d: user %s blocked the bot on retry, unsubscribing", broadcast_id, user_id)
                    _safe_unsubscribe(broadcast_id, user_id)
                    failed += 1
                except Exception as retry_err:
                    logger.error("Broadcast %d: retry failed for user %s: %s", broadcast_id, user_id, retry_err)
                    failed += 1
            except (TimedOut, BadRequest) as e:
                logger.error("Broadcast %d: failed for user %s: %s", broadcast_id, user_id, e)
                failed += 1
            except Exception as e:
                logger.error("Broadcast %d: unexpected error for user %s (%s): %s", broadcast_id, user_id, channel, e)
                failed += 1

            # עדכון התקדמות ב-DB מדי פעם — async כדי לא לחסום את ה-event loop (BC3)
            if (i + 1) % _PROGRESS_UPDATE_INTERVAL == 0:
                try:
                    await asyncio.to_thread(db.update_broadcast_progress, broadcast_id, sent, failed)
                except Exception as e:
                    logger.error("Broadcast %d: progress update failed: %s", broadcast_id, e)

            await asyncio.sleep(_SEND_DELAY)

        # סיום — עדכון סופי
        db.complete_broadcast(broadcast_id, sent, failed)
        logger.info(
            "Broadcast %d completed: %d sent, %d failed out of %d recipients",
            broadcast_id, sent, failed, total,
        )
    except Exception:
        # Safety net: exception לא-צפויה בלולאה (cancellation, OOM, וכו׳)
        # היתה משאירה את השידור תקוע ב-status='sending' לנצח. מסמנים failed
        # עם הספירות שכבר יש, כדי שה-UI יראה את האמת ולא "שולח..." נעוץ.
        logger.error(
            "Broadcast %d: unexpected exception in send loop", broadcast_id,
            exc_info=True,
        )
        try:
            db.fail_broadcast(broadcast_id, sent, failed)
        except Exception:
            logger.error(
                "Broadcast %d: fail_broadcast fallback נכשל", broadcast_id,
                exc_info=True,
            )
        raise
    finally:
        # סגירת ה-Bot אם אותחל כאן (admin-only mode)
        # בתוך try/except נפרד כדי שכשל ב-shutdown לא ידרוס סטטוס completed
        if needs_init and bot is not None:
            try:
                await bot.shutdown()
            except Exception as e:
                logger.error("Broadcast %d: bot shutdown failed: %s", broadcast_id, e)


def start_broadcast_task(
    bot: Bot,
    broadcast_id: int,
    message_text: str,
    recipients: list[str],
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    needs_init: bool = False,
    recipients_with_channel: Optional[list[dict]] = None,
) -> None:
    """הפעלת שליחת שידור כ-task ברקע ב-event loop קיים.

    נקרא מתוך Flask (thread נפרד) — מזריק task ל-event loop של הבוט.
    אם אין event loop (למשל admin-only mode) — שולח סינכרוני ב-thread חדש.

    ⚠ feature gate (Phase 3): נבדק כאן בנוסף ל-send_broadcast עצמו, כדי
    שגם broadcast יזום מ-scheduler יידע מיד שהפעולה נחסמה במקום לפתוח
    thread/loop ולסיים מיד אחר כך.
    """
    try:
        from ai_chatbot import feature_flags
        if not feature_flags.has_feature("broadcast"):
            logger.warning(
                "start_broadcast_task: blocked broadcast_id=%d — feature "
                "'broadcast' is not active in current plan (%s)",
                broadcast_id, feature_flags.get_current_plan(),
            )
            try:
                db.fail_broadcast(broadcast_id)
            except Exception:
                logger.error(
                    "start_broadcast_task: failed to mark broadcast %d as failed",
                    broadcast_id, exc_info=True,
                )
            return
    except Exception:
        logger.error(
            "start_broadcast_task: feature_flags check raised — proceeding",
            exc_info=True,
        )
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            send_broadcast(
                bot, broadcast_id, message_text, recipients,
                needs_init=needs_init,
                recipients_with_channel=recipients_with_channel,
            ),
            loop,
        )
        # טיפול בשגיאות שנופלות מחוץ ללולאת ה-per-message (למשל DB errors)
        future.add_done_callback(
            lambda f: _handle_future_error(f, broadcast_id)
        )
    else:
        # fallback — הרצה בלולאה חדשה (admin-only mode ללא בוט פעיל)
        import threading

        def _run():
            try:
                asyncio.run(send_broadcast(
                    bot, broadcast_id, message_text, recipients,
                    needs_init=needs_init,
                    recipients_with_channel=recipients_with_channel,
                ))
            except Exception as e:
                logger.error("Broadcast thread failed: %s", e)
                # לא דורסים sent/failed — שומרים את ההתקדמות שכבר נכתבה ל-DB
                db.fail_broadcast(broadcast_id)

        thread = threading.Thread(target=_run, daemon=True, name=f"broadcast-{broadcast_id}")
        thread.start()


def _handle_future_error(future: asyncio.Future, broadcast_id: int) -> None:
    """callback לטיפול בשגיאות של broadcast task שרץ ב-event loop."""
    if future.cancelled():
        # ה-task בוטל (למשל כיבוי הבוט) — מסמנים ככישלון כדי שלא יישאר תקוע ב-sending
        logger.warning("Broadcast %d task was cancelled", broadcast_id)
        try:
            db.fail_broadcast(broadcast_id)
        except Exception as db_err:
            logger.error("Broadcast %d: failed to mark cancelled broadcast in DB: %s", broadcast_id, db_err)
        return

    exc = future.exception()
    if exc is not None:
        logger.error("Broadcast %d task failed: %s", broadcast_id, exc)
        try:
            # לא דורסים sent/failed — שומרים את ההתקדמות שכבר נכתבה ל-DB
            db.fail_broadcast(broadcast_id)
        except Exception as db_err:
            logger.error("Broadcast %d: failed to mark as failed in DB: %s", broadcast_id, db_err)
