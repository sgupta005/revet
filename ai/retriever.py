from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStoreRetriever

from ai.constants import DEFAULT_K
from ai.vectorstore import get_vectorstore


def get_retriever(repo: str, k: int = DEFAULT_K) -> VectorStoreRetriever:
    """Return a retriever for the given repo, with a default number of results to return."""
    return get_vectorstore().as_retriever(
        search_kwargs={"k": k, "filter": {"repo": repo}},
    )


def format_doc(doc: Document) -> str:
    """Render a retrieved chunk with a `path:start-end` location header; the page
    content already carries the File/symbol context embedded at index time, so
    retrieval and grounding both show path, symbol, and line range."""
    m = doc.metadata
    return f"[{m.get('path')}:{m.get('start_line')}-{m.get('end_line')}]\n{doc.page_content}"
