"""The README makes checkable claims about the public surface. These are its oracles.

Prose drifts silently: the constructor line lost `pool_maxsize` for two releases (added
0.4.4, never documented) and nothing failed. Names and floors are mechanical, so test them.
"""

from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path

import jira_resilient
from jira_resilient import JiraClient

_ROOT = Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text()


def _pyproject() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text())


def test_exported_version_matches_the_packaged_one():
    """`__version__` is in `__all__`, so it is a published claim. CI checks the git tag
    against pyproject and nothing checks this one — and it has drifted before (afda442,
    "chore: sync uv.lock self-version to 0.4.4")."""
    assert jira_resilient.__version__ == _pyproject()["project"]["version"]


def test_readme_constructor_documents_every_parameter():
    line = re.search(r"^`JiraClient\((?P<params>[^`]*)\)`", _README, re.M)["params"]
    documented = {p.split("=")[0].strip() for p in line.split(",")} - {"*"}
    assert documented == set(inspect.signature(JiraClient).parameters)


def test_readme_python_floor_matches_requires_python():
    requires = _pyproject()["project"]["requires-python"]
    documented = re.search(r"^\| Python \| (?P<floor>\S+)\+ \|", _README, re.M)["floor"]
    assert requires == f">={documented}"
