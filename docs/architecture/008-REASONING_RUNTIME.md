# 008 — REASONING RUNTIME

| Field       | Value                                               |
|-------------|-----------------------------------------------------|
| Status      | Approved                                            |
| Version     | 1.0.0                                               |
| Created     | 2026-07-05                                          |
| Authors     | AEOS Architecture Team                              |
| Supersedes  | (none — new document)                               |
| See Also    | 007-AGENT_RUNTIME.md, 001-ARCHITECTURE.md           |

---

## Abstract

This document defines the **AEOS Reasoning Runtime** — the sub-system responsible for the quality, consistency, and observability of agent cognition. The Reasoning Runtime is not an agent. It does not execute tasks. It is a library of reasoning primitives, protocols, and quality mechanisms that agents invoke during their cognitive cycles.

The Reasoning Runtime provides: four distinct reasoning modes with defined semantics, a structured Chain-of-Thought protocol, a formal Reflection Protocol with a self-critique checklist, a complete confidence scoring specification, uncertainty propagation rules, integration hooks for the reviewer agent, a clear upgrade path from rule-based to LLM-backed reasoning, and an observability layer that makes every reasoning step traceable.

Any agent that participates in the AEOS cognitive pipeline must use the Reasoning Runtime for its reasoning operations. Ad-hoc, non-standard reasoning is not permitted in production agents.

---

## Table of Contents

1. [What is the Reasoning Runtime?](#1-what-is-the-reasoning-runtime)
2. [Why a Dedicated Reasoning Runtime?](#2-why-a-dedicated-reasoning-runtime)
3. [Four Reasoning Modes](#3-four-reasoning-modes)
   - 3.1 [Deductive Reasoning](#31-deductive-reasoning)
   - 3.2 [Inductive Reasoning](#32-inductive-reasoning)
   - 3.3 [Abductive Reasoning](#33-abductive-reasoning)
   - 3.4 [Analogical Reasoning](#34-analogical-reasoning)
4. [Chain-of-Thought Protocol](#4-chain-of-thought-protocol)
5. [Reflection Protocol](#5-reflection-protocol)
6. [Self-Critique Checklist](#6-self-critique-checklist)
7. [Confidence Scoring](#7-confidence-scoring)
8. [Uncertainty Propagation](#8-uncertainty-propagation)
9. [Integration with the Reviewer Agent](#9-integration-with-the-reviewer-agent)
10. [LLM-Backed vs. Rule-Based Reasoning](#10-llm-backed-vs-rule-based-reasoning)
11. [Observability](#11-observability)
12. [Cross-References](#12-cross-references)

---

## 1. What is the Reasoning Runtime?

The Reasoning Runtime is a shared cognitive infrastructure library. It sits below the agent layer and above the tool/memory layer in the AEOS architecture stack:

```
┌─────────────────────────────────────────────────────┐
│                   AGENT LAYER                       │
│  (simple_agent, planner_agent, research_agent, ...) │
├─────────────────────────────────────────────────────┤
│              REASONING RUNTIME                      │  ◀── this document
│  Modes | Chain-of-Thought | Reflection | Confidence │
├─────────────────────────────────────────────────────┤
│          TOOL & MEMORY LAYER                        │
│  (memory tiers, RAG, external tools, policy engine) │
└─────────────────────────────────────────────────────┘
```

Agents do not implement their own reasoning logic. They call Reasoning Runtime primitives. This has several consequences:

- **Uniformity:** All agents reason using the same vocabulary of modes, protocols, and scoring methods. A `DEDUCTIVE` reasoning step in `simple_agent` is structurally identical to one in `analyst_agent`.
- **Replaceability:** The Reasoning Runtime implementation can be upgraded (e.g., from rule-based to LLM-backed) without modifying agents.
- **Observability:** Because reasoning goes through a single runtime, every reasoning operation can be traced, logged, and analyzed from a central point.
- **Testability:** Reasoning logic can be tested independently of agent behavior. A failing reasoning mode can be diagnosed and fixed without touching any agent.

### 1.1 Runtime Components

The Reasoning Runtime consists of five components:

1. **Mode Dispatcher:** Routes reasoning requests to the appropriate mode implementation.
2. **Thought Builder:** Constructs structured Chain-of-Thought records.
3. **Reflection Engine:** Manages reflection rounds, runs the self-critique checklist, and determines verdicts.
4. **Confidence Calculator:** Computes, propagates, and aggregates confidence scores.
5. **Observability Emitter:** Records all reasoning events to the trace log.

### 1.2 Runtime API Surface (Summary)

```python
class ReasoningRuntime:
    def reason(
        self,
        mode: ReasoningMode,
        facts: list[ExtractedFact],
        question: str,
        context: AgentContext
    ) -> ReasoningOutput: ...

    def build_thought(
        self,
        step: int,
        mode: ReasoningMode,
        inputs: list[str],
        conclusion: str,
        confidence: float,
        rejected: list[str] = None
    ) -> ThoughtStep: ...

    def reflect(
        self,
        execution_result: ExecutionResult,
        execution_plan: ExecutionPlan,
        understanding: Understanding,
        context: AgentContext
    ) -> ReflectionReport: ...

    def score_confidence(
        self,
        evidence_items: list[MemoryItem | KnowledgeChunk],
        internal_consistency: float,
        coverage: float
    ) -> float: ...

    def propagate_confidence(
        self,
        upstream_confidences: list[float],
        strategy: str = "minimum"
    ) -> float: ...
```

---

## 2. Why a Dedicated Reasoning Runtime?

### 2.1 The Problem with Ad-hoc Reasoning

Before the Reasoning Runtime was introduced, each AEOS agent reasoned in its own way:

- `simple_agent` used regex pattern matching to extract keywords and classify intent.
- `analyst_agent` used a custom chain of if-else logic to derive conclusions.
- `planner_agent` used free-text string parsing to turn task descriptions into plans.
- `reviewer_agent` used a numerical scoring formula that was not shared with anyone.

The consequences were severe:

**Non-standard outputs.** Each agent's reasoning produced different output formats. Pipelines of agents required format-translation logic at every handoff. Translation logic introduced errors, and the errors were invisible because there was no structural type check.

**Non-observable reasoning.** When a pipeline produced a wrong answer, it was impossible to determine which agent's reasoning step had failed. Reasoning was entirely inside each agent's private methods, with no shared logging, no shared confidence representation, and no shared taxonomy of failure types.

**Non-improvable reasoning.** When an agent reasoned incorrectly, there was no standard way to measure "how incorrectly" or to compare the incorrect reasoning against a correct reference. Each agent's quality was evaluated subjectively — the `reviewer_agent` scored the final output, but it could not score the reasoning process.

**Non-composable reasoning.** Different agents used different reasoning strategies without coordinating. A `planner_agent` could select an approach using deductive reasoning, while the `executor_agent` that carried out the plan used inductive reasoning based on its own observations. These two modes are not compatible in series without a handoff protocol — but there was no such protocol.

### 2.2 What the Reasoning Runtime Provides

By centralizing reasoning in a dedicated runtime, AEOS gains:

- **A shared vocabulary:** Deductive, inductive, abductive, and analogical reasoning are named, defined, and consistently implemented. Every agent speaks the same reasoning language.
- **Standard outputs:** Every reasoning operation produces a `ReasoningOutput` or `ThoughtStep` object. There is no free-form reasoning.
- **Centralized observability:** All reasoning events are emitted to the trace log from one place. Debugging a reasoning failure means reading the reasoning trace, not reverse-engineering an agent's internal methods.
- **Composable confidence:** Confidence scores are computed by one formula, propagated by one strategy, and interpreted by one threshold table. Any agent in the pipeline can read another agent's confidence and know exactly what it means.
- **Independent upgradeability:** The rule-based reasoning implementation can be replaced with an LLM-backed implementation in the Reasoning Runtime without any change to any agent. Agents only call `reasoning_runtime.reason()`; they do not know or care whether that call uses rules or an LLM.

---

## 3. Four Reasoning Modes

The Reasoning Runtime supports four formal reasoning modes. Each mode has a precise definition, a set of appropriate use cases, a concrete AEOS example, and documented failure modes. Agents do not choose reasoning modes arbitrarily — the mode selection is part of the Reason step (Step 4 of the cognitive cycle, see §007-AGENT_RUNTIME.md §2.4) and is determined by the structure of the task and the available evidence.

### 3.1 Deductive Reasoning

**Definition:**

Deductive reasoning moves from general premises to specific conclusions. If the premises are true and the logical form is valid, the conclusion is necessarily true. It is the strongest form of reasoning: a valid deductive argument guarantees its conclusion.

```
General Rule:  "All tasks of type SUMMARIZE require a source document."
Specific Case: "This task is of type SUMMARIZE."
Conclusion:    "This task requires a source document."
```

**When to use:**

Use deductive reasoning when:
- The agent has access to domain rules that apply universally to the task type.
- The task is deterministic: given these inputs and these rules, there is exactly one correct output.
- The agent's knowledge includes policy rules, schema definitions, or logical constraints.
- The task has hard constraints that must be satisfied by any valid solution.

Deductive reasoning is inappropriate when rules are probabilistic, when the domain is novel, or when the evidence is sparse.

**AEOS example:**

The `executor_agent` receives a task: "Validate this JSON payload against the User schema."

The agent has a rule: "A valid User schema requires exactly the fields: `id` (integer), `name` (string), `email` (string)."

The agent applies deductive reasoning:
- Premise: Valid User requires fields `id`, `name`, `email`.
- Observation: The payload has `id` and `name` but not `email`.
- Conclusion: The payload is invalid; it is missing the required `email` field.

This conclusion is necessary, not probabilistic. The agent reports it with confidence 1.0.

**Failure modes:**

| Failure | Description |
|---------|-------------|
| `RULE_NOT_FOUND` | No applicable rule exists for this task type; deduction cannot begin |
| `PREMISE_UNCERTAIN` | The general rule is probabilistic, not universal; conclusion inherits uncertainty |
| `OVERAPPLICATION` | Agent applies a rule from an adjacent domain where it does not strictly apply |
| `RULE_CONFLICT` | Two valid rules produce contradictory conclusions for the same specific case |
| `UNDERDETERMINATION` | Rules are present but insufficient to determine the conclusion uniquely |

---

### 3.2 Inductive Reasoning

**Definition:**

Inductive reasoning moves from specific observations to general conclusions. Unlike deduction, inductive conclusions are not guaranteed — they are supported by evidence to varying degrees. The strength of an inductive argument depends on the number, diversity, and quality of the supporting observations.

```
Observation 1: "Task A of type ANALYZE took 1.2s."
Observation 2: "Task B of type ANALYZE took 1.3s."
Observation 3: "Task C of type ANALYZE took 1.1s."
General Conclusion: "ANALYZE tasks typically take 1.0–1.5s."
```

**When to use:**

Use inductive reasoning when:
- The agent has access to multiple past instances of similar tasks or events.
- The goal is to identify a pattern, trend, or typical behavior.
- The task is to estimate, forecast, or characterize a class of entities.
- No universal rules exist, but sufficient empirical evidence is available.

Inductive reasoning is inappropriate when the sample is too small, when the sample is biased, or when the domain is deterministic (in which case deduction is more appropriate).

**AEOS example:**

The `research_agent` is asked: "What is the typical response time for the payment API in peak hours?"

The agent retrieves 47 logged execution results from the knowledge base, all tagged with "payment_api" and "peak_hours". It applies inductive reasoning:
- Sample size: 47 observations.
- Observed range: 380ms – 2,100ms.
- Observed mean: 820ms, median: 740ms.
- Pattern: 80% of calls complete under 1,100ms; outliers above 1,500ms correlate with end-of-month batches.
- General conclusion: "During peak hours, the payment API typically responds in 700–1,100ms, with end-of-month outliers up to 2,100ms."

Confidence is set to 0.75 — high, but not certain, because: the sample is reasonably large (47 > 30), but the time range of the observations is unknown (conditions may have changed), and there may be selection bias (only logged calls, not all calls).

**Failure modes:**

| Failure | Description |
|---------|-------------|
| `INSUFFICIENT_SAMPLE` | Too few observations to support a general conclusion (< minimum threshold) |
| `SAMPLE_BIAS` | Retrieved examples are not representative of the population |
| `OVERGENERALIZATION` | Conclusion extends beyond what the evidence supports |
| `BASE_RATE_NEGLECT` | Agent ignores the prior probability in favor of salient examples |
| `RECENCY_BIAS` | Agent gives excessive weight to recent examples over the full sample |

---

### 3.3 Abductive Reasoning

**Definition:**

Abductive reasoning selects the most likely explanation for a set of observed evidence. It does not guarantee truth — it produces the best available explanation given current evidence. It is reasoning to an inference, not reasoning to a proof. Charles Peirce called it "inference to the best explanation."

```
Evidence:    "The output is garbled unicode."
Evidence:    "The task involved a file from an external system."
Evidence:    "Prior tasks with the same source produced correct output."
Best Explanation: "The file was re-encoded between tasks; an encoding mismatch occurred."
```

**When to use:**

Use abductive reasoning when:
- You have observed effects and need to determine the most likely cause.
- Multiple competing explanations are possible and you must select among them.
- Diagnostic tasks: "Why did X fail?"
- Classification under uncertainty: "What kind of task is this, really?"
- Error recovery: "What went wrong and how can it be fixed?"

Abductive reasoning is inappropriate when a certain explanation can be derived deductively (use deduction) or when the evidence is too sparse to support any explanation confidently.

**AEOS example:**

The `analyst_agent` receives a task: "The previous pipeline run produced incorrect totals. Diagnose the root cause."

Evidence available:
- The totals are off by exactly 12%.
- The discount logic was recently modified.
- The aggregation step ran without errors.
- The source data has not changed.
- A prior run with the same data produced correct totals.

The agent generates three competing explanations:
1. **Discount logic bug (confidence: 0.72):** The discount modification introduced an off-by-one error in rate application. This explains the consistent 12% discrepancy.
2. **Aggregation rounding error (confidence: 0.18):** Floating-point rounding accumulated. But this would produce variable, not consistent, discrepancies.
3. **Source data schema change (confidence: 0.10):** An upstream schema change altered field semantics. But the data is confirmed unchanged.

The agent selects Explanation 1 as the best explanation. Confidence = 0.72. The rejected alternatives and their confidence scores are recorded in the thought chain.

**Failure modes:**

| Failure | Description |
|---------|-------------|
| `NO_VIABLE_EXPLANATION` | Evidence cannot be explained by any hypothesis the agent can generate |
| `EQUAL_CONFIDENCE_TIE` | Two or more explanations have indistinguishable confidence scores |
| `EVIDENCE_CONTAMINATION` | Some "evidence" is actually noise or an artifact of a prior reasoning error |
| `EXPLANATION_SPACE_INCOMPLETE` | The agent fails to consider the correct explanation because it was not generated |
| `ANCHORING_BIAS` | Agent overweights the first generated explanation regardless of evidence fit |

---

### 3.4 Analogical Reasoning

**Definition:**

Analogical reasoning applies the solution from a known, similar case (the "source analog") to a new, structurally similar case (the "target"). Its strength depends on the degree of structural similarity between the source and target: the more similar they are in relevant dimensions, the more confident the analogical conclusion.

```
Source Analog: "When task type=SUMMARIZE and source=PDF, we used the pdf_extractor tool. It worked."
Target:        "Task type=SUMMARIZE, source=PDF with embedded images."
Analogy:       "Use the pdf_extractor tool. Also consider image handling."
Confidence:    0.65 (similar but not identical — image handling is new)
```

**When to use:**

Use analogical reasoning when:
- The agent has stored experience with past similar tasks.
- No applicable general rules exist (ruling out deduction).
- The sample of similar cases is small (ruling out induction).
- The task is novel but recognizably similar to a known case.
- Speed matters: retrieving and adapting a known solution is faster than deriving one from scratch.

Analogical reasoning is inappropriate when the source and target are only superficially similar but structurally different, or when the source's context differs significantly from the target's context.

**AEOS example:**

The `planner_agent` receives a task: "Generate a weekly executive summary from CRM data."

In long-term memory, it finds a past execution: "Generate a weekly sales report from CRM data." That task used a 5-step plan: (1) query CRM API, (2) aggregate by region, (3) compute week-over-week delta, (4) generate narrative, (5) format as PDF.

The `planner_agent` reasons analogically:
- **Structural similarity:** Both tasks are weekly CRM reports with narrative output. Similarity score: 0.80.
- **Key differences:** Target says "executive summary" — this implies higher abstraction, fewer numbers, shorter length. The source plan's step 4 (generate narrative) is applicable but needs adaptation. Step 5 (PDF format) may not apply — executive summaries are often emailed as plain text.
- **Adapted plan:** Same 5 steps, with step 4 adapted ("generate high-level narrative with 3–5 key insights, omit regional breakdowns") and step 5 adapted ("format as formatted email, not PDF").

Confidence = 0.70 — the core structure transfers well, but adaptations introduce uncertainty.

**Failure modes:**

| Failure | Description |
|---------|-------------|
| `NO_SOURCE_ANALOG` | No sufficiently similar past case exists in memory |
| `SURFACE_SIMILARITY_TRAP` | Source and target look similar but are structurally different |
| `NEGATIVE_TRANSFER` | The source solution does not transfer to the target — it makes things worse |
| `OUTDATED_ANALOG` | The source case is from a context that no longer applies |
| `OVER_RELIANCE` | Agent maps too much of the source to the target, ignoring target-specific requirements |

---

## 4. Chain-of-Thought Protocol

The Chain-of-Thought (CoT) protocol defines how agents produce, structure, and store reasoning traces. The protocol ensures that reasoning is never free-form: every thought is a structured artifact with required fields.

### 4.1 Motivation

A free-form thought string — "I think the best approach is to use the summarize tool because the task says to summarize" — is unverifiable, non-auditable, and non-reusable. It cannot be scored for quality, compared to alternatives, or used to train future reasoning.

A structured thought record is all of these things. The CoT protocol defines the required structure.

### 4.2 Thought Format Specification

Each thought in the chain is a `ThoughtStep` object (defined in §007-AGENT_RUNTIME.md §4). The JSON representation of a `ThoughtStep` is the canonical format for transmission, storage, and replay.

**Required fields:**

| Field | Type | Description |
|-------|------|-------------|
| `step_number` | integer | Sequential index of this thought in the chain (1-based) |
| `reasoning_mode` | enum | Which of the four modes was used for this thought |
| `inputs_considered` | list[str] | IDs or descriptions of evidence, facts, or prior thoughts used as input |
| `conclusion` | string | The specific conclusion reached by this thought step |
| `confidence` | float | Confidence in this conclusion (0.0–1.0) |
| `alternatives_rejected` | list[str] | Other conclusions that were considered and rejected; why each was rejected |

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `source_citations` | list[str] | Memory item IDs or knowledge chunk IDs that support the conclusion |
| `domain` | string | Domain or subdomain this thought applies to |
| `uncertainty_flags` | list[str] | Specific sources of uncertainty in this conclusion |
| `revision_of` | integer | `step_number` of an earlier thought this step revises (used in reflection loops) |

### 4.3 Example Thought JSON

```json
{
  "step_number": 3,
  "reasoning_mode": "abductive",
  "inputs_considered": [
    "fact_id:f001 — output totals are off by 12%",
    "fact_id:f002 — discount logic was modified 2 days ago",
    "fact_id:f003 — prior run with same data was correct",
    "fact_id:f004 — aggregation step reported no errors"
  ],
  "conclusion": "The most likely root cause is a bug in the recently-modified discount logic. The consistent 12% discrepancy is consistent with a fixed-rate calculation error introduced by the modification. This explanation is preferred over aggregation errors (which would produce variable discrepancies) and schema changes (which are ruled out by f003).",
  "confidence": 0.72,
  "alternatives_rejected": [
    "Aggregation rounding error — rejected because floating-point errors produce variable, not consistent, discrepancies (confidence: 0.18)",
    "Source data schema change — rejected because fact_id:f003 confirms the same data produces correct results in a prior run (confidence: 0.10)"
  ],
  "source_citations": ["fact_id:f001", "fact_id:f002", "fact_id:f003", "fact_id:f004"],
  "domain": "data_pipeline_diagnostics",
  "uncertainty_flags": [
    "discount modification details not available — root cause is inferred, not confirmed",
    "prior run timestamp unknown — conditions may have changed"
  ],
  "revision_of": null
}
```

### 4.4 Chain Construction Rules

1. Every Reason step (Step 4 of the cognitive cycle) produces at least one `ThoughtStep`. Complex reasoning may produce multiple steps.
2. Each `ThoughtStep` must cite at least one input (from `inputs_considered`). Uncited conclusions are not permitted.
3. `alternatives_rejected` must be populated whenever more than one conclusion was under consideration. An empty `alternatives_rejected` list is valid only when the reasoning is deductive with a single valid conclusion.
4. Confidence values are not self-reported estimates — they are computed by the Confidence Calculator (see §7) based on evidence quality, source consistency, and gap coverage.
5. The complete thought chain is appended to `AgentContext.thought_log` after each Reason step. It is persisted to the audit trail at the end of the cognitive cycle.

### 4.5 Thought Chain Validation

Before appending a `ThoughtStep` to the chain, the Reasoning Runtime validates:

- `step_number` is sequential (no gaps, no duplicates).
- `reasoning_mode` is one of the four defined modes.
- `inputs_considered` is non-empty.
- `conclusion` is non-empty and below the maximum length (2,048 characters).
- `confidence` is in [0.0, 1.0].
- If `revision_of` is set, it references a valid earlier step number.

Invalid thought steps are rejected. The agent must fix the violation before the thought can be logged.

---

## 5. Reflection Protocol

The Reflection Protocol defines when and how agents critique their own outputs. Reflection is executed in Step 9 of the 11-step cognitive cycle (see §007-AGENT_RUNTIME.md §2.9). The protocol specifies trigger conditions, reflection dimensions, the reflection loop, and loop termination.

### 5.1 Trigger Conditions

Reflection is always executed after Step 8 (Execute). However, the *depth* of reflection is determined by whether any trigger condition is met:

| Trigger | Condition | Reflection Depth |
|---------|-----------|-----------------|
| `BASELINE_REFLECTION` | Always | Run self-critique checklist; compute success_rate |
| `LOW_CONFIDENCE` | `current_confidence < reflect_confidence_threshold` (default: 0.60) | Baseline + gap analysis + revision consideration |
| `REVIEWER_REVISE` | External reviewer returned `REVISE` verdict | Baseline + gap analysis + revision — mandatory revision |
| `REVIEWER_REJECT` | External reviewer returned `REJECT` verdict | Baseline + full gap analysis + abort consideration |
| `EXPLICIT_REFLECTION_REQUEST` | Task contains instruction "double-check your work" or equivalent | Baseline + gap analysis + revision |
| `PLAN_DEVIATION_DETECTED` | One or more plan steps deviated from expected output | Baseline + plan adherence analysis + gap analysis |
| `COST_OVERRUN` | Execution cost exceeded 80% of budget | Baseline + efficiency analysis |

### 5.2 Reflection Dimensions

Every reflection operation assesses the output across four dimensions:

**1. Completeness:** Does the output include everything the task required? Are there required elements that are absent or truncated?

Completeness is measured against the success criteria in `Understanding`. Each criterion is checked: present/absent/partial. A weighted completeness score is computed.

**2. Correctness:** Are the claims in the output factually accurate based on available evidence? Does the output contradict any retrieved facts?

Correctness is checked by cross-referencing key claims in the `ExecutionResult.final_output` against the `RetrievedContext` facts. Claims that cannot be traced to any retrieved evidence are flagged.

**3. Consistency:** Is the output internally consistent? Does it contradict itself? Do the conclusions follow from the stated reasoning?

Consistency is checked by scanning the output for: statements that contradict each other, conclusions that do not follow from cited evidence, and numbers that don't add up.

**4. Confidence calibration:** Is the reported confidence score appropriate for the actual quality of the output? An output that has significant gaps should have a low confidence score. An output that is fully supported by strong evidence should have a high confidence score.

Calibration is checked by comparing the output's confidence to the completeness, correctness, and consistency scores. A large discrepancy between confidence and quality indicates overconfidence (the most common miscalibration).

### 5.3 The Reflection Loop

```
┌─────────────────────────────────────────────────────────────────┐
│                    REFLECTION LOOP                              │
│                                                                 │
│   ExecutionResult                                               │
│         │                                                       │
│         ▼                                                       │
│   ┌───────────┐                                                 │
│   │  REFLECT  │ — run self-critique, gap analysis, scoring      │
│   └─────┬─────┘                                                 │
│         │                                                       │
│         ▼                                                       │
│   ┌───────────────────────┐                                     │
│   │  VERDICT DECISION     │                                     │
│   │  success_rate ≥ 0.80? │──── YES ──▶ ACCEPT                  │
│   │  success_rate ≥ 0.50? │──── YES ──▶ REVISE                  │
│   │  success_rate < 0.50? │──── YES ──▶ ABORT                   │
│   └───────────────────────┘                                     │
│              │                                                  │
│           REVISE                                                │
│              │                                                  │
│              ▼                                                  │
│   ┌──────────────────────┐                                      │
│   │ revision_count < max?│──── NO ──▶ Force ABORT               │
│   └──────────┬───────────┘                                      │
│              │ YES                                              │
│              ▼                                                  │
│   Re-enter cognitive cycle at revision_target_step             │
│   (Plan or Execute, with gap context injected)                  │
│              │                                                  │
│              ▼                                                  │
│         [Execute again]                                         │
│              │                                                  │
│              └──────────── back to REFLECT ─────────────────────┘
└─────────────────────────────────────────────────────────────────┘
```

### 5.4 Maximum Reflection Rounds

The parameter `max_reflection_rounds` (default: 3, configurable per agent) limits how many times the reflection loop can iterate before a forced abort is triggered.

The forced abort when `max_reflection_rounds` is exceeded produces an `AbortResult` with:
- `abort_code = ABORT_MAX_REVISIONS_EXCEEDED`
- `partial_results`: The best result produced across all revision rounds (the one with the highest `success_rate`).
- `recovery_hints`: Suggestions for why the task resisted resolution (e.g., task is too complex for this agent, missing domain knowledge, insufficient retrieval results).

The rationale for this limit: a task that cannot be resolved in 3 revision rounds is either fundamentally beyond the agent's current capability, requires information not available in the system, or has a structural ambiguity that human input is needed to resolve. Continuing to loop without limit wastes resources and risks producing increasingly wrong outputs.

---

## 6. Self-Critique Checklist

The self-critique checklist is a standardized set of 10 questions that every agent must answer about its output before finalizing the reflection verdict. The checklist is run by the Reflection Engine as part of every Reflect step.

Each question produces a `SelfCritiqueAnswer` object (defined in §007-AGENT_RUNTIME.md §4) with a yes/no/partial answer and an `is_concern` flag. Answers that are "no" or "partial" where "yes" is required trigger the `is_concern = True` flag. The number of flagged concerns contributes to the `success_rate` computation.

---

**Question 1: Does the output fully address the original task?**

The agent checks each component of the original task against the output. "Fully address" means: every required output element is present, every required transformation was applied, and no required component is missing or only partially present.

*Why it matters:* The most common agent failure mode is partial completion — the agent produces some of what was asked but silently omits other parts. This question catches that failure.

*Scoring:* YES = 1.0, PARTIAL = 0.5, NO = 0.0. Weighted contribution to success_rate: 0.20 (highest weight — completeness is the primary quality dimension).

---

**Question 2: Are all claims supported by retrieved evidence?**

The agent reviews each factual claim in the output and checks whether it can be traced to a specific item in `RetrievedContext`. Unsupported claims are listed.

*Why it matters:* Unsupported claims are hallucinations or unsolicited inventions. In a production reasoning system, every factual claim must have a provenance. This question enforces epistemic accountability.

*Scoring:* YES = 1.0, PARTIAL (some unsupported claims) = 0.5, NO (many unsupported claims) = 0.0. Weight: 0.15.

---

**Question 3: Are there internal contradictions?**

The agent scans the output for pairs of statements that directly contradict each other. Examples: "The total is $1,200" and "The total is $1,400"; "The API is deprecated" and "Use the API for all future calls."

*Why it matters:* A contradictory output cannot be correct. Contradictions indicate that the reasoning produced conflicting conclusions without resolving them. The output is internally inconsistent and unreliable.

*Scoring:* NO (no contradictions) = 1.0, YES (contradictions found) = 0.0. Weight: 0.15.

---

**Question 4: Is the confidence score calibrated to actual evidence?**

The agent checks whether its `current_confidence` reflects the actual quality of the evidence. A rule of thumb: confidence should track evidence coverage (what percentage of the question can be answered from evidence) × evidence quality (average source confidence of retrieved items).

*Why it matters:* Overconfident outputs mislead callers into trusting outputs that are poorly grounded. Underconfident outputs are unnecessarily flagged for review. Calibration is the foundation of useful uncertainty communication.

*Scoring:* YES (calibrated) = 1.0, NO (significantly miscalibrated) = 0.0. Weight: 0.10.

---

**Question 5: Were relevant alternatives considered and rejected?**

The agent checks that the `alternatives_rejected` fields in its thought chain are populated appropriately. For every hypothesis selection, tool selection, and conclusion reached under uncertainty, were competing alternatives explicitly considered?

*Why it matters:* An agent that never considers alternatives is not reasoning — it is guessing. Explicit alternative consideration is the mark of rigorous thinking and a prerequisite for confident selection.

*Scoring:* YES = 1.0, PARTIAL = 0.5, NO = 0.0. Weight: 0.10.

---

**Question 6: Are there missing pieces the next agent in the pipeline needs?**

The agent considers what downstream agents or callers will do with this output. Are there fields, metadata, or context objects that the caller will need that are not present?

*Why it matters:* Pipeline failures often occur not because an agent produced wrong output, but because it produced incomplete output — the next agent cannot proceed without something that was not provided.

*Scoring:* NO missing pieces = 1.0, SOME missing = 0.5, MANY missing = 0.0. Weight: 0.10.

---

**Question 7: Does the output format match what the caller expects?**

The agent checks the `Understanding.hard_constraints` and `Understanding.soft_constraints` for any format requirements (e.g., "output must be valid JSON", "output must be under 500 words", "output must use formal language"). Is each requirement satisfied?

*Why it matters:* Correct content in the wrong format is often unusable. A perfectly analyzed dataset returned as raw Python objects instead of the requested JSON is a delivery failure even if the analysis is correct.

*Scoring:* YES (all format requirements met) = 1.0, PARTIAL = 0.5, NO = 0.0. Weight: 0.10.

---

**Question 8: Are there policy or safety concerns in the output?**

The agent reviews the output for: content that would violate data governance rules (e.g., PII in output that should be anonymized), safety concerns (e.g., instructions that could cause harm), access control violations (e.g., system-internal information exposed in a user-facing output).

*Why it matters:* Even a technically correct output can be a policy violation. Policy-violating outputs that leave the agent layer are a system integrity failure.

*Scoring:* NO concerns = 1.0, POTENTIAL concern (review recommended) = 0.5, CONFIRMED violation = 0.0. Weight: 0.10 (but any CONFIRMED violation overrides the verdict to ABORT regardless of overall success_rate).

---

**Question 9: Is the output actionable?**

The agent asks: can the caller do something with this output? An output that says "it depends on many factors" with no further specificity is not actionable. An output that says "add field X to schema Y to resolve the validation failure" is actionable.

*Why it matters:* AEOS agents exist to generate value through action. An unactionable output — even if technically correct — fails the ultimate purpose. Actionability is the quality dimension that connects correctness to utility.

*Scoring:* YES (fully actionable) = 1.0, PARTIAL (some actionable elements) = 0.5, NO (not actionable) = 0.0. Weight: 0.05.

---

**Question 10: What is the single biggest risk in this output?**

The agent produces a plain-language statement of the most significant risk associated with its output. This is not a yes/no question — it is a mandatory free-text field. If the agent cannot identify any risk, it must state "no significant risk identified" and explain why.

*Why it matters:* Risk identification forces the agent to think adversarially about its own output. It surfaces concerns that might not appear in the other nine questions. The risk statement is included in the output metadata and is visible to callers and to the reviewer agent.

*Scoring:* Always = 1.0 (this question is about process, not quality; the agent gets credit for answering, not for having no risk). Weight: 0.05.

---

**Checklist summary weights:**

| # | Question Summary | Weight |
|---|-----------------|--------|
| 1 | Task fully addressed | 0.20 |
| 2 | Claims are evidenced | 0.15 |
| 3 | No internal contradictions | 0.15 |
| 4 | Confidence is calibrated | 0.10 |
| 5 | Alternatives were considered | 0.10 |
| 6 | Next-agent needs met | 0.10 |
| 7 | Format requirements met | 0.10 |
| 8 | No policy/safety concerns | 0.10 |
| 9 | Output is actionable | 0.05 |
| 10 | Risk identified | 0.05 |
| **Total** | | **1.00** |

`success_rate = Σ(question_score × question_weight)`

---

## 7. Confidence Scoring

Confidence scoring is one of the most important functions of the Reasoning Runtime. A well-calibrated confidence score is a trust signal: it tells callers how much weight to place on an output, tells the reviewer agent how deeply to inspect it, and tells the pipeline orchestrator whether to proceed or pause for human review.

### 7.1 Score Range and Semantics

Confidence scores range from **0.0 to 1.0** (inclusive):

| Range | Label | Meaning | Default System Behavior |
|-------|-------|---------|------------------------|
| 0.00 – 0.30 | Low | The agent has little or no reliable evidence for this output. The output is speculative or a best-guess. | Escalate to human review; do not act autonomously |
| 0.30 – 0.60 | Moderate | The agent has partial evidence. The output is directionally likely correct but may have gaps or errors. | Flag for optional review; proceed with caution |
| 0.60 – 0.85 | High | The agent has substantial, consistent evidence. The output is expected to be correct. | Proceed autonomously; log for periodic review |
| 0.85 – 1.00 | Very High | The agent has strong, multi-source, consistent evidence. High certainty. | Proceed autonomously |

These bands are semantically significant — they have operational consequences. A confidence of 0.29 triggers human escalation. A confidence of 0.31 does not. Agents must not artificially inflate scores to cross these thresholds.

### 7.2 Score Derivation

The Confidence Calculator derives a score from three inputs:

**A. Evidence volume score (`ev_volume`):**

Measures how much relevant evidence was retrieved. Computed as:

```
ev_volume = min(retrieved_item_count / target_item_count, 1.0)
```

Where `target_item_count` is the agent's configured target for evidence density (default: 5 items). If 5 or more relevant items were retrieved, `ev_volume = 1.0`. If 2 items were retrieved, `ev_volume = 0.40`.

**B. Source quality score (`ev_quality`):**

Measures the average confidence of the retrieved evidence sources. Computed as:

```
ev_quality = mean(item.confidence for item in retrieved_context.all_items)
```

Source confidence values are set when items are written to memory (see §007-AGENT_RUNTIME.md §2.11). A fact derived from a high-confidence external API has a high source confidence. A fact derived from an agent's speculation has a low source confidence.

**C. Internal consistency score (`consistency`):**

Measures how consistent the evidence is. Computed as:

```
conflicts_found = len(retrieved_context.conflicts)
conflict_penalty = conflicts_found * 0.10   # each conflict reduces by 0.10
consistency = max(1.0 - conflict_penalty, 0.0)
```

**Final confidence computation:**

```
raw_confidence = (0.40 × ev_volume) + (0.40 × ev_quality) + (0.20 × consistency)
```

The weights (0.40, 0.40, 0.20) are defaults and are configurable per agent and per domain.

**Coverage adjustment:**

After computing `raw_confidence`, apply a coverage adjustment for identified evidence gaps:

```
gap_penalty = len(reasoning_output.evidence_gaps) * 0.05
adjusted_confidence = max(raw_confidence - gap_penalty, 0.0)
```

Each identified gap (a piece of information that would have been useful but was not retrieved) reduces confidence by 5 percentage points. This ensures that confidence is not inflated when the agent "doesn't know what it doesn't know."

### 7.3 Confidence Propagation in Multi-Step Pipelines

When an agent produces an output that becomes the input to another agent, the confidence of the upstream output must influence the confidence of the downstream output. This is confidence propagation.

The Reasoning Runtime supports three propagation strategies, selectable per pipeline:

**Strategy 1: Minimum (default)**

The downstream confidence is capped at the minimum confidence of any upstream output it depends on:

```
downstream_confidence = min(upstream_confidence, derived_confidence)
```

Rationale: A chain is only as strong as its weakest link. If any upstream step had low confidence, the downstream output cannot be more confident than that step.

**Strategy 2: Average**

The downstream confidence is the average of its own derived confidence and all upstream confidences it directly depends on:

```
downstream_confidence = mean([derived_confidence] + [c for c in upstream_confidences])
```

Rationale: Low confidence in one upstream step doesn't necessarily poison the entire downstream result if other upstream steps were high-confidence.

**Strategy 3: Weighted by step importance**

Each step in the pipeline is assigned an importance weight. The downstream confidence is the weighted average of all upstream confidences:

```
downstream_confidence = Σ(weight_i × confidence_i) / Σ(weight_i)
```

Rationale: Some steps are more critical to the final result than others. The Retrieve step's confidence should count more than the Observe step's confidence in most pipelines.

### 7.4 Confidence Aggregation in Multi-Hypothesis Selection

When the Evaluate step scores multiple hypotheses and selects a winner, the winner's `initial_confidence` is not directly used as the agent's `current_confidence`. Instead, the confidence is adjusted by the selection competition:

```
selection_margin = winner_score - second_place_score
selection_factor = 0.5 + (0.5 × selection_margin)   # ranges 0.5–1.0
adjusted_confidence = winner.initial_confidence × selection_factor
```

A large selection margin (the winner was clearly better) produces a `selection_factor` close to 1.0 — little adjustment. A small margin (barely better than second place) produces a `selection_factor` close to 0.5 — significant downward adjustment. This reflects the epistemic reality that a barely-preferred hypothesis deserves less confidence than a clearly-preferred one.

### 7.5 When to Report Low Confidence vs. When to Fail

Low confidence does not automatically mean the agent should fail. The appropriate response depends on the confidence range and the task's `confidence_policy`:

| Confidence Range | `confidence_policy = PERMISSIVE` | `confidence_policy = STANDARD` | `confidence_policy = STRICT` |
|------------------|----------------------------------|--------------------------------|------------------------------|
| 0.85–1.00 | Proceed | Proceed | Proceed |
| 0.60–0.85 | Proceed | Proceed | Proceed with flag |
| 0.30–0.60 | Proceed with flag | Proceed with flag | Request human review |
| 0.00–0.30 | Proceed with flag | Request human review | Fail (return AbortResult) |

The `confidence_policy` is set per task by the caller and defaults to `STANDARD`.

### 7.6 Anti-Patterns in Confidence Scoring

The following are explicitly prohibited:

**Overconfidence:** Reporting a confidence score significantly higher than what the evidence supports. Common cause: agent defaults confidence to 0.90 without computing it from evidence. Detection: confidence is high but `ev_volume` is low.

**Confidence inflation:** Each step in a multi-step process nudges the confidence upward without new evidence to justify it. By the end of the pipeline, confidence is 0.95 even though the evidence was always sparse. Detection: confidence increases significantly between steps without a corresponding increase in evidence quality or volume.

**False certainty:** Reporting confidence = 1.0 for any empirically-derived conclusion. True certainty (1.0) is reserved for logical tautologies, mathematical identities, and outputs derived solely from deductive reasoning with verified premises. Any output that depends on retrieved evidence has inherent uncertainty and cannot be 1.0.

**Confidence anchoring:** Setting confidence based on the first hypothesis generated rather than computing it from evidence. This is a form of anchoring bias applied to the confidence score.

---

## 8. Uncertainty Propagation

Uncertainty is the complement of confidence: `uncertainty = 1.0 - confidence`. Understanding how uncertainty accumulates through pipeline stages is essential for knowing when to surface it to users.

### 8.1 Uncertainty Sources

Uncertainty enters the pipeline at three points:

**1. Retrieval uncertainty:** When retrieved evidence is sparse, low-confidence, or conflicting. This is the primary source of uncertainty in knowledge-intensive tasks.

**2. Reasoning uncertainty:** When the reasoning mode used is inherently probabilistic (abductive, inductive, analogical) and the evidence does not strongly differentiate between competing conclusions.

**3. Execution uncertainty:** When a plan step fails and its contingency is used, or when a tool returns a result with lower fidelity than expected.

### 8.2 Uncertainty Accumulation

In a sequential pipeline of N steps, uncertainty compounds. If each step introduces a fractional uncertainty `u_i`, the combined uncertainty is bounded by:

```
combined_uncertainty ≥ max(u_i for i in 1..N)
```

In the worst case (every step independently uncertain), uncertainty accumulates. The Confidence Calculator tracks uncertainty at each step and surfaces it through the confidence trace (see §11.2).

### 8.3 Surfacing Uncertainty to Users

Uncertainty should be surfaced to users when:

- The final output confidence is below the `min_output_confidence` threshold (default: 0.60).
- The output confidence dropped significantly between two consecutive steps (delta > 0.20), indicating a specific step introduced substantial uncertainty.
- The self-critique checklist flagged multiple concerns.
- The reflection verdict was `REVISE` (even if ultimately accepted after revision).

When surfacing uncertainty to users, the agent must:

1. Report the confidence score and its qualitative label (Low / Moderate / High / Very High).
2. State the primary source of uncertainty (e.g., "Retrieved evidence was sparse — only 2 relevant items found").
3. List the top-2 evidence gaps (from `ReasoningOutput.evidence_gaps`).
4. State the biggest risk (from the self-critique Question 10 answer).

### 8.4 Uncertainty vs. Ignorance

There is an important distinction between uncertainty (known unknowns) and ignorance (unknown unknowns):

- **Uncertainty** is when the agent knows what it doesn't know: "I have 2 evidence items where I needed 5; I am uncertain about the missing 3."
- **Ignorance** is when the agent doesn't know what it is missing: "I retrieved what I could, but there may be relevant facts I didn't query for."

The Reasoning Runtime can only model and propagate uncertainty. Ignorance is addressed architecturally by: requiring broad retrieval queries, diverse query strategies, and periodic knowledge base audits. The reflection protocol's Question 6 ("are there missing pieces?") is the runtime's primary tool for converting ignorance into uncertainty.

---

## 9. Integration with the Reviewer Agent

The `reviewer_agent` is an AEOS agent that scores outputs from other agents. Its verdicts (`PASS`, `REVISE`, `REJECT`) are the primary external quality signal that the Reasoning Runtime's Reflection Protocol responds to.

### 9.1 How Verdicts Map to Reflection Triggers

| Reviewer Verdict | Reasoning Runtime Response |
|------------------|---------------------------|
| `PASS` | If `success_rate ≥ 0.80`, accept the output. If `success_rate < 0.80` (indicating self-critique disagreed), log the discrepancy; accept the reviewer's verdict but flag for monitoring. |
| `REVISE` | Trigger the `REVIEWER_REVISE` reflection condition. Run full gap analysis. Re-enter the cognitive cycle at the revision target step. |
| `REJECT` | Trigger the `REVIEWER_REJECT` reflection condition. If `revision_count < max_reflection_rounds`, attempt one revision. If revisions are exhausted, abort with `ABORT_FUNDAMENTAL_FAILURE`. |

### 9.2 Reviewer Score vs. Self-Assessment Confidence

The reviewer agent produces a numerical score (0–100) that is normalized to 0.0–1.0. This score and the agent's self-assessed `current_confidence` are combined:

```
final_reported_confidence = (0.60 × self_confidence) + (0.40 × reviewer_score_normalized)
```

The 60/40 weighting reflects that the agent has more context about its own reasoning process than the reviewer (which sees only the output), but the reviewer provides valuable external calibration.

If no external reviewer is invoked (`use_external_reviewer = False`), the final reported confidence is the self-assessed confidence only.

### 9.3 Reviewer Confidence Feedback

When the reviewer agent returns a verdict, it also optionally returns a `reviewer_confidence` — how confident the reviewer is in its own verdict. This is used to weight the reviewer's contribution:

```
reviewer_weight = 0.40 × reviewer_confidence
self_weight = 1.0 - reviewer_weight
final_reported_confidence = (self_weight × self_confidence) + (reviewer_weight × reviewer_score_normalized)
```

A reviewer that is uncertain about its verdict (low `reviewer_confidence`) contributes less to the final reported confidence.

### 9.4 Disagreement Monitoring

When the reviewer's verdict disagrees with the agent's self-critique verdict, this disagreement is logged as a `ReviewerSelfCritiqueDisagreement` event. These events are monitored to detect:

- **Agent overconfidence:** Agent consistently rates itself higher than the reviewer.
- **Agent underconfidence:** Agent consistently rates itself lower than the reviewer.
- **Reviewer bias:** A specific reviewer agent consistently disagrees with a specific executing agent.

Persistent disagreement patterns trigger an agent calibration review.

---

## 10. LLM-Backed vs. Rule-Based Reasoning

The current AEOS implementation uses rule-based reasoning throughout the cognitive cycle. The Reasoning Runtime is designed to support a phased upgrade to LLM-backed reasoning without requiring changes to any agent.

### 10.1 Current State: Rule-Based Reasoning

All four reasoning modes are currently implemented with rule-based logic:

- **Deductive:** Pattern-matching against a rule registry. If task matches rule pattern, apply rule to produce conclusion.
- **Inductive:** Statistical aggregation of retrieved examples. Compute means, medians, ranges, and patterns from sample data.
- **Abductive:** Ranked hypothesis generation using a scoring function over retrieved evidence. Score = evidence_fit × prior_probability.
- **Analogical:** Cosine similarity between current task embedding and stored past task embeddings. Select highest-similarity past case; adapt its solution.

Rule-based reasoning is fast, deterministic, and cost-free (no LLM calls). Its limitation is that it is brittle: it can only reason within the patterns it was programmed to handle. Novel tasks, ambiguous evidence, and complex multi-step inferences degrade rule-based quality rapidly.

### 10.2 LLM-Backed Reasoning: The Target State

In LLM-backed reasoning, the Reasoning Runtime submits reasoning requests to an LLM (e.g., Claude) rather than executing rule-based logic. The LLM receives: the extracted facts, the task question, the reasoning mode instruction, and the required output format (the `ThoughtStep` JSON schema). It returns a structured `ThoughtStep`.

This is strictly superior for: novel task types, nuanced evidence assessment, complex multi-step inference chains, and natural language understanding tasks. The LLM's broad world knowledge compensates for sparse retrieved context.

The trade-off is cost: each LLM-backed reasoning call incurs API cost and latency. The upgrade path is therefore incremental.

### 10.3 Upgrade Path: Which Steps to Upgrade First

The recommended upgrade sequence, ordered by impact-to-cost ratio:

| Priority | Step | Mode to Upgrade | Rationale |
|----------|------|-----------------|-----------|
| 1 | Step 4 (Reason) | All modes | The primary reasoning step; upgrading this has the highest quality impact |
| 2 | Step 2 (Understand) | Intent classification, ambiguity resolution | Intent misclassification cascades through all downstream steps |
| 3 | Step 5 (Hypothesize) | Hypothesis generation | LLM generates more creative and diverse hypotheses than rule-based generators |
| 4 | Step 9 (Reflect) | Self-critique | LLM can assess output quality more nuancedly than the checklist alone |
| 5 | Step 10 (Learn) | Insight extraction | LLM can extract more general and useful insights from execution outcomes |

Steps 1, 3, 6, 7, 8, 11 remain rule-based even in the fully upgraded system: they are either reception/dispatch steps (1, 3) or deterministic execution steps (6, 7, 8, 11) that do not benefit significantly from LLM reasoning.

### 10.4 Backward Compatibility

The upgrade is backward-compatible by design:

- The `ReasoningRuntime.reason()` method signature does not change.
- The `ThoughtStep` output format does not change.
- Agents do not know or care whether the reasoning is rule-based or LLM-backed.
- The upgrade is controlled by a configuration flag: `reasoning_backend = "rule_based" | "llm"`.
- The flag can be set per agent, per reasoning mode, or globally.
- A/B testing is supported: the flag can be set to `"ab_test"`, which randomly routes calls to both backends and compares outputs.

---

## 11. Observability

The Reasoning Runtime exposes a comprehensive observability layer that makes every reasoning operation traceable, measurable, and debuggable.

### 11.1 Per-Step Thought Logs

Every `ThoughtStep` produced during a cognitive cycle is appended to `AgentContext.thought_log` immediately after the Reason step that produced it. At cycle completion, the complete thought log is:

- Included in the `AbortResult` or `TaskResult` returned to the caller.
- Persisted to the AEOS audit trail (identified by `task_id`).
- Optionally exported to the AEOS tracing system in OpenTelemetry-compatible span format.

The thought log is the primary debugging tool for cognitive failures. An engineer investigating a wrong output can read the thought log to see:
- What reasoning mode was used at each step.
- What evidence was cited.
- What alternatives were considered and rejected.
- Where confidence was high and where it dropped.

### 11.2 Confidence Trace

The confidence trace is a time-ordered list of confidence scores — one entry per cognitive step that updates confidence:

```json
[
  {"step": "reason",       "step_number": 4, "confidence": 0.72, "delta": null},
  {"step": "evaluate",     "step_number": 6, "confidence": 0.68, "delta": -0.04},
  {"step": "reflect",      "step_number": 9, "confidence": 0.65, "delta": -0.03},
  {"step": "final_output", "step_number": 11, "confidence": 0.65, "delta": 0.00}
]
```

The confidence trace makes calibration issues immediately visible:
- A monotonically decreasing trace indicates progressive confidence erosion — each step is introducing new doubts.
- A sudden large drop at one step identifies where confidence collapsed and why.
- A monotonically increasing trace (without LLM upgrade) is a calibration red flag — rule-based systems should not gain confidence without new evidence.

The confidence trace is included in `TaskResult.metadata.confidence_trace` and in the audit trail.

### 11.3 Reflection Round Count

The number of revision rounds completed in the reflection loop is recorded in `AgentContext.revision_count` and reported in `TaskResult.metadata.reflection_rounds`. This metric is important for:

- Identifying tasks that consistently require multiple revision rounds (potential agent capability gaps).
- Detecting agent regression: if an agent that previously accepted outputs in 1 round now requires 3, something has changed.
- Capacity planning: high revision_round counts mean higher-than-expected resource consumption per task.

### 11.4 Reasoning Mode Usage Per Step

The reasoning mode used at each step is recorded in the thought chain. An aggregate `reasoning_mode_distribution` is computed at cycle end:

```json
{
  "reasoning_mode_distribution": {
    "deductive":   0.40,
    "inductive":   0.20,
    "abductive":   0.30,
    "analogical":  0.10
  }
}
```

This distribution is a characterization of the agent's reasoning style for a given task type. It is useful for:
- Understanding which agents are heavy deductive reasoners vs. inductive.
- Detecting if an agent is over-relying on one mode (e.g., always using analogical because its knowledge base is rich with past cases).
- Calibrating the upgrade path: if a task type is predominantly abductive, upgrading the abductive mode to LLM-backed is highest priority for that task type.

### 11.5 Reasoning Runtime Telemetry Events

The Reasoning Runtime emits the following telemetry events to the AEOS observability pipeline:

| Event | Fields | When Emitted |
|-------|--------|-------------|
| `reasoning.step_started` | `task_id`, `agent_id`, `step_name`, `reasoning_mode` | Start of each reasoning step |
| `reasoning.step_completed` | `task_id`, `step_name`, `confidence`, `duration_ms` | Completion of each reasoning step |
| `reasoning.thought_produced` | `task_id`, `thought_step_number`, `mode`, `confidence` | When a ThoughtStep is appended to the log |
| `reasoning.reflection_started` | `task_id`, `trigger_condition`, `revision_count` | When the Reflect step begins |
| `reasoning.reflection_verdict` | `task_id`, `verdict`, `success_rate`, `concern_count` | When a reflection verdict is produced |
| `reasoning.revision_loop_entered` | `task_id`, `revision_count`, `revision_target_step` | When REVISE verdict triggers re-entry |
| `reasoning.confidence_updated` | `task_id`, `step_name`, `old_confidence`, `new_confidence`, `delta` | On every confidence update |
| `reasoning.abort_triggered` | `task_id`, `abort_code`, `abort_step` | When any abort condition is met |
| `reasoning.cycle_completed` | `task_id`, `final_confidence`, `total_duration_ms`, `reflection_rounds`, `mode_distribution` | At cycle end |

These events are structured logs compatible with standard log aggregation systems (e.g., Elasticsearch, Loki, CloudWatch Logs). They can also be consumed as spans by distributed tracing systems (e.g., Jaeger, Tempo) if the AEOS tracing integration is enabled.

### 11.6 Observability Dashboard Metrics

The following metrics are derived from reasoning telemetry and are available in the AEOS operational dashboard:

- **Average final confidence per agent per intent type:** Identifies which agent/intent combinations are poorly calibrated.
- **Reflection round rate:** Percentage of tasks requiring ≥ 2 reflection rounds, by agent.
- **Abort rate by abort code:** Identifies systematic failure patterns.
- **Reasoning mode distribution by task type:** Identifies reasoning style patterns.
- **Confidence delta per step:** Identifies which pipeline steps are the biggest confidence sinkers.
- **Self-critique concern rate per question:** Identifies which quality dimension is most frequently flagged.
- **Reviewer vs. self-assessment agreement rate:** Identifies calibration drift over time.

---

## 12. Cross-References

| Document | Relationship |
|----------|-------------|
| **007-AGENT_RUNTIME.md** | Defines the 11-step cognitive cycle that invokes the Reasoning Runtime. Step 4 (Reason) and Step 9 (Reflect) are the primary invocation points. The `ThoughtStep`, `ReasoningOutput`, and `ReflectionReport` types defined in that document's §4 are produced by the Reasoning Runtime. |
| **001-ARCHITECTURE.md** | System-level architecture. The Reasoning Runtime is positioned in the Cognitive Layer described in §3.3 of that document. |

---

*End of 008 — REASONING RUNTIME v1.0.0*
