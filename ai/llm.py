from functools import lru_cache

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
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


def make_chat_model(model: str | None = None) -> BaseChatModel:
    """Build a chat model via init_chat_model; `model` is a provider-prefixed
    id (e.g. `openai:gpt-4o`) defaulting to settings.llm_model, so the provider
    is swappable by changing one config string."""
    return init_chat_model(model or settings.llm_model, api_key=settings.openai_api_key)


@lru_cache(maxsize=None)
def get_chat_model(model: str | None = None) -> BaseChatModel:
    """Return a cached chat model per model id; pass a cheaper id (e.g.
    `openai:gpt-4o-mini`) for graders/reviewers to control cost."""
    return make_chat_model(model)
