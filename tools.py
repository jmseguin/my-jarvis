#!/usr/bin/env python3
"""Jarvis tools. Standard library only.

web_search(query): drives headless Chrome (no Selenium, no npm) against
Brave Search (DuckDuckGo blocks headless Chrome, Bing returns junk to it) and returns the top results as text.

Safety:
- Chrome gets a throwaway profile, so it never touches your real profile,
  cookies or logins.
- The query goes into the URL only (urlencoded); no shell is involved.
- Only the query text leaves this machine. Notes are never sent.
- Page content is untrusted data: the server never executes anything in it.
"""
import html
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from html.parser import HTMLParser

import settings

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chrome",
]
MAX_QUERY = 200
# Headless Chrome announces itself as "HeadlessChrome"; send a normal Chrome UA instead.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class ToolError(RuntimeError):
    pass


def find_chrome(configured=None):
    configured = configured or settings.get("CHROME_PATH")
    for c in ([configured] if configured else []) + CHROME_CANDIDATES:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise ToolError("Chrome was not found; set CHROME_PATH in .env")


class _Results(HTMLParser):
    """Pulls (title, url, snippet) out of Brave Search results (div.snippet)."""

    def __init__(self):
        super().__init__()
        self.items, self._cur = [], None
        self._field = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div" and "snippet" in cls and a.get("data-type") == "web":
            self._cur = {"title": "", "url": "", "snippet": ""}
            self.items.append(self._cur)
        elif self._cur is None:
            return
        elif tag == "a" and not self._cur["url"] and (a.get("href") or "").startswith("http"):
            self._cur["url"] = html.unescape(a["href"])
        elif tag == "div" and "title" in cls and not self._cur["title"]:
            self._field = "title"
        elif tag == "div" and "content" in cls and not self._cur["snippet"]:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "div":
            self._field = None

    def handle_data(self, data):
        if self._cur is not None and self._field:
            self._cur[self._field] += data


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _kill_tree(proc):
    """Make sure Chrome and all its helper processes are gone."""
    if proc.poll() is None or os.name == "nt":
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def web_search(query, cfg=None):
    """Return a plain-text block of the top results for `query`."""
    cfg = cfg or {}
    query = _clean(query)[:MAX_QUERY]
    if not query:
        raise ToolError("empty search query")
    chrome = find_chrome(cfg.get("chrome"))
    url = "https://search.brave.com/search?" + urllib.parse.urlencode({"q": query, "source": "web"}, quote_via=urllib.parse.quote)
    profile = tempfile.mkdtemp(prefix="jarvis-chrome-")
    out_path = os.path.join(profile, "dom.html")
    timeout = cfg.get("timeout", 30)
    try:
        # stdout goes to a file, not a pipe: Chrome's helper processes inherit
        # pipe handles and would keep us blocked after the main process exits.
        with open(out_path, "wb") as out:
            proc = subprocess.Popen(
                [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check", "--lang=en-US", f"--user-agent={USER_AGENT}",
                 f"--user-data-dir={profile}",
                 "--virtual-time-budget=3000", "--dump-dom", url],
                stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ToolError("Chrome timed out") from None
            finally:
                _kill_tree(proc)
        with open(out_path, "rb") as f:
            dom = f.read().decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not dom.strip():
        raise ToolError("Chrome returned no page")
    parser = _Results()
    parser.feed(dom)
    items = [i for i in parser.items if i["url"].startswith("http")]
    n = int(cfg.get("max_results", 5))
    if not items:
        raise ToolError("no results parsed (search page changed or was blocked)")
    return "\n\n".join(
        f"[{k}] {_clean(i['title'])}\n{i['url']}\n{_clean(i['snippet'])[:300]}"
        for k, i in enumerate(items[:n], 1))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(web_search(" ".join(sys.argv[1:]) or "python standard library"))
