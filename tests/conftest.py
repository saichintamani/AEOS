"""
AEOS Test Suite — Fixtures & Shared Config
"""
import os
# Prevent tensorflow import via transformers (protobuf conflict in conda envs)
os.environ["USE_TF"] = "0"
os.environ["TRANSFORMERS_NO_TENSORFLOW"] = "1"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture(scope="session")
async def async_client():
    from app.main import app, _orchestrator
    from app.agents.simple_agent import SimpleAgent
    from app.agents.planner_agent import PlannerAgent
    from app.agents.research_agent import ResearchAgent
    from app.agents.reviewer_agent import ReviewerAgent

    # ASGITransport does not run ASGI lifespan events, so bootstrap manually.
    if not _orchestrator._ready:
        for agent_cls in (SimpleAgent, PlannerAgent, ResearchAgent, ReviewerAgent):
            try:
                _orchestrator.register(agent_cls())
            except Exception:
                pass  # already registered in a previous fixture call
        await _orchestrator.startup()
        app.state.orchestrator = _orchestrator

        try:
            from app.rag.rag_engine import get_rag_engine
            await get_rag_engine().initialize()
        except Exception:
            pass

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    await _orchestrator.shutdown()


@pytest.fixture
def sample_task():
    return "Analyze the codebase and identify performance bottlenecks"


@pytest.fixture
def ml_inline_data():
    """Minimal dataset for ML pipeline tests."""
    return [
        {"x1": 1.0, "x2": 0.5, "label": 0},
        {"x1": 2.0, "x2": 1.5, "label": 1},
        {"x1": 3.0, "x2": 2.5, "label": 1},
        {"x1": 4.0, "x2": 3.5, "label": 0},
        {"x1": 5.0, "x2": 4.5, "label": 1},
        {"x1": 1.5, "x2": 0.8, "label": 0},
        {"x1": 2.5, "x2": 1.8, "label": 1},
        {"x1": 3.5, "x2": 2.8, "label": 0},
        {"x1": 4.5, "x2": 3.8, "label": 1},
        {"x1": 0.5, "x2": 0.2, "label": 0},
    ]


@pytest.fixture
def rag_engine():
    from app.rag.rag_engine import RAGEngine
    engine = RAGEngine(namespace="test_isolated")
    yield engine
    engine.reset()
