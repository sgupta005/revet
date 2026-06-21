import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.db.models import Installation, Repository
from app.db.session import AsyncSessionLocal
from app.github.constants import DEDUP_KEY, DEDUP_TTL
from app.redis_client import get_redis
from app.workers.tasks import analyze_issue, auto_pr, index_repo, review_pr

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
) -> Response:
    body = await request.body()
    if not _verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    redis = get_redis()
    first_seen = await redis.set(
        DEDUP_KEY.format(delivery_id=x_github_delivery), "1", nx=True, ex=DEDUP_TTL
    )
    if not first_seen:
        return Response(status_code=200)

    payload = json.loads(body)
    await _dispatch(x_github_event, payload)
    return Response(status_code=200)


async def _dispatch(event: str, payload: dict) -> None:
    if event == "installation":
        await _handle_installation(payload)
    elif event == "installation_repositories":
        await _handle_installation_repositories(payload)
    elif event == "push":
        _handle_push(payload)
    elif event == "pull_request":
        _handle_pull_request(payload)
    elif event == "issues":
        _handle_issues(payload)
    else:
        logger.info("ignoring webhook event=%s action=%s", event, payload.get("action"))


async def _handle_installation(payload: dict) -> None:
    if payload.get("action") != "created":
        return
    inst = payload["installation"]
    repos = payload.get("repositories", [])
    async with AsyncSessionLocal() as session:
        installation = await _upsert_installation(session, inst)
        for repo in repos:
            await _upsert_repository(session, installation.id, repo)
        await session.commit()
    for repo in repos:
        index_repo.delay(repo["full_name"], inst["id"])


async def _handle_installation_repositories(payload: dict) -> None:
    if payload.get("action") != "added":
        return
    inst = payload["installation"]
    added = payload.get("repositories_added", [])
    async with AsyncSessionLocal() as session:
        installation = await _upsert_installation(session, inst)
        for repo in added:
            await _upsert_repository(session, installation.id, repo)
        await session.commit()
    for repo in added:
        index_repo.delay(repo["full_name"], inst["id"])


def _handle_push(payload: dict) -> None:
    index_repo.delay(
        payload["repository"]["full_name"],
        payload["installation"]["id"],
        _changed_paths(payload),
    )


def _handle_pull_request(payload: dict) -> None:
    if payload.get("action") not in {"opened", "synchronize"}:
        return
    review_pr.delay(
        payload["repository"]["full_name"],
        payload["installation"]["id"],
        payload["pull_request"]["number"],
    )


def _handle_issues(payload: dict) -> None:
    action = payload.get("action")
    repo_full_name = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    number = payload["issue"]["number"]
    if action == "opened":
        analyze_issue.delay(repo_full_name, installation_id, number)
    elif action == "labeled" and payload.get("label", {}).get("name") == "auto-fix":
        auto_pr.delay(repo_full_name, installation_id, number)


def _changed_paths(payload: dict) -> list[str]:
    paths: set[str] = set()
    for commit in payload.get("commits", []):
        paths.update(commit.get("added", []))
        paths.update(commit.get("modified", []))
        paths.update(commit.get("removed", []))
    return sorted(paths)


async def _upsert_installation(session: AsyncSession, inst: dict) -> Installation:
    result = await session.execute(
        select(Installation).where(
            Installation.github_installation_id == inst["id"]
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    account = inst["account"]
    installation = Installation(
        github_installation_id=inst["id"],
        account_login=account["login"],
        account_type=account["type"],
    )
    session.add(installation)
    await session.flush()
    return installation


async def _upsert_repository(
    session: AsyncSession, installation_pk: int, repo: dict
) -> None:
    result = await session.execute(
        select(Repository).where(Repository.full_name == repo["full_name"])
    )
    if result.scalar_one_or_none():
        return
    session.add(
        Repository(
            installation_id=installation_pk,
            full_name=repo["full_name"],
            github_repo_id=repo["id"],
        )
    )
