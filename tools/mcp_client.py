#!/usr/bin/env python3
"""Minimal MCP client over stdio (newline-delimited JSON-RPC). Standard library only.

Jarvis spawns an MCP server on first use, speaks initialize / tools/call, and
keeps it warm. Only tools named in the server's `allow` list in config.json can
be called, so the default is read-only no matter what the server offers.

Credentials come from .env (see settings.py); only the env keys a server lists
in config.json are handed to it, on top of the normal process environment.
"""
import atexit
import json
import os
import shutil
import subprocess
import threading

import settings

PROTOCOL = "2024-11-05"
CALL_TIMEOUT = 90


class McpError(RuntimeError):
    pass


class McpServer:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.proc = None
        self._lock = threading.Lock()          # one request at a time
        self._next_id = 0
        self._pending = {}                     # id -> [event, response]
        self.tools = {}

    # ------------------------------------------------------------ lifecycle
    def _missing_env(self):
        env = settings.load_env()
        return [k for k in self.cfg.get("required_env", []) if not env.get(k)]

    def _any_of_missing(self):
        env = settings.load_env()
        for group in self.cfg.get("required_any", []):      # e.g. a PAT or an API token
            if not any(env.get(k) for k in group):
                return group
        return None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        missing = self._missing_env()
        if missing:
            raise McpError(f"{self.name}: set {', '.join(missing)} in .env")
        group = self._any_of_missing()
        if group:
            raise McpError(f"{self.name}: set one of {', '.join(group)} in .env")
        exe = shutil.which(self.cfg["command"]) or self.cfg["command"]
        env = dict(os.environ)
        loaded = settings.load_env()
        for k in self.cfg.get("env", []):
            if loaded.get(k):
                env[k] = loaded[k]
        self.proc = subprocess.Popen(
            [exe, *self.cfg.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
        threading.Thread(target=self._reader, daemon=True).start()
        atexit.register(self.stop)
        self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "jarvis", "version": "1"}},
                      timeout=self.cfg.get("start_timeout", 120))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._request("tools/list", {})
        self.tools = {t["name"]: t for t in listed.get("tools", [])}

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, check=False)
            else:
                p.kill()

    # ------------------------------------------------------------ transport
    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _reader(self):
        proc = self.proc
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue                        # stray non-protocol output
            slot = self._pending.get(msg.get("id"))
            if slot:
                slot[1] = msg
                slot[0].set()
        for slot in list(self._pending.values()):   # server died: wake everyone
            slot[0].set()

    def _request(self, method, params, timeout=CALL_TIMEOUT):
        self._next_id += 1
        rid = self._next_id
        slot = self._pending[rid] = [threading.Event(), None]
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot[0].wait(timeout):
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} timed out after {timeout}s")
        self._pending.pop(rid, None)
        msg = slot[1]
        if msg is None:
            raise McpError(f"{self.name}: server exited")
        if "error" in msg:
            raise McpError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    # ------------------------------------------------------------ public
    def call(self, tool, arguments):
        if tool not in self.cfg.get("allow", []):
            raise McpError(f"{self.name}: tool '{tool}' is not on the allow list")
        with self._lock:
            self.start()
            if tool not in self.tools:
                raise McpError(f"{self.name}: server has no tool '{tool}'")
            res = self._request("tools/call", {"name": tool, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise McpError(f"{self.name}: {text[:300] or 'tool error'}")
        return text


_servers = {}
_servers_lock = threading.Lock()


def server(name):
    cfg = (settings.config().get("mcp_servers") or {}).get(name)
    if not cfg or cfg.get("enabled", True) is False:
        raise McpError(f"MCP server '{name}' is not enabled in config.json")
    with _servers_lock:
        if name not in _servers:
            _servers[name] = McpServer(name, cfg)
        return _servers[name]
