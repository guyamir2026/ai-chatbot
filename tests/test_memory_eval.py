"""
טסטים ל-memory/eval/run_eval.py (שלב 5).

מכסה: parsing של confidence bucket, semantic match (offline), scoring
פר-case (TP/FP/FN), אגרגציה (precision/recall/F1), בדיקת רפים, ורנדור
דוח. אין קריאות ל-OpenAI אמיתי — judge מוזרק כפרמטר.

הטסט הסופי מריץ את כל ה-30 cases עם extractor mocked שמחזיר ידנית את
ה-expected של כל case — מאמת שה-pipeline כולו (load → run → score →
aggregate → render) עובד end-to-end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memory.eval import run_eval


# ────────────────────────────────────────────────────────────────────
# Confidence bucket
# ────────────────────────────────────────────────────────────────────


class TestConfidenceBucket:
    def test_parse_standard(self):
        assert run_eval.parse_confidence_bucket("0.85-0.94") == (0.85, 0.94)

    def test_parse_full_range(self):
        assert run_eval.parse_confidence_bucket("0.95-1.00") == (0.95, 1.0)

    def test_in_bucket_inclusive(self):
        assert run_eval.is_confidence_in_bucket(0.85, "0.85-0.94") is True
        assert run_eval.is_confidence_in_bucket(0.94, "0.85-0.94") is True
        assert run_eval.is_confidence_in_bucket(0.90, "0.85-0.94") is True

    def test_out_of_bucket(self):
        assert run_eval.is_confidence_in_bucket(0.84, "0.85-0.94") is False
        assert run_eval.is_confidence_in_bucket(0.95, "0.85-0.94") is False


# ────────────────────────────────────────────────────────────────────
# Match scoring
# ────────────────────────────────────────────────────────────────────


def _always_match(actual: str, expected: str) -> bool:
    return True


def _never_match(actual: str, expected: str) -> bool:
    return False


def _expected_extraction(**overrides) -> dict:
    base = {
        "action": "add",
        "fact_type": "preference",
        "content_semantic": "מעדיפה בקרים",
        "requires_consent": False,
        "confidence_bucket": "0.85-0.94",
    }
    base.update(overrides)
    return base


def _actual_extraction(**overrides) -> dict:
    base = {
        "action": "add",
        "fact_type": "preference",
        "content": "מעדיפה בקרים",
        "requires_consent": False,
        "confidence": 0.9,
        "evidence": "ev",
        "supersedes_id": None,
        "confirms_id": None,
    }
    base.update(overrides)
    return base


class TestMatchSingle:
    def test_perfect_match(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(), _expected_extraction(), _always_match,
        )
        assert ok is True
        assert details["confidence_in_bucket"] is True

    def test_action_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(action="confirm"),
            _expected_extraction(action="add"),
            _always_match,
        )
        assert ok is False
        assert details["reason"] == "action"

    def test_fact_type_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(fact_type="open_issue"),
            _expected_extraction(fact_type="preference"),
            _always_match,
        )
        assert ok is False
        assert details["reason"] == "fact_type"

    def test_content_semantic_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(), _expected_extraction(), _never_match,
        )
        assert ok is False
        assert details["reason"] == "content_semantic"

    def test_requires_consent_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(requires_consent=True),
            _expected_extraction(requires_consent=False),
            _always_match,
        )
        assert ok is False
        assert details["reason"] == "requires_consent"

    def test_confirm_id_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(action="confirm", confirms_id=10),
            _expected_extraction(action="confirm", confirms_id=20),
            _always_match,
        )
        assert ok is False
        assert details["reason"] == "confirms_id"

    def test_supersede_id_mismatch(self):
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(action="supersede", supersedes_id=10),
            _expected_extraction(action="supersede", supersedes_id=99),
            _always_match,
        )
        assert ok is False
        assert details["reason"] == "supersedes_id"

    def test_confidence_out_of_bucket_still_matches(self):
        """match נחשב גם כאשר confidence לא ב-bucket — ה-bucket נמדד רק
        ב-Calibration metric, לא חוסם את ה-match."""
        ok, details = run_eval._match_single_extraction(
            _actual_extraction(confidence=0.5),
            _expected_extraction(confidence_bucket="0.85-0.94"),
            _always_match,
        )
        assert ok is True
        assert details["confidence_in_bucket"] is False


# ────────────────────────────────────────────────────────────────────
# Score case
# ────────────────────────────────────────────────────────────────────


def _case(case_id="t1", category="clear_extraction", expected=None,
          existing_facts=None) -> dict:
    return {
        "id": case_id,
        "category": category,
        "description": "test case",
        "business_id": "salon_tlv",
        "existing_facts": existing_facts or [],
        "conversation": [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        "expected": expected or {"extractions": [], "min_skipped_count": 0},
    }


def _actual_result(extractions=None, skipped=None, **kwargs) -> dict:
    return {
        "extractions": extractions or [],
        "skipped": skipped or [],
        "tokens_used": 100,
        "success": kwargs.get("success", True),
        "error": kwargs.get("error"),
    }


class TestScoreCase:
    def test_perfect_match_no_extraction(self):
        case = _case(category="no_extraction",
                     expected={"extractions": []})
        actual = _actual_result(extractions=[])
        r = run_eval.score_case(case, actual, _always_match)
        assert r.passed is True
        assert not r.false_positives
        assert not r.false_negatives

    def test_false_positive_in_no_extraction(self):
        """no_extraction case שייצר extraction → FP."""
        case = _case(category="no_extraction",
                     expected={"extractions": []})
        actual = _actual_result(extractions=[_actual_extraction()])
        r = run_eval.score_case(case, actual, _always_match)
        assert len(r.false_positives) == 1
        assert not r.passed

    def test_false_negative_when_missing_expected(self):
        case = _case(expected={"extractions": [_expected_extraction()]})
        actual = _actual_result(extractions=[])
        r = run_eval.score_case(case, actual, _always_match)
        assert len(r.false_negatives) == 1
        assert not r.passed

    def test_perfect_match_with_extraction(self):
        case = _case(expected={"extractions": [_expected_extraction()]})
        actual = _actual_result(extractions=[_actual_extraction()])
        r = run_eval.score_case(case, actual, _always_match)
        assert len(r.matches) == 1
        assert r.passed is True
        assert r.confidence_total == 1
        assert r.confidence_correct == 1

    def test_calibration_counts_bucket_correctness(self):
        case = _case(expected={"extractions": [_expected_extraction()]})
        # confidence=0.5 — לא ב-bucket 0.85-0.94
        actual = _actual_result(extractions=[_actual_extraction(confidence=0.5)])
        r = run_eval.score_case(case, actual, _always_match)
        assert len(r.matches) == 1  # match כן נחשב
        assert r.confidence_correct == 0
        assert r.confidence_total == 1

    def test_pii_metrics_only_for_pii_category(self):
        case = _case(
            category="pii_sensitive",
            expected={"extractions": [_expected_extraction(
                requires_consent=True, fact_type="personal_info",
                content_semantic="אלרגית לאגוזים",
            )]},
        )
        actual = _actual_result(extractions=[_actual_extraction(
            requires_consent=True, fact_type="personal_info",
            content="אלרגית לאגוזים",
        )])
        r = run_eval.score_case(case, actual, _always_match)
        assert r.pii_total == 1
        assert r.pii_correct == 1
        assert r.passed is True

    def test_extractor_error_marked(self):
        case = _case()
        actual = _actual_result(success=False, error="API timeout")
        r = run_eval.score_case(case, actual, _always_match)
        assert r.error == "API timeout"
        assert not r.passed


# ────────────────────────────────────────────────────────────────────
# Aggregate
# ────────────────────────────────────────────────────────────────────


class TestAggregate:
    def test_precision_recall_f1(self):
        """4 TP, 1 FP, 1 FN → precision=0.8, recall=0.8, f1=0.8"""
        results = []
        # 4 TP cases
        for i in range(4):
            r = run_eval.CaseResult(
                case_id=f"tp{i}", category="clear_extraction",
                description="", expected={}, actual={},
                matches=[{"actual": {}, "expected": {}, "details": {}}],
            )
            results.append(r)
        # 1 FP
        r = run_eval.CaseResult(
            case_id="fp", category="no_extraction",
            description="", expected={}, actual={"extractions": [{}]},
            false_positives=[{}],
        )
        results.append(r)
        # 1 FN
        r = run_eval.CaseResult(
            case_id="fn", category="clear_extraction",
            description="", expected={}, actual={"extractions": []},
            false_negatives=[{}],
        )
        results.append(r)

        m = run_eval.aggregate_metrics(results)
        assert m["tp"] == 4
        assert m["fp"] == 1
        assert m["fn"] == 1
        assert m["precision"] == pytest.approx(0.8)
        assert m["recall"] == pytest.approx(0.8)
        assert m["f1"] == pytest.approx(0.8)

    def test_fp_rate_in_no_extraction_category(self):
        """2 cases no_extraction, אחד עם FP — fp_rate=0.5."""
        results = [
            run_eval.CaseResult(
                case_id="ne1", category="no_extraction",
                description="", expected={},
                actual={"extractions": [{"x": 1}]},  # FP
                false_positives=[{}],
            ),
            run_eval.CaseResult(
                case_id="ne2", category="no_extraction",
                description="", expected={},
                actual={"extractions": []},  # clean
            ),
        ]
        m = run_eval.aggregate_metrics(results)
        assert m["fp_rate_no_extraction"] == pytest.approx(0.5)


class TestThresholds:
    def test_all_pass(self):
        m = {
            "tp": 10, "fp": 0, "fn": 0,
            "precision": 0.95, "recall": 0.95, "f1": 0.95,
            "fp_rate_no_extraction": 0.0,
            "pii_accuracy": 1.0,
            "confidence_calibration": 0.9,
        }
        ok, fails = run_eval.metrics_pass(m)
        assert ok is True
        assert fails == []

    def test_precision_too_low(self):
        m = {
            "tp": 8, "fp": 4, "fn": 0,
            "precision": 0.67, "recall": 1.0, "f1": 0.80,
            "fp_rate_no_extraction": 0.0,
            "pii_accuracy": 1.0,
            "confidence_calibration": 0.9,
        }
        ok, fails = run_eval.metrics_pass(m)
        assert ok is False
        assert "precision" in fails

    def test_pii_must_be_100(self):
        m = {
            "tp": 10, "fp": 0, "fn": 0,
            "precision": 1.0, "recall": 1.0, "f1": 1.0,
            "fp_rate_no_extraction": 0.0,
            "pii_accuracy": 0.99,  # פספסנו אחד
            "confidence_calibration": 0.9,
        }
        ok, fails = run_eval.metrics_pass(m)
        assert ok is False
        assert "pii_accuracy" in fails


# ────────────────────────────────────────────────────────────────────
# Report rendering
# ────────────────────────────────────────────────────────────────────


class TestReport:
    def test_renders_markdown(self):
        results = [
            run_eval.CaseResult(
                case_id="t1", category="clear_extraction",
                description="test", expected={}, actual={},
                matches=[{"actual": {}, "expected": {}, "details": {}}],
            ),
        ]
        metrics = run_eval.aggregate_metrics(results)
        passed, failed = run_eval.metrics_pass(metrics)
        report = run_eval.render_report(results, metrics, failed)

        assert "# Eval Results" in report
        assert "Per-Category Breakdown" in report
        assert "Failed Cases (0)" in report or "All cases passed" in report
        assert "Precision" in report
        assert "PII Accuracy" in report

    def test_failed_cases_shown_in_report(self):
        case_result = run_eval.CaseResult(
            case_id="bad1", category="no_extraction",
            description="generated extraction unexpectedly",
            expected={"extractions": []},
            actual={"extractions": [{"action": "add", "content": "X"}]},
            false_positives=[{"action": "add", "content": "X"}],
        )
        metrics = run_eval.aggregate_metrics([case_result])
        _, failed = run_eval.metrics_pass(metrics)
        report = run_eval.render_report([case_result], metrics, failed)
        assert "bad1" in report
        assert "False Positives" in report


# ────────────────────────────────────────────────────────────────────
# End-to-end: load eval set + run all cases with mock extractor
# ────────────────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_eval_set_loads(self):
        data = run_eval.load_eval_set()
        assert "cases" in data
        assert "business_profiles" in data
        assert len(data["cases"]) == 30

    def test_build_business_profile_serializes_services(self):
        data = run_eval.load_eval_set()
        profile = run_eval.build_business_profile(data, "salon_tlv")
        assert profile["business_id"] == "salon_tlv"
        assert profile["business_type"] == "מספרה"
        # services_json הוא string (לא list) — כי כך ה-extractor מצפה.
        assert isinstance(profile["services_json"], str)
        parsed = json.loads(profile["services_json"])
        assert isinstance(parsed, list)
        assert any(s["name"] == "תספורת נשים" for s in parsed)

    def test_perfect_run_all_metrics_pass(self):
        """מריץ את כל 30 ה-cases עם extractor mocked שמחזיר בדיוק את
        ה-expected של כל case. כל המטריקות חייבות לעבור — מאמת שאין
        בעיה בלוגיקת ה-scoring/aggregation."""
        eval_set = run_eval.load_eval_set()

        def perfect_extractor(*, user_id, business_id, conversation,
                              business_profile, existing_facts):
            # מוצא את ה-case לפי user_id ("eval_<case_id>")
            target_id = user_id.removeprefix("eval_")
            case = next(c for c in eval_set["cases"] if c["id"] == target_id)
            expected = case.get("expected", {})
            actual_extractions = []
            for exp in expected.get("extractions", []):
                # ממיר expected → actual (content_semantic → content,
                # confidence_bucket → midpoint, וכו').
                bucket = exp.get("confidence_bucket", "0.85-0.94")
                lo, hi = run_eval.parse_confidence_bucket(bucket)
                conf = (lo + hi) / 2
                actual_extractions.append({
                    "action": exp["action"],
                    "fact_type": exp["fact_type"],
                    "content": exp["content_semantic"],
                    "requires_consent": exp["requires_consent"],
                    "confidence": conf,
                    "evidence": "mock evidence",
                    "supersedes_id": exp.get("supersedes_id"),
                    "confirms_id": exp.get("confirms_id"),
                })
            return {
                "extractions": actual_extractions,
                "skipped": [{"proposed_fact": "x", "reason": "y"}] * expected.get("min_skipped_count", 0),
                "tokens_used": 100,
                "success": True,
                "error": None,
            }

        results = run_eval.run_all_cases(
            eval_set, perfect_extractor, _always_match,
        )
        metrics = run_eval.aggregate_metrics(results)
        ok, fails = run_eval.metrics_pass(metrics)
        assert ok is True, f"מטריקות שנכשלו עם extractor מושלם: {fails} — {metrics}"

        # ודא שלא היו extraction errors
        assert all(r.error is None for r in results)

    def test_pessimistic_run_metrics_fail(self):
        """extractor שמחזיר תמיד כלום — recall נמוך, FN גבוה, אבל אין FP."""
        eval_set = run_eval.load_eval_set()

        def empty_extractor(**kwargs):
            return {"extractions": [], "skipped": [],
                    "tokens_used": 0, "success": True, "error": None}

        results = run_eval.run_all_cases(
            eval_set, empty_extractor, _always_match,
        )
        metrics = run_eval.aggregate_metrics(results)
        ok, fails = run_eval.metrics_pass(metrics)
        # recall נמוך כי FN גדול
        assert "recall" in fails or "f1" in fails
        assert ok is False

    def test_limit_and_case_filter(self):
        eval_set = run_eval.load_eval_set()

        def noop_extractor(**kwargs):
            return {"extractions": [], "skipped": [], "tokens_used": 0,
                    "success": True, "error": None}

        # limit=3
        results = run_eval.run_all_cases(eval_set, noop_extractor, _always_match,
                                          limit=3)
        assert len(results) == 3

        # case_filter
        results = run_eval.run_all_cases(
            eval_set, noop_extractor, _always_match,
            case_filter={"no_extract_01", "pii_01"},
        )
        assert len(results) == 2
        ids = {r.case_id for r in results}
        assert ids == {"no_extract_01", "pii_01"}
