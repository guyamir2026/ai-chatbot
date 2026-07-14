"""
טסט שמאכף שהרשאות ה-OAuth של מטא (`_OAUTH_SCOPES`) כוללות את ההרשאות
הקריטיות.

המוטיבציה: `business_management` חיונית לחיבור עמודים שמנוהלים תחת
**תיק עסקי** (Business Portfolio). בלעדיה `/me/accounts` מחזיר ריק
והפאנל מציג "לא נמצאו עמודים" למרות שלמשתמש יש עמוד — וגם /me/businesses
נכשל ב-"(#100) Missing Permission". הטסט נועד למנוע רגרסיה שקטה שבה מישהו
יסיר את ההרשאה מה-scopes ויחזיר את הבאג.
"""


def test_oauth_scopes_include_business_management():
    from admin.meta_oauth import _OAUTH_SCOPES
    assert "business_management" in _OAUTH_SCOPES.split(",")


def test_oauth_scopes_include_pages_read_engagement():
    """pages_read_engagement נדרש לקריאת השדה instagram_business_account
    מהעמוד. בלעדיו get_ig_business_account נכשל ב-(#100) "requires the
    pages_read_engagement permission" וה-IG לא מתחבר אוטומטית."""
    from admin.meta_oauth import _OAUTH_SCOPES
    assert "pages_read_engagement" in _OAUTH_SCOPES.split(",")


def test_oauth_scopes_include_core_messaging_permissions():
    from admin.meta_oauth import _OAUTH_SCOPES
    scopes = set(_OAUTH_SCOPES.split(","))
    for required in (
        "pages_show_list",
        "pages_manage_metadata",
        "pages_read_engagement",
        "pages_messaging",
        "instagram_basic",
        "instagram_manage_messages",
    ):
        assert required in scopes, f"חסרה הרשאה קריטית: {required}"


def test_oauth_scopes_well_formed():
    """אין scope ריק (פסיק מיותר) ואין רווחים — מטא מצפה ל-CSV נקי."""
    from admin.meta_oauth import _OAUTH_SCOPES
    scopes = _OAUTH_SCOPES.split(",")
    assert all(s.strip() for s in scopes), "scope ריק — בדוק פסיק מיותר"
    assert " " not in _OAUTH_SCOPES, "רווח ב-scopes — מטא דורשת CSV בלי רווחים"
