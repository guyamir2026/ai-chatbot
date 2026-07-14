# JSON Schema ל-Structured Outputs

Schema להעברה ל-OpenAI API עם `response_format={"type": "json_schema", "json_schema": {...}}`.

## הסכמה המלאה

```python
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
                            "enum": ["add", "confirm", "supersede"],
                            "description": "add = עובדה חדשה, confirm = אישור של existing fact, supersede = החלפת existing fact"
                        },
                        "fact_type": {
                            "type": "string",
                            "enum": ["preference", "personal_info", "relationship", "vocabulary", "open_issue"],
                            "description": "סוג העובדה"
                        },
                        "content": {
                            "type": "string",
                            "description": "ניסוח העובדה בעברית, גוף שלישי, עד 15 מילים"
                        },
                        "requires_consent": {
                            "type": "boolean",
                            "description": "True אם העובדה נוגעת למידע רגיש (בריאות/פיננסי/משפחתי/דתי/מיני)"
                        },
                        "confidence": {
                            "type": "number",
                            "description": "ציון ביטחון 0.6-1.0 לפי ה-rubric. ערכים מתחת 0.6 - אל תחזיר את ה-extraction כלל."
                        },
                        "evidence": {
                            "type": "string",
                            "description": "ציטוט קצר או paraphrase צמוד מהשיחה התומך בעובדה"
                        },
                        "supersedes_id": {
                            "type": ["integer", "null"],
                            "description": "ID של existing_fact שהעובדה הזו מחליפה. חובה אם action=supersede, אחרת null."
                        },
                        "confirms_id": {
                            "type": ["integer", "null"],
                            "description": "ID של existing_fact שהעובדה הזו מאשרת. חובה אם action=confirm, אחרת null."
                        }
                    },
                    "required": [
                        "action",
                        "fact_type",
                        "content",
                        "requires_consent",
                        "confidence",
                        "evidence",
                        "supersedes_id",
                        "confirms_id"
                    ],
                    "additionalProperties": False
                }
            },
            "skipped": {
                "type": "array",
                "description": "מועמדים שנשקלו אך לא חולצו, לצרכי דיבאג. רק מקרים גבוליים משמעותיים.",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate": {
                            "type": "string",
                            "description": "תיאור קצר של מה שנשקל לחילוץ"
                        },
                        "reason": {
                            "type": "string",
                            "description": "הסיבה לדילוג - כלל ספציפי משער הקבלה שלא התקיים"
                        }
                    },
                    "required": ["candidate", "reason"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["extractions", "skipped"],
        "additionalProperties": False
    }
}
```

## הערות

- `strict: True` - אוכף את ה-schema. OpenAI לא יחזיר JSON שלא תואם.
- `additionalProperties: False` - מונע מהמודל להוסיף שדות שלא הוגדרו.
- `confidence` מינימום 0.6 לא נכפה בסכמה (JSON Schema לא תומך ב-`minimum` ב-strict mode). זה נאכף ב-post-validation בקוד.
- `nullable` ב-Structured Outputs מיוצג כ-`"type": ["integer", "null"]`.

## דוגמה לשימוש

```python
from openai import OpenAI

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4.1-mini",
    messages=[
        {"role": "system", "content": EXTRACTOR_PROMPT},
        {"role": "user", "content": format_input(business_context, existing_facts, conversation)}
    ],
    response_format={"type": "json_schema", "json_schema": EXTRACTOR_SCHEMA},
    temperature=0.1
)

result = json.loads(response.choices[0].message.content)
```
