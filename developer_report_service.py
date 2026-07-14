"""
שירות דיווחי באגים למפתח — שליחת התראות לטלגרם + מייל.

בעל העסק מדווח על בעיות דרך פאנל האדמין, והדיווח נשלח ישירות
לטלגרם של המפתח ו/או למייל שלו עם תיאור + צילומי מסך.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests as http_requests

from ai_chatbot.config import (
    DEVELOPER_BOT_TOKEN,
    DEVELOPER_CHAT_ID,
    DEVELOPER_EMAIL,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    BUSINESS_NAME,
)

logger = logging.getLogger(__name__)

# גודל מקסימלי לצילום מסך — 10MB (מגבלת Telegram Bot API)
MAX_SCREENSHOT_SIZE = 10 * 1024 * 1024
MAX_SCREENSHOTS = 3
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def is_configured() -> bool:
    """בדיקה אם לפחות ערוץ אחד (טלגרם או מייל) מוגדר."""
    return _telegram_configured() or _email_configured()


def _telegram_configured() -> bool:
    return bool(DEVELOPER_BOT_TOKEN and DEVELOPER_CHAT_ID)


def _email_configured() -> bool:
    return bool(DEVELOPER_EMAIL and SMTP_HOST and SMTP_PASSWORD)


def allowed_file(filename: str) -> bool:
    """בדיקה אם סיומת הקובץ מותרת."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def send_report_to_developer(
    description: str,
    report_id: int,
    screenshots: Optional[list[tuple[str, bytes]]] = None,
) -> bool:
    """שליחת דיווח באג לטלגרם ולמייל של המפתח.

    שולח לכל ערוץ שמוגדר. מחזיר True אם לפחות ערוץ אחד הצליח.
    """
    if not is_configured():
        logger.warning("Developer notifications not configured — skipping report")
        return False

    telegram_ok = False
    email_ok = False

    if _telegram_configured():
        telegram_ok = _send_telegram(description, report_id, screenshots)

    if _email_configured():
        email_ok = _send_email(description, report_id, screenshots)

    return telegram_ok or email_ok


# ── טלגרם ────────────────────────────────────────────────────────────────────


def _send_telegram(
    description: str,
    report_id: int,
    screenshots: Optional[list[tuple[str, bytes]]] = None,
) -> bool:
    """שליחת דיווח לטלגרם של המפתח."""
    text = (
        f"🐛 *דיווח באג חדש*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*עסק:* {_escape_markdown(BUSINESS_NAME)}\n"
        f"*מזהה:* \\#{report_id}\n\n"
        f"{_escape_markdown(description)}"
    )

    try:
        if screenshots:
            return _send_telegram_with_photos(text, screenshots)

        resp = http_requests.post(
            f"https://api.telegram.org/bot{DEVELOPER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": DEVELOPER_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error("Failed to send developer report via Telegram: %s", resp.text)
        return resp.ok

    except Exception as e:
        logger.error("Error sending developer report via Telegram: %s", e)
        return False


def _send_telegram_with_photos(text: str, screenshots: list[tuple[str, bytes]]) -> bool:
    """שליחת דיווח עם צילומי מסך לטלגרם."""
    try:
        resp = http_requests.post(
            f"https://api.telegram.org/bot{DEVELOPER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": DEVELOPER_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error("Failed to send report text via Telegram: %s", resp.text)
            return False

        success = True
        for filename, file_data in screenshots:
            try:
                photo_resp = http_requests.post(
                    f"https://api.telegram.org/bot{DEVELOPER_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": DEVELOPER_CHAT_ID},
                    files={"photo": (filename, file_data, _mime_from_filename(filename))},
                    timeout=30,
                )
                if not photo_resp.ok:
                    logger.error("Failed to send screenshot %s: %s", filename, photo_resp.text)
                    success = False
            except Exception as e:
                logger.error("Error sending screenshot %s: %s", filename, e)
                success = False

        return success

    except Exception as e:
        logger.error("Error sending report with photos via Telegram: %s", e)
        return False


# ── מייל ─────────────────────────────────────────────────────────────────────


def _send_email(
    description: str,
    report_id: int,
    screenshots: Optional[list[tuple[str, bytes]]] = None,
) -> bool:
    """שליחת דיווח באג למייל המפתח עם צילומי מסך כצרופות."""
    try:
        msg = EmailMessage()
        msg["Subject"] = f"🐛 דיווח באג #{report_id} — {BUSINESS_NAME}"
        msg["From"] = SMTP_USER or DEVELOPER_EMAIL
        msg["To"] = DEVELOPER_EMAIL

        body = (
            f"דיווח באג חדש\n"
            f"{'=' * 30}\n"
            f"עסק: {BUSINESS_NAME}\n"
            f"מזהה: #{report_id}\n\n"
            f"{description}"
        )
        msg.set_content(body)

        # צרופות תמונה
        if screenshots:
            for filename, file_data in screenshots:
                maintype, _, subtype = _mime_from_filename(filename).partition("/")
                msg.add_attachment(
                    file_data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USER or DEVELOPER_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info("Developer report #%d sent via email to %s", report_id, DEVELOPER_EMAIL)
        return True

    except Exception as e:
        logger.error("Error sending developer report via email: %s", e)
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mime_from_filename(filename: str) -> str:
    """הסקת MIME type מסיומת קובץ תמונה."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext, "image/jpeg")


def _escape_markdown(text: str) -> str:
    """Escape תווים מיוחדים ל-MarkdownV2 של טלגרם."""
    # backslash חייב להיות ראשון — אחרת הוא ידרוס escapes שכבר הוספנו
    special_chars = r"\\_*[]()~`>#+-=|{}.!"
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += f"\\{char}"
        else:
            escaped += char
    return escaped
