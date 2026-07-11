# AEOS Phase 9 DRP — Implementation Guidelines

**Document:** `025-IMPLEMENTATION_GUIDELINES.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06  
**Audience:** All engineers contributing to Phase 9B implementation

---

## Purpose

This document provides the engineering rules, standards, and Definition of Done for every contributor to AEOS Phase 9B. These guidelines ensure that implementations conform to the Architecture Contract (`015`), invariants (`019`), and ADRs (`014`). They also ensure that the codebase remains maintainable as the system scales.

These rules are enforced by code review, automated linting, and conformance tests.

---

## 1. Coding Standards

### CS-001 — Language and Runtime
- **Language:** Python 3.11+ (async/await; no threading for I/O)
- **Type annotations:** Required on all function signatures. `mypy --strict` must pass
- **Async:** All I/O operations MUST be async. Blocking I/O in async code is prohibited
- **Formatting:** `black` (line length 88) + `isort`. Enforced by CI
- **Linting:** `ruff` (replaces flake8/pylint). Zero warnings in new code

### CS-002 — Import Structure
```python
# Standard library imports first
import asyncio
import time
from typing import Optional

# Third-party imports second (alphabetical)
import aioredis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

# AEOS internal imports last (from most general to most specific)
from app.kernel import HyperKernel
from app.execution.schemas import WorkflowState
from app.execution.engine import ExecutionEngine
```

Circular imports are prohibited. If a circular dependency is discovered, it indicates a layering violation that must be resolved architecturally (extract shared types to a `schemas.py` or `types.py` module).

### CS-003 — Error Handling
```python
# CORRECT: specific exception types with context
try:
    await redis.execute_command("SETNX", lease_key, worker_id, "EX", 120)
except aioredis.ConnectionError as exc:
    raise LeaseAcquisitionError(
        f"Redis unreachable during lease acquisition for step {step_id}"
    ) from exc

# WRONG: bare except or too-broad Exception catch
try:
    await redis.execute_command(...)
except Exception:  # ← never do this in production code
    pass
```

- Never swallow exceptions silently
- Always include context when re-raising: `raise NewError(...) from original_exc`
- Log at ERROR before re-raising; log at WARNING for expected/recoverable errors

### CS-004 — Logging
All logs MUST be structured JSON:
```python
# Use structlog, not stdlib logging directly
import structlog
logger = structlog.get_logger(__name__)

# CORRECT: structured fields
logger.info(
    "step_execution_complete",
    workflow_id=wf_id,
    step_id=step_id,
    duration_ms=elapsed_ms,
    node_type=node.node_type,
)

# WRONG: f-string messages with embedded fields
logger.info(f"Step {step_id} complete in {elapsed_ms}ms")  # not searchable
```

Required fields in every log entry:
- `timestamp` (ISO 8601, added by structlog)
- `level`
- `service` (e.g., `aeos-worker`)
- `worker_node_id` (for worker-side logs)
- `workflow_id` and `step_id` (when in workflow context)
- `trace_id` (when trace context is available)

### CS-005 — Metrics
All new code paths MUST emit metrics. Every latency-sensitive operation MUST use a Histogram:
```python
from prometheus_client import Histogram, Counter, Gauge

# CORRECT: Histogram for latency
STEP_EXECUTION_DURATION = Histogram(
    "aeos_step_execution_duration_seconds",
    "Duration of step execution",
    labelnames=["node_type", "status"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

with STEP_EXECUTION_DURATION.labels(node_type="AgentNode", status="success").time():
    result = await execute_agent_node(node, context)

# WRONG: Summary (cannot be aggregated across workers)
from prometheus_client import Summary
STEP_DURATION_SUMMARY = Summary(...)  # ← prohibited for latency
```

### CS-006 — Secrets
Secrets MUST be sourced from Vault agent at runtime. They MUST NOT appear in:
- Environment variables
- ConfigMaps
- Docker images
- Source code (even as test defaults)

```python
# CORRECT: read from Vault-mounted file
def get_redis_password() -> str:
    with open("/var/run/secrets/aeos/redis-password") as f:
        return f.read().strip()

# WRONG: hardcoded or env var
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "changeme")  # ← prohibited
```

---

## 2. Architecture Boundaries

### AB-001 — Layer Hierarchy
The AEOS codebase follows a strict layered architecture. Lower layers MUST NOT import from higher layers:

```
Layer 5: API / HTTP (app/main.py, app/api/)
    ↑ imports
Layer 4: Orchestration (app/orchestration/)
    ↑ imports
Layer 3: Execution Engine (app/execution/)
    ↑ imports
Layer 2: Agents / Cognitive (app/agents/)
    ↑ imports
Layer 1: Kernel / Infrastructure (app/kernel/, app/core/)
```

A linting rule (`ruff` custom check or import-linter) MUST enforce that no Layer N imports from Layer > N.

### AB-002 — Service Boundaries
Services communicate via defined interfaces only:
- Worker ↔ Cluster Manager: gRPC (Appendix A.4 proto)
- Worker ↔ Kafka: AIOKafka producer/consumer
- Worker ↔ Redis: aioredis (via `_wf_key()` helper for all wf-scoped keys)
- Worker ↔ Policy Service: gRPC (Appendix A.3 proto)
- Worker ↔ Capability Registry: gRPC (Appendix A.2 proto)

Direct database access from workers is prohibited (workers do not connect to Postgres directly; LTM access goes through `app/core/memory_v2.py`).

### AB-003 — Redis Key Discipline
All Redis keys for workflow-scoped state MUST use the `_wf_key()` helper:
```python
from app.execution.redis_keys import _wf_key

# CORRECT
key = _wf_key(workflow_id, f"step:{step_id}:result")
# → "{wf:abc123}:step:7:result"

# WRONG: bare key without hashtag
key = f"workflow:{workflow_id}:step:{step_id}:result"  # ← CROSSSLOT risk
```

The `_wf_key()` function MUST be the only way to construct workflow-scoped Redis keys. Lint checks MUST flag any string starting with `"workflow:"` or `"wf:"` that doesn't use `_wf_key()`.

### AB-004 — Kafka Consumer Group Discipline
New Kafka consumers MUST use the `KafkaConsumerFactory` to ensure correct group ID assignment:
```python
from app.messaging.kafka_factory import KafkaConsumerFactory

factory = KafkaConsumerFactory(node_id=self.node_id)

# For task topics (competing consumer):
task_consumer = factory.create_task_consumer(topics=["aeos.tasks.normal"])
# Automatically uses group_id="aeos-workers"

# For event topics (fan-out):
event_consumer = factory.create_event_consumer(topics=["aeos.events.governance"])
# Automatically uses group_id=f"aeos-worker-{self.node_id}"
```

Direct instantiation of `AIOKafkaConsumer` with a hardcoded `group_id` is prohibited outside of `kafka_factory.py`.

---

## 3. Dependency Rules

### DR-001 — Allowed Third-Party Dependencies
New third-party dependencies MUST be approved via an ADR before inclusion. The current approved dependency set:

| Package | Purpose | Version constraint |
|---------|---------|-------------------|
| `fastapi` | HTTP API framework | >=0.110 |
| `uvicorn` | ASGI server | >=0.29 |
| `aiokafka` | Async Kafka client | >=0.10 |
| `aioredis` | Async Redis client | >=2.0 |
| `grpcio`, `grpcio-tools` | gRPC | >=1.60 |
| `protobuf` | Protocol Buffers | >=4.25 |
| `pydantic` | Data validation | >=2.0 |
| `structlog` | Structured logging | >=24.0 |
| `prometheus_client` | Metrics | >=0.20 |
| `hvac` | Vault client | >=2.0 |
| `opentelemetry-*` | Distributed tracing | >=1.22 |
| `sqlalchemy` | ORM (async) | >=2.0 |
| `alembic` | DB migrations | >=1.13 |
| `weaviate-client` | Vector DB client | >=4.0 |
| `anthropic` | LLM client | >=0.20 |

### DR-002 — Prohibited Dependencies
The following packages are explicitly prohibited:

| Package | Reason |
|---------|--------|
| `redis` (sync) | Use `aioredis` (async); sync Redis blocks the event loop |
| `kafka-python` | Use `aiokafka`; kafka-python is not async |
| `threading` | Use `asyncio`; threads + async is an anti-pattern |
| `requests` | Use `httpx` (async); `requests` is blocking |
| `confluent-kafka` (in worker) | Use `aiokafka`; confluent-kafka requires native extension |

### DR-003 — Dependency Pinning
All dependencies MUST be pinned to a specific version in `requirements.txt` (for exact reproducibility) and bounded in `pyproject.toml` (for library compatibility). Example:
```toml
# pyproject.toml — compatible version range
dependencies = [
    "aiokafka>=0.10,<1.0",
    "aioredis>=2.0,<3.0",
]
```
```
# requirements.txt — exact pin for deployments
aiokafka==0.11.0
aioredis==2.0.1
```

---

## 4. Review Checklist

Every pull request MUST pass this checklist before merge. Reviewers check each item explicitly.

### Architecture Review
- [ ] Does the change conform to the Architecture Contract (015)?
- [ ] Does the change preserve all invariants (019)? List affected invariants.
- [ ] Are all Redis keys using `_wf_key()` for wf-scoped state?
- [ ] Is the Kafka consumer group assignment using `KafkaConsumerFactory`?
- [ ] Does the change introduce any circular imports or layer violations?
- [ ] If a new ADR-worthy decision is made, is an ADR drafted?

### Protocol and State Machine Review
- [ ] If a state transition is introduced, does it match the spec in (017)?
- [ ] If a protocol sequence is changed, does it match the spec in (016)?
- [ ] Are invalid state transitions raising `StateMachineViolation`?

### Security Review
- [ ] Are there any secrets hardcoded or in environment variables?
- [ ] Are all new endpoints authenticated and authorized?
- [ ] Could any new code introduce injection vulnerabilities (prompt injection, SQL injection)?
- [ ] Are governance bypass paths possible in the new code?

### Testing Review
- [ ] Unit tests for all new code paths (coverage ≥ 80% for new files)
- [ ] Integration tests for any new protocol interactions
- [ ] State machine tests for new states/transitions
- [ ] Are failure paths tested (not just happy path)?
- [ ] Do tests mock at the right boundary (not mocking internal state)?

### Observability Review
- [ ] Does new code emit appropriate metrics (Histograms for latency)?
- [ ] Are all new log statements structured (not f-strings)?
- [ ] Are trace spans created for new cross-service calls?

### Performance Review
- [ ] Does the change have a potential performance regression?
- [ ] Are there any blocking I/O calls in async code paths?
- [ ] Are Redis/Kafka calls batched where appropriate?

---

## 5. Testing Requirements

### TR-001 — Test Coverage
| Code type | Minimum coverage |
|-----------|-----------------|
| State machine transitions | 100% (all valid + all invalid) |
| Protocol implementations | 100% happy path + top 3 failure modes |
| Security-critical code (token validation, governance) | 100% |
| Business logic | 80% line coverage |
| Infrastructure adapters (Redis, Kafka, gRPC) | 60% (integration tests cover the rest) |

### TR-002 — Test Categories
Every implementation module MUST have tests in the following categories:

**Unit tests** (`tests/unit/`):
- Test one component in isolation
- All external dependencies mocked (Redis, Kafka, gRPC stubs)
- Fast (< 10ms each)
- No network I/O

**Integration tests** (`tests/integration/`):
- Test component interactions with real infrastructure (Redis, Kafka via testcontainers)
- Verify protocol sequences (not just individual calls)
- Slower (< 10s each)
- Use `pytest-asyncio` for async tests

**Conformance tests** (`tests/conformance/`):
- Reference contract IDs and invariant IDs in docstrings
- Test against a running cluster (staging or integration environment)
- Slow (minutes)

### TR-003 — Test Naming Convention
```python
def test_<what_is_being_tested>_<condition>_<expected_outcome>():
    """
    Contract: AC-EXEC-001
    Invariant: INV-EXEC-004
    """
    ...

# Examples:
def test_setnx_returns_zero_skips_execution():
def test_expired_token_raises_token_validation_error():
def test_phase1_checkpoint_atomic_with_multi_exec():
```

### TR-004 — Test Data Management
- Tests MUST NOT depend on external state (other tests, pre-existing database rows)
- Tests MUST clean up their data after completion (`try/finally` or fixtures with teardown)
- Tests MUST use deterministic data (no `time.time()` without mocking; no random UUIDs without seeding)

---

## 6. Performance Requirements for Implementations

### PR-001 — Async Discipline
All I/O MUST be async. This includes:
- Redis calls: use `await redis.execute_command(...)`
- Kafka produces: use `await producer.send_and_wait(...)`
- gRPC calls: use `await stub.Method(request)`
- Database queries: use `await session.execute(...)`
- HTTP calls: use `await httpx_client.get(...)` (not `requests.get()`)

Detecting blocking I/O in async code: run with `asyncio.set_event_loop_policy()` configured to use `uvloop` and set a task timeout; any blocking call > 10ms will be detected by the timeout.

### PR-002 — Connection Pooling
All connection pools MUST be initialized at service startup, not per-request:
```python
# CORRECT: pool created once at startup
class Worker:
    async def startup(self):
        self.redis_pool = await aioredis.create_redis_pool(...)
        self.kafka_producer = AIOKafkaProducer(...)
        await self.kafka_producer.start()

# WRONG: new connection per request
async def execute_step(self, ...):
    redis = await aioredis.create_redis(...)  # ← creates new connection every call
```

### PR-003 — Backpressure Implementation
Workers MUST implement the backpressure protocol:
```python
class Worker:
    MAX_IN_FLIGHT = 10
    
    async def consume_loop(self):
        async for message in self.task_consumer:
            if self.in_flight_tasks >= self.MAX_IN_FLIGHT:
                self.task_consumer.pause()  # Stop consuming
                await self._wait_for_capacity()
                self.task_consumer.resume()
            
            asyncio.create_task(self._process_task(message))
            self.in_flight_tasks += 1
    
    async def _wait_for_capacity(self):
        while self.in_flight_tasks > self.MAX_IN_FLIGHT // 2:
            await asyncio.sleep(0.1)
```

---

## 7. Security Requirements for Implementations

### SR-001 — Input Validation
All external inputs (API requests, Kafka messages, gRPC requests) MUST be validated using Pydantic models before processing:
```python
class TaskSubmissionRequest(BaseModel):
    task_type: str = Field(..., pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    payload: dict
    deadline_unix: int = Field(..., gt=0)
    priority: TaskPriority = TaskPriority.NORMAL
    
    @validator("deadline_unix")
    def deadline_must_be_future(cls, v):
        if v <= int(time.time()):
            raise ValueError("deadline must be in the future")
        return v
```

### SR-002 — SQL Injection Prevention
All database queries MUST use parameterized queries (SQLAlchemy ORM or `text()` with bound parameters). String formatting in SQL queries is prohibited:
```python
# CORRECT: parameterized
result = await session.execute(
    select(Policy).where(Policy.task_type == task_type)
)

# WRONG: string formatting (SQL injection risk)
result = await session.execute(
    f"SELECT * FROM policies WHERE task_type = '{task_type}'"  # ← prohibited
)
```

### SR-003 — Prompt Injection Awareness
When constructing LLM prompts that include user-supplied content, the content MUST be sanitized and clearly delineated from system instructions:
```python
# CORRECT: delineated user input
system_prompt = "You are a research assistant. Analyze the following topic:"
user_content = sanitize_for_prompt(user_input)
full_prompt = f"{system_prompt}\n\n<user_input>\n{user_content}\n</user_input>"

# WRONG: directly interpolating user input into instructions
full_prompt = f"Analyze this topic and {user_input}"  # ← prompt injection risk
```

### SR-004 — Audit Log Writes are Synchronous for Rejections
```python
# CORRECT: synchronous audit write for REJECTED decisions
async def evaluate_governance(self, request):
    result = await self._evaluate(request)
    
    if result.decision == Decision.REJECTED:
        # Synchronous write — not fire-and-forget
        await self.audit_log.write(
            task_id=request.task_id,
            decision=Decision.REJECTED,
            reason=result.reason,
            timestamp_ns=time.time_ns()
        )
    
    return result
```

---

## 8. Documentation Requirements

### DOC-001 — Module Docstrings
Every new module MUST have a module-level docstring explaining:
- What the module does
- What it owns/is responsible for
- What it does NOT do (scope boundaries)
- Key classes/functions to be aware of

### DOC-002 — Protocol Reference Comments
Any code implementing a protocol from (016) MUST reference the protocol ID:
```python
async def acquire_execution_lease(self, wf_id: str, step_id: str) -> bool:
    """Acquire execution lease for a step.
    
    Protocol: PROTO-019 (Execution Lease Acquisition)
    Contract: AC-EXEC-001
    Invariant: INV-EXEC-004
    
    Returns True if lease acquired, False if held by another worker.
    Worker MUST NOT execute the step if this returns False.
    """
    lease_key = _wf_key(wf_id, f"step:{step_id}:lease")
    result = await self.redis.setnx(lease_key, self.node_id)
    if result:
        await self.redis.expire(lease_key, 120)
    return bool(result)
```

### DOC-003 — ADR References for Non-Obvious Decisions
When implementing a design choice that is not obvious, reference the ADR:
```python
# Kafka consumer group IDs: task topics use shared group, event topics use per-worker group.
# See ADR-002 (Consumer Group Separation) and PROTO-006.
# DO NOT change the group_id assignment without updating ADR-002.
```

---

## 9. Definition of Done

A Phase 9B feature or component is **Done** when ALL of the following are true:

### Code Quality
- [ ] `mypy --strict` passes with zero errors
- [ ] `ruff check` passes with zero warnings
- [ ] `black --check` passes (code is formatted)
- [ ] `isort --check` passes (imports are sorted)

### Testing
- [ ] All unit tests pass (`pytest tests/unit/`)
- [ ] All integration tests pass (`pytest tests/integration/`)
- [ ] All conformance tests pass for affected contract IDs (`pytest tests/conformance/ -k <relevant_tests>`)
- [ ] Coverage meets TR-001 thresholds
- [ ] State machine tests cover all new states and transitions

### Architecture Conformance
- [ ] Architecture Contract review checklist (Section 4) signed off
- [ ] All affected invariants verified by tests
- [ ] All affected protocols implemented per (016) spec
- [ ] All state machines implemented per (017) spec
- [ ] Import layer audit: no layer violations

### Observability
- [ ] All required metrics emitted (AC-OBS-002)
- [ ] All latency metrics use Histograms (not Summaries)
- [ ] Structured logging on all new code paths
- [ ] Trace context propagated for cross-service calls

### Security
- [ ] Security review checklist (Section 4) signed off
- [ ] No secrets in code, environment variables, or ConfigMaps
- [ ] Input validation on all external inputs
- [ ] Governance bypass path audit: no new bypasses

### Deployment
- [ ] Deployment runbook updated
- [ ] Kubernetes manifests updated (if new service or config change)
- [ ] Alembic migration created (if DB schema change)
- [ ] Health check endpoints functional
- [ ] Chaos experiment defined for new critical path (if introducing new infrastructure dependency)

### Documentation
- [ ] Module docstrings present
- [ ] Protocol/ADR references in non-obvious code
- [ ] Architecture documents updated if design changed
- [ ] CHANGELOG entry added

---

## 10. Wave-by-Wave Implementation Order

The following implementation order MUST be followed for Phase 9B. Starting a wave before all conformance tests from the previous wave pass is prohibited.

| Wave | Components | Dependencies | Key Contracts |
|------|-----------|-------------|---------------|
| **9B.1** | gRPC service stubs, Kafka producer/consumer factory, Redis key helpers, mTLS setup | None | AC-NET-001, AB-003, AB-004 |
| **9B.2** | Cluster Manager (Raft), CM gRPC service, membership state machine | 9B.1 complete | DC-001, DC-002, SM-RAFT, SM-CLUSTER-MEMBER |
| **9B.3** | Distributed Scheduler (in-worker), workflow engine extensions, KEDA config | 9B.2 complete | AC-SCHED-001, DC-007, SM-TASK |
| **9B.4** | Distributed memory (WTM Redis, LTM Postgres, episodic Weaviate) | 9B.3 complete | INV-MEM-001, DC-009, SM-MEMORY |
| **9B.5** | Capability Registry, capability federation, capability advertisement | 9B.2 complete | PROTO-011, PROTO-012, SM-CAPABILITY |
| **9B.6** | Cloud runtime (KEDA, PDB, NetworkPolicy, PodAntiAffinity, Vault, Terraform) | 9B.3 complete | AC-CLOUD-001–005, AC-SEC-001–005 |
| **9B.7** | Production hardening, chaos engineering runs, performance benchmarks | All waves complete | `020`, `022` acceptance criteria |

---

*End of Implementation Guidelines — `025-IMPLEMENTATION_GUIDELINES.md`*
