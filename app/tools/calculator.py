"""Safe arithmetic calculator.

Expressions are parsed with :mod:`ast` and evaluated by walking an explicit
allow-list of node types. ``eval`` is never used, attribute access and names
outside the allow-list are rejected, and exponent sizes are bounded to prevent
denial-of-service via huge numbers.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import BaseTool, ToolContext, ToolError, ToolResult

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _mean(values: list[float]) -> float:
    if not values:
        raise ToolError("mean() of an empty list")
    return sum(values) / len(values)


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "mean": _mean,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}

MAX_EXPONENT = 1000
MAX_MAGNITUDE = 1e100
MAX_LIST_ITEMS = 10_000


class CalculatorInput(BaseModel):
    expression: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "Arithmetic expression, e.g. '(12.5 * 4) / 3' or 'mean([4, 8, 15])'. "
            "Supports + - * / // % **, parentheses, lists, and the functions "
            "abs, round, min, max, sum, mean, sqrt, log, log10, exp, floor, ceil, "
            "plus constants pi and e."
        ),
    )


class _Evaluator:
    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:
            raise ToolError(f"Unsupported syntax: {type(node).__name__}")
        return method(node)

    def _eval_Expression(self, node: ast.Expression) -> Any:
        return self.eval(node.body)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ToolError("Only numeric constants are allowed")
        return node.value

    def _eval_Name(self, node: ast.Name) -> Any:
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ToolError(f"Unknown name '{node.id}'")

    def _eval_List(self, node: ast.List) -> list[Any]:
        if len(node.elts) > MAX_LIST_ITEMS:
            raise ToolError("List too long")
        return [self.eval(e) for e in node.elts]

    _eval_Tuple = _eval_List

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ToolError("Unsupported unary operator")
        return op(self.eval(node.operand))

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ToolError("Unsupported operator")
        left, right = self.eval(node.left), self.eval(node.right)
        if isinstance(node.op, ast.Pow) and (
            abs(right) > MAX_EXPONENT or abs(left) > MAX_MAGNITUDE
        ):
            raise ToolError("Exponent too large")
        result = op(left, right)
        if isinstance(result, int | float) and abs(result) > MAX_MAGNITUDE:
            raise ToolError("Result too large")
        return result

    def _eval_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ToolError("Only whitelisted functions can be called")
        if node.keywords:
            raise ToolError("Keyword arguments are not supported")
        args = [self.eval(a) for a in node.args]
        return _FUNCTIONS[node.func.id](*args)


def evaluate_expression(expression: str) -> float | int:
    """Evaluate an arithmetic expression safely."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Invalid expression: {exc.msg}") from exc
    try:
        result = _Evaluator().eval(tree)
    except ToolError:
        raise
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ToolError(f"Math error: {exc}") from exc
    if isinstance(result, list):
        raise ToolError("Expression must evaluate to a single number")
    return result


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "Evaluate arithmetic expressions exactly. Use for any numeric calculation."
    input_model = CalculatorInput

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, CalculatorInput)
        value = evaluate_expression(params.expression)
        return ToolResult(
            content=f"{params.expression} = {value}",
            data={"expression": params.expression, "result": value},
        )
