"""
AEOS Unit Tests — RAG Engine
Tests chunker, embedder, vector store, retriever, and full end-to-end pipeline.
"""

import pytest


# ── Chunker ────────────────────────────────────────────────────────────────────

def test_chunker_splits_long_text():
    from app.rag.chunker import SemanticChunker
    chunker = SemanticChunker(chunk_size=20, overlap=5)
    # Build a multi-sentence text that clearly exceeds 20 tokens
    sentences = ["This is sentence number {}.".format(i) for i in range(30)]
    long_text = " ".join(sentences)
    chunks = chunker.chunk(long_text)
    assert len(chunks) > 1


def test_chunker_empty_text_returns_empty():
    from app.rag.chunker import SemanticChunker
    chunker = SemanticChunker()
    assert chunker.chunk("") == []
    assert chunker.chunk("   ") == []


def test_chunker_short_text_is_single_chunk():
    from app.rag.chunker import SemanticChunker
    chunker = SemanticChunker(chunk_size=512)
    chunks = chunker.chunk("Short sentence.")
    assert len(chunks) == 1
    assert chunks[0].text == "Short sentence."


def test_chunker_chunk_has_metadata():
    from app.rag.chunker import SemanticChunker
    chunker = SemanticChunker()
    chunks = chunker.chunk("Hello world. This is a test.", metadata={"source": "test"})
    assert all("source" in c.metadata for c in chunks)
    assert all("chunk_index" in c.metadata for c in chunks)


def test_chunker_indices_sequential():
    from app.rag.chunker import SemanticChunker
    chunker = SemanticChunker(chunk_size=30, overlap=5)
    text = "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five."
    chunks = chunker.chunk(text)
    for i, chunk in enumerate(chunks):
        assert chunk.index == i


# ── Ingestor ───────────────────────────────────────────────────────────────────

def test_ingestor_inline_text():
    from app.rag.ingestor import DocumentIngestor
    ingestor = DocumentIngestor()
    doc = ingestor.ingest_text("This is test content about AI systems.", source="test")
    assert doc.content == "This is test content about AI systems."
    assert doc.source == "test"
    assert doc.id != ""
    assert len(doc.chunks) >= 1


def test_ingestor_document_has_sha256_id():
    from app.rag.ingestor import DocumentIngestor
    ingestor = DocumentIngestor()
    doc1 = ingestor.ingest_text("same content", source="a")
    doc2 = ingestor.ingest_text("same content", source="b")
    assert doc1.id == doc2.id  # same content = same hash


def test_ingestor_different_content_different_id():
    from app.rag.ingestor import DocumentIngestor
    ingestor = DocumentIngestor()
    doc1 = ingestor.ingest_text("content A", source="a")
    doc2 = ingestor.ingest_text("content B", source="b")
    assert doc1.id != doc2.id


def test_ingestor_detect_python_type(tmp_path):
    from app.rag.ingestor import DocumentIngestor
    f = tmp_path / "test.py"
    f.write_text("def hello(): pass")
    ingestor = DocumentIngestor()
    doc = ingestor.ingest_file(f)
    assert doc is not None
    assert doc.doc_type == "python"


def test_ingestor_skips_missing_file():
    from app.rag.ingestor import DocumentIngestor
    ingestor = DocumentIngestor()
    doc = ingestor.ingest_file("/nonexistent/path/file.txt")
    assert doc is None


# ── Embedder ───────────────────────────────────────────────────────────────────

def test_embedder_returns_list_of_floats():
    # Use cached singleton to avoid reloading model (Windows page file limit)
    from app.rag.embedder import get_embedder
    model = get_embedder()
    vectors = model.embed(["hello world"])
    assert isinstance(vectors, list)
    assert len(vectors) == 1
    assert isinstance(vectors[0], list)
    assert all(isinstance(v, float) for v in vectors[0])


def test_embedder_correct_dimension():
    from app.rag.embedder import get_embedder
    model = get_embedder()
    model.embed(["test"])
    assert model.dimension == 384  # all-MiniLM-L6-v2


def test_embedder_embed_one():
    from app.rag.embedder import get_embedder
    model = get_embedder()
    vec = model.embed_one("single string")
    assert isinstance(vec, list)
    assert len(vec) == 384


def test_embedder_empty_input_returns_empty():
    from app.rag.embedder import get_embedder
    model = get_embedder()
    assert model.embed([]) == []


def test_embedder_batch_returns_matching_count():
    from app.rag.embedder import get_embedder
    model = get_embedder()
    texts = ["first", "second", "third"]
    vectors = model.embed(texts)
    assert len(vectors) == 3


# ── VectorStore ────────────────────────────────────────────────────────────────

def test_vector_store_add_and_count():
    from app.rag.vector_store import VectorStore
    from app.rag.chunker import Chunk
    from app.rag.embedder import EmbeddingModel
    store = VectorStore(namespace="test_vs_count")
    embedder = EmbeddingModel()
    chunk = Chunk(text="test chunk", index=0, token_count=2, metadata={"source": "test"})
    embs = embedder.embed([chunk.text])
    store.add_chunks([chunk], doc_id="doc1", embeddings=embs)
    assert store.count() >= 1
    store.reset()


def test_vector_store_query_returns_results():
    from app.rag.vector_store import VectorStore
    from app.rag.chunker import Chunk
    from app.rag.embedder import EmbeddingModel
    store = VectorStore(namespace="test_vs_query")
    embedder = EmbeddingModel()
    chunks = [
        Chunk(text="The cat sat on the mat", index=0, token_count=6, metadata={}),
        Chunk(text="Python is a programming language", index=1, token_count=5, metadata={}),
    ]
    embs = embedder.embed([c.text for c in chunks])
    store.add_chunks(chunks, doc_id="doc2", embeddings=embs)
    q_emb = embedder.embed_one("feline animal")
    results = store.query(q_emb, top_k=2)
    assert len(results) >= 1
    assert all("text" in r and "score" in r for r in results)
    store.reset()


def test_vector_store_scores_between_0_and_1():
    from app.rag.vector_store import VectorStore
    from app.rag.chunker import Chunk
    from app.rag.embedder import EmbeddingModel
    store = VectorStore(namespace="test_vs_scores")
    embedder = EmbeddingModel()
    chunk = Chunk(text="score test", index=0, token_count=2, metadata={})
    embs = embedder.embed([chunk.text])
    store.add_chunks([chunk], doc_id="d3", embeddings=embs)
    q_emb = embedder.embed_one("score test")
    results = store.query(q_emb, top_k=1)
    for r in results:
        assert 0.0 <= r["score"] <= 1.0
    store.reset()


# ── Retriever ──────────────────────────────────────────────────────────────────

def test_retriever_returns_ranked_results():
    from app.rag.vector_store import VectorStore
    from app.rag.retriever import HybridRetriever
    from app.rag.chunker import Chunk
    from app.rag.embedder import EmbeddingModel
    store = VectorStore(namespace="test_retriever")
    embedder = EmbeddingModel()
    chunks = [
        Chunk(text="machine learning algorithms optimize models", index=0, token_count=5, metadata={}),
        Chunk(text="cooking recipes for pasta dishes", index=1, token_count=5, metadata={}),
        Chunk(text="deep learning neural networks", index=2, token_count=4, metadata={}),
    ]
    embs = embedder.embed([c.text for c in chunks])
    store.add_chunks(chunks, doc_id="d_ret", embeddings=embs)
    retriever = HybridRetriever(store, top_k=2)
    results = retriever.retrieve("neural network machine learning", top_k=2)
    assert len(results) <= 2
    assert all(r.rank >= 1 for r in results)
    # Scores should be descending
    if len(results) > 1:
        assert results[0].score >= results[1].score
    store.reset()


def test_retriever_empty_store_returns_empty():
    from app.rag.vector_store import VectorStore
    from app.rag.retriever import HybridRetriever
    store = VectorStore(namespace="test_empty_retriever")
    retriever = HybridRetriever(store)
    results = retriever.retrieve("any query")
    assert results == []


# ── RAGEngine end-to-end ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rag_engine_ingest_and_query(rag_engine):
    await rag_engine.initialize()
    count = rag_engine.ingest_text(
        "AEOS is a production-grade AI Engineering Orchestration System.",
        source="unit_test",
    )
    assert count >= 1
    results = rag_engine.query("what is AEOS?")
    assert len(results) >= 1
    assert results[0].score > 0


@pytest.mark.asyncio
async def test_rag_engine_empty_returns_empty(rag_engine):
    await rag_engine.initialize()
    results = rag_engine.query("anything")
    assert results == []


@pytest.mark.asyncio
async def test_rag_engine_store_count(rag_engine):
    await rag_engine.initialize()
    assert rag_engine.store_count() == 0
    rag_engine.ingest_text("some content here for counting", source="count_test")
    assert rag_engine.store_count() >= 1


@pytest.mark.asyncio
async def test_rag_engine_multiple_docs(rag_engine):
    await rag_engine.initialize()
    rag_engine.ingest_text("FastAPI is a modern web framework for Python.", source="doc1")
    rag_engine.ingest_text("ChromaDB is a vector database for embeddings.", source="doc2")
    rag_engine.ingest_text("sentence-transformers provides text embeddings.", source="doc3")
    results = rag_engine.query("vector database embeddings", top_k=3)
    assert len(results) >= 1
    sources = [r.metadata.get("source", "") for r in results]
    assert any("doc" in s for s in sources)
