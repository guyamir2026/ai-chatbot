"""
טסטים ל-Telegram multi-bot runtime (multi-tenant שלב 2, spec 6.1).

מכסים: ‏resolve של טוקנים פר-tenant, ‏bot_state רב-tenant, האתחול העצל
של אפליקציות ב-bot_registry, ‏dispatch תחת tenant context, ה-route של
Flask (מפתח + secret), השליחה היוצאת ב-live_chat וה-CLI.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bot_registry
import bot_state
import control_plane as cp
from tenancy import DEFAULT_TENANT, tenant_context


@pytest.fixture
def platform_env(tmp_path):
    """סביבת פלטפורמה עם tenant מחובר-טלגרם + איפוס ה-registries."""
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        bot_registry.reset_registry()
        bot_state.reset_state()
        cp.create_tenant("salon-a", "מספרת דנה")
        cp.set_tenant_secret("salon-a", "telegram_bot_token", "tg-token-a")
        cp.set_tenant_secret("salon-a", "telegram_webhook_secret", "whsec-a")
        cp.set_route("telegram_webhook_key", "tgkey-a", "salon-a")
        yield tmp_path
        bot_registry.reset_registry()
        bot_state.reset_state()
        cp.invalidate_status_cache()


class TestTokenResolver:
    def test_default_uses_env_dynamically(self, platform_env):
        with patch("ai_chatbot.config.TELEGRAM_BOT_TOKEN", "env-token"):
            assert bot_registry.resolve_telegram_token() == "env-token"

    def test_tenant_uses_own_secret(self, platform_env):
        assert bot_registry.resolve_telegram_token("salon-a") == "tg-token-a"
        with tenant_context("salon-a"):
            assert bot_registry.resolve_telegram_token() == "tg-token-a"

    def test_tenant_without_token_gets_empty_not_env(self, platform_env):
        """‏tenant בלי טוקן לא נופל ל-env — אין שליחה בזהות עסק אחר."""
        cp.create_tenant("salon-b", "ב")
        with patch("ai_chatbot.config.TELEGRAM_BOT_TOKEN", "env-token"):
            assert bot_registry.resolve_telegram_token("salon-b") == ""


class TestBotState:
    def test_legacy_set_bot_registers_default(self, platform_env):
        fake_bot, fake_loop = MagicMock(), MagicMock()
        bot_state.set_bot(fake_bot, fake_loop)
        assert bot_state.get_bot() is fake_bot  # בלי context ⇒ default
        assert bot_state.get_loop() is fake_loop

    def test_tenant_bot_isolated(self, platform_env):
        default_bot, tenant_bot = MagicMock(), MagicMock()
        bot_state.set_bot(default_bot, MagicMock())
        bot_state.register_tenant_bot("salon-a", tenant_bot)

        assert bot_state.get_bot() is default_bot
        with tenant_context("salon-a"):
            assert bot_state.get_bot() is tenant_bot
        with tenant_context("salon-b"):
            # ‏tenant בלי בוט רשום — None, לא הבוט של אחרים
            assert bot_state.get_bot() is None

        bot_state.unregister_tenant_bot("salon-a")
        with tenant_context("salon-a"):
            assert bot_state.get_bot() is None


def _fake_ptb_app():
    app = MagicMock()
    app.initialize = AsyncMock()
    app.shutdown = AsyncMock()
    app.process_update = AsyncMock()
    app.bot = MagicMock()
    return app


class TestEnsureTenantApplication:
    def test_lazy_init_once_and_registered(self, platform_env):
        fake_app = _fake_ptb_app()
        with patch("bot.telegram_bot.create_tenant_bot_application",
                   return_value=fake_app) as m_create:
            app1 = asyncio.run(bot_registry.ensure_tenant_application("salon-a"))
            app2 = asyncio.run(bot_registry.ensure_tenant_application("salon-a"))

        assert app1 is fake_app and app2 is fake_app
        m_create.assert_called_once_with("tg-token-a")
        fake_app.initialize.assert_awaited_once()
        with tenant_context("salon-a"):
            assert bot_state.get_bot() is fake_app.bot

    def test_no_token_returns_none(self, platform_env):
        cp.create_tenant("salon-b", "ב")
        app = asyncio.run(bot_registry.ensure_tenant_application("salon-b"))
        assert app is None

    def test_dispatch_runs_under_tenant_context(self, platform_env):
        fake_app = _fake_ptb_app()
        seen = {}

        async def record_update(update):
            from tenancy import get_current_tenant

            seen["tenant"] = get_current_tenant()

        fake_app.process_update = AsyncMock(side_effect=record_update)
        with patch("bot.telegram_bot.create_tenant_bot_application",
                   return_value=fake_app):
            asyncio.run(
                bot_registry.dispatch_tenant_update("salon-a", {"update_id": 1})
            )
        assert seen["tenant"] == "salon-a"
        fake_app.process_update.assert_awaited_once()

    def test_shutdown_clears_registry(self, platform_env):
        fake_app = _fake_ptb_app()
        with patch("bot.telegram_bot.create_tenant_bot_application",
                   return_value=fake_app):
            asyncio.run(bot_registry.ensure_tenant_application("salon-a"))
        asyncio.run(bot_registry.shutdown_tenant_applications())
        fake_app.shutdown.assert_awaited_once()
        with tenant_context("salon-a"):
            assert bot_state.get_bot() is None


def _make_app():
    """אפליקציית אדמין לטסט — patch על קבועי ה-auth הקפואים (ראה
    test_channel_routing על אותו דפוס)."""
    import admin.app as admin_app

    with patch.object(admin_app, "ADMIN_SECRET_KEY", "test-secret"), \
         patch.object(admin_app, "ADMIN_USERNAME", "admin"), \
         patch.object(admin_app, "ADMIN_PASSWORD", "pw"):
        app = admin_app.create_admin_app()
    app.config["TESTING"] = True
    return app


class TestTelegramTenantRoute:
    def test_unknown_key_404(self, platform_env):
        client = _make_app().test_client()
        resp = client.post("/telegram/webhook/t/no-such-key", json={"update_id": 1})
        assert resp.status_code == 404

    def test_missing_or_bad_secret_403(self, platform_env):
        client = _make_app().test_client()
        # בלי header
        resp = client.post("/telegram/webhook/t/tgkey-a", json={"update_id": 1})
        assert resp.status_code == 403
        # עם secret שגוי
        resp = client.post(
            "/telegram/webhook/t/tgkey-a",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_tenant_without_secret_fails_closed(self, platform_env):
        """‏tenant בלי webhook secret רשום — הבקשה נדחית (fail-closed)."""
        cp.create_tenant("salon-b", "ב")
        cp.set_route("telegram_webhook_key", "tgkey-b", "salon-b")
        client = _make_app().test_client()
        resp = client.post(
            "/telegram/webhook/t/tgkey-b",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": ""},
        )
        assert resp.status_code == 403

    def test_no_bot_loop_503(self, platform_env):
        client = _make_app().test_client()
        resp = client.post(
            "/telegram/webhook/t/tgkey-a",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "whsec-a"},
        )
        assert resp.status_code == 503

    def test_valid_request_dispatches_to_loop(self, platform_env):
        """‏E2E של ה-route: מפתח+secret תקינים ⇒ ה-dispatch רץ על הלולאה
        תחת ה-tenant הנכון (עם לולאת asyncio אמיתית ב-thread, כמו בפרודקשן)."""
        seen = {}
        done = threading.Event()

        async def fake_dispatch(tenant, data):
            seen["tenant"] = tenant
            seen["update"] = data
            done.set()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            flask_app = _make_app()
            flask_app.config["_bot_loop"] = loop
            client = flask_app.test_client()
            with patch("bot_registry.dispatch_tenant_update",
                       side_effect=fake_dispatch):
                resp = client.post(
                    "/telegram/webhook/t/tgkey-a",
                    json={"update_id": 7},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "whsec-a"},
                )
            assert resp.status_code == 200
            assert done.wait(timeout=5), "dispatch לא רץ על הלולאה"
            assert seen["tenant"] == "salon-a"
            assert seen["update"]["update_id"] == 7
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()


class TestLiveChatOutbound:
    def test_send_telegram_uses_tenant_token(self, platform_env):
        import live_chat_service as lcs

        sent = {}

        def fake_post(url, **kwargs):
            sent["url"] = url
            resp = MagicMock()
            resp.ok = True
            return resp

        with patch.object(lcs.http_requests, "post", side_effect=fake_post):
            with tenant_context("salon-a"):
                assert lcs.send_telegram_message("123", "שלום") is True
        assert "bottg-token-a/" in sent["url"]

    def test_send_telegram_tenant_without_token_noop(self, platform_env):
        import live_chat_service as lcs

        cp.create_tenant("salon-b", "ב")
        with patch.object(lcs.http_requests, "post") as m_post:
            with tenant_context("salon-b"):
                assert lcs.send_telegram_message("123", "שלום") is False
        m_post.assert_not_called()


class TestConnectTelegramCli:
    def test_connect_creates_key_secret_and_syncs(self, platform_env, monkeypatch):
        import platform_cli

        monkeypatch.setattr("ai_chatbot.config.ADMIN_URL", "https://app.example.com")
        synced = {}

        async def fake_sync(slug, url, secret):
            synced.update(slug=slug, url=url, secret=secret)

        cp.create_tenant("salon-c", "ג")
        cp.set_tenant_secret("salon-c", "telegram_bot_token", "tg-token-c")

        with patch("bot_registry.sync_telegram_webhook", side_effect=fake_sync):
            assert platform_cli.main(["connect-telegram", "salon-c"]) == 0

        key = cp.get_tenant_route_key("salon-c", "telegram_webhook_key")
        secret = cp.get_tenant_secret("salon-c", "telegram_webhook_secret")
        assert key and secret
        assert synced["slug"] == "salon-c"
        assert synced["url"] == f"https://app.example.com/telegram/webhook/t/{key}"
        assert synced["secret"] == secret

        # אידמפוטנטי — ריצה שנייה לא מחליפה מפתחות
        with patch("bot_registry.sync_telegram_webhook", side_effect=fake_sync):
            assert platform_cli.main(["connect-telegram", "salon-c"]) == 0
        assert cp.get_tenant_route_key("salon-c", "telegram_webhook_key") == key
        assert cp.get_tenant_secret("salon-c", "telegram_webhook_secret") == secret

    def test_connect_without_token_fails_with_hint(self, platform_env, capsys):
        import platform_cli

        cp.create_tenant("salon-d", "ד")
        assert platform_cli.main(["connect-telegram", "salon-d"]) == 1
        assert "telegram_bot_token" in capsys.readouterr().out
