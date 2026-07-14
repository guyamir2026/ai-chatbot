"""
טסטים ל-memory/background.py (שלב 6 — Background extraction scheduler).

מכסה: לולאת _process_due_users עם mocks ל-DB ול-LLM, lock נגד
double-extraction, error isolation בין משתמשים, ENV toggle.

כל הטסטים על _process_due_users ישירות — לא מריצים thread (פשוט,
דטרמיניסטי). הטסט היחיד שנוגע ב-thread הוא test_disabled_flag_blocks_start.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from memory import background


@pytest.fixture(autouse=True)
def _reset_lock_set():
    """לבטל את ה-_in_progress בין טסטים — אחרת lock state דולף."""
    background._in_progress.clear()
    yield
    background._in_progress.clear()


@pytest.fixture(autouse=True)
def _default_idle(monkeypatch):
    """autouse — ברירת מחדל לכל הטסטים: ההודעה האחרונה של המשתמש לפני
    45 דקות (מעבר ל-idle threshold של 30 דק) → שיחה הסתיימה, ה-cycle
    ימשיך לטעון messages. טסטים שצריכים שיחה פעילה — patch ידנית עם
    `monkeypatch.setattr(background.db, "get_user_last_message_time",
    lambda u: _utc_str(5))` או דומה.
    """
    monkeypatch.setattr(
        background.db, "get_user_last_message_time",
        lambda user_id: _utc_str(45),
    )


def _utc_str(minutes_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_msg_id_counter = {"n": 0}


def _msg(role: str, message: str, minutes_ago: int) -> dict:
    """כל הודעה מקבלת id ייחודי כדי שה-max_message_id ב-validator
    יהיה דטרמיניסטי בטסטים."""
    _msg_id_counter["n"] += 1
    return {
        "id": _msg_id_counter["n"], "user_id": "u1", "role": role,
        "message": message, "created_at": _utc_str(minutes_ago),
    }


class TestProcessDueUsers:
    """ה-cycle הראשי. mock על DB helpers + run_extraction_for_user
    כדי לבדוק את לוגיקת ה-skip/extract בלי לרוץ pipeline אמיתי."""

    def test_user_with_active_conversation_skipped(self, monkeypatch):
        """ההודעה האחרונה של המשתמש לפני 5 דקות → תוך חלון idle של 30 דק
        → דלג. (שלב 6.3: idle check מבוסס על get_user_last_message_time
        — MAX של כל הודעות המשתמש, לא ה-batch.)"""
        monkeypatch.setattr(
            background.db, "get_user_last_message_time",
            lambda user_id: _utc_str(5),
        )
        messages = [_msg("user", "hi", 10), _msg("assistant", "hey", 5)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user") as m_extract:
            background._process_due_users()
            m_extract.assert_not_called()

    def test_user_with_idle_conversation_extracted(self):
        """הודעה אחרונה לפני 45 דקות → מעבר לסף → run_extraction נקרא."""
        messages = [_msg("user", "hi", 60), _msg("assistant", "hey", 45)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}) as m_extract:
            background._process_due_users()
            m_extract.assert_called_once_with("u1", background.BUSINESS_ID, messages)

    def test_user_with_no_new_messages_skipped(self):
        """get_conversation_after מחזיר 0 או 1 → לא קוראים ל-extract."""
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=1234), \
             patch.object(background.db, "get_conversation_after",
                          return_value=[]), \
             patch.object(background, "run_extraction_for_user") as m_extract:
            background._process_due_users()
            m_extract.assert_not_called()

    def test_single_message_skipped(self):
        """הודעה אחת בלבד (< 2) → דלג."""
        messages = [_msg("user", "hi", 60)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user") as m_extract:
            background._process_due_users()
            m_extract.assert_not_called()

    def test_exception_per_user_does_not_break_cycle(self):
        """3 users, מקסם זורק על האמצעי → 1+3 עוברים, ה-cycle לא קורס."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        call_count = {"n": 0}

        def fake_extract(uid, biz, msgs):
            call_count["n"] += 1
            if uid == "u_bad":
                raise RuntimeError("LLM down")
            return {"status": "completed"}

        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_a", "u_bad", "u_c"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          side_effect=fake_extract):
            # לא צריך לזרוק
            background._process_due_users()

        # 3 קריאות בוצעו (גם המקסם זרק — נתפס פנימית)
        assert call_count["n"] == 3

    def test_in_progress_lock_prevents_double_extraction(self):
        """user_id כבר ב-_in_progress (cycle קודם תקוע) → דלג."""
        background._in_progress.add("u1")  # סימולציה — תקוע
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user") as m_extract:
            background._process_due_users()
            m_extract.assert_not_called()
        # ה-lock לא נמחק (כי לא נכנסנו ל-try) — צפוי
        assert "u1" in background._in_progress

    def test_in_progress_released_after_extraction(self):
        """אחרי שה-extract רץ, user_id יוסר מ-_in_progress."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}):
            background._process_due_users()
        assert "u1" not in background._in_progress

    def test_in_progress_released_even_on_exception(self):
        """גם אם extract זרק, ה-lock משוחרר ב-finally."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          side_effect=RuntimeError("boom")):
            background._process_due_users()
        assert "u1" not in background._in_progress

    def test_failed_extraction_logged_not_raised(self):
        """run_extraction_for_user מחזיר status=failed → לא זורק."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "failed",
                                        "error": "LLM timeout"}):
            background._process_due_users()  # לא זורק

    def test_no_users_active_short_circuits(self):
        """אין משתמשים פעילים → לא נופלים כלום."""
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=[]), \
             patch.object(background, "run_extraction_for_user") as m_extract:
            background._process_due_users()
            m_extract.assert_not_called()

    def test_bad_created_at_format_logged_but_proceeds(self):
        """created_at לא בפורמט הצפוי → warning, ממשיכים ל-extract."""
        messages = [
            {"id": 1, "user_id": "u1", "role": "user",
             "message": "x", "created_at": "INVALID"},
            {"id": 2, "user_id": "u1", "role": "assistant",
             "message": "y", "created_at": "ALSO_BAD"},
        ]
        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u1"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          return_value=messages), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}) as m_extract:
            background._process_due_users()
            # נקרא — ה-parse נכשל בשקט וזרימת ה-skip_active לא רצה
            m_extract.assert_called_once()


class TestStartStopScheduler:
    """לא מריצים cycle אמיתי — בודקים את ה-lifecycle בלבד."""

    def test_disabled_flag_blocks_start(self):
        """MEMORY_BACKGROUND_ENABLED=False → start מחזיר False, thread None."""
        with patch.object(background, "MEMORY_BACKGROUND_ENABLED", False):
            # אם כבר רץ thread מטסט קודם, נעצור אותו קודם
            background._scheduler_thread = None
            assert background.start_scheduler() is False
            assert background._scheduler_thread is None

    def test_already_running_returns_true_without_double_start(self):
        """is_alive() guard — start שני באותו process מחזיר True בלי
        ליצור thread נוסף."""
        with patch.object(background, "MEMORY_BACKGROUND_ENABLED", True), \
             patch.object(background, "_POLL_INTERVAL", 0.01):
            background._scheduler_thread = None
            background._scheduler_stop.clear()
            assert background.start_scheduler() is True
            first_thread = background._scheduler_thread
            # קריאה שנייה
            assert background.start_scheduler() is True
            assert background._scheduler_thread is first_thread
            # ניקוי
            background.stop_scheduler(timeout=1.0)

    def test_stop_scheduler_idempotent(self):
        """stop_scheduler על scheduler שלא רץ — לא קורס."""
        background._scheduler_thread = None
        background.stop_scheduler(timeout=0.1)  # לא זורק


class TestLookbackFallback:
    """Regression (Cursor HIGH): משתמש בלי extraction_run קודם → לא
    להעביר None ל-get_conversation_after (שהיה מושך היסטוריה ישנה).
    במקום זה, fallback ל-since_lookback (now - MEMORY_LOOKBACK_DAYS)."""

    def test_user_with_no_prior_run_uses_lookback_window(self):
        """get_conversation_after נקראת עם תאריך בתוך 7-30 דקות
        מ-(now - MEMORY_LOOKBACK_DAYS days), לא עם None."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        captured = {}

        def fake_get_after(user_id, after_id, since_iso, limit):
            captured["after_id"] = after_id
            captured["since_iso"] = since_iso
            return messages

        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_new"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=None), \
             patch.object(background.db, "get_conversation_after",
                          side_effect=fake_get_after), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}):
            background._process_due_users()

        # last_id=None → after_id=None, since_iso=fallback ל-lookback
        assert captured["after_id"] is None
        assert captured["since_iso"] is not None
        # ה-since צריך להיות תוך 5 שניות מהמועד הצפוי
        # (MEMORY_LOOKBACK_DAYS=7 לפי default)
        from datetime import datetime, timedelta, timezone
        expected = datetime.now(timezone.utc) - timedelta(
            days=background.MEMORY_LOOKBACK_DAYS,
        )
        actual = datetime.strptime(
            captured["since_iso"], "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
        assert abs((actual - expected).total_seconds()) < 5

    def test_user_with_prior_run_uses_last_id_not_lookback(self):
        """אם יש last_id (cursor שלב 6.2) → after_id=last_id, since_iso=None."""
        messages = [_msg("user", "x", 60), _msg("assistant", "y", 45)]
        captured = {}

        def fake_get_after(user_id, after_id, since_iso, limit):
            captured["after_id"] = after_id
            captured["since_iso"] = since_iso
            return messages

        with patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_existing"]), \
             patch.object(background.db, "get_last_extraction_message_id",
                          return_value=5678), \
             patch.object(background.db, "get_conversation_after",
                          side_effect=fake_get_after), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}):
            background._process_due_users()

        # last_id=5678 → after_id=5678, since_iso=None (id cursor העדיף)
        assert captured["after_id"] == 5678
        assert captured["since_iso"] is None


class TestBacklogProcessing:
    """Regression (Cursor HIGH שלב 6.3): backlog > cap. לפני התיקון
    (DESC+LIMIT), הסבב הראשון היה לוקח את ה-cap ה**אחרונות** ושומר
    last_message_id = MAX → סבב הבא היה מתחיל מ-id יותר גבוה והודעות
    ישנות (id נמוך, שלא נכנסו ל-batch) היו נעלמות לעד.

    אחרי התיקון: ASC + LIMIT. הסבב הראשון מעבד את ה-50 ה**ראשונות**
    (ids נמוכים), שומר MAX. הסבב הבא מעבד את הנותרות (ids גבוהים).
    בנוסף, ה-idle check נשען על MAX(created_at) של כל ההודעות, לא
    על ה-batch — כך אם השיחה עדיין פעילה, גם backlog לא יעובד.

    משתמש בטסט אמיתי עם DB (לא mock) כדי לאמת את האינטראקציה.
    """

    @pytest.fixture(autouse=True)
    def _use_real_db_idle_check(self, monkeypatch):
        """Override של ה-autouse fixture של המודול — בטסטים האלה אנחנו
        עובדים על DB אמיתי, צריך את ה-`get_user_last_message_time`
        האמיתי כדי לבדוק את האינטראקציה."""
        from database import get_user_last_message_time as real_fn
        monkeypatch.setattr(
            background.db, "get_user_last_message_time", real_fn,
        )

    def test_backlog_processed_oldest_first_across_cycles(self, db_conn):
        """80 הודעות, cap=50, שיחה הסתיימה. סבב 1 = ids 1-50,
        סבב 2 = ids 51-80. אחרי 2 cycles כולן מעובדות, ה-3rd ידלג."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        # 80 הודעות, ההודעה האחרונה לפני שעה (מעבר ל-idle)
        seeded_ids: list[int] = []
        for i in range(80):
            ts = (now - timedelta(hours=8) + timedelta(minutes=i * 5)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            cur = db_conn.execute(
                "INSERT INTO conversations (user_id, role, message, "
                "created_at) VALUES (?, ?, ?, ?)",
                ("u_backlog", "user" if i % 2 == 0 else "assistant",
                 f"m{i}", ts),
            )
            seeded_ids.append(int(cur.lastrowid))
        db_conn.commit()

        from database import log_extraction_run

        with patch.object(background, "MEMORY_CONVERSATION_CAP", 50), \
             patch.object(background, "run_extraction_for_user") as m_extract, \
             patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_backlog"]):

            # ה-fake_extract מדמה אמיתי: שומר last_message_id ב-DB
            # כדי שהסבב הבא יראה את ה-cursor מתקדם.
            def fake_extract(uid, biz, msgs):
                max_id = max(int(m["id"]) for m in msgs)
                log_extraction_run({
                    "user_id": uid, "business_id": biz,
                    "status": "completed",
                    "last_message_id": max_id,
                })
                return {"status": "completed"}

            m_extract.side_effect = fake_extract

            # סבב 1
            background._process_due_users()
            assert m_extract.call_count == 1
            cycle1_msgs = m_extract.call_args_list[0][0][2]
            assert len(cycle1_msgs) == 50
            assert cycle1_msgs[0]["message"] == "m0"
            assert cycle1_msgs[-1]["message"] == "m49"

            # סבב 2 — ה-cursor התקדם, נשארו 30
            background._process_due_users()
            assert m_extract.call_count == 2
            cycle2_msgs = m_extract.call_args_list[1][0][2]
            assert len(cycle2_msgs) == 30
            assert cycle2_msgs[0]["message"] == "m50"
            assert cycle2_msgs[-1]["message"] == "m79"

            # סבב 3 — אין הודעות חדשות
            background._process_due_users()
            # call_count נשאר 2 (לא נקרא ל-extract)
            assert m_extract.call_count == 2

    def test_backlog_with_active_conversation_skipped(self, db_conn):
        """80 הודעות backlog + הודעה אחרונה לפני 10 דקות → שיחה פעילה
        → לא לעבד את ה-backlog (idle check על MAX(created_at) של כל
        ההודעות, לא ה-batch)."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        for i in range(80):
            # הודעה אחרונה לפני 10 דקות → שיחה פעילה
            ts = (now - timedelta(hours=8) + timedelta(minutes=i * 6)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            db_conn.execute(
                "INSERT INTO conversations (user_id, role, message, "
                "created_at) VALUES (?, ?, ?, ?)",
                ("u_active_backlog", "user" if i % 2 == 0 else "assistant",
                 f"m{i}", ts),
            )
        db_conn.commit()

        with patch.object(background, "MEMORY_CONVERSATION_CAP", 50), \
             patch.object(background, "run_extraction_for_user") as m_extract, \
             patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_active_backlog"]):
            background._process_due_users()

        # שיחה פעילה (MAX < 30 דק) → לא לעבד את ה-backlog
        m_extract.assert_not_called()

    def test_short_conversation_idle_processed_normally(self, db_conn):
        """10 הודעות (פחות מ-cap), שיחה הסתיימה. כל ה-10 מעובדות
        בסבב אחד."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        for i in range(10):
            # האחרונה לפני שעה
            ts = (now - timedelta(hours=2) + timedelta(minutes=i * 5)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            db_conn.execute(
                "INSERT INTO conversations (user_id, role, message, "
                "created_at) VALUES (?, ?, ?, ?)",
                ("u_short", "user" if i % 2 == 0 else "assistant",
                 f"m{i}", ts),
            )
        db_conn.commit()

        with patch.object(background, "MEMORY_CONVERSATION_CAP", 50), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}) as m_extract, \
             patch.object(background.db, "get_users_with_pending_messages",
                          return_value=["u_short"]):
            background._process_due_users()

        m_extract.assert_called_once()
        called_messages = m_extract.call_args[0][2]
        assert len(called_messages) == 10
        assert called_messages[0]["message"] == "m0"
        assert called_messages[-1]["message"] == "m9"

    def test_abandoned_user_with_backlog_still_processed_after_lookback(
        self, db_conn,
    ):
        """Regression critical (שלב 6.4): user שולח 80 הודעות, cycle 1
        מעבד 50, נעלם 10 ימים. cycle 2 — עדיין מעבד את ה-30 הנותרות
        דרך get_users_with_pending_messages (backlog לא נושר עם זמן).

        לפני התיקון: get_users_active_since(now - 7d) לא היה מחזיר את
        המשתמש כי כל ההודעות שלו ישנות מ-7 ימים → 30 ההודעות אבודות.
        """
        from datetime import datetime, timedelta, timezone
        from database import log_extraction_run

        # 80 הודעות, כל ההודעות לפני 10 ימים (מחוץ ל-lookback)
        now = datetime.now(timezone.utc)
        for i in range(80):
            ts = (now - timedelta(days=10) + timedelta(minutes=i * 5)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            db_conn.execute(
                "INSERT INTO conversations (user_id, role, message, "
                "created_at) VALUES (?, ?, ?, ?)",
                ("u_abandoned", "user" if i % 2 == 0 else "assistant",
                 f"m{i}", ts),
            )
        db_conn.commit()

        # סימולציה: cycle 1 כבר רץ ועיבד את 50 הראשונות.
        first_50_ids = [
            r["id"] for r in db_conn.execute(
                "SELECT id FROM conversations WHERE user_id='u_abandoned' "
                "ORDER BY id ASC LIMIT 50"
            )
        ]
        log_extraction_run({
            "user_id": "u_abandoned", "business_id": "default",
            "status": "completed",
            "last_message_id": max(first_50_ids),
        })

        with patch.object(background, "MEMORY_CONVERSATION_CAP", 50), \
             patch.object(background, "run_extraction_for_user",
                          return_value={"status": "completed"}) as m_extract:
            background._process_due_users()

        # cycle 2 רץ למרות שכל ההודעות לפני lookback (10 ימים > 7)
        m_extract.assert_called_once()
        called_messages = m_extract.call_args[0][2]
        assert len(called_messages) == 30
        assert called_messages[0]["message"] == "m50"
        assert called_messages[-1]["message"] == "m79"
