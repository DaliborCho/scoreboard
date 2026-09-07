"""What a customer's formula may say, and what it may not do.

Two halves. The first is arithmetic: does the thing compute what a sales
manager would expect on a wall. The second matters more — this parses text
typed into a form on the public internet, so most of these tests are about
what it refuses rather than what it evaluates.
"""
import pytest

from scoreboard.domain.formula import Expression, FormulaError


def value(text, **values):
    return Expression.parse(text).evaluate(values)


# ---------------------------------------------------------------- arithmetic
def test_the_ordinary_cases():
    assert value("a + b", a=2, b=3) == 5
    assert value("a - b", a=2, b=3) == -1
    assert value("a * b", a=2, b=3) == 6
    assert value("a / b", a=3, b=2) == 1.5
    assert value("(a + b) * 10", a=2, b=3) == 50
    assert value("-a", a=4) == -4


def test_the_formula_that_started_this():
    """`(sold - refunds) / issued * 100` — not expressible as one division."""
    assert value("(sold - refunds) / issued * 100",
                 sold=30, refunds=6, issued=120) == 20.0


def test_a_threshold_changes_the_rate():
    """Tiered commission, which is why comparisons are allowed at all."""
    formula = "net * (0.12 if sold >= 10 else 0.08)"
    assert value(formula, net=100_000, sold=12) == 12_000
    assert value(formula, net=100_000, sold=4) == 8_000


def test_the_shipped_functions_work():
    assert value("min(a, b)", a=3, b=7) == 3
    assert value("max(a, b)", a=3, b=7) == 7
    assert value("abs(a - b)", a=3, b=7) == 4
    assert value("round(a / b, 2)", a=1, b=3) == 0.33


def test_dividing_by_nothing_is_zero_not_an_explosion():
    """A blank cell on a wall reads as a broken screen. Zero is the honest
    reading of "nothing sold out of nothing issued"."""
    assert value("a / b", a=5, b=0) == 0.0
    assert value("(a / b) * 100", a=5, b=0) == 0.0
    assert value("a % b", a=5, b=0) == 0.0


def test_a_name_nobody_supplied_reads_as_zero():
    """The same reason: a missing figure must not take the board down."""
    assert value("issued + missing", issued=7) == 7


def test_an_absurd_power_does_not_raise():
    assert value("a ** b", a=10, b=1_000_000) == 0.0


# ---------------------------------------------------------------- refusals
@pytest.mark.parametrize("text", [
    "__import__('os').system('id')",
    "().__class__.__bases__",
    "open('/etc/passwd').read()",
    "exec('x=1')",
    "eval('1+1')",
    "[x for x in range(10)]",
    "{'a': 1}",
    "lambda: 1",
    "a if b else c if d else e" .replace("else e", "else __import__('os')"),
    "a.b",
    "a[0]",
    "a and b",
    "not a",
    "'text'",
    "f'{a}'",
    "a := 1",
    "print(a)",
    "globals()",
])
def test_anything_that_is_not_arithmetic_is_refused(text):
    """An allowlist, so syntax nobody has thought about is refused by default.

    The person typing this is a customer's sales manager, not a programmer,
    and the box is reachable from the public internet.
    """
    with pytest.raises(FormulaError):
        Expression.parse(text)


def test_a_formula_may_only_name_fields_that_exist():
    with pytest.raises(FormulaError) as caught:
        Expression.parse("sold / invented", {"sold", "issued"})
    assert "invented" in str(caught.value)


def test_a_field_defined_later_is_refused_by_name():
    """Evaluation is in order, so a later name would silently read zero."""
    with pytest.raises(FormulaError):
        Expression.parse("later / sold", {"sold"})


def test_an_empty_formula_is_refused():
    with pytest.raises(FormulaError):
        Expression.parse("   ")


def test_a_formula_longer_than_a_line_is_refused():
    with pytest.raises(FormulaError):
        Expression.parse("a + " * 200 + "a")


def test_broken_syntax_says_so_in_plain_words():
    with pytest.raises(FormulaError) as caught:
        Expression.parse("a + + )")
    assert "not arithmetic" in str(caught.value)


# ---------------------------------------------------------------- shorthand
def test_the_three_box_form_becomes_the_same_expression():
    from scoreboard.domain.metrics import ratio

    close_rate = ratio("sold_leads", "issued_leads", 100.0)
    assert close_rate.evaluate({"sold_leads": 10, "issued_leads": 40}) == 25.0
    # Provenance is kept so the console can still offer three labelled boxes
    # for what is genuinely three labelled boxes.
    assert close_rate.shorthand == ("sold_leads", "issued_leads", 100.0)


def test_a_scale_of_one_leaves_the_expression_plain():
    from scoreboard.domain.metrics import ratio

    assert ratio("net_split", "issued_leads").text == "net_split / issued_leads"
