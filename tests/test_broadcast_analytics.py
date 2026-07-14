"""
טסטים ל-שלב 8 — analytics: get_error_breakdown, get_analytics_summary, CSV.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


def _make_approved_template(db, content_sid="HX_T1", category="UTILITY"):
    db.upsert_whatsapp_template({
        "content_sid": content_sid, "friendly_name": "t",
        "language": "he", "category": category,
        "approval_status": "approved", "body_text": "x",
        "variables": [],
    })


# ── get_error_breakdown ──────────────────────────────────────────────────────


class TestErrorBreakdown:
    def test_aggregates_by_error_code(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")

        # 3 × 63016, 2 × 21408, 1 × undelivered
        for i in range(3):
            did, _ = db.create_delivery_queue(cid, f"+97250{i:07d}", {})
            db.mark_delivery_failed(did, "63016", "not on WhatsApp")
        for i in range(2):
            did, _ = db.create_delivery_queue(cid, f"+97260{i:07d}", {})
            db.mark_delivery_failed(did, "21408", "invalid")
        did, _ = db.create_delivery_queue(cid, "+972701000001", {})
        db.mark_delivery_sent(did, "SM_U")
        db.update_delivery_status_by_twilio_sid(
            "SM_U", "undelivered", error_code="63024", error_message="blocked",
        )

        breakdown = db.get_error_breakdown(cid)
        assert len(breakdown) == 3
        # ממוין לפי count יורד
        assert breakdown[0]["error_code"] == "63016"
        assert breakdown[0]["count"] == 3
        assert breakdown[1]["error_code"] == "21408"
        assert breakdown[1]["count"] == 2
        assert breakdown[2]["error_code"] == "63024"
        assert breakdown[2]["count"] == 1

    def test_empty_error_code_becomes_unknown(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_failed(did, "", "no code")

        breakdown = db.get_error_breakdown(cid)
        assert len(breakdown) == 1
        assert breakdown[0]["error_code"] == "UNKNOWN"

    def test_sent_deliveries_not_in_breakdown(self, db):
        """רק failed/undelivered נכללים — לא delivered/read."""
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        did, _ = db.create_delivery_queue(cid, "+972501000001", {})
        db.mark_delivery_sent(did, "SM_OK")
        db.update_delivery_status_by_twilio_sid("SM_OK", "delivered")

        assert db.get_error_breakdown(cid) == []

    def test_empty_campaign_returns_empty(self, db):
        _make_approved_template(db)
        cid = db.create_broadcast_campaign(template_sid="HX_T1")
        assert db.get_error_breakdown(cid) == []


# ── get_analytics_summary ────────────────────────────────────────────────────


class TestAnalyticsSummary:
    def test_by_status_aggregates_correctly(self, db):
        _make_approved_template(db)
        c1 = db.create_broadcast_campaign(template_sid="HX_T1")  # draft
        c2 = db.create_broadcast_campaign(template_sid="HX_T1")
        c3 = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_status(c2, "completed")
        db.set_campaign_status(c3, "completed")

        summary = db.get_broadcast_analytics_summary()
        assert summary["by_status"]["draft"] == 1
        assert summary["by_status"]["completed"] == 2
        assert summary["total_campaigns"] == 3

    def test_totals_aggregate_counters(self, db):
        _make_approved_template(db)
        c1 = db.create_broadcast_campaign(template_sid="HX_T1")
        c2 = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_counters(c1, {
            "total_recipients": 100, "sent": 80, "delivered": 70,
            "read": 30, "failed": 5,
        })
        db.set_campaign_status(c1, "completed")
        db.set_campaign_counters(c2, {
            "total_recipients": 50, "sent": 45, "delivered": 40,
            "read": 20, "failed": 2,
        })
        db.set_campaign_status(c2, "completed")

        summary = db.get_broadcast_analytics_summary()
        t = summary["totals"]
        assert t["total_recipients"] == 150
        assert t["total_sent"] == 125
        assert t["total_delivered"] == 110
        assert t["total_read"] == 50
        assert t["total_failed"] == 7

    def test_top_errors_cross_campaign(self, db):
        _make_approved_template(db)
        # 3 campaigns, שגיאה 63016 מופיעה ב-2 מהם (2+1), 21408 מופיעה ב-1 (3)
        c1 = db.create_broadcast_campaign(template_sid="HX_T1")
        c2 = db.create_broadcast_campaign(template_sid="HX_T1")
        for i in range(2):
            did, _ = db.create_delivery_queue(c1, f"+97250{i:07d}", {})
            db.mark_delivery_failed(did, "63016", "x")
        did, _ = db.create_delivery_queue(c2, "+972601000001", {})
        db.mark_delivery_failed(did, "63016", "x")
        for i in range(3):
            did, _ = db.create_delivery_queue(c2, f"+97270{i:07d}", {})
            db.mark_delivery_failed(did, "21408", "y")

        summary = db.get_broadcast_analytics_summary()
        top = summary["top_errors"]
        # 21408 (3) מוביל, 63016 (3) אחריו
        errors = {e["error_code"]: e["count"] for e in top}
        assert errors["21408"] == 3
        assert errors["63016"] == 3

    def test_empty_db_returns_zeros(self, db):
        summary = db.get_broadcast_analytics_summary()
        assert summary["total_campaigns"] == 0
        assert summary["totals"]["total_recipients"] == 0
        assert summary["top_errors"] == []

    def test_draft_campaigns_excluded_from_totals(self, db):
        """Drafts לא נספרים במונים הכוללים (הם לא שודרו)."""
        _make_approved_template(db)
        c_draft = db.create_broadcast_campaign(template_sid="HX_T1")
        db.set_campaign_counters(c_draft, {
            "total_recipients": 999, "sent": 0, "delivered": 0,
            "read": 0, "failed": 0,
        })
        # משאירים ב-draft
        summary = db.get_broadcast_analytics_summary()
        assert summary["totals"]["total_recipients"] == 0
