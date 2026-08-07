"""`list_fields` on the hardened HTTP path — the contract every other read already had."""

from __future__ import annotations

import time

import pytest
import requests
import responses

from jira_resilient import JiraClient, JiraParseError
from jira_resilient.exceptions import JiraAuthError


@pytest.fixture
def base_url() -> str:
    return "https://jira.example.com"


@pytest.fixture
def client(base_url):
    return JiraClient(base_url, pat="test", verify=False)


# ----- list_fields goes through the hardened HTTP path ---------------------


@responses.activate
def test_list_fields_returns_the_catalog(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/field",
        json=[{"id": "summary", "name": "Summary"}],
    )
    assert client.list_fields() == [{"id": "summary", "name": "Summary"}]


@responses.activate
def test_list_fields_401_raises_jira_auth_error(client, base_url):
    """An expired/wrong PAT must surface as JiraAuthError like every other read — a caller
    guarding the JiraResilientError family should not have to also catch requests.HTTPError
    for this one method."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", status=401)
    with pytest.raises(JiraAuthError):
        client.list_fields()


@responses.activate
def test_list_fields_403_raises_jira_auth_error(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", status=403)
    with pytest.raises(JiraAuthError):
        client.list_fields()


@responses.activate
def test_list_fields_retries_5xx(client, base_url, monkeypatch):
    """A transient 503 is retried with the app-level 5xx backoff instead of being raised
    on the first attempt."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", status=503)
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", json=[{"id": "summary"}])
    assert client.list_fields() == [{"id": "summary"}]
    assert sleeps == [30]


@responses.activate
def test_list_fields_honors_retry_after(client, base_url, monkeypatch):
    """A rate-limited catalog read waits exactly what the server asked for."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(
        responses.GET, f"{base_url}/rest/api/2/field", status=429, headers={"Retry-After": "7"}
    )
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", json=[])
    assert client.list_fields() == []
    assert sleeps == [7.0]


@responses.activate
def test_list_fields_5xx_budget_is_three_attempts(client, base_url, monkeypatch):
    """Bounded on purpose: a hard-down server costs 3 calls / 90s of backoff, not the
    client-wide 5 / 450s. This is a startup catalog read, not a per-issue fetch."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    for _ in range(5):
        responses.add(responses.GET, f"{base_url}/rest/api/2/field", status=503)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        client.list_fields()
    assert ei.value.response.status_code == 503  # the real 503, not a bare RequestException
    assert len(responses.calls) == 3
    assert sleeps == [30, 60]


@responses.activate
def test_list_fields_does_not_follow_an_sso_redirect(client, base_url):
    """The proxy/SSO 302 http.py's redirect rejection exists to catch. Following it made the
    login page's own 200 body the return value of list_fields."""
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/field",
        status=302,
        headers={"Location": f"{base_url}/sso/login"},
    )
    responses.add(responses.GET, f"{base_url}/sso/login", json={"error": "login required"})
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        client.list_fields()
    assert ei.value.response.status_code == 302
    assert [c.request.url for c in responses.calls] == [f"{base_url}/rest/api/2/field"]


@responses.activate
def test_list_fields_non_json_200_raises_parse_error(client, base_url):
    """Preserved: an SSO/proxy HTML page served with HTTP 200 is a parse failure, not an
    empty catalog."""
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/field",
        body="<html>login</html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(JiraParseError):
        client.list_fields()


@responses.activate
def test_list_fields_non_list_200_raises_parse_error(client, base_url):
    """A JSON error envelope with HTTP 200 must not be handed back as the catalog: the
    declared return type is list[dict] and callers iterate it."""
    responses.add(responses.GET, f"{base_url}/rest/api/2/field", json={"errorMessages": ["nope"]})
    with pytest.raises(JiraParseError):
        client.list_fields()
