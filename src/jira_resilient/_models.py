"""Lightweight return types — NamedTuples for attribute access without dataclass overhead."""

from __future__ import annotations

from typing import Literal, NamedTuple

Tier = Literal["full", "hub", "minimal"]


class SearchPage(NamedTuple):
    """One page of `/rest/api/2/search` results.

    `names` and `schema` come from the response's optional expansions of the same
    name (populated when the search was issued with `expand=names,schema`). Both
    are field-id keyed; consumers building a field catalog typically merge them
    across pages.
    """

    issues: list[dict]
    names: dict[str, str]
    schema: dict[str, dict]


class ResilientFetchResult(NamedTuple):
    """The outcome of `JiraClient.get_issue_resilient(...)`.

    `tier` records WHICH fallback path succeeded — callers usually log this so
    operators know which keys are pathological:
        - "full"    — fields=*all with changelog+names+schema (default path)
        - "hub"     — fields=*all,-issuelinks plus a separate issuelinks fetch
        - "minimal" — minimal field set (description + custom_fields lost)
    """

    issue: dict
    tier: Tier
