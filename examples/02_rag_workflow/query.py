"""
AEOS Example 2 — RAG Query

Query the knowledge base using semantic search.

Usage:
    python query.py "What is AEOS?"
    python query.py "How does Raft consensus work?"
"""

import asyncio
import sys


async def query(question: str, top_k: int = 5, host: str = "http://localhost:8000"):
    try:
        import httpx
    except ImportError:
        print("Install httpx: pip install httpx")
        return

    print(f"\nQuery: {question}")
    print("-" * 60)

    async with httpx.AsyncClient(base_url=host, timeout=30.0) as client:
        # Direct RAG query (semantic search)
        resp = await client.post(
            "/api/v1/rag/query",
            json={"query": question, "top_k": top_k},
        )
        data = resp.json()
        results = data.get("results", [])
        if results:
            print(f"\nTop {len(results)} retrieved chunks:")
            for r in results:
                print(f"  [{r['rank']}] score={r['score']:.3f}  {r['text'][:100]}...")
        else:
            print("No results found. Have you ingested any documents? (python ingest.py)")
            return

        # Agent-augmented answer
        print("\nAgent answer:")
        resp2 = await client.post(
            "/api/v1/run",
            json={
                "task": f"Using the knowledge base, answer: {question}",
                "mode": "single-agent",
            },
            timeout=60.0,
        )
        data2 = resp2.json()
        result = data2.get("result") or data2.get("response") or "(no response)"
        print(f"  {str(result)[:400]}")


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is AEOS?"
    asyncio.run(query(q))
