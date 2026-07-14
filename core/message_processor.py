"""
Message Processor — לוגיקה עסקית גנרית לעיבוד הודעות נכנסות.

מודול זה מחלץ את הלוגיקה העסקית (intent detection, RAG pipeline, rate limiting)
מקוד ה-Telegram ב-bot/handlers.py, כך שכל ערוץ הודעות (Telegram, WhatsApp) יוכל
לקרוא לו.

שתי נקודות כניסה:
  - process_incoming_message() — עיבוד הודעת טקסט חופשי (intent → routing → response)
  - process_rag_query()        — צינור RAG + LLM (נקודת כניסה אחת לכל שאילתות ה-RAG)
"""

import logging
from dataclasses import dataclass, field

from ai_chatbot import database as db
from ai_chatbot.business_hours import (
    is_currently_open,
    get_weekly_schedule_text,
    get_out_of_office_agent_notice,
)
from ai_chatbot.config import (
    CONTEXT_WINDOW_SIZE,
    FALLBACK_RESPONSE,
    FOLLOW_UP_ENABLED,
    HANDOFF_MARKER,
    LEAD_MARKER,
)
from ai_chatbot.intent import Intent, detect_intent_with_llm, get_direct_response
from ai_chatbot.llm import generate_answer, strip_source_citation
from ai_chatbot.rate_limiter import check_rate_limit, record_message
from ai_chatbot.vacation_service import VacationService

logger = logging.getLogger(__name__)

__all__ = [
    "MessageResult",
    "process_incoming_message",
    "process_rag_query",
    "should_handoff_to_human",
    "strip_handoff_marker",
    "extract_lead_from_response",
    "strip_lead_marker",
]


# ── תוצאת עיבוד ──────────────────────────────────────────────────────────────


@dataclass
class MessageResult:
    """תוצאת עיבוד הודעה — מוחזרת לשכבת הערוץ (Telegram/WhatsApp) לביצוע.

    Attributes:
        text: טקסט התשובה (HTML מהמודל, ללא ציון מקור).
        intent: הכוונה שזוהתה.
        action: פעולה נדרשת מהערוץ — reply / request_agent / start_booking /
                cancel_appointment / handoff_to_human / rate_limited / complaint.
        follow_up_questions: שאלות המשך (אם הפיצ'ר פעיל).
        sources: מקורות מה-RAG.
        consecutive_fallbacks: מונה fallbacks מעודכן — הקורא שומר אותו בין קריאות.
        needs_summarization: האם לתזמן סיכום שיחה ברקע.
        handoff_reason: סיבת העברה לנציג (כשaction=handoff_to_human).
        agent_request_message: הודעה לבעל העסק (כשaction=request_agent).
        is_html: האם הטקסט מכיל HTML שדורש סניטציה לפי הערוץ.
        show_keyboard: האם להציג מקלדת ראשית (False ב-soft fallback ראשון).
    """

    text: str
    intent: Intent
    action: str = "reply"
    follow_up_questions: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    consecutive_fallbacks: int = 0
    needs_summarization: bool = False
    handoff_reason: str = ""
    agent_request_message: str = ""
    is_html: bool = False
    show_keyboard: bool = True
    rag_context: str = ""


# ── פונקציות עזר ──────────────────────────────────────────────────────────────


def should_handoff_to_human(text: str) -> bool:
    """זיהוי תשובת LLM שמבקשת להעביר לבעל העסק.

    מנגנון: ה-system prompt מורה ל-LLM להתחיל את התשובה ב-HANDOFF_MARKER
    כשהוא רוצה להעביר. כאן בודקים את קיום הטוקן בלבד — אין fuzzy matching,
    אין false positives על ניסוחים תמימים.

    fallback: גם התאמה מדויקת ל-FALLBACK_RESPONSE (ללא טוקן) נחשבת —
    זה מטפל ב-edge cases של מקרים שבהם הטוקן הוסר במקום אחר בצינור.
    """
    if not text:
        return False
    t = text.strip()
    if t.startswith(HANDOFF_MARKER):
        return True
    # safety net — אם איכשהו הטוקן הוסר אבל ה-LLM כתב את ה-fallback המדויק
    if t == FALLBACK_RESPONSE.strip():
        return True
    return False


def strip_handoff_marker(text: str) -> str:
    """מסיר את HANDOFF_MARKER מתחילת תשובת LLM, אם קיים.

    הטוקן הוא סיגנל פנימי בין ה-LLM לפרסר; אסור שיגיע ללקוח. הפונקציה
    מסירה גם רווחים/שורות ריקות שעקבו אחרי הטוקן (כפי שהוגדר בפרומפט).
    """
    if not text:
        return text
    stripped = text.lstrip()
    if stripped.startswith(HANDOFF_MARKER):
        return stripped[len(HANDOFF_MARKER):].lstrip()
    return text


# ── ניתוח LEAD_MARKER (ערוץ widget) ──────────────────────────────────────────
# בערוץ widget ה-LLM שם בתחילת התשובה את LEAD_MARKER (אם המבקר מסר שם
# וטלפון), ואחריו שורות במבנה ``name: ...`` ו-``phone: ...``. ראה
# ``_build_channel_rules("widget")`` ב-config.py.

import re as _re  # noqa: E402

# טלפונים ישראליים — נייד או קווי. תומך בפורמטים נפוצים:
#   0501234567 / 050-123-4567 / 050 123 4567 / +972501234567 / +972-50-123-4567
_PHONE_RE = _re.compile(
    r"""
    ^                       # תחילת המחרוזת
    (?:\+972|972|0)         # קידומת מדינה או 0
    [\s\-]*                 # מפרידים אופציונליים
    (\d[\s\-]*){8,9}        # 8-9 ספרות עם מפרידים
    $
    """,
    _re.VERBOSE,
)


def _normalize_phone(raw: str) -> str | None:
    """מנקה ומאמת מספר טלפון. מחזיר None אם לא תקין."""
    if not raw:
        return None
    s = raw.strip()
    # שומרים רק ספרות + ה-+ של קידומת מדינה
    digits_only = _re.sub(r"[^\d+]", "", s)
    if not _PHONE_RE.match(s) and not _PHONE_RE.match(digits_only):
        # ולידציה נכשלה
        return None
    # שמירה בפורמט נייטרלי — בלי מפרידים, עם 0 בתחילה לישראלי מקומי
    digits = _re.sub(r"\D", "", digits_only)
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    if not digits.startswith("0") or len(digits) < 9 or len(digits) > 10:
        return None
    return digits


def extract_lead_from_response(response_text: str) -> dict | None:
    """מחלץ ליד מתשובת LLM שמתחילה ב-LEAD_MARKER.

    מחזיר ``{"name": str, "phone": str}`` אם הטוקן קיים *וגם* שני השדות
    תקינים (שם לא ריק, טלפון שעובר ולידציה). אחרת ``None`` —
    ה-caller צריך ליפול חזרה לתשובה רגילה בלי שמירת ליד.

    הסריקה ממשיכה גם דרך שורות ריקות בין השדות (LLM לפעמים מוסיף
    אותן בטעות) ועוצרת רק כשמגיעה לשורה לא ריקה שאינה שדה — שזו
    הודעת התודה ללקוח.
    """
    if not response_text:
        return None
    stripped = response_text.lstrip()
    if not stripped.startswith(LEAD_MARKER):
        return None
    after_marker = stripped[len(LEAD_MARKER):].lstrip()
    name = ""
    phone = ""
    for line in after_marker.splitlines():
        line = line.strip()
        if not line:
            # שורה ריקה — ממשיכים לסרוק (השדה השני יכול להיות
            # אחריה אם ה-LLM הזיז אותו בטעות)
            continue
        m_name = _re.match(r"^(?:name|שם)\s*[:：]\s*(.+)$", line, _re.IGNORECASE)
        m_phone = _re.match(r"^(?:phone|טלפון)\s*[:：]\s*(.+)$", line, _re.IGNORECASE)
        if m_name and not name:
            name = m_name.group(1).strip()[:100]
            continue
        if m_phone and not phone:
            phone = m_phone.group(1).strip()[:30]
            continue
        # שורה לא ריקה שאינה שדה — מכאן זה גוף התשובה ללקוח, עוצרים.
        break
    if not name or not phone:
        return None
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None
    return {"name": name, "phone": normalized_phone}


def strip_lead_marker(text: str) -> str:
    """מסיר את LEAD_MARKER ואת בלוק השדות שאחריו מתשובת LLM.

    כך הלקוח רואה רק את הטקסט הנקי ('מצוין! פנייתך התקבלה...') בלי
    הטוקן והפרטים שלו. אם הטוקן לא נמצא — מחזירים את הטקסט כמו שהוא.

    האסטרטגיה: סורקים שורה-שורה אחרי הטוקן. צורכים את **המופע הראשון**
    של ``name:`` ושל ``phone:``. שורות ריקות מתעלמים. ברגע שמגיעה
    שורה לא ריקה שאינה המופע הראשון של שדה — היא וכל מה שאחריה הם
    התשובה ללקוח. זה מסונכרן עם ``extract_lead_from_response``: שתי
    הפונקציות צורכות אותו דבר, ולכן אם ה-LLM שם בטעות שורה כמו
    'טלפון: שלך נרשם, תודה' — חילוץ עוצר על המופע הכפול והסטריפ
    משאיר אותה ללקוח (ולא מסיר אותה כשדה).

    הסיבה שלא מסתפקים ב-``split("\\n\\n", 1)`` כקיצור: אם ה-LLM
    שם שורה ריקה *בין* name ל-phone, ה-split עוצר מוקדם ופרטי
    הטלפון דולפים ללקוח. סריקה שורתית עמידה בפני סדרים לא-צפויים.
    """
    if not text:
        return text
    stripped = text.lstrip()
    if not stripped.startswith(LEAD_MARKER):
        return text
    after_marker = stripped[len(LEAD_MARKER):].lstrip()
    name_re = _re.compile(r"^\s*(?:name|שם)\s*[:：]", _re.IGNORECASE)
    phone_re = _re.compile(r"^\s*(?:phone|טלפון)\s*[:：]", _re.IGNORECASE)
    lines = after_marker.splitlines()
    seen_name = False
    seen_phone = False
    body_start = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            # שורה ריקה — ממשיכים לסרוק (יכולה להיות בין שדות
            # או אחריהם, לפני התוכן ללקוח).
            continue
        if name_re.match(line) and not seen_name:
            seen_name = True
            continue
        if phone_re.match(line) and not seen_phone:
            seen_phone = True
            continue
        # שורה שאינה שדה ראשוני — מכאן זה תוכן ללקוח. כולל את
        # המקרה של מופע כפול של שדה ('טלפון: שלך נרשם') שזה
        # לא באמת שדה אלא טקסט תודה שהתחיל באותה מילה.
        body_start = i
        break
    return "\n".join(lines[body_start:]).lstrip()


# ── צינור RAG — נקודת כניסה אחת ──────────────────────────────────────────────


def process_rag_query(
    *,
    user_id: str,
    display_name: str,
    user_message: str,
    query: str,
    handoff_reason: str,
    intent: Intent = Intent.GENERAL,
    consecutive_fallbacks: int = 0,
    channel: str = "telegram",
) -> MessageResult:
    """הרצת צינור RAG + LLM — נקודת כניסה אחת לכל שאילתות ה-RAG.

    שומר הודעות ב-DB ומחזיר תוצאה לשכבת הערוץ.
    הטקסט המוחזר הוא ללא ציון מקור (stripped) אך לא עבר סניטציה ספציפית לערוץ —
    שכבת הערוץ אחראית על סניטציה (למשל sanitize_telegram_html).
    """
    history = db.get_conversation_history(user_id, limit=CONTEXT_WINDOW_SIZE)
    db.save_message(user_id, display_name, "user", user_message, channel=channel)

    result = generate_answer(
        user_query=query,
        conversation_history=history,
        user_id=user_id,
        username=display_name,
        channel=channel,
    )

    # רישום פער ידע — רק כששאילתת RAG אמיתית לא מצאה תוצאות.
    # process_rag_query נקרא רק מ-intents שצריכים RAG (GENERAL, PRICING, LOCATION),
    # לכן אין צורך בסינון נוסף — intents כמו HUMAN_AGENT או GREETING לא מגיעים לכאן.
    if result["chunks_used"] == 0:
        try:
            db.save_unanswered_question(
                user_id, display_name, user_message,
                intent=intent.value, channel=channel,
            )
        except Exception as e:
            logger.error("Failed to log unanswered question: %s", e)

    stripped = strip_source_citation(result["answer"])
    # זיהוי handoff חייב לקרות לפני הסרת הטוקן — should_handoff_to_human
    # מחפש את HANDOFF_MARKER בתחילת הטקסט.
    is_handoff = should_handoff_to_human(stripped)
    # מסירים את הטוקן בכל מקרה, גם אם זה לא handoff (הגנה — אסור שהטוקן
    # יגיע ללקוח אם ה-LLM הוסיף אותו בטעות).
    stripped = strip_handoff_marker(stripped)

    if is_handoff:
        fallback_count = consecutive_fallbacks + 1

        if fallback_count == 1:
            # ניסיון ראשון — הצעה לנסח מחדש, בלי agent request
            soft_msg = "לא הצלחתי למצוא תשובה מדויקת. אפשר לנסח את השאלה אחרת?"
            db.save_message(user_id, display_name, "assistant", soft_msg, channel=channel)
            return MessageResult(
                text=soft_msg,
                intent=intent,
                consecutive_fallbacks=fallback_count,
                needs_summarization=True,
                show_keyboard=False,
            )

        if fallback_count == 2:
            # ניסיון שני — תפריט ראשי + הצעת נציג
            menu_msg = (
                "עדיין לא מצאתי תשובה מתאימה.\n"
                "הנה כמה אפשרויות שאולי יעזרו, "
                "או לחצו על <b>👤 דברו עם נציג</b>:"
            )
            db.save_message(user_id, display_name, "assistant", menu_msg, channel=channel)
            return MessageResult(
                text=menu_msg,
                intent=intent,
                is_html=True,
                consecutive_fallbacks=fallback_count,
                needs_summarization=True,
            )

        # ניסיון שלישי+ — העברה לנציג
        db.save_message(user_id, display_name, "assistant", FALLBACK_RESPONSE, channel=channel)
        return MessageResult(
            text=FALLBACK_RESPONSE,
            intent=intent,
            action="handoff_to_human",
            consecutive_fallbacks=0,
            handoff_reason=handoff_reason,
            needs_summarization=True,
        )

    # תשובה מוצלחת
    db.save_message(
        user_id, display_name, "assistant",
        result["answer"], ", ".join(result["sources"]),
        channel=channel,
    )
    follow_up_qs = result.get("follow_up_questions", [])

    return MessageResult(
        text=stripped,
        intent=intent,
        is_html=True,
        follow_up_questions=follow_up_qs if FOLLOW_UP_ENABLED else [],
        sources=result["sources"],
        consecutive_fallbacks=0,
        needs_summarization=True,
        rag_context=result.get("rag_context", ""),
    )


# ── עיבוד הודעה ראשי ─────────────────────────────────────────────────────────


def process_incoming_message(
    user_id: str,
    text: str,
    user_info: dict,
    consecutive_fallbacks: int = 0,
    rate_limit_already_checked: bool = False,
    channel: str = "telegram",
) -> MessageResult:
    """עיבוד הודעת טקסט חופשי — זיהוי כוונה וניתוב.

    Args:
        user_id: מזהה המשתמש (string בכל ערוץ).
        text: טקסט ההודעה.
        user_info: מידע על המשתמש — display_name (חובה), telegram_username (אופציונלי).
        consecutive_fallbacks: מונה fallbacks רצופים (מהקורא).
        rate_limit_already_checked: True אם הקורא כבר בדק rate limit (למשל דקורטור).
        channel: ערוץ ההודעה — "telegram" או "whatsapp". נשמר ב-DB עם כל הודעה.

    Returns:
        MessageResult עם התשובה, הכוונה, והפעולה הנדרשת מהערוץ.
    """
    display_name = user_info.get("display_name", "")
    user_id = str(user_id)  # תמיד string — תואם Telegram (מספרי) ו-WhatsApp (טלפון)

    # ── Rate limiting ────────────────────────────────────────────────────
    if not rate_limit_already_checked:
        limit_msg = check_rate_limit(user_id)
        if limit_msg is not None:
            return MessageResult(
                text=limit_msg,
                intent=Intent.GENERAL,
                action="rate_limited",
            )
        record_message(user_id)

    # ── Intent detection ─────────────────────────────────────────────────
    intent = detect_intent_with_llm(text)

    # כשקביעת תורים כבויה לעסק — בקשת תור/פגישה מטופלת כבקשת נציג:
    # העסק אינו מתאם אונליין, רק מעביר לבעל העסק. מנרמלים את הכוונה כאן,
    # לפני בלוקי הדיספאטץ', כדי לעבור דרך צינור ה-HUMAN_AGENT הקיים
    # (יצירת בקשת נציג + התראה לבעלים) במקום לפתוח flow של קביעת תור.
    if intent == Intent.APPOINTMENT_BOOKING and not db.is_booking_enabled():
        intent = Intent.HUMAN_AGENT

    # איפוס fallbacks לכוונות שלא עוברות RAG
    if intent not in (Intent.GENERAL, Intent.PRICING, Intent.LOCATION):
        consecutive_fallbacks = 0

    # ── GREETING / FAREWELL — תשובה ישירה, ללא RAG ───────────────────
    if intent in (Intent.GREETING, Intent.FAREWELL):
        db.save_message(user_id, display_name, "user", text, channel=channel)
        response = get_direct_response(intent)
        db.save_message(user_id, display_name, "assistant", response, channel=channel)
        return MessageResult(text=response, intent=intent)

    # ── BUSINESS_HOURS — סטטוס חי, ללא RAG ───────────────────────────
    if intent == Intent.BUSINESS_HOURS:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        schedule = get_weekly_schedule_text()
        # מצב חופשה: מחליפים את "פתוח/סגור עכשיו" בהודעת חופשה,
        # כי is_currently_open מתבסס על שעות שבועיות בלבד ויטעה לקוח
        # ששואל "באיזה שעות אתם פתוחים?" כשהעסק בחופשה.
        if VacationService.is_active():
            notice = VacationService.get_hours_message()
            response = f"{notice}\n\nשעות הפעילות הרגילות:\n{schedule}"
        else:
            status = is_currently_open()
            response = f"{status['message']}\n\n{schedule}"
        db.save_message(user_id, display_name, "assistant", response, channel=channel)
        return MessageResult(text=response, intent=intent)

    # ── APPOINTMENT_BOOKING — הפניה לכפתור תורים ─────────────────────
    if intent == Intent.APPOINTMENT_BOOKING:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        if VacationService.is_active():
            response = VacationService.get_booking_message()
            db.save_message(user_id, display_name, "assistant", response, channel=channel)
            return MessageResult(text=response, intent=intent)
        response = (
            "אשמח לעזור לכם לבקש תור! 📅\n\n"
            "לחצו על הכפתור <b>📅 בקשת תור</b> למטה כדי להתחיל."
        )
        db.save_message(user_id, display_name, "assistant", response, channel=channel)
        return MessageResult(
            text=response, intent=intent,
            action="start_booking", is_html=True,
        )

    # ── APPOINTMENT_CANCEL — בקשת אישור ביטול ─────────────────────────
    if intent == Intent.APPOINTMENT_CANCEL:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        confirm_text = "האם אתם בטוחים שתרצו לבטל את התור?"
        db.save_message(user_id, display_name, "assistant", confirm_text, channel=channel)
        return MessageResult(
            text=confirm_text, intent=intent,
            action="cancel_appointment",
        )

    # ── APPOINTMENT_RESCHEDULE — בקשת שינוי תאריך/שעה ─────────────
    if intent == Intent.APPOINTMENT_RESCHEDULE:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        confirm_text = "בטח! אעזור לכם לשנות את התור. 🔄"
        db.save_message(user_id, display_name, "assistant", confirm_text, channel=channel)
        return MessageResult(
            text=confirm_text, intent=intent,
            action="reschedule_appointment",
        )

    # ── HUMAN_AGENT — בקשת נציג אנושי ────────────────────────────────
    if intent == Intent.HUMAN_AGENT:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        if VacationService.is_active():
            response = VacationService.get_agent_message()
            db.save_message(user_id, display_name, "assistant", response, channel=channel)
            return MessageResult(text=response, intent=intent)

        # הודעת "חוץ מהמשרד" — ציפייה נכונה לזמן חזרה של הנציג
        ooo_notice = get_out_of_office_agent_notice()
        if ooo_notice:
            response = (
                "👤 הפנייה שלך הועברה בהצלחה!\n\n"
                f"{ooo_notice}\n"
                "בינתיים, אתם מוזמנים לשאול אותי כל שאלה נוספת!"
            )
        else:
            response = (
                "👤 הפנייה שלך הועברה בהצלחה! "
                "בעל העסק יראה את ההודעה ויחזור אליך ברגע שיתפנה.\n\n"
                "בינתיים, אתם מוזמנים לשאול אותי כל שאלה נוספת!"
            )
        db.save_message(user_id, display_name, "assistant", response, channel=channel)
        return MessageResult(
            text=response, intent=intent,
            action="request_agent",
            agent_request_message=f"הלקוח ביקש נציג: {text}",
        )

    # ── COMPLAINT — הצעת נציג אנושי ──────────────────────────────────
    if intent == Intent.COMPLAINT:
        db.save_message(user_id, display_name, "user", text, channel=channel)
        response = (
            "אנחנו מצטערים לשמוע שהחוויה לא הייתה טובה. 😔\n"
            "נשמח לטפל בפנייתכם באופן אישי.\n\n"
            'לחצו על <b>👤 דברו עם נציג</b> למטה כדי שבעל העסק יחזור אליכם בהקדם.'
        )
        db.save_message(user_id, display_name, "assistant", response, channel=channel)
        return MessageResult(
            text=response, intent=intent,
            action="complaint", is_html=True,
        )

    # ── LOCATION — שאילתת מיקום דרך RAG ──────────────────────────────
    if intent == Intent.LOCATION:
        return process_rag_query(
            user_id=user_id,
            display_name=display_name,
            user_message=text,
            query="מיקום כתובת הגעה: " + text,
            handoff_reason=f"הלקוח שאל על מיקום: {text}",
            intent=intent,
            consecutive_fallbacks=consecutive_fallbacks,
            channel=channel,
        )

    # ── PRICING / GENERAL — צינור RAG ────────────────────────────────
    query = ("מחירון: " + text) if intent == Intent.PRICING else text
    handoff_reason = (
        f"הלקוח שאל על מחירים: {text}" if intent == Intent.PRICING
        else f"הלקוח ביקש עזרה בנושא: {text}"
    )
    return process_rag_query(
        user_id=user_id,
        display_name=display_name,
        user_message=text,
        query=query,
        handoff_reason=handoff_reason,
        intent=intent,
        consecutive_fallbacks=consecutive_fallbacks,
        channel=channel,
    )
