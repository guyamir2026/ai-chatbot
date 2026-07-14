"""
טסטים ל-Web Push notifications — notifications/push_service.py + database CRUD.

הטסטים מוודאים:
1. CRUD על push_subscriptions (UNIQUE על endpoint, upsert, delete, touch).
2. notify_live_chat_message — no-op כש-VAPID חסר.
3. notify_live_chat_message — שולח לכל המנויים כש-VAPID מוגדר.
4. מחיקה אוטומטית של מנוי כש-push service מחזיר 410.
"""

from unittest.mock import MagicMock, patch

import pytest


# ─── CRUD ───────────────────────────────────────────────────────────────────


class TestPushSubscriptionCRUD:
    def test_upsert_and_get(self, db_conn):
        import database as db
        db.upsert_push_subscription(
            "https://fcm.googleapis.com/fcm/send/abc123",
            "p256dh_key_a",
            "auth_key_a",
            "Mozilla/5.0",
        )
        subs = db.get_all_push_subscriptions()
        assert len(subs) == 1
        assert subs[0]["endpoint"] == "https://fcm.googleapis.com/fcm/send/abc123"
        assert subs[0]["p256dh"] == "p256dh_key_a"
        assert subs[0]["auth"] == "auth_key_a"

    def test_upsert_same_endpoint_replaces_keys(self, db_conn):
        """UNIQUE constraint על endpoint — קריאה שנייה דורסת keys ישנים."""
        import database as db
        endpoint = "https://example.com/push/xyz"
        db.upsert_push_subscription(endpoint, "old_p256", "old_auth")
        db.upsert_push_subscription(endpoint, "new_p256", "new_auth")
        subs = db.get_all_push_subscriptions()
        assert len(subs) == 1
        assert subs[0]["p256dh"] == "new_p256"
        assert subs[0]["auth"] == "new_auth"

    def test_multiple_endpoints(self, db_conn):
        import database as db
        db.upsert_push_subscription("https://a.example/x", "p1", "a1")
        db.upsert_push_subscription("https://b.example/y", "p2", "a2")
        subs = db.get_all_push_subscriptions()
        assert len(subs) == 2

    def test_delete(self, db_conn):
        import database as db
        endpoint = "https://to-delete.example/p"
        db.upsert_push_subscription(endpoint, "p", "a")
        db.delete_push_subscription(endpoint)
        assert db.get_all_push_subscriptions() == []

    def test_delete_nonexistent_is_safe(self, db_conn):
        import database as db
        db.delete_push_subscription("https://nothing.example/x")  # לא מתפוצץ

    def test_touch_updates_last_used(self, db_conn):
        import database as db
        endpoint = "https://touch.example/q"
        db.upsert_push_subscription(endpoint, "p", "a")
        # לפני touch — last_used_at הוא NULL
        row = db_conn.execute(
            "SELECT last_used_at FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        assert row["last_used_at"] is None
        db.touch_push_subscription(endpoint)
        row = db_conn.execute(
            "SELECT last_used_at FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        assert row["last_used_at"] is not None


# ─── notify_live_chat_message ───────────────────────────────────────────────


@pytest.fixture
def push_module(db_conn, monkeypatch):
    """מודול push_service טעון מחדש עם VAPID מוגדר ו-pywebpush ממוקר.

    אנחנו לא מעמיסים את pywebpush האמיתי — patching ה-import בתוך הפונקציה
    מאפשר לטסטים לרוץ גם בלי החבילה מותקנת.
    """
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public-key")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-key")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    import importlib
    import config as _config
    importlib.reload(_config)
    import notifications.push_service as push_service
    importlib.reload(push_service)
    return push_service


def test_notify_no_subscriptions_is_no_op(push_module, db_conn):
    """אין מנויים → לא קוראים ל-pywebpush בכלל."""
    fake_webpush = MagicMock()
    fake_module = MagicMock(webpush=fake_webpush, WebPushException=Exception)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_module.notify_live_chat_message("user-1", "Alice", "hello")
    fake_webpush.assert_not_called()


def test_notify_sends_to_all_subscriptions(push_module, db_conn):
    """קוראים ל-webpush פעם אחת לכל מנוי, עם payload נכון."""
    import database as db
    db.upsert_push_subscription("https://a.example/x", "p1", "a1")
    db.upsert_push_subscription("https://b.example/y", "p2", "a2")

    fake_webpush = MagicMock()
    fake_module = MagicMock(webpush=fake_webpush, WebPushException=Exception)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_module.notify_live_chat_message("+972501234567", "דנה", "שלום, מה השעה?")

    assert fake_webpush.call_count == 2
    # בדיקת payload — title=display_name, url מכיל user_id מוצפן (`+` → %2B)
    call_kwargs = fake_webpush.call_args_list[0].kwargs
    import json as _json
    payload = _json.loads(call_kwargs["data"])
    assert payload["title"] == "דנה"
    assert payload["body"] == "שלום, מה השעה?"
    assert "%2B972501234567" in payload["url"]
    assert payload["tag"] == "live-chat-+972501234567"


def test_notify_truncates_long_body(push_module, db_conn):
    import database as db
    db.upsert_push_subscription("https://a.example/x", "p", "a")
    long_text = "א" * 200

    fake_webpush = MagicMock()
    fake_module = MagicMock(webpush=fake_webpush, WebPushException=Exception)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_module.notify_live_chat_message("u", "User", long_text)

    import json as _json
    payload = _json.loads(fake_webpush.call_args.kwargs["data"])
    # 80 תווים + סופית "…" (תו אחד)
    assert len(payload["body"]) <= 81
    assert payload["body"].endswith("…")


def test_notify_removes_subscription_on_410(push_module, db_conn):
    """410 Gone = המשתמש ביטל הרשאה. המנוי נמחק כדי לא לנסות שוב."""
    import database as db
    db.upsert_push_subscription("https://gone.example/x", "p", "a")

    class _FakeResponse:
        status_code = 410

    class _FakeWebPushException(Exception):
        def __init__(self):
            super().__init__("Gone")
            self.response = _FakeResponse()

    def _fake_webpush(**kwargs):
        raise _FakeWebPushException()

    fake_module = MagicMock(webpush=_fake_webpush, WebPushException=_FakeWebPushException)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_module.notify_live_chat_message("u", "User", "hi")

    assert db.get_all_push_subscriptions() == []


def test_notify_keeps_subscription_on_transient_error(push_module, db_conn):
    """500 = בעיה זמנית בשירות ה-push. לא מוחקים — ננסה שוב בפעם הבאה."""
    import database as db
    db.upsert_push_subscription("https://keep.example/x", "p", "a")

    class _FakeResponse:
        status_code = 500

    class _FakeWebPushException(Exception):
        def __init__(self):
            super().__init__("Server error")
            self.response = _FakeResponse()

    def _fake_webpush(**kwargs):
        raise _FakeWebPushException()

    fake_module = MagicMock(webpush=_fake_webpush, WebPushException=_FakeWebPushException)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_module.notify_live_chat_message("u", "User", "hi")

    assert len(db.get_all_push_subscriptions()) == 1


def test_notify_disabled_without_vapid(db_conn, monkeypatch):
    """בלי VAPID — פשוט return, אין קריאה ל-pywebpush."""
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
    monkeypatch.setenv("VAPID_SUBJECT", "")
    import importlib
    import config as _config
    importlib.reload(_config)
    import notifications.push_service as push_service
    importlib.reload(push_service)

    import database as db
    db.upsert_push_subscription("https://a.example/x", "p", "a")

    fake_webpush = MagicMock()
    fake_module = MagicMock(webpush=fake_webpush, WebPushException=Exception)
    with patch.dict("sys.modules", {"pywebpush": fake_module}):
        push_service.notify_live_chat_message("u", "User", "hi")

    fake_webpush.assert_not_called()


# ─── VAPID keygen ───────────────────────────────────────────────────────────


class TestVapidKeygen:
    def test_generates_valid_keypair(self):
        """המפתחות שנוצרים תואמים לאורכים המצופים: 32B private, 65B public."""
        from utils.vapid_keygen import generate_vapid_keypair
        import base64

        public_b64, private_b64 = generate_vapid_keypair()

        def _b64url_decode(s: str) -> bytes:
            padding = "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s + padding)

        private_bytes = _b64url_decode(private_b64)
        public_bytes = _b64url_decode(public_b64)
        assert len(private_bytes) == 32
        assert len(public_bytes) == 65  # uncompressed P-256 point
        assert public_bytes[0] == 0x04  # uncompressed marker
