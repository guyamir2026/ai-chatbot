"""
Platform Maintenance Scheduler — תחזוקה תקופתית חוצת-tenants (שלב 2).

thread רקע יחיד (אותו דפוס כמו broadcast_scheduler) שמריץ:
- **גיבוי לילי** של קבצי כל ה-tenants + platform.db (backup_service).
- **Keep-alive שבועי** לטוקני Google Calendar (google_calendar) —
  מונע תפוגת "לא-בשימוש 6 חודשים" ומגלה ניתוקים מוקדם.

תזמון "כל X שעות מאז ההרצה האחרונה" (לא "בשעה 3 בלילה") — נשען על
last-run מתמיד ב-platform_meta, ולכן אידמפוטנטי בין restarts וחסין
לקצוות של שעון-קיר (מעבר יום, שעון קיץ). ריצה ראשונה אחרי deploy:
אין last-run ⇒ רץ מיד (גיבוי טרי אחרי deploy + keep-alive — שניהם
בטוחים ואידמפוטנטיים).

כיבוי: PLATFORM_MAINTENANCE_ENABLED=0.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# תדירות בדיקה — כל 15 דקות בודקים אם הגיע זמן למשימה כלשהי.
_POLL_INTERVAL = 15 * 60

# מרווחי המשימות (שעות). ניתנים לכוונון ב-env לצורך טסטים/תפעול.
_BACKUP_INTERVAL_H = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
_CALENDAR_REFRESH_INTERVAL_H = float(os.getenv("CALENDAR_REFRESH_INTERVAL_HOURS", "168"))

_KEY_LAST_BACKUP = "last_backup_epoch"
_KEY_LAST_CAL_REFRESH = "last_calendar_refresh_epoch"

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def _due(key: str, interval_hours: float, now_epoch: float) -> bool:
    """האם הגיע זמן למשימה — מרווח מאז ההרצה האחרונה שנשמרה."""
    from control_plane import get_platform_meta

    raw = get_platform_meta(key)
    if raw is None:
        return True  # מעולם לא רץ ⇒ רץ עכשיו
    try:
        last = float(raw)
    except (TypeError, ValueError):
        return True
    return (now_epoch - last) >= interval_hours * 3600


def _mark_ran(key: str, now_epoch: float) -> None:
    from control_plane import set_platform_meta

    set_platform_meta(key, str(now_epoch))


def run_due_tasks(now_epoch: float | None = None) -> dict:
    """מריץ את המשימות שהגיע זמנן. מחזיר מה בוצע (לטסטים/לוג).

    now_epoch אופציונלי — כברירת מחדל השעה הנוכחית (זמין ב-runtime;
    הפרמטר מאפשר דטרמיניזם בטסטים).
    """
    if now_epoch is None:
        now_epoch = time.time()
    ran = {"backup": None, "calendar_refresh": None}

    # ── גיבוי לילי ──
    if _due(_KEY_LAST_BACKUP, _BACKUP_INTERVAL_H, now_epoch):
        try:
            from backup_service import run_backup

            stamp = datetime.fromtimestamp(now_epoch, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            ran["backup"] = run_backup(stamp, now_epoch)
            _mark_ran(_KEY_LAST_BACKUP, now_epoch)
        except Exception:
            logger.error("platform_maintenance: backup failed", exc_info=True)

    # ── keep-alive של Google Calendar ──
    if _due(_KEY_LAST_CAL_REFRESH, _CALENDAR_REFRESH_INTERVAL_H, now_epoch):
        try:
            from google_calendar import refresh_all_tenant_calendars

            ran["calendar_refresh"] = refresh_all_tenant_calendars()
            _mark_ran(_KEY_LAST_CAL_REFRESH, now_epoch)
        except Exception:
            logger.error(
                "platform_maintenance: calendar refresh failed", exc_info=True,
            )

    return ran


def _scheduler_loop() -> None:
    logger.info("platform_maintenance: started (poll=%ds)", _POLL_INTERVAL)
    while not _scheduler_stop.is_set():
        try:
            run_due_tasks()
        except Exception:
            logger.error("platform_maintenance: loop iteration failed", exc_info=True)
        _scheduler_stop.wait(_POLL_INTERVAL)
    logger.info("platform_maintenance: stopped")


def start_scheduler() -> bool:
    """הפעלת ה-scheduler ב-thread ברקע. בטוח לקריאה חוזרת.

    Returns:
        True אם הופעל; False אם מושבת (PLATFORM_MAINTENANCE_ENABLED=0).
    """
    global _scheduler_thread
    if os.getenv("PLATFORM_MAINTENANCE_ENABLED", "1") == "0":
        logger.info("platform_maintenance: disabled by env")
        return False
    if _scheduler_thread and _scheduler_thread.is_alive():
        return True
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="platform-maintenance",
    )
    _scheduler_thread.start()
    return True


def stop_scheduler() -> None:
    _scheduler_stop.set()
