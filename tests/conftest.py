"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def base_url() -> str:
    return "https://jira.example.com"


@pytest.fixture
def client(base_url):
    """A JiraClient pointed at the base_url. No real network — pair with `responses`
    or a mocked session to actually exercise."""
    from jira_resilient import JiraClient

    return JiraClient(base_url, pat="test-pat", verify=False)
