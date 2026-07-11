"""
AEOS Core Configuration
Loads from environment variables with sane defaults.
All settings are immutable after startup.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class AEOSSettings(BaseSettings):
    # ── Identity ──────────────────────────────────────────────────────────────
    app_name: str = Field(default="AEOS", description="Application name")
    app_version: str = Field(default="0.1.0", description="Semantic version")
    environment: str = Field(default="development", description="Runtime environment: development | staging | production")

    # ── API ───────────────────────────────────────────────────────────────────
    api_prefix: str = Field(default="/api/v1", description="Global API route prefix")
    api_host: str = Field(default="0.0.0.0", description="Bind host")
    api_port: int = Field(default=8000, description="Bind port")

    # ── Debug ─────────────────────────────────────────────────────────────────
    debug: bool = Field(default=False, description="Enable debug mode (verbose logging, /debug routes)")

    # ── Orchestrator ──────────────────────────────────────────────────────────
    default_agent: str = Field(default="simple_agent", description="Agent ID used when no routing match found")
    agent_timeout_seconds: int = Field(default=60, description="Max seconds an agent run() may take")
    max_retries: int = Field(default=3, description="Max retry attempts on transient agent failure")

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Log level: DEBUG | INFO | WARNING | ERROR")
    log_json: bool = Field(default=True, description="Emit logs as JSON (False = human-readable for local dev)")

    # ── RAG Engine ────────────────────────────────────────────────────────────
    chroma_host: str = Field(default="", description="ChromaDB HTTP host (empty = in-memory EphemeralClient)")
    chroma_port: int = Field(default=8001, description="ChromaDB HTTP port")
    chroma_collection: str = Field(default="aeos_default", description="Default ChromaDB collection name")
    embedding_model: str = Field(default="all-MiniLM-L6-v2", description="sentence-transformers model name")
    rag_top_k: int = Field(default=5, description="Default number of RAG results to return")
    rag_chunk_size: int = Field(default=512, description="Target token count per chunk")
    rag_chunk_overlap: int = Field(default=64, description="Overlap tokens between consecutive chunks")
    rag_persist_dir: str = Field(default="./data/rag", description="Directory for on-disk NumpyVectorStore persistence (empty = in-memory only)")
    rag_max_ingest_chars: int = Field(default=1_000_000, description="Max characters accepted by /rag/ingest")
    rag_max_query_chars: int = Field(default=4000, description="Max characters accepted by a RAG query/answer")
    rag_max_top_k: int = Field(default=20, description="Upper bound on top_k accepted from clients")

    # ── Security ──────────────────────────────────────────────────────────────
    api_key: str = Field(default="", description="If set, RAG routes require a matching X-API-Key header (empty = open, for local demo)")
    cors_allow_origins: list[str] = Field(default_factory=list, description="Explicit allowed CORS origins; empty = same-origin only (never wildcard+credentials)")
    rag_rate_limit_per_minute: int = Field(default=60, description="Per-client request budget on RAG routes (token bucket)")

    # ── GitHub Analyzer ───────────────────────────────────────────────────────
    github_token: str = Field(default="", description="GitHub personal access token (optional, raises rate limit)")
    github_api_url: str = Field(default="https://api.github.com", description="GitHub API base URL")

    # ── ML Pipeline ───────────────────────────────────────────────────────────
    ml_model_registry_path: str = Field(default="./data/model_registry", description="Filesystem path for model registry")
    ml_dataset_path: str = Field(default="./data/datasets", description="Root directory for datasets")

    # ── Shared Memory ─────────────────────────────────────────────────────────
    memory_max_long_term: int = Field(default=1000, description="Max long-term memory entries before LRU eviction")
    memory_short_term_ttl_seconds: int = Field(default=3600, description="Short-term memory TTL (informational; cleared on task end)")

    # ── Message Bus ───────────────────────────────────────────────────────────
    message_bus_max_queue: int = Field(default=500, description="Max pending messages per topic queue")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_settings() -> AEOSSettings:
    """
    Cached singleton — called once at startup, reused everywhere.
    Use dependency injection in FastAPI routes:
        settings: AEOSSettings = Depends(get_settings)
    """
    return AEOSSettings()


# Module-level convenience reference
settings = get_settings()
