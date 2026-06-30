from dataclasses import dataclass
import httpx

from .constants import MAX_FILE_PAGES, PER_PAGE

@dataclass(frozen=True, slots=True)
class PRFile:
    path: str
    status: str
    patch: str  # unified-diff hunk; empty for binary/too-large files GitHub omits


@dataclass(frozen=True, slots=True)
class PullRequestData:
    number: int
    title: str
    body: str
    state: str
    files: list[PRFile]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def _list_files(
    client: httpx.AsyncClient, repo: str, number: int, token: str
) -> list[PRFile]:
    """Page through a PR's changed files (bounded by MAX_FILE_PAGES so a huge PR
    never fans out unbounded GitHub calls)."""
    files: list[PRFile] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        resp = await client.get(
            f"/repos/{repo}/pulls/{number}/files",
            params={"per_page": PER_PAGE, "page": page},
            headers=_headers(token),
        )
        resp.raise_for_status()
        batch = resp.json()
        files.extend(
            PRFile(path=f["filename"], status=f["status"], patch=f.get("patch", ""))
            for f in batch
        )
        if len(batch) < PER_PAGE:
            break
    return files


async def fetch_pull_request(
    client: httpx.AsyncClient, repo: str, number: int, token: str
) -> PullRequestData:
    """Fetch a PR's metadata plus its changed files in one call site, using the
    scoped installation token."""
    resp = await client.get(f"/repos/{repo}/pulls/{number}", headers=_headers(token))
    resp.raise_for_status()
    pr = resp.json()
    files = await _list_files(client, repo, number, token)
    return PullRequestData(
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        state=pr.get("state") or "open",
        files=files,
    )


async def post_review(
    client: httpx.AsyncClient, repo: str, number: int, body: str, token: str
) -> None:
    """Post one PR review as a summary comment (event=COMMENT — never approves or
    requests changes, and needs no per-line diff position mapping)."""
    resp = await client.post(
        f"/repos/{repo}/pulls/{number}/reviews",
        json={"body": body, "event": "COMMENT"},
        headers=_headers(token),
    )
    resp.raise_for_status()
