"""Which timeout reaches which tier — the thing `JiraClient(timeout=...)` silently did not do.

Observed at `session.request`, the last point before the transport, because that is where the
lie was: the ladders passed a hardcoded 60 and the constructor's `timeout` never arrived.
Asserting on `client.fast_fail_timeout` instead would only prove the attribute was stored.

The all-tiers-fail path is used throughout: it walks every rung in one call, so one recording
covers tier 1, tier 2 and tier 3 budgets in order.
"""

from __future__ import annotations

import pytest
import requests

from jira_resilient import JiraClient, JiraFetchError

_MINIMAL_TIER_TIMEOUT = 60  # get_issue_minimal's own fixed budget, 3 attempts


def _record_timeouts(client: JiraClient, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Capture the `timeout` of every request the client attempts, and fail them all so the
    ladder walks to the bottom. `ConnectionError` (not HTTPError) is what a 4xx-free network
    failure looks like, which is the only shape that degrades rather than fast-failing."""
    seen: list[int] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        seen.append(kwargs["timeout"])
        raise requests.ConnectionError("simulated")

    monkeypatch.setattr(client.session, "request", request)
    return seen


def test_client_timeout_does_not_raise_the_tier1_budget(monkeypatch, no_sleep):
    """The deliberate half of the design, pinned so a later "fix" cannot quietly undo it.
    `timeout=300` says "wait for my hub issues"; the ladder says "quit early so tier 2 can
    run". Tier 2 is what actually fetches a hub issue (measured: full failed, links-only
    succeeded in 167.2s), so the ladder wins and 300 must not reach tier 1."""
    client = JiraClient("https://j", "t", timeout=300)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("HUB-1")
    assert seen[:4] == [60, 60, 60, 60]  # tier 1 and tier 2, twice each
    assert 300 not in seen


def test_fast_fail_timeout_is_the_knob_for_tier1(monkeypatch, no_sleep):
    """The escape hatch that replaces the lie: a caller who has measured their own instance
    moves the tier-1 budget explicitly, instead of setting `timeout` and getting 60 anyway."""
    client = JiraClient("https://j", "t", timeout=300, fast_fail_timeout=180)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("HUB-1")
    assert seen[:4] == [180, 180, 180, 180]


def test_fast_fail_timeout_is_clamped_to_the_client_timeout(monkeypatch, no_sleep):
    """A tier whose job is to fail FASTER than a normal read must never wait longer than one.
    Before, a caller with a 20s SLA still got 60s tier-1 requests. The clamp is that
    invariant only, NOT a global ceiling — tier 3 keeps its own fixed budget below."""
    client = JiraClient("https://j", "t", timeout=20)
    assert client.fast_fail_timeout == 20
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("HUB-1")
    assert seen[:4] == [20, 20, 20, 20]
    assert seen[4:] == [_MINIMAL_TIER_TIMEOUT] * 3


def test_fast_fail_attempts_are_clamped_to_max_attempts(monkeypatch, no_sleep):
    """`max_attempts=1` means "do not retry anything". Tier 1's own budget of 2 attempts is a
    ceiling to fit under, not a floor to override."""
    client = JiraClient("https://j", "t", max_attempts=1, fast_fail_timeout=45)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("HUB-1")
    assert seen.count(45) == 2  # one attempt at tier 1, one at tier 2 — not two apiece


def test_default_client_keeps_two_attempts_per_tier(monkeypatch, no_sleep):
    """The counterpart to the clamp: an ordinary client is unchanged by this release."""
    client = JiraClient("https://j", "t", fast_fail_timeout=45)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("HUB-1")
    assert seen.count(45) == 4


def test_search_ladder_uses_the_same_budget(monkeypatch, no_sleep):
    """`_search_one_page` carried its own copy of the hardcoded budget, so fixing only
    `get_issue_resilient` would have left half the library lying. All three search tiers use
    the fast-fail budget."""
    client = JiraClient("https://j", "t", timeout=300, fast_fail_timeout=90)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(JiraFetchError):
        client._search_one_page('project = "X"', page_size=20)
    assert seen == [90] * 6  # three tiers, two attempts each


def test_get_issuelinks_is_not_clamped(monkeypatch, no_sleep):
    """The hub rescue keeps the LONGEST budget in the library, deliberately above `timeout`.
    A links-only fetch measured 167.2s where the full payload timed out, so clamping it to a
    120s client timeout would break the exact case the ladder exists for."""
    client = JiraClient("https://j", "t", timeout=120)
    seen = _record_timeouts(client, monkeypatch)
    with pytest.raises(requests.ConnectionError):
        client.get_issuelinks("HUB-1")
    assert seen == [600, 600]
