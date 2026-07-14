"""
מודול מיתוג עסקי — עיבוד לוגו והשתלתו על QR Code.

תפקידים:
1. process_uploaded_logo — ולידציה + נרמול של תמונה שהעלה בעל העסק
   (PNG/JPG, מקס' 5MB, pad לריבוע עם רקע שקוף, resize ל-≤512x512).
2. overlay_logo_on_qr — שילוב הלוגו במרכז ה-QR (~22% מרוחב ה-QR,
   עם ריבוע לבן מאחור כדי שהקצוות חדים — בטוח עם ECC=H).
"""

import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


# ── קבועים ───────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB
# שומרים את הלוגו ברזולוציה גבוהה כדי לאפשר resize יחיד באיכות מקסימלית בעת
# overlay על QR. 2048 מכסה 99% של לוגואים בעולם האמיתי, ומונע upscale מטשטש
# כשהמשתמש בוחר scale גבוה (30x = QR ~1230px → לוגו ~270-340px).
MAX_LOGO_DIM = 2048
ALLOWED_FORMATS = {"PNG", "JPEG"}    # שמות פורמטים של PIL
LOGO_WIDTH_RATIO = 0.22              # יחס רוחב הלוגו לרוחב ה-QR
WHITE_PADDING_RATIO = 1.10           # ריבוע לבן: 110% מגודל הלוגו (מרווח 5% מכל צד)


class LogoValidationError(ValueError):
    """שגיאת ולידציה של לוגו (פורמט/גודל/תוכן). מועברת לשכבת ה-UI."""


def process_uploaded_logo(raw_bytes: bytes) -> tuple[bytes, str]:
    """ולידציה + נרמול של לוגו שהועלה.

    Args:
        raw_bytes: תוכן הקובץ הגולמי שהמשתמש העלה.

    Returns:
        (processed_bytes, mime_type) — תמיד PNG עם RGBA (תומך שקיפות).

    Raises:
        LogoValidationError: אם הקובץ לא תקין או חורג מהגבולות.
    """
    if not raw_bytes:
        raise LogoValidationError("הקובץ ריק.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise LogoValidationError(
            f"הקובץ גדול מדי ({len(raw_bytes) // 1024}KB). "
            f"מקסימום מותר: {MAX_UPLOAD_BYTES // (1024 * 1024)}MB."
        )

    # ולידציה דרך PIL — חוסם קבצים לא תקינים גם אם ה-mime type "נכון"
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()  # מאלץ קריאת הפיקסלים — חושף קבצים שבורים
    except UnidentifiedImageError:
        raise LogoValidationError("הקובץ אינו תמונה תקינה.")
    except Exception as e:
        logger.error("שגיאה בפתיחת לוגו: %s", e)
        raise LogoValidationError("לא ניתן לקרוא את התמונה.")

    if img.format not in ALLOWED_FORMATS:
        raise LogoValidationError(
            f"פורמט {img.format or 'לא ידוע'} לא נתמך. השתמשו ב-PNG או JPG."
        )

    # ── המרה ל-RGBA כדי לתמוך באחידות בשקיפות ─────────────────────────────
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # ── pad לריבוע עם רקע שקוף ─────────────────────────────────────────────
    # אם הלוגו מלבני, נוסיף רקע שקוף בצדדים כדי שיהפוך לריבוע בלי לחתוך תוכן.
    img = _pad_to_square_transparent(img)

    # ── resize ל-512x512 max (אם גדול יותר) ───────────────────────────────
    if img.width > MAX_LOGO_DIM:
        img = img.resize((MAX_LOGO_DIM, MAX_LOGO_DIM), Image.LANCZOS)

    # ── שמירה כ-PNG (שומר שקיפות) ─────────────────────────────────────────
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png"


def overlay_logo_on_qr(qr_png_bytes: bytes, logo_png_bytes: bytes) -> bytes:
    """שילוב לוגו במרכז QR Code.

    הלוגו מצויר במרכז בגודל 22% מרוחב ה-QR, מעל ריבוע לבן מעט גדול יותר
    (110%) כדי שהגבולות חדים. עם ECC=H (כבר בשימוש בקוד) — איבוד של ~5%
    משטח הקוד נמצא הרבה מתחת לסף 30% של תיקון השגיאות.

    Args:
        qr_png_bytes: ה-QR גולמי (PNG bytes) מ-segno.
        logo_png_bytes: לוגו מנורמל (PNG עם RGBA) מ-process_uploaded_logo.

    Returns:
        PNG bytes של ה-QR עם הלוגו.
    """
    qr_img = Image.open(io.BytesIO(qr_png_bytes)).convert("RGBA")
    logo_img = Image.open(io.BytesIO(logo_png_bytes)).convert("RGBA")

    qr_w, qr_h = qr_img.size
    logo_size = int(qr_w * LOGO_WIDTH_RATIO)
    pad_size = int(logo_size * WHITE_PADDING_RATIO)

    # resize הלוגו לגודל היעד
    logo_img = logo_img.resize((logo_size, logo_size), Image.LANCZOS)

    # ריבוע לבן (אטום) מתחת ללוגו — מנקה את הדפוס של ה-QR מתחתיו
    white_pad = Image.new("RGBA", (pad_size, pad_size), (255, 255, 255, 255))

    # מיקום במרכז ה-QR
    pad_x = (qr_w - pad_size) // 2
    pad_y = (qr_h - pad_size) // 2
    logo_x = (qr_w - logo_size) // 2
    logo_y = (qr_h - logo_size) // 2

    qr_img.paste(white_pad, (pad_x, pad_y))
    qr_img.paste(logo_img, (logo_x, logo_y), logo_img)  # mask=logo עצמו (תומך שקיפות)

    out = io.BytesIO()
    qr_img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _pad_to_square_transparent(img: Image.Image) -> Image.Image:
    """משלים תמונה לריבוע ע"י הוספת רקע שקוף בצדדים הקצרים.

    אם התמונה כבר ריבועית — מוחזרת כמו שהיא.
    """
    w, h = img.size
    if w == h:
        return img

    side = max(w, h)
    padded = Image.new("RGBA", (side, side), (0, 0, 0, 0))  # שקוף
    paste_x = (side - w) // 2
    paste_y = (side - h) // 2
    padded.paste(img, (paste_x, paste_y), img if img.mode == "RGBA" else None)
    return padded
