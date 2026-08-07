"""What each degradation tier actually ASKS FOR — the request payloads, not the tier labels.

The library exists to survive a hub issue whose `issuelinks` array is too large to serialize:
tier 2 re-fetches that issue WITHOUT the field, tier 3 falls back to a small named field set.
Every existing tier test asserts the tier LABEL on the returned result, and the mock hands that
back unconditionally — so a tier 2 that re-requested the exact payload that just timed out, and
a tier 3 that asked for `*all`, both passed the entire suite. A mutation battery confirmed it:
seven mutants rewriting these payloads survived a fully green run. Nothing in the suite asserted
a `/search` body at all, and only `get_user`'s query string was ever checked on a GET.

So each test here captures the outgoing request and asserts the DISTINGUISHING property of the
tier that sent it: what tier 2 must exclude, what tier 3 must not ask for, what the two cheap
queries must stay cheap about. Not byte-for-byte payloads — adding a field to the minimal set is
a legitimate change; asking for everything at the tier whose job is to ask for less is the bug.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import responses

# What a degraded tier exists to avoid sending: `*all` pulls every custom field, and each named
# field is on its own big enough to blow the timeout on a pathological issue.
_OVERSIZED = {"*all", "description", "comment", "attachment", "issuelinks"}

# A sample of what a warehouse loader still needs after degradation. The point is that the
# minimal tier NAMES its fields, not that it names exactly these three — extend the set freely.
_CORE = {"summary", "status", "updated"}

# Timed-out attempts before the tier under test gets its turn: 2 per tier, `fast_fail`'s budget.
_TIMEOUTS_BEFORE = {"hub": 2, "minimal": 4}


def _tokens(spec: str | list[str]) -> set[str]:
    """A `fields` spec as a token set, whether it arrived as a GET comma-string or a POST list."""
    return set(spec.split(",") if isinstance(spec, str) else spec)


def _issue_gets() -> list[dict[str, str]]:
    """Query of every `GET /issue/…`, in call order. Timed-out attempts are recorded too."""
    gets = [c.request for c in responses.calls if c.request.method == "GET"]
    return [
        {k: v[0] for k, v in parse_qs(urlparse(r.url).query, keep_blank_values=True).items()}
        for r in gets
    ]


def _search_posts() -> list[dict[str, Any]]:
    """Body of every `POST /search`, in call order."""
    return [json.loads(c.request.body) for c in responses.calls if c.request.method == "POST"]


def _drive_issue_to(client, base_url, tier: str) -> list[dict[str, str]]:
    """Force `get_issue_resilient` down to `tier`, then return its TIER ATTEMPTS in order.

    The hub tier's separate `fields=issuelinks` supplement is not a tier attempt and is dropped:
    it is the only `/issue` request that sends no `expand`.
    """
    url = f"{base_url}/rest/api/2/issue/HUB-1"
    for _ in range(_TIMEOUTS_BEFORE[tier]):
        responses.add(responses.GET, url, body=requests.exceptions.Timeout("simulated"))
    responses.add(responses.GET, url, json={"key": "HUB-1", "id": "1", "fields": {}})
    responses.add(responses.GET, url, json={"fields": {"issuelinks": [{"id": "9999"}]}})
    assert client.get_issue_resilient("HUB-1").tier == tier  # the drive landed where intended
    return [q for q in _issue_gets() if "expand" in q]


def _drive_search_to(client, base_url, tier: str) -> list[dict[str, Any]]:
    """Force one `/search` page down to `tier`, then return that page's TIER ATTEMPTS in order.

    Every attempt at a page re-sends that page's JQL, so the first body's JQL selects them; the
    next page's carries an advanced `id` cursor and is excluded.
    """
    url = f"{base_url}/rest/api/2/search"
    for _ in range(_TIMEOUTS_BEFORE.get(tier, 0)):
        responses.add(responses.POST, url, body=requests.exceptions.Timeout("simulated"))
    issue = {"key": "HUB-1", "id": "1", "fields": {}}
    responses.add(responses.POST, url, json={"issues": [issue], "names": {}, "schema": {}})
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/HUB-1",
        json={"fields": {"issuelinks": [{"id": "9999"}]}},
    )
    responses.add(responses.POST, url, json={"issues": [], "names": {}, "schema": {}})
    assert [p.tier for p in client.search_seek("HUB")] == [tier]
    bodies = _search_posts()
    return [b for b in bodies if b["jql"] == bodies[0]["jql"]]


# ----- single-issue fetch: get_issue_resilient's three tiers ------------------


@responses.activate
def test_hub_tier_refetches_the_issue_without_issuelinks(client, base_url, no_sleep):
    """Tier 2's entire reason to exist is dropping the field that blew tier 1's timeout.

    A tier 2 that re-sends `fields=*all` costs a second timeout and hands tier 3 the same
    failure, while `ResilientFetchResult.tier` still reads "hub" and every existing test still
    passes. The mutation that does exactly that survived the suite.
    """
    attempts = _drive_issue_to(client, base_url, "hub")
    full, hub = attempts[0], attempts[-1]
    assert "-issuelinks" in _tokens(hub["fields"]), (
        f"tier 2 must exclude issuelinks; it asked for fields={hub['fields']!r}"
    )
    assert hub["fields"] != full["fields"], (
        f"tier 2 re-sent tier 1's payload verbatim ({full['fields']!r}) — it cannot succeed "
        "where tier 1 just timed out"
    )


@responses.activate
def test_minimal_tier_asks_for_named_fields_not_the_wildcard(client, base_url, no_sleep):
    """Tier 3 is the last chance to get ANY row back, so it must be the cheapest request made.

    The surviving mutant replaced the `get_issue_minimal` call with a full `*all` fetch: the
    result is labelled "minimal", callers log a degradation, and the request is heavier than
    the two that already timed out. Also pins the absent changelog — the other half of what
    makes this tier cheap.
    """
    minimal = _drive_issue_to(client, base_url, "minimal")[-1]
    asked = _tokens(minimal["fields"])
    assert not asked & _OVERSIZED, f"tier 3 asked for {sorted(asked & _OVERSIZED)}"
    assert asked >= _CORE, f"tier 3 dropped {sorted(_CORE - asked)} — the row is unusable"
    assert "changelog" not in minimal["expand"]


@responses.activate
def test_get_issue_minimal_defaults_to_a_named_field_set(client, base_url):
    """The same guarantee at the public entry point, which callers reach directly.

    `fields` is a defaulted keyword, so a mutated default is invisible to every caller and to
    tier 3 alike; the method still returns an issue and the docstring still promises "a small
    field set".
    """
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/HUB-1", json={"key": "HUB-1"})
    client.get_issue_minimal("HUB-1")
    [sent] = _issue_gets()
    asked = _tokens(sent["fields"])
    assert not asked & _OVERSIZED, f"the minimal default asked for {sorted(asked & _OVERSIZED)}"
    assert asked >= _CORE, f"the minimal default dropped {sorted(_CORE - asked)}"


# ----- listing: _search_one_page's three tiers --------------------------------


@responses.activate
def test_search_full_tier_requests_the_changelog_expansion(client, base_url):
    """`SearchPage(tier="full")` is the promise that nothing was dropped, changelog included.

    A consumer building transition histories from search pages is the reason the expansion is
    there. Drop it and every issue reports an empty history — indistinguishable from a project
    where nothing ever transitioned, on a page that labels itself full.
    """
    [full] = _drive_search_to(client, base_url, "full")
    assert "changelog" in full["expand"], f"full tier expanded only {full['expand']!r}"


@responses.activate
def test_search_hub_tier_drops_issuelinks_from_the_page_request(client, base_url, no_sleep):
    """The listing-layer twin of the hub fetch: one mega-hub in a page must not poison the page.

    Tier 2 re-queries the same JQL minus `issuelinks` and grafts them back per issue. If the
    re-query still asks for them the page fails identically, and the seek cursor stops.
    """
    attempts = _drive_search_to(client, base_url, "hub")
    full, hub = attempts[0], attempts[-1]
    assert "-issuelinks" in hub["fields"], f"hub tier asked for fields={hub['fields']!r}"
    assert hub["fields"] != full["fields"], "hub tier re-sent the full-tier payload verbatim"


@responses.activate
def test_search_minimal_tier_asks_for_named_fields_not_the_wildcard(client, base_url, no_sleep):
    """Tier 3 keeps the seek cursor advancing past a page the server cannot serialize.

    A minimal tier that asks for `*all` cannot do that — it fails wherever tier 1 failed, so the
    scan raises instead of degrading, and the whole three-tier structure buys nothing.
    """
    minimal = _drive_search_to(client, base_url, "minimal")[-1]
    asked = _tokens(minimal["fields"])
    assert not asked & _OVERSIZED, f"search tier 3 asked for {sorted(asked & _OVERSIZED)}"
    assert asked >= _CORE, f"search tier 3 dropped {sorted(_CORE - asked)}"
    assert "changelog" not in minimal["expand"]


# ----- the two queries that are only safe while they stay cheap ---------------


@responses.activate
def test_list_keys_asks_for_nothing_but_the_key(client, base_url):
    """`list_keys` pages 1000 rows at a time, a size only survivable because a row is one field.

    Mutate the field list to `*all` and the method that "never times out" becomes the heaviest
    request the client makes — 1000 fully-hydrated issues per page — while still returning the
    same key list from a mock, and from a small project in production.
    """
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "HUB-1"}], "total": 1},
    )
    assert client.list_keys('project = "HUB"') == ["HUB-1"]
    [body] = _search_posts()
    # A subset check, not equality: also asking for `id` would be a legitimate change (seek
    # by id), while asking for anything oversized is the defect.
    asked = set(body["fields"])
    assert not asked & _OVERSIZED, f"list_keys asked for {sorted(asked & _OVERSIZED)}"
    assert "key" in asked, f"list_keys asked for {body['fields']!r}"


@responses.activate
def test_next_minute_probe_stays_one_row_of_one_field(client, base_url):
    """The delta scan fires this probe once per drained minute — its cost is multiplied by
    history depth, so it reads the first row's `updated` and nothing else.

    Both halves were mutated and both survived: `maxResults` 1 → 100 and `fields` → `["*all"]`
    turn a one-row lookup into a hydrated 100-issue fetch per minute of backlog. The probe reads
    `issues[0]` either way, so every assertion in the suite is unaffected.
    """
    url = f"{base_url}/rest/api/2/search"
    row = {"key": "HUB-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
    responses.add(responses.POST, url, json={"issues": [row], "names": {}, "schema": {}})
    responses.add(responses.POST, url, json={"issues": []})
    list(client.search_seek("HUB", after_ts=datetime(2026, 5, 18, 7, 30, tzinfo=UTC)))
    # The probe is the only /search caller that sends no `expand`; the drain always sends one.
    [probe] = [b for b in _search_posts() if "expand" not in b]
    # maxResults stays exact — one row IS the contract. `fields` is a subset check, since
    # also reading `key` for a log line would be legitimate.
    assert probe["maxResults"] == 1, f"probe asked for {probe['maxResults']} rows, not 1"
    asked = set(probe["fields"])
    assert not asked & _OVERSIZED, f"probe asked for {sorted(asked & _OVERSIZED)}"
    assert "updated" in asked, f"probe asked for fields={probe['fields']!r}"
