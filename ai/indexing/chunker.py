import hashlib
from dataclasses import dataclass

from tree_sitter import Node

from ai.indexing.languages import get_parser, language_for

from ai.constants import MAX_CHUNK_CHARS

DEFINITION_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset(
        {"function_definition", "class_definition", "decorated_definition"}
    ),
    "javascript": frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
        }
    ),
    "typescript": frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "enum_declaration",
            "type_alias_declaration",
        }
    ),
    "go": frozenset(
        {"function_declaration", "method_declaration", "type_declaration"}
    ),
    "java": frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
            "method_declaration",
        }
    ),
    "rust": frozenset(
        {
            "function_item",
            "struct_item",
            "impl_item",
            "enum_item",
            "trait_item",
            "mod_item",
            "union_item",
        }
    ),
    "ruby": frozenset({"method", "singleton_method", "class", "module"}),
    "php": frozenset(
        {
            "function_definition",
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
            "enum_declaration",
            "method_declaration",
        }
    ),
    "c": frozenset(
        {"function_definition", "struct_specifier", "type_definition", "enum_specifier"}
    ),
    "cpp": frozenset(
        {
            "function_definition",
            "class_specifier",
            "struct_specifier",
            "namespace_definition",
            "enum_specifier",
            "template_declaration",
        }
    ),
    "csharp": frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "struct_declaration",
            "enum_declaration",
            "record_declaration",
            "namespace_declaration",
            "method_declaration",
        }
    ),
}
DEFINITION_TYPES["tsx"] = DEFINITION_TYPES["typescript"]

# Languages where top-level definitions are commonly wrapped in `export ...`.
_EXPORT_WRAPPER_LANGUAGES = frozenset({"javascript", "typescript", "tsx"})
_EXPORT_WRAPPERS = frozenset({"export_statement"})

_NAME_LEAF_TYPES = frozenset(
    {
        "identifier",
        "field_identifier",
        "type_identifier",
        "qualified_identifier",
        "scoped_identifier",
        "constant",
        "destructor_name",
        "operator_name",
    }
)


@dataclass(frozen=True, slots=True)
class CodeChunk:
    path: str
    language: str
    chunk_type: str
    name: str | None
    start_line: int  # 1-based, inclusive
    end_line: int
    text: str


def embedding_text(chunk: CodeChunk) -> str:
    """Generate the text to be embedded for a code chunk.
    
    The file path, chunk type and name are also embedded along with the code,
    this improves the quality of embeddings for search and retrieval, as it 
    provides additional context about the code snippet.
    """
    header = f"File: {chunk.path}"
    if chunk.name:
        header += f"\n{chunk.chunk_type}: {chunk.name}"
    return f"{header}\n```{chunk.language}\n{chunk.text}\n```"


def chunk_id(repo: str, path: str, start_line: int, end_line: int) -> str:
    """Generate a deterministic ID for a code chunk based on the 
    repository, file path, and line span.
    
    This id will be same everytime for the same chunk, so when we index a file
    again we can upsert the same chunk instead of inserting.
    """
    return hashlib.sha1(
        f"{repo}:{path}:{start_line}-{end_line}".encode()
    ).hexdigest()


def chunk_file(path: str, content: str) -> list[CodeChunk]:
    """Chunk a file into smaller pieces based on top-level definitions and line limits.
    
    find language -> get parser -> parse -> find top-leve definitions ->
    if a chunk exceeds MAX_CHUNK_CHARS, split by lines -> call _finalize -> 
    return list of CodeChunk
    """
    language = language_for(path)
    if language is None:
        return []
    parser = get_parser(language)
    if parser is None:
        return _finalize(path, language, "file", None, 1, content)

    data = content.encode("utf-8")
    root = parser.parse(data).root_node
    def_types = DEFINITION_TYPES.get(language, frozenset())

    """Till we find a top-level definition(function or class), we keep adding
    nodes to pending. Once we find a top-level definition, we flush the pending
    i.e. treat whatever is present in pending as a single chunk."""
    chunks: list[CodeChunk] = []
    pending: list[Node] = []

    def flush() -> None:
        if not pending:
            return
        text = data[pending[0].start_byte : pending[-1].end_byte].decode(
            "utf-8", "replace"
        )
        start = pending[0].start_point[0] + 1
        if text.strip():
            chunks.extend(_finalize(path, language, "module", None, start, text))
        pending.clear()

    for node in root.children:
        inner = _definition_node(node, language, def_types)
        if inner is not None:
            flush()
            chunks.extend(_definition_chunks(path, language, data, node, inner))
        else:
            pending.append(node)
    flush()

    if not chunks:
        return _finalize(path, language, "file", None, 1, content)
    return chunks


def _definition_node(node: Node, language: str, def_types: frozenset[str]) -> Node | None:
    """ Returns the node naming the definition (unwrapping decorator/export
    wrappers), or None when the top-level node is not a definition. The chunk
    span stays the outer node so decorators / the `export` keyword are kept in 
    the embedding."""
    if node.type in def_types:
        if node.type == "decorated_definition":
            return node.child_by_field_name("definition") or node
        return node
    if language in _EXPORT_WRAPPER_LANGUAGES and node.type in _EXPORT_WRAPPERS:
        declaration = node.child_by_field_name("declaration")
        if declaration is not None and declaration.type in def_types:
            return declaration
        for child in node.named_children:
            if child.type in def_types:
                return child
    return None


def _definition_chunks(
    path: str, language: str, data: bytes, node: Node, inner: Node
) -> list[CodeChunk]:
    """Return the chunks for a top-level definition node (function,class etc)."""
    text = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
    return _finalize(
        path,
        language,
        inner.type,
        _node_name(inner),
        node.start_point[0] + 1,
        text,
    )


def _node_name(node: Node) -> str | None:
    """Extract node name i.e function name, class name etc."""
    named = node.child_by_field_name("name")
    if named is not None:
        return named.text.decode("utf-8", "replace")
    declarator = node.child_by_field_name("declarator")
    depth = 0
    while declarator is not None and depth < 6:
        if declarator.type in _NAME_LEAF_TYPES:
            return declarator.text.decode("utf-8", "replace")
        named = declarator.child_by_field_name("name")
        if named is not None:
            return named.text.decode("utf-8", "replace")
        declarator = declarator.child_by_field_name("declarator")
        depth += 1
    return None


def _finalize(
    path: str,
    language: str,
    chunk_type: str,
    name: str | None,
    start_line: int,
    text: str,
) -> list[CodeChunk]:
    """Convert raw text into CodeChunk objects, splitting by lines 
    if the text exceeds MAX_CHUNK_CHARS."""
    chunks: list[CodeChunk] = []
    for sub_start, sub_text in _split_by_lines(start_line, text):
        if not sub_text.strip():
            continue
        n_lines = max(1, len(sub_text.splitlines()))
        chunks.append(
            CodeChunk(
                path=path,
                language=language,
                chunk_type=chunk_type,
                name=name,
                start_line=sub_start,
                end_line=sub_start + n_lines - 1,
                text=sub_text,
            )
        )
    return chunks


def _split_by_lines(start_line: int, text: str) -> list[tuple[int, str]]:
    """Split text into chunks by lines, ensuring each chunk 
    does not exceed MAX_CHUNK_CHARS."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [(start_line, text)]
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_chars = 0
    buf_start = start_line
    line_no = start_line
    for line in text.splitlines(keepends=True):
        if buf and buf_chars + len(line) > MAX_CHUNK_CHARS:
            out.append((buf_start, "".join(buf)))
            buf = []
            buf_chars = 0
            buf_start = line_no
        buf.append(line)
        buf_chars += len(line)
        line_no += 1
    if buf:
        out.append((buf_start, "".join(buf)))
    return out
