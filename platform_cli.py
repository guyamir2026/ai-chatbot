"""
Platform CLI — ניהול tenants מהטרמינל (multi-tenant שלב 2).

שימוש:
    python -m platform_cli create-tenant salon-a "מספרת דנה"
    python -m platform_cli list-tenants
    python -m platform_cli suspend salon-a
    python -m platform_cli activate salon-a
    python -m platform_cli gen-key
    python -m platform_cli set-route twilio_number +14155551234 salon-a
    python -m platform_cli delete-route twilio_number +14155551234
    python -m platform_cli list-routes [salon-a]
    python -m platform_cli set-secret salon-a telegram_bot_token   # ערך מוזן ב-prompt/stdin
    python -m platform_cli list-secrets salon-a

הערה: ערכי סודות לעולם לא עוברים כארגומנט (דולפים ל-shell history/ps) —
הם נקראים מ-stdin (pipe) או מ-prompt מוסתר.
"""

import argparse
import getpass
import logging
import sys

import control_plane as cp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("platform_cli")


def _read_secret_value() -> str:
    """קריאת ערך סוד: מ-stdin אם הוא pipe, אחרת prompt מוסתר."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass("value (hidden): ")


def _cmd_create_tenant(args) -> int:
    cp.create_tenant(
        args.slug, args.display_name, plan=args.plan, seed_hours=not args.no_seed,
    )
    print(f"✓ tenant '{args.slug}' נוצר ({args.display_name})")
    print(f"  DB: {cp.tenant_db_path(args.slug)}")
    return 0


def _cmd_list_tenants(args) -> int:
    tenants = cp.list_tenants()
    if not tenants:
        print("(אין tenants רשומים — המערכת במצב legacy יחיד)")
        return 0
    for t in tenants:
        print(
            f"{t['tenant_id']:<24} {t['status']:<10} {t['plan']:<10} "
            f"{t['display_name']}"
        )
    return 0


def _cmd_set_status(args, status: str) -> int:
    cp.set_tenant_status(args.slug, status)
    print(f"✓ {args.slug} → {status}")
    return 0


def _cmd_gen_key(args) -> int:
    print(cp.generate_route_key())
    return 0


def _cmd_set_route(args) -> int:
    cp.set_route(args.route_type, args.route_key, args.slug)
    print(f"✓ {args.route_type}:{args.route_key} → {args.slug}")
    return 0


def _cmd_delete_route(args) -> int:
    if cp.delete_route(args.route_type, args.route_key):
        print("✓ נמחק")
        return 0
    print("(לא נמצא)")
    return 1


def _cmd_list_routes(args) -> int:
    for r in cp.list_routes(args.slug):
        print(f"{r['tenant_id']:<24} {r['route_type']:<22} {r['route_key']}")
    return 0


def _cmd_set_secret(args) -> int:
    value = _read_secret_value()
    cp.set_tenant_secret(args.slug, args.name, value)
    if value:
        print(f"✓ הסוד '{args.name}' נשמר מוצפן ל-{args.slug}")
    else:
        print(f"✓ הסוד '{args.name}' נמחק מ-{args.slug}")
    return 0


def _cmd_list_secrets(args) -> int:
    names = cp.list_tenant_secret_names(args.slug)
    if not names:
        print("(אין סודות)")
        return 0
    for n in names:
        print(n)  # שמות בלבד — ערכים לעולם לא מודפסים
    return 0


def _cmd_connect_telegram(args) -> int:
    """חיבור בוט טלגרם של tenant: מפתח ראוט + secret + רישום ה-webhook.

    אידמפוטנטי — מפתח/secret קיימים נשמרים (רישום חוזר רק מרענן את
    ה-webhook מול טלגרם). דורש שהסוד telegram_bot_token כבר הוגדר.
    """
    import asyncio

    import config as _config
    from bot_registry import resolve_telegram_token, sync_telegram_webhook

    slug = args.slug
    if cp.get_tenant(slug) is None:
        print(f"tenant לא רשום: {slug}")
        return 1
    if not resolve_telegram_token(slug):
        print("חסר telegram_bot_token — קודם:")
        print(f"  python -m platform_cli set-secret {slug} telegram_bot_token")
        return 1

    # מפתח ראוט — קיים או חדש
    key = cp.get_tenant_route_key(slug, "telegram_webhook_key")
    if not key:
        key = cp.generate_route_key()
        cp.set_route("telegram_webhook_key", key, slug)
        print(f"✓ נוצר מפתח webhook: {key[:8]}…")

    # secret לאימות ה-header של טלגרם — קיים או חדש (fail-closed ב-route)
    from control_plane import get_tenant_secret

    secret = get_tenant_secret(slug, "telegram_webhook_secret")
    if not secret:
        secret = cp.generate_route_key()
        cp.set_tenant_secret(slug, "telegram_webhook_secret", secret)
        print("✓ נוצר webhook secret")

    base = (_config.ADMIN_URL or "").rstrip("/")
    if not base:
        print("⚠ ADMIN_URL לא מוגדר — לא ניתן לרשום webhook מול טלגרם.")
        print("  המפתחות נשמרו; להריץ שוב אחרי הגדרת ADMIN_URL.")
        return 1

    webhook_url = f"{base}/telegram/webhook/t/{key}"
    bot_username = asyncio.run(sync_telegram_webhook(slug, webhook_url, secret))
    # שם המשתמש של הבוט (getMe) — נשמר לקישורי QR / widget, כמו בפאנל
    if isinstance(bot_username, str) and bot_username.strip():
        cp.set_tenant_secret(
            slug, "telegram_bot_username", bot_username.strip().lstrip("@")
        )
        print(f"✓ נלכד שם הבוט: @{bot_username.strip().lstrip('@')}")
    print(f"✓ webhook נרשם מול טלגרם: {webhook_url}")
    return 0


def _cmd_create_admin(args) -> int:
    """יצירת משתמש אדמין (owner של tenant, או platform-admin עם --platform)."""
    password = _read_secret_value()
    role = "platform_admin" if args.platform else "owner"
    cp.create_admin_user(
        args.email,
        password,
        role=role,
        tenant_id=None if args.platform else args.slug,
        display_name=args.display_name or "",
    )
    scope = "platform" if args.platform else f"tenant {args.slug}"
    print(f"✓ משתמש אדמין נוצר ({scope})")
    return 0


def _cmd_list_admins(args) -> int:
    users = cp.list_admin_users(args.slug)
    if not users:
        print("(אין משתמשי אדמין)")
        return 0
    for u in users:
        print(
            f"{u['email']:<32} {u['role']:<15} {u['tenant_id'] or '-':<18} "
            f"{u['status']}"
        )
    return 0


def _cmd_disable_admin(args) -> int:
    cp.set_admin_user_status(args.email, "disabled")
    print("✓ המשתמש הושבת")
    return 0


def _cmd_show_urls(args) -> int:
    """הדפסת ה-URLs הציבוריים של ה-tenant — להדבקה ב-Twilio Console וכו'."""
    import config as _config

    base = (_config.ADMIN_URL or "https://<ADMIN_URL>").rstrip("/")
    if cp.get_tenant(args.slug) is None:
        print(f"tenant לא רשום: {args.slug}")
        return 1
    twilio_key = cp.get_tenant_route_key(args.slug, "twilio_webhook_key")
    widget_key = cp.get_tenant_route_key(args.slug, "widget_key")
    telegram_key = cp.get_tenant_route_key(args.slug, "telegram_webhook_key")
    print(f"— {args.slug} —")
    if telegram_key:
        print(f"Telegram webhook: {base}/telegram/webhook/t/{telegram_key}")
    else:
        print("Telegram        : (לא מחובר — connect-telegram)")
    if twilio_key:
        print(f"Twilio inbound : {base}/webhook/whatsapp/t/{twilio_key}")
        print(f"Twilio status  : {base}/webhook/whatsapp/t/{twilio_key}/status")
    else:
        print("Twilio         : (אין מפתח — gen-key + set-route twilio_webhook_key)")
    if widget_key:
        print(f"Widget embed   : {base}/widget/embed.js?k={widget_key}")
    else:
        print("Widget         : (אין מפתח — gen-key + set-route widget_key)")
    print(f"עמודים ציבוריים: {base}/t/{args.slug}/p/<page_id>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-tenant", help="יצירת tenant חדש (רישום + DB + seed)")
    p.add_argument("slug")
    p.add_argument("display_name")
    p.add_argument("--plan", default="premium")
    p.add_argument("--no-seed", action="store_true", help="בלי זריעת שעות פעילות")
    p.set_defaults(func=_cmd_create_tenant)

    p = sub.add_parser("list-tenants", help="רשימת ה-tenants ומצבם")
    p.set_defaults(func=_cmd_list_tenants)

    p = sub.add_parser("suspend", help="השעיית tenant (חוסם גישה ל-DB שלו)")
    p.add_argument("slug")
    p.set_defaults(func=lambda a: _cmd_set_status(a, "suspended"))

    p = sub.add_parser("activate", help="החזרת tenant לפעילות")
    p.add_argument("slug")
    p.set_defaults(func=lambda a: _cmd_set_status(a, "active"))

    p = sub.add_parser("gen-key", help="מפתח ראוטינג אקראי (webhook/widget)")
    p.set_defaults(func=_cmd_gen_key)

    p = sub.add_parser("set-route", help="מיפוי מפתח נכנס → tenant")
    p.add_argument("route_type", choices=cp.ROUTE_TYPES)
    p.add_argument("route_key")
    p.add_argument("slug")
    p.set_defaults(func=_cmd_set_route)

    p = sub.add_parser("delete-route", help="הסרת ראוט")
    p.add_argument("route_type", choices=cp.ROUTE_TYPES)
    p.add_argument("route_key")
    p.set_defaults(func=_cmd_delete_route)

    p = sub.add_parser("list-routes", help="רשימת ראוטים")
    p.add_argument("slug", nargs="?", default=None)
    p.set_defaults(func=_cmd_list_routes)

    p = sub.add_parser("set-secret", help="שמירת סוד מוצפן (ערך מ-stdin/prompt)")
    p.add_argument("slug")
    p.add_argument("name")
    p.set_defaults(func=_cmd_set_secret)

    p = sub.add_parser("list-secrets", help="שמות הסודות של tenant (ללא ערכים)")
    p.add_argument("slug")
    p.set_defaults(func=_cmd_list_secrets)

    p = sub.add_parser("show-urls", help="ה-URLs הציבוריים של tenant (ל-Twilio Console וכו')")
    p.add_argument("slug")
    p.set_defaults(func=_cmd_show_urls)

    p = sub.add_parser(
        "connect-telegram",
        help="חיבור בוט טלגרם: מפתח + secret + רישום webhook מול טלגרם",
    )
    p.add_argument("slug")
    p.set_defaults(func=_cmd_connect_telegram)

    p = sub.add_parser("create-admin", help="משתמש אדמין חדש (סיסמה מ-stdin/prompt)")
    p.add_argument("email")
    p.add_argument("slug", nargs="?", default=None,
                   help="ה-tenant של בעל העסק (לא נדרש עם --platform)")
    p.add_argument("--platform", action="store_true",
                   help="platform admin — גישה חוצת-tenants")
    p.add_argument("--display-name", default="")
    p.set_defaults(func=_cmd_create_admin)

    p = sub.add_parser("list-admins", help="רשימת משתמשי האדמין")
    p.add_argument("slug", nargs="?", default=None)
    p.set_defaults(func=_cmd_list_admins)

    p = sub.add_parser("disable-admin", help="השבתת משתמש אדמין")
    p.add_argument("email")
    p.set_defaults(func=_cmd_disable_admin)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # CLI — הודעה קריאה במקום traceback
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
