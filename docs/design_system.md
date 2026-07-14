> מסמך אפיון נאמן למקור (לנדינג Flowly). מתעד את כל ה-tokens, הטיפוגרפיה, הרכיבים והדפוסים הסיגנצ'רים שמרכיבים את האסתטיקה ה-Editorial Warmth.

**גרסה:** 1.0
**תאריך:** אפריל 2026
**שפת המסמך:** עברית · RTL
**שימוש מומלץ:** reference פנימי לפרויקטים בעברית עם אסתטיקה editorial-modern/warm minimalism.

---

## תוכן עניינים

1. [קונספט וחמשת עקרונות הבסיס](#1-קונספט)
2. [פלטת צבעים מלאה](#2-פלטת-צבעים)
3. [טיפוגרפיה — גופנים וסקאלה](#3-טיפוגרפיה)
4. [מרווחים, רדיוסים וצללים](#4-מרווחים-רדיוסים-צללים)
5. [רכיבי ליבה](#5-רכיבי-ליבה)
6. [דפוסי סיגנצ'ר](#6-דפוסי-סיגנצר)
7. [עקרונות אנימציה ואינטראקציה](#7-אנימציה-ואינטראקציה)
8. [לעשות / לא לעשות](#8-לעשות-לא-לעשות)
9. [CSS Tokens — קוד מלא להעתקה](#9-css-tokens)
10. [HTML Imports](#10-html-imports)

---

## 1. קונספט

**הכיוון: Editorial Warmth.** עיצוב שואב מעיתונאות מודפסת איכותית: גופן סריף עברי לכותרות, פלטת קרם-ירוק-חרדל, ושימוש מכוון בנייר ולא בלבן זוהר. המטרה: לשבור את האסתטיקה הגנרית של SaaS ישראלי (סגול-לבן-איקוני-מצוייר) ולתת תחושה של מוצר שנכתב, לא רק נבנה.

### חמשת עקרונות הבסיס

1. **חום מעל ניקיון** — כל רקע "לבן" הוא קרם. כל "שחור" הוא דיו ירקרק. הצבעים נושמים, לא בורחים.
2. **טיפוגרפיה כעוגן ויזואלי** — זוג גופנים בלבד: סריף עברי דרמטי לכותרות (Frank Ruhl Libre), סנס נקי לגוף (Heebo). לא מערבבים, לא מוסיפים.
3. **RTL מלא ואמיתי** — לא תוצאת לוואי, נקודת התחלה. אייקונים, פונטים, רווחים, אלמנטים דקורטיביים — כולם מאופיינים מימין לשמאל.
4. **אקסנטים נדירים** — חרדל ועפר-אדום מופיעים פעם בפסקה, לא כל הזמן. הם מסמנים, לא ממלאים.
5. **טקסטורה ועומק עדין** — שכבת רעש (grain) על כל המסכים, צללים ירוקים-יער (לא אפור גנרי), פינות מעוגלות אחידות (16/20/32px).

---

## 2. פלטת צבעים

### רקעים ונייר

| משתנה | HEX | שימוש |
|---|---|---|
| `--cream` | `#f5f0e6` | רקע ראשי של הדף |
| `--cream-soft` | `#faf6ec` | רקעים משניים, mockup chrome |
| `--paper` | `#fbf8f1` | רקעי כרטיסים, blocks |

### דיו וטקסט

| משתנה | HEX | שימוש |
|---|---|---|
| `--ink` | `#16211b` | טקסט ראשי (כותרות, body bold) |
| `--ink-soft` | `#2c3a32` | גוף טקסט |
| `--muted` | `#6b7a72` | טקסט משני, captions, metadata |

### צבעי מותג

| משתנה | HEX | שימוש |
|---|---|---|
| `--forest-deep` | `#143024` | מותג ראשי (CTAs, headers, dark sections) |
| `--forest` | `#1f4a35` | מותג רגיל (links, highlights) |
| `--forest-light` | `#2d6b4e` | מותג בהיר (hover, accents) |
| `--mustard` | `#d4942b` | אקסנט מרכזי (highlights, badges) |
| `--mustard-soft` | `#e8b657` | אקסנט בהיר (icons על רקע כהה) |
| `--clay` | `#c85a3c` | אקסנט נדיר ביותר (warnings, special CTAs) |

### גבולות

| משתנה | HEX | שימוש |
|---|---|---|
| `--border` | `#d9d1bf` | גבולות ברורים, dividers |
| `--border-soft` | `#e8e0cd` | גבולות עדינים, subtle dividers |

### סטטוסים

| משתנה | HEX | שימוש |
|---|---|---|
| `--status-green-bg` | `#dcece0` | רקע סטטוס חיובי |
| `--status-green-fg` | `#1f4a35` | טקסט סטטוס חיובי |
| `--status-yellow-bg` | `#faecc8` | רקע סטטוס המתנה/אזהרה |
| `--status-yellow-fg` | `#8a6518` | טקסט סטטוס המתנה/אזהרה |
| `--status-red-bg` | `#f5d8cf` | רקע סטטוס שגיאה/דחוף |
| `--status-red-fg` | `#a63b1f` | טקסט סטטוס שגיאה/דחוף |

---

## 3. טיפוגרפיה

### גופנים — שניים בלבד

- **Frank Ruhl Libre** — סריף עברי דרמטי, לכותרות וטקסט בולט בלבד. משקלים: 400, 500, 700, 900.
- **Heebo** — סנס-סריף נקי, לכל גוף הטקסט וה-UI. משקלים: 300, 400, 500, 700, 900.

> **כלל ברזל:** לא להוסיף גופן שלישי. גם לא לעטרים. הזיווג הזה הוא מה שיוצר את החתימה.

### סקאלת טיפוגרפיה

| שם | גופן | גודל | line-height | letter-spacing | שימוש |
|---|---|---|---|---|---|
| Display 1 | Frank Ruhl Libre 700 | `clamp(40px, 6vw, 72px)` | 1.05 | -0.03em | כותרת hero של עמוד |
| Display 2 | Frank Ruhl Libre 700 | `clamp(32px, 4.5vw, 52px)` | 1.1 | -0.02em | כותרת חלוקה (H2) |
| Heading 3 | Frank Ruhl Libre 700 | 26px | 1.2 | -0.01em | כותרת רכיב/כרטיס |
| Heading 4 | Frank Ruhl Libre 700 | 22px | 1.25 | normal | כותרת mockup/sub-card |
| Body Large | Heebo 400 | 19px | 1.55 | normal | פסקת פתיחה, sub-headers |
| Body Base | Heebo 400 | 16px | 1.6–1.65 | normal | טקסט גוף ברירת מחדל |
| Body Small | Heebo 400 | 14px | 1.5 | normal | metadata, captions |
| Label | Heebo 500 | 13px | 1 | 0.2em | uppercase tags, section markers |
| Mono Display | Frank Ruhl Libre 900 | 64–80px | 1 | normal | מספרי פיצ'רים דקורטיביים (`01`, `02`...) — בצבע border |

### כללי כותרות

- כל H1–H4 ב-Frank Ruhl Libre עם `font-weight: 700`
- כותרות ראשיות (H1, H2) משתמשות ב-`clamp()` רספונסיבי
- כותרות ב-blocks כהים מקבלות `color: var(--cream)`
- כותרות ב-blocks בהירים מקבלות `color: var(--ink)` (כברירת מחדל) או `color: var(--forest-deep)` (בכרטיסי תוכן)

---

## 4. מרווחים, רדיוסים, צללים

### סקאלת מרווחים

| שם | ערך | שימוש מומלץ |
|---|---|---|
| `--sp-xs` | 4px | הצמדה בין tag לאייקון |
| `--sp-sm` | 8px | בין שורות בתוך כרטיס |
| `--sp-md` | 16px | padding ברכיבים קטנים, gap בכרטיסים סמוכים |
| `--sp-lg` | 24px | padding בכרטיס, gap בגריד |
| `--sp-xl` | 32px | padding בכרטיס גדול, גובה בלוקים |
| `--sp-2xl` | 48px | בין סקציות פנימיות |
| `--sp-3xl` | 80px | בין סקציות עיקריות |
| `--sp-4xl` | 100px | סקציות hero, padding דרמטי |

### רדיוסים

| שם | ערך | שימוש |
|---|---|---|
| `--r-sm` | 8–10px | inputs, tags, פינות פנימיות |
| `--r-md` | 12–14px | כרטיסים פנימיים |
| `--r-lg` | 16–20px | כרטיסי תוכן, mockups |
| `--r-xl` | 32px | בלוקי hero/CTA גדולים |
| `--r-full` | 999px | כפתורים, pills, avatars |

### צללים

כל הצללים שלנו **forest-tinted** — לא אפור גנרי. זה חלק מהטון.

| שם | ערך | שימוש |
|---|---|---|
| `--sh-sm` | `0 2px 4px rgba(20, 48, 36, 0.04)` | כרטיסים שטוחים, subtle |
| `--sh-md` | `0 8px 24px rgba(20, 48, 36, 0.08)` | כרטיסי hover, elevated cards |
| `--sh-lg` | `0 20px 50px rgba(20, 48, 36, 0.15)` | mockups, modals |
| `--sh-xl` | `0 40px 80px rgba(20, 48, 36, 0.1)` | hero mockups, dramatic depth |
| `--sh-cta` | `0 8px 24px rgba(20, 48, 36, 0.25)` | כפתורי CTA primary (עוצמה) |

> **חשוב:** כפתור CTA לא מקבל `--sh-md` הרגיל. הצל שלו עז יותר (alpha 0.25) כי הוא צריך להזמין לחיצה.

---

## 5. רכיבי ליבה

### כפתורים

```
padding: 16px 28px
border-radius: 999px (full)
font-size: 16px
font-weight: 500
font-family: Heebo
gap: 10px (אם יש אייקון/חץ)
```

**גרסאות:**

- **Primary** — `background: --forest-deep; color: --cream; box-shadow: --sh-cta`. Hover: `background: --forest; transform: translateY(-2px); box-shadow: עוצמתי יותר (alpha 0.3)`.
- **Ghost** — `background: transparent; color: --ink; border: 1px solid --border`. Hover: `background: --paper`.
- **Mustard** (על רקע כהה) — `background: --mustard; color: --forest-deep`. Hover: `background: --mustard-soft`.

> **כלל:** Nav-CTA קטן יותר מ-CTA רגיל (padding `10px 22px`). שאר הכפתורים אחידים.

### תגיות סטטוס

```
padding: 4px 12px
border-radius: 999px
font-size: 11px
font-weight: 500
```

3 וריאציות עם זוגות bg+fg מהטבלת סטטוסים. **תמיד** משתמשים ב-bg+fg יחד, לא בודדים.

### כרטיס בסיסי

```
background: #fff
border: 1px solid var(--border-soft)
border-radius: 16px
padding: 24px
```

### כרטיס פיצ'ר עם מספר דקורטיבי

```
background: var(--paper)
border: 1px solid var(--border-soft)
border-radius: 20px
padding: 32–36px
position: relative
```

המספר (`01`, `02`...) ממוקם `position: absolute` בפינה השמאלית-עליונה (`top: 18–20px; left: 22–24px`), בגופן Frank Ruhl Libre 64–80px, weight 900, צבע `--border` (כדי שיהיה דקורטיבי-עדין).

האייקון מעל הכותרת: `width/height: 44–48px; border-radius: 12px; background: --forest-deep; color: --mustard-soft`.

### כותרת סקציה (Section Tag)

```html
<span class="section-tag">קטגוריה</span>
```

```css
font-size: 13px
text-transform: uppercase
letter-spacing: 0.2em
color: var(--forest)
font-weight: 500
```

מקבל `::before` עם נקודה חרדל בצבע `--mustard`.

### Logo Mark — סיגנצ'ר

עיגול forest עם טבעת חרדל מסתובבת:

```html
<span class="logo-mark"></span>
```

```css
width: 28px; height: 28px
background: var(--forest)
border-radius: 50%
position: relative

.logo-mark::after {
  content: '';
  position: absolute;
  inset: 6px;
  border: 2px solid var(--mustard);
  border-radius: 50%;
  border-top-color: transparent;
  border-left-color: transparent;
}
```

### Hero Tag (badge קטן עם נקודה פועמת)

```css
display: inline-flex
gap: 8px
background: var(--paper)
border: 1px solid var(--border)
padding: 8px 16px
border-radius: 999px
font-size: 13px
```

הנקודה: `width: 6px; height: 6px; background: --mustard; box-shadow: 0 0 0 3px rgba(212, 148, 43, 0.2)` עם animation pulse 2s.

### FAQ Accordion

```
padding: 24px 0
border-bottom: 1px solid var(--border)
cursor: pointer
```

הכותרת: Frank Ruhl Libre 20px 700. הכפתור `+`: עיגול 28×28 forest-deep עם cream `+` שמסתובב 45° ב-`.open` (הופך ל-`×`). ה-answer נפתח עם `max-height` transition 0.4s.

### Pricing Card עם Featured

הכרטיס המודגש (`.featured`) שונה מהשניים האחרים:

```
background: var(--forest-deep)
color: var(--cream)
transform: scale(1.03)
```

ה-CTA שלו הופך מ-forest ל-mustard (`background: --mustard; color: --forest-deep`) — היפוך טון מכוון לדגש.

ה-badge "הכי פופולרי" ממוקם `position: absolute; top: -12px; right: 50%; transform: translateX(50%)` עם רקע mustard וטקסט forest-deep.

### Final CTA Block

בלוק CTA סוגר הוא דפוס עצמאי:

```
background: var(--forest-deep)
color: var(--cream)
padding: 100px 40px
border-radius: 32px
margin: 60px 24px
position: relative
overflow: hidden
```

עם `::before` שזה **הילה רדיאלית חרדל** בפינה:

```css
.final-cta::before {
  content: '';
  position: absolute;
  top: -40%;
  right: -10%;
  width: 500px; height: 500px;
  background: radial-gradient(circle, var(--mustard) 0%, transparent 60%);
  opacity: 0.2;
}
```

### Mockup Window

חלון "browser" עם 3 נקודות:

```
.mockup {
  background: #fff;
  border-radius: 18px;
  border: 1px solid var(--border-soft);
  box-shadow: --sh-md, --sh-lg, --sh-xl (שלושה צללים מצטברים)
  transform: perspective(1200px) rotateY(-3deg) rotateX(2deg);
  transition: transform 0.5s;
}
.mockup:hover { transform: perspective(1200px) rotateY(0) rotateX(0); }
```

ה-bar העליון: `padding: 12px 16px; background: var(--cream-soft); border-bottom: 1px solid var(--border-soft)`. שלוש נקודות 11×11 בצבעים `#e06c5a` / `#e8b657` / `#6ca87a` (אדום-צהוב-ירוק "macOS").

### Avatar Gradients

avatars בעיגול 32–44px עם linear-gradient של זוגות צבעי המותג:

```css
.avatar-1 { background: linear-gradient(135deg, #d4942b, #e8b657); } /* mustard */
.avatar-2 { background: linear-gradient(135deg, #1f4a35, #2d6b4e); } /* forest */
.avatar-3 { background: linear-gradient(135deg, #c85a3c, #e58b6e); } /* clay */
.avatar-4 { background: linear-gradient(135deg, #16211b, #2c3a32); } /* ink */
```

החריג היחיד למדיניות "אין גרדיאנטים" — קישוטיים בלבד.

### Sticky Nav עם Backdrop Blur

```css
nav {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(245, 240, 230, 0.85); /* cream שקוף */
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-soft);
}
```

---

## 6. דפוסי סיגנצ'ר

### 6.1 ההדגשה החרדל בכותרת — *הסיגנצ'ר הוויזואלי המרכזי*

הדפוס שיוצר את הזהות. שימוש: עוטפים מילה ב-`<em>` בכותרת.

```html
<h1>כל העסק שלך במקום אחד. <em>סוף סוף.</em></h1>
```

```css
h1 em {
  font-style: italic;
  color: var(--forest);
  position: relative;
  isolation: isolate; /* חובה — אחרת ה-::after ייעלם מאחורי ancestors */
}
h1 em::after {
  content: '';
  position: absolute;
  bottom: 4–6px;
  right: -3px;
  left: -3px;
  height: 8–10px;
  background: var(--mustard);
  opacity: 0.35;
  z-index: -1;
  border-radius: 3–4px;
}
```

> **תקלה ידועה:** `position: relative` בלבד לא יוצר stacking context. אם ה-em יושב בתוך `.card` או כל ancestor אטום, ה-z-index השלילי יברח החוצה והפס יחבא מאחורי ה-card. **חובה** `isolation: isolate`.

### 6.2 שכבת Grain (רעש מודפס)

על כל הדף, fixed:

```css
body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 9998;
  opacity: 0.3–0.35;
  mix-blend-mode: multiply;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' /%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.4'/%3E%3C/svg%3E");
}
```

**z-index 9998** — מתחת ל-modals (9999) אבל מעל הכל אחר. **mix-blend-mode: multiply** קריטי — בלעדיו זה ייראה כשכבת lint מלוכלכת ולא כנייר.

### 6.3 כרטיסים בזווית עדינה (Tilted Cards)

בסקציות "כאוס" / "בעיה" — כרטיסים מקבלים rotation עדין:

```css
.chaos-item:nth-child(1) { transform: rotate(-1deg); }
.chaos-item:nth-child(2) { transform: rotate(1deg); }
.chaos-item:nth-child(3) { transform: rotate(-0.5deg); }
.chaos-item:nth-child(4) { transform: rotate(1.5deg); }

.chaos-item:hover {
  transform: rotate(0) scale(1.02);
  transition: transform 0.3s;
}
```

זווית בין `±0.5°` ל-`±1.5°`. יותר מזה הופך לבלגן.

### 6.4 Mockup 3D Tilt

```css
.mockup {
  transform: perspective(1200px) rotateY(-3deg) rotateX(2deg);
  transition: transform 0.5s;
}
.mockup:hover {
  transform: perspective(1200px) rotateY(0) rotateX(0);
}
```

תמיד RTL: `rotateY` שלילי (סיבוב לימין). זה תואם לתחושת "המוצר מציג את עצמו אליך מימין לשמאל".

### 6.5 Pulse Animation

לכל נקודת סטטוס פעילה:

```css
@keyframes pulse-soft {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.55; }
}
.status-dot.active {
  animation: pulse-soft 2.4s ease-in-out infinite;
}
```

> **חשוב:** עוטפים ב-`@media (prefers-reduced-motion: reduce) { animation: none; }` — חובת נגישות.

### 6.6 Scroll Reveal

```css
.reveal {
  opacity: 0;
  transform: translateY(24px);
  transition: opacity 0.8s ease, transform 0.8s ease;
}
.reveal.in {
  opacity: 1;
  transform: translateY(0);
}
```

```javascript
const io = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.classList.add('in');
      io.unobserve(e.target);
    }
  });
}, { threshold: 0.12 });

document.querySelectorAll('.reveal').forEach(el => io.observe(el));
```

threshold 0.12 — מתחיל מוקדם, לפני שהאלמנט שלם בוויופורט. נותן תחושה זורמת.

### 6.7 Logos Strip (רצועת לוגואים)

לסקציית "לקוחות שלנו". 4 לוגואים טיפוגרפיים מעורבים — לא תמונות:

```html
<span class="fake-logo">Mirkam</span>
<span class="fake-logo italic">Aleph.studio</span>
<span class="fake-logo bold">KARMEL+</span>
<span class="fake-logo mono">NOVA / 22</span>
```

```css
.logos-wrap { opacity: 0.6; gap: 60px; }
.fake-logo { font-family: 'Frank Ruhl Libre'; font-size: 22px; font-weight: 700; }
.fake-logo.italic { font-style: italic; }
.fake-logo.bold { font-weight: 900; }
.fake-logo.mono { font-family: 'Heebo'; font-weight: 900; letter-spacing: 0.1em; }
```

opacity 0.6 קריטי — הלוגואים מוצגים כ"ראיה היקפית", לא כתוכן ראשי.

---

## 7. אנימציה ואינטראקציה

### עקרונות

- **Hover על כפתורים** — `translateY(-1px)` עד `translateY(-2px)`, **לא** scale.
- **Hover על כרטיסים** — `translateY(-3px)` עד `translateY(-4px)`, עם `box-shadow` שמתעצם.
- **Hover על mockups** — שחזור rotation (3D tilt → flat).
- **Transitions** — 0.2s לאינטראקציות מיידיות (hover/focus), 0.3s לכרטיסים, 0.5s ל-mockups, 0.8s ל-reveal.
- **Easing** — `ease` לרוב, `ease-in-out` ל-pulse, `ease-out` ל-swap.
- **Reduced motion** — תמיד מחויב למבטל animations.

### Color transitions

```css
transition: color 0.2s, background 0.2s, border-color 0.2s;
```

לא `transition: all` — רק תכונות ספציפיות. מונע flicker על font-rendering.

---

## 8. לעשות / לא לעשות

### ✓ לעשות

- להשתמש ב-cream כברירת מחדל לרקע, לא בלבן
- לזווג רק Frank Ruhl Libre עם Heebo
- לשמור על mustard ו-clay כאקסנטים נדירים
- להוסיף שכבת grain על כל הדף
- פינות מעוגלות בקפיצות של 4px (8/12/16/20/32)
- RTL מלא — אייקונים, חצים, צללים
- `isolation: isolate` על כל אלמנט עם `::after`/`::before` ב-z-index שלילי

### ✗ לא לעשות

- אין סגול, ורוד או גרדיאנטים סגולים-כחולים
- אין גופן שלישי
- אין mustard בכל קומפוננטה — הופך רעשני
- אין emojis צבעוניים כאייקונים — להשתמש בסמלים מינימליסטיים (◎, ⊞, ₪, ♡)
- אין `#fff` צרוף כרקע עמוד
- אין `transition: all` — רק תכונות ספציפיות
- אין צללי Tailwind גנריים (אפורים) — הצללים שלנו ירוקים-יער

---

## 9. CSS Tokens

```css
/* === FLOWLY DESIGN TOKENS === */
:root {
  /* backgrounds */
  --cream:        #f5f0e6;
  --cream-soft:   #faf6ec;
  --paper:        #fbf8f1;

  /* ink */
  --ink:          #16211b;
  --ink-soft:     #2c3a32;
  --muted:        #6b7a72;

  /* brand */
  --forest:       #1f4a35;
  --forest-deep:  #143024;
  --forest-light: #2d6b4e;
  --mustard:      #d4942b;
  --mustard-soft: #e8b657;
  --clay:         #c85a3c;

  /* lines */
  --border:       #d9d1bf;
  --border-soft:  #e8e0cd;

  /* status */
  --status-green-bg:   #dcece0;
  --status-green-fg:   #1f4a35;
  --status-yellow-bg:  #faecc8;
  --status-yellow-fg:  #8a6518;
  --status-red-bg:     #f5d8cf;
  --status-red-fg:     #a63b1f;

  /* typography */
  --font-display: 'Frank Ruhl Libre', serif;
  --font-body:    'Heebo', sans-serif;

  /* spacing */
  --sp-xs:  4px;
  --sp-sm:  8px;
  --sp-md:  16px;
  --sp-lg:  24px;
  --sp-xl:  32px;
  --sp-2xl: 48px;
  --sp-3xl: 80px;
  --sp-4xl: 100px;

  /* radius */
  --r-sm:   10px;
  --r-md:   14px;
  --r-lg:   20px;
  --r-xl:   32px;
  --r-full: 999px;

  /* shadows (forest-tinted) */
  --sh-sm:  0 2px 4px rgba(20, 48, 36, 0.04);
  --sh-md:  0 8px 24px rgba(20, 48, 36, 0.08);
  --sh-lg:  0 20px 50px rgba(20, 48, 36, 0.15);
  --sh-xl:  0 40px 80px rgba(20, 48, 36, 0.1);
  --sh-cta: 0 8px 24px rgba(20, 48, 36, 0.25);

  /* transitions */
  --t-fast:   0.2s ease;
  --t-base:   0.3s ease;
  --t-slow:   0.5s ease;
  --t-reveal: 0.8s ease;
}

/* === BODY DEFAULT === */
body {
  font-family: var(--font-body);
  background: var(--cream);
  color: var(--ink);
  line-height: 1.6;
  direction: rtl;
}

/* === GRAIN OVERLAY (חובה לאסתטיקה) === */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 9998;
  opacity: 0.35;
  mix-blend-mode: multiply;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' /%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.4'/%3E%3C/svg%3E");
}

/* === HEADINGS === */
h1, h2, h3, h4 {
  font-family: var(--font-display);
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--ink);
}

/* === EM HIGHLIGHT (signature) === */
h1 em, h2 em {
  font-style: italic;
  color: var(--forest);
  position: relative;
  isolation: isolate;
}
h1 em::after, h2 em::after {
  content: '';
  position: absolute;
  bottom: 4px;
  right: -3px;
  left: -3px;
  height: 8px;
  background: var(--mustard);
  opacity: 0.35;
  z-index: -1;
  border-radius: 3px;
}

/* === REDUCED MOTION === */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## 10. HTML Imports

```html
<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Frank+Ruhl+Libre:wght@400;500;700;900&family=Heebo:wght@300;400;500;700;900&display=swap" rel="stylesheet">

  <link rel="stylesheet" href="design-tokens.css">
</head>
<body>
  <!-- תוכן -->
</body>
</html>
```
