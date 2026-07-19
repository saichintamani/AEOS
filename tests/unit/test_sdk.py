"""
Unit tests for the AEOS SDK and Workflow Compiler (Phase 10).
No HTTP server required — all tests are offline.
"""

import pytest
from aeos.workflow.compiler import WorkflowCompiler, WorkflowValidationError
from aeos.sdk.types import RunResult, WorkflowResult, StepResult
from aeos.sdk.workflow import WorkflowBuilder


# ── WorkflowCompiler tests ────────────────────────────────────────────────────

class TestWorkflowCompiler:
    def _compiler(self):
        return WorkflowCompiler()

    def _minimal_raw(self, name="test", steps=None):
        return {
            "workflow": {
                "name": name,
                "steps": steps or [{"task": "Do something"}],
            }
        }

    def test_compile_minimal(self):
        raw = self._minimal_raw()
        compiled = self._compiler().compile(raw)
        assert compiled["name"] == "test"
        assert len(compiled["steps"]) == 1
        assert compiled["steps"][0]["task"] == "Do something"
        assert compiled["steps"][0]["mode"] == "single-agent"

    def test_compile_with_all_fields(self):
        raw = {
            "workflow": {
                "name": "full-workflow",
                "description": "A complete workflow",
                "version": "2.0",
                "agents": ["planner", "researcher"],
                "steps": [
                    {
                        "name": "step-one",
                        "task": "Do step one",
                        "mode": "multi-agent",
                        "agent": "planner",
                        "timeout_s": 120,
                        "retry": 2,
                    },
                    {
                        "name": "step-two",
                        "task": "Do step two",
                        "depends_on": ["step-one"],
                    },
                ],
            }
        }
        compiled = self._compiler().compile(raw)
        assert compiled["name"] == "full-workflow"
        assert compiled["version"] == "2.0"
        assert compiled["agents"] == ["planner", "researcher"]
        s0 = compiled["steps"][0]
        assert s0["name"] == "step-one"
        assert s0["mode"] == "multi-agent"
        assert s0["timeout_s"] == 120
        assert s0["retry"] == 2
        s1 = compiled["steps"][1]
        assert s1["depends_on"] == ["step-one"]

    def test_variable_interpolation(self):
        raw = self._minimal_raw(steps=[{"task": "Research {topic} for {audience}"}])
        compiled = self._compiler().compile(raw, variables={"topic": "Raft", "audience": "engineers"})
        assert compiled["steps"][0]["task"] == "Research Raft for engineers"

    def test_unresolved_variable_left_as_is(self):
        raw = self._minimal_raw(steps=[{"task": "Research {unknown_var}"}])
        compiled = self._compiler().compile(raw, variables={})
        assert "{unknown_var}" in compiled["steps"][0]["task"]

    def test_missing_workflow_key_raises(self):
        with pytest.raises(WorkflowValidationError, match="top-level 'workflow:'"):
            self._compiler().compile({"name": "oops"})

    def test_missing_name_raises(self):
        with pytest.raises(WorkflowValidationError, match="workflow.name"):
            self._compiler().compile({"workflow": {"steps": [{"task": "x"}]}})

    def test_missing_steps_raises(self):
        with pytest.raises(WorkflowValidationError, match="workflow.steps"):
            self._compiler().compile({"workflow": {"name": "x"}})

    def test_empty_steps_raises(self):
        with pytest.raises(WorkflowValidationError):
            self._compiler().compile({"workflow": {"name": "x", "steps": []}})

    def test_step_missing_task_raises(self):
        with pytest.raises(WorkflowValidationError, match="task"):
            self._compiler().compile({"workflow": {"name": "x", "steps": [{"name": "s1"}]}})

    def test_invalid_mode_raises(self):
        raw = self._minimal_raw(steps=[{"task": "x", "mode": "invalid-mode"}])
        with pytest.raises(WorkflowValidationError, match="mode"):
            self._compiler().compile(raw)

    def test_duplicate_step_names_raise(self):
        raw = self._minimal_raw(steps=[
            {"name": "step-a", "task": "x"},
            {"name": "step-a", "task": "y"},
        ])
        with pytest.raises(WorkflowValidationError, match="Duplicate"):
            self._compiler().compile(raw)

    def test_depends_on_unknown_step_raises(self):
        raw = self._minimal_raw(steps=[
            {"name": "step-a", "task": "x", "depends_on": ["nonexistent"]},
        ])
        with pytest.raises(WorkflowValidationError, match="nonexistent"):
            self._compiler().compile(raw)

    def test_auto_generated_step_id(self):
        raw = self._minimal_raw(steps=[{"task": "x"}, {"task": "y"}])
        compiled = self._compiler().compile(raw)
        assert compiled["steps"][0]["id"] == "step-0"
        assert compiled["steps"][1]["id"] == "step-1"


# ── WorkflowBuilder tests ─────────────────────────────────────────────────────

class TestWorkflowBuilder:
    def test_build_minimal(self):
        raw = WorkflowBuilder("my-wf").add_step("s1", "Do x").build()
        assert raw["workflow"]["name"] == "my-wf"
        assert len(raw["workflow"]["steps"]) == 1

    def test_compile_from_builder(self):
        compiled = (
            WorkflowBuilder("research")
            .agents("planner", "researcher")
            .add_step("plan", "Plan: {q}", agent="planner")
            .add_step("research", "Research: {q}", mode="multi-agent", depends_on=["plan"])
            .compile(variables={"q": "What is Raft?"})
        )
        assert compiled["name"] == "research"
        assert compiled["agents"] == ["planner", "researcher"]
        assert "What is Raft?" in compiled["steps"][0]["task"]
        assert compiled["steps"][1]["depends_on"] == ["plan"]

    def test_fluent_chain_returns_builder(self):
        builder = WorkflowBuilder("test")
        result = builder.add_step("s1", "x")
        assert result is builder


# ── SDK types tests ───────────────────────────────────────────────────────────

class TestRunResult:
    def test_from_api_success(self):
        data = {
            "status": "success",
            "agent_id": "researcher",
            "result": "Here is the answer.",
            "trace_id": "abc123",
        }
        r = RunResult.from_api(data)
        assert r.ok
        assert r.agent_id == "researcher"
        assert r.result == "Here is the answer."
        assert r.trace_id == "abc123"

    def test_from_api_failure(self):
        r = RunResult.from_api({"status": "failed"})
        assert not r.ok

    def test_response_field_fallback(self):
        r = RunResult.from_api({"status": "success", "response": "fallback text"})
        assert r.result == "fallback text"

    def test_agent_field_fallback(self):
        r = RunResult.from_api({"status": "success", "agent": "simple_agent"})
        assert r.agent_id == "simple_agent"


class TestWorkflowResult:
    def test_ok_when_all_steps_succeed(self):
        steps = [
            StepResult("s1", RunResult.from_api({"status": "success"})),
            StepResult("s2", RunResult.from_api({"status": "success"})),
        ]
        wr = WorkflowResult(workflow_name="test", steps=steps)
        assert wr.ok

    def test_not_ok_when_any_step_fails(self):
        steps = [
            StepResult("s1", RunResult.from_api({"status": "success"})),
            StepResult("s2", RunResult.from_api({"status": "failed"})),
        ]
        wr = WorkflowResult(workflow_name="test", steps=steps)
        assert not wr.ok

    def test_final_result_from_last_step(self):
        steps = [
            StepResult("s1", RunResult.from_api({"status": "success", "result": "first"})),
            StepResult("s2", RunResult.from_api({"status": "success", "result": "final"})),
        ]
        wr = WorkflowResult(workflow_name="test", steps=steps)
        assert wr.final_result == "final"
