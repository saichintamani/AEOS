# AEOS Changelog

All notable changes to AEOS are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- Phase 10: Developer Platform
  - `aeos` CLI (`init`, `start`, `cluster`, `workflow`, `benchmark`, `validate`)
  - Workflow DSL/YAML compiler with variable interpolation and `depends_on` support
  - 4 project templates: `minimal`, `research-assistant`, `rag-system`, `multi-agent`
  - Public SDK: `AEOSClient`, `WorkflowBuilder`, `AgentConfig`, `RunResult`
  - 4 runnable examples: research, RAG, multi-agent collaboration, enterprise approval
  - `pyproject.toml` with `aeos` CLI entrypoint and optional dependency groups
  - `Makefile` for common developer tasks

---

## [0.1.0-alpha] — 2026-07-11

### Added
- Phase 9B.6: Production Hardening
  - `GET /metrics` — Prometheus text format scrape endpoint
  - `app/distributed/observability/prometheus.py` — PrometheusExporter, APIMetrics
  - `app/api/validation.py` — 6 REST endpoints for invariant engine
  - InvariantEngine wired into FastAPI lifespan (30s background monitor)
  - `infrastructure/monitoring/grafana/dashboards/aeos-overview.json` — 13-panel Grafana dashboard
  - `scripts/benchmark.py` — performance benchmark runner (local + HTTP modes)
  - `docker-compose.cluster.yml` — 3-node AEOS cluster with monitoring profile
  - `scripts/cluster.sh` — cluster management shell script

- Phase 9B.5: Real Distributed Runtime
  - KafkaTransport (aiokafka, idempotent producer, DLQ)
  - RedisLeaseStore (Lua scripts, fencing tokens)
  - GrpcChannel + GrpcServiceRegistry
  - Full Raft consensus (RaftNode: election, heartbeat, log replication)
  - RedisMembershipStore
  - RedisCheckpointStore (two-phase protocol)
  - DistributedScheduler (LeaderScheduler + WorkerScheduler + AdmissionControl)
  - CapabilityFederation (TTL eviction, advertise/discover/match)
  - Chaos test suite (8 fault scenarios)
  - 5-worker in-process E2E demo

- Phase 9B.3–9B.4: Distributed Runtime Infrastructure
  - Worker pool, backpressure, runtime intelligence
  - Knowledge graph, learning systems, self-healing
  - Plugin architecture, SDK

- Phase 8.3: Distributed Execution Engine (DEE)
  - EventBus, MetricsCollector, PriorityQueue, CircuitBreaker
  - CheckpointStore, WorkerPool, 10 node executors, DispatchingExecutor
  - ExecutionPlanner, ReplayEngine, GraphVisualizer (Mermaid/DOT/ASCII)

- Phase 8A: HyperKernel
  - AEOSKernel (6-phase boot), EventBus, ServiceRegistry, ResourceManager
  - PolicyEngine, Scheduler, HealthManager, LocalLifecycleManager
  - CognitiveAgent v2 (11-step pipeline)
  - 4-tier memory (Sensory/Working/LongTerm/Episodic)

- Phase 9A: Architecture specification
  - DRP RFC, ADRs, protocol specs, state machines, invariant catalog
  - Chaos plans, conformance test plans, performance benchmark specs

- Phase 5: ML Platform (`ml_platform/`)
  - Datasets, feature store, training engine, experiment tracker
  - Model registry, inference engine, A/B routers, monitoring, SHAP/LIME

- Phase 2: RAG Knowledge Intelligence Layer
  - 7 loader types, 3 chunkers, SentenceTransformer + OpenAI embeddings
  - NumPy + ChromaDB vector stores, 5-stage retrieval pipeline

- Phase 1: Core Runtime
  - FastAPI app, Orchestrator, BaseAgent, structured JSON logger
  - 3 endpoints: `/health`, `/api/v1/run`, `/api/v1/debug/state`

### Test Coverage
- 511 tests passing (131 in validation/metrics, 331 in Phase 9B distributed infra)

---

[Unreleased]: https://github.com/your-org/aeos/compare/v0.1.0-alpha...HEAD
[0.1.0-alpha]: https://github.com/your-org/aeos/releases/tag/v0.1.0-alpha
