"""
Validator + DB writer — שלב 4 של מערכת הזיכרון המתמשך.

שלוש פונקציות:
- validate_extraction(): בדיקות לוגיות על פלט יחיד של ה-LLM (action↔ids,
  אורך content, confidence threshold, התאמה ל-existing_facts).
- save_extractions(): שמירה ל-DB לפי action — add/confirm/supersede,
  כולל dedup ל-active וקביעת status מ-confidence + requires_consent.
- run_extraction_for_user(): orchestrator שמחבר הכול — שולף profile +
  existing_facts, קורא ל-extractor, validate, save, ולוג ל-extraction_runs.

CLAUDE.md:
- אסור except Exception: pass — לכל כשל logger.error מפורש.
- בלולאות I/O ארוכות — try/except סביב כל פריט כדי שכשל אחד לא יעצור
  את האחרים (relevant ל-save_extractions עם רשימה ארוכה).

ראה docs/Customer-memory/claude_code_instructions.md (שלב 4).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ai_chatbot import database as db
from ai_chatbot import config as _config
from memory import extractor

logger = logging.getLogger(__name__)

# סף מינימלי ל-confidence — מתחתיו זה skipped מההתחלה.
_MIN_CONFIDENCE = 0.6
# סף ל-status='active' — מתחתיו pending_approval. requires_consent=True
# תמיד דוחף ל-pending_approval ללא קשר ל-confidence.
_ACTIVE_CONFIDENCE_THRESHOLD = 0.85
# cap על אורך content — spec: 15 מילים, עם שוליים של 5.
_MAX_CONTENT_WORDS = 20

# fact_types חוקיים — schema enforces, אבל בודקים שוב defensively.
_VALID_FACT_TYPES = {
    "preference", "personal_info", "relationship", "vocabulary", "open_issue",
}

_VALID_ACTIONS = {"add", "confirm", "supersede", "resolve"}


def validate_extraction(
    ext: dict, existing_facts: list[dict],
) -> tuple[bool, Optional[str]]:
    """בודק פלט יחיד של ה-LLM. מחזיר (True, None) או (False, reason).

    בדיקות:
    1. action חוקי + fact_type/evidence תקינים + confidence >= MIN.
    2. action↔ids consistency:
       - add: confirms/supersedes/resolves_id = null
       - confirm: confirms_id חייב, האחרים null
       - supersede: supersedes_id חייב, האחרים null
       - resolve: resolves_id חייב, content=null, fact_type=open_issue,
         confirms/supersedes null
    3. confirms_id/supersedes_id/resolves_id חייבים להתאים ל-fact קיים.
    4. content (כש-action אינו resolve): לא ריק, עד MAX_WORDS מילים.
    5. confidence >= MIN_CONFIDENCE.
    """
    if not isinstance(ext, dict):
        return False, "extraction אינו dict"

    action = ext.get("action")
    if action not in _VALID_ACTIONS:
        return False, f"action לא חוקי: {action!r}"

    fact_type = ext.get("fact_type")
    if fact_type not in _VALID_FACT_TYPES:
        return False, f"fact_type לא חוקי: {fact_type!r}"

    # evidence + confidence חלים על כל ה-actions (כולל resolve).
    evidence = (ext.get("evidence") or "").strip()
    if not evidence:
        return False, "evidence ריק"

    try:
        confidence = float(ext.get("confidence", 0))
    except (TypeError, ValueError):
        return False, "confidence אינו מספר"
    if confidence < _MIN_CONFIDENCE:
        return False, f"confidence={confidence} מתחת לסף {_MIN_CONFIDENCE}"

    confirms_id = ext.get("confirms_id")
    supersedes_id = ext.get("supersedes_id")
    resolves_id = ext.get("resolves_id")
    existing_by_id = {
        f.get("id"): f for f in (existing_facts or []) if f.get("id") is not None
    }
    existing_ids = set(existing_by_id)

    # ─── resolve — מסלול נפרד (content=null, סגירת open_issue) ───────────
    if action == "resolve":
        if ext.get("content") is not None:
            return False, "resolve חייב content=null"
        if fact_type != "open_issue":
            return False, "resolve מותר רק ל-fact_type=open_issue"
        if resolves_id is None:
            return False, "resolve חייב resolves_id"
        if confirms_id is not None or supersedes_id is not None:
            return False, "resolve חייב confirms_id=null ו-supersedes_id=null"
        if resolves_id not in existing_ids:
            return False, f"resolves_id={resolves_id} לא נמצא ב-existing_facts"
        if existing_by_id[resolves_id].get("fact_type") != "open_issue":
            return False, f"resolves_id={resolves_id} אינו open_issue"
        return True, None

    # ─── fact update (add/confirm/supersede) — content חובה ─────────────
    content = (ext.get("content") or "").strip()
    if not content:
        return False, "content ריק"
    if len(content.split()) > _MAX_CONTENT_WORDS:
        return False, f"content ארוך מ-{_MAX_CONTENT_WORDS} מילים"
    if resolves_id is not None:
        return False, f"{action} חייב resolves_id=null"

    if action == "add":
        if confirms_id is not None or supersedes_id is not None:
            return False, "add חייב confirms_id=null ו-supersedes_id=null"
    elif action == "confirm":
        if confirms_id is None:
            return False, "confirm חייב confirms_id"
        if supersedes_id is not None:
            return False, "confirm חייב supersedes_id=null"
        if confirms_id not in existing_ids:
            return False, f"confirms_id={confirms_id} לא נמצא ב-existing_facts"
    elif action == "supersede":
        if supersedes_id is None:
            return False, "supersede חייב supersedes_id"
        if confirms_id is not None:
            return False, "supersede חייב confirms_id=null"
        if supersedes_id not in existing_ids:
            return False, f"supersedes_id={supersedes_id} לא נמצא ב-existing_facts"

    return True, None


def _determine_status(confidence: float, requires_consent: bool) -> str:
    """טבלת ההחלטה מה-spec:

    | confidence | requires_consent | status           |
    |------------|------------------|------------------|
    | >= 0.85    | False            | active           |
    | >= 0.85    | True             | pending_approval |
    | 0.60-0.84  | any              | pending_approval |
    """
    if confidence >= _ACTIVE_CONFIDENCE_THRESHOLD and not requires_consent:
        return "active"
    return "pending_approval"


def _is_active_dup(
    user_id: str, business_id: str, fact_type: str, content: str,
) -> bool:
    """בדיקת dedup ברמת אפליקציה — האם כבר קיים active fact זהה?

    safety net מעל ה-partial UNIQUE index. עוטף בלבד את active facts;
    pending_approval מותר להכפיל (האדמין ידחה ב-UI).
    """
    actives = db.get_customer_facts(user_id, business_id, status="active")
    for f in actives:
        if f.get("fact_type") == fact_type and (f.get("content") or "").strip() == content.strip():
            return True
    return False


def _empty_save_counts() -> dict:
    """ברירת מחדל של מילון counts. *מקור יחיד של אמת* — גם save_extractions
    וגם error path ב-run_extraction_for_user משתמשים בו, כדי שמפתחות לא
    יחסרו ב-branches מסוימים (קוראים שעושים result["saved"]["X"] בלי
    .get() לא יקבלו KeyError לסירוגין)."""
    return {
        "added": 0, "confirmed": 0, "superseded": 0, "resolved": 0,
        "dedup_skipped": 0, "batch_conflict_skipped": 0, "errors": 0,
    }


def save_extractions(
    extractions: list[dict], user_id: str, business_id: str = "",
) -> dict:
    """שומר extractions ל-DB לפי action. מחזיר counts לפי תוצאה.

    Returns:
        dict עם המפתחות מ-_empty_save_counts() — added/confirmed/superseded/
        dedup_skipped/batch_conflict_skipped/errors.

    Batch conflict protection:
    existing_facts ב-validate_extraction הוא snapshot שנלקח לפני save.
    אם LLM מחזיר שני extractions שמתכוונים לאותו fact קיים (שני supersede
    על אותו id, או confirm+supersede על אותו id), שניהם יעברו ולידציה
    אבל השני יגרום ל-orphan: עוקף את ה-superseded_by_id של הראשון ומשאיר
    fact חדש active בלי קישור נכנס. הפתרון: מעקב פנימי targeted_ids שדוחה
    את השני (CLAUDE.md atomicity-של-linked-field — אסור לדרוס קישורים).
    """
    # ברירת המחדל נפתרת בזמן-ריצה (לא בזמן def) — כלל ה-multi-tenant:
    # ערך שנקבע ב-default arg קופא בזמן ה-import ולא ניתן להחלפה פר-tenant.
    business_id = business_id or _config.BUSINESS_ID
    counts = _empty_save_counts()
    # קבוצה של ה-existing fact ids שכבר היו target של confirm/supersede
    # בלולאה הזו. מונע orphans כשה-LLM מחזיר שני actions על אותו fact.
    targeted_ids: set[int] = set()

    for ext in extractions or []:
        action = ext.get("action")
        # כל פריט עטוף ב-try/except (CLAUDE.md — לולאות I/O ארוכות).
        try:
            confidence = float(ext.get("confidence", 0))
            requires_consent = bool(ext.get("requires_consent"))
            fact_type = ext.get("fact_type")
            content = (ext.get("content") or "").strip()
            evidence = (ext.get("evidence") or "").strip()
            status = _determine_status(confidence, requires_consent)

            if action == "add":
                # dedup רק כש-status='active' — pending_approval מותר להכפיל.
                if status == "active" and _is_active_dup(
                    user_id, business_id, fact_type, content,
                ):
                    counts["dedup_skipped"] += 1
                    logger.info(
                        "validator: dedup דחה add (user=%s, content=%r)",
                        user_id, content[:50],
                    )
                    continue
                db.insert_customer_fact({
                    "user_id": user_id,
                    "business_id": business_id,
                    "fact_type": fact_type,
                    "content": content,
                    "confidence": confidence,
                    "requires_consent": requires_consent,
                    "status": status,
                    "evidence": evidence,
                    "source": "inferred",
                })
                counts["added"] += 1

            elif action == "confirm":
                fact_id = int(ext["confirms_id"])
                # Batch conflict guard — מונע confirm חוזר על אותו fact
                # או confirm על fact שכבר עבר supersede ב-batch זה.
                if fact_id in targeted_ids:
                    counts["batch_conflict_skipped"] += 1
                    logger.warning(
                        "validator: batch conflict — confirm על fact_id=%d "
                        "שכבר טופל ב-batch זה (user=%s)",
                        fact_id, user_id,
                    )
                    continue
                updated = db.update_customer_fact(fact_id, {
                    "last_confirmed_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S",
                    ),
                })
                if updated:
                    targeted_ids.add(fact_id)
                    counts["confirmed"] += 1
                else:
                    logger.warning(
                        "validator: confirm לא מצא fact_id=%d (user=%s)",
                        fact_id, user_id,
                    )
                    counts["errors"] += 1

            elif action == "supersede":
                old_id = int(ext["supersedes_id"])
                # Batch conflict guard — מונע supersede חוזר על אותו fact
                # (הבאג שדווח: השני היה מעדכן superseded_by_id של הישן ודוחס
                # את הקישור לראשון, ויוצר orphan active מהראשון).
                if old_id in targeted_ids:
                    counts["batch_conflict_skipped"] += 1
                    logger.warning(
                        "validator: batch conflict — supersede על fact_id=%d "
                        "שכבר טופל ב-batch זה (user=%s) — מונע orphan",
                        old_id, user_id,
                    )
                    continue
                # קריאה אטומית — INSERT של ה-new + UPDATE של ה-old באותה
                # טרנזקציה. אם UPDATE נכשל אחרי INSERT מוצלח, ה-rollback
                # של ה-connection מבטל את שני השינויים כיחידה אחת
                # (CLAUDE.md — atomicity של linked-field).
                db.supersede_customer_fact(old_id, {
                    "user_id": user_id,
                    "business_id": business_id,
                    "fact_type": fact_type,
                    "content": content,
                    "confidence": confidence,
                    "requires_consent": requires_consent,
                    "status": status,
                    "evidence": evidence,
                    "source": "inferred",
                })
                targeted_ids.add(old_id)
                counts["superseded"] += 1

            elif action == "resolve":
                issue_id = int(ext["resolves_id"])
                # Batch conflict guard — מונע resolve חוזר על אותו fact או
                # resolve על fact שכבר עבר confirm/supersede ב-batch זה.
                if issue_id in targeted_ids:
                    counts["batch_conflict_skipped"] += 1
                    logger.warning(
                        "validator: batch conflict — resolve על fact_id=%d "
                        "שכבר טופל ב-batch זה (user=%s)",
                        issue_id, user_id,
                    )
                    continue
                # סגירת ה-open_issue (status='resolved') — לא יוצר שורה חדשה.
                resolved = db.resolve_customer_fact(issue_id, evidence)
                if resolved:
                    targeted_ids.add(issue_id)
                    counts["resolved"] += 1
                else:
                    logger.warning(
                        "validator: resolve לא מצא fact_id=%d (user=%s)",
                        issue_id, user_id,
                    )
                    counts["errors"] += 1

        except Exception:
            counts["errors"] += 1
            logger.error(
                "validator: כשל בשמירת extraction action=%s user=%s",
                action, user_id, exc_info=True,
            )
            continue

    return counts


def _compute_conversation_boundaries(
    conversation: list[dict],
) -> tuple[Optional[str], Optional[str]]:
    """מחזיר (start, end) כ-UTC strings אם בהודעות יש created_at; אחרת None.

    הודעות מ-DB מגיעות עם created_at; הודעות מ-eval-set / OpenAI לא.
    תיעוד ב-extraction_runs חשוב לדיבוג של ה-segmentation בשלב 6.
    """
    timestamps = []
    for msg in conversation or []:
        if not isinstance(msg, dict):
            continue
        ts = msg.get("created_at")
        if ts:
            timestamps.append(str(ts))
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def run_extraction_for_user(
    user_id: str,
    business_id: str,
    conversation: list[dict],
) -> dict:
    """Orchestrator — מחבר extractor → validator → save → log.

    Args:
        user_id: מזהה משתמש (TEXT, כמו ב-DB).
        business_id: מזהה עסק — בשלב 1 single-tenant ('default').
        conversation: רשימת הודעות בפורמט DB ({role, message, created_at})
            או OpenAI ({role, content}). ה-extractor יודע לטפל בשניהם.

    Returns:
        {
            "run_id": int | None,        # id ב-extraction_runs
            "status": "completed"|"failed",
            "extractions_count": int,    # סה"כ extractions שחולצו ע"י LLM
            "saved": dict,               # counts מ-save_extractions
            "validation_failures": int,
            "skipped_count": int,        # מה-LLM (skipped)
            "tokens_used": int,
            "error": str | None,
        }
    """
    business_profile = db.get_business_profile(business_id)
    # פולים active + pending_approval; rejected/superseded לא רלוונטיים
    # ל-extractor (לא נוצרים confirm/supersede מולם).
    existing_facts = (
        db.get_customer_facts(user_id, business_id, status="active")
        + db.get_customer_facts(user_id, business_id, status="pending_approval")
    )

    convo_start, convo_end = _compute_conversation_boundaries(conversation)

    # קריאה ל-LLM
    result = extractor.extract_facts(
        user_id=user_id,
        business_id=business_id,
        conversation=conversation,
        business_profile=business_profile,
        existing_facts=existing_facts,
    )

    tokens_used = int(result.get("tokens_used", 0))
    skipped_count = len(result.get("skipped") or [])

    if not result.get("success"):
        # כשל LLM — log ל-extraction_runs ויציאה.
        run_id = None
        try:
            run_id = db.log_extraction_run({
                "user_id": user_id,
                "business_id": business_id,
                "conversation_start": convo_start,
                "conversation_end": convo_end,
                "messages_count": len(conversation or []),
                "extractions_count": 0,
                "skipped_count": skipped_count,
                "status": "failed",
                "error_message": (result.get("error") or "")[:500],
                "tokens_used": tokens_used,
            })
        except Exception:
            logger.error("run_extraction: כשל ב-log_extraction_run (failed branch)", exc_info=True)
        return {
            "run_id": run_id,
            "status": "failed",
            "extractions_count": 0,
            # מילון counts אחיד — אותו schema כמו ה-success path, כדי
            # שקוראים שעושים result["saved"]["X"] לא יקבלו KeyError
            # לסירוגין בענף ה-failed.
            "saved": _empty_save_counts(),
            "validation_failures": 0,
            "skipped_count": skipped_count,
            "tokens_used": tokens_used,
            "error": result.get("error"),
        }

    raw_extractions = result.get("extractions") or []
    valid = []
    validation_failures = 0
    for ext in raw_extractions:
        ok, reason = validate_extraction(ext, existing_facts)
        if ok:
            valid.append(ext)
        else:
            validation_failures += 1
            # CLAUDE.md: לא לבלוע כשלים בשקט.
            logger.info(
                "validator: extraction נדחה (user=%s, action=%s, fact_type=%s): %s",
                user_id, ext.get("action"), ext.get("fact_type"), reason,
            )

    saved = save_extractions(valid, user_id, business_id)

    # שלב 6.2 — id-based cursor: MAX(conversations.id) של ההודעות שעובדו.
    # נשמר גם אם ה-LLM החזיר 0 extractions, כדי שהסבב הבא לא יעבד אותן
    # שוב. monotonic ו-atomic — עוקף באג same-second של timestamp.
    max_message_id = None
    try:
        ids = [
            int(m["id"]) for m in (conversation or [])
            if m.get("id") is not None
        ]
        if ids:
            max_message_id = max(ids)
    except Exception:
        logger.exception("run_extraction: max_message_id computation failed")

    run_id = None
    log_succeeded = True
    try:
        run_id = db.log_extraction_run({
            "user_id": user_id,
            "business_id": business_id,
            "conversation_start": convo_start,
            "conversation_end": convo_end,
            "messages_count": len(conversation or []),
            "extractions_count": (
                saved["added"] + saved["confirmed"]
                + saved["superseded"] + saved["resolved"]
            ),
            # skipped_count כולל גם batch_conflict_skipped כדי שהאדמין יראה
            # את התמונה המלאה של מה ש-LLM החזיר אבל לא נשמר.
            "skipped_count": (
                skipped_count + validation_failures
                + saved.get("batch_conflict_skipped", 0)
            ),
            "status": "completed",
            "error_message": "",
            "tokens_used": tokens_used,
            "last_message_id": max_message_id,
        })
    except Exception:
        # שלב 6.2 — באג 1: save_extractions עבר אבל log נכשל. ה-facts
        # ב-DB אבל אין run → ה-cursor לא מתקדם. מחזירים status='failed'
        # כדי שה-scheduler לא יספור כ-extracted. הסבב הבא יעבד את אותן
        # הודעות; dedup ב-save_extractions + UNIQUE partial index ימנעו
        # duplicates. עלות: קריאת LLM אחת מיותרת ב-edge case של DB lock /
        # disk full.
        log_succeeded = False
        logger.error(
            "run_extraction: log_extraction_run failed after save succeeded "
            "(user=%s, max_message_id=%s). Cursor not advanced; next cycle "
            "will reprocess. Dedup prevents duplicate facts.",
            user_id, max_message_id, exc_info=True,
        )

    return {
        "run_id": run_id,
        # שלב 6.2 — אם log נכשל, status='failed' כדי שה-scheduler לא יקדם
        # cursor (לא יספור extracted). ה-facts כן ב-DB; dedup בסבב הבא.
        "status": "completed" if log_succeeded else "failed",
        "extractions_count": len(raw_extractions),
        "saved": saved,
        "validation_failures": validation_failures,
        "skipped_count": skipped_count,
        "tokens_used": tokens_used,
        "error": None if log_succeeded else (
            "log_extraction_run failed after save_extractions succeeded; "
            "cursor not advanced"
        ),
    }
