from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tz_column() -> Column:
    return Column(DateTime(timezone=True), nullable=False)


def _tz_updated_column() -> Column:
    return Column(DateTime(timezone=True), nullable=False, onupdate=_utcnow)


class IndexingStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PRKind(str, Enum):
    REVIEW = "review"
    AUTO_PR = "auto-pr"


class User(SQLModel, table=True):
    __tablename__ = "user"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    github_id: int = Field(unique=True, index=True)
    login: str
    avatar_url: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())


class Installation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    github_installation_id: int = Field(unique=True, index=True)
    account_login: str
    account_type: str  # "User" or "Organization"
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class Repository(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    installation_id: int = Field(foreign_key="installation.id", index=True)
    full_name: str = Field(unique=True, index=True)  # "owner/repo"
    github_repo_id: int
    indexing_status: IndexingStatus = Field(default=IndexingStatus.NOT_STARTED)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())


class Rule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    installation_id: int = Field(foreign_key="installation.id", index=True)
    name: str
    body: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())


class PullRequest(SQLModel, table=True):
    __tablename__ = "pull_request"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    github_pr_number: int
    kind: PRKind
    state: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())


class Issue(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    github_issue_number: int
    state: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())


class ChatThread(SQLModel, table=True):
    """Ownership bridge linking a LangGraph thread_id to the user who created it.
    The LangGraph checkpointer holds the actual messages keyed by thread_id; this
    table exists only to enforce that thread_ids are never bare capabilities (invariant #14)."""

    __tablename__ = "chat_thread"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    repo: str = Field(index=True)
    title: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_updated_column())
