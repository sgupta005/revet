from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class IssueData:
    number: int
    title: str
    body: str
    state: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def fetch_issue(
    client: httpx.AsyncClient, repo: str, number: int, token: str
) -> IssueData:
    """Fetch an issue's metadata using the scoped installation token."""
    resp = await client.get(f"/repos/{repo}/issues/{number}", headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    return IssueData(
        number=number,
        title=data.get("title") or "",
        body=data.get("body") or "",
        state=data.get("state") or "open",
    )


async def post_issue_comment(
    client: httpx.AsyncClient, repo: str, number: int, body: str, token: str
) -> None:
    """Post a comment on an issue (also used for the auto-PR link comment — a PR is
    an issue on GitHub's REST surface)."""
    resp = await client.post(
        f"/repos/{repo}/issues/{number}/comments",
        json={"body": body},
        headers=_headers(token),
    )
    resp.raise_for_status()
