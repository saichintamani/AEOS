"""
app.runtime_intelligence — Intelligent Orchestration Core (IOC)

Phase 9B.3: AEOS Runtime Intelligence layer.
"""

from app.runtime_intelligence.contracts import (
    CapabilityMatcher,
    CapabilityProfile,
    CapabilityScore,
    DecisionDimension,
    DecisionEngine,
    ExecutionDecision,
    ExecutionGraph,
    ExecutionRecord,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
    LearningEngine,
    SimulationResult,
    SimulationScenario,
    TaskDependencyType,
    TaskNode,
    TaskPlanner,
    TaskRequirements,
)
from app.runtime_intelligence.capability_graph import CapabilityGraph
from app.runtime_intelligence.capability_matcher import (
    CapabilityRanker,
    CapabilityResolver,
    DefaultCapabilityMatcher,
)
from app.runtime_intelligence.dag_builder import DAGBuilder
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.execution_planner import ExecutionPlan, ExecutionPlanner
from app.runtime_intelligence.explanation_engine import ExplanationEngine, Verbosity
from app.runtime_intelligence.knowledge_graph import KnowledgeGraph
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime_intelligence.optimizer import ExecutionOptimizer
from app.runtime_intelligence.planner import DefaultTaskPlanner
from app.runtime_intelligence.policy_ranker import PolicyRanker, PolicyRule
from app.runtime_intelligence.reasoning import ChainOfThought, ReasoningEngine, ReasoningTrace
from app.runtime_intelligence.runtime_predictor import RuntimePredictor
from app.runtime_intelligence.scheduler_ai import AIScheduler
from app.runtime_intelligence.simulation import SimulationEngine
from app.runtime_intelligence.strategy_selector import StrategySelector
from app.runtime_intelligence.workflow_optimizer import WorkflowOptimizer

__all__ = [
    # Contracts
    "CapabilityMatcher", "CapabilityProfile", "CapabilityScore",
    "DecisionDimension", "DecisionEngine", "ExecutionDecision",
    "ExecutionGraph", "ExecutionRecord", "KnowledgeEdge", "KnowledgeNode",
    "KnowledgeNodeType", "LearningEngine", "SimulationResult", "SimulationScenario",
    "TaskDependencyType", "TaskNode", "TaskPlanner", "TaskRequirements",
    # Wave 9B.3.1
    "CapabilityGraph", "CapabilityRanker", "CapabilityResolver", "DefaultCapabilityMatcher",
    # Wave 9B.3.2
    "DAGBuilder", "DefaultTaskPlanner", "ExecutionOptimizer",
    "ExecutionPlan", "ExecutionPlanner", "WorkflowOptimizer",
    # Wave 9B.3.3
    "ChainOfThought", "ReasoningEngine", "ReasoningTrace",
    # Wave 9B.3.4
    "AIScheduler", "PolicyRanker", "PolicyRule", "StrategySelector",
    # Wave 9B.3.6
    "DefaultLearningEngine", "RuntimePredictor",
    # Wave 9B.3.7
    "ExpectedUtilityDecisionEngine",
    # Wave 9B.3.8
    "KnowledgeGraph",
    # Wave 9B.3.9
    "ExplanationEngine", "Verbosity",
    # Wave 9B.3.10
    "SimulationEngine",
]
