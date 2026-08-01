import logging
import requests
import config

logger = logging.getLogger(__name__)
TIMEOUT = (5, 20)
MAX_LIMIT = 20

# ``.env.example`` ships this placeholder, so a copied-but-unedited file yields a
# non-empty token that fails every request with 401.  Treat it as unconfigured.
_PLACEHOLDER_TOKENS = {"your_groupme_token", "your_groupme_access_token", "changeme"}

_AUTH_FAILURE_MESSAGE = (
    "GroupMe rejected the stored access token (HTTP 401). "
    "Regenerate it at https://dev.groupme.com/ and update GROUPME_ACCESS_TOKEN in .env."
)

_UNCONFIGURED_MESSAGE = (
    "GroupMe access token is not configured. "
    "Create one at https://dev.groupme.com/ and set GROUPME_ACCESS_TOKEN in .env."
)


def _access_token() -> str | None:
    """Return a usable token, or None when it is missing or a placeholder."""
    token = (config.GROUPME_TOKEN or "").strip()
    if not token or token.lower() in _PLACEHOLDER_TOKENS:
        return None
    return token


def _describe_failure(exc: requests.RequestException, action: str) -> str:
    """Log an actionable reason and return the user-facing fallback text.

    A bare ``HTTPError`` hides the status code, which makes an expired token
    indistinguishable from a transient outage in the logs.  Surface the status
    so a revoked credential is obvious without re-running the request by hand.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        logger.error("GroupMe %s failed: HTTP %s unauthorized — access token is invalid or expired.", action, status)
        return _AUTH_FAILURE_MESSAGE
    if status is not None:
        logger.warning("GroupMe %s failed: HTTP %s.", action, status)
    else:
        logger.warning("GroupMe %s failed: %s.", action, type(exc).__name__)
    return "GroupMe is temporarily unavailable."


def get_groups():
    """Fetch all groups the user is a part of to find their IDs."""
    token = _access_token()
    if not token:
        logger.warning("GroupMe group listing skipped: access token is missing or a placeholder.")
        return _UNCONFIGURED_MESSAGE

    url = "https://api.groupme.com/v3/groups"
    params = {"token": token, "per_page": 100}
    
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        groups = response.json().get('response', [])
        
        if not groups:
            return "No groups found."
            
        result = ["📋 **Your GroupMe Groups:**"]
        for g in groups:
            result.append(f"- {g['name']} (ID: {g['id']})")
        return "\n".join(result)
    except requests.RequestException as exc:
        return _describe_failure(exc, "group listing")

def get_latest_messages(group_id, limit=5):
    """Fetch the latest messages from a specific GroupMe group."""
    token = _access_token()
    if not token:
        logger.warning("GroupMe message fetch skipped: access token is missing or a placeholder.")
        return _UNCONFIGURED_MESSAGE

    group_id = str(group_id)
    if not group_id.isdigit() or len(group_id) > 32:
        return "Invalid GroupMe group ID."
    limit = max(1, min(int(limit), MAX_LIMIT))

    url = f"https://api.groupme.com/v3/groups/{group_id}/messages"
    params = {
        "token": token,
        "limit": limit
    }

    try:
        response = requests.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        messages = response.json().get('response', {}).get('messages', [])
        
        if not messages:
            return "No messages found in this group."
            
        result = [f"💬 **Recent GroupMe Messages:**"]
        for msg in reversed(messages):
            sender = msg.get('name', 'Unknown')
            text = msg.get('text', '')
            if text:
                result.append(f"**{sender}**: {text}")
                
        return "\n".join(result)

    except requests.RequestException as exc:
        return _describe_failure(exc, "messages")

if __name__ == "__main__":
    print("Testing GroupMe API connection...")
    print(get_groups())
