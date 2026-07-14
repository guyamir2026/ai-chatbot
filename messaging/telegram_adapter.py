"""
TelegramAdapter — מימוש MessageAdapter עבור Telegram.

עוטף את python-telegram-bot (Bot object) ומספק ממשק אחיד.
"""

import logging
from typing import Optional

from telegram import Bot

from messaging.base import MessageAdapter
from messaging.formatter import format_message

logger = logging.getLogger(__name__)


class TelegramAdapter(MessageAdapter):
    """Adapter לשליחת הודעות דרך Telegram Bot API."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(
        self,
        chat_id: str,
        text: str,
        buttons: Optional[list[str]] = None,
    ) -> None:
        """שליחת הודעת טקסט ב-HTML parse_mode."""
        formatted = format_message(text, "telegram")
        await self.bot.send_message(
            chat_id=int(chat_id),
            text=formatted,
            parse_mode="HTML",
        )

    async def send_contact(
        self, chat_id: str, name: str, phone: str
    ) -> None:
        """שליחת כרטיס איש קשר."""
        await self.bot.send_contact(
            chat_id=int(chat_id),
            first_name=name,
            phone_number=phone,
        )

    async def send_location(
        self, chat_id: str, lat: float, lon: float
    ) -> None:
        """שליחת מיקום."""
        await self.bot.send_location(
            chat_id=int(chat_id),
            latitude=lat,
            longitude=lon,
        )

    async def send_file(
        self, chat_id: str, file_data: bytes, filename: str
    ) -> None:
        """שליחת קובץ כמסמך."""
        import io

        await self.bot.send_document(
            chat_id=int(chat_id),
            document=io.BytesIO(file_data),
            filename=filename,
        )
