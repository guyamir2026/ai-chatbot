"""
טסטים ל-messaging/meta_graph_client.py.

`requests` ממוקה ב-conftest.py — כל הטסטים מציבים `return_value`
על `requests.get/post/delete` ומאמתים שה-URL וה-params נכונים.
"""

from unittest.mock import MagicMock

import pytest


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _set_meta_config(monkeypatch):
    """משתני סביבה למטא — נדרשים כדי שהמודול ייטען וייצר URLs."""
    import messaging.meta_graph_client as mgc
    monkeypatch.setattr(mgc, "META_APP_ID", "fake-app-id")
    monkeypatch.setattr(mgc, "META_APP_SECRET", "fake-app-secret")
    monkeypatch.setattr(mgc, "META_GRAPH_API_VERSION", "v21.0")


class TestExchangeCodeForUserToken:
    def test_success_returns_token(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(
            json_data={"access_token": "user-tok"}
        ))
        monkeypatch.setattr(requests, "get", mock_get)

        token = mgc.exchange_code_for_user_token(
            code="auth-code-123",
            redirect_uri="https://example.com/cb",
        )
        assert token == "user-tok"
        # אימות URL ו-params
        args, kwargs = mock_get.call_args
        assert "oauth/access_token" in args[0]
        assert kwargs["params"]["code"] == "auth-code-123"
        assert kwargs["params"]["redirect_uri"] == "https://example.com/cb"
        assert kwargs["params"]["client_id"] == "fake-app-id"
        assert "timeout" in kwargs

    def test_missing_app_id_raises(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        monkeypatch.setattr(mgc, "META_APP_ID", "")
        with pytest.raises(mgc.MetaGraphError, match="META_APP_ID"):
            mgc.exchange_code_for_user_token("c", "u")

    def test_meta_error_response_raises(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(
            status_code=400,
            json_data={"error": {
                "message": "Invalid code", "type": "OAuthException", "code": 100,
            }},
        ))
        monkeypatch.setattr(requests, "get", mock_get)

        with pytest.raises(mgc.MetaGraphError, match="Invalid code"):
            mgc.exchange_code_for_user_token("bad-code", "u")

    def test_empty_token_raises(self, monkeypatch):
        """תגובת 200 בלי access_token היא תקלה."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={}))
        monkeypatch.setattr(requests, "get", mock_get)

        with pytest.raises(mgc.MetaGraphError, match="לא הוחזר"):
            mgc.exchange_code_for_user_token("c", "u")


class TestExchangeForLongLivedUserToken:
    def test_success(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(
            json_data={"access_token": "long-tok"}
        ))
        monkeypatch.setattr(requests, "get", mock_get)

        token = mgc.exchange_for_long_lived_user_token("short-tok")
        assert token == "long-tok"
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["grant_type"] == "fb_exchange_token"
        assert kwargs["params"]["fb_exchange_token"] == "short-tok"


class TestListUserPages:
    def test_returns_pages(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={
            "data": [
                {"id": "P1", "name": "Page 1", "access_token": "pt1", "tasks": []},
                {"id": "P2", "name": "Page 2", "access_token": "pt2", "tasks": []},
            ],
        }))
        monkeypatch.setattr(requests, "get", mock_get)

        pages = mgc.list_user_pages("user-tok")
        assert len(pages) == 2
        assert pages[0]["id"] == "P1"
        assert pages[1]["name"] == "Page 2"

    def test_empty_when_no_pages(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={"data": []}))
        monkeypatch.setattr(requests, "get", mock_get)
        assert mgc.list_user_pages("tok") == []


class TestListBusinessPages:
    """שליפת עמודים תחת תיק עסקי (owned_pages + client_pages)."""

    def test_returns_owned_pages_with_tokens(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests

        def fake_get(url, **kwargs):
            if "owned_pages" in url:
                return _mock_response(json_data={"data": [
                    {"id": "BP1", "name": "Biz Page 1",
                     "access_token": "bt1", "tasks": []},
                ]})
            return _mock_response(json_data={"data": []})

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        pages = mgc.list_business_pages("BIZ_1", "user-tok")
        assert len(pages) == 1
        assert pages[0]["id"] == "BP1"
        assert pages[0]["access_token"] == "bt1"

    def test_combines_owned_and_client_dedup(self, monkeypatch):
        """עמוד שמופיע גם ב-owned וגם ב-client מוחזר פעם אחת, הסדר נשמר."""
        import messaging.meta_graph_client as mgc
        import requests

        def fake_get(url, **kwargs):
            if "owned_pages" in url:
                return _mock_response(json_data={"data": [
                    {"id": "P1", "name": "P1", "access_token": "t1"},
                    {"id": "P2", "name": "P2", "access_token": "t2"},
                ]})
            return _mock_response(json_data={"data": [
                {"id": "P2", "name": "P2", "access_token": "t2"},  # כפילות
                {"id": "P3", "name": "P3", "access_token": "t3"},
            ]})

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        pages = mgc.list_business_pages("BIZ_1", "user-tok")
        assert [p["id"] for p in pages] == ["P1", "P2", "P3"]

    def test_prefers_token_when_page_in_both_edges(self, monkeypatch):
        """עמוד שב-owned_pages בלי token וב-client_pages עם token — מוחזר
        עם ה-token, לא הגרסה הריקה (הערת Bugbot High severity, PR #305)."""
        import messaging.meta_graph_client as mgc
        import requests

        def fake_get(url, **kwargs):
            if "owned_pages" in url:
                return _mock_response(json_data={"data": [
                    {"id": "P1", "name": "P1"},  # owned — בלי token
                ]})
            return _mock_response(json_data={"data": [
                {"id": "P1", "name": "P1", "access_token": "tok"},  # client — עם token
            ]})

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        pages = mgc.list_business_pages("BIZ_1", "user-tok")
        assert len(pages) == 1
        assert pages[0]["access_token"] == "tok"

    def test_partial_failure_continues(self, monkeypatch):
        """כשל ב-owned_pages (Missing Permission) לא מונע מ-client להחזיר."""
        import messaging.meta_graph_client as mgc
        import requests

        def fake_get(url, **kwargs):
            if "owned_pages" in url:
                return _mock_response(status_code=400, json_data={
                    "error": {"message": "Missing Permission", "code": 100}})
            return _mock_response(json_data={"data": [
                {"id": "C1", "name": "Client Page", "access_token": "ct1"},
            ]})

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        pages = mgc.list_business_pages("BIZ_1", "user-tok")
        assert len(pages) == 1
        assert pages[0]["id"] == "C1"

    def test_empty_on_total_failure(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(
            status_code=400, json_data={"error": {"message": "boom", "code": 1}}))
        monkeypatch.setattr(requests, "get", mock_get)
        assert mgc.list_business_pages("BIZ_1", "user-tok") == []

    def test_filters_non_dict_and_idless_items(self, monkeypatch):
        """פריט שאינו dict / בלי id / data שאינו list — מסוננים בלי קריסה."""
        import messaging.meta_graph_client as mgc
        import requests

        def fake_get(url, **kwargs):
            if "owned_pages" in url:
                return _mock_response(json_data={"data": [
                    {"id": "OK", "name": "ok", "access_token": "t"},
                    "not-a-dict",
                    {"name": "no-id"},
                ]})
            return _mock_response(json_data={"data": "not-a-list"})

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        pages = mgc.list_business_pages("BIZ_1", "user-tok")
        assert len(pages) == 1
        assert pages[0]["id"] == "OK"

    def test_queries_both_edges_with_access_token_field(self, monkeypatch):
        """שתי קריאות (owned+client), ו-access_token חייב להיות ב-fields."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={"data": []}))
        monkeypatch.setattr(requests, "get", mock_get)

        mgc.list_business_pages("BIZ_42", "user-tok")
        called_urls = [c.args[0] for c in mock_get.call_args_list]
        assert any("BIZ_42/owned_pages" in u for u in called_urls)
        assert any("BIZ_42/client_pages" in u for u in called_urls)
        for c in mock_get.call_args_list:
            assert "access_token" in c.kwargs["params"]["fields"]


class TestGetIgBusinessAccount:
    def test_with_linked_ig(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={
            "instagram_business_account": {"id": "IG_99", "username": "dana_biz"},
        }))
        monkeypatch.setattr(requests, "get", mock_get)

        ig = mgc.get_ig_business_account("P1", "pt1")
        assert ig == {"id": "IG_99", "username": "dana_biz"}

    def test_without_linked_ig_returns_none(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={}))
        monkeypatch.setattr(requests, "get", mock_get)

        assert mgc.get_ig_business_account("P1", "pt1") is None

    def test_empty_ig_id_returns_none(self, monkeypatch):
        """instagram_business_account עם id ריק — גם None."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={
            "instagram_business_account": {"id": "", "username": ""},
        }))
        monkeypatch.setattr(requests, "get", mock_get)
        assert mgc.get_ig_business_account("P1", "pt1") is None

    def test_fallback_to_connected_when_business_empty(self, monkeypatch):
        """business_account ריק אבל connected_instagram_account קיים — מחזיר connected."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={
            "connected_instagram_account": {"id": "IG_C", "username": "creator_acc"},
        }))
        monkeypatch.setattr(requests, "get", mock_get)
        ig = mgc.get_ig_business_account("P1", "pt1")
        assert ig == {"id": "IG_C", "username": "creator_acc"}

    def test_prefers_business_over_connected(self, monkeypatch):
        """כששני השדות מאוכלסים — business_account מנצח."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_get = MagicMock(return_value=_mock_response(json_data={
            "instagram_business_account": {"id": "IG_B", "username": "biz"},
            "connected_instagram_account": {"id": "IG_C", "username": "creator"},
        }))
        monkeypatch.setattr(requests, "get", mock_get)
        assert mgc.get_ig_business_account("P1", "pt1")["id"] == "IG_B"

    def test_full_fetch_failure_falls_back_to_business_only(self, monkeypatch):
        """אם השליפה עם connected נכשלת (שדה לא נתמך) — נופל ל-business בלבד."""
        import messaging.meta_graph_client as mgc
        import requests
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if "connected_instagram_account" in kwargs["params"]["fields"]:
                return _mock_response(status_code=400, json_data={
                    "error": {"message": "nonexisting field", "code": 100}})
            return _mock_response(json_data={
                "instagram_business_account": {"id": "IG_B", "username": "biz"},
            })

        monkeypatch.setattr(requests, "get", MagicMock(side_effect=fake_get))
        ig = mgc.get_ig_business_account("P1", "pt1")
        assert ig == {"id": "IG_B", "username": "biz"}
        assert calls["n"] == 2  # ניסיון מלא שנכשל + fallback מוצלח


class TestSubscribePageToWebhook:
    def test_success(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_post = MagicMock(return_value=_mock_response(
            json_data={"success": True}
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        mgc.subscribe_page_to_webhook("PAGE_1", "page-tok")
        args, kwargs = mock_post.call_args
        assert "PAGE_1/subscribed_apps" in args[0]
        assert kwargs["params"]["access_token"] == "page-tok"
        assert "messages" in kwargs["params"]["subscribed_fields"]

    def test_failure_raises(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_post = MagicMock(return_value=_mock_response(
            status_code=403,
            json_data={"error": {"message": "Permission denied", "code": 200}},
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        with pytest.raises(mgc.MetaGraphError, match="Permission denied"):
            mgc.subscribe_page_to_webhook("PAGE_1", "page-tok")

    def test_http_200_with_success_false_raises(self, monkeypatch):
        """מטא יכולה להחזיר 200 עם {"success": false} — חייבים לזרוק."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_post = MagicMock(return_value=_mock_response(
            json_data={"success": False}
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        with pytest.raises(mgc.MetaGraphError, match="success=False"):
            mgc.subscribe_page_to_webhook("PAGE_1", "page-tok")

    def test_http_200_with_missing_success_raises(self, monkeypatch):
        """תגובת 200 בלי שדה success — נחשבת ככשל."""
        import messaging.meta_graph_client as mgc
        import requests
        mock_post = MagicMock(return_value=_mock_response(json_data={}))
        monkeypatch.setattr(requests, "post", mock_post)

        with pytest.raises(mgc.MetaGraphError):
            mgc.subscribe_page_to_webhook("PAGE_1", "page-tok")


class TestUnsubscribePageFromWebhook:
    def test_uses_delete(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_delete = MagicMock(return_value=_mock_response(
            json_data={"success": True}
        ))
        monkeypatch.setattr(requests, "delete", mock_delete)

        mgc.unsubscribe_page_from_webhook("PAGE_1", "page-tok")
        assert mock_delete.called
        args, kwargs = mock_delete.call_args
        assert "PAGE_1/subscribed_apps" in args[0]
        assert kwargs["params"]["access_token"] == "page-tok"

    def test_success_false_raises(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        import requests
        mock_delete = MagicMock(return_value=_mock_response(
            json_data={"success": False}
        ))
        monkeypatch.setattr(requests, "delete", mock_delete)

        with pytest.raises(mgc.MetaGraphError, match="success=False"):
            mgc.unsubscribe_page_from_webhook("PAGE_1", "page-tok")


class TestGraphUrl:
    def test_uses_configured_version(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        monkeypatch.setattr(mgc, "META_GRAPH_API_VERSION", "v22.0")
        url = mgc._graph_url("some/path")
        assert "v22.0" in url
        assert url.endswith("some/path")

    def test_default_version_when_empty(self, monkeypatch):
        import messaging.meta_graph_client as mgc
        monkeypatch.setattr(mgc, "META_GRAPH_API_VERSION", "")
        url = mgc._graph_url("oauth/access_token")
        # נופלים ל-v21.0 ברירת מחדל בקוד
        assert "v21.0" in url
