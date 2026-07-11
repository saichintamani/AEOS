"""
AEOS Distributed Execution Engine — Condition Evaluator

Safe expression evaluator for ConditionalNode.condition_expr.

Supports:
  - Comparisons: ==, !=, <, >, <=, >=, in, not in
  - Boolean: and, or, not
  - Subscript: upstream['node_id'], result['key']
  - Attribute: obj.field
  - Arithmetic: +, -, *, /
  - Safe builtins: len(), str(), int(), float(), bool()

Deliberately rejects: imports, function definitions, exec/eval,
attribute writes, any node type not explicitly whitelisted.

Usage:
    ctx = ConditionContext(upstream={"node_1": {"status": "success", "count": 5}})
    result = evaluate_condition("upstream['node_1']['count'] > 3", ctx)
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any

from app.core.logger import get_logger

__all__ = [
    "ConditionContext",
    "ConditionEvaluator",
    "evaluate_condition",
    "ConditionError",
]

log = get_logger(__name__)


class ConditionError(ValueError):
    """Raised when a condition expression fails to parse or evaluate safely."""


@dataclass
class ConditionContext:
    """
    Namespace available to condition expressions.

    Attributes:
        upstream:  dict mapping node_id → step result value
        workflow:  dict of workflow-level metadata (trace_id, task_id, ...)
        extra:     any additional caller-defined variables
    """
    upstream: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_namespace(self) -> dict[str, Any]:
        """Flatten into a single variable namespace for the evaluator."""
        return {
            "upstream": self.upstream,
            "workflow": self.workflow,
            **self.extra,
        }


# ── AST safe-eval ─────────────────────────────────────────────────────────────

_COMPARE_OPS: dict[type, Any] = {
    ast.Eq:    operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt:    operator.lt,
    ast.LtE:   operator.le,
    ast.Gt:    operator.gt,
    ast.GtE:   operator.ge,
    ast.In:    lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_BIN_OPS: dict[type, Any] = {
    ast.Add:  operator.add,
    ast.Sub:  operator.sub,
    ast.Mult: operator.mul,
    ast.Div:  operator.truediv,
    ast.Mod:  operator.mod,
    ast.FloorDiv: operator.floordiv,
}

_SAFE_BUILTINS: dict[str, Any] = {
    "len":   len,
    "str":   str,
    "int":   int,
    "float": float,
    "bool":  bool,
    "abs":   abs,
    "min":   min,
    "max":   max,
    "round": round,
    "True":  True,
    "False": False,
    "None":  None,
}


class _SafeEvaluator(ast.NodeVisitor):
    """
    Recursive AST visitor that evaluates whitelisted expressions.

    Raises ConditionError for any unsupported node type.
    """

    def __init__(self, namespace: dict[str, Any]) -> None:
        self._ns = {**_SAFE_BUILTINS, **namespace}

    # ── Top-level ────────────────────────────────────────────────────────────

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    # ── Literals ─────────────────────────────────────────────────────────────

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_List(self, node: ast.List) -> list:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple:
        return tuple(self.visit(e) for e in node.elts)

    def visit_Dict(self, node: ast.Dict) -> dict:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    # ── Name / attribute / subscript ─────────────────────────────────────────

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self._ns:
            raise ConditionError(f"Name {node.id!r} is not available in condition context")
        return self._ns[node.id]

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        obj = self.visit(node.value)
        try:
            return getattr(obj, node.attr)
        except AttributeError:
            raise ConditionError(f"Object has no attribute {node.attr!r}")

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        obj = self.visit(node.value)
        # Python 3.8 wraps slices in ast.Index; 3.9+ uses the value directly
        slice_node = node.slice
        if isinstance(slice_node, ast.Index):  # Python 3.8 compat
            key = self.visit(slice_node.value)   # type: ignore[attr-defined]
        else:
            key = self.visit(slice_node)
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ConditionError(f"Subscript error: {exc}")

    # ── Operators ─────────────────────────────────────────────────────────────

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            op_fn = _COMPARE_OPS.get(type(op))
            if op_fn is None:
                raise ConditionError(f"Comparison operator {type(op).__name__!r} not allowed")
            try:
                if not op_fn(left, right):
                    return False
            except TypeError as exc:
                raise ConditionError(f"Type error in comparison: {exc}")
            left = right
        return True

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        if isinstance(node.op, ast.And):
            return all(bool(self.visit(v)) for v in node.values)
        # Or
        return any(bool(self.visit(v)) for v in node.values)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ConditionError(f"Unary operator {type(node.op).__name__!r} not allowed")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            raise ConditionError(f"Binary operator {type(node.op).__name__!r} not allowed")
        try:
            return op_fn(left, right)
        except (TypeError, ZeroDivisionError) as exc:
            raise ConditionError(f"Arithmetic error: {exc}")

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        # Ternary: value if condition else other
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node: ast.Call) -> Any:
        """Only allow whitelisted built-in function calls."""
        if not isinstance(node.func, ast.Name):
            raise ConditionError("Method calls are not allowed in condition expressions")
        fn = self._ns.get(node.func.id)
        if fn not in _SAFE_BUILTINS.values() or not callable(fn):
            raise ConditionError(f"Function {node.func.id!r} is not allowed in condition expressions")
        if node.keywords or node.starargs if hasattr(node, "starargs") else node.keywords:
            raise ConditionError("Keyword arguments are not allowed in condition expressions")
        args = [self.visit(a) for a in node.args]
        return fn(*args)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ConditionError(
            f"AST node {type(node).__name__!r} is not allowed in condition expressions. "
            "Only comparisons, boolean logic, subscripts, and safe builtins are permitted."
        )


# ── Public API ────────────────────────────────────────────────────────────────

class ConditionEvaluator:
    """
    Evaluates condition expressions against a ConditionContext.

    Stateless — can be reused across nodes and workflows.
    """

    def evaluate(self, expr: str, context: ConditionContext) -> bool:
        """
        Evaluate a condition expression.

        Returns:
            bool — result of the expression

        Raises:
            ConditionError — expression is invalid or disallowed
        """
        return evaluate_condition(expr, context.to_namespace())

    def is_valid(self, expr: str) -> tuple[bool, str]:
        """
        Validate an expression without evaluating it.

        Returns:
            (is_valid, error_message)
        """
        try:
            ast.parse(expr.strip(), mode="eval")
            return True, ""
        except SyntaxError as exc:
            return False, str(exc)


def evaluate_condition(expr: str, namespace: dict[str, Any]) -> bool:
    """
    Module-level helper: evaluate a condition expression string.

    Args:
        expr:       Python-like condition expression
        namespace:  Variables available in the expression

    Returns:
        bool result

    Raises:
        ConditionError on parse or evaluation errors
    """
    if not expr or not expr.strip():
        raise ConditionError("Condition expression must not be empty")

    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"Invalid condition syntax: {exc}") from exc

    evaluator = _SafeEvaluator(namespace)
    try:
        result = evaluator.visit(tree)
    except ConditionError:
        raise
    except Exception as exc:
        raise ConditionError(f"Condition evaluation failed: {exc}") from exc

    return bool(result)
