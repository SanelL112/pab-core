import json
from unittest.mock import MagicMock, patch

from scrapers import composio_fetcher


def _sse_payload(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def test_canvas_expired_token_is_reported_explicitly_and_connection_is_closed():
    connection = MagicMock()
    response = MagicMock(status=200)
    response.read.return_value = _sse_payload({"error": {"message": "Expired access token."}})
    connection.getresponse.return_value = response

    with patch.object(composio_fetcher, "_load_token", return_value="test-token"), patch.object(
        composio_fetcher.http.client, "HTTPSConnection", return_value=connection
    ):
        result = composio_fetcher._call_mcp("CANVAS_GET_CURRENT_USER", {})

    assert result["successful"] is False
    assert result["data"]["error_code"] == "CANVAS_REAUTH_REQUIRED"
    assert "re-authenticate" in result["data"]["message"].lower()
    connection.close.assert_called_once()


def test_mcp_accepts_sse_tool_results():
    connection = MagicMock()
    response = MagicMock(status=200)
    response.read.return_value = _sse_payload({
        "result": {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "data": {"results": [{"response": {"successful": True, "data": {"response_data": ["ok"]}}}]}
                }),
            }]
        }
    })
    connection.getresponse.return_value = response

    with patch.object(composio_fetcher, "_load_token", return_value="test-token"), patch.object(
        composio_fetcher.http.client, "HTTPSConnection", return_value=connection
    ):
        result = composio_fetcher._call_mcp("GMAIL_FETCH_EMAILS", {})

    assert result == {"successful": True, "data": {"response_data": ["ok"]}}
    connection.close.assert_called_once()
