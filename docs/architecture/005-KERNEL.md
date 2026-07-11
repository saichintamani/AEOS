# AEOS Kernel Architecture

---

| Field         | Value                                              |
|---------------|----------------------------------------------------|
| **Document**  | 005-KERNEL.md                                      |
| **Status**    | Approved                                           |
| **Version**   | 1.0.0                                              |
| **Authors**   | AEOS Platform Team                                 |
| **Created**   | 2026-07-05                                         |
| **Supersedes**| None (new document)                                |
| **See Also**  | [001-ARCHITECTURE.md](001-ARCHITECTURE.md), [006-EXECUTION_ENGINE.md](006-EXECUTION_ENGINE.md), [015-PLUGIN_ARCHITECTURE.md](015-PLUGIN_ARCHITECTURE.md) |

---

## Abstract

The AEOS Kernel is the central runtime coordinator of the AI Engineering Operating System — the single mandatory choke-point through which every platform operation, resource request, and lifecycle event passes. It sits between the FastAPI request boundary and the entire runtime layer (Execution Engine, Plugin Registry, Service Registry, Event Bus), providing uniform policy enforcement, observability, resource accounting, and component lifecycle management. By modeling an AI platform runtime after a real operating-system kernel, AEOS achieves the predictability, auditability, and composability required for production-grade AI workloads.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Kernel Philosophy](#2-kernel-philosophy)
3. [Ownership Boundary](#3-ownership-boundary)
4. [Kernel Architecture Diagram](#4-kernel-architecture-diagram)
5. [Boot Sequence](#5-boot-sequence)
6. [Kernel Public API](#6-kernel-public-api)
7. [Plugin Contract](#7-plugin-contract)
8. [Service Contract](#8-service-contract)
9. [Event Model](#9-event-model)
10. [Resource Management](#10-resource-management)
11. [Policy Enforcement Points](#11-policy-enforcement-points)
12. [Shutdown Sequence](#12-shutdown-sequence)
13. [Failure Handling](#13-failure-handling)
14. [Future Extensions](#14-future-extensions)
15. [Glossary](#15-glossary)

---

## 1. Motivation

### 1.1 The Problem with Direct Routing

In AEOS v1, FastAPI routes called the Orchestrator directly:

```
HTTP Request → FastAPI Router → OrchestratorService → ExecutionEngine → Agent
```

This direct call chain worked for a single-service prototype but created compounding problems as the platform grew:

**1. Scattered policy enforcement.** Rate limiting lived in a FastAPI middleware, authorization in a separate decorator, and tool-level governance inside the Execution Engine. There was no single point to audit whether a policy was actually applied. Adding a new policy required editing 3–5 files.

**2. No resource accounting.** Agents could spawn sub-agents, call expensive LLM APIs, and allocate memory without any component knowing the aggregate cost. A single misbehaving agent could starve other workloads with no mechanism for detection or correction.

**3. Ad-hoc plugin loading.** Plugins (RAG, ML, GitHub) were imported at module load time via hardcoded `import` statements. Adding a plugin required modifying the core Orchestrator. Dependency ordering was not enforced.

**4. No lifecycle state machine.** Components could call each other before they were initialized. Shutdown was `Ctrl+C`, with no drain period and frequent data loss.

**5. Observability was bolted on.** Telemetry calls were sprinkled throughout business logic, making them easy to miss and impossible to enforce universally.

**6. Service discovery was implicit.** Components received references through constructor injection, but there was no registry. If a service needed to call another it had to be passed in at construction time — a brittle and circular-dependency-prone design.

### 1.2 The Kernel Solution

The AEOS Kernel inserts itself as a mandatory intermediary that owns all cross-cutting concerns:

```
HTTP Request → FastAPI Router → AEOSKernel → ExecutionEngine → Agent
                                     ↕
                           [Plugin Registry | Service Registry | Event Bus | Resource Manager | Policy Engine]
```

Every component that wants to do anything must go through the Kernel:
- Want to call a service? Ask the Kernel for it.
- Want to allocate memory? Request a grant from the Kernel.
- Want to emit a telemetry event? Publish through the Kernel event bus.
- Want to run a task? The Kernel schedules it.
- Want to load a plugin? The Kernel validates, orders, and initializes it.

The result is a platform where **every operation is accounted for, every policy is enforced, and every component is observable** — without any component needing to implement these concerns itself.

### 1.3 Analogy: Why "Kernel"?

A Linux kernel manages processes, memory, I/O, and system calls. Application code never directly accesses hardware — it issues system calls, and the kernel mediates every request. The AEOS Kernel applies this same principle to AI workloads:

| Linux Kernel        | AEOS Kernel                          |
|---------------------|--------------------------------------|
| Process scheduler   | Task scheduler (priority, ordering)  |
| Memory manager      | Resource manager (tokens, memory)    |
| Device drivers      | Plugin system                        |
| System call table   | Kernel Public API                    |
| `/proc` filesystem  | Health + observability endpoints     |
| `init` (PID 1)      | Kernel startup sequence              |
| cgroups             | Resource grants per agent/plugin     |
| SELinux/AppArmor    | Policy engine                        |

---

## 2. Kernel Philosophy

The Kernel is designed around five foundational principles. These are **not guidelines** — they are enforced by the architecture. Any change to the Kernel that violates these principles requires a formal Architecture Review.

### Principle 1: The Kernel Never Executes Domain Logic

The Kernel does not know what a "RAG query" is, what an "agent" does, or what "GitHub analysis" means. The Kernel knows: plugins, services, events, resources, policies, and lifecycle states. All domain logic lives inside plugins and services. The Kernel's job is to provide the *infrastructure* for domain logic to execute safely.

**Corollary:** Adding a new AI capability to AEOS means writing a new plugin — it never means modifying the Kernel.

### Principle 2: All Resources Are Kernel-Managed

No component allocates significant compute, memory, or external API quota without the Kernel's awareness. Components that need resources must request a `ResourceGrant`. The Kernel tracks allocations, enforces quotas, and reclaims grants on component failure. This principle prevents the "noisy neighbor" problem where one workload silently starves others.

**Corollary:** The Kernel is the source of truth for resource usage. Monitoring dashboards query the Kernel, not individual components.

### Principle 3: All Operations Are Observable

Every call to the Kernel Public API produces at least one `KernelEvent`. The Kernel emits telemetry for plugin loads, service registrations, policy decisions, resource grants, and lifecycle transitions — without requiring the caller to instrument anything. Observability is a kernel concern, not a component concern.

**Corollary:** A component that cannot be observed cannot be registered with the Kernel.

### Principle 4: Policies Are Centrally Declared and Universally Enforced

Security policies, rate limits, and governance rules are registered with the Kernel's Policy Engine once. The Kernel enforces them at every policy enforcement point (PEP) — before agent execution, before tool invocation, before external API calls. No component can bypass a registered policy.

**Corollary:** Removing or weakening a policy requires modifying the Policy Engine registration, not hunting through component code.

### Principle 5: The Kernel Is Fail-Safe, Not Fail-Open

When the Kernel is uncertain — when a health check times out, when a policy evaluation fails, when a resource request cannot be assessed — it **denies** the operation, not permits it. A platform that allows uncertain operations to proceed is not production-grade.

**Corollary:** All Kernel API methods that can fail must raise typed exceptions rather than returning ambiguous `None` or `False` values.

---

## 3. Ownership Boundary

This table defines exactly what the Kernel owns versus what is deliberately outside its scope. Violations of this boundary are architectural defects.

| Domain | Kernel Owns ✅ | Kernel Does NOT Own ❌ |
|--------|---------------|------------------------|
| **Plugins** | Manifest validation, dependency resolution, load ordering, lifecycle (init/shutdown), health tracking | Business logic inside a plugin, plugin configuration values |
| **Services** | Service registry, capability declarations, service discovery | What a service does, service-internal state |
| **Events** | Event bus, topic routing, subscription management, event schema validation | Event payload business meaning, event consumers' logic |
| **Resources** | Grant allocation, quota enforcement, usage tracking, grant revocation on failure | How a component uses its granted resources |
| **Policies** | Policy evaluation, PEP placement, policy registration | Policy authoring (policies are declared externally and registered) |
| **Scheduling** | Task priority, execution ordering, concurrency limits | Task execution (delegated to Execution Engine) |
| **Lifecycle** | State machine transitions, transition validation, lifecycle history | Component-internal behavior during state transitions |
| **Observability** | Telemetry emission for all kernel events, health aggregation | Metric visualization, alerting rules, dashboards |
| **Networking** | None | HTTP routing (FastAPI owns this), external API clients |
| **Persistence** | None | Storage (owned by individual services) |
| **AI Logic** | None | LLM calls, embeddings, agent reasoning, tool implementations |
| **Authentication** | Policy enforcement (is this actor authorized?) | Token issuance, OAuth flows, session management |

---

## 4. Kernel Architecture Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                           AEOS PLATFORM BOUNDARY                            ║
║                                                                              ║
║   ┌─────────────────────────────────────────────────────────────────────┐   ║
║   │                        FastAPI Layer                                │   ║
║   │   POST /tasks   GET /health   WS /stream   POST /plugins/load      │   ║
║   └───────────────────────────────┬─────────────────────────────────────┘   ║
║                                   │  Every request enters the Kernel         ║
║                                   ▼                                          ║
║   ╔═══════════════════════════════════════════════════════════════════════╗  ║
║   ║                         AEOS KERNEL                                  ║  ║
║   ║                                                                       ║  ║
║   ║  ┌──────────────────────────────────────────────────────────────┐   ║  ║
║   ║  │                  Kernel Public API Surface                    │   ║  ║
║   ║  │  register_plugin | register_service | emit | request_resources│   ║  ║
║   ║  │  enforce_policy  | subscribe        | get_service | health()  │   ║  ║
║   ║  └──────────┬───────────────────────────────────────────────────┘   ║  ║
║   ║             │                                                         ║  ║
║   ║  ┌──────────┴──────────────────────────────────────────────────┐    ║  ║
║   ║  │                   Kernel Core Bus                            │    ║  ║
║   ║  └──┬───────────┬──────────────┬──────────────┬───────────┬───┘    ║  ║
║   ║     │           │              │              │           │          ║  ║
║   ║  ┌──▼──┐  ┌─────▼────┐  ┌─────▼────┐  ┌─────▼───┐  ┌───▼──────┐  ║  ║
║   ║  │     │  │          │  │          │  │         │  │          │  ║  ║
║   ║  │ 1.  │  │   2.     │  │   3.     │  │   4.    │  │   5.     │  ║  ║
║   ║  │Plugin│  │ Service  │  │  Event   │  │Resource │  │ Policy   │  ║  ║
║   ║  │Mgr  │  │Registry  │  │   Bus    │  │Manager  │  │ Engine   │  ║  ║
║   ║  │     │  │          │  │          │  │         │  │          │  ║  ║
║   ║  └──┬──┘  └─────┬────┘  └─────┬────┘  └─────┬───┘  └───┬──────┘  ║  ║
║   ║     │           │              │              │           │          ║  ║
║   ║  ┌──▼───────────▼──────────────▼──────────────▼───────────▼──────┐  ║  ║
║   ║  │                   Kernel Core Services                         │  ║  ║
║   ║  │  ┌────────────┐  ┌──────────────┐  ┌────────────────────────┐ │  ║  ║
║   ║  │  │  6.        │  │    7.        │  │    8.                  │ │  ║  ║
║   ║  │  │ Scheduler  │  │  Lifecycle   │  │  Observability Hooks   │ │  ║  ║
║   ║  │  │            │  │  Manager     │  │  (Telemetry Middleware) │ │  ║  ║
║   ║  │  └────────────┘  └──────────────┘  └────────────────────────┘ │  ║  ║
║   ║  └────────────────────────────────────────────────────────────────┘  ║  ║
║   ╚═══════════════════════════════════════════════════════════════════════╝  ║
║                                   │                                          ║
║              ┌────────────────────┼─────────────────────┐                   ║
║              ▼                    ▼                     ▼                   ║
║   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐     ║
║   │  Execution       │  │  Plugin          │  │  Platform Services   │     ║
║   │  Engine          │  │  Runtime         │  │  (RAG, ML, GitHub,   │     ║
║   │  (Task Runner)   │  │  (Loaded Plugins)│  │   OSIP, Memory)      │     ║
║   └──────────────────┘  └──────────────────┘  └──────────────────────┘     ║
╚══════════════════════════════════════════════════════════════════════════════╝

Legend:
  1. Plugin Mgr       — manifest validation, dependency ordering, lifecycle
  2. Service Registry — registration, discovery, capability indexing
  3. Event Bus        — topic routing, subscription, wildcard matching
  4. Resource Manager — grant allocation, quota enforcement, usage tracking
  5. Policy Engine    — policy evaluation, PEP coordination, audit logging
  6. Scheduler        — task priority, ordering, concurrency limits
  7. Lifecycle Mgr    — state machine, transition validation, history
  8. Observability    — telemetry middleware wrapping all API calls
```

---

## 5. Boot Sequence

The Kernel follows a strict six-phase boot sequence. Phases are sequential — the Kernel does not advance to the next phase until all tasks in the current phase complete successfully. A failure in any phase halts boot and transitions the Kernel to `FAILED`.

```
CREATED ──► INITIALIZING ──► READY ──► RUNNING
                 │
                 │  (any phase failure)
                 ▼
              FAILED
```

### Phase 0: Pre-Boot — Configuration Loading and Environment Validation

**Duration target:** < 500 ms  
**Kernel state during phase:** `CREATED`

The Kernel has been instantiated but has not started any sub-systems. Phase 0 ensures the environment is sane before committing any resources.

**Tasks:**
1. Load the platform configuration file (`aeos.yaml` or environment variables). Fail fast if required keys are missing.
2. Validate all required environment variables are present (`OPENAI_API_KEY`, `DATABASE_URL`, etc.). Emit a structured error listing every missing variable — do not fail on the first missing variable.
3. Validate the plugin directory exists and is readable.
4. Validate the data directory has sufficient disk space (configurable threshold, default 1 GB).
5. Instantiate the structured logger. From this point forward, all boot output goes to the structured logger.
6. Emit `kernel.boot.pre_boot.started` event (to internal buffer — event bus is not yet running).
7. Set a boot timestamp. All subsequent boot phase durations are measured from this timestamp.

**Failure behavior:** Emit `kernel.boot.pre_boot.failed` with the reason. Transition to `FAILED`. Log all failures, not just the first.

---

### Phase 1: Core Services Initialization

**Duration target:** < 2 seconds  
**Kernel state during phase:** `INITIALIZING`

The Kernel initializes the internal services it needs to function. These are not platform services (RAG, ML) — they are Kernel infrastructure.

**Tasks:**
1. Start the internal event bus (in-memory queue, async consumers). Subscribe internal kernel handlers.
2. Initialize the metrics endpoint (`/metrics` in Prometheus format). Register kernel-level metrics: `aeos_kernel_plugins_loaded`, `aeos_kernel_tasks_scheduled`, `aeos_kernel_policy_decisions_total`, `aeos_kernel_resource_grants_active`.
3. Initialize the Lifecycle Manager. Register the kernel's own lifecycle.
4. Initialize the Scheduler with the configured concurrency limits.
5. Initialize the Resource Manager with the configured capacity (total memory budget, CPU millicores budget, API call quotas).
6. Initialize the Policy Engine. Load built-in policies (rate limiting, tool allowlist/denylist).
7. Initialize the Service Registry (empty).
8. Initialize the Plugin Manager (empty).
9. Emit `kernel.boot.phase1.complete` with timing.

**Failure behavior:** If any core service fails to initialize, the Kernel cannot function. Transition to `FAILED`, log the specific service failure, and terminate.

---

### Phase 2: Plugin Discovery and Loading

**Duration target:** < 10 seconds (configurable)  
**Kernel state during phase:** `INITIALIZING`

The Kernel scans the plugin directory, parses manifests, resolves the dependency graph, and loads plugins in topological order.

**Tasks:**

**2a. Manifest scanning:**
- Walk the `plugins/` directory (built-ins first, then external plugins).
- For each subdirectory containing a `plugin.yaml` manifest, parse and validate the manifest schema.
- Reject manifests with missing required fields (`id`, `name`, `version`, `min_aeos_version`).
- Emit `kernel.plugin.manifest.discovered` for each valid manifest.

**2b. Dependency resolution:**
- Build a directed acyclic graph (DAG) where each node is a plugin and each edge is a declared dependency.
- Detect cycles. If a dependency cycle exists, log all plugins in the cycle and abort Phase 2.
- Compute a topological sort of the DAG. This is the load order.
- Verify that all declared dependencies are present in the discovered manifest set. Log missing dependencies and abort if any are required (optional dependencies that are missing are skipped).

**2c. Version compatibility check:**
- For each plugin, verify `manifest.min_aeos_version <= current_aeos_version`.
- Reject plugins that require a newer AEOS version than running.

**2d. Plugin loading (in topological order):**
- For each plugin in dependency order:
  1. Import the plugin module.
  2. Instantiate the plugin class.
  3. Call `plugin.initialize(kernel=self)`.
  4. Wait up to `plugin_init_timeout_seconds` (default: 30s) for initialization.
  5. Call `plugin.health()` and verify it returns `PluginStatus.READY`.
  6. Register the plugin in the Plugin Manager.
  7. Emit `kernel.plugin.loaded` with the plugin manifest.

**Failure behavior:**
- If a non-required plugin fails to load, log the failure, mark it `FAILED`, and continue loading remaining plugins.
- If a required plugin (declared in `required_plugins` config) fails to load, abort Phase 2 and transition the Kernel to `FAILED`.

---

### Phase 3: Service Registration

**Duration target:** < 5 seconds  
**Kernel state during phase:** `INITIALIZING`

Platform services register themselves with the Kernel's Service Registry. This phase is the platform's service discovery initialization.

**Tasks:**
1. For each loaded plugin, call `plugin.register_services(kernel=self)`. Each plugin declares what services it provides.
2. For built-in platform services not owned by a plugin (e.g., the Task Queue, the Workflow Engine), call `kernel.register_service()` directly.
3. Build the capability index: for each registered capability string, map it to the set of services that provide it.
4. Validate that all required capabilities (declared in `required_capabilities` config) are satisfied.
5. Emit `kernel.service.registry.built` with total service count and capability index summary.

**Failure behavior:** If a required capability cannot be satisfied by any registered service, abort Phase 3 and transition to `FAILED`.

---

### Phase 4: Health Verification

**Duration target:** < 15 seconds  
**Kernel state during phase:** `INITIALIZING`

Every registered component is asked to report its health. The Kernel only accepts traffic when all required components are healthy.

**Tasks:**
1. Call `health()` on every registered plugin (in parallel, with timeout per plugin, default 10s).
2. Call `health()` on every registered service (in parallel, with timeout, default 10s).
3. Aggregate results into a `KernelHealth` snapshot.
4. If any **required** component reports unhealthy or times out, log the failure and abort.
5. If any **optional** component reports unhealthy, log a warning and continue.
6. Emit `kernel.boot.health_check.complete` with the health snapshot.

**Retry policy:** Health checks that timeout or fail may be retried up to `health_check_max_retries` times (default: 3) with `health_check_retry_delay_seconds` (default: 2s) between attempts.

---

### Phase 5: Ready — Traffic Accepted

**Duration target:** instantaneous  
**Kernel state after phase:** `RUNNING`

The Kernel transitions to `RUNNING` and begins accepting requests.

**Tasks:**
1. Transition Kernel lifecycle state: `INITIALIZING → READY → RUNNING`.
2. Record the `ready_at` timestamp.
3. Emit `kernel.boot.complete` with full boot summary (total duration, plugins loaded, services registered, capabilities available).
4. Signal the FastAPI startup event handler that the Kernel is ready. FastAPI begins accepting HTTP traffic.
5. Start background health polling (default interval: 30 seconds) for all registered components.
6. Start resource usage reporting (default interval: 60 seconds).

---

## 6. Kernel Public API

All methods in this section are part of the `AEOSKernel` abstract interface. Concrete implementations must satisfy these contracts exactly. All methods are available at `kernel.*` after `kernel.startup()` completes.

---

### `register_plugin(plugin: BasePlugin) -> None`

```python
async def register_plugin(self, plugin: BasePlugin) -> None:
    """
    Register a plugin with the kernel.

    The kernel validates the plugin manifest, resolves dependencies,
    and calls plugin.initialize() if the kernel is already running.
    This method is idempotent if the same plugin instance is registered
    twice, but raises PluginConflictError if two different plugins share
    the same manifest ID.

    Call sequence:
        1. Validate plugin.manifest schema completeness.
        2. Check for plugin ID conflict in the plugin registry.
        3. Verify all declared dependencies are already loaded.
        4. Check AEOS version compatibility (manifest.min_aeos_version).
        5. Call plugin.initialize(kernel=self).
        6. Wait up to plugin_init_timeout_seconds for completion.
        7. Call plugin.health() and assert PluginStatus.READY.
        8. Add to plugin registry.
        9. Emit kernel.plugin.loaded event.
        10. Register plugin capabilities with the Service Registry.

    Args:
        plugin: A BasePlugin instance with a valid manifest.

    Raises:
        PluginConflictError: A plugin with the same ID is already registered.
        PluginDependencyError: One or more declared dependencies are not loaded.
        PluginInitializationError: plugin.initialize() raised an exception
            or timed out, or plugin.health() did not return READY.
        KernelStateError: The kernel is not in RUNNING or INITIALIZING state.
    """
```

---

### `unload_plugin(plugin_id: str) -> None`

```python
async def unload_plugin(self, plugin_id: str) -> None:
    """
    Gracefully unload a plugin.

    Calls plugin.shutdown(), removes from registry, releases resources.
    If other plugins depend on this plugin, they are unloaded first
    (reverse dependency order).

    Call sequence:
        1. Look up plugin by plugin_id. Raise PluginNotFoundError if absent.
        2. Find all plugins that depend on this plugin (dependents).
        3. Recursively unload dependents first (reverse topological order).
        4. Set plugin lifecycle state to STOPPING.
        5. Call plugin.shutdown() with timeout (default: 30s).
        6. Release all ResourceGrants held by this plugin.
        7. Unregister plugin capabilities from the Service Registry.
        8. Remove from plugin registry.
        9. Emit kernel.plugin.unloaded event.

    Args:
        plugin_id: The plugin manifest ID (e.g., "rag_plugin").

    Raises:
        PluginNotFoundError: plugin_id is not registered.
        PluginShutdownError: plugin.shutdown() raised or timed out.
    """
```

---

### `get_plugin(plugin_id: str) -> BasePlugin | None`

```python
def get_plugin(self, plugin_id: str) -> BasePlugin | None:
    """
    Return the plugin instance for the given ID, or None if not registered.

    This is a synchronous, non-blocking lookup. No I/O is performed.

    Args:
        plugin_id: The plugin manifest ID.

    Returns:
        The BasePlugin instance, or None if not found.
    """
```

---

### `list_plugins() -> list[PluginManifest]`

```python
def list_plugins(self) -> list[PluginManifest]:
    """
    Return manifests for all currently loaded plugins.

    Returns manifests in load order (dependency-resolved topological order).
    Does not include plugins in FAILED or UNLOADED state.

    Returns:
        List of PluginManifest dataclasses, one per loaded plugin.
    """
```

---

### `register_service(service_id: str, service: Any, capabilities: list[str]) -> None`

```python
def register_service(
    self,
    service_id: str,
    service: Any,
    capabilities: list[str],
) -> None:
    """
    Register a platform service with the kernel service registry.

    Services are discoverable by any component via get_service().
    Capabilities declare what the service can do (used by the
    CapabilityRegistry for service discovery by capability rather than ID).

    A service registered with capabilities ["rag.query", "rag.index"] can
    be discovered by any component that calls:
        kernel.find_by_capability("rag.query")

    This method is synchronous because service registration occurs during
    Phase 3 (boot) and during plugin initialization, both of which are
    already in an async context managed by the caller.

    Args:
        service_id: Unique service identifier (e.g., "rag_engine").
            Convention: lowercase, underscore-separated, noun phrase.
        service: The service instance. Must implement the ServiceProtocol
            (health() method returning ServiceHealth).
        capabilities: List of dot-namespaced capability strings this
            service provides. Convention: "domain.action"
            Examples: ["rag.query", "rag.index", "rag.delete"]

    Raises:
        ServiceConflictError: A service with service_id is already registered.
        InvalidCapabilityError: A capability string fails schema validation.
    """
```

---

### `get_service(service_id: str) -> Any`

```python
def get_service(self, service_id: str) -> Any:
    """
    Retrieve a registered service by ID.

    This is the primary service discovery method. Components should call
    this rather than holding direct references to services, because the
    Kernel can replace a service at runtime (e.g., after a hot-reload).

    This method is synchronous and non-blocking. The Kernel does not
    perform I/O to locate services — the registry is an in-memory map.

    Args:
        service_id: The service identifier used during register_service().

    Returns:
        The service instance.

    Raises:
        ServiceNotFoundError: service_id is not in the service registry.
    """
```

---

### `list_services() -> dict[str, list[str]]`

```python
def list_services(self) -> dict[str, list[str]]:
    """
    Return mapping of service_id → capabilities for all registered services.

    Returns:
        Dictionary mapping each registered service_id to its list of
        declared capability strings. Ordering is registration order.

    Example:
        {
            "rag_engine": ["rag.query", "rag.index"],
            "ml_service": ["ml.train", "ml.predict"],
            "github_service": ["github.analyze", "github.clone"],
        }
    """
```

---

### `emit(event: KernelEvent) -> None`

```python
async def emit(self, event: KernelEvent) -> None:
    """
    Emit a kernel event to all registered subscribers on the event topic.

    The event is validated against the known event schema (if a schema is
    registered for the topic), then dispatched to all matching subscribers
    concurrently. Subscriber failures are caught, logged, and do not
    propagate to the emitter.

    Telemetry: Every call to emit() increments the kernel event counter
    metric, labeled by topic. This provides automatic event volume tracking
    without any additional instrumentation.

    Args:
        event: The KernelEvent to broadcast. Must have a non-empty topic
            and source. The timestamp is set automatically if empty.

    Note:
        emit() is fire-and-forget from the caller's perspective. Subscribers
        receive the event asynchronously. If ordering guarantees are needed,
        use a topic with a single subscriber and synchronous processing.
    """
```

---

### `subscribe(topic: str, handler: Callable[[KernelEvent], Coroutine]) -> None`

```python
def subscribe(
    self,
    topic: str,
    handler: Callable[[KernelEvent], Coroutine],
) -> None:
    """
    Subscribe an async handler to a topic pattern.

    Topic patterns support glob-style wildcards:
        "kernel.*"         — all direct kernel sub-topics
        "kernel.plugin.*"  — all kernel.plugin events
        "agent.result"     — exact topic match only
        "*"                — all events (use with extreme caution)

    The same handler can be subscribed to multiple topics. Subscribing
    the same (topic, handler) pair twice is a no-op (idempotent).

    Args:
        topic: Topic string or wildcard pattern.
        handler: Async callable (coroutine function) that accepts a
            single KernelEvent argument. Must not raise — exceptions
            in handlers are caught, logged, and suppressed.

    Raises:
        ValueError: If topic is empty or handler is not a coroutine function.
    """
```

---

### `unsubscribe(topic: str, handler: Callable[[KernelEvent], Coroutine]) -> None`

```python
def unsubscribe(
    self,
    topic: str,
    handler: Callable[[KernelEvent], Coroutine],
) -> None:
    """
    Remove a previously registered subscription.

    If the (topic, handler) pair is not found, this method is a no-op.

    Args:
        topic: The topic string or pattern used during subscribe().
        handler: The exact handler callable reference used during subscribe().
    """
```

---

### `request_resources(request: ResourceRequest) -> ResourceGrant`

```python
async def request_resources(self, request: ResourceRequest) -> ResourceGrant:
    """
    Request compute/memory resources from the kernel resource manager.

    The Kernel evaluates the request against:
        1. Available capacity (total - currently allocated).
        2. Per-requester quotas (max memory per agent, max CPU per plugin).
        3. Active policies (policy engine may further restrict allocation).
        4. Priority queue (higher priority requests preempt lower when
           resources are near capacity — subject to configured policy).

    If granted, the Kernel tracks the allocation and associates it with
    the requester_id. Resources are automatically reclaimed if:
        - The grant expires (expires_at passes without release).
        - The requester (plugin/agent/service) is shut down or fails.
        - release_resources() is called with the grant_id.

    Args:
        request: A ResourceRequest describing what is needed and by whom.

    Returns:
        A ResourceGrant. Always returns (never raises) — check grant.granted
        and grant.denied_reason to determine outcome.

    Note:
        Callers MUST call release_resources(grant.grant_id) when done.
        Leaked grants are reclaimed after grant.expires_at but represent
        wasted capacity in the interim.
    """
```

---

### `release_resources(grant_id: str) -> None`

```python
async def release_resources(self, grant_id: str) -> None:
    """
    Release previously granted resources back to the kernel.

    Should be called as soon as the requester no longer needs the
    allocated resources. Calling after grant expiry is a no-op.

    Args:
        grant_id: The grant ID returned in ResourceGrant.grant_id.

    Raises:
        ResourceGrantNotFoundError: grant_id was never issued or was
            already released. (Idempotent double-release does not raise —
            only completely unknown IDs raise.)
    """
```

---

### `enforce_policy(context: PolicyContext) -> PolicyResult`

```python
async def enforce_policy(self, context: PolicyContext) -> PolicyResult:
    """
    Evaluate whether an action is permitted by the active policy set.

    Called by the Execution Engine before every agent execution and
    by the Tool Runtime before every tool invocation. Also available
    to plugins for self-governance checks.

    Evaluation order:
        1. Hard-deny policies (blacklists, disabled tools) — fail fast.
        2. Rate limit policies — check per-actor call counts.
        3. Quota policies — check resource usage against limits.
        4. Custom registered policies — evaluated in registration order.
        5. Default-allow if no policy explicitly denied.

    The result includes the policy_id that made the decision (for auditing)
    and a human-readable reason (for debugging).

    Telemetry: Every call emits kernel.policy.evaluated with actor, action,
    resource, and the allow/deny decision.

    Args:
        context: PolicyContext describing actor, action, resource, metadata.

    Returns:
        PolicyResult with allowed=True or allowed=False.

    Note:
        This method never raises for policy evaluation failures — it returns
        allowed=False with a reason explaining the failure. This upholds
        Principle 5: fail-safe, not fail-open.
    """
```

---

### `startup() -> None`

```python
async def startup(self) -> None:
    """
    Execute the full kernel boot sequence (6 phases, 0–5).

    Phase 0: Validate configuration and environment.
    Phase 1: Initialize core services (logger, metrics, scheduler,
             resource manager, policy engine, service registry, plugin manager).
    Phase 2: Discover and load plugins in dependency order.
    Phase 3: Register all platform services.
    Phase 4: Verify all components healthy.
    Phase 5: Set state to RUNNING and accept traffic.

    This method is idempotent: calling startup() on an already-RUNNING
    kernel is a no-op.

    Raises:
        KernelBootError: Any phase failed. The error message includes
            the phase number and detailed failure description.
            After this error, kernel.state() returns FAILED.
    """
```

---

### `shutdown(graceful: bool = True) -> None`

```python
async def shutdown(self, graceful: bool = True) -> None:
    """
    Execute the kernel shutdown sequence.

    Graceful shutdown (graceful=True, default):
        Phase 1 — DRAINING: Stop accepting new tasks. In-flight tasks
            are allowed to complete up to drain_timeout_seconds (default 30s).
        Phase 2 — PLUGIN SHUTDOWN: Shutdown plugins in reverse dependency
            order. Each plugin gets plugin_shutdown_timeout_seconds (default 30s).
        Phase 3 — RESOURCE CLEANUP: Release all active ResourceGrants.
            Emit final resource usage report.
        Phase 4 — TELEMETRY FLUSH: Flush all buffered telemetry events.
            Close metrics connections. Set state to STOPPED.

    Forceful shutdown (graceful=False):
        Skips Phase 1 drain. Proceeds immediately to plugin shutdown
        with a reduced timeout (5s per plugin). Use only when graceful
        shutdown is not possible (e.g., SIGKILL received).

    This method is idempotent: calling shutdown() on an already-STOPPED
    kernel is a no-op.

    Args:
        graceful: If True (default), drain in-flight tasks before stopping.
            If False, stop immediately.
    """
```

---

### `state() -> LifecycleState`

```python
def state(self) -> LifecycleState:
    """
    Return the current kernel lifecycle state.

    This method is synchronous, non-blocking, and always succeeds.
    It is safe to call from any context, including signal handlers.

    Returns:
        The current LifecycleState enum value.
    """
```

---

### `health() -> KernelHealth`

```python
def health() -> KernelHealth:
    """
    Return a snapshot of kernel health including all component statuses.

    This method returns the most recently computed health snapshot.
    Health is polled in the background at health_check_interval_seconds
    (default: 30s). This method does NOT trigger a new health check
    — it returns the cached snapshot.

    To force a live health check (e.g., for a /health endpoint), call
    await kernel.check_health_now() instead.

    Returns:
        KernelHealth dataclass with:
            - state: current LifecycleState
            - plugins_loaded: count of loaded plugins
            - services_registered: count of registered services
            - uptime_seconds: seconds since kernel entered RUNNING state
            - failed_components: list of component IDs in FAILED state
            - healthy: True if all required components are healthy
    """
```

---

## 7. Plugin Contract

Every plugin that runs on AEOS must implement the `BasePlugin` abstract class and provide a `PluginManifest`. This section specifies the complete contract.

### 7.1 Plugin Manifest

The manifest is the plugin's identity and declaration of intent. It is parsed by the Kernel before the plugin code is loaded.

```yaml
# plugins/my_plugin/plugin.yaml
id: "my_plugin"                          # Unique across all plugins. Snake_case.
name: "My Plugin"                        # Human-readable name.
version: "1.2.3"                         # SemVer. Must increment on any change.
description: "One sentence description." # Used in registry listings.
author: "Team Name <team@example.com>"
min_aeos_version: "2.0.0"               # Minimum AEOS version this plugin requires.
dependencies:                            # Other plugin IDs this plugin requires.
  - "rag_plugin"                         # These must be loaded before this plugin.
capabilities:                            # Dot-namespaced capability strings.
  - "my_domain.do_thing"
  - "my_domain.check_thing"
config_schema:                           # JSON Schema for plugin configuration.
  type: object
  required: ["api_key"]
  properties:
    api_key:
      type: string
      description: "API key for the external service."
    timeout_seconds:
      type: integer
      default: 30
```

**Required fields:** `id`, `name`, `version`, `min_aeos_version`  
**Optional fields:** `description`, `author`, `dependencies`, `capabilities`, `config_schema`

### 7.2 BasePlugin Lifecycle Methods

```python
class BasePlugin(ABC):

    @property
    @abstractmethod
    def manifest(self) -> PluginManifest:
        """Return the plugin's manifest. Must be available before initialize()."""

    @abstractmethod
    async def initialize(self, kernel: AEOSKernel) -> None:
        """
        Initialize the plugin.

        Called by the Kernel after manifest validation and dependency
        resolution. The plugin receives a reference to the Kernel and
        should use it to register services, subscribe to events, and
        request initial resource grants.

        Contract:
            - Must complete within plugin_init_timeout_seconds.
            - Must not call kernel.startup() or kernel.shutdown().
            - Must call kernel.register_service() for each service provided.
            - Must call kernel.subscribe() for each event topic needed.
            - Must transition internal state to READY on success.
            - Must raise PluginInitializationError on failure (not return None).
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Shutdown the plugin gracefully.

        Called by the Kernel during plugin unload or kernel shutdown.
        The plugin should release external connections, flush buffers,
        and release any ResourceGrants it holds.

        Contract:
            - Must complete within plugin_shutdown_timeout_seconds.
            - Must not raise exceptions — log and suppress errors.
            - Must not call any Kernel API methods after shutdown() is called.
        """

    @abstractmethod
    def health(self) -> PluginStatus:
        """
        Return the current health status of this plugin.

        Called by the Kernel during Phase 4 boot and during periodic
        background health polling.

        Returns:
            PluginStatus.READY if the plugin is healthy.
            PluginStatus.DEGRADED if the plugin is partially functional.
            PluginStatus.FAILED if the plugin cannot serve requests.

        Contract:
            - Must be synchronous and non-blocking.
            - Must complete within health_check_timeout_seconds (default 10s).
            - Must never raise — return FAILED status on internal error.
        """

    @abstractmethod
    async def on_kernel_event(self, event: KernelEvent) -> None:
        """
        Handle a kernel event delivered to this plugin.

        This method is called for events on topics the plugin subscribed to
        via kernel.subscribe(). Failures in this method are caught, logged,
        and do not propagate.

        Args:
            event: The KernelEvent delivered to this plugin.
        """
```

---

## 8. Service Contract

A service is any platform capability registered with the Kernel Service Registry. Services are registered by plugins (or directly by the Kernel during Phase 1) and discovered by components at runtime.

### 8.1 Required Interface

All services must implement the `ServiceProtocol`:

```python
class ServiceProtocol(Protocol):

    @property
    def service_id(self) -> str:
        """Unique service identifier. Must match the ID used in register_service()."""

    def health(self) -> ServiceHealth:
        """
        Return service health.

        Must be synchronous and non-blocking.
        Must complete in < 1 second.
        Must never raise.
        """

    def capabilities(self) -> list[str]:
        """
        Return the list of capabilities this service provides.

        Must be idempotent and return the same list as declared during
        register_service(). Capability lists must not change at runtime.
        """
```

### 8.2 Service Registration Conventions

| Convention | Rule |
|------------|------|
| `service_id` format | `lowercase_snake_case`, noun phrase (e.g., `rag_engine`, `memory_store`) |
| Capability format | `domain.action` dot-namespaced (e.g., `rag.query`, `ml.predict`) |
| Registration timing | During plugin `initialize()` or Kernel Phase 3 |
| Deregistration | Automatic on plugin unload; manual via `kernel.deregister_service()` |
| Duplicate IDs | `ServiceConflictError` raised; no silent overwriting |

---

## 9. Event Model

The Kernel Event Bus is the primary inter-component communication channel for non-request-response interactions (telemetry, notifications, state changes). All events use the `KernelEvent` dataclass.

### 9.1 Event Schema

```python
@dataclass
class KernelEvent:
    topic: str      # Dot-namespaced topic. Convention: "domain.entity.action"
    source: str     # Component that emitted the event (plugin_id or "kernel")
    payload: dict   # Structured event data. Schema varies by topic.
    timestamp: str  # ISO 8601 UTC timestamp (auto-set if empty)
    trace_id: str   # Distributed trace ID (propagated from HTTP request, if any)
```

### 9.2 Built-in Kernel Event Topics

| Topic | Emitted By | Payload Keys | Description |
|-------|------------|--------------|-------------|
| `kernel.boot.started` | Kernel | `phase`, `timestamp` | Kernel boot sequence started |
| `kernel.boot.phase_complete` | Kernel | `phase`, `duration_ms` | A boot phase completed |
| `kernel.boot.complete` | Kernel | `duration_ms`, `plugins_loaded`, `services_registered` | Kernel fully booted |
| `kernel.boot.failed` | Kernel | `phase`, `reason`, `error` | Boot failed in a specific phase |
| `kernel.plugin.manifest.discovered` | Plugin Manager | `plugin_id`, `path` | Plugin manifest found during scan |
| `kernel.plugin.loaded` | Plugin Manager | `plugin_id`, `version`, `capabilities` | Plugin successfully loaded |
| `kernel.plugin.load_failed` | Plugin Manager | `plugin_id`, `reason`, `error` | Plugin failed to load |
| `kernel.plugin.unloaded` | Plugin Manager | `plugin_id` | Plugin successfully unloaded |
| `kernel.plugin.health_changed` | Plugin Manager | `plugin_id`, `from_status`, `to_status` | Plugin health status changed |
| `kernel.service.registered` | Service Registry | `service_id`, `capabilities` | Service added to registry |
| `kernel.service.deregistered` | Service Registry | `service_id` | Service removed from registry |
| `kernel.service.health_failed` | Service Registry | `service_id`, `reason` | Service health check failed |
| `kernel.resource.granted` | Resource Manager | `grant_id`, `requester_id`, `memory_bytes`, `cpu_millicores` | Resource grant issued |
| `kernel.resource.denied` | Resource Manager | `requester_id`, `reason`, `requested_memory`, `requested_cpu` | Resource request denied |
| `kernel.resource.released` | Resource Manager | `grant_id`, `requester_id` | Resources returned to pool |
| `kernel.resource.expired` | Resource Manager | `grant_id`, `requester_id` | Grant expired before release |
| `kernel.policy.evaluated` | Policy Engine | `actor_id`, `action`, `resource`, `allowed`, `policy_id` | Policy decision made |
| `kernel.lifecycle.transition` | Lifecycle Manager | `component_id`, `from_state`, `to_state`, `reason` | Component lifecycle state changed |
| `kernel.shutdown.initiated` | Kernel | `graceful`, `reason` | Shutdown sequence started |
| `kernel.shutdown.complete` | Kernel | `duration_ms`, `drained_tasks` | Shutdown complete |

### 9.3 Topic Naming Convention

```
{domain}.{entity}.{action_past_tense}

Examples:
  kernel.plugin.loaded      ✅
  plugin_loaded             ❌ (no domain namespace)
  kernel.plugin.load        ❌ (action not past tense — use past tense for events)
  KERNEL.PLUGIN.LOADED      ❌ (uppercase)
```

---

## 10. Resource Management

The Kernel Resource Manager provides a lightweight resource accounting layer that prevents any single agent, plugin, or service from monopolizing platform resources.

### 10.1 Resource Types Tracked

| Resource | Unit | Default Capacity | Per-Requester Default Quota |
|----------|------|-----------------|----------------------------|
| Memory | bytes | System RAM × 0.8 | 2 GB |
| CPU | millicores | System cores × 1000 × 0.8 | 2000m (2 cores) |
| GPU | count | Detected GPUs | 1 |
| LLM API tokens | tokens/minute | Configured per provider | 50,000 |
| External API calls | calls/minute | Configured per service | 100 |
| Concurrent tasks | count | Configurable (default: 50) | 5 |

### 10.2 Grant Lifecycle

```
request_resources()
        │
        ▼
  ┌─── Capacity check ───┐
  │                      │
  │  Available?          │  Not available?
  │       ▼              │       ▼
  │  Policy check        │  Check priority queue
  │       ▼              │       ▼
  │  Allowed?            │  Preempt lower priority?
  │       ▼              │       ▼
  │  Allocate            │  Deny with reason
  │  Grant ID issued     │  Return ResourceGrant(granted=False)
  └──────────────────────┘
        │
        ▼
  Grant tracked in memory
  expires_at set
        │
        ▼
  Component uses resources
        │
        ├── release_resources(grant_id) ──► Resources returned, grant closed
        │
        └── expires_at passes without release ──► Auto-reclaim, emit kernel.resource.expired
```

### 10.3 Priority and Preemption

Resource requests include a priority (1–10, default 5). When capacity is insufficient:

- Priority 8–10 (system): Reserved for Kernel internal operations. Never preempted.
- Priority 6–7 (high): Agent executions with SLA requirements.
- Priority 4–5 (normal): Standard agent and tool executions.
- Priority 1–3 (low): Background processing, batch tasks.

A high-priority request may preempt a low-priority active grant if configured (`allow_preemption: true`). Preempted components receive a `kernel.resource.preempted` event and have `preemption_drain_seconds` (default: 5s) to finish.

### 10.4 Resource Quotas

Quotas are enforced per `requester_id`. Quotas are configured in `aeos.yaml`:

```yaml
resource_manager:
  quotas:
    default:
      memory_bytes: 2147483648     # 2 GB
      cpu_millicores: 2000
      concurrent_tasks: 5
      llm_tokens_per_minute: 50000
    agents:
      "high_priority_agent":
        memory_bytes: 8589934592   # 8 GB
        llm_tokens_per_minute: 200000
    plugins:
      "ml_plugin":
        gpu_count: 2
```

---

## 11. Policy Enforcement Points

A Policy Enforcement Point (PEP) is a location in the execution flow where the Kernel's Policy Engine is called before an operation proceeds. All PEPs are mandatory — they cannot be bypassed.

### 11.1 PEP Locations

```
HTTP Request arrives
        │
        ├── PEP-1: Request authorization (actor authenticated? rate limited?)
        │
        ▼
Kernel receives task execution request
        │
        ├── PEP-2: Task authorization (actor allowed to execute this task type?)
        │
        ▼
Execution Engine selects agent
        │
        ├── PEP-3: Agent selection (actor allowed to use this agent?)
        │
        ▼
Agent calls Tool
        │
        ├── PEP-4: Tool invocation (actor allowed to call this tool?)
        ├── PEP-5: Tool parameter validation (parameters within allowed ranges?)
        │
        ▼
Tool calls External API
        │
        ├── PEP-6: External API authorization (service allowed for this actor?)
        │
        ▼
Agent requests Resource Grant
        │
        ├── PEP-7: Resource allocation (within quota? within capacity?)
        │
        ▼
Plugin loads
        │
        └── PEP-8: Plugin authorization (operator allowed to load this plugin?)
```

### 11.2 Built-in Policies

| Policy ID | Type | Default | Description |
|-----------|------|---------|-------------|
| `rate_limit.api` | Rate Limit | 100 req/min per actor | HTTP API rate limit |
| `rate_limit.agent_execution` | Rate Limit | 20 exec/min per actor | Agent execution rate limit |
| `tool.allowlist` | Access Control | All tools allowed | Allowlist of permitted tools |
| `tool.denylist` | Access Control | Empty | Hard-deny list for tools |
| `resource.quota` | Quota | See §10.4 | Per-actor resource quotas |
| `plugin.trusted_sources` | Access Control | Official plugins only | Allowed plugin source origins |
| `data.classification` | Governance | Warn on PII | Data classification enforcement |

---

## 12. Shutdown Sequence

The Kernel shutdown sequence is designed to minimize data loss and ensure all in-flight operations complete cleanly.

### Phase 1: Draining (graceful=True only)

**Timeout:** `drain_timeout_seconds` (default: 30s)

1. Set Kernel lifecycle state: `RUNNING → DRAINING`.
2. Emit `kernel.shutdown.initiated` with `graceful=True`.
3. Stop the FastAPI server from accepting new connections.
4. Stop the Scheduler from accepting new task submissions.
5. Poll active task count every 500ms. Wait until count reaches 0 or timeout expires.
6. If timeout expires with tasks still running: log each still-running task ID. Proceed to Phase 2 (force those tasks to terminate).

### Phase 2: Plugin Shutdown (reverse dependency order)

**Timeout per plugin:** `plugin_shutdown_timeout_seconds` (default: 30s)

1. Set Kernel lifecycle state: `DRAINING → STOPPING`.
2. For each plugin in reverse topological order (dependents first):
   a. Set plugin lifecycle state to `STOPPING`.
   b. Call `plugin.shutdown()`.
   c. Wait up to `plugin_shutdown_timeout_seconds`.
   d. If timeout: log warning, force-mark plugin as `STOPPED`.
   e. Emit `kernel.plugin.unloaded` for each plugin.
3. After all plugins stopped: deregister all services from the Service Registry.

### Phase 3: Resource Cleanup

**Timeout:** 5 seconds

1. Enumerate all active `ResourceGrant` objects.
2. For each active grant: emit `kernel.resource.released` with source `kernel.shutdown`.
3. Clear the Resource Manager state.
4. Emit `kernel.resource.pool.cleared`.

### Phase 4: Telemetry Flush and Final State

**Timeout:** 10 seconds

1. Drain the event bus queue. Wait up to 10 seconds for all pending events to be delivered to subscribers.
2. Flush metrics to the metrics backend.
3. Flush structured logs.
4. Set Kernel lifecycle state: `STOPPING → STOPPED`.
5. Log: `"AEOS Kernel shutdown complete. Uptime: {uptime_seconds}s"`.

### Forceful Shutdown (graceful=False)

Skip Phase 1. Execute Phases 2–4 with reduced timeouts (5s per plugin, 2s resource cleanup, 2s telemetry flush). Used when SIGKILL is received or a health watchdog determines the Kernel is unresponsive.

---

## 13. Failure Handling

### 13.1 Plugin Failure

**Detection:** Plugin's `health()` returns `PluginStatus.FAILED`, or plugin raises an unhandled exception during a kernel event handler.

**Kernel response:**
1. Set plugin lifecycle state to `FAILED`.
2. Emit `kernel.plugin.health_changed` with `to_status=FAILED`.
3. Revoke all `ResourceGrant` objects held by the failing plugin.
4. Deregister all services the plugin registered (those services become unavailable).
5. Evaluate: Is this a required plugin? (Check `required_plugins` config.)
   - **Required:** Transition Kernel to `FAILED`. Begin graceful shutdown.
   - **Optional:** Log the failure. Mark plugin as `FAILED`. Continue running.
6. If configured, attempt plugin restart: call `plugin.shutdown()` then `plugin.initialize()`. Max `plugin_restart_attempts` (default: 3).

### 13.2 Service Failure

**Detection:** Service's `health()` returns `ServiceHealth.UNHEALTHY` during background polling.

**Kernel response:**
1. Emit `kernel.service.health_failed` with service_id and reason.
2. Mark service as `DEGRADED` in the registry. It remains discoverable but callers receive a `ServiceDegradedWarning`.
3. If the service remains unhealthy for `service_failure_timeout_seconds` (default: 60s), deregister it.
4. Evaluate capability impact: which capabilities are now unsatisfied?
5. Emit `kernel.capability.unavailable` for each capability now without a healthy provider.

### 13.3 Resource Exhaustion

**Detection:** Resource Manager capacity reaches `resource_high_watermark` (default: 90%).

**Kernel response:**
1. Emit `kernel.resource.high_watermark` with usage statistics.
2. New resource requests below priority threshold (configurable, default: 5) are denied with `denied_reason="capacity_high_watermark"`.
3. If capacity reaches 100%, all new requests denied regardless of priority.
4. If configured, emit alert to external monitoring system.

### 13.4 Event Bus Saturation

**Detection:** Internal event queue depth exceeds `event_queue_max_depth` (default: 10,000).

**Kernel response:**
1. Log a warning at every 1,000-event depth increment.
2. At queue depth = `event_queue_max_depth`: new events are dropped. Increment `aeos_kernel_events_dropped_total` metric.
3. Emit (synchronously, bypassing the queue) `kernel.event_bus.saturated`.

### 13.5 Kernel Internal Error

Any unhandled exception in Kernel internals (not in a plugin or service):
1. Log the full traceback with `CRITICAL` severity.
2. Emit `kernel.internal.error` synchronously.
3. Transition Kernel to `FAILED`.
4. Begin forceful shutdown (`graceful=False`).
5. Exit with non-zero exit code so the process supervisor (systemd, Kubernetes) can restart.

---

## 14. Future Extensions

### 14.1 Distributed Kernel (v3 Target)

The `LocalKernel` implementation is designed for single-process deployment. A `DistributedKernel` will implement the same `AEOSKernel` interface but distribute sub-systems across nodes:

- **Plugin Manager:** Plugins can run in separate worker processes or containers.
- **Service Registry:** Backed by a distributed store (etcd, Redis) for multi-node discovery.
- **Event Bus:** Replaced by a distributed message broker (NATS, Kafka).
- **Resource Manager:** Cluster-level resource tracking with node affinity.

Because all components interact with `AEOSKernel` through the abstract interface, migrating to a distributed kernel requires no changes to plugins or services — only the concrete kernel implementation changes.

### 14.2 Kernel Federation

Multiple AEOS instances (each with its own Kernel) will be able to federate: sharing service registries and forwarding events across instance boundaries. This enables multi-tenant AEOS deployments where each tenant's workload runs in an isolated Kernel with shared infrastructure services.

### 14.3 Cross-Node Resource Management

The Resource Manager will gain awareness of physical topology (NUMA nodes, GPU interconnects, network bandwidth between nodes) to make placement decisions that minimize resource contention across nodes — analogous to Kubernetes' scheduler topology constraints.

### 14.4 Plugin Hot-Reloading

Plugins will support hot-reload without kernel restart: a new version of a plugin can be loaded alongside the old version, traffic gradually shifted, and the old version unloaded — a blue/green deployment model for platform capabilities.

### 14.5 Policy-as-Code

Policies will be declarable in a structured DSL (YAML-based, similar to OPA Rego but simpler) and loaded from the filesystem without code changes. Policy updates will take effect without kernel restart.

### 14.6 Kernel Introspection API

A structured introspection API (separate from `/health`) will expose:
- Full plugin dependency graph (JSON)
- Service capability index
- Active resource grants with requester IDs
- Policy evaluation audit log (last N decisions)
- Lifecycle transition history for all components

---

## 15. Glossary

| Term | Definition |
|------|------------|
| **Kernel** | The AEOS central runtime coordinator. The single mandatory intermediary for all platform operations. |
| **Plugin** | A loaded extension to the AEOS platform. Provides domain-specific capabilities. Must implement `BasePlugin`. |
| **Service** | A platform capability registered with the Kernel Service Registry. Discovered by `service_id`. |
| **Capability** | A dot-namespaced string (e.g., `rag.query`) declaring what a service can do. Used for service discovery by function. |
| **KernelEvent** | A structured notification emitted through the Kernel Event Bus. All telemetry is modeled as events. |
| **ResourceGrant** | An authorization from the Resource Manager to use a specific amount of compute/memory. |
| **ResourceRequest** | A request submitted by a component to the Resource Manager for a ResourceGrant. |
| **PolicyContext** | The context passed to the Policy Engine at a Policy Enforcement Point. |
| **PolicyResult** | The outcome of a Policy Engine evaluation: allowed or denied with reason. |
| **PEP** | Policy Enforcement Point. A location in the execution flow where policy is checked before proceeding. |
| **LifecycleState** | An enum value representing where in the component lifecycle a component currently is. |
| **Manifest** | A `plugin.yaml` file declaring a plugin's identity, dependencies, capabilities, and configuration schema. |
| **Boot Sequence** | The six-phase initialization process the Kernel runs before accepting traffic. |
| **Drain** | The period during graceful shutdown when the Kernel stops accepting new work but allows in-flight work to complete. |
| **Topological Order** | The plugin load order derived from the dependency DAG, ensuring every plugin's dependencies are loaded before it. |
| **Health Snapshot** | A `KernelHealth` dataclass capturing the state of all components at a point in time. |

---

## Cross-References

- **[001-ARCHITECTURE.md](001-ARCHITECTURE.md)** — Platform-level architecture overview. The Kernel is introduced as the central coordinator in §3.2.
- **[006-EXECUTION_ENGINE.md](006-EXECUTION_ENGINE.md)** — The Execution Engine runs inside the Kernel's resource and policy envelope. See §4 for how the Engine calls `kernel.enforce_policy()` and `kernel.request_resources()`.
- **[015-PLUGIN_ARCHITECTURE.md](015-PLUGIN_ARCHITECTURE.md)** *(planned)* — Deep dive into the plugin manifest schema, dependency resolution algorithm, and plugin development guide for external contributors.

---

*End of document — AEOS Kernel Architecture v1.0.0*
