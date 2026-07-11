"""
Software Intelligence Platform (OSIP) for AEOS
===============================================
Enterprise-grade Open Source Intelligence Platform for automated software
analysis, security scanning, and code intelligence.

Version: 0.1.0
"""

__version__ = "0.1.0"

from software_intelligence.pipelines.repository_pipeline import (
    RepositoryProcessingPipeline,
    PipelineConfig,
    PipelineResult,
)
from software_intelligence.repository.providers import get_provider
from software_intelligence.parsers.base import ParserEngine
from software_intelligence.search.engine import SearchEngine
from software_intelligence.cache.store import CacheStore

__all__ = [
    "RepositoryProcessingPipeline",
    "PipelineConfig",
    "PipelineResult",
    "get_provider",
    "ParserEngine",
    "SearchEngine",
    "CacheStore",
]
