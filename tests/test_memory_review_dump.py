"""
טסטים ל-memory/eval/review_dump.py — הדוח הקריא לבדיקה אנושית.

בודק את פונקציות הרינדור (טהורות, ללא קריאות API). build_review נבדק
עם extractor mocked שמזריק פלט קבוע.
"""

from __future__ import annotations

import pytest

from memory.eval import review_dump


class TestRenderConversation:
    def test_renders_dialogue(self):
        convo = [
            {"role": "user", "content": "מה שעות הפתיחה?"},
            {"role": "assistant", "content": "9:00-20:00"},
        ]
        out = review_dump.render_conversation(convo)
        assert "👤 לקוח" in out
        assert "מה שעות הפתיחה?" in out
        assert "🤖 בוט" in out
        assert "9:00-20:00" in out

    def test_db_message_format(self):
        """תומך גם בפורמט DB ({role, message})."""
        convo = [{"role": "user", "message": "טקסט מ-DB"}]
        out = review_dump.render_conversation(convo)
        assert "טקסט מ-DB" in out

    def test_empty(self):
        assert "אין הודעות" in review_dump.render_conversation([])


class TestRenderExpected:
    def test_no_extraction(self):
        out = review_dump.render_expected({"extractions": []})
        assert "כלום" in out

    def test_no_extraction_with_min_skipped(self):
        out = review_dump.render_expected({"extractions": [], "min_skipped_count": 2})
        assert "2" in out

    def test_single_extraction(self):
        expected = {
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content_semantic": "מעדיפה בקרים",
                "requires_consent": False,
                "confidence_bucket": "0.85-0.94",
            }]
        }
        out = review_dump.render_expected(expected)
        assert "add" in out
        assert "preference" in out
        assert "מעדיפה בקרים" in out
        assert "0.85-0.94" in out
        assert "consent=לא" in out

    def test_pii_shows_consent(self):
        expected = {
            "extractions": [{
                "action": "add", "fact_type": "personal_info",
                "content_semantic": "אלרגית לאגוזים",
                "requires_consent": True,
                "confidence_bucket": "0.85-0.94",
            }]
        }
        out = review_dump.render_expected(expected)
        assert "כן (רגיש)" in out

    def test_confirm_shows_id(self):
        expected = {
            "extractions": [{
                "action": "confirm", "fact_type": "preference",
                "content_semantic": "x", "requires_consent": False,
                "confidence_bucket": "0.85-0.94", "confirms_id": 15,
            }]
        }
        out = review_dump.render_expected(expected)
        assert "confirms_id=15" in out


class TestRenderActual:
    def test_extraction_error(self):
        out = review_dump.render_actual({"success": False, "error": "API down"})
        assert "API down" in out

    def test_no_extractions(self):
        out = review_dump.render_actual({"extractions": [], "skipped": [], "success": True})
        assert "לא חולצה" in out

    def test_extraction_with_evidence(self):
        actual = {
            "success": True,
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": "מעדיפה בקרים", "requires_consent": False,
                "confidence": 0.96, "evidence": "הכי טוב לי בבוקר",
                "supersedes_id": None, "confirms_id": None,
            }],
            "skipped": [],
        }
        out = review_dump.render_actual(actual)
        assert "מעדיפה בקרים" in out
        assert "0.96" in out
        assert "הכי טוב לי בבוקר" in out

    def test_skipped_in_details(self):
        actual = {
            "success": True,
            "extractions": [],
            "skipped": [{"proposed_fact": "פנוי ב-17:30", "reason": "פרט רגעי"}],
        }
        out = review_dump.render_actual(actual)
        assert "skipped" in out
        assert "פנוי ב-17:30" in out
        assert "פרט רגעי" in out

    def test_supersede_shows_id(self):
        actual = {
            "success": True,
            "extractions": [{
                "action": "supersede", "fact_type": "preference",
                "content": "ערבים", "requires_consent": False,
                "confidence": 0.9, "supersedes_id": 21, "confirms_id": None,
            }],
            "skipped": [],
        }
        out = review_dump.render_actual(actual)
        assert "supersedes_id=21" in out

    def test_resolve_shows_id_and_null_content(self):
        """resolve (v2.2) — content=null מוצג כתווית, resolves_id מוצג."""
        actual = {
            "success": True,
            "extractions": [{
                "action": "resolve", "fact_type": "open_issue",
                "content": None, "requires_consent": False,
                "confidence": 0.96, "evidence": "ההחזר הגיע",
                "supersedes_id": None, "confirms_id": None, "resolves_id": 42,
            }],
            "skipped": [],
        }
        out = review_dump.render_actual(actual)
        assert "resolve" in out
        assert "resolves_id=42" in out
        # content=null לא מוצג כריק אלא כתווית סגירה
        assert "סגירת open_issue" in out

    def test_null_content_non_resolve_no_resolve_label(self):
        """content=null אצל action שאינו resolve (schema מתיר) — תווית
        ניטרלית, לא תווית resolve מטעה עם id=None."""
        actual = {
            "success": True,
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": None, "requires_consent": False,
                "confidence": 0.9, "evidence": "ev",
                "supersedes_id": None, "confirms_id": None, "resolves_id": None,
            }],
            "skipped": [],
        }
        out = review_dump.render_actual(actual)
        assert "ללא content" in out
        assert "סגירת open_issue" not in out
        assert "id=None" not in out


class TestRenderCase:
    def test_full_case(self):
        case = {
            "id": "extract_01", "category": "clear_extraction",
            "business_id": "salon_tlv", "description": "העדפת זמן",
            "conversation": [
                {"role": "user", "content": "בוקר נוח לי"},
                {"role": "assistant", "content": "בסדר"},
            ],
            "expected": {"extractions": [{
                "action": "add", "fact_type": "preference",
                "content_semantic": "מעדיפה בוקר", "requires_consent": False,
                "confidence_bucket": "0.85-0.94",
            }]},
        }
        actual = {
            "success": True,
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": "מעדיפה תורים בבוקר", "requires_consent": False,
                "confidence": 0.95, "evidence": "בוקר נוח לי",
                "supersedes_id": None, "confirms_id": None,
            }],
            "skipped": [],
        }
        out = review_dump.render_case(case, actual)
        assert "## extract_01" in out
        assert "clear_extraction" in out
        assert "העדפת זמן" in out
        assert "השיחה" in out
        assert "בוקר נוח לי" in out
        assert "צפוי לחלץ" in out
        assert "מעדיפה בוקר" in out
        assert "חולץ בפועל" in out
        assert "מעדיפה תורים בבוקר" in out

    def test_case_with_existing_facts(self):
        case = {
            "id": "confirm_01", "category": "confirm_supersede",
            "business_id": "salon_tlv", "description": "אישור",
            "existing_facts": [{
                "id": 15, "fact_type": "preference",
                "content": "מעדיפה בוקר",
            }],
            "conversation": [
                {"role": "user", "content": "בוקר תמיד נוח"},
                {"role": "assistant", "content": "מעולה"},
            ],
            "expected": {"extractions": []},
        }
        actual = {"success": True, "extractions": [], "skipped": []}
        out = review_dump.render_case(case, actual)
        assert "עובדות קיימות" in out
        assert "id=15" in out
        assert "מעדיפה בוקר" in out


class TestRenderToc:
    def test_lists_all_cases(self):
        cases = [
            {"id": "a1", "category": "no_extraction", "description": "first"},
            {"id": "b2", "category": "pii_sensitive", "description": "second"},
        ]
        out = review_dump.render_toc(cases)
        assert "תוכן עניינים" in out
        assert "[a1](#a1)" in out
        assert "first" in out
        assert "[b2](#b2)" in out
        assert "second" in out


class TestBuildReview:
    def test_end_to_end_with_mock(self):
        eval_set = review_dump.load_eval_set()

        def stub_extractor(*, user_id, business_id, conversation,
                           business_profile, existing_facts):
            return {
                "extractions": [{
                    "action": "add", "fact_type": "preference",
                    "content": "פלט בדיקה", "requires_consent": False,
                    "confidence": 0.9, "evidence": "ev",
                    "supersedes_id": None, "confirms_id": None,
                }],
                "skipped": [], "tokens_used": 10,
                "success": True, "error": None,
            }

        report = review_dump.build_review(eval_set, stub_extractor)
        # כותרת + TOC + כל 30 ה-cases
        assert "# Eval Review" in report
        assert "תוכן עניינים" in report
        assert report.count("## ") >= 30  # לפחות 30 כותרות case (+ TOC)
        assert "פלט בדיקה" in report

    def test_case_filter(self):
        eval_set = review_dump.load_eval_set()

        def stub(*args, **kwargs):
            return {"extractions": [], "skipped": [], "success": True, "error": None}

        report = review_dump.build_review(eval_set, stub, case_filter={"pii_01"})
        assert "## pii_01" in report
        # case אחר לא אמור להופיע ככותרת
        assert "## no_extract_01" not in report
