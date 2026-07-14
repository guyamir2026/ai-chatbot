"""
טסטים ל-memory/validator.py (שלב 4 של מערכת הזיכרון).

שלוש קבוצות:
- validate_extraction: כל הכללים מה-spec (action↔ids, אורך content,
  confidence, מציאות confirms_id/supersedes_id ב-existing).
- save_extractions: add/confirm/supersede, dedup, status decision table,
  עמידות מול I/O errors.
- run_extraction_for_user: orchestrator — מחבר הכל ולוג ל-extraction_runs.
  משתמש ב-mock על memory.extractor.extract_facts ולא קורא ל-API אמיתי.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from memory import validator


# ────────────────────────────────────────────────────────────────────
# validate_extraction
# ────────────────────────────────────────────────────────────────────


def _ext(**overrides):
    """ברירות מחדל ל-extraction תקין."""
    base = {
        "action": "add",
        "fact_type": "preference",
        "content": "מעדיפה תורים בשעות הבוקר",
        "confidence": 0.92,
        "requires_consent": False,
        "evidence": "הכי טוב לי בבוקר",
        "supersedes_id": None,
        "confirms_id": None,
    }
    base.update(overrides)
    return base


class TestValidate:
    def test_valid_add(self):
        ok, reason = validator.validate_extraction(_ext(), [])
        assert ok is True
        assert reason is None

    def test_rejects_empty_content(self):
        ok, reason = validator.validate_extraction(_ext(content="   "), [])
        assert ok is False
        assert "content" in reason

    def test_rejects_empty_evidence(self):
        ok, reason = validator.validate_extraction(_ext(evidence=""), [])
        assert ok is False
        assert "evidence" in reason

    def test_rejects_content_over_20_words(self):
        long = " ".join(["מילה"] * 25)
        ok, reason = validator.validate_extraction(_ext(content=long), [])
        assert ok is False
        assert "20" in reason or "ארוך" in reason

    def test_rejects_low_confidence(self):
        ok, reason = validator.validate_extraction(_ext(confidence=0.5), [])
        assert ok is False
        assert "0.5" in reason or "confidence" in reason

    def test_rejects_confidence_not_numeric(self):
        ok, reason = validator.validate_extraction(_ext(confidence="high"), [])
        assert ok is False

    def test_rejects_invalid_action(self):
        ok, reason = validator.validate_extraction(_ext(action="delete"), [])
        assert ok is False
        assert "action" in reason

    def test_rejects_invalid_fact_type(self):
        ok, reason = validator.validate_extraction(_ext(fact_type="bogus"), [])
        assert ok is False
        assert "fact_type" in reason

    def test_rejects_add_with_confirms_id(self):
        ok, reason = validator.validate_extraction(_ext(confirms_id=5), [{"id": 5}])
        assert ok is False
        assert "add" in reason

    def test_rejects_add_with_supersedes_id(self):
        ok, reason = validator.validate_extraction(_ext(supersedes_id=5), [{"id": 5}])
        assert ok is False
        assert "add" in reason

    def test_valid_confirm(self):
        ok, reason = validator.validate_extraction(
            _ext(action="confirm", confirms_id=12),
            [{"id": 12, "fact_type": "preference"}],
        )
        assert ok is True

    def test_rejects_confirm_without_id(self):
        ok, reason = validator.validate_extraction(
            _ext(action="confirm", confirms_id=None), [],
        )
        assert ok is False

    def test_rejects_confirm_with_supersedes_id(self):
        ok, reason = validator.validate_extraction(
            _ext(action="confirm", confirms_id=12, supersedes_id=15),
            [{"id": 12}, {"id": 15}],
        )
        assert ok is False
        assert "supersedes_id=null" in reason

    def test_rejects_confirm_unknown_id(self):
        ok, reason = validator.validate_extraction(
            _ext(action="confirm", confirms_id=999),
            [{"id": 12}],
        )
        assert ok is False
        assert "999" in reason

    def test_valid_supersede(self):
        ok, reason = validator.validate_extraction(
            _ext(action="supersede", supersedes_id=21, content="ערבים"),
            [{"id": 21, "fact_type": "preference"}],
        )
        assert ok is True

    def test_rejects_supersede_without_id(self):
        ok, reason = validator.validate_extraction(
            _ext(action="supersede", supersedes_id=None), [],
        )
        assert ok is False

    def test_rejects_supersede_unknown_id(self):
        ok, reason = validator.validate_extraction(
            _ext(action="supersede", supersedes_id=99),
            [{"id": 12}],
        )
        assert ok is False

    def test_rejects_non_dict(self):
        ok, reason = validator.validate_extraction("not a dict", [])
        assert ok is False


# ────────────────────────────────────────────────────────────────────
# _determine_status
# ────────────────────────────────────────────────────────────────────


class TestStatusDecisionTable:
    def test_high_confidence_no_consent_active(self):
        assert validator._determine_status(0.9, False) == "active"
        assert validator._determine_status(0.85, False) == "active"
        assert validator._determine_status(1.0, False) == "active"

    def test_high_confidence_with_consent_pending(self):
        assert validator._determine_status(0.9, True) == "pending_approval"
        assert validator._determine_status(0.99, True) == "pending_approval"

    def test_mid_confidence_pending_when_auto_approve_off(self):
        # ברירת מחדל: auto_approve=False → בטחון בינוני נשאר pending.
        assert validator._determine_status(0.84, False) == "pending_approval"
        assert validator._determine_status(0.7, True) == "pending_approval"
        assert validator._determine_status(0.6, False) == "pending_approval"

    def test_mid_confidence_active_when_auto_approve_on(self):
        # auto_approve=True → בטחון בינוני לא-רגיש עובר ל-active מיד.
        assert validator._determine_status(0.84, False, True) == "active"
        assert validator._determine_status(0.6, False, True) == "active"

    def test_sensitive_always_pending_even_with_auto_approve(self):
        # שער הפרטיות אינו נעקף: requires_consent=True נשאר pending גם
        # כש-auto_approve דלוק, בכל רמת בטחון.
        assert validator._determine_status(0.99, True, True) == "pending_approval"
        assert validator._determine_status(0.7, True, True) == "pending_approval"
        assert validator._determine_status(0.6, True, True) == "pending_approval"

    def test_high_confidence_active_regardless_of_auto_approve(self):
        # בטחון גבוה לא-רגיש כבר active בלי קשר ל-auto_approve.
        assert validator._determine_status(0.9, False, False) == "active"
        assert validator._determine_status(0.9, False, True) == "active"


# ────────────────────────────────────────────────────────────────────
# save_extractions
# ────────────────────────────────────────────────────────────────────


class TestSaveExtractions:
    def test_add_single_active(self, db_conn):
        from database import get_customer_facts

        counts = validator.save_extractions(
            [_ext(content="מעדיפה בקרים", confidence=0.95)],
            user_id="u1", business_id="default",
        )
        assert counts["added"] == 1
        facts = get_customer_facts("u1", status="active")
        assert len(facts) == 1
        assert facts[0]["status"] == "active"

    def test_add_requires_consent_pending(self, db_conn):
        from database import get_customer_facts

        validator.save_extractions(
            [_ext(content="רגישה לאגוזים", confidence=0.9,
                  requires_consent=True, fact_type="personal_info")],
            user_id="u1", business_id="default",
        )
        active = get_customer_facts("u1", status="active")
        pending = get_customer_facts("u1", status="pending_approval")
        assert len(active) == 0
        assert len(pending) == 1
        assert pending[0]["requires_consent"] == 1

    def test_add_mid_confidence_pending(self, db_conn):
        from database import get_customer_facts

        validator.save_extractions(
            [_ext(confidence=0.72)],
            user_id="u1", business_id="default",
        )
        assert len(get_customer_facts("u1", status="active")) == 0
        assert len(get_customer_facts("u1", status="pending_approval")) == 1

    def test_dedup_skipped_for_existing_active(self, db_conn):
        """add עם content זהה ל-active קיים → dedup_skipped, לא נכנס ל-DB."""
        from database import get_customer_facts, insert_customer_fact

        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה בקרים", "confidence": 0.9, "status": "active",
        })
        counts = validator.save_extractions(
            [_ext(content="מעדיפה בקרים", confidence=0.95)],
            user_id="u1", business_id="default",
        )
        assert counts["dedup_skipped"] == 1
        assert counts["added"] == 0
        assert len(get_customer_facts("u1", status="active")) == 1

    def test_dedup_does_not_block_pending(self, db_conn):
        """pending_approval מותר להכפיל — האדמין ידחה ב-UI."""
        from database import get_customer_facts

        validator.save_extractions(
            [_ext(confidence=0.7)],
            user_id="u1", business_id="default",
        )
        counts = validator.save_extractions(
            [_ext(confidence=0.7)],
            user_id="u1", business_id="default",
        )
        assert counts["added"] == 1
        assert counts["dedup_skipped"] == 0
        assert len(get_customer_facts("u1", status="pending_approval")) == 2

    def test_confirm_updates_last_confirmed_at(self, db_conn):
        """confirm לא יוצר שורה חדשה, רק מעדכן last_confirmed_at של הקיים."""
        from database import get_customer_facts, insert_customer_fact, update_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה בקרים", "confidence": 0.92, "status": "active",
        })
        # מגדירים תאריך עתיק כדי שה-confirm יוכיח שהוא דרס אותו.
        # insert_customer_fact משתמש בברירת המחדל (now); ה-update הזה הוא
        # היחיד שיכול לדחוף ערך מותאם דרך ה-CRUD.
        update_customer_fact(fact_id, {"last_confirmed_at": "2025-01-01 10:00:00"})
        before = get_customer_facts("u1", status="active")[0]["last_confirmed_at"]
        assert before == "2025-01-01 10:00:00"

        counts = validator.save_extractions(
            [_ext(action="confirm", confirms_id=fact_id)],
            user_id="u1",
        )
        assert counts["confirmed"] == 1
        assert counts["added"] == 0
        facts = get_customer_facts("u1", status="active")
        assert len(facts) == 1
        # last_confirmed_at השתנה (נכתב עכשיו, לא 2025-01-01).
        assert facts[0]["last_confirmed_at"] != before
        assert facts[0]["last_confirmed_at"].startswith("2026") or \
               facts[0]["last_confirmed_at"].startswith("2025-")

    def test_supersede_creates_new_and_marks_old(self, db_conn):
        """supersede: ה-fact הישן עובר ל-status='superseded' עם
        superseded_by_id מצביע ל-new; חדש נשאר active."""
        from database import get_customer_facts, insert_customer_fact

        old_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה בקרים", "confidence": 0.9, "status": "active",
        })

        counts = validator.save_extractions(
            [_ext(action="supersede", supersedes_id=old_id,
                  content="מעדיפה ערבים", confidence=0.9)],
            user_id="u1",
        )
        assert counts["superseded"] == 1

        all_facts = get_customer_facts("u1", status="all")
        assert len(all_facts) == 2
        old = next(f for f in all_facts if f["id"] == old_id)
        new = next(f for f in all_facts if f["id"] != old_id)
        assert old["status"] == "superseded"
        assert old["superseded_by_id"] == new["id"]
        assert new["status"] == "active"
        assert new["content"] == "מעדיפה ערבים"

    def test_batch_conflict_two_supersedes_same_id_no_orphan(self, db_conn):
        """באג שדווח (Medium): LLM מחזיר שני supersede על אותו old_id.
        בלי הגנה, השני היה מעדכן superseded_by_id של ה-old לקישור החדש
        ויוצר orphan — fact חדש active בלי קישור נכנס מאף ישן.

        אחרי תיקון: השני נדחה כ-batch_conflict_skipped; אין orphan.
        """
        from database import get_customer_facts, insert_customer_fact

        old_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "מעדיפה בקרים", "confidence": 0.9, "status": "active",
        })

        counts = validator.save_extractions(
            [
                _ext(action="supersede", supersedes_id=old_id,
                     content="מעדיפה ערבים", confidence=0.9),
                _ext(action="supersede", supersedes_id=old_id,
                     content="מעדיפה צהריים", confidence=0.9),
            ],
            user_id="u1",
        )
        assert counts["superseded"] == 1
        assert counts["batch_conflict_skipped"] == 1

        all_facts = get_customer_facts("u1", status="all")
        # 2 facts: הישן (superseded) + החדש (active). השני לא נוצר.
        assert len(all_facts) == 2
        old = next(f for f in all_facts if f["id"] == old_id)
        new = next(f for f in all_facts if f["id"] != old_id)
        assert old["status"] == "superseded"
        # ה-superseded_by_id של ה-old מצביע ל-new הראשון, לא לשני.
        assert old["superseded_by_id"] == new["id"]
        assert new["status"] == "active"
        # אין fact צהריים — השני לא נוצר.
        contents = {f["content"] for f in all_facts}
        assert "מעדיפה צהריים" not in contents

    def test_batch_conflict_two_confirms_same_id(self, db_conn):
        """שני confirm על אותו fact — השני נדחה (idempotent + מונע
        עדכון כפול של last_confirmed_at שלא יוסיף ערך)."""
        from database import insert_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        counts = validator.save_extractions(
            [
                _ext(action="confirm", confirms_id=fact_id),
                _ext(action="confirm", confirms_id=fact_id),
            ],
            user_id="u1",
        )
        assert counts["confirmed"] == 1
        assert counts["batch_conflict_skipped"] == 1

    def test_batch_conflict_confirm_then_supersede_same_id(self, db_conn):
        """confirm ואז supersede על אותו fact — סותר, השני נדחה."""
        from database import get_customer_facts, insert_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "ישן", "confidence": 0.9, "status": "active",
        })
        counts = validator.save_extractions(
            [
                _ext(action="confirm", confirms_id=fact_id),
                _ext(action="supersede", supersedes_id=fact_id,
                     content="חדש"),
            ],
            user_id="u1",
        )
        assert counts["confirmed"] == 1
        assert counts["superseded"] == 0
        assert counts["batch_conflict_skipped"] == 1

        # ה-fact נשאר active (confirm כן עבר); ה-supersede נדחה.
        actives = get_customer_facts("u1", status="active")
        assert len(actives) == 1
        assert actives[0]["id"] == fact_id
        assert actives[0]["content"] == "ישן"

    def test_batch_conflict_supersede_then_confirm_same_id(self, db_conn):
        """supersede ואז confirm על אותו fact — confirm על fact שכבר
        מסומן superseded, נדחה."""
        from database import get_customer_facts, insert_customer_fact

        fact_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "ישן", "confidence": 0.9, "status": "active",
        })
        counts = validator.save_extractions(
            [
                _ext(action="supersede", supersedes_id=fact_id,
                     content="חדש"),
                _ext(action="confirm", confirms_id=fact_id),
            ],
            user_id="u1",
        )
        assert counts["superseded"] == 1
        assert counts["confirmed"] == 0
        assert counts["batch_conflict_skipped"] == 1

        all_facts = get_customer_facts("u1", status="all")
        # ה-old superseded, החדש active.
        assert len(all_facts) == 2
        old = next(f for f in all_facts if f["id"] == fact_id)
        assert old["status"] == "superseded"

    def test_different_ids_not_blocked(self, db_conn):
        """שני supersedes על fact_id שונים — שניהם עוברים."""
        from database import get_customer_facts, insert_customer_fact

        old1 = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "old1", "confidence": 0.9, "status": "active",
        })
        old2 = insert_customer_fact({
            "user_id": "u1", "fact_type": "vocabulary",
            "content": "old2", "confidence": 0.9, "status": "active",
        })

        counts = validator.save_extractions(
            [
                _ext(action="supersede", supersedes_id=old1, content="new1"),
                _ext(action="supersede", supersedes_id=old2, content="new2",
                     fact_type="vocabulary"),
            ],
            user_id="u1",
        )
        assert counts["superseded"] == 2
        assert counts["batch_conflict_skipped"] == 0

    def test_io_error_in_one_does_not_stop_others(self, db_conn):
        """כשל בפריט אחד → counts.errors++, השאר ממשיכים (CLAUDE.md
        לולאות I/O ארוכות — עמידות בפני כשלים)."""
        from database import get_customer_facts

        # confirm על fact_id שלא קיים → אמור להחזיר 0 ב-update_customer_fact
        # ו-counts.errors++. ה-add אחריו צריך לעבור.
        counts = validator.save_extractions(
            [
                _ext(action="confirm", confirms_id=999999),
                _ext(content="עובדה תקינה", confidence=0.95),
            ],
            user_id="u1",
        )
        # ה-confirm כשל (לא מצא fact), ה-add עבר.
        assert counts["added"] == 1
        assert counts["errors"] >= 1
        assert len(get_customer_facts("u1", status="active")) == 1


# ────────────────────────────────────────────────────────────────────
# save_extractions — אישור אוטומטי פר-עסק (memory_auto_approve)
# ────────────────────────────────────────────────────────────────────


class TestSaveExtractionsAutoApprove:
    """כשהמתג memory_auto_approve דלוק, עובדות לא-רגישות בבטחון בינוני
    נכנסות ל-active מיד. מידע רגיש נשאר בתור לאישור ידני — שער הפרטיות
    אינו נעקף."""

    def test_mid_confidence_active_when_auto_approve_on(self, db_conn):
        from database import get_customer_facts

        with patch.object(validator.db, "is_memory_auto_approve", return_value=True):
            counts = validator.save_extractions(
                [_ext(confidence=0.72)],
                user_id="u1", business_id="default",
            )
        assert counts["added"] == 1
        # בטחון בינוני (0.72) שהיה pending כברירת מחדל — עכשיו active.
        assert len(get_customer_facts("u1", status="active")) == 1
        assert len(get_customer_facts("u1", status="pending_approval")) == 0

    def test_sensitive_stays_pending_even_when_auto_approve_on(self, db_conn):
        from database import get_customer_facts

        with patch.object(validator.db, "is_memory_auto_approve", return_value=True):
            validator.save_extractions(
                [_ext(content="רגישה לאגוזים", confidence=0.72,
                      requires_consent=True, fact_type="personal_info")],
                user_id="u1", business_id="default",
            )
        # שער הפרטיות: מידע רגיש נשאר בתור גם כש-auto_approve דלוק.
        assert len(get_customer_facts("u1", status="active")) == 0
        pending = get_customer_facts("u1", status="pending_approval")
        assert len(pending) == 1
        assert pending[0]["requires_consent"] == 1

    def test_mid_confidence_stays_pending_when_auto_approve_off(self, db_conn):
        from database import get_customer_facts

        with patch.object(validator.db, "is_memory_auto_approve", return_value=False):
            validator.save_extractions(
                [_ext(confidence=0.72)],
                user_id="u1", business_id="default",
            )
        # ברירת מחדל (כבוי) — התנהגות קיימת נשמרת.
        assert len(get_customer_facts("u1", status="active")) == 0
        assert len(get_customer_facts("u1", status="pending_approval")) == 1

    def test_read_failure_falls_back_to_manual(self, db_conn):
        """אם קריאת ההגדרה נכשלת — fail-safe ל-manual (pending), לא active."""
        from database import get_customer_facts

        with patch.object(validator.db, "is_memory_auto_approve",
                          side_effect=RuntimeError("db locked")):
            validator.save_extractions(
                [_ext(confidence=0.72)],
                user_id="u1", business_id="default",
            )
        # כשל בקריאה → auto_approve=False → נשאר pending (בטוח).
        assert len(get_customer_facts("u1", status="active")) == 0
        assert len(get_customer_facts("u1", status="pending_approval")) == 1


# ────────────────────────────────────────────────────────────────────
# run_extraction_for_user (orchestrator)
# ────────────────────────────────────────────────────────────────────


class TestOrchestrator:
    def test_full_flow_completed(self, db_conn):
        """LLM מצליח → ולידציה → save → log ל-extraction_runs status=completed."""
        from database import get_customer_facts, get_last_extraction_run

        llm_result = {
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": "מעדיפה בקרים", "confidence": 0.93,
                "requires_consent": False, "evidence": "ה-9 בבוקר נוח לי",
                "supersedes_id": None, "confirms_id": None,
            }],
            "skipped": [{"proposed_fact": "x", "reason": "y"}],
            "tokens_used": 500, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts", return_value=llm_result):
            out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "בוקרים נוח לי",
                     "created_at": "2026-05-26 10:00:00"},
                    {"role": "assistant", "message": "בסדר",
                     "created_at": "2026-05-26 10:00:05"},
                ],
            )
        assert out["status"] == "completed"
        assert out["saved"]["added"] == 1
        assert out["tokens_used"] == 500
        assert out["skipped_count"] == 1
        assert out["validation_failures"] == 0

        # שורה אחת ב-extraction_runs עם boundaries מהשיחה.
        last = get_last_extraction_run("u1", "default")
        assert last["status"] == "completed"
        assert last["tokens_used"] == 500
        assert last["conversation_start"] == "2026-05-26 10:00:00"
        assert last["conversation_end"] == "2026-05-26 10:00:05"
        assert last["messages_count"] == 2

        # ה-fact נשמר.
        assert len(get_customer_facts("u1", status="active")) == 1

    def test_llm_failure_logged(self, db_conn):
        """LLM כשל → status=failed, error_message מתועד, אין facts שנשמרו."""
        from database import get_customer_facts, get_last_extraction_run

        llm_result = {
            "extractions": [], "skipped": [],
            "tokens_used": 50, "success": False, "error": "APIConnectionError",
        }
        with patch.object(validator.extractor, "extract_facts", return_value=llm_result):
            out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )
        assert out["status"] == "failed"
        assert "APIConnectionError" in (out["error"] or "")

        last = get_last_extraction_run("u1", "default")
        assert last["status"] == "failed"
        assert "APIConnectionError" in last["error_message"]
        assert len(get_customer_facts("u1", status="all")) == 0

    def test_validation_failures_counted_and_logged(self, db_conn):
        """LLM מחזיר extraction לא תקין → נדחה ב-validation, נספר ב-run."""
        from database import get_customer_facts, get_last_extraction_run

        llm_result = {
            "extractions": [
                # תקין
                {"action": "add", "fact_type": "preference",
                 "content": "טוב", "confidence": 0.95,
                 "requires_consent": False, "evidence": "ev",
                 "supersedes_id": None, "confirms_id": None},
                # נדחה — confidence נמוך
                {"action": "add", "fact_type": "preference",
                 "content": "רע", "confidence": 0.3,
                 "requires_consent": False, "evidence": "ev",
                 "supersedes_id": None, "confirms_id": None},
                # נדחה — confirms_id לא קיים
                {"action": "confirm", "fact_type": "preference",
                 "content": "x", "confidence": 0.9,
                 "requires_consent": False, "evidence": "ev",
                 "supersedes_id": None, "confirms_id": 9999},
            ],
            "skipped": [],
            "tokens_used": 700, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts", return_value=llm_result):
            out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )

        assert out["extractions_count"] == 3
        assert out["saved"]["added"] == 1
        assert out["validation_failures"] == 2

        last = get_last_extraction_run("u1", "default")
        # skipped_count ב-run = LLM_skipped + validation_failures
        assert last["skipped_count"] == 2
        assert last["extractions_count"] == 1  # רק תקין נשמר
        assert len(get_customer_facts("u1", status="all")) == 1

    def test_batch_conflict_skipped_aggregated_into_skipped_count(self, db_conn):
        """batch_conflict_skipped מהשמירה צריך להתווסף ל-skipped_count
        של extraction_runs (כדי שהאדמין יראה את התמונה המלאה)."""
        from database import get_last_extraction_run, insert_customer_fact

        old_id = insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "ישן", "confidence": 0.9, "status": "active",
        })

        llm_result = {
            "extractions": [
                # שני supersede על אותו old_id — השני יוסר כ-batch_conflict
                {"action": "supersede", "fact_type": "preference",
                 "content": "חדש1", "confidence": 0.9,
                 "requires_consent": False, "evidence": "ev",
                 "supersedes_id": old_id, "confirms_id": None},
                {"action": "supersede", "fact_type": "preference",
                 "content": "חדש2", "confidence": 0.9,
                 "requires_consent": False, "evidence": "ev",
                 "supersedes_id": old_id, "confirms_id": None},
            ],
            "skipped": [{"proposed_fact": "x", "reason": "y"}],  # 1 מ-LLM
            "tokens_used": 100, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts", return_value=llm_result):
            out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )

        # extractions_count מספר רק את מה שנשמר בפועל
        assert out["saved"]["superseded"] == 1
        assert out["saved"]["batch_conflict_skipped"] == 1

        last = get_last_extraction_run("u1", "default")
        # skipped_count = 1 (מ-LLM) + 0 (validation_failures) + 1 (batch_conflict)
        assert last["skipped_count"] == 2

    def test_saved_dict_schema_consistent_across_branches(self, db_conn):
        """saved dict חייב להיות עם אותם מפתחות בכל ה-branches (success/
        failed) — אחרת קוראים שעושים result['saved']['batch_conflict_skipped']
        יקבלו KeyError לסירוגין רק כש-LLM נכשל. תוקן בעקבות סקירה (Low)."""
        from database import insert_customer_fact

        # Success branch
        success_llm = {
            "extractions": [], "skipped": [],
            "tokens_used": 0, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts", return_value=success_llm):
            success_out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )

        # Failed branch
        failed_llm = {
            "extractions": [], "skipped": [],
            "tokens_used": 0, "success": False, "error": "API down",
        }
        with patch.object(validator.extractor, "extract_facts", return_value=failed_llm):
            failed_out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )

        # אותם מפתחות בדיוק
        assert set(success_out["saved"].keys()) == set(failed_out["saved"].keys())
        # וכוללים את batch_conflict_skipped (הקריטי לקוראים חיצוניים)
        assert "batch_conflict_skipped" in failed_out["saved"]
        assert failed_out["saved"]["batch_conflict_skipped"] == 0

    def test_existing_facts_passed_to_extractor(self, db_conn):
        """active + pending_approval של המשתמש עוברים ל-extractor."""
        from database import insert_customer_fact

        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "active fact", "confidence": 0.9, "status": "active",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "pending fact", "confidence": 0.7,
            "status": "pending_approval",
        })
        insert_customer_fact({
            "user_id": "u1", "fact_type": "preference",
            "content": "rejected fact", "confidence": 0.9, "status": "rejected",
        })

        llm_result = {
            "extractions": [], "skipped": [],
            "tokens_used": 0, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts", return_value=llm_result) as mock_ex:
            validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "a"},
                    {"role": "assistant", "message": "b"},
                ],
            )
        existing = mock_ex.call_args.kwargs["existing_facts"]
        contents = {f["content"] for f in existing}
        assert "active fact" in contents
        assert "pending fact" in contents
        # rejected לא עובר ל-LLM (יוצר רעש מיותר)
        assert "rejected fact" not in contents


class TestResolveValidation:
    """validate_extraction עבור action=resolve (פרומפט v2.2)."""

    @staticmethod
    def _open_issue(fid=42):
        return [{"id": fid, "fact_type": "open_issue", "content": "ממתינה להחזר"}]

    @staticmethod
    def _resolve_ext(**ov):
        base = {
            "action": "resolve", "fact_type": "open_issue", "content": None,
            "confidence": 0.95, "requires_consent": False,
            "evidence": "ההחזר הגיע",
            "supersedes_id": None, "confirms_id": None, "resolves_id": 42,
        }
        base.update(ov)
        return base

    def test_valid_resolve(self):
        ok, reason = validator.validate_extraction(self._resolve_ext(), self._open_issue())
        assert ok is True, reason

    def test_resolve_non_open_issue_fact_type_rejected(self):
        ok, reason = validator.validate_extraction(
            self._resolve_ext(fact_type="preference"), self._open_issue())
        assert ok is False
        assert "open_issue" in reason

    def test_resolve_missing_resolves_id(self):
        ok, reason = validator.validate_extraction(
            self._resolve_ext(resolves_id=None), self._open_issue())
        assert ok is False
        assert "resolves_id" in reason

    def test_resolve_non_null_content_rejected(self):
        ok, reason = validator.validate_extraction(
            self._resolve_ext(content="text"), self._open_issue())
        assert ok is False
        assert "content=null" in reason

    def test_resolve_with_confirms_id_rejected(self):
        ok, reason = validator.validate_extraction(
            self._resolve_ext(confirms_id=42), self._open_issue())
        assert ok is False

    def test_resolve_unknown_id_rejected(self):
        ok, reason = validator.validate_extraction(
            self._resolve_ext(resolves_id=999), self._open_issue())
        assert ok is False
        assert "999" in reason

    def test_resolve_target_not_open_issue_rejected(self):
        existing = [{"id": 42, "fact_type": "preference", "content": "x"}]
        ok, reason = validator.validate_extraction(self._resolve_ext(), existing)
        assert ok is False
        assert "open_issue" in reason

    def test_non_resolve_with_resolves_id_rejected(self):
        # add עם resolves_id != null → דחייה
        ok, reason = validator.validate_extraction(
            _ext(resolves_id=5), [{"id": 5, "fact_type": "open_issue"}])
        assert ok is False
        assert "resolves_id=null" in reason


class TestSaveResolve:
    def test_save_resolve_updates_existing(self, db_conn):
        from database import get_customer_facts, insert_customer_fact
        fid = insert_customer_fact({
            "user_id": "u1", "fact_type": "open_issue",
            "content": "ממתינה להחזר", "confidence": 0.9, "status": "active",
        })
        counts = validator.save_extractions([{
            "action": "resolve", "fact_type": "open_issue", "content": None,
            "confidence": 0.95, "requires_consent": False, "evidence": "ההחזר הגיע",
            "supersedes_id": None, "confirms_id": None, "resolves_id": fid,
        }], user_id="u1")
        assert counts["resolved"] == 1
        assert counts["added"] == 0
        row = db_conn.execute(
            "SELECT status, resolution_evidence FROM customer_facts WHERE id=?",
            (fid,)).fetchone()
        assert row["status"] == "resolved"
        assert row["resolution_evidence"] == "ההחזר הגיע"
        assert get_customer_facts("u1", status="active") == []

    def test_save_resolve_missing_fact_counts_error(self, db_conn):
        counts = validator.save_extractions([{
            "action": "resolve", "fact_type": "open_issue", "content": None,
            "confidence": 0.95, "requires_consent": False, "evidence": "ev",
            "supersedes_id": None, "confirms_id": None, "resolves_id": 999999,
        }], user_id="u1")
        assert counts["resolved"] == 0
        assert counts["errors"] == 1

    def test_save_resolve_batch_conflict(self, db_conn):
        from database import insert_customer_fact
        fid = insert_customer_fact({
            "user_id": "u1", "fact_type": "open_issue",
            "content": "x", "confidence": 0.9, "status": "active",
        })
        def _r():
            return {"action": "resolve", "fact_type": "open_issue", "content": None,
                    "confidence": 0.95, "requires_consent": False, "evidence": "ev",
                    "supersedes_id": None, "confirms_id": None, "resolves_id": fid}
        counts = validator.save_extractions([_r(), _r()], user_id="u1")
        assert counts["resolved"] == 1
        assert counts["batch_conflict_skipped"] == 1


class TestLogExtractionFailureHandling:
    """שלב 6.2 — אם log_extraction_run נכשל אחרי save_extractions עבר,
    status='failed' מוחזר כדי שה-scheduler לא יקדם cursor. dedup
    מבטיח שאין duplicates בסבב הבא.
    """

    def test_log_failure_returns_failed_status(self, db_conn):
        from database import get_customer_facts

        llm_result = {
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": "מעדיפה בקרים", "confidence": 0.93,
                "requires_consent": False, "evidence": "ev",
                "supersedes_id": None, "confirms_id": None,
            }],
            "skipped": [], "tokens_used": 100, "success": True, "error": None,
        }
        with patch.object(validator.extractor, "extract_facts",
                          return_value=llm_result), \
             patch.object(validator.db, "log_extraction_run",
                          side_effect=RuntimeError("disk full")):
            out = validator.run_extraction_for_user(
                user_id="u1", business_id="default",
                conversation=[
                    {"id": 100, "role": "user", "message": "אני אוהבת בוקר",
                     "created_at": "2026-05-01 10:00:00"},
                    {"id": 101, "role": "assistant", "message": "מצוין",
                     "created_at": "2026-05-01 10:01:00"},
                ],
            )

        # status='failed' — ה-scheduler לא ייספור את זה כ-extracted
        assert out["status"] == "failed"
        assert "log_extraction_run failed" in (out["error"] or "")
        # save_extractions עבד — ה-fact ב-DB
        facts = get_customer_facts("u1", status="active")
        assert any(f["content"] == "מעדיפה בקרים" for f in facts)

    def test_log_failure_dedup_protects_next_cycle(self, db_conn):
        """אחרי log failure, אם הסבב הבא רץ עם אותן הודעות — dedup
        ימנע יצירת fact כפול. UNIQUE partial index הוא ה-safety net."""
        from database import get_customer_facts

        llm_result = {
            "extractions": [{
                "action": "add", "fact_type": "preference",
                "content": "אהבה בקפה", "confidence": 0.93,
                "requires_consent": False, "evidence": "ev",
                "supersedes_id": None, "confirms_id": None,
            }],
            "skipped": [], "tokens_used": 100, "success": True, "error": None,
        }
        conv = [
            {"id": 1, "role": "user", "message": "x",
             "created_at": "2026-05-01 10:00:00"},
            {"id": 2, "role": "assistant", "message": "y",
             "created_at": "2026-05-01 10:01:00"},
        ]

        # סבב 1 — log נכשל
        with patch.object(validator.extractor, "extract_facts",
                          return_value=llm_result), \
             patch.object(validator.db, "log_extraction_run",
                          side_effect=RuntimeError("locked")):
            validator.run_extraction_for_user(
                user_id="u_dedup", business_id="default", conversation=conv,
            )

        # סבב 2 — log עובד, אותן הודעות
        with patch.object(validator.extractor, "extract_facts",
                          return_value=llm_result):
            out2 = validator.run_extraction_for_user(
                user_id="u_dedup", business_id="default", conversation=conv,
            )

        assert out2["status"] == "completed"
        # סבב 2 אמור היה לדלג על ה-fact (dedup) או להחליף — בשני המקרים
        # אין כפילות ב-DB.
        facts = get_customer_facts("u_dedup", status="active")
        contents = [f["content"] for f in facts]
        # רק fact אחד עם התוכן הזה
        assert contents.count("אהבה בקפה") == 1

    def test_max_message_id_persisted(self, db_conn):
        """validator מחשב MAX(id) של conversation ומעביר ל-log_extraction_run."""
        llm_result = {
            "extractions": [], "skipped": [],
            "tokens_used": 50, "success": True, "error": None,
        }
        captured_run = {}

        def fake_log(run_data):
            captured_run.update(run_data)
            return 1

        with patch.object(validator.extractor, "extract_facts",
                          return_value=llm_result), \
             patch.object(validator.db, "log_extraction_run",
                          side_effect=fake_log):
            validator.run_extraction_for_user(
                user_id="u_max", business_id="default",
                conversation=[
                    {"id": 500, "role": "user", "message": "x",
                     "created_at": "2026-05-01 10:00:00"},
                    {"id": 999, "role": "assistant", "message": "y",
                     "created_at": "2026-05-01 10:01:00"},
                    {"id": 750, "role": "user", "message": "z",
                     "created_at": "2026-05-01 10:02:00"},
                ],
            )

        # MAX(500, 999, 750) = 999
        assert captured_run["last_message_id"] == 999
