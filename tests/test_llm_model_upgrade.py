"""
טסטים ל-get_llm_model — מודל LLM כשדרוג פר-חבילה (multi-tenant).

מכסים: fallback ל-env כברירת מחדל (אפס שינוי התנהגות), מודל סטטי
בהגדרת החבילה, override ב-env פר-חבילה, ובחירה לפי חבילת ה-tenant.
"""

from unittest.mock import patch

import feature_flags as ff
import plans_config


class TestGetLlmModel:
    def test_default_falls_back_to_env(self, db_conn):
        """בלי מודל בחבילה — נופל ל-OPENAI_MODEL (התנהגות קיימת)."""
        with patch("ai_chatbot.config.OPENAI_MODEL", "gpt-4.1-mini"):
            assert ff.get_llm_model() == "gpt-4.1-mini"

    def test_static_plan_model_used(self, db_conn):
        ff.set_plan("premium")
        with patch("ai_chatbot.config.OPENAI_MODEL", "gpt-4.1-mini"), \
             patch.dict(plans_config.PLANS["premium"], {"llm_model": "gpt-4.1"}):
            assert ff.get_llm_model() == "gpt-4.1"

    def test_env_override_wins_over_static(self, db_conn, monkeypatch):
        ff.set_plan("premium")
        monkeypatch.setenv("PLAN_LLM_MODEL_PREMIUM", "gpt-5-turbo")
        with patch("ai_chatbot.config.OPENAI_MODEL", "gpt-4.1-mini"), \
             patch.dict(plans_config.PLANS["premium"], {"llm_model": "gpt-4.1"}):
            assert ff.get_llm_model() == "gpt-5-turbo"

    def test_model_follows_tenant_plan(self, db_conn, monkeypatch):
        """חבילות שונות ⇒ מודלים שונים (השדרוג בפועל)."""
        monkeypatch.setenv("PLAN_LLM_MODEL_BASIC", "gpt-4.1-mini")
        monkeypatch.setenv("PLAN_LLM_MODEL_PREMIUM", "gpt-4.1")
        ff.set_plan("basic")
        assert ff.get_llm_model() == "gpt-4.1-mini"
        ff.set_plan("premium")
        assert ff.get_llm_model() == "gpt-4.1"

    def test_fail_open_on_plan_error(self, db_conn):
        """כשל בקריאת החבילה ⇒ ברירת המחדל מ-env, לא חריגה."""
        with patch("ai_chatbot.config.OPENAI_MODEL", "gpt-4.1-mini"), \
             patch("feature_flags.get_current_plan", side_effect=RuntimeError("db down")):
            assert ff.get_llm_model() == "gpt-4.1-mini"

    def test_all_plans_have_llm_model_field(self):
        """כל חבילה מגדירה את השדה (ריק = ברירת מחדל) — אין KeyError."""
        for plan_name, plan_def in plans_config.PLANS.items():
            assert "llm_model" in plan_def, plan_name


class TestPerTenantModelIsolation:
    def test_two_tenants_different_models(self, tmp_path, monkeypatch):
        """שני tenants על חבילות שונות ⇒ כל אחד מקבל את המודל שלו."""
        import control_plane as cp
        from tenancy import tenant_context

        monkeypatch.setenv("PLAN_LLM_MODEL_BASIC", "model-basic")
        monkeypatch.setenv("PLAN_LLM_MODEL_PREMIUM", "model-premium")
        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            from ai_chatbot import database as db
            db.init_db()
            cp.create_tenant("salon-basic", "א", plan="basic")
            cp.create_tenant("salon-prem", "ב", plan="premium")

            with tenant_context("salon-basic"):
                assert ff.get_llm_model() == "model-basic"
            with tenant_context("salon-prem"):
                assert ff.get_llm_model() == "model-premium"
            cp.invalidate_status_cache()
