"""
טסטים ל-messaging/whatsapp_templates_sync.py ולמערך ה-helpers של תבניות
WhatsApp ב-database.py.

מוקים מחליפים את Twilio — אין קריאות HTTP אמיתיות.
"""

from unittest.mock import patch

import pytest


# ── fixture: DB נקי לכל טסט ──────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """מאתחל DB בקובץ זמני ומחזיר את מודול database מוכן."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── טסטים ל-helpers ב-database.py ────────────────────────────────────────────


class TestWhatsAppTemplateDBHelpers:
    def test_upsert_minimal_required_fields(self, db):
        db.upsert_whatsapp_template({
            "content_sid": "HX111",
            "friendly_name": "order_update",
        })
        row = db.get_whatsapp_template("HX111")
        assert row is not None
        assert row["friendly_name"] == "order_update"
        assert row["language"] == "he"
        assert row["approval_status"] == "unsubmitted"
        assert row["buttons"] == []
        assert row["variables"] == []

    def test_upsert_full_fields_decodes_json(self, db):
        db.upsert_whatsapp_template({
            "content_sid": "HX222",
            "friendly_name": "appointment_reminder",
            "language": "he",
            "category": "UTILITY",
            "approval_status": "approved",
            "header_type": "text",
            "body_text": "היי {{1}}, תור ל-{{2}} ב-{{3}}",
            "footer_text": "להסרה השב הסר",
            "buttons": [{"type": "quick_reply", "title": "אישור", "id": "confirm"}],
            "variables": [
                {"index": "1", "name": "customer_name", "example": "דני"},
                {"index": "2", "name": "service", "example": "תספורת"},
                {"index": "3", "name": "time", "example": "14:00"},
            ],
            "raw": {"types": {"twilio/quick-reply": {"body": "..."}}},
        })
        row = db.get_whatsapp_template("HX222")
        assert row["body_text"].startswith("היי")
        assert row["footer_text"] == "להסרה השב הסר"
        assert len(row["variables"]) == 3
        assert row["variables"][0]["example"] == "דני"
        assert row["buttons"][0]["title"] == "אישור"

    def test_upsert_updates_existing_sid(self, db):
        db.upsert_whatsapp_template({
            "content_sid": "HX333",
            "friendly_name": "old_name",
            "approval_status": "pending",
        })
        db.upsert_whatsapp_template({
            "content_sid": "HX333",
            "friendly_name": "new_name",
            "approval_status": "approved",
        })
        row = db.get_whatsapp_template("HX333")
        assert row["friendly_name"] == "new_name"
        assert row["approval_status"] == "approved"

    def test_upsert_requires_content_sid(self, db):
        with pytest.raises(ValueError, match="content_sid"):
            db.upsert_whatsapp_template({"friendly_name": "x"})

    def test_upsert_requires_friendly_name(self, db):
        with pytest.raises(ValueError, match="friendly_name"):
            db.upsert_whatsapp_template({"content_sid": "HX1"})

    def test_list_with_filters(self, db):
        db.upsert_whatsapp_template({
            "content_sid": "HX_A", "friendly_name": "a",
            "language": "he", "approval_status": "approved", "category": "UTILITY",
        })
        db.upsert_whatsapp_template({
            "content_sid": "HX_B", "friendly_name": "b",
            "language": "en", "approval_status": "approved", "category": "MARKETING",
        })
        db.upsert_whatsapp_template({
            "content_sid": "HX_C", "friendly_name": "c",
            "language": "he", "approval_status": "pending", "category": "UTILITY",
        })

        he_approved = db.list_whatsapp_templates(approval_status="approved", language="he")
        assert len(he_approved) == 1
        assert he_approved[0]["content_sid"] == "HX_A"

        all_approved = db.list_whatsapp_templates(approval_status="approved")
        assert len(all_approved) == 2

        marketing = db.list_whatsapp_templates(category="MARKETING")
        assert len(marketing) == 1
        assert marketing[0]["content_sid"] == "HX_B"

    def test_count_by_status(self, db):
        db.upsert_whatsapp_template({
            "content_sid": "HX_X", "friendly_name": "x", "approval_status": "approved",
        })
        db.upsert_whatsapp_template({
            "content_sid": "HX_Y", "friendly_name": "y", "approval_status": "approved",
        })
        db.upsert_whatsapp_template({
            "content_sid": "HX_Z", "friendly_name": "z", "approval_status": "rejected",
        })
        counts = db.count_whatsapp_templates_by_status()
        assert counts.get("approved") == 2
        assert counts.get("rejected") == 1

    def test_delete_not_in_keeps_only_listed(self, db):
        for sid in ("HX_1", "HX_2", "HX_3"):
            db.upsert_whatsapp_template({"content_sid": sid, "friendly_name": sid})

        deleted = db.delete_whatsapp_templates_not_in(["HX_1", "HX_3"])
        assert deleted == 1
        remaining = [t["content_sid"] for t in db.list_whatsapp_templates()]
        assert set(remaining) == {"HX_1", "HX_3"}

    def test_delete_not_in_empty_list_is_noop(self, db):
        """הגנה: אם הסנכרון החזיר ריק (רשת נפלה באמצע) — אל תמחק הכל."""
        db.upsert_whatsapp_template({"content_sid": "HX_X", "friendly_name": "x"})
        deleted = db.delete_whatsapp_templates_not_in([])
        assert deleted == 0
        assert db.get_whatsapp_template("HX_X") is not None


# ── טסטים לפרסר ──────────────────────────────────────────────────────────────


class TestTemplateParser:
    def test_extract_variables_preserves_order(self):
        from messaging.whatsapp_templates_sync import _extract_variable_indices
        assert _extract_variable_indices("היי {{1}}, {{2}} ו-{{1}} שוב") == ["1", "2"]
        assert _extract_variable_indices("") == []
        assert _extract_variable_indices("ללא משתנים") == []

    def test_parse_quick_reply_content(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_QR",
            "friendly_name": "welcome_menu",
            "language": "he",
            "variables": {"1": "דני"},
            "types": {
                "twilio/quick-reply": {
                    "body": "היי {{1}}, איך אפשר לעזור?",
                    "actions": [
                        {"title": "תור", "id": "menu_booking"},
                        {"title": "מחירון", "id": "menu_price"},
                    ],
                }
            },
        })
        assert parsed["content_sid"] == "HX_QR"
        assert parsed["content_type"] == "twilio/quick-reply"
        # quick-reply הוא body-only — אין header כברירת מחדל
        assert parsed["header_type"] == "none"
        assert parsed["body_text"].startswith("היי")
        assert len(parsed["buttons"]) == 2
        assert parsed["buttons"][0]["type"] == "quick_reply"
        assert parsed["buttons"][0]["title"] == "תור"
        assert len(parsed["variables"]) == 1
        assert parsed["variables"][0]["example"] == "דני"

    def test_parse_list_picker_content(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_LP",
            "friendly_name": "service_picker",
            "language": "he",
            "types": {
                "twilio/list-picker": {
                    "body": "בחרו שירות",
                    "button": "פתח רשימה",
                    "items": [
                        {"item": "תספורת", "id": "srv_haircut", "description": "גברים"},
                        {"item": "זקן", "id": "srv_beard"},
                    ],
                }
            },
        })
        assert parsed["content_type"] == "twilio/list-picker"
        assert len(parsed["buttons"]) == 2
        assert parsed["buttons"][0]["type"] == "list_item"
        assert parsed["buttons"][0]["description"] == "גברים"

    def test_parse_media_content_detects_image(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_IMG",
            "friendly_name": "promo",
            "language": "he",
            "types": {
                "twilio/media": {
                    "body": "מבצע חדש!",
                    "media": ["https://cdn.example.com/promo.jpg"],
                }
            },
        })
        assert parsed["header_type"] == "image"

    def test_parse_media_content_detects_video(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_VID",
            "friendly_name": "v",
            "types": {"twilio/media": {"body": "x", "media": ["https://x/y.mp4"]}},
        })
        assert parsed["header_type"] == "video"

    def test_parse_media_strips_query_string_for_extension(self):
        """Regression bugbot: URLs חתומים נראים כ-foo.mp4?token=xyz,
        ובלי הסרת ה-query נכשל הזיהוי וההודעה מסווגת כ-image."""
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_SIGNED",
            "friendly_name": "v",
            "types": {"twilio/media": {
                "body": "x",
                "media": ["https://cdn.example.com/clip.mp4?Signature=abc&Expires=123"],
            }},
        })
        assert parsed["header_type"] == "video"
        # ה-URL נשמר במלואו כדי שהשליחה לוואטסאפ תכלול את ה-signature
        assert parsed["header_media_url"] == \
            "https://cdn.example.com/clip.mp4?Signature=abc&Expires=123"

    def test_parse_media_pdf_with_query_string(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_DOC",
            "friendly_name": "d",
            "types": {"twilio/media": {
                "body": "x",
                "media": ["https://files.example.com/report.pdf?dl=1"],
            }},
        })
        assert parsed["header_type"] == "document"

    def test_parse_empty_types_is_safe(self):
        from messaging.whatsapp_templates_sync import _parse_content
        parsed = _parse_content({
            "sid": "HX_E", "friendly_name": "empty", "language": "he", "types": {},
        })
        assert parsed["body_text"] == ""
        assert parsed["header_type"] == "none"
        assert parsed["buttons"] == []

    def test_text_based_types_have_no_header(self):
        """Regression: text/quick-reply/list-picker/call-to-action/card הם
        body-only. פעם הם קיבלו header_type='text' שגוי שגרם ל-UI להציג
        אייקון תמונה מטעה."""
        from messaging.whatsapp_templates_sync import _parse_content
        for ct in ("twilio/text", "twilio/quick-reply", "twilio/list-picker",
                   "twilio/call-to-action", "twilio/card"):
            parsed = _parse_content({
                "sid": f"HX_{ct}", "friendly_name": ct, "language": "he",
                "types": {ct: {"body": "x"}},
            })
            assert parsed["header_type"] == "none", \
                f"{ct} אמור להיות body-only ללא header"


class TestApprovalStatusNormalization:
    def test_normalize_known_statuses(self):
        from messaging.whatsapp_templates_sync import _normalize_approval_status
        assert _normalize_approval_status("approved") == "approved"
        assert _normalize_approval_status("received") == "pending"
        assert _normalize_approval_status("disabled") == "paused"
        assert _normalize_approval_status("rejected") == "rejected"

    def test_normalize_unknown_returns_unsubmitted(self):
        from messaging.whatsapp_templates_sync import _normalize_approval_status
        assert _normalize_approval_status("weird_status") == "unsubmitted"

    def test_parse_approval_payload_new_format(self):
        from messaging.whatsapp_templates_sync import _parse_approval_payload
        result = _parse_approval_payload({
            "whatsapp": {
                "status": "approved",
                "category": "marketing",
                "rejection_reason": None,
            }
        })
        assert result["approval_status"] == "approved"
        assert result["category"] == "MARKETING"

    def test_parse_approval_payload_old_format(self):
        from messaging.whatsapp_templates_sync import _parse_approval_payload
        result = _parse_approval_payload({
            "status": "rejected",
            "category": "UTILITY",
            "rejection_reason": "Policy violation",
        })
        assert result["approval_status"] == "rejected"
        assert result["rejection_reason"] == "Policy violation"

    def test_parse_approval_payload_empty(self):
        from messaging.whatsapp_templates_sync import _parse_approval_payload
        assert _parse_approval_payload({})["approval_status"] == "unsubmitted"


class TestCategoryNormalization:
    """הגנה מפני IntegrityError אם Twilio/Meta מחזירים קטגוריה חדשה שלא
    מופיעה ב-CHECK constraint של DB (למשל בהוספה עתידית של קטגוריה)."""

    def test_known_categories_pass_through(self):
        from messaging.whatsapp_templates_sync import _normalize_category
        assert _normalize_category("UTILITY") == "UTILITY"
        assert _normalize_category("marketing") == "MARKETING"
        assert _normalize_category("Authentication") == "AUTHENTICATION"

    def test_unknown_category_maps_to_unknown(self):
        from messaging.whatsapp_templates_sync import _normalize_category
        assert _normalize_category("SERVICE") == "UNKNOWN"
        assert _normalize_category("NEW_META_CATEGORY_2027") == "UNKNOWN"

    def test_empty_category_returns_none_for_preservation(self):
        """Regression: ערך חסר/ריק מחזיר None במקום 'UNKNOWN', כדי שה-
        upsert ב-DB יוכל לשמור את הקטגוריה הקיימת (תבניות pending
        שעדיין אין להן קטגוריה ב-Twilio לא יידרסו ל-UNKNOWN וייעלמו
        מהפילטר ב-UI)."""
        from messaging.whatsapp_templates_sync import _normalize_category
        assert _normalize_category(None) is None
        assert _normalize_category("") is None
        assert _normalize_category("   ") is None

    def test_parse_approval_normalizes_unknown_category(self):
        """Regression: פעם זה החזיר את הקטגוריה הגולמית ו-upsert נכשל."""
        from messaging.whatsapp_templates_sync import _parse_approval_payload
        result = _parse_approval_payload({
            "whatsapp": {"status": "approved", "category": "NEW_SERVICE_CAT_2027"}
        })
        assert result["category"] == "UNKNOWN"

    def test_sync_with_unknown_category_stores_as_unknown(self, db, monkeypatch):
        """End-to-end: קטגוריה חדשה שלא קיימת ב-CHECK constraint של ה-DB
        נשמרת כ-UNKNOWN (לא גורמת ל-IntegrityError)."""
        from messaging import whatsapp_templates_sync as sync_mod

        def fake_iter():
            yield {
                "sid": "HX_WEIRD", "friendly_name": "weird", "language": "he",
                "types": {"twilio/text": {"body": "x"}},
            }

        def fake_fetch_raw(content_sid, timeout=15):
            # מחקה את נתיב ה-HTTP האמיתי: _fetch_approval_status קורא ל-
            # _parse_approval_payload שמנרמל את הקטגוריה לפני החזרה.
            return sync_mod._parse_approval_payload({
                "whatsapp": {
                    "status": "approved",
                    "category": "FUTURE_META_CATEGORY",  # ערך חדש שלא ב-enum
                }
            })

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status", fake_fetch_raw)

        stats = sync_mod.sync_templates_from_twilio(prune_deleted=False)
        assert stats["errors"] == 0
        assert db.get_whatsapp_template("HX_WEIRD")["category"] == "UNKNOWN"


class TestPaginationFailureHandling:
    """באג שזוהה ב-review: כשל pagination באמצע דפדוף מותיר seen_sids חלקי
    ואז prune מוחק את כל מה שלא ראינו בדפים הקודמים. חובה לדלג על prune
    כש-pagination לא הסתיים בהצלחה."""

    def test_pagination_error_skips_prune(self, db, monkeypatch):
        """מצב: דף 1 מוחזר בהצלחה, דף 2 נכשל. seen_sids חלקי.
        ציפייה: prune מדולג; תבנית מדף קודם לא נמחקת."""
        from messaging import whatsapp_templates_sync as sync_mod

        # תבנית שקיימת ב-DB מהסנכרון הקודם ונמצאת בדף 2 (שלא נמשך)
        db.upsert_whatsapp_template({
            "content_sid": "HX_PAGE2_EXISTING",
            "friendly_name": "page2_existing",
            "approval_status": "approved",
        })

        def fake_iter_that_fails():
            # דף 1 — מצליח
            yield {
                "sid": "HX_PAGE1_NEW", "friendly_name": "page1_new", "language": "he",
                "types": {"twilio/text": {"body": "ok"}},
            }
            # דף 2 — נכשל באמצע
            raise sync_mod._PaginationIncomplete("simulated network error on page 2")

        def fake_fetch(sid, timeout=15):
            return {"approval_status": "approved", "category": "UTILITY",
                    "rejection_reason": None}

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter_that_fails)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status", fake_fetch)

        stats = sync_mod.sync_templates_from_twilio(prune_deleted=True)

        assert stats["pagination_complete"] is False
        assert stats["errors"] >= 1
        assert stats["deleted"] == 0
        # התבנית שנמצאת בדף 2 ולא נראתה — עדיין קיימת
        assert db.get_whatsapp_template("HX_PAGE2_EXISTING") is not None
        # התבנית מדף 1 — נשמרה
        assert db.get_whatsapp_template("HX_PAGE1_NEW") is not None

    def test_network_error_on_first_page_does_not_wipe_db(self, db, monkeypatch):
        """מצב קיצון: הדף הראשון נכשל מיד (רשת למטה). seen_sids ריק.
        ציפייה: לא מוחק שום דבר מה-DB."""
        from messaging import whatsapp_templates_sync as sync_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_SURVIVES", "friendly_name": "survives",
        })

        def fake_iter_immediate_fail():
            raise sync_mod._PaginationIncomplete("network down")
            yield  # pragma: no cover — generator marker

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter_immediate_fail)

        stats = sync_mod.sync_templates_from_twilio(prune_deleted=True)

        assert stats["pagination_complete"] is False
        assert stats["fetched"] == 0
        assert stats["deleted"] == 0
        assert db.get_whatsapp_template("HX_SURVIVES") is not None


# ── טסט end-to-end לפונקציית הסנכרון (עם mock ל-HTTP) ────────────────────────


class TestSyncFromTwilio:
    def test_full_sync_writes_to_db_and_prunes(self, db, monkeypatch):
        """שלושה contents נמשכים, אחד מהם מקבל approved, אחד rejected, אחד
        ללא ApprovalRequest. לאחר מכן מתבצע prune של כל מה שלא ברשימה."""
        from messaging import whatsapp_templates_sync as sync_mod

        # נטען מראש תבנית שאמורה להימחק (לא הוחזרה מה-API)
        db.upsert_whatsapp_template({"content_sid": "HX_OLD", "friendly_name": "old"})

        fake_contents = [
            {
                "sid": "HX_A",
                "friendly_name": "tpl_a",
                "language": "he",
                "variables": {"1": "דני"},
                "types": {"twilio/quick-reply": {
                    "body": "שלום {{1}}",
                    "actions": [{"title": "אישור", "id": "ok"}],
                }},
            },
            {
                "sid": "HX_B",
                "friendly_name": "tpl_b",
                "language": "en",
                "types": {"twilio/text": {"body": "plain"}},
            },
            {
                "sid": "HX_C",
                "friendly_name": "tpl_c",
                "language": "he",
                "types": {"twilio/text": {"body": "pending one"}},
            },
        ]

        approval_by_sid = {
            "HX_A": {"whatsapp": {"status": "approved", "category": "UTILITY"}},
            "HX_B": {"whatsapp": {"status": "rejected", "category": "MARKETING",
                                   "rejection_reason": "Policy"}},
            # HX_C — אין ApprovalRequest (404)
        }

        def fake_iter():
            yield from fake_contents

        def fake_fetch_approval(sid, timeout=15):
            payload = approval_by_sid.get(sid)
            if payload is None:
                return {"approval_status": "unsubmitted", "category": "UTILITY",
                        "rejection_reason": None}
            return sync_mod._parse_approval_payload(payload)

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status", fake_fetch_approval)

        stats = sync_mod.sync_templates_from_twilio()

        assert stats["fetched"] == 3
        assert stats["upserted"] == 3
        assert stats["deleted"] == 1  # HX_OLD נמחק
        assert stats["errors"] == 0

        # ודא סטטוסים סופיים
        assert db.get_whatsapp_template("HX_A")["approval_status"] == "approved"
        assert db.get_whatsapp_template("HX_B")["approval_status"] == "rejected"
        assert db.get_whatsapp_template("HX_B")["rejection_reason"] == "Policy"
        assert db.get_whatsapp_template("HX_C")["approval_status"] == "unsubmitted"
        assert db.get_whatsapp_template("HX_OLD") is None

    def test_sync_continues_on_per_template_error(self, db, monkeypatch):
        """אם תבנית אחת גורמת לחריגה — שאר התבניות עדיין נשמרות."""
        from messaging import whatsapp_templates_sync as sync_mod

        def fake_iter():
            yield {
                "sid": "HX_GOOD", "friendly_name": "good", "language": "he",
                "types": {"twilio/text": {"body": "ok"}},
            }
            yield {
                "sid": "HX_BAD", "friendly_name": "bad", "language": "he",
                "types": {"twilio/text": {"body": "ok"}},
            }

        call_count = {"n": 0}

        def fake_fetch_approval(sid, timeout=15):
            call_count["n"] += 1
            if sid == "HX_BAD":
                raise RuntimeError("network explode")
            return {"approval_status": "approved", "category": "UTILITY",
                    "rejection_reason": None}

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status", fake_fetch_approval)

        stats = sync_mod.sync_templates_from_twilio(prune_deleted=False)

        assert stats["upserted"] == 1
        assert stats["errors"] == 1
        assert db.get_whatsapp_template("HX_GOOD") is not None
        assert db.get_whatsapp_template("HX_BAD") is None

    def test_sync_preserves_existing_category_when_no_approval(self, db, monkeypatch):
        """Regression: תבנית שנוצרה דרך הפאנל עם MARKETING ועדיין לא
        נשלחה לאישור (404 ב-ApprovalRequests) — אסור שתשתנה ל-UTILITY
        אחרי סנכרון. זה גרם לתבנית להיעלם מ-filter MARKETING ב-UI."""
        from messaging import whatsapp_templates_sync as sync_mod

        # יוצרים תבנית קיימת עם MARKETING
        db.upsert_whatsapp_template({
            "content_sid": "HX_PROMO",
            "friendly_name": "promo_2026",
            "category": "MARKETING",
            "approval_status": "unsubmitted",
            "body_text": "שלום",
        })

        def fake_iter():
            yield {
                "sid": "HX_PROMO",
                "friendly_name": "promo_2026",
                "language": "he",
                "types": {"twilio/text": {"body": "שלום"}},
            }

        # _fetch_approval_status מחזיר 404 → category=None
        def fake_fetch_approval(content_sid, timeout=15):
            return {
                "approval_status": "unsubmitted",
                "category": None,
                "rejection_reason": None,
            }

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status",
                            fake_fetch_approval)

        sync_mod.sync_templates_from_twilio(prune_deleted=False)

        tpl = db.get_whatsapp_template("HX_PROMO")
        # MARKETING נשמר — לא נדרס ל-UTILITY כפי שקרה לפני התיקון
        assert tpl["category"] == "MARKETING"

    def test_sync_new_template_without_approval_defaults_utility(self, db, monkeypatch):
        """תבנית חדשה לחלוטין שמופיעה ב-Twilio אבל אין לה ApprovalRequest
        ולא קיימת ב-DB → category=UTILITY כברירת מחדל סבירה."""
        from messaging import whatsapp_templates_sync as sync_mod

        def fake_iter():
            yield {
                "sid": "HX_NEW", "friendly_name": "new_one",
                "language": "he",
                "types": {"twilio/text": {"body": "x"}},
            }

        def fake_fetch_approval(content_sid, timeout=15):
            return {"approval_status": "unsubmitted",
                    "category": None, "rejection_reason": None}

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status",
                            fake_fetch_approval)

        sync_mod.sync_templates_from_twilio(prune_deleted=False)

        tpl = db.get_whatsapp_template("HX_NEW")
        assert tpl["category"] == "UTILITY"

    def test_sync_preserves_category_when_200_lacks_category_field(self, db, monkeypatch):
        """Regression bugbot: Twilio מחזיר 200 עם status אך ללא קטגוריה
        (תבנית pending שעדיין בסקירה). בעבר נורמליזציה החזירה 'UNKNOWN'
        ודרסה את MARKETING המקורי. עכשיו _normalize_category מחזיר None
        ו-sync שומר את הקיים."""
        from messaging import whatsapp_templates_sync as sync_mod

        db.upsert_whatsapp_template({
            "content_sid": "HX_PENDING",
            "friendly_name": "promo_in_review",
            "category": "MARKETING",
            "approval_status": "pending",
            "body_text": "x",
        })

        def fake_iter():
            yield {
                "sid": "HX_PENDING", "friendly_name": "promo_in_review",
                "language": "he", "types": {"twilio/text": {"body": "x"}},
            }

        # Twilio מחזיר 200 עם whatsapp.status אבל בלי category
        def fake_fetch_approval(content_sid, timeout=15):
            return sync_mod._parse_approval_payload({
                "whatsapp": {"status": "pending"},
            })

        monkeypatch.setattr(sync_mod, "_iter_all_contents", fake_iter)
        monkeypatch.setattr(sync_mod, "_fetch_approval_status",
                            fake_fetch_approval)

        sync_mod.sync_templates_from_twilio(prune_deleted=False)

        tpl = db.get_whatsapp_template("HX_PENDING")
        # MARKETING נשמר — לא נדרס ל-UNKNOWN
        assert tpl["category"] == "MARKETING"
        assert tpl["approval_status"] == "pending"
