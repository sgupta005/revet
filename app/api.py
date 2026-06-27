import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.dependencies import (
    AuthedUser,
    call_with_refresh,
    get_current_user,
    user_installations,
    verify_installation_access,
)
from app.auth.sessions import create_session, delete_session
from app.db.models import IndexingStatus, Installation, Repository, User
from app.db.session import get_session as get_db
from app.github.constants import USER_REPOS_KEY, USER_CACHE_TTL
from app.github.oauth import (
    GitHubRepo,
    OAuthError,
    exchange_code,
    get_authenticated_user,
    list_installation_repositories,
)
from app.redis_client import get_redis
from app.workers.tasks import index_repo

router = APIRouter()


class SessionRequest(BaseModel):
    code: str


class UserOut(BaseModel):
    id: int
    github_id: int
    login: str
    avatar_url: str


class InstallationOut(BaseModel):
    id: int  # GitHub installation id
    account_login: str
    account_type: str


class SessionResponse(BaseModel):
    session_token: str
    user: UserOut


class MeResponse(BaseModel):
    user: UserOut
    installations: list[InstallationOut]


class RepositoryOut(BaseModel):
    full_name: str
    indexing_status: IndexingStatus


class IndexStatusResponse(BaseModel):
    full_name: str
    indexing_status: IndexingStatus
    chunk_count: int


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        github_id=user.github_id,
        login=user.login,
        avatar_url=user.avatar_url,
    )


@router.post("/auth/session", response_model=SessionResponse)
async def auth_session(
    req: SessionRequest, db: AsyncSession = Depends(get_db)
) -> SessionResponse:
    """Exchange an OAuth `code` for a user token, upsert the `User`, and open a
    Redis session. The client secret and the user token stay backend-side — the
    browser receives only the opaque `session_token` (invariant #12)."""
    try:
        tokens = await exchange_code(req.code)
    except OAuthError:
        raise HTTPException(status_code=401, detail="oauth exchange failed")
    identity = await get_authenticated_user(tokens.access_token)

    result = await db.execute(select(User).where(User.github_id == identity.id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(github_id=identity.id, login=identity.login, avatar_url=identity.avatar_url)
        db.add(user)
    else:
        user.login = identity.login
        user.avatar_url = identity.avatar_url
    await db.commit()
    await db.refresh(user)

    session_token = await create_session(user.id, tokens)
    return SessionResponse(session_token=session_token, user=_user_out(user))


@router.post("/auth/logout", status_code=204)
async def auth_logout(authed: AuthedUser = Depends(get_current_user)) -> Response:
    """Invalidate the caller's Redis session."""
    await delete_session(authed.session_token)
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
async def me(authed: AuthedUser = Depends(get_current_user)) -> MeResponse:
    """Current user plus the installations they can access (`GET /user/installations`)."""
    installations = await user_installations(authed)
    return MeResponse(
        user=_user_out(authed.user),
        installations=[InstallationOut(**i.model_dump()) for i in installations],
    )


async def _live_repositories(
    authed: AuthedUser, installation_id: int, refresh: bool
) -> list[GitHubRepo]:
    """The installation's repos the user can access, briefly cached; `refresh`
    bypasses and repopulates the cache."""
    redis = get_redis()
    key = USER_REPOS_KEY.format(user_id=authed.user.id, installation_id=installation_id)
    if not refresh:
        cached = await redis.get(key)
        if cached:
            return [GitHubRepo(**item) for item in json.loads(cached)]
    repos = await call_with_refresh(
        authed, lambda token: list_installation_repositories(token, installation_id)
    )
    await redis.set(key, json.dumps([r.model_dump() for r in repos]), ex=USER_CACHE_TTL)
    return repos


@router.get(
    "/installations/{installation_id}/repositories",
    response_model=list[RepositoryOut],
)
async def installation_repositories(
    installation_id: int,
    refresh: int = Query(default=0),
    authed: AuthedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RepositoryOut]:
    """The user's live repos for an installation joined with each repo's stored
    indexing status (NOT_STARTED when not yet indexed). Access-checked first."""
    await verify_installation_access(authed, installation_id)
    live = await _live_repositories(authed, installation_id, refresh=bool(refresh))

    result = await db.execute(
        select(Repository.full_name, Repository.indexing_status)
        .join(Installation, Repository.installation_id == Installation.id)
        .where(Installation.github_installation_id == installation_id)
    )
    status_by_name = {full_name: status for full_name, status in result.all()}

    return [
        RepositoryOut(
            full_name=repo.full_name,
            indexing_status=status_by_name.get(repo.full_name, IndexingStatus.NOT_STARTED),
        )
        for repo in live
    ]


async def _authorize_repo(authed: AuthedUser, db: AsyncSession, full_name: str) -> int:
    """Resolve a stored repo to its GitHub installation id and verify the user can
    access that installation; 404 if the app was never installed on the repo."""
    result = await db.execute(
        select(Installation.github_installation_id)
        .join(Repository, Repository.installation_id == Installation.id)
        .where(Repository.full_name == full_name)
    )
    installation_id = result.scalar_one_or_none()
    if installation_id is None:
        raise HTTPException(status_code=404, detail=f"repo not installed: {full_name}")
    await verify_installation_access(authed, installation_id)
    return installation_id


@router.post("/repos/{owner}/{repo}/index", status_code=202)
async def index_repository(
    owner: str,
    repo: str,
    authed: AuthedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Enqueue the existing `index_repo` task for an access-checked repo (no heavy
    work in the request path)."""
    full_name = f"{owner}/{repo}"
    installation_id = await _authorize_repo(authed, db, full_name)
    index_repo.delay(full_name, installation_id)
    return {"status": "queued", "full_name": full_name}


@router.get("/repos/{owner}/{repo}/index-status", response_model=IndexStatusResponse)
async def index_status(
    owner: str,
    repo: str,
    authed: AuthedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IndexStatusResponse:
    """Current indexing status and indexed-chunk count for an access-checked repo."""
    from ai.vectorstore import count_chunks, get_vectorstore

    full_name = f"{owner}/{repo}"
    await _authorize_repo(authed, db, full_name)

    result = await db.execute(
        select(Repository.indexing_status).where(Repository.full_name == full_name)
    )
    status = result.scalar_one()
    chunk_count = await count_chunks(get_vectorstore(), full_name)
    return IndexStatusResponse(
        full_name=full_name, indexing_status=status, chunk_count=chunk_count
    )
