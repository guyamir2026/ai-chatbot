"""
טסטים למודול זיהוי כוונות — intent.py

בודק שלוש שכבות:
  1. detect_intent() — regex מהיר לברכות/פרידות בלבד
  2. _detect_intent_regex_full() — fallback regex מלא (כל הכוונות)
  3. detect_intent_with_llm() — היברידי: regex fast → LLM → regex fallback
  4. _detect_intent_llm() — unit test ל-function calling מול mock
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from intent import (
    Intent,
    detect_intent,
    detect_intent_with_llm,
    _detect_intent_regex_full,
    _detect_intent_llm,
    get_direct_response,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fast path — detect_intent() (regex: ברכות ופרידות בלבד)
# ══════════════════════════════════════════════════════════════════════════════

class TestGreetingFast:
    @pytest.mark.parametrize("msg", [
        "שלום", "היי", "הי", "בוקר טוב", "ערב טוב", "מה נשמע",
        "אהלן", "הלו",
        "hi", "hello", "hey", "Hi!", "Hello.",
        "good morning", "good evening",
    ])
    def test_greeting_detected(self, msg):
        assert detect_intent(msg) == Intent.GREETING

    @pytest.mark.parametrize("msg", [
        "שלום, כמה עולה תספורת?",
        "hi how much is a haircut",
        "hello I want to book an appointment",
    ])
    def test_greeting_with_follow_up_not_greeting(self, msg):
        """ברכה עם שאלה נוספת לא צריכה להסתווג כברכה."""
        assert detect_intent(msg) != Intent.GREETING

    def test_greeting_has_direct_response(self):
        resp = get_direct_response(Intent.GREETING)
        assert resp is not None
        assert len(resp) > 0


class TestFarewellFast:
    @pytest.mark.parametrize("msg", [
        "תודה", "תודה רבה", "ביי", "להתראות", "יום טוב",
        "thanks", "thank you", "bye", "goodbye",
    ])
    def test_farewell_detected(self, msg):
        assert detect_intent(msg) == Intent.FAREWELL

    def test_farewell_has_direct_response(self):
        resp = get_direct_response(Intent.FAREWELL)
        assert resp is not None


class TestFastPathGeneral:
    @pytest.mark.parametrize("msg", [
        "כמה עולה תספורת?",
        "שעות פתיחה",
        "רוצה תור",
        "",
        "   ",
    ])
    def test_non_greeting_farewell_returns_general(self, msg):
        """detect_intent() מחזיר GENERAL לכל מה שאינו ברכה/פרידה."""
        assert detect_intent(msg) == Intent.GENERAL


# ══════════════════════════════════════════════════════════════════════════════
# Regex fallback מלא — _detect_intent_regex_full()
# ══════════════════════════════════════════════════════════════════════════════

class TestRegexFullBusinessHours:
    @pytest.mark.parametrize("msg", [
        "שעות פתיחה", "מתי אתם פותחים?", "אתם פתוחים?",
        "פתוח היום?", "פתוחים עכשיו?", "עד מתי פתוחים?",
        "are you open", "what are your hours", "business hours",
        "is the salon open",
    ])
    def test_business_hours_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.BUSINESS_HOURS


class TestRegexFullPricing:
    @pytest.mark.parametrize("msg", [
        "כמה עולה תספורת?", "מה המחיר?", "מחירון",
        "how much is a haircut?", "what's the price?", "pricing",
    ])
    def test_pricing_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.PRICING

    def test_pricing_before_booking(self):
        """'כמה עולה לקבוע תור' — מחיר מנצח את קביעת תור."""
        assert _detect_intent_regex_full("כמה עולה לקבוע תור?") == Intent.PRICING


class TestRegexFullBooking:
    @pytest.mark.parametrize("msg", [
        "רוצה תור", "רוצה לקבוע תור", "אפשר תור?",
        "book an appointment", "I want to book",
    ])
    def test_booking_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.APPOINTMENT_BOOKING


class TestRegexFullCancel:
    @pytest.mark.parametrize("msg", [
        "לבטל תור", "ביטול תור", "רוצה לבטל את התור",
        "cancel my appointment", "I want to cancel my booking",
    ])
    def test_cancel_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.APPOINTMENT_CANCEL


class TestRegexFullReschedule:
    @pytest.mark.parametrize("msg", [
        "לשנות את התור", "שינוי תור", "רוצה לשנות את התור",
        "להזיז את התור", "לדחות את התור", "להקדים את התור",
        "reschedule my appointment", "change the date of my appointment",
        "אני רוצה לשנות את התור",
        "לשנות את השעה של התור", "לשנות את התאריך של התור",
    ])
    def test_reschedule_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.APPOINTMENT_RESCHEDULE


class TestRegexFullHumanAgent:
    @pytest.mark.parametrize("msg", [
        "נציג",
        "תעביר אותי לנציג",
        "תעבירו אותי לנציג",
        "אני רוצה לדבר עם בנאדם",
        "אפשר נציג",
        "אפשר לדבר עם מישהו",
        "לדבר עם מישהו",
        "רוצה נציג",
        "תן לי נציג",
        "תני לי נציג",
        "אדם אמיתי",
        "אני רוצה נציג",
        "talk to a human",
        "I need an agent",
        "transfer me to a representative",
        "can I speak to a person",
        # רגרסיה — ניסוחים שלא נתפסו והובילו לבאג WhatsApp:
        # ה-LLM ענה "אעביר את הפנייה" אבל לא נוצרה בקשת נציג כי
        # ה-intent זוהה כ-GENERAL במקום HUMAN_AGENT.
        "תעבירו את זה לבעל העסק",
        "תעביר את ההודעה לבעלים",
        "אפשר לדבר עם בעל העסק?",
        "אני רוצה לדבר עם בעל העסק",
        "מבקש שיחזרו אלי",
        "שיחזור אלי בעל העסק",
        "בעל העסק יחזור אלי בבקשה",
    ])
    def test_human_agent_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.HUMAN_AGENT

    def test_complaint_not_triggered_by_agent_request(self):
        assert _detect_intent_regex_full("תעביר לנציג") != Intent.COMPLAINT
        assert _detect_intent_regex_full("רוצה נציג") != Intent.COMPLAINT


class TestRegexFullComplaint:
    @pytest.mark.parametrize("msg", [
        "אני לא מרוצה", "רוצה להתלונן", "שירות גרוע",
        "יש לי בעיה", "שירות נוראי", "מאוכזב", "מאוכזבת", "חוויה רעה",
        "אוי נו", "באסה", "דבילי", "שירות על הפנים", "לא עונה על השאלה",
        "בושה", "בושה וחרפה", "איזה זלזול", "שירות פח",
        "עושים צחוק", "עושה צחוק",
        "תבטלו את ההזמנה", "רוצה זיכוי", "תחזירו לי את הכסף",
        "אני עוזב", "לא קונה אצלכם יותר", "לא קונה פה יותר",
        "מחכה כבר שעות", "אף אחד לא עונה",
        "i want to complain", "terrible service",
        "i want a refund", "give me my money back",
    ])
    def test_complaint_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.COMPLAINT


class TestRegexFullLocation:
    @pytest.mark.parametrize("msg", [
        "מה הכתובת שלכם?", "איפה אתם?",
        "איך מגיעים אליכם?",
        "where are you?", "what is your address?",
    ])
    def test_location_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.LOCATION


class TestRegexFullGeneral:
    @pytest.mark.parametrize("msg", [
        "ספרו לי על השירותים",
        "what services do you offer?",
        "", "   ",
    ])
    def test_general_detected(self, msg):
        assert _detect_intent_regex_full(msg) == Intent.GENERAL


# ══════════════════════════════════════════════════════════════════════════════
# LLM intent detection — _detect_intent_llm() עם mock
# ══════════════════════════════════════════════════════════════════════════════

def _mock_llm_response(intent_value: str):
    """יוצר mock response של OpenAI function calling."""
    tool_call = MagicMock()
    tool_call.function.arguments = json.dumps({"intent": intent_value})

    message = MagicMock()
    message.tool_calls = [tool_call]

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    return response


class TestLLMIntentDetection:
    """בדיקת _detect_intent_llm() עם mock לקריאת OpenAI."""

    @pytest.mark.parametrize("msg,expected_intent", [
        ("אפשר להגיע מחר?", Intent.APPOINTMENT_BOOKING),
        ("יש לכם מקום ביום שלישי?", Intent.APPOINTMENT_BOOKING),
        ("זה יקר לי", Intent.PRICING),
        ("פתוחים בשבת?", Intent.BUSINESS_HOURS),
        ("איפה זה?", Intent.LOCATION),
        ("אני מתוסכל מהשירות", Intent.COMPLAINT),
        ("ספרו לי על השירותים", Intent.GENERAL),
    ])
    def test_llm_classifies_correctly(self, msg, expected_intent):
        mock_response = _mock_llm_response(expected_intent.value)
        with patch("ai_chatbot.openai_client.get_openai_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_client_fn.return_value = mock_client

            result = _detect_intent_llm(msg)
            assert result == expected_intent

            # לוודא שהקריאה נעשתה עם הפרמטרים הנכונים
            call_kwargs = mock_client.chat.completions.create.call_args[1]
            assert call_kwargs["temperature"] == 0
            assert len(call_kwargs["tools"]) == 1
            assert call_kwargs["tools"][0]["function"]["name"] == "classify_intent"

    def test_llm_failure_falls_back_to_regex(self):
        """כשל ב-LLM → חוזר ל-regex מלא."""
        with patch("ai_chatbot.openai_client.get_openai_client") as mock_client_fn:
            mock_client_fn.side_effect = Exception("API error")

            result = _detect_intent_llm("כמה עולה תספורת?")
            # regex מלא צריך לתפוס את זה כ-PRICING
            assert result == Intent.PRICING

    def test_llm_invalid_intent_returns_general(self):
        """ערך לא תקין מה-LLM → GENERAL."""
        mock_response = _mock_llm_response("invalid_intent")
        with patch("ai_chatbot.openai_client.get_openai_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_client_fn.return_value = mock_client

            result = _detect_intent_llm("בלה בלה")
            assert result == Intent.GENERAL


# ══════════════════════════════════════════════════════════════════════════════
# Hybrid — detect_intent_with_llm()
# ══════════════════════════════════════════════════════════════════════════════

class TestHybridDetection:
    """בדיקת הזרימה ההיברידית: regex fast → LLM → regex fallback."""

    def test_greeting_skips_llm(self):
        """ברכה מזוהה ב-regex — לא קוראים ל-LLM."""
        with patch("intent._detect_intent_llm") as mock_llm:
            result = detect_intent_with_llm("שלום")
            assert result == Intent.GREETING
            mock_llm.assert_not_called()

    def test_farewell_skips_llm(self):
        """פרידה מזוהה ב-regex — לא קוראים ל-LLM."""
        with patch("intent._detect_intent_llm") as mock_llm:
            result = detect_intent_with_llm("תודה")
            assert result == Intent.FAREWELL
            mock_llm.assert_not_called()

    def test_non_greeting_calls_llm_when_enabled(self):
        """הודעה שאינה ברכה/פרידה — LLM נקרא כש-LLM_INTENT_ENABLED=True."""
        with patch("intent._detect_intent_llm", return_value=Intent.APPOINTMENT_BOOKING) as mock_llm, \
             patch("ai_chatbot.config.LLM_INTENT_ENABLED", True):
            result = detect_intent_with_llm("אפשר להגיע מחר?")
            assert result == Intent.APPOINTMENT_BOOKING
            mock_llm.assert_called_once()

    def test_llm_disabled_falls_back_to_regex(self):
        """כש-LLM מושבת — חוזר ל-regex מלא."""
        with patch("intent._detect_intent_llm") as mock_llm, \
             patch("ai_chatbot.config.LLM_INTENT_ENABLED", False):
            result = detect_intent_with_llm("כמה עולה תספורת?")
            assert result == Intent.PRICING
            mock_llm.assert_not_called()

    def test_empty_message_returns_general(self):
        assert detect_intent_with_llm("") == Intent.GENERAL
        assert detect_intent_with_llm("   ") == Intent.GENERAL


# ══════════════════════════════════════════════════════════════════════════════
# Direct responses
# ══════════════════════════════════════════════════════════════════════════════

class TestDirectResponses:
    def test_greeting_has_direct_response(self):
        assert get_direct_response(Intent.GREETING) is not None

    def test_farewell_has_direct_response(self):
        assert get_direct_response(Intent.FAREWELL) is not None

    def test_business_hours_no_direct_response(self):
        assert get_direct_response(Intent.BUSINESS_HOURS) is None

    def test_general_no_direct_response(self):
        assert get_direct_response(Intent.GENERAL) is None

    def test_human_agent_no_direct_response(self):
        assert get_direct_response(Intent.HUMAN_AGENT) is None

    def test_complaint_no_direct_response(self):
        assert get_direct_response(Intent.COMPLAINT) is None

    def test_location_no_direct_response(self):
        assert get_direct_response(Intent.LOCATION) is None
