from functools import lru_cache

from langchain_openai import OpenAIEmbeddings

from app.config import settings


def make_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
    )


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """Get embeddings with caching to avoid re-initialization."""
    return make_embeddings()
