"""
Follow-up Configuration — תבניות הודעות, פרומפטים ו-JSON schema למנוע ההחלטה.

מודול זה מכיל את כל ההגדרות הסטטיות של מערכת ה-follow-up:
- תבניות הודעות (templates) לשליחה ללקוחות
- System prompt בעברית למנוע ההחלטה (Gemini Flash)
- JSON schema לתשובה מובנית מה-LLM
- פרומפט לניתוח שיחה (lead analysis)
"""

# ── תבניות הודעות ────────────────────────────────────────────────────────────
# {name_suffix} — " דניאל" (עם רווח) אם יש שם, אחרת ריק.
# {service_name} — שם השירות שהלקוח התעניין בו.

FOLLOWUP_TEMPLATES: dict[str, str] = {
    "followup_interest_check": (
        "היי{name_suffix}, רצינו לבדוק אם עדיין רלוונטי לך "
        "לקבל מידע לגבי {service_name}. "
        "אם כן, אפשר לענות כאן ונשמח לעזור 😊"
    ),
    "followup_booking_resume": (
        "היי{name_suffix}, ראינו שהתעניינת ב{service_name}. "
        "אם עדיין תרצה, אפשר להמשיך מכאן "
        "ולעזור לך להשלים את התהליך 📅"
    ),
    "followup_answer_questions": (
        "היי{name_suffix}, אם עדיין יש לך שאלות "
        "לגבי {service_name}, אפשר לענות כאן ונשמח לעזור 💬"
    ),
}

TEMPLATE_KEYS = list(FOLLOWUP_TEMPLATES.keys())


def render_template(template_key: str, *, name: str = "", service_name: str = "") -> str:
    """רינדור תבנית follow-up עם משתנים.

    Args:
        template_key: מפתח מתוך FOLLOWUP_TEMPLATES.
        name: שם הלקוח (אופציונלי).
        service_name: שם השירות.

    Returns:
        טקסט ההודעה המוכנה לשליחה.
    """
    template = FOLLOWUP_TEMPLATES.get(template_key, FOLLOWUP_TEMPLATES["followup_interest_check"])
    name_suffix = f" {name}" if name else ""
    return template.format(
        name_suffix=name_suffix,
        service_name=service_name or "השירות שהתעניינת בו",
    )


# ── פרומפט לניתוח שיחה (Lead Analysis) ──────────────────────────────────────
# נשלח ל-LLM כדי לסכם שיחה ולהחליט אם הלקוח מתאים ל-follow-up.

LEAD_ANALYSIS_PROMPT = """\
נתח את השיחה הבאה בין לקוח לבוט עסקי, והחזר JSON בלבד (ללא טקסט נוסף).

עליך לזהות:
1. באיזה שירות הלקוח התעניין (service_of_interest)
2. מה סוג הכוונה (intent_type): info_only / price_check / availability_check / booking_intent / support_issue / complaint / unknown
3. מה "טמפרטורת" הליד (lead_temperature): cold / warm / hot
   - cold: שאלה כללית בלי כוונת רכישה
   - warm: שאל על מחיר או זמינות
   - hot: ביקש לקבוע תור, שאל על מחיר + זמינות, או הגיע קרוב להזמנה
4. סיכום קצר בעברית (summary)

חוקים:
- אם הלקוח כבר קבע תור או השלים הזמנה — lead_temperature חייב להיות "cold".
- אם הלקוח אמר "לא מעוניין" / "תודה, לא צריך" — lead_temperature חייב להיות "cold".
- שאלה על מחיר או זמינות = לפחות "warm".
- ביקש לקבוע / הגיע לשלב הזמנה ולא סיים = "hot".

פורמט תשובה (JSON בלבד):
```json
{
  "service_of_interest": "שם השירות",
  "intent_type": "booking_intent",
  "lead_temperature": "hot",
  "summary": "סיכום קצר בעברית"
}
```

השיחה:
"""

# ── פרומפט למנוע ההחלטה (Follow-up Decision Engine) ─────────────────────────
# נשלח ל-Gemini Flash עם נתוני הליד — מחזיר החלטה מובנית.

FOLLOWUP_DECISION_PROMPT = """\
הנחיות לניתוח שיחה וקבלת החלטת פולו-אפ

נתח את נתוני השיחה שהוזנו והחלט האם על העסק לשלוח הודעת המשך (Follow-up).

החזר *אך ורק* JSON תקין במבנה הבא — חובה לכלול את כל השדות:
{
  "should_send_followup": <true או false — האם לשלוח follow-up>,
  "confidence": <מספר שלם 0-100 — רמת הביטחון בהחלטה. 0-29=נמוך, 30-69=בינוני, 70-100=גבוה>,
  "lead_temperature": <"cold" | "warm" | "hot" — חום הליד>,
  "intent_type": <"info_only" | "price_check" | "availability_check" | "booking_intent" | "support_issue" | "complaint" | "post_booking" | "unknown">,
  "recommended_template_key": <מפתח תבנית אחד או null אם אין לשלוח>,
  "template_variables": {"service_name": "<שם השירות אם רלוונטי>"},
  "reason_summary": "<משפט קצר 1-2 שורות שמסביר את ההחלטה>"
}

דוגמה לליד "חם" (לקוח שאל על מחיר ועבר זמן ביחיב):
{
  "should_send_followup": true,
  "confidence": 75,
  "lead_temperature": "warm",
  "intent_type": "price_check",
  "recommended_template_key": "followup_answer_questions",
  "template_variables": {"service_name": "טיפול שיניים"},
  "reason_summary": "הלקוח שאל על מחיר ולא סיים — שווה תזכורת אדיבה"
}

דוגמה לליד שאין להגיב אליו (לקוח כבר קבע תור):
{
  "should_send_followup": false,
  "confidence": 95,
  "lead_temperature": "hot",
  "intent_type": "post_booking",
  "recommended_template_key": null,
  "template_variables": {},
  "reason_summary": "הלקוח כבר קבע תור — אין צורך ב-follow-up"
}

סדרי עדיפויות לקבלת החלטה:
1. הגנה על חווית המשתמש.
2. מניעת ספאם.
3. עמידה בחוקי הערוץ (וואטסאפ/טלגרם).
4. שליחת הודעת המשך אך ורק אם הייתה כוונת רכישה ממשית והמשתמש נטש לפני ביצוע הפעולה (Conversion).

מדריך פרשנות:
- שאלות על "מחיר" או "זמינות" = אותות רכישה חזקים יותר משאלה כללית. confidence גבוה (60+).
- "כבר קבע תור/הזמין" = should_send_followup=false, intent_type=post_booking.
- "לא מעוניין / עצור / אל תיצור קשר" = should_send_followup=false, confidence=95+.
- תלונה / נושא תמיכה = should_send_followup=false (לא מתאים שיווק).

מפתחות תבניות זמינים:
- followup_interest_check — בדיקת עניין כללית (הלקוח שאל על שירות)
- followup_booking_resume — המשך תהליך הזמנה (הלקוח כמעט קבע תור)
- followup_answer_questions — מענה על שאלות (הלקוח שאל שאלות ולא סיים)

קלט (Input):
"""

# ── JSON Schema לתשובת מנוע ההחלטה ──────────────────────────────────────────

FOLLOWUP_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "should_send_followup",
        "confidence",
        "lead_temperature",
        "intent_type",
        "recommended_template_key",
        "template_variables",
        "reason_summary",
    ],
    "properties": {
        "should_send_followup": {"type": "boolean"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "lead_temperature": {
            "type": "string",
            "enum": ["cold", "warm", "hot"],
        },
        "intent_type": {
            "type": "string",
            "enum": [
                "info_only", "price_check", "availability_check",
                "booking_intent", "support_issue", "complaint",
                "post_booking", "unknown",
            ],
        },
        "recommended_template_key": {
            "type": ["string", "null"],
            "enum": [
                "followup_interest_check",
                "followup_booking_resume",
                "followup_answer_questions",
                None,
            ],
        },
        "template_variables": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
            },
        },
        "reason_summary": {"type": "string", "maxLength": 500},
    },
}

# ── JSON Schema לניתוח שיחה (Lead Analysis) ─────────────────────────────────

LEAD_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service_of_interest", "intent_type", "lead_temperature", "summary"],
    "properties": {
        "service_of_interest": {"type": "string"},
        "intent_type": {
            "type": "string",
            "enum": [
                "info_only", "price_check", "availability_check",
                "booking_intent", "support_issue", "complaint", "unknown",
            ],
        },
        "lead_temperature": {
            "type": "string",
            "enum": ["cold", "warm", "hot"],
        },
        "summary": {"type": "string", "maxLength": 500},
    },
}
