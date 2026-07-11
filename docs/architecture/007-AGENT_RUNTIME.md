# 007 — AGENT RUNTIME

| Field       | Value                                      |
|-------------|--------------------------------------------|
| Status      | Approved                                   |
| Version     | 1.0.0                                      |
| Created     | 2026-07-05                                 |
| Authors     | AEOS Architecture Team                     |
| Supersedes  | (none — new document)                      |
| See Also    | 001-ARCHITECTURE.md, 008-REASONING_RUNTIME.md, 009-MEMORY_SYSTEM.md |

---

## Abstract

This document defines the **AEOS v2 Agent Runtime** — the execution model governing how every autonomous agent in the AEOS system processes a task. The runtime replaces the v1 `think()/act()` two-phase lifecycle with an **11-Step Cognitive Model** that provides explicit stages for observation, understanding, retrieval, reasoning, hypothesis generation, evaluation, planning, execution, reflection, learning, and memory consolidation.

The 11-step model is not simply a refactor of the two-phase lifecycle. It is a qualitatively different approach to autonomous agent design: one that makes cognition observable, interruptible, auditable, and improvable over time. Every step produces a typed artifact. Every step has defined failure modes. Every step interacts with the memory system in a specified way. The result is an agent architecture that can be tested, tuned, monitored, and reasoned about by engineers — not just by the agent itself.

This document is the authoritative specification for implementing or migrating any agent in the AEOS system. Agents that do not conform to this runtime are not considered production-ready under v2.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [The 11-Step Cognitive Model](#2-the-11-step-cognitive-model)
   - 2.1 [Step 1: Observe](#21-step-1-observe)
   - 2.2 [Step 2: Understand](#22-step-2-understand)
   - 2.3 [Step 3: Retrieve](#23-step-3-retrieve)
   - 2.4 [Step 4: Reason](#24-step-4-reason)
   - 2.5 [Step 5: Hypothesize](#25-step-5-hypothesize)
   - 2.6 [Step 6: Evaluate](#26-step-6-evaluate)
   - 2.7 [Step 7: Plan](#27-step-7-plan)
   - 2.8 [Step 8: Execute](#28-step-8-execute)
   - 2.9 [Step 9: Reflect](#29-step-9-reflect)
   - 2.10 [Step 10: Learn](#210-step-10-learn)
   - 2.11 [Step 11: Remember](#211-step-11-remember)
3. [Stage Diagram](#3-stage-diagram)
4. [Typed Interface Definitions](#4-typed-interface-definitions)
5. [Cognitive Cycle Composition](#5-cognitive-cycle-composition)
6. [Abort Conditions](#6-abort-conditions)
7. [Agent Context Propagation](#7-agent-context-propagation)
8. [Memory Access Per Stage](#8-memory-access-per-stage)
9. [Tool Invocation Rules](#9-tool-invocation-rules)
10. [Confidence Thresholds](#10-confidence-thresholds)
11. [Agent Specialization](#11-agent-specialization)
12. [Migration Guide](#12-migration-guide)
13. [v1 Compatibility](#13-v1-compatibility)
14. [Cross-References](#14-cross-references)

---

## 1. Motivation

### 1.1 The Limits of think()/act()

The v1 agent lifecycle consists of two methods:

```python
def think(self, task: str) -> str:
    ...

def act(self, thought: str, context: dict) -> Any:
    ...
```

This model was adequate for the prototype phase of AEOS. It is insufficient for production autonomous agents for the following reasons:

**1. Cognitive opacity.** The `think()` method returns a string. There is no structural requirement on what that string means, how it was derived, or whether it is grounded in any retrieved evidence. Two agents can produce identical `think()` outputs through completely different internal processes, and the system has no way to distinguish them. This makes debugging cognitive failures nearly impossible: when an agent produces a wrong result, you cannot trace which cognitive step failed because the steps are not exposed.

**2. No memory integration.** The v1 lifecycle does not specify when or how agents access long-term memory, short-term working memory, or the RAG knowledge base. In practice, each agent has implemented memory access ad hoc — some in `think()`, some in `act()`, some not at all. There is no consistency, no visibility, and no way to reason about what any given agent "knows" at any point in its execution.

**3. No hypothesis generation or evaluation.** The v1 model collapses planning and execution into a single `act()` call. The agent does not explicitly generate multiple candidate approaches, evaluate them against constraints and policy rules, and select the best one. Instead, it takes the first reasonable path it finds. This produces brittle agents that fail silently when the first approach is wrong: they execute the wrong plan without knowing alternatives existed.

**4. No self-critique or reflection.** After `act()` completes, v1 agents do not ask: "Did I actually solve the task?" "Did my execution match my plan?" "What should I do differently next time?" The reviewer agent can score the output externally, but the executing agent itself has no mechanism for self-assessment. Agents cannot improve from experience.

**5. No learning or knowledge consolidation.** v1 agents do not write insights back to memory. Every execution starts from the same baseline state regardless of prior experience. The system cannot accumulate domain knowledge or agent-specific heuristics over time. This is a fundamental limitation for any system intended to operate autonomously at scale.

**6. Untyped data flow.** The `thought: str` and `context: dict` types carry no structural guarantees. Each agent interprets these values differently. Pipelines of agents are fragile: a change in how `planner_agent` formats its thought string can silently break `executor_agent` even though no interface contract was violated, because there was no interface contract.

**7. No abort semantics.** There is no specified mechanism for an agent to abort its own execution when it detects a policy violation, an unresolvable ambiguity, or resource exhaustion. In v1, agents either complete or raise an unhandled exception. There is no structured partial-completion result, no clean handoff to a recovery agent, and no audit trail of why execution stopped.

### 1.2 What Production Autonomous Agents Require

A production-grade autonomous agent runtime must provide:

- **Structured, typed cognitive stages** — so that every step of reasoning is inspectable and testable independently.
- **Memory integration at defined points** — so that every agent draws on available knowledge and contributes new knowledge back to the system.
- **Hypothesis generation and evaluation** — so that agents consider multiple approaches and select based on evidence and policy.
- **Self-reflection** — so that agents can detect and recover from their own errors before returning outputs.
- **Learning consolidation** — so that agents improve over time and the system accumulates domain knowledge.
- **Abort and recovery semantics** — so that partial failures are handled gracefully with full audit trails.
- **Observability hooks at every stage** — so that engineers can trace, debug, and optimize cognitive pipelines.

The 11-Step Cognitive Model defined in this document provides all of these properties.

---

## 2. The 11-Step Cognitive Model

The cognitive model is a pipeline of 11 stages. Each stage receives a typed input from the previous stage and produces a typed output for the next stage. The `AgentContext` object flows through all 11 stages, accumulating data. Feedback loops between stages are explicitly specified.

### 2.1 Step 1: Observe

**Description:** Parse and structure all raw inputs — the task string, the context dictionary, and the conversation/execution history — into a typed `ObservationContext` that downstream steps can consume without re-parsing.

**Input:** `RawTaskInput(task: str, context: dict, history: list[dict])`

**Output:** `ObservationContext` (see §4 for full definition)

**What the agent does:**

The Observe step is about reception, not interpretation. The agent does not yet try to understand what the task means. It simply parses and normalizes everything it has received:

- **Task string normalization:** Strip leading/trailing whitespace. Detect encoding issues. Identify if the task is a plain string, a JSON-encoded command, or a structured template. Convert to a normalized string form.

- **Context unpacking:** Enumerate all keys in the context dictionary. Type-check each value. Flag missing expected keys (e.g., `session_id`, `caller_agent_id`). Populate defaults where specified.

- **History structuring:** Parse the conversation history list into a typed sequence of `HistoryEntry` objects with role (user/agent/system), timestamp, and content. Compute history length and time span.

- **Metadata extraction:** Extract or generate the observation timestamp, a unique observation ID (UUIDv4), and any system-injected metadata (e.g., request priority, timeout budget).

- **Input validation:** Check for obviously malformed inputs — null task strings, non-serializable context values, corrupted history entries. If validation fails, the Observe step raises an `ObservationError` and the cognitive cycle aborts immediately with an `ABORT_MALFORMED_INPUT` code. This is the only step where an abort does not produce a partial result, because no useful work has been done.

- **Size and complexity bounds:** Compute token estimates for the task string and history. If they exceed configured limits, truncate history according to the truncation policy (see §009-MEMORY_SYSTEM.md for policy details) and set `ObservationContext.history_truncated = True`.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `ObservationError.NULL_TASK` | Task string is null or empty | Abort with `ABORT_MALFORMED_INPUT` |
| `ObservationError.ENCODING_ERROR` | Task contains non-UTF-8 bytes | Attempt recovery; abort if unrecoverable |
| `ObservationError.CONTEXT_SCHEMA_MISMATCH` | Required context keys missing | Log warning; populate defaults; continue |
| `ObservationError.HISTORY_CORRUPT` | History entries malformed | Drop corrupt entries; continue |
| `ObservationError.OVERSIZE_INPUT` | Combined input exceeds token budget | Truncate history; continue with warning |

**Memory access:**

- **Reads:** None. The Observe step deliberately reads no memory — it works only with what it received. This ensures the observation is a clean snapshot of the external world, not contaminated by prior state.
- **Writes:** Writes the `ObservationContext` to the `AgentContext.observation` field (in-context working memory). Does not persist to long-term memory.

**Tool access:** None. The Observe step invokes no external tools.

---

### 2.2 Step 2: Understand

**Description:** Classify the intent of the task, extract named entities and key parameters, resolve any ambiguities using heuristics or dialogue, and identify hard constraints that must be satisfied by any solution.

**Input:** `ObservationContext`

**Output:** `Understanding`

**What the agent does:**

The Understand step is the semantic gateway of the pipeline. After Observe has given us a clean, typed representation of the input, Understand asks: "What does this task actually mean, and what are the boundaries within which any solution must operate?"

- **Intent classification:** Map the normalized task string to one of the system's defined intent categories. In AEOS v2, the intent taxonomy includes: `QUERY`, `TRANSFORM`, `GENERATE`, `VALIDATE`, `PLAN`, `SUMMARIZE`, `ANALYZE`, `AGGREGATE`, `EXECUTE_ACTION`, `UNKNOWN`. The classification uses keyword matching, pattern templates, and — when an LLM backend is available — embedding-based classification. The confidence of the classification is recorded.

- **Entity extraction:** Identify all named entities in the task: subject (what the task is about), object (what the task should produce or modify), temporal references (deadlines, time ranges), quantitative parameters (counts, thresholds, limits), and agent references (if the task references other agents or subsystems).

- **Constraint identification:** Extract hard constraints — things that are non-negotiable. Examples: "must complete in under 2 seconds", "output must be valid JSON", "do not access external APIs", "result must cite sources". Hard constraints are distinguished from soft preferences.

- **Preference extraction:** Extract soft preferences that should influence solution selection but are not blockers: "prefer shorter outputs", "use formal language", "favor solutions that reuse cached results".

- **Ambiguity detection:** Identify terms or parameters in the task that have multiple valid interpretations. For each ambiguity: attempt to resolve it using context clues, history, or entity defaults. If unresolvable, mark as `UNRESOLVED_AMBIGUITY`. The agent may escalate unresolved ambiguities to the user or caller depending on the `ambiguity_policy` in the agent configuration.

- **Scope assessment:** Determine whether the task is atomic (can be solved in a single execution path) or composite (requires decomposition into subtasks). Mark `Understanding.requires_decomposition = True` if composite.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `UnderstandingError.UNCLASSIFIABLE_INTENT` | Task cannot be mapped to any intent category | Set intent = `UNKNOWN`; flag for escalation |
| `UnderstandingError.CONFLICTING_CONSTRAINTS` | Two hard constraints are mutually exclusive | Abort or escalate; record conflict in context |
| `UnderstandingError.UNRESOLVABLE_AMBIGUITY` | Ambiguity blocks all valid interpretations | Escalate to caller or abort |
| `UnderstandingError.SCOPE_TOO_LARGE` | Task scope exceeds agent capability bounds | Flag for decomposition; continue |

**Memory access:**

- **Reads:** Short-term session memory — to resolve entity references from earlier in the session (e.g., "do the same as before" requires knowing what "before" was). Reads agent-specific entity registry for domain vocabulary.
- **Writes:** Writes resolved entities and classified intent to the `AgentContext.understanding` field.

**Tool access:** None for rule-based understanding. When LLM-backed understanding is enabled (see §008-REASONING_RUNTIME.md §LLM-Backed vs. Rule-Based Reasoning), an LLM call is made here. This is subject to the tool invocation budget (see §9).

---

### 2.3 Step 3: Retrieve

**Description:** Query the agent's available memory tiers and the RAG knowledge base to surface all context that is relevant to the understood task, so that downstream reasoning steps have the richest possible information.

**Input:** `Understanding`

**Output:** `RetrievedContext`

**What the agent does:**

The Retrieve step is the epistemological foundation of the pipeline. An agent that reasons from nothing will hallucinate. An agent that retrieves well will reason from evidence.

- **Query construction:** Using the `Understanding` output (intent, entities, constraints), construct one or more retrieval queries. Queries are typed by target: a `MemoryQuery` targets the memory system, a `KnowledgeQuery` targets the RAG knowledge base. Queries include relevance filters (entity type, time range, minimum confidence of stored fact).

- **Short-term memory retrieval:** Query the session's short-term working memory for recent results, intermediate artifacts, and any agent communications received in this session. Short-term memory is always consulted first — it contains the most current state of the world as AEOS knows it.

- **Long-term memory retrieval:** Query the agent's long-term memory for persistent knowledge: past task solutions, learned heuristics, domain facts accumulated over previous sessions. Long-term memory retrieval is scored by relevance; only results above `retrieve_relevance_threshold` (default: 0.65) are included.

- **RAG knowledge base query:** Submit the knowledge query to the vector knowledge base. Retrieve up to `max_rag_results` (default: 5) chunks, ranked by semantic similarity to the query embedding. Each result carries a provenance record (source, timestamp, confidence of the source).

- **Context merging and deduplication:** Merge results from all three retrieval sources. Deduplicate entries that appear in multiple sources (prefer the more recent or higher-confidence version). Resolve conflicts between sources: if short-term memory contradicts long-term memory on a factual matter, short-term memory wins by recency; log the conflict in `AgentContext`.

- **Relevance scoring:** Assign each retrieved item a relevance score (0.0–1.0) against the query. Items below the threshold are discarded. The relevance score will later contribute to the confidence computation in the Reason step.

- **Retrieval budget enforcement:** The total retrieval result size is subject to a token budget. If results exceed the budget, apply the configured prioritization strategy: `RECENCY_FIRST`, `RELEVANCE_FIRST`, or `SOURCE_PRIORITY`.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `RetrievalError.MEMORY_UNAVAILABLE` | Memory system is down | Continue with empty retrieval; flag in context |
| `RetrievalError.RAG_UNAVAILABLE` | Knowledge base is unreachable | Continue without RAG results; log warning |
| `RetrievalError.ZERO_RESULTS` | No relevant results found | Continue; Reason step will note low evidence |
| `RetrievalError.BUDGET_EXCEEDED` | Result set exceeds token budget | Truncate per prioritization strategy |
| `RetrievalError.CONFLICT_UNRESOLVABLE` | Sources in direct factual conflict | Surface conflict in `RetrievedContext`; let Reason step adjudicate |

**Memory access:**

- **Reads:** Short-term session memory (read). Long-term agent memory (read). RAG knowledge base (read). All three are read; none are written during this step.
- **Writes:** Writes the `RetrievedContext` to `AgentContext.retrieved_context`. Does not yet persist anything to long-term memory.

**Tool access:** The memory query interfaces are internally considered tools. RAG queries invoke the vector similarity search engine, which is a sandboxed read-only tool invocation (no external network calls, no side effects). Cost is tracked per query.

---

### 2.4 Step 4: Reason

**Description:** Apply domain logic to the understood task using all retrieved context, producing a structured reasoning output that analyzes the task, identifies relevant facts, and frames the problem for hypothesis generation.

**Input:** `Understanding` + `RetrievedContext`

**Output:** `ReasoningOutput`

**What the agent does:**

The Reason step is where the agent moves from gathering information to thinking about it. It does not yet commit to a solution — that is the job of Hypothesize and Plan. Reason's job is to produce a rigorous, well-grounded analysis of what the task requires and what the retrieved evidence says about it.

- **Fact extraction:** From the `RetrievedContext`, extract all facts that are directly relevant to the task. Facts are typed: `ASSERTION` (a claimed truth), `CONSTRAINT` (a limit or rule), `EXAMPLE` (a precedent or past case), `COUNTER_EXAMPLE` (a case that limits generalization), `UNKNOWN` (a gap in available knowledge).

- **Reasoning mode selection:** Select the appropriate reasoning mode(s) for this task (see §008-REASONING_RUNTIME.md for full mode definitions):
  - `DEDUCTIVE`: When task requires applying known rules to specific cases.
  - `INDUCTIVE`: When task requires identifying patterns from multiple retrieved examples.
  - `ABDUCTIVE`: When task requires choosing the most likely explanation for observed evidence.
  - `ANALOGICAL`: When task requires applying a solution from a similar past case.

- **Domain logic application:** Apply the selected reasoning mode to produce a structured analysis:
  - State the key question the task is asking.
  - List the relevant evidence (with source citations from `RetrievedContext`).
  - Identify what the evidence supports.
  - Identify what the evidence does not support or leaves unresolved.
  - Note any logical tensions or contradictions in the evidence.

- **Gap identification:** Explicitly list what information is *not* available but would be needed for a high-confidence answer. These gaps will inform hypothesis confidence scoring in the next step.

- **Initial confidence estimate:** Compute an initial confidence estimate for the reasoning output based on: evidence volume (how many relevant facts were retrieved), evidence quality (source confidence scores), internal consistency (no contradictions = higher confidence), and coverage (how many identified gaps remain).

- **Thought chain construction:** Build a structured thought chain (see §008-REASONING_RUNTIME.md §Chain-of-Thought Protocol for format). The thought chain records each reasoning move, the evidence it cited, and the conclusion it reached.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `ReasoningError.INSUFFICIENT_EVIDENCE` | Retrieved context too sparse to reason | Proceed with low confidence; flag |
| `ReasoningError.CONTRADICTORY_EVIDENCE` | Evidence directly contradicts itself | Surface contradiction in output; lower confidence |
| `ReasoningError.OUT_OF_DOMAIN` | Task outside agent's knowledge domain | Produce reasoning output with `domain_match = False` |
| `ReasoningError.REASONING_LOOP` | Circular reasoning detected in thought chain | Break loop; mark affected conclusion as unconfirmed |

**Memory access:**

- **Reads:** `AgentContext.retrieved_context` (in-context). The agent-specific reasoning rules registry (long-term memory, read-only).
- **Writes:** Writes `ReasoningOutput` to `AgentContext.reasoning`. Appends thought chain to `AgentContext.thought_log`.

**Tool access:** None by default. When LLM-backed reasoning is enabled, an LLM call is made here — this is the primary LLM-invocation point in the pipeline. Subject to tool budget.

---

### 2.5 Step 5: Hypothesize

**Description:** Generate between 1 and N candidate approaches or solutions to the task, each grounded in the reasoning output, with an initial confidence estimate and a brief rationale.

**Input:** `ReasoningOutput`

**Output:** `HypothesisSet`

**What the agent does:**

The Hypothesize step is the creative branch point of the pipeline. Instead of committing immediately to a single approach, the agent generates a set of candidate solutions — hypotheses — and records them for evaluation. This explicit branching is what allows the system to select the best path rather than the first path.

- **Hypothesis generation strategy:** Based on the intent category and reasoning output, apply the hypothesis generation strategy:
  - **Single-hypothesis mode** (`min_hypotheses == max_hypotheses == 1`): For clearly determined tasks where only one valid approach exists (e.g., a format-conversion task with a defined schema). Skip generation overhead; produce one hypothesis directly.
  - **Bounded generation mode** (default): Generate 2–5 hypotheses representing meaningfully different approaches. Each approach must differ from others in at least one material dimension (e.g., different tool, different algorithm, different output format).
  - **Exhaustive generation mode** (max_hypotheses = unlimited): For high-stakes decisions where completeness matters more than speed. Generate all viable approaches.

- **Hypothesis structure:** Each hypothesis contains:
  - `hypothesis_id`: A unique identifier.
  - `approach`: A plain-language description of the proposed solution path.
  - `rationale`: Why this approach is viable given the reasoning output.
  - `initial_confidence`: A 0.0–1.0 score based on how well the reasoning output supports this approach.
  - `estimated_cost`: Resource cost estimate (time, API calls, memory).
  - `risks`: Known risks or weaknesses in this approach.
  - `prerequisites`: Conditions that must be true for this approach to work.

- **Hypothesis diversity enforcement:** The generator checks that hypotheses are not functionally equivalent. If two hypotheses would produce the same execution steps, they are merged with the higher-confidence one surviving.

- **Baseline hypothesis:** One hypothesis is always generated that represents the simplest possible valid approach — the "do the minimum" fallback. This ensures the Evaluate step always has at least one safe option.

- **Confidence initialization:** Initial confidence is set from the Reason step's confidence estimate, adjusted per hypothesis by: how well the evidence supports *this specific approach*, whether the prerequisites for this approach are confirmed by retrieved context, and estimated risk.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `HypothesisError.NO_VIABLE_HYPOTHESIS` | No approach can satisfy hard constraints | Abort with `ABORT_NO_VIABLE_APPROACH` |
| `HypothesisError.ALL_HYPOTHESES_LOW_CONFIDENCE` | All approaches below minimum confidence | Flag; continue to Evaluate with low-confidence set |
| `HypothesisError.GENERATION_FAILED` | Generator error or timeout | Fall back to baseline hypothesis |

**Memory access:**

- **Reads:** Long-term memory: past successful approaches for similar intents (used to bootstrap generation with proven patterns). `AgentContext.reasoning` (in-context).
- **Writes:** `AgentContext.hypothesis_set`.

**Tool access:** None. Hypothesis generation is an internal cognitive step. No external tools are called.

---

### 2.6 Step 6: Evaluate

**Description:** Score each hypothesis in the `HypothesisSet` against the task's success criteria, resource constraints, and policy rules; flag any policy violations; rank surviving hypotheses.

**Input:** `HypothesisSet` + `Understanding` (for constraints and success criteria)

**Output:** `EvaluatedHypotheses`

**What the agent does:**

The Evaluate step is the system's quality gate before execution. It takes the creative output of Hypothesize and applies rigorous, structured scoring to each candidate.

- **Success criteria scoring:** For each hypothesis, score how well the approach addresses each success criterion identified in the `Understanding` step. Success criteria are scored 0.0–1.0. The weighted average across all criteria gives a `success_score`.

- **Resource constraint evaluation:** Check each hypothesis against resource constraints:
  - Time budget: Will the estimated execution time fit within the task's deadline (if any)?
  - API call budget: Will the estimated tool calls fit within the per-task call limit?
  - Memory budget: Will the intermediate artifacts fit within working memory bounds?
  - Cost budget: If cost tracking is enabled, does the estimated cost fit within budget?
  Hypotheses that violate hard resource constraints are eliminated. Those that violate soft constraints are penalized (score reduction) but not eliminated.

- **Policy rule check:** Run each hypothesis against the AEOS policy engine. Policy rules include:
  - Access control: Does this approach require tools or data sources the agent is not authorized to use?
  - Safety rules: Does this approach risk producing harmful output?
  - Data governance: Does this approach involve data that must not leave the system boundary?
  Hypotheses that violate any hard policy rule are eliminated and marked with `policy_violation = True`. This is a firm gate — policy violations cannot be overridden by confidence scores.

- **Comparative ranking:** After elimination, rank surviving hypotheses by a composite score: `final_score = w_success * success_score + w_confidence * initial_confidence - w_cost * estimated_cost_normalized`. Default weights: `w_success=0.5`, `w_confidence=0.3`, `w_cost=0.2`. Weights are configurable per agent.

- **Minimum survivor check:** If all hypotheses are eliminated, the cognitive cycle aborts with `ABORT_ALL_HYPOTHESES_ELIMINATED`. If only one survives (including the baseline), that hypothesis proceeds without ranking.

- **Evaluation log:** Every scoring decision, resource check, and policy check is recorded in the `EvaluatedHypotheses.evaluation_log`. This log is persisted to the audit trail for post-hoc review.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `EvaluationError.POLICY_ENGINE_UNAVAILABLE` | Policy service unreachable | Abort — cannot proceed without policy validation |
| `EvaluationError.ALL_ELIMINATED` | No hypotheses survive | Abort with `ABORT_ALL_HYPOTHESES_ELIMINATED` |
| `EvaluationError.SCORING_CONFLICT` | Multiple hypotheses tied at max score | Select by secondary criterion (lowest cost) |

**Memory access:**

- **Reads:** Policy rule registry (long-term memory, read-only). Resource quota registry (system memory, read-only). `AgentContext.hypothesis_set` (in-context).
- **Writes:** `AgentContext.evaluated_hypotheses`. Audit log entry.

**Tool access:** Policy engine is invoked as a tool (read-only, sandboxed). This is the one mandatory tool call in the pre-execution phase.

---

### 2.7 Step 7: Plan

**Description:** Select the highest-ranked surviving hypothesis and decompose it into a concrete, ordered `ExecutionPlan` with explicit step dependencies, tool assignments, and expected intermediate outputs.

**Input:** `EvaluatedHypotheses`

**Output:** `ExecutionPlan`

**What the agent does:**

The Plan step bridges intent and action. The winning hypothesis is a high-level description of an approach; the execution plan is the concrete sequence of operations that implements that approach.

- **Hypothesis selection:** Take the top-ranked hypothesis from `EvaluatedHypotheses`. Record the selected hypothesis ID and its final score in `AgentContext`.

- **Step decomposition:** Break the selected approach into atomic, executable steps. Each step is defined by:
  - `step_id`: A unique identifier.
  - `step_type`: One of `TOOL_CALL`, `COMPUTE`, `DECISION`, `AGENT_CALL`, `DATA_TRANSFORM`, `VALIDATION`.
  - `description`: What this step does.
  - `inputs`: Data or artifacts this step consumes (referencing outputs of prior steps by ID).
  - `expected_output`: What this step should produce.
  - `tool_ref`: If `step_type == TOOL_CALL`, which tool to invoke and with what parameters.
  - `timeout_ms`: Maximum allowed time for this step.
  - `retry_policy`: How many times to retry on transient failure.

- **Dependency graph construction:** Determine which steps depend on which. Build a DAG (directed acyclic graph) of step dependencies. Steps with no dependencies can execute in parallel. The plan records the critical path — the longest chain of serial dependencies — which determines the plan's minimum execution time.

- **Contingency steps:** For steps that are likely to fail (e.g., external API calls), include contingency steps: what to do if the step fails after retries. Contingency steps may reference alternative tools or fallback data sources.

- **Plan validation:** Before passing the plan to Execute, validate it:
  - All referenced tool IDs exist in the agent's tool registry.
  - The dependency graph has no cycles.
  - All expected intermediate outputs are typed (output type is declared).
  - The total estimated execution time fits within the task deadline.
  If validation fails, abort with `ABORT_PLAN_INVALID`.

- **Confidence update:** The plan's construction may reveal that some steps are harder or riskier than expected. Update `AgentContext.current_confidence` to reflect any plan-time discoveries.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `PlanningError.DECOMPOSITION_FAILED` | Cannot break hypothesis into atomic steps | Try next-ranked hypothesis; if none, abort |
| `PlanningError.DEPENDENCY_CYCLE` | DAG validation finds a cycle | Attempt to break cycle; abort if unbreakable |
| `PlanningError.TOOL_NOT_AVAILABLE` | Required tool not in agent registry | Try next-ranked hypothesis; if none, abort |
| `PlanningError.PLAN_EXCEEDS_DEADLINE` | Estimated execution time > task deadline | Trim optional steps or abort |

**Memory access:**

- **Reads:** Agent tool registry (long-term memory, read-only). Past execution plans for similar tasks (long-term memory — used to bootstrap step decomposition). `AgentContext.evaluated_hypotheses`.
- **Writes:** `AgentContext.execution_plan`. Plan is checkpointed to short-term memory for recovery purposes.

**Tool access:** None (the Plan step does not call tools — it only assigns tool calls to future steps).

---

### 2.8 Step 8: Execute

**Description:** Carry out each step in the `ExecutionPlan` in dependency order, invoking tools, performing computations, and collecting the result of each step into an `ExecutionResult`.

**Input:** `ExecutionPlan`

**Output:** `ExecutionResult`

**What the agent does:**

Execute is the only step in the cognitive cycle that directly interacts with the external world. All tool calls, API calls, data transformations, and computations happen here.

- **Execution scheduling:** Walk the dependency DAG in topological order. Steps with no unsatisfied dependencies are eligible to run. Where the agent configuration allows parallelism (`allow_parallel_execution = True`), eligible steps are dispatched concurrently. Where parallelism is disabled (default for simple agents), steps run serially.

- **Step execution:** For each step:
  - Load the step's input data by resolving references to prior step outputs.
  - Invoke the designated tool or computation with the specified parameters.
  - Capture the raw result, the execution time, and any tool-returned metadata.
  - Validate the result against the step's declared `expected_output` type.
  - Record the outcome in `ExecutionResult.step_outcomes[step_id]`.

- **Retry handling:** If a step fails with a retryable error (transient network failure, rate limit), retry according to `step.retry_policy`. Record each retry attempt. After max retries, mark the step as `FAILED` and execute the contingency step if one was defined.

- **Hard failure handling:** If a step fails without a contingency and it is on the critical path (i.e., downstream steps depend on it), execution halts for that path. If the step is on a non-critical path, record the failure and continue with other paths.

- **Cost tracking:** For every tool call, record the cost (API credits, compute time, tokens) in `ExecutionResult.cost_breakdown`. Maintain a running total against the task's cost budget. If the running cost exceeds the budget, pause execution and evaluate whether to abort or continue with remaining budget.

- **Side-effect auditing:** Every write operation — to the file system, a database, an external API — is logged to the side-effect audit trail before it is executed. The audit trail entry records: what was written, when, by which step, and what the expected consequence is. This allows recovery and rollback analysis.

- **Intermediate result caching:** Expensive step outputs (large computation results, LLM responses) are cached in short-term memory so that if the Reflect step triggers a partial re-execution, the expensive steps don't need to repeat.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `ExecutionError.TOOL_UNAVAILABLE` | Tool is down or not responding | Execute contingency step; if none, mark path failed |
| `ExecutionError.OUTPUT_TYPE_MISMATCH` | Step output doesn't match declared type | Mark step failed; continue if non-critical |
| `ExecutionError.COST_BUDGET_EXCEEDED` | Running cost exceeds budget | Abort remaining steps; return partial result |
| `ExecutionError.TIMEOUT` | Step exceeds its timeout | Mark step failed; execute contingency |
| `ExecutionError.POLICY_VIOLATION_RUNTIME` | Tool call blocked by runtime policy | Abort immediately; record violation |

**Memory access:**

- **Reads:** Short-term memory: intermediate result cache. Tool configuration (long-term memory). `AgentContext.execution_plan`.
- **Writes:** Intermediate results to short-term memory cache. Side-effect audit log. `AgentContext.execution_result`.

**Tool access:** All tools designated in the `ExecutionPlan` are invoked here. Tool calls are sandboxed per §9. Maximum tool calls per execution are enforced. Tools are invoked with a timeout.

---

### 2.9 Step 9: Reflect

**Description:** Review the `ExecutionResult` against the `ExecutionPlan` and the original `Understanding`; identify gaps, errors, unmet success criteria, and quality issues; decide whether to accept the result or trigger a revision cycle.

**Input:** `ExecutionResult` + `ExecutionPlan` + `Understanding`

**Output:** `ReflectionReport`

**What the agent does:**

Reflect is the agent's self-quality-control step. Without it, the agent blindly returns whatever Execute produced. With it, the agent acts as its own reviewer — catching problems before they leave the cognitive cycle.

- **Plan adherence assessment:** Compare the `ExecutionResult` to the `ExecutionPlan` step by step. For each planned step: Did it complete? Did it produce the expected output type? Did it complete within the allocated time? Flag every deviation as a `PlanDeviation`.

- **Success criteria evaluation:** For each success criterion from the `Understanding` step, assess whether the collected `ExecutionResult` satisfies it. Score each criterion 0.0–1.0. Compute an overall `success_rate`.

- **Self-critique checklist:** Run the 10-question self-critique checklist (defined in §008-REASONING_RUNTIME.md §Self-Critique Checklist). Record the answer to each question in the reflection report. Questions that produce negative answers contribute to the reflection decision.

- **Gap analysis:** Identify specific gaps between what was planned and what was produced. Categorize each gap:
  - `COMPLETENESS_GAP`: A required piece of output is missing.
  - `QUALITY_GAP`: A produced output is present but below quality threshold.
  - `ACCURACY_GAP`: A produced output appears factually incorrect or internally inconsistent.
  - `FORMAT_GAP`: A produced output is correctly computed but wrongly formatted.

- **Reflection verdict:** Based on the self-critique and gap analysis, the Reflect step produces one of three verdicts:
  - `ACCEPT`: The result meets all success criteria. Proceed to Learn.
  - `REVISE`: The result has fixable gaps. Trigger the reflection feedback loop: re-enter the pipeline at the appropriate step (typically Plan or Execute) with the gap analysis as additional context.
  - `ABORT`: The result is fundamentally wrong or the gaps cannot be fixed within remaining resource budget. Abort the cognitive cycle; report the failure.

- **Revision loop guard:** Each time the `REVISE` verdict is reached, a revision counter is incremented. If the counter exceeds `max_reflection_rounds` (default: 3), the verdict is forced to `ABORT` regardless of the gap analysis, to prevent infinite loops.

- **Confidence update:** The reflection step adjusts `AgentContext.current_confidence` based on the `success_rate`. A high `success_rate` increases confidence; a low `success_rate` decreases it.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `ReflectionError.COMPARISON_IMPOSSIBLE` | Execution result too malformed to compare | Verdict = `ABORT` |
| `ReflectionError.MAX_REVISIONS_EXCEEDED` | Revision loop exceeded maximum rounds | Verdict = `ABORT` |
| `ReflectionError.SELF_CRITIQUE_TIMEOUT` | Self-critique checklist took too long | Proceed with partial critique; note in report |

**Memory access:**

- **Reads:** `AgentContext.execution_result`, `AgentContext.execution_plan`, `AgentContext.understanding`. Reviewer agent historical verdicts for this agent (long-term memory — used to calibrate the self-critique).
- **Writes:** `AgentContext.reflection_report`. If verdict is `REVISE`, updates `AgentContext.revision_count`.

**Tool access:** The Reflect step may invoke the `reviewer_agent` as a tool-like sub-agent to obtain an external quality score. This is optional (controlled by `use_external_reviewer` config flag) and subject to the tool call budget.

---

### 2.10 Step 10: Learn

**Description:** Extract generalizable insights from this execution cycle — what worked, what didn't, what would have been a better approach — and package them as `LearningInsight` objects for persistence.

**Input:** `ReflectionReport` + `ExecutionResult` + `ReasoningOutput` + `EvaluatedHypotheses`

**Output:** `LearningInsight` (list)

**What the agent does:**

Learn is the mechanism by which AEOS agents improve over time. Without it, every execution is equally uninformed by experience. With it, the system accumulates structured knowledge that makes future similar tasks faster, more accurate, and more confident.

- **Outcome classification:** Classify the overall execution outcome: `SUCCESS`, `PARTIAL_SUCCESS`, `FAILURE`. This classification drives what kind of insights are extracted.

- **Approach effectiveness insight:** Was the selected hypothesis effective? Did it produce the expected quality? Compare the selected hypothesis's `initial_confidence` to the actual `success_rate`. A significant gap (>0.2) is an indication that hypothesis confidence calibration needs adjustment. Record a `CALIBRATION_INSIGHT`.

- **Execution path insight:** Identify which steps in the execution plan were bottlenecks (slowest), which steps failed and why, and whether the step order was optimal. Record a `PERFORMANCE_INSIGHT`.

- **Domain knowledge insight:** If the task revealed a new fact about the domain that was not in the retrieved context, extract that fact as a `KNOWLEDGE_INSIGHT`. This fact is a candidate for writing to the long-term knowledge base.

- **Heuristic extraction:** If the task reveals a pattern — "for tasks of type X with constraint Y, approach Z always works best" — record it as a `HEURISTIC_INSIGHT`. These heuristics are stored in the agent's long-term memory and consulted during future Hypothesize steps.

- **Anti-pattern recording:** If the task reveals an approach that looked promising but failed, record it as an `ANTI_PATTERN_INSIGHT`. Anti-patterns are consulted during future Evaluate steps to eliminate known-bad approaches quickly.

- **Insight quality filter:** Not all potential insights are worth persisting. Apply a quality filter: an insight must be specific (not vague), generalizable (applicable beyond this exact task), and non-redundant (not already present in long-term memory). Insights that fail the filter are discarded.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `LearningError.EXTRACTION_FAILED` | Cannot extract any insights | Skip learning; continue to Remember |
| `LearningError.INSIGHT_QUALITY_ZERO` | All extracted insights fail quality filter | Skip learning; continue to Remember |

**Memory access:**

- **Reads:** Long-term memory: existing heuristics and anti-patterns (to check for redundancy). `AgentContext` (full context — all steps' outputs are available).
- **Writes:** Writes `LearningInsight` list to `AgentContext.learning_insights`. Does not yet write to long-term memory (that happens in Remember).

**Tool access:** None.

---

### 2.11 Step 11: Remember

**Description:** Write key results, insights, and context from this execution to the appropriate memory tier — ensuring that the agent's future executions benefit from what was learned and that the system maintains an accurate state of the world.

**Input:** `LearningInsight` list + `ExecutionResult` + `ReflectionReport`

**Output:** `MemoryWriteSet` (a manifest of all memory writes performed)

**What the agent does:**

Remember is the consolidation step. It closes the cognitive cycle by committing selected information to persistent memory, making the knowledge available to future tasks and future agents.

- **Write target determination:** For each `LearningInsight` and key result, determine which memory tier is appropriate:
  - **Short-term memory (session scope):** Intermediate results needed by other agents in this session. Current session state. Anything that expires at session end.
  - **Long-term memory (agent scope):** Heuristics, anti-patterns, calibration adjustments. Learned facts about the domain. Agent performance metrics.
  - **Long-term memory (shared scope):** Knowledge facts that are true for the domain regardless of which agent discovered them. These go to the shared knowledge base.
  - **RAG knowledge base:** Significant new domain knowledge suitable for embedding and retrieval by future tasks.

- **Conflict resolution:** Before writing, check if a conflicting entry already exists at the target memory location. If a conflict is found, apply the conflict resolution policy: `OVERWRITE_IF_NEWER`, `MERGE`, `KEEP_BOTH_VERSIONED`, or `REJECT_WRITE`. Record the conflict resolution decision.

- **Serialization:** Convert the data to be written into the storage format for the target memory tier. All writes are tagged with: the writing agent's ID, the source task ID, the confidence of the written fact, and a timestamp.

- **Write execution:** Execute all memory writes. Each write is atomic (all-or-nothing). Failed writes are retried once. If a write fails permanently, record it in `MemoryWriteSet.failed_writes`. Failed writes do not block the cognitive cycle from completing — they are logged for later retry by the memory system.

- **Result packaging:** Package the final result of the cognitive cycle — the `ExecutionResult`, the `ReflectionReport`, and the overall confidence score — into a `TaskResult` object. This is the artifact returned to the caller.

- **Telemetry emission:** Emit a telemetry event summarizing the completed cognitive cycle: agent ID, task ID, step timings, total confidence, memory writes, tool calls made, cost incurred. This event feeds the AEOS observability system.

**Failure modes:**

| Failure | Cause | Handling |
|---------|-------|----------|
| `MemoryError.WRITE_FAILED` | Memory tier unreachable | Log; continue; flag for retry |
| `MemoryError.SERIALIZATION_ERROR` | Data cannot be serialized | Log; skip that specific write |
| `MemoryError.CONFLICT_UNRESOLVABLE` | Conflict resolution policy fails | Log conflict; skip write |

**Memory access:**

- **Reads:** All memory tiers: to check for conflicts before writing.
- **Writes:** All memory tiers: the only step with explicit write authority to all tiers. All writes are recorded in `MemoryWriteSet`.

**Tool access:** None. Memory write operations are internal system calls, not external tools.

---

## 3. Stage Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                    AEOS v2 — 11-STEP COGNITIVE CYCLE                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

  ┌────────┐
  │  RAW   │
  │ INPUT  │ (task: str, context: dict, history: list)
  └───┬────┘
      │
      ▼
  ┌─────────┐     ┌────────────┐     ┌──────────┐     ┌────────┐
  │ OBSERVE │────▶│ UNDERSTAND │────▶│ RETRIEVE │────▶│ REASON │
  └─────────┘     └────────────┘     └──────────┘     └───┬────┘
                                                           │
                                                           ▼
  ┌──────────┐     ┌──────────┐     ┌────────────────┐    │
  │   PLAN   │◀────│ EVALUATE │◀────│  HYPOTHESIZE   │◀───┘
  └────┬─────┘     └──────────┘     └────────────────┘
       │
       ▼
  ┌─────────┐
  │ EXECUTE │
  └────┬────┘
       │
       ▼
  ┌─────────┐   ┌────────────────────────────────────┐
  │ REFLECT │   │  REVISION FEEDBACK LOOP             │
  └────┬────┘   │  (verdict=REVISE → re-enter at     │
       │        │   Plan or Execute with gap context) │
       │        └────────────────────────────────────┘
       │             ↑________________________________│
       │             (reflection verdict = REVISE triggers loop)
       │
       ▼
  ┌────────┐
  │  LEARN │
  └────┬───┘
       │
       ▼
  ┌──────────┐
  │ REMEMBER │
  └────┬─────┘
       │
       ▼
  ┌────────────┐
  │   OUTPUT   │ (TaskResult: ExecutionResult + ReflectionReport + confidence)
  └────────────┘

  ─────────────────────────────────────────────────
  ABORT PATHS (can originate from any step):
  ─────────────────────────────────────────────────
  OBSERVE   ──abort──▶  ABORT_MALFORMED_INPUT
  UNDERSTAND ─abort──▶  ABORT_CONFLICTING_CONSTRAINTS
  HYPOTHESIZE ─abort─▶  ABORT_NO_VIABLE_APPROACH
  EVALUATE  ──abort──▶  ABORT_ALL_HYPOTHESES_ELIMINATED
  PLAN      ──abort──▶  ABORT_PLAN_INVALID
  EXECUTE   ──abort──▶  ABORT_COST_BUDGET_EXCEEDED / ABORT_POLICY_VIOLATION
  REFLECT   ──abort──▶  ABORT_MAX_REVISIONS_EXCEEDED / ABORT_FUNDAMENTAL_FAILURE

  All aborts produce an AbortResult with: abort_code, abort_step,
  partial_results (if any), audit_trail, and recovery_hints.
```

---

## 4. Typed Interface Definitions

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional
from datetime import datetime
import uuid


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class IntentCategory(Enum):
    QUERY           = "query"
    TRANSFORM       = "transform"
    GENERATE        = "generate"
    VALIDATE        = "validate"
    PLAN            = "plan"
    SUMMARIZE       = "summarize"
    ANALYZE         = "analyze"
    AGGREGATE       = "aggregate"
    EXECUTE_ACTION  = "execute_action"
    UNKNOWN         = "unknown"


class ReasoningMode(Enum):
    DEDUCTIVE   = "deductive"
    INDUCTIVE   = "inductive"
    ABDUCTIVE   = "abductive"
    ANALOGICAL  = "analogical"


class MemoryTier(Enum):
    SHORT_TERM          = "short_term"
    LONG_TERM_AGENT     = "long_term_agent"
    LONG_TERM_SHARED    = "long_term_shared"
    RAG_KNOWLEDGE_BASE  = "rag_knowledge_base"


class HypothesisStatus(Enum):
    PENDING     = "pending"
    SELECTED    = "selected"
    ELIMINATED  = "eliminated"
    REJECTED_POLICY = "rejected_policy"


class ReflectionVerdict(Enum):
    ACCEPT  = "accept"
    REVISE  = "revise"
    ABORT   = "abort"


class InsightType(Enum):
    CALIBRATION = "calibration"
    PERFORMANCE = "performance"
    KNOWLEDGE   = "knowledge"
    HEURISTIC   = "heuristic"
    ANTI_PATTERN = "anti_pattern"


class StepType(Enum):
    TOOL_CALL       = "tool_call"
    COMPUTE         = "compute"
    DECISION        = "decision"
    AGENT_CALL      = "agent_call"
    DATA_TRANSFORM  = "data_transform"
    VALIDATION      = "validation"


class StepOutcomeStatus(Enum):
    SUCCESS     = "success"
    FAILED      = "failed"
    SKIPPED     = "skipped"
    RETRIED     = "retried"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 OUTPUT: ObservationContext
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HistoryEntry:
    role:       str          # "user" | "agent" | "system"
    content:    str
    timestamp:  datetime
    agent_id:   Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class ObservationContext:
    """Typed, normalized representation of all raw inputs."""

    observation_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    observed_at:        datetime = field(default_factory=datetime.utcnow)

    # Normalized task
    raw_task:           str = ""
    normalized_task:    str = ""
    task_format:        str = "plain_text"  # "plain_text" | "json_command" | "template"
    task_token_count:   int = 0

    # Context
    context_keys:       list[str] = field(default_factory=list)
    context_values:     dict[str, Any] = field(default_factory=dict)
    session_id:         Optional[str] = None
    caller_agent_id:    Optional[str] = None
    priority:           int = 5          # 1 (highest) – 10 (lowest)
    timeout_budget_ms:  Optional[int] = None

    # History
    history:            list[HistoryEntry] = field(default_factory=list)
    history_truncated:  bool = False
    history_token_count: int = 0

    # Validation flags
    is_valid:           bool = True
    validation_warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 OUTPUT: Understanding
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskConstraint:
    constraint_type:  str      # "hard" | "soft"
    description:      str
    parameter:        Optional[str] = None
    value:            Optional[Any] = None


@dataclass
class TaskEntity:
    entity_type:   str        # "subject" | "object" | "temporal" | "quantitative" | "agent_ref"
    value:         str
    confidence:    float = 1.0
    resolved:      bool = True
    raw_text:      str = ""


@dataclass
class Ambiguity:
    ambiguous_term:   str
    possible_meanings: list[str] = field(default_factory=list)
    resolved:          bool = False
    resolved_to:       Optional[str] = None
    resolution_basis:  Optional[str] = None  # "context" | "history" | "default" | "escalated"


@dataclass
class Understanding:
    """Semantic interpretation of the task."""

    intent:                  IntentCategory = IntentCategory.UNKNOWN
    intent_confidence:       float = 0.0
    entities:                list[TaskEntity] = field(default_factory=list)
    hard_constraints:        list[TaskConstraint] = field(default_factory=list)
    soft_constraints:        list[TaskConstraint] = field(default_factory=list)
    success_criteria:        list[str] = field(default_factory=list)
    ambiguities:             list[Ambiguity] = field(default_factory=list)
    unresolved_ambiguities:  list[Ambiguity] = field(default_factory=list)
    requires_decomposition:  bool = False
    scope_assessment:        str = "atomic"    # "atomic" | "composite"
    escalation_required:     bool = False
    escalation_reason:       Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 OUTPUT: RetrievedContext
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MemoryItem:
    item_id:          str
    content:          Any
    source_tier:      MemoryTier
    source_key:       str
    relevance_score:  float
    confidence:       float
    timestamp:        datetime
    provenance:       Optional[str] = None


@dataclass
class KnowledgeChunk:
    chunk_id:         str
    content:          str
    source:           str
    similarity_score: float
    confidence:       float
    retrieved_at:     datetime = field(default_factory=datetime.utcnow)


@dataclass
class SourceConflict:
    fact:             str
    source_a:         MemoryTier
    value_a:          Any
    source_b:         MemoryTier
    value_b:          Any
    resolution:       str   # "short_term_wins" | "manual_adjudication_required"


@dataclass
class RetrievedContext:
    """Merged, deduplicated results from all memory tiers and RAG."""

    short_term_items:     list[MemoryItem] = field(default_factory=list)
    long_term_items:      list[MemoryItem] = field(default_factory=list)
    knowledge_chunks:     list[KnowledgeChunk] = field(default_factory=list)
    total_items:          int = 0
    conflicts:            list[SourceConflict] = field(default_factory=list)
    retrieval_budget_used: int = 0  # tokens
    retrieval_budget_max:  int = 0
    short_term_available:  bool = True
    long_term_available:   bool = True
    rag_available:         bool = True


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 OUTPUT: ReasoningOutput
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractedFact:
    fact_type:   str    # "assertion" | "constraint" | "example" | "counter_example" | "unknown"
    content:     str
    source_item_id: Optional[str] = None
    confidence:  float = 1.0


@dataclass
class ThoughtStep:
    step_number:       int
    reasoning_mode:    ReasoningMode
    inputs_considered: list[str]
    conclusion:        str
    confidence:        float
    alternatives_rejected: list[str] = field(default_factory=list)


@dataclass
class ReasoningOutput:
    """Structured analysis of the task grounded in retrieved evidence."""

    key_question:         str = ""
    extracted_facts:      list[ExtractedFact] = field(default_factory=list)
    reasoning_mode:       ReasoningMode = ReasoningMode.DEDUCTIVE
    analysis:             str = ""
    evidence_supports:    list[str] = field(default_factory=list)
    evidence_gaps:        list[str] = field(default_factory=list)
    contradictions:       list[str] = field(default_factory=list)
    thought_chain:        list[ThoughtStep] = field(default_factory=list)
    initial_confidence:   float = 0.5
    domain_match:         bool = True


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 OUTPUT: HypothesisSet
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Hypothesis:
    hypothesis_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    approach:           str = ""
    rationale:          str = ""
    initial_confidence: float = 0.5
    estimated_cost:     float = 0.0   # normalized 0.0–1.0
    risks:              list[str] = field(default_factory=list)
    prerequisites:      list[str] = field(default_factory=list)
    is_baseline:        bool = False
    status:             HypothesisStatus = HypothesisStatus.PENDING


@dataclass
class HypothesisSet:
    """Candidate approaches generated for the task."""

    hypotheses:           list[Hypothesis] = field(default_factory=list)
    generation_strategy:  str = "bounded"   # "single" | "bounded" | "exhaustive"
    generation_count:     int = 0


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 OUTPUT: EvaluatedHypotheses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HypothesisScore:
    hypothesis_id:      str
    success_score:      float
    resource_penalty:   float
    policy_violation:   bool
    policy_violations:  list[str] = field(default_factory=list)
    final_score:        float = 0.0
    eliminated:         bool = False
    elimination_reason: Optional[str] = None


@dataclass
class EvaluatedHypotheses:
    """Scored and ranked hypothesis set after policy and resource checks."""

    scores:             list[HypothesisScore] = field(default_factory=list)
    ranked_survivors:   list[str] = field(default_factory=list)   # hypothesis_ids, best first
    winner_id:          Optional[str] = None
    evaluation_log:     list[str] = field(default_factory=list)
    all_eliminated:     bool = False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 OUTPUT: ExecutionPlan
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    step_id:          str = field(default_factory=lambda: str(uuid.uuid4()))
    step_type:        StepType = StepType.COMPUTE
    description:      str = ""
    depends_on:       list[str] = field(default_factory=list)   # step_ids
    inputs:           dict[str, Any] = field(default_factory=dict)
    expected_output:  str = ""           # type name or description
    tool_ref:         Optional[str] = None
    tool_params:      dict[str, Any] = field(default_factory=dict)
    timeout_ms:       int = 30_000
    retry_policy:     dict[str, Any] = field(default_factory=lambda: {"max_retries": 2, "backoff_ms": 500})
    contingency_step_id: Optional[str] = None
    is_critical_path: bool = False


@dataclass
class ExecutionPlan:
    """Ordered, dependency-linked execution steps."""

    selected_hypothesis_id: str = ""
    steps:                  list[PlanStep] = field(default_factory=list)
    critical_path:          list[str] = field(default_factory=list)   # step_ids
    estimated_total_time_ms: int = 0
    estimated_total_cost:   float = 0.0
    allow_parallel:         bool = False
    is_valid:               bool = True
    validation_errors:      list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 OUTPUT: ExecutionResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StepOutcome:
    step_id:          str
    status:           StepOutcomeStatus
    output:           Any = None
    output_type:      str = ""
    execution_time_ms: int = 0
    retry_count:      int = 0
    error:            Optional[str] = None
    tool_call_id:     Optional[str] = None
    cost_incurred:    float = 0.0


@dataclass
class SideEffectRecord:
    step_id:          str
    operation:        str    # "write" | "delete" | "update"
    target:           str    # file path, DB key, API endpoint
    executed_at:      datetime = field(default_factory=datetime.utcnow)
    reversible:       bool = False
    rollback_ref:     Optional[str] = None


@dataclass
class ExecutionResult:
    """Step-by-step outcomes of the execution phase."""

    step_outcomes:    dict[str, StepOutcome] = field(default_factory=dict)
    final_output:     Any = None
    final_output_type: str = ""
    side_effects:     list[SideEffectRecord] = field(default_factory=list)
    cost_breakdown:   dict[str, float] = field(default_factory=dict)
    total_cost:       float = 0.0
    total_time_ms:    int = 0
    is_complete:      bool = False
    failed_steps:     list[str] = field(default_factory=list)
    skipped_steps:    list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 OUTPUT: ReflectionReport
# ─────────────────────────────────────────────────────────────────────────────

class GapType(Enum):
    COMPLETENESS    = "completeness_gap"
    QUALITY         = "quality_gap"
    ACCURACY        = "accuracy_gap"
    FORMAT          = "format_gap"


@dataclass
class OutputGap:
    gap_type:     GapType
    description:  str
    step_id:      Optional[str] = None
    severity:     str = "medium"    # "low" | "medium" | "high" | "blocking"


@dataclass
class SelfCritiqueAnswer:
    question_id:  int     # 1–10 (see §008-REASONING_RUNTIME.md)
    question:     str
    answer:       str
    is_concern:   bool = False


@dataclass
class ReflectionReport:
    """Self-assessment of execution quality vs. plan and success criteria."""

    plan_adherence_score:     float = 1.0   # 0.0–1.0
    success_rate:             float = 0.0   # 0.0–1.0
    plan_deviations:          list[str] = field(default_factory=list)
    output_gaps:              list[OutputGap] = field(default_factory=list)
    self_critique:            list[SelfCritiqueAnswer] = field(default_factory=list)
    verdict:                  ReflectionVerdict = ReflectionVerdict.ACCEPT
    revision_target_step:     Optional[str] = None  # step to re-enter if REVISE
    revision_context:         dict[str, Any] = field(default_factory=dict)
    revision_count:           int = 0
    adjusted_confidence:      float = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# STEP 10 OUTPUT: LearningInsight
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LearningInsight:
    insight_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    insight_type:   InsightType = InsightType.KNOWLEDGE
    content:        str = ""
    context:        dict[str, Any] = field(default_factory=dict)
    confidence:     float = 0.5
    generalizable:  bool = True
    applies_to:     list[str] = field(default_factory=list)   # intent categories
    derived_from:   str = ""    # task_id that produced this insight
    quality_score:  float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# STEP 11 OUTPUT: MemoryWriteSet
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MemoryWrite:
    target_tier:      MemoryTier
    target_key:       str
    value:            Any
    ttl_seconds:      Optional[int] = None  # None = permanent
    confidence:       float = 1.0
    source_task_id:   str = ""
    conflict_policy:  str = "overwrite_if_newer"
    status:           str = "pending"   # "pending" | "success" | "failed"


@dataclass
class MemoryWriteSet:
    """Manifest of all memory writes performed in the Remember step."""

    writes:          list[MemoryWrite] = field(default_factory=list)
    successful:      int = 0
    failed:          int = 0
    failed_writes:   list[MemoryWrite] = field(default_factory=list)
    conflicts_found: int = 0
    conflicts_resolved: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# AGENT CONTEXT: The accumulating state object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """
    Flows through all 11 steps, accumulating typed outputs.
    Passed by reference; each step mutates the relevant field.
    """

    # Identity
    task_id:            str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id:           str = ""
    agent_version:      str = ""
    started_at:         datetime = field(default_factory=datetime.utcnow)

    # Step outputs (populated as the cycle progresses)
    observation:        Optional[ObservationContext] = None
    understanding:      Optional[Understanding] = None
    retrieved_context:  Optional[RetrievedContext] = None
    reasoning:          Optional[ReasoningOutput] = None
    hypothesis_set:     Optional[HypothesisSet] = None
    evaluated_hypotheses: Optional[EvaluatedHypotheses] = None
    execution_plan:     Optional[ExecutionPlan] = None
    execution_result:   Optional[ExecutionResult] = None
    reflection_report:  Optional[ReflectionReport] = None
    learning_insights:  list[LearningInsight] = field(default_factory=list)
    memory_write_set:   Optional[MemoryWriteSet] = None

    # Tracking
    current_step:       int = 0
    current_confidence: float = 0.5
    revision_count:     int = 0
    thought_log:        list[ThoughtStep] = field(default_factory=list)
    abort_code:         Optional[str] = None
    abort_step:         Optional[str] = None
    is_aborted:         bool = False

    # Configuration overrides (per-task)
    config_overrides:   dict[str, Any] = field(default_factory=dict)
```

---

## 5. Cognitive Cycle Composition

### 5.1 Sequential Pipeline (Default)

In the default configuration, the 11 steps execute strictly in order. Each step receives the output of the previous step. The `AgentContext` is updated after each step completes.

```
step_1_result = observe(raw_input, context)
step_2_result = understand(step_1_result)
step_3_result = retrieve(step_2_result)
step_4_result = reason(step_2_result, step_3_result)
step_5_result = hypothesize(step_4_result)
step_6_result = evaluate(step_5_result, step_2_result)
step_7_result = plan(step_6_result)
step_8_result = execute(step_7_result)
step_9_result = reflect(step_8_result, step_7_result, step_2_result)
step_10_result = learn(step_9_result, step_8_result, step_4_result, step_6_result)
step_11_result = remember(step_10_result, step_8_result, step_9_result)
```

### 5.2 Iterative Mode (Feedback Loops)

The pipeline supports two feedback loops that make the cycle iterative:

**Loop A — Reflection Feedback Loop:**
When `ReflectionReport.verdict == REVISE`, the cycle re-enters at the step specified by `revision_target_step`. Common re-entry points:
- `Plan` — when the plan was correct but the execution failed; re-plan with updated context.
- `Execute` — when only a subset of steps failed; re-execute failed steps with revised parameters.
- `Hypothesize` — when the reflection reveals the selected approach was fundamentally wrong; try the next-ranked hypothesis.
- `Retrieve` — when the reflection reveals that important context was not retrieved; re-retrieve with expanded query.

The revision count is incremented on each loop. If `revision_count > max_reflection_rounds`, the loop is broken and the cycle aborts.

**Loop B — Confidence Escalation Loop:**
If `current_confidence` drops below `min_acceptable_confidence` at the end of the Reason step, the agent may optionally return to the Retrieve step with a refined query to gather more evidence. This loop is bounded by `max_retrieval_rounds` (default: 2).

### 5.3 Parallel Execution Within Execute

The Execute step itself supports internal parallelism when `ExecutionPlan.allow_parallel = True`. Within a single cognitive cycle, multiple plan steps with no mutual dependencies can be dispatched concurrently. This is controlled by the agent's `max_parallel_steps` configuration parameter (default: 1, meaning no parallelism).

---

## 6. Abort Conditions

The cognitive cycle terminates early — before completing all 11 steps — when one of the following abort conditions is triggered:

| Abort Code | Triggering Step | Condition | Recovery Hint |
|------------|-----------------|-----------|---------------|
| `ABORT_MALFORMED_INPUT` | Observe | Task is null, empty, or unrecoverable encoding error | Caller should validate input before sending |
| `ABORT_CONFLICTING_CONSTRAINTS` | Understand | Two hard constraints are mutually exclusive | Caller should revise task constraints |
| `ABORT_NO_VIABLE_APPROACH` | Hypothesize | No hypothesis can satisfy all hard constraints | Relax constraints or use a different agent |
| `ABORT_ALL_HYPOTHESES_ELIMINATED` | Evaluate | All hypotheses eliminated by policy or resource checks | Check policy configuration; expand resource budgets |
| `ABORT_POLICY_VIOLATION` | Evaluate or Execute | Hard policy rule violated | Review policy rules; request explicit exception |
| `ABORT_PLAN_INVALID` | Plan | Plan validation fails (cycle, missing tool, etc.) | Inspect plan validation errors; fix tool registry |
| `ABORT_COST_BUDGET_EXCEEDED` | Execute | Running cost exceeds task budget | Increase budget or simplify task |
| `ABORT_TIMEOUT` | Execute | Total task timeout exceeded | Increase timeout or reduce task scope |
| `ABORT_MAX_REVISIONS_EXCEEDED` | Reflect | Revision loop count exceeds `max_reflection_rounds` | Increase rounds or simplify task |
| `ABORT_FUNDAMENTAL_FAILURE` | Reflect | Execution result is fundamentally wrong, unfixable | Reassign task to a different agent type |
| `ABORT_MEMORY_CRITICAL_FAILURE` | Remember | Memory write to required tier fails permanently | Check memory system health |

**Abort result structure:**

All aborts produce an `AbortResult` containing:
- `abort_code`: The abort code from the table above.
- `abort_step`: The name of the step where abort was triggered.
- `partial_results`: Any useful partial results produced before the abort.
- `audit_trail`: The complete history of steps executed and their outcomes.
- `recovery_hints`: Specific, actionable suggestions for resolving the abort condition.
- `context_snapshot`: A snapshot of the `AgentContext` at the time of abort.

---

## 7. Agent Context Propagation

The `AgentContext` object is created once at the beginning of the cognitive cycle and passed (by reference) to all 11 steps. Each step is responsible for:

1. Reading from `AgentContext` the outputs of prior steps it needs.
2. Writing its own output to the designated `AgentContext` field.
3. Updating `AgentContext.current_step` and `AgentContext.current_confidence` when appropriate.

**No step may modify the output of a prior step.** Each step's output field in `AgentContext` is write-once (enforced by the runtime). This ensures full auditability: the complete trace of the cognitive cycle is preserved in the `AgentContext` at cycle completion.

**Context serialization:** The `AgentContext` is serializable to JSON at any point. This enables:
- Checkpointing: Save context mid-cycle for recovery after crashes.
- Debugging: Inspect the full context state at any step.
- Replay: Re-run from any step by restoring the context to that step's state.
- Tracing: Export the full context as an observability trace.

**Context size management:** As the cycle progresses, `AgentContext` grows. After the Execute step, the runtime may compact the context by replacing large intermediate objects (e.g., full retrieval results) with summaries. Compaction is logged in `AgentContext.compact_log` and is irreversible within the current cycle.

---

## 8. Memory Access Per Stage

| Step         | Short-Term Read | Short-Term Write | Long-Term Read | Long-Term Write | RAG Read | RAG Write |
|--------------|:--------------:|:----------------:|:--------------:|:---------------:|:--------:|:---------:|
| 1. Observe   |       —        |       ✓          |       —        |        —        |    —     |     —     |
| 2. Understand|       ✓        |       ✓          |       ✓ (entity registry) |  —  |    —     |     —     |
| 3. Retrieve  |       ✓        |       —          |       ✓        |        —        |    ✓     |     —     |
| 4. Reason    |       ✓        |       ✓ (thought log) |  ✓ (rules) |     —        |    —     |     —     |
| 5. Hypothesize|      ✓        |       ✓          |       ✓ (past approaches) | —  |    —     |     —     |
| 6. Evaluate  |       ✓        |       ✓ (audit)  |       ✓ (policy, quotas) | — |   —     |     —     |
| 7. Plan      |       ✓        |       ✓ (checkpoint) | ✓ (tool registry, past plans) | — | — |   —     |
| 8. Execute   |       ✓        |       ✓ (cache, audit) | ✓ (tool config) | —      |    —     |     —     |
| 9. Reflect   |       ✓        |       ✓          |       ✓ (reviewer history) | — |  —     |     —     |
| 10. Learn    |       ✓        |       —          |       ✓ (existing insights) | — |  —     |     —     |
| 11. Remember |       ✓        |       ✓          |       ✓        |        ✓        |    —     |     ✓     |

**Key observations:**
- Only step 11 (Remember) writes to long-term memory or RAG. All other steps that modify state do so only in the session's short-term memory or the in-context `AgentContext`.
- Step 3 (Retrieve) is the sole step that reads from all three source types simultaneously.
- Step 6 (Evaluate) is the only pre-execution step that writes to an external audit log.

---

## 9. Tool Invocation Rules

### 9.1 Which Steps Can Invoke Tools

| Step          | Tool Invocation | Mandatory | Notes |
|---------------|:--------------:|:---------:|-------|
| 1. Observe    | Prohibited     | —         | Reception only; no external calls |
| 2. Understand | Optional       | No        | LLM call when LLM-backed mode enabled |
| 3. Retrieve   | Required       | Yes       | Memory queries and RAG are always tool calls |
| 4. Reason     | Optional       | No        | LLM call when LLM-backed mode enabled |
| 5. Hypothesize | Prohibited    | —         | Internal generation only |
| 6. Evaluate   | Required       | Yes       | Policy engine call is mandatory |
| 7. Plan       | Prohibited     | —         | Planning is internal |
| 8. Execute    | Required       | Yes       | All designated plan tools are called here |
| 9. Reflect    | Optional       | No        | May call reviewer_agent sub-agent |
| 10. Learn     | Prohibited     | —         | Internal analysis only |
| 11. Remember  | Prohibited     | —         | Memory writes are internal system calls |

### 9.2 Sandboxing

All tool invocations are executed in a sandboxed context with the following constraints:

- **Network access:** Tool calls that make outbound network requests must be pre-approved in the agent's tool registry. Unapproved outbound calls are blocked at the sandbox level.
- **File system access:** Tools may read from designated read-only paths. Write access is restricted to the agent's designated scratch space. Writes outside the scratch space require explicit policy permission.
- **Execution time:** Every tool call has a timeout (`step.timeout_ms`). Tool calls that exceed their timeout are killed and the step is marked `FAILED`.
- **Side-effect rollback:** For tools with reversible side effects (e.g., file writes), the sandbox records a rollback reference. If the overall cognitive cycle aborts after such a tool call, the side effects can optionally be rolled back.

### 9.3 Cost Tracking

Every tool invocation contributes to the task's cost budget. Cost is tracked in two dimensions:
- **API credits:** For LLM calls and external service calls.
- **Compute time:** For local computation steps.

The running cost is maintained in `ExecutionResult.cost_breakdown` and checked against the task budget after each step in the Execute phase. If the budget is exhausted, the cycle is aborted with `ABORT_COST_BUDGET_EXCEEDED`.

### 9.4 Tool Registry

The agent tool registry (stored in long-term memory) records:
- Tool ID and version.
- Tool type (LLM, external API, local compute, sub-agent).
- Authorization level required.
- Cost estimate per invocation.
- Timeout default.
- Retry policy.

Tools not in the registry cannot be invoked. Attempts to invoke unregistered tools raise a `ToolNotRegisteredError` and are blocked.

---

## 10. Confidence Thresholds

Confidence values range from 0.0 to 1.0. The following thresholds are defaults; each agent may override them in its configuration.

| Stage        | Threshold Parameter               | Default | Meaning |
|--------------|-----------------------------------|---------|---------|
| Understand   | `min_intent_confidence`           | 0.50    | Below this, intent is set to UNKNOWN and escalation is considered |
| Retrieve     | `min_retrieval_relevance`         | 0.65    | Items below this score are discarded |
| Reason       | `min_reasoning_confidence`        | 0.35    | Below this, the Hypothesize step is warned; all hypotheses get a confidence penalty |
| Hypothesize  | `min_hypothesis_confidence`       | 0.20    | Below this, a hypothesis is marked "speculative" and scored down in Evaluate |
| Evaluate     | `min_winning_hypothesis_score`    | 0.40    | Below this, the plan is not trusted; extra reflection rounds are required |
| Reflect      | `accept_success_rate`             | 0.80    | Above this, verdict = ACCEPT |
| Reflect      | `revise_success_rate`             | 0.50    | Between this and accept threshold = REVISE |
| Reflect      | `abort_success_rate`              | 0.50    | Below this, verdict = ABORT |
| Output       | `min_output_confidence`           | 0.60    | Final output confidence reported to caller |

**Confidence propagation:** The `current_confidence` in `AgentContext` is updated at three key points:
1. After Reason — set to `ReasoningOutput.initial_confidence`.
2. After Evaluate — adjusted by the winning hypothesis's `final_score`.
3. After Reflect — set to `ReflectionReport.adjusted_confidence`.

The final reported confidence is the value of `current_confidence` after the Remember step. See §008-REASONING_RUNTIME.md §Confidence Scoring for full confidence mechanics.

---

## 11. Agent Specialization

The 6 existing v1 agents map to the 11-step model with different emphasis profiles. "Emphasis" means the agent's implementation of that step is more sophisticated than the default.

| Agent             | Primary Emphasis Steps | Secondary Steps | Minimal Steps |
|-------------------|------------------------|-----------------|---------------|
| `simple_agent`    | 1 (Observe), 2 (Understand) | 4 (Reason) | 5, 6, 7, 10 |
| `planner_agent`   | 5 (Hypothesize), 7 (Plan) | 2 (Understand), 6 (Evaluate) | 8 (minimal — delegates execute) |
| `research_agent`  | 3 (Retrieve), 4 (Reason) | 11 (Remember — writes to RAG) | 5, 7 |
| `reviewer_agent`  | 9 (Reflect), 6 (Evaluate) | 2 (Understand) | 5, 7, 8 |
| `analyst_agent`   | 4 (Reason), 5 (Hypothesize), 9 (Reflect) | 3 (Retrieve) | 8 (delegates) |
| `executor_agent`  | 8 (Execute), 7 (Plan) | 9 (Reflect) | 5, 10 |

**Specialization does not mean skipping steps.** All 11 steps run for every agent. Specialization means that in the emphasized steps, the agent uses a richer implementation (e.g., the `planner_agent`'s Plan step decomposes tasks into multi-agent DAGs, not just single-agent steps). In minimal steps, the agent uses the simplest valid implementation (e.g., the `simple_agent`'s Hypothesize step always produces exactly one hypothesis).

---

## 12. Migration Guide

This section provides a step-by-step guide for migrating an existing v1 `think()/act()` agent to the 11-step model.

### Step 1: Extract the think() logic

Read the existing `think()` method and identify which cognitive operations it performs. Common patterns:

| think() pattern | Maps to step |
|-----------------|-------------|
| "Parse the task string and extract key terms" | Step 1 (Observe) + Step 2 (Understand) |
| "Look up relevant context from the knowledge base" | Step 3 (Retrieve) |
| "Apply rules to determine the best approach" | Step 4 (Reason) |
| "Decide between option A and option B" | Step 5 (Hypothesize) + Step 6 (Evaluate) |

### Step 2: Extract the act() logic

Read the existing `act()` method. Common patterns:

| act() pattern | Maps to step |
|---------------|-------------|
| "Build a list of execution steps" | Step 7 (Plan) |
| "Call the summarize tool" | Step 8 (Execute) |
| "Check if the output looks right" | Step 9 (Reflect) |
| "Return the result" | Step 11 (Remember) → caller receives TaskResult |

### Step 3: Create the AgentContext

Replace the ad-hoc `thought: str` string with an `AgentContext` object. Map each piece of data previously carried in `context: dict` to the appropriate typed field in `AgentContext`.

### Step 4: Implement each step as a method

Convert each logical block identified above into its own method:

```python
class MyAgentV2(BaseAgentV2):

    def observe(self, raw: RawTaskInput, ctx: AgentContext) -> ObservationContext: ...
    def understand(self, obs: ObservationContext, ctx: AgentContext) -> Understanding: ...
    def retrieve(self, und: Understanding, ctx: AgentContext) -> RetrievedContext: ...
    def reason(self, und: Understanding, ret: RetrievedContext, ctx: AgentContext) -> ReasoningOutput: ...
    def hypothesize(self, rsn: ReasoningOutput, ctx: AgentContext) -> HypothesisSet: ...
    def evaluate(self, hyp: HypothesisSet, und: Understanding, ctx: AgentContext) -> EvaluatedHypotheses: ...
    def plan(self, eval: EvaluatedHypotheses, ctx: AgentContext) -> ExecutionPlan: ...
    def execute(self, plan: ExecutionPlan, ctx: AgentContext) -> ExecutionResult: ...
    def reflect(self, result: ExecutionResult, plan: ExecutionPlan, und: Understanding, ctx: AgentContext) -> ReflectionReport: ...
    def learn(self, report: ReflectionReport, result: ExecutionResult, rsn: ReasoningOutput, eval: EvaluatedHypotheses, ctx: AgentContext) -> list[LearningInsight]: ...
    def remember(self, insights: list[LearningInsight], result: ExecutionResult, report: ReflectionReport, ctx: AgentContext) -> MemoryWriteSet: ...
```

### Step 5: Wire the cognitive cycle

The `BaseAgentV2` class provides a `run_cognitive_cycle(raw: RawTaskInput) -> TaskResult` method that calls each step in order, manages the `AgentContext`, handles abort conditions, and manages the reflection feedback loop. You do not implement the orchestration — only the individual step methods.

### Step 6: Update tests

v1 tests typically call `think()` and `act()` directly. Replace these with tests for individual step methods using the typed input/output interfaces. Add integration tests that call `run_cognitive_cycle()` end-to-end.

### Step 7: Remove the v1 shim

Once the v2 implementation passes all tests and the agent is deployed, remove the v1 compatibility shim (see §13).

---

## 13. v1 Compatibility

During the transition period, v1 and v2 agents coexist in the AEOS system. The `BaseAgentV2` class provides a compatibility shim that maps v1 method calls to the 11-step model:

```python
class BaseAgentV2(BaseAgent):
    """
    Compatibility shim: v1 think()/act() calls are mapped to the
    11-step cognitive cycle as follows:

    think(task: str) → str
        Executes steps 1–4 (Observe, Understand, Retrieve, Reason)
        Returns ReasoningOutput serialized to a string (JSON)

    act(thought: str, context: dict) → Any
        Parses the thought string as ReasoningOutput JSON
        Executes steps 5–8 (Hypothesize, Evaluate, Plan, Execute)
        Skips steps 9–11 (no reflection, no learning, no memory write)
        Returns ExecutionResult.final_output

    Note: When using the shim, the agent does NOT benefit from
    reflection, learning, or memory consolidation. Migrate to
    run_cognitive_cycle() to access the full 11-step model.
    """

    def think(self, task: str) -> str:
        raw = RawTaskInput(task=task, context={}, history=[])
        ctx = AgentContext(agent_id=self.agent_id)
        obs = self.observe(raw, ctx)
        und = self.understand(obs, ctx)
        ret = self.retrieve(und, ctx)
        rsn = self.reason(und, ret, ctx)
        return rsn.to_json()  # serialized for v1 compatibility

    def act(self, thought: str, context: dict) -> Any:
        rsn = ReasoningOutput.from_json(thought)
        ctx = AgentContext(agent_id=self.agent_id)
        ctx.reasoning = rsn
        hyp = self.hypothesize(rsn, ctx)
        und = Understanding()  # minimal; context dict not fully parsed
        evl = self.evaluate(hyp, und, ctx)
        pln = self.plan(evl, ctx)
        res = self.execute(pln, ctx)
        return res.final_output
```

**v1 method call mapping table:**

| v1 Call | v2 Steps Executed | Steps Skipped |
|---------|-------------------|---------------|
| `think(task)` | 1, 2, 3, 4 | 5, 6, 7, 8, 9, 10, 11 |
| `act(thought, context)` | 5, 6, 7, 8 | 1, 2, 3, 4, 9, 10, 11 |
| `think()` + `act()` together | 1–8 | 9, 10, 11 |
| `run_cognitive_cycle()` | 1–11 | (none) |

The shim is intended for backward compatibility only. It is not the recommended execution path and will be deprecated in AEOS v3.

---

## 14. Cross-References

| Document | Relationship |
|----------|-------------|
| **001-ARCHITECTURE.md** | System-level architecture; defines AEOS modules and their relationships. The Agent Runtime is the execution layer described in §3.2 of that document. |
| **008-REASONING_RUNTIME.md** | Defines the Reasoning Runtime sub-system invoked by steps 4 (Reason) and 9 (Reflect). Specifies reasoning modes, Chain-of-Thought protocol, confidence scoring, and the self-critique checklist referenced in §2.9 of this document. |
| **009-MEMORY_SYSTEM.md** | Defines all memory tiers, their storage mechanisms, TTL policies, and conflict resolution strategies. The memory access patterns in §8 of this document are governed by that specification. |

---

*End of 007 — AGENT RUNTIME v1.0.0*
