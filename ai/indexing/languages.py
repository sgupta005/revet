from collections.abc import Callable
from functools import lru_cache
from pathlib import PurePosixPath

from tree_sitter import Language, Parser

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
}

FILENAME_LANGUAGE: dict[str, str] = {
    "dockerfile": "dockerfile",
}

# Directories never worth indexing (vendored, build output, caches, VCS).
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "out",
        "target",
        "bin",
        "obj",
        "__pycache__",
        ".venv",
        "venv",
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        "coverage",
    }
)

LOCKFILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "uv.lock",
    }
)

# Grammars are only loaded for languages where function/class-aware chunking is
# meaningful; data/markup languages fall back to whole-file chunks.
_PARSER_LOADERS: dict[str, Callable[[], object]] = {
    "python": lambda: __import__("tree_sitter_python").language(),
    "javascript": lambda: __import__("tree_sitter_javascript").language(),
    "typescript": lambda: __import__("tree_sitter_typescript").language_typescript(),
    "tsx": lambda: __import__("tree_sitter_typescript").language_tsx(),
    "go": lambda: __import__("tree_sitter_go").language(),
    "java": lambda: __import__("tree_sitter_java").language(),
    "rust": lambda: __import__("tree_sitter_rust").language(),
    "ruby": lambda: __import__("tree_sitter_ruby").language(),
    "php": lambda: __import__("tree_sitter_php").language_php(),
    "c": lambda: __import__("tree_sitter_c").language(),
    "cpp": lambda: __import__("tree_sitter_cpp").language(),
    "csharp": lambda: __import__("tree_sitter_c_sharp").language(),
}


def language_for(path: str) -> str | None:
    """Return the language name for a given file path, 
    based on its extension or filename."""
    name = PurePosixPath(path).name
    lang = FILENAME_LANGUAGE.get(name.lower())
    if lang:
        return lang
    return EXTENSION_LANGUAGE.get(PurePosixPath(path).suffix.lower())


def is_indexable(path: str) -> bool:
    """Return True if the file at the given path is indexable."""
    parts = PurePosixPath(path).parts
    if any(part in SKIP_DIRS for part in parts):
        return False
    name = PurePosixPath(path).name.lower()
    if name in LOCKFILES:
        return False
    if ".min." in name:  # minified bundles: unreadable, no semantic value
        return False
    return language_for(path) is not None


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser | None:
    """Return cached tree sitter parser for a given language."""
    loader = _PARSER_LOADERS.get(language)
    if loader is None:
        return None
    return Parser(Language(loader()))
