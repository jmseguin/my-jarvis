"""Reads the project-root .env (never committed). Standard library only.

Format: KEY=value lines, '#' comments. A real environment variable of the
same name overrides the file. See .env.example for every supported key.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env(path=ENV_FILE):
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(env) + [k for k in os.environ if k.isupper()]:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def get(name, default=""):
    return load_env().get(name, default)


def expand(value):
    """Replace ${NAME} in a config.json string with its .env value ('' if unset)."""
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: get(m.group(1)), value) if isinstance(value, str) else value
