"""`except JiraResilientError` is a boundary the README promises; nothing pinned it.

The only prior assertion about the hierarchy was that a NAME appears in `__all__`, which says
nothing about bases. Rewriting `class JiraAuthError(JiraResilientError)` to
`class JiraAuthError(Exception)` left all 187 tests green while breaking every caller that
guards the family with one clause. Four such severances survived a mutation battery —
`JiraAuthError`, `JiraParseError`, `JiraJqlError`, and the `JiraResilientError` half of
`JiraQueryValidationError` (a doctest pins only its `ValueError` half).

The structural check enumerates the module instead of listing names, so an exception added
later is pinned the day it is written. An enumeration that quietly stops matching is the exact
failure mode this audit exists to find, so the first test guards the enumerator itself.
"""

from __future__ import annotations

import inspect

import pytest

from jira_resilient import (
    JiraResilientError,
    exceptions,
)

FAMILY = [
    obj
    for _, obj in inspect.getmembers(exceptions, inspect.isclass)
    if issubclass(obj, BaseException) and obj.__module__ == exceptions.__name__
]

# What README "Exceptions" documents as of 0.6.1. A floor, never a ceiling.
DOCUMENTED = {
    "JiraAuthError",
    "JiraFetchError",
    "JiraJqlError",
    "JiraParseError",
    "JiraQueryValidationError",
    "JiraResilientError",
}


def test_the_family_enumeration_finds_every_documented_exception():
    """Guards the guard. Every parametrized test below iterates `FAMILY`; a filter that stops
    matching turns all of them into vacuous passes without failing anything. A name-subset
    assertion is the stronger form of a minimum count — it fails on an emptied enumeration and
    on a single dropped class alike, and still lets the family grow.
    """
    assert {c.__name__ for c in FAMILY} >= DOCUMENTED


@pytest.mark.parametrize("exc", FAMILY, ids=lambda c: c.__name__)
def test_every_exception_in_the_package_descends_from_the_base(exc):
    """README: "Catch `JiraResilientError` for library-raised failures." That is a claim about
    BASES, and no other test reads one — `__all__` membership is satisfied by a class with any
    base at all, which is why severing four of them changed nothing.
    """
    assert issubclass(exc, JiraResilientError)
