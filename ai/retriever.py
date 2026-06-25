from langchain_core.vectorstores import VectorStoreRetriever

from ai.vectorstore import get_vectorstore

from ai.constants import DEFAULT_K

def get_retriever(repo: str, k: int = DEFAULT_K) -> VectorStoreRetriever:
    """Return a retriever for the given repo, with a default number of results to return."""
    return get_vectorstore().as_retriever(
        search_kwargs={"k": k, "filter": {"repo": repo}},
    )
