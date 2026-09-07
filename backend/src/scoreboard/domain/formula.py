"""Arithmetic a customer can write, evaluated without letting them run code.

A derived metric used to be one division: a numerator, a denominator and a
scale. That covers close rate and average sale and stops at the first customer
who wants `(sold_leads - refunds) / issued_leads * 100`, or a commission that
is one rate below a threshold and another above it.

So an expression is text now. It is parsed with Python's own parser and then
walked by hand, allowing exactly the nodes arithmetic needs. Nothing is
compiled and `eval` is never called, so there is no path from a form field to
running code — which matters more here than in most places, because the person
typing the formula is a customer's sales manager rather than a programmer.

The product rule is untouched. An expression names additive components and
metrics declared before it, and is evaluated *after* those are summed. That is
what keeps a rate computed from a group's totals rather than averaged across
its rows, whatever the expression happens to say.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

MAX_LENGTH = 500

#: Functions a formula may call. Deliberately short: each one has an obvious
#: reading on a leaderboard, and anything longer starts to need a debugger.
FUNCTIONS = {
    "min": min,
    "max": max,
    "abs": abs,
    # `round` is here because money on a wall is read to the cent or not at
    # all, and a customer should not have to multiply and divide to get there.
    "round": round,
}


class FormulaError(ValueError):
    """An expression that will not parse, or names something it may not."""


_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)
_UNARY = (ast.UAdd, ast.USub)


@dataclass(frozen=True)
class Expression:
    """A parsed formula, ready to evaluate against a set of values.

    Frozen and cached at construction: parsing happens when a field is saved
    or a catalogue is loaded, never per row of a board.
    """

    text: str
    tree: ast.Expression
    names: frozenset[str]
    #: How this was written, when it was written as one division: numerator,
    #: denominator, scale. Provenance rather than a second representation --
    #: the expression is still the only thing evaluated -- so the console can
    #: offer three labelled boxes for the case that is three labelled boxes.
    shorthand: tuple[str, str, float] | None = None

    @staticmethod
    def parse(text: str, known: frozenset[str] | set[str] = frozenset(),
              shorthand: tuple[str, str, float] | None = None) -> Expression:
        source = (text or "").strip()
        if not source:
            raise FormulaError("A calculated field needs a formula.")
        if len(source) > MAX_LENGTH:
            raise FormulaError(
                f"That formula is longer than {MAX_LENGTH} characters. "
                "A metric nobody can read on one line is a metric nobody trusts."
            )
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise FormulaError(f"That is not arithmetic: {exc.msg}.") from exc

        names = _check(tree.body)
        if known:
            unknown = sorted(names - set(known) - set(FUNCTIONS))
            if unknown:
                raise FormulaError(
                    f"{', '.join(repr(n) for n in unknown)} "
                    f"{'is not a field' if len(unknown) == 1 else 'are not fields'} "
                    "in this organization, or is defined after this one."
                )
        return Expression(source, tree, frozenset(names), shorthand)

    def evaluate(self, values: dict[str, float]) -> float:
        return _evaluate(self.tree.body, values)


def _check(node: ast.AST) -> set[str]:
    """Walk the tree, refusing anything that is not arithmetic.

    An allowlist rather than a blocklist: a node type nobody thought about is
    refused by default, which is the only way this stays safe as the language
    grows new syntax.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("A formula holds numbers and field names only.")
        return set()

    if isinstance(node, ast.Name):
        return {node.id}

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BINARY):
            raise FormulaError("Only + - * / % and ** are allowed.")
        return _check(node.left) | _check(node.right)

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _UNARY):
            raise FormulaError("Only a leading + or - is allowed.")
        return _check(node.operand)

    if isinstance(node, ast.IfExp):
        # `a if b > c else d` — the one piece of branching worth having, for
        # tiered commissions and thresholds.
        return _check(node.test) | _check(node.body) | _check(node.orelse)

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or not isinstance(
            node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
        ):
            raise FormulaError("A comparison is one of < <= > >= == != .")
        return _check(node.left) | _check(node.comparators[0])

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise FormulaError(
                f"The only functions available are: {', '.join(sorted(FUNCTIONS))}."
            )
        if node.keywords:
            raise FormulaError("Function arguments are given in order, without names.")
        names: set[str] = set()
        for argument in node.args:
            names |= _check(argument)
        return names

    raise FormulaError("A formula holds numbers, field names and arithmetic only.")


def _evaluate(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        return float(node.value)

    if isinstance(node, ast.Name):
        # A name nobody supplied is zero rather than an error. A board is read
        # across a room with nobody beside it, so a missing figure has to show
        # as nothing rather than take the screen down.
        return float(values.get(node.id) or 0)

    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, values)
        right = _evaluate(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.Mod)):
            # Zero, not an exception and not a blank cell. "Nothing sold out of
            # nothing issued" reads as 0%; an empty cell reads as a fault.
            if not right:
                return 0.0
            return left / right if isinstance(node.op, ast.Div) else left % right
        if isinstance(node.op, ast.Pow):
            try:
                result = left ** right
            except (OverflowError, ZeroDivisionError, ValueError):
                return 0.0
            # A complex result has no place on a scoreboard.
            return float(result) if isinstance(result, (int, float)) else 0.0

    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, values)
        return -value if isinstance(node.op, ast.USub) else value

    if isinstance(node, ast.IfExp):
        return _evaluate(node.body if _truth(node.test, values) else node.orelse, values)

    if isinstance(node, ast.Call):
        arguments = [_evaluate(argument, values) for argument in node.args]
        # Every value here is a float, and `round(x, 2.0)` is a TypeError while
        # `round(x, 2)` is what somebody writing "to the cent" means. Coerced
        # rather than refused, because the distinction is Python's, not theirs.
        if node.func.id == "round" and len(arguments) == 2:
            arguments[1] = int(arguments[1])
        try:
            return float(FUNCTIONS[node.func.id](*arguments))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    return 0.0


def _truth(node: ast.AST, values: dict[str, float]) -> bool:
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        right = _evaluate(node.comparators[0], values)
        operator = node.ops[0]
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        if isinstance(operator, ast.GtE):
            return left >= right
        if isinstance(operator, ast.Eq):
            return left == right
        return left != right
    return bool(_evaluate(node, values))
