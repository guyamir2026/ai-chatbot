"""
בוני URL ציבוריים תלויי-tenant (multi-tenant שלב 2, spec 6.4).

כל לינק ציבורי שיוצא ללקוח קצה (עמוד תשובה ארוכה, קובץ ICS, status
callback של Twilio) חייב להיבנות כאן — כך שה-tenant הנוכחי קובע את
הנתיב: ה-tenant של ברירת המחדל שומר על הנתיבים ה-legacy ‏(/p/<id>),
וכל tenant אחר מקבל נתיב עם slug ‏(/t/<slug>/p/<id>) שה-route הציבורי
יודע לפתוח מקובץ ה-DB הנכון.

ה-base הוא ADMIN_URL (בפלטפורמה — הדומיין המרכזי). כשהוא ריק אין לאן
להפנות — הקוראים כבר מטפלים בזה (fallback לשליחה רגילה).
"""

import logging
from typing import Optional

from tenancy import DEFAULT_TENANT, get_current_tenant

logger = logging.getLogger(__name__)


def _base_url() -> str:
    import config as _config  # דינמי — מכבד patches ועדכון חם

    return (_config.ADMIN_URL or "").rstrip("/")


def _tenant_prefix() -> str:
    """‏'' ל-tenant של ברירת המחדל; ‏'/t/<slug>' לכל tenant אחר."""
    tenant = get_current_tenant()
    if tenant == DEFAULT_TENANT:
        return ""
    return f"/t/{tenant}"


def public_page_url(page_id: str) -> str:
    """URL של עמוד תשובה ציבורי (/p/<id>) עבור ה-tenant הנוכחי."""
    return f"{_base_url()}{_tenant_prefix()}/p/{page_id}"


def public_ics_url(page_id: str) -> str:
    """URL של קובץ ICS ציבורי (/ics/<id>) עבור ה-tenant הנוכחי."""
    return f"{_base_url()}{_tenant_prefix()}/ics/{page_id}"


def legal_page_url() -> str:
    """URL של עמוד המסמכים המשפטיים (/legal) עבור ה-tenant הנוכחי.

    מחזיר '' כש-ADMIN_URL לא מוגדר — הקורא (הודעת הפתיחה) שולח אז בלי
    קישור, כדי לא לשבור.
    """
    base = _base_url()
    if not base:
        return ""
    return f"{base}{_tenant_prefix()}/legal"


def whatsapp_status_callback_url() -> Optional[str]:
    """‏URL ל-status callbacks של Twilio עבור ה-tenant הנוכחי.

    ה-callback חוזר מ-Twilio ומאומת עם ה-auth token של ה-tenant — לכן
    ל-tenant שאינו default הוא חייב לכלול את מפתח ה-webhook שלו. אם
    ל-tenant אין מפתח רשום (עוד לא חובר ל-Twilio דרך הפלטפורמה) —
    מחזירים None והקורא שולח בלי callback.
    """
    base = _base_url()
    if not base:
        return None
    tenant = get_current_tenant()
    if tenant == DEFAULT_TENANT:
        return f"{base}/webhook/whatsapp/status"
    try:
        from control_plane import get_tenant_route_key

        key = get_tenant_route_key(tenant, "twilio_webhook_key")
    except Exception:
        logger.error("status_callback_url: route lookup failed", exc_info=True)
        return None
    if not key:
        return None
    return f"{base}/webhook/whatsapp/t/{key}/status"
