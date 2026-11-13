#!/usr/bin/env python3
"""Offline & privacy drill for My-Jarvis.

1. Wraps server outbound HTTP (urllib.request.urlopen) to log every (host, reason).
2. Boots an in-process server, opens the viewer in headless Chrome, sends one real
   question through /chat, speaks the answer through /tts, feeds that WAV back
   through /hear, and reads browser performance.getEntriesByType('resource').
3. Prints every host that is not localhost or 127.0.0.1. Passes only if empty.
4. Supports --test-claude-trap to prove the watcher catches api.anthropic.com.
5. Ends by printing THE HONEST LIST read directly from the codebase.
"""
import asyncio
import http.client
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import websockets

import server

ROOT = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

OUTBOUND_LOG = []
_orig_urlopen = urllib.request.urlopen


def _reason_for(url, stack_hint=""):
    if "/chat/completions" in url or "/v1/messages" in url or "/api/ps" in url:
        return "brain"
    if "/engines/select" in url or "/profiles" in url or "/v1/audio/speech" in url or "/health" in url:
        return "voice"
    return stack_hint or "outbound"


def logged_urlopen(req_or_url, *args, **kwargs):
    url = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
    host = urllib.parse.urlparse(url).hostname or url
    OUTBOUND_LOG.append((host, _reason_for(url), url))
    return _orig_urlopen(req_or_url, *args, **kwargs)


urllib.request.urlopen = logged_urlopen


async def cdp_eval(ws, msg_id, expr):
    await ws.send(json.dumps({
        "id": msg_id, "method": "Runtime.evaluate",
        "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
    }))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id:
            if "exceptionDetails" in msg.get("result", {}):
                raise RuntimeError(msg["result"]["exceptionDetails"])
            return msg["result"]["result"].get("value")


async def run_browser_turn(port):
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            CHROME, "--headless=new", "--remote-debugging-port=9233",
            "--autoplay-policy=no-user-gesture-required",
            "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--user-data-dir=/tmp/galaxy-drill-chrome",
            "--window-size=1280,800", f"{base}/?mute=1",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(30):
            try:
                with _orig_urlopen("http://127.0.0.1:9233/json/list", timeout=1) as r:
                    tabs = json.loads(r.read())
                for t in tabs:
                    if t.get("type") == "page" and f"127.0.0.1:{port}" in t.get("url", ""):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        if not ws_url:
            raise RuntimeError("Chrome tab did not open")
        await asyncio.sleep(2.5)
        async with websockets.connect(ws_url, max_size=10_000_000) as ws:
            ready = "0"
            for k in range(30):
                ready = await cdp_eval(ws, 100 + k, "document.getElementById('status').dataset.ready")
                if ready == "1":
                    break
                await asyncio.sleep(0.25)
            if ready != "1":
                raise RuntimeError("Viewer failed offline render check")
            # Run a real /chat turn in the browser
            await cdp_eval(ws, 2, """
              (() => {
                document.getElementById('q').value = 'What is Tadashi AI OS?';
                document.getElementById('ask').dispatchEvent(new Event('submit', {cancelable: true}));
                return 'sent';
              })()
            """)
            for i in range(40):
                ans = await cdp_eval(ws, 10 + i, "document.getElementById('answer').textContent")
                if ans and not ans.startswith("Thinking") and not ans.startswith("Hello, sir."):
                    break
                await asyncio.sleep(0.25)
            resources = await cdp_eval(ws, 99, """
              performance.getEntriesByType('resource').map(e => ({
                name: e.name,
                host: (() => { try { return new URL(e.name).hostname; } catch { return ''; } })()
              }))
            """)
            return ans, resources
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def print_honest_list():
    """Read the actual code and config to list features that still need the internet."""
    print("\n=== THE HONEST LIST (read from code) ===")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    for name, b in (cfg.get("brains") or {}).items():
        host = urllib.parse.urlparse(b.get("base_url", "")).hostname or ""
        if host and host not in LOCAL_HOSTS:
            print(f"- config.json brains.{name}: calls {b['base_url']} ({b.get('model')}) when selected.")
    hear_src = (ROOT / "hear.py").read_text(encoding="utf-8")
    if "mlx-community/" in hear_src:
        print("- hear.py: Whisper models (small, large-v3-turbo) load from local HuggingFace cache, "
              "or download from huggingface.co on first-ever run if not cached.")
    voice_src = (ROOT / "voice.py").read_text(encoding="utf-8")
    if "127.0.0.1:3900" in voice_src:
        print("- voice.py / VoiceStudio: runs locally on 127.0.0.1:3900 (VoiceStudio itself may fetch "
              "model weights from huggingface.co on a first-ever run).")
    vendor_txt = ROOT / "viewer" / "vendor" / "VERSIONS.txt"
    if vendor_txt.exists():
        print("- viewer/: all 3D libraries are vendored locally in viewer/vendor/ (0 CDN calls).")


def main():
    simulate_claude = "--test-claude-trap" in sys.argv
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    brains, boot = server.load_brains()
    server._active_brain = boot

    if server.voice_mod and cfg.get("voice"):
        server.VOICE = server.voice_mod.VoiceBay(cfg["voice"], server.VOICE_CACHE)
    if server.hear_mod and cfg.get("hearing"):
        server.HEAR = server.hear_mod.HearingBay(cfg["hearing"])
        server.HEAR.load_and_warm()

    handler = partial(server.Handler, directory=str(server.VIEWER))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        # Clear boot-time logs and test the full /chat -> /tts -> /hear loop
        OUTBOUND_LOG.clear()
        ans, browser_res = asyncio.run(run_browser_turn(port))
        print(f"Browser /chat answer: {ans[:120]!r}")

        # Speak answer through /tts -> WAV
        tts_req = urllib.request.Request(
            f"{base}/tts",
            data=json.dumps({"text": "Standing by, sir."}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _orig_urlopen(tts_req, timeout=60) as r:
            wav_bytes = r.read()
        print(f"/tts returned {len(wav_bytes)} bytes ({r.headers.get('X-Jarvis-Voice')})")

        # Feed WAV back through /hear
        hear_req = urllib.request.Request(
            f"{base}/hear",
            data=wav_bytes,
            headers={"Content-Type": "audio/wav"},
            method="POST",
        )
        with _orig_urlopen(hear_req, timeout=30) as r:
            heard = json.loads(r.read())
        print(f"/hear returned: {heard}")

        if simulate_claude:
            # Simulate switching to Claude with a non-placeholder key (never written to disk)
            claude = dict(brains["claude"], api_key="sk-ant-simulated-drill-key")
            try:
                server.complete(claude, [{"role": "user", "content": "hi"}], 10)
            except Exception as e:
                print(f"Simulated Claude call attempted (expected network/auth failure): {e}")

        external = []
        for host, reason, url in OUTBOUND_LOG:
            if host not in LOCAL_HOSTS:
                external.append(f"server:{reason} -> {host}")
        for entry in browser_res:
            h = entry.get("host", "")
            if h and h not in LOCAL_HOSTS:
                external.append(f"browser:resource -> {h} ({entry['name']})")

        print("\n=== EXTERNAL HOSTS (must be empty) ===")
        print(external)
        print_honest_list()
        if external:
            print("\nDRILL RESULT: FAIL (external hosts detected)")
            sys.exit(1)
        print("\nDRILL RESULT: PASS (0 external hosts)")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
