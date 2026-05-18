"""Exception hierarchy for jira-resilient.

All library-raised exceptions inherit from JiraResilientError, so callers can catch
the whole family with one `except JiraResilientError` clause. Underlying
`requests.RequestException` is wrapped (and chained via `from`) on fetch failures.
"""
from __future__ import annotations


class JiraResilientError(Exception):
    """Base class for all jira-resilient exceptions."""


class JiraAuthError(JiraResilientError):
    """Authentication or authorization failed (401, 403)."""


class JiraParseError(JiraResilientError):
    """A response was 2xx but didn't contain the fields we expected.

    Distinct from JiraFetchError: the network worked, but the payload was malformed
    in a way the library can't recover from (e.g. an issue page missing both
    `fields.updated` and `key`).
    """


class JiraFetchError(JiraResilientError):
    """All retry attempts (or all fallback tiers) exhausted without success."""
