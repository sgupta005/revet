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
