"""
Follow-up Service — מערכת מעקב אוטומטית אחרי לידים שלא השלימו הזמנה.

שלושה שלבים עיקריים:
1. **ניתוח שיחה (analyze_lead)** — בסוף שיחה משמעותית, LLM מסכם intent + lead score.
2. **עיבוד תקופתי (process_pending_followups)** — כל 15 דקות, בודק מי זכאי ושולח.
3. **סימון תגובה (mark_replied / mark_converted)** — כשמשתמש חוזר אחרי follow-up.

מנוע ההחלטה משתמש ב-Gemini Flash דרך OpenAI-compatible API.
חוקי בטיחות קשיחים בקוד מונעים ספאם — בנוסף לשיקול של ה-LLM.
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from ai_chatbot import database as db
from ai_chatbot.config import (
    FOLLOWUP_ENABLED,
    FOLLOWUP_MODEL,
    FOLLOWUP_DELAY_HOURS,
    FOLLOWUP_MIN_CONFIDENCE,
    FOLLOWUP_WHATSAPP_BUFFER_MINUTES,
)
from ai_chatbot.followup_config import (
    LEAD_ANALYSIS_PROMPT,
    LEAD_ANALYSIS_SCHEMA,
    FOLLOWUP_DECISION_PROMPT,
    FOLLOWUP_DECISION_SCHEMA,
    TEMPLATE_KEYS,
    render_template,
)

logger = logging.getLogger(__name__)

# ── Gemini / LLM client נפרד למנוע ה-follow-up ─────────────────────────────
# משתמש ב-FOLLOWUP_MODEL (ברירת מחדל: gemini-3.0-flash).
# אם לא הוגדר FOLLOWUP_BASE_URL — משתמש ב-OPENAI_BASE_URL הרגיל.

_followup_client = None
_followup_client_lock = threading.Lock()


def _get_followup_client():
    """קבלת OpenAI client מוגדר למודל follow-up (Gemini Flash)."""
    global _followup_client
    if _followup_client is None:
        with _followup_client_lock:
            if _followup_client is None:
                try:
                    from openai import OpenAI
                except ImportError:
                    raise RuntimeError("openai package not installed")
                base_url = os.getenv("FOLLOWUP_BASE_URL") or os.getenv("OPENAI_BASE_URL")
                if base_url:
                    _followup_client = OpenAI(base_url=base_url)
                else:
                    _followup_client = OpenAI()
    return _followup_client


def _call_llm(prompt: str, user_content: str, schema: dict | None = None) -> dict:
    """קריאה ל-LLM ופרסור JSON מהתשובה.

    Args:
        prompt: system prompt.
        user_content: תוכן הודעת המשתמש.
        schema: JSON schema לוולידציה (אופציונלי — לוג בלבד, לא חוסם).

    Returns:
        dict עם התשובה המפורסרת.

    Raises:
        ValueError: אם התשובה לא JSON תקין.
    """
    client = _get_followup_client()
    response = client.chat.completions.create(
        model=FOLLOWUP_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=1024,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    if not raw:
        raise ValueError("LLM returned empty/null content")
    raw = raw.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("תשובת LLM לא JSON תקין: %s — raw: %.300s", e, raw)
        raise ValueError(f"Invalid JSON from LLM: {e}") from e

    # וולידציה בסיסית — בדיקת שדות חובה לפי ה-schema
    if schema and "required" in schema:
        missing = [k for k in schema["required"] if k not in result]
        if missing:
            logger.warning("שדות חסרים בתשובת LLM: %s — raw: %.300s", missing, raw)

    return result


# ── 1. ניתוח שיחה (Lead Analysis) ───────────────────────────────────────────


def analyze_lead(user_id: str, *, username: str = "", channel: str = "telegram") -> None:
    """מנתח שיחה אחרונה ויוצר רשומת follow-up אם הליד חם/חמים.

    נקרא ברקע אחרי שיחה משמעותית. לא זורק exceptions — רק מלוג.

    ⚠ feature gate (Phase 3): מתבצע רק אם החבילה כוללת followup_24h.
    שתי שכבות שליטה: FOLLOWUP_ENABLED (env-level kill switch) +
    feature_flags (per-plan/customer override). שתיהן חייבות להיות פעילות.
    """
    if not FOLLOWUP_ENABLED:
        return

    try:
        from ai_chatbot import feature_flags
        if not feature_flags.has_feature("followup_24h"):
            logger.debug(
                "analyze_lead: skipped user_id=%s — feature 'followup_24h' "
                "not active (plan=%s)",
                user_id, feature_flags.get_current_plan(),
            )
            return
    except Exception:
        # feature_flags bullet-proof — אבל אם בכל זאת יזרוק, להמשיך כדי
        # לא לאבד פעולת analyze_lead לגיטימית. רק לוג.
        logger.error(
            "analyze_lead: feature_flags check raised — proceeding",
            exc_info=True,
        )

    try:
        _analyze_lead_inner(user_id, username=username, channel=channel)
    except Exception:
        logger.exception("שגיאה בניתוח ליד עבור user_id=%s", user_id)


def _analyze_lead_inner(user_id: str, *, username: str = "", channel: str = "telegram") -> None:
    """לוגיקה פנימית של ניתוח ליד — נפרדת לבדיקות."""

    # בדיקה מקדימה: אם כבר יש follow-up פעיל — לא יוצרים חדש
    if db.has_pending_or_sent_followup(user_id):
        logger.debug("ליד %s — כבר יש follow-up פעיל, דילוג", user_id)
        return

    # בדיקה: אם יש הזמנה אחרונה — לא צריך follow-up
    if db.has_recent_booking(user_id, hours=48):
        logger.debug("ליד %s — יש הזמנה אחרונה, דילוג", user_id)
        return

    # שליפת היסטוריית שיחה אחרונה
    history = db.get_conversation_history(user_id, limit=10)
    if len(history) < 4:
        return  # מינימום 2 חילופי הודעות (user+assistant×2) כדי לא לדרג "שלום" כ-cold

    # בניית טקסט השיחה לניתוח
    conversation_text = _format_conversation_for_llm(history)

    # קריאה ל-LLM לניתוח
    analysis = _call_llm(
        LEAD_ANALYSIS_PROMPT,
        conversation_text,
        schema=LEAD_ANALYSIS_SCHEMA,
    )

    lead_temp = analysis.get("lead_temperature", "cold")
    if lead_temp == "cold":
        logger.debug("ליד %s — טמפרטורה cold, לא יוצרים follow-up", user_id)
        # שומרים רשומה cancelled כדי למנוע קריאות LLM חוזרות על כל הודעה
        db.create_lead_followup(
            user_id=user_id,
            followup_due_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            username=username,
            channel=channel,
            service_of_interest=analysis.get("service_of_interest", ""),
            intent_type=analysis.get("intent_type", "unknown"),
            lead_temperature="cold",
            conversation_summary=analysis.get("summary", ""),
            analysis_json=json.dumps(analysis, ensure_ascii=False),
            status="cancelled",
            stop_reason="cold_lead",
        )
        return

    # חישוב due_at — לוואטסאפ מקזזים מרווח בטיחות כדי לשלוח לפני שחלון
    # השיחה של Twilio נסגר (24h מאז ההודעה האחרונה). אחרי החלון, ההודעה
    # הופכת ל-template עם תמחור גבוה. ראה FOLLOWUP_WHATSAPP_BUFFER_MINUTES.
    due_at = datetime.now(timezone.utc) + timedelta(hours=FOLLOWUP_DELAY_HOURS)
    if channel == "whatsapp" and FOLLOWUP_WHATSAPP_BUFFER_MINUTES > 0:
        due_at -= timedelta(minutes=FOLLOWUP_WHATSAPP_BUFFER_MINUTES)
    due_at_str = due_at.strftime("%Y-%m-%d %H:%M:%S")

    # יצירת רשומת follow-up
    followup_id = db.create_lead_followup(
        user_id=user_id,
        followup_due_at=due_at_str,
        username=username,
        channel=channel,
        service_of_interest=analysis.get("service_of_interest", ""),
        intent_type=analysis.get("intent_type", "unknown"),
        lead_temperature=lead_temp,
        conversation_summary=analysis.get("summary", ""),
        analysis_json=json.dumps(analysis, ensure_ascii=False),
    )
    logger.info(
        "נוצר follow-up #%d עבור user_id=%s — temp=%s, service=%s, due=%s",
        followup_id, user_id, lead_temp,
        analysis.get("service_of_interest", "?"), due_at_str,
    )


def _format_conversation_for_llm(history: list[dict]) -> str:
    """פורמט היסטוריית שיחה לטקסט שה-LLM יכול לנתח."""
    lines = []
    for msg in history:
        role = "לקוח" if msg.get("role") == "user" else "בוט"
        lines.append(f"{role}: {msg.get('message', '')}")
    return "\n".join(lines)


# ── 2. בדיקות זכאות (Hard-coded Safety Checks) ──────────────────────────────


def check_eligibility(user_id: str) -> tuple[bool, str]:
    """בדיקות בטיחות קשיחות — לא תלויות ב-LLM.

    מחזיר (eligible, stop_reason).
    """
    # חסום?
    if db.is_user_blocked(user_id):
        return False, "user_blocked"

    # כבר יש הזמנה אחרונה?
    if db.has_recent_booking(user_id, hours=48):
        return False, "has_recent_booking"

    # שיחה פתוחה עם נציג?
    from ai_chatbot.live_chat_service import LiveChatService
    if LiveChatService.is_active(user_id):
        return False, "live_chat_active"

    # ביטל הרשמה?
    if not db.is_user_subscribed(user_id):
        return False, "unsubscribed"

    return True, ""


# ── 3. מנוע החלטה (Decision Engine) ─────────────────────────────────────────


def get_followup_decision(lead: dict) -> dict:
    """שולח את הליד ל-Gemini Flash ומקבל החלטה מובנית.

    Args:
        lead: dict מטבלת lead_followups.

    Returns:
        dict עם שדות ההחלטה (should_send_followup, confidence, recommended_template_key, ...).
    """
    # בניית input ל-LLM
    input_data = {
        "channel": lead.get("channel", "telegram"),
        "user_name": lead.get("username", ""),
        "service_of_interest": lead.get("service_of_interest", ""),
        "intent_type": lead.get("intent_type", "unknown"),
        "lead_temperature": lead.get("lead_temperature", "cold"),
        "conversation_summary": lead.get("conversation_summary", ""),
        "hours_since_creation": _hours_since(lead.get("created_at", "")),
    }

    input_json = json.dumps(input_data, ensure_ascii=False, indent=2)

    decision = _call_llm(
        FOLLOWUP_DECISION_PROMPT,
        input_json,
        schema=FOLLOWUP_DECISION_SCHEMA,
    )
    return decision


def _hours_since(iso_timestamp: str) -> float:
    """חישוב שעות שעברו מאז timestamp ISO."""
    if not iso_timestamp:
        return 0
    try:
        # DB שומר ללא timezone — מניחים UTC
        dt = datetime.fromisoformat(iso_timestamp).replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return round(delta.total_seconds() / 3600, 1)
    except (ValueError, TypeError):
        return 0


# ── 4. שליחת follow-up ──────────────────────────────────────────────────────


def send_followup_message(lead: dict, template_key: str, variables: dict) -> bool:
    """שולח הודעת follow-up ללקוח.

    Args:
        lead: רשומת lead_followups.
        template_key: מפתח התבנית.
        variables: משתני התבנית (service_name, ...).

    Returns:
        True אם נשלח בהצלחה.
    """
    from ai_chatbot.live_chat_service import send_message_by_channel

    user_id = lead["user_id"]
    channel = lead.get("channel", "telegram")
    name = lead.get("username", "")
    service_name = variables.get("service_name", lead.get("service_of_interest", ""))

    text = render_template(template_key, name=name, service_name=service_name)

    success = send_message_by_channel(user_id, text, channel)
    if success:
        logger.info("follow-up נשלח ל-%s (channel=%s, template=%s)", user_id, channel, template_key)
    else:
        logger.error("שליחת follow-up נכשלה ל-%s (channel=%s)", user_id, channel)
    return success


# ── 5. עיבוד תקופתי (Scheduler Job) ─────────────────────────────────────────


async def process_pending_followups(_context=None) -> dict:
    """ה-job התקופתי: שולף לידים pending שהגיע זמנם, מריץ eligibility + decision, שולח.

    מחזיר סטטיסטיקות: {"processed": N, "sent": N, "skipped": N, "errors": N}.

    ⚠ feature gate (Phase 3): מתבצע רק אם החבילה כוללת followup_24h.
    """
    if not FOLLOWUP_ENABLED:
        return {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}

    try:
        from ai_chatbot import feature_flags
        if not feature_flags.has_feature("followup_24h"):
            logger.info(
                "process_pending_followups: skipped — feature 'followup_24h' "
                "not active (plan=%s)",
                feature_flags.get_current_plan(),
            )
            return {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}
    except Exception:
        logger.error(
            "process_pending_followups: feature_flags check raised — proceeding",
            exc_info=True,
        )

    stats = {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}

    # מנקים follow-ups ישנים מדי שלא טופלו
    try:
        expired = db.expire_old_followups(max_age_hours=72)
        if expired:
            logger.info("סומנו %d follow-ups כ-expired", expired)
    except Exception:
        logger.exception("שגיאה בסימון follow-ups ישנים")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    leads = db.get_pending_followups(due_before=now_str)

    for lead in leads:
        stats["processed"] += 1
        try:
            # עטיפה ב-to_thread כדי לא לחסום את event loop הבוט
            # (_process_single_lead מבצע קריאות HTTP סינכרוניות ל-LLM ולשליחת הודעות)
            await asyncio.to_thread(_process_single_lead, lead, stats)
        except Exception:
            logger.exception("שגיאה בעיבוד follow-up #%s", lead.get("id"))
            stats["errors"] += 1
            # סימון ככישלון — לא חוסמים את שאר הלידים
            try:
                db.update_followup_status(
                    lead["id"], "cancelled", stop_reason="processing_error",
                )
            except Exception:
                logger.exception("שגיאה בסימון follow-up #%s ככישלון", lead.get("id"))

    if stats["processed"]:
        logger.info(
            "עיבוד follow-ups: processed=%d, sent=%d, skipped=%d, errors=%d",
            stats["processed"], stats["sent"], stats["skipped"], stats["errors"],
        )
    return stats


def _process_single_lead(lead: dict, stats: dict) -> None:
    """עיבוד ליד בודד — eligibility → decision → send."""
    user_id = lead["user_id"]
    followup_id = lead["id"]

    # הגנה מפני כפילויות: אם כבר נשלח follow-up למשתמש הזה
    # (למשל מרשומת pending כפולה שנוצרה ע"י race condition) — מבטלים
    with db.get_connection() as conn:
        already_sent = conn.execute(
            "SELECT 1 FROM lead_followups WHERE user_id = ? "
            "AND status IN ('sent', 'replied', 'converted') LIMIT 1",
            (user_id,),
        ).fetchone()
    if already_sent:
        db.update_followup_status(followup_id, "cancelled", stop_reason="duplicate_user")
        logger.info("follow-up #%d בוטל: כבר נשלח follow-up למשתמש %s", followup_id, user_id)
        stats["skipped"] += 1
        return

    # שלב 1: בדיקות בטיחות קשיחות
    eligible, stop_reason = check_eligibility(user_id)
    if not eligible:
        db.update_followup_status(followup_id, "cancelled", stop_reason=stop_reason)
        logger.info("follow-up #%d בוטל: %s", followup_id, stop_reason)
        stats["skipped"] += 1
        return

    # שלב 2: מנוע החלטה (LLM)
    decision = get_followup_decision(lead)

    should_send = decision.get("should_send_followup", False)
    confidence = decision.get("confidence", 0)
    template_key = decision.get("recommended_template_key")

    # בדיקת סף ביטחון
    if not should_send or confidence < FOLLOWUP_MIN_CONFIDENCE:
        reason = f"llm_declined (confidence={confidence})" if not should_send else f"low_confidence ({confidence})"
        db.update_followup_status(followup_id, "cancelled", stop_reason=reason)
        logger.info("follow-up #%d בוטל ע\"י מנוע החלטה: %s", followup_id, reason)
        stats["skipped"] += 1
        return

    # וולידציית template key
    if template_key not in TEMPLATE_KEYS:
        template_key = "followup_interest_check"  # fallback בטוח

    variables = decision.get("template_variables", {})
    variables_json = json.dumps(variables, ensure_ascii=False)

    # שלב 3: שליחה
    success = send_followup_message(lead, template_key, variables)

    if success:
        db.update_followup_status(
            followup_id, "sent",
            template_key=template_key,
            template_variables=variables_json,
        )
        stats["sent"] += 1
    else:
        db.update_followup_status(
            followup_id, "cancelled", stop_reason="send_failed",
        )
        stats["errors"] += 1


# ── 6. סימון תגובה / המרה ───────────────────────────────────────────────────


def handle_user_returned(user_id: str) -> None:
    """נקרא כשמשתמש שולח הודעה — בודק אם חזר אחרי follow-up ומסמן."""
    if not FOLLOWUP_ENABLED:
        return
    try:
        db.mark_followup_replied(user_id)
    except Exception:
        logger.exception("שגיאה בסימון replied עבור user_id=%s", user_id)


def handle_booking_created(user_id: str) -> None:
    """נקרא כשנוצרת הזמנה — בודק אם זה המרה אחרי follow-up."""
    if not FOLLOWUP_ENABLED:
        return
    try:
        db.mark_followup_converted(user_id)
    except Exception:
        logger.exception("שגיאה בסימון converted עבור user_id=%s", user_id)
