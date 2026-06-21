"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def base_url() -> str:
    return "https://jira.example.com"
