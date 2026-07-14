"""
messaging — שכבת הפשטה לשליחת הודעות בערוצים שונים.

מספק ממשק אחיד (MessageAdapter) עם מימושים ל-Telegram ו-WhatsApp.
"""

from messaging.base import MessageAdapter
from messaging.formatter import format_message

__all__ = ["MessageAdapter", "format_message"]
