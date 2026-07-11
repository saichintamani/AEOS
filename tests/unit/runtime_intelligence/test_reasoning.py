"""Unit tests — ReasoningEngine, ChainOfThought."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime_intelligence.capability_matcher import DefaultCapabilityMatcher
from app.runtime_intelligence.reasoning import ChainOfThought, ReasoningEngine


def _profile(worker_id="w1", trust=0.9, load=0.1, latency_ms=100.0,
             skills=None, gpu=False) -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        trust_score=trust,
        current_load=load,
        avg_latency_ms=latency_ms,
        skills=frozenset(skills or []),
        gpu_available=gpu,
        health_score=1.0,
        historical_success_rate=0.95,
    )


class TestReasoningEngine:

    def _scores(self, profiles, req):
        m = DefaultCapabilityMatcher()
        return m.rank(profiles, req)

    def test_trace_has_task_id(self):
        engine = ReasoningEngine()
        profiles = [_profile("w1")]
        req = TaskRequirements(task_id="t-42")
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        assert trace.task_id == "t-42"

    def test_trace_chosen_worker(self):
        engine = ReasoningEngine()
        profiles = [_profile("w1"), _profile("w2", load=0.9)]
        req = TaskRequirements()
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        assert trace.chosen_worker_id == scores[0].worker_id

    def test_trace_no_eligible(self):
        engine = ReasoningEngine()
        req = TaskRequirements(requires_gpu=True)
        profiles = [_profile("w1", gpu=False)]
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        assert trace.chosen_worker_id == ""
        assert "No eligible" in trace.conclusion

    def test_trace_observations_populated(self):
        engine = ReasoningEngine()
        profiles = [_profile("w1")]
        req = TaskRequirements(required_skills=frozenset({"nlp"}))
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        obs_keys = {o.key for o in trace.observations}
        assert "candidate_count" in obs_keys
        assert "required_skills" in obs_keys

    def test_cot_text_non_empty(self):
        engine = ReasoningEngine()
        profiles = [_profile("w1")]
        req = TaskRequirements()
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        assert len(trace.cot_text) > 0

    def test_chain_of_thought_format(self):
        engine = ReasoningEngine()
        profiles = [_profile("w1")]
        req = TaskRequirements(task_id="t-99")
        scores = self._scores(profiles, req)
        trace = engine.reason(req, profiles, scores)
        cot = ChainOfThought.format(trace)
        assert "t-99" in cot
        assert "Conclusion" in cot
