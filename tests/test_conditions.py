"""Tests for the condition expression parser and evaluator.

Verifies: C2, C3, C4
"""

from __future__ import annotations

import pytest

from pymh.conditions import (
    ConditionParseError,
    evaluate,
    parse,
)

# --- Parsing: basic comparisons ---


class TestParseSimple:
    def test_equality_string(self) -> None:
        ast = parse('completeness == "full"')
        assert len(ast.and_exprs) == 1
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.variable == "completeness"
        assert comp.operator == "=="
        assert comp.value == "full"

    def test_equality_bool(self) -> None:
        ast = parse("escalate == true")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.value is True

    def test_gte_number(self) -> None:
        ast = parse("task_attempts >= 3")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.variable == "task_attempts"
        assert comp.operator == ">="
        assert comp.value == 3

    def test_equality_null(self) -> None:
        ast = parse("blocker == null")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.value is None

    def test_ne_operator(self) -> None:
        ast = parse("blocker != null")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.operator == "!="

    def test_lt_float(self) -> None:
        ast = parse("quality_score < 0.5")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.value == 0.5

    def test_dotted_variable(self) -> None:
        ast = parse("tags.coverage >= 80")
        comp = ast.and_exprs[0].comparisons[0]
        assert comp.variable == "tags.coverage"


# --- Parsing: logical operators ---


class TestParseLogical:
    def test_and_two(self) -> None:
        ast = parse('completeness == "full" and quality_score >= 80')
        assert len(ast.and_exprs) == 1
        assert len(ast.and_exprs[0].comparisons) == 2

    def test_or_two(self) -> None:
        ast = parse('completeness == "full" or escalate == true')
        assert len(ast.and_exprs) == 2
        assert len(ast.and_exprs[0].comparisons) == 1
        assert len(ast.and_exprs[1].comparisons) == 1

    def test_and_binds_tighter_than_or(self) -> None:
        # "a and b or c" should parse as "(a and b) or c"
        ast = parse("escalate == true and needs_replan == false or blocker != null")
        assert len(ast.and_exprs) == 2
        assert len(ast.and_exprs[0].comparisons) == 2  # a and b
        assert len(ast.and_exprs[1].comparisons) == 1  # c

    def test_max_three_comparisons(self) -> None:
        expr = 'a == 1 and b == 2 and c == 3'
        ast = parse(expr)
        total = sum(len(ae.comparisons) for ae in ast.and_exprs)
        assert total == 3

    def test_exceeds_max_comparisons(self) -> None:
        expr = 'a == 1 and b == 2 and c == 3 and d == 4'
        with pytest.raises(ConditionParseError, match="max is 3"):
            parse(expr)

    def test_three_across_or(self) -> None:
        expr = 'a == 1 or b == 2 or c == 3'
        ast = parse(expr)
        total = sum(len(ae.comparisons) for ae in ast.and_exprs)
        assert total == 3


# --- Parsing: error cases ---


class TestParseErrors:
    def test_empty_string(self) -> None:
        with pytest.raises(ConditionParseError, match="Empty"):
            parse("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ConditionParseError, match="Empty"):
            parse("   ")

    def test_missing_value(self) -> None:
        with pytest.raises(ConditionParseError):
            parse("x ==")

    def test_unknown_operator(self) -> None:
        with pytest.raises(ConditionParseError):
            parse("x ++ 3")

    def test_trailing_tokens(self) -> None:
        with pytest.raises(ConditionParseError, match="Unexpected tokens"):
            parse('x == 1 2')


# --- Evaluation: basics ---


class TestEvaluateBasic:
    def test_default_always_true(self) -> None:
        assert evaluate("default", {}) is True
        assert evaluate("  default  ", {"x": 1}) is True

    def test_simple_eq_match(self) -> None:
        assert evaluate('completeness == "full"', {"completeness": "full"}) is True

    def test_simple_eq_no_match(self) -> None:
        assert evaluate('completeness == "full"', {"completeness": "partial"}) is False

    def test_gte_match(self) -> None:
        assert evaluate("task_attempts >= 3", {"task_attempts": 3}) is True
        assert evaluate("task_attempts >= 3", {"task_attempts": 5}) is True

    def test_gte_no_match(self) -> None:
        assert evaluate("task_attempts >= 3", {"task_attempts": 2}) is False

    def test_bool_match(self) -> None:
        assert evaluate("escalate == true", {"escalate": True}) is True
        assert evaluate("escalate == true", {"escalate": False}) is False

    def test_numeric_string_coercion(self) -> None:
        # "80" as string should coerce to 80 for numeric comparison
        assert evaluate("quality_score >= 80", {"quality_score": "80"}) is True


# --- Evaluation: variable resolution ---


class TestEvaluateResolution:
    def test_conditions_scope(self) -> None:
        space = {"conditions": {"completeness": "full"}}
        assert evaluate('completeness == "full"', space) is True

    def test_tags_scope(self) -> None:
        space = {"tags": {"coverage": 95}}
        assert evaluate("coverage >= 90", space) is True

    def test_system_conditions_scope(self) -> None:
        space = {"system_conditions": {"fuel_remaining": 3}}
        assert evaluate("fuel_remaining >= 3", space) is True

    def test_top_level(self) -> None:
        space = {"quality_score": 85}
        assert evaluate("quality_score >= 80", space) is True

    def test_dotted_path(self) -> None:
        space = {"tags": {"coverage": 92}}
        assert evaluate("tags.coverage >= 90", space) is True

    def test_resolution_priority(self) -> None:
        # conditions scope takes priority over top-level
        space = {
            "conditions": {"x": "from_conditions"},
            "x": "from_top",
        }
        assert evaluate('x == "from_conditions"', space) is True

    def test_unknown_variable_is_none(self) -> None:
        assert evaluate("nonexistent == null", {}) is True


# --- Evaluation: null handling ---


class TestEvaluateNull:
    def test_eq_null_when_none(self) -> None:
        assert evaluate("blocker == null", {"blocker": None}) is True

    def test_eq_null_when_missing(self) -> None:
        assert evaluate("blocker == null", {}) is True

    def test_ne_null_when_present(self) -> None:
        assert evaluate("blocker != null", {"blocker": "some issue"}) is True

    def test_ne_null_when_none(self) -> None:
        assert evaluate("blocker != null", {"blocker": None}) is False

    def test_numeric_op_with_null_is_false(self) -> None:
        assert evaluate("x >= 3", {}) is False
        assert evaluate("x < 3", {}) is False
        assert evaluate("x > 3", {}) is False
        assert evaluate("x <= 3", {}) is False


# --- Evaluation: logical operators ---


class TestEvaluateLogical:
    def test_and_both_true(self) -> None:
        space = {"a": 5, "b": 10}
        assert evaluate("a >= 5 and b >= 10", space) is True

    def test_and_one_false(self) -> None:
        space = {"a": 5, "b": 9}
        assert evaluate("a >= 5 and b >= 10", space) is False

    def test_or_first_true(self) -> None:
        space = {"a": 5, "b": 1}
        assert evaluate("a >= 5 or b >= 10", space) is True

    def test_or_second_true(self) -> None:
        space = {"a": 1, "b": 10}
        assert evaluate("a >= 5 or b >= 10", space) is True

    def test_or_neither_true(self) -> None:
        space = {"a": 1, "b": 1}
        assert evaluate("a >= 5 or b >= 10", space) is False

    def test_precedence_and_or(self) -> None:
        # "a and b or c" => "(a and b) or c"
        # a=false, b=true, c=true => false AND true = false, false OR true = true
        space = {"a": 1, "b": 10, "c": 100}
        assert evaluate("a >= 5 and b >= 10 or c >= 100", space) is True
        # a=true, b=false, c=false => true AND false = false, false OR false = false
        space2 = {"a": 10, "b": 1, "c": 1}
        assert evaluate("a >= 5 and b >= 10 or c >= 100", space2) is False
