# Scorecard ל-Fact Extractor

מסמך זה מגדיר איך מודדים את ביצועי ה-extractor מול ה-eval set.

## עקרונות מנחים

1. **Precision חשוב יותר מ-recall.** במערכת הזו, false positive (לחלץ עובדה לא נכונה) פוגע יותר מ-false negative (לפספס עובדה). עובדה שגויה ברירת מחדל תוזרק לכל שיחה עתידית ותעוות תשובות.

2. **Confidence הוא bucket, לא ערך מדויק.** לא משווים confidence=0.87 ל-confidence=0.89. בודקים שזה נופל בטווח הצפוי (0.85-0.94).

3. **Content נמדד סמנטית, לא לקסיקלית.** "מעדיפה תורים בבוקר" ו"מעדיפה בקרים" - שניהם תקינים. הניסוח לא חייב להיות זהה ל-expected.

## מטריקות פר case

### 1. Extraction Match (השוואה אחת לאחת)

לכל extraction בפלט בפועל, מנסים להתאים אותו ל-extraction ב-expected:

**התאמה נחשבת נכונה אם:**
- `fact_type` זהה
- `action` זהה
- `content` תואם סמנטית ל-`content_semantic` (שיפוט - ידני או LLM-judge)
- `requires_consent` זהה
- `confidence` בתוך ה-`confidence_bucket`
- אם action=confirm: `confirms_id` תואם
- אם action=supersede: `supersedes_id` תואם

**ציון לכל extraction:** 1 אם כל הנ"ל מתקיימים, 0 אחרת.

### 2. False Positives (קריטי)

extraction שיש בפועל אבל אין לו תואם ב-expected.

**משקל:** false positive נספר כפול במטריקות הסיכום כי הוא הכי מסוכן.

### 3. False Negatives

extraction שיש ב-expected אבל אין לו תואם בפועל.

### 4. Skipped Quality

עבור cases מקטגוריית `no_extraction` ו-`edge_cases`:
- האם המודל החזיר extractions ריק? ✓
- האם ה-skipped כולל לפחות `min_skipped_count` פריטים? ✓
- האם ה-reasons הגיוניים? (שיפוט)

## מטריקות אגרגטיביות

### Precision

```
TP / (TP + FP)
```

כאשר:
- TP = extractions שתאמו ל-expected
- FP = extractions שלא תאמו לאף expected

**רף הצלחה:** ≥ 90%

### Recall

```
TP / (TP + FN)
```

כאשר:
- FN = extractions ב-expected שלא תאמו לאף extraction בפועל

**רף הצלחה:** ≥ 70%

### F1

```
2 * (Precision * Recall) / (Precision + Recall)
```

**רף הצלחה:** ≥ 78%

### False Positive Rate בקטגוריית no_extraction

```
מספר שיחות no_extraction שיצרו לפחות extraction אחד / סה"כ שיחות no_extraction
```

**רף הצלחה:** ≤ 5% (כלומר, מתוך 10 שיחות לא צריכות לצאת יותר מ-0 extractions, אבל מאפשרים מקרה גבולי אחד)

### PII Detection Accuracy

```
מספר extractions עם requires_consent נכון / סה"כ extractions בקטגוריית pii_sensitive
```

**רף הצלחה:** 100% (זה pass/fail, אסור לפספס PII)

### Confidence Calibration

לכל extraction נכון, האם ה-confidence נפל ב-bucket הצפוי?

**רף הצלחה:** ≥ 80% מהמקרים בתוך ה-bucket הצפוי.

## טבלת סיכום לדוגמה

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Precision | ≥ 90% | 92% | ✅ |
| Recall | ≥ 70% | 75% | ✅ |
| F1 | ≥ 78% | 82.8% | ✅ |
| FP Rate (no_extraction) | ≤ 5% | 10% | ❌ |
| PII Accuracy | 100% | 100% | ✅ |
| Confidence Calibration | ≥ 80% | 73% | ❌ |

## איך משתמשים בזה

1. **לפני go-live:** מריצים את ה-eval. אם פחות מ-4 מטריקות עוברות → לתקן פרומפט.
2. **אחרי שינוי פרומפט:** משווים לפני/אחרי. אם איזושהי מטריקה ירדה משמעותית - בדיקה ידנית של ה-cases שנשברו.
3. **חודשית:** מוסיפים cases חדשים מהשטח (שיחות אמיתיות עם expected ידני).

## כלי LLM-judge להשוואה סמנטית של content

כדי לא לעשות שיפוט ידני בכל הרצה, אפשר להשתמש ב-LLM-judge:

```
פרומפט: "האם שני המשפטים הבאים מבטאים את אותה משמעות עסקית?
משפט A: {expected_content_semantic}
משפט B: {actual_content}

ענה רק כן/לא."

מודל: gpt-4.1-mini
temperature: 0
```

עלות זניחה (30 cases × ממוצע 1.5 extractions = ~45 קריאות לכל הרצת eval).

## מבנה דוח eval

הסקריפט שיריץ את ה-eval ייצר קובץ markdown:

```
eval_results_{timestamp}.md
- סיכום מטריקות עם status
- פירוט פר case: pass/fail + הפלט בפועל מול הצפוי
- רשימת cases שנכשלו עם הסבר
```
