"""
AEOS Example 2 — Document Ingestion

Ingests a text file (or inline text) into the AEOS RAG knowledge base.

Usage:
    python ingest.py my_document.txt
    python ingest.py --text "AEOS is an AI orchestration platform." --source manual
"""

import asyncio
import argparse
from pathlib import Path


async def ingest(text: str, source: str, namespace: str = "default", host: str = "http://localhost:8000"):
    try:
        import httpx
    except ImportError:
        print("Install httpx: pip install httpx")
        return

    print(f"Ingesting {len(text)} characters from '{source}' into namespace '{namespace}' ...")
    async with httpx.AsyncClient(base_url=host, timeout=60.0) as client:
        resp = await client.post(
            "/api/v1/rag/ingest",
            json={"text": text, "source": source, "namespace": namespace},
        )
        data = resp.json()
        if data.get("status") == "success":
            print(f"Done. {data.get('chunks_added', '?')} chunks added.")
        else:
            print(f"Error: {data}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest documents into AEOS RAG")
    parser.add_argument("file", nargs="?", help="Path to a text file to ingest")
    parser.add_argument("--text", help="Inline text to ingest")
    parser.add_argument("--source", default="file", help="Source label")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--host", default="http://localhost:8000")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text()
        source = args.source if args.source != "file" else args.file
    elif args.text:
        text = args.text
        source = args.source
    else:
        # Demo: ingest a sample text about AEOS
        text = """\
AEOS (AI Engineering Orchestration System) is a production-grade distributed
multi-agent runtime. It combines a Raft-based consensus layer, Kafka transport,
Redis coordination, and a 15-stage execution engine to orchestrate AI agents
at scale. AEOS supports RAG (Retrieval-Augmented Generation), ML pipelines,
GitHub analysis, and complex multi-agent workflows through a declarative YAML DSL.
"""
        source = "sample/aeos-description"

    asyncio.run(ingest(text, source=source, namespace=args.namespace, host=args.host))
