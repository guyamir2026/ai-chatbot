"""
Meta adapter — המרה בין מזהי ספק (PSID/IGSID) ל-`user_id` פנימי.

מטרה: שכבת גבול אחת בלבד בין "מזהה Graph API" ל"מזהה DB פנימי".
שאר הקוד (handlers, DB, לוגים) רואים רק `user_id` מנורמל עם prefix
(`meta_ig:<igsid>`, `meta_msg:<psid>`). הקריאות ל-Graph API (שמצפות
ל-PSID/IGSID טהור) עוברות דרך `to_provider_recipient`.

החלטות:
- prefix של channel בלבד (לא asset_id) — `user_id` הוא immutable; asset_id
  משתנה (החלפת page/account) ולא צריך לחיות במפתח הראשי. provenance
  של asset נשמר בעמודות נפרדות ב-`users` (provider_asset_id).
- שכבת הגנה ב-DB: UNIQUE(channel, provider_asset_id, external_user_id)
  מבטיחה ש-(asset, raw_id) לא ימופה לשני user_ids שונים בטעות.
"""
from __future__ import annotations

CHANNEL_IG = "meta_ig"
CHANNEL_MSG = "meta_msg"

_VALID_META_CHANNELS = (CHANNEL_IG, CHANNEL_MSG)


class InvalidUserIdError(ValueError):
    """`user_id` לא תואם לפורמט הצפוי לערוץ."""


def to_internal_user_id(channel: str, external_id: str) -> str:
    """ממיר מזהה ספק (PSID/IGSID) ל-user_id פנימי עם prefix של channel.

    דוגמאות:
        to_internal_user_id("meta_ig", "1784012345") → "meta_ig:1784012345"
        to_internal_user_id("meta_msg", "9876543210") → "meta_msg:9876543210"
    """
    if channel not in _VALID_META_CHANNELS:
        raise InvalidUserIdError(
            f"channel='{channel}' לא נתמך. ערוצים תקפים: {_VALID_META_CHANNELS}"
        )
    if not external_id:
        raise InvalidUserIdError("external_id ריק — אסור")
    return f"{channel}:{external_id}"


def to_provider_recipient(internal_user_id: str) -> str:
    """מפשיט את ה-prefix של channel ומחזיר את ה-PSID/IGSID הטהור.

    זה ה-id ש-Graph API מצפה לו בכל קריאה (Send API, User Profile וכו').
    כל קוד שיוצא למטא חייב לעבור דרך כאן ולא להניח שה-user_id "נקי".

    דוגמאות:
        to_provider_recipient("meta_ig:1784012345") → "1784012345"
        to_provider_recipient("meta_msg:9876543210") → "9876543210"
    """
    for prefix in _VALID_META_CHANNELS:
        full_prefix = f"{prefix}:"
        if internal_user_id.startswith(full_prefix):
            external = internal_user_id[len(full_prefix):]
            if not external:
                raise InvalidUserIdError(
                    f"user_id='{internal_user_id}' מכיל prefix בלי id"
                )
            return external
    raise InvalidUserIdError(
        f"user_id='{internal_user_id}' לא תואם לפורמט מטא "
        f"(prefix חסר; ערוצים תקפים: {_VALID_META_CHANNELS})"
    )


def parse_channel(internal_user_id: str) -> str:
    """מחזיר את ה-channel מתוך ה-prefix של ה-user_id.

    שימושי כשמקבלים user_id כקלט ורוצים לדעת לאיזה ערוץ הוא שייך
    לפני שמחליטים איך לענות.

    דוגמאות:
        parse_channel("meta_ig:123") → "meta_ig"
        parse_channel("meta_msg:456") → "meta_msg"
    """
    for prefix in _VALID_META_CHANNELS:
        if internal_user_id.startswith(f"{prefix}:"):
            return prefix
    raise InvalidUserIdError(
        f"user_id='{internal_user_id}' לא של ערוץ מטא"
    )
