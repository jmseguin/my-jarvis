"""Reads the project-root .env (never committed). Standard library only.

Format: KEY=value lines, '#' comments. Precedence: non-empty value in .env, then the fresh
Windows user environment, then the inherited process environment. See .env.example for every supported key.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def _registry_user_env():
    """Fresh Windows user environment variables. A long-running parent process
    (terminal, IDE) can hand us stale copies, so these win over os.environ."""
    out = {}
    if os.name != "nt":
        return out
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                if isinstance(value, str) and name.isupper():
                    out[name] = value
                i += 1
    except OSError:
        pass
    return out


QUOTES = "\"'"


def unquote(v):
    """Value of a .env entry. Quotes are optional: 'C:\\My Notes', "C:\\My Notes" and
    C:\\My Notes all work (spaces never need quoting here, because nothing splits on
    them). Anything after a closing quote, such as a trailing comment, is dropped."""
    v = v.strip()
    if v and v[0] in QUOTES:
        end = v.find(v[0], 1)
        if end == -1:
            return v[1:].strip()                      # unterminated quote: take the rest
        rest = v[end + 1:].strip()
        if not rest or rest.startswith("#"):          # whole value was quoted (+ comment)
            return v[1:end]
        # several quoted items, e.g. "C:\a b";"C:\c d": keep as is for split_paths()
    return v


def split_paths(raw):
    """Split a ';' separated list of paths. A path may be quoted, and quoted paths
    may contain ';'. Blank items are skipped."""
    items, cur, quote = [], "", None
    for ch in raw or "":
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in QUOTES:
            quote = ch
        elif ch == ";":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [i.strip() for i in items if i.strip()]


def as_path(raw):
    """A user-written path -> Path: %VAR% / $VAR and ~ expanded."""
    return Path(os.path.expanduser(os.path.expandvars(unquote(raw))))


def load_env(path=ENV_FILE):
    """Precedence, highest first: non-empty value in .env, fresh Windows user
    environment, inherited process environment."""
    env = {k: v for k, v in os.environ.items() if k.isupper()}
    env.update(_registry_user_env())
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = unquote(v)
                if v:
                    env[k.strip()] = v
    return env


def get(name, default=""):
    return load_env().get(name, default)


def expand(value):
    """Replace ${NAME} or ${NAME:-default} in a config.json string with its .env value
    (the default when unset or empty, else '')."""
    import re

    def sub(m):
        return get(m.group(1)) or m.group(2) or ""
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", sub, value) if isinstance(value, str) else value


def config():
    """Parsed config.json from the project root."""
    import json
    return json.loads((ENV_FILE.parent / "config.json").read_text(encoding="utf-8"))


def notes_dir():
    """The one folder Jarvis may WRITE notes into (JARVIS_NOTES_DIR in .env), created if missing."""
    raw = get("JARVIS_NOTES_DIR")
    if not raw:
        raise RuntimeError("JARVIS_NOTES_DIR is not set in .env, so I have nowhere to write notes")
    p = as_path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()
