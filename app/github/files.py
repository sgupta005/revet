import base64
import binascii
from dataclasses import dataclass

import httpx

from ai.indexing.languages import  is_indexable
from ai.constants import MAX_FILE_BYTES


@dataclass(frozen=True, slots=True)
class RepoFile:
    path: str
    content: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def _decode_blob(raw: str, encoding: str) -> str | None:
    if encoding != "base64":
        return None
    try:
        data = base64.b64decode(raw)
    except (binascii.Error, ValueError):
        return None
    if b"\x00" in data:  # binary file slipped past the extension allow-list
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def get_default_branch(client: httpx.AsyncClient, repo: str, token: str) -> str:
    resp = await client.get(f"/repos/{repo}", headers=_headers(token))
    resp.raise_for_status()
    return resp.json()["default_branch"]


async def list_indexable_blobs(
    client: httpx.AsyncClient, repo: str, branch: str, token: str
) -> list[tuple[str, str]]:
    resp = await client.get(
        f"/repos/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=_headers(token),
    )
    resp.raise_for_status()
    tree = resp.json()["tree"]
    return [
        (entry["path"], entry["sha"])
        for entry in tree
        if entry["type"] == "blob"
        and entry.get("size", 0) <= MAX_FILE_BYTES
        and is_indexable(entry["path"])
    ]


async def get_blob(
    client: httpx.AsyncClient, repo: str, path: str, sha: str, token: str
) -> RepoFile | None:
    resp = await client.get(f"/repos/{repo}/git/blobs/{sha}", headers=_headers(token))
    resp.raise_for_status()
    payload = resp.json()
    text = _decode_blob(payload["content"], payload["encoding"])
    if text is None:
        return None
    return RepoFile(path=path, content=text)


async def list_tree(
    client: httpx.AsyncClient, repo: str, branch: str, token: str
) -> list[str]:
    """Return every file path in the repo tree (unfiltered) for agent navigation."""
    resp = await client.get(
        f"/repos/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=_headers(token),
    )
    resp.raise_for_status()
    return [e["path"] for e in resp.json()["tree"] if e["type"] == "blob"]


async def list_dir(
    client: httpx.AsyncClient, repo: str, path: str, ref: str, token: str
) -> list[tuple[str, str]]:
    """Return `(name, type)` entries under a directory; empty when the path is a
    file or missing (Contents API returns an object, not a list, for files)."""
    resp = await client.get(
        f"/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_headers(token),
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        return []
    return [(e["name"], e["type"]) for e in payload]


async def get_file(
    client: httpx.AsyncClient, repo: str, path: str, ref: str, token: str
) -> RepoFile | None:
    resp = await client.get(
        f"/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_headers(token),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("type") != "file" or payload.get("size", 0) > MAX_FILE_BYTES:
        return None
    text = _decode_blob(payload["content"], payload["encoding"])
    if text is None:
        return None
    return RepoFile(path=path, content=text)
