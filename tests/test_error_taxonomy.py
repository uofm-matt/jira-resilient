"""Every documented failure branch, exercised.

0.6.0 shipped the taxonomy (`JiraAuthError` / `JiraParseError` / `JiraFetchError` /
`JiraJqlError`) but not its coverage: the four `_probe_next_minute` failure paths and every
`if _is_http_404(...) ... raise` re-raise were unreached, so a mutation swapping any of them
for a swallow left the suite green. These are the oracles for the contract the README states.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests
import responses

from jira_resilient import JiraClient, JiraFetchError, JiraParseError
from jira_resilient.exceptions import JiraJqlError


def _delta(client):
    return list(client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0, tzinfo=UTC)))


# ----- is_authenticated: the two non-401 falsehoods --------------------------


@responses.activate
def test_is_authenticated_network_error_is_false(client, base_url, no_sleep):
    responses.add(
        responses.GET, f"{base_url}/rest/api/2/myself", body=requests.ConnectionError("boom")
    )
    assert client.is_authenticated is False


@responses.activate
def test_is_authenticated_non_json_200_is_false(client, base_url):
    """An SSO/proxy login page answers 200 with HTML — not an authenticated probe."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/myself", body="<html>login</html>")
    assert client.is_authenticated is False


# ----- _probe_next_minute: all four failure paths ----------------------------


@responses.activate
def test_next_minute_probe_400_raises_jql_error(client, base_url):
    """A malformed `extra_filter` reaches the server on the advance probe; a 400 there is a
    rejected QUERY, so it must surface as JiraJqlError with JIRA's messages, not as a fetch
    failure the caller would retry."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        status=400,
        json={"errorMessages": ["Field 'nope' does not exist"]},
    )
    with pytest.raises(JiraJqlError) as ei:
        _delta(client)
    assert ei.value.error_messages == ["Field 'nope' does not exist"]


@responses.activate
def test_next_minute_probe_non_400_raises_fetch_error(client, base_url):
    """Any other transport failure on the probe is a fetch failure, not a query failure —
    the distinction is what tells a caller whether retrying can help."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", status=404)
    with pytest.raises(JiraFetchError):
        _delta(client)


@responses.activate
def test_next_minute_probe_row_without_updated_raises_parse_error(client, base_url):
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"id": "1", "fields": {}}]},
    )
    with pytest.raises(JiraParseError):
        _delta(client)


@responses.activate
def test_next_minute_probe_unparseable_updated_raises_parse_error(client, base_url):
    """The cursor is derived from this string. An unparseable one must stop the scan, not
    silently leave the cursor where it was and re-read the same minute forever."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"id": "1", "fields": {"updated": "last tuesday"}}]},
    )
    with pytest.raises(JiraParseError):
        _delta(client)


@responses.activate
def test_delta_accepts_a_naive_after_ts(client, base_url):
    """A naive cursor is read as UTC rather than rejected — callers persist naive
    timestamps and the alternative is a TypeError deep in the JQL builder."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": []})
    assert list(client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0))) == []


# ----- 404-vs-everything-else on the sub-entity reads ------------------------


@responses.activate
def test_get_changelog_reraises_a_non_404_from_the_paginated_route(client, base_url):
    """Only a 404 is ambiguous (endpoint absent vs issue absent). A 500 must propagate —
    swallowing it would flip the route permanently on one transient failure."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/changelog", status=410)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        client.get_changelog("XX-1")
    assert client._changelog_paginated is True  # route not poisoned
    assert ei.value.response.status_code == 410


@responses.activate
def test_get_changelog_existence_probe_reraises_a_non_404(client, base_url, no_sleep):
    """The disambiguating probe must not report "issue gone" for a transient failure."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/changelog", status=404)
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1", status=500)
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1", status=500)
    with pytest.raises(requests.exceptions.HTTPError):
        client.get_changelog("XX-1")
    assert client._changelog_paginated is True


@responses.activate
def test_get_remote_links_empty_non_list_body_is_no_links(client, base_url):
    """`{}` is JIRA's empty answer here; only a NON-empty non-list body is an error
    envelope worth raising on."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/remotelink", json={})
    assert client.get_remote_links("XX-1") == []


@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/issue/XX-1/watchers", lambda c: c.get_watchers("XX-1")),
        ("/issue/XX-1/votes", lambda c: c.get_voters("XX-1")),
        ("/user", lambda c: c.get_user(username="jdoe")),
        ("/issue/XX-1/properties", lambda c: c.get_issue_properties("XX-1")),
    ],
)
@responses.activate
def test_absence_readers_reraise_non_404(client, base_url, path, call):
    """These four map a 404 to an empty result. A 403 (no permission) or a 500 is NOT
    absence and must not be reported as "no watchers" / "no such user"."""
    responses.add(responses.GET, f"{base_url}/rest/api/2{path}", status=410)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        call(client)
    assert ei.value.response.status_code == 410


@responses.activate
def test_properties_skip_an_entry_without_a_key(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties",
        json={"keys": [{"self": "x"}, {"key": "real"}]},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties/real",
        json={"key": "real", "value": {"a": 1}},
    )
    assert client.get_issue_properties("XX-1") == {"real": {"a": 1}}


@responses.activate
def test_properties_reraise_a_non_404_on_a_value_read(client, base_url):
    """A 404 on one value is a raced deletion (skip it); a 500 is not, and returning a
    partial property map as if complete would silently drop data."""
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties",
        json={"keys": [{"key": "real"}]},
    )
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/properties/real", status=410)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        client.get_issue_properties("XX-1")
    assert ei.value.response.status_code == 410


# ----- resilient fetch: the hub-tier client error ----------------------------


@responses.activate
def test_resilient_fetch_stops_at_a_hub_tier_client_error(client, base_url, no_sleep):
    """A 4xx at the hub tier means the request is wrong, and the minimal tier fetches the
    SAME key — so it fails identically. Fail fast with JiraFetchError instead."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1", status=500)
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1", status=500)
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1", status=410)
    with pytest.raises(JiraFetchError) as ei:
        client.get_issue_resilient("XX-1")
    assert "client error" in str(ei.value)
    assert len(responses.calls) == 3  # no minimal-tier attempt


@responses.activate
def test_hub_search_tier_skips_an_issue_without_a_key(client, base_url, no_sleep):
    """The per-issue issuelinks graft is keyed by `key`; a row without one cannot be
    grafted and must be passed through untouched rather than crashing the page."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", status=500)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", status=500)
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"id": "1"}], "total": 1},
    )
    pages = list(client.search_paged("project = PROJ"))
    assert [p.tier for p in pages] == ["hub"]
    assert "fields" not in pages[0].issues[0]


# ----- the HTTP boundary: what a 2xx body and a 3xx are allowed to do --------

_NON_OBJECT_BODIES = [[], [{"displayName": "x"}], "login required", 12]


@responses.activate
@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=repr)
def test_a_non_object_2xx_body_raises_inside_the_family(client, base_url, body):
    """A 200 that decodes to a list, string or number reaches `.get` and raised
    `AttributeError` — which is neither a `JiraResilientError` nor a `RequestException`,
    so it escaped both boundaries the README tells callers to catch.

    An SSO/proxy page and a JSON error envelope both arrive in exactly this shape.
    """
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/comment", json=body)
    with pytest.raises(JiraParseError):
        client.get_comments("XX-1")


@responses.activate
@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=repr)
def test_the_probes_swallow_a_non_object_body_rather_than_raising(client, base_url, body):
    """The two self-guarding probes promise not to raise, so for them the same body must
    reach their documented fallback instead of the exception above."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/myself", json=body)
    responses.add(responses.GET, f"{base_url}/rest/api/2/serverInfo", json=body)
    unprobed = JiraClient(base_url, pat="test", verify=False)
    assert unprobed.is_authenticated is False
    assert unprobed.server_tz is UTC


@responses.activate
def test_a_redirect_never_becomes_an_authenticated_session_or_a_timezone(client, base_url):
    """Both probes used `session.get`, which follows redirects — so an SSO 302 made
    `is_authenticated` True against a login page, and `server_tz` adopt the LOGIN HOST's
    offset. That offset is rendered into every delta JQL literal, shifting the minute
    window the scan drains. 0.6.1 fixed this for `list_fields` and left the two probes.
    """
    for path in ("myself", "serverInfo"):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/{path}",
            status=302,
            headers={"Location": f"https://sso.example.invalid/{path}"},
        )
    responses.add(
        responses.GET, "https://sso.example.invalid/myself", json={"displayName": "SSO Portal"}
    )
    responses.add(
        responses.GET,
        "https://sso.example.invalid/serverInfo",
        json={"serverTime": "2026-05-19T13:00:00.000+0530"},
    )
    unprobed = JiraClient(base_url, pat="test", verify=False)
    assert unprobed.is_authenticated is False
    assert unprobed.server_tz is UTC
    assert not any("sso.example.invalid" in c.request.url for c in responses.calls)


@responses.activate
def test_an_html_body_on_a_2xx_raises_inside_the_family(client, base_url):
    """The other half of the shape guard: a body that is not JSON *at all*.

    The non-object tests above all send valid JSON of the wrong type. An SSO/proxy login page
    is HTML, which fails at decode rather than at the isinstance check — a different branch,
    and the more likely one in the wild.
    """
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/comment",
        body="<html><body>Please log in</body></html>",
        content_type="text/html",
    )
    with pytest.raises(JiraParseError, match="non-JSON body"):
        client.get_comments("XX-1")
