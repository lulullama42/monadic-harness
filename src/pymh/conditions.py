"""Condition expression parser and evaluator.

Grammar (EBNF):
    condition   = or_expr
    or_expr     = and_expr ("or" and_expr)*
    and_expr    = comparison ("and" comparison)*
    comparison  = variable operator value
    variable    = identifier ("." identifier)*
    operator    = ">=" | "<=" | ">" | "<" | "==" | "!="
    value       = number | quoted_string | "null" | "true" | "false"
    identifier  = [a-z_][a-z0-9_]*

Max 3 comparisons per condition (enforced at parse time).
`and` binds tighter than `or`. Per decisions #32.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Union

# Type aliases
ConditionSpace = dict[str, Any]
Value = Union[str, int, float, bool, None]


# --- AST Nodes ---


@dataclass(frozen=True)
class Comparison:
    variable: str
    operator: str
    value: Value


@dataclass(frozen=True)
class AndExpr:
    comparisons: list[Comparison]


@dataclass(frozen=True)
class OrExpr:
    and_exprs: list[AndExpr]


# --- Tokenizer ---

# Order matters: >= before >, <= before <
TOKEN_PATTERN = re.compile(
    r"""
    \s*(?:
        (>=|<=|!=|==|>|<)       # operators (group 1)
        | ("(?:[^"\\]|\\.)*")   # quoted string (group 2)
        | (\b(?:and|or)\b)      # logical keywords (group 3)
        | (\b(?:true|false|null)\b)  # literals (group 4)
        | (\b[a-z_][a-z0-9_.]*\b)   # identifiers/numbers (group 5)
        | (-?\d+(?:\.\d+)?)     # numbers starting with - (group 6)
    )\s*
    """,
    re.VERBOSE,
)


def _tokenize(expr: str) -> list[str]:
    """Tokenize a condition expression string."""
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        # Skip whitespace
        while pos < len(expr) and expr[pos] in " \t":
            pos += 1
        if pos >= len(expr):
            break

        m = TOKEN_PATTERN.match(expr, pos)
        if not m:
            raise ConditionParseError(f"Unexpected character at position {pos}: '{expr[pos:]}'")

        token = m.group(0).strip()
        if token:
            tokens.append(token)
        pos = m.end()

    return tokens


# --- Parser ---


class ConditionParseError(Exception):
    """Raised when a condition expression cannot be parsed."""


def parse(expr: str) -> OrExpr:
    """Parse a condition expression string into an AST.

    Args:
        expr: Condition expression string.

    Returns:
        OrExpr AST node.

    Raises:
        ConditionParseError: If the expression is malformed.
    """
    if not expr or not expr.strip():
        raise ConditionParseError("Empty condition expression")

    tokens = _tokenize(expr)
    if not tokens:
        raise ConditionParseError("Empty condition expression")

    ast, remaining = _parse_or_expr(tokens)

    if remaining:
        raise ConditionParseError(f"Unexpected tokens after expression: {remaining}")

    # Enforce max 3 comparisons
    total = sum(len(ae.comparisons) for ae in ast.and_exprs)
    if total > 3:
        raise ConditionParseError(
            f"Condition has {total} comparisons, max is 3"
        )

    return ast


def _parse_or_expr(tokens: Sequence[str]) -> tuple[OrExpr, list[str]]:
    remaining = list(tokens)
    and_exprs: list[AndExpr] = []

    ae, remaining = _parse_and_expr(remaining)
    and_exprs.append(ae)

    while remaining and remaining[0] == "or":
        remaining = remaining[1:]  # consume 'or'
        ae, remaining = _parse_and_expr(remaining)
        and_exprs.append(ae)

    return OrExpr(and_exprs), remaining


def _parse_and_expr(tokens: Sequence[str]) -> tuple[AndExpr, list[str]]:
    remaining = list(tokens)
    comparisons: list[Comparison] = []

    comp, remaining = _parse_comparison(remaining)
    comparisons.append(comp)

    while remaining and remaining[0] == "and":
        remaining = remaining[1:]  # consume 'and'
        comp, remaining = _parse_comparison(remaining)
        comparisons.append(comp)

    return AndExpr(comparisons), remaining


def _parse_comparison(tokens: Sequence[str]) -> tuple[Comparison, list[str]]:
    if len(tokens) < 3:
        raise ConditionParseError(
            f"Expected 'variable operator value', got: {' '.join(tokens)}"
        )

    variable = tokens[0]
    operator = tokens[1]
    raw_value = tokens[2]

    if operator not in (">=", "<=", ">", "<", "==", "!="):
        raise ConditionParseError(f"Unknown operator: '{operator}'")

    value = _parse_value(raw_value)

    return Comparison(variable, operator, value), list(tokens[3:])


def _parse_value(raw: str) -> Value:
    """Parse a raw token into a typed value."""
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]

    # Try number
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass

    # Treat as unquoted string (lenient)
    return raw


# --- Evaluator ---


def evaluate(expr: str, space: ConditionSpace) -> bool:
    """Evaluate a condition expression against a condition space.

    The condition space should be a flat dict or nested dict with dotted-key access.
    Variable resolution order: conditions → tags → system_conditions (if nested),
    or flat lookup if the space is flat.

    Args:
        expr: Condition expression string (or "default" which always matches).
        space: Dict containing variable values.

    Returns:
        True if the condition matches, False otherwise.

    Raises:
        ConditionParseError: If the expression is malformed.
    """
    if expr.strip() == "default":
        return True

    ast = parse(expr)
    return _eval_or(ast, space)


def _eval_or(node: OrExpr, space: ConditionSpace) -> bool:
    # Short-circuit OR: true if any and_expr is true
    return any(_eval_and(ae, space) for ae in node.and_exprs)


def _eval_and(node: AndExpr, space: ConditionSpace) -> bool:
    # Short-circuit AND: true if all comparisons are true
    return all(_eval_comparison(c, space) for c in node.comparisons)


def _eval_comparison(comp: Comparison, space: ConditionSpace) -> bool:
    resolved = _resolve_variable(comp.variable, space)
    return _compare(resolved, comp.operator, comp.value)


def _resolve_variable(name: str, space: ConditionSpace) -> Value:
    """Resolve a variable name from the condition space.

    Resolution order:
    1. conditions.{name}
    2. tags.{name}
    3. system_conditions.{name} (includes evidence.{name})
    4. Top-level {name}
    5. Dotted path (e.g., "tags.coverage")

    Returns None if not found.
    """
    # Check structured scopes first
    for scope_key in ("conditions", "tags", "system_conditions"):
        scope = space.get(scope_key)
        if isinstance(scope, dict) and name in scope:
            return scope[name]  # type: ignore[return-value]

    # Check top-level
    if name in space:
        return space[name]  # type: ignore[return-value]

    # Dotted path: e.g., "tags.coverage"
    if "." in name:
        parts = name.split(".")
        current: Any = space
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current  # type: ignore[return-value]

    return None


def _compare(left: Value, op: str, right: Value) -> bool:
    """Compare two values with the given operator."""
    # Null handling per spec
    if op == "==" and right is None:
        return left is None
    if op == "!=" and right is None:
        return left is not None
    if op == "==" and left is None:
        return right is None
    if op == "!=" and left is None:
        return right is not None

    # Numeric operators with null → false
    if left is None or right is None:
        if op in (">", "<", ">=", "<="):
            return False
        if op == "==":
            return left is None and right is None
        if op == "!=":
            return not (left is None and right is None)

    # Type coercion for comparison
    left_num = _to_number(left)
    right_num = _to_number(right)

    if left_num is not None and right_num is not None:
        # Numeric comparison
        if op == "==":
            return left_num == right_num
        if op == "!=":
            return left_num != right_num
        if op == ">=":
            return left_num >= right_num
        if op == "<=":
            return left_num <= right_num
        if op == ">":
            return left_num > right_num
        if op == "<":
            return left_num < right_num

    # String/bool comparison (equality only makes sense)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right

    # Ordering on non-numeric types → false
    return False


def _to_number(val: Value) -> float | None:
    """Try to convert a value to a number for comparison."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    return None
