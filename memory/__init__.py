"""
Customer Memory System — מערכת זיכרון מתמשך פר-לקוח.

חבילה זו מכילה את כל שלבי המערכת:
- extractor.py (שלב 3) — LLM שמחלץ עובדות יציבות משיחה.
- validator.py (שלב 4) — ולידציה ושמירה ל-DB.
- background.py (שלב 6) — scheduler לזיהוי שיחות שהסתיימו.
- context.py (שלב 8) — שליפת facts לבוט.
- prompts/, schemas/, eval/ — נכסים סטטיים וכלי הערכה.

ראה docs/Customer-memory/claude_code_instructions.md למפרט מלא.
תשתית DB ו-CRUD ב-database.py (שלב 1).
"""
