"""
יצירת זוג מפתחות VAPID חד-פעמית להתראות Web Push.

הרצה:
    python -m utils.vapid_keygen

הסקריפט מדפיס שתי שורות מוכנות להעתקה לקובץ `.env`:
    VAPID_PUBLIC_KEY=<base64url>
    VAPID_PRIVATE_KEY=<base64url>

הפורמט הוא base64url ללא padding — תואם הן ל-`pywebpush` והן ל-Push API בדפדפן
(`applicationServerKey` ב-`PushManager.subscribe`).

חשוב: לשמור את ה-private key רק בשרת. שינוי המפתחות בעתיד מבטל את כל ה-
subscriptions הקיימים — לקוחות יצטרכו לאשר התראות מחדש.
"""

import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(data: bytes) -> str:
    """base64url ללא padding — לפי תקן Web Push."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> tuple[str, str]:
    """מחזיר (public_key_b64url, private_key_b64url) — שניהם raw bytes ב-base64url.

    private_key: 32-byte raw scalar (P-256). זה הפורמט ש-pywebpush מצפה לו.
    public_key:  uncompressed point (65 bytes, מתחיל ב-0x04). זה הפורמט
                 שה-PushManager בדפדפן מצפה לו כ-applicationServerKey.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())

    private_numbers = private_key.private_numbers()
    private_bytes = private_numbers.private_value.to_bytes(32, byteorder="big")

    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    return _b64url(public_bytes), _b64url(private_bytes)


def main() -> None:
    public_b64, private_b64 = generate_vapid_keypair()
    print("# הוסף לקובץ .env:")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print('VAPID_SUBJECT="mailto:owner@example.com"  # שנה לכתובת אימייל פעילה')


if __name__ == "__main__":
    main()
