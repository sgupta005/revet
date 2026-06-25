import asyncio
import logging
from collections.abc import Sequence

import httpx
from langchain_core.documents import Document
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select

from ai.indexing.chunker import CodeChunk, chunk_file, chunk_id, embedding_text
from ai.indexing.languages import is_indexable
from ai.llm import make_embeddings
from ai.vectorstore import delete_paths, make_vectorstore
from ai.constants import FETCH_CONCURRENCY, UPSERT_BATCH

from app.db.models import IndexingStatus, Repository
from app.db.session import build_engine
from app.github.constants import GITHUB_API
from app.github.auth import get_installation_token
from app.github.files import (
    RepoFile,
    get_blob,
    get_default_branch,
    get_file,
    list_indexable_blobs,
)

logger = logging.getLogger(__name__)

async def run_index(
    repo: str,
    installation_id: int,
    changed_paths: Sequence[str] | None = None,
) -> None:
    """Run the indexing pipeline for a given repository.
    
    Download repository → Chunk code → Generate embeddings → 
    Store in vector database → Update indexing status
    """
    engine = build_engine()
    embeddings = make_embeddings()
    store = make_vectorstore(embeddings, async_mode=True)
    try:
        await store.acreate_collection()
        await _set_status(engine, repo, IndexingStatus.INDEXING)
        token = await get_installation_token(installation_id)
        async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
            branch = await get_default_branch(client, repo, token)
            if changed_paths is None:
                files = await _fetch_full(client, repo, branch, token)
            else:
                await delete_paths(store, repo, changed_paths)
                files = await _fetch_changed(client, repo, branch, token, changed_paths)

        chunks = [c for f in files for c in chunk_file(f.path, f.content)]
        await _upsert(store, repo, chunks)
        await _set_status(engine, repo, IndexingStatus.COMPLETED)
        logger.info(
            "index_repo done repo=%s files=%d chunks=%d incremental=%s",
            repo,
            len(files),
            len(chunks),
            changed_paths is not None,
        )
    except Exception:
        await _set_status(engine, repo, IndexingStatus.FAILED)
        raise
    finally:
        await store._async_engine.dispose()
        await engine.dispose()


async def _fetch_full(
    client: httpx.AsyncClient, repo: str, branch: str, token: str
) -> list[RepoFile]:
    """Fetch all indexable files from the repository."""
    blobs = await list_indexable_blobs(client, repo, branch, token)
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(path: str, sha: str) -> RepoFile | None:
        async with semaphore:
            return await get_blob(client, repo, path, sha, token)

    results = await asyncio.gather(*(fetch(p, s) for p, s in blobs))
    return [f for f in results if f is not None]


async def _fetch_changed(
    client: httpx.AsyncClient,
    repo: str,
    branch: str,
    token: str,
    changed_paths: Sequence[str],
) -> list[RepoFile]:
    """Fetch only the changed indexable files from the repository."""
    paths = [p for p in changed_paths if is_indexable(p)]
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(path: str) -> RepoFile | None:
        async with semaphore:
            return await get_file(client, repo, path, branch, token)

    results = await asyncio.gather(*(fetch(p) for p in paths))
    return [f for f in results if f is not None]


async def _upsert(store, repo: str, chunks: list[CodeChunk]) -> None:
    """Upsert the given code chunks into the vector store in batches."""
    for start in range(0, len(chunks), UPSERT_BATCH):
        batch = chunks[start : start + UPSERT_BATCH]
        documents = [
            Document(
                page_content=embedding_text(chunk),
                metadata={
                    "repo": repo,
                    "path": chunk.path,
                    "name": chunk.name,
                    "chunk_type": chunk.chunk_type,
                    "language": chunk.language,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                },
            )
            for chunk in batch
        ]
        ids = [
            chunk_id(repo, chunk.path, chunk.start_line, chunk.end_line)
            for chunk in batch
        ]
        await store.aadd_documents(documents, ids=ids)


async def _set_status(
    engine: AsyncEngine, repo: str, status: IndexingStatus
) -> None:
    """Update the indexing status of the repository in the database."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            select(Repository).where(Repository.full_name == repo)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.indexing_status = status
        session.add(row)
        await session.commit()
