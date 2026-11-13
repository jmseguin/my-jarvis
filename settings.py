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
                v = v.strip().strip('"').strip("'")
                if v:
                    env[k.strip()] = v
    return env


def get(name, default=""):
    return load_env().get(name, default)


def expand(value):
    """Replace ${NAME} in a config.json string with its .env value ('' if unset)."""
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: get(m.group(1)), value) if isinstance(value, str) else value


def config():
    """Parsed config.json from the project root."""
    import json
    return json.loads((ENV_FILE.parent / "config.json").read_text(encoding="utf-8"))
