"""
טסטים ל-memory/extractor.py (שלב 3 של מערכת הזיכרון).

מכסה: short-circuit על שיחות קצרצרות, בניית prompt, retry, parsing,
ו-pre-filter ל-existing_facts כשיש > 8. הקריאות ל-OpenAI מ-mocked דרך
patch על get_memory_openai_client (לא קוראים ל-API אמיתי).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ייבוא ברמת המודול — _isolate_env autouse מוודא ש-OPENAI_API_KEY לא חסום
from memory import extractor


def _mock_openai_client(json_response: str | dict, total_tokens: int = 100):
    """בונה mock client שמחזיר את ה-content הנתון כתשובה לכל chat.completions
    + embeddings סטנדרטיים."""
    if isinstance(json_response, dict):
        json_response = json.dumps(json_response, ensure_ascii=False)

    client = MagicMock()
    msg = SimpleNamespace(content=json_response)
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(total_tokens=total_tokens)
    chat_resp = SimpleNamespace(choices=[choice], usage=usage)
    client.chat.completions.create.return_value = chat_resp

    # embeddings — מחזיר וקטור פשוט לכל קלט (לא נדרש לרוב הטסטים)
    def _embeddings_create(model, input):
        n = len(input) if isinstance(input, list) else 1
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[1.0, 0.0, 0.0]) for _ in range(n)]
        )
    client.embeddings.create.side_effect = _embeddings_create
    return client


class TestShortCircuit:
    def test_empty_conversation_returns_empty(self):
        result = extractor.extract_facts(
            user_id="u1", business_id="default",
            conversation=[],
            business_profile={}, existing_facts=[],
        )
        assert result["success"] is True
        assert result["extractions"] == []
        assert result["skipped"] == []
        assert result["tokens_used"] == 0

    def test_single_message_returns_empty(self):
        """שיחה של הודעה אחת — בלי context לעומק, לא שווה לקרוא ל-LLM."""
        result = extractor.extract_facts(
            user_id="u1", business_id="default",
            conversation=[{"role": "user", "content": "שלום"}],
            business_profile={}, existing_facts=[],
        )
        assert result["success"] is True
        assert result["extractions"] == []

    def test_normalized_empty_returns_empty(self):
        """3 הודעות אבל כולן ריקות אחרי normalize → טופלות כ-short-circuit."""
        result = extractor.extract_facts(
            user_id="u1", business_id="default",
            conversation=[
                {"role": "user", "content": ""},
                {"role": "assistant", "content": None},
                {"role": "user", "content": "   "},  # רווחים בלבד
            ],
            business_profile={}, existing_facts=[],
        )
        assert result["success"] is True


class TestSuccessfulExtraction:
    def test_returns_parsed_llm_response(self):
        llm_output = {
            "extractions": [
                {
                    "action": "add",
                    "fact_type": "preference",
                    "content": "מעדיפה תורים בשעות הבוקר",
                    "requires_consent": False,
                    "confidence": 0.92,
                    "evidence": "הכי טוב לי בבוקר",
                    "supersedes_id": None,
                    "confirms_id": None,
                }
            ],
            "skipped": [],
        }
        client = _mock_openai_client(llm_output, total_tokens=523)
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "אני מעדיפה בקרים"},
                    {"role": "assistant", "content": "נרשם"},
                ],
                business_profile={
                    "business_type": "מספרה",
                    "business_name": "סטודיו לירון",
                    "services_json": json.dumps([
                        {"name": "תספורת", "aliases": [], "category": "תספורות"},
                    ]),
                    "what_matters_for_extraction": "סוג שיער",
                },
                existing_facts=[],
            )

        assert result["success"] is True
        assert result["error"] is None
        assert result["tokens_used"] == 523
        assert len(result["extractions"]) == 1
        assert result["extractions"][0]["fact_type"] == "preference"

    def test_empty_extractions_with_skipped(self):
        """no_extraction case — LLM מחזיר רק skipped."""
        llm_output = {
            "extractions": [],
            "skipped": [{"proposed_fact": "פנוי ב-17:30", "reason": "פרט רגעי"}],
        }
        client = _mock_openai_client(llm_output)
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "היום פנוי ב-17:30"},
                    {"role": "assistant", "content": "נרשם"},
                ],
                business_profile={}, existing_facts=[],
            )
        assert result["extractions"] == []
        assert len(result["skipped"]) == 1


class TestPromptBuilding:
    def test_prompt_contains_business_context(self):
        """ה-business_profile עובר ל-LLM בתוך ה-placeholder."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "שיחה"},
                    {"role": "assistant", "content": "תשובה"},
                ],
                business_profile={
                    "business_type": "קליניקת אסתטיקה",
                    "business_name": "קליניקת גלאם",
                    "services_json": json.dumps([
                        {"name": "מניקור ג'ל", "aliases": ["ג'ל"], "category": "ציפורניים"},
                    ]),
                    "what_matters_for_extraction": "סוג עור, רגישויות",
                },
                existing_facts=[],
            )
        call_args = client.chat.completions.create.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert "קליניקת אסתטיקה" in prompt
        assert "קליניקת גלאם" in prompt
        assert "מניקור ג'ל" in prompt
        assert "סוג עור, רגישויות" in prompt

    def test_prompt_does_not_leak_internal_fact_fields(self):
        """access_count / created_at / business_id לא נשלחים ל-LLM
        (רעש מיותר בקונטקסט). רק id/fact_type/content/confidence/
        requires_consent/status."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={},
                existing_facts=[{
                    "id": 5, "user_id": "u1", "business_id": "default",
                    "fact_type": "preference",
                    "content": "מעדיפה בקרים",
                    "confidence": 0.9,
                    "requires_consent": 0,
                    "status": "active",
                    "evidence": "evidence text",
                    "created_at": "2026-01-01 10:00:00",
                    # ערך sentinel ייחודי שלא מופיע בפרומפט הסטטי (דוגמאות
                    # ה-v2.2 משתמשות במספרים כמו 42/4587, אז לא לבחור אותם).
                    "access_count": 987654,
                    "superseded_by_id": None,
                    "source": "inferred",
                }],
            )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        # שדות פנימיים לא בפרומפט
        assert "access_count" not in prompt
        assert "987654" not in prompt
        assert "2026-01-01 10:00:00" not in prompt
        # שדות נחוצים כן
        assert "מעדיפה בקרים" in prompt

    def test_uses_json_schema_strict(self):
        """response_format חייב להיות json_schema strict, לא json_object."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={}, existing_facts=[],
            )
        call_kwargs = client.chat.completions.create.call_args.kwargs
        rf = call_kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "customer_fact_extraction"
        assert call_kwargs["temperature"] == 0.1

    def test_db_message_format_normalized(self):
        """conversation בפורמט DB ({role, message}) מומר ל-{role, content}
        לפני שליחה ל-LLM."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "message": "טקסט מ-DB", "created_at": "x"},
                    {"role": "assistant", "message": "תשובה"},
                ],
                business_profile={}, existing_facts=[],
            )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        # ההודעה מ-DB מופיעה בפרומפט
        assert "טקסט מ-DB" in prompt


class TestRetry:
    def test_retry_on_first_failure(self):
        """כשל ראשון → retry → הצלחה. tokens מסכומים בין הנסיונות."""
        client = MagicMock()
        # ניסיון ראשון: זורק חריגה. ניסיון שני: מחזיר תשובה תקינה.
        good_resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"extractions": [], "skipped": []}),
            ))],
            usage=SimpleNamespace(total_tokens=200),
        )
        client.chat.completions.create.side_effect = [
            RuntimeError("API timeout"),
            good_resp,
        ]
        with patch.object(extractor, "get_memory_openai_client", return_value=client), \
             patch.object(extractor.time, "sleep") as mock_sleep:
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={}, existing_facts=[],
            )
        assert result["success"] is True
        assert client.chat.completions.create.call_count == 2
        mock_sleep.assert_called_once_with(extractor._RETRY_DELAY_SECONDS)
        assert result["tokens_used"] == 200

    def test_both_attempts_fail(self):
        """שני הנסיונות נכשלים → success=False עם error."""
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("API down")
        with patch.object(extractor, "get_memory_openai_client", return_value=client), \
             patch.object(extractor.time, "sleep"):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={}, existing_facts=[],
            )
        assert result["success"] is False
        assert result["extractions"] == []
        assert "RuntimeError" in (result["error"] or "")
        assert client.chat.completions.create.call_count == 2

    def test_invalid_json_triggers_retry(self):
        """LLM החזיר JSON שבור → retry. (תרחיש נדיר עם strict, אבל אפשרי
        בכשל infrastructure)."""
        client = MagicMock()
        bad = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="not json at all { ]",
            ))],
            usage=SimpleNamespace(total_tokens=50),
        )
        good = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"extractions": [], "skipped": []}),
            ))],
            usage=SimpleNamespace(total_tokens=100),
        )
        client.chat.completions.create.side_effect = [bad, good]
        with patch.object(extractor, "get_memory_openai_client", return_value=client), \
             patch.object(extractor.time, "sleep"):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={}, existing_facts=[],
            )
        assert result["success"] is True
        # tokens של שתי הקריאות מסכומים
        assert result["tokens_used"] == 150


class TestPreFilter:
    def test_no_filter_when_under_threshold(self):
        """≤8 facts → לא מפעילים pre-filter; שולחים את כולם."""
        facts = [
            {"id": i, "fact_type": "preference", "content": f"f{i}",
             "confidence": 0.9, "requires_consent": 0, "status": "active"}
            for i in range(8)
        ]
        result = extractor._pre_filter_existing_facts(facts, [])
        assert len(result) == 8

    def test_keeps_all_open_issues(self):
        """open_issues תמיד נכנסים, גם אם הם מעבר ל-cap הסמנטי."""
        open_issues = [
            {"id": i, "fact_type": "open_issue", "content": f"issue{i}",
             "confidence": 0.7, "requires_consent": 0, "status": "active"}
            for i in range(5)
        ]
        others = [
            {"id": 100 + i, "fact_type": "preference", "content": f"pref{i}",
             "confidence": 0.9, "requires_consent": 0, "status": "active"}
            for i in range(10)
        ]
        # סף לדמיון cosine — mock להחזיר וקטור שווה לכולם, כך שהסדר נשמר
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor._pre_filter_existing_facts(
                open_issues + others,
                [{"role": "user", "content": "שיחה"}],
            )
        open_issue_count = sum(1 for f in result if f["fact_type"] == "open_issue")
        assert open_issue_count == 5
        # סך הכל לא עובר את ה-cap
        from ai_chatbot.config import MEMORY_EXISTING_FACTS_CAP
        assert len(result) <= MEMORY_EXISTING_FACTS_CAP

    def test_fallback_when_embeddings_fail(self):
        """אם embeddings זורק חריגה — חוזרים לסדר המקורי בלי לקרוס."""
        facts = [
            {"id": i, "fact_type": "preference", "content": f"f{i}",
             "confidence": 0.9 - i * 0.01, "requires_consent": 0,
             "status": "active"}
            for i in range(15)
        ]
        client = MagicMock()
        client.embeddings.create.side_effect = RuntimeError("embed API down")
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor._pre_filter_existing_facts(
                facts, [{"role": "user", "content": "x"}],
            )
        # לא קורס, מחזיר רשימה לא ריקה
        assert isinstance(result, list)
        assert len(result) > 0


class TestConversationCap:
    def test_caps_long_conversation(self):
        """שיחה ארוכה (>MEMORY_CONVERSATION_CAP) נחתכת ל-N אחרונות."""
        from ai_chatbot.config import MEMORY_CONVERSATION_CAP

        long_convo = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg{i}"}
            for i in range(MEMORY_CONVERSATION_CAP + 20)
        ]
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=long_convo,
                business_profile={}, existing_facts=[],
            )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        # ההודעה הראשונה (0) לא בפרומפט; ההודעות האחרונות כן.
        assert "msg0" not in prompt
        assert f"msg{MEMORY_CONVERSATION_CAP + 19}" in prompt


class TestPromptInjectionResistance:
    """תוקן ב-PR #288 בעקבות סקירה (Low: prompt template injection).
    תוכן מבעל העסק / מ-facts קיימים שמכיל '{{conversation_json}}' או
    placeholders אחרים לא צריך לטרגר החלפה משנית — _render_prompt עושה
    single-pass עם regex."""

    def test_business_name_with_placeholder_does_not_leak(self):
        """business_name שמכיל "{{conversation_json}}" כטקסט לא צריך לגרום
        לזליגת ה-conversation לשם business_name."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "הודעה_סודית_של_משתמש"},
                    {"role": "assistant", "content": "תשובה"},
                ],
                business_profile={
                    "business_type": "אחר",
                    # הזרקה — שם העסק מכיל את ה-placeholder.
                    "business_name": "{{conversation_json}}",
                    "services_json": "[]",
                    "what_matters_for_extraction": "",
                },
                existing_facts=[],
            )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        # ההודעה הסודית מופיעה בדיוק פעם אחת — בתוך <conversation>,
        # לא בתוך <business_context> (זה היה הבאג בלעדי תיקון).
        assert prompt.count("הודעה_סודית_של_משתמש") == 1
        # ה-placeholder המקורי משוקף בתוך business_name כ-string JSON
        assert '"{{conversation_json}}"' in prompt or \
               "{{conversation_json}}" in prompt

    def test_existing_fact_content_with_placeholder_safe(self):
        """fact content שמכיל '{{existing_facts_json}}' לא מטרגר recursion."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "convo_message"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={},
                existing_facts=[{
                    "id": 1, "fact_type": "preference",
                    # תוכן ה-fact עצמו מכיל placeholder
                    "content": "{{conversation_json}} {{business_context_json}}",
                    "confidence": 0.9, "requires_consent": 0,
                    "status": "active",
                }],
            )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        # ה-conversation מופיע פעם אחת (לא הוזרק שוב לתוך ה-fact)
        assert prompt.count("convo_message") == 1


class TestRenderPromptUnit:
    """unit tests ל-_render_prompt עצמו — מבטיח single-pass."""

    def test_basic_substitution(self):
        result = extractor._render_prompt(
            "Hello {{name}}!", {"name": "World"},
        )
        assert result == "Hello World!"

    def test_unknown_placeholder_left_intact(self):
        result = extractor._render_prompt(
            "Hello {{name}} and {{unknown}}", {"name": "X"},
        )
        assert "{{unknown}}" in result
        assert "X" in result

    def test_single_pass_no_recursion(self):
        """אם הערך של placeholder מכיל placeholder אחר — לא מתבצעת
        החלפה משנית. זה ההבדל המרכזי מ-chained .replace()."""
        result = extractor._render_prompt(
            "{{a}} and {{b}}",
            {"a": "{{b}}", "b": "REAL"},
        )
        # אחרי תיקון: {{a}} → "{{b}}" (literal), {{b}} → "REAL"
        assert result == "{{b}} and REAL"

    def test_multiple_occurrences_same_placeholder(self):
        result = extractor._render_prompt(
            "{{x}} {{x}} {{x}}", {"x": "foo"},
        )
        assert result == "foo foo foo"


class TestEmbeddingModelIsolation:
    """ה-pre-filter מבצע embeddings דרך ה-client הבלעדי של memory עם
    MEMORY_EMBEDDING_MODEL (קבוע, text-embedding-3-small). זה חייב
    להיות נפרד מ-EMBEDDING_MODEL הראשי שעשוי להיות מכוון לספק אחר
    (Gemini למשל). תוקן ב-PR #288 בעקבות סקירה (Medium)."""

    def test_embedding_call_uses_memory_specific_model(self):
        """בדיקה ישירה: ה-call ל-embeddings.create חייב להעביר
        MEMORY_EMBEDDING_MODEL ולא איזה ENV-driven model אחר."""
        from ai_chatbot.config import MEMORY_EMBEDDING_MODEL

        # יוצרים מספיק facts כדי להפעיל את ה-pre-filter (סף = 8)
        facts = [
            {"id": i, "fact_type": "preference", "content": f"fact_{i}",
             "confidence": 0.9, "requires_consent": 0, "status": "active"}
            for i in range(15)
        ]
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "test"},
                    {"role": "assistant", "content": "response"},
                ],
                business_profile={}, existing_facts=facts,
            )

        # ה-embeddings.create נקרא לפחות פעם אחת ב-pre-filter
        assert client.embeddings.create.called, "pre-filter לא הפעיל embeddings"
        # ובכל קריאה — המודל הוא MEMORY_EMBEDDING_MODEL (text-embedding-3-small)
        for call in client.embeddings.create.call_args_list:
            model_used = call.kwargs.get("model") or (call.args[0] if call.args else None)
            assert model_used == MEMORY_EMBEDDING_MODEL, (
                f"embeddings נקרא עם {model_used!r}, אבל MEMORY_EMBEDDING_MODEL "
                f"= {MEMORY_EMBEDDING_MODEL!r}"
            )

    def test_memory_embedding_model_is_hardcoded_openai(self):
        """MEMORY_EMBEDDING_MODEL הוא קבוע (לא ENV-driven), כדי שלא
        ייקרא בטעות מ-EMBEDDING_MODEL הראשי של הבוט."""
        from ai_chatbot import config
        # לא ENV-driven — הקבוע חייב להישאר זהה גם אם EMBEDDING_MODEL
        # מוגדר אחרת ב-env (זה לא מה שאנחנו בודקים פה אבל זה ההצהרה).
        assert config.MEMORY_EMBEDDING_MODEL == "text-embedding-3-small"


class TestBusinessContextRobustness:
    def test_malformed_services_json_does_not_crash(self):
        """services_json לא תקין → ה-extractor לא קורס, services=[]."""
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={
                    "business_type": "אחר",
                    "services_json": "this is not json {",
                },
                existing_facts=[],
            )
        assert result["success"] is True

    def test_missing_business_profile_does_not_crash(self):
        client = _mock_openai_client({"extractions": [], "skipped": []})
        with patch.object(extractor, "get_memory_openai_client", return_value=client):
            result = extractor.extract_facts(
                user_id="u1", business_id="default",
                conversation=[
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                business_profile={},  # ריק לגמרי
                existing_facts=[],
            )
        assert result["success"] is True
