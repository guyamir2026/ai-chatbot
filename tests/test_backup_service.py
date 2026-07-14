"""
טסטים ל-backup_service (multi-tenant שלב 2).

מכסים: גיבוי עקבי של DB (תוכן נשמר), גיבוי FAISS, platform.db, ‏prune
לפי שם תיקייה (חסין clock-skew), עמידות (כשל ב-tenant אחד לא עוצר את
השאר), וה-seam של העלאה ל-object storage.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import backup_service as bk
import control_plane as cp
from tenancy import tenant_context


def _epoch(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def platform_env(tmp_path):
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        from ai_chatbot import database as db

        db.init_db()
        cp.create_tenant("salon-a", "א")
        cp.create_tenant("salon-b", "ב")
        bk.set_upload_hook(None)
        yield tmp_path
        bk.set_upload_hook(None)
        cp.invalidate_status_cache()


def _seed(tenant_id, title):
    from ai_chatbot import database as db

    with tenant_context(tenant_id):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO kb_entries (category, title, content) "
                "VALUES ('FAQ', ?, 'x')",
                (title,),
            )


class TestBackupContent:
    def test_tenant_db_backed_up_with_content(self, platform_env):
        _seed("salon-a", "רשומה-א")
        assert bk.backup_tenant("salon-a", "2026-07-13") is True

        dst = platform_env / "backups" / "2026-07-13" / "salon-a" / "chatbot.db"
        assert dst.exists()
        conn = sqlite3.connect(str(dst))
        n = conn.execute(
            "SELECT COUNT(*) FROM kb_entries WHERE title='רשומה-א'"
        ).fetchone()[0]
        conn.close()
        assert n == 1

    def test_faiss_dir_copied(self, platform_env):
        from tenancy import tenant_faiss_dir

        with tenant_context("salon-a"):
            faiss_dir = Path(tenant_faiss_dir())
        faiss_dir.mkdir(parents=True, exist_ok=True)
        (faiss_dir / "index.faiss").write_bytes(b"fake-index")

        bk.backup_tenant("salon-a", "2026-07-13")
        copied = (
            platform_env / "backups" / "2026-07-13" / "salon-a"
            / "faiss_index" / "index.faiss"
        )
        assert copied.exists()
        assert copied.read_bytes() == b"fake-index"

    def test_platform_db_backed_up(self, platform_env):
        assert bk.backup_platform_db("2026-07-13") is True
        dst = platform_env / "backups" / "2026-07-13" / "_platform" / "platform.db"
        assert dst.exists()
        conn = sqlite3.connect(str(dst))
        rows = conn.execute("SELECT tenant_id FROM tenants ORDER BY tenant_id").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["salon-a", "salon-b"]


class TestRunBackup:
    def test_covers_all_tenants_and_platform(self, platform_env):
        _seed("salon-a", "a")
        _seed("salon-b", "b")
        summary = bk.run_backup("2026-07-13", _epoch(2026, 7, 13))
        assert summary["tenants_ok"] == 2
        assert summary["tenants_failed"] == 0
        assert summary["platform_ok"] is True
        base = platform_env / "backups" / "2026-07-13"
        assert (base / "salon-a" / "chatbot.db").exists()
        assert (base / "salon-b" / "chatbot.db").exists()

    def test_one_tenant_failure_does_not_stop_others(self, platform_env):
        _seed("salon-a", "a")
        _seed("salon-b", "b")
        orig = bk.backup_tenant

        def flaky(tenant_id, stamp):
            if tenant_id == "salon-a":
                raise RuntimeError("disk error")
            return orig(tenant_id, stamp)

        with patch.object(bk, "backup_tenant", side_effect=flaky):
            # run_backup עוטף כל tenant — כשל אצל א' לא מונע את ב'
            summary = bk.run_backup("2026-07-13", _epoch(2026, 7, 13))
        # א' נכשל (חריגה → נספר failed), ב' הצליח
        assert summary["tenants_failed"] == 1
        assert summary["tenants_ok"] == 1
        assert (platform_env / "backups" / "2026-07-13" / "salon-b" / "chatbot.db").exists()


class TestPrune:
    def test_prune_by_folder_date_not_mtime(self, platform_env):
        root = platform_env / "backups"
        (root / "2026-07-13").mkdir(parents=True)   # טרי
        (root / "2026-06-01").mkdir(parents=True)   # ישן (>14 יום)
        (root / "not-a-date").mkdir(parents=True)   # לא-תאריך

        # now = 2026-07-13; retention 14 יום → cutoff ~2026-06-29
        now = _epoch(2026, 7, 13)
        removed = bk._prune_old_backups(now)

        assert removed == 1
        assert (root / "2026-07-13").exists()       # טרי נשאר
        assert not (root / "2026-06-01").exists()   # ישן נמחק
        assert (root / "not-a-date").exists()       # לא-תאריך לא נגעו


class TestUploadHook:
    def test_hook_called_for_each_artifact(self, platform_env):
        _seed("salon-a", "a")
        uploaded = []
        bk.set_upload_hook(lambda path, key: uploaded.append(key))
        bk.backup_tenant("salon-a", "2026-07-13")
        assert any(k.endswith("chatbot.db") for k in uploaded)

    def test_upload_failure_does_not_break_local_backup(self, platform_env):
        _seed("salon-a", "a")

        def boom(path, key):
            raise RuntimeError("s3 down")

        bk.set_upload_hook(boom)
        # הגיבוי המקומי מצליח למרות כשל ההעלאה
        assert bk.backup_tenant("salon-a", "2026-07-13") is True
        assert (
            platform_env / "backups" / "2026-07-13" / "salon-a" / "chatbot.db"
        ).exists()
