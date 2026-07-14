"""
Twilio Content API — עוזרים משותפים ל-sync ול-runtime.

מרכז את auth + base URL כדי ש-whatsapp_templates.py (יצירה/שליחה) ו-
whatsapp_templates_sync.py (סנכרון) ישתמשו באותו מקור יחיד.
"""

from __future__ import annotations


def get_auth() -> tuple[str, str]:
    """החזרת (account_sid, auth_token) מ-config."""
    from ai_chatbot.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    return TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN


def content_api_url(path: str = "") -> str:
    """בניית URL ל-Twilio Content API."""
    base = "https://content.twilio.com/v1/Content"
    return f"{base}/{path}" if path else base
