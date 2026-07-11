# 006 — AEOS Execution Engine

| Field       | Value                                         |
|-------------|-----------------------------------------------|
| **Status**  | Approved                                      |
| **Version** | 1.0.0                                         |
| **Date**    | 2026-07-05                                    |
| **Authors** | AEOS Platform Team                            |
| **Replaces**| Implicit orchestrator logic in orchestrator.py|

---

## Abstract

The **Execution Engine** is the central transformation layer of AEOS. It sits between the AEOS Kernel (which owns lifecycle and plugin management) and the Agent Runtime (which owns individual agent cognition). Its responsibility is singular and total: take a raw user intent, compile it into a validated, typed execution graph, run that graph to completion, and return a governed, audited result.

This document specifies the 15-stage execution pipeline, its typed data structures, the execution graph model, the workflow state machine, parallelism semantics, error propagation rules, timeout handling, and the migration path from the current Orchestrator to this new model.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [System Position](#2-system-position)
3. [The 15-Stage Execution Pipeline](#3-the-15-stage-execution-pipeline)
4. [Full Pipeline Diagram](#4-full-pipeline-diagram)
5. [Typed Data Structures](#5-typed-data-structures)
6. [The Execution Graph](#6-the-execution-graph)
7. [Workflow State Machine](#7-workflow-state-machine)
8. [Revision Loop](#8-revision-loop)
9. [Parallelism](#9-parallelism)
10. [Timeout Handling](#10-timeout-handling)
11. [Error Propagation](#11-error-propagation)
12. [Current State vs. Target State](#12-current-state-vs-target-state)
13. [Migration Path](#13-migration-path)
14. [Cross-References](#14-cross-references)

---

## 1. Motivation

### 1.1 Why the Current Orchestrator Is Insufficient at Scale

The current `orchestrator.py` is a competent, well-structured coordinator for single-agent, single-step task execution. It provides intelligent keyword-based routing, a fallback chain, event emission, and memory integration. For AEOS v1, this is sufficient.

As AEOS grows toward autonomous multi-agent operation, five structural limitations emerge:

**1. No Compilation Phase.**  
The current Orchestrator resolves routing at runtime, during the same call that executes the task. There is no separation between "figuring out what to do" and "doing it." This makes it impossible to validate a plan before committing execution resources, preview a task's execution graph for review, or cache and reuse compiled plans for repeated task patterns.

**2. No Graph Structure.**  
Tasks are dispatched to one primary agent with one optional fallback. Multi-agent tasks requiring sequential or parallel composition (research → analyze → synthesize) cannot be expressed. The execution model is flat: one agent in, one result out.

**3. No Reflection or Quality Gate.**  
Results are returned to the caller as-is. There is no layer that evaluates output quality, detects incomplete answers, or triggers revision. Quality control is entirely the caller's responsibility.

**4. No Governance Gate.**  
Policy checks, cost accounting, and audit logging happen incidentally (in agent code) rather than as a structured, mandatory final stage. This means governance is optional and inconsistent.

**5. No Formal Lifecycle.**  
The Orchestrator blends intent parsing, routing, dispatch, collection, and response construction into one `run_task()` method. This is pragmatic for v1 but makes it impossible to instrument individual stages, apply different timeout policies per stage, or recover from failures at a specific point in the pipeline.

### 1.2 What the Execution Engine Adds

The Execution Engine solves all five problems by introducing:

- A **15-stage pipeline** with explicit typed input/output contracts per stage
- A **compilation phase** (stages 1–9) that validates the plan before execution begins
- A **graph execution model** supporting sequential, parallel, and conditional node types
- A **Reflection Gate** (stage 14) providing structured quality evaluation and revision triggering
- A **Governance Gate** (stage 15) as a mandatory, audited final checkpoint
- A **Workflow State Machine** managing lifecycle transitions with observable state
- **Formal error propagation rules** distinguishing halt-on-error from continue-with-partial

---

## 2. System Position

```
┌─────────────────────────────────────────────┐
│                  API Layer                  │
│         (FastAPI routes, WebSocket)         │
└─────────────────────────┬───────────────────┘
                          │ raw task string + mode
                          ▼
┌─────────────────────────────────────────────┐
│            AEOS Kernel (005)                │
│   (lifecycle, plugin management, services)  │
└─────────────────────────┬───────────────────┘
                          │ validated kernel context
                          ▼
╔═════════════════════════════════════════════╗
║          EXECUTION ENGINE (this doc)        ║
║                                             ║
║   Stages 1-9:  Compilation Phase            ║
║   Stage 10:    Workflow Entry               ║
║   Stages 11-13: Runtime Phase               ║
║   Stages 14-15: Evaluation Phase            ║
╚═════════════════════════════════════════════╝
                          │ governed result
                          ▼
┌─────────────────────────────────────────────┐
│          Agent Runtime (007)                │
│   (agent cognition: Observe→Orient→         │
│    Decide→Act→Reflect)                      │
└─────────────────────────────────────────────┘
                          │ agent results
                          ▼
┌─────────────────────────────────────────────┐
│          Memory System (009)                │
│      (Tier 1-4, read/write/search)          │
└─────────────────────────────────────────────┘
```

---

## 3. The 15-Stage Execution Pipeline

Each stage is described with: its role, input type, output type, internal logic, and failure modes.

---

### Stage 1: Intent Reception

**Role:** Accept the raw task request from the API layer and establish a trace context.

**Input:** 
```
raw_task: str          # The user's task string
mode: str              # Execution mode: "auto" | "plan_only" | "dry_run" | "sync"
caller_id: str         # API key or session identifier
request_id: str        # UUID from the HTTP layer
```

**Output:** `RawIntent`

**Logic:**
- Extract `raw_task`, `mode`, `caller_id`, `request_id` from the incoming API call.
- Generate a new `trace_id` (UUID4) if none is provided. This trace_id propagates through all 15 stages and into every event emitted on the message bus.
- Capture the `received_at` timestamp (UTC ISO 8601).
- Write raw input into Tier 1 Sensory Buffer (see 009-MEMORY_SYSTEM.md): stores the raw strings with a <1-second TTL for use in Stage 3 (Intent Understanding).
- Publish event: `task.execution.started` with `{trace_id, task_preview: raw_task[:100], mode}`.
- No transformation of the input occurs here. Stage 1 is the intake gate only.

**Failure Modes:**
- `mode` is not a recognized value → reject with `ExecutionError(code="INVALID_MODE")` before entering the pipeline.
- `raw_task` is empty string → reject with `ExecutionError(code="EMPTY_TASK")`.
- `request_id` collision detected (duplicate processing) → return cached result if available, else reject with `ExecutionError(code="DUPLICATE_REQUEST")`.

---

### Stage 2: Input Validation

**Role:** Sanitize the raw task string, enforce size limits, and run policy pre-screening.

**Input:** `RawIntent`

**Output:** `ValidatedInput`

**Logic:**
- **Length check:** `len(raw_task)` must not exceed `settings.max_task_length` (default: 8,192 characters). Reject if exceeded.
- **Encoding check:** Verify the task is valid UTF-8. Reject if it contains null bytes or non-printable control characters (except newlines and tabs).
- **Content policy pre-screen:** Run the task string through the registered `PolicyPreScreener` plugin. The pre-screener is a fast, synchronous check (no LLM call) that looks for obvious policy violations: disallowed keywords, known harmful patterns, prompt injection signatures.
  - Pre-screener returns: `PASS | WARN | BLOCK`
  - `BLOCK` → publish `task.governance.rejected` event, return `ExecutionError(code="POLICY_BLOCKED", reason=screener_reason)`.
  - `WARN` → attach warning to `ValidatedInput.policy_warnings`. Execution continues but the warning is carried through all stages and surfaced in the Governance Gate (Stage 15).
- **Whitespace normalization:** Strip leading/trailing whitespace. Normalize internal whitespace runs to single spaces.
- **Injection detection:** Check for obvious prompt injection patterns (e.g., `"Ignore all previous instructions"`). Flag as `WARN` if found.

**Failure Modes:**
- Task too long → `ExecutionError(code="TASK_TOO_LONG", max=8192, actual=len)`
- Invalid encoding → `ExecutionError(code="INVALID_ENCODING")`
- Policy BLOCK → `ExecutionError(code="POLICY_BLOCKED")`
- PolicyPreScreener plugin unavailable → continue without pre-screening; attach `policy_warnings=["pre_screener_unavailable"]`.

---

### Stage 3: Intent Understanding

**Role:** Parse the validated task string into a structured representation: task type, entities, goals, and ambiguity flags.

**Input:** `ValidatedInput`

**Output:** `ClassifiedIntent`

**Logic:**
- **Task Type Classification:** Assign one of the known task types:
  - `RESEARCH` — information gathering, web search, document retrieval
  - `ANALYSIS` — evaluation, comparison, assessment of provided data
  - `EXECUTION` — performing an action: running code, deploying, writing files
  - `PLANNING` — decomposing a complex request into sub-tasks
  - `SYNTHESIS` — combining multiple inputs into a unified output
  - `CONVERSATIONAL` — simple Q&A, no tool use expected
  - `MULTI_STEP` — explicitly requires multiple agents or steps
  - `UNKNOWN` — cannot classify; falls back to `MULTI_STEP` with a warning

  Classification uses the keyword routing logic currently in `orchestrator.py` (`_RESEARCH_KW`, `_ANALYST_KW`, etc.) as a fast first-pass. In v2, this is augmented by an LLM-based classifier for `UNKNOWN` cases.

- **Entity Extraction:** Identify named entities in the task string:
  - File paths, URLs, code snippets, named concepts, dates, quantities.
  - Stored in `ClassifiedIntent.entities: list[Entity]`.

- **Ambiguity Detection:** Identify if the task contains unresolvable pronouns ("it", "that"), references to context not provided, or multiple plausible interpretations.
  - If ambiguity score > `settings.ambiguity_threshold` (default: 0.7): set `ClassifiedIntent.needs_clarification = True`.
  - Clarification handling: in `auto` mode, the Execution Engine makes a best-effort interpretation and notes the assumption. In `interactive` mode, it returns a clarification request to the API layer.

- **Complexity Estimation:** Estimate the number of agents and steps likely needed (1, 2-3, 4+). Used in Stage 8 (Planning) to select the appropriate planning strategy.

**Failure Modes:**
- Classifier plugin throws → fall back to keyword-based classification, log warning.
- All classification attempts fail → set `task_type=UNKNOWN`, set `needs_clarification=True`, continue.

---

### Stage 4: Constraint Collection

**Role:** Gather all constraints that apply to this execution: resource limits, time budgets, policy rules, caller-specific quotas.

**Input:** `ClassifiedIntent`

**Output:** `ConstraintSet`

**Logic:**
- **Source 1 — System defaults:** Read from `settings`: `max_agent_steps`, `default_timeout_seconds`, `max_parallel_agents`, `max_cost_per_task`.
- **Source 2 — Caller-specific overrides:** Look up the `caller_id` in the caller registry. Callers may have elevated limits (premium) or reduced limits (trial).
- **Source 3 — Mode-specific constraints:** `plan_only` mode disables all execution stages (11-13). `dry_run` mode simulates but does not actually call agents. `sync` mode disables parallelism (all nodes execute sequentially).
- **Source 4 — Task-type constraints:** Some task types carry implied constraints. `EXECUTION` tasks require tool access to be explicitly enabled. `RESEARCH` tasks apply rate limits on search tool invocations.
- **Source 5 — Policy rules:** Query the `PolicyEngine` for rules applicable to this task type and caller. Rules may further restrict: blocked tool categories, required reviewer agents, mandatory reflection.
- Assemble all constraints into a `ConstraintSet`. Conflicts are resolved by taking the more restrictive value.

**Failure Modes:**
- Caller registry unavailable → use system defaults, log warning.
- Policy engine unavailable → use defaults, attach `warnings=["policy_engine_unavailable"]`.
- Constraint set is internally contradictory (e.g., `min_steps=5` and `max_steps=3`) → `ExecutionError(code="CONTRADICTORY_CONSTRAINTS")`.

---

### Stage 5: Constraint Solving

**Role:** Verify that the collected constraints can be satisfied given available resources. Fail fast if they cannot.

**Input:** `ClassifiedIntent` + `ConstraintSet`

**Output:** `ConstraintSolution`

**Logic:**
- **Agent availability check:** Query the agent registry. Are the agent types needed for this task type currently available (registered, healthy, not rate-limited)?
- **Tool availability check:** Are the tools required for this task type available and enabled for this caller?
- **Time budget feasibility:** Given the complexity estimate from Stage 3 and the `timeout_seconds` constraint, is the task feasible? (Rough check: `estimated_steps × avg_step_latency_ms < timeout_seconds × 1000`.)
- **Cost budget feasibility:** Estimate maximum token consumption. Is it within the `max_cost_per_task` limit?
- **Parallelism feasibility:** If the task requires parallel execution but `max_parallel_agents=1` (sync mode), reconfigure the plan to be sequential.

If any hard constraint cannot be satisfied, emit `task.validation.failed` and return `ExecutionError` with a structured reason explaining exactly which constraint failed and why. This is the "fail fast" gate — it is far cheaper to reject here than to discover the constraint violation mid-execution.

If soft constraints cannot be met, degrade gracefully: reduce parallelism, reduce `top_k` search results, shorten synthesis output, etc. Record all degradations in `ConstraintSolution.degradations`.

**Failure Modes:**
- No agents available → `ExecutionError(code="NO_AGENTS_AVAILABLE")`
- Required tool blocked by policy → `ExecutionError(code="TOOL_ACCESS_DENIED")`
- Task not feasible within time budget → `ExecutionError(code="TIMEOUT_NOT_FEASIBLE")`
- All hard constraints satisfied, some soft constraints degraded → proceed with degraded `ConstraintSolution`.

---

### Stage 6: Goal Decomposition

**Role:** Break the classified intent into 1-N discrete, independently-evaluable goals.

**Input:** `ClassifiedIntent` + `ConstraintSolution`

**Output:** `GoalSet` (a list of `Goal` objects)

**Logic:**
- For `CONVERSATIONAL` and simple `RESEARCH` tasks: produce a single goal matching the intent directly.
- For `MULTI_STEP`, `PLANNING`, `SYNTHESIS` tasks: decompose into sub-goals.
  - Decomposition strategy depends on task type:
    - **Sequential decomposition:** Goal B depends on Goal A's result (e.g., research first, then analyze).
    - **Parallel decomposition:** Goals A and B are independent and can be pursued concurrently.
    - **Hierarchical decomposition:** Goal A is a parent with sub-goals A1, A2, A3.
  - The Planner Agent (when available) performs LLM-based decomposition. When not available, a rule-based decomposer handles common patterns.
- Each `Goal` has:
  - A `goal_id` (UUID)
  - A natural-language `description`
  - A `goal_type`: `INFORMATION | ACTION | SYNTHESIS | EVALUATION`
  - A list of `dependency_ids` (other goals that must complete before this one starts)
  - An initial `success_criteria` description (formalized in Stage 7)

**Failure Modes:**
- Decomposition produces 0 goals → `ExecutionError(code="DECOMPOSITION_FAILED")`
- Decomposition produces circular dependencies → `ExecutionError(code="CIRCULAR_GOAL_DEPENDENCY")`
- Decomposition produces more goals than `max_goals` constraint → truncate to highest-priority goals, log warning.

---

### Stage 7: Goal Building

**Role:** Enrich each raw goal with complete metadata needed for planning and evaluation.

**Input:** `GoalSet` (raw)

**Output:** `GoalSet` (enriched)

**Logic:**
- For each `Goal`:
  - **Priority assignment:** Score 1 (critical) to 5 (optional). Primary goals get priority 1. Enrichment goals (e.g., "also summarize in bullet points") get priority 4-5.
  - **Deadline assignment:** Distribute the overall task timeout across goals. Critical-path goals get proportionally more time.
  - **Success criteria formalization:** Convert the natural-language success criteria into a structured `SuccessCriteria` object with measurable conditions:
    - `min_word_count`, `required_entities_present`, `sentiment_constraint`, `format_requirement`
  - **Fallback goal assignment:** If this goal fails, what is the acceptable degraded alternative? (e.g., if "comprehensive research" fails, fall back to "brief summary of available knowledge")
  - **Evaluation metric selection:** Which evaluator metrics apply to this goal's output?
    - `FACTUALITY | COMPLETENESS | RELEVANCE | COHERENCE | SAFETY`
  - **Resource allocation:** How many agent steps and what tool budget does this goal get?

**Failure Modes:**
- Priority assignment fails (all goals assigned priority 1) → normalize to spread priorities.
- Deadline sum exceeds task timeout → proportionally reduce all deadlines.
- No success criteria can be formalized → use generic completeness check.

---

### Stage 8: Planning

**Role:** Select the agents that will execute each goal, assign responsibilities, and build a draft execution plan.

**Input:** `GoalSet` (enriched) + `ConstraintSolution`

**Output:** `ExecutionPlan`

**Logic:**
- **Agent selection:** For each goal, select the most appropriate agent type:
  - `RESEARCH` goals → `research_agent`
  - `ANALYSIS` goals → `analyst_agent`
  - `ACTION` goals → `executor_agent`
  - `SYNTHESIS` goals → `synthesis_agent` (or `simple_agent` fallback)
  - `EVALUATION` goals → `reviewer_agent`
  - Selection respects the `ConstraintSolution.available_agents` list.
- **Fallback assignment:** For each primary agent assignment, record the fallback chain from `_FALLBACK_CHAIN` (currently defined in `orchestrator.py`, promoted to a platform-level registry in v2).
- **DAG construction (draft):** Create draft execution graph:
  - Each goal becomes one or more `AgentNode` entries.
  - Goal dependencies become `SequentialEdge` entries.
  - Independent goals connected in `ParallelNode` groups.
- **Tool assignment:** Attach the required tool list to each node.
- **Priority sort:** Apply topological sort with priority tie-breaking to establish execution order.

**Failure Modes:**
- No suitable agent for a goal → use `simple_agent` universal fallback, log warning.
- Fallback chain is empty → `ExecutionError(code="NO_FALLBACK_AVAILABLE")`.
- Draft DAG has cycles (should not happen if Stage 6 passed, but defensive check) → `ExecutionError(code="PLAN_CYCLIC")`.

---

### Stage 9: Execution Graph Compilation

**Role:** Take the draft execution plan and produce a validated, fully-typed, executable `ExecutionGraph`. This is the compilation step.

**Input:** `ExecutionPlan`

**Output:** `ExecutionGraph`

**Logic:**
- **Agent reference resolution:** For each agent type string in the plan (e.g., `"research_agent"`), resolve to a concrete registered agent instance. Verify the instance is healthy.
- **Type checking:** Verify that each node's output type matches the expected input type of its downstream nodes. (e.g., a node producing `ResearchResult` feeding into an `AnalysisNode` that accepts `ResearchResult`.)
- **Dependency validation:** Run Kahn's algorithm to confirm the graph is a valid DAG (no cycles, all dependencies satisfiable).
- **Parallel group identification:** Identify sets of nodes with no dependencies between them. Mark as `ParallelGroup` for concurrent execution in Stage 11.
- **Timeout budget distribution:** Assign `timeout_ms` to each node based on the goal deadline set in Stage 7.
- **Tool capability verification:** For each `ToolNode`, verify the tool is registered, enabled, and accessible.
- **Graph serialization:** Serialize the compiled graph to a JSON-safe representation for:
  - Audit logging
  - `plan_only` mode response
  - Caching (future optimization: compiled graphs for common task patterns)
- Set `ExecutionGraph.compiled_at` timestamp. The graph is now immutable.

**Failure Modes:**
- Agent instance not found after resolution → `ExecutionError(code="AGENT_NOT_FOUND", agent_type=...)`
- Type mismatch between nodes → `ExecutionError(code="TYPE_MISMATCH", from_node=..., to_node=..., expected=..., got=...)`
- Graph has cycle (defensive) → `ExecutionError(code="GRAPH_CYCLIC")`
- Tool not found → `ExecutionError(code="TOOL_NOT_FOUND", tool_id=...)`

> **Note:** `plan_only` mode exits here. The compiled `ExecutionGraph` is returned as the response. Stages 10-15 do not run.

---

### Stage 10: Workflow Runtime Entry

**Role:** Hand the compiled `ExecutionGraph` to the Workflow State Machine and initialize runtime state.

**Input:** `ExecutionGraph`

**Output:** `WorkflowState` (PLANNING → EXECUTING transition)

**Logic:**
- Instantiate `WorkflowState` with:
  - `workflow_id`: new UUID (distinct from `trace_id`)
  - `execution_graph`: the compiled graph (immutable reference)
  - `state`: `PENDING` → `PLANNING` → `EXECUTING`
  - `step_results`: empty dict
  - `completed_nodes`: empty set
  - `failed_nodes`: empty set
  - `started_at`: UTC timestamp
- Write initial workflow state to Tier 2 Working Memory (keyed by `workflow_id`).
- Publish event: `task.execution.started` (runtime phase, distinct from Stage 1's reception event).
- Transition state machine: `PENDING → PLANNING → EXECUTING`.
- For `dry_run` mode: simulate all downstream stages, return a mock result without calling agents.

**Failure Modes:**
- Working memory write fails → `ExecutionError(code="MEMORY_WRITE_FAILED")`. Cannot proceed without state.
- State machine transition fails (invalid prior state) → `ExecutionError(code="INVALID_STATE_TRANSITION")`.

---

### Stage 11: Step Execution

**Role:** Iterate through the compiled execution graph in topological order; dispatch each node to the Agent Runtime for execution.

**Input:** `WorkflowState`

**Output:** `WorkflowState` (updated with each step's result)

**Logic:**

This stage is the core execution loop. It implements a topological executor with these properties:

**Topological Execution Algorithm:**
```
ready_queue = nodes with no unmet dependencies
while ready_queue is not empty:
    parallel_batch = all nodes in ready_queue (up to max_parallel_agents)
    results = await asyncio.gather(*[execute_node(n) for n in parallel_batch])
    for node, result in zip(parallel_batch, results):
        record result in WorkflowState.step_results
        mark node as COMPLETED or FAILED
        unblock downstream nodes whose dependencies are now all met
        add newly-unblocked nodes to ready_queue
```

**Node Dispatch:**
- `AgentNode`: call `agent_runtime.run(agent_id, task, context, timeout_ms)`
- `ToolNode`: call `tool_registry.invoke(tool_id, params, timeout_ms)`
- `ConditionalNode`: evaluate the condition expression against available step results; select the true or false branch; dynamically enable/disable downstream nodes
- `ParallelNode`: already handled by `asyncio.gather` in the batch loop
- `JoinNode`: wait for all incoming edges to have results; merge results using the join strategy (FIRST, ALL, MAJORITY)

**Context Passing:**
Each node receives:
- Its own goal description and success criteria
- All step results from completed upstream nodes (from Tier 2 Working Memory)
- The global task context (trace_id, task_id, mode)

**Step State Updates:**
After each node completes, update Tier 2 Working Memory with:
```python
mem.write_short(task_id, f"step.{node_id}.result", step_result)
mem.write_short(task_id, f"step.{node_id}.status", "completed" | "failed")
```

**Failure Modes:**
- Node returns an error → see Stage 12 (Result Collection) and Section 11 (Error Propagation).
- Node times out → see Section 10 (Timeout Handling).
- `asyncio.gather` raises (unexpected) → catch, mark all batch nodes as FAILED, attempt recovery.

---

### Stage 12: Result Collection

**Role:** Gather all step results from completed nodes; apply partial failure handling rules.

**Input:** `WorkflowState` (after Stage 11 completes)

**Output:** `WorkflowState` with `StepResult` objects for all nodes

**Logic:**
- Read all `step_results` from Tier 2 Working Memory.
- For each node:
  - If `COMPLETED`: extract the `StepResult` (success value, latency, agent metadata).
  - If `FAILED`: extract the `StepResult` (error type, error message, recovery_attempted flag).
  - If `TIMED_OUT`: create a synthetic `StepResult` with `status=TIMED_OUT, value=None`.
  - If `SKIPPED` (downstream of a failed required node): create a synthetic `StepResult` with `status=SKIPPED`.
- **Partial success evaluation:** Compute the `partial_success_ratio`:
  - `completed / (completed + failed + timed_out)`
  - If `partial_success_ratio >= settings.min_success_ratio` (default: 0.6): proceed to Stage 13 with partial results.
  - If `partial_success_ratio < settings.min_success_ratio`: transition to FAILED state, skip Stages 13-14, proceed directly to Stage 15 for governance logging.
- Publish per-node events: `agent.result.produced` for each completed node.

**Failure Modes:**
- All nodes failed → skip to Stage 15, return failure.
- Partial failure below threshold → skip to Stage 15, return failure.
- Partial failure above threshold → continue with partial results, flag in output.

---

### Stage 13: Aggregation

**Role:** Merge the individual step results from multiple nodes into a single, coherent combined output.

**Input:** `WorkflowState` with all `StepResult` objects

**Output:** `AggregatedResult`

**Logic:**
- **Aggregation strategy selection:** Based on the execution graph structure:
  - **Sequential chain:** The final node's result is the primary output; prepend a summary of intermediate results.
  - **Parallel fan-out:** All results are of equal importance; merge by concatenation with section headers.
  - **Hierarchical:** Parent goal result is primary; child results are supporting evidence.
  - **Mixed:** Apply sequential then parallel rules in nested fashion.
- **Result merging:** Combine text outputs:
  - Deduplicate overlapping content (by sentence-level similarity for text, exact match for data).
  - Apply goal priorities: priority-1 results are primary content; priority-4+ results are appendices.
  - Preserve attribution: which agent produced which part of the output.
- **Metadata aggregation:**
  - Total token cost = sum of all step costs.
  - Total latency = wall-clock time from Stage 11 start to Stage 13 end.
  - Agent roster = set of all agents that participated.
  - Confidence = weighted average of per-step confidence scores.
- **Format application:** Apply requested output format (markdown, JSON, plain text, structured data) from the original `ClassifiedIntent.output_format`.

**Failure Modes:**
- Aggregation produces empty output despite successful nodes → retry with a simpler concatenation strategy, log warning.
- Format application fails → return raw merged text, log format failure.

---

### Stage 14: Reflection Gate

**Role:** Evaluate the aggregated output against the original goals' success criteria. Trigger a revision loop if quality is below threshold.

**Input:** `AggregatedResult` + `GoalSet` (enriched, from Stage 7)

**Output:** `ReflectionGateResult`

**Logic:**
- For each goal in the `GoalSet`, evaluate the `AggregatedResult` against the goal's `SuccessCriteria`:
  - **COMPLETENESS:** Does the result address all required entities and topics from the goal?
  - **FACTUALITY:** (Optional, requires `reviewer_agent`) Are factual claims internally consistent?
  - **RELEVANCE:** Does the result stay on topic relative to the goal description?
  - **COHERENCE:** Is the result coherent (no contradictions, logical flow)?
  - **FORMAT:** Does the output match the requested format?
- Assign a `quality_score` (0.0 to 1.0) per goal and an `overall_quality_score` (weighted average by goal priority).
- **Decision:**
  - `overall_quality_score >= settings.reflection_threshold` (default: 0.75): **PASS** → proceed to Stage 15.
  - `overall_quality_score < settings.reflection_threshold` AND `revision_attempts < settings.max_revisions` (default: 2): **REVISE** → trigger revision loop (see Section 8).
  - `overall_quality_score < settings.reflection_threshold` AND `revision_attempts >= max_revisions`: **PASS_WITH_WARNING** → proceed to Stage 15 with a quality warning attached.
- Publish event: `agent.cognitive.step.completed` with `{step: "reflection", quality_score, decision}`.
- Write reflection result to Tier 2 Working Memory and Tier 3 Long-Term Memory:
  ```python
  mem.write_long(f"task_reflection.{task_id}", reflection_summary)
  ```

**Failure Modes:**
- `reviewer_agent` unavailable → skip factuality check, proceed with available metrics.
- Quality evaluation itself fails → default to PASS_WITH_WARNING to avoid infinite revision loop.

---

### Stage 15: Governance Gate

**Role:** Final mandatory checkpoint. Performs policy compliance verification, cost accounting, audit logging, and prepares the final response.

**Input:** `ReflectionGateResult` + `AggregatedResult` + `WorkflowState`

**Output:** `GovernanceGateResult` → final API response

**Logic:**
- **Policy compliance check:** Run the output through the `PolicyEngine.evaluate_output()`:
  - Check for PII in output that should not be surfaced.
  - Check for content policy violations introduced by agent-generated content.
  - Check that the output does not exceed data access permissions for `caller_id`.
  - Result: `PASS | REDACT | BLOCK`.
    - `REDACT`: Remove flagged sections, annotate redaction in output.
    - `BLOCK`: Suppress output entirely, return `ExecutionError(code="OUTPUT_POLICY_BLOCKED")`.
- **Cost accounting:** Compute final cost:
  - Token cost (LLM calls per agent).
  - Tool call cost (search, external API calls).
  - Compute cost (agent execution time × compute rate).
  - Record against `caller_id` quota.
  - If over-budget: log warning, do not block (cost was incurred; blocking here does not help).
- **Audit logging:** Write a complete audit record to the audit log store:
  ```
  AuditRecord {
    trace_id, task_id, caller_id,
    task_preview (first 200 chars),
    agents_used, tools_used,
    stage_latencies (ms per stage),
    total_cost, quality_score,
    governance_decision,
    timestamp
  }
  ```
- **Final response assembly:** Build the `GovernanceGateResult`:
  - `status`: "completed" | "completed_partial" | "failed" | "blocked"
  - `result`: the (possibly redacted) aggregated output
  - `thought`: the execution trace summary (which agents ran, key decisions made)
  - `trace_id`: for client-side correlation
  - `quality_score`: from Stage 14
  - `latency_ms`: end-to-end wall clock time
  - `cost`: token + tool + compute cost breakdown
  - `warnings`: list of any non-fatal issues (policy warnings, partial failures, quality below threshold)
- **Memory promotion:** Write task summary to Tier 3 Long-Term Memory:
  ```python
  mem.write_long(f"task_summary.{task_id}", {
      "task": task_preview,
      "result_summary": result[:500],
      "agents_used": agents_used,
      "quality_score": quality_score,
      "timestamp": now()
  })
  ```
- **Tier 2 cleanup:** Clear all Tier 2 Working Memory for this task: `mem.clear_task(task_id)`.
- Publish events: `task.execution.completed` or `task.execution.failed`.

**Failure Modes:**
- Policy engine unavailable → log error, attach `warnings=["governance_check_skipped"]`, return result.
- Audit log write fails → log critical error, do not block response (audit failure is not a user-facing error).
- Cost accounting fails → log error, continue.

---

## 4. Full Pipeline Diagram

```
API Layer
    │
    │  raw_task: str, mode: str
    ▼
╔═══════════════════════════════╗
║  Stage 1: Intent Reception    ║
║  → RawIntent                  ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 2: Input Validation    ║
║  → ValidatedInput             ║
╚═══════════╦═══════════════════╝
            │                  ╔══ BLOCK ══► PolicyRejected
            ▼
╔═══════════════════════════════╗
║  Stage 3: Intent Understanding║
║  → ClassifiedIntent           ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 4: Constraint Collection║
║  → ConstraintSet              ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 5: Constraint Solving  ║
║  → ConstraintSolution         ║
╚═══════════╦═══════════════════╝
            │                  ╔══ UNSATISFIABLE ══► ExecutionError
            ▼
╔═══════════════════════════════╗
║  Stage 6: Goal Decomposition  ║
║  → GoalSet (raw)              ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 7: Goal Building       ║
║  → GoalSet (enriched)         ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 8: Planning            ║
║  → ExecutionPlan              ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 9: Graph Compilation   ║
║  → ExecutionGraph (immutable) ║
╚═══════════╦═══════════════════╝
            │                  ╔══ plan_only mode ══► return graph
            ▼
╔═══════════════════════════════╗
║  Stage 10: Workflow Entry     ║
║  → WorkflowState (EXECUTING)  ║
╚═══════════╦═══════════════════╝
            │
            ▼
╔═══════════════════════════════╗
║  Stage 11: Step Execution     ║  ◄─────────────────────────────┐
║  Topological node dispatch    ║                                 │
║  → WorkflowState (in-progress)║                                 │
╚═══════════╦═══════════════════╝                                 │
            │                                                     │
            ▼                                                     │
╔═══════════════════════════════╗                                 │
║  Stage 12: Result Collection  ║                                 │
║  → StepResult per node        ║                                 │
╚═══════════╦═══════════════════╝                                 │
            │                  ╔══ ratio < threshold ══► skip to Stage 15
            ▼                                                     │
╔═══════════════════════════════╗                                 │
║  Stage 13: Aggregation        ║                                 │
║  → AggregatedResult           ║                                 │
╚═══════════╦═══════════════════╝                                 │
            │                                                     │
            ▼                                                     │
╔═══════════════════════════════╗                                 │
║  Stage 14: Reflection Gate    ║                                 │
║  PASS │ REVISE │ PASS_WARNED  ║                                 │
╚═══════╦════════╦══════════════╝                                 │
        │        │                                                │
        │     REVISE: re-execute                                  │
        │     specific failed nodes ─────────────────────────────┘
        │     (max 2 revisions)
        │
      PASS or PASS_WARNED
        │
        ▼
╔═══════════════════════════════╗
║  Stage 15: Governance Gate    ║
║  Policy │ Cost │ Audit │ Resp ║
╚═══════════╦═══════════════════╝
            │
            ▼
       API Response
  (OrchestratorResponse)
```

---

## 5. Typed Data Structures

All data structures are Python `dataclasses`. All use `field(default_factory=...)` for mutable defaults. All are JSON-serializable.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import uuid


# ── Stage 1 Output ─────────────────────────────────────────────────────────────

@dataclass
class RawIntent:
    """Output of Stage 1: Intent Reception."""
    raw_task: str
    mode: str                          # "auto" | "plan_only" | "dry_run" | "sync"
    caller_id: str
    request_id: str
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    received_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


# ── Stage 2 Output ─────────────────────────────────────────────────────────────

@dataclass
class ValidatedInput:
    """Output of Stage 2: Input Validation."""
    task: str                          # sanitized, normalized
    mode: str
    caller_id: str
    trace_id: str
    policy_warnings: list[str] = field(default_factory=list)
    validated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


# ── Stage 3 Output ─────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    RESEARCH       = "RESEARCH"
    ANALYSIS       = "ANALYSIS"
    EXECUTION      = "EXECUTION"
    PLANNING       = "PLANNING"
    SYNTHESIS      = "SYNTHESIS"
    CONVERSATIONAL = "CONVERSATIONAL"
    MULTI_STEP     = "MULTI_STEP"
    UNKNOWN        = "UNKNOWN"


@dataclass
class Entity:
    entity_type: str     # "url" | "file_path" | "code" | "concept" | "date" | "quantity"
    value: str
    span: tuple[int, int]  # character offsets in the original task string


@dataclass
class ClassifiedIntent:
    """Output of Stage 3: Intent Understanding."""
    task: str
    task_type: TaskType
    entities: list[Entity] = field(default_factory=list)
    needs_clarification: bool = False
    ambiguity_score: float = 0.0       # 0.0 (clear) to 1.0 (maximally ambiguous)
    complexity: str = "simple"         # "simple" | "medium" | "complex"
    estimated_steps: int = 1
    output_format: str = "markdown"    # "markdown" | "json" | "plain" | "structured"
    trace_id: str = ""
    policy_warnings: list[str] = field(default_factory=list)


# ── Stage 4 Output ─────────────────────────────────────────────────────────────

@dataclass
class ConstraintSet:
    """Output of Stage 4: Constraint Collection."""
    max_steps: int = 10
    max_parallel_agents: int = 3
    timeout_seconds: float = 120.0
    max_cost_tokens: int = 100_000
    max_cost_usd: float = 1.0
    allowed_tool_categories: list[str] = field(default_factory=lambda: ["search", "code", "file"])
    blocked_tool_categories: list[str] = field(default_factory=list)
    require_reviewer: bool = False
    require_reflection: bool = True
    min_quality_threshold: float = 0.75
    mode_overrides: dict[str, Any] = field(default_factory=dict)
    caller_tier: str = "standard"      # "trial" | "standard" | "premium"
    warnings: list[str] = field(default_factory=list)


# ── Stage 5 Output ─────────────────────────────────────────────────────────────

@dataclass
class ConstraintDegradation:
    constraint: str
    original_value: Any
    degraded_value: Any
    reason: str


@dataclass
class ConstraintSolution:
    """Output of Stage 5: Constraint Solving."""
    satisfiable: bool
    available_agents: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    effective_timeout_seconds: float = 120.0
    effective_max_parallel: int = 3
    degradations: list[ConstraintDegradation] = field(default_factory=list)
    unsatisfied_hard_constraints: list[str] = field(default_factory=list)


# ── Stages 6-7 Output ─────────────────────────────────────────────────────────

class GoalType(str, Enum):
    INFORMATION = "INFORMATION"
    ACTION      = "ACTION"
    SYNTHESIS   = "SYNTHESIS"
    EVALUATION  = "EVALUATION"


@dataclass
class SuccessCriteria:
    min_word_count: int = 0
    required_entities: list[str] = field(default_factory=list)
    required_format: str = ""
    evaluation_metrics: list[str] = field(default_factory=lambda: ["COMPLETENESS", "RELEVANCE"])


@dataclass
class Goal:
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    goal_type: GoalType = GoalType.INFORMATION
    priority: int = 3                  # 1 (critical) to 5 (optional)
    dependency_ids: list[str] = field(default_factory=list)
    deadline_ms: float = 30_000.0
    success_criteria: SuccessCriteria = field(default_factory=SuccessCriteria)
    fallback_description: str = ""
    resource_budget_tokens: int = 10_000


@dataclass
class GoalSet:
    goals: list[Goal] = field(default_factory=list)
    decomposition_strategy: str = "sequential"  # "sequential" | "parallel" | "hierarchical" | "mixed"

    def by_priority(self) -> list[Goal]:
        return sorted(self.goals, key=lambda g: g.priority)


# ── Stage 8 Output ─────────────────────────────────────────────────────────────

@dataclass
class AgentAssignment:
    goal_id: str
    primary_agent_type: str
    fallback_agent_types: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    """Output of Stage 8: Planning."""
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    assignments: list[AgentAssignment] = field(default_factory=list)
    goal_set: GoalSet = field(default_factory=GoalSet)
    execution_strategy: str = "topological"  # "topological" | "sequential" | "parallel_all"
    trace_id: str = ""


# ── Stage 9 Output ─────────────────────────────────────────────────────────────

@dataclass
class ExecutionGraph:
    """Output of Stage 9: Graph Compilation. Immutable after compilation."""
    graph_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    nodes: list[Any] = field(default_factory=list)   # list[GraphNode] (see Section 6)
    edges: list[Any] = field(default_factory=list)   # list[GraphEdge] (see Section 6)
    parallel_groups: list[list[str]] = field(default_factory=list)  # list of node_id sets
    topological_order: list[str] = field(default_factory=list)      # node_ids in order
    compiled_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    total_timeout_ms: float = 120_000.0
    trace_id: str = ""
    plan_id: str = ""


# ── Stage 10 Output ────────────────────────────────────────────────────────────

class WorkflowStatus(str, Enum):
    PENDING      = "PENDING"
    PLANNING     = "PLANNING"
    EXECUTING    = "EXECUTING"
    REFLECTING   = "REFLECTING"
    AGGREGATING  = "AGGREGATING"
    GOVERNANCE   = "GOVERNANCE"
    COMPLETED    = "COMPLETED"
    FAILED       = "FAILED"


@dataclass
class WorkflowState:
    """Live state of a running workflow. Mutable during Stages 10-15."""
    workflow_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: WorkflowStatus = WorkflowStatus.PENDING
    execution_graph: Optional[ExecutionGraph] = None
    step_results: dict[str, Any] = field(default_factory=dict)   # node_id → StepResult
    completed_nodes: set[str] = field(default_factory=set)
    failed_nodes: set[str] = field(default_factory=set)
    skipped_nodes: set[str] = field(default_factory=set)
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    revision_count: int = 0
    trace_id: str = ""
    task_id: str = ""


# ── Stage 11-12 Output ─────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"
    TIMED_OUT  = "TIMED_OUT"
    SKIPPED    = "SKIPPED"


@dataclass
class StepResult:
    node_id: str
    status: StepStatus
    value: Any = None                  # The agent's output (text, dict, etc.)
    error: str = ""
    agent_id: str = ""
    latency_ms: float = 0.0
    token_cost: int = 0
    confidence: float = 1.0
    produced_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


# ── Stage 13 Output ────────────────────────────────────────────────────────────

@dataclass
class AggregatedResult:
    content: str                       # The merged output (primary format)
    content_format: str = "markdown"
    supporting_data: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, str] = field(default_factory=dict)   # section → agent_id
    partial: bool = False              # True if some steps failed
    partial_success_ratio: float = 1.0
    total_token_cost: int = 0
    total_latency_ms: float = 0.0
    agents_used: list[str] = field(default_factory=list)
    confidence: float = 1.0


# ── Stage 14 Output ────────────────────────────────────────────────────────────

class ReflectionDecision(str, Enum):
    PASS         = "PASS"
    REVISE       = "REVISE"
    PASS_WARNED  = "PASS_WARNED"


@dataclass
class GoalEvaluation:
    goal_id: str
    quality_score: float               # 0.0 to 1.0
    completeness: float = 1.0
    relevance: float = 1.0
    coherence: float = 1.0
    factuality: Optional[float] = None  # None if reviewer not available
    format_ok: bool = True
    issues: list[str] = field(default_factory=list)


@dataclass
class ReflectionGateResult:
    decision: ReflectionDecision
    overall_quality_score: float
    goal_evaluations: list[GoalEvaluation] = field(default_factory=list)
    revision_targets: list[str] = field(default_factory=list)  # node_ids to re-execute
    warnings: list[str] = field(default_factory=list)


# ── Stage 15 Output ────────────────────────────────────────────────────────────

class GovernanceDecision(str, Enum):
    PASS    = "PASS"
    REDACT  = "REDACT"
    BLOCK   = "BLOCK"


@dataclass
class CostBreakdown:
    token_cost_tokens: int = 0
    token_cost_usd: float = 0.0
    tool_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0
    total_usd: float = 0.0


@dataclass
class GovernanceGateResult:
    governance_decision: GovernanceDecision
    status: str                         # "completed" | "completed_partial" | "failed" | "blocked"
    result: Any                         # the final output (possibly redacted)
    thought: str                        # execution trace summary
    trace_id: str = ""
    quality_score: float = 1.0
    latency_ms: float = 0.0
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    warnings: list[str] = field(default_factory=list)
    agents_used: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
```

---

## 6. The Execution Graph

### 6.1 Node Types

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class GraphNode:
    """Base class for all execution graph nodes."""
    node_id: str
    node_type: str
    goal_id: str
    timeout_ms: float = 30_000.0
    retry_count: int = 0
    max_retries: int = 1


@dataclass
class AgentNode(GraphNode):
    """
    Dispatches a task to a specific agent instance.
    The Agent Runtime handles the cognitive loop internally.
    """
    node_type: str = "agent"
    agent_type: str = ""               # e.g., "research_agent"
    agent_instance_id: str = ""        # resolved in Stage 9
    task_description: str = ""
    context_keys: list[str] = field(default_factory=list)  # which Working Memory keys to pass
    output_key: str = ""               # key to write result to in Working Memory


@dataclass
class ToolNode(GraphNode):
    """
    Invokes a registered tool directly (without agent wrapper).
    Used for simple, deterministic tool calls.
    """
    node_type: str = "tool"
    tool_id: str = ""
    tool_params: dict[str, Any] = field(default_factory=dict)
    output_key: str = ""


@dataclass
class ConditionalNode(GraphNode):
    """
    Evaluates a condition expression. Enables or disables downstream nodes
    based on the result.
    """
    node_type: str = "conditional"
    condition_expr: str = ""           # Python expression evaluated against step_results
    true_branch_node_ids: list[str] = field(default_factory=list)
    false_branch_node_ids: list[str] = field(default_factory=list)


@dataclass
class ParallelNode(GraphNode):
    """
    Marks a group of child nodes for concurrent execution.
    All child nodes start simultaneously; ParallelNode completes when all children complete.
    """
    node_type: str = "parallel"
    child_node_ids: list[str] = field(default_factory=list)


@dataclass
class JoinNode(GraphNode):
    """
    Waits for multiple upstream nodes and merges their results.
    """
    node_type: str = "join"
    input_node_ids: list[str] = field(default_factory=list)
    join_strategy: str = "all"         # "all" | "first" | "majority" | "best_quality"
    output_key: str = ""
```

### 6.2 Edge Types

```python
@dataclass
class GraphEdge:
    """Base class for all edges."""
    edge_id: str
    from_node_id: str
    to_node_id: str
    edge_type: str


@dataclass
class SequentialEdge(GraphEdge):
    """
    from_node must COMPLETE before to_node can start.
    Standard dependency edge.
    """
    edge_type: str = "sequential"


@dataclass
class ConditionalEdge(GraphEdge):
    """
    to_node starts only if condition evaluates to True at runtime.
    Used with ConditionalNode.
    """
    edge_type: str = "conditional"
    condition_value: bool = True       # which branch value this edge represents


@dataclass
class DataFlowEdge(GraphEdge):
    """
    Carries specific data from from_node's output to to_node's input.
    Enables selective context passing (only pass the relevant result keys).
    """
    edge_type: str = "data_flow"
    data_keys: list[str] = field(default_factory=list)  # which output keys to forward
```

### 6.3 Topological Execution Algorithm

```python
import asyncio
from collections import defaultdict, deque


async def execute_graph(
    graph: ExecutionGraph,
    workflow_state: WorkflowState,
    agent_runtime,
    tool_registry,
    memory,
    max_parallel: int = 3,
) -> WorkflowState:
    """
    Kahn's algorithm with priority sorting and parallel batch execution.
    """
    # Build in-degree map and adjacency list
    in_degree: dict[str, int] = {n.node_id: 0 for n in graph.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for edge in graph.edges:
        if isinstance(edge, SequentialEdge):
            in_degree[edge.to_node_id] += 1
            adjacency[edge.from_node_id].append(edge.to_node_id)

    # Initialize ready queue with nodes that have no dependencies
    # Sort by priority (lower number = higher priority)
    node_map = {n.node_id: n for n in graph.nodes}
    ready: deque[GraphNode] = deque(
        sorted(
            [node_map[nid] for nid, deg in in_degree.items() if deg == 0],
            key=lambda n: getattr(n, 'priority', 3)  # from goal metadata
        )
    )

    while ready:
        # Extract up to max_parallel nodes for concurrent execution
        batch: list[GraphNode] = []
        while ready and len(batch) < max_parallel:
            batch.append(ready.popleft())

        # Execute batch concurrently
        results = await asyncio.gather(
            *[_execute_node(node, workflow_state, agent_runtime, tool_registry, memory)
              for node in batch],
            return_exceptions=True
        )

        # Process results; unblock downstream nodes
        for node, result in zip(batch, results):
            if isinstance(result, Exception):
                step_result = StepResult(
                    node_id=node.node_id,
                    status=StepStatus.FAILED,
                    error=str(result),
                )
            else:
                step_result = result

            workflow_state.step_results[node.node_id] = step_result

            if step_result.status == StepStatus.COMPLETED:
                workflow_state.completed_nodes.add(node.node_id)
            else:
                workflow_state.failed_nodes.add(node.node_id)

            # Decrement in-degree for downstream nodes
            for downstream_id in adjacency[node.node_id]:
                in_degree[downstream_id] -= 1
                if in_degree[downstream_id] == 0:
                    downstream_node = node_map[downstream_id]
                    # Check if this node should be skipped due to upstream failure
                    if _should_skip(downstream_node, workflow_state, graph):
                        workflow_state.skipped_nodes.add(downstream_id)
                    else:
                        # Insert in priority order
                        _insert_sorted(ready, downstream_node)

    return workflow_state
```

---

## 7. Workflow State Machine

The Workflow State Machine governs the lifecycle of a single task execution. It ensures that stages are visited in a valid order and that illegal transitions are caught and logged.

### 7.1 States

| State        | Description                                                    |
|--------------|----------------------------------------------------------------|
| `PENDING`    | Workflow created; compilation has not started                  |
| `PLANNING`   | Stages 1–9 in progress (compilation phase)                     |
| `EXECUTING`  | Stages 10–12 in progress (agent runtime phase)                 |
| `REFLECTING` | Stage 14 in progress (quality evaluation)                      |
| `AGGREGATING`| Stage 13 in progress (result merging)                          |
| `GOVERNANCE` | Stage 15 in progress (policy, audit, cost)                     |
| `COMPLETED`  | Stage 15 completed successfully                                |
| `FAILED`     | Any hard failure that terminates the pipeline                  |

### 7.2 Valid Transitions

```
PENDING      → PLANNING     (compilation begins)
PLANNING     → EXECUTING    (graph compiled, runtime starts)
PLANNING     → FAILED       (compilation failed)
EXECUTING    → AGGREGATING  (all nodes complete or partial threshold met)
EXECUTING    → FAILED       (partial threshold not met)
AGGREGATING  → REFLECTING   (aggregation complete)
REFLECTING   → GOVERNANCE   (reflection PASS or PASS_WARNED)
REFLECTING   → EXECUTING    (revision loop: REVISE decision)
GOVERNANCE   → COMPLETED    (governance PASS or REDACT)
GOVERNANCE   → FAILED       (governance BLOCK)
```

### 7.3 State Machine Diagram

```
                ┌─────────┐
                │ PENDING │
                └────┬────┘
                     │ compilation begins
                     ▼
                ┌──────────┐
                │ PLANNING │ ◄─── Stages 1-9
                └────┬─────┘
          ┌──────────┤
          │ FAILED   │ success
          ▼          ▼
     [FAILED]  ┌───────────┐
               │ EXECUTING │ ◄─── Stages 10-12 ◄─────────┐
               └─────┬─────┘                              │
          ┌──────────┤                                     │ REVISE
          │ FAILED   │ success                             │
          ▼          ▼                                     │
     [FAILED]  ┌─────────────┐                            │
               │ AGGREGATING │ ◄── Stage 13               │
               └──────┬──────┘                            │
                      │                                    │
                      ▼                                    │
               ┌────────────┐                             │
               │ REFLECTING │ ◄── Stage 14 ───────────────┘
               └─────┬──────┘
                     │ PASS / PASS_WARNED
                     ▼
               ┌────────────┐
               │ GOVERNANCE │ ◄── Stage 15
               └──────┬─────┘
          ┌───────────┤
          │ BLOCK     │ PASS / REDACT
          ▼           ▼
     [FAILED]    [COMPLETED]
```

---

## 8. Revision Loop

The Reflection Gate (Stage 14) can trigger a targeted revision of specific graph nodes when output quality is below the threshold. This is not a full pipeline restart; only the nodes identified as producing low-quality output are re-executed.

### 8.1 Revision Process

```
Stage 14: Reflection Gate
    │
    │  ReflectionDecision = REVISE
    │  revision_targets = [node_id_A, node_id_B]
    │  revision_count < max_revisions (default: 2)
    │
    ▼
1. Construct revision context:
   - Original task description for each target node
   - The low-quality output that was produced
   - The specific quality issues identified (completeness, relevance, etc.)
   - An explicit revision instruction: "The previous response was incomplete
     regarding X. Please provide a more thorough answer covering Y and Z."

2. Re-execute target nodes only:
   - Set node status back to PENDING in WorkflowState
   - Re-dispatch each target node to Agent Runtime with revision context
   - Downstream nodes of revised nodes are also re-executed (cascade)

3. After revision execution:
   - Re-run Stage 12 (Result Collection) for revised nodes only
   - Re-run Stage 13 (Aggregation) to merge revised results
   - Re-run Stage 14 (Reflection Gate) with revision_count += 1

4. If revision_count >= max_revisions:
   - Force ReflectionDecision = PASS_WARNED
   - Attach warning: "max_revisions_reached; quality_score={score}"
   - Proceed to Stage 15
```

### 8.2 Revision Tracking in WorkflowState

```python
@dataclass
class RevisionRecord:
    revision_number: int
    target_node_ids: list[str]
    pre_revision_quality: float
    post_revision_quality: float
    triggered_at: str
```

The `WorkflowState` carries a `revision_history: list[RevisionRecord]` that is included in the audit record at Stage 15.

---

## 9. Parallelism

### 9.1 Concurrent Node Execution

Independent graph nodes (those with no dependencies between them) are executed concurrently using `asyncio.gather`. The maximum degree of concurrency is bounded by `ConstraintSolution.effective_max_parallel`.

```python
# Concurrent execution with timeout per batch
async def execute_batch_with_timeout(
    nodes: list[GraphNode],
    timeout_ms: float,
    **kwargs,
) -> list[StepResult]:
    tasks = [
        asyncio.wait_for(
            _execute_node(node, **kwargs),
            timeout=node.timeout_ms / 1000.0
        )
        for node in nodes
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        r if not isinstance(r, Exception)
        else StepResult(node_id=n.node_id, status=StepStatus.FAILED, error=str(r))
        for n, r in zip(nodes, results)
    ]
```

### 9.2 Parallelism Constraints

- **`sync` mode:** `max_parallel = 1`. All nodes execute sequentially in topological order.
- **Standard mode:** `max_parallel = settings.max_parallel_agents` (default: 3).
- **Premium callers:** May be granted `max_parallel` up to 10.
- **Shared resources:** Nodes sharing a tool with global rate limits (e.g., search API) are serialized even if they would otherwise be parallel. A `SemaphoreGuard` wraps rate-limited tool invocations.

### 9.3 Event Loop Considerations

The Execution Engine runs within a single asyncio event loop. Agent Runtime calls that involve blocking I/O (file reads, subprocess execution) must use `asyncio.to_thread()` to avoid blocking the loop. Long-running CPU-bound work should be dispatched to a `ProcessPoolExecutor`.

---

## 10. Timeout Handling

### 10.1 Timeout Levels

```
Level 1: Per-node timeout    (GraphNode.timeout_ms)
Level 2: Per-stage timeout   (implicit: sum of node timeouts in stage + buffer)
Level 3: Pipeline timeout    (ConstraintSet.timeout_seconds × 1000)
```

Each level is enforced independently. A node that times out at Level 1 does not immediately fail the pipeline — it fails that node, and the pipeline continues per the error propagation rules (Section 11).

### 10.2 Per-Node Timeout

```python
try:
    result = await asyncio.wait_for(
        agent_runtime.run(node.agent_type, task, context),
        timeout=node.timeout_ms / 1000.0
    )
except asyncio.TimeoutError:
    result = StepResult(
        node_id=node.node_id,
        status=StepStatus.TIMED_OUT,
        error=f"Node timed out after {node.timeout_ms}ms",
    )
    # Publish event
    await bus.publish("task.execution.timed_out", AgentMessage(
        topic="task.execution.timed_out",
        sender_id="execution_engine",
        payload={"node_id": node.node_id, "timeout_ms": node.timeout_ms},
        trace_id=trace_id,
    ))
```

### 10.3 Pipeline Timeout

A top-level `asyncio.wait_for` wraps the entire Stage 11 execution loop:

```python
try:
    workflow_state = await asyncio.wait_for(
        execute_graph(graph, workflow_state, ...),
        timeout=constraint_solution.effective_timeout_seconds,
    )
except asyncio.TimeoutError:
    # Mark all in-progress nodes as TIMED_OUT
    for node_id in _get_in_progress_nodes(workflow_state):
        workflow_state.step_results[node_id] = StepResult(
            node_id=node_id,
            status=StepStatus.TIMED_OUT,
            error="Pipeline timeout exceeded",
        )
        workflow_state.failed_nodes.add(node_id)
    workflow_state.status = WorkflowStatus.FAILED
    # Skip to Stage 15 for governance logging
```

### 10.4 Timeout Recovery

When a node times out:
1. If the node has `max_retries > retry_count`: retry once with double the timeout.
2. If the node has a fallback agent: re-dispatch to fallback agent with original timeout.
3. If neither: mark as TIMED_OUT, apply error propagation rules.

---

## 11. Error Propagation

When a node fails (error or timeout), downstream nodes are affected according to the **propagation policy** defined on each edge:

### 11.1 Propagation Policies

| Policy          | Behavior                                                                          |
|-----------------|-----------------------------------------------------------------------------------|
| `HALT`          | All downstream nodes of the failed node are SKIPPED. The pipeline completes with partial results. |
| `CONTINUE`      | Downstream nodes execute with a null/empty value for the failed node's output.     |
| `FALLBACK`      | Downstream nodes receive a pre-defined fallback value from the failed node's `fallback_value`. |
| `COMPENSATE`    | A designated compensation node is triggered to produce an alternative result.     |

The default policy is `HALT` for `SequentialEdge` and `CONTINUE` for edges from `JoinNode`.

### 11.2 Error Escalation Rules

```
Single node failure:
  → Apply propagation policy
  → Continue executing independent sibling nodes
  → Record in WorkflowState.failed_nodes

Multiple node failures (>= 50% of nodes in a parallel group):
  → HALT the entire parallel group
  → Mark remaining nodes in group as SKIPPED
  → Continue sequential nodes after the parallel group (with partial input)

Critical node failure (node.priority == 1):
  → Attempt fallback agent immediately
  → If fallback also fails: set WorkflowStatus.FAILED, skip to Stage 15

Pipeline-level failure (partial_success_ratio < min_success_ratio):
  → Set WorkflowStatus.FAILED
  → Skip Stages 13-14
  → Proceed to Stage 15 for governance logging and failure response
```

---

## 12. Current State vs. Target State

### 12.1 Stage Mapping

| Pipeline Stage                  | Current Orchestrator               | Status in v2                           |
|---------------------------------|------------------------------------|----------------------------------------|
| 1. Intent Reception             | Start of `run_task()`              | **Exists** — formalize as stage        |
| 2. Input Validation             | Not present                        | **New** — add validation layer         |
| 3. Intent Understanding         | Keyword routing (`_RESEARCH_KW` etc)| **Exists** — extend with LLM classify |
| 4. Constraint Collection        | `settings.*` reads in `run_task()` | **Exists partial** — formalize         |
| 5. Constraint Solving           | Not present (implicit)             | **New** — explicit feasibility check   |
| 6. Goal Decomposition           | Not present (always 1 goal)        | **New** — multi-goal support           |
| 7. Goal Building                | Not present                        | **New** — metadata enrichment          |
| 8. Planning                     | Agent selection logic              | **Exists** — extract, formalize        |
| 9. Graph Compilation            | Not present (no graph model)       | **New** — compilation phase            |
| 10. Workflow Runtime Entry      | `run_task()` begins agent call     | **Exists partial** — formalize state   |
| 11. Step Execution              | `agent.run()` call + fallback      | **Exists** — extend to graph execution |
| 12. Result Collection           | Catch block + `AgentResponse`      | **Exists** — formalize                 |
| 13. Aggregation                 | Single result (no merge needed)    | **New** — multi-result merge           |
| 14. Reflection Gate             | Reviewer agent (optional)          | **Exists partial** — formalize as gate |
| 15. Governance Gate             | Not present (spread across agents) | **New** — centralized governance       |

### 12.2 What Changes

**Removed from Orchestrator:**
- Agent routing keywords (promoted to Intent Understanding stage)
- Direct fallback chain dict (promoted to platform agent registry)
- `OrchestratorResponse` construction (replaced by `GovernanceGateResult`)

**Kept from Orchestrator:**
- Message bus integration pattern
- Memory read/write pattern
- Trace ID propagation
- `asyncio.gather` for parallel execution
- Structured logging conventions

---

## 13. Migration Path

The migration from the current Orchestrator to the full Execution Engine is designed to be incremental. Each phase is independently deployable and independently testable.

### Phase 1 — Formalize Current (v1.1)
**Target:** Zero behavior change. Pure refactoring.
- Extract Stage 1-3 logic from `run_task()` into standalone functions: `receive_intent()`, `validate_input()`, `classify_intent()`.
- Extract Stage 8 agent selection into `PlanningEngine.select_agent()`.
- Extract Stage 11 dispatch into `StepExecutor.execute_agent_node()`.
- `run_task()` becomes a thin orchestrator calling these functions in sequence.
- Add type annotations matching the data structures in Section 5.

### Phase 2 — Add Constraint Layer (v1.2)
- Implement `ConstraintCollector` and `ConstraintSolver` (Stages 4-5).
- Adds fail-fast rejection for infeasible tasks.
- Adds `plan_only` mode support.

### Phase 3 — Add Graph Model (v1.3)
- Implement `ExecutionGraph` data structure and `GraphCompiler` (Stage 9).
- `StepExecutor` reads from the graph rather than directly from the plan.
- For single-agent tasks: graph is trivially a single `AgentNode` with no edges.
- Multi-agent tasks still use the flat model (no parallel groups yet).

### Phase 4 — Add Goal Decomposition (v1.4)
- Implement `GoalDecomposer` (Stage 6) and `GoalBuilder` (Stage 7).
- Multi-goal tasks now produce graphs with multiple nodes and edges.
- `Aggregator` (Stage 13) merges multi-node results.

### Phase 5 — Add Governance Gate (v1.5)
- Implement `GovernanceGate` (Stage 15).
- Moves policy checking and audit logging to a dedicated stage.
- Adds cost accounting.

### Phase 6 — Full Execution Engine (v2.0)
- Implement `WorkflowStateMachine`.
- Implement `ReflectionGate` as a formal stage (Stage 14).
- Implement revision loop.
- Implement `InputValidator` (Stage 2).
- All 15 stages wired up; `orchestrator.py` becomes a thin adapter.

---

## 14. Cross-References

| Document                            | Relationship                                              |
|-------------------------------------|-----------------------------------------------------------|
| `005-KERNEL.md`                     | The Kernel boots the Execution Engine as a service; provides the plugin registry used in Stages 4-5 and 8-9 |
| `007-AGENT_RUNTIME.md`              | The Agent Runtime is called by Stage 11 (Step Execution). Each agent's cognitive loop (Observe→Orient→Decide→Act→Reflect) executes within a single node dispatch |
| `009-MEMORY_SYSTEM.md`              | Stages 1, 10, 11, 14, 15 all read/write specific memory tiers. This document specifies *which* stages access *which* tiers |
| `004-EVENT_MODEL.md`                | This document specifies which events are emitted at each stage. All events flow through the Message Bus specified in 004 |
| `001-ARCHITECTURE.md`               | High-level system diagram placing the Execution Engine in context |

---

*End of 006-EXECUTION_ENGINE.md*
