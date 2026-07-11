"""
AEOS RAG Package — Public API

Import from here, not from sub-modules, unless you need a specific type.

Quick-start:
    from app.rag import KnowledgePipeline, get_rag_engine

    # High-level pipeline (new code)
    pipeline = KnowledgePipeline()
    await pipeline.initialize()
    pipeline.ingest_file("/docs/spec.pdf")
    context = pipeline.retrieve_context("What is AEOS?")
    text = context.as_context_string()

    # Legacy engine (existing routes / agents)
    engine = get_rag_engine()
    await engine.initialize()
    engine.ingest_text("...", source="my_doc")
    results = engine.query("what is AEOS?")
"""

# ── Schemas (data contracts) ───────────────────────────────────────────────────
from app.rag.schemas import (
    Document,
    Chunk,
    EmbeddingResult,
    SearchResult,
    RetrievalResult,
    ContextPackage,
    IngestStats,
)

# ── Exceptions ─────────────────────────────────────────────────────────────────
from app.rag.exceptions import (
    RAGError,
    LoaderError,
    ChunkingError,
    EmbeddingError,
    VectorStoreError,
    RetrievalError,
    PipelineError,
)

# ── Loaders ────────────────────────────────────────────────────────────────────
from app.rag.loader import (
    BaseLoader,
    TextLoader,
    MarkdownLoader,
    PDFLoader,
    HTMLLoader,
    JSONLoader,
    PythonLoader,
    DocumentLoaderRegistry,
    loader_registry,
)

# ── Chunkers ───────────────────────────────────────────────────────────────────
from app.rag.chunker import (
    BaseChunker,
    FixedChunker,
    RecursiveChunker,
    SemanticChunker,
    get_chunker,
)

# ── Embeddings ─────────────────────────────────────────────────────────────────
from app.rag.embeddings import (
    EmbeddingService,
    SentenceTransformerEmbeddings,
    OpenAIEmbeddings,
    get_embedding_service,
    # Backward-compat aliases
    EmbeddingModel,
    get_embedder,
)

# ── Vector store ───────────────────────────────────────────────────────────────
from app.rag.vector_store import (
    VectorStoreInterface,
    NumpyVectorStore,
    ChromaVectorStore,
    VectorStore,
)

# ── Retrieval pipeline ─────────────────────────────────────────────────────────
from app.rag.retriever import (
    RetrievalStage,
    EmbeddingStage,
    SearchStage,
    FilterStage,
    RankingStage,
    AssemblyStage,
    RetrievalPipeline,
    RetrievedDocument,   # legacy compat
    HybridRetriever,     # legacy alias
)

# ── KnowledgePipeline (primary entry point for agents) ────────────────────────
from app.rag.pipeline import KnowledgePipeline

# ── Legacy engine facade (used by routes + existing agent code) ───────────────
from app.rag.rag_engine import RAGEngine, get_rag_engine

# ── Backward-compat: ingestor exports ─────────────────────────────────────────
# Code that imported DocumentIngestor from app.rag can use DocumentLoaderRegistry
DocumentIngestor = DocumentLoaderRegistry   # structural alias

__all__ = [
    # Schemas
    "Document", "Chunk", "EmbeddingResult", "SearchResult",
    "RetrievalResult", "ContextPackage", "IngestStats",
    # Exceptions
    "RAGError", "LoaderError", "ChunkingError", "EmbeddingError",
    "VectorStoreError", "RetrievalError", "PipelineError",
    # Loaders
    "BaseLoader", "TextLoader", "MarkdownLoader", "PDFLoader", "HTMLLoader",
    "JSONLoader", "PythonLoader", "DocumentLoaderRegistry", "loader_registry",
    "DocumentIngestor",
    # Chunkers
    "BaseChunker", "FixedChunker", "RecursiveChunker", "SemanticChunker", "get_chunker",
    # Embeddings
    "EmbeddingService", "SentenceTransformerEmbeddings", "OpenAIEmbeddings",
    "get_embedding_service", "EmbeddingModel", "get_embedder",
    # Vector store
    "VectorStoreInterface", "NumpyVectorStore", "ChromaVectorStore", "VectorStore",
    # Retrieval
    "RetrievalStage", "EmbeddingStage", "SearchStage", "FilterStage",
    "RankingStage", "AssemblyStage", "RetrievalPipeline",
    "RetrievedDocument", "HybridRetriever",
    # Pipeline
    "KnowledgePipeline",
    # Legacy engine
    "RAGEngine", "get_rag_engine",
]
