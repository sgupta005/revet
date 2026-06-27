import httpx
from pydantic import BaseModel

from app.config import settings
from app.github.constants import GITHUB_API, GITHUB_OAUTH_BASE


class OAuthError(Exception):
    """Raised when GitHub returns an `error` from the token endpoint (bad/expired
    code or refresh token); handlers map it to a 401."""


class OAuthTokens(BaseModel):
    """User-to-server tokens from the OAuth code/refresh exchange. `refresh_token`
    and `expires_in` are empty/0 when the App issues non-expiring user tokens."""

    access_token: str
    refresh_token: str = ""
    expires_in: int = 0


class GitHubIdentity(BaseModel):
    """The logged-in person's durable identity (`GET /user`)."""

    id: int
    login: str
    avatar_url: str


class GitHubInstallation(BaseModel):
    """One installation the user can access (`GET /user/installations`)."""

    id: int  # GitHub installation id, used as the access-check capability
    account_login: str
    account_type: str


class GitHubRepo(BaseModel):
    """One repo the user can access within an installation."""

    full_name: str
    github_repo_id: int


def _user_headers(user_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
    }


async def exchange_code(code: str) -> OAuthTokens:
    """Exchange an OAuth `code` for user tokens; the client secret is read from env
    only and never returned to the caller."""
    return await _token_request({"grant_type": "authorization_code", "code": code})


async def refresh_user_token(refresh_token: str) -> OAuthTokens:
    """Exchange a refresh token for a fresh user access token (expiring-token mode)."""
    return await _token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


async def _token_request(grant: dict[str, str]) -> OAuthTokens:
    async with httpx.AsyncClient(base_url=GITHUB_OAUTH_BASE, timeout=10) as client:
        resp = await client.post(
            "/login/oauth/access_token",
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                **grant,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise OAuthError(data.get("error_description", data["error"]))
    return OAuthTokens.model_validate(data)


async def get_authenticated_user(user_token: str) -> GitHubIdentity:
    """Resolve the user token to a GitHub identity (`GET /user`)."""
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        resp = await client.get("/user", headers=_user_headers(user_token))
        resp.raise_for_status()
        return GitHubIdentity.model_validate(resp.json())


async def list_user_installations(user_token: str) -> list[GitHubInstallation]:
    """Every installation this user can access — the source of truth for access checks."""
    items = await _paginate(user_token, "/user/installations", "installations")
    return [
        GitHubInstallation(
            id=item["id"],
            account_login=item["account"]["login"],
            account_type=item["account"]["type"],
        )
        for item in items
    ]


async def list_installation_repositories(
    user_token: str, installation_id: int
) -> list[GitHubRepo]:
    """Repos in an installation this user can access (the index/chat candidates)."""
    items = await _paginate(
        user_token,
        f"/user/installations/{installation_id}/repositories",
        "repositories",
    )
    return [
        GitHubRepo(full_name=item["full_name"], github_repo_id=item["id"])
        for item in items
    ]


async def _paginate(user_token: str, path: str, key: str) -> list[dict]:
    """Collect every page of a user-token list endpoint that wraps its array in `key`."""
    results: list[dict] = []
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        page = 1
        while True:
            resp = await client.get(
                path,
                params={"per_page": 100, "page": page},
                headers=_user_headers(user_token),
            )
            resp.raise_for_status()
            batch = resp.json()[key]
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1
