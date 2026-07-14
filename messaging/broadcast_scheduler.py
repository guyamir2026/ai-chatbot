"""
Broadcast Scheduler — thread ברקע שמבצע קמפיינים בזמן המתוזמן.

תפקידים:
    1. סוקר את broadcast_campaigns לחיפוש scheduled שהגיע זמנם.
    2. לכל קמפיין מתאים, מנסה להעביר sending (דרך start_campaign_send).
    3. אם הקמפיין MARKETING ונופל בחלון חסום (שבת/חג) — מעדכן scheduled_at
       ל-next_allowed_time ומשאיר scheduled; לא שולח עכשיו.
    4. כל iteration עטוף ב-try/except כדי שכשל בודד לא יעצור את ה-loop.

Deployment:
    - בסביבה עם worker יחיד (Render, docker single-container) — הכל תקין.
    - בסביבה עם מספר workers (gunicorn עם workers>1) — רק worker אחד צריך
      להריץ את ה-scheduler. הגדרת BROADCAST_SCHEDULER_ENABLED=1 בתהליך אחד
      ו-0 בשאר (או להשתמש ב-Celery/RQ בגרסה עתידית).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

# Imports at module level כך ש-monkeypatch בטסטים יוכל לדרוס אותם.
from messaging.broadcast_sender import start_campaign_send
from messaging.shabbat_window import is_blocked_for_marketing, next_allowed_time

logger = logging.getLogger(__name__)

_IL = ZoneInfo("Asia/Jerusalem")

# תדירות סקירה — שנייה/דקה. 60 דקה סבירה; בלחץ אפשר להקטין ל-30.
_POLL_INTERVAL = 60

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def _iso_now_il() -> str:
    """Current time in Israel TZ, formatted as 'YYYY-MM-DD HH:MM:SS' (SQLite-friendly)."""
    return datetime.now(_IL).strftime("%Y-%m-%d %H:%M:%S")


def _process_due_campaigns() -> None:
    """איתור וטיפול בקמפיינים שהגיע זמנם. נקרא מתוך ה-loop ברקע."""
    from ai_chatbot import database as db

    try:
        # scheduled_at מאוחסן כשעון ישראל. מעבירים זמן ישראלי נוכחי ל-DB
        # להשוואה מדויקת — לא מסתמכים על TZ של השרת (ב-Render זה UTC).
        now_str = _iso_now_il()
        due = db.list_due_scheduled_campaigns(now_str=now_str)
    except Exception:
        logger.error("broadcast_scheduler: שאילתת due נכשלה", exc_info=True)
        return

    for row in due:
        cid = int(row["id"])
        try:
            template = db.get_whatsapp_template(row["template_sid"])
            if not template:
                # תבנית נמחקה בין תזמון לביצוע — מסמנים failed ולא עובדים על זה שוב.
                logger.error(
                    "broadcast_scheduler: תבנית %s לא נמצאה — קמפיין %s סומן failed",
                    row["template_sid"], cid,
                )
                # scheduled → failed (ישירות; לא עוברים דרך draft כי לא נעבדו)
                with db.get_connection() as conn:
                    conn.execute(
                        "UPDATE broadcast_campaigns SET status = 'failed', "
                        "updated_at = datetime('now') WHERE id = ? AND status = 'scheduled'",
                        (cid,),
                    )
                continue

            category = (template.get("category") or "UTILITY").upper()
            now_il = datetime.now(_IL)

            # Auto-defer ל-MARKETING בחלון שבת/חגים
            if category == "MARKETING":
                blocked, reason = is_blocked_for_marketing(now_il)
                if blocked:
                    deferred = next_allowed_time(now_il, category="MARKETING")
                    deferred_str = deferred.strftime("%Y-%m-%d %H:%M:%S")
                    if db.reschedule_campaign_at(cid, deferred_str):
                        logger.info(
                            "broadcast_scheduler: קמפיין %s נדחה (%s) ל-%s",
                            cid, reason, deferred_str,
                        )
                    continue  # לא מעבירים ל-sending עדיין

            # לא חסום — מעבירים ישירות scheduled→sending ומפעילים thread
            started = start_campaign_send(cid, from_status="scheduled")
            if started:
                logger.info(
                    "broadcast_scheduler: קמפיין %s התחיל שליחה (scheduled_at=%s)",
                    cid, row["scheduled_at"],
                )
            else:
                logger.warning(
                    "broadcast_scheduler: start_campaign_send נכשל עבור %s", cid,
                )
        except Exception:
            logger.error(
                "broadcast_scheduler: שגיאה בעיבוד קמפיין %s", cid,
                exc_info=True,
            )


def _scheduler_loop() -> None:
    """לולאת ה-scheduler. יוצאת כש-_scheduler_stop נקרא."""
    logger.info("broadcast_scheduler: started (poll=%ds)", _POLL_INTERVAL)
    while not _scheduler_stop.is_set():
        try:
            _process_due_campaigns()
        except Exception:
            logger.error("broadcast_scheduler: loop iteration failed", exc_info=True)
        _scheduler_stop.wait(_POLL_INTERVAL)
    logger.info("broadcast_scheduler: stopped")


def start_scheduler() -> bool:
    """הפעלת ה-scheduler ב-thread ברקע. בטוח לקריאה חוזרת.

    Returns:
        True אם הופעל; False אם מושבת ע"י env var BROADCAST_SCHEDULER_ENABLED=0.
    """
    global _scheduler_thread
    if os.getenv("BROADCAST_SCHEDULER_ENABLED", "1") == "0":
        logger.info("broadcast_scheduler: disabled by BROADCAST_SCHEDULER_ENABLED=0")
        return False
    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.info("broadcast_scheduler: already running")
        return True
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="broadcast-scheduler",
    )
    _scheduler_thread.start()
    return True


def stop_scheduler() -> None:
    """עצירת ה-scheduler (לטסטים/shutdown)."""
    _scheduler_stop.set()
