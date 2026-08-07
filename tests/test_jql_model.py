"""The oracle's own oracle: the properties of `tests/jql_model.py` itself.

`tests/test_search_scope.py` proves the 0.6.0 scope fix by asking `jql_model` what a query
MEANS rather than what string it emits. Nothing asked what `jql_model` means. The audit priced
that gap: apply the real scope defect (drop the parentheses in `build_full_scan_jql`) AND
invert this evaluator's AND/OR precedence, and the full suite goes green — the two errors
cancel. Either one alone is caught. So the scope suite's entire defect-detection power rests on
the dozen lines pinned below.

Three claims `jql_model`'s docstring makes, each load-bearing:

  PRECEDENCE  NOT, then AND, then OR — checked as a truth table over every assignment of the
              atoms rather than on one example, and against the parse tree itself.
  REFUSAL     an unmodeled field or operator raises rather than evaluating to anything.
  MEMBERSHIP  `labels = x` asks whether x is among them, not whether it equals all of them.

Precedence reference (tightest first: NOT, AND, OR):
https://confluence.atlassian.com/jiracorecloud/advanced-searching-operators-reference-765593677.html
"""

from __future__ import annotations

from itertools import product

import jql_model
import pytest

_BOOLS2 = list(product((False, True), repeat=2))
_BOOLS3 = list(product((False, True), repeat=3))

_CMP_A = ("cmp", "labels", "=", "a")
_CMP_B = ("cmp", "labels", "=", "b")
_CMP_C = ("cmp", "labels", "=", "c")


def _row(**atoms: bool) -> dict:
    """A universe row carrying exactly the atoms that are true.

    `labels` is the oracle's only set-valued field, so label membership supplies the only atoms
    that vary independently — three `status =` clauses could never be true at once, and a truth
    table whose rows cannot all exist proves nothing about the ones it skipped.
    """
    return {
        "id": 1,
        "key": "PROJ-1",
        "project": "PROJ",
        "status": "Open",
        "labels": [name for name, present in atoms.items() if present],
    }


def _parse(jql: str) -> tuple:
    return jql_model._Parser(jql_model._tokenize(jql)).parse()


# ----- PRECEDENCE ------------------------------------------------------------


@pytest.mark.parametrize(("a", "b", "c"), _BOOLS3)
def test_and_binds_tighter_than_or(a, b, c):
    """`a AND b OR c` means `(a AND b) OR c`, on all eight assignments rather than on one.

    Swapping the two parser levels is invisible to every example that happens to agree, and six
    of the eight do agree. Only a=F b=F c=T and a=F b=T c=T separate them, so a hand-picked
    example has a one-in-four chance of catching the swap.
    """
    assert jql_model.matches("labels = a AND labels = b OR labels = c", _row(a=a, b=b, c=c)) == (
        (a and b) or c
    )


@pytest.mark.parametrize(("a", "b"), _BOOLS2)
def test_not_binds_tighter_than_and(a, b):
    """`NOT a AND b` means `(NOT a) AND b`, not `NOT (a AND b)`.

    The two readings differ exactly where b is false — the half of the table a scope test is
    least likely to include, since its filters are written to select rows, not reject them.
    """
    assert jql_model.matches("NOT labels = a AND labels = b", _row(a=a, b=b)) == ((not a) and b)


@pytest.mark.parametrize(("a", "b", "c"), _BOOLS3)
def test_parentheses_override_precedence(a, b, c):
    """The whole 0.6.0 fix in `build_full_scan_jql` is a pair of parentheses. An oracle that
    parsed them as decoration would ratify the defect it was written to catch."""
    assert jql_model.matches("labels = a AND (labels = b OR labels = c)", _row(a=a, b=b, c=c)) == (
        a and (b or c)
    )


def test_the_scope_defect_is_visible_to_this_oracle():
    """The meta-property the audit found missing: the scoped scan and the bare one must not
    mean the same thing.

    `test_search_scope` can only catch a dropped paren on a row where the two readings diverge,
    and this is that row — OTHER-1, copied from the scope suite's own UNIVERSE: out of project,
    and carrying the label that the trailing OR arm matches on. Invert the precedence and the
    two strings become synonyms, the divergence disappears, and the scope suite passes with the
    real defect applied. That is exactly what the mutation battery observed.
    """
    bare = 'project = "PROJ" AND status = "Done" OR labels = urgent'
    scoped = 'project = "PROJ" AND (status = "Done" OR labels = urgent)'
    leaked = {"id": 1, "key": "OTHER-1", "project": "OTHER", "status": "Open", "labels": ["urgent"]}
    assert jql_model.matches(bare, leaked) is True
    assert jql_model.matches(scoped, leaked) is False


@pytest.mark.parametrize(
    ("jql", "tree"),
    [
        ("labels = a AND labels = b OR labels = c", ("or", ("and", _CMP_A, _CMP_B), _CMP_C)),
        ("labels = a OR labels = b AND labels = c", ("or", _CMP_A, ("and", _CMP_B, _CMP_C))),
    ],
)
def test_parse_tree_shape(jql, tree):
    """Precedence read structurally, because the truth tables cannot read it unambiguously.

    Those tables are built from `labels` clauses, so a membership defect and a precedence
    defect both surface as a wrong answer. These two assert the shape directly, which a
    membership bug cannot forge.
    """
    assert _parse(jql) == tree


# ----- REFUSAL ---------------------------------------------------------------


def test_unmodeled_field_raises():
    """A permissive oracle hides the rows it exists to detect. If `assignee = bob` evaluated
    false instead of raising, every SCOPE assertion over a filter naming it would hold
    vacuously — nothing selected, nothing leaked, nothing learned."""
    with pytest.raises(ValueError, match="does not model field 'assignee'"):
        jql_model.matches("assignee = bob", _row())


def test_unmodeled_operator_raises():
    """The same contract on the operator axis. `~` lexes — it is in the token table — but has
    no evaluation rule, so it must refuse rather than fall through to a default."""
    with pytest.raises(ValueError, match="does not model operator '~'"):
        jql_model.matches('status ~ "Done"', _row())


def test_refusal_is_short_circuit_dependent():
    """The refusal is not total, which matters when reading a green run.

    `_evaluate` delegates to Python's `and`, so an unmodeled clause is only reached on rows
    whose left-hand side already matched. One query therefore raises for a row in the project
    and answers False for the next row out of it. A filter naming a field the oracle does not
    model looks like it works until an in-project row arrives — and in a suite that seeds few
    in-project rows, that can be never.
    """
    jql = 'project = "PROJ" AND assignee = bob'
    with pytest.raises(ValueError, match="does not model field 'assignee'"):
        jql_model.matches(jql, _row())
    assert jql_model.matches(jql, _row() | {"project": "OTHER"}) is False


# ----- MEMBERSHIP ------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "jql", "expected"),
    [
        (["urgent"], "labels = urgent", True),
        (["urgent", "hot"], "labels = urgent", True),
        (["hot", "urgent"], "labels = urgent", True),
        (["hot"], "labels = urgent", False),
        ([], "labels = urgent", False),
        (["urgent"], "labels = urg", False),
        (["urgent", "hot"], "labels != urgent", False),
        (["hot"], "labels != urgent", True),
        ([], "labels != urgent", True),
    ],
)
def test_labels_is_membership_not_equality(labels, jql, expected):
    """`labels` is set-valued: `labels = urgent` means "urgent is among them".

    Equality against the whole list answers False for every multi-label row, and for
    single-label rows too since `"urgent" != ["urgent"]` — which would silently empty out the
    `labels = urgent OR labels = hot` filters the scope suite leans on, leaving SCOPE and
    NARROWING to pass over an empty result set. Position must not matter, and membership must
    not decay into a substring test.
    """
    assert jql_model.matches(jql, _row() | {"labels": labels}) is expected
