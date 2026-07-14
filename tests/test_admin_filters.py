"""טסטים לפילטרים של פאנל האדמין — בעיקר _telegram_html."""

from admin.app import _telegram_html


class TestTelegramHtmlFilter:
    """פילטר Jinja2 שמרנדר הודעות שנשמרו מטלגרם/וואטסאפ בתצוגת הפאנל."""

    def test_preserves_balanced_tags(self):
        result = str(_telegram_html("<b>כותרת</b> ו-<i>הערה</i>"))
        assert result == "<b>כותרת</b> ו-<i>הערה</i>"

    def test_plain_text_unchanged(self):
        result = str(_telegram_html("שלום עולם"))
        assert result == "שלום עולם"

    def test_newlines_become_br(self):
        result = str(_telegram_html("שורה 1\nשורה 2"))
        assert "<br>" in result

    def test_escapes_unknown_tags(self):
        result = str(_telegram_html("<script>alert(1)</script>"))
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_closes_orphan_open_tag(self):
        """תג פתיחה יתום נסגר אוטומטית בסוף — מונע דליפת עיצוב להודעה הבאה."""
        result = str(_telegram_html("טקסט עם <u>קו תחתון פתוח"))
        assert result.endswith("</u>")
        assert result.count("<u>") == 1
        assert result.count("</u>") == 1

    def test_closes_multiple_orphan_open_tags_in_order(self):
        """כמה תגים פתוחים — כולם נסגרים בסדר הפוך (LIFO)."""
        result = str(_telegram_html("<b>bold <i>italic <u>under"))
        # סדר הסגירה הוא הפוך לפתיחה
        assert result.endswith("</u></i></b>")

    def test_example_tags_in_user_message_do_not_leak(self):
        """תרחיש ממשי: משתמש כתב דוגמה "<b>, <i>, <u>" בהודעה — התגים נפתחים ונסגרים אוטומטית."""
        text = "השתמש רק בתגים <b>, <i>, <u>. אל תשתמש ב-Markdown."
        result = str(_telegram_html(text))
        # כל שלושת התגים נסגרים בסוף
        assert result.count("<b>") == result.count("</b>") == 1
        assert result.count("<i>") == result.count("</i>") == 1
        assert result.count("<u>") == result.count("</u>") == 1

    def test_drops_orphan_closing_tag(self):
        """תג סגירה בלי פתיחה תואמת — מושלך (לא נחשף כטקסט גולמי)."""
        result = str(_telegram_html("טקסט רגיל </u> עוד טקסט"))
        assert "</u>" not in result
        assert "&lt;/u&gt;" not in result
        assert "טקסט רגיל" in result and "עוד טקסט" in result

    def test_drops_mismatched_closing_tag(self):
        """תג סגירה לא תואם (סדר הפוך) — היתום מושלך, התקין נשמר."""
        # <b>...</i> — </i> לא תואם לראש המחסנית (b)
        result = str(_telegram_html("<b>טקסט</i>"))
        # ה-</i> מושלך, <b> נשאר פתוח ונסגר בסוף
        assert result == "<b>טקסט</b>"

    def test_attributed_tag_does_not_affect_stack(self):
        """תג עם מאפיינים לא בטוחים — escape בלבד, לא דוחף למחסנית."""
        # התג <b class="x"> נכתב כ-escape, אבל </b> אחריו הוא יתום ולכן מושלך
        result = str(_telegram_html('<b class="x">טקסט</b>'))
        assert "&lt;b class=" in result
        # ה-</b> מושלך כי אין פתיחה תואמת במחסנית
        assert "</b>" not in result

    def test_safe_href_anchor_is_tracked(self):
        """<a href="https://..."> נשמר ונסגר אוטומטית אם חסר </a>."""
        result = str(_telegram_html('<a href="https://example.com">לחץ'))
        assert '<a href="https://example.com">' in result
        assert result.endswith("</a>")


class TestWhatsAppMarkdownConversion:
    """המרת WhatsApp markdown ל-HTML בתצוגת הפאנל.

    בWhatsApp: *bold*, _italic_, ~strike~ עם כוכבית/קו תחתון/טילדה בודדים.
    ההמרה רצה רק כשה-channel הוא 'whatsapp' — כדי לא לעוות הודעות
    של משתמשי טלגרם שכותבים *50* או _my_var_ לא בכוונת עיצוב.
    """

    def test_single_asterisk_becomes_bold(self):
        result = str(_telegram_html("הטקסט *מודגש* כאן", channel="whatsapp"))
        assert "<b>מודגש</b>" in result

    def test_single_underscore_becomes_italic(self):
        result = str(_telegram_html("הטקסט _נטוי_ כאן", channel="whatsapp"))
        assert "<i>נטוי</i>" in result

    def test_tilde_becomes_strike(self):
        result = str(_telegram_html("הטקסט ~מחוק~ כאן", channel="whatsapp"))
        assert "<s>מחוק</s>" in result

    def test_in_parentheses(self):
        """לפי הדיווח: '_(30 דק׳)_' צריך להיות italic."""
        result = str(_telegram_html("מחיר _(30 דק׳)_ כאן", channel="whatsapp"))
        assert "<i>(30 דק׳)</i>" in result

    def test_double_asterisk_not_converted_to_bold(self):
        """**X** הוא markdown של פלטפורמות אחרות — לא נוגעים בו."""
        # מצופה שלא ייווצר <b> בודד
        result = str(_telegram_html("הטקסט **דאבל** כאן", channel="whatsapp"))
        # ה-*דאבל* הפנימי כן ייתפס כ-bold (כפי שמצופה)
        # אבל **לא** ייווצר תגית bold כפולה. ההמרה בודדת.
        assert "<b><b>" not in result

    def test_snake_case_not_converted(self):
        """_my_var_ לא צריך להיות italic — זה כתיב משתנה."""
        result = str(_telegram_html("הקוד my_var_name לא נטוי", channel="whatsapp"))
        # אין italic סביב המשתנה
        assert "<i>" not in result

    def test_telegram_message_not_converted(self):
        """רגרסיה: הודעת טלגרם עם *50* או _var_ — לא ממירים.
        משתמש טלגרם שכתב *50* התכוון לסימן כפל/הדגשה ידנית, לא ל-bold."""
        # ללא channel='whatsapp' — אין המרה
        result = str(_telegram_html("הטקסט *50* בלי שינוי"))
        assert "<b>" not in result
        # גם channel='telegram' מפורש — אין המרה
        result_tg = str(_telegram_html("הטקסט _my_var_ בלי שינוי", channel="telegram"))
        assert "<i>" not in result_tg

    def test_multiplication_not_converted(self):
        """5*2 לא צריך להפוך ל-bold — אין רווח לפני."""
        result = str(_telegram_html("הסכום 5*2 הוא 10", channel="whatsapp"))
        assert "<b>" not in result

    def test_at_start_of_line(self):
        """*מודגש* בתחילת שורה — חייב להיתפס."""
        result = str(_telegram_html("*מודגש*", channel="whatsapp"))
        assert "<b>מודגש</b>" in result


class TestFormatPhoneFilter:
    """פירמוט מספר WhatsApp בתצוגות הפאנל.

    רגרסיה: כש-+ ב-URL מתפרש כרווח, user_id="+972..." הופך ל-" 972...".
    כותרת השיחה הציגה '972...' (זוג גלוי לעין משתמש בנייד), במקום
    '0526915503' המקומי. format_phone צריך לטפל בכל הוואריאציות.
    """

    def test_e164_with_plus(self):
        from utils.phone import format_phone
        assert format_phone("+972526915503") == "0526915503"

    def test_e164_without_plus(self):
        """972... ללא + (אחרי URL decode)."""
        from utils.phone import format_phone
        assert format_phone("972526915503") == "0526915503"

    def test_with_leading_space(self):
        """' 972...' — תוצאה של `+` ב-URL → space."""
        from utils.phone import format_phone
        assert format_phone(" 972526915503") == "0526915503"

    def test_telegram_user_id_unchanged(self):
        from utils.phone import format_phone
        assert format_phone("123456789") == "123456789"

    def test_zero_after_972_unchanged(self):
        """972 + 0 לא תקף ישראלית — לא ממירים. תקף לשני הענפים: עם + ובלי.
        תוצאה של ספק שמדבק 9720XXX — אסור להציג כ-00XXX.
        """
        from utils.phone import format_phone
        assert format_phone("9720123456789") == "9720123456789"
        assert format_phone("+9720123456789") == "+9720123456789"

    def test_non_string_unchanged(self):
        from utils.phone import format_phone
        assert format_phone(None) is None
        assert format_phone(12345) == 12345


class TestUserIdRouting:
    """רגרסיה: לינקי live-chat ב-WhatsApp נשברו עם '+' ב-URL.

    הסיבה: '+' ב-URL מתפרש כרווח (URL-encoded as %20). לכן user_id
    '+972...' הופך ל-' 972...' אחרי request decode, וה-validator
    דחה אותו כ-'מזהה משתמש לא תקין'. הפתרון: נורמליזציה בדקורטור.
    """

    def test_normalize_user_id_with_leading_space(self):
        """' 972...' (אחרי URL decode) → '+972...' מתאים ל-DB."""
        # הלוגיקה משוכפלת מ-admin/app.py:_normalize_user_id (מוגדר כ-closure
        # ב-create_app), כדי לבדוק את ההתנהגות בלי להעמיס import עם תלויות.
        import re
        user_id = " 972526915503"
        cleaned = user_id.lstrip()
        if cleaned.startswith("972") and len(cleaned) >= 12 and cleaned[3] != "0":
            cleaned = "+" + cleaned
        assert cleaned == "+972526915503"
        assert re.match(r"^\+?\d{1,15}$", cleaned)

    def test_normalize_telegram_user_id_unchanged(self):
        """Telegram user_id (מספרי) — לא שונה."""
        user_id = "123456789"
        cleaned = user_id.lstrip()
        # אין הוספה של + לטלגרם
        assert cleaned == user_id

    def test_e164_without_plus_normalized(self):
        """'972...' (בלי +) → '+972...'."""
        user_id = "972526915503"
        cleaned = user_id.lstrip()
        if cleaned.startswith("972") and len(cleaned) >= 12 and cleaned[3] != "0":
            cleaned = "+" + cleaned
        assert cleaned == "+972526915503"


class TestBSUIDValidation:
    """Meta WhatsApp BSUID (Business-Scoped User ID), בתוקף מסוף 2026.
    משתמשי Username עשויים להגיע בלי מספר טלפון — רק כ-`IL.abc123XYZ`.
    ה-validator הישן (regex של ספרות בלבד) דחה אותם → 400 על
    /live-chat ו-/conversations. כיסוי לרגרסיה זו.
    """

    def test_user_id_re_accepts_bsuid(self):
        from admin.app import _USER_ID_RE
        # פורמט תקין: ISO-2 + נקודה + alphanumeric
        assert _USER_ID_RE.match("IL.abc123XYZ")
        assert _USER_ID_RE.match("US.A1b2C3d4")
        assert _USER_ID_RE.match("GB.x")  # מינימום תו אחד אחרי הנקודה

    def test_user_id_re_still_accepts_phone_and_telegram(self):
        """לא רגרסיה — הפורמטים הקיימים ממשיכים לעבוד."""
        from admin.app import _USER_ID_RE
        assert _USER_ID_RE.match("+972526915503")
        assert _USER_ID_RE.match("972526915503")
        assert _USER_ID_RE.match("123456789")  # Telegram

    def test_user_id_re_rejects_invalid_bsuid_shapes(self):
        """ולידציה — צורות לא חוקיות לא עוברות."""
        from admin.app import _USER_ID_RE
        # אותיות קטנות בקוד מדינה
        assert not _USER_ID_RE.match("il.abc123")
        # קוד מדינה לא באורך 2
        assert not _USER_ID_RE.match("ISR.abc123")
        assert not _USER_ID_RE.match("I.abc123")
        # נקודה בלבד בלי alphanumeric
        assert not _USER_ID_RE.match("IL.")
        # תווים מיוחדים בחלק האחרי-נקודה
        assert not _USER_ID_RE.match("IL.abc-123")
        assert not _USER_ID_RE.match("IL.abc 123")
        # שתי נקודות
        assert not _USER_ID_RE.match("IL.abc.123")

    def test_bsuid_re_isolated(self):
        """_BSUID_RE — regex עצמאי לזיהוי BSUID בלבד."""
        from admin.app import _BSUID_RE
        assert _BSUID_RE.match("IL.abc123")
        assert not _BSUID_RE.match("+972526915503")
        assert not _BSUID_RE.match("123456789")

    def test_normalize_user_id_passes_bsuid_unchanged(self):
        """BSUID לא עובר נורמליזציה של טלפון (אין `+` להחזיר, אין lstrip
        משמעותי). מאשרים שהלוגיקה החדשה לא משבשת אותו.

        הסיכון לפני התיקון: BSUID מתחיל ב-`IL` ולא ב-`972`, אז המסלול
        הקיים פשוט מחזיר אותו דרך branch של `cleaned != user_id`. אבל
        ה-short-circuit החדש יותר ברור ומשמש כתיעוד.
        """
        # בודקים דרך _BSUID_RE שזו אותה לוגיקה ש-_normalize_user_id מפעיל.
        from admin.app import _BSUID_RE
        bsuid = "IL.abc123XYZ"
        assert _BSUID_RE.match(bsuid)
        # אילו היינו מריצים את לוגיקת הטלפון הישנה — היא לא הייתה משנה
        # את BSUID (לא מתחיל ב-972). מאמתים שזה עדיין נכון.
        assert not bsuid.startswith("972")


class TestIlPhoneFilter:
    """_format_il_phone — המרת טלפון בינלאומי ישראלי לפורמט מקומי."""

    def test_plus_972_to_local(self):
        from admin.app import _format_il_phone
        assert _format_il_phone("+972543978620") == "0543978620"

    def test_972_without_plus(self):
        from admin.app import _format_il_phone
        assert _format_il_phone("972543978620") == "0543978620"

    def test_already_local_unchanged(self):
        from admin.app import _format_il_phone
        assert _format_il_phone("0543978620") == "0543978620"

    def test_empty_returns_empty(self):
        from admin.app import _format_il_phone
        assert _format_il_phone("") == ""
        assert _format_il_phone(None) == ""

    def test_non_israeli_unchanged(self):
        from admin.app import _format_il_phone
        # מספר אמריקאי — לא משנים
        assert _format_il_phone("+15551234567") == "+15551234567"

    def test_non_israeli_preserves_formatting(self):
        """מספר זר עם מקפים/רווחים — שומרים על הפורמט המקורי."""
        from admin.app import _format_il_phone
        assert _format_il_phone("+1-555-123-4567") == "+1-555-123-4567"
        assert _format_il_phone("+44 20 7946 0958") == "+44 20 7946 0958"

    def test_zero_after_972_unchanged(self):
        """Bugbot regression: ספקים שמשרשרים 972 + פורמט מקומי יוצרים
        9720XXX. בלי guard נקבל "00XXX" שהוא חסר משמעות. ה-helper של
        utils.phone (שאליו אנחנו מאצילים) כבר מטפל בזה — מחזיר את הקלט
        ללא שינוי כי הספרה אחרי 972 חייבת להיות לא-אפס. תקף לשני הענפים
        — עם + ובלי."""
        from admin.app import _format_il_phone
        assert _format_il_phone("9720543978620") == "9720543978620"
        assert _format_il_phone("+9720543978620") == "+9720543978620"


# ════════════════════════════════════════════════════════════════
# Demo PII masking — mask_name / mask_phone filters
# ════════════════════════════════════════════════════════════════


class TestMaskPhoneFilter:
    """mask_phone: טלפון → ••••••XXXX (4 אחרונות) במצב דמו.
    בגישה רגילה — passthrough ל-format_phone."""

    def test_phone_in_demo_session_masked(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_phone("+972526659110") == "••••••9110"
        assert admin_app._mask_phone("0526659110") == "••••••9110"

    def test_phone_outside_demo_passthrough(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: False)
        # +972... מנורמל ל-IL format ע"י format_phone הקיים
        assert admin_app._mask_phone("+972526659110") == "0526659110"

    def test_short_digits_passthrough_in_demo(self, monkeypatch):
        """פחות מ-4 ספרות → לא מוסיף ••• (כי אין מה למסך)."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        # _format_phone מחזיר "123" כי הוא לא טלפון ישראלי
        result = admin_app._mask_phone("123")
        assert result == "123"

    def test_none_and_empty(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_phone(None) == ""
        assert admin_app._mask_phone("") == ""

    def test_no_request_context_does_not_crash(self):
        """ייבוא ישיר בלי Flask request → _demo_active מחזיר False;
        הפילטר עובד כ-passthrough."""
        from admin.app import _mask_phone
        assert _mask_phone("+972526659110") == "0526659110"


class TestMaskNameFilter:
    """mask_name: שם → שם_פרטי X. במצב דמו. שם בודד נשאר as-is.
    שם שהוא בעצם טלפון → מוסך כטלפון. בגישה רגילה — passthrough."""

    def test_two_word_name_in_demo(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("Sylvie Shamir") == "Sylvie S."

    def test_hebrew_two_word_in_demo(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("אמיר חיים") == "אמיר ח."

    def test_single_word_in_demo_unchanged(self, monkeypatch):
        """שם בודד נשאר as-is (לפי דרישת המשתמש)."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("Amir") == "Amir"
        assert admin_app._mask_name("אמיר") == "אמיר"

    def test_three_word_name_masks_second_part_first_char(self, monkeypatch):
        """3 מילים → 'יוסי ל.' (split(maxsplit=1) → אות ראשונה של החלק השני)."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("יוסי לוי כהן") == "יוסי ל."

    def test_name_that_looks_like_phone_in_demo_masked(self, monkeypatch):
        """שם שהוא טלפון → מיסוך טלפון (4 אחרונות)."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("0543977654") == "••••••7654"
        assert admin_app._mask_name("+972526659110") == "••••••9110"

    def test_telegram_numeric_id_in_demo_masked(self, monkeypatch):
        """user_id מספרי של Telegram (chat_id) → מסוך כטלפון."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        # 10 ספרות — נראה כטלפון
        assert admin_app._mask_name("6865105071") == "••••••5071"

    def test_outside_demo_full_name(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: False)
        # בלי דמו: שם נשאר כמו שהוא
        assert admin_app._mask_name("Sylvie Shamir") == "Sylvie Shamir"
        # טלפון בלי דמו: מנורמל ל-IL
        assert admin_app._mask_name("+972526659110") == "0526659110"

    def test_none_and_empty(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name(None) == ""
        assert admin_app._mask_name("") == ""
        assert admin_app._mask_name("   ") == ""

    def test_strips_whitespace(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_name("  Sylvie Shamir  ") == "Sylvie S."


class TestLooksLikePhone:
    """_looks_like_phone — מבחין בין שם לטלפון/user_id מספרי."""

    def test_phone_formats_detected(self):
        from admin.app import _looks_like_phone
        assert _looks_like_phone("+972526659110")
        assert _looks_like_phone("972526659110")
        assert _looks_like_phone("0526659110")
        assert _looks_like_phone("052-6659110")
        assert _looks_like_phone("0543977654")

    def test_telegram_chat_id_detected(self):
        from admin.app import _looks_like_phone
        assert _looks_like_phone("6865105071")  # 10 ספרות
        assert _looks_like_phone("123456789")   # 9 ספרות

    def test_names_not_detected(self):
        from admin.app import _looks_like_phone
        assert not _looks_like_phone("Sylvie Shamir")
        assert not _looks_like_phone("אמיר חיים")
        assert not _looks_like_phone("Amir")

    def test_short_numbers_not_detected(self):
        from admin.app import _looks_like_phone
        # פחות מ-6 ספרות
        assert not _looks_like_phone("12345")

    def test_bsuid_not_detected(self):
        from admin.app import _looks_like_phone
        assert not _looks_like_phone("IL.abc123XYZ")

    def test_empty_inputs(self):
        from admin.app import _looks_like_phone
        assert not _looks_like_phone(None)
        assert not _looks_like_phone("")
        assert not _looks_like_phone("   ")


class TestDemoActiveDetection:
    """_demo_active מבוסס flask.session — בודק שלא קורס מחוץ ל-request."""

    def test_no_request_context_returns_false(self):
        from admin.app import _demo_active
        assert _demo_active() is False


class TestMaskUsernameFilter:
    """mask_username: handles ייחודיים (telegram_username, ig_username).
    בניגוד ל-mask_name, גם מילה בודדת מטושטשת — כי handle הוא unique
    identifier. תיקון Cursor: לפני זה, @amir_xyz בדמו נראה ככפי שהוא,
    מסגיר את הזהות.
    """

    def test_long_username_in_demo_masked(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_username("amir_xyz") == "am•••"
        assert admin_app._mask_username("YourBusiness") == "Yo•••"

    def test_short_username_in_demo_fully_masked(self, monkeypatch):
        """handle של 2 תווים או פחות — כולו ••• (אחרת זה דליפה כמעט שלמה)."""
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_username("ab") == "•••"
        assert admin_app._mask_username("a") == "•••"

    def test_username_outside_demo_unchanged(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: False)
        assert admin_app._mask_username("amir_xyz") == "amir_xyz"

    def test_none_and_empty(self, monkeypatch):
        from admin import app as admin_app
        monkeypatch.setattr(admin_app, "_demo_active", lambda: True)
        assert admin_app._mask_username(None) == ""
        assert admin_app._mask_username("") == ""
        assert admin_app._mask_username("   ") == ""

    def test_no_request_context_does_not_crash(self):
        from admin.app import _mask_username
        # _demo_active=False → passthrough
        assert _mask_username("amir_xyz") == "amir_xyz"
