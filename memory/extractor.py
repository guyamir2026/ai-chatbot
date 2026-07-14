"""
Fact Extractor — שלב 3 של מערכת הזיכרון המתמשך פר-לקוח.

הפונקציה המרכזית `extract_facts()` קולטת שיחה שהסתיימה, פרופיל עסק
ועובדות קיימות, ומחזירה אובייקטים מובנים של extractions ו-skipped.
לא כותב ל-DB — שלב 4 (validator) אחראי על הוולידציה וההתמדה.

מאפיינים:
- Structured Outputs strict — OpenAI אוכף את הסכמה (extractor_schema.py).
- Pre-filter ל-existing_facts מעל MEMORY_EXISTING_FACTS_CAP, on-the-fly
  embeddings (text-embedding-3-small) + cosine similarity. שומר תמיד את
  כל open_issues + top-K לפי דמיון לטקסט השיחה.
- Cap על אורך השיחה (MEMORY_CONVERSATION_CAP) — מונע prompt ענק
  בשיחות ארוכות במיוחד (בעיות נפוצות #5 ב-spec).
- Retry יחיד אחרי 5 שניות במקרה של כשל קריאה / JSON לא תקין.
- Short-circuit על שיחה ריקה / < 2 הודעות.

ראה docs/Customer-memory/claude_code_instructions.md (שלב 3).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import numpy as np

from ai_chatbot.config import (
    MEMORY_CONVERSATION_CAP,
    MEMORY_EMBEDDING_MODEL,
    MEMORY_EXISTING_FACTS_CAP,
    MEMORY_EXTRACTION_MODEL,
)
# Client בלעדי לרכיב הזיכרון — נפרד מ-OPENAI_API_KEY/OPENAI_BASE_URL
# הראשיים של הבוט. הסיבה: ה-spec דורש gpt-4.1-mini של OpenAI אמיתי,
# והבוט עלול להיות מכוון ל-Gemini. ראה memory/openai_client.py.
from memory.openai_client import get_memory_openai_client
from memory.schemas.extractor_schema import EXTRACTOR_SCHEMA

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "fact_extractor.txt"
_PROMPT_TEMPLATE: str | None = None  # lazy-loaded singleton

# מעל הסף הזה (לפי spec — 8 facts) מפעילים pre-filter סמנטי.
_PRE_FILTER_THRESHOLD = 8

# השהיה לפני retry — לפי spec ("retry פעם אחת אחרי 5 שניות").
_RETRY_DELAY_SECONDS = 5

# Single-pass placeholder substitution — לא chained .replace().
# טעם: chained replace עלול להפעיל החלפה משנית אם תוכן ה-JSON המוזרק
# (business_name, what_matters_for_extraction, content של facts קיימים)
# מכיל מחרוזת כמו "{{conversation_json}}". regex עם single sub פותר
# את זה — כל placeholder מוחלף בדיוק פעם אחת, ותוצאות ההחלפה לא
# נסרקות שוב.
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _render_prompt(template: str, substitutions: dict[str, str]) -> str:
    """ממיר {{key}} לערך מהמילון, ב-pass יחיד. placeholders לא מוכרים
    נשארים כפי שהם (שיקוף ההתנהגות הקודמת)."""
    def _replace(match: re.Match) -> str:
        return substitutions.get(match.group(1), match.group(0))
    return _PLACEHOLDER_RE.sub(_replace, template)


def _load_prompt_template() -> str:
    """טעינה lazy של הפרומפט; נשמר במטמון פנימי לכל אורך התהליך."""
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _PROMPT_TEMPLATE


def _empty_result(success: bool = True, error: str | None = None,
                  tokens_used: int = 0) -> dict:
    """תוצאה אחידה לכל ה-short-circuits וה-errors."""
    return {
        "extractions": [],
        "skipped": [],
        "tokens_used": tokens_used,
        "success": success,
        "error": error,
    }


def _build_business_context(business_profile: dict) -> dict:
    """ממיר business_profile מה-DB (services_json כ-string) לאובייקט שמוכן
    להזרקה ל-prompt כ-JSON.

    אם services_json לא תקין — מחזירים רשימה ריקה ולוג שגיאה (לא מקריס
    את ה-extraction; ה-LLM יקבל business_context חלקי וכנראה ידלג).
    """
    services_raw = business_profile.get("services_json") or "[]"
    services: list = []
    if isinstance(services_raw, list):
        services = services_raw
    else:
        try:
            parsed = json.loads(services_raw)
            if isinstance(parsed, list):
                services = parsed
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "extractor: כשל בפענוח services_json לעסק '%s'",
                business_profile.get("business_id", "?"),
                exc_info=True,
            )

    # נרמול: ה-extractor מזהה vocabulary לפי name+aliases בלבד (ראה
    # memory/prompts/fact_extractor.txt). שולחים רק אותם — כך שגם פרופילים
    # ישנים ששמרו "category" לא ישלחו שדה לא-בשימוש כרעש ל-LLM.
    normalized_services = [
        {"name": s.get("name", ""), "aliases": s.get("aliases", [])}
        for s in services
        if isinstance(s, dict)
    ]

    return {
        "business_type": business_profile.get("business_type") or "",
        "business_name": business_profile.get("business_name") or "",
        "services": normalized_services,
        "what_matters_for_extraction":
            business_profile.get("what_matters_for_extraction") or "",
    }


def _conversation_text(conversation: list[dict]) -> str:
    """שילוב הודעות לטקסט אחד — משמש רק לחישוב embedding ב-pre-filter."""
    parts: list[str] = []
    for msg in conversation:
        if not isinstance(msg, dict):
            continue
        # תומך גם בפורמט DB (`message`) וגם בפורמט OpenAI (`content`).
        content = msg.get("content") or msg.get("message") or ""
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """חישוב batch embeddings דרך OpenAI. מחזיר None אם הקריאה נכשלת —
    הקורא חוזר ל-fallback לקסיקלי."""
    if not texts:
        return []
    try:
        client = get_memory_openai_client()
        # MEMORY_EMBEDDING_MODEL — קבוע (text-embedding-3-small), לא יורש
        # מ-EMBEDDING_MODEL הראשי שעלול להיות שם של מודל מספק אחר
        # (Gemini למשל). ה-client של memory עובד רק מול OpenAI אמיתי.
        resp = client.embeddings.create(model=MEMORY_EMBEDDING_MODEL, input=texts)
        return [d.embedding for d in resp.data]
    except Exception:
        logger.error(
            "extractor: כשל בחישוב embeddings ל-pre-filter (n=%d)",
            len(texts), exc_info=True,
        )
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """דמיון cosine בין שני וקטורים. 0.0 אם אחד מהם וקטור-אפס."""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _pre_filter_existing_facts(
    existing_facts: list[dict], conversation: list[dict],
) -> list[dict]:
    """כשיש > _PRE_FILTER_THRESHOLD facts — שומר כל open_issue + top-K לפי
    דמיון סמנטי לטקסט השיחה. סך הכל עד MEMORY_EXISTING_FACTS_CAP.

    הגיון:
    - open_issues הם dynamic state שיכול להפוך ל-supersede → תמיד נכללים.
    - שאר ה-types נבחרים לפי רלוונטיות לשיחה הנוכחית.

    אם embeddings נכשל — fallback לפי הסדר המקורי (כבר ממוין ב-CRUD לפי
    confidence DESC, last_confirmed_at DESC).
    """
    if len(existing_facts) <= _PRE_FILTER_THRESHOLD:
        return existing_facts

    open_issues = [f for f in existing_facts if f.get("fact_type") == "open_issue"]
    others = [f for f in existing_facts if f.get("fact_type") != "open_issue"]

    cap = MEMORY_EXISTING_FACTS_CAP
    if len(open_issues) >= cap:
        # קצה נדיר — יותר open_issues מה-cap; חותכים על pi pi פתח.
        return open_issues[:cap]

    slots_left = cap - len(open_issues)
    if len(others) <= slots_left:
        return open_issues + others

    convo_text = _conversation_text(conversation)
    if not convo_text:
        # אין טקסט לחישוב דמיון — חוזרים בסדר המקורי.
        return open_issues + others[:slots_left]

    texts_to_embed = [convo_text] + [str(f.get("content") or "") for f in others]
    embeddings = _embed_texts(texts_to_embed)
    if embeddings is None or len(embeddings) < len(texts_to_embed):
        return open_issues + others[:slots_left]

    convo_emb = embeddings[0]
    scored = [
        (_cosine(convo_emb, embeddings[i + 1]), f)
        for i, f in enumerate(others)
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [f for _, f in scored[:slots_left]]
    return open_issues + selected


def _format_existing_facts(facts: list[dict]) -> list[dict]:
    """מצמצם facts מ-DB-row מלא לתת-קבוצה שה-LLM צריך לראות.

    שדות פנימיים (created_at, access_count, business_id, וכו') לא רלוונטיים
    ל-extractor ויוצרים רעש. ה-LLM צריך: id (לטובת confirms_id/supersedes_id),
    fact_type, content, confidence, requires_consent, status.
    """
    out = []
    for f in facts:
        out.append({
            "id": f.get("id"),
            "fact_type": f.get("fact_type"),
            "content": f.get("content"),
            "confidence": f.get("confidence"),
            "requires_consent": bool(f.get("requires_consent")),
            "status": f.get("status"),
        })
    return out


def _cap_conversation(conversation: list[dict]) -> list[dict]:
    """cap על מספר ההודעות לפני שליחה ל-LLM (בעיות נפוצות #5 ב-spec —
    שיחה של 100 הודעות בלי הפסקה תיקח tokens יקרים בלי תרומה ל-extraction).

    שומרים את ה-N האחרונות — קרובות לסיום השיחה, סביר שהן מכילות את
    הסיכומים והעובדות היציבות יותר.
    """
    cap = MEMORY_CONVERSATION_CAP
    if len(conversation) <= cap:
        return conversation
    return conversation[-cap:]


def _normalize_conversation(conversation: list[dict]) -> list[dict]:
    """ממיר conversation לפורמט אחיד {role, content} לפני הזרקה ל-prompt.

    טבלת `conversations` ב-DB מחזיקה את הטקסט ב-`message`; OpenAI / eval-set
    משתמשים ב-`content`. נאחד ל-`content`.
    """
    normalized = []
    for msg in conversation:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content") or msg.get("message") or ""
        if not content:
            continue
        normalized.append({"role": role, "content": str(content)})
    return normalized


def _call_llm(prompt: str) -> tuple[dict | None, int, str | None]:
    """קריאה ל-LLM עם strict json_schema.

    Returns:
        (parsed_or_None, tokens_used, error_str_or_None)
    """
    try:
        client = get_memory_openai_client()
        resp = client.chat.completions.create(
            model=MEMORY_EXTRACTION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": EXTRACTOR_SCHEMA,
            },
            temperature=0.1,
        )
        content = resp.choices[0].message.content
        usage = getattr(resp, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        try:
            parsed = json.loads(content)
            return parsed, tokens, None
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("extractor: כשל בפענוח JSON מהתשובה", exc_info=True)
            return None, tokens, f"json_decode: {exc}"
    except Exception as exc:
        # תופסים גם openai.APIError, openai.RateLimitError, network errors —
        # CLAUDE.md מאפשר Exception ברמה הגבוהה כל עוד logging מפורש.
        logger.error("extractor: כשל בקריאת LLM", exc_info=True)
        return None, 0, f"{type(exc).__name__}: {exc}"


def extract_facts(
    user_id: str,
    business_id: str,
    conversation: list[dict],
    business_profile: dict,
    existing_facts: list[dict],
) -> dict:
    """מחלץ עובדות יציבות משיחה שהסתיימה.

    Args:
        user_id: מזהה משתמש (לוגיקה בלבד; לא נכנס ל-prompt).
        business_id: מזהה עסק (לוגיקה בלבד; forward-compat).
        conversation: רשימת dicts. תומך גם בפורמט DB ({role, message}) וגם
            בפורמט OpenAI ({role, content}).
        business_profile: dict מ-`db.get_business_profile()` — כולל
            business_type, business_name, services_json (string),
            what_matters_for_extraction.
        existing_facts: רשימת facts קיימים של המשתמש (active+pending).

    Returns:
        {
            "extractions": [...],   # raw מ-LLM; ולידציה ב-validator
            "skipped": [...],
            "tokens_used": int,
            "success": bool,
            "error": str | None,
        }
    """
    # short-circuit על שיחה ריקה/קצרצרה — אין שום דבר לחלץ.
    if not conversation or len(conversation) < 2:
        return _empty_result(success=True)

    normalized = _normalize_conversation(conversation)
    if len(normalized) < 2:
        return _empty_result(success=True)

    capped_conversation = _cap_conversation(normalized)
    filtered_facts = _pre_filter_existing_facts(
        existing_facts or [], capped_conversation,
    )

    # בניית הפרומפט — single-pass רנדור עם regex (לא chained replace).
    template = _load_prompt_template()
    business_context = _build_business_context(business_profile or {})
    prompt = _render_prompt(template, {
        "business_context_json": json.dumps(
            business_context, ensure_ascii=False, indent=2,
        ),
        "existing_facts_json": json.dumps(
            _format_existing_facts(filtered_facts),
            ensure_ascii=False, indent=2,
        ),
        "conversation_json": json.dumps(
            capped_conversation, ensure_ascii=False, indent=2,
        ),
    })

    # קריאה ראשונה
    parsed, tokens, error = _call_llm(prompt)

    # retry יחיד אחרי 5 שניות
    if parsed is None:
        logger.warning(
            "extractor: ניסיון ראשון נכשל (user=%s, business=%s): %s. "
            "retry בעוד %ds",
            user_id, business_id, error, _RETRY_DELAY_SECONDS,
        )
        time.sleep(_RETRY_DELAY_SECONDS)
        parsed, tokens_retry, error = _call_llm(prompt)
        tokens += tokens_retry
        if parsed is None:
            return _empty_result(
                success=False, error=error or "unknown",
                tokens_used=tokens,
            )

    return {
        "extractions": parsed.get("extractions") or [],
        "skipped": parsed.get("skipped") or [],
        "tokens_used": tokens,
        "success": True,
        "error": None,
    }
