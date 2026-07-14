"""
Plans Configuration — תבניות 3 חבילות ה-SaaS ורשימת הפיצ'רים האפשריים.

המודול מכיל את ההגדרה הסטטית של החבילות (basic / advanced / premium) ושל
הפיצ'רים שניתנים להפעלה/כיבוי. הערכים כאן הם **ברירות מחדל** של החבילה —
לכל פריסה ניתן לדרוס פיצ'ר ספציפי ידנית דרך `subscription.features_json`
(ראה `feature_flags.py`).

הוספת פיצ'ר חדש בעתיד:
1. להוסיף את שמו ל-`ALL_FEATURES`.
2. להוסיף ערך ב-`features` של כל אחת מ-3 החבילות.
3. לעטוף את נקודת הכניסה לקוד הפיצ'ר ב-`if not has_feature("..."): return`.
4. לעטוף את ה-route המתאים ב-`@require_feature("...")`.
"""

from typing import Any

# שמות החבילות הנתמכות — חייב להתאים ל-CHECK constraint של עמודת `plan`
PLAN_BASIC = "basic"
PLAN_ADVANCED = "advanced"
PLAN_PREMIUM = "premium"

VALID_PLANS = (PLAN_BASIC, PLAN_ADVANCED, PLAN_PREMIUM)

# ── שמות פיצ'רים — single source of truth ─────────────────────────────────
# כל מפתח שמופיע ב-features_json או בשאילתות `has_feature` חייב להופיע כאן.
FEATURE_CALENDAR_SYNC = "calendar_sync"
FEATURE_FOLLOWUP_24H = "followup_24h"
FEATURE_BROADCAST = "broadcast"
FEATURE_SCENARIOS_MAX = "scenarios_max"  # מטא-נתון להצגה (לא נאכף בקוד)
FEATURE_WIDGET = "widget"  # widget להטמעה באתר חיצוני — חבילת "מקצועי" בלבד

# הערה: landing_page הוסר מהמערכת — דפי נחיתה הם שירות ידני שספק
# ה-SaaS מספק (יוצר ידנית ב-DB), לא יכולת UI. עמודת `page_type` ב-
# response_pages נשארה כדי להבחין בין fallback ל-landing אם כן ייווצרו
# ידנית, אבל אין feature flag.

ALL_FEATURES: frozenset[str] = frozenset({
    FEATURE_CALENDAR_SYNC,
    FEATURE_FOLLOWUP_24H,
    FEATURE_BROADCAST,
    FEATURE_SCENARIOS_MAX,
    FEATURE_WIDGET,
})

# ── הגדרת 3 החבילות ──────────────────────────────────────────────────────
# `grace_period_days` — ימי חסד לשינויים ידניים אחרי תחילת/שדרוג חבילה
# `features`        — ברירות מחדל של feature flags
# הערה: הערוץ (telegram/whatsapp) **אינו** מאפיין של חבילה — הוא מאפיין
# פר-tenant שנקבע אוטומטית בחיבור הערוץ הראשון (subscription.channel,
# ראה feature_flags.get_channel/set_channel).
PLANS: dict[str, dict[str, Any]] = {
    PLAN_BASIC: {
        "display_name": "בסיסית",
        "grace_period_days": 15,
        # מודל ה-LLM של החבילה. ריק = ברירת המחדל מ-env (OPENAI_MODEL).
        # שדרוג = חבילה עם מודל חזק יותר. ראה get_llm_model ב-feature_flags.
        "llm_model": "",
        "features": {
            FEATURE_CALENDAR_SYNC: True,
            FEATURE_FOLLOWUP_24H: False,
            FEATURE_BROADCAST: False,
            FEATURE_WIDGET: False,
            FEATURE_SCENARIOS_MAX: None,  # None = ללא הגבלה אכיפה
        },
    },
    PLAN_ADVANCED: {
        "display_name": "מתקדם",
        "grace_period_days": 15,
        "llm_model": "",
        "features": {
            FEATURE_CALENDAR_SYNC: True,
            FEATURE_FOLLOWUP_24H: True,
            FEATURE_BROADCAST: False,
            FEATURE_WIDGET: False,
            FEATURE_SCENARIOS_MAX: None,
        },
    },
    PLAN_PREMIUM: {
        "display_name": "מקצועי",
        "grace_period_days": 30,
        "llm_model": "",
        "features": {
            FEATURE_CALENDAR_SYNC: True,
            FEATURE_FOLLOWUP_24H: True,
            FEATURE_BROADCAST: True,
            FEATURE_WIDGET: True,
            FEATURE_SCENARIOS_MAX: 5,  # שיווקי בלבד — לא נאכף
        },
    },
}

# חבילת ברירת מחדל — מוצר חד-שכבתי: premium (כל הפיצ'רים דלוקים).
# משמש גם כ-fallback כשקריאת ה-DB נכשלת (fail-open: הכל דלוק, עקבי עם
# זריעת ה-migration ל-premium).
DEFAULT_PLAN = PLAN_PREMIUM


def get_plan_definition(plan: str) -> dict[str, Any]:
    """החזרת ההגדרה המלאה של חבילה, או של ברירת המחדל אם הערך לא תקין."""
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


def is_valid_plan(plan: str) -> bool:
    return plan in PLANS


def is_valid_feature(feature_name: str) -> bool:
    return feature_name in ALL_FEATURES


def classify_plan_change(previous_plan: str, new_plan: str) -> str:
    """
    סיווג שינוי חבילה — קובע האם plan_started_at מתאפס.

    - `upgrade`         — שדרוג (basic→advanced/premium, advanced→premium): מאפס
    - `downgrade`       — הורדה: שומר על plan_started_at המקורי
    - `same`            — אותה חבילה (רק override של feature flags)
    """
    rank = {PLAN_BASIC: 0, PLAN_ADVANCED: 1, PLAN_PREMIUM: 2}
    prev = rank.get(previous_plan)
    new = rank.get(new_plan)
    if prev is None or new is None or prev == new:
        return "same"
    return "upgrade" if new > prev else "downgrade"


# סדר עולה של חבילות — basic נמוך יותר מ-advanced נמוך יותר מ-premium.
PLAN_RANK = {PLAN_BASIC: 0, PLAN_ADVANCED: 1, PLAN_PREMIUM: 2}


def _is_feature_active_default(value) -> bool:
    """האם ערך-ברירת-מחדל של פיצ'ר נחשב 'פעיל' (זמין למשתמש)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return True  # None = ללא הגבלה = פעיל
    if isinstance(value, (int, float)):
        return value > 0
    return bool(value)


def get_min_plan_for_feature(feature_name: str) -> str | None:
    """
    מחזיר את שם החבילה המינימלית שמדליקה את הפיצ'ר באופן בלעדי —
    כלומר, החבילה הזולה ביותר שצריך לשדרג אליה כדי לקבל גישה.

    סמנטיקה (מיועד למודאל שדרג ולעמודת "זמין החל מ-" ב-/my-plan):
    - אם הפיצ'ר פעיל ב-**כל** החבילות (universal — למשל calendar_sync,
      או scenarios_max שערכו None במשמעות 'ללא הגבלה') → מחזיר None,
      כי אין באמת "minimum plan" לשדרוג.
    - אם הפיצ'ר לא פעיל באף חבילה → None.
    - אחרת → החבילה הראשונה לפי rank עולה שבה הוא פעיל.
    """
    if feature_name not in ALL_FEATURES:
        return None

    sorted_plans = sorted(PLANS.keys(), key=lambda p: PLAN_RANK.get(p, 99))
    activations = [
        _is_feature_active_default(PLANS[p]["features"].get(feature_name))
        for p in sorted_plans
    ]
    if all(activations) or not any(activations):
        # פעיל בכולן (universal) או בכלל לא פעיל — אין "מינימום" לשדרוג
        return None
    for plan_key, active in zip(sorted_plans, activations):
        if active:
            return plan_key
    return None
