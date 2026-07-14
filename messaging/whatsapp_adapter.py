"""
WhatsAppAdapter — מימוש MessageAdapter עבור WhatsApp דרך Twilio SDK.

Twilio SDK הוא סינכרוני — כל הקריאות עטופות ב-asyncio.to_thread.
כפתורים לא נתמכים ב-API הרגיל — fallback לטקסט מספרי.
"""

import asyncio
import logging
from typing import Optional

from messaging.base import MessageAdapter
from messaging.formatter import format_message

logger = logging.getLogger(__name__)


class WhatsAppAdapter(MessageAdapter):
    """Adapter לשליחת הודעות דרך Twilio WhatsApp API."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        whatsapp_number: str,
    ) -> None:
        from twilio.rest import Client

        self.client = Client(account_sid, auth_token)
        self.from_number = f"whatsapp:{whatsapp_number}"

    def _resolve_send_to(self, chat_id: str) -> str:
        """תרגום chat_id לכתובת שליחה — reverse lookup אם ה-id הוא BSUID.

        אם אין מספר טלפון — שולח ישירות ל-BSUID (Twilio תומך ב-to=whatsapp:IL.BSUID).
        CC = קוד מדינה ISO alpha-2 (IL, US, BR).
        """
        from messaging.whatsapp_sender import _is_phone_number
        if _is_phone_number(chat_id):
            return f"whatsapp:{chat_id}"
        from utils.user_identity import get_whatsapp_send_address
        phone = get_whatsapp_send_address(chat_id)
        return f"whatsapp:{phone or chat_id}"

    async def send_text(
        self,
        chat_id: str,
        text: str,
        buttons: Optional[list[str]] = None,
    ) -> None:
        """שליחת הודעת טקסט. כפתורים ממירים לטקסט מספרי."""
        formatted = format_message(text, "whatsapp")

        # כפתורים — fallback לטקסט מספרי (Twilio לא תומך בכפתורים ב-API הרגיל)
        if buttons:
            lines = [formatted, ""]
            for i, label in enumerate(buttons, 1):
                lines.append(f"{i}. {label}")
            lines.append("")
            lines.append("(שלחו את המספר)")
            formatted = "\n".join(lines)

        send_to = self._resolve_send_to(chat_id)
        await asyncio.to_thread(
            self.client.messages.create,
            body=formatted,
            from_=self.from_number,
            to=send_to,
        )

    async def send_contact(
        self, chat_id: str, name: str, phone: str
    ) -> None:
        """שליחת פרטי איש קשר כטקסט (WhatsApp API לא תומך ב-vCard ישירות)."""
        text = f"📇 {name}\n📞 {phone}"
        send_to = self._resolve_send_to(chat_id)
        await asyncio.to_thread(
            self.client.messages.create,
            body=text,
            from_=self.from_number,
            to=send_to,
        )

    async def send_location(
        self, chat_id: str, lat: float, lon: float
    ) -> None:
        """שליחת מיקום כקישור ל-Google Maps."""
        url = f"https://maps.google.com/maps?q={lat},{lon}"
        send_to = self._resolve_send_to(chat_id)
        await asyncio.to_thread(
            self.client.messages.create,
            body=f"📍 מיקום: {url}",
            from_=self.from_number,
            to=send_to,
        )

    async def send_file(
        self, chat_id: str, file_data: bytes, filename: str
    ) -> None:
        """שליחת קובץ דרך media URL — לא נתמך ישירות מ-bytes ב-Twilio.

        בשלב זה שולח הודעת טקסט עם שם הקובץ. תמיכה מלאה דורשת
        העלאת הקובץ ל-URL ציבורי ושליחה כ-media_url.
        """
        logger.warning(
            "WhatsApp file sending not fully implemented — "
            "sending filename text instead: %s",
            filename,
        )
        send_to = self._resolve_send_to(chat_id)
        await asyncio.to_thread(
            self.client.messages.create,
            body=f"📎 קובץ: {filename}",
            from_=self.from_number,
            to=send_to,
        )
