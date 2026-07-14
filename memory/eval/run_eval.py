"""
Eval runner — שלב 5 של מערכת הזיכרון המתמשך.

הסקריפט מריץ את כל ה-cases ב-docs/Customer-memory/Extractor-eval-set.json
מול ה-extractor (memory/extractor.py) — לא דרך run_extraction_for_user
כדי לא לכתוב ל-DB. מצרף מטריקות לפי docs/Customer-memory/scorecard.md
ומחזיר exit code 0 רק כשכל המטריקות עוברות את הרפים.

שימוש:
    python -m memory.eval.run_eval --report eval_results.md
    python -m memory.eval.run_eval --case-id pii_01    # case יחיד
    python -m memory.eval.run_eval --limit 5            # ראשונים בלבד

ה-LLM-judge להשוואה סמנטית של content הוא הדרך היחידה — לפי ה-spec
(scorecard.md). אין offline fallback: judge LLM כושל → match=False
(שמרני, מונע false positives ב-matching).

ראה docs/Customer-memory/scorecard.md לרפים המלאים.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# נתיב ה-eval set מהמסמך שצורף.
EVAL_SET_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs" / "Customer-memory" / "Extractor-eval-set.json"
)

# רפים מ-scorecard.md
THRESHOLDS = {
    "precision": 0.90,
    "recall": 0.70,
    "f1": 0.78,
    "fp_rate_no_extraction": 0.05,   # max
    "pii_accuracy": 1.00,
    "confidence_calibration": 0.80,
}

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# סוגי נתונים
# ────────────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    """תוצאה של case יחיד."""
    case_id: str
    category: str
    description: str
    expected: dict
    actual: dict
    matches: list[dict] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)
    false_negatives: list[dict] = field(default_factory=list)
    confidence_correct: int = 0
    confidence_total: int = 0
    pii_correct: int = 0
    pii_total: int = 0
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        # case "עובר" אם אין FP/FN וכל requires_consent נכון.
        # (מטריקות אגרגטיביות נמדדות אחר כך — זו רק תווית per-case לדוח.)
        return (
            not self.false_positives
            and not self.false_negatives
            and self.pii_correct == self.pii_total
            and self.error is None
        )


# ────────────────────────────────────────────────────────────────────
# טעינה ובניית קלט ל-extractor
# ────────────────────────────────────────────────────────────────────


def load_eval_set(path: Path = EVAL_SET_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_business_profile(
    eval_set: dict, business_id: str,
) -> dict:
    """ממיר business_profile מ-eval-set לפורמט שה-extractor מצפה לו.

    eval-set מחזיק services כ-list; ה-extractor רוצה services_json כ-string
    (כי כך הוא מגיע מה-DB ב-runtime).
    """
    profile = eval_set["business_profiles"].get(business_id, {})
    return {
        "business_id": business_id,
        "business_type": profile.get("business_type", ""),
        "business_name": profile.get("business_name", ""),
        "services_json": json.dumps(profile.get("services", []), ensure_ascii=False),
        "what_matters_for_extraction": profile.get("what_matters_for_extraction", ""),
    }


# ────────────────────────────────────────────────────────────────────
# Confidence bucket parsing
# ────────────────────────────────────────────────────────────────────


def parse_confidence_bucket(bucket: str) -> tuple[float, float]:
    """ממיר "0.85-0.94" → (0.85, 0.94). תומך גם ב-"0.95-1.00"."""
    parts = bucket.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"bucket לא תקין: {bucket!r}")
    lo, hi = float(parts[0]), float(parts[1])
    return lo, hi


def is_confidence_in_bucket(confidence: float, bucket: str) -> bool:
    lo, hi = parse_confidence_bucket(bucket)
    # קצוות סגורים — bucket = [lo, hi]
    return lo <= confidence <= hi


# ────────────────────────────────────────────────────────────────────
# Semantic content match
# ────────────────────────────────────────────────────────────────────


JudgeFunc = Callable[[str, str], bool]


def llm_semantic_match(actual: str, expected: str) -> bool:
    """LLM-judge: שואל את ה-LLM אם שני המשפטים מבטאים אותה משמעות.

    זו הדרך היחידה להשוואת content סמנטית — לפי spec
    (scorecard.md). פרומפט קצוב — תשובת "כן"/"לא" בלבד. temperature=0.

    משתמש ב-MEMORY_JUDGE_MODEL (קבוע gpt-4.1-mini ב-config) ועובד מול
    OpenAI אמיתי דרך client נפרד מ-OPENAI_API_KEY הראשי של הבוט (ראה
    memory/openai_client.py).

    כשל בקריאה → False (שמרני: מונע false positives ב-matching שמסתירים
    בעיות של ה-extractor). שגיאה נרשמת ב-logger.error.
    """
    if not actual or not expected:
        return False
    try:
        from ai_chatbot.config import MEMORY_JUDGE_MODEL
        # client בלעדי ל-memory — נפרד מ-OPENAI_BASE_URL הראשי של הבוט.
        # נוצר בתיקון ב' (memory/openai_client.py). הסיבה: ה-spec דורש
        # gpt-4.1-mini אמיתי של OpenAI, וה-OPENAI_BASE_URL הראשי עלול
        # להיות מכוון ל-Gemini.
        from memory.openai_client import get_memory_openai_client

        client = get_memory_openai_client()
        prompt = (
            "האם שני המשפטים הבאים מבטאים את אותה משמעות עסקית עבור צ'אטבוט?\n"
            f"משפט A: {expected}\n"
            f"משפט B: {actual}\n\n"
            "ענה רק במילה אחת: כן או לא."
        )
        resp = client.chat.completions.create(
            model=MEMORY_JUDGE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        # נסבול תשובות עם נקודה / מילים נוספות
        return "כן" in answer or "yes" in answer
    except Exception:
        logger.error("eval: LLM judge נכשל — מחזיר match=False (שמרני)", exc_info=True)
        return False


# ────────────────────────────────────────────────────────────────────
# Case scoring
# ────────────────────────────────────────────────────────────────────


def _match_single_extraction(
    actual: dict, expected: dict, judge: JudgeFunc,
) -> tuple[bool, dict]:
    """בודק האם actual תואם expected ספציפי. מחזיר (matched, details).

    details כולל מה נכשל — שימושי לדיווח שגיאות.
    """
    details = {}

    # 1. action
    if actual.get("action") != expected.get("action"):
        return False, {"reason": "action", "expected": expected.get("action"),
                       "actual": actual.get("action")}

    # 2. fact_type
    if actual.get("fact_type") != expected.get("fact_type"):
        return False, {"reason": "fact_type", "expected": expected.get("fact_type"),
                       "actual": actual.get("fact_type")}

    # 3. content semantic
    actual_content = (actual.get("content") or "").strip()
    expected_content = (expected.get("content_semantic") or "").strip()
    if not judge(actual_content, expected_content):
        return False, {"reason": "content_semantic",
                       "expected": expected_content, "actual": actual_content}

    # 4. requires_consent
    if bool(actual.get("requires_consent")) != bool(expected.get("requires_consent")):
        return False, {"reason": "requires_consent",
                       "expected": expected.get("requires_consent"),
                       "actual": actual.get("requires_consent")}

    # 5. ids — אם expected מציין confirm/supersede, ה-id חייב להתאים
    if expected.get("action") == "confirm":
        if actual.get("confirms_id") != expected.get("confirms_id"):
            return False, {"reason": "confirms_id",
                           "expected": expected.get("confirms_id"),
                           "actual": actual.get("confirms_id")}
    if expected.get("action") == "supersede":
        if actual.get("supersedes_id") != expected.get("supersedes_id"):
            return False, {"reason": "supersedes_id",
                           "expected": expected.get("supersedes_id"),
                           "actual": actual.get("supersedes_id")}

    # 6. confidence bucket — נספר בנפרד (מטריקת calibration), לא חוסם match
    in_bucket = is_confidence_in_bucket(
        float(actual.get("confidence", 0)),
        expected.get("confidence_bucket", "0.0-1.0"),
    )
    details["confidence_in_bucket"] = in_bucket
    details["actual_confidence"] = actual.get("confidence")

    return True, details


def score_case(case: dict, actual_result: dict, judge: JudgeFunc) -> CaseResult:
    """משווה תוצאה לפועל מול expected של case יחיד.

    אלגוריתם: לכל extraction בפועל, מנסה למצוא expected תואם (greedy
    matching — ראשון שתואם זוכה). כל מי שלא נמצא לו זוג הוא FP/FN.
    """
    expected_block = case.get("expected", {})
    expected_extractions = list(expected_block.get("extractions", []))
    actual_extractions = list(actual_result.get("extractions") or [])

    result = CaseResult(
        case_id=case.get("id", "?"),
        category=case.get("category", "?"),
        description=case.get("description", ""),
        expected=expected_block,
        actual=actual_result,
    )

    # error מ-extractor (success=False) → לא ניתן להעריך
    if not actual_result.get("success", True):
        result.error = actual_result.get("error") or "extraction_failed"

    used_expected: set[int] = set()

    for actual in actual_extractions:
        matched_idx = None
        for i, expected in enumerate(expected_extractions):
            if i in used_expected:
                continue
            ok, details = _match_single_extraction(actual, expected, judge)
            if ok:
                matched_idx = i
                result.matches.append({
                    "actual": actual, "expected": expected, "details": details,
                })
                # מטריקת calibration — נמדדת רק על matches
                result.confidence_total += 1
                if details.get("confidence_in_bucket"):
                    result.confidence_correct += 1
                # מטריקת PII — נמדדת רק על cases של pii_sensitive
                if result.category == "pii_sensitive":
                    result.pii_total += 1
                    if (bool(actual.get("requires_consent"))
                            == bool(expected.get("requires_consent"))):
                        result.pii_correct += 1
                break

        if matched_idx is None:
            result.false_positives.append(actual)
        else:
            used_expected.add(matched_idx)

    # expected שלא נתפסו → FN
    for i, expected in enumerate(expected_extractions):
        if i not in used_expected:
            result.false_negatives.append(expected)

    # מקרים מיוחדים של PII — אם אין match בכלל אבל יש expected עם
    # requires_consent=True, זה חמור (FN על PII) — נספר גם פה.
    if result.category == "pii_sensitive":
        for fn in result.false_negatives:
            if bool(fn.get("requires_consent")):
                result.pii_total += 1
                # FN על PII = כשלון מובהק
                # pii_correct לא גדל כי לא חזרנו עליו.

    return result


# ────────────────────────────────────────────────────────────────────
# Aggregation
# ────────────────────────────────────────────────────────────────────


def aggregate_metrics(results: list[CaseResult]) -> dict:
    """מצרף מטריקות אגרגטיביות לפי scorecard.md."""
    tp = sum(len(r.matches) for r in results)
    fp = sum(len(r.false_positives) for r in results)
    fn = sum(len(r.false_negatives) for r in results)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0 else 0.0
    )

    # FP rate בקטגוריית no_extraction
    no_extract_cases = [r for r in results if r.category == "no_extraction"]
    no_extract_fp = sum(
        1 for r in no_extract_cases
        if (r.actual.get("extractions") or [])
    )
    fp_rate_no_extraction = (
        no_extract_fp / len(no_extract_cases)
        if no_extract_cases else 0.0
    )

    # PII accuracy — מ-cases של pii_sensitive
    pii_correct = sum(r.pii_correct for r in results)
    pii_total = sum(r.pii_total for r in results)
    pii_accuracy = pii_correct / pii_total if pii_total > 0 else 1.0

    # Confidence calibration
    conf_correct = sum(r.confidence_correct for r in results)
    conf_total = sum(r.confidence_total for r in results)
    confidence_calibration = (
        conf_correct / conf_total if conf_total > 0 else 1.0
    )

    metrics = {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fp_rate_no_extraction": fp_rate_no_extraction,
        "pii_accuracy": pii_accuracy,
        "confidence_calibration": confidence_calibration,
    }
    return metrics


def metrics_pass(metrics: dict) -> tuple[bool, list[str]]:
    """בודק מול הרפים. מחזיר (all_passed, list_of_failed_metric_names)."""
    failures = []
    if metrics["precision"] < THRESHOLDS["precision"]:
        failures.append("precision")
    if metrics["recall"] < THRESHOLDS["recall"]:
        failures.append("recall")
    if metrics["f1"] < THRESHOLDS["f1"]:
        failures.append("f1")
    if metrics["fp_rate_no_extraction"] > THRESHOLDS["fp_rate_no_extraction"]:
        failures.append("fp_rate_no_extraction")
    if metrics["pii_accuracy"] < THRESHOLDS["pii_accuracy"]:
        failures.append("pii_accuracy")
    if metrics["confidence_calibration"] < THRESHOLDS["confidence_calibration"]:
        failures.append("confidence_calibration")
    return (not failures, failures)


# ────────────────────────────────────────────────────────────────────
# Report generation
# ────────────────────────────────────────────────────────────────────


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _status_icon(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def render_report(
    results: list[CaseResult], metrics: dict, failed_metrics: list[str],
) -> str:
    """מחזיר דוח Markdown מלא."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = []
    lines.append(f"# Eval Results — Customer Memory Extractor")
    lines.append("")
    lines.append(f"_Generated: {ts}_")
    lines.append("")
    lines.append(f"Total cases: {len(results)}")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Target | Actual | Status |")
    lines.append("|---|---|---|---|")
    rows = [
        ("Precision", f">= {_fmt_pct(THRESHOLDS['precision'])}",
         _fmt_pct(metrics["precision"]),
         "precision" not in failed_metrics),
        ("Recall", f">= {_fmt_pct(THRESHOLDS['recall'])}",
         _fmt_pct(metrics["recall"]),
         "recall" not in failed_metrics),
        ("F1", f">= {_fmt_pct(THRESHOLDS['f1'])}",
         _fmt_pct(metrics["f1"]),
         "f1" not in failed_metrics),
        ("FP Rate (no_extraction)",
         f"<= {_fmt_pct(THRESHOLDS['fp_rate_no_extraction'])}",
         _fmt_pct(metrics["fp_rate_no_extraction"]),
         "fp_rate_no_extraction" not in failed_metrics),
        ("PII Accuracy", f"= {_fmt_pct(THRESHOLDS['pii_accuracy'])}",
         _fmt_pct(metrics["pii_accuracy"]),
         "pii_accuracy" not in failed_metrics),
        ("Confidence Calibration",
         f">= {_fmt_pct(THRESHOLDS['confidence_calibration'])}",
         _fmt_pct(metrics["confidence_calibration"]),
         "confidence_calibration" not in failed_metrics),
    ]
    for name, target, actual, passed in rows:
        lines.append(f"| {name} | {target} | {actual} | {_status_icon(passed)} |")
    lines.append("")
    lines.append(f"**Counts**: TP={metrics['tp']}, FP={metrics['fp']}, FN={metrics['fn']}")
    lines.append("")

    # Per-category breakdown
    lines.append("## Per-Category Breakdown")
    lines.append("")
    categories: dict[str, list[CaseResult]] = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)
    lines.append("| Category | Total | Passed | TP | FP | FN |")
    lines.append("|---|---|---|---|---|---|")
    for cat in sorted(categories.keys()):
        cat_results = categories[cat]
        passed_count = sum(1 for r in cat_results if r.passed)
        cat_tp = sum(len(r.matches) for r in cat_results)
        cat_fp = sum(len(r.false_positives) for r in cat_results)
        cat_fn = sum(len(r.false_negatives) for r in cat_results)
        lines.append(
            f"| {cat} | {len(cat_results)} | {passed_count} | "
            f"{cat_tp} | {cat_fp} | {cat_fn} |"
        )
    lines.append("")

    # Failed cases
    failed_cases = [r for r in results if not r.passed]
    lines.append(f"## Failed Cases ({len(failed_cases)})")
    lines.append("")
    if not failed_cases:
        lines.append("_All cases passed._")
    for r in failed_cases:
        lines.append(f"### {r.case_id} ({r.category}) — {r.description}")
        lines.append("")
        if r.error:
            lines.append(f"**Extractor error:** `{r.error}`")
            lines.append("")
            continue
        if r.false_positives:
            lines.append(f"**False Positives ({len(r.false_positives)}):**")
            lines.append("```json")
            lines.append(json.dumps(r.false_positives, ensure_ascii=False, indent=2))
            lines.append("```")
        if r.false_negatives:
            lines.append(f"**False Negatives ({len(r.false_negatives)}):**")
            lines.append("```json")
            lines.append(json.dumps(r.false_negatives, ensure_ascii=False, indent=2))
            lines.append("```")
        if r.category == "pii_sensitive" and r.pii_total > r.pii_correct:
            lines.append(
                f"**PII mismatch:** {r.pii_correct}/{r.pii_total} correct"
            )
        lines.append("")

    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Main runner
# ────────────────────────────────────────────────────────────────────


def run_all_cases(
    eval_set: dict,
    extractor_func: Callable[..., dict],
    judge: JudgeFunc,
    case_filter: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> list[CaseResult]:
    """מריץ את כל ה-cases (או תת-קבוצה). מחזיר CaseResult לכל אחד.

    extractor_func: שמרים אותו כפרמטר כדי שטסטים יוכלו להזריק mock.
    """
    results: list[CaseResult] = []
    cases = eval_set.get("cases", [])
    if case_filter:
        cases = [c for c in cases if c.get("id") in case_filter]
    if limit:
        cases = cases[:limit]

    for case in cases:
        business_id = case.get("business_id", "default")
        profile = build_business_profile(eval_set, business_id)
        actual = extractor_func(
            user_id=f"eval_{case.get('id', '')}",
            business_id=business_id,
            conversation=case.get("conversation", []),
            business_profile=profile,
            existing_facts=case.get("existing_facts", []),
        )
        results.append(score_case(case, actual, judge))

    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run customer-memory eval set")
    parser.add_argument(
        "--report", type=str, default=None,
        help="נתיב לכתיבת דוח Markdown (ברירת מחדל: stdout בלבד)",
    )
    parser.add_argument(
        "--case-id", action="append", default=None,
        help="הרצה של case ספציפי (אפשר לחזור עם כמה ערכים)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="cap על מספר ה-cases (לדיבוג מהיר)",
    )
    parser.add_argument(
        "--eval-set", type=str, default=str(EVAL_SET_PATH),
        help="נתיב ל-eval set JSON (ברירת מחדל: docs/Customer-memory/...)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

    # ייבוא דחוי כדי שטסטים יוכלו לדרוס לפני ייבוא.
    from memory import extractor

    eval_set = load_eval_set(Path(args.eval_set))

    case_filter = set(args.case_id) if args.case_id else None

    print(f"Running {len(eval_set['cases'])} cases "
          f"(filter={case_filter}, limit={args.limit}, judge=llm)...")

    results = run_all_cases(
        eval_set, extractor.extract_facts, llm_semantic_match,
        case_filter=case_filter, limit=args.limit,
    )
    metrics = aggregate_metrics(results)
    passed_all, failed_metrics = metrics_pass(metrics)
    report = render_report(results, metrics, failed_metrics)

    print(report)

    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"\nReport written to: {args.report}")

    if passed_all:
        print("\nAll metrics PASSED.")
        return 0
    print(f"\nMetrics FAILED: {', '.join(failed_metrics)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
