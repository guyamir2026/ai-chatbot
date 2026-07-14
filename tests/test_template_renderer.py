"""
טסטים ל-messaging/template_renderer.py (substitute + render_preview) ול-
helpers של broadcast_campaigns ב-database.py.

אין תלות ב-Flask או Twilio — לוגיקה טהורה.
"""

from unittest.mock import patch

import pytest


# ── fixture: DB נקי לכל טסט ──────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── רינדור משתנים (substitute_variables) ─────────────────────────────────────


class TestSubstituteVariables:
    def test_simple_substitution(self):
        from messaging.template_renderer import substitute_variables
        result = substitute_variables("היי {{1}}, תור ב-{{2}}", {"1": "דני", "2": "14:00"})
        assert result == "היי דני, תור ב-14:00"

    def test_missing_variable_leaves_placeholder(self):
        """חוסר ערך משאיר {{N}} כפי שהוא — חשוב כדי שה-UI יסמן חסר."""
        from messaging.template_renderer import substitute_variables
        result = substitute_variables("היי {{1}}, תור ב-{{2}}", {"1": "דני"})
        assert result == "היי דני, תור ב-{{2}}"

    def test_empty_value_treated_as_missing(self):
        from messaging.template_renderer import substitute_variables
        assert substitute_variables("a {{1}} b", {"1": ""}) == "a {{1}} b"
        assert substitute_variables("a {{1}} b", {"1": "   "}) == "a {{1}} b"
        assert substitute_variables("a {{1}} b", {"1": None}) == "a {{1}} b"

    def test_same_variable_twice_replaced_both(self):
        from messaging.template_renderer import substitute_variables
        result = substitute_variables("{{1}} ו-{{1}} שוב", {"1": "דני"})
        assert result == "דני ו-דני שוב"

    def test_int_and_str_keys_both_work(self):
        """ב-Jinja הערכים לפעמים מגיעים עם מפתח int — לוודא שהנרמול עובד."""
        from messaging.template_renderer import substitute_variables
        assert substitute_variables("{{1}}", {1: "דני"}) == "דני"
        assert substitute_variables("{{1}}", {"1": "דני"}) == "דני"

    def test_empty_text_returns_empty(self):
        from messaging.template_renderer import substitute_variables
        assert substitute_variables("", {"1": "x"}) == ""
        assert substitute_variables(None, {"1": "x"}) == ""

    def test_no_placeholders_returns_unchanged(self):
        from messaging.template_renderer import substitute_variables
        assert substitute_variables("ללא משתנים", {"1": "x"}) == "ללא משתנים"

    def test_whitespace_in_placeholder(self):
        """Twilio לפעמים מחזיר {{ 1 }} עם רווחים — תומכים בזה."""
        from messaging.template_renderer import substitute_variables
        assert substitute_variables("{{  1  }}", {"1": "דני"}) == "דני"


# ── find_missing_variables ───────────────────────────────────────────────────


class TestFindMissingVariables:
    def test_all_provided(self):
        from messaging.template_renderer import find_missing_variables
        tpl = {"variables": [{"index": "1"}, {"index": "2"}]}
        assert find_missing_variables(tpl, {"1": "a", "2": "b"}) == []

    def test_some_missing(self):
        from messaging.template_renderer import find_missing_variables
        tpl = {"variables": [{"index": "1"}, {"index": "2"}, {"index": "3"}]}
        assert find_missing_variables(tpl, {"1": "a"}) == ["2", "3"]

    def test_empty_string_is_missing(self):
        from messaging.template_renderer import find_missing_variables
        tpl = {"variables": [{"index": "1"}]}
        assert find_missing_variables(tpl, {"1": ""}) == ["1"]
        assert find_missing_variables(tpl, {"1": "   "}) == ["1"]

    def test_no_variables_no_missing(self):
        from messaging.template_renderer import find_missing_variables
        assert find_missing_variables({"variables": []}, {}) == []
        assert find_missing_variables({}, {}) == []


# ── render_preview ───────────────────────────────────────────────────────────


def _make_template(
    body="היי {{1}}, תור ל-{{2}}",
    footer="להסרה השב הסר",
    buttons=None,
    variables=None,
    approval_status="approved",
    header_type="none",
):
    return {
        "content_sid": "HX_TEST",
        "friendly_name": "test_tpl",
        "language": "he",
        "category": "UTILITY",
        "approval_status": approval_status,
        "header_type": header_type,
        "body_text": body,
        "footer_text": footer,
        "buttons": buttons or [],
        "variables": variables if variables is not None else [
            {"index": "1", "name": "customer_name", "example": "דני"},
            {"index": "2", "name": "service", "example": "תספורת"},
        ],
    }


class TestRenderPreview:
    def test_full_render_with_all_values(self):
        from messaging.template_renderer import render_preview
        preview = render_preview(_make_template(), {"1": "אביטל", "2": "מניקור"})
        assert preview["body"] == "היי אביטל, תור ל-מניקור"
        assert preview["footer"] == "להסרה השב הסר"
        assert preview["missing_variables"] == []
        assert preview["warnings"] == []
        assert preview["can_send"] is True

    def test_missing_values_flagged(self):
        from messaging.template_renderer import render_preview
        preview = render_preview(_make_template(), {"1": "דני"})
        assert "{{2}}" in preview["body"]
        assert preview["missing_variables"] == ["2"]
        assert any("חסרים" in w for w in preview["warnings"])
        assert preview["can_send"] is False

    def test_pending_template_cannot_send(self):
        from messaging.template_renderer import render_preview
        tpl = _make_template(approval_status="pending")
        preview = render_preview(tpl, {"1": "דני", "2": "תספורת"})
        # אין משתנים חסרים אבל התבנית לא אושרה — לא ניתן לשלוח
        assert preview["missing_variables"] == []
        assert preview["can_send"] is False
        assert any("pending" in w for w in preview["warnings"])

    def test_rejected_template_warning(self):
        from messaging.template_renderer import render_preview
        tpl = _make_template(approval_status="rejected")
        preview = render_preview(tpl, {"1": "x", "2": "y"})
        assert preview["can_send"] is False
        assert any("rejected" in w for w in preview["warnings"])

    def test_body_length_warnings(self):
        from messaging.template_renderer import render_preview
        # body של 1100 תווים — מעל soft limit (1024) ומתחת ל-hard limit (1600)
        long_body = "x" * 1100
        preview = render_preview(_make_template(body=long_body, variables=[]), {})
        assert preview["body_length"] == 1100
        assert any("מתקרב" in w for w in preview["warnings"])
        assert preview["can_send"] is True  # עדיין מתחת ל-hard limit

    def test_body_over_hard_limit_blocks_send(self):
        from messaging.template_renderer import render_preview
        huge_body = "x" * 2000
        preview = render_preview(_make_template(body=huge_body, variables=[]), {})
        assert preview["can_send"] is False
        assert any("חורג" in w for w in preview["warnings"])

    def test_buttons_passed_through(self):
        from messaging.template_renderer import render_preview
        buttons = [
            {"type": "quick_reply", "title": "אישור", "id": "ok"},
            {"type": "quick_reply", "title": "ביטול", "id": "no"},
        ]
        preview = render_preview(
            _make_template(buttons=buttons, variables=[]), {}
        )
        assert len(preview["buttons"]) == 2

    def test_too_many_quick_replies_warning(self):
        from messaging.template_renderer import render_preview
        buttons = [
            {"type": "quick_reply", "title": f"btn{i}"} for i in range(5)
        ]
        preview = render_preview(
            _make_template(buttons=buttons, variables=[]), {}
        )
        assert any("Quick Reply" in w for w in preview["warnings"])

    def test_header_type_preserved(self):
        from messaging.template_renderer import render_preview
        preview = render_preview(
            _make_template(header_type="image", variables=[]), {}
        )
        assert preview["header_type"] == "image"

    def test_footer_also_substituted(self):
        """footer יכול גם להכיל {{N}} — ה-renderer מחליף שם גם כן."""
        from messaging.template_renderer import render_preview
        tpl = _make_template(
            body="body",
            footer="תור ב-{{1}}",
            variables=[{"index": "1", "name": "time", "example": "14:00"}],
        )
        preview = render_preview(tpl, {"1": "15:30"})
        assert preview["footer"] == "תור ב-15:30"

    def test_render_preview_includes_html_versions(self):
        """render_preview מחזיר גם body_html/footer_html עם markdown מומר."""
        from messaging.template_renderer import render_preview
        tpl = _make_template(
            body="היי *{{1}}*, _תודה_",
            footer="להסרה השב *הסר*",
            variables=[{"index": "1", "name": "name"}],
        )
        preview = render_preview(tpl, {"1": "דני"})
        assert "<strong>דני</strong>" in preview["body_html"]
        assert "<em>תודה</em>" in preview["body_html"]
        assert "<strong>הסר</strong>" in preview["footer_html"]
        # הגולמי (body) נותר כמו שהוא — ל-logic שרתי
        assert preview["body"] == "היי *דני*, _תודה_"


# ── wa_markdown_to_html ──────────────────────────────────────────────────────


class TestWaMarkdownToHtml:
    def test_bold(self):
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("*דני*") == "<strong>דני</strong>"
        assert wa_markdown_to_html("היי *עולם*!") == "היי <strong>עולם</strong>!"

    def test_italic(self):
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("_חשוב_") == "<em>חשוב</em>"

    def test_strikethrough(self):
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("~ישן~") == "<del>ישן</del>"

    def test_code(self):
        from messaging.template_renderer import wa_markdown_to_html
        assert "<code>foo</code>" in wa_markdown_to_html("run `foo`")

    def test_multiple_styles_same_text(self):
        from messaging.template_renderer import wa_markdown_to_html
        out = wa_markdown_to_html("*bold* and _italic_ and ~strike~")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out
        assert "<del>strike</del>" in out

    def test_html_escaped_first(self):
        """חובה: HTML escaping לפני markdown. תוקף: תוכן מערכי משתנים יכול
        להיות עוין (למשל שם לקוח שמכיל <script>)."""
        from messaging.template_renderer import wa_markdown_to_html
        out = wa_markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_asterisk_inside_word_not_formatted(self):
        """Regression: 5*3=15 לא אמור להפוך לטקסט מודגש."""
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("5*3=15") == "5*3=15"
        # גם בין תווים ערביים/עבריים
        assert "<strong>" not in wa_markdown_to_html("abc*def*ghi")

    def test_empty_input(self):
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("") == ""
        assert wa_markdown_to_html(None) == ""

    def test_newlines_preserved(self):
        """ה-CSS של בועת WA משתמש ב-white-space: pre-wrap, אז \n נשאר."""
        from messaging.template_renderer import wa_markdown_to_html
        out = wa_markdown_to_html("שורה 1\n*שורה 2*")
        assert "\n" in out
        assert "<strong>שורה 2</strong>" in out

    def test_unmatched_asterisk_preserved(self):
        """כוכבית בודדת נשארת כתו ליטרלי (escaped)."""
        from messaging.template_renderer import wa_markdown_to_html
        assert wa_markdown_to_html("חוזר *בלי סגירה") == "חוזר *בלי סגירה"

    def test_nested_markers_different_types(self):
        """*bold with _italic inside_* — WhatsApp מאפשר שילוב."""
        from messaging.template_renderer import wa_markdown_to_html
        out = wa_markdown_to_html("*bold _italic_ bold*")
        assert "<strong>" in out
        assert "<em>italic</em>" in out

    def test_code_content_not_reinterpreted(self):
        """Regression: תוכן בתוך backticks לא עובר עיבוד markdown נוסף."""
        from messaging.template_renderer import wa_markdown_to_html
        out = wa_markdown_to_html("`*not bold*`")
        # תוך ה-code יופיע &amp; של הכוכביות (escaped), לא <strong>
        assert "<strong>" not in out
        assert "<code>" in out


# ── DB helpers ל-broadcast_campaigns ─────────────────────────────────────────


class TestBroadcastCampaignsDB:
    def test_create_returns_id_and_stores_mapping(self, db):
        cid = db.create_broadcast_campaign(
            template_sid="HX_ABC",
            variable_mapping={"1": "דני", "2": "תספורת"},
            title="תזכורת תורים שבועית",
        )
        assert cid > 0

        camp = db.get_broadcast_campaign(cid)
        assert camp["template_sid"] == "HX_ABC"
        assert camp["title"] == "תזכורת תורים שבועית"
        assert camp["status"] == "draft"
        assert camp["variable_mapping"] == {"1": "דני", "2": "תספורת"}

    def test_create_requires_template_sid(self, db):
        with pytest.raises(ValueError, match="template_sid"):
            db.create_broadcast_campaign(template_sid="")

    def test_create_with_empty_mapping(self, db):
        cid = db.create_broadcast_campaign(template_sid="HX_Z")
        camp = db.get_broadcast_campaign(cid)
        assert camp["variable_mapping"] == {}

    def test_update_draft_replaces_mapping(self, db):
        cid = db.create_broadcast_campaign(
            template_sid="HX_A",
            variable_mapping={"1": "ישן"},
            title="old",
        )
        ok = db.update_broadcast_campaign_draft(
            campaign_id=cid,
            variable_mapping={"1": "חדש", "2": "נוסף"},
            title="new title",
        )
        assert ok is True

        camp = db.get_broadcast_campaign(cid)
        assert camp["variable_mapping"] == {"1": "חדש", "2": "נוסף"}
        assert camp["title"] == "new title"

    def test_update_draft_without_title_keeps_title(self, db):
        cid = db.create_broadcast_campaign(
            template_sid="HX_A",
            variable_mapping={"1": "x"},
            title="keep me",
        )
        db.update_broadcast_campaign_draft(cid, variable_mapping={"1": "y"}, title=None)
        camp = db.get_broadcast_campaign(cid)
        assert camp["title"] == "keep me"
        assert camp["variable_mapping"] == {"1": "y"}

    def test_update_non_draft_status_is_noop(self, db):
        """ניסיון לערוך קמפיין ששודר כבר — לא משנה כלום."""
        cid = db.create_broadcast_campaign(template_sid="HX_A")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE broadcast_campaigns SET status = 'completed' WHERE id = ?",
                (cid,),
            )

        ok = db.update_broadcast_campaign_draft(cid, {"1": "new"}, "new")
        assert ok is False
        camp = db.get_broadcast_campaign(cid)
        assert camp["variable_mapping"] == {}

    def test_get_missing_returns_none(self, db):
        assert db.get_broadcast_campaign(99999) is None

    def test_list_ordered_by_creation_desc(self, db):
        ids = [
            db.create_broadcast_campaign(template_sid=f"HX_{i}", title=f"t{i}")
            for i in range(3)
        ]
        # סימון הטיוטות כשמורות (אחרת list_broadcast_campaigns יסנן אותן —
        # היא מחזירה רק drafts עם last_saved_at IS NOT NULL).
        for cid in ids:
            db.update_broadcast_campaign_draft(campaign_id=cid, variable_mapping={})
        rows = db.list_broadcast_campaigns()
        returned_ids = [r["id"] for r in rows]
        # החדש ביותר ראשון
        assert returned_ids == list(reversed(ids))

    def test_list_filter_by_status(self, db):
        c1 = db.create_broadcast_campaign(template_sid="HX_A")
        c2 = db.create_broadcast_campaign(template_sid="HX_B")
        # סימון c1 כטיוטה שנשמרה במפורש כדי שתופיע ב-list
        db.update_broadcast_campaign_draft(campaign_id=c1, variable_mapping={})
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE broadcast_campaigns SET status = 'completed' WHERE id = ?",
                (c2,),
            )

        drafts = db.list_broadcast_campaigns(status="draft")
        assert [r["id"] for r in drafts] == [c1]

        completed = db.list_broadcast_campaigns(status="completed")
        assert [r["id"] for r in completed] == [c2]

    def test_delete_draft(self, db):
        cid = db.create_broadcast_campaign(template_sid="HX_X")
        assert db.delete_broadcast_campaign(cid) is True
        assert db.get_broadcast_campaign(cid) is None

    def test_cannot_delete_completed(self, db):
        """אודיט: קמפיינים ששודרו לא נמחקים."""
        cid = db.create_broadcast_campaign(template_sid="HX_X")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE broadcast_campaigns SET status = 'completed' WHERE id = ?",
                (cid,),
            )
        assert db.delete_broadcast_campaign(cid) is False
        assert db.get_broadcast_campaign(cid) is not None

    def test_corrupt_mapping_json_falls_back_to_empty(self, db):
        """Resilience: אם מישהו כתב JSON פגום ידנית ל-DB — ה-helper לא קורס."""
        cid = db.create_broadcast_campaign(template_sid="HX_X")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE broadcast_campaigns SET variable_mapping_json = ? WHERE id = ?",
                ("{not valid json", cid),
            )
        camp = db.get_broadcast_campaign(cid)
        assert camp["variable_mapping"] == {}
