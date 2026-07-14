> מסמך אפיון עיצוב לפאנל הניהול במצב Dark — נאמן למקור שב-`admin/static/css/style.css` ו-`admin/templates/base.html`. כל ה-tokens, הרכיבים והדפוסים שבמסמך הזה ממומשים בקוד; אין כאן הצעות עתידיות.

**גרסה:** 1.0
**תאריך:** מאי 2026
**שפת המסמך:** עברית · RTL
**Scope:** ערכת נושא ברירת המחדל של הפאנל (`:root`, ללא `data-theme`).
**לא כולל:** ערכות `light`, `light-warm`, `light-kibbutz` (מתועדות חלקית ב-`docs/design_system.md`).

---

## תוכן עניינים

1. [קונספט וחמשת עקרונות הבסיס](#1-קונספט)
2. [פלטת צבעים מלאה](#2-פלטת-צבעים)
3. [טיפוגרפיה — גופנים, משקלים, סקאלה](#3-טיפוגרפיה)
4. [מרווחים, רדיוסים, צללים, layout](#4-מרווחים-רדיוסים-צללים-layout)
5. [רכיבי ליבה](#5-רכיבי-ליבה)
6. [דפוסי סיגנצ'ר של Dark](#6-דפוסי-סיגנצר)
7. [אנימציה ואינטראקציה](#7-אנימציה-ואינטראקציה)
8. [נגישות וריספונסיביות](#8-נגישות-וריספונסיביות)
9. [לעשות / לא לעשות](#9-לעשות-לא-לעשות)
10. [CSS Tokens — קוד מלא להעתקה](#10-css-tokens)

---

## 1. קונספט

**הכיוון: SaaS Slate-Blue Modern.** ערכת נושא כהה אך לא שחורה, מבוססת `slate-900/950/800` של Tailwind (TypeScale רוחב). חתימת המותג היא **גרדיאנט כחול→סגול** (`#2563EB → #7C3AED`) שמופיע בכל נקודות העניין: לוגו, פריט סיידבר פעיל, כפתור ראשי, כותרות עם clip-text, login icon. כל היתר נשאר נייטרלי כדי שהגרדיאנט יבלוט.

### חמשת עקרונות הבסיס

1. **כהה, לא שחור** — אין `#000`. הרקע הכי כהה הוא `#020617` (slate-950) שמשמש סיידבר ושדות `form-input`. הדף עצמו על `#0F172A` (slate-900). הניגודיות מגיעה מהכרטיסים, לא מהרקע.
2. **גרדיאנט = אקסנט, לא רקע** — כחול→סגול שמור לרכיבים אקטיביים בלבד (active nav, primary buttons, brand icon, page-header `<h1> <i>`). שאר הרכיבים `solid` עם גוון שקוף עדין (alpha 0.05–0.15) כדי לא להתחרות.
3. **RTL מלא** — `dir="rtl"` על `<html>`, סיידבר ב-`right: 0`, `border-left` על הסיידבר, `border-right` על list-group active. כל הרווחים, האייקונים והפריסות מותאמים מימין לשמאל מלכתחילה.
4. **שני גופני סנס בלבד** — `Rubik` לכל ה-body, `Assistant` לכותרות מותג. אין גופן סריף ב-Dark (Frank Ruhl Libre נטען אבל בשימוש רק ב-`light-warm`).
5. **צללים עם זוהר** — שלוש רמות צל שחור (`shadow-sm/md/lg`) ועוד `shadow-glow` ייעודי בכחול המותג (`rgba(37, 99, 235, 0.3)`) שמשמש את אייקון הכניסה ואת הילת ה-focus.

---

## 2. פלטת צבעים

כל הערכים מוגדרים ב-`admin/static/css/style.css:7-69` תחת `:root`.

### רקעים

| משתנה | HEX | שימוש |
|---|---|---|
| `--bg-dark` | `#0F172A` | רקע ראשי של הדף (`body`) |
| `--bg-darker` | `#020617` | סיידבר, scrollbar track, רקע `form-input` ו-`live-chat-input` |
| `--bg-card` | `#1E293B` | רקע כרטיסים, hover על סיידבר nav, רקע `mobile table tr` |
| `--bg-input` | `#f5f0e8` | רקע `form-control` (קרם — מכוון; הקלט בולט מול הרקע הכהה) |
| `--bg-topbar` | `rgba(15, 23, 42, 0.95)` | top-bar במובייל עם `backdrop-filter: blur(10px)` |

> **הערה ייחודית ל-Dark:** `--bg-input` הוא קרם בהיר ולא slate. הכתיבה מתבצעת על נייר בהיר גם בערכת Dark. שדות שמוגדרים `.form-input` (לא `.form-control`) משתמשים ב-`--bg-darker` — שני סוגי שדות עם שני רקעים שונים.

### טקסט

| משתנה | HEX | שימוש |
|---|---|---|
| `--text-primary` | `#F8FAFC` | טקסט ראשי, כותרות, פרטי שולח בהודעות |
| `--text-secondary` | `#CBD5E1` | טקסט גוף, תאי טבלה, קישורי סיידבר |
| `--text-muted` | `#94A3B8` | metadata, captions, placeholders של `.form-input`, אייקוני סטטוס |
| `--text-input` | `#1a1a1a` | טקסט בתוך `.form-control` (על רקע קרם) |
| `--text-input-placeholder` | `#999` | placeholder ב-`.form-control` |

### מותג

| משתנה | HEX | שימוש |
|---|---|---|
| `--brand-blue` | `#2563EB` | קישורים (`<a>`), focus outline, sortable active, `list-group-item.active` border |
| `--brand-blue-light` | `#60a5fa` | טקסט `.badge-primary` |
| `--brand-purple` | `#7C3AED` | hover על קישורים, סוף הגרדיאנט הראשי |
| `--brand-purple-light` | `#a78bfa` | טקסט `.badge-category` |

### סטטוסים

| משתנה | HEX | שימוש |
|---|---|---|
| `--success` | `#10B981` | `alert-success`, `badge-success`, `text-success`, `status-dot.status-active` |
| `--warning` | `#F59E0B` | `alert-warning`, `badge-warning`, `text-warning`, RAG stale, `status-dot.status-warning` |
| `--danger` | `#EF4444` | `alert-danger`, `badge-danger`, `btn-danger`, `nav-badge`, logout link |
| `--info` | `#3B82F6` | `alert-info`, `badge-info` |

### גבולות

| משתנה | HEX | שימוש |
|---|---|---|
| `--border` | `#334155` | כל הגבולות, dividers, scrollbar thumb, footers |

### גרדיאנטים

| משתנה | ערך | שימוש |
|---|---|---|
| `--gradient-primary` | `linear-gradient(135deg, #2563EB 0%, #7C3AED 100%)` | **חתימה.** brand-icon, `sidebar-nav a.active`, `btn-primary`, `login-icon`, `filter-pill.active`, `page-header h1 i` (clip-text), `stat-card.gradient-blue` |
| `--gradient-hero` | `linear-gradient(180deg, #0F172A 0%, #1E293B 100%)` | רקע דף ה-login |
| `--gradient-success` | `linear-gradient(135deg, #10B981 0%, #059669 100%)` | `btn-success`, `stat-card.gradient-success` |
| `--gradient-warning` | `linear-gradient(135deg, #F59E0B 0%, #D97706 100%)` | `btn-warning`, `stat-card.gradient-warning` |
| `--gradient-info` | `linear-gradient(135deg, #3B82F6 0%, #2563EB 100%)` | `stat-card.gradient-info` |
| `--gradient-purple` | `linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%)` | `stat-card.gradient-purple` |
| `--gradient-dark` | `linear-gradient(135deg, #475569 0%, #334155 100%)` | `stat-card.gradient-dark` (סטטיסטיקות עם משמעות נטרלית) |

### צללים

| משתנה | ערך | שימוש |
|---|---|---|
| `--shadow-sm` | `0 2px 8px rgba(0, 0, 0, 0.1)` | כרטיסי טבלה במובייל |
| `--shadow-md` | `0 4px 16px rgba(0, 0, 0, 0.2)` | `sidebar-nav a.active`, hover על כפתורים |
| `--shadow-lg` | `0 8px 32px rgba(0, 0, 0, 0.3)` | hover על stat-card, login-card, סיידבר פתוח במובייל |
| `--shadow-glow` | `0 0 30px rgba(37, 99, 235, 0.3)` | login-icon (הילה כחולה ייעודית) |

> **חשוב:** הצללים שחורים, לא תינטד. השוני מ-`light-warm` הוא מכוון — חום הוא דיאלקט, ב-Dark העומק נוצר מהבדלי lightness, לא מגוון.

---

## 3. טיפוגרפיה

### גופנים

נטענים מ-Google Fonts ב-`base.html:27`:

| גופן | משקלים | שימוש ב-Dark |
|---|---|---|
| **Rubik** (סנס) | 300, 400, 500, 600, 700 | `body` (default), `.btn`, `.form-input`, `.form-control` |
| **Assistant** (סנס) | 700, 800 | `.sidebar-brand .brand-name`, `.mobile-topbar .brand-name` |
| Frank Ruhl Libre | 500, 700, 900 | **לא בשימוש ב-Dark.** ייעודי ל-`light-warm`. נטען בכל מקרה. |
| Heebo | 300–700 | **לא בשימוש ב-Dark.** ייעודי ל-`light-warm`. נטען בכל מקרה. |

### Body Text

```css
font-family: 'Rubik', sans-serif;
font-weight: 400;
line-height: 1.6;
font-size: 16px;  /* html */
color: var(--text-primary);
-webkit-font-smoothing: antialiased;
-moz-osx-font-smoothing: grayscale;
```

### סקאלה

| רכיב | font-size | font-weight | line-height |
|---|---|---|---|
| `html` | `16px` | — | — |
| `body` | `1rem` (16px) | 400 | 1.6 |
| `.page-header h1` | `1.75rem` (28px) | 700 | — |
| `.sidebar-brand .brand-name` | `1.1rem` (≈18px) | 700 | — |
| `.login-title` | `1.5rem` (24px) | 700 | — |
| `.stat-value` | `1.5rem` | 700 | 1.2 |
| `.stat-label` | `0.8rem` | — | — |
| `.btn` | `0.875rem` | 600 | 1.4 |
| `.btn-sm` | `0.8rem` | 600 | — |
| `.form-label` | `0.875rem` | 600 | — |
| `.form-input/.form-control` | `0.9375rem` | 400 | 1.5 |
| `.form-control-sm` | `0.8125rem` | 400 | — |
| `.message-content` | `0.9375rem` | 400 | 1.6 |
| `.badge` | `0.75rem` | 600 | — |
| `.badge-primary` | `0.8rem` | 600 | — |
| `.alert` | `0.9rem` | — | — |
| `table th` | `0.8rem` | 600 (UPPERCASE, letter-spacing 0.05em) | — |
| `table td` | `0.875rem` | — | — |
| `.text-small` | `0.8rem` | — | — |
| `.form-hint` | `0.8rem` | — | — |
| `.empty-state i` | `3rem` | — | — |

### Mobile (≤768px)

```css
html { font-size: 14px; }
.page-header h1 { font-size: 1.35rem; }
```

---

## 4. מרווחים, רדיוסים, צללים, layout

### מרווחים (spacing tokens)

| משתנה | ערך | שימוש |
|---|---|---|
| `--space-xs` | `0.5rem` | `.gap-sm`, `.actions-cell` |
| `--space-sm` | `1rem` | פדינג בסיידבר nav, gap בין alerts, `mb-1` |
| `--space-md` | `2rem` | פדינג של main-content, `margin-bottom` של `.page-header`, `.stats-grid` |
| `--space-lg` | `4rem` | זמין אך כמעט לא בשימוש |

### רדיוסים

| משתנה | ערך | שימוש |
|---|---|---|
| `--radius-sm` | `8px` | brand-icon, sidebar-nav a, theme-toggle, btn-icon, form-input, list-group, popover textarea |
| `--radius-md` | `12px` | btn (default), alert, message-bubble, login-icon, mobile table tr |
| `--radius-lg` | `16px` | card, stat-card, status-bar, login-card |
| Pill | `20px` (קבוע, לא token) | `.badge`, `.filter-pill` |
| Circle | `50%` | `.status-dot`, spinner |

### Layout — מבנה הפאנל

```css
--sidebar-width: 260px;
--topbar-height: 56px;
```

**מבנה (`base.html:48-288`):**

```
┌─────────────────────────────────────────────────────────────┐
│ mobile-topbar (56px, mobile-only, blur backdrop)            │
├─────────────────────────────────────┬───────────────────────┤
│                                     │                       │
│         main-content                │      sidebar          │
│   (margin-right: 260px)             │   (260px, fixed,      │
│   padding: 2rem                     │    bg-darker,         │
│                                     │    right: 0,          │
│   - flash messages                  │    border-left)       │
│   - rag-stale-banner                │                       │
│   - grace-banner                    │   - sidebar-brand     │
│   - {% block content %}             │   - sidebar-nav (ul)  │
│                                     │   - sidebar-footer    │
│                                     │     (theme-toggle +   │
│                                     │      logout)          │
└─────────────────────────────────────┴───────────────────────┘
```

הסיידבר תמיד `position: fixed` ב-`right: 0`. `main-content` מקבל `margin-right: 260px` כדי לפנות לו מקום. ב-mobile (≤768px) הסיידבר נכבה ל-`transform: translateX(100%)` ונפתח עם class `.open`.

### Transition

```css
--transition: all 0.3s ease;
```

מוחל על: כל קישור, כל כפתור, כל `.btn`, `.card`, `.filter-pill`, `.list-group-item`, `.user-note-trigger`, `.theme-toggle`, ועוד.

---

## 5. רכיבי ליבה

### Sidebar

* רוחב 260px, `position: fixed; right: 0; height: 100vh`.
* רקע `--bg-darker` (`#020617`), `border-left: 1px solid var(--border)`.
* `overflow-y: auto`, scrollbar custom (6px, ראה §6).

**Brand block:**
* פדינג 1.5rem, גבול תחתון 1px.
* `brand-icon`: 40×40, `radius-sm`, רקע `--gradient-primary`, אייקון לבן.
* `brand-name`: Assistant 700, 1.1rem, `text-overflow: ellipsis`.

**Sidebar nav:**
* `<ul>` ללא bullets, פדינג 1rem.
* `<a>`: `display: flex; gap: 0.75rem; padding: 0.75rem 1rem; radius-sm`.
* `font-weight: 500`, צבע `--text-secondary`.
* **Hover:** `background-color: var(--bg-card)`, `color: var(--text-primary)`.
* **Active:** `background: var(--gradient-primary)`, `color: white`, `box-shadow: var(--shadow-md)`.
* **Locked feature** (`.locked-feature`): `opacity: 0.55`, רקע שקוף, אייקון נעילה ב-`margin-right: auto`.

**Nav badge** (התראה אדומה למעלה-שמאל בקישור):
* `position: absolute; top: 8px; left: 8px`.
* `background: var(--danger)`, טקסט לבן, `font-size: 0.65rem; font-weight: 700`.
* `min-width: 18px; height: 18px; border-radius: 9px`.

**Sidebar footer:**
* `border-top: 1px solid var(--border)`.
* קישור התנתקות בצבע `--danger`. hover: `background-color: rgba(239, 68, 68, 0.1)`.

### Mobile Topbar

* `display: none` בדסקטופ; `display: flex` ב-≤768px.
* `position: fixed; top: 0; right: 0; left: 0; height: 56px`.
* רקע `--bg-topbar` (כחול-כהה שקוף 0.95) + `backdrop-filter: blur(10px)`.
* `border-bottom: 1px solid var(--border)`, `z-index: 200`.
* תוכן: hamburger מימין → brand-name באמצע (truncated) → spacer.

### Cards

```css
background-color: var(--bg-card);   /* #1E293B */
border: 1px solid var(--border);    /* #334155 */
border-radius: var(--radius-lg);    /* 16px */
overflow: hidden;
transition: all 0.3s ease;
```

* **Hover:** `border-color: rgba(37, 99, 235, 0.3)` — גוון כחול עדין.
* `.card-header`: 1.25rem 1.5rem padding, `border-bottom: 1px solid var(--border)`, `font-weight: 600`.
* `.card-body`: 1.5rem padding.
* **`.card.channel-card-disabled`** (Phase 4): `opacity: 0.55; filter: grayscale(0.4)` — מסמן שדה ערוץ לא רלוונטי.

### Stat Cards

הסטטיסטיקות בדשבורד הן הרכיב הצבעוני ביותר ב-Dark.

```css
.stat-card {
    border-radius: 16px;
    padding: 1rem 0.75rem;
    text-align: center;
    color: white;          /* תמיד לבן — הרקע צבעוני */
    border: none;
    position: relative;
    overflow: hidden;
}
```

* **Hover:** `transform: translateY(-3px)` + `shadow-lg` + שכבת `::before` עם `rgba(255,255,255,0.1)`.
* **וריאנטים:** `.gradient-blue` (primary), `.gradient-success`, `.gradient-info`, `.gradient-warning`, `.gradient-purple`, `.gradient-dark`.
* `stat-icon`: 1.4rem, `opacity: 0.9`. `stat-value`: 1.5rem 700. `stat-label`: 0.8rem 0.9 opacity.

### Status Bar

```css
display: flex; align-items: center; gap: 1.25rem;
padding: 0.75rem 1.25rem;
background: var(--bg-card);
border: 1px solid var(--border);
border-radius: var(--radius-lg);
```

**status-dot** (8×8 circle):
* `.status-active`: רקע `#10B981` + `box-shadow: 0 0 6px rgba(16, 185, 129, 0.5)` (זוהר ירוק).
* `.status-inactive`: רקע `--text-muted`, `opacity: 0.5`.
* `.status-warning`: רקע `#f59e0b` + הילה אמבר + אנימציה `status-warning-pulse 2s ease-in-out infinite` (פועם בין shadow 6px ל-10px). מיועד לחיבור שבור (למשל GCal token פג).

### Tables

* `width: 100%; border-collapse: collapse`.
* **th:** `text-align: right` (RTL), `font-size: 0.8rem; font-weight: 600`, UPPERCASE, `letter-spacing: 0.05em`, `color: var(--text-muted)`, `border-bottom: 2px solid var(--border)`, `white-space: nowrap`.
* **th.sortable.sort-active:** `color: var(--brand-blue)`.
* **td:** `padding: 0.875rem 1rem`, `color: var(--text-secondary)`, גבול תחתון 1px.
* **tr hover:** `background-color: rgba(37, 99, 235, 0.05)` — גוון כחול כמעט בלתי נראה, חתימה של Dark.

ב-mobile: `<thead>` נחבא; כל `<tr>` הופך לכרטיס עם רקע `--bg-card`, גבול, radius-md, shadow-sm.

### Buttons

| Variant | רקע | טקסט | מאפיין ייחודי |
|---|---|---|---|
| `.btn-primary` | `--gradient-primary` | white | `box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3)` ⇒ hover מתחזק ל-`0 4px 16px ... 0.4` |
| `.btn-secondary` | transparent | `--text-secondary` | border 1px `--border`, hover → `bg-card` + טקסט primary |
| `.btn-success` | `--gradient-success` | white | hover shadow ירוק |
| `.btn-danger` | `--danger` solid | white | hover `#DC2626` + shadow אדום |
| `.btn-soft-danger` | `rgba(239, 68, 68, 0.12)` | `--danger` | border `rgba(239, 68, 68, 0.25)` — לפעולות הרסניות שלא צריכות לצעוק (מחיקה ברשימה ארוכה) |
| `.btn-outline-danger` | transparent | `--danger` | border 1px `--danger`. hover: רקע אדום מלא + טקסט לבן |
| `.btn-warning` | `--gradient-warning` | white | — |

**גודל וצורה:**
* Default: `padding: 0.625rem 1.25rem; radius-md`.
* `.btn-sm`: `padding: 0.375rem 0.75rem; font-size: 0.8rem`.
* `.btn-icon`: `36×36`, `padding: 0`, `radius-sm`. `.btn-icon.btn-sm`: `30×30`.
* `.btn:hover`: `transform: translateY(-1px)`. `:active`: חוזר ל-0.

### Badges

```css
display: inline-flex; align-items: center;
padding: 0.25rem 0.625rem;
font-size: 0.75rem; font-weight: 600;
border-radius: 20px;     /* pill */
white-space: nowrap;
```

| Variant | רקע (alpha 0.15–0.2) | טקסט |
|---|---|---|
| `.badge-success` | `rgba(16, 185, 129, 0.15)` | `--success` |
| `.badge-warning` | `rgba(245, 158, 11, 0.15)` | `--warning` |
| `.badge-danger` | `rgba(239, 68, 68, 0.15)` | `--danger` |
| `.badge-info` | `rgba(59, 130, 246, 0.15)` | `--info` |
| `.badge-muted` | `rgba(148, 163, 184, 0.15)` | `--text-muted` |
| `.badge-secondary` | `rgba(148, 163, 184, 0.15)` | `--text-muted` (fallback ניטרלי) |
| `.badge-category` | `rgba(124, 58, 237, 0.18)` | `--brand-purple-light` |
| `.badge-primary` | `rgba(59, 130, 246, 0.2)` | `--brand-blue-light` (0.8rem) |

### Forms

**שני סוגי שדות — חשוב להבדיל:**

| מחלקה | רקע | טקסט | placeholder |
|---|---|---|---|
| `.form-input` / `.form-textarea` / `.form-select` | `--bg-darker` (`#020617`) | `--text-primary` | `--text-muted` |
| `.form-control` | `--bg-input` (`#f5f0e8` קרם) | `--text-input` (`#1a1a1a`) | `--text-input-placeholder` (`#999`) |

המשותף: `padding: 0.75rem 1rem`, border 1px `--border`, `radius-sm`, `font-family: 'Rubik'`, `font-size: 0.9375rem`.

**Focus (כל הסוגים):**
```css
outline: none;
border-color: var(--brand-blue);
box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.2);
```

* `.form-textarea`: `resize: vertical; min-height: 100px`.
* `.form-control-sm`: `padding: 0.375rem 0.625rem; font-size: 0.8125rem`.
* `.form-label`: `display: block; font-weight: 600; margin-bottom: 0.5rem; color: --text-primary`.
* `.form-hint`: `0.8rem; color: --text-muted; margin-top: 0.375rem`.

### Alerts / Flash

```css
padding: 0.875rem 1.25rem;
border-radius: var(--radius-md);
display: flex; align-items: center; justify-content: space-between;
gap: 0.75rem;
animation: fadeIn 0.3s ease;
font-size: 0.9rem;
```

| Variant | רקע (alpha 0.12) | border (alpha 0.3) | טקסט |
|---|---|---|---|
| `.alert-success` | `rgba(16, 185, 129, 0.12)` | `rgba(16, 185, 129, 0.3)` | `--success` |
| `.alert-danger` | `rgba(239, 68, 68, 0.12)` | `rgba(239, 68, 68, 0.3)` | `--danger` |
| `.alert-warning` | `rgba(245, 158, 11, 0.12)` | `rgba(245, 158, 11, 0.3)` | `--warning` |
| `.alert-info` | `rgba(59, 130, 246, 0.12)` | `rgba(59, 130, 246, 0.3)` | `--info` |

* `.btn-close`: `background: none; border: none; color: inherit; opacity: 0.7` (hover: 1).
* Auto-dismiss אחרי 5 שניות (`base.html:485-491`) — `opacity: 0` ב-300ms ואז remove.

**RAG Stale Warning** — אזהרה גלובלית עליונה (`#rag-stale-banner`):
* אותה פלטה כמו `alert-warning` (אמבר 12%/30%).
* `display: flex; justify-content: space-between` עם CTA "בנייה מחדש".

### Filter Pills

```css
padding: 0.375rem 1rem;
border-radius: 20px;       /* pill */
font-size: 0.8rem; font-weight: 600;
background: var(--bg-card);
border: 1px solid var(--border);
```

* **Hover:** `border-color: var(--brand-blue); color: var(--brand-blue)`.
* **Active:** `background: var(--gradient-primary); color: white; border-color: transparent`.

### List Group

```css
.list-group-item {
    padding: 0.875rem 1rem;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
}
.list-group-item:hover { background: rgba(37, 99, 235, 0.05); }
.list-group-item.active {
    background: rgba(37, 99, 235, 0.1);
    border-right: 3px solid var(--brand-blue);  /* RTL: אקסנט מימין */
    color: var(--text-primary);
}
```

### Conversations

* **Layout:** `grid-template-columns: 280px 1fr` (במובייל → `1fr`).
* **`.user-list`:** `max-height: calc(100vh - 160px); overflow-y: auto`.
* **`.messages-container`:** `max-height: calc(100vh - 160px); overflow-y: auto; padding: 1rem`.

**Message Bubble:**
```css
padding: 0.875rem 1.25rem;
border-radius: var(--radius-md);
animation: fadeIn 0.3s ease;
max-width: 85%;
```

| Variant | רקע | גבול | יישור |
|---|---|---|---|
| `.user-msg` | `rgba(37, 99, 235, 0.1)` | `rgba(37, 99, 235, 0.2)` | `margin-left: auto` (RTL: שמאל) |
| `.bot-msg` | `--bg-card` | `--border` | `margin-right: auto` (RTL: ימין) |

> **חשוב:** ב-`#live-chat-messages` האנימציה מבוטלת (`animation: none`) כדי למנוע הבהוב בכל polling.

### Live Chat

```css
.live-chat-container {
    display: flex; flex-direction: column;
    height: calc(100vh - 280px); min-height: 400px;
}
.live-chat-input {
    border-top: 1px solid var(--border);
    padding: 1rem;
    background: var(--bg-darker);
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
}
```

### Login Page

* `min-height: 100vh; display: flex; align-items: center; justify-content: center`.
* רקע: `--gradient-hero` (180deg slate-900→slate-800).
* **`.login-card`:** `max-width: 400px`, `background: rgba(30, 41, 59, 0.8)` + `backdrop-filter: blur(20px)`, border, `radius-lg`, `padding: 2.5rem`, `box-shadow: var(--shadow-lg)`, `animation: fadeIn 0.6s`.
* **`.login-icon`:** 64×64, `--gradient-primary`, אייקון 1.75rem לבן, `box-shadow: var(--shadow-glow)` (זה השימוש היחידי ב-glow).

### Theme Toggle

```css
.theme-toggle {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    width: 36px; height: 36px;
    border-radius: var(--radius-sm);
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem;
}
.theme-toggle:hover { color: var(--text-primary); background: var(--bg-card); }
```

האייקון משתנה לפי ה-theme ה**הבא** במחזור (`base.html:526-531`):
* Dark → אייקון `bi-sun-fill` (יעד הבא: light classic).
* המחזור: `dark → light → light-warm → light-kibbutz → dark`.

### User Note Popover

* `position: fixed; z-index: 9999; width: 260px; direction: rtl`.
* רקע `--bg-card`, border `--border`, `box-shadow: 0 8px 32px rgba(0,0,0,0.3)`.
* **`.note-privacy-banner`** (תיקון 13 — אזהרת פרטיות): רקע `rgba(255,193,7,0.12)`, `border-right: 3px solid #ffc107`, `font-size: 0.75rem`, `padding: 6px 8px`.
* `textarea`: רקע `--bg-input` (קרם), טקסט `--text-input` (כהה), border 1px, `min-height: 70px`.

### Empty State

```css
text-align: center;
padding: var(--space-md);
color: var(--text-muted);
```

`<i>`: `font-size: 3rem; opacity: 0.5; display: block; margin-bottom: 0.75rem`.

### Live Chat Toast (in-page notification)

מוגדר inline ב-`base.html:388-393` (אין class CSS):
* `position: fixed; top: 1rem; left: 1rem; z-index: 9999`.
* רקע `var(--brand-blue, #2563eb)`, טקסט לבן, `padding: 0.75rem 1rem`, `border-radius: 0.5rem`.
* `box-shadow: 0 4px 12px rgba(0,0,0,0.15)`.
* נעלם אחרי 6 שניות (opacity → 0 ב-300ms).

### Upgrade Modal (Phase 4)

* **Overlay:** `position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); z-index: 9999`.
* **Card:** רקע `var(--card-bg, #fff)` (fallback לבן — המודל מתוכנן להיות בהיר גם ב-Dark), `border-radius: 16px; max-width: 460px; padding: 2rem`, `box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35)`.
* **Icon:** `font-size: 2.4rem; color: #6f42c1` (סגול שונה מהמותג — מובחן).

### Grace Period Banner (Phase 5)

| Variant | רקע | border | אייקון | טקסט |
|---|---|---|---|---|
| `.grace-banner--ending` | `rgba(253, 126, 20, 0.12)` | `rgba(253, 126, 20, 0.35)` | `#fd7e14` (כתום) | `--text-primary` |
| `.grace-banner--ended` | `rgba(108, 117, 125, 0.10)` | `rgba(108, 117, 125, 0.30)` | `#6c757d` (אפור) | `--text-muted` |

> **קריאות:** הטקסט חייב `--text-primary` (לא `--text` — משתנה שאינו קיים
> ונופל ל-fallback כהה על רקע כהה). הבאנר כולל כפתור סגירה `.grace-banner__close`
> (יורש `color: inherit`) — הסגירה נשמרת ב-localStorage לפי מספר הימים הנותרים.

### Spinner

```css
width: 16px; height: 16px;
border: 2px solid rgba(255, 255, 255, 0.3);
border-top-color: white;
border-radius: 50%;
animation: spin 0.6s linear infinite;
```

מיועד לכפתורים — לכן הצבעים ביחס ללבן.

---

## 6. דפוסי סיגנצ'ר

### גרדיאנט המותג כחול→סגול

זה ה-DNA של Dark. מופיע ב-7 מקומות בלבד, וזה מכוון:

1. `sidebar-brand .brand-icon` — לוגו במעלה הסיידבר.
2. `sidebar-nav a.active` — פריט פעיל בתפריט.
3. `btn-primary` — כפתור ראשי.
4. `filter-pill.active` — פילטר נבחר.
5. `login-icon` — אייקון בדף הכניסה.
6. `page-header h1 i` — אייקון כותרת עם `clip-text` (טקסט נצבע בגרדיאנט).
7. `stat-card.gradient-blue` — סטטיסטיקה ראשית.

> **לא להוסיף את הגרדיאנט לרכיבים נוספים** — הוא מאבד את החתימה.

### Page Header עם Clip-Text

```css
.page-header h1 i {
    background: var(--gradient-primary);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
```

האייקון בכותרת H1 צבוע בגרדיאנט עצמו (לא רקע) — דרך וויזואלית להכניס את המותג בלי להוסיף עוד אלמנט.

### Tinted Hover (כחול עדין)

ב-Dark, כל `:hover` של רכיב ניטרלי מקבל גוון כחול שקוף עדין:

| רכיב | רקע hover |
|---|---|
| `table tbody tr` | `rgba(37, 99, 235, 0.05)` |
| `.list-group-item` | `rgba(37, 99, 235, 0.05)` |
| `.list-group-item.active` | `rgba(37, 99, 235, 0.1)` |
| `.message-bubble.user-msg` | `rgba(37, 99, 235, 0.1)` |
| `.card:hover` border | `rgba(37, 99, 235, 0.3)` |

זה הופך את הפעולה לרגישה בלי "להבליט" אותה — ה-hover מורגש, לא מצעק.

### Status Glow

נקודות סטטוס (`.status-dot`) מקבלות `box-shadow` בצבע שלהן עצמן ב-50% opacity:

```css
.status-dot.status-active  { box-shadow: 0 0 6px rgba(16, 185, 129, 0.5); }
.status-dot.status-warning { box-shadow: 0 0 6px rgba(245, 158, 11, 0.5);
                             animation: status-warning-pulse 2s infinite; }
```

### Sidebar Active Shadow

`sidebar-nav a.active` מקבל `box-shadow: var(--shadow-md)`. בערכת Dark הצל שחור — המראה הוא של "כפתור מורם" מהסיידבר. ב-`light-warm` הצל ירוק-יער. ב-`light-kibbutz` אין צל בכלל. ה-token הוא הפיצ'ר.

### Custom Scrollbar (Webkit)

```css
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-darker); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
```

צרים (6px), עדינים, מתמזגים עם הסיידבר.

### Anti-FOUC Inline Script

`base.html:3-18` — IIFE שרץ לפני ה-`<body>`, קורא `localStorage['admin-theme']`, ומאמת מול `['dark', 'light', 'light-warm', 'light-kibbutz']`. ערך לא חוקי (למשל `"blue"` מ-deploy ישן) — מנקה את המפתח מ-localStorage כדי שלא יחזור על עצמו. זו הגנה שורשית על הסטייט.

---

## 7. אנימציה ואינטראקציה

### Keyframes

```css
@keyframes fadeIn  { from { opacity: 0; transform: translateY(20px); }
                     to   { opacity: 1; transform: translateY(0);     } }
@keyframes slideUp { from { opacity: 0; transform: translateY(30px); }
                     to   { opacity: 1; transform: translateY(0);     } }
@keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }
@keyframes spin    { to   { transform: rotate(360deg); } }
@keyframes status-warning-pulse {
    0%, 100% { box-shadow: 0 0 6px  rgba(245, 158, 11, 0.5); }
    50%      { box-shadow: 0 0 10px rgba(245, 158, 11, 0.8); }
}
```

### מחלקות אנימציה

| מחלקה | אנימציה | משך |
|---|---|---|
| `.animate-fade-in` | fadeIn | 0.6s ease both |
| `.animate-slide-up` | slideUp | 0.6s ease both |
| `.stagger-1`..`.stagger-5` | `animation-delay: 0.1s..0.5s` | להופעת רשימות מדורגות |

### Hover-Lift על כפתורים וסטטים

* `.btn:hover` → `transform: translateY(-1px)`. `:active` → `0`.
* `.stat-card:hover` → `translateY(-3px) + shadow-lg + ::before opacity 1`.

### HTMX Swap Animations

```css
.htmx-swapping { opacity: 0; transition: opacity 0.3s ease; }
.htmx-added    { animation: fadeIn 0.3s ease; }
tr.htmx-swapping { opacity: 0; transition: opacity 0.3s ease-out; }
```

> **חריג חשוב:** `#live-chat-messages.htmx-swapping` מבטל את ה-fade (`opacity: 1; transition: none`) כדי למנוע הבהוב פאנלים בזמן polling של 5 שניות.

### Anti-Flicker Global Hook

`base.html:495-511` — מאזין ל-`htmx:beforeSwap`/`afterSwap`. אם תגובת השרת זהה למה שכבר ב-DOM (לפי `target.id`), ה-swap מבוטל (`shouldSwap = false`). זה מונע re-render מיותר על polling, ובפרט ב-conversations + live-chat.

### Animation בפועל

* `.alert` נטען עם `animation: fadeIn 0.3s ease`.
* `.message-bubble` נטען עם `animation: fadeIn 0.3s ease` (חוץ מ-`#live-chat-messages`).
* `.login-card` נטען עם `animation: fadeIn 0.6s ease`.

---

## 8. נגישות וריספונסיביות

### Focus Visible

```css
a:focus-visible, button:focus-visible, input:focus-visible,
select:focus-visible, textarea:focus-visible, [tabindex]:focus-visible {
    outline: 2px solid var(--brand-blue);
    outline-offset: 2px;
}
```

מותאם — outline כחול 2px עם offset 2px. גם בערכות בהירות (`brand-blue` מתחלף לפי theme).

### Reduced Motion

```css
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }
}
```

מבטל בפועל אנימציות ו-transitions למשתמשים שביקשו זאת ברמת מערכת ההפעלה.

### Breakpoints

| Breakpoint | שינויים מרכזיים |
|---|---|
| `≤1024px` | `.stats-grid` → `minmax(160px, 1fr)`. `.content-two-col` → 1 עמודה. |
| `≤768px` | `html` → `font-size: 14px`. סיידבר נכבה (`translateX(100%)`), נפתח עם `.open`. `mobile-topbar` נחשף. `main-content` → `margin-right: 0`, `padding-top: calc(56px + 1rem)`. טבלאות → "כרטיס לכל שורה". `.conversation-layout` → 1 עמודה. `.user-list` → `max-height: 200px`. `.message-bubble` → `max-width: 95%`. |

**כפתור hamburger** (`.hamburger`): `background: none; border: none; color: --text-primary; font-size: 1.5rem`.

**Sidebar Overlay** (`.sidebar-overlay.active`): `position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); z-index: 99` — נחשף כשהסיידבר פתוח במובייל, נסגר בלחיצה.

---

## 9. לעשות / לא לעשות

### לעשות

* **להשתמש ב-tokens תמיד.** `var(--bg-card)` ולא `#1E293B`. גם ב-Dark — כי השם מבטא תפקיד, לא צבע.
* **לשמור את הגרדיאנט הראשי לחתימה.** רק 7 מקומות, ראה §6.
* **Hover ניטרלי = `rgba(37, 99, 235, 0.05–0.10)`.** כחול שקוף עדין על טבלאות, list-groups, message-bubbles.
* **`box-shadow` בצבע הסטטוס לזוהר.** ירוק/אמבר 50% — לא אפור, לא שחור.
* **Pill = 20px קבוע** (לא token). שמור על כך לעקביות בכל badges ו-filter-pills.
* **כל theme חייב לעבור דרך ה-IIFE ב-base.html** (`VALID = ['dark', 'light', 'light-warm', 'light-kibbutz']`). הוספת theme חדש דורשת עדכון בשני המקומות (IIFE + `THEME_CYCLE`).
* **ב-mobile, להפוך טבלה לכרטיסים.** כל הפאנל כבר עושה זאת ב-`@media (max-width: 768px)` — לא לבטל לטבלה ספציפית.

### לא לעשות

* **לא להוסיף שחור (`#000`).** הרקע הכי כהה הוא `#020617`. שחור שובר את ה-Slate-Blue.
* **לא להוסיף גרדיאנט לכרטיסים רגילים.** רק stat-cards משתמשים בגרדיאנטים. ל-`.card` יש `--bg-card` סוליד.
* **לא להגדיר `animation: fadeIn` על תוכן בתוך `#live-chat-messages`.** זה יגרום להבהוב בכל polling. הקוד כבר מבטל את זה (`animation: none`) — לא לעקוף.
* **לא לקרוא ל-`color: white` ידנית.** הכפתורים שזה מתאים להם (`btn-primary`, `btn-success`, `btn-danger`, `btn-warning`) כבר מוגדרים. בכל מקום אחר השתמש ב-`--text-primary`.
* **לא לערבב `.form-input` עם `.form-control`.** הראשון רקע כהה (`--bg-darker`), השני רקע קרם (`--bg-input`). הרכיב נבחר לפי הקונטקסט הקיים בעמוד — לא ליצור עירוב.
* **לא להוסיף `box-shadow` עם RGB אפור גנרי.** המערכת משתמשת ב-`shadow-sm/md/lg` (שחור) או ב-`shadow-glow` (כחול מותג) או ב-shadow ייעודי לסטטוס. אין אפור.
* **לא לדרוס selectors קיימים בתוך `[data-theme="..."]`.** הגישה השורשית של הקוד היא **רק** override של ה-tokens. שמור על כך גם בעתיד — אחרת ה-themes יתחילו להידרדר אחד את השני.

---

## 10. CSS Tokens

קוד מלא להעתקה — תואם בדיוק ל-`admin/static/css/style.css:7-69`:

```css
:root {
    /* Backgrounds */
    --bg-dark: #0F172A;
    --bg-darker: #020617;
    --bg-card: #1E293B;
    --bg-input: #f5f0e8;
    --bg-topbar: rgba(15, 23, 42, 0.95);

    /* Text */
    --text-primary: #F8FAFC;
    --text-secondary: #CBD5E1;
    --text-muted: #94A3B8;
    --text-input: #1a1a1a;
    --text-input-placeholder: #999;

    /* Brand */
    --brand-blue: #2563EB;
    --brand-blue-light: #60a5fa;
    --brand-purple: #7C3AED;
    --brand-purple-light: #a78bfa;

    /* Status */
    --success: #10B981;
    --warning: #F59E0B;
    --danger: #EF4444;
    --info: #3B82F6;

    /* Border */
    --border: #334155;

    /* Gradients */
    --gradient-primary: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
    --gradient-hero: linear-gradient(180deg, #0F172A 0%, #1E293B 100%);
    --gradient-success: linear-gradient(135deg, #10B981 0%, #059669 100%);
    --gradient-warning: linear-gradient(135deg, #F59E0B 0%, #D97706 100%);
    --gradient-info: linear-gradient(135deg, #3B82F6 0%, #2563EB 100%);
    --gradient-purple: linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%);
    --gradient-dark: linear-gradient(135deg, #475569 0%, #334155 100%);

    /* Shadows */
    --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.1);
    --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.2);
    --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.3);
    --shadow-glow: 0 0 30px rgba(37, 99, 235, 0.3);

    /* Spacing */
    --space-xs: 0.5rem;
    --space-sm: 1rem;
    --space-md: 2rem;
    --space-lg: 4rem;

    /* Radius */
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;

    /* Sidebar */
    --sidebar-width: 260px;
    --topbar-height: 56px;

    /* Transitions */
    --transition: all 0.3s ease;
}
```

### Reset ו-base body

```css
body {
    font-family: 'Rubik', sans-serif;
    font-weight: 400;
    line-height: 1.6;
    color: var(--text-primary);
    background-color: var(--bg-dark);
    direction: rtl;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

a { color: var(--brand-blue); text-decoration: none; transition: var(--transition); }
a:hover { color: var(--brand-purple); }
```

### HTML Imports

מ-`base.html:25-33`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Assistant:wght@700;800&family=Frank+Ruhl+Libre:wght@500;700;900&family=Heebo:wght@300;400;500;600;700&family=Rubik:wght@300;400;500;600;700&display=swap" rel="stylesheet">

<!-- Bootstrap Icons (אייקוני bi-*) -->
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet" crossorigin="anonymous">

<!-- Design System -->
<link href="{{ url_for('static', filename='css/style.css') }}" rel="stylesheet">

<!-- HTMX (מפעיל את כל ה-swap animations שמתועדים ב-§7) -->
<script src="https://unpkg.com/htmx.org@2.0.4" crossorigin="anonymous"></script>
```

---

## נספח: השוואה מהירה לערכות הבהירות

| Token | dark (`:root`) | light | light-warm | light-kibbutz |
|---|---|---|---|---|
| `--bg-dark` | `#0F172A` | `#F1F5F9` | `#f5f0e6` (cream) | `#F5EFD8` (wheat) |
| `--bg-card` | `#1E293B` | `#FFFFFF` | `#fbf8f1` (paper) | `#DDD0AE` (faded poster) |
| `--text-primary` | `#F8FAFC` | `#0F172A` | `#1a1a1a` | `#2D2A1F` (olive ink) |
| `--brand-blue` | `#2563EB` | `#2563EB` | `#1f4a35` (forest) | `#3E5524` (kibbutz olive) |
| `--gradient-primary` | blue→purple gradient | blue→purple gradient | `#1f4a35` solid | `#2D2A1F` solid (ink fill) |
| `--shadow-md` | `rgba(0,0,0,0.2)` | `rgba(0,0,0,0.08)` | `rgba(20,48,36,0.08)` (forest) | `none` |
| `--radius-sm` | `8px` | `8px` | `8px` | `2px` (sharp poster) |

**הכלל השורשי:** ה-CSS נבנה כך שעקיפת tokens **מספיקה** כדי להחליף כל ערכת נושא. אין selector שכתוב במפורש לערכה ספציפית, חוץ מ-`btn-soft-danger` שמקבל וריאציות לפי `[data-theme="..."]` — כי הוא דורש איזון אדום עדין שתלוי באופי הסביבה. שמור על העיקרון הזה: tokens קודם, selectors רק כשאין ברירה.
