"""
AEOS Unit Tests — RAG Answer Generation (the "G" in RAG)

Uses hand-built ContextPackages so these tests need no embedding model.
"""
import pytest

from app.rag.schemas import ContextPackage, RetrievalResult
from app.rag.generator import (
    ExtractiveGenerator,
    OpenAIGenerator,
    get_generator,
    NO_ANSWER,
)


def _pkg(results):
    return ContextPackage(
        query="q",
        results=results,
        total_retrieved=len(results),
        total_tokens=sum(len(r.text) // 4 for r in results),
        retrieval_latency_ms=1.0,
        embedding_latency_ms=0.5,
        search_latency_ms=0.3,
    )


def _result(text, score, source, idx=0, doc_id="d"):
    return RetrievalResult(
        text=text, score=score, rank=idx + 1, source=source,
        doc_id=doc_id, chunk_index=idx, metadata={"source": source},
    )


def test_extractive_builds_cited_answer():
    pkg = _pkg([
        _result("AEOS orchestrates AI agents.", 0.9, "a.md", 0),
        _result("It has a RAG knowledge layer.", 0.8, "b.md", 1),
    ])
    res = ExtractiveGenerator().generate("what is AEOS?", pkg)
    assert res.used_generator == "extractive"
    assert "[1]" in res.answer and "[2]" in res.answer
    assert len(res.citations) == 2
    assert res.citations[0].marker == 1
    assert res.sources == ["a.md", "b.md"]
    assert res.confidence == pytest.approx(0.9, abs=1e-6)


def test_extractive_empty_context_returns_no_answer():
    res = ExtractiveGenerator().generate("anything", _pkg([]))
    assert res.answer == NO_ANSWER
    assert res.citations == []
    assert res.confidence == 0.0


def test_extractive_respects_max_citations():
    results = [_result(f"fact {i}", 0.9 - i * 0.1, f"s{i}.md", i) for i in range(5)]
    res = ExtractiveGenerator(max_citations=2).generate("q", _pkg(results))
    assert len(res.citations) == 2


def test_extractive_answer_only_contains_retrieved_text():
    # Grounding guarantee: the answer cannot include anything not retrieved.
    pkg = _pkg([_result("The sky is blue.", 0.7, "sky.md", 0)])
    res = ExtractiveGenerator().generate("color of sky?", pkg)
    assert "sky is blue" in res.answer.lower()


def test_factory_selects_extractive_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert get_generator().name == "extractive"


def test_factory_selects_openai_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-used")
    gen = get_generator()
    assert gen.name == "openai"
    assert isinstance(gen, OpenAIGenerator)


def test_openai_generator_falls_back_on_failure(monkeypatch):
    # With a bogus key the client call fails; generator must degrade to an
    # extractive (still-grounded) answer rather than raising.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invalid")
    pkg = _pkg([_result("Fallback content here.", 0.6, "f.md", 0)])
    res = OpenAIGenerator().generate("q", pkg)
    assert res.answer  # non-empty
    assert "[1]" in res.answer  # extractive fallback produced citations
