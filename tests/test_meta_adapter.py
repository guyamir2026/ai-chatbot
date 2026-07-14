"""
טסטים ל-messaging/meta_adapter.py.

חשוב במיוחד: **round-trip** — כדי לתפוס באגים של פיצול prefix לפני
שהם מגיעים ל-Graph API (יועץ חיצוני הדגיש את זה כקריטי).
"""

import pytest

from messaging.meta_adapter import (
    CHANNEL_IG,
    CHANNEL_MSG,
    InvalidUserIdError,
    parse_channel,
    to_internal_user_id,
    to_provider_recipient,
)


class TestToInternalUserId:
    def test_ig(self):
        assert to_internal_user_id(CHANNEL_IG, "1784012345") == "meta_ig:1784012345"

    def test_msg(self):
        assert to_internal_user_id(CHANNEL_MSG, "9876543210") == "meta_msg:9876543210"

    def test_invalid_channel(self):
        with pytest.raises(InvalidUserIdError, match="channel="):
            to_internal_user_id("telegram", "123")

    def test_empty_external_id(self):
        with pytest.raises(InvalidUserIdError, match="external_id ריק"):
            to_internal_user_id(CHANNEL_IG, "")


class TestToProviderRecipient:
    def test_ig(self):
        assert to_provider_recipient("meta_ig:1784012345") == "1784012345"

    def test_msg(self):
        assert to_provider_recipient("meta_msg:9876543210") == "9876543210"

    def test_no_prefix_raises(self):
        with pytest.raises(InvalidUserIdError, match="prefix חסר"):
            to_provider_recipient("1784012345")

    def test_wrong_prefix_raises(self):
        with pytest.raises(InvalidUserIdError):
            to_provider_recipient("telegram:123")

    def test_only_prefix_raises(self):
        with pytest.raises(InvalidUserIdError, match="prefix בלי id"):
            to_provider_recipient("meta_ig:")


class TestRoundTrip:
    """ה-property הקריטי — to_provider(to_internal(c, x)) == x לכל x תקין."""

    @pytest.mark.parametrize("channel", [CHANNEL_IG, CHANNEL_MSG])
    @pytest.mark.parametrize("external_id", [
        "1784012345",
        "9876543210987654",  # PSID טיפוסי באורך 16
        "1",  # מינימלי
        "id_with_underscores_123",  # מטא לא משתמשת בזה אבל הקוד חייב לתמוך
    ])
    def test_round_trip(self, channel, external_id):
        internal = to_internal_user_id(channel, external_id)
        assert to_provider_recipient(internal) == external_id
        assert parse_channel(internal) == channel


class TestParseChannel:
    def test_ig(self):
        assert parse_channel("meta_ig:123") == "meta_ig"

    def test_msg(self):
        assert parse_channel("meta_msg:456") == "meta_msg"

    def test_non_meta_raises(self):
        with pytest.raises(InvalidUserIdError):
            parse_channel("telegram:123")

    def test_no_prefix_raises(self):
        with pytest.raises(InvalidUserIdError):
            parse_channel("9876543210")
