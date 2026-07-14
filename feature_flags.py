"""
Feature Flags — single source of truth לבדיקת פיצ'רים פעילים בפריסה.

המודול מספק שכבת abstraction מעל טבלת `subscription` (singleton) ושילוב עם
תבניות החבילות שב-`plans_config.py`. פונקציית הליבה היא `has_feature` —
משתמשת בה גם השכבה Backend (`@require_feature` decorator + בדיקות בתוך
service functions) וגם השכבה Frontend (context processor ב-`admin/app.py`
שמאפשר `{% if has_feature("broadcast") %}` ב-Jinja).

לפי האפיון, אין cache — שאילתת SELECT על שורה אחת זולה. אם נראה איטיות
בפרודקשן, נוסיף cache עם invalidation בנקודות הכתיבה (`set_plan`,
`override_feature`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plans_config import (
    ALL_FEATURES,
    DEFAULT_PLAN,
    PLANS,
    VALID_PLANS,
    classify_plan_change,
    get_plan_definition,
    is_valid_feature,
    is_valid_plan,
)

logger = logging.getLogger(__name__)


# ── קריאה — הסטטוס הנוכחי ─────────────────────────────────────────────────


def _default_subscription_row() -> dict:
    """ברירות מחדל בטוחות — מוחזרות בכל מקרה של כשל DB / טבלה חסרה."""
    return {
        "id": 1,
        "plan": DEFAULT_PLAN,
        "features_json": "{}",
        "plan_started_at": "",
        "plan_ends_at": None,
        "grace_period_days": PLANS[DEFAULT_PLAN]["grace_period_days"],
        "notes": "",
        "updated_at": "",
    }


def get_subscription_row() -> dict:
    """
    שליפת שורת ה-subscription. הפונקציה הזו **לא זורקת חריגות** —
    כל כשל (DB locked, קובץ חסר, טבלה לא קיימת, פגיעה ב-connection)
    מחזיר ברירות מחדל. כך כל cascade של פונקציות שתלויות בה
    (has_feature, is_in_grace_period וכו') בטוחות לקריאה מ-context
    processor של Flask בלי try/except מקיף.

    היצירה הראשונית של השורה קורית במיגרציה ב-`run_migrations`.
    """
    from database import get_connection  # lazy import — שובר circular

    row = None
    try:
        with get_connection() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM subscription WHERE id = 1"
                ).fetchone()
            except Exception:
                # הטבלה עוד לא קיימת — DB טרם עבר migration
                logger.warning(
                    "feature_flags: subscription table missing — falling back to %s",
                    DEFAULT_PLAN,
                )
                row = None
    except Exception:
        # כשל ב-get_connection עצמו (sqlite3.connect, DB locked,
        # קובץ חסר, הרשאות וכו') — לא לקרוס את הקורא, להחזיר ברירות מחדל.
        logger.error(
            "feature_flags: failed to open DB connection — falling back to %s",
            DEFAULT_PLAN,
            exc_info=True,
        )
        row = None

    if not row:
        return _default_subscription_row()
    return dict(row)


def get_current_plan() -> str:
    """החזרת שם החבילה הנוכחית (basic/advanced/premium)."""
    plan = get_subscription_row().get("plan") or DEFAULT_PLAN
    if not is_valid_plan(plan):
        logger.error("feature_flags: invalid plan %r in DB — using default", plan)
        return DEFAULT_PLAN
    return plan


def get_plan_config() -> dict[str, Any]:
    """החזרת ההגדרה המלאה של החבילה הנוכחית מ-`plans_config.PLANS`."""
    return get_plan_definition(get_current_plan())


def _parse_features_json(raw: str | None) -> dict[str, Any]:
    """פענוח features_json בבטחה — מחזיר {} אם השדה ריק או שבור."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("feature_flags: failed to parse features_json: %r", raw)
        return {}
    if not isinstance(parsed, dict):
        logger.error("feature_flags: features_json is not a dict: %r", parsed)
        return {}
    return parsed


def get_feature_overrides() -> dict[str, Any]:
    """
    API ציבורי לקבלת מפת ה-overrides הידניים הפעילים. שווה ל-
    `_parse_features_json(get_subscription_row()["features_json"])` —
    אבל חיצוני (callers ב-admin/app.py לא צריכים לגעת בפנימיים).
    """
    return _parse_features_json(get_subscription_row().get("features_json"))


def has_feature(feature_name: str) -> bool:
    """
    בדיקה האם פיצ'ר פעיל.

    סדר העדיפות:
    1. אם הפיצ'ר לא קיים ב-`ALL_FEATURES` → False + לוג warning.
    2. אם יש override ב-`features_json` (override ידני) → לפי הערך שם.
    3. אחרת → ברירת המחדל מ-`PLANS[plan]["features"]`.

    טיפים:
    - ערך numeric (כמו `scenarios_max=5`) מוחזר כ-True (יש מגבלה > 0).
      להחזרת הערך עצמו השתמש ב-`get_feature_value`.
    - ערך `None` של פיצ'ר מספרי (ללא הגבלה) — מוחזר כ-True.
    """
    if not is_valid_feature(feature_name):
        logger.warning("feature_flags: unknown feature %r — returning False", feature_name)
        return False

    row = get_subscription_row()
    overrides = _parse_features_json(row.get("features_json"))

    if feature_name in overrides:
        value = overrides[feature_name]
    else:
        plan_features = get_plan_definition(row.get("plan") or DEFAULT_PLAN)["features"]
        value = plan_features.get(feature_name, False)

    return _feature_value_is_active(value)


def _feature_value_is_active(value: Any) -> bool:
    """המרת ערך פיצ'ר ל-boolean: True=פעיל, False=כבוי."""
    if isinstance(value, bool):
        return value
    if value is None:
        # None = ללא הגבלה (פעיל)
        return True
    if isinstance(value, (int, float)):
        return value > 0
    return bool(value)


def get_feature_value(feature_name: str, default: Any = None) -> Any:
    """
    החזרת הערך הגולמי של פיצ'ר (לא boolean) — שימושי לפיצ'רים מספריים
    כמו `scenarios_max`. שימוש: `get_feature_value("scenarios_max")` ⇒ 5.
    """
    if not is_valid_feature(feature_name):
        return default

    row = get_subscription_row()
    overrides = _parse_features_json(row.get("features_json"))
    if feature_name in overrides:
        return overrides[feature_name]
    plan_features = get_plan_definition(row.get("plan") or DEFAULT_PLAN)["features"]
    return plan_features.get(feature_name, default)


# ── תקופת חסד ──────────────────────────────────────────────────────────────


def _parse_db_datetime(value: str | None) -> Optional[datetime]:
    """פענוח timestamp מ-DB (פורמט UTC `YYYY-MM-DD HH:MM:SS`) ל-datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.error("feature_flags: failed to parse datetime %r", value)
            return None


def get_grace_period_days() -> int:
    """
    מספר ימי החסד הפעילים. שדה `grace_period_days` ב-`subscription` יכול
    לדרוס את ברירת המחדל של החבילה — אם לא — נופלים על ערך מהחבילה.
    """
    row = get_subscription_row()
    raw = row.get("grace_period_days")
    if raw is not None:
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass
    return int(get_plan_definition(row.get("plan") or DEFAULT_PLAN)["grace_period_days"])


def grace_period_ends_at() -> Optional[datetime]:
    """תאריך סיום תקופת החסד (UTC), או None אם אין `plan_started_at`."""
    started = _parse_db_datetime(get_subscription_row().get("plan_started_at"))
    if not started:
        return None
    return started + timedelta(days=get_grace_period_days())


def is_in_grace_period() -> bool:
    """האם הפריסה עדיין בתקופת חסד."""
    ends = grace_period_ends_at()
    if not ends:
        return False
    return datetime.now(timezone.utc) < ends


def is_grace_ended() -> bool:
    """
    True רק אם החסד אכן התקיים פעם והסתיים. לא מחזיר True במצב שבו
    `plan_started_at` ריק (לקוח שלא הוגדרה לו תקופת חסד עדיין —
    אין מה להציג ל-banner ש"הסתיים").
    """
    ends = grace_period_ends_at()
    if not ends:
        return False
    return datetime.now(timezone.utc) >= ends


def days_remaining_in_grace() -> int:
    """
    כמה ימים שלמים נותרו בתקופת החסד. 0 = הסתיים. שלילי לא מוחזר —
    מקבעים ל-0 כדי שתבניות יוכלו להציג בלי למפות שלילי.
    """
    ends = grace_period_ends_at()
    if not ends:
        return 0
    delta = ends - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    # ceil — אם נותרו 12 שעות, זה עדיין "1 יום"
    return max(0, -(-int(delta.total_seconds()) // 86400))


# ── כתיבה ─────────────────────────────────────────────────────────────────


def _record_history(
    conn,
    previous_plan: str,
    new_plan: str,
    previous_features_json: str,
    new_features_json: str,
    reason: str,
) -> None:
    """כתיבת שורה ל-plan_history — מתועד גם בכל override של פיצ'ר."""
    conn.execute(
        """INSERT INTO plan_history
            (previous_plan, new_plan, previous_features_json, new_features_json, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (previous_plan, new_plan, previous_features_json, new_features_json, reason),
    )


def set_plan(plan: str, reason: str = "") -> dict:
    """
    החלפת חבילה. שדרוג מאפס `plan_started_at`; downgrade שומר על המקורי;
    אותה חבילה (no-op) — לא משנה כלום.

    מחזיר: שורת ה-subscription המעודכנת.
    """
    if not is_valid_plan(plan):
        raise ValueError(f"set_plan: invalid plan {plan!r}")

    from database import get_connection

    with get_connection() as conn:
        # הגנה מפני שורה חסרה (מיגרציה שנכשלה חלקית). יוצרים את ה-row
        # עם ברירות מחדל אם הוא לא קיים, כדי שה-UPDATE שאחר כך לא ייפול
        # על 0 שורות וה-SELECT הבא לא יחזיר None.
        conn.execute("INSERT OR IGNORE INTO subscription (id) VALUES (1)")
        row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        previous_plan = (dict(row).get("plan") if row else None) or DEFAULT_PLAN
        previous_features_json = (dict(row).get("features_json") if row else "") or "{}"

        change = classify_plan_change(previous_plan, plan)
        new_grace = int(PLANS[plan]["grace_period_days"])
        full_reason = (reason or "").strip()
        if full_reason:
            full_reason = f"{change}: {full_reason}"
        else:
            full_reason = change

        if change == "upgrade":
            # שדרוג — מאפס plan_started_at
            conn.execute(
                """UPDATE subscription
                       SET plan = ?, grace_period_days = ?,
                           plan_started_at = datetime('now'),
                           updated_at = datetime('now')
                     WHERE id = 1""",
                (plan, new_grace),
            )
        elif change == "downgrade":
            # downgrade — שומר על plan_started_at הקיים, מעדכן רק plan + grace
            conn.execute(
                """UPDATE subscription
                       SET plan = ?, grace_period_days = ?,
                           updated_at = datetime('now')
                     WHERE id = 1""",
                (plan, new_grace),
            )
        else:
            # same plan — רק מעדכן updated_at כדי שיהיה תיעוד למתי נגעו
            conn.execute(
                "UPDATE subscription SET updated_at = datetime('now') WHERE id = 1"
            )

        # תיעוד בהיסטוריה (גם אם זו אותה חבילה — שומר audit trail)
        new_row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        _record_history(
            conn,
            previous_plan=previous_plan,
            new_plan=plan,
            previous_features_json=previous_features_json,
            new_features_json=(dict(new_row).get("features_json") or "{}"),
            reason=full_reason,
        )
        return dict(new_row)


def override_feature(feature_name: str, value: Any) -> dict:
    """
    דריסה ידנית של פיצ'ר ב-`features_json`. לא משנה את חבילת הבסיס.
    שימושי כדי לתת ללקוח גישה זמנית לפיצ'ר Pro, או לכבות פיצ'ר באופן
    נקודתי ללקוח ספציפי.
    """
    if not is_valid_feature(feature_name):
        raise ValueError(f"override_feature: unknown feature {feature_name!r}")

    from database import get_connection

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        if not row:
            raise RuntimeError("override_feature: subscription row missing")
        current = _parse_features_json(dict(row).get("features_json"))
        previous_features_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
        current[feature_name] = value
        new_features_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """UPDATE subscription
                   SET features_json = ?, updated_at = datetime('now')
                 WHERE id = 1""",
            (new_features_json,),
        )
        plan_now = dict(row).get("plan") or DEFAULT_PLAN
        _record_history(
            conn,
            previous_plan=plan_now,
            new_plan=plan_now,
            previous_features_json=previous_features_json,
            new_features_json=new_features_json,
            reason=f"override_only: {feature_name}={value!r}",
        )
        new_row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        return dict(new_row)


def get_plan_history(limit: int = 50) -> list[dict]:
    """
    שליפת היסטוריית שינויי החבילה (audit). מוחזרת מהחדש לישן.
    אם הטבלה לא קיימת (DB ישן) או יש כשל DB — מחזיר רשימה ריקה
    בלי לזרוק (משמש את מסך המפתח).
    """
    from database import get_connection

    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_history ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.error("feature_flags: failed to load plan_history", exc_info=True)
        return []


def reset_feature_to_plan_default(feature_name: str) -> dict:
    """
    הסרת override — הפיצ'ר חוזר לברירת המחדל של החבילה הנוכחית.
    """
    if not is_valid_feature(feature_name):
        raise ValueError(
            f"reset_feature_to_plan_default: unknown feature {feature_name!r}"
        )

    from database import get_connection

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        if not row:
            raise RuntimeError("reset_feature_to_plan_default: subscription row missing")
        current = _parse_features_json(dict(row).get("features_json"))
        if feature_name not in current:
            return dict(row)
        previous_features_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
        del current[feature_name]
        new_features_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """UPDATE subscription
                   SET features_json = ?, updated_at = datetime('now')
                 WHERE id = 1""",
            (new_features_json,),
        )
        plan_now = dict(row).get("plan") or DEFAULT_PLAN
        _record_history(
            conn,
            previous_plan=plan_now,
            new_plan=plan_now,
            previous_features_json=previous_features_json,
            new_features_json=new_features_json,
            reason=f"reset_to_default: {feature_name}",
        )
        new_row = conn.execute("SELECT * FROM subscription WHERE id = 1").fetchone()
        return dict(new_row)
