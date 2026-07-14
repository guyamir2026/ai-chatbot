"""
טסטים עבור messaging/formatter.py — המרת HTML לפורמט ערוצים שונים.
"""

from messaging.formatter import format_message


class TestFormatMessageTelegram:
    """טלגרם — ללא שינוי, מחזיר HTML כמו שהוא."""

    def test_bold_unchanged(self):
        assert format_message("<b>bold</b>", "telegram") == "<b>bold</b>"

    def test_italic_unchanged(self):
        assert format_message("<i>italic</i>", "telegram") == "<i>italic</i>"

    def test_underline_unchanged(self):
        assert format_message("<u>underline</u>", "telegram") == "<u>underline</u>"

    def test_link_unchanged(self):
        html = '<a href="https://example.com">link</a>'
        assert format_message(html, "telegram") == html

    def test_code_unchanged(self):
        assert format_message("<code>x=1</code>", "telegram") == "<code>x=1</code>"

    def test_complex_unchanged(self):
        html = "<b>כותרת</b>\n<i>הערה</i>\n<u>חשוב</u>"
        assert format_message(html, "telegram") == html


class TestFormatMessageWhatsApp:
    """WhatsApp — המרת HTML לפורמט WhatsApp."""

    def test_bold(self):
        assert format_message("<b>bold</b>", "whatsapp") == "*bold*"

    def test_italic(self):
        assert format_message("<i>italic</i>", "whatsapp") == "_italic_"

    def test_underline_removed(self):
        """WhatsApp לא תומך ב-underline — התג מוסר, הטקסט נשאר."""
        assert format_message("<u>underline</u>", "whatsapp") == "underline"

    def test_link(self):
        html = '<a href="https://example.com">link text</a>'
        assert format_message(html, "whatsapp") == "link text (https://example.com)"

    def test_code(self):
        assert format_message("<code>x=1</code>", "whatsapp") == "`x=1`"

    def test_nested_bold_italic(self):
        """תגים מקוננים — bold בתוך italic."""
        html = "<i><b>bold italic</b></i>"
        result = format_message(html, "whatsapp")
        assert "*bold italic*" in result
        assert "_" in result

    def test_mixed_content(self):
        html = "<b>כותרת</b>\nטקסט רגיל\n<i>הערה</i>"
        result = format_message(html, "whatsapp")
        assert "*כותרת*" in result
        assert "טקסט רגיל" in result
        assert "_הערה_" in result

    def test_plain_text_unchanged(self):
        """טקסט ללא HTML — ללא שינוי."""
        assert format_message("שלום עולם", "whatsapp") == "שלום עולם"

    def test_unknown_tags_removed(self):
        """תגים לא ידועים — הסרה."""
        assert format_message("<s>deleted</s>", "whatsapp") == "deleted"

    def test_link_with_single_quotes(self):
        html = "<a href='https://example.com'>link</a>"
        assert format_message(html, "whatsapp") == "link (https://example.com)"

    def test_html_entities_decoded(self):
        """HTML entities מפוענחים בערוץ WhatsApp."""
        assert format_message("Terms &amp; Conditions", "whatsapp") == "Terms & Conditions"
        assert format_message("A &lt; B &gt; C", "whatsapp") == "A < B > C"
        assert format_message("it&#39;s here", "whatsapp") == "it's here"


class TestFormatMessageMeta:
    """Meta DM (Messenger + Instagram) — plain text, הסרת כל התגים."""

    def test_bold_stripped_messenger(self):
        assert format_message("<b>bold</b>", "meta_msg") == "bold"

    def test_bold_stripped_instagram(self):
        assert format_message("<b>bold</b>", "meta_ig") == "bold"

    def test_italic_stripped(self):
        assert format_message("<i>italic</i>", "meta_msg") == "italic"

    def test_underline_stripped(self):
        assert format_message("<u>underline</u>", "meta_ig") == "underline"

    def test_code_stripped(self):
        assert format_message("<code>x=1</code>", "meta_msg") == "x=1"

    def test_link_preserved_as_text_and_url(self):
        html = '<a href="https://example.com">link text</a>'
        assert format_message(html, "meta_msg") == "link text (https://example.com)"

    def test_plain_text_unchanged(self):
        assert format_message("שלום עולם", "meta_ig") == "שלום עולם"

    def test_html_entities_decoded(self):
        assert format_message("Terms &amp; Conditions", "meta_msg") == "Terms & Conditions"

    def test_real_price_list_no_raw_tags(self):
        """הדוגמה האמיתית מהפרודקשן: מחירון עם <b> לא יציג תגים גולמיים."""
        html = (
            "צהריים טובים,\n\n"
            "• <b>תספורת נשים ועיצוב</b>: 250₪\n"
            "• <b>תספורת גברים</b>: 100₪"
        )
        result = format_message(html, "meta_ig")
        assert "<b>" not in result and "</b>" not in result
        assert "תספורת נשים ועיצוב" in result
        assert "250₪" in result


class TestFormatMessageUnknownChannel:
    """ערוץ לא מוכר — הסרת כל התגים (כמו Meta — plain text בטוח)."""

    def test_unknown_channel_strips_all_tags(self):
        html = "<b>bold</b> <i>italic</i>"
        assert format_message(html, "sms") == "bold italic"
