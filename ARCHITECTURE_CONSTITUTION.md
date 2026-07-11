# AEOS Architecture Constitution

> **Version:** 1.0.0  
> **Status:** Ratified  
> **Date:** 2026-07-05  
> **Authority:** This document supersedes all prior design decisions. All architecture documents, ADRs, code, and configuration in the AEOS repository must conform to the laws stated herein.

---

## Preamble

The AEOS Architecture Constitution is the highest-governing engineering document in the AEOS repository. It exists because large, complex systems built by multiple contributors over time inevitably accumulate contradictory decisions, local optimizations that degrade the whole, and implicit assumptions that become load-bearing without anyone noticing. The Constitution prevents this.

This document was created because AEOS is not a prototype or a script collection — it is a **production operating system for AI workloads**. Operating systems must be internally consistent. Kernels cannot have exceptions. Policy enforcement cannot be optional. The gap between "mostly correct" and "correct" in a platform that routes autonomous agent execution is the gap between a reliable system and a catastrophic one.

Every engineer working on AEOS, whether adding a new agent, extending the plugin system, or modifying the ML pipeline, is required to read this document before writing code. Every architecture decision record (ADR) must cite which Constitutional Laws it upholds or, in rare cases, which it is amending (through the Amendment Process described below). Every code review must include the question: *Does this change violate any Constitutional Law?*

The Constitution does not specify implementation. It specifies **invariants** — properties of the system that must remain true regardless of how the implementation evolves. Specific architecture decisions live in `docs/architecture/`. Implementation decisions live in code and ADRs. The Constitution lives here.

---

## The 10 Architectural Laws of AEOS

### Law 1: Everything Passes Through the Kernel

**Statement:** No request, task, event, or state mutation may bypass the AEOS Kernel. The Kernel is the single entry point for all work dispatched to the AEOS runtime.

**Rationale:** A kernel that can be bypassed is not a kernel — it is a suggestion. The value of centralizing coordination in the Kernel is that it enables uniform policy enforcement (security, cost governance, rate limiting, observability), consistent lifecycle management, and a single source of truth for what the system is doing at any moment. If agents could route work around the Kernel, observability would become incomplete, governance policies would have holes, and debugging would require understanding arbitrary call chains. The Kernel is not a performance bottleneck — it is a coordination surface. It must be treated as a non-negotiable architectural boundary.

**Implications:**
- All agent invocations are dispatched by the Kernel, never called directly by external callers.
- All workflow executions are registered with and governed by the Kernel.
- All tool invocations flow through the Tool Runtime, which is managed by the Kernel.
- HTTP routes hand off requests to the Kernel; they do not execute business logic themselves.

---

### Law 2: Agents Never Call Each Other Directly

**Statement:** Agent-to-agent communication is mediated exclusively through the AEOS Message Bus or Kernel dispatch. An agent may not hold a reference to another agent instance and may not invoke another agent's methods.

**Rationale:** Direct agent-to-agent calls create implicit coupling, make call graphs invisible to the observability system, prevent the Kernel from enforcing resource governance, and make the system impossible to reason about at scale. They also break isolation: if Agent A can call Agent B directly, Agent A's failure mode can propagate uncontrolled into Agent B. The Message Bus enforces explicit contracts, typed messages, and observable communication channels. This is the same reason operating systems use IPC rather than allowing processes to write into each other's memory spaces.

**Implications:**
- Agents publish events or results to the Message Bus or return structured outputs to the Kernel.
- Agents consume tasks from queues, not from direct invocation by peers.
- Multi-agent workflows are expressed as Workflow DAGs executed by the Workflow Runtime, not as procedural agent-calling code.
- Agent discovery is managed through the Service Registry, not through direct instantiation.

---

### Law 3: All State Is Explicit and Typed

**Statement:** All state in AEOS — workflow state, agent state, task state, session state, configuration state — must be represented by explicitly defined, typed data structures. Implicit state (module-level globals, thread-local storage used across abstraction boundaries, untyped dicts as primary state containers) is prohibited.

**Rationale:** Non-deterministic AI workloads are already difficult to debug. Implicit state makes them impossible. When a workflow fails, the only path to root cause is inspecting the state at each step. If state is implicit or untyped, this inspection is ambiguous. Typed state enables serialization, persistence, versioning, and replay. It enables schema validation at boundaries. It enables the Governance Layer to audit what happened. Pydantic models, TypedDicts, or dataclasses are the acceptable representations; raw dicts are acceptable only as intermediate parsing structures before being converted to typed forms.

**Implications:**
- All Pydantic models must have explicit field types and validators.
- Workflow state machines must define their state schema.
- Agent context and memory structures must be typed.
- API request and response schemas must be fully specified.
- Configuration objects must be typed (Pydantic Settings or equivalent).

---

### Law 4: Every Component Has a Single Owner

**Statement:** Every subsystem, module, service, and data store in AEOS has exactly one owning component. No piece of state or logic is jointly owned. Cross-component coordination is through defined interfaces, not shared internal state.

**Rationale:** Shared ownership is a polite name for no ownership. When two components both "own" a data structure or behavior, neither has full authority to change it, and both feel entitled to modify it arbitrarily. This creates merge conflicts in reasoning, not just in code. Single ownership enables clear contracts: if you need data from a component, you go through its interface. This makes refactoring safe, makes testing precise, and makes responsibility clear in postmortems.

**Implications:**
- The Memory System owns agent memory. Agents request memory reads/writes through the Memory API.
- The Knowledge Runtime owns RAG and graph storage. Components request knowledge through the Knowledge API.
- The ML Platform owns model serving. Agents invoke models through the ML Platform API.
- The Kernel owns scheduling and dispatch. No component self-schedules.
- If two components need to share data, one must own it and expose an API.

---

### Law 5: Failure Is a First-Class Citizen

**Statement:** Every component must define its failure modes explicitly. Every operation that can fail must return a typed error or raise a typed exception. Retry logic, circuit breakers, fallback behavior, and failure propagation must be explicitly specified and implemented, not left to chance.

**Rationale:** Production systems fail. Networks partition. LLM APIs return 429s. Vector stores go down. The question is not whether failure will occur but whether AEOS handles it gracefully or cascades into uncontrolled degradation. Building failure handling as an afterthought means it will be incomplete and inconsistent. Defining failure modes first means the system is resilient by construction. This mirrors how the Linux kernel handles system call errors — every call has defined error codes, and callers must handle them.

**Implications:**
- All async operations use typed Result types or structured exception hierarchies.
- Every external API call has retry configuration, timeout, and circuit breaker.
- Agent failures are caught by the Agent Runtime, logged with full context, and reported to the Kernel.
- The Kernel maintains a failure registry and can make governance decisions based on failure history.
- Workflow steps have explicit compensation/rollback definitions.
- No bare `except Exception: pass` blocks exist in production code paths.

---

### Law 6: Every Operation Is Observable

**Statement:** Every request, task dispatch, agent invocation, tool call, LLM call, database query, and state transition must emit structured telemetry: at minimum, a trace span, a structured log entry, and the relevant metrics. No code path may be invisible to the Observability Layer.

**Rationale:** AI systems are non-deterministic. The same input can produce different outputs. Debugging failures requires reconstructing exactly what happened, in what order, with what parameters and latencies. Without comprehensive observability, AEOS would be a black box that cannot be operated in production. This is not about compliance — it is about operability. If you cannot measure it, you cannot fix it. Observability is also a prerequisite for the Governance Layer: you cannot enforce cost policies without tracking costs, you cannot enforce rate limits without counting requests, you cannot audit security without logging access.

**Implications:**
- All operations use distributed tracing with parent-child span relationships.
- All LLM calls emit token counts, model ID, latency, and cost estimate.
- All agent invocations emit a structured log with task_id, agent_id, input hash, and result status.
- Metrics are exported in Prometheus format for all runtime counters and histograms.
- Health check endpoints expose runtime state, not just liveness.
- Trace IDs propagate across async boundaries and between components.

---

### Law 7: Security Is Enforced at Every Layer

**Statement:** Security controls are not a perimeter concern. Every component enforces security at its own boundary. Authentication, authorization, input validation, output sanitization, and secret management are enforced at every layer independently.

**Rationale:** Defense in depth is the only security model that works for complex systems. Perimeter security alone fails when any inner layer has a vulnerability. In AEOS, an agent that can access arbitrary tools without authorization, or a plugin that can read secrets from the environment, or a workflow that can be triggered without authentication — any one of these represents a complete security failure. Each layer must enforce its own security invariants without assuming that outer layers have already done so.

**Implications:**
- API endpoints validate authentication tokens independently, even behind an API gateway.
- Tool invocations validate that the invoking agent has permission to use that tool.
- Plugin loading validates plugin signatures and sandbox permissions before execution.
- LLM prompts are sanitized for prompt injection at the Tool Runtime layer.
- Secrets are loaded from a secrets manager, never from environment variables in production or from code.
- Agent outputs that will be returned to users are sanitized for data exfiltration vectors.

---

### Law 8: Configuration Never Lives in Code

**Statement:** All tunable parameters — model names, endpoint URLs, timeouts, retry counts, feature flags, cost limits, agent personas, tool permissions — must live in configuration files or environment variables, never hardcoded in source files. The only exception is configuration schema definitions (the Pydantic Settings classes that define what configuration exists).

**Rationale:** Configuration that lives in code requires a code change, review cycle, and deployment to adjust. In a production AI platform, operators must be able to change model endpoints, adjust rate limits, enable or disable features, and tune timeouts without touching source code. Code changes also introduce risk: a misconfigured timeout hardcoded in a function is harder to audit and change safely than the same value in a config file. Configuration-as-code (the schema) is correct; configuration values in code are a violation.

**Implications:**
- All `app/core/config.py` settings use Pydantic BaseSettings with environment variable sources.
- No model names, API endpoints, or cost limits appear as string literals in business logic.
- Feature flags are loaded from configuration, enabling runtime-variable behavior.
- Agent configuration (persona, tools, memory) is loaded from YAML or database, not hardcoded.
- All default values are explicitly documented and justifiable.

---

### Law 9: All External I/O Is Behind an Abstraction

**Statement:** All interactions with external systems — LLM APIs, vector databases, relational databases, object storage, message queues, third-party REST APIs — must pass through an adapter interface that abstracts the specific implementation. Business logic may only depend on the interface, never on the concrete client library.

**Rationale:** External systems change. APIs deprecate. Libraries release breaking versions. Vendor lock-in limits optionality. More critically for testing: code that calls external systems directly cannot be unit tested reliably. Abstracting external I/O behind interfaces means implementations can be swapped (production LiteLLM → a different provider), mocked in tests (real database → in-memory fake), and instrumented consistently (all database calls go through one adapter that adds tracing). This mirrors the POSIX I/O abstraction: your code doesn't care whether the filesystem is ext4 or NFS; it calls POSIX functions and the kernel handles the rest.

**Implications:**
- LLM calls go through `app/core/llm/` adapter, never directly through `openai` or `litellm` clients in business logic.
- Database queries go through repository classes, never through SQLAlchemy session calls in route handlers.
- Vector store operations go through the Knowledge Runtime interface, never directly through ChromaDB or Pinecone clients.
- All adapter interfaces have corresponding test fakes/mocks.
- When a new external system is added, a new adapter interface is defined first.

---

### Law 10: The Spec Governs the Code, Never the Reverse

**Statement:** Architecture specifications, particularly this Constitution and the documents in `docs/architecture/`, define the intended system. Code is an implementation of the spec. If code deviates from the spec, the code is wrong. If the spec needs updating, an ADR must be filed and the spec updated before the code change is merged.

**Rationale:** In most software projects, documentation drifts from code until it becomes meaningless. AEOS inverts this: the specification is authoritative. This is how Kubernetes, the Linux kernel, and other production systems are governed — the API contract and architecture documentation define the system, and implementations are validated against them. This ensures that the architecture is always intentional, that decisions are recorded, and that future contributors understand not just what the code does but why it was designed that way.

**Implications:**
- Every significant architecture change requires a filed ADR (Architecture Decision Record) in `docs/adr/`.
- ADRs reference Constitutional Laws they uphold or amend.
- Code reviewers are responsible for checking spec conformance, not just code quality.
- When code and spec disagree and spec is correct, code must change. No exceptions.
- Documentation is a first-class deliverable, not an afterthought.

---

## What the Constitution Governs

The following categories of decisions require explicit Constitutional alignment before implementation:

| Decision Category | Why It's Constitutional | Required Process |
|---|---|---|
| New top-level runtime component (new "layer") | Changes the fundamental architecture of how components interact | ADR + Constitution cross-reference |
| New communication pattern between components | May violate Law 1 or Law 2 | ADR + explicit law citation |
| New external dependency (database, API, service) | Must comply with Law 9 (abstraction) | ADR + adapter interface design |
| New security enforcement point | Must not weaken Law 7 | Security review + ADR |
| New configuration system or parameter source | Must comply with Law 8 | ADR |
| Changes to the Kernel interface | Affects compliance with Laws 1 and 4 | Architecture review + ADR |
| New state representation for cross-component data | Must comply with Law 3 | Schema design review + ADR |
| Removal of an observability instrument | May violate Law 6 | ADR with justification |
| Changes to the plugin/tool security model | Affects Law 7 | Security review + ADR |
| Any change to this Constitution | Highest-impact change possible | Constitutional Amendment Process |

The following decisions do **not** require Constitutional review (but must comply with existing laws):

- Adding a new agent type within the existing Agent Runtime
- Adding a new tool within the existing Tool Registry
- Adding new endpoints to an existing API surface
- Refactoring internals of a component without changing its interface
- Adding new configuration parameters within the existing config system
- Bug fixes within a single component's boundary

---

## Amendment Process

The Constitution may be amended when a law is found to be incorrect, incomplete, or incompatible with a justified architectural evolution. Amendments are rare and require high justification.

### Step 1: Problem Statement (ADR Draft)

File an ADR in `docs/adr/` titled `ADR-NNNN-amend-law-N-<description>.md`. The ADR must include:
- Which Constitutional Law is proposed for amendment
- The specific wording change proposed
- The technical motivation (what does the current law prevent that should be allowed?)
- The risk analysis (what invariants does the current law enforce, and how will those invariants be preserved after the amendment?)
- At least two alternative approaches that were considered

### Step 2: Extended Review Period

Constitutional amendments are open for review for a minimum of **10 business days**. All engineering contributors are notified. Any engineer may raise a blocking objection. Blocking objections must be resolved before the amendment proceeds.

### Step 3: Architecture Council Approval

The Architecture Council (currently: the technical leads of the Kernel, Runtime, and Platform teams) must explicitly approve the amendment by documented sign-off in the ADR.

### Step 4: Constitution Update

Once approved, the Constitution is updated in the same PR as the ADR. The ADR is linked from the amended law's rationale section. The version number of the Constitution is incremented.

### Step 5: Propagation

Within 30 days of ratification, all architecture documents and ADRs that referenced the amended law must be updated to reflect the new wording.

---

## Canonical Runtime Stack

The following ASCII diagram represents the canonical layered architecture of AEOS. Every HTTP request or external event enters at the top and resolves at the bottom. All data flows through this stack. No layer skips another.

```
╔══════════════════════════════════════════════════════════════════════════╗
║                        AEOS CANONICAL RUNTIME STACK                      ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 1  │  TRANSPORT LAYER                                              ║
║           │  HTTP/1.1, HTTP/2, WebSocket, gRPC                           ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 2  │  API GATEWAY LAYER                                            ║
║           │  FastAPI routes, authentication middleware, rate limiting,    ║
║           │  request validation, OpenAPI documentation                    ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 3  │  INTENT UNDERSTANDING LAYER                                   ║
║           │  Task parsing, intent classification, goal extraction,        ║
║           │  constraint identification, priority assignment               ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 4  │  ━━━━━━━━━━━━ AEOS KERNEL ━━━━━━━━━━━━                      ║
║           │  Central coordinator, policy enforcement, resource            ║
║           │  governance, service registry, lifecycle management,          ║
║           │  ALL inter-layer communication passes through here            ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 5  │  EXECUTION ENGINE LAYER                                       ║
║           │  Constraint solving, goal decomposition, plan generation,     ║
║           │  execution strategy selection                                 ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 6  │  WORKFLOW RUNTIME LAYER                                       ║
║           │  DAG execution, state machine management, step sequencing,    ║
║           │  parallel execution, compensation logic                       ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 7  │  AGENT RUNTIME LAYER                                          ║
║           │  6 specialized agents (Research, Code, Data Analysis,         ║
║           │  DevOps, Security, Documentation), cognitive cycle execution  ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 8  │  REASONING RUNTIME LAYER                                      ║
║           │  Chain-of-thought, ReAct loop, reflection, self-critique,     ║
║           │  multi-model reasoning strategies                             ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 9  │  TOOL RUNTIME LAYER                                           ║
║           │  Tool registry, sandboxed execution, permission checking,     ║
║           │  cost tracking, result validation                             ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 10 │  MEMORY SYSTEM LAYER                                          ║
║           │  4-tier memory model: working / episodic / semantic /         ║
║           │  procedural. Session management, memory consolidation         ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 11 │  KNOWLEDGE RUNTIME LAYER                                      ║
║           │  RAG engine (ChromaDB), Knowledge Graph (Neo4j),              ║
║           │  document ingestion, semantic search, entity extraction       ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 12 │  ML PLATFORM LAYER                                            ║
║           │  Model training, evaluation, serving, A/B testing,            ║
║           │  fine-tuning pipelines, model registry                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 13 │  SOFTWARE INTELLIGENCE LAYER (OSIP)                          ║
║           │  Code analysis, repository intelligence, dependency graph,    ║
║           │  code quality metrics, vulnerability scanning                 ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 14 │  OBSERVABILITY LAYER                                          ║
║           │  Distributed tracing (OpenTelemetry), metrics (Prometheus),   ║
║           │  structured logging, health checks, alerting                  ║
╠══════════════════════════════════════════════════════════════════════════╣
║  LAYER 15 │  GOVERNANCE LAYER                                             ║
║           │  Policy engine, cost tracking, audit log, security controls,  ║
║           │  compliance reporting, rate limit enforcement                  ║
╚══════════════════════════════════════════════════════════════════════════╝

          ↑ All layers emit telemetry to Layer 14 (Observability)
          ↑ All layers are governed by Layer 15 (Governance)
          ↑ All inter-layer calls are mediated by Layer 4 (Kernel)
```

---

## Core Vocabulary

The following terms have precise definitions within AEOS. When these terms appear in documentation, code, ADRs, or conversation, they carry these specific meanings. Using them loosely or interchangeably is an error.

### Runtime Concepts

**Agent**
An autonomous computational unit with a defined role, cognitive cycle, tool permissions, and memory scope. An Agent receives a Task from the Kernel, executes a reasoning loop (Perceive → Reason → Act → Reflect), and returns a typed AgentResult. Agents are stateless between invocations; all persistence goes through Memory or Knowledge systems.

**Tool**
A callable capability exposed to Agents through the Tool Runtime. Tools are sandboxed, permissioned, cost-tracked, and versioned. Examples: `web_search`, `code_executor`, `database_query`, `file_read`. Tools are not Agents; they have no reasoning capability and execute deterministically given their inputs.

**Plugin**
A self-contained extension package that adds new capabilities to AEOS without modifying core platform code. Plugins may provide new Tools, new Agent types, new Knowledge connectors, or new Workflow step types. Plugins are loaded through the Plugin Loader, sandboxed per Law 7, and registered with the Kernel per Law 1.

**Kernel**
The central coordination component of AEOS. Analogous to an OS kernel: it manages resources, schedules work, enforces policy, and provides the service bus through which all components communicate. The Kernel is not a monolithic process; it is a coordination interface that can be distributed. It maintains the Service Registry, the Task Queue, and the Policy Engine.

**Workflow**
A directed acyclic graph (DAG) of Tasks with defined dependencies, parallelism constraints, and state transitions. Workflows are declared as typed data structures, not as procedural code. The Workflow Runtime executes Workflows, manages their state, handles failures, and reports completion to the Kernel.

**Task**
The atomic unit of work in AEOS. A Task has a typed input, a typed expected output, a priority, a deadline, a set of required capabilities, and a lifecycle (PENDING → DISPATCHED → RUNNING → COMPLETE | FAILED | CANCELLED). Tasks are created by the Execution Engine, dispatched by the Kernel, and executed by Agents or sub-workflows.

**Goal**
A high-level user intent expressed in natural language or structured form, before decomposition into Tasks. The Intent Understanding Layer converts an incoming request into a Goal. The Execution Engine decomposes a Goal into one or more Tasks or Workflows.

**Intent**
The parsed representation of a user request, intermediate between raw text and a structured Goal. Intent includes the action type, entities, constraints, and priority extracted from the input. The Intent Understanding Layer produces Intents; the Execution Engine converts Intents to Goals.

**Session**
A stateful interaction context with a user or calling system. A Session has an ID, a user identity, an active Memory context, a set of in-progress Tasks, and an expiry. Sessions are owned by the Memory System and referenced by the Kernel for context-aware dispatch.

**Memory**
Structured, typed storage for Agent context across the lifecycle of a session or beyond. AEOS uses a 4-tier model: Working Memory (in-process, ephemeral), Episodic Memory (session-scoped, sequential), Semantic Memory (long-term, vector-indexed), and Procedural Memory (skill and pattern storage).

**Knowledge**
Information retrieved from external or pre-indexed sources through the Knowledge Runtime. Distinguished from Memory: Memory is what the system learned or was given during a session; Knowledge is what was indexed beforehand (documents, code, databases) and retrieved via RAG or graph queries.

**Reasoning Strategy**
A defined algorithm for how an Agent uses its LLM to make decisions. Current strategies include: Chain-of-Thought (CoT), ReAct (Reason+Act), Reflection, Tree-of-Thought (ToT), and Self-Critique. The Reasoning Runtime selects and executes the appropriate strategy based on Task type and Agent configuration.

### Infrastructure Concepts

**Message Bus**
The asynchronous communication channel for inter-component events in AEOS. Implemented as an event queue (Redis Streams or Kafka). All Agent-to-Agent communication and all component events are published to the Message Bus. No component holds direct references to other components for communication purposes.

**Service Registry**
A catalog maintained by the Kernel that tracks all registered services, agents, tools, and plugins — their capabilities, health status, current load, and resource consumption. Components discover each other through the Service Registry, not through direct imports or hardcoded addresses.

**Execution Plan**
The output of the Execution Engine: a structured specification of which Agents will execute which Tasks in what order, with what inputs, what tool permissions, and what resource budget. Execution Plans are typed data structures, not code.

**Adapter**
A thin interface implementation that bridges AEOS internal interfaces to external system clients (per Law 9). Every external system has exactly one Adapter. The Adapter handles connection management, serialization, error translation, and instrumentation.

**Resource Budget**
A typed constraint on compute, memory, LLM tokens, API calls, and cost assigned to a Task or Workflow by the Governance Layer. Budgets are enforced by the Kernel; tasks that exceed their budget are terminated or throttled.

**ADR (Architecture Decision Record)**
A formal document in `docs/adr/` that records a significant architectural decision: what was decided, why, what alternatives were considered, and what the consequences are. ADRs are immutable once accepted; superseded ADRs are marked as such but not deleted.

**Cognitive Cycle**
The internal execution loop of an Agent: Perceive (receive task and context) → Reason (apply reasoning strategy to form a plan) → Act (invoke tools to gather information or effect changes) → Reflect (evaluate results and determine next step) → Return (emit typed result). The Cognitive Cycle is implemented in the Agent Runtime.

**OSIP (Open Software Intelligence Platform)**
The Software Intelligence Layer of AEOS. OSIP provides code analysis, repository understanding, dependency graph construction, vulnerability scanning, and code quality metrics. It is the layer that makes AEOS useful for AI-assisted software engineering workflows.

**Governance Policy**
A typed rule enforced by the Governance Layer that constrains behavior in the runtime. Examples: cost-per-task limits, rate limits per user, prohibited tool combinations, required approval for specific workflows. Policies are loaded from configuration per Law 8.

---

## Scope

### What Is IN AEOS

- The AEOS Kernel and its coordination protocols
- The FastAPI-based API Gateway and all route definitions
- The Intent Understanding and Execution Engine layers
- The Workflow Runtime (DAG execution, state machine)
- The Agent Runtime and all six core agent types
- The Reasoning Runtime (CoT, ReAct, Reflection strategies)
- The Tool Runtime (registry, sandboxing, cost tracking)
- The 4-tier Memory System
- The Knowledge Runtime (RAG with ChromaDB, Knowledge Graph with Neo4j)
- The ML Platform (training, serving, evaluation pipeline)
- The Software Intelligence Layer (OSIP)
- The Observability Layer (tracing, metrics, logging infrastructure)
- The Governance Layer (policy engine, audit log, cost tracking)
- The Plugin Architecture (loader, sandbox, registry)
- The SDK for building Plugins and custom Agents

### What Is NOT IN AEOS

- **Specific LLM models**: AEOS integrates with LLMs through adapters but does not include, train, or host LLM weights.
- **A general-purpose chatbot interface**: AEOS is infrastructure, not a consumer product. UIs are not in scope.
- **A data engineering platform**: AEOS has an ML pipeline but is not a replacement for Airflow, Spark, or dbt for general data transformation workflows.
- **A code editor or IDE**: OSIP provides code analysis but is not a development environment.
- **Multi-tenant SaaS**: AEOS v1/v2 is single-tenant deployment. Multi-tenancy is a future scope item.
- **LLM fine-tuning at scale**: The ML Platform supports fine-tuning of smaller models but is not a training cluster for foundation models.
- **A general-purpose workflow orchestrator**: AEOS Workflows are specifically designed for AI agent workflows, not arbitrary job scheduling.
- **Auth/SSO provider**: AEOS validates tokens from an external identity provider; it does not issue identities.
- **Domain-specific business logic**: AEOS provides the runtime; business-specific agents and tools are plugins.

---

## Cross-Reference Index

| Document | Path | Relationship to Constitution |
|---|---|---|
| Vision and Mission | `docs/architecture/000-VISION.md` | Explains *why* these laws exist; the problem context |
| Full Architecture Specification | `docs/architecture/001-ARCHITECTURE.md` | Implements the laws in concrete architectural decisions |
| Kernel Design | `docs/architecture/010-KERNEL.md` | Implements Laws 1, 4, 6 |
| Agent Runtime Spec | `docs/architecture/020-AGENT-RUNTIME.md` | Implements Laws 2, 3, 5, 6 |
| Tool Runtime Spec | `docs/architecture/030-TOOL-RUNTIME.md` | Implements Laws 7, 9 |
| Memory System Spec | `docs/architecture/040-MEMORY.md` | Implements Laws 3, 4 |
| Knowledge Runtime Spec | `docs/architecture/050-KNOWLEDGE.md` | Implements Laws 4, 9 |
| Observability Spec | `docs/architecture/060-OBSERVABILITY.md` | Implements Law 6 |
| Governance Spec | `docs/architecture/070-GOVERNANCE.md` | Implements Laws 5, 7, 8 |
| Plugin Architecture | `docs/architecture/080-PLUGINS.md` | Implements Laws 7, 9, 10 |
| ADR Index | `docs/adr/README.md` | Records all Constitutional-level decisions |
| Operational Runbook | `docs/ops/RUNBOOK.md` | Operational implementation of Laws 5, 6 |

---

*This document is version-controlled. All changes require the Amendment Process described above. The current version is 1.0.0, ratified on 2026-07-05.*
