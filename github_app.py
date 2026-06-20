"""
GitHub App helper module.

Design principle:
  - The GitHub App Private Key (PEM) lives only in Secret Manager / env var.
  - The Installation Access Token (IAT) is generated on-demand and NEVER persisted to DB.
  - DB only stores the non-sensitive installation_id (integer).
"""

import os
import re
import time
import base64
import logging
import httpx
from typing import Optional, Union

logger = logging.getLogger(__name__)

GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY")  # PEM string
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# JWT generation (RS256) – requires PyJWT[crypto]
# ---------------------------------------------------------------------------

def _generate_app_jwt() -> str:
    """
    Generate a short-lived GitHub App JWT signed with the App private key.
    Valid for 10 minutes (GitHub maximum).
    """
    try:
        import jwt as pyjwt
    except ImportError:
        raise RuntimeError(
            "PyJWT with crypto extras is required: pip install 'PyJWT[crypto]'"
        )

    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        raise RuntimeError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY environment variables must be set."
        )

    now = int(time.time())
    payload = {
        "iat": now - 60,        # issued-at with 60s clock skew buffer
        "exp": now + (10 * 60), # 10 minute expiry (GitHub max)
        "iss": str(GITHUB_APP_ID),
    }

    # Private key may be stored with escaped newlines (\n as literal string)
    private_key = GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")

    token = pyjwt.encode(payload, private_key, algorithm="RS256")
    # pyjwt >=2.0 returns str directly
    return token if isinstance(token, str) else token.decode("utf-8")


# ---------------------------------------------------------------------------
# Installation Access Token (IAT)
# ---------------------------------------------------------------------------

def get_installation_access_token(installation_id: Union[str, int]) -> str:
    """
    Exchange an installation_id for a short-lived Installation Access Token.

    The token is valid for ~1 hour and is NEVER stored in the database.
    Call this function each time you need to access GitHub on behalf of the installation.
    """
    app_jwt = _generate_app_jwt()
    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    if resp.status_code != 201:
        raise RuntimeError(
            f"Failed to get IAT for installation {installation_id}: "
            f"HTTP {resp.status_code} – {resp.text}"
        )

    return resp.json()["token"]


# ---------------------------------------------------------------------------
# GitHub repository URL parsing
# ---------------------------------------------------------------------------

def parse_github_repo_url(url: str) -> tuple[str, str]:
    """
    Parse a GitHub repository URL and return (owner, repo).

    Supported formats:
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
    """
    url = url.strip().rstrip("/")

    # HTTPS
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    # SSH
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    raise ValueError(f"Cannot parse GitHub URL: {url!r}")


# ---------------------------------------------------------------------------
# GitHub Contents API – file fetching
# ---------------------------------------------------------------------------

def get_github_file_content(
    owner: str,
    repo: str,
    path: str,
    token: str,
    ref: str = "HEAD",
) -> Optional[str]:
    """
    Fetch the raw content of a single file from a GitHub repository.

    Returns the decoded file content as a string, or None if the file
    does not exist or an error occurs.

    Uses the GitHub Contents API (max 1 MB per file).
    """
    path = path.lstrip("/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            url,
            headers=headers,
            params={"ref": ref},
        )

    if resp.status_code == 404:
        logger.debug("File not found in GitHub: %s/%s/%s", owner, repo, path)
        return None

    if resp.status_code != 200:
        logger.warning(
            "GitHub Contents API error for %s/%s/%s: HTTP %s",
            owner, repo, path, resp.status_code,
        )
        return None

    data = resp.json()

    # The API returns base64-encoded content for files
    if data.get("type") != "file":
        logger.debug("Path is not a file: %s/%s/%s (type=%s)", owner, repo, path, data.get("type"))
        return None

    encoded = data.get("content", "")
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to decode file content for %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Utility: build clone URL with IAT (for MCP server use)
# ---------------------------------------------------------------------------

def build_authenticated_clone_url(repo_url: str, iat: str) -> str:
    """
    Convert a plain GitHub HTTPS URL to an authenticated clone URL using the IAT.

    Example:
      https://github.com/owner/repo.git
      → https://x-access-token:{iat}@github.com/owner/repo.git
    """
    repo_url = repo_url.strip()
    # Normalise to HTTPS
    repo_url = re.sub(r"^git@github\.com:", "https://github.com/", repo_url)
    repo_url = repo_url if repo_url.endswith(".git") else repo_url + ".git"
    return repo_url.replace("https://", f"https://x-access-token:{iat}@")
