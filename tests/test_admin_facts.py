"""
טסטים למסכי ניהול facts בפאנל (שלב 7).

מכסה את 3 המסכים: תור אישור, רשימת לקוחות, פרטי לקוחה. בודק שה-DB
מתעדכן נכון אחרי approve/reject/edit/delete, שתמיכת BSUID + טלפון
עובדת ב-URL routing, וש-empty states מוצגים נכון.

הדפוס מבוסס על tests/test_admin_business_profile.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "test_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test_pass_for_unit_tests_only")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests")

    import importlib
    for mod_name in ("ai_chatbot.config", "admin.app"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "ADMIN_USERNAME"):
            monkeypatch.setattr(mod, "ADMIN_USERNAME", "test_admin", raising=False)
        if hasattr(mod, "ADMIN_PASSWORD"):
            monkeypatch.setattr(mod, "ADMIN_PASSWORD",
                                "test_pass_for_unit_tests_only", raising=False)
        if hasattr(mod, "ADMIN_SECRET_KEY"):
            monkeypatch.setattr(mod, "ADMIN_SECRET_KEY",
                                "test-secret-key-for-unit-tests", raising=False)


@pytest.fixture
def client(db_conn):
    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "test_admin"
        yield c


def _seed(**overrides) -> int:
    """seed fact ל-DB טסט."""
    from database import insert_customer_fact
    base = {
        "user_id": "u1", "business_id": "default",
        "fact_type": "preference", "content": "default content",
        "confidence": 0.9, "status": "active",
    }
    base.update(overrides)
    return insert_customer_fact(base)


# ────────────────────────────────────────────────────────────────────
# /pending-facts
# ────────────────────────────────────────────────────────────────────


class TestPendingFactsPage:
    def test_shows_only_pending(self, client, db_conn):
        _seed(content="active_one", status="active")
        _seed(content="pending_one", status="pending_approval")
        _seed(content="pending_two", status="pending_approval",
              fact_type="personal_info")
        _seed(content="rejected_one", status="rejected", fact_type="vocabulary")

        resp = client.get("/pending-facts")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "pending_one" in body
        assert "pending_two" in body
        assert "active_one" not in body
        assert "rejected_one" not in body

    def test_empty_state(self, client, db_conn):
        # רק active — תור אישור ריק
        _seed(content="x", status="active")
        resp = client.get("/pending-facts")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "אין עובדות הממתינות לאישור" in body

    def test_approve_all_button_hidden_when_empty(self, client, db_conn):
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        assert "אשר הכל" not in body

    def test_approve_all_button_shown_with_count(self, client, db_conn):
        _seed(content="p1", status="pending_approval")
        _seed(content="p2", status="pending_approval", fact_type="personal_info")
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        assert "אשר הכל (2)" in body


class TestApproveReject:
    def test_approve_changes_status_to_active(self, client, db_conn):
        fid = _seed(content="x", status="pending_approval")
        resp = client.post(f"/pending-facts/{fid}/approve")
        # redirect (לא HTMX request)
        assert resp.status_code in (302, 303)
        # DB עודכן
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "active"

    def test_reject_changes_status_to_rejected(self, client, db_conn):
        fid = _seed(content="x", status="pending_approval")
        resp = client.post(f"/pending-facts/{fid}/reject")
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "rejected"

    def test_approve_htmx_returns_partial_with_state(self, client, db_conn):
        fid = _seed(content="approved_content", status="pending_approval")
        resp = client.post(
            f"/pending-facts/{fid}/approve",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "מאושר" in body
        assert "approved_content" in body

    def test_approve_nonexistent_404_for_htmx(self, client):
        resp = client.post(
            "/pending-facts/99999/approve",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_approve_all_bulk(self, client, db_conn):
        for i in range(5):
            _seed(content=f"p{i}", status="pending_approval",
                  fact_type="vocabulary")
        resp = client.post("/pending-facts/approve-all")
        assert resp.status_code in (302, 303)
        # כולם עברו ל-active
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM customer_facts WHERE status='active'",
        ).fetchone()["c"]
        assert count == 5
        pending = db_conn.execute(
            "SELECT COUNT(*) AS c FROM customer_facts "
            "WHERE status='pending_approval'",
        ).fetchone()["c"]
        assert pending == 0


# ────────────────────────────────────────────────────────────────────
# /customer-memory
# ────────────────────────────────────────────────────────────────────


class TestCustomerMemoryList:
    def test_shows_only_users_with_facts(self, client, db_conn):
        _seed(user_id="u_with_facts", content="x", status="active")
        # user שאין לו facts (אבל כן רשום ב-users) — לא יופיע
        with db_conn:
            db_conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                ("u_no_facts", "Nobody"),
            )
        resp = client.get("/customer-memory")
        body = resp.data.decode("utf-8")
        assert "u_with_facts" in body
        assert "Nobody" not in body

    def test_empty_state(self, client, db_conn):
        resp = client.get("/customer-memory")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "עדיין לא הצטברו עובדות" in body


class TestCustomerMemoryDetail:
    def test_groups_facts_by_type(self, client, db_conn):
        _seed(user_id="111111111", content="prefers mornings",
              fact_type="preference", status="active")
        _seed(user_id="111111111", content="works at Acme",
              fact_type="personal_info", status="active")
        resp = client.get("/customer-memory/111111111")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "prefers mornings" in body
        assert "works at Acme" in body
        # תוויות הסקציות בעברית (translate_fact_type)
        assert "העדפה" in body
        assert "מידע אישי" in body

    def test_phone_user_id_with_plus_works(self, client, db_conn):
        """רגרסיה: user_id `+972...` עובר urlencode → ' 972...' → normalize
        → '+972...' לא קורס."""
        _seed(user_id="+972526915503", content="x", status="active")
        # שולחים את ה-+ עם encoding %2B כדי לדמות לינק נכון
        resp = client.get("/customer-memory/%2B972526915503")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "x" in body

    def test_bsuid_user_id_works(self, client, db_conn):
        """תמיכת BSUID (Meta WhatsApp, סוף 2026) — `IL.abc...` ב-URL."""
        _seed(user_id="IL.abc123XYZ", content="hello", status="active")
        resp = client.get("/customer-memory/IL.abc123XYZ")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "hello" in body

    def test_empty_state_for_user_without_facts(self, client, db_conn):
        """user_id שעובר ולידציה אבל אין לו facts — empty state."""
        resp = client.get("/customer-memory/123456789")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "אין עדיין עובדות" in body

    def test_invalid_user_id_returns_400_for_htmx(self, client):
        """user_id לא חוקי → 400 ב-HTMX (decorator מחזיר 400 רק עם HX-Request;
        בלעדיו עושה flash+redirect, דפוס הקיים בפאנל)."""
        resp = client.get(
            "/customer-memory/!!!invalid!!!",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


class TestEditCustomerFact:
    def test_edit_updates_content(self, client, db_conn):
        fid = _seed(user_id="111111111", content="old text", status="active")
        resp = client.post(
            f"/customer-memory/111111111/{fid}/edit",
            data={"content": "new text"},
        )
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT content FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["content"] == "new text"

    def test_edit_empty_content_rejected(self, client, db_conn):
        fid = _seed(user_id="111111111", content="original", status="active")
        resp = client.post(
            f"/customer-memory/111111111/{fid}/edit",
            data={"content": "   "},
        )
        # ל-non-HTMX → redirect + flash. ל-HTMX → 400.
        assert resp.status_code in (302, 303, 400)
        # התוכן לא השתנה
        row = db_conn.execute(
            "SELECT content FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["content"] == "original"

    def test_edit_user_id_mismatch_rejected(self, client, db_conn):
        """fact של u1, ניסיון עריכה דרך URL של u2 → לא משנה כלום
        (URL forgery defense)."""
        fid = _seed(user_id="111111111", content="original", status="active")
        resp = client.post(
            f"/customer-memory/222222222/{fid}/edit",
            data={"content": "hacked"},
        )
        assert resp.status_code in (302, 303, 404)
        row = db_conn.execute(
            "SELECT content FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["content"] == "original"


class TestDeleteCustomerFact:
    def test_delete_removes_from_db(self, client, db_conn):
        fid = _seed(user_id="111111111", content="to delete", status="active")
        resp = client.post(f"/customer-memory/111111111/{fid}/delete")
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT id FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row is None

    def test_delete_htmx_returns_empty(self, client, db_conn):
        fid = _seed(user_id="111111111", content="x", status="active")
        resp = client.post(
            f"/customer-memory/111111111/{fid}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.data == b""

    def test_delete_user_id_mismatch_rejected(self, client, db_conn):
        """delete דרך user_id לא נכון לא מוחק."""
        fid = _seed(user_id="111111111", content="protected", status="active")
        resp = client.post(f"/customer-memory/222222222/{fid}/delete")
        # לא נמחק
        row = db_conn.execute(
            "SELECT id FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row is not None


# ────────────────────────────────────────────────────────────────────
# /api/stats — pending_facts ב-response
# ────────────────────────────────────────────────────────────────────


class TestApiStatsPendingFacts:
    def test_pending_facts_count_in_stats(self, client, db_conn):
        _seed(content="p1", status="pending_approval")
        _seed(content="p2", status="pending_approval", fact_type="personal_info")
        _seed(content="a1", status="active")  # לא נספר
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["pending_facts"] == 2

    def test_pending_facts_zero_when_none(self, client, db_conn):
        _seed(content="a", status="active")
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["pending_facts"] == 0


# ────────────────────────────────────────────────────────────────────
# שלב 7.1 — תיקוני Cursor bot: compare-and-swap + COUNT + CSRF
# ────────────────────────────────────────────────────────────────────


class TestApproveRejectIdempotency:
    """approve/reject על fact שכבר אושר/נדחה — לא משנה כלום (race-safe)."""

    def test_approve_already_active_no_change(self, client, db_conn):
        fid = _seed(content="x", status="active")
        resp = client.post(f"/pending-facts/{fid}/approve")
        # non-HTMX → redirect עם flash
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "active"

    def test_approve_already_active_htmx_returns_404(self, client, db_conn):
        fid = _seed(content="x", status="active")
        resp = client.post(
            f"/pending-facts/{fid}/approve",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_reject_already_rejected_no_change(self, client, db_conn):
        fid = _seed(content="x", status="rejected", fact_type="vocabulary")
        resp = client.post(f"/pending-facts/{fid}/reject")
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "rejected"


class TestPendingFactsTotalCount:
    """כפתור 'אשר הכל' מציג את ה-COUNT האמיתי (לא facts|length החסום ל-200)."""

    def test_button_shows_total_pending(self, client, db_conn):
        for i in range(3):
            _seed(content=f"p{i}", status="pending_approval",
                  fact_type="vocabulary")
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        assert "אשר הכל (3)" in body

    def test_button_hidden_when_no_pending(self, client, db_conn):
        _seed(content="x", status="active")
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        assert "אשר הכל" not in body

    def test_total_pending_exceeds_displayed(self, client, db_conn):
        """אם > 200 pending, הטבלה מציגה 200 אבל ה-button את המספר האמיתי.
        בנוסף מופיע ה-alert של "מציג X מתוך Y"."""
        for i in range(205):
            _seed(content=f"p{i}", status="pending_approval",
                  fact_type="vocabulary")
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        assert "אשר הכל (205)" in body
        assert "מציג 200 מתוך 205" in body


class TestStatsPendingFactsBusinessId:
    """/api/stats קורא ל-get_pending_facts_count(BUSINESS_ID), לא ל-default."""

    def test_stats_counts_only_current_business(self, client, db_conn, monkeypatch):
        # 2 pending ב-default, 5 pending בעסק אחר
        for i in range(2):
            _seed(content=f"a{i}", status="pending_approval",
                  fact_type="vocabulary", business_id="default")
        for i in range(5):
            _seed(content=f"b{i}", status="pending_approval",
                  fact_type="vocabulary", business_id="other_biz")
        # BUSINESS_ID default ב-fixture → count=2
        resp = client.get("/api/stats")
        assert resp.get_json()["pending_facts"] == 2

    def test_stats_not_capped_at_200(self, client, db_conn):
        """get_pending_facts חסום ל-200, get_pending_facts_count לא.
        ה-badge חייב להראות את המספר האמיתי."""
        for i in range(205):
            _seed(content=f"p{i}", status="pending_approval",
                  fact_type="vocabulary")
        resp = client.get("/api/stats")
        assert resp.get_json()["pending_facts"] == 205


class TestApproveAllCSRFFormHasToken:
    """באג 1 (HIGH): טופס approve-all הוא POST רגיל (לא HTMX) → חייב
    csrf_token hidden input. CSRFProtect מופעל גלובלית ב-admin/app.py:602.
    """

    def test_approve_all_form_includes_csrf_token(self, client, db_conn):
        _seed(content="p1", status="pending_approval")
        resp = client.get("/pending-facts")
        body = resp.data.decode("utf-8")
        # ה-csrf_token משולב כ-hidden input בתוך הטופס
        assert 'name="csrf_token"' in body
        # ה-action של הטופס
        assert 'action="/pending-facts/approve-all"' in body


# ────────────────────────────────────────────────────────────────────
# שלב 7.2 — תיקוני Cursor: IntegrityError + business_id guard + JS cancel
# ────────────────────────────────────────────────────────────────────


class TestApproveDuplicateActiveIntegrity:
    """ה-UNIQUE partial index idx_customer_facts_active_unique מונע
    2 facts active עם אותו (user_id, business_id, fact_type, content).
    אישור pending עם תאום active קיים → 500 לפני התיקון, flash אחרי.
    """

    def test_single_approve_duplicate_returns_flash_not_500(self, client, db_conn):
        # active קיים
        _seed(user_id="111111111", content="prefers tea", status="active")
        # pending עם אותו content + fact_type + user_id
        fid = _seed(user_id="111111111", content="prefers tea",
                    status="pending_approval")
        resp = client.post(f"/pending-facts/{fid}/approve")
        # redirect ל-pending_facts (לא 500)
        assert resp.status_code in (302, 303)
        # ה-pending נשאר pending (לא נדרס)
        row = db_conn.execute(
            "SELECT status FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["status"] == "pending_approval"

    def test_single_approve_duplicate_htmx_returns_409(self, client, db_conn):
        _seed(user_id="111111111", content="dup", status="active")
        fid = _seed(user_id="111111111", content="dup",
                    status="pending_approval")
        resp = client.post(
            f"/pending-facts/{fid}/approve",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409
        # HX-Reswap=none כדי שה-tr לא יוחלף בטקסט; toast דרך HX-Trigger
        assert resp.headers.get("HX-Reswap") == "none"
        assert "showToast" in (resp.headers.get("HX-Trigger") or "")
        # הגוף ריק (לא טקסט שיוזרק לטבלה)
        assert resp.data == b""

    def test_approve_all_skips_duplicates(self, client, db_conn):
        """UPDATE OR IGNORE — כפילויות נשארות pending, אחרים מתאשרים."""
        # 1 active קיים
        _seed(user_id="111111111", content="existing", status="active")
        # 1 pending שתואם ל-active (לא יאושר)
        _seed(user_id="111111111", content="existing", status="pending_approval")
        # 2 pending נקיים (יאושרו)
        _seed(user_id="111111111", content="new_a",
              status="pending_approval", fact_type="vocabulary")
        _seed(user_id="111111111", content="new_b",
              status="pending_approval", fact_type="personal_info")

        resp = client.post("/pending-facts/approve-all")
        assert resp.status_code in (302, 303)

        # שני ה-new אושרו
        active_count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM customer_facts WHERE status='active'",
        ).fetchone()["c"]
        assert active_count == 3  # existing + new_a + new_b

        # הכפילות נשארה pending
        pending_count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM customer_facts "
            "WHERE status='pending_approval'",
        ).fetchone()["c"]
        assert pending_count == 1


class TestEditDuplicateActiveIntegrity:
    """edit_customer_fact: עריכת content על fact active כך שתואם ל-active
    אחר → IntegrityError. תופסים, flash, לא 500."""

    def test_edit_to_duplicate_returns_flash_not_500(self, client, db_conn):
        _seed(user_id="111111111", content="existing", status="active")
        # fact אחר שאחרי עריכה יתאים
        fid = _seed(user_id="111111111", content="different",
                    status="active", fact_type="preference")
        # ניסיון לערוך אותו ל-"existing" → התנגשות
        resp = client.post(
            f"/customer-memory/111111111/{fid}/edit",
            data={"content": "existing"},
        )
        assert resp.status_code in (302, 303)
        # ה-content לא השתנה (rollback)
        row = db_conn.execute(
            "SELECT content FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["content"] == "different"

    def test_edit_to_duplicate_htmx_returns_409(self, client, db_conn):
        _seed(user_id="111111111", content="dup", status="active")
        fid = _seed(user_id="111111111", content="orig",
                    status="active", fact_type="preference")
        resp = client.post(
            f"/customer-memory/111111111/{fid}/edit",
            data={"content": "dup"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409
        # ה-tr המקורי בטבלה לא יוחלף — toast במקום
        assert resp.headers.get("HX-Reswap") == "none"
        assert "showToast" in (resp.headers.get("HX-Trigger") or "")
        assert resp.data == b""


class TestEditDeleteBusinessIdGuard:
    """edit/delete חייבים לכלול business_id ב-SELECT, אחרת admin של עסק A
    יכול לערוך/למחוק fact של עסק B אם הוא יודע id+user_id."""

    def test_edit_other_business_blocked(self, client, db_conn):
        # fact בעסק "other_biz" — לא תהיה ניתנת לעריכה תחת BUSINESS_ID=default
        fid = _seed(user_id="111111111", content="other_biz_content",
                    status="active", business_id="other_biz")
        resp = client.post(
            f"/customer-memory/111111111/{fid}/edit",
            data={"content": "hacked"},
        )
        assert resp.status_code in (302, 303, 404)
        # התוכן לא השתנה
        row = db_conn.execute(
            "SELECT content FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row["content"] == "other_biz_content"

    def test_delete_other_business_blocked(self, client, db_conn):
        fid = _seed(user_id="111111111", content="x",
                    status="active", business_id="other_biz")
        resp = client.post(f"/customer-memory/111111111/{fid}/delete")
        # ה-fact עדיין קיים
        row = db_conn.execute(
            "SELECT id FROM customer_facts WHERE id=?", (fid,),
        ).fetchone()
        assert row is not None


class TestCancelButtonRestoresEditButton:
    """Low: ביטול בטופס עריכה צריך להחזיר את כפתור 'ערוך' להיות גלוי
    כדי שהמשתמש יוכל לפתוח את הטופס שוב בלי refresh."""

    def test_template_has_id_on_edit_button_and_restore_in_cancel(
        self, client, db_conn,
    ):
        fid = _seed(user_id="111111111", content="x", status="active")
        resp = client.get("/customer-memory/111111111")
        body = resp.data.decode("utf-8")
        # ה-button של "ערוך" קיבל id ייחודי
        assert f'id="fact-edit-btn-{fid}"' in body
        # ה-onclick של "ביטול" מחזיר את ה-display שלו
        assert f"getElementById('fact-edit-btn-{fid}').style.display=''" in body
