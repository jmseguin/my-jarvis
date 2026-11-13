"""Jira + Confluence tool, via the mcp-atlassian MCP server (read-only).

Tags: [JIRA: ticket key or search words], [CONFLUENCE: search words or page id].
Credentials: see the Atlassian section of .env.example.
"""
import re

from . import mcp_client

ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
MAX_CONTEXT_CHARS = 6000


def _call(tool, arguments, ctx):
    # Company data may only reach a brain that runs on this machine.
    if not ctx["local_brain"]:
        raise RuntimeError("Atlassian data stays on this machine, and the active brain is in the cloud")
    text = mcp_client.server("atlassian").call(tool, arguments)
    return text.replace("@@@hl@@@", "").replace("@@@endhl@@@", "")   # Confluence search highlights


def run_jira(arg, ctx):
    q = (arg or "").strip()
    if not q:
        raise RuntimeError("no ticket or search words given")
    if ISSUE_KEY_RE.fullmatch(q.upper()):
        text = _call("jira_get_issue", {"issue_key": q.upper(), "comment_limit": 3}, ctx)
    else:
        jql = q if re.search(r"(=|~|order by|\bin \()", q, re.I) else \
            'text ~ "' + q.replace('"', " ") + '" ORDER BY updated DESC'
        text = _call("jira_search", {"jql": jql, "limit": 5,
                                     "fields": "summary,status,assignee,priority,updated"}, ctx)
    return f'Looked up Jira for "{q}".', {"context": text[:MAX_CONTEXT_CHARS], "query": q}


def run_confluence(arg, ctx):
    q = (arg or "").strip()
    if not q:
        raise RuntimeError("no search words given")
    if q.isdigit():
        text = _call("confluence_get_page", {"page_id": q}, ctx)
    else:
        text = _call("confluence_search", {"query": q, "limit": 5}, ctx)
    return f'Looked up Confluence for "{q}".', {"context": text[:MAX_CONTEXT_CHARS], "query": q}


TAGS = {
    "JIRA": {
        "intent": re.compile(r"\b(jira|tickets?|issues?|jql|epics?|stor(?:y|ies)|bugs?|[A-Z][A-Z0-9]+-\d+)\b", re.I),
        "route": re.compile(r"\b(?:jira|ticket|issue)\b.*?(?P<arg>\b[A-Z][A-Z0-9_]+-\d+\b)"),
        "label": "JIRA RESULTS",
        "run": run_jira,
    },
    "CONFLUENCE": {
        "intent": re.compile(r"\b(confluence|wiki|pages?)\b", re.I),
        "route": re.compile(r"\b(?:search|find|look\s*up|check)\s+(?:in\s+|on\s+)?(?:the\s+)?confluence"
                            r"\s+(?:for|about)?\s*(?P<arg>.+)", re.I),
        "label": "CONFLUENCE RESULTS",
        "run": run_confluence,
    },
}
