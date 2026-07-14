"""
MessageAdapter — ממשק מופשט לשליחת הודעות בכל ערוץ.

כל ערוץ (Telegram, WhatsApp) ממש את הממשק הזה כדי שהלוגיקה העסקית
תוכל לשלוח הודעות מבלי לדעת דרך איזה ערוץ הן נשלחות.
"""

from abc import ABC, abstractmethod
from typing import Optional


class MessageAdapter(ABC):
    """ממשק אחיד לשליחת הודעות — כל ערוץ ממש את המתודות."""

    @abstractmethod
    async def send_text(
        self,
        chat_id: str,
        text: str,
        buttons: Optional[list[str]] = None,
    ) -> None:
        """שליחת הודעת טקסט. buttons — רשימת תוויות לכפתורים (אופציונלי)."""
        ...

    @abstractmethod
    async def send_contact(
        self, chat_id: str, name: str, phone: str
    ) -> None:
        """שליחת כרטיס איש קשר."""
        ...

    @abstractmethod
    async def send_location(
        self, chat_id: str, lat: float, lon: float
    ) -> None:
        """שליחת מיקום."""
        ...

    @abstractmethod
    async def send_file(
        self, chat_id: str, file_data: bytes, filename: str
    ) -> None:
        """שליחת קובץ."""
        ...
