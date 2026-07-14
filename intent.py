"""
Intent Detection Module — classifies user messages to optimize routing.

גישה היברידית:
  1. Fast path — regex לברכות/פרידות (anchored, מדויק, חוסך קריאת API).
  2. LLM path  — function calling לכל השאר. תופס ניסוחים טבעיים
     שה-regex מפספס (למשל "אפשר להגיע מחר?" → APPOINTMENT_BOOKING).
  3. Fallback  — אם ה-LLM לא זמין או מושבת, חוזרים ל-regex המקורי.

Supported intents:
  GREETING              — "Hi", "Hello", "שלום"           → Direct response (no RAG)
  FAREWELL              — "Thanks", "Bye", "תודה"         → Direct response + feedback
  BUSINESS_HOURS        — "Are you open?", "שעות פתיחה"   → Direct response (hours status)
  PRICING               — "How much?", "כמה עולה?"       → Targeted RAG (pricing)
  APPOINTMENT_BOOKING   — "Want appointment", "רוצה תור"  → Trigger booking flow
  APPOINTMENT_CANCEL    — "Want to cancel", "לבטל תור"    → Trigger cancellation flow
  HUMAN_AGENT           — "תעביר לנציג", "talk to agent"  → Direct handoff to human
  COMPLAINT             — "תלונה", "bad service"          → Offers human agent
  LOCATION              — "Where are you?", "איך מגיעים"  → RAG query for address
  GENERAL               — Everything else                 → Full RAG (current behavior)
"""

import json
import re
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class Intent(Enum):
    GREETING = "greeting"
    FAREWELL = "farewell"
    BUSINESS_HOURS = "business_hours"
    APPOINTMENT_BOOKING = "appointment_booking"
    APPOINTMENT_CANCEL = "appointment_cancel"
    APPOINTMENT_RESCHEDULE = "appointment_reschedule"
    PRICING = "pricing"
    COMPLAINT = "complaint"
    HUMAN_AGENT = "human_agent"
    LOCATION = "location"
    GENERAL = "general"


# ─── מיפוי ערכי מחרוזת ל-Intent enum ────────────────────────────────────────
_INTENT_BY_VALUE: dict[str, Intent] = {i.value: i for i in Intent}


# ─── Regex fast path — ברכות ופרידות בלבד (anchored, מדויק) ────────────────
# ברכות ופרידות הן הודעות קצרות עם anchor (^...$) — regex מדויק מאוד.
# שאר הכוונות עוברות ל-LLM שתופס ניסוחים טבעיים טוב יותר.

_GREETING_PATTERN = re.compile(
    r"^("
    r"hi|hello|hey|hiya|good morning|good evening|good afternoon"
    r"|שלום|היי|הי|בוקר טוב|ערב טוב|צהריים טובים|מה נשמע|מה קורה|אהלן|הלו"
    r")[.!?\s]*$",
    re.IGNORECASE,
)

_FAREWELL_PATTERN = re.compile(
    r"^("
    r"thanks|thank you|bye|goodbye|see you|have a good day|good night"
    r"|תודה|תודה רבה|ביי|ביביי|להתראות|יום טוב|לילה טוב|שבוע טוב|יאללה ביי"
    r")[.!?\s]*$",
    re.IGNORECASE,
)

_FAST_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    (Intent.GREETING, _GREETING_PATTERN),
    (Intent.FAREWELL, _FAREWELL_PATTERN),
]


# ─── Regex fallback — כל הכוונות (למקרה שה-LLM לא זמין) ────────────────────
_FALLBACK_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    (Intent.GREETING, _GREETING_PATTERN),
    (Intent.FAREWELL, _FAREWELL_PATTERN),
    # Business hours
    (
        Intent.BUSINESS_HOURS,
        re.compile(
            r"("
            r"are\s*you\s*open|when\s*(do\s*you|are\s*you)\s*(open|close)"
            r"|what\s*(are\s*)?your\s*hours|opening\s*hours|business\s*hours"
            r"|what\s*time\s*(do\s*you|are\s*you)\s*(open|close)"
            r"|is\s*(the\s*)?(store|shop|salon)\s*open"
            r"|שעות\s*פתיחה|שעות\s*פעילות|שעות\s*עבודה"
            r"|מתי\s*(אתם\s*)?(פותחים|סוגרים|פתוחים)"
            r"|אתם\s*פתוחים|פתוח\s*היום|פתוח\s*עכשיו|פתוחים\s*היום|פתוחים\s*עכשיו"
            r"|האם\s*(אתם\s*)?פתוחים|סגור\s*היום|סגורים\s*היום"
            r"|עד\s*מתי\s*(אתם\s*)?(פתוחים|פתוח)|עד\s*כמה\s*(אתם\s*)?פתוחים"
            r"|מה\s*שעות\s*(הפתיחה|הפעילות)"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Pricing
    (
        Intent.PRICING,
        re.compile(
            r"("
            r"how\s*much|what.*price\b|what.*cost\b|pricing|price\s*list"
            r"|כמה\s*עולה|כמה\s*זה\s*עולה|מה\s*המחיר|מה\s*העלות|מחיר|מחירון|מחירים"
            r"|כמה\s*יעלה|כמה\s*כסף|עלות|תעריף|תעריפים"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Appointment booking
    (
        Intent.APPOINTMENT_BOOKING,
        re.compile(
            r"("
            r"book\s*(an?\s*)?appointment|make\s*(an?\s*)?appointment"
            r"|schedule\s*(an?\s*)?appointment|set\s*up\s*(an?\s*)?appointment"
            r"|i\s*want\s*(an?\s*)?appointment|i\s*want\s*to\s*book"
            r"|רוצה\s*תור|רוצה\s*לקבוע\s*תור|לקבוע\s*תור|אפשר\s*תור|אפשר\s*לקבוע\s*תור"
            r"|קביעת\s*תור|לזמן\s*תור|אני\s*רוצה\s*לקבוע\s*תור"
            r"|בואו\s*נקבע\s*תור|יש\s*תורים\s*פנויים|מתי\s*אפשר\s*לקבוע\s*תור"
            # פגישה — מילה נרדפת לתור. ביטויים ספציפיים בלבד (כמו ב"תור"),
            # לא "פגישה" חשופה, כדי לא לתפוס "הייתה לי פגישה" וכד'.
            r"|רוצה\s*פגישה|לקבוע\s*פגישה|לתאם\s*פגישה|קביעת\s*פגישה|אפשר\s*פגישה|לזמן\s*פגישה"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Appointment cancellation
    (
        Intent.APPOINTMENT_CANCEL,
        re.compile(
            r"("
            r"cancel\s*(my\s*)?appointment|cancel\s*(my\s*)?booking"
            r"|i\s*want\s*to\s*cancel\s*(my\s*)?(appointment|booking|the\s*appointment)"
            r"|לבטל\s*(את\s*)?ה?תור|ביטול\s*(ה)?תור|רוצה\s*לבטל\s*(את\s*)?ה?תור|אני\s*מבטל\s*(את\s*)?ה?תור"
            r"|אני\s*רוצה\s*לבטל\s*את\s*התור|אני\s*צריך\s*לבטל\s*(את\s*)?ה?תור"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Appointment reschedule
    (
        Intent.APPOINTMENT_RESCHEDULE,
        re.compile(
            r"("
            r"reschedule\s*(my\s*)?appointment|reschedule\s*(my\s*)?booking"
            r"|change\s*(the\s*)?(date|time)\s*(of\s*)?.*appointment"
            r"|move\s*(my\s*)?appointment|shift\s*(my\s*)?appointment"
            r"|לשנות\s*(את\s*)?ה?תור|שינוי\s*(ה)?תור|רוצה\s*לשנות\s*(את\s*)?ה?תור"
            r"|להזיז\s*(את\s*)?ה?תור|לדחות\s*(את\s*)?ה?תור|להקדים\s*(את\s*)?ה?תור"
            r"|אני\s*רוצה\s*לשנות\s*את\s*התור|אני\s*צריך\s*לשנות\s*(את\s*)?ה?תור"
            r"|לשנות\s*(את\s*)?(ה)?תאריך\s*(של\s*)?ה?תור|לשנות\s*(את\s*)?(ה)?שעה\s*(של\s*)?ה?תור"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Human agent
    (
        Intent.HUMAN_AGENT,
        re.compile(
            r"("
            r"talk\s*to\s*(an?\s*)?(human|person|agent|representative|someone)"
            r"|i\s*need\s*(an?\s*)?(human|person|agent)"
            r"|transfer\s*(me\s*)?(to\s*)?(an?\s*)?(human|agent|representative)"
            r"|can\s*i\s*(speak|talk)\s*(to|with)\s*(an?\s*)?(human|person|agent|representative)"
            r"|תעביר\s*(אותי\s*)?(ל)?נציג|אדם\s*אמיתי"
            r"|לדבר\s*עם\s*(מישהו|בנאדם|נציג|אדם|בעל\s*העסק|בעלים)"
            r"|אני\s*רוצה\s*(לדבר\s*עם\s*)?(נציג|בנאדם|אדם|בעל\s*העסק|בעלים)"
            r"|תן\s*לי\s*נציג|תני\s*לי\s*נציג"
            r"|אפשר\s*נציג|אפשר\s*לדבר\s*עם\s*(נציג|מישהו|בעל\s*העסק|בעלים)"
            r"|תעבירו\s*(אותי\s*)?(ל)?(נציג|בעל\s*העסק|בעלים)"
            r"|תעביר(ו|י)?\s*(את\s*)?(זה|הפנייה|ההודעה)\s*(ל)?(נציג|בעל\s*העסק|בעלים)"
            r"|רוצה\s*נציג"
            r"|מבקש\s*ש?(יחזרו|יחזור|בעל\s*העסק|מישהו)"
            r"|ש(יחזרו|יחזור)\s*אלי"
            r"|בעל\s*העסק\s*ש?י(חזור|תקשר)"
            r"|^נציג[.!?\s]*$"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Complaint
    (
        Intent.COMPLAINT,
        re.compile(
            r"("
            r"i\s*(want\s*to\s*)?complain|complaint|not\s*happy|not\s*satisfied|terrible\s*service"
            r"|bad\s*service|worst\s*service|awful|disgusting|unacceptable|ridiculous|rip\s*off"
            r"|i\s*want\s*a\s*refund|give\s*me\s*my\s*money\s*back|waste\s*of\s*(time|money)"
            r"|אני\s*לא\s*מרוצה|לא\s*מרוצה|יש\s*לי\s*בעיה|רוצה\s*להתלונן|תלונה"
            r"|שירות\s*גרוע|שירות\s*נוראי|מאוכזב|מאוכזבת|אני\s*כועס|אני\s*כועסת"
            r"|לא\s*בסדר|חוויה\s*רעה|חוויה\s*גרועה"
            r"|אוי\s*נו|באסה|דבילי|שירות\s*על\s*הפנים|לא\s*עונה\s*על\s*השאלה"
            r"|בושה|בושה\s*וחרפה|איזה\s*זלזול|שירות\s*פח"
            r"|עושים\s*צחוק|עושה\s*צחוק"
            r"|תבטלו\s*את\s*ההזמנה|רוצה\s*זיכוי|תחזירו\s*לי\s*את\s*הכסף"
            r"|אני\s*עוזב|לא\s*קונה\s*(אצלכם|פה)\s*יותר"
            r"|מחכה\s*כבר\s*שעות|אף\s*אחד\s*לא\s*עונה|לא\s*מגיבים"
            r"|כבר\s*שעה\s*שאני\s*מחכה|מתי\s*כבר\s*תענו"
            r")",
            re.IGNORECASE,
        ),
    ),
    # Location
    (
        Intent.LOCATION,
        re.compile(
            r"("
            r"where\s*are\s*you|what.*address|how\s*(do\s*i\s*)?get\s*there|your\s*location|directions"
            r"|איפה\s*אתם|מה\s*הכתובת|כתובת|איך\s*מגיעים|איך\s*אפשר\s*להגיע|מיקום|היכן\s*אתם"
            r"|איפה\s*(ה)?(חנות|סלון|עסק|מקום)|הגעה"
            r")",
            re.IGNORECASE,
        ),
    ),
]


# ─── LLM Function Calling — הגדרת הכלי לסיווג כוונות ──────────────────────

_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": "סיווג כוונת הודעת הלקוח לקטגוריה המתאימה",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [i.value for i in Intent],
                    "description": (
                        "הכוונה המזוהה:\n"
                        "- greeting: ברכה בלבד (שלום, היי, בוקר טוב)\n"
                        "- farewell: פרידה/תודה בלבד (ביי, תודה, להתראות)\n"
                        "- business_hours: שאלה על שעות פתיחה/סגירה/זמינות\n"
                        "- pricing: שאלה על מחיר, עלות, תעריף\n"
                        "- appointment_booking: רצון לקבוע/לזמן תור או פגישה, לבוא, להגיע\n"
                        "- appointment_cancel: רצון לבטל תור קיים\n"
                        "- appointment_reschedule: רצון לשנות תאריך או שעה של תור קיים (לא לבטל, אלא להזיז)\n"
                        "- human_agent: בקשה לדבר עם נציג/אדם אמיתי\n"
                        "- complaint: תלונה, תסכול, חוויה רעה\n"
                        "- location: שאלה על כתובת, מיקום, הגעה\n"
                        "- general: כל שאלה אחרת שלא מתאימה לקטגוריות למעלה"
                    ),
                },
            },
            "required": ["intent"],
        },
    },
}

_LLM_SYSTEM_PROMPT = (
    "אתה מסווג כוונות הודעות לקוחות של עסק.\n"
    "קרא את ההודעה וסווג אותה לקטגוריה המתאימה ביותר באמצעות הפונקציה classify_intent.\n"
    "דוגמאות שחשוב לתפוס:\n"
    '- "אפשר להגיע מחר?" → appointment_booking\n'
    '- "יש לכם מקום ביום שלישי?" → appointment_booking\n'
    '- "מתי אתם פנויים?" → appointment_booking\n'
    '- "אני רוצה לבוא" → appointment_booking\n'
    '- "אני רוצה לקבוע פגישה" → appointment_booking\n'
    '- "אפשר לתאם פגישה?" → appointment_booking\n'
    '- "אני רוצה לשנות את התור" → appointment_reschedule\n'
    '- "אפשר להזיז את התור?" → appointment_reschedule\n'
    '- "זה יקר לי" → pricing\n'
    '- "פתוחים בשבת?" → business_hours\n'
    '- "איפה זה?" → location\n'
    '- "אני מתוסכל" → complaint\n'
    '- "תעזרו לי" → human_agent (רק אם ברור שרוצים אדם אמיתי)\n'
    "אם לא ברור — סווג כ-general."
)


def detect_intent(message: str) -> Intent:
    """
    Fast path — סיווג regex בלבד (ברכות ופרידות).

    לשימוש פנימי וכ-fallback. ה-handler קורא ל-detect_intent_with_llm()
    שמשלב regex + LLM.
    """
    text = message.strip()
    if not text:
        return Intent.GENERAL

    for intent, pattern in _FAST_PATTERNS:
        if pattern.search(text):
            logger.info("Intent detected (fast): %s for message: '%s'", intent.value, text[:60])
            return intent

    return Intent.GENERAL


def _detect_intent_regex_full(message: str) -> Intent:
    """
    Regex מלא — כל הכוונות (fallback כשה-LLM לא זמין).
    """
    text = message.strip()
    if not text:
        return Intent.GENERAL

    for intent, pattern in _FALLBACK_PATTERNS:
        if pattern.search(text):
            logger.info("Intent detected (regex fallback): %s for message: '%s'", intent.value, text[:60])
            return intent

    logger.info("Intent detected (regex fallback): general for message: '%s'", text[:60])
    return Intent.GENERAL


def _detect_intent_llm(message: str) -> Intent:
    """
    סיווג כוונה באמצעות LLM function calling.

    משתמש במודל קל (INTENT_MODEL) עם tool_choice=required כדי לכפות
    קריאה לפונקציית הסיווג. מחזיר Intent.GENERAL בכל מקרה של כשל.
    """
    from ai_chatbot.openai_client import get_openai_client
    from ai_chatbot.config import INTENT_MODEL

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            tools=[_INTENT_TOOL],
            tool_choice={"type": "function", "function": {"name": "classify_intent"}},
            temperature=0,
            max_tokens=50,
        )

        # חילוץ תוצאת ה-function call
        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            logger.warning("LLM returned no tool_calls for message: '%s'", message[:60])
            return _detect_intent_regex_full(message)
        tool_call = tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        intent_value = args.get("intent", "general")
        intent = _INTENT_BY_VALUE.get(intent_value, Intent.GENERAL)

        logger.info("Intent detected (LLM): %s for message: '%s'", intent.value, message[:60])
        return intent

    except Exception as e:
        logger.error("LLM intent detection failed, falling back to regex: %s", e)
        return _detect_intent_regex_full(message)


def detect_intent_with_llm(message: str) -> Intent:
    """
    סיווג כוונה היברידי — regex fast path + LLM fallback.

    1. ברכות/פרידות — regex (מדויק, חוסך API call).
    2. אם regex לא מצא כוונה ספציפית — LLM function calling.
    3. אם LLM מושבת או נכשל — regex מלא כ-fallback.
    """
    from ai_chatbot.config import LLM_INTENT_ENABLED

    text = message.strip()
    if not text:
        return Intent.GENERAL

    # שלב 1: regex מהיר לברכות/פרידות
    fast_intent = detect_intent(text)
    if fast_intent != Intent.GENERAL:
        return fast_intent

    # שלב 2: LLM (אם מופעל)
    if LLM_INTENT_ENABLED:
        return _detect_intent_llm(text)

    # שלב 3: fallback ל-regex מלא
    return _detect_intent_regex_full(text)


# ─── Direct responses (no RAG needed) ────────────────────────────────────────

_GREETING_RESPONSES = [
    "שלום! 👋 ברוכים הבאים. איך אפשר לעזור לכם היום?",
]

_FAREWELL_RESPONSES = [
    "תודה שפניתם אלינו! 😊 אם תצטרכו עוד משהו, אנחנו כאן.\n\n"
    "נשמח לשמוע מכם — איך הייתה החוויה שלכם?",
]


def get_direct_response(intent: Intent) -> str | None:
    """
    Return a canned response for intents that don't require RAG.

    Returns None for intents that should go through the RAG pipeline.
    """
    if intent == Intent.GREETING:
        return _GREETING_RESPONSES[0]
    if intent == Intent.FAREWELL:
        return _FAREWELL_RESPONSES[0]
    return None
