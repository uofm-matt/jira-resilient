"""Both `search_seek` branches fail the same way on an unusable cursor id."""

from __future__ import annotations

import pytest
import responses

from jira_resilient import JiraClient, JiraParseError


@pytest.fixture
def base_url() -> str:
    return "https://jira.example.com"


@pytest.fixture
def client(base_url):
    return JiraClient(base_url, pat="test", verify=False)


@pytest.mark.parametrize("row", [{"key": "P-1"}, {"id": "abc"}, {"id": None}])
@responses.activate
def test_full_scan_unusable_cursor_id_raises_parse_error(client, base_url, row):
    """The delta drain has raised JiraParseError here since 0.5.0; the full scan — the
    DEFAULT branch of the same public method — let a bare KeyError / ValueError /
    TypeError escape both `JiraResilientError` and `requests.RequestException`, which is
    the pair the README tells callers to catch."""
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": [row]})
    with pytest.raises(JiraParseError):
        list(client.search_seek("PROJ"))
