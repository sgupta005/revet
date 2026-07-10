"""GitHub Git Data + Pulls write helpers for auto-PR (Phase 9).

Auto-PR builds ONE clean commit on a new branch via the Git Data API (create a
tree from the base tree with the changed/deleted entries → create a commit →
create the branch ref at that commit), then opens a PR. This is separate from
`pulls.py` (which only *reads* PRs and posts reviews) because these are write
operations against the git database.
"""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class OpenedPR:
    number: int
    html_url: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def get_branch_head(
    client: httpx.AsyncClient, repo: str, branch: str, token: str
) -> tuple[str, str]:
    """Return `(commit_sha, tree_sha)` for the tip of `branch` — the parent commit
    and base tree the new commit builds on."""
    ref = await client.get(
        f"/repos/{repo}/git/ref/heads/{branch}", headers=_headers(token)
    )
    ref.raise_for_status()
    commit_sha = ref.json()["object"]["sha"]
    commit = await client.get(
        f"/repos/{repo}/git/commits/{commit_sha}", headers=_headers(token)
    )
    commit.raise_for_status()
    return commit_sha, commit.json()["tree"]["sha"]


async def create_tree(
    client: httpx.AsyncClient,
    repo: str,
    base_tree: str,
    entries: list[dict],
    token: str,
) -> str:
    """Create a new tree from `base_tree` with `entries` applied; returns its sha.
    Each entry is `{path, mode, type, content}` (create/update) or
    `{path, mode, type, sha: None}` (delete — a null sha removes the path)."""
    resp = await client.post(
        f"/repos/{repo}/git/trees",
        json={"base_tree": base_tree, "tree": entries},
        headers=_headers(token),
    )
    resp.raise_for_status()
    return resp.json()["sha"]


async def create_commit(
    client: httpx.AsyncClient,
    repo: str,
    message: str,
    tree: str,
    parent: str,
    token: str,
) -> str:
    """Create a commit with the given tree and single parent; returns its sha."""
    resp = await client.post(
        f"/repos/{repo}/git/commits",
        json={"message": message, "tree": tree, "parents": [parent]},
        headers=_headers(token),
    )
    resp.raise_for_status()
    return resp.json()["sha"]


async def create_ref(
    client: httpx.AsyncClient, repo: str, branch: str, sha: str, token: str
) -> None:
    """Create branch ref `refs/heads/{branch}` pointing at `sha`."""
    resp = await client.post(
        f"/repos/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        headers=_headers(token),
    )
    resp.raise_for_status()


async def create_pull_request(
    client: httpx.AsyncClient,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str,
    token: str,
) -> OpenedPR:
    """Open a PR from `head` into `base`; returns its number + html_url."""
    resp = await client.post(
        f"/repos/{repo}/pulls",
        json={"title": title, "head": head, "base": base, "body": body},
        headers=_headers(token),
    )
    resp.raise_for_status()
    data = resp.json()
    return OpenedPR(number=data["number"], html_url=data["html_url"])
