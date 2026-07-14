"""
שלב 6 — Background extraction scheduler.

Thread רקע שמופעל מ-main.py דרך start_scheduler(). כל 5 דקות סורק את
conversations, מזהה משתמשים עם שיחות שהסתיימו (אין הודעה ב-30 הדקות
האחרונות), ומפעיל run_extraction_for_user (memory/validator.py).

הדפוס נלקח מ-messaging/broadcast_scheduler.py — אותו מבנה של thread
דאמון, threading.Event לעצירה נקייה, ENV check בתוך start_scheduler.

ENV vars (config.py):
- MEMORY_BACKGROUND_ENABLED (default true) — כיבוי קונפיגורבילי.
- MEMORY_IDLE_MINUTES (default 30) — סף "שיחה נגמרה".
- MEMORY_LOOKBACK_DAYS (default 7) — חלון סריקה לאחור.
- MEMORY_CONVERSATION_CAP (default 50) — cap הודעות לסבב.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from ai_chatbot import database as db
from ai_chatbot.config import (
    BUSINESS_ID,
    MEMORY_BACKGROUND_ENABLED,
    MEMORY_CONVERSATION_CAP,
    MEMORY_IDLE_MINUTES,
    MEMORY_LOOKBACK_DAYS,
)
from memory.validator import run_extraction_for_user

logger = logging.getLogger(__name__)

# כמו broadcast_scheduler — קבוע ברמת מודול, לא ENV. שינוי דורש דיפלוי.
_POLL_INTERVAL = 5 * 60  # 5 דקות

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()

# Lock על user_ids בעיבוד כרגע. process-local בלבד.
#
# **מגבלת ארכיטקטורה ידועה (מתועדת)**: אם בעתיד יעלו multi-worker
# (gunicorn -w 2+, או scheduler נפרד מ-bot), ה-set הזה לא ימנע
# double-extraction כי כל process יחזיק set משלו. הפתרון אז:
# advisory lock ב-DB (טבלה ייעודית `scheduler_locks(user_id PK,
# locked_at)` עם TTL). כיום deployment הוא single-process על Render
# Web Service, ה-set מספיק.
# המפתח: (tenant, user_id) — אותו לקוח אצל שני עסקים = שתי עבודות נפרדות.
_in_progress: set[tuple[str, str]] = set()
_lock = threading.Lock()


def start_scheduler() -> bool:
    """Idempotent. מחזיר True אם רץ (כבר או חדש), False אם disabled.

    ENV check בפנים כדי ש-main.py לא צריך לדעת על ה-flag (דפוס
    broadcast_scheduler).
    """
    global _scheduler_thread
    if not MEMORY_BACKGROUND_ENABLED:
        logger.info(
            "memory.background: disabled via MEMORY_BACKGROUND_ENABLED"
        )
        return False
    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.info("memory.background: already running")
        return True
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="memory-background", daemon=True,
    )
    _scheduler_thread.start()
    logger.info(
        "memory.background: scheduler started "
        "(poll=%ds, idle=%dmin, lookback=%dd)",
        _POLL_INTERVAL, MEMORY_IDLE_MINUTES, MEMORY_LOOKBACK_DAYS,
    )
    return True


def stop_scheduler(timeout: float = 5.0) -> None:
    """כיבוי נקי — set ה-event ו-join עם timeout. אם thread לא נסגר
    בזמן, מסתפק ב-warning (הוא daemon, ייהרס בכיבוי תהליך).
    """
    _scheduler_stop.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=timeout)
        if _scheduler_thread.is_alive():
            logger.warning(
                "memory.background: thread didn't stop in %ss — "
                "daemon will be killed on process exit",
                timeout,
            )
    logger.info("memory.background: scheduler stopped")


def _scheduler_loop() -> None:
    """try/except חיצוני — כשל ב-cycle אחד לא עוצר את הלולאה.
    wait() במקום sleep() כדי שכיבוי יהיה מיידי (event.set() מעיר אותו).
    """
    from tenancy import tenant_context

    while not _scheduler_stop.is_set():
        # כל ה-tenants הפעילים, כל אחד ב-context משלו; כשל אצל אחד לא
        # עוצר את השאר (כלל לולאות I/O ב-CLAUDE.md).
        try:
            from control_plane import list_schedulable_tenant_ids

            tenant_ids = list_schedulable_tenant_ids()
        except Exception:
            logger.exception("memory.background: listing tenants failed")
            tenant_ids = []
        for tenant_id in tenant_ids:
            if _scheduler_stop.is_set():
                break
            try:
                with tenant_context(tenant_id):
                    _process_due_users()
            except Exception:
                logger.exception(
                    "memory.background: cycle failed (tenant=%s)", tenant_id,
                )
        _scheduler_stop.wait(_POLL_INTERVAL)


def _process_due_users() -> None:
    """Cycle אחד של ה-scheduler.

    נכתב כפונקציה ציבורית (לא _) כדי שטסטים יוכלו לקרוא לה ישירות
    בלי לסטרט thread. (ה-underscore רק כדי לציין שזה לא חלק מה-API
    החיצוני — start/stop בלבד.)
    """
    start_ts = time.monotonic()
    counts = {
        "scanned": 0,
        "extracted": 0,
        "skipped_active": 0,
        "skipped_no_new_messages": 0,
        "skipped_locked": 0,
        "errors": 0,
    }

    now = datetime.now(timezone.utc)
    since_lookback = (
        now - timedelta(days=MEMORY_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    idle_threshold = now - timedelta(minutes=MEMORY_IDLE_MINUTES)

    # שלב 6.4 — get_users_with_pending_messages עוקף את הבאג של "user שנעלם
    # עם backlog": משתמש שלא חזר ב-7 ימים עדיין מופיע אם יש לו הודעות עם
    # `id > last_message_id`. since_lookback עדיין נחוץ למטה כ-fallback
    # ב-get_conversation_after למשתמש חדש (אין run עם last_message_id).
    user_ids = db.get_users_with_pending_messages(
        BUSINESS_ID, MEMORY_LOOKBACK_DAYS,
    )
    counts["scanned"] = len(user_ids)

    from tenancy import get_current_tenant

    _tenant = get_current_tenant()
    for user_id in user_ids:
        # Lock: אם משתמש בעיבוד מ-cycle קודם (תקוע על LLM ארוך?), דלג.
        _key = (_tenant, user_id)
        with _lock:
            if _key in _in_progress:
                counts["skipped_locked"] += 1
                continue
            _in_progress.add(_key)

        try:
            # שלב 6.3 — idle check **לפני** טעינת ההודעות וקריאת LLM.
            # שאילתה זולה (אינדקס user_id+created_at), חוסכת round-trip
            # מיותר אם השיחה עדיין פעילה. **קריטי**: מבוסס על MAX(created_at)
            # של כל הודעות המשתמש, לא ה-batch. backlog ארוך עלול לכלול
            # הודעות ישנות (id נמוך) למרות ששיחה פעילה בפועל; הבדיקה כאן
            # שואלת "האם בכלל היה משהו ב-30 הדקות האחרונות?".
            last_msg_iso = db.get_user_last_message_time(user_id)
            if last_msg_iso:
                try:
                    last_dt = datetime.strptime(
                        last_msg_iso, "%Y-%m-%d %H:%M:%S",
                    ).replace(tzinfo=timezone.utc)
                    if last_dt > idle_threshold:
                        counts["skipped_active"] += 1
                        continue
                except ValueError:
                    logger.warning(
                        "memory.background: bad created_at format: %s",
                        last_msg_iso,
                    )

            # cursor הוא id (monotonic, unique, atomic). אם אין run קודם
            # (משתמש חדש או runs ישנים בלי last_message_id) → fallback
            # ל-since_lookback פעם אחת.
            last_id = db.get_last_extraction_message_id(user_id, BUSINESS_ID)
            messages = db.get_conversation_after(
                user_id,
                after_id=last_id,
                since_iso=since_lookback if last_id is None else None,
                limit=MEMORY_CONVERSATION_CAP,
            )
            if len(messages) < 2:
                counts["skipped_no_new_messages"] += 1
                continue

            result = run_extraction_for_user(
                user_id, BUSINESS_ID, messages,
            )
            if result.get("status") == "completed":
                counts["extracted"] += 1
            else:
                counts["errors"] += 1
                logger.warning(
                    "memory.background: extraction failed user_id=%s: %s",
                    user_id, result.get("error"),
                )
        except Exception:
            counts["errors"] += 1
            logger.exception(
                "memory.background: unexpected error user_id=%s",
                user_id,
            )
        finally:
            with _lock:
                _in_progress.discard(_key)

    duration = time.monotonic() - start_ts
    logger.info(
        "memory.background cycle: scanned=%d extracted=%d "
        "skipped_active=%d skipped_no_new=%d skipped_locked=%d "
        "errors=%d duration=%.1fs",
        counts["scanned"], counts["extracted"], counts["skipped_active"],
        counts["skipped_no_new_messages"], counts["skipped_locked"],
        counts["errors"], duration,
    )
