"""
JSON Schema ל-Structured Outputs של ה-Fact Extractor.

מוגדר לפי docs/Customer-memory/json_schema.md, להעברה ל-OpenAI API עם
`response_format={"type": "json_schema", "json_schema": EXTRACTOR_SCHEMA}`.

הערות חשובות:
- `strict: True` — OpenAI לא יחזיר JSON שלא תואם לסכמה.
- `additionalProperties: False` — מונע מהמודל להוסיף שדות שלא הוגדרו.
- `confidence` מינימום 0.6 לא נכפה בסכמה (strict mode לא תומך ב-`minimum`).
  הסינון מתבצע ב-post-validation (memory/validator.py, שלב 4).
- `nullable` ב-strict mode מיוצג כ-`"type": ["integer", "null"]`.
"""


EXTRACTOR_SCHEMA = {
    "name": "customer_fact_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "extractions": {
                "type": "array",
                "description": "רשימת עובדות שחולצו מהשיחה. ריק אם אין עובדות מתאימות.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "confirm", "supersede", "resolve"],
                            "description": "add = עובדה חדשה, confirm = אישור של existing fact, supersede = החלפת existing fact, resolve = סגירת open_issue קיים (content=null)",
                        },
                        "fact_type": {
                            "type": "string",
                            "enum": [
                                "preference",
                                "personal_info",
                                "relationship",
                                "vocabulary",
                                "open_issue",
                            ],
                            "description": "סוג העובדה",
                        },
                        "content": {
                            "type": ["string", "null"],
                            "description": "ניסוח העובדה בעברית, גוף שלישי, עד 15 מילים. null עבור action=resolve.",
                        },
                        "requires_consent": {
                            "type": "boolean",
                            "description": "True אם העובדה נוגעת למידע רגיש - שחשיפתו עלולה לפגוע בלקוח, או שנחשב פרטי לפי נורמות מקובלות (בריאות/הריון/נפשי/פיננסי/מיני). מידע דמוגרפי בסיסי או תזונה ניטרלית אינם רגישים.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "ציון ביטחון 0.6-1.0 לפי ה-rubric. ערכים מתחת 0.6 - אל תחזיר את ה-extraction כלל.",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "ציטוט קצר או paraphrase צמוד מהשיחה התומך בעובדה",
                        },
                        "supersedes_id": {
                            "type": ["integer", "null"],
                            "description": "ID של existing_fact שהעובדה הזו מחליפה. חובה אם action=supersede, אחרת null.",
                        },
                        "confirms_id": {
                            "type": ["integer", "null"],
                            "description": "ID של existing_fact שהעובדה הזו מאשרת. חובה אם action=confirm, אחרת null.",
                        },
                        "resolves_id": {
                            "type": ["integer", "null"],
                            "description": "ID של open_issue קיים שהעובדה סוגרת. חובה אם action=resolve, אחרת null.",
                        },
                    },
                    "required": [
                        "action",
                        "fact_type",
                        "content",
                        "requires_consent",
                        "confidence",
                        "evidence",
                        "supersedes_id",
                        "confirms_id",
                        "resolves_id",
                    ],
                    "additionalProperties": False,
                },
            },
            "skipped": {
                "type": "array",
                "description": "מועמדים שנשקלו אך לא חולצו, לצרכי דיבאג. רק מקרים גבוליים משמעותיים.",
                "items": {
                    "type": "object",
                    "properties": {
                        "proposed_fact": {
                            "type": "string",
                            "description": "תיאור קצר של העובדה המוצעת שנשקלה ולא נבחרה",
                        },
                        "reason": {
                            "type": "string",
                            "description": "הסיבה לדילוג - כלל ספציפי משער הקבלה שלא התקיים",
                        },
                    },
                    "required": ["proposed_fact", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["extractions", "skipped"],
        "additionalProperties": False,
    },
}
