import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_postgres import PGVector

from ai.constants import DEFAULT_K
from ai.retriever import format_doc
from ai.vectorstore import get_vectorstore, search_symbol
from app.github.auth import get_installation_token
from app.github.constants import GITHUB_API
from app.github.files import get_default_branch, get_file, list_dir, list_tree


def _context(config: RunnableConfig) -> tuple[str, int, str | None]:
    """Pull `(repo, installation_id, ref)` the graph injected via
    `config["configurable"]`; tools never receive these from the model, so a
    retrieval can never be tricked across repos (invariant #6)."""
    cfg = config.get("configurable", {})
    return cfg["repo"], cfg["installation_id"], cfg.get("ref")


def _store(config: RunnableConfig) -> PGVector:
    """Return the vector store to search with. Celery graph runs inject a per-run
    store via `config["configurable"]["store"]` (each runs its own asyncio.run
    loop — the cached singleton would bind to a closed loop, invariant #3). Chat
    runs on FastAPI's single long-lived loop and injects nothing, so it falls back
    to the cached `get_vectorstore()`."""
    store = config.get("configurable", {}).get("store")
    return store if store is not None else get_vectorstore()


@tool
async def retrieve_code(query: str, config: RunnableConfig) -> str:
    """Semantically search the indexed repository and return the most relevant
    code chunks, each with its file path, symbol, and line range."""
    repo, _, _ = _context(config)
    docs = await _store(config).asimilarity_search(
        query, k=DEFAULT_K, filter={"repo": repo}
    )
    if not docs:
        return "No relevant code found."
    return "\n\n---\n\n".join(format_doc(d) for d in docs)


@tool
async def read_file(path: str, config: RunnableConfig) -> str:
    """Read the full contents of a file in the repository at the configured ref
    (the default branch when no ref is set)."""
    repo, installation_id, ref = _context(config)
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        ref = ref or await get_default_branch(client, repo, token)
        file = await get_file(client, repo, path, ref, token)
    if file is None:
        return f"File not found or not readable: {path}"
    return file.content


@tool
async def grep_symbol(name: str, config: RunnableConfig) -> str:
    """Find where a symbol (function, class, type, ...) is defined in the indexed
    repository, returning matching paths and line ranges."""
    repo, _, _ = _context(config)
    matches = await search_symbol(_store(config), repo, name)
    if not matches:
        return f"No symbol matching '{name}' found in the index."
    return "\n".join(
        f"{path}:{start}-{end} ({chunk_type} {symbol})"
        for path, start, end, chunk_type, symbol in matches
    )


@tool
async def list_directory(path: str, config: RunnableConfig) -> str:
    """List the files and subdirectories directly under a directory path in the
    repository."""
    repo, installation_id, ref = _context(config)
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        ref = ref or await get_default_branch(client, repo, token)
        entries = await list_dir(client, repo, path, ref, token)
    if not entries:
        return f"No entries found under: {path or '/'}"
    return "\n".join(f"{kind}\t{name}" for name, kind in entries)


@tool
async def get_file_tree(config: RunnableConfig) -> str:
    """Return the repository's full file tree (all file paths) at the configured
    ref, for orienting before reading specific files."""
    repo, installation_id, ref = _context(config)
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        branch = ref or await get_default_branch(client, repo, token)
        paths = await list_tree(client, repo, branch, token)
    return "\n".join(paths) if paths else "Empty repository."


CODEBASE_TOOLS = [retrieve_code, read_file, grep_symbol, list_directory, get_file_tree]
