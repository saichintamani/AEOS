"""
AEOS Public SDK — Phase 10

Stable public API surface for building applications on AEOS.
Import from here, not from internal app.* modules.

Usage::

    from aeos.sdk import AEOSClient, WorkflowBuilder, AgentConfig

    client = AEOSClient(base_url="http://localhost:8000")
    result = await client.run("Summarise this document", mode="multi-agent")
"""

from aeos.sdk.client import AEOSClient
from aeos.sdk.workflow import WorkflowBuilder
from aeos.sdk.types import AgentConfig, RunResult, WorkflowResult

__all__ = [
    "AEOSClient",
    "WorkflowBuilder",
    "AgentConfig",
    "RunResult",
    "WorkflowResult",
]
