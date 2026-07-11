"""Unit tests — WorkflowCompiler."""

from __future__ import annotations

import pytest

from app.runtime.workflow_compiler import (
    CompilationError,
    TaskSpec,
    WorkflowCompiler,
    WorkflowDefinition,
)


def _def(tasks: list[TaskSpec], wf_id: str = "wf-1") -> WorkflowDefinition:
    return WorkflowDefinition(workflow_id=wf_id, tasks=tasks)


class TestWorkflowCompiler:

    def test_compile_single_task(self):
        c = WorkflowCompiler()
        g = c.compile(_def([TaskSpec(task_id="t1", task_type="nlp")]))
        assert len(g.nodes) == 1
        assert "t1" in g.nodes

    def test_compile_with_dependency(self):
        c = WorkflowCompiler()
        g = c.compile(_def([
            TaskSpec(task_id="t1", task_type="ingest"),
            TaskSpec(task_id="t2", task_type="process", depends_on=["t1"]),
        ]))
        edges = [(f, t) for f, t, _ in g.edges]
        assert ("t1", "t2") in edges

    def test_compile_sets_workflow_id(self):
        c = WorkflowCompiler()
        g = c.compile(_def([TaskSpec(task_id="t1", task_type="t")], wf_id="wf-99"))
        assert g.workflow_id == "wf-99"

    def test_compile_propagates_requirements(self):
        c = WorkflowCompiler()
        g = c.compile(_def([
            TaskSpec(task_id="t1", task_type="ml", requires_gpu=True,
                     required_memory_gb=16.0, required_skills=["vision"])
        ]))
        req = g.nodes["t1"].requirements
        assert req.requires_gpu
        assert req.required_memory_gb == 16.0
        assert "vision" in req.required_skills

    def test_duplicate_task_id_raises(self):
        c = WorkflowCompiler()
        with pytest.raises(CompilationError, match="Duplicate"):
            c.compile(_def([
                TaskSpec(task_id="t1", task_type="a"),
                TaskSpec(task_id="t1", task_type="b"),
            ]))

    def test_unknown_depends_on_raises(self):
        c = WorkflowCompiler()
        with pytest.raises(CompilationError, match="unknown"):
            c.compile(_def([
                TaskSpec(task_id="t1", task_type="a", depends_on=["nonexistent"]),
            ]))

    def test_cycle_raises(self):
        c = WorkflowCompiler()
        with pytest.raises(CompilationError, match="cycle"):
            c.compile(_def([
                TaskSpec(task_id="t1", task_type="a", depends_on=["t2"]),
                TaskSpec(task_id="t2", task_type="b", depends_on=["t1"]),
            ]))

    def test_empty_workflow_compiles(self):
        c = WorkflowCompiler()
        g = c.compile(_def([]))
        assert len(g.nodes) == 0

    def test_parallel_tasks_no_edges(self):
        c = WorkflowCompiler()
        g = c.compile(_def([
            TaskSpec(task_id="a", task_type="x"),
            TaskSpec(task_id="b", task_type="y"),
            TaskSpec(task_id="c", task_type="z"),
        ]))
        assert len(g.edges) == 0
