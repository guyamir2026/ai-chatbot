"""
Review dump — דוח קריא לבדיקה אנושית של פלט ה-extractor.

*שונה מ-run_eval.py*: אין judging, אין PASS/FAIL, אין מטריקות. הסקריפט
מריץ את ה-extractor על כל 30 ה-cases ומדפיס לכל אחד:
- השיחה המקורית כדיאלוג קריא (לא JSON)
- מה ה-expected אומר שצריך לחלץ
- מה ה-extractor חילץ בפועל (+ skipped, לעזר)

המטרה: שאדם יעבור ידנית ויחליט בעצמו אם כל extraction תקין — במקום
להסתמך על LLM judge.

שימוש:
    python -m memory.eval.review_dump --output /var/data/eval_review.md
    python -m memory.eval.review_dump --case-id pii_01   # case יחיד

קורא ל-OpenAI אמיתי דרך ה-client הבלעדי (MEMORY_OPENAI_API_KEY).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from memory.eval.run_eval import build_business_profile, load_eval_set, EVAL_SET_PATH

logger = logging.getLogger(__name__)

_ROLE_LABELS = {
    "user": "👤 לקוח",
    "assistant": "🤖 בוט",
    "system": "⚙️ מערכת",
}

_CONSENT_LABEL = {True: "כן (רגיש)", False: "לא"}


def _consent(val) -> str:
    return _CONSENT_LABEL.get(bool(val), "לא")


def render_conversation(conversation: list[dict]) -> str:
    """ממיר רשימת הודעות לדיאלוג קריא."""
    lines = []
    for msg in conversation or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content") or msg.get("message") or ""
        label = _ROLE_LABELS.get(role, role)
        lines.append(f"- **{label}:** {content}")
    return "\n".join(lines) if lines else "_(אין הודעות)_"


def render_expected(expected: dict) -> str:
    """מה ה-eval-set מצפה שיחולץ (content_semantic + מטא)."""
    extractions = expected.get("extractions", [])
    if not extractions:
        note = "_(כלום — אין לחלץ)_"
        min_skipped = expected.get("min_skipped_count")
        if min_skipped:
            note += f"\n_(צפי: לפחות {min_skipped} ב-skipped)_"
        return note

    lines = []
    for e in extractions:
        ids = ""
        if e.get("action") == "confirm":
            ids = f" | confirms_id={e.get('confirms_id')}"
        elif e.get("action") == "supersede":
            ids = f" | supersedes_id={e.get('supersedes_id')}"
        lines.append(
            f"- **{e.get('action')}** | `{e.get('fact_type')}` | "
            f"\"{e.get('content_semantic', '')}\" | "
            f"consent={_consent(e.get('requires_consent'))} | "
            f"bucket={e.get('confidence_bucket', '?')}{ids}"
        )
    return "\n".join(lines)


def render_actual(actual_result: dict) -> str:
    """מה ה-extractor החזיר בפועל."""
    if not actual_result.get("success", True):
        return f"_(שגיאה: {actual_result.get('error')})_"

    extractions = actual_result.get("extractions") or []
    skipped = actual_result.get("skipped") or []

    lines = []
    if not extractions:
        lines.append("_(לא חולצה אף עובדה)_")
    else:
        for e in extractions:
            ids = ""
            if e.get("confirms_id") is not None:
                ids += f" | confirms_id={e.get('confirms_id')}"
            if e.get("supersedes_id") is not None:
                ids += f" | supersedes_id={e.get('supersedes_id')}"
            if e.get("resolves_id") is not None:
                ids += f" | resolves_id={e.get('resolves_id')}"
            # resolve מחזיר content=null — מציגים תווית ברורה. content=null
            # אצל action אחר (schema מתיר) — תווית ניטרלית, לא resolve.
            content = e.get("content")
            if content is None:
                if e.get("action") == "resolve":
                    content_disp = f"(resolve — סגירת open_issue id={e.get('resolves_id')})"
                else:
                    content_disp = "(ללא content)"
            else:
                content_disp = f"\"{content}\""
            lines.append(
                f"- **{e.get('action')}** | `{e.get('fact_type')}` | "
                f"{content_disp} | "
                f"consent={_consent(e.get('requires_consent'))} | "
                f"confidence={e.get('confidence')}{ids}"
            )
            if e.get("evidence"):
                lines.append(f"  - evidence: _{e.get('evidence')}_")

    # skipped — לעזר בלבד (להבין מה המודל שקל ודחה)
    if skipped:
        lines.append("")
        lines.append("  <details><summary>skipped (לעזר)</summary>")
        lines.append("")
        for s in skipped:
            lines.append(f"  - {s.get('proposed_fact', '')} — _{s.get('reason', '')}_")
        lines.append("")
        lines.append("  </details>")

    return "\n".join(lines)


def render_case(case: dict, actual_result: dict) -> str:
    """case שלם — כותרת, שיחה, צפוי, בפועל."""
    cid = case.get("id", "?")
    parts = []
    parts.append(f"## {cid}")
    parts.append("")
    parts.append(f"**קטגוריה:** {case.get('category', '?')} · "
                 f"**עסק:** {case.get('business_id', '?')}")
    parts.append(f"**תיאור:** {case.get('description', '')}")

    existing = case.get("existing_facts") or []
    if existing:
        parts.append("")
        parts.append("**עובדות קיימות (קלט):**")
        for f in existing:
            parts.append(
                f"- id={f.get('id')} | `{f.get('fact_type')}` | "
                f"\"{f.get('content', '')}\""
            )

    parts.append("")
    parts.append("### השיחה")
    parts.append(render_conversation(case.get("conversation", [])))
    parts.append("")
    parts.append("### צפוי לחלץ")
    parts.append(render_expected(case.get("expected", {})))
    parts.append("")
    parts.append("### חולץ בפועל")
    parts.append(render_actual(actual_result))
    parts.append("")
    parts.append("---")
    return "\n".join(parts)


def render_toc(cases: list[dict]) -> str:
    """תוכן עניינים — case_id + תיאור, קישורי anchor."""
    lines = ["## תוכן עניינים", ""]
    for i, case in enumerate(cases, 1):
        cid = case.get("id", "?")
        desc = case.get("description", "")
        cat = case.get("category", "")
        # anchor של GitHub markdown — lowercase, underscores נשמרים.
        lines.append(f"{i}. [{cid}](#{cid}) — _{cat}_ — {desc}")
    return "\n".join(lines)


def build_review(
    eval_set: dict,
    extractor_func: Callable[..., dict],
    case_filter: Optional[set[str]] = None,
) -> str:
    """בונה את הדוח המלא. extractor_func כפרמטר → testable עם mock."""
    cases = eval_set.get("cases", [])
    if case_filter:
        cases = [c for c in cases if c.get("id") in case_filter]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out = [
        "# Eval Review — Customer Memory Extractor",
        "",
        f"_Generated: {ts}_",
        "",
        "דוח לבדיקה ידנית. לכל case: השיחה המקורית, מה ה-eval מצפה לחלץ, "
        "ומה ה-extractor חילץ בפועל. **בלי judging, בלי PASS/FAIL** — "
        "ההכרעה בעיניים שלך.",
        "",
        f"סה\"כ {len(cases)} cases.",
        "",
        render_toc(cases),
        "",
        "---",
        "",
    ]

    for case in cases:
        business_id = case.get("business_id", "default")
        profile = build_business_profile(eval_set, business_id)
        actual = extractor_func(
            user_id=f"review_{case.get('id', '')}",
            business_id=business_id,
            conversation=case.get("conversation", []),
            business_profile=profile,
            existing_facts=case.get("existing_facts", []),
        )
        out.append(render_case(case, actual))
        out.append("")

    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Human-review dump of memory extractor outputs"
    )
    parser.add_argument(
        "--output", type=str, default="/var/data/eval_review.md",
        help="נתיב לכתיבת הדוח (ברירת מחדל: /var/data/eval_review.md)",
    )
    parser.add_argument(
        "--case-id", action="append", default=None,
        help="case ספציפי (אפשר לחזור)",
    )
    parser.add_argument(
        "--eval-set", type=str, default=str(EVAL_SET_PATH),
        help="נתיב ל-eval set JSON",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

    from memory import extractor

    eval_set = load_eval_set(Path(args.eval_set))
    case_filter = set(args.case_id) if args.case_id else None

    print(f"Generating review dump for {len(eval_set['cases'])} cases "
          f"(filter={case_filter})...")

    report = build_review(eval_set, extractor.extract_facts, case_filter)

    Path(args.output).write_text(report, encoding="utf-8")
    print(f"Review written to: {args.output}")

    # תוכן עניינים גם ל-stdout כדי שתראה מיד מה יש
    cases = eval_set["cases"]
    if case_filter:
        cases = [c for c in cases if c.get("id") in case_filter]
    print()
    print(render_toc(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
