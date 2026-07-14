"""
טסטי בידוד state בזיכרון בין tenants (multi-tenant שלב 2, סעיף 5.3 ב-spec).

כל טסט מדמה שני tenants ומוודא שה-state של אחד לא מדלף לשני:
query cache של RAG, ‏vacation cache, ‏rate limiter, מכונת המצבים של
WhatsApp, ‏follow-up store, ‏vector store registry, ‏Twilio resolver.
"""

import time
from unittest.mock import patch

import pytest

from tenancy import DEFAULT_TENANT, tenant_context


class TestQueryCacheIsolation:
    def test_same_question_not_shared_between_tenants(self):
        from rag import engine as eng

        with eng._query_cache_lock:
            eng._query_cache.clear()

        with tenant_context("salon-a"):
            key_a = eng._cache_key("מה המחיר?", 5)
            with eng._query_cache_lock:
                eng._query_cache[key_a] = (time.time(), [{"text": "מחירון של א"}])

        with tenant_context("salon-b"):
            key_b = eng._cache_key("מה המחיר?", 5)
            with eng._query_cache_lock:
                cached = eng._query_cache.get(key_b)

        assert key_a != key_b
        assert cached is None  # אותה שאלה — אבל אין דליפה בין עסקים

        with eng._query_cache_lock:
            eng._query_cache.clear()

    def test_rebuild_clears_only_current_tenant(self, tmp_path):
        from rag import engine as eng

        with eng._query_cache_lock:
            eng._query_cache.clear()
            eng._query_cache[("salon-a", "ש", 5)] = (time.time(), [])
            eng._query_cache[("salon-b", "ש", 5)] = (time.time(), [])

        # מדמים את קטע ניקוי ה-cache שרץ בסוף rebuild עבור salon-a
        with tenant_context("salon-a"):
            _tenant = "salon-a"
            with eng._query_cache_lock:
                for k in [k for k in eng._query_cache if k[0] == _tenant]:
                    del eng._query_cache[k]

        with eng._query_cache_lock:
            assert ("salon-a", "ש", 5) not in eng._query_cache
            assert ("salon-b", "ש", 5) in eng._query_cache
            eng._query_cache.clear()


class TestVacationCacheIsolation:
    def test_vacation_of_one_tenant_not_served_to_other(self, tmp_path):
        import vacation_service as vs

        vs.VacationService._cache.clear()
        with patch.object(vs, "db") as mock_db:
            mock_db.get_vacation_mode.return_value = {"is_active": 1}
            with tenant_context("salon-a"):
                assert vs.VacationService.is_active() is True

            # ה-tenant השני קורא DB בעצמו (miss) — לא את ה-cache של הראשון
            mock_db.get_vacation_mode.return_value = {"is_active": 0}
            with tenant_context("salon-b"):
                assert vs.VacationService.is_active() is False

            # ובתוך ה-TTL — כל אחד שומר על הערך שלו
            mock_db.get_vacation_mode.side_effect = AssertionError("must hit cache")
            with tenant_context("salon-a"):
                assert vs.VacationService.is_active() is True
            with tenant_context("salon-b"):
                assert vs.VacationService.is_active() is False
        vs.VacationService._cache.clear()


class TestRateLimiterIsolation:
    def test_same_user_id_counted_separately_per_tenant(self):
        import rate_limiter as rl

        rl._user_timestamps.clear()
        user = "+972500000001"
        with tenant_context("salon-a"):
            for _ in range(5):
                rl.record_message(user)
        with tenant_context("salon-b"):
            rl.record_message(user)

        assert len(rl._user_timestamps[("salon-a", user)]) == 5
        assert len(rl._user_timestamps[("salon-b", user)]) == 1
        rl._user_timestamps.clear()


class TestConversationStateIsolation:
    def test_booking_flow_state_per_tenant(self):
        from messaging import conversation_state as cs

        cs._sessions.clear()
        user = "+972500000001"
        with tenant_context("salon-a"):
            cs.set_state(user, cs.STATE_BOOKING_DATE, {"service": "תספורת"})
        with tenant_context("salon-b"):
            assert cs.get_state(user) is None  # אין דליפת flow בין עסקים
            cs.set_state(user, cs.STATE_BOOKING_SERVICE)
        with tenant_context("salon-a"):
            state = cs.get_state(user)
            assert state["state"] == cs.STATE_BOOKING_DATE
            assert cs.get_session_data(user, "service") == "תספורת"
            cs.clear_state(user)
            assert cs.get_state(user) is None
        with tenant_context("salon-b"):
            assert cs.get_state(user)["state"] == cs.STATE_BOOKING_SERVICE
        cs._sessions.clear()


class TestFollowUpStoreIsolation:
    def test_follow_up_questions_per_tenant(self):
        from messaging import whatsapp_webhook as wh

        wh._follow_up_store.clear()
        user = "+972500000001"
        with tenant_context("salon-a"):
            wh._follow_up_store[wh._follow_up_key(user)] = ["שאלה של א"]
        with tenant_context("salon-b"):
            assert wh._follow_up_store.get(wh._follow_up_key(user), []) == []
        wh._follow_up_store.clear()


class TestVectorStoreRegistry:
    def test_each_tenant_gets_own_store(self, tmp_path):
        from rag import vector_store as vsm

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.FAISS_INDEX_PATH", tmp_path / "faiss"):
            vsm.reset_vector_store(all_tenants=True)
            with tenant_context("salon-a"):
                store_a = vsm.get_vector_store()
                store_a_again = vsm.get_vector_store()
            with tenant_context("salon-b"):
                store_b = vsm.get_vector_store()

            assert store_a is store_a_again  # יציבות בתוך tenant
            assert store_a is not store_b    # הפרדה בין tenants

            # reset של ה-tenant הנוכחי לא מפיל את השני
            with tenant_context("salon-a"):
                vsm.reset_vector_store()
                assert vsm.get_vector_store() is not store_a
            with tenant_context("salon-b"):
                assert vsm.get_vector_store() is store_b
            vsm.reset_vector_store(all_tenants=True)

    def test_lru_eviction_bounded(self, tmp_path):
        from rag import vector_store as vsm

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.FAISS_INDEX_PATH", tmp_path / "faiss"), \
             patch.object(vsm, "_MAX_HOT_STORES", 3):
            vsm.reset_vector_store(all_tenants=True)
            for i in range(6):
                with tenant_context(f"t{i}"):
                    vsm.get_vector_store()
            assert len(vsm._stores) == 3  # התקרה נאכפת
            vsm.reset_vector_store(all_tenants=True)


class TestTwilioResolver:
    def test_default_tenant_uses_env(self, monkeypatch):
        from messaging import whatsapp_sender as ws

        monkeypatch.setattr("ai_chatbot.config.TWILIO_ACCOUNT_SID", "AC-env")
        monkeypatch.setattr("ai_chatbot.config.TWILIO_AUTH_TOKEN", "tok-env")
        monkeypatch.setattr("ai_chatbot.config.TWILIO_WHATSAPP_NUMBER", "+1000")
        sid, token, number = ws._resolve_twilio_settings()
        assert (sid, token, number) == ("AC-env", "tok-env", "+1000")

    def test_other_tenant_never_falls_back_to_env(self, tmp_path, monkeypatch):
        """קריטי: tenant בלי סודות רשומים לא שולח בזהות (וחיוב) ה-env."""
        from messaging import whatsapp_sender as ws

        monkeypatch.setattr("ai_chatbot.config.TWILIO_ACCOUNT_SID", "AC-env")
        with patch("ai_chatbot.config.DATA_DIR", tmp_path):
            with tenant_context("salon-a"):
                with pytest.raises(RuntimeError):
                    ws._resolve_twilio_settings()

    def test_other_tenant_uses_own_secrets(self, tmp_path):
        import control_plane as cp
        from messaging import whatsapp_sender as ws

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            cp.create_tenant("salon-a", "א")
            cp.set_tenant_secret("salon-a", "twilio_account_sid", "AC-a")
            cp.set_tenant_secret("salon-a", "twilio_auth_token", "tok-a")
            cp.set_tenant_secret("salon-a", "twilio_whatsapp_number", "+2000")
            with tenant_context("salon-a"):
                assert ws._resolve_twilio_settings() == ("AC-a", "tok-a", "+2000")
            cp.invalidate_status_cache()


class TestSchedulerIteration:
    def test_broadcast_loop_covers_all_active_tenants(self, tmp_path):
        """הלולאה קוראת ל-_process_due_campaigns פעם לכל tenant פעיל,
        וכשל אצל אחד לא עוצר את השאר."""
        import control_plane as cp
        from messaging import broadcast_scheduler as bs

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            cp.create_tenant("salon-a", "א")
            cp.create_tenant("salon-b", "ב")
            cp.create_tenant("salon-c", "ג")
            cp.set_tenant_status("salon-b", "suspended")

            seen: list[str] = []

            def fake_process():
                from tenancy import get_current_tenant

                tenant = get_current_tenant()
                seen.append(tenant)
                if tenant == "salon-a":
                    raise RuntimeError("boom")  # כשל אצל א' לא עוצר את ג'

            # ריצה אחת של גוף הלולאה: stop כבר set אחרי האיטרציה הראשונה
            with patch.object(bs, "_process_due_campaigns", side_effect=fake_process):
                bs._scheduler_stop.clear()
                orig_wait = bs._scheduler_stop.wait

                def stop_after_first(timeout=None):
                    bs._scheduler_stop.set()
                    return True

                with patch.object(bs._scheduler_stop, "wait", stop_after_first):
                    bs._scheduler_loop()
                bs._scheduler_stop.set()

            assert seen == ["salon-a", "salon-c"]  # מושעה לא נכלל; כשל לא עצר
            cp.invalidate_status_cache()

    def test_loop_falls_back_to_default_without_registry(self, tmp_path):
        from messaging import broadcast_scheduler as bs

        with patch("ai_chatbot.config.DATA_DIR", tmp_path):
            seen: list[str] = []

            def fake_process():
                from tenancy import get_current_tenant

                seen.append(get_current_tenant())

            with patch.object(bs, "_process_due_campaigns", side_effect=fake_process):
                bs._scheduler_stop.clear()

                def stop_after_first(timeout=None):
                    bs._scheduler_stop.set()
                    return True

                with patch.object(bs._scheduler_stop, "wait", stop_after_first):
                    bs._scheduler_loop()
                bs._scheduler_stop.set()

            assert seen == [DEFAULT_TENANT]
