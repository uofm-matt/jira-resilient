"""Exception hierarchy for jira-resilient.

Everything the library raises deliberately inherits from JiraResilientError, so one
`except JiraResilientError` clause catches the family. That is NOT total coverage of what
can escape a call: the thin endpoint wrappers let `requests.RequestException` propagate
unwrapped (see README's error-handling section), and only the resilient paths wrap it and
chain via `from`. Catch both if you need a hard boundary.
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


class JiraQueryValidationError(ValueError, JiraResilientError):
    """A project key or `extra_filter` was rejected before any request was sent.

    Inherits BOTH so neither kind of caller is surprised: `except ValueError` kept working
    across 0.6.0 (these checks raised bare ValueError through 0.5.0), and
    `except JiraResilientError` now catches it too. Before 0.6.0 the full-scan path skipped
    these checks entirely and a bad filter surfaced as a server-side 400 (JiraJqlError), so
    without the second base a caller guarding the family would newly see an escape.
    """


class JiraJqlError(JiraResilientError):
    """JIRA rejected the JQL itself (HTTP 400 from /search).

    Distinct from JiraFetchError: a 400 means the QUERY is wrong, not that
    the connection/payload is wrong. Falling through to lower fetch tiers
    (which use the same JQL) can't help. `error_messages` carries JIRA's
    `errorMessages` array verbatim so callers can introspect the rejection.
    """

    def __init__(self, message: str, error_messages: list[str] | None = None):
        super().__init__(message)
        self.error_messages = error_messages or []
