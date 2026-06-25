from collections.abc import Sequence
from functools import lru_cache

from langchain_postgres import PGVector
from sqlalchemy import bindparam, text

from app.config import settings

from ai.constants import COLLECTION_NAME, EMBEDDING_DIM, EMBEDDING_TABLE

def _connection_string() -> str:
    return settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)


def make_vectorstore(embeddings, *, async_mode: bool) -> PGVector:
    return PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION_NAME,
        connection=_connection_string(),
        embedding_length=EMBEDDING_DIM,
        use_jsonb=True,
        async_mode=async_mode,
    )


@lru_cache(maxsize=1)
def get_vectorstore() -> PGVector:
    """Return a cached PGVector instance."""
    from ai.llm import get_embeddings

    return make_vectorstore(get_embeddings(), async_mode=True)


async def delete_paths(store: PGVector, repo: str, paths: Sequence[str]) -> None:
    """Delete embeddings for given repo and paths. Path here refers to the file path in the repository."""
    if not paths:
        return
    stmt = text(
        f"DELETE FROM {EMBEDDING_TABLE} "
        "WHERE cmetadata->>'repo' = :repo AND cmetadata->>'path' IN :paths"
    ).bindparams(bindparam("paths", expanding=True))
    async with store._make_async_session() as session:
        await session.execute(stmt, {"repo": repo, "paths": list(paths)})
        await session.commit()
