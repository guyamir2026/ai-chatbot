"""
טסטים ל-plans_config.py — הגדרת 3 החבילות ועזרים סטטיים.
"""

from plans_config import (
    ALL_FEATURES,
    DEFAULT_PLAN,
    FEATURE_BROADCAST,
    FEATURE_CALENDAR_SYNC,
    FEATURE_FOLLOWUP_24H,
    FEATURE_SCENARIOS_MAX,
    PLAN_ADVANCED,
    PLAN_BASIC,
    PLAN_PREMIUM,
    PLANS,
    VALID_PLANS,
    classify_plan_change,
    get_min_plan_for_feature,
    get_plan_definition,
    is_valid_feature,
    is_valid_plan,
)


class TestPlansStructure:
    def test_three_plans_exist(self):
        assert set(VALID_PLANS) == {PLAN_BASIC, PLAN_ADVANCED, PLAN_PREMIUM}
        assert set(PLANS.keys()) == {PLAN_BASIC, PLAN_ADVANCED, PLAN_PREMIUM}

    def test_each_plan_has_required_keys(self):
        for plan_key, plan in PLANS.items():
            assert "display_name" in plan
            assert "grace_period_days" in plan
            assert "features" in plan
            assert isinstance(plan["grace_period_days"], int)

    def test_all_features_appear_in_every_plan(self):
        # כל פיצ'ר ב-ALL_FEATURES חייב להופיע בברירת המחדל של כל חבילה
        for plan_key, plan in PLANS.items():
            for feature in ALL_FEATURES:
                assert feature in plan["features"], (
                    f"Feature {feature} missing from plan {plan_key}"
                )

    def test_basic_grace_is_15(self):
        assert PLANS[PLAN_BASIC]["grace_period_days"] == 15

    def test_advanced_grace_is_15(self):
        assert PLANS[PLAN_ADVANCED]["grace_period_days"] == 15

    def test_premium_grace_is_30(self):
        assert PLANS[PLAN_PREMIUM]["grace_period_days"] == 30

    def test_plans_do_not_define_channel(self):
        """הערוץ הוא מאפיין פר-tenant (subscription.channel), לא של חבילה.

        אם 'channel' יחזור להגדרת חבילה — הטסט יזכיר שהצימוד בוטל בכוונה
        (הערוץ נקבע בנעילה אוטומטית בחיבור הראשון, ראה feature_flags).
        """
        for plan_key, plan in PLANS.items():
            assert "channel" not in plan, plan_key


class TestFeatureMatrix:
    """בדיקה שמטריצת הפיצ'רים תואמת את הטבלה השיווקית."""

    def test_calendar_in_all_plans(self):
        for plan in (PLAN_BASIC, PLAN_ADVANCED, PLAN_PREMIUM):
            assert PLANS[plan]["features"][FEATURE_CALENDAR_SYNC] is True

    def test_followup_only_in_advanced_and_premium(self):
        assert PLANS[PLAN_BASIC]["features"][FEATURE_FOLLOWUP_24H] is False
        assert PLANS[PLAN_ADVANCED]["features"][FEATURE_FOLLOWUP_24H] is True
        assert PLANS[PLAN_PREMIUM]["features"][FEATURE_FOLLOWUP_24H] is True

    def test_broadcast_only_in_premium(self):
        assert PLANS[PLAN_BASIC]["features"][FEATURE_BROADCAST] is False
        assert PLANS[PLAN_ADVANCED]["features"][FEATURE_BROADCAST] is False
        assert PLANS[PLAN_PREMIUM]["features"][FEATURE_BROADCAST] is True

    def test_landing_page_removed_from_features(self):
        # landing_page הוסר — שירות ידני, לא feature flag
        assert "landing_page" not in ALL_FEATURES
        for plan_key, plan in PLANS.items():
            assert "landing_page" not in plan["features"], (
                f"landing_page נשאר ב-{plan_key}"
            )

    def test_scenarios_max_premium_is_5(self):
        assert PLANS[PLAN_PREMIUM]["features"][FEATURE_SCENARIOS_MAX] == 5

    def test_scenarios_max_basic_advanced_unlimited(self):
        assert PLANS[PLAN_BASIC]["features"][FEATURE_SCENARIOS_MAX] is None
        assert PLANS[PLAN_ADVANCED]["features"][FEATURE_SCENARIOS_MAX] is None


class TestValidators:
    def test_is_valid_plan(self):
        assert is_valid_plan(PLAN_BASIC)
        assert is_valid_plan(PLAN_PREMIUM)
        assert not is_valid_plan("enterprise")
        assert not is_valid_plan("")

    def test_is_valid_feature(self):
        assert is_valid_feature(FEATURE_BROADCAST)
        assert not is_valid_feature("magic_unicorn")

    def test_get_plan_definition_falls_back_on_invalid(self):
        # שם חבילה לא קיים → מחזיר את ברירת המחדל
        assert get_plan_definition("xyz") == PLANS[DEFAULT_PLAN]

    def test_get_plan_definition_returns_correct_plan(self):
        assert get_plan_definition(PLAN_PREMIUM) is PLANS[PLAN_PREMIUM]


class TestClassifyPlanChange:
    def test_upgrade_basic_to_advanced(self):
        assert classify_plan_change(PLAN_BASIC, PLAN_ADVANCED) == "upgrade"

    def test_upgrade_basic_to_premium(self):
        assert classify_plan_change(PLAN_BASIC, PLAN_PREMIUM) == "upgrade"

    def test_upgrade_advanced_to_premium(self):
        assert classify_plan_change(PLAN_ADVANCED, PLAN_PREMIUM) == "upgrade"

    def test_downgrade_premium_to_advanced(self):
        assert classify_plan_change(PLAN_PREMIUM, PLAN_ADVANCED) == "downgrade"

    def test_downgrade_premium_to_basic(self):
        assert classify_plan_change(PLAN_PREMIUM, PLAN_BASIC) == "downgrade"

    def test_downgrade_advanced_to_basic(self):
        assert classify_plan_change(PLAN_ADVANCED, PLAN_BASIC) == "downgrade"

    def test_same_plan(self):
        assert classify_plan_change(PLAN_BASIC, PLAN_BASIC) == "same"
        assert classify_plan_change(PLAN_PREMIUM, PLAN_PREMIUM) == "same"

    def test_invalid_inputs_return_same(self):
        # ערכים לא תקינים — לא שדרוג, לא הורדה
        assert classify_plan_change("invalid", PLAN_BASIC) == "same"
        assert classify_plan_change(PLAN_BASIC, "invalid") == "same"


class TestMinPlanForFeature:
    """
    החבילה המינימלית הנדרשת לשדרוג כדי לקבל גישה לפיצ'ר.

    סמנטיקה: פיצ'ר פעיל בכל החבילות (universal) → מחזיר None
    כי אין מה לשדרג. זה מונע הצגה מטעה של "זמין החל מ-בסיסי"
    לפיצ'רים שתמיד זמינים.
    """

    def test_calendar_sync_universal_returns_none(self):
        # יומן פעיל בכל החבילות → אין minimum plan לשדרוג
        assert get_min_plan_for_feature(FEATURE_CALENDAR_SYNC) is None

    def test_scenarios_max_universal_returns_none(self):
        # None בכל החבילות + 5 ב-premium — כולן "פעילות" → None
        assert get_min_plan_for_feature(FEATURE_SCENARIOS_MAX) is None

    def test_followup_min_is_advanced(self):
        # פולואפ פעיל מ-advanced ומעלה (False ב-basic)
        assert get_min_plan_for_feature(FEATURE_FOLLOWUP_24H) == PLAN_ADVANCED

    def test_broadcast_min_is_premium(self):
        # broadcast רק ב-premium (False ב-basic ו-advanced)
        assert get_min_plan_for_feature(FEATURE_BROADCAST) == PLAN_PREMIUM

    def test_unknown_feature_returns_none(self):
        assert get_min_plan_for_feature("magic_unicorn") is None

    def test_removed_landing_page_returns_none(self):
        # פיצ'ר שהוסר → None (לא בא-ALL_FEATURES)
        assert get_min_plan_for_feature("landing_page") is None
