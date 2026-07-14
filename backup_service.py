"""
Backup Service — גיבוי לילי עקבי של קבצי ה-tenants (multi-tenant שלב 2).

עד עכשיו לא היה שום מנגנון גיבוי בקוד — העמידות נשענה כולה על הדיסק
של Render. בפלטפורמה עם עסקים רבים בקובץ-לכל-אחד זה SPOF שחייב כיסוי.

מה מגובה:
- קובץ ה-SQLite של כל tenant פעיל (כולל ה-tenant של ברירת המחדל).
- קובץ ה-`platform.db` (רישום ה-control plane — קריטי לשחזור).
- תיקיית אינדקס ה-FAISS של כל tenant (אם קיימת).

עקביות: ה-SQLite מגובה דרך `sqlite3` **online backup API** (לא cp) —
בטוח לגיבוי בזמן ש-WAL פעיל וכתיבות מתרחשות, בלי לתפוס lock ארוך.

יעד: `BACKUP_DIR` (env, ברירת מחדל `DATA_DIR/backups`) — דיסק מקומי/mounted.
העלאה ל-object storage היא **seam מפורש**: מגדירים `_upload_hook` והוא
נקרא לכל ארכיון שנוצר. בלי hook — גיבוי מקומי בלבד (עם rotation).
"""

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# seam ל-object storage: פונקציה (local_path: Path, relative_key: str) -> None.
# בלי hook — לא מעלים לענן, רק שומרים מקומית.
_upload_hook: Optional[Callable[[Path, str], None]] = None


def set_upload_hook(hook: Optional[Callable[[Path, str], None]]) -> None:
    """רישום פונקציית העלאה ל-object storage (S3/GCS/וכו')."""
    global _upload_hook
    _upload_hook = hook


def _backup_dir() -> Path:
    import config as _config

    raw = os.getenv("BACKUP_DIR", "").strip()
    base = Path(raw) if raw else Path(_config.DATA_DIR) / "backups"
    return base


def _retention_days() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "14")))
    except ValueError:
        return 14


def _sqlite_backup(src: Path, dst: Path) -> None:
    """גיבוי עקבי של קובץ SQLite דרך online backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _maybe_upload(local_path: Path, relative_key: str) -> None:
    if _upload_hook is None:
        return
    try:
        _upload_hook(local_path, relative_key)
    except Exception:
        # כשל העלאה לא צריך להפיל את הגיבוי המקומי — הוא עדיין קיים על הדיסק
        logger.error("backup upload hook failed for %s", relative_key, exc_info=True)


def backup_tenant(tenant_id: str, stamp: str) -> bool:
    """גיבוי של tenant בודד (DB + FAISS). מחזיר True בהצלחה.

    ה-stamp הוא חותמת התיקייה (מועבר מבחוץ כי Date.now לא זמין כאן —
    הקורא מספק אותו). כשל בקובץ אחד נרשם ולא זורק — הקורא ממשיך לשאר.
    """
    from tenancy import tenant_context, tenant_db_path, tenant_faiss_dir

    try:
        with tenant_context(tenant_id):
            db_src = tenant_db_path()
            faiss_src = tenant_faiss_dir()
    except Exception:
        logger.error("backup_tenant: resolving paths failed (%s)", tenant_id, exc_info=True)
        return False

    if not Path(db_src).exists():
        # tenant רשום אבל בלי קובץ DB עדיין (לא אמור לקרות אחרי create_tenant)
        logger.warning("backup_tenant: no DB file for %s", tenant_id)
        return False

    dest_root = _backup_dir() / stamp / tenant_id
    ok = True
    try:
        db_dst = dest_root / "chatbot.db"
        _sqlite_backup(Path(db_src), db_dst)
        _maybe_upload(db_dst, f"{stamp}/{tenant_id}/chatbot.db")
    except Exception:
        logger.error("backup_tenant: DB backup failed (%s)", tenant_id, exc_info=True)
        ok = False

    try:
        faiss_dir = Path(faiss_src)
        if faiss_dir.exists() and faiss_dir.is_dir():
            faiss_dst = dest_root / "faiss_index"
            shutil.copytree(faiss_dir, faiss_dst, dirs_exist_ok=True)
            _maybe_upload(faiss_dst, f"{stamp}/{tenant_id}/faiss_index")
    except Exception:
        logger.error("backup_tenant: FAISS backup failed (%s)", tenant_id, exc_info=True)
        ok = False

    return ok


def backup_platform_db(stamp: str) -> bool:
    """גיבוי של platform.db (control plane) — קריטי לשחזור הרישום."""
    from control_plane import platform_db_path

    src = platform_db_path()
    if not src.exists():
        return True  # אין רישום עדיין — אין מה לגבות (מצב legacy)
    try:
        dst = _backup_dir() / stamp / "_platform" / "platform.db"
        _sqlite_backup(src, dst)
        _maybe_upload(dst, f"{stamp}/_platform/platform.db")
        return True
    except Exception:
        logger.error("backup_platform_db failed", exc_info=True)
        return False


def _prune_old_backups(now_epoch: float) -> int:
    """מחיקת תיקיות גיבוי ישנות מעבר ל-retention. מחזיר כמה נמחקו.

    ה-prune מבוסס על **שם התיקייה** (התאריך המקודד בו, `%Y-%m-%d`),
    לא על mtime של הדיסק — דטרמיניסטי ובלתי-תלוי ב-clock skew או ב-touch
    מחדש של קבצים (mtime היה עלול למחוק גיבוי טרי אם השעון קופץ).
    תיקיות ששמן אינו תאריך תקין — מדולגות בבטחה.
    """
    from datetime import datetime, timezone

    root = _backup_dir()
    if not root.exists():
        return 0
    cutoff_date = datetime.fromtimestamp(
        now_epoch - _retention_days() * 86400, tz=timezone.utc
    ).date()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue  # לא תיקיית תאריך — לא נוגעים
        if folder_date < cutoff_date:
            try:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
            except OSError:
                logger.error("prune: failed on %s", child, exc_info=True)
    if removed:
        logger.info("backup prune: removed %d old backup folder(s)", removed)
    return removed


def run_backup(stamp: str, now_epoch: float) -> dict:
    """גיבוי מלא: כל ה-tenants + platform.db + prune. מחזיר סיכום.

    stamp — חותמת התיקייה (למשל '2026-07-13'); now_epoch — לצורך prune.
    שניהם מסופקים ע"י הקורא (scheduler) כי הזמן אינו זמין דטרמיניסטית כאן.
    """
    from control_plane import list_schedulable_tenant_ids

    summary = {"tenants_ok": 0, "tenants_failed": 0, "platform_ok": False, "pruned": 0}

    try:
        tenant_ids = list_schedulable_tenant_ids()
    except Exception:
        logger.error("run_backup: listing tenants failed", exc_info=True)
        tenant_ids = []

    for tenant_id in tenant_ids:
        # כל tenant בעטיפת try/except משלו — כשל (גם בלתי-צפוי) אצל אחד
        # לא עוצר את גיבוי השאר (כלל לולאות I/O ב-CLAUDE.md).
        try:
            ok = backup_tenant(tenant_id, stamp)
        except Exception:
            logger.error("run_backup: כשל בלתי-צפוי ב-tenant %s", tenant_id, exc_info=True)
            ok = False
        summary["tenants_ok" if ok else "tenants_failed"] += 1

    summary["platform_ok"] = backup_platform_db(stamp)
    summary["pruned"] = _prune_old_backups(now_epoch)
    logger.info("nightly backup complete: %s", summary)
    return summary
