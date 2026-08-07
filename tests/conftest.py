"""Shared pytest fixtures.

`client` pins `_server_tz` because most tests never exercise the `/serverInfo` probe and
would otherwise have to stub it just to get past it. The three modules that DO care about
the probe — or that count requests — override `client` locally with an unpinned one; those
overrides are deliberate, not leftovers.
"""

from __future__ import annotations

import time
from datetime import UTC

import pytest

from jira_resilient import JiraClient


@pytest.fixture
def base_url() -> str:
    return "https://jira.example.com"


@pytest.fixture
def client(base_url: str) -> JiraClient:
    c = JiraClient(base_url, pat="test", verify=False)
    c._server_tz = UTC
    return c


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry backoff. Tests that ASSERT on the sleep values patch it themselves and
    capture into a list instead — this is only for the ones where the wait is incidental."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
