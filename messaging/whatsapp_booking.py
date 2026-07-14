"""
WhatsApp Booking Flow — טיפול בזרימת קביעת תור דרך WhatsApp.

ב-Telegram יש ConversationHandler עם InlineKeyboard.
ב-WhatsApp — שימוש ב-Twilio Content Templates (List Picker / Quick Reply) כשאפשר,
עם fallback לטקסט מספרי כשה-Content API לא זמין.

Flow:
1. message_processor מחזיר action="start_booking"
2. webhook קורא ל-start_booking → שולח List Picker או רשימה ממוספרת
3. הודעה הבאה (ButtonPayload / טקסט) → handle_booking_step → מנתב לפי state
"""

import logging
import re
from datetime import date as _date_type, timedelta as _timedelta
from html import escape as _esc

from messaging.conversation_state import (
    get_state,
    set_state,
    clear_state,
    get_session_data,
    STATE_BOOKING_SERVICE,
    STATE_BOOKING_DATE,
    STATE_BOOKING_TIME,
    STATE_BOOKING_CONFIRM,
)
from messaging.whatsapp_sender import send_whatsapp
import database as db
from utils.phone import format_phone as _format_phone
from entity_extraction import normalize_date
from config import ADMIN_URL, TELEGRAM_OWNER_CHAT_ID

logger = logging.getLogger(__name__)

# תבנית לזיהוי שעה מספרית
_TIME_NUMERIC_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")

# ─── דחיית auto-booking: לאיזה שלב לחזור כדי שהבוט ימשיך להאזין ──────────────
# הבאג: הודעת הדחייה מזמינה "בחרו שעה/תאריך אחר/ת", אבל clear_state הרג את
# ה-flow — ולכן ההודעה הבאה של הלקוח (השעה החדשה) נפלה ל-RAG ("לא הבנתי").
# הפתרון: נשארים ב-flow בשלב המתאים לפי סוג הדחייה. set_state ממזג את הנתונים
# הקיימים (service/date), כך שהם נשמרים.
# דחיות שהלקוח מתקן ע"י בחירת *שעה* אחרת (אותו תאריך תקין):
_REJECT_RETRY_TIME = frozenset({
    "calendar_busy", "slot_already_taken", "before_business_hours",
    "exceeds_closing_time", "slot_in_past", "slot_crosses_midnight",
})
# דחיות שהלקוח מתקן ע"י בחירת *תאריך* אחר (היום עצמו סגור/רחוק):
_REJECT_RETRY_DATE = frozenset({
    "closed_regular", "closed_holiday", "closed_special_day", "slot_too_far_ahead",
})
# כל השאר (vacation_active, invalid_duration, סיבה לא ידועה) — terminal:
# אין מה לתקן בתוך אותו flow, מנקים ומציעים להתחיל מחדש.


def _format_date_display(iso_date: str) -> str:
    """המרת YYYY-MM-DD → DD/MM/YYYY."""
    try:
        parts = iso_date.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (IndexError, AttributeError):
        return iso_date


def _normalize_time_for_gcal(raw_time: str) -> str | None:
    """נרמול שעה ל-HH:MM. מחזיר None לטקסט חופשי."""
    m = _TIME_NUMERIC_RE.match(raw_time.strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _send_service_list_picker(user_id: str, services: list[dict]) -> None:
    """שליחת List Picker עם רשימת שירותים דרך Twilio Content API.

    Raises:
        Exception: אם יצירת/שליחת ה-template נכשלה.
    """
    from messaging.whatsapp_templates import ensure_list_picker, send_with_template

    # שם בסיס קבוע — ensure_list_picker מוסיף hash תוכן לשם ומוחק גרסאות ישנות
    template_name = "booking_services"

    # אין יותר משך פר-שירות — מציגים רק את שם השירות. המשך נקבע על ידי
    # ברירת המחדל הגלובלית (default_appointment_duration_minutes) ובחירת
    # בעל העסק ברגע אישור התור.
    items = [
        {
            "title": svc["name"],
            "id": f"svc_{svc['id']}",
        }
        for svc in services
    ]

    content_sid = ensure_list_picker(
        friendly_name=template_name,
        body="📅 *בקשת תור*\n\nבחרו שירות מהרשימה:",
        button_text="בחרו שירות",
        items=items,
    )

    send_with_template(user_id, content_sid)


def _send_confirm_buttons(user_id: str, service: str, date_display: str, time: str) -> bool:
    """שליחת כפתורי אישור/ביטול דרך Quick Reply עם content_variables.

    ה-template קבוע (עם placeholders) — הנתונים הדינמיים מועברים כמשתנים.
    כך אפשר לשתף template אחד בין כל ההזמנות.

    Returns:
        True אם הצליח, False אם נכשל (ה-caller ישלח טקסט רגיל).
    """
    try:
        from messaging.whatsapp_templates import ensure_quick_reply, send_with_template

        content_sid = ensure_quick_reply(
            friendly_name="booking_confirm",
            body=(
                "📋 *סיכום בקשת התור:*\n\n"
                "• שירות: {{1}}\n"
                "• תאריך: {{2}}\n"
                "• שעה: {{3}}"
            ),
            buttons=[
                ("✅ כן, אשרו", "confirm_yes"),
                ("❌ לא, בטלו", "confirm_no"),
            ],
        )
        send_with_template(user_id, content_sid, {
            "1": service,
            "2": date_display,
            "3": time,
        })
        return True
    except Exception:
        logger.warning("Quick Reply confirm נכשל, חוזרים לטקסט", exc_info=True)
        return False


# שמות ימים בעברית — אינדקס לפי weekday() של Python (0=Monday)
_HEBREW_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def _get_available_dates(service_duration: int, page: int = 0) -> tuple[list[_date_type], bool]:
    """החזרת (רשימת תאריכים פנויים, has_more).

    משתמש ב-get_month_availability מ-calendar_keyboard אם זמין,
    אחרת סורק 60 ימים קדימה לפי business_hours.
    """
    today = _date_type.today()
    available = []
    # סורקים עד 60 ימים קדימה כדי למצוא מספיק ימים פנויים
    try:
        from bot.calendar_keyboard import get_month_availability

        buf_min = db.get_auto_booking_buffer_minutes()
        # סריקת עד 60 ימים — קאש per-month
        checked_months = {}
        for offset in range(60):
            d = today + _timedelta(days=offset)
            month_key = (d.year, d.month)
            if month_key not in checked_months:
                checked_months[month_key] = get_month_availability(
                    d.year, d.month, service_duration,
                    buffer_after_event_minutes=buf_min,
                )
            avail = checked_months[month_key]
            if avail.get(d.day, {}).get("available"):
                available.append(d)
    except ImportError:
        # fallback — בדיקה בסיסית לפי business_hours בלבד
        try:
            from business_hours import get_status_for_date
            for offset in range(60):
                d = today + _timedelta(days=offset)
                status = get_status_for_date(d)
                if status.get("is_open"):
                    available.append(d)
        except ImportError:
            # אין business_hours — מחזירים 14 ימים קדימה (ללא סינון)
            available = [today + _timedelta(days=i) for i in range(14)]

    # pagination — דף של 10 תוצאות (מגבלת List Picker)
    start = page * 10
    return available[start:start + 10], len(available) > start + 10


def _send_date_list_picker(user_id: str, service_name: str, service_duration: int, page: int = 0) -> bool:
    """ניסיון לשלוח List Picker עם תאריכים פנויים. מחזיר True אם הצליח."""
    dates, has_more = _get_available_dates(service_duration, page)
    if not dates:
        return False

    try:
        from messaging.whatsapp_templates import ensure_list_picker, send_with_template

        items = []
        for d in dates:
            day_name = _HEBREW_DAYS[d.weekday()]
            label = f"יום {day_name} {d.strftime('%d/%m')}"
            items.append({
                "title": label,
                "id": f"date_{d.isoformat()}",
            })

        # כפתור "עוד ימים" אם יש דף הבא (משתמש ב-slot מהרשימה)
        if has_more:
            # מוסיפים רק אם לא חורגים מ-10 פריטים
            if len(items) >= 10:
                items = items[:9]  # פינוי מקום לכפתור "עוד"
            items.append({
                "title": "▶ עוד ימים...",
                "id": f"date_more_{page + 1}",
            })

        content_sid = ensure_list_picker(
            friendly_name="booking_dates",
            body=f"✅ שירות: *{service_name}*\n\n📆 בחרו תאריך מהרשימה:",
            button_text="בחרו תאריך",
            items=items,
        )
        send_with_template(user_id, content_sid)

        # שמירת page בסשן לטיפול ב-"עוד ימים"
        set_state(user_id, STATE_BOOKING_DATE, {"date_page": page})
        return True
    except Exception:
        logger.warning("List Picker תאריכים נכשל, חוזרים לטקסט", exc_info=True)
        return False


def _date_prompt(user_id: str, service_name: str, service_duration: int = 60) -> str:
    """הודעת בחירת תאריך — ניסיון List Picker, fallback לטקסט.

    מחזיר מחרוזת ריקה אם ה-List Picker נשלח ישירות, או טקסט לשליחה.
    """
    # ניסיון לשלוח List Picker אינטראקטיבי
    if _send_date_list_picker(user_id, service_name, service_duration):
        return ""  # כבר נשלח ישירות

    # fallback — טקסט חופשי
    availability_note = ""
    try:
        from google_calendar import is_connected
        if is_connected():
            availability_note = "\n\n🟢 המערכת תבדוק זמינות ביומן אוטומטית."
    except ImportError:
        pass

    return (
        f"✅ שירות: *{service_name}*{availability_note}\n\n"
        "📆 מה ה*תאריך* המועדף?\n\n"
        "אפשר לכתוב למשל:\n"
        "• מחר / מחרתיים\n"
        "• יום ראשון / ביום שלישי\n"
        "• 15/03 / 14 במרץ\n\n"
        "(או שלחו *ביטול* לביטול)"
    )


def start_booking(user_id: str) -> str:
    """התחלת תהליך booking — מחזיר הודעת שירותים ממוספרת.

    נקרא כש-message_processor מזהה intent=APPOINTMENT_BOOKING.
    """
    # קביעת תורים כבויה — הגנת עומק (המסלול הרגיל כבר מנותב ל-HUMAN_AGENT
    # ב-message_processor). לא פותחים flow; מכוונים לבקשת נציג.
    if not db.is_booking_enabled():
        return (
            "כרגע לא ניתן לקבוע תור אונליין דרך הצ'אט. "
            "כתבו *נציג* ואעביר את פנייתכם ישירות לבעל העסק. 🙏"
        )
    # שליפת שירותים פעילים מ-DB
    services = db.get_all_services(active_only=True)
    if not services:
        # אין שירותים בטבלה — fallback ל-RAG (כמו בטלגרם)
        try:
            from ai_chatbot.llm import generate_answer, strip_source_citation
            from core.message_processor import (
                should_handoff_to_human, strip_handoff_marker, MessageResult,
            )
            from config import FALLBACK_RESPONSE
            history = db.get_conversation_history(user_id, limit=5)
            result = generate_answer(
                user_query="אילו שירותים אתם מציעים? פרטו בקצרה.",
                conversation_history=history,
                user_id=user_id,
                username=user_id,
                channel="whatsapp",
            )
            answer = strip_source_citation(result["answer"])
            # זיהוי handoff חייב להיות לפני הסרת הטוקן.
            is_handoff = should_handoff_to_human(answer)
            # מסירים את הטוקן בכל מקרה — אסור שיגיע ללקוח גם בטעות.
            answer = strip_handoff_marker(answer)

            if is_handoff:
                # ה-LLM סימן שאין לו מידע על שירותים — מעבירים לבעל העסק
                # במקום להציג את התשובה כאילו היא רשימת שירותים תקינה
                # (סימטרי לזרימה ב-bot/handlers.py:_booking_start_core).
                try:
                    from messaging.whatsapp_webhook import _handle_agent_request
                    from intent import Intent
                    fake_result = MessageResult(
                        text="",
                        intent=Intent.HUMAN_AGENT,
                        handoff_reason=(
                            "הלקוח ביקש לקבוע תור דרך WhatsApp, אך אין מידע זמין "
                            "על השירותים במאגר."
                        ),
                    )
                    _handle_agent_request(user_id, fake_result, profile_name=user_id)
                except Exception:
                    logger.error(
                        "WhatsApp booking handoff failed", exc_info=True,
                    )
                clear_state(user_id)
                return FALLBACK_RESPONSE

            if answer and answer.strip():
                # מעבר לשלב בחירת שירות — free-text (בלי service_map מ-DB)
                set_state(user_id, STATE_BOOKING_SERVICE, {"service_map": {}, "freetext_mode": True})
                return (
                    "📅 *בקשת תור*\n\n"
                    f"{answer}\n\n"
                    "אנא כתבו את *השירות* שתרצו להזמין:\n"
                    "(או שלחו *ביטול* לביטול)"
                )
        except Exception:
            logger.error("שגיאה בשליפת שירותים מ-RAG עבור WhatsApp booking", exc_info=True)
        # גם RAG לא הצליח — fallback סופי
        return (
            "📅 *בקשת תור*\n\n"
            "כרגע אין שירותים מוגדרים במערכת.\n"
            "אנא פנו לבית העסק ישירות לקביעת תור."
        )

    # שמירת מיפוי — מפתח הוא svc_<id> (ל-List Picker) או מספר סידורי (לטקסט)
    service_map = {}
    for i, svc in enumerate(services, 1):
        service_map[str(i)] = svc
        service_map[f"svc_{svc['id']}"] = svc  # מפתח ל-interactive payload

    set_state(user_id, STATE_BOOKING_SERVICE, {"service_map": service_map})

    # ניסיון לשלוח List Picker אינטראקטיבי (עד 10 פריטים)
    if len(services) <= 10:
        try:
            _send_service_list_picker(user_id, services)
            return ""  # מחרוזת ריקה = כבר נשלח ישירות, ה-webhook לא צריך לשלוח
        except Exception:
            logger.warning("List Picker נכשל, חוזרים לטקסט ממוספר", exc_info=True)

    # fallback — רשימה ממוספרת (גם כשיש יותר מ-10 שירותים)
    lines = ["📅 *בקשת תור*\n\nבחרו שירות:"]
    for i, svc in enumerate(services, 1):
        lines.append(f"{i}. {svc['name']}")
    lines.append("\n(שלחו את המספר)")
    return "\n".join(lines)


def handle_booking_step(user_id: str, text: str) -> str | None:
    """טיפול בהודעה כשיש state פתוח. מחזיר תשובה או None אם אין state.

    הפונקציה מנתבת לפי ה-state הנוכחי.
    """
    session = get_state(user_id)
    if session is None:
        return None

    state = session["state"]

    # ביטול — בכל שלב
    if text.lower().strip() in ("ביטול", "cancel", "בטל"):
        clear_state(user_id)
        return "❌ תהליך בקשת התור בוטל. איך עוד אפשר לעזור?"

    if state == STATE_BOOKING_SERVICE:
        return _handle_service_selection(user_id, text)
    elif state == STATE_BOOKING_DATE:
        return _handle_date_input(user_id, text)
    elif state == STATE_BOOKING_TIME:
        return _handle_time_input(user_id, text)
    elif state == STATE_BOOKING_CONFIRM:
        return _handle_confirmation(user_id, text)

    # state לא מוכר — ניקוי
    logger.warning("Unknown booking state %r for user %s", state, user_id)
    clear_state(user_id)
    return None


def _classify_freetext_input(text: str) -> str:
    """סיווג קלט בזרימת freetext_mode — האם זה שם שירות או משהו אחר.

    מחזיר אחד מהבאים:
    - "service" — נראה כשם שירות, ממשיכים לבחירת תאריך
    - "question" — שאלה / בקשת מידע, מעבירים ל-RAG ונשארים בשלב
    - "agent" — בקשת נציג / תלונה, יוצאים מהזרימה ומפעילים handoff
    - "cancel" — רוצה לבטל את ה-flow
    - "reschedule" — בקשת דחיית תור קיים (לא רלוונטי כאן)

    משתמש ב-detect_intent_with_llm הקיים בבוט במקום היוריסטיקות גסות.
    זה יקר יותר (קריאת LLM נוספת), אבל freetext_mode הוא נדיר בפרודקשן
    (רק כשאין שירותים ב-DB), אז ההשפעה זניחה.
    """
    t = (text or "").strip()
    if not t:
        return "service"  # ריק — מבקשים שוב

    # ביטול מפורש — מילים מקובלות בכל ערוצי הבוט
    lower = t.lower()
    if lower in ("ביטול", "cancel", "stop", "exit", "יציאה"):
        return "cancel"

    try:
        from intent import detect_intent_with_llm, Intent
        intent = detect_intent_with_llm(t)
    except Exception:
        logger.error("freetext intent detection failed", exc_info=True)
        # נופלים ל-fallback היוריסטי — שאלה לפי "?" או מילת שאלה
        if t.endswith("?") or t.endswith("؟"):
            return "question"
        return "service"

    # Intents שמסיטים את הזרימה למסלול אחר
    if intent in (Intent.HUMAN_AGENT, Intent.COMPLAINT):
        return "agent"
    if intent == Intent.APPOINTMENT_CANCEL:
        return "reschedule"  # לא באמת cancel של ה-flow, אלא בקשה לטפל בתור קיים
    # Intents שדורשים RAG (מענה על שאלה/בקשת מידע) ולא בחירת שירות
    if intent in (Intent.PRICING, Intent.BUSINESS_HOURS, Intent.LOCATION, Intent.FAREWELL):
        return "question"
    # GENERAL — אם זה ניסוח של שאלה ("?" או מילת שאלה) — מטפלים כשאלה.
    # אחרת מתייחסים כשם שירות (הכי נפוץ במצב הזה).
    if intent == Intent.GENERAL:
        if t.endswith("?") or t.endswith("؟"):
            return "question"
        # מילות שאלה בתחילת הטקסט
        question_starters = (
            "האם ", "מה ", "כמה ", "איזה ", "איזו ", "מתי ", "איפה ", "למה ",
            "what ", "how ", "when ", "where ", "why ", "which ",
        )
        if any(lower.startswith(w) for w in question_starters):
            return "question"
        return "service"
    # APPOINTMENT_BOOKING / APPOINTMENT_RESCHEDULE / GREETING — כברירת מחדל
    # נחשבים כשם שירות (המשתמש פשוט כתב משהו שדומה לשם של שירות).
    return "service"


def _handle_service_selection(user_id: str, text: str) -> str:
    """טיפול בבחירת שירות — לפי מספר, שם, או טקסט חופשי (מצב RAG)."""
    service_map = get_session_data(user_id, "service_map", {})
    freetext_mode = get_session_data(user_id, "freetext_mode", False)

    # מצב free-text (שירותים הגיעו מ-RAG, לא מ-DB) — מקבלים כל טקסט כשם שירות
    if freetext_mode:
        service_name = text.strip()
        if not service_name:
            return "אנא כתבו את שם השירות שתרצו להזמין:\n(או שלחו *ביטול* לביטול)"

        # סיווג חכם של הקלט — שואל שאלה? בקשת נציג? סתם שם שירות?
        # היוריסטיקה של "?" בלבד הייתה צרה מדי: "תעביר אותי לנציג",
        # "בעצם תספר לי על שירות X" וכד' היו מתפרשים כשם שירות.
        category = _classify_freetext_input(service_name)

        if category == "cancel":
            clear_state(user_id)
            return "❌ בקשת התור בוטלה. אין בעיה!\nאתם מוזמנים לבקש תור חדש בכל עת."

        if category == "agent":
            # יוצאים מ-booking ומפעילים handoff. סימטרי לזרימה ב-start_booking
            # כש-LLM מסמן handoff על שאלת השירותים.
            try:
                from messaging.whatsapp_webhook import _handle_agent_request
                from core.message_processor import MessageResult
                from intent import Intent
                fake_result = MessageResult(
                    text="",
                    intent=Intent.HUMAN_AGENT,
                    handoff_reason=(
                        "הלקוח ביקש להעביר לנציג בזמן בחירת שירות "
                        "ל-WhatsApp booking flow."
                    ),
                )
                _handle_agent_request(user_id, fake_result, profile_name=user_id)
            except Exception:
                logger.error("WhatsApp booking → agent handoff failed", exc_info=True)
            clear_state(user_id)
            return "📞 העברתי את הפנייה לבעל העסק. נציג יחזור אליכם בהקדם."

        if category == "reschedule":
            # אינטנט של ביטול/דחיית תור קיים — לא רלוונטי באמצע booking flow.
            # מנקים ומפנים לפעולה הנכונה.
            clear_state(user_id)
            return (
                "🔄 לביטול או דחייה של תור קיים — שלחו *ביטול תור* (או *cancel*).\n"
                "אם רציתם לקבוע תור חדש — שלחו *תור* שוב."
            )

        if category == "question":
            # שאלת המשך — מעבירים ל-RAG ונשארים בשלב STATE_BOOKING_SERVICE.
            try:
                from ai_chatbot.llm import generate_answer, strip_source_citation
                from core.message_processor import strip_handoff_marker
                history = db.get_conversation_history(user_id, limit=5)
                rag_result = generate_answer(
                    user_query=service_name,
                    conversation_history=history,
                    user_id=user_id,
                    username=user_id,
                    channel="whatsapp",
                )
                answer = strip_source_citation(rag_result.get("answer", ""))
                answer = strip_handoff_marker(answer)
                if answer and answer.strip():
                    return (
                        f"{answer}\n\n"
                        "📋 כשתסיימו לחשוב — אנא כתבו את *השירות* שתרצו להזמין:\n"
                        "(או שלחו *ביטול* לביטול)"
                    )
            except Exception:
                logger.error("RAG fallback failed for question in booking flow", exc_info=True)
            return (
                "🤔 לא הצלחתי לענות כרגע.\n"
                "אנא כתבו את *שם השירות* שתרצו להזמין, או שלחו *ביטול*."
            )

        # category == "service" — מתקדמים לבחירת תאריך
        # משך לחישוב זמינות — ברירת מחדל גלובלית (אין יותר משך פר-שירות)
        service_duration = int(
            db.get_appointment_duration_settings().get("default_minutes") or 60
        )
        set_state(user_id, STATE_BOOKING_DATE, {
            "booking_service": service_name,
            "booking_service_duration": service_duration,
        })
        return _date_prompt(user_id, service_name, service_duration)

    selected = None
    key = text.strip()
    # ניסיון לפי מפתח (מספר סידורי או svc_<id> מ-List Picker)
    if key in service_map:
        selected = service_map[key]
    else:
        # ניסיון לפי שם (case-insensitive)
        for svc in service_map.values():
            if svc["name"].lower() == key.lower():
                selected = svc
                break

    if not selected:
        # בחירה לא תקינה — הצגת הרשימה שוב (רק מפתחות מספריים, לא svc_<id>)
        lines = ["🤔 לא זיהיתי את הבחירה. אנא שלחו את *המספר* בלבד:\n"]
        for num, svc in service_map.items():
            if num.isdigit():
                lines.append(f"{num}. {svc['name']}")
        lines.append("\n(או שלחו *ביטול* לביטול)")
        return "\n".join(lines)

    # שמירת הבחירה ומעבר לשלב הבא
    service_name = selected["name"]
    # משך לחישוב זמינות — ברירת מחדל גלובלית (אין יותר משך פר-שירות)
    service_duration = int(
        db.get_appointment_duration_settings().get("default_minutes") or 60
    )

    set_state(user_id, STATE_BOOKING_DATE, {
        "booking_service": service_name,
        "booking_service_duration": service_duration,
    })

    return _date_prompt(user_id, service_name, service_duration)


def _handle_date_input(user_id: str, text: str) -> str:
    """טיפול בקלט תאריך — תומך ב-interactive_id מ-List Picker + טקסט חופשי."""
    # טיפול בלחיצת "עוד ימים" — שליחת דף הבא
    if text.startswith("date_more_"):
        try:
            page = int(text.split("_")[2])
        except (ValueError, IndexError):
            page = 1
        service_name = get_session_data(user_id, "booking_service", "")
        service_duration = get_session_data(user_id, "booking_service_duration", 60)
        if _send_date_list_picker(user_id, service_name, service_duration, page):
            return ""  # כבר נשלח ישירות
        return "אין עוד תאריכים פנויים. אנא כתבו תאריך ידנית.\n(או שלחו *ביטול* לביטול)"

    # לחיצה על תאריך מ-List Picker — date_YYYY-MM-DD
    if text.startswith("date_"):
        iso_part = text[5:]  # חיתוך "date_"
        try:
            _date_type.fromisoformat(iso_part)
            normalized = iso_part
        except ValueError:
            normalized = normalize_date(text)
    else:
        normalized = normalize_date(text)

    if normalized is None:
        return (
            "🤔 לא הצלחתי לזהות תאריך.\n\n"
            "אפשר לכתוב למשל:\n"
            "• מחר / מחרתיים\n"
            "• יום ראשון / ביום שלישי\n"
            "• 15/03 / 14 במרץ\n\n"
            "(או שלחו *ביטול* לביטול)"
        )

    service_name = get_session_data(user_id, "booking_service", "")
    service_duration = get_session_data(user_id, "booking_service_duration", 60)

    # בדיקת זמינות ביומן Google
    available_slots_text = ""
    no_slots = False
    try:
        from google_calendar import is_connected, get_available_slots
        if is_connected():
            target = _date_type.fromisoformat(normalized)
            buf_min = db.get_auto_booking_buffer_minutes()
            slots = get_available_slots(
                target, service_duration_minutes=service_duration,
                buffer_after_event_minutes=buf_min,
            )
            if slots:
                slots_str = " | ".join(f"*{s}*" for s in slots)
                available_slots_text = f"\n\n🟢 שעות פנויות: {slots_str}"
            else:
                no_slots = True
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בבדיקת זמינות Google Calendar (WhatsApp)", exc_info=True)

    if no_slots:
        return (
            f"📅 תאריך: *{_format_date_display(normalized)}*\n\n"
            "🔴 אין שעות פנויות בתאריך זה.\n"
            "אנא כתבו *תאריך אחר*.\n\n"
            "(או שלחו *ביטול* לביטול)"
        )

    set_state(user_id, STATE_BOOKING_TIME, {"booking_date": normalized})

    return (
        f"📅 תאריך: *{_format_date_display(normalized)}*{available_slots_text}\n\n"
        "🕐 איזו *שעה* מתאימה לכם?\n"
        "(לדוגמה: 10:00, אחר הצהריים, 14:00)\n\n"
        "(או שלחו *ביטול* לביטול)"
    )


def _handle_time_input(user_id: str, text: str) -> str | None:
    """טיפול בקלט שעה — הצגת סיכום לאישור."""
    preferred_time = text.strip()
    set_state(user_id, STATE_BOOKING_CONFIRM, {"booking_time": preferred_time})

    service = get_session_data(user_id, "booking_service", "")
    date_raw = get_session_data(user_id, "booking_date", "")
    date_display = _format_date_display(date_raw)

    summary = (
        "📋 *סיכום בקשת התור:*\n\n"
        f"• שירות: {service}\n"
        f"• תאריך: {date_display}\n"
        f"• שעה: {preferred_time}"
    )

    # ניסיון לשלוח כפתורי Quick Reply (כן/לא) עם content_variables
    if _send_confirm_buttons(user_id, service, date_display, preferred_time):
        return ""  # מחרוזת ריקה = כבר נשלח ישירות

    # fallback — טקסט רגיל
    return summary + "\n\nאנא אשרו — כתבו *כן* או *לא*:"


def _reenter_after_rejection(user_id: str, reason: str, message: str) -> str:
    """אחרי דחיית auto-booking — נשארים ב-flow בשלב המתאים כדי שהבוט ימשיך
    להאזין לתיקון של הלקוח (שעה/תאריך אחר), במקום לנקות state ולהשאיר את
    ההזמנה "בחרו אחר/ת" בלי מאזין (⇒ הלקוח נופל ל-RAG "לא הבנתי").

    set_state ממזג את הנתונים הקיימים (service/date), כך שהם נשמרים.
    סיבה סופית (חופשה / שגיאה פנימית / לא-ידוע) ⇒ מנקים ומציעים להתחיל מחדש.
    """
    if reason in _REJECT_RETRY_TIME:
        set_state(user_id, STATE_BOOKING_TIME)
        return message
    if reason in _REJECT_RETRY_DATE:
        set_state(user_id, STATE_BOOKING_DATE)
        return message
    clear_state(user_id)
    return f"{message}\n\nשלחו *תור* כדי לנסות שוב."


def _handle_confirmation(user_id: str, text: str) -> str:
    """טיפול באישור/ביטול סופי."""
    answer = text.strip().lower()

    # תמיכה ב-ButtonPayload מ-Quick Reply + טקסט חופשי
    _YES = {"yes", "y", "confirm", "כן", "אישור", "confirm_yes"}
    _NO = {"no", "n", "לא", "ביטול", "confirm_no"}

    if answer not in _YES:
        if answer in _NO:
            clear_state(user_id)
            return "❌ בקשת התור בוטלה. אין בעיה!\nאתם מוזמנים לבקש תור חדש בכל עת."
        # לא הבנו — מבקשים שוב
        return "אנא כתבו *כן* לאישור או *לא* לביטול:"

    service = get_session_data(user_id, "booking_service", "")
    date_raw = get_session_data(user_id, "booking_date", "")
    preferred_time = get_session_data(user_id, "booking_time", "")
    date_display = _format_date_display(date_raw)

    # בדיקה חוזרת מול Google Calendar
    try:
        from google_calendar import is_connected, get_available_slots
        if is_connected() and date_raw and preferred_time:
            target = _date_type.fromisoformat(date_raw)
            time_check = _normalize_time_for_gcal(preferred_time)
            if time_check is not None:
                # ברירת מחדל גלובלית — אין יותר משך פר-שירות
                svc_dur = int(
                    db.get_appointment_duration_settings().get("default_minutes") or 60
                )
                buf_min = db.get_auto_booking_buffer_minutes()
                slots = get_available_slots(
                    target, service_duration_minutes=svc_dur,
                    buffer_after_event_minutes=buf_min,
                )
                if time_check not in slots:
                    # נשארים ב-flow כדי שהבוט יאזין לתיקון (במקום לנקות state
                    # ולהשאיר "בחרו אחרת" בלי מאזין). מבחינים בין שני מצבים:
                    if not slots:
                        # אין שעות פנויות ביום הזה כלל — חוזרים לבחירת *תאריך*
                        # (כמו _handle_date_input), אחרת הלקוח תקוע בשלב השעה
                        # שבו אף שעה לא תעבוד באותו יום.
                        set_state(user_id, STATE_BOOKING_DATE)
                        return (
                            f"📅 תאריך: *{date_display}*\n\n"
                            "🔴 אין שעות פנויות בתאריך זה.\n"
                            "אנא כתבו *תאריך אחר*.\n\n"
                            "(או שלחו *ביטול* לביטול)"
                        )
                    # יש שעות אחרות — נשארים בשלב השעה ומציגים אותן
                    set_state(user_id, STATE_BOOKING_TIME)
                    slots_str = " | ".join(f"*{s}*" for s in slots)
                    return (
                        f"⚠️ לצערנו, השעה {preferred_time} כבר לא פנויה "
                        f"בתאריך {date_display}.\n\n🟢 שעות פנויות: {slots_str}\n\n"
                        "🕐 אנא בחרו *שעה אחרת*:\n"
                        "(או שלחו *ביטול* לביטול)"
                    )
    except ImportError:
        pass
    except Exception:
        logger.error("שגיאה בבדיקה חוזרת מול Google Calendar (WhatsApp)", exc_info=True)

    # בדיקת כפילות
    existing = [
        a for a in db.get_pending_appointments_for_user(user_id)
        if a["preferred_date"] == date_raw and a["preferred_time"] == preferred_time
    ]
    if existing:
        logger.info("תור כפול נחסם (WhatsApp): user=%s date=%s time=%s", user_id, date_raw, preferred_time)
        clear_state(user_id)
        return "⚠️ כבר יש לכם בקשת תור לתאריך ושעה אלו."

    # יצירת התור
    try:
        from sqlite3 import IntegrityError
        appt_id = db.create_appointment(
            user_id=user_id,
            username=db.get_username_for_user(user_id) or user_id,
            service=service,
            preferred_date=date_raw,
            preferred_time=preferred_time,
            channel="whatsapp",
        )
    except IntegrityError:
        logger.warning("כפילות תור WhatsApp (IntegrityError): user=%s", user_id)
        clear_state(user_id)
        return f"⚠️ כבר יש לכם בקשת תור לתאריך {date_display} בשעה {preferred_time}."
    except Exception:
        logger.error("שגיאה ביצירת תור WhatsApp: user=%s", user_id, exc_info=True)
        clear_state(user_id)
        return "⚠️ אירעה שגיאה ביצירת התור. אנא נסו שוב מאוחר יותר."

    # ─── החלטת auto-booking (לפי הגדרת בעל העסק) ───────────────────
    auto_confirmed = False
    rejected_reason: str | None = None
    try:
        from ai_chatbot.core.booking_decision import gather_and_decide
        decision = gather_and_decide(
            user_id=user_id,
            slot_date_str=date_raw,
            slot_time_str=preferred_time,
            # מחריגים את התור שזה עתה נוצר כדי שלא יחסום את השעה של עצמו
            # בבדיקת הזמינות מול היומן (calendar_busy כוזב).
            exclude_appointment_id=appt_id,
        )
        if decision.decision == "confirmed":
            duration_settings = db.get_appointment_duration_settings()
            duration = int(duration_settings.get("default_minutes") or 60)
            db.update_appointment_status(
                appt_id, "confirmed", confirmed_duration_minutes=duration,
            )
            auto_confirmed = True
        elif decision.decision == "rejected":
            # סדר חשוב: אם ה-update נכשל, ה-except למטה יתפוס ו-rejected_reason
            # יישאר None ⇒ נופלים חזרה למסלול pending רגיל (עקבי עם confirmed).
            db.update_appointment_status(appt_id, "cancelled")
            rejected_reason = decision.reason
    except Exception:
        logger.error("auto-booking decision failed (WhatsApp)", exc_info=True)

    # ─── מסלול דחייה ──────────────────────────────────────────
    if rejected_reason:
        from ai_chatbot.core.booking_decision import get_rejection_message
        rejection_msg = get_rejection_message(rejected_reason)
        # שומרים בהיסטוריה את הטקסט שהוצג ללקוח בלבד — לא קוד שגיאה פנימי.
        # קוד שגיאה ב-history עלול לדלוף ללקוח דרך LLM context או תצוגות אחרות.
        # סיבת הדחייה הפנימית מתועדת ב-logger בלבד (ראה gather_and_decide).
        logger.info(
            "WhatsApp booking rejected: user=%s date=%s time=%s reason=%s",
            user_id, date_raw, preferred_time, rejected_reason,
        )
        # נשארים ב-flow בשלב המתאים (שעה/תאריך) כדי שהבוט יאזין לתיקון,
        # במקום לנקות state ולהשאיר את "בחרו אחר/ת" בלי מאזין.
        client_msg = _reenter_after_rejection(user_id, rejected_reason, f"⚠️ {rejection_msg}")
        db.save_message(
            user_id, db.get_username_for_user(user_id) or user_id, "assistant",
            client_msg,
            channel="whatsapp",
        )
        return client_msg

    # התראה לבעל העסק
    _notify_owner_booking(
        user_id, appt_id, service, date_display, preferred_time,
        auto_confirmed=auto_confirmed,
    )

    # אישור אוטומטי — מפעיל את צינור ההתראה הסטנדרטי (ICS לוואטסאפ + GCal sync).
    # שומרים אם ההודעה הצליחה כדי לדעת אם נצטרך fallback קצר ללקוח.
    notify_succeeded = False
    if auto_confirmed:
        try:
            from appointment_notifications import notify_appointment_status
            appt = db.get_appointment(appt_id)
            if appt:
                notify_succeeded = bool(notify_appointment_status(appt))
        except Exception:
            logger.error(
                "auto-confirm: notify_appointment_status failed (WhatsApp)",
                exc_info=True,
            )

    # שמירה ב-DB
    db.save_message(
        user_id, db.get_username_for_user(user_id) or user_id, "assistant",
        f"בקשת תור: {service} בתאריך {date_display} בשעה {preferred_time}",
        channel="whatsapp",
    )

    clear_state(user_id)

    if auto_confirmed:
        # תמיד מחזירים ack מיידי ב-thread של WhatsApp — לא תלוי ב-notify_succeeded.
        # notify_appointment_status מחזיר True על סמך הצלחת Twilio API, לא delivery
        # בפועל; ובקצה תיתכן בעיית routing (channel לא תקין על ה-appt). אם נסתפק
        # ב-empty string, תרחיש הקצה הזה משאיר את הלקוח בלי שום סימן שהבקשה נקלטה.
        # ההודעה המפורטת עם הקישור ל-ICS תגיע מ-notify_appointment_status בנפרד.
        if notify_succeeded:
            # ack מינימלי — לא חוזרים ל-"✨ סבבה — תיכף תקבלו אישור" שביקשנו לבטל.
            return "✅ התור אושר ונקלט במערכת."
        # ההתראה הסטנדרטית נכשלה — fallback מלא (התור confirmed ב-DB).
        return (
            "✅ התור אושר!\n\n"
            f"• שירות: {service}\n"
            f"• תאריך: {date_display}\n"
            f"• שעה: {preferred_time}"
        )

    return (
        "📋 בקשת התור התקבלה!\n\n"
        f"• שירות: {service}\n"
        f"• תאריך: {date_display}\n"
        f"• שעה: {preferred_time}\n\n"
        "העברנו את הפרטים לבית העסק. "
        "ניצור איתכם קשר בהקדם לאישור סופי של השעה."
    )


def _notify_owner_booking(
    user_id: str, appt_id: int, service: str, date_display: str, preferred_time: str,
    auto_confirmed: bool = False,
) -> None:
    """התראה לבעל העסק על תור חדש מ-WhatsApp (WhatsApp או Telegram)."""
    display_name = db.get_username_for_user(user_id) or user_id
    phone_display = _format_phone(user_id)
    panel_link = f"\n🔗 {ADMIN_URL}/appointments" if ADMIN_URL else ""
    header = (
        f"✅ תור חדש אושר אוטומטית #{appt_id} (WhatsApp)"
        if auto_confirmed
        else f"📅 בקשת תור חדשה #{appt_id} (WhatsApp)"
    )
    notification = (
        f"{header}\n\n"
        f"לקוח: {display_name}\n"
        f"טלפון: {phone_display}\n"
        f"שירות: {service}\n"
        f"תאריך: {date_display}\n"
        f"שעה: {preferred_time}"
        f"{panel_link}"
    )
    try:
        from messaging.whatsapp_sender import notify_owner_whatsapp
        notify_owner_whatsapp(notification)
    except Exception as e:
        logger.error("Failed to notify owner (WhatsApp) about booking: %s", e)
    # fallback — אם מוגדר גם טלגרם, שולח גם שם
    if TELEGRAM_OWNER_CHAT_ID:
        try:
            from live_chat_service import send_telegram_message
            send_telegram_message(str(TELEGRAM_OWNER_CHAT_ID), notification)
        except Exception as e:
            logger.error("Failed to notify owner (Telegram) about booking: %s", e)
