"""טסטים ל-messaging/whatsapp_templates_create.py — Phase 1 של פיצ'ר
'צור תבנית broadcast מהאדמין'."""

from unittest.mock import patch, MagicMock

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


# ── extract_variable_indices ─────────────────────────────────────────────────


class TestSingleSourceOfTruth:
    """Regression bugbot: BROADCAST_CATEGORIES חייב להיות אותו ערך
    כמו BROADCAST_TEMPLATE_CATEGORIES ב-database — כדי שעדכון אחד
    יישקף אוטומטית בכל מקום ולא יהיה drift.

    הערה: לא משתמשים ב-fixture db כי הוא עושה importlib.reload שמייצר
    tuple חדש ושובר identity. ההשוואה כאן בערך — אם בעתיד מישהו ישכח
    לעדכן את שתי הרשימות, הטסט ייכשל מיידית.
    """

    def test_categories_imported_from_db(self):
        from messaging.whatsapp_templates_create import BROADCAST_CATEGORIES
        from ai_chatbot.database import BROADCAST_TEMPLATE_CATEGORIES
        assert BROADCAST_CATEGORIES == BROADCAST_TEMPLATE_CATEGORIES

    def test_create_module_does_not_redefine_categories(self):
        """וידוא טכני: ה-symbol הוא alias דרך import — לא literal חדש.
        בודקים ע"י קריאת קוד המקור (אין `BROADCAST_CATEGORIES = (`)."""
        import inspect
        from messaging import whatsapp_templates_create as wtc
        source = inspect.getsource(wtc)
        # אסור שיהיה הקצאה literal — חייב להיות import alias
        assert "BROADCAST_CATEGORIES = (" not in source, (
            "BROADCAST_CATEGORIES חייב להיות alias מ-database, לא tuple "
            "מקומי — אחרת drift בין שני המקורות."
        )


class TestExtractVariableIndices:
    def test_no_variables(self):
        from messaging.whatsapp_templates_create import extract_variable_indices
        assert extract_variable_indices("שלום עולם") == []

    def test_single_variable(self):
        from messaging.whatsapp_templates_create import extract_variable_indices
        assert extract_variable_indices("שלום {{1}}") == [1]

    def test_multiple_unique(self):
        from messaging.whatsapp_templates_create import extract_variable_indices
        assert extract_variable_indices("{{1}} ו-{{2}} ו-{{3}}") == [1, 2, 3]

    def test_duplicates_kept_once(self):
        from messaging.whatsapp_templates_create import extract_variable_indices
        # אם {{1}} מופיע פעמיים — נחשב רק פעם אחת
        assert extract_variable_indices("{{1}} שלום {{1}} שוב") == [1]

    def test_handles_empty_and_none(self):
        from messaging.whatsapp_templates_create import extract_variable_indices
        assert extract_variable_indices("") == []
        assert extract_variable_indices(None) == []


# ── validate_spec ────────────────────────────────────────────────────────────


def _spec(**overrides):
    from messaging.whatsapp_templates_create import TemplateSpec
    base = dict(
        friendly_name="promo_test",
        language="he",
        category="MARKETING",
        body="שלום {{1}}!",
        sample_values=["דני"],
        quick_reply_buttons=[],
    )
    base.update(overrides)
    return TemplateSpec(**base)


def _cta(type_="URL", label="לחצו כאן", value="https://example.com"):
    from messaging.whatsapp_templates_create import CTAButton
    return CTAButton(type=type_, label=label, value=value)


class TestValidateSpec:
    def test_valid_minimal(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec()) == []

    def test_friendly_name_must_be_lowercase_snake(self):
        from messaging.whatsapp_templates_create import validate_spec
        for bad in ("Promo", "promo-test", "1promo", "promo!", "", "x" * 65):
            errors = validate_spec(_spec(friendly_name=bad))
            assert any("שם תבנית" in e for e in errors), bad

    def test_language_restricted(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(language="ru"))
        assert any("שפה" in e for e in errors)

    def test_category_restricted(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(category="OTHER"))
        assert any("קטגוריה" in e for e in errors)

    def test_body_required(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(body="", sample_values=[]))
        assert any("גוף ההודעה" in e for e in errors)

    def test_body_max_length(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(body="א" * 1025, sample_values=[]))
        assert any("ארוך מדי" in e for e in errors)

    def test_sample_values_required_when_variables_exist(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(body="{{1}} {{2}}", sample_values=["רק אחד"]))
        assert any("ערכי דוגמה" in e for e in errors)

    def test_sample_value_cannot_be_empty(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(body="שלום {{1}}", sample_values=[""]))
        assert any("ריק" in e for e in errors)

    def test_consecutive_indices_required(self):
        """Meta דורש {{1}}, {{2}}, {{3}} — לא {{1}}, {{3}}."""
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            body="{{1}} ו-{{3}}", sample_values=["a", "b"],
        ))
        assert any("רציפים" in e for e in errors)

    def test_button_count_max(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            quick_reply_buttons=["a", "b", "c", "d"],
        ))
        assert any("3 כפתורים" in e for e in errors)

    def test_button_label_max_length(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            quick_reply_buttons=["x" * 26],
        ))
        assert any("ארוך מדי" in e for e in errors)

    def test_button_label_empty(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            quick_reply_buttons=["   "],
        ))
        assert any("ריק" in e for e in errors)


# ── derive_twilio_payload ────────────────────────────────────────────────────


class TestDeriveTwilioPayload:
    def test_text_only_uses_twilio_text(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            body="שלום!", sample_values=[],
        ))
        assert "twilio/text" in payload["types"]
        assert payload["types"]["twilio/text"]["body"] == "שלום!"
        assert "twilio/quick-reply" not in payload["types"]

    def test_with_buttons_uses_quick_reply(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            quick_reply_buttons=["מעוניין", "לא מעוניין"],
        ))
        assert "twilio/quick-reply" in payload["types"]
        actions = payload["types"]["twilio/quick-reply"]["actions"]
        assert len(actions) == 2
        assert actions[0]["title"] == "מעוניין"
        assert actions[0]["id"] == "qr_1"

    def test_variables_mapped_to_samples(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            body="{{1}} ו-{{2}}", sample_values=["א", "ב"],
        ))
        assert payload["variables"] == {"1": "א", "2": "ב"}

    def test_friendly_name_and_language_propagated(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            friendly_name="my_promo", language="en",
        ))
        assert payload["friendly_name"] == "my_promo"
        assert payload["language"] == "en"

    def test_empty_button_labels_filtered(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            quick_reply_buttons=["a", "  ", "b"],
        ))
        actions = payload["types"]["twilio/quick-reply"]["actions"]
        # רק 2 כפתורים תקפים — הריק נופל
        assert len(actions) == 2

    def test_out_of_order_placeholders_map_correctly(self):
        """Regression: ה-body כולל {{2}} לפני {{1}}. ערכי הדוגמה במערך
        מאופיינים לפי האינדקס (sample_values[0]={{1}}, sample_values[1]={{2}})
        ולא לפי סדר ההופעה — לכן Twilio צריך לקבל {"1": "ראשון", "2": "שני"}."""
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            body="הזמן תור עכשיו עם {{2}}, יקירנו {{1}}!",
            sample_values=["דני", "נציגתנו"],  # [0]={{1}}, [1]={{2}}
        ))
        assert payload["variables"] == {"1": "דני", "2": "נציגתנו"}


# ── _parse_template_form (admin route helper) ───────────────────────────────


class TestParseTemplateForm:
    """הלוגיקה שב-admin/app.py — בודקים שהפרסור בונה sample_values
    לפי האינדקס המספרי, לא לפי סדר ההופעה ב-body. הבדיקה לא דורשת DB."""

    def _parse(self, form_dict: dict) -> dict:
        # שכפול הלוגיקה של _parse_template_form (פנימית) — הקלאס מאפשר
        # לבדוק רגרסיה גם בלי לאתחל את כל אדמין Flask. חייב להישאר
        # תואם ללוגיקה ב-admin/app.py.
        from messaging.whatsapp_templates_create import (
            extract_variable_indices, CTAButton,
        )

        class _Form(dict):
            def getlist(self, key):
                return self.get(key, [])

        form = _Form(form_dict)
        body = form.get("body", "")
        indices = extract_variable_indices(body)
        if indices:
            max_idx = max(indices)
            sample_values = [
                (form.get(f"sample_{i}", "") or "").strip()
                for i in range(1, max_idx + 1)
            ]
        else:
            sample_values = []
        # CTA parsing — חייב להיות זהה ללוגיקה בייצור: רק label OR value
        # מסמנים כוונת המשתמש; type לבד = ברירת מחדל של dropdown ולא נחשב.
        cta_buttons = []
        for i in range(1, 3):
            t = (form.get(f"cta_type_{i}", "") or "").strip().upper()
            lbl = (form.get(f"cta_label_{i}", "") or "").strip()
            val = (form.get(f"cta_value_{i}", "") or "").strip()
            if lbl or val:
                cta_buttons.append(CTAButton(type=t, label=lbl, value=val))
        return {
            "sample_values": sample_values,
            "indices": indices,
            "cta_buttons": cta_buttons,
        }

    def test_in_order_body(self):
        result = self._parse({
            "body": "{{1}} ו-{{2}}",
            "sample_1": "א", "sample_2": "ב",
        })
        assert result["sample_values"] == ["א", "ב"]

    def test_out_of_order_body(self):
        """Regression bugbot: body עם {{2}} לפני {{1}} — sample_values
        חייב להיות [val_for_1, val_for_2] ולא [val_for_2, val_for_1]."""
        result = self._parse({
            "body": "ראשית {{2}} ואז {{1}}",
            "sample_1": "אחד", "sample_2": "שניים",
        })
        assert result["sample_values"] == ["אחד", "שניים"]

    def test_no_placeholders_returns_empty(self):
        result = self._parse({"body": "טקסט בלי משתנים"})
        assert result["sample_values"] == []

    def test_cta_dropdown_only_does_not_create_button(self):
        """Regression bugbot: בחירת סוג CTA מה-dropdown בלי label ו-value
        לא אמורה ליצור CTAButton (אחרת תופיע שגיאת mutual exclusion
        מבלבלת כשמשתמש משלים Quick Reply ולחיצה מקרית הזיזה את
        ה-dropdown)."""
        result = self._parse({
            "body": "hi",
            "cta_type_1": "URL",  # רק dropdown — בלי label/value
            "cta_label_1": "",
            "cta_value_1": "",
        })
        assert result["cta_buttons"] == []

    def test_cta_partial_label_creates_button_for_validation(self):
        """אם המשתמש כתב label בלי value — נוצר CTAButton כדי שולידציה
        תיתן שגיאה ספציפית (חסר value) ולא תידחה בלחיצה אחת מקרית."""
        result = self._parse({
            "body": "hi",
            "cta_type_1": "URL",
            "cta_label_1": "לחצו כאן",
            "cta_value_1": "",
        })
        assert len(result["cta_buttons"]) == 1
        assert result["cta_buttons"][0].label == "לחצו כאן"
        assert result["cta_buttons"][0].value == ""

    def test_cta_full_row_creates_button(self):
        result = self._parse({
            "body": "hi",
            "cta_type_1": "URL",
            "cta_label_1": "לחצו כאן",
            "cta_value_1": "https://example.com",
        })
        assert len(result["cta_buttons"]) == 1
        btn = result["cta_buttons"][0]
        assert btn.type == "URL"
        assert btn.value == "https://example.com"


# ── create_marketing_template (Twilio API mocked) ────────────────────────────


class TestCreateMarketingTemplate:
    def _mock_twilio_response(self, sid="HXnew123", status=201):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {
            "sid": sid,
            "friendly_name": "promo_test",
            "language": "he",
        }
        resp.text = "ok"
        return resp

    def test_validation_error_raises_value_error(self, db):
        from messaging.whatsapp_templates_create import (
            create_marketing_template,
        )
        with pytest.raises(ValueError, match="גוף ההודעה"):
            create_marketing_template(_spec(body="", sample_values=[]))

    def test_twilio_error_raises_runtime_error(self, db, monkeypatch):
        from messaging import whatsapp_templates_create as wtc
        from unittest.mock import MagicMock
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.text = "duplicate friendly_name"
        bad_resp.json.return_value = {"message": "duplicate friendly_name"}
        monkeypatch.setattr(wtc, "requests", MagicMock(post=MagicMock(return_value=bad_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")
        with pytest.raises(RuntimeError, match="400"):
            wtc.create_marketing_template(_spec(body="hello", sample_values=[]))

    def test_success_inserts_to_db_with_unsubmitted_status(self, db, monkeypatch):
        from messaging import whatsapp_templates_create as wtc
        from unittest.mock import MagicMock
        ok_resp = MagicMock()
        ok_resp.status_code = 201
        ok_resp.json.return_value = {
            "sid": "HXabc", "friendly_name": "promo_test",
        }
        ok_resp.text = "ok"
        monkeypatch.setattr(wtc, "requests", MagicMock(post=MagicMock(return_value=ok_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")

        result = wtc.create_marketing_template(_spec(
            body="שלום {{1}}", sample_values=["דני"],
            quick_reply_buttons=["מעוניין"],
        ))

        assert result["content_sid"] == "HXabc"
        assert result["approval_status"] == "unsubmitted"
        assert result["body_text"] == "שלום {{1}}"
        assert result["category"] == "MARKETING"
        # ה-buttons נשמרים ב-DB ב-schema התואם ל-sync (lowercase type, title, id)
        assert len(result["buttons"]) == 1
        btn = result["buttons"][0]
        assert btn["type"] == "quick_reply"
        assert btn["title"] == "מעוניין"
        assert btn["id"] == "qr_1"
        # variables ב-schema התואם ל-sync (index, name, example)
        assert len(result["variables"]) == 1
        var = result["variables"][0]
        assert var["index"] == "1"
        assert var["name"] == "variable_1"
        assert var["example"] == "דני"

    def test_zero_index_placeholder_rejected_by_validation(self):
        """Regression: {{0}} חייב להיתפס ע"י ולידציה כדי שלא ייגרם
        sample_values[-1] (גישה לאיבר האחרון) ב-derive_twilio_payload."""
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            body="שלום {{0}}", sample_values=["x"],
        ))
        assert any("מ-{{1}}" in e or "מ-{{0}}" in e or "{{1}} ומעלה" in e
                   for e in errors)

    def test_returns_dict_even_if_db_get_returns_none(self, db, monkeypatch):
        """Regression bugbot: get_whatsapp_template יכול להחזיר None במצב
        race — אסור שה-caller יקרוס על tpl['content_sid']."""
        from messaging import whatsapp_templates_create as wtc
        ok_resp = MagicMock()
        ok_resp.status_code = 201
        ok_resp.json.return_value = {"sid": "HXrace"}
        ok_resp.text = "ok"
        monkeypatch.setattr(wtc, "requests",
                            MagicMock(post=MagicMock(return_value=ok_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")
        # מאלצים get_whatsapp_template להחזיר None (race סינתטי)
        from ai_chatbot import database as adb
        monkeypatch.setattr(adb, "get_whatsapp_template", lambda sid: None)

        result = wtc.create_marketing_template(_spec(
            body="שלום", sample_values=[],
            quick_reply_buttons=["מעוניין"],
        ))
        # ה-caller (route) עושה tpl['content_sid'] / tpl['friendly_name']
        # — מוודאים שיש אותם
        assert result["content_sid"] == "HXrace"
        assert result["friendly_name"] == "promo_test"
        assert result["approval_status"] == "unsubmitted"
        assert result["buttons"][0]["type"] == "quick_reply"

    def test_body_length_uses_unstripped(self):
        """Regression bugbot: validation צריכה למדוד את האורך שיגיע
        ל-Twilio (ללא strip) — לא רק את התוכן ה'מועיל'."""
        from messaging.whatsapp_templates_create import validate_spec
        # 1024 רווחים מובילים + תוכן קטן → strip ייתן 4 תווים, אבל
        # הגוף שיישלח ל-Twilio הוא 1028 → חייב להיתפס כשגיאה.
        body = " " * 1025 + "test"
        errors = validate_spec(_spec(body=body, sample_values=[]))
        assert any("ארוך מדי" in e for e in errors)

    def test_derive_payload_skips_zero_index_safely(self):
        """גם אם איכשהו {{0}} עבר ולידציה — derive_twilio_payload לא
        יחזיר אותו ב-variables (מסונן ע"י i >= 1) ולא יגרום
        sample_values[-1] (גישה לאיבר האחרון)."""
        from messaging.whatsapp_templates_create import derive_twilio_payload
        # עוקפים ולידציה ע"י קריאה ישירה. לפי החוזה sample_values[0]={{1}}.
        spec_with_zero = _spec(
            body="{{0}} {{1}}", sample_values=["one_val"],
        )
        payload = derive_twilio_payload(spec_with_zero)
        # רק "1" אמור להופיע — לא "0", ולא לאחזר את האיבר האחרון בטעות
        assert "0" not in payload["variables"]
        assert payload["variables"].get("1") == "one_val"


# ── Phase 2: header / footer / CTA buttons ───────────────────────────────────


class TestPhase2HeaderFooterValidation:
    def test_header_too_long(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(header_text="x" * 61))
        assert any("Header" in e and "ארוך" in e for e in errors)

    def test_header_with_placeholder_rejected(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(header_text="שלום {{1}}"))
        assert any("Header" in e and "משתנים" in e for e in errors)

    def test_footer_too_long(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(footer="x" * 61))
        assert any("Footer" in e and "ארוך" in e for e in errors)

    def test_footer_with_placeholder_rejected(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(footer="ניתן להסיר {{1}}"))
        assert any("Footer" in e and "משתנים" in e for e in errors)

    def test_valid_header_and_footer(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            header_text="כותרת קצרה",
            footer="להסרה השיבו 'הסר'",
        )) == []

    def test_footer_without_buttons_or_header_rejected(self):
        """Regression bugbot: footer ב-twilio/text נשמט שקט. הולידציה
        חייבת להזהיר במקום לתת לזה לקרות."""
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(footer="להסרה השיבו 'הסר'"))
        assert any("Footer" in e and ("כפתור" in e or "Header" in e)
                   for e in errors)

    def test_footer_with_quick_reply_allowed(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            footer="להסרה השיבו 'הסר'",
            quick_reply_buttons=["מעוניין"],
        )) == []

    def test_footer_with_cta_allowed(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            footer="להסרה השיבו 'הסר'",
            cta_buttons=[_cta()],
        )) == []


class TestPhase2CTAValidation:
    def test_too_many_cta_buttons(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(cta_buttons=[_cta(), _cta(), _cta()]))
        assert any("CTA" in e and "2" in e for e in errors)

    def test_invalid_cta_type(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(cta_buttons=[_cta(type_="EMAIL")]))
        assert any("CTA #1" in e and "סוג" in e for e in errors)

    def test_url_must_be_http(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            cta_buttons=[_cta(type_="URL", value="example.com")],
        ))
        assert any("CTA #1" in e and "http" in e for e in errors)

    def test_phone_must_be_e164(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            cta_buttons=[_cta(type_="PHONE", label="התקשרו",
                              value="not-a-phone")],
        ))
        assert any("CTA #1" in e and "טלפון" in e for e in errors)

    def test_phone_e164_accepted(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            cta_buttons=[_cta(type_="PHONE", label="התקשרו",
                              value="+972501234567")],
        )) == []

    def test_label_too_long(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            cta_buttons=[_cta(label="x" * 26)],
        ))
        assert any("CTA #1" in e and "ארוך" in e for e in errors)

    def test_quick_reply_and_cta_mutually_exclusive(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            quick_reply_buttons=["מעוניין"],
            cta_buttons=[_cta()],
        ))
        assert any("Quick Reply" in e and "CTA" in e for e in errors)


class TestPhase2PayloadShape:
    def test_text_only_when_no_extras(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(body="hi", sample_values=[]))
        assert "twilio/text" in payload["types"]

    def test_quick_reply_when_only_quick_reply_buttons(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            quick_reply_buttons=["מעוניין"],
        ))
        assert "twilio/quick-reply" in payload["types"]

    def test_call_to_action_when_only_cta(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(cta_buttons=[
            _cta(type_="URL", label="קישור", value="https://x.com"),
            _cta(type_="PHONE", label="התקשרו", value="+972501234567"),
        ]))
        assert "twilio/call-to-action" in payload["types"]
        actions = payload["types"]["twilio/call-to-action"]["actions"]
        assert actions[0] == {"type": "URL", "title": "קישור",
                              "url": "https://x.com"}
        # PHONE → PHONE_NUMBER ב-Twilio API
        assert actions[1] == {"type": "PHONE_NUMBER", "title": "התקשרו",
                              "phone": "+972501234567"}

    def test_card_when_header_text_present(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            header_text="כותרת",
            cta_buttons=[_cta()],
        ))
        assert "twilio/card" in payload["types"]
        card = payload["types"]["twilio/card"]
        assert card["title"] == "כותרת"
        assert card["actions"][0]["type"] == "URL"

    def test_footer_attached_to_non_text_types(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            footer="להסרה השיבו 'הסר'",
            quick_reply_buttons=["מעוניין"],
        ))
        assert payload["types"]["twilio/quick-reply"]["footer"] == \
            "להסרה השיבו 'הסר'"

    def test_footer_skipped_for_twilio_text(self):
        """twilio/text לא תומך ב-footer ב-Twilio Content API,
        אז לא שולחים אותו בטעות."""
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            footer="ignored", sample_values=[],
        ))
        text_payload = payload["types"]["twilio/text"]
        assert "footer" not in text_payload


class TestPhase2DBPersistence:
    def test_db_includes_header_footer_and_cta_button(self, db, monkeypatch):
        from messaging import whatsapp_templates_create as wtc
        ok_resp = MagicMock()
        ok_resp.status_code = 201
        ok_resp.json.return_value = {"sid": "HXp2"}
        ok_resp.text = "ok"
        monkeypatch.setattr(wtc, "requests",
                            MagicMock(post=MagicMock(return_value=ok_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")

        result = wtc.create_marketing_template(_spec(
            body="שלום {{1}}",
            header_text="כותרת",
            footer="להסרה השיבו 'הסר'",
            cta_buttons=[_cta(type_="URL", label="קישור",
                              value="https://x.com")],
        ))
        assert result["header_type"] == "text"
        # Regression bugbot High: header_text חייב להיות persisted ב-DB,
        # לא רק b-raw_json. אחרת רינדור עתידי מאבד את הכותרת.
        assert result["header_text"] == "כותרת"
        assert result["footer_text"] == "להסרה השיבו 'הסר'"
        assert result["content_type"] == "twilio/card"  # יש header → card
        # buttons נשמרו עם schema של sync (lowercase, title, url)
        assert len(result["buttons"]) == 1
        btn = result["buttons"][0]
        assert btn["type"] == "call_to_action"
        assert btn["title"] == "קישור"
        assert btn["url"] == "https://x.com"


# ── Phase 3: header media (URL חיצוני) ───────────────────────────────────────


class TestPhase3HeaderMediaValidation:
    def test_url_must_be_http(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_media_type="image",
            header_media_url="example.com/x.jpg",
        ))
        assert any("http" in e for e in errors)

    def test_extension_mismatch_rejected(self):
        """אם ה-URL מסתיים בסיומת של סוג אחר (לא מוכר ולא תואם)
        — נחשב טעות. URLs בלי סיומת (Drive/SharePoint) עוברים."""
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_media_type="image",
            header_media_url="https://example.com/movie.mp4",  # video → image: mismatch
        ))
        assert any("video" in e or "סיומת" in e or "תואם" in e
                   for e in errors)

    def test_url_without_extension_accepted(self):
        """Regression: URLs מ-Google Drive/SharePoint לרוב לא מסתיימים
        בסיומת קובץ (יש בהם פרמטרים בלבד). הוולידציה כבר לא דורשת סיומת
        מוכרת — Twilio/Meta יבדקו MIME type."""
        from messaging.whatsapp_templates_create import validate_spec
        for url in (
            "https://drive.google.com/uc?id=1ABC234XYZ",
            "https://example.sharepoint.com/sites/x/_layouts/15/download.aspx?Source=...",
            "https://signed.example.com/files/abc?Signature=xyz&Expires=123",
        ):
            assert validate_spec(_spec(
                header_media_type="image", header_media_url=url,
            )) == [], url

    def test_extended_image_extensions_recognized(self):
        """Regression bugbot: רשימת הסיומות הורחבה ל-gif/webp/bmp
        (image), avi/mkv/m4v (video). gif עבור image עובר; gif עבור
        video נחשב mismatch כי gif שייך ל-image."""
        from messaging.whatsapp_templates_create import validate_spec
        # gif עבור image — תקין
        assert validate_spec(_spec(
            header_media_type="image",
            header_media_url="https://example.com/x.gif",
        )) == []
        # gif עבור video — mismatch (gif בקבוצת image)
        errors = validate_spec(_spec(
            header_media_type="video",
            header_media_url="https://example.com/x.gif",
        ))
        assert any("image" in e for e in errors)

    def test_avi_recognized_as_video(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            header_media_type="video",
            header_media_url="https://example.com/clip.avi",
        )) == []
        errors = validate_spec(_spec(
            header_media_type="image",
            header_media_url="https://example.com/clip.avi",
        ))
        assert any("video" in e for e in errors)

    def test_extension_with_query_string_accepted(self):
        from messaging.whatsapp_templates_create import validate_spec
        # ?token=... אחרי הסיומת לא אמור לפסול
        assert validate_spec(_spec(
            header_media_type="image",
            header_media_url="https://example.com/x.jpg?sig=abc",
        )) == []

    def test_text_and_media_mutually_exclusive(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_text="כותרת",
            header_media_type="image",
            header_media_url="https://example.com/x.jpg",
        ))
        assert any("הדדית בלעדיים" in e or "טקסט או מדיה" in e
                   for e in errors)

    def test_media_type_without_url_rejected(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_media_type="image",
            header_media_url=None,
        ))
        assert any("URL" in e for e in errors)

    def test_media_url_without_type_rejected(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_media_url="https://example.com/x.jpg",
        ))
        assert any("סוג" in e for e in errors)

    def test_invalid_media_type_rejected(self):
        from messaging.whatsapp_templates_create import validate_spec
        errors = validate_spec(_spec(
            header_media_type="audio",
            header_media_url="https://example.com/x.mp3",
        ))
        assert any("סוג לא תקף" in e for e in errors)

    def test_video_extensions_accepted(self):
        from messaging.whatsapp_templates_create import validate_spec
        for ext in (".mp4", ".mov", ".webm"):
            assert validate_spec(_spec(
                header_media_type="video",
                header_media_url=f"https://example.com/x{ext}",
            )) == [], ext

    def test_document_pdf_accepted(self):
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            header_media_type="document",
            header_media_url="https://example.com/file.pdf",
        )) == []


class TestPhase3PayloadAndDB:
    def test_card_includes_media_array(self):
        from messaging.whatsapp_templates_create import derive_twilio_payload
        payload = derive_twilio_payload(_spec(
            header_media_type="image",
            header_media_url="https://example.com/banner.jpg",
        ))
        card = payload["types"]["twilio/card"]
        assert card["media"] == ["https://example.com/banner.jpg"]
        # אין title כי לא הוגדר header_text
        assert "title" not in card

    def test_db_persists_media_url_and_header_type(self, db, monkeypatch):
        from messaging import whatsapp_templates_create as wtc
        ok_resp = MagicMock()
        ok_resp.status_code = 201
        ok_resp.json.return_value = {"sid": "HXp3"}
        ok_resp.text = "ok"
        monkeypatch.setattr(wtc, "requests",
                            MagicMock(post=MagicMock(return_value=ok_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")

        result = wtc.create_marketing_template(_spec(
            body="שלום",
            sample_values=[],
            header_media_type="video",
            header_media_url="https://example.com/clip.mp4",
        ))
        # header_type ב-DB מקבל את סוג המדיה (image/video/document)
        assert result["header_type"] == "video"
        assert result["header_media_url"] == "https://example.com/clip.mp4"
        # text נשאר ריק (mutual exclusion)
        assert result["header_text"] == ""
        assert result["content_type"] == "twilio/card"

    def test_resolve_header_type_falls_to_none_for_invalid(self):
        from messaging.whatsapp_templates_create import _resolve_header_type
        # עוקפים ולידציה — _resolve_header_type צריך להיות שמרני
        spec = _spec(header_media_type="audio",  # לא חוקי
                     header_media_url="https://example.com/x.mp3")
        assert _resolve_header_type(spec) == "none"

    def test_footer_with_media_header_allowed(self):
        """Regression bugbot: media header צריך להיחשב כ-header
        בולידציה של footer — לא לדחות footer + media בלי buttons."""
        from messaging.whatsapp_templates_create import validate_spec
        assert validate_spec(_spec(
            footer="להסרה השיבו 'הסר'",
            header_media_type="image",
            header_media_url="https://example.com/banner.jpg",
        )) == []


class TestUpsertCoalescePreservesMedia:
    """Regression bugbot: upsert מ-callers שלא מכירים header_media_url
    (sync ישן, submit) חייב לא לדרוס ערכים קיימים."""

    def test_submit_path_preserves_existing_media_url(self, db):
        """מדמה: יוצרים תבנית עם media URL, אז קוראים upsert בלי
        header_media_url (כפי שעושה submit_template_for_approval).
        ה-URL חייב להישאר."""
        db.upsert_whatsapp_template({
            "content_sid": "HX_M1",
            "friendly_name": "with_media",
            "header_type": "image",
            "header_media_url": "https://example.com/banner.jpg",
            "header_text": "כותרת מקורית",
            "body_text": "שלום",
        })

        # submit-style upsert: לא מעביר header_media_url בכלל
        db.upsert_whatsapp_template({
            "content_sid": "HX_M1",
            "friendly_name": "with_media",
            "approval_status": "pending",
            "header_type": "image",
            "body_text": "שלום",
        })

        tpl = db.get_whatsapp_template("HX_M1")
        assert tpl["header_media_url"] == "https://example.com/banner.jpg"
        assert tpl["header_text"] == "כותרת מקורית"
        assert tpl["approval_status"] == "pending"

    def test_explicit_empty_string_does_clear(self, db):
        """כש-create מעביר header_media_url='' במפורש (כי המשתמש לא
        בחר media) — זה אמור לדרוס ערך קיים."""
        db.upsert_whatsapp_template({
            "content_sid": "HX_M2",
            "friendly_name": "tpl",
            "header_type": "image",
            "header_media_url": "https://example.com/x.jpg",
            "body_text": "א",
        })
        # create-style upsert: מעביר "" במפורש כדי לנקות
        db.upsert_whatsapp_template({
            "content_sid": "HX_M2",
            "friendly_name": "tpl",
            "header_type": "none",
            "header_media_url": "",
            "header_text": "",
            "body_text": "א",
        })
        tpl = db.get_whatsapp_template("HX_M2")
        assert tpl["header_media_url"] == ""
        assert tpl["header_text"] == ""


# ── delete_template idempotency + create-rollback orphan cleanup ─────────────


class TestDeleteTemplateIdempotency:
    """Regression bugbot: delete_template חייב להחזיר True גם על 404
    (תבנית כבר לא קיימת = הצלחה idempotent), אחרת מצב 'ערוך' תקוע
    כשתבנית נמחקה out-of-band מ-Twilio Console."""

    def test_204_returns_true(self, monkeypatch):
        from messaging import whatsapp_templates as wt
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.status_code = 204
        monkeypatch.setattr(wt.requests, "delete",
                            MagicMock(return_value=resp))
        monkeypatch.setattr(wt, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wt, "_content_api_url", lambda sid: "https://x")
        assert wt.delete_template("HX_X") is True

    def test_404_returns_true(self, monkeypatch):
        """Regression bugbot."""
        from messaging import whatsapp_templates as wt
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.status_code = 404
        monkeypatch.setattr(wt.requests, "delete",
                            MagicMock(return_value=resp))
        monkeypatch.setattr(wt, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wt, "_content_api_url", lambda sid: "https://x")
        assert wt.delete_template("HX_GONE") is True

    def test_500_returns_false(self, monkeypatch):
        from messaging import whatsapp_templates as wt
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.status_code = 500; resp.text = "err"
        monkeypatch.setattr(wt.requests, "delete",
                            MagicMock(return_value=resp))
        monkeypatch.setattr(wt, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wt, "_content_api_url", lambda sid: "https://x")
        assert wt.delete_template("HX_BUSY") is False


class TestCreateOrphanCleanup:
    """Regression bugbot: אם DB upsert נכשל אחרי Twilio create הצליח,
    התבנית ב-Twilio צריכה להימחק כדי לא להישאר orphan עם friendly_name
    תפוס שחוסם rollback ו-retries עתידיים."""

    def test_db_failure_triggers_twilio_delete(self, db, monkeypatch):
        from messaging import whatsapp_templates_create as wtc
        from unittest.mock import MagicMock
        ok_resp = MagicMock()
        ok_resp.status_code = 201
        ok_resp.json.return_value = {"sid": "HX_ORPHAN"}
        ok_resp.text = "ok"
        monkeypatch.setattr(wtc, "requests",
                            MagicMock(post=MagicMock(return_value=ok_resp)))
        monkeypatch.setattr(wtc, "_get_auth", lambda: ("k", "s"))
        monkeypatch.setattr(wtc, "_content_api_url", lambda: "https://x")

        # מאלצים upsert להיכשל
        from ai_chatbot import database as adb
        def bad_upsert(*a, **kw):
            raise RuntimeError("simulated DB failure")
        monkeypatch.setattr(adb, "upsert_whatsapp_template", bad_upsert)

        # תופסים את ה-delete_template שאמור להיקרא בניקוי orphan
        deleted_sids = []
        def fake_delete(sid):
            deleted_sids.append(sid)
            return True
        from messaging import whatsapp_templates as wt_mod
        monkeypatch.setattr(wt_mod, "delete_template", fake_delete)

        with pytest.raises(RuntimeError, match="simulated DB failure"):
            wtc.create_marketing_template(_spec(body="hi", sample_values=[]))
        # הרי הוא מסתפק ב-delete של ה-Twilio template
        assert deleted_sids == ["HX_ORPHAN"]
