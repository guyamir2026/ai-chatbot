"""
טסטים ל-messaging/meta_sender.py.
מוקה: requests, ולא קוראים ל-Graph API אמיתי.
"""

from unittest.mock import MagicMock

import pytest


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _set_meta_config(monkeypatch):
    import messaging.meta_graph_client as mgc
    monkeypatch.setattr(mgc, "META_APP_ID", "fake-app-id")
    monkeypatch.setattr(mgc, "META_APP_SECRET", "fake-app-secret")
    monkeypatch.setattr(mgc, "META_GRAPH_API_VERSION", "v21.0")


class TestSendMetaMessage:
    def test_success_returns_message_id(self, monkeypatch):
        import messaging.meta_sender as ms
        import requests
        mock_post = MagicMock(return_value=_mock_response(
            json_data={"recipient_id": "RECEIVER_1", "message_id": "MID_123"}
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        msg_id = ms.send_meta_message(
            recipient_external_id="RECEIVER_1",
            text="שלום",
            page_token="page-tok-xyz",
        )

        assert msg_id == "MID_123"
        # מאמת שהקריאה למטא בנויה נכון
        args, kwargs = mock_post.call_args
        assert "me/messages" in args[0]
        assert kwargs["params"]["access_token"] == "page-tok-xyz"
        body = kwargs["json"]
        assert body["recipient"] == {"id": "RECEIVER_1"}
        assert body["message"] == {"text": "שלום"}
        assert body["messaging_type"] == "RESPONSE"

    def test_graph_error_raises(self, monkeypatch):
        import messaging.meta_sender as ms
        import requests
        from messaging.meta_graph_client import MetaGraphError
        mock_post = MagicMock(return_value=_mock_response(
            status_code=400,
            json_data={"error": {"message": "Invalid recipient", "code": 100}},
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        with pytest.raises(MetaGraphError, match="Invalid recipient"):
            ms.send_meta_message("BAD", "x", "tok")

    def test_missing_message_id_raises(self, monkeypatch):
        """HTTP 200 בלי message_id ⇒ נחשב לכשל לוגי."""
        import messaging.meta_sender as ms
        import requests
        from messaging.meta_graph_client import MetaGraphError
        mock_post = MagicMock(return_value=_mock_response(
            json_data={"recipient_id": "R"}
        ))
        monkeypatch.setattr(requests, "post", mock_post)

        with pytest.raises(MetaGraphError, match="לא חזר message_id"):
            ms.send_meta_message("R", "x", "tok")

    def test_non_json_body_raises(self, monkeypatch):
        import messaging.meta_sender as ms
        import requests
        from messaging.meta_graph_client import MetaGraphError
        bad = _mock_response()
        bad.json = MagicMock(side_effect=ValueError("not json"))
        monkeypatch.setattr(requests, "post", MagicMock(return_value=bad))

        with pytest.raises(MetaGraphError, match="JSON תקין"):
            ms.send_meta_message("R", "x", "tok")
