"""טסטים לסינון תבניות broadcast: קטגוריות מרובות + החרגת תבניות שיחה
דינמיות (`*_<16hex>`)."""

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


def _seed(db, sid, name, category="MARKETING", status="approved"):
    db.upsert_whatsapp_template({
        "content_sid": sid,
        "friendly_name": name,
        "language": "he",
        "category": category,
        "approval_status": status,
        "body_text": "x",
        "variables": [],
    })


# ── is_internal_conversation_template ────────────────────────────────────────


class TestIsInternalConversationTemplate:
    def test_recognizes_hash_suffix(self, db):
        assert db.is_internal_conversation_template(
            "welcome_menu_a1b2c3d4e5f60718",
        ) is True

    def test_rejects_clean_names(self, db):
        for name in ("welcome_menu", "appointment_reminder",
                     "marketing_promo_2026", ""):
            assert db.is_internal_conversation_template(name) is False

    def test_rejects_short_or_uppercase_hex(self, db):
        # פחות מ-16 תווים → לא תבנית פנימית
        assert db.is_internal_conversation_template("foo_abc123") is False
        # אותיות גדולות → לא תואם (hash תמיד lowercase)
        assert db.is_internal_conversation_template("foo_A1B2C3D4E5F60718") is False

    def test_handles_none(self, db):
        assert db.is_internal_conversation_template(None) is False


# ── list_whatsapp_templates: category as sequence ────────────────────────────


class TestCategoryFilter:
    def test_single_category_string(self, db):
        _seed(db, "HX1", "promo", category="MARKETING")
        _seed(db, "HX2", "reminder", category="UTILITY")
        rows = db.list_whatsapp_templates(category="MARKETING")
        assert {r["content_sid"] for r in rows} == {"HX1"}

    def test_multiple_categories_via_list(self, db):
        _seed(db, "HX1", "promo", category="MARKETING")
        _seed(db, "HX2", "reminder", category="UTILITY")
        _seed(db, "HX3", "otp", category="AUTHENTICATION")
        _seed(db, "HX4", "unknown_one", category="UNKNOWN")
        rows = db.list_whatsapp_templates(
            category=["MARKETING", "UTILITY", "AUTHENTICATION"],
        )
        assert {r["content_sid"] for r in rows} == {"HX1", "HX2", "HX3"}

    def test_empty_sequence_treated_as_no_filter(self, db):
        _seed(db, "HX1", "promo", category="MARKETING")
        rows = db.list_whatsapp_templates(category=[])
        assert len(rows) == 1


# ── exclude_internal ─────────────────────────────────────────────────────────


class TestExcludeInternal:
    def test_excludes_hash_suffixed_names(self, db):
        _seed(db, "HX1", "welcome_menu_abcdef0123456789", category="UTILITY")
        _seed(db, "HX2", "appointment_reminder", category="UTILITY")
        rows = db.list_whatsapp_templates(exclude_internal=True)
        assert {r["content_sid"] for r in rows} == {"HX2"}

    def test_default_includes_internal(self, db):
        _seed(db, "HX1", "welcome_menu_abcdef0123456789", category="UTILITY")
        rows = db.list_whatsapp_templates()
        assert len(rows) == 1

    def test_combined_with_category(self, db):
        _seed(db, "HX1", "promo", category="MARKETING")
        _seed(db, "HX2", "qr_abcdef0123456789", category="UTILITY")
        _seed(db, "HX3", "appointment", category="UTILITY")
        rows = db.list_whatsapp_templates(
            category=["MARKETING", "UTILITY"],
            exclude_internal=True,
        )
        assert {r["content_sid"] for r in rows} == {"HX1", "HX3"}


# ── count helpers ────────────────────────────────────────────────────────────


class TestCounts:
    def test_count_by_status_respects_category(self, db):
        _seed(db, "HX1", "promo", category="MARKETING", status="approved")
        _seed(db, "HX2", "u1", category="UTILITY", status="approved")
        _seed(db, "HX3", "u2", category="UTILITY", status="pending")
        counts = db.count_whatsapp_templates_by_status(category="MARKETING")
        assert counts == {"approved": 1}

    def test_count_by_status_excludes_internal(self, db):
        _seed(db, "HX1", "qr_abcdef0123456789", category="UTILITY", status="approved")
        _seed(db, "HX2", "real_template", category="UTILITY", status="approved")
        counts = db.count_whatsapp_templates_by_status(exclude_internal=True)
        assert counts == {"approved": 1}

    def test_count_by_category(self, db):
        _seed(db, "HX1", "a", category="MARKETING")
        _seed(db, "HX2", "b", category="MARKETING")
        _seed(db, "HX3", "c", category="UTILITY")
        cats = db.count_whatsapp_templates_by_category()
        assert cats["MARKETING"] == 2
        assert cats["UTILITY"] == 1

    def test_count_by_category_excludes_internal(self, db):
        _seed(db, "HX1", "qr_abcdef0123456789", category="UTILITY")
        _seed(db, "HX2", "real_promo", category="MARKETING")
        cats = db.count_whatsapp_templates_by_category(exclude_internal=True)
        assert cats.get("UTILITY", 0) == 0
        assert cats["MARKETING"] == 1


# ── BROADCAST_TEMPLATE_CATEGORIES constant ───────────────────────────────────


class TestBroadcastCategoriesConstant:
    def test_contains_expected(self, db):
        assert set(db.BROADCAST_TEMPLATE_CATEGORIES) == {
            "MARKETING", "UTILITY", "AUTHENTICATION",
        }
