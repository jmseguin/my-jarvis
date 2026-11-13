"""Jarvis tools. One module per tool; each exposes a TAGS dict:

    TAGS = {"NAME": {"intent": <regex the user's question must match>,
                     "label":  "RESULTS HEADER",
                     "run":    fn(arg, ctx) -> (receipt, {"context": text, "query": str})}}

Optional keys: "route" (regex with named group `arg` for unambiguous requests) and
"direct": True (answer comes straight from the tool, no second model pass, no privacy gate:
only for tools that send nothing out, like the clock).

To add a tool: create tools/<name>.py with TAGS, add the module to MODULES below,
add its tag line to PERSONA in server.py, and document any credentials in
.env.example. The server enforces the privacy gate (explicit request + query
built from the user's own words) for every tool in the registry.

ctx = {"local_brain": bool}  (True when the active brain runs on this machine).
"""
from . import atlassian, clock, web_search

MODULES = [clock, web_search, atlassian]

REGISTRY = {}
for _m in MODULES:
    REGISTRY.update(_m.TAGS)
