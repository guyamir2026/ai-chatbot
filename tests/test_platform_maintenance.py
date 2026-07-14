"""
טסטים ל-platform_maintenance ול-keep-alive של Google Calendar (שלב 2).
"""

from unittest.mock import patch

import pytest

import control_plane as cp
import platform_maintenance as pm


@pytest.fixture
def platform_env(tmp_path):
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        from ai_chatbot import database as db

        db.init_db()
        cp.create_tenant("salon-a", "א")
        yield tmp_path
        cp.invalidate_status_cache()


class TestScheduling:
    def test_first_run_triggers_both(self, platform_env):
        with patch("backup_service.run_backup", return_value={"ok": 1}) as m_bk, \
             patch("google_calendar.refresh_all_tenant_calendars",
                   return_value={"refreshed": 1}) as m_cal:
            ran = pm.run_due_tasks(now_epoch=1_800_000_000.0)
        assert ran["backup"] == {"ok": 1}
        assert ran["calendar_refresh"] == {"refreshed": 1}
        m_bk.assert_called_once()
        m_cal.assert_called_once()

    def test_interval_respected(self, platform_env):
        now = 1_800_000_000.0
        with patch("backup_service.run_backup", return_value={"ok": 1}), \
             patch("google_calendar.refresh_all_tenant_calendars",
                   return_value={"refreshed": 1}):
            pm.run_due_tasks(now_epoch=now)

            # שעה אחרי — אף משימה לא רצה שוב
            with patch("backup_service.run_backup") as m_bk, \
                 patch("google_calendar.refresh_all_tenant_calendars") as m_cal:
                pm.run_due_tasks(now_epoch=now + 3600)
                m_bk.assert_not_called()
                m_cal.assert_not_called()

            # 25 שעות — גיבוי כן (24h), calendar לא (168h)
            with patch("backup_service.run_backup", return_value={}) as m_bk, \
                 patch("google_calendar.refresh_all_tenant_calendars") as m_cal:
                pm.run_due_tasks(now_epoch=now + 25 * 3600)
                m_bk.assert_called_once()
                m_cal.assert_not_called()

            # 8 ימים — גם calendar
            with patch("backup_service.run_backup", return_value={}), \
                 patch("google_calendar.refresh_all_tenant_calendars",
                       return_value={}) as m_cal:
                pm.run_due_tasks(now_epoch=now + 8 * 24 * 3600)
                m_cal.assert_called_once()

    def test_last_run_persisted_across_restart(self, platform_env):
        """ה-last-run נשמר ב-platform_meta — restart לא מריץ שוב מיד."""
        now = 1_800_000_000.0
        with patch("backup_service.run_backup", return_value={}), \
             patch("google_calendar.refresh_all_tenant_calendars", return_value={}):
            pm.run_due_tasks(now_epoch=now)
        # "restart" — ה-meta נקרא מ-DB, לא מזיכרון התהליך
        assert cp.get_platform_meta("last_backup_epoch") == str(now)
        with patch("backup_service.run_backup") as m_bk, \
             patch("google_calendar.refresh_all_tenant_calendars"):
            pm.run_due_tasks(now_epoch=now + 3600)
            m_bk.assert_not_called()

    def test_backup_failure_does_not_block_calendar(self, platform_env):
        with patch("backup_service.run_backup", side_effect=RuntimeError("boom")), \
             patch("google_calendar.refresh_all_tenant_calendars",
                   return_value={"refreshed": 1}) as m_cal:
            ran = pm.run_due_tasks(now_epoch=1_800_000_000.0)
        assert ran["backup"] is None       # נכשל
        assert ran["calendar_refresh"] == {"refreshed": 1}  # רץ בכל זאת
        m_cal.assert_called_once()


class TestCalendarKeepAlive:
    def test_refresh_all_iterates_tenants_and_isolates_failure(self, platform_env):
        cp.create_tenant("salon-b", "ב")
        import google_calendar as gc

        seen = []

        def fake_refresh():
            from tenancy import get_current_tenant

            t = get_current_tenant()
            seen.append(t)
            if t == "salon-a":
                raise RuntimeError("boom")
            return "refreshed"

        with patch.object(gc, "refresh_tenant_calendar_token", side_effect=fake_refresh):
            counts = gc.refresh_all_tenant_calendars()

        assert set(seen) == {"salon-a", "salon-b"}  # שניהם נסרקו
        assert counts["refreshed"] == 1              # ב' הצליח; א' זרק אבל לא עצר

    def test_not_connected_tenant_skipped(self, platform_env):
        import google_calendar as gc
        from tenancy import tenant_context

        # salon-a בלי חיבור Google → 'not_connected'
        with tenant_context("salon-a"):
            assert gc.refresh_tenant_calendar_token() == "not_connected"
