"""
טסטים ל-memory/context.py (שלב 8 — הזרקת facts ל-context של הבוט).

מכסה: שליפת active facts, סינון vocabulary לפי current_message, cap,
access_count++ , status filtering, מיון יציב. format_facts_block:
פורמט תאריכים, "מידע רגיש", "אומת שוב" רק כשרלוונטי, בלוק ריק.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memory import context


# ────────────────────────────────────────────────────────────────────
# get_relevant_facts_for_context
# ────────────────────────────────────────────────────────────────────


def _seed_fact(db_conn, **overrides):
    """מוסיף שורה ל-customer_facts. מחזיר id."""
    from database import insert_customer_fact
    base = {
        "user_id": "u1", "business_id": "default",
        "fact_type": "preference", "content": "default content",
        "confidence": 0.9, "status": "active",
        "requires_consent": False,
    }
    base.update(overrides)
    return insert_customer_fact(base)


class TestGetRelevantFacts:
    def test_empty_user_returns_empty_list(self, db_conn):
        assert context.get_relevant_facts_for_context("u_nobody", "default") == []

    def test_active_facts_included(self, db_conn):
        _seed_fact(db_conn, content="מעדיפה בקרים", fact_type="preference")
        _seed_fact(db_conn, content="רגישה לאגוזים",
                   fact_type="personal_info", requires_consent=True)
        out = context.get_relevant_facts_for_context("u1", "default")
        contents = {f["content"] for f in out}
        assert contents == {"מעדיפה בקרים", "רגישה לאגוזים"}

    def test_excludes_non_active_statuses(self, db_conn):
        """resolved/superseded/rejected/pending_approval לא נכללים."""
        _seed_fact(db_conn, content="active fact", status="active")
        _seed_fact(db_conn, content="pending fact", status="pending_approval",
                   fact_type="personal_info")
        _seed_fact(db_conn, content="rejected fact", status="rejected",
                   fact_type="vocabulary")
        _seed_fact(db_conn, content="superseded fact", status="superseded",
                   fact_type="relationship")
        _seed_fact(db_conn, content="resolved issue", status="resolved",
                   fact_type="open_issue")
        out = context.get_relevant_facts_for_context("u1", "default")
        assert [f["content"] for f in out] == ["active fact"]

    def test_vocabulary_included_only_on_substring_match(self, db_conn):
        """vocabulary נכלל רק כש-current_message מכיל את ה-content."""
        _seed_fact(db_conn, fact_type="vocabulary",
                   content="'הטיפול הקבוע'")
        # ללא current_message → לא נכלל
        assert context.get_relevant_facts_for_context("u1", "default") == []
        # עם current_message שמכיל → כן נכלל
        out = context.get_relevant_facts_for_context(
            "u1", "default", "אני רוצה שוב את 'הטיפול הקבוע' שלי",
        )
        assert len(out) == 1
        # current_message שלא מכיל → לא נכלל
        assert context.get_relevant_facts_for_context(
            "u1", "default", "שלום, מה שלומך?",
        ) == []

    def test_vocabulary_substring_match_case_insensitive(self, db_conn):
        _seed_fact(db_conn, fact_type="vocabulary", content="The Usual")
        out = context.get_relevant_facts_for_context(
            "u1", "default", "i want THE USUAL please",
        )
        assert len(out) == 1

    def test_caps_at_10_facts(self, db_conn):
        for i in range(15):
            _seed_fact(db_conn, content=f"fact_{i:02d}", confidence=0.9 - i * 0.01)
        out = context.get_relevant_facts_for_context("u1", "default")
        assert len(out) == 10
        # הסדר הוא לפי confidence DESC → 10 הראשונים הם 0..9
        assert [f["content"] for f in out] == [f"fact_{i:02d}" for i in range(10)]

    def test_sort_tiebreaker_by_id_desc(self, db_conn):
        """כש-confidence ו-last_confirmed_at זהים — id DESC (newest first)."""
        id_a = _seed_fact(db_conn, content="A", confidence=0.9)
        id_b = _seed_fact(db_conn, content="B", confidence=0.9)
        out = context.get_relevant_facts_for_context("u1", "default")
        # id_b מאוחר יותר → אמור להופיע ראשון
        assert [f["id"] for f in out] == [id_b, id_a]

    def test_sort_last_confirmed_at_desc_when_confidence_equal(self, db_conn):
        """Regression (cursor Medium): כש-confidence שווה, last_confirmed_at
        צריך להיות DESC — ה-fact עם confirmed_at מאוחר יותר ראשון.
        הבאג: string sort אסצנדינג נתן את הישן ראשון."""
        from database import insert_customer_fact, update_customer_fact
        # שני facts זהים ב-confidence אבל עם last_confirmed_at שונים
        id_old = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "old_confirm", "confidence": 0.9, "status": "active",
        })
        id_new = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "new_confirm", "confidence": 0.9, "status": "active",
        })
        # שינוי ידני של last_confirmed_at — id_new הוא המאוחר
        update_customer_fact(id_old, {"last_confirmed_at": "2025-01-01 10:00:00"})
        update_customer_fact(id_new, {"last_confirmed_at": "2026-05-01 10:00:00"})

        out = context.get_relevant_facts_for_context("u1", "default")
        # id_new (מאוחר ב-last_confirmed_at) חייב להופיע ראשון — DESC
        assert out[0]["content"] == "new_confirm"
        assert out[1]["content"] == "old_confirm"

    def test_access_count_incremented(self, db_conn):
        """כל fact שנשלף — access_count מתעלה ב-1 בקריאה."""
        fid = _seed_fact(db_conn, content="x")
        before = db_conn.execute(
            "SELECT access_count FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()["access_count"]
        context.get_relevant_facts_for_context("u1", "default")
        after = db_conn.execute(
            "SELECT access_count FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()["access_count"]
        assert after == before + 1

    def test_access_count_failure_does_not_crash(self, db_conn):
        """כשל ב-_bump_access_count לא מקריס את get_relevant_facts."""
        _seed_fact(db_conn, content="x")
        with patch.object(
            context, "_bump_access_count", side_effect=RuntimeError("DB locked"),
        ):
            with pytest.raises(RuntimeError):
                # bump עצמו זורק כשמ-mocked — אבל ב-real flow זה try/except
                # פנימי. בודקים שה-real impl תופס את זה:
                context._bump_access_count([1])

        # ה-impl האמיתי תופס פנימית — מאמתים שאין re-raise.
        with patch.object(context.db, "get_connection",
                          side_effect=RuntimeError("locked")):
            # הקריאה אמורה להחזיר ריק (כי גם get_customer_facts ייכשל),
            # אבל אם facts כן מגיעים — _bump_access_count לא יפיל.
            context._bump_access_count([1, 2, 3])  # לא מקריס


class TestFormatFactsBlock:
    """ה-fixture מנטרל staleness לטסטים האלה — הם בודקים פורמט בלבד.
    Staleness נבדק בנפרד ב-TestStalenessFlag עם תאריכים יחסיים ל-now."""

    @pytest.fixture(autouse=True)
    def _disable_staleness(self):
        with patch.object(context, "MEMORY_STALENESS_DAYS", 10_000):
            yield

    def test_empty_returns_none(self):
        assert context.format_facts_block([], "29/05/2026") is None
        assert context.format_facts_block(None, "29/05/2026") is None

    def test_current_date_in_header(self):
        out = context.format_facts_block(
            [{"content": "x", "created_at": None}], "29/05/2026",
        )
        assert "תאריך נוכחי: 29/05/2026" in out

    def test_header_label(self):
        out = context.format_facts_block(
            [{"content": "x", "created_at": None}], "01/01/2026",
        )
        assert "מה שאתה יודע על הלקוח:" in out
        assert "- x" in out

    def test_requires_consent_adds_sensitive_tag_first(self):
        out = context.format_facts_block([{
            "content": "בהריון", "requires_consent": 1,
            "created_at": "2026-03-15 10:00:00",
            "last_confirmed_at": "2026-03-15 10:00:00",
        }], "29/05/2026")
        # "מידע רגיש" מופיע ראשון לפני "נאמר"
        assert "(מידע רגיש, נאמר 15/03/2026)" in out

    def test_no_consent_no_sensitive_tag(self):
        out = context.format_facts_block([{
            "content": "מעדיפה בוקר", "requires_consent": 0,
            "created_at": "2026-02-12 10:00:00",
            "last_confirmed_at": "2026-02-12 10:00:00",
        }], "29/05/2026")
        assert "מידע רגיש" not in out
        assert "(נאמר 12/02/2026)" in out

    def test_il_date_format_dd_mm_yyyy(self):
        out = context.format_facts_block([{
            "content": "x", "created_at": "2026-01-05 14:30:00",
            "last_confirmed_at": "2026-01-05 14:30:00",
        }], "29/05/2026")
        # פורמט: DD/MM/YYYY עם אפסים מובילים
        assert "נאמר 05/01/2026" in out

    def test_confirmed_shown_only_when_diff_more_than_day(self):
        """last_confirmed_at שונה מ-created_at >= יום → 'אומת שוב' מופיע."""
        out = context.format_facts_block([{
            "content": "x", "created_at": "2026-02-12 10:00:00",
            "last_confirmed_at": "2026-04-15 10:00:00",  # 2 חודשים אחרי
        }], "29/05/2026")
        assert "אומת שוב 15/04/2026" in out

    def test_confirmed_hidden_when_same_day(self):
        """כשהשניים זהים (insert טרי) — 'אומת שוב' לא מופיע."""
        out = context.format_facts_block([{
            "content": "x", "created_at": "2026-02-12 10:00:00",
            "last_confirmed_at": "2026-02-12 10:05:00",  # 5 דקות אחרי
        }], "29/05/2026")
        assert "אומת שוב" not in out
        assert "נאמר 12/02/2026" in out

    def test_full_example_format(self):
        """מבחן end-to-end של פורמט הבלוק שביקש המשתמש."""
        out = context.format_facts_block([
            {"content": "בהריון בחודש חמישי", "requires_consent": 1,
             "created_at": "2026-03-15 10:00:00",
             "last_confirmed_at": "2026-03-15 10:00:00"},
            {"content": "מעדיפה תורים בבוקר", "requires_consent": 0,
             "created_at": "2026-02-12 10:00:00",
             "last_confirmed_at": "2026-04-15 10:00:00"},
            {"content": "אלרגית לאגוזים", "requires_consent": 1,
             "created_at": "2026-01-10 10:00:00",
             "last_confirmed_at": "2026-01-10 10:00:00"},
        ], "29/05/2026")
        expected = (
            "תאריך נוכחי: 29/05/2026\n"
            "\n"
            "מה שאתה יודע על הלקוח:\n"
            "- בהריון בחודש חמישי (מידע רגיש, נאמר 15/03/2026)\n"
            "- מעדיפה תורים בבוקר (נאמר 12/02/2026, אומת שוב 15/04/2026)\n"
            "- אלרגית לאגוזים (מידע רגיש, נאמר 10/01/2026)"
        )
        assert out == expected

    def test_missing_dates_handled_gracefully(self):
        """fact בלי created_at — לא נכלל "נאמר" אבל לא קורס."""
        out = context.format_facts_block(
            [{"content": "x", "created_at": None}], "29/05/2026",
        )
        assert "- x" in out
        assert "נאמר" not in out

    def test_skips_empty_content_facts(self):
        """fact עם content ריק — לא מופיע ב-bullet."""
        out = context.format_facts_block(
            [{"content": ""}, {"content": "x"}], "29/05/2026",
        )
        # רק "x" מופיע כשורת bullet
        assert "- x" in out
        lines = [l for l in out.splitlines() if l.startswith("- ")]
        assert lines == ["- x"]


class TestInjectionToggleParsing:
    """Regression (cursor Medium): MEMORY_INJECTION_ENABLED חייב לקבל את
    אותם ערכים כמו שאר ה-toggles בקובץ (true/1/yes — case-insensitive).
    הבאג: == "1" בלבד גרם ל-MEMORY_INJECTION_ENABLED=true (קונבנציה
    שגורה ב-deploy) להשבית בשקט את ההזרקה."""

    def _reload_config(self, monkeypatch, value):
        """דורס את ה-env, מאתחל מחדש את config ומחזיר את הערך."""
        import importlib
        if value is None:
            monkeypatch.delenv("MEMORY_INJECTION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("MEMORY_INJECTION_ENABLED", value)
        import config as cfg
        importlib.reload(cfg)
        return cfg.MEMORY_INJECTION_ENABLED

    @pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "Yes"])
    def test_accepts_truthy_variants(self, monkeypatch, val):
        assert self._reload_config(monkeypatch, val) is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
    def test_accepts_falsy_variants(self, monkeypatch, val):
        assert self._reload_config(monkeypatch, val) is False

    def test_default_enabled(self, monkeypatch):
        """ברירת מחדל = פעיל (כשה-env לא מוגדר)."""
        assert self._reload_config(monkeypatch, None) is True


class TestIsraelTimezone:
    """Regression (cursor Medium): כל התאריכים (header + נאמר/אומת שוב)
    חייבים להיות בשעון ישראל. DB מאחסן UTC, ה-host עלול להיות UTC,
    ובאמת התאריכים בלוגים של admin עוברים tz conversion."""

    def test_now_israel_is_aware_jerusalem(self):
        """now_israel מחזיר datetime aware ב-Asia/Jerusalem."""
        dt = context.now_israel()
        assert dt.tzinfo is not None
        assert str(dt.tzinfo) == "Asia/Jerusalem"

    def test_format_current_date_il_pattern(self):
        """DD/MM/YYYY תקין."""
        import re
        s = context.format_current_date_il()
        assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", s)

    def test_parse_db_dt_converts_utc_to_israel(self):
        """DB ערך 21:30 UTC ב-29/05/2026 = 00:30 בישראל ב-30/05/2026 (קיץ).
        בלי conversion, התאריך היה נשאר 29/05/2026 — באג שגרר tags
        שגויים סביב חצות."""
        # קיץ ישראל: UTC+3 (DST). 21:30 UTC ב-29/05 → 00:30 30/05 בישראל.
        dt = context._parse_db_dt("2026-05-29 21:30:00")
        assert dt is not None
        assert dt.tzinfo is not None
        # בשעון ישראל זה אחרי חצות → היום הבא
        assert dt.strftime("%d/%m/%Y") == "30/05/2026"

    def test_parse_db_dt_winter_utc_plus_2(self):
        """חורף ישראל: UTC+2. 22:30 UTC ב-15/01 → 00:30 16/01 בישראל."""
        dt = context._parse_db_dt("2026-01-15 22:30:00")
        assert dt.strftime("%d/%m/%Y") == "16/01/2026"

    def test_parse_db_dt_invalid_returns_none(self):
        assert context._parse_db_dt(None) is None
        assert context._parse_db_dt("") is None
        assert context._parse_db_dt("garbage") is None


class TestEmptyContentReturnsNone:
    """Regression (cursor Medium): facts עם content ריק/whitespace בלבד —
    התוצאה היא None (אין מה להזריק), לא בלוק עם header בלבד."""

    def test_all_whitespace_content_returns_none(self):
        facts = [{"content": "   "}, {"content": ""}, {"content": None}]
        assert context.format_facts_block(facts, "29/05/2026") is None

    def test_mixed_returns_block_with_only_valid_bullets(self):
        """fact אחד תקין + שניים ריקים → בלוק עם bullet אחד בלבד."""
        out = context.format_facts_block(
            [{"content": "valid", "created_at": None},
             {"content": "  "},
             {"content": ""}],
            "29/05/2026",
        )
        assert out is not None
        assert "- valid" in out
        bullets = [l for l in out.splitlines() if l.startswith("- ")]
        assert bullets == ["- valid"]


class TestStalenessFlag:
    """Staleness flag ("ייתכן שלא רלוונטי") — facts ישנים מעבר ל-
    MEMORY_STALENESS_DAYS מסומנים כדי שהבוט יידע לטפל בזהירות."""

    def _fact(self, *, created=None, confirmed=None, content="x", consent=False):
        return {
            "content": content,
            "requires_consent": 1 if consent else 0,
            "created_at": created,
            "last_confirmed_at": confirmed,
        }

    @staticmethod
    def _utc_str(days_ago: int) -> str:
        """מחזיר UTC timestamp בפורמט DB עבור N ימים אחורה (מעכשיו)."""
        from datetime import datetime, timezone, timedelta
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_stale_fact_shows_relevance_warning(self):
        """fact מעל 90 יום (last_confirmed_at = 100d ago) → "ייתכן שלא רלוונטי"."""
        old = self._utc_str(100)
        out = context.format_facts_block(
            [self._fact(created=old, confirmed=old)],
            "29/05/2026",
        )
        assert "ייתכן שלא רלוונטי" in out

    def test_fresh_fact_no_relevance_warning(self):
        """fact חדש מ-90 יום → אין סימון."""
        recent = self._utc_str(30)
        out = context.format_facts_block(
            [self._fact(created=recent, confirmed=recent)],
            "29/05/2026",
        )
        assert "ייתכן שלא רלוונטי" not in out

    def test_staleness_falls_back_to_created_at(self):
        """בלי last_confirmed_at — ההשוואה מול created_at."""
        old = self._utc_str(100)
        out = context.format_facts_block(
            [self._fact(created=old, confirmed=None)],
            "29/05/2026",
        )
        assert "ייתכן שלא רלוונטי" in out
        # ועדיין יש "נאמר" (כי created_at קיים) ואין "אומת שוב"
        assert "נאמר" in out
        assert "אומת שוב" not in out

    def test_staleness_uses_last_confirmed_not_created(self):
        """fact ישן ב-created_at אבל אומת מחדש לאחרונה → לא stale.
        (השדה הרלוונטי הוא last_confirmed_at, לא created_at.)"""
        old_created = self._utc_str(200)
        recent_confirm = self._utc_str(10)
        out = context.format_facts_block(
            [self._fact(created=old_created, confirmed=recent_confirm)],
            "29/05/2026",
        )
        # לא stale — confirmed לאחרונה
        assert "ייתכן שלא רלוונטי" not in out
        # ויש "אומת שוב" כי ההפרש גדול מיום
        assert "אומת שוב" in out

    def test_staleness_threshold_configurable(self):
        """patch על MEMORY_STALENESS_DAYS ל-10 → fact בן 30 יום מסומן."""
        from unittest.mock import patch
        thirty_days_ago = self._utc_str(30)
        with patch.object(context, "MEMORY_STALENESS_DAYS", 10):
            out = context.format_facts_block(
                [self._fact(created=thirty_days_ago, confirmed=thirty_days_ago)],
                "29/05/2026",
            )
        assert "ייתכן שלא רלוונטי" in out

    def test_no_dates_no_staleness_flag(self):
        """fact בלי created_at ובלי last_confirmed_at — לא מסומן (אין reference)."""
        out = context.format_facts_block(
            [self._fact(created=None, confirmed=None)],
            "29/05/2026",
        )
        assert "ייתכן שלא רלוונטי" not in out

    def test_staleness_tag_order(self):
        """סדר ה-tags: מידע רגיש → נאמר → אומת שוב → ייתכן שלא רלוונטי."""
        old_created = self._utc_str(200)
        old_confirmed = self._utc_str(100)  # 100 יום אחורה, מעל הסף 90
        out = context.format_facts_block(
            [self._fact(
                created=old_created, confirmed=old_confirmed, consent=True,
            )],
            "29/05/2026",
        )
        bullet = [l for l in out.splitlines() if l.startswith("- ")][0]
        # ארבעת ה-tags מופיעים בסדר הצפוי
        idx_sensitive = bullet.find("מידע רגיש")
        idx_said = bullet.find("נאמר")
        idx_confirmed = bullet.find("אומת שוב")
        idx_stale = bullet.find("ייתכן שלא רלוונטי")
        assert idx_sensitive < idx_said < idx_confirmed < idx_stale
