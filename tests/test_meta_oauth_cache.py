"""
טסטים ל-OAuth cache פנים-תהליכי ב-admin/meta_oauth.py.

המוטיבציה: page access tokens לא צריכים לחיות ב-Flask session
(שזה signed cookie לא מוצפן). הם חיים ב-_pending_oauth_cache
ובצד ה-session הולך רק nonce.
"""

import time

import pytest


@pytest.fixture(autouse=True)
def _clean_cache():
    """מנקה את ה-cache בין טסטים — חי במודול גלובלי."""
    import admin.meta_oauth as mo
    mo._pending_oauth_cache.clear()
    yield
    mo._pending_oauth_cache.clear()


class TestCachePendingPages:
    def test_stores_and_retrieves(self):
        from admin.meta_oauth import _cache_pending_pages, _get_pending_page
        pages = [
            {"id": "P1", "name": "Page 1", "access_token": "tok1"},
            {"id": "P2", "name": "Page 2", "access_token": "tok2"},
        ]
        nonce = _cache_pending_pages(pages)
        assert isinstance(nonce, str)
        assert len(nonce) > 20  # secrets.token_urlsafe(24) → 32+ chars

        p = _get_pending_page(nonce, "P2")
        assert p["access_token"] == "tok2"
        assert p["name"] == "Page 2"

    def test_unknown_nonce_returns_none(self):
        from admin.meta_oauth import _get_pending_page
        assert _get_pending_page("nope", "P1") is None

    def test_unknown_page_in_known_nonce_returns_none(self):
        from admin.meta_oauth import _cache_pending_pages, _get_pending_page
        nonce = _cache_pending_pages([{"id": "P1", "name": "x", "access_token": "t"}])
        assert _get_pending_page(nonce, "P_OTHER") is None

    def test_drop_pending_removes(self):
        from admin.meta_oauth import (
            _cache_pending_pages,
            _drop_pending,
            _get_pending_page,
        )
        nonce = _cache_pending_pages([{"id": "P1", "name": "x", "access_token": "t"}])
        _drop_pending(nonce)
        assert _get_pending_page(nonce, "P1") is None

    def test_ttl_expires(self, monkeypatch):
        """אחרי TTL — ה-entry נמחק אוטומטית בקריאה הבאה."""
        import admin.meta_oauth as mo
        # מקצרים את ה-TTL ל-0.1 שנייה לטסט
        monkeypatch.setattr(mo, "_PENDING_OAUTH_TTL_SEC", 0.1)
        nonce = mo._cache_pending_pages([{"id": "P1", "name": "x", "access_token": "t"}])
        # מיד — קיים
        assert mo._get_pending_page(nonce, "P1") is not None
        # אחרי TTL — נעלם
        time.sleep(0.15)
        assert mo._get_pending_page(nonce, "P1") is None

    def test_each_call_generates_unique_nonce(self):
        from admin.meta_oauth import _cache_pending_pages
        n1 = _cache_pending_pages([{"id": "P1", "name": "x", "access_token": "t"}])
        n2 = _cache_pending_pages([{"id": "P1", "name": "x", "access_token": "t"}])
        assert n1 != n2

    def test_gc_cleans_expired_entries(self, monkeypatch):
        """_gc_pending_oauth מסיר entries ישנים, משאיר חדשים."""
        import admin.meta_oauth as mo
        # יוצרים entry "ישן" ידנית
        mo._pending_oauth_cache["OLD"] = {
            "stored_at": time.time() - 10_000,
            "pages": [],
        }
        # ו-entry חדש
        n = mo._cache_pending_pages([{"id": "P", "name": "x", "access_token": "t"}])

        # ניקוי מתבצע בכל get
        mo._get_pending_page(n, "P")
        assert "OLD" not in mo._pending_oauth_cache
        assert n in mo._pending_oauth_cache
