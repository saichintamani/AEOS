"""
AEOS Unit Tests — NumpyVectorStore disk persistence

Uses fake vectors so these tests need no embedding model.
"""
from app.rag.vector_store import NumpyVectorStore


def _add_one(store, doc_id="d1", text="hello world"):
    store.add(
        ids=[f"{doc_id}__0"],
        embeddings=[[0.1, 0.2, 0.3]],
        documents=[text],
        metadatas=[{"doc_id": doc_id, "source": "x.md"}],
    )


def test_persists_and_reloads(tmp_path):
    d = tmp_path / "ns"
    s = NumpyVectorStore(persist_dir=d)
    _add_one(s)
    assert s.count() == 1
    assert (d / "vectors.npz").exists()
    assert (d / "meta.json").exists()

    # A fresh instance pointed at the same dir loads the saved data.
    s2 = NumpyVectorStore(persist_dir=d)
    assert s2.count() == 1
    res = s2.query([0.1, 0.2, 0.3], n_results=1)
    assert res["ids"][0] == ["d1__0"]
    assert res["documents"][0] == ["hello world"]


def test_reset_removes_persisted_files(tmp_path):
    d = tmp_path / "ns"
    s = NumpyVectorStore(persist_dir=d)
    _add_one(s)
    assert d.exists()
    s.reset()
    assert s.count() == 0
    assert not d.exists()  # dir removed so a re-run starts clean


def test_in_memory_when_no_persist_dir():
    s = NumpyVectorStore()  # no persist_dir → pure in-memory
    _add_one(s)
    assert s.count() == 1
    # Nothing written to disk; a fresh in-memory instance is empty.
    assert NumpyVectorStore().count() == 0


def test_delete_persists(tmp_path):
    d = tmp_path / "ns"
    s = NumpyVectorStore(persist_dir=d)
    _add_one(s, doc_id="keep", text="keep me")
    _add_one(s, doc_id="drop", text="drop me")
    s.delete({"doc_id": "drop"})
    reloaded = NumpyVectorStore(persist_dir=d)
    assert reloaded.count() == 1
    assert reloaded.query([0.1, 0.2, 0.3], n_results=1)["documents"][0] == ["keep me"]


def test_corrupt_file_starts_empty(tmp_path):
    d = tmp_path / "ns"
    d.mkdir(parents=True)
    (d / "vectors.npz").write_bytes(b"not a real npz")
    (d / "meta.json").write_text("{ broken json", encoding="utf-8")
    s = NumpyVectorStore(persist_dir=d)  # must not raise
    assert s.count() == 0
