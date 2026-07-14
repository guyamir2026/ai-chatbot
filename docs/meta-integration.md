# חיבור הבוט/הפאנל ל-Meta (Facebook Messenger + Instagram DM)

הדרכה צעד-אחר-צעד לחיבור הפאנל לאפליקציית Meta עבור Messenger ו-Instagram
Direct. WhatsApp **לא** נכלל כאן — הוא עובר דרך Twilio (ראה תיעוד נפרד).

## תוצאה סופית

אחרי השלמת ההוראות:
- הודעות שמגיעות לעמוד Facebook → הבוט עונה אוטומטית.
- הודעות שמגיעות ל-Instagram Business → הבוט עונה אוטומטית.
- בעל העסק יכול לראות שיחות בפאנל ולקחת אותן לשיחה חיה.

---

## דרישות מקדימות

לפני שמתחילים, ודא שיש לך:

1. **Facebook account אישי** עם הרשאת admin על העמוד הרלוונטי.
2. **Facebook Page** של העסק (יש לך, אחרת ייצר ב-facebook.com/pages/create).
3. **Instagram Business / Creator Account** — מקושר ל-Facebook Page.
   - בדיקה: באפליקציית Instagram → Settings → Account →
     "Switch to Professional Account" אם עדיין Personal.
   - קישור: Settings → Linked Accounts → Facebook → בחר את העמוד.
4. **דומיין HTTPS פעיל** של הפאנל (לדוגמה
   `https://my-bot-admin.onrender.com`). Meta דורש HTTPS עם תעודה תקפה
   ל-webhook.
5. **גישת admin לפאנל** (ENV: `ADMIN_USERNAME` + `ADMIN_PASSWORD`).

---

## שלב 1 — יצירת אפליקציה ב-Meta for Developers

1. כניסה ל-https://developers.facebook.com/apps/
2. לחץ **"Create App"**. ייפתח אשף בן חמישה שלבים בסרגל העליון:
   **App details → Use cases → Business → Requirements → Overview**.
3. במסך **App details** מלא:
   - **App name**: לדוגמה `<business name> bot` (נראה רק לך, לא ללקוחות).
   - **App contact email**: שלך.
4. **Next** → תועבר למסך Use cases (שלב 2 במסמך זה).

> **Business Portfolio**: באשף תידרש לקשר Portfolio בשלב Business.
> אם אין לך — אפשר ליצור בקלות מ-`business.facebook.com → Create
> Account`. אימות העסק עצמו (Business Verification) יידרש רק לפני
> App Review (ראה שלב 9).

מעכשיו אתה במצב **Development**. הוא מספיק לטסטים שלך, אבל יעבוד רק
לחשבונות שבהם אתה admin/tester. במצב הזה הבוט יענה רק לעצמך — לא
ללקוחות אמיתיים. **App Review** נדרש לפני go-live (ראה שלב 9).

---

## שלב 2 — בחירת Use cases

> **שינוי ב-Meta (2025):** מבנה ה-Products הישן ("Add Product →
> Messenger / Instagram / Facebook Login for Business") הוחלף
> ב-Use cases מבוססי-תוצאה. במקום להוסיף מוצרים בנפרד, מצהירים פעם
> אחת לאיזה שימוש האפליקציה משמשת — וזה קובע אילו הרשאות תוכל לבקש
> ב-App Review.

במסך **Use cases** של האשף, לחץ על הפילטר **"All"** משמאל כדי לראות
את כל ה-19 האפשרויות.

### לסמן

- ✅ **Engage with customers on Messenger from Meta** — מחליף את
  ה-product הישן של Messenger. כולל אוטומטית את ה-OAuth flow לבעלי
  עמודים (מה שהיה "Facebook Login for Business").
- ✅ **Manage messaging & content on Instagram** — מחליף את ה-product
  הישן של Instagram.
- ✅ **Manage everything on your Page** — נותן את הרשאות ה-Pages API
  (`pages_show_list`, `pages_manage_metadata`, `pages_read_engagement`,
  `business_management`) הנדרשות ב-App Review (ראה שלב 9).

> **"Facebook Login for Business" — איפה הוא?** הוא לא נעלם — הוטמע.
> Meta הבינה שמי שבוחר use case של business messaging בהכרח צריך
> שבעלי עסקים יוכלו להתחבר ולהעניק הרשאה לעמוד שלהם, אז ה-Login flow
> נכלל אוטומטית. את הגדרות ה-Valid OAuth Redirect URIs תמצא אחרי
> יצירת ה-App תחת **App Settings → Basic** או **Use cases → Customize
> → Settings**, לא כ-product נפרד בתפריט הצדדי.

**Next** → ממשיכים לשלב Business באשף.

### Business (שלב באשף)

קישור **Business Portfolio**. אם אין לך — צור באמצעות
`business.facebook.com → Create Account`. שם העסק חייב להיות זהה
בדיוק למסמכים שתעלה ב-Business Verification בהמשך (תעודת רישום עוסק
/ ח.פ.). Business Verification לא נדרש *עכשיו*, אבל כן נדרש לפני
App Review (שלב 9).

### Requirements (שלב באשף)

- **Privacy Policy URL** — חובה לפני App Review. דף פשוט בדומיין
  שלך (`/privacy`).
- **Terms of Service URL** — אותו דבר (`/terms`).
- אייקון אפליקציה (1024×1024) — אופציונלי בהתחלה, יידרש לפני Review.

אם אין לך עדיין — אפשר לדלג ולהשלים לפני App Review.

### Overview

סיכום הבחירות → **Create app** → מועברים ל-Dashboard.

---

## שלב 3 — קבלת App ID + App Secret

1. בסיידבר → **"App settings"** → **"Basic"**.
2. בראש הדף תראה:
   - **App ID** — מספר ארוך. העתק.
   - **App secret** — לחץ **"Show"**, אשר עם הסיסמה שלך. העתק.
3. **שמור את שני הערכים במקום בטוח** — תצטרך אותם בשלב 5.

> ⚠️ **App secret אסור לחשוף לעולם** — לא ב-Git, לא בלוגים, לא לאף אחד.
> אם בטעות נחשף — באותו מסך לחץ **"Reset"** מיד.

---

## שלב 4 — בחירת Verify Token

זה token שאתה ממציא — string ארוך ואקראי. Meta תשלח אותו בכל קריאת
verification של ה-webhook, והקוד שלנו ישווה אותו ל-`META_VERIFY_TOKEN`
שב-ENV.

ייצר אחד:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

לדוגמה: `8xJ_kP9LqRtVwYzA-bCdE3fGhIjK5MnOpQrStUvWxYz`. שמור אותו.

---

## שלב 5 — הגדרת ENV vars ב-Render

ב-Render Dashboard → השירות שלך → **Environment** → הוסף את המשתנים:

| Key | Value |
|---|---|
| `META_APP_ID` | מ-שלב 3 |
| `META_APP_SECRET` | מ-שלב 3 |
| `META_VERIFY_TOKEN` | מ-שלב 4 |
| `META_OAUTH_REDIRECT_URI` | `https://<your-admin-domain>/admin/meta/callback` |
| `META_GRAPH_API_VERSION` | `v21.0` (ברירת מחדל — אפשר להשאיר ריק) |
| `ADMIN_URL` | `https://<your-admin-domain>` (כבר אמור להיות מוגדר) |

**חשוב**: `META_OAUTH_REDIRECT_URI` חייב להיות **בדיוק** מה שתגדיר ב-Meta
בשלב 6.3 — כולל https, סלאשים, ו-`/admin/meta/callback` בסוף.

לחץ **"Save changes"** ב-Render. השירות יידחף מחדש (כ-30 שניות).

---

## שלב 6 — הגדרת Webhook + OAuth ב-Meta Dashboard

### 6.1 — Messenger Webhook

1. ב-Meta App Dashboard → **Messenger** → **"Settings"** (תת-תפריט).
2. גלול ל-**"Webhooks"** → **"Add Callback URL"**.
3. **Callback URL**: `https://<your-admin-domain>/webhooks/meta`
4. **Verify Token**: ה-token מ-שלב 4 (אותו אחד שב-ENV).
5. לחץ **"Verify and Save"**.
   - אם נכשל: בדוק ש-`META_VERIFY_TOKEN` ב-Render זהה בדיוק; שהשירות
     רץ; שה-URL נגיש (פתח אותו בדפדפן — צריך לקבל תשובה כלשהי, לא 502).
6. אחרי שעבר verification → ליד **Webhook Fields** לחץ **"Manage"**
   ובחר:
   - `messages` ✅
   - `messaging_postbacks` ✅
   - (השאר אופציונליים)
7. **Add Subscriptions**.

### 6.2 — Instagram Webhook

1. **Instagram** → **"API setup with Facebook login"** → גלול ל-
   **"Webhooks"**.
2. **Callback URL** + **Verify Token**: זהים ל-6.1.
3. **Verify and Save**.
4. ב-Webhook Fields בחר:
   - `messages` ✅
   - (השאר אופציונליים)

### 6.3 — Facebook Login redirect URI

1. בסיידבר → **Facebook Login for Business** → **"Settings"**.
2. ב-**"Valid OAuth Redirect URIs"** הוסף:
   `https://<your-admin-domain>/admin/meta/callback`
3. **Save Changes**.

### 6.4 — App Domains

1. **App Settings** → **Basic**.
2. **App Domains** → הוסף את הדומיין שלך (בלי `https://`, לדוגמה
   `my-bot-admin.onrender.com`).
3. גלול למטה → **Save Changes**.

---

## שלב 7 — חיבור עמוד דרך הפאנל

1. כניסה לפאנל: `https://<your-admin-domain>/admin/meta/setup`
2. תראה את מצב החיבור הנוכחי (כנראה ריק).
3. לחץ **"חבר חשבון Meta"**.
4. תועבר לדיאלוג של Facebook:
   - לוגין (אם לא מחובר).
   - **"Edit settings"** — בחר את ה-Page של העסק (ואת ה-Instagram אם
     מחובר). אל תבטל אף הרשאה — כולן נדרשות.
   - **"Continue"** → **"Save"**.
5. תועבר חזרה לפאנל למסך **"בחר עמוד"** עם רשימת העמודים שלך.
6. בחר את העמוד הרלוונטי → **"חבר"**.
7. הפאנל יבצע ברקע:
   - יחליף את ה-User Token ל-Page Access Token (long-lived).
   - יצפין ויכתוב ל-DB.
   - ירשם את העמוד ל-subscription של Webhook.
   - אם יש Instagram Business מקושר — יחבר אותו אוטומטית.
8. בסיום תראה את העמוד ברשימת "עמודים מחוברים".

---

## שלב 8 — בדיקה

### 8.1 — Messenger
1. מהטלפון/דפדפן שלך, לך לעמוד ה-Facebook של העסק.
2. שלח הודעה ("שלום").
3. תוך 1-3 שניות אמורה להגיע תשובה מהבוט.
4. בפאנל → **"שיחות"** — אמורה להופיע השיחה עם badge של Messenger
   (`bi-messenger`).

### 8.2 — Instagram
1. שלח DM לחשבון ה-Instagram Business של העסק.
2. בדיקה זהה כמו ב-Messenger; הצ'אנל יסומן עם `bi-instagram`.

### 8.3 — אם לא עובד

| תסמין | בדיקה |
|---|---|
| Webhook verification נכשל בשלב 6 | `META_VERIFY_TOKEN` ב-Render = ה-token שהזנת ב-Meta? |
| OAuth מחזיר "Invalid redirect URI" | `META_OAUTH_REDIRECT_URI` ב-Render = הערך ב-Meta Dashboard? |
| הודעות לא מגיעות | בלוגים של Render חפש `meta_webhook`. אם אין — Meta לא שולחת. בדוק ב-Meta Dashboard → Webhooks → "Recent Deliveries". |
| הבוט לא מגיב ו**אין שום לוג** ב-Render כששולחים הודעה | כמעט תמיד: האפליקציה ב-**Development mode** ושלחת מחשבון שאינו admin/tester. מטא חוסמת את ההודעה **במקור** — היא לא מגיעה ל-webhook (ולכן אין לוג). **זכור:** אי אפשר לשלוח לעמוד שאתה מנהל מאותו חשבון, אז בדיקה נעשית מחשבון שני — והוא חייב להיות tester. פתרון: App roles → Roles → Testers → הוסף את החשבון השולח, ואשר מ-`developers.facebook.com/requests`. לפרודקשן: Live mode + App Review (שלב 9). |
| הודעות מגיעות אבל הבוט לא עונה | בדוק ש-`OPENAI_API_KEY` מוגדר ושיש credits. |
| Instagram לא מקושר אוטומטית | (1) ודא שהאינסטגרם **Professional** — Business או Creator, **שניהם עובדים** — ולא Personal. (2) הכי חשוב: ודא שהוא מקושר ל**עמוד הפייסבוק הספציפי** שחיברת (לא לעמוד אחר): Instagram app → Settings → Linked Accounts → Facebook. הקוד שולף את ה-IG המקושר לעמוד שבחרת בלבד. (3) בלוגים חפש `get_ig_business_account` — `business=False connected=False` משמעו שאין IG מקושר לעמוד הזה. הקוד מנסה גם `connected_instagram_account` כ-fallback. |
| הפאנל אומר "לא נמצאו עמודים" אבל יש לך עמוד | כמעט תמיד: העמוד תחת **תיק עסקי** (Business Portfolio). ודא ש-`business_management` ברשימת ההרשאות (שלב 9) ושאתה admin של האפליקציה. בלוגים חפש `Meta OAuth` — אם `/me/businesses` מראה `(#100) Missing Permission`, זו בדיוק ההרשאה החסרה. |

---

## שלב 9 — App Review (מעבר ל-Production)

עד עכשיו ה-App במצב **Development** — רק חשבונות שאתה רשם כ-admin/tester
יכולים להגיב. כדי שלקוחות אמיתיים יוכלו לכתוב:

1. ב-App Dashboard → **App Review** → **Permissions and Features**.
2. לכל אחת מהרשאות הבאות לחץ **"Request"**:
   - `pages_messaging`
   - `pages_show_list`
   - `pages_manage_metadata`
   - `pages_read_engagement` — נדרשת לקריאת מטא-דאטה של העמוד, כולל השדה
     `instagram_business_account`. בלעדיה חיבור האינסטגרם נכשל ב-(#100)
     "requires the pages_read_engagement permission" וה-IG לא מתחבר
     אוטומטית. (admin/tester מקבל מיד; לקוחות אחרים — אחרי App Review.)
   - `instagram_basic`
   - `instagram_manage_messages`
   - `business_management` — נדרשת לחיבור עמודים שמנוהלים תחת **תיק
     עסקי** (Business Portfolio / Meta Business Suite). בלעדיה
     `/me/accounts` מחזיר רשימה ריקה לעמודים כאלה, והפאנל יציג בטעות
     "לא נמצאו עמודים". **הערה**: בעל האפליקציה (admin/tester) מקבל את
     ההרשאה הזו אוטומטית גם לפני App Review — היא נדרשת ב-Review רק כדי
     שלקוחות אחרים יוכלו לחבר עמוד שתחת תיק עסקי.
3. לכל הרשאה Meta תבקש:
   - **Use case description**: הסבר קצר באנגלית מה הבוט עושה.
   - **Video screencast**: סרטון 1-2 דקות שמראה את הזרימה (לקוח שולח
     הודעה → הבוט עונה → בעל העסק רואה בפאנל).
   - **Test credentials**: לרוב לא נדרש כי Meta בודקת דרך החשבון
     שלך, אבל אם מבקשים — תן username/password של חשבון Facebook
     ייעודי.
4. **Submit for Review**. הבדיקה לוקחת 3-7 ימים.
5. אחרי אישור: ב-Dashboard → **Settings → Basic** → גלול למטה → שנה
   **App Mode** מ-Development ל-**Live**.

> **לפני App Review**: ודא שיש לאפליקציה Privacy Policy URL ו-Terms
> of Service URL ב-Settings → Basic. אפשר להשתמש בדפים פשוטים בדומיין
> שלך (`/privacy`, `/terms`).

---

## תחזוקה שוטפת

### Page Access Token expiration
ה-Page Access Token שהמערכת מצפינה הוא **long-lived** (60 ימים) ומתחדש
אוטומטית כל פעם שבעל העסק נכנס ל-`/admin/meta/setup` ולוחץ פעולה.
אם לא היה רענון 60+ ימים — Meta תבטל אותו ותקבל 401 בלוגים. פתרון:
חזור על שלב 7 (re-OAuth).

### ניתוק עמוד
ב-`/admin/meta/setup` → ליד העמוד → **"נתק"**. הפעולה:
- מבטלת את ה-Webhook subscription.
- מוחקת את הטוקן המוצפן מ-DB.
- העמוד יישאר ב-Meta — רק החיבור לבוט שלנו ינותק.
- **הערה לחיבור מחדש**: הניתוק *לא* מבטל את אישור האפליקציה בצד פייסבוק.
  לכן בחיבור מחדש מטא לעיתים מדלגת על מסך בחירת-העמוד. אם אחרי חיבור
  מחדש מתקבל "לא נמצאו עמודים", הסר את האפליקציה ב-
  `facebook.com/settings?tab=business_tools` והתחבר שוב כדי לאלץ מסך
  בחירה טרי. (אם העמוד תחת תיק עסקי — צריך גם `business_management`, ראה
  טבלת הבעיות בשלב 8.3.)

### עדכון גרסת Graph API
ברירת מחדל: `v21.0`. Meta מוציאה גרסה חדשה כל ~3 חודשים, וגרסאות
ישנות פוקעות אחרי כשנתיים. לעדכון: שינוי `META_GRAPH_API_VERSION`
ב-Render → restart. בדוק תאימות ב-https://developers.facebook.com/docs/graph-api/changelog

---

## נספח — ENV vars מלאים

```bash
# חובה
META_APP_ID=123456789012345
META_APP_SECRET=abc123def456ghi789jkl012mno345pq
META_VERIFY_TOKEN=8xJ_kP9LqRtVwYzA-bCdE3fGhIjK5MnOpQrStUvWxYz
META_OAUTH_REDIRECT_URI=https://my-bot-admin.onrender.com/admin/meta/callback
ADMIN_URL=https://my-bot-admin.onrender.com

# אופציונלי (יש ברירות מחדל)
META_GRAPH_API_VERSION=v21.0
META_MESSENGER_MAX_LENGTH=2000
META_INSTAGRAM_MAX_LENGTH=1000
```

---

## נספח — Endpoints של הפאנל

| Path | מטרה |
|---|---|
| `GET /admin/meta/setup` | מסך ראשי — חיבור/ניתוק עמודים |
| `GET /admin/meta/connect` | מתחיל OAuth flow (פנימי) |
| `GET /admin/meta/callback` | callback מ-Meta אחרי OAuth (פנימי) |
| `POST /admin/meta/select-page` | בחירת עמוד אחרי OAuth (פנימי) |
| `POST /admin/meta/disconnect/<page_id>` | ניתוק עמוד |
| `GET /webhooks/meta` | Meta verification handshake |
| `POST /webhooks/meta` | קבלת events מ-Meta (messages, postbacks) |
