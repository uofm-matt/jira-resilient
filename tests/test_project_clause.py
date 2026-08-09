"""Scoping properties of `project_clause`, the composition primitive.

The defect this function exists to prevent was found in the wild, in two different downstream
files, both spelled `f'project = "{key}" AND {filter}'`. With a top-level `OR` in the filter
that parses as `(project = "P" AND a) OR b` and returns rows from every project — so the
interesting tests here are DIFFERENTIAL: the hand-rolled string and the primitive are both
read by `tests/jql_model.py`, a precedence-correct evaluator derived from JIRA's grammar
rather than from this library. A string assertion cannot tell those two apart; that is the
whole reason the defect survived a suite that already pinned emitted text.

`UNIVERSE` is borrowed from `test_search_scope` deliberately: same rows, same oracle, so a
leak here means exactly what a leak there means. Its out-of-project rows carry low ids on
purpose — under `ORDER BY id ASC` a broken scope serves them first.
"""

from __future__ import annotations

import jql_model
import pytest
from test_search_scope import UNIVERSE

import jira_resilient
from jira_resilient import build_jql, project_clause
from jira_resilient.exceptions import JiraQueryValidationError

# A small grammar rather than a hand-picked list: every two-atom combination under both
# connectives, each with and without a leading NOT. `project = "OTHER"` is in the atoms
# because it is the worst case — a filter that names a different project outright, which an
# unparenthesized conjunction turns into "OR everything in OTHER".
ATOMS = ['status = "Done"', "labels = urgent", "labels = hot", 'project = "OTHER"', "id > 0"]
FILTERS = [
    f"{neg}{a} {op} {b}"
    for a in ATOMS
    for b in ATOMS
    for op in ("AND", "OR")
    for neg in ("", "NOT ")
]

# The subset that actually exercises the defect: a top-level OR is what escapes.
ESCAPING = [f for f in FILTERS if " OR " in f]


def _rows(jql):
    return sorted(r["key"] for r in UNIVERSE if jql_model.matches(jql, r))


def _intended(project, extra_filter):
    """What the caller asked for, spelled independently of how the primitive spells it."""
    return sorted(
        r["key"]
        for r in UNIVERSE
        if r["project"] == project and jql_model.matches(f"({extra_filter})", r)
    )


def test_no_filter_can_leave_the_project():
    """SCOPE: whatever the filter says, the result stays inside the project.

    The grammar is looped inside the test rather than parametrized over: 100 filters x 7
    properties is 700 test ids for one six-line function, which drowns the suite it belongs
    to. Collecting the failures loses nothing — the assertion still names every filter that
    breaks the property, which is what the ids were carrying.
    """
    leaked = {}
    for f in FILTERS:
        got = _rows(project_clause("PROJ", f))
        if not all(k.startswith("PROJ-") for k in got):
            leaked[f] = got
    assert not leaked, f"filters that left the project: {leaked}"


def test_clause_means_project_and_filter_exactly():
    """Not merely safe — right. The clause selects the intersection, no more and no less."""
    wrong = {f for f in FILTERS if _rows(project_clause("PROJ", f)) != _intended("PROJ", f)}
    assert not wrong, f"filters whose result set is not the intersection: {sorted(wrong)}"


def test_a_filter_can_only_narrow():
    unfiltered = set(_rows(project_clause("PROJ")))
    widened = {
        f: sorted(got - unfiltered)
        for f in FILTERS
        if (got := set(_rows(project_clause("PROJ", f)))) - unfiltered
    }
    assert not widened, f"filters that ADDED rows: {widened}"


def test_the_hand_rolled_clone_leaks_where_the_primitive_does_not():
    """Clone #2 from the field, side by side with its replacement, under one oracle.

    The first assertion is the important one: it fails if the filter set stops reproducing
    the original defect, so this test cannot quietly decay into a tautology.
    """
    toothless = [
        f
        for f in ESCAPING
        if not [k for k in _rows(f'project = "PROJ" AND {f}') if not k.startswith("PROJ-")]
    ]
    assert not toothless, (
        f"no longer exercise the defect — the differential proves nothing: {toothless}"
    )
    wrong = {f for f in ESCAPING if _rows(project_clause("PROJ", f)) != _intended("PROJ", f)}
    assert not wrong, f"the primitive leaked on: {sorted(wrong)}"


@pytest.mark.parametrize("extra_filter", [None, ""])
def test_it_replaces_clone_one_including_its_empty_case(extra_filter):
    """Clone #1 was `base + (f" AND {filter}" if filter else "")`; the conditional folds in."""
    assert project_clause("PROJ", extra_filter) == 'project = "PROJ"'


def test_it_is_build_jql_minus_the_sort():
    """Pins the documented relation so the two cannot drift apart in a later edit."""
    drifted = {
        f
        for f in [None, *FILTERS]
        if build_jql("PROJ", extra_filter=f) != f"{project_clause('PROJ', f)} ORDER BY updated ASC"
    }
    assert not drifted, (
        f"build_jql is no longer project_clause + the sort for: {sorted(map(str, drifted))}"
    )


def test_it_carries_no_ordering():
    """A fragment, not a query — asserted through the oracle's own ORDER BY splitter."""
    ordered = {
        f for f in [None, *FILTERS] if jql_model.split_order_by(project_clause("PROJ", f))[1] != ""
    }
    assert not ordered, f"clause carried an ORDER BY for: {sorted(map(str, ordered))}"


def test_the_docstring_warning_about_appending_or_is_true():
    """The docstring says appending ` OR ...` unbinds the scope again. Keep that honest."""
    contained = [
        f
        for f in ESCAPING
        if not [k for k in _rows(f"{project_clause('PROJ')} OR {f}") if not k.startswith("PROJ-")]
    ]
    assert not contained, f"appending OR no longer escapes for: {contained} — the warning is stale"


def test_rejects_invalid_project_key():
    for bad in ("proj", "PROJ-1", "x", "ABC DEF", ""):
        with pytest.raises(JiraQueryValidationError, match="Invalid project key"):
            project_clause(bad, 'status = "Done"')


@pytest.mark.parametrize(
    "dangerous",
    [
        "status = X; DROP TABLE users",
        "status = X UNION select 1",
        "status = X -- comment",
        "status = X /* injection */",
        "status = X DELETE",
    ],
)
def test_rejects_dangerous_extra_filter(dangerous):
    # Same guard as `build_jql`; `ValueError` is the base a pre-0.6.0 caller catches.
    with pytest.raises(ValueError, match="Unsafe characters"):
        project_clause("PROJ", dangerous)


def test_it_is_reachable_from_the_package_root():
    """Discoverability IS the fix: the consumer who hand-rolled never opened `jql.py`."""
    assert "project_clause" in jira_resilient.__all__
    assert jira_resilient.project_clause is project_clause
