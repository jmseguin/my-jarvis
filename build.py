#!/usr/bin/env python3
"""Indexer: scan markdown notes and write viewer/graph-data.js.

Standard library only. Usage:
    python3 build.py                 # scan the default note roots below
    python3 build.py ~/some/folder   # scan specific folders instead

Output: viewer/graph-data.js containing
    const GRAPH = {nodes: [...], links: [...]};
Every node's numeric `id` equals its index in the nodes array.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "viewer" / "graph-data.js"

# The roots you asked for, plus the vault that actually exists on this Mac
# (~/Documents/ObsidianVault, no space). Missing roots are skipped with a warning.
DEFAULT_ROOTS = [
    "~/Documents/Obsidian Vault",
    "~/Documents/Notes",
    "~/Documents/ObsidianVault",
]

EXCERPT_CHARS = 700
MIN_TITLE_LEN = 4          # shorter titles are too noisy for "mentions" linking
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".venv"}

FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


def norm(s):
    """Normalise a title / link target for comparison."""
    s = s.strip().lower()
    if s.endswith(".md"):
        s = s[:-3]
    return re.sub(r"[\s_\-]+", " ", s).strip()


def pretty(stem):
    return re.sub(r"[_]+", " ", stem).strip() or stem


def make_excerpt(body):
    text = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[:EXCERPT_CHARS]
    space = cut.rfind(" ")
    if space > EXCERPT_CHARS * 0.8:
        cut = cut[:space]
    return cut.rstrip() + "…"


def scan(roots):
    files = []
    seen = set()
    for raw in roots:
        root = Path(os.path.expanduser(raw)).resolve()
        if not root.is_dir():
            print(f"  ! skipping missing folder: {raw}")
            continue
        print(f"  scanning {root}")
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.lower().endswith(".md"):
                    p = (Path(dirpath) / fn).resolve()
                    if p not in seen:
                        seen.add(p)
                        files.append((root, p))
    return files


def build(roots):
    files = scan(roots)
    nodes, keys_for = [], []   # keys_for[i] = set of names that resolve to node i
    wikis = []                 # wikis[i]   = set of normalised wikilink targets

    for root, path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  ! cannot read {path}: {e}")
            continue
        body = FRONTMATTER_RE.sub("", raw, count=1)
        rel = path.relative_to(root)
        stem = path.stem
        parent = path.parent.name if path.parent != root else root.name
        h1 = H1_RE.search(body)
        heading = h1.group(1).strip() if h1 else ""

        # Label from the filename. Obsidian-style index files (_area, _project)
        # would all collide, so prefix them with their folder.
        if stem.startswith("_") and path.parent != root:
            label = f"{pretty(parent)} · {stem.lstrip('_')}"
        else:
            label = pretty(stem.lstrip("_")) if stem.startswith("_") else pretty(stem)

        # Title used for "one note mentions the other's title" linking.
        title = label if not stem.startswith("_") else (heading or pretty(stem.lstrip("_")))
        title = re.sub(r"^(area|project)\s*:\s*", "", title, flags=re.I)

        idx = len(nodes)
        nodes.append({
            "id": idx,                       # CRITICAL: id == index
            "label": label,
            "group": parent,
            "path": f"{root.name}/{rel.as_posix()}",
            "excerpt": make_excerpt(body),
        })
        keys_for.append({norm(stem), norm(rel.with_suffix("").as_posix()),
                         norm(label), norm(title)})
        wikis.append({norm(m.group(1)) for m in WIKILINK_RE.finditer(body)})
        nodes[idx]["_title"] = title
        # Mentions are searched in prose only: template headings like "## Projects"
        # appear in every area note and would link everything to everything.
        nodes[idx]["_text"] = re.sub(r"^#{1,6}\s.*$", "", body, flags=re.M).lower()

    # Resolve names -> node index
    lookup = {}
    for i, keys in enumerate(keys_for):
        for k in keys:
            lookup.setdefault(k, i)

    links = {}

    def add(a, b, kind):
        if a == b:
            return
        key = (min(a, b), max(a, b))
        if key not in links:
            links[key] = {"source": key[0], "target": key[1], "kind": kind}

    # 1) direct [[wikilinks]]
    for i, targets in enumerate(wikis):
        for t in targets:
            j = lookup.get(t)
            if j is not None:
                add(i, j, "wikilink")
    # 2) shared [[wikilinks]] (both notes link to the same target)
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if wikis[i] & wikis[j]:
                add(i, j, "shared")
    # 3) one note mentions the other's title in plain text
    for j, n in enumerate(nodes):
        t = n["_title"].strip()
        if len(t) < MIN_TITLE_LEN:
            continue
        pat = re.compile(r"(?<![\w])" + re.escape(t.lower()) + r"(?![\w])")
        for i, m in enumerate(nodes):
            if i != j and pat.search(m["_text"]):
                add(i, j, "mention")

    for n in nodes:
        n.pop("_title"), n.pop("_text")

    # Sanity: ids must equal indexes
    assert all(n["id"] == i for i, n in enumerate(nodes))
    return {"nodes": nodes, "links": list(links.values())}


def main():
    roots = sys.argv[1:] or DEFAULT_ROOTS
    print("Building knowledge galaxy…")
    graph = build(roots)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("// Generated by build.py — do not edit by hand.\n"
                   "const GRAPH = " + json.dumps(graph, ensure_ascii=False, indent=1) + ";\n",
                   encoding="utf-8")
    groups = sorted({n["group"] for n in graph["nodes"]})
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(graph['nodes'])} nodes, "
          f"{len(graph['links'])} links, {len(groups)} groups")


if __name__ == "__main__":
    main()
