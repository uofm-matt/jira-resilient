"""jira-resilient — Resilient JIRA Server data extraction at scale.

Quickstart:

    from jira_resilient import JiraClient

    client = JiraClient("https://jira.example.com", pat="...")
    if not client.is_authenticated:
        raise SystemExit("auth failed")

    # Seek-paginated scan of a large project — survives 100K+ issue projects
    # where offset pagination dies.
    for page in client.search_seek("PROJ"):
        for issue in page.issues:
            ...

    # Three-tier resilient fetch (full → hub-fetch → minimal fallback)
    result = client.get_issue_resilient("HUB-1234")
    print(result.tier, result.issue["key"])
"""

from jira_resilient._models import ResilientFetchResult, SearchPage, Tier
from jira_resilient.client import JiraClient
from jira_resilient.exceptions import (
    JiraAuthError,
    JiraFetchError,
    JiraParseError,
    JiraResilientError,
)
from jira_resilient.jql import build_jql

__version__ = "0.4.4"

__all__ = [
    "JiraAuthError",
    "JiraClient",
    "JiraFetchError",
    "JiraParseError",
    "JiraResilientError",
    "ResilientFetchResult",
    "SearchPage",
    "Tier",
    "__version__",
    "build_jql",
]
