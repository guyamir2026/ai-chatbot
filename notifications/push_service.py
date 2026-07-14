"""
Web Push notifications לבעל העסק — התראות גם כשהדשבורד סגור.

המנגנון מתבסס על תקן Web Push (RFC 8030) ועובד עם FCM (Chrome/Edge), Mozilla
Push Service (Firefox) ו-Apple Push Notification service (Safari, iOS 16.4+).
ה-payload מוצפן end-to-end ע"י pywebpush — שירות ה-push של הדפדפן אינו רואה
את התוכן.

נקודות שילוב:
- live_chat_service.live_chat_guard / live_chat_guard_booking — Telegram.
- messaging/whatsapp_webhook.py — WhatsApp דרך Twilio.
- messaging/meta_webhook.py — Messenger / Instagram DM.
"""

import json
import logging
from typing import Optional
from urllib.parse import quote

import database as db
from config import (
    VAPID_PUBLIC_KEY,
    VAPID_PRIVATE_KEY,
    VAPID_SUBJECT,
)

logger = logging.getLogger(__name__)

# מגבלת תווים לתקציר ההודעה ב-notification body — תואם ל-toast הקיים בפאנל.
_BODY_PREVIEW_LIMIT = 80

# דגל פנימי כדי להדפיס אזהרה על VAPID חסר פעם אחת בלבד (לא ספאם בלוג).
_warned_missing_vapid = False


def _is_configured() -> bool:
    """בודק אם VAPID מוגדר. בלי שלושת הערכים, push מושבת בשקט."""
    global _warned_missing_vapid
    if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT:
        return True
    if not _warned_missing_vapid:
        logger.warning(
            "Web Push disabled — VAPID_PUBLIC_KEY/PRIVATE_KEY/SUBJECT not set. "
            "Run `python -m utils.vapid_keygen` to generate keys."
        )
        _warned_missing_vapid = True
    return False


def _build_payload(user_id: str, display_name: str, text: str) -> dict:
    """בונה את ה-payload שיוצג ב-notification ע"י ה-Service Worker.

    `tag` דורס notifications קודמות מאותו לקוח (אנדרואיד/דסקטופ Chrome) — כך
    לא נערמות 20 התראות אם המשתמש שלח 20 הודעות; רק האחרונה מוצגת. `url`
    נשלח ב-`data` כדי שלחיצה תפתח את עמוד השיחה הנכון.
    """
    body = (text or "").strip()
    if len(body) > _BODY_PREVIEW_LIMIT:
        body = body[:_BODY_PREVIEW_LIMIT].rstrip() + "…"
    # urlencode על user_id — מספר WhatsApp `+972...` חייב escape (ראה כלל
    # ב-CLAUDE.md לגבי `+` שמתפרש כ-space).
    url = f"/live-chat/{quote(user_id, safe='')}"
    return {
        "title": display_name or "הודעה חדשה בשיחה חיה",
        "body": body or "הודעה חדשה",
        "url": url,
        "tag": f"live-chat-{user_id}",
        "user_id": user_id,
    }


def notify_live_chat_message(
    user_id: str, display_name: str, text: Optional[str]
) -> None:
    """שולח Web Push לכל המנויים הרשומים על הודעה חדשה בשיחה חיה.

    קריאה לפונקציה הזו אינה מורידה את הביצועים של ה-webhook באופן משמעותי
    (pywebpush.webpush סינכרוני אבל מהיר — פוסט אחד לכל מנוי). אם בעתיד יהיו
    הרבה מנויים, אפשר להעביר לרקע. כרגע צפויים 1-3 מנויים לבעל עסק יחיד.
    """
    if not _is_configured():
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.error("pywebpush not installed — Web Push disabled")
        return

    try:
        subscriptions = db.get_all_push_subscriptions()
    except Exception:
        logger.exception("Failed to load push subscriptions from DB")
        return
    if not subscriptions:
        return

    payload = _build_payload(user_id, display_name, text or "")
    payload_json = json.dumps(payload, ensure_ascii=False)
    vapid_claims = {"sub": VAPID_SUBJECT}

    # לולאת I/O על רשימת פריטים — לפי הכלל ב-CLAUDE.md, כל קריאת רשת
    # עטופה ב-try/except כדי שכשל במנוי אחד לא יעצור את שאר המנויים.
    for sub in subscriptions:
        endpoint = sub["endpoint"]
        subscription_info = {
            "endpoint": endpoint,
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload_json,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=vapid_claims,
                ttl=60,  # נקודתי — לא נדרש delivery מאוחר יותר מדקה
            )
            try:
                db.touch_push_subscription(endpoint)
            except Exception:
                logger.warning("touch_push_subscription failed for endpoint=%s...", endpoint[:40])
        except WebPushException as exc:
            # 404 = endpoint לא קיים; 410 = subscription בוטלה ע"י המשתמש.
            # שניהם terminal — מוחקים מהמנויים כדי לא לנסות שוב.
            status = getattr(exc.response, "status_code", None) if exc.response else None
            if status in (404, 410):
                try:
                    db.delete_push_subscription(endpoint)
                    logger.info("Removed expired push subscription (status=%s)", status)
                except Exception:
                    logger.exception("Failed to delete expired push subscription")
            else:
                logger.error(
                    "Web Push failed for endpoint=%s..., status=%s: %s",
                    endpoint[:40], status, exc,
                )
        except Exception:
            logger.exception("Unexpected error sending Web Push to endpoint=%s...", endpoint[:40])
