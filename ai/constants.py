DEFAULT_K = 8
COLLECTION_NAME = "code_chunks"
EMBEDDING_DIM = 1536  # text-embedding-3-small; changing the model changes this column

# langchain_postgres stores documents in this table with a jsonb `cmetadata` column.
EMBEDDING_TABLE = "langchain_pg_embedding"

# Keep chunks comfortably under the embedding model's token limit; oversized
# definitions / files are split into line windows below this character budget.
MAX_CHUNK_CHARS = 6000

MAX_FILE_BYTES = 100 * 1024

FETCH_CONCURRENCY = 10
UPSERT_BATCH = 100

# Chat (corrective + agentic RAG)
MAX_REWRITES = 2  # corrective-RAG retrieve→grade→rewrite loops are bounded (PRD §F3)
MAX_TOOL_ROUNDS = 3  # bound the agentic generate tool loop (invariant #10)
# Cheaper model for grading/rewriting; generation uses the default settings.llm_model.
GRADER_MODEL = "openai:gpt-4o-mini"

# PR review (multi-agent fan-out)
# Cheap per-reviewer model; aggregation is a deterministic dedupe+rank (no LLM), so the
# capable default model is not used here — revisit with evals (progress-tracker open question).
REVIEWER_MODEL = "openai:gpt-4o-mini"
# Perspectives fanned out in parallel; "custom-rules" only runs when the installation has rules.
REVIEW_PERSPECTIVES = ("correctness", "security", "quality", "custom-rules")
MAX_DIFF_CHARS = 20000  # cap diff sent to reviewers to control token cost
REVIEW_CONTEXT_K = 6  # related-code chunks retrieved for reviewer context
REVIEW_QUERY_CHARS = 1500  # cap the retrieval query built from the diff
MIN_FINDING_CONFIDENCE = 0.3  # drop low-confidence findings during aggregation
MAX_FINDINGS = 20  # cap findings posted in one review

# Custom rules (PRD §F7): per-repo, injected into PR review / issue analysis / auto-PR.
# A generous fixed cap bounds prompt size in every rule-aware feature.
MAX_RULES = 50
