"""
בדיקות בידוד (isolation) בין tenants — מקצה-לקצה, מול המימוש האמיתי.

משלים את tests/test_tenant_state_isolation.py (שבודק מפתוח caches ברמת
היחידה) בזרימות אמיתיות: RAG עם שני בסיסי ידע שונים, בידוד ברמת ה-HTTP
(שני sessions + IDOR), fail-loud בלי context, ובידוד schedulers.

ארבע הקטגוריות מסעיף 5.4 ב-spec, מותאמות לקוד כפי שהוא בפועל:
- ה-ContextVar מוגדר עם default=None; fail-loud קורה רק כש-TENANCY_STRICT
  דלוק (אחרת get_current_tenant נופל ל-DEFAULT_TENANT). הבדיקות מתעדות
  את שתי ההתנהגויות.
- כל tenant יושב על קובץ SQLite נפרד (tenant_db_path), ולכן בידוד ה-DB
  הוא פיזי; ה-caches בזיכרון ממופתחים לפי (tenant, ...).
"""

from contextlib import contextmanager
from unittest.mock import patch

import numpy as np
import pytest

import control_plane as cp
from rag.embeddings import _local_embedding
from tenancy import DEFAULT_TENANT, get_current_tenant, tenant_context


# ── עוזרי בדיקה ──────────────────────────────────────────────────────────

@contextmanager
def _platform(tmp_path):
    """סביבת פלטפורמה נקייה: DATA_DIR/DB_PATH זמניים + platform.db + default DB."""
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"), \
         patch("ai_chatbot.config.FAISS_INDEX_PATH", tmp_path / "faiss"):
        cp.invalidate_status_cache()
        from ai_chatbot import database as db
        db.init_db()  # ה-DB של ה-tenant של ברירת המחדל
        try:
            yield db
        finally:
            cp.invalidate_status_cache()


def _fake_embed(text):
    """embedding דטרמיניסטי offline (hash מקומי) — בלי קריאת רשת ל-OpenAI."""
    return _local_embedding(text)


def _fake_batch(texts):
    return np.array([_local_embedding(t) for t in texts], dtype=np.float32)


# ── בדיקה 1 — Fail-loud כשאין tenant context ─────────────────────────────

class TestFailLoudWithoutContext:
    def test_strict_mode_no_context_raises_on_get_current_tenant(self, monkeypatch):
        monkeypatch.setenv("TENANCY_STRICT", "true")
        from tenancy import MissingTenantContext, get_current_tenant as gct
        with pytest.raises(MissingTenantContext):
            gct()

    def test_strict_mode_no_context_raises_on_get_connection(self, monkeypatch, tmp_path):
        """שכבת ה-DB חייבת לזרוק לפני שהיא נוגעת בקובץ כלשהו."""
        monkeypatch.setenv("TENANCY_STRICT", "true")
        monkeypatch.setattr("ai_chatbot.config.DB_PATH", tmp_path / "default.db")
        from tenancy import MissingTenantContext
        from database import get_connection
        with pytest.raises(MissingTenantContext):
            with get_connection():
                pass  # pragma: no cover — לא אמורים להגיע לכאן

    def test_non_strict_falls_back_to_default(self, monkeypatch):
        """מתעד: בלי TENANCY_STRICT, קריאה בלי context נופלת בשקט ל-default.

        זו סטייה מהתכנון ("fail-loud תמיד") — הגנת ה-fail-loud היא opt-in.
        בפרודקשן חובה להדליק TENANCY_STRICT, אחרת נתיב ששכח לקבוע context
        יגיש בשקט את ה-DB של ה-tenant של ברירת המחדל.
        """
        monkeypatch.delenv("TENANCY_STRICT", raising=False)
        assert get_current_tenant() == DEFAULT_TENANT

    def test_strict_mode_with_context_still_works(self, tmp_path, monkeypatch):
        """strict לא שובר עבודה תקינה — עם context הכל עובד."""
        with _platform(tmp_path) as db:
            cp.create_tenant("salon-a", "א")
            monkeypatch.setenv("TENANCY_STRICT", "true")  # רק אחרי ההקמה
            with tenant_context("salon-a"):
                with db.get_connection() as conn:
                    assert conn.execute("SELECT 1").fetchone()[0] == 1


# ── בדיקה 2 — אין דליפת RAG/cache בין tenants (הכי קריטית) ────────────────

class TestRagRetrievalIsolationE2E:
    def test_same_question_returns_each_tenants_own_kb(self, tmp_path):
        """שני עסקים, בסיסי ידע שונים, אותה שאלה בדיוק — כל אחד מקבל את שלו.

        מריץ את הצינור האמיתי: add_kb_entry → rebuild_index (FAISS פר-tenant
        תחת tenant_faiss_dir) → retrieve (get_vector_store + _query_cache
        ממופתחים לפי tenant). אם ה-FAISS store או ה-query cache היו משותפים,
        עסק B היה מקבל את הכתובת של עסק A.
        """
        from rag import engine as eng
        from rag import vector_store as vsm

        with _platform(tmp_path) as db:
            cp.create_tenant("salon-a", "א")
            cp.create_tenant("salon-b", "ב")
            eng._query_cache.clear()
            vsm.reset_vector_store(all_tenants=True)

            # embeddings דטרמיניסטיים offline; מנטרלים את סף הרלוונטיות
            # הסמנטי (RAG_MIN_RELEVANCE) — אין לו משמעות עם hash מזויף,
            # והבדיקה כאן היא בידוד ה-store/cache ולא איכות הדמיון.
            with patch.object(eng, "get_embedding", _fake_embed), \
                 patch.object(eng, "get_embeddings_batch", _fake_batch), \
                 patch.object(vsm, "RAG_MIN_RELEVANCE", -1.0):
                with tenant_context("salon-a"):
                    db.add_kb_entry(
                        "Location", "כתובת",
                        "הכתובת שלנו היא רחוב רוטשילד 10. ROTHSCHILD_MARKER_A",
                    )
                    eng.rebuild_index()
                with tenant_context("salon-b"):
                    db.add_kb_entry(
                        "Location", "כתובת",
                        "הכתובת שלנו היא רחוב הרצל 20. HERZL_MARKER_B",
                    )
                    eng.rebuild_index()

                # אותה שאלה בדיוק — קודם A (ממלא cache), אחר כך B
                with tenant_context("salon-a"):
                    res_a = eng.retrieve("מה הכתובת של העסק?")
                with tenant_context("salon-b"):
                    res_b = eng.retrieve("מה הכתובת של העסק?")

            text_a = " ".join(r.get("text", "") for r in res_a)
            text_b = " ".join(r.get("text", "") for r in res_b)

            assert "ROTHSCHILD_MARKER_A" in text_a, "A לא קיבל את ה-KB של עצמו"
            assert "HERZL_MARKER_B" not in text_a, "דליפה! A ראה KB של B"
            # הבדיקה הקריטית — B אחרי ש-A מילא cache לאותה שאלה:
            assert "HERZL_MARKER_B" in text_b, "B לא קיבל את ה-KB של עצמו"
            assert "ROTHSCHILD_MARKER_A" not in text_b, "דליפה! B קיבל KB/cache של A"

            eng._query_cache.clear()
            vsm.reset_vector_store(all_tenants=True)

    def test_same_user_id_state_isolated_across_tenants(self, tmp_path):
        """אותו user_id מול שני עסקים — rate limiter, מכונת מצב, follow-up,
        pending-delete, ומנעולי summarize לא מתערבבים.

        מכסה caches שממופתחים (tenant, user_id) שלא נבדקו קצה-לקצה במקום אחר.
        """
        import rate_limiter as rl
        from llm import _lock_key
        from messaging import conversation_state as cs
        from messaging import whatsapp_privacy as wp

        user = "+972500000001"  # אותו אדם פונה לשני העסקים
        rl._user_timestamps.clear()
        cs._sessions.clear()
        wp._pending_deletes.clear()

        with tenant_context("salon-a"):
            for _ in range(4):
                rl.record_message(user)
            cs.set_state(user, cs.STATE_BOOKING_DATE, {"svc": "תספורת"})
            wp.register_pending_delete(user)
            lock_a = _lock_key(user)
        with tenant_context("salon-b"):
            rl.record_message(user)
            # אין דליפת מונה/מצב/מחיקה מ-A
            assert cs.get_state(user) is None
            assert wp.is_pending_delete(user) is False
            lock_b = _lock_key(user)

        assert len(rl._user_timestamps[("salon-a", user)]) == 4
        assert len(rl._user_timestamps[("salon-b", user)]) == 1
        assert lock_a == ("salon-a", user) and lock_b == ("salon-b", user)
        # A שומר על המצב שלו אחרי ש-B נגע
        with tenant_context("salon-a"):
            assert cs.get_state(user)["state"] == cs.STATE_BOOKING_DATE
            assert wp.is_pending_delete(user) is True

        rl._user_timestamps.clear()
        cs._sessions.clear()
        wp._pending_deletes.clear()


# ── בדיקה 3 — בידוד ברמת הפאנל (HTTP) ────────────────────────────────────

def _make_admin_app():
    import admin.app as admin_app
    with patch.object(admin_app, "ADMIN_SECRET_KEY", "test-secret"), \
         patch.object(admin_app, "ADMIN_USERNAME", "admin"), \
         patch.object(admin_app, "ADMIN_PASSWORD", "legacy-pw"):
        app = admin_app.create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


class TestHttpPanelIsolation:
    def _seed(self, db):
        cp.create_tenant("salon-a", "עסק א")
        cp.create_tenant("salon-b", "עסק ב")
        with tenant_context("salon-a"):
            db.upsert_user("1111", "MARKER_ALICE_A", "telegram")
        with tenant_context("salon-b"):
            db.upsert_user("2222", "MARKER_BOB_B", "telegram")

    def test_owner_cannot_reach_other_tenant_record_idor(self, tmp_path):
        """session של owner A לא מגיע לרשומה של B גם עם מזהה B מפורש."""
        with _platform(tmp_path) as db:
            self._seed(db)
            client = _make_admin_app().test_client()
            with client.session_transaction() as s:
                s["logged_in"] = True
                s["tenant_id"] = "salon-a"
                s["admin_role"] = "owner"

            # הלקוח של A נגיש ל-A
            r = client.get("/customers/1111")
            assert r.status_code == 200
            assert "MARKER_ALICE_A" in r.get_data(as_text=True)

            # מזהה של B — לא קיים ב-DB של A ⇒ "לקוח לא נמצא" (redirect), בלי נתוני B
            r = client.get("/customers/2222", follow_redirects=False)
            assert r.status_code == 302  # לא 200, לא נתוני B
            r = client.get("/customers/2222", follow_redirects=True)
            assert "MARKER_BOB_B" not in r.get_data(as_text=True)

    def test_context_does_not_leak_between_requests_same_client(self, tmp_path):
        """אותו thread/worker: בקשה כ-A ואז כ-B — כל אחת רואה רק את שלה.

        מאמת את זוג before_request/teardown_request (קביעה + reset של ה-
        ContextVar פר-בקשה) — הסיכון ב-threaded=True שבו threads ממוחזרים.
        """
        with _platform(tmp_path) as db:
            self._seed(db)
            client = _make_admin_app().test_client()

            with client.session_transaction() as s:
                s["logged_in"] = True
                s["tenant_id"] = "salon-a"
                s["admin_role"] = "owner"
            assert "MARKER_ALICE_A" in client.get("/customers/1111").get_data(as_text=True)

            # אותו client, מחליפים ל-B
            with client.session_transaction() as s:
                s["tenant_id"] = "salon-b"
            r_b = client.get("/customers/2222")
            assert r_b.status_code == 200
            assert "MARKER_BOB_B" in r_b.get_data(as_text=True)
            # A כבר לא נגיש תחת session של B (אין שיירים מהבקשה הקודמת)
            assert client.get("/customers/1111", follow_redirects=False).status_code == 302

    def test_owner_cannot_reach_platform_screen(self, tmp_path):
        """מסך הפלטפורמה גדור ב-platform_admin_required (404 ל-owner)."""
        with _platform(tmp_path) as db:
            self._seed(db)
            client = _make_admin_app().test_client()
            with client.session_transaction() as s:
                s["logged_in"] = True
                s["tenant_id"] = "salon-a"
                s["admin_role"] = "owner"
            assert client.get("/platform").status_code == 404


# ── בדיקה 4 — Jobs מתוזמנים מבודדים ──────────────────────────────────────

class TestSchedulerIsolation:
    def test_memory_scheduler_per_tenant_context_and_failure_isolation(self, tmp_path):
        """ה-scheduler של הזיכרון מעבד כל tenant פעיל בהקשר שלו, מדלג על
        מושעה, וכשל אצל אחד לא עוצר את השאר (try/except פר-tenant בתוך הלולאה)."""
        from memory import background as mb

        with _platform(tmp_path):
            cp.create_tenant("salon-a", "א")
            cp.create_tenant("salon-b", "ב")
            cp.create_tenant("salon-c", "ג")
            cp.set_tenant_status("salon-b", "suspended")

            seen: list[str] = []

            def fake_process():
                t = get_current_tenant()
                seen.append(t)
                if t == "salon-a":
                    raise RuntimeError("boom")  # כשל אצל א' לא צריך לעצור את ג'

            with patch.object(mb, "_process_due_users", side_effect=fake_process):
                mb._scheduler_stop.clear()

                def stop_after_first(timeout=None):
                    mb._scheduler_stop.set()
                    return True

                with patch.object(mb._scheduler_stop, "wait", stop_after_first):
                    mb._scheduler_loop()
                mb._scheduler_stop.set()

            assert set(seen) == {"salon-a", "salon-c"}  # שניהם עובדו למרות הכשל
            assert "salon-b" not in seen  # מושעה לא נכלל

    def test_schedulable_tenants_excludes_default_once_registered(self, tmp_path):
        """מתעד ממצא: ברגע שנרשם tenant אחד, ה-default יוצא ממחזור ה-
        schedulers הפלטפורמתיים (memory/broadcast/backup/calendar).

        משמעות: (1) לקוח פלטפורמה מקבל את ה-jobs האלה; (2) ה-tenant של
        ברירת המחדל **לא** מעובד על ידם יותר — כולל שהגיבוי לא מכסה את
        קובץ ה-DB שלו. תקין בהנחה שפריסה היא או-legacy-או-פלטפורמה, אבל
        חשוב לדעת בפריסה מעורבת.
        """
        from control_plane import list_schedulable_tenant_ids

        with _platform(tmp_path):
            # אין רישום → מצב legacy: default בלבד
            assert list_schedulable_tenant_ids() == [DEFAULT_TENANT]
            # נרשם tenant → default יוצא, רק active נכללים
            cp.create_tenant("salon-a", "א")
            ids = list_schedulable_tenant_ids()
            assert ids == ["salon-a"]
            assert DEFAULT_TENANT not in ids
