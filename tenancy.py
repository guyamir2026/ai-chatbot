"""
ניהול הקשר Tenant — תשתית שלב 1 של המעבר ל-multi-tenant.

ראה docs/multi_tenant_migration_spec.md (פרק 5 — Tenant Isolation).

בשלב הנוכחי קיים tenant יחיד (DEFAULT_TENANT = "default") שממופה לקבצים
הקיימים (DB_PATH / FAISS_INDEX_PATH מ-config), כך שההתנהגות זהה לחלוטין
למצב שלפני השינוי. בשלב 2 (הפלטפורמה) ייווספו tenants אמיתיים תחת
DATA_DIR/tenants/<slug>/ וייכנס מצב STRICT.

עקרונות:
- ה-context נקבע **רק בנקודות כניסה** (בקשת HTTP, עדכון בוט, job, CLI)
  דרך tenant_context() / set_current_tenant. קוד עמוק לעולם לא מנחש
  tenant — הוא קורא get_current_tenant().
- contextvars זורמים אוטומטית לתוך asyncio tasks, אבל **לא** לתוך
  threading.Thread חדש ולא דרך asyncio.to_thread בכיוון ההפוך — בהעברת
  עבודה בין threads יש להעביר את ה-tenant כפרמטר ולקבוע אותו מחדש.
- מצב STRICT (משתנה סביבה TENANCY_STRICT=true): גישה בלי context קובע
  ⇒ חריגה. כבוי כברירת מחדל בשלב 1; יודלק בפלטפורמה בשלב 2.
"""

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"

# slug חוקי: אותיות קטנות/ספרות/מקף, מתחיל באות/ספרה, עד 32 תווים.
# הולידציה היא גם קו ההגנה מפני path traversal (ה-slug משמש כשם תיקייה).
_TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_current_tenant: ContextVar[Optional[str]] = ContextVar("current_tenant", default=None)


class TenancyError(RuntimeError):
    """שגיאת בסיס של שכבת ה-tenancy."""


class MissingTenantContext(TenancyError):
    """גישה לנתונים בלי tenant context קובע (רלוונטי במצב STRICT)."""


class InvalidTenantSlug(TenancyError):
    """מזהה tenant שאינו עומד בכללי ה-slug."""


class TenantSuspendedError(TenancyError):
    """גישה לנתוני tenant מושעה / בתהליך הגירה — חסומה."""


class UnregisteredTenantError(TenancyError):
    """במצב STRICT: tenant שאינו רשום ב-control plane — חסום (הגנת typo)."""


def _strict_mode() -> bool:
    # נקרא דינמית (לא קבוע import-time) כדי שאפשר יהיה להדליק בטסטים
    # ובשלב 2 בלי לגעת בקוד.
    return os.getenv("TENANCY_STRICT", "").strip().lower() in ("1", "true", "yes")


def validate_tenant_id(tenant_id: str) -> str:
    """מוודא שה-tenant_id הוא slug חוקי ומחזיר אותו. זורק InvalidTenantSlug."""
    if not isinstance(tenant_id, str) or not _TENANT_SLUG_RE.match(tenant_id):
        raise InvalidTenantSlug(f"invalid tenant id: {tenant_id!r}")
    return tenant_id


def set_current_tenant(tenant_id: str):
    """קובע את ה-tenant הנוכחי ומחזיר token לשחזור (ראה reset_current_tenant)."""
    return _current_tenant.set(validate_tenant_id(tenant_id))


def reset_current_tenant(token) -> None:
    """משחזר את מצב ה-context שלפני set_current_tenant (לשימוש ב-teardown)."""
    _current_tenant.reset(token)


def get_current_tenant() -> str:
    """מחזיר את ה-tenant הנוכחי.

    כשה-context לא נקבע: במצב רגיל נופל ל-DEFAULT_TENANT (תאימות שלב 1);
    במצב STRICT — זורק MissingTenantContext.
    """
    tenant = _current_tenant.get()
    if tenant is not None:
        return tenant
    if _strict_mode():
        raise MissingTenantContext(
            "tenant context was not set on this execution path "
            "(entry point must call set_current_tenant/tenant_context)"
        )
    return DEFAULT_TENANT


@contextmanager
def tenant_context(tenant_id: str):
    """קובע tenant לבלוק קוד ומשחזר את הקודם ביציאה (גם בחריגה)."""
    token = set_current_tenant(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


def _tenants_root() -> Path:
    import config as _config  # ייבוא עצל — נקרא דינמית כדי לכבד patches

    return Path(_config.DATA_DIR) / "tenants"


def _check_tenant_allowed(tenant: str) -> None:
    """אכיפת סטטוס מול ה-control plane (שלב 2).

    - tenant מושעה / בהגירה ⇒ חסימה (בכל מצב).
    - tenant לא-רשום ⇒ נחסם רק במצב STRICT (הגנת typo בפלטפורמה);
      במצב רגיל מותר — טסטים ופיתוח יוצרים tenants בלי registry.
    - ה-tenant של ברירת המחדל (legacy) לעולם לא נבדק — הוא לא רשום.
    """
    if tenant == DEFAULT_TENANT:
        return
    # ייבוא עצל — control_plane מייבא את tenancy ברמת המודול; הכיוון
    # ההפוך חייב להיות בתוך הפונקציה כדי לא ליצור מעגל ייבוא.
    from control_plane import get_tenant_status_cached

    status = get_tenant_status_cached(tenant)
    if status in ("suspended", "migrating"):
        raise TenantSuspendedError(
            f"tenant '{tenant}' במצב '{status}' — הגישה לנתוניו חסומה"
        )
    if status is None and _strict_mode():
        raise UnregisteredTenantError(
            f"tenant '{tenant}' אינו רשום ב-control plane (מצב STRICT)"
        )


def tenant_db_path(tenant_id: Optional[str] = None) -> Path:
    """נתיב קובץ ה-SQLite של ה-tenant.

    ה-tenant של ברירת המחדל ממופה ל-config.DB_PATH הקיים (תאימות מלאה
    לפריסות ולטסטים). כל tenant אחר — DATA_DIR/tenants/<slug>/chatbot.db.
    הערה: הפונקציה לא יוצרת תיקיות — יצירת ה-tenant (שלב 2, onboarding)
    אחראית לכך.
    """
    tenant = validate_tenant_id(tenant_id) if tenant_id else get_current_tenant()
    if tenant == DEFAULT_TENANT:
        import config as _config

        return Path(_config.DB_PATH)
    _check_tenant_allowed(tenant)
    path = (_tenants_root() / tenant / "chatbot.db").resolve()
    # belt-and-braces מעבר לולידציית ה-slug: הנתיב חייב להישאר תחת השורש
    if not path.is_relative_to(_tenants_root().resolve()):
        raise InvalidTenantSlug(f"tenant path escapes tenants root: {tenant!r}")
    return path


def tenant_faiss_dir(tenant_id: Optional[str] = None) -> Path:
    """תיקיית אינדקס ה-FAISS של ה-tenant (מקבילה ל-tenant_db_path)."""
    tenant = validate_tenant_id(tenant_id) if tenant_id else get_current_tenant()
    if tenant == DEFAULT_TENANT:
        import config as _config

        return Path(_config.FAISS_INDEX_PATH)
    _check_tenant_allowed(tenant)
    return (_tenants_root() / tenant / "faiss_index").resolve()
