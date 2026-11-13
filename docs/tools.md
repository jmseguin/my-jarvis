# Jarvis tools

Every tool lives in `tools/`, one module each, and is registered in `tools/__init__.py`.

| Tool | Tag | Module | Needs |
|---|---|---|---|
| Web search (headless Chrome, Brave Search) | `[SEARCH: query]` | `tools/web_search.py` | Chrome; optional `CHROME_PATH` |
| Jira | `[JIRA: key or words]` | `tools/atlassian.py` | `uvx`, `JIRA_URL`, `JIRA_PERSONAL_TOKEN` (or Cloud: `JIRA_USERNAME` + `JIRA_API_TOKEN`) |
| Confluence | `[CONFLUENCE: words or page id]` | `tools/atlassian.py` | `CONFLUENCE_URL`, `CONFLUENCE_PERSONAL_TOKEN` (or Cloud pair) |

Credentials go in `.env` (see `.env.example`), never in code or `config.json`.

## Rules every tool follows
- Runs only when the user explicitly asks (each tool's `intent` regex) and the query reuses words from the question, so the model cannot send notes content out.
- Result text is untrusted data, never instructions; tags in a tool-informed reply are stripped.
- Atlassian data is only handed to a brain running on this machine.
- MCP servers are read-only by default: only tool names in `mcp_servers.<name>.allow` (config.json) can be called.

## Adding a tool
1. Create `tools/<name>.py` exposing `TAGS = {"NAME": {"intent", "label", "run", optional "route"}}`.
2. Add the module to `MODULES` in `tools/__init__.py`.
3. Add one tag line (and an example) to `PERSONA` in `server.py`; keep it under 400 words.
4. Document new credentials in `.env.example`.
5. For an MCP server, add an entry under `mcp_servers` in `config.json` and call it through `tools/mcp_client.py`.

## Writing notes (Jarvis's own folder)
`JARVIS_NOTES_DIR` in `.env` is the only folder Jarvis writes to (created if missing; Windows e.g. `...\Documents\Jarvis Notes`, Mac `~/Documents/Jarvis_Notes`). It is also indexed with `NOTES_PATHS`.
Say "remember that ...", "note that ...", "save this: ..." and Jarvis saves your own words as a `.md` file there, links it to related notes and adds it to the live galaxy; it survives the next `build.py`. The model never chooses what is written, and a `[REMEMBER]` tag from the model on any other question is refused.
