"""
טסטים ל-feature_flags.py — has_feature, set_plan, override_feature, grace period.

DB זמני נטען דרך פטרן ה-fixture של הפרויקט (patch על ai_chatbot.config.DB_PATH).
"""

import importlib
import json
from unittest.mock import patch

import pytest

from plans_config import (
    FEATURE_BROADCAST,
    FEATURE_CALENDAR_SYNC,
    FEATURE_FOLLOWUP_24H,
    FEATURE_SCENARIOS_MAX,
    PLAN_ADVANCED,
    PLAN_BASIC,
    PLAN_PREMIUM,
)


@pytest.fixture
def ff(tmp_path):
    """
    טוען feature_flags עם DB זמני שעבר init_db (כולל המיגרציה של subscription).
    """
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        yield feature_flags


class TestSubscriptionRow:
    def test_default_plan_after_migration(self, ff):
        # מוצר חד-שכבתי — מיגרציה מאתחלת subscription עם plan='premium'
        assert ff.get_current_plan() == PLAN_PREMIUM

    def test_subscription_row_has_required_fields(self, ff):
        row = ff.get_subscription_row()
        assert row["plan"] == PLAN_PREMIUM
        assert row["features_json"] in ("", "{}")  # ברירת מחדל
        assert "plan_started_at" in row
        assert int(row["grace_period_days"]) == 30


class TestHasFeatureBasicPlan:
    @pytest.fixture(autouse=True)
    def _force_basic(self, ff):
        # ברירת המחדל היא premium (מוצר חד-שכבתי); מחלקה זו בודקת
        # התנהגות basic ולכן קובעת אותה מפורשות.
        ff.set_plan(PLAN_BASIC)

    def test_calendar_active_in_basic(self, ff):
        assert ff.has_feature(FEATURE_CALENDAR_SYNC) is True

    def test_followup_inactive_in_basic(self, ff):
        assert ff.has_feature(FEATURE_FOLLOWUP_24H) is False

    def test_broadcast_inactive_in_basic(self, ff):
        assert ff.has_feature(FEATURE_BROADCAST) is False

    def test_unknown_feature_returns_false(self, ff):
        assert ff.has_feature("nonexistent_feature") is False

    def test_removed_landing_page_returns_false(self):
        # landing_page הוסר מהמערכת — לא קיים יותר ב-ALL_FEATURES.
        # has_feature מחזיר False על שמות לא ידועים (עם warning בלוג).
        from feature_flags import has_feature
        assert has_feature("landing_page") is False


class TestSetPlan:
    def test_upgrade_to_premium_activates_all(self, ff):
        ff.set_plan(PLAN_PREMIUM, reason="manual upgrade in tests")
        assert ff.get_current_plan() == PLAN_PREMIUM
        assert ff.has_feature(FEATURE_BROADCAST) is True
        assert ff.has_feature(FEATURE_FOLLOWUP_24H) is True
        # landing_page הוסר — לא נכלל יותר באקטיבציה

    def test_upgrade_resets_plan_started_at(self, ff):
        # מאתר את plan_started_at הראשוני
        before = ff.get_subscription_row()["plan_started_at"]
        # שדרוג צריך לאפס (datetime('now') לפחות לא קטן יותר)
        ff.set_plan(PLAN_ADVANCED, reason="upgrade test")
        after = ff.get_subscription_row()["plan_started_at"]
        # SQLite datetime('now') מחזיר UTC עד שניה — יכול להיות שווה
        assert after >= before

    def test_downgrade_keeps_plan_started_at(self, ff):
        # שדרוג קודם, עדכון plan_started_at "ישן"
        ff.set_plan(PLAN_PREMIUM)
        from database import get_connection
        with get_connection() as conn:
            conn.execute(
                "UPDATE subscription SET plan_started_at = '2020-01-01 00:00:00' WHERE id = 1"
            )
        before = ff.get_subscription_row()["plan_started_at"]
        # downgrade — plan_started_at לא משתנה
        ff.set_plan(PLAN_BASIC, reason="downgrade test")
        after = ff.get_subscription_row()["plan_started_at"]
        assert before == after == "2020-01-01 00:00:00"

    def test_invalid_plan_raises(self, ff):
        with pytest.raises(ValueError):
            ff.set_plan("enterprise")

    def test_grace_period_updates_with_plan(self, ff):
        ff.set_plan(PLAN_PREMIUM)
        assert ff.get_subscription_row()["grace_period_days"] == 30
        ff.set_plan(PLAN_BASIC)
        assert ff.get_subscription_row()["grace_period_days"] == 15

    def test_history_recorded(self, ff):
        # ברירת המחדל premium — קובעים basic כבסיס כדי ש-advanced ייחשב upgrade
        ff.set_plan(PLAN_BASIC)
        ff.set_plan(PLAN_ADVANCED, reason="test")
        from database import get_connection
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_history ORDER BY id DESC"
            ).fetchall()
        assert len(rows) >= 1
        latest = dict(rows[0])
        assert latest["new_plan"] == PLAN_ADVANCED
        assert "upgrade" in latest["reason"]

    def test_set_plan_recreates_row_if_missing(self, ff):
        """
        תרחיש: שורת subscription נמחקה (מיגרציה שנכשלה חלקית, או ניקוי
        ידני). set_plan חייב ליצור אותה מחדש ולא להתרסק עם TypeError.
        """
        from database import get_connection
        with get_connection() as conn:
            conn.execute("DELETE FROM subscription WHERE id = 1")
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM subscription"
            ).fetchone()["c"] == 0
        # set_plan לא אמור לזרוק
        new_row = ff.set_plan(PLAN_PREMIUM, reason="recovery test")
        assert new_row["plan"] == PLAN_PREMIUM
        assert ff.get_current_plan() == PLAN_PREMIUM


class TestGetFeatureOverrides:
    """API ציבורי לקבלת overrides — עוטף את _parse_features_json."""

    def test_empty_when_no_overrides(self, ff):
        assert ff.get_feature_overrides() == {}

    def test_returns_overrides_after_set(self, ff):
        ff.override_feature(FEATURE_BROADCAST, True)
        overrides = ff.get_feature_overrides()
        assert overrides == {FEATURE_BROADCAST: True}


class TestOverrideFeature:
    @pytest.fixture(autouse=True)
    def _force_basic(self, ff):
        # קובעים basic מפורשות — הטסטים כאן מניחים ברירות מחדל של basic
        # (broadcast כבוי), בעוד ברירת המחדל של המערכת היא premium.
        ff.set_plan(PLAN_BASIC)

    def test_override_grants_temporary_feature(self, ff):
        # basic → broadcast=False כברירת מחדל
        assert ff.has_feature(FEATURE_BROADCAST) is False
        ff.override_feature(FEATURE_BROADCAST, True)
        assert ff.has_feature(FEATURE_BROADCAST) is True
        # החבילה עצמה לא השתנתה
        assert ff.get_current_plan() == PLAN_BASIC

    def test_override_denies_default_feature(self, ff):
        # ב-basic, calendar=True כברירת מחדל. נדרוס ל-False
        assert ff.has_feature(FEATURE_CALENDAR_SYNC) is True
        ff.override_feature(FEATURE_CALENDAR_SYNC, False)
        assert ff.has_feature(FEATURE_CALENDAR_SYNC) is False

    def test_unknown_feature_raises(self, ff):
        with pytest.raises(ValueError):
            ff.override_feature("magic_thing", True)

    def test_features_json_persisted(self, ff):
        ff.override_feature(FEATURE_BROADCAST, True)
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT features_json FROM subscription WHERE id = 1"
            ).fetchone()
        parsed = json.loads(row["features_json"])
        assert parsed.get(FEATURE_BROADCAST) is True


class TestResetFeature:
    @pytest.fixture(autouse=True)
    def _force_basic(self, ff):
        # reset מחזיר לברירת המחדל של החבילה — הטסטים בודקים basic (broadcast כבוי).
        ff.set_plan(PLAN_BASIC)

    def test_reset_removes_override(self, ff):
        ff.override_feature(FEATURE_BROADCAST, True)
        assert ff.has_feature(FEATURE_BROADCAST) is True
        ff.reset_feature_to_plan_default(FEATURE_BROADCAST)
        # חזר לברירת המחדל של basic = False
        assert ff.has_feature(FEATURE_BROADCAST) is False

    def test_reset_no_op_when_no_override(self, ff):
        # אם אין override — לא קורה כלום
        ff.reset_feature_to_plan_default(FEATURE_BROADCAST)
        assert ff.has_feature(FEATURE_BROADCAST) is False


class TestGetFeatureValue:
    @pytest.fixture(autouse=True)
    def _force_basic(self, ff):
        # ברירת מחדל basic — test_returns_none_for_unlimited מניח scenarios_max=None
        # (ב-premium הוא 5). טסטים שצריכים premium קובעים אותו מפורשות.
        ff.set_plan(PLAN_BASIC)

    def test_returns_numeric_for_scenarios_max(self, ff):
        ff.set_plan(PLAN_PREMIUM)
        assert ff.get_feature_value(FEATURE_SCENARIOS_MAX) == 5

    def test_returns_none_for_unlimited(self, ff):
        # basic = None (ללא הגבלה)
        assert ff.get_feature_value(FEATURE_SCENARIOS_MAX) is None

    def test_returns_default_for_unknown(self, ff):
        assert ff.get_feature_value("magic", default="x") == "x"


class TestGracePeriod:
    def test_in_grace_after_fresh_migration(self, ff):
        # מיגרציה כותבת plan_started_at=now → אנחנו בחסד.
        # ברירת המחדל premium = 30 ימי חסד.
        assert ff.is_in_grace_period() is True
        assert 1 <= ff.days_remaining_in_grace() <= 30

    def test_grace_ended_old_started_at(self, ff):
        from database import get_connection
        with get_connection() as conn:
            conn.execute(
                "UPDATE subscription SET plan_started_at = '2020-01-01 00:00:00' WHERE id = 1"
            )
        assert ff.is_in_grace_period() is False
        assert ff.days_remaining_in_grace() == 0

    def test_premium_has_30_day_grace(self, ff):
        ff.set_plan(PLAN_PREMIUM)
        # Premium = 30 יום, לכן days_remaining צריך להיות סביב 30
        assert 28 <= ff.days_remaining_in_grace() <= 30


class TestFeatureValueIsActive:
    """בדיקות לפונקציה הפנימית _feature_value_is_active דרך has_feature."""

    def test_numeric_positive_is_active(self, ff):
        ff.override_feature(FEATURE_SCENARIOS_MAX, 5)
        assert ff.has_feature(FEATURE_SCENARIOS_MAX) is True

    def test_numeric_zero_is_inactive(self, ff):
        ff.override_feature(FEATURE_SCENARIOS_MAX, 0)
        assert ff.has_feature(FEATURE_SCENARIOS_MAX) is False

    def test_none_means_unlimited_active(self, ff):
        # ב-basic: scenarios_max=None → has_feature = True (ללא הגבלה)
        assert ff.has_feature(FEATURE_SCENARIOS_MAX) is True


class TestRobustnessOnDbFailure:
    """
    הגנה מפני קריסה בעת כשל DB טרנזיינטי. אסור שכל קריאת page render
    תקרוס בגלל get_connection() שזורק (DB locked, קובץ חסר, וכו').
    """

    def test_get_subscription_row_returns_defaults_when_connection_fails(self, ff, monkeypatch):
        # מדמה כשל ב-sqlite3.connect — get_connection() יזרוק
        def _broken_connection():
            raise OSError("simulated DB connection failure")

        monkeypatch.setattr("database.get_connection", _broken_connection)
        row = ff.get_subscription_row()
        # ברירות מחדל (fail-open ל-premium) — לא חריגה
        assert row["plan"] == PLAN_PREMIUM
        assert row["features_json"] == "{}"

    def test_has_feature_uses_premium_default_when_connection_fails(self, ff, monkeypatch):
        def _broken_connection():
            raise OSError("simulated DB connection failure")

        monkeypatch.setattr("database.get_connection", _broken_connection)
        # כשל DB → fallback ל-premium (fail-open): broadcast ו-calendar דלוקים,
        # לא חריגה. עדיף שפיצ'ר יישאר זמין מאשר שכל render יקרוס.
        assert ff.has_feature(FEATURE_BROADCAST) is True
        assert ff.has_feature(FEATURE_CALENDAR_SYNC) is True

    def test_grace_period_helpers_safe_when_connection_fails(self, ff, monkeypatch):
        def _broken_connection():
            raise OSError("simulated DB connection failure")

        monkeypatch.setattr("database.get_connection", _broken_connection)
        # ברירות מחדל: plan_started_at=='' → אין חסד פעיל
        assert ff.is_in_grace_period() is False
        assert ff.days_remaining_in_grace() == 0

    def test_get_current_plan_safe_when_connection_fails(self, ff, monkeypatch):
        def _broken_connection():
            raise OSError("simulated DB connection failure")

        monkeypatch.setattr("database.get_connection", _broken_connection)
        assert ff.get_current_plan() == PLAN_PREMIUM
