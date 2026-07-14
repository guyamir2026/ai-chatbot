"""
טסטים למסך פרופיל העסק בפאנל (שלב 2 של מערכת הזיכרון).

מכסה: שמירת הטופס דרך POST, פיענוח שירותים דינמיים, ו-GET שמשחזר
את הערכים. הטסטים משתמשים בלקוח הטסט של Flask + ב-`db_conn` fixture
שמייצר DB tmp נפרד לכל טסט.
"""

import json

import pytest
from werkzeug.datastructures import MultiDict


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    """משתני סביבה ש-create_admin_app() דורש (ADMIN_USERNAME/PASSWORD).

    `_validate_admin_security_config` מקריס את ה-app אם הם חסרים.

    גם patch על המודולים אחרי import — טסטים אחרים בריפו עושים reload של
    `database` ו-`ai_chatbot.config`, מה ששובר binding של הקבועים שנקבעו
    בעת ייבוא ראשון. patch ישיר על המודול שורד reload.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "test_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test_pass_for_unit_tests_only")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests")

    # אם המודולים כבר נטענו (טסט קודם), נדרוס את הקבועים שלהם ישירות
    import importlib
    for mod_name in ("ai_chatbot.config", "admin.app"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "ADMIN_USERNAME"):
            monkeypatch.setattr(mod, "ADMIN_USERNAME", "test_admin", raising=False)
        if hasattr(mod, "ADMIN_PASSWORD"):
            monkeypatch.setattr(mod, "ADMIN_PASSWORD", "test_pass_for_unit_tests_only", raising=False)
        if hasattr(mod, "ADMIN_SECRET_KEY"):
            monkeypatch.setattr(mod, "ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests", raising=False)


@pytest.fixture
def client(db_conn):
    """Flask test client עם CSRF מוגדר off (פנימי לטסטים) ו-session מאומת.

    ה-`db_conn` fixture מאתחל את ה-DB tmp ומחזיק patch על DB_PATH לאורך
    הטסט — admin app מקבל את אותו DB דרך ai_chatbot.database.
    """
    # ייבוא דחוי: ADMIN_USERNAME/PASSWORD חייבים להיקבע ב-env *לפני*
    # ייבוא ai_chatbot.config (שנקרא מ-admin.app בעת ייבוא).
    from admin.app import create_admin_app

    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "test_admin"
        yield c


class TestBusinessProfileGET:
    def test_get_empty_renders_blank_form(self, client):
        """GET ראשון על profile ריק — דף נטען בלי שגיאות."""
        resp = client.get("/business-profile")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "פרופיל עסק" in body
        # אופציות סוגי עסק נטענות
        assert "מספרה" in body
        assert "קליניקת אסתטיקה" in body

    def test_get_shows_existing_profile(self, client, db_conn):
        from database import upsert_business_profile

        upsert_business_profile({
            "business_id": "default",
            "business_type": "מספרה",
            "business_name": "סטודיו לירון",
            "services_json": json.dumps([
                {"name": "תספורת", "aliases": ["גזירה", "סטייל"], "category": "תספורות"},
            ], ensure_ascii=False),
            "what_matters_for_extraction": "סוג שיער, אורך מועדף",
        })
        resp = client.get("/business-profile")
        body = resp.data.decode("utf-8")
        assert "סטודיו לירון" in body
        assert "סוג שיער, אורך מועדף" in body
        # שירותים מופיעים בטבלה
        assert "תספורת" in body
        # aliases מוצגים כ-CSV
        assert "גזירה, סטייל" in body


class TestBusinessProfilePOST:
    def test_post_saves_basic_fields(self, client, db_conn):
        from database import get_business_profile

        resp = client.post("/business-profile", data={
            "business_type": "מספרה",
            "business_name_field": "סטודיו לירון",
            "what_matters_for_extraction": "סוג שיער",
            # רשימת שירותים ריקה
        }, follow_redirects=False)
        assert resp.status_code == 302  # redirect אחרי שמירה

        prof = get_business_profile("default")
        assert prof["business_type"] == "מספרה"
        assert prof["business_name"] == "סטודיו לירון"
        assert prof["what_matters_for_extraction"] == "סוג שיער"
        assert json.loads(prof["services_json"]) == []

    def test_post_saves_services_parallel_arrays(self, client, db_conn):
        """3 רשימות מקבילות (name/aliases/category) מתאחדות לאובייקטים."""
        from database import get_business_profile

        # MultiDict כדי להעביר ערכים כפולים לאותו key — Werkzeug 3+ דורש
        # MultiDict ולא list של tuples.
        data = MultiDict()
        data.add("business_type", "קליניקת אסתטיקה")
        data.add("business_name_field", "קליניקת גלאם")
        data.add("what_matters_for_extraction", "סוג עור, רגישויות")
        data.add("service_name", "מניקור ג'ל")
        data.add("service_aliases", "מניקור, ג'ל")
        data.add("service_category", "ציפורניים")
        data.add("service_name", "פדיקור רפואי")
        data.add("service_aliases", "פדיקור")
        data.add("service_category", "רגליים")
        client.post("/business-profile", data=data)

        prof = get_business_profile("default")
        services = json.loads(prof["services_json"])
        assert len(services) == 2
        assert services[0]["name"] == "מניקור ג'ל"
        assert services[0]["aliases"] == ["מניקור", "ג'ל"]
        assert services[0]["category"] == "ציפורניים"
        assert services[1]["name"] == "פדיקור רפואי"
        assert services[1]["aliases"] == ["פדיקור"]

    def test_post_skips_empty_service_rows(self, client, db_conn):
        """שורה עם service_name ריק (משתמש לחץ "הוסף" ולא מילא) — מדלגים."""
        from database import get_business_profile

        data = MultiDict()
        data.add("business_type", "אחר")
        data.add("business_name_field", "Test")
        data.add("what_matters_for_extraction", "")
        data.add("service_name", "שירות תקין")
        data.add("service_aliases", "")
        data.add("service_category", "קטגוריה")
        data.add("service_name", "")  # ריק — מדלגים
        data.add("service_aliases", "כינוי")
        data.add("service_category", "שלא יופיע")
        client.post("/business-profile", data=data)

        services = json.loads(get_business_profile("default")["services_json"])
        assert len(services) == 1
        assert services[0]["name"] == "שירות תקין"

    def test_post_aliases_csv_handles_whitespace(self, client, db_conn):
        """CSV של aliases — רווחים בין הפסיקים מנוקים; פסיקים ריקים נדחים."""
        from database import get_business_profile

        data = MultiDict()
        data.add("business_type", "אחר")
        data.add("business_name_field", "T")
        data.add("what_matters_for_extraction", "")
        data.add("service_name", "A")
        data.add("service_aliases", "  alias1 ,  alias2  ,, alias3,  ")
        data.add("service_category", "cat")
        client.post("/business-profile", data=data)

        services = json.loads(get_business_profile("default")["services_json"])
        assert services[0]["aliases"] == ["alias1", "alias2", "alias3"]

    def test_post_unicode_persists(self, client, db_conn):
        """תווי עברית נשמרים בלי escape (ensure_ascii=False ב-json.dumps)."""
        from database import get_business_profile

        client.post("/business-profile", data={
            "business_type": "מספרה",
            "business_name_field": "סטודיו",
            "what_matters_for_extraction": "סוג שיער",
        })
        raw = get_business_profile("default")["services_json"]
        # אם היה ensure_ascii=True היינו רואים \u05XX — דורש שלא יופיע
        assert "\\u" not in raw

    def test_post_overwrites_existing(self, client, db_conn):
        """שמירה שנייה דורסת את הראשונה (upsert)."""
        from database import get_business_profile

        client.post("/business-profile", data={
            "business_type": "מספרה",
            "business_name_field": "ראשון",
            "what_matters_for_extraction": "א",
        })
        client.post("/business-profile", data={
            "business_type": "מסעדה / בית קפה",
            "business_name_field": "שני",
            "what_matters_for_extraction": "ב",
        })
        prof = get_business_profile("default")
        assert prof["business_name"] == "שני"
        assert prof["business_type"] == "מסעדה / בית קפה"


class TestAuthRequired:
    def test_unauthenticated_redirects_to_login(self, db_conn):
        """ללא session — redirect ל-/login (לא 200 ולא 500)."""
        from admin.app import create_admin_app

        app = create_admin_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            resp = c.get("/business-profile", follow_redirects=False)
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")
