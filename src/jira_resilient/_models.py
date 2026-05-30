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

    `tier` records WHICH tier the search request fell to when fetching this page
    (mirrors `ResilientFetchResult.tier`). Defaults to "full"; degrades to "hub"
    when a single hub issue's `issuelinks` blew the page payload, and "minimal"
    when even that wasn't enough. Callers usually log the tier so operators see
    which projects are tipping toward pathological.

    `fallback` is True when the page came from `search_seek`'s post-reindex
    id-ordered recovery scan rather than the normal time cursor. Incremental
    callers (delta sync) use this to stop early once the recovery scan stops
    surfacing changes — the time cursor is unreliable post-reindex, so the
    fallback re-scans the whole project, which is wasteful for a delta.
    """

    issues: list[dict]
    names: dict[str, str]
    schema: dict[str, dict]
    tier: Tier = "full"
    fallback: bool = False


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
