"""Quick checks for .env path parsing. Run: python tests_settings.py"""
import os
import tempfile
from pathlib import Path

import settings as S

CASES = [
    ('"C:\\A B\\Notes"', "C:\\A B\\Notes"),          # double quoted, spaces
    ("'C:\\A B'", "C:\\A B"),                        # single quoted
    ("C:\\A B\\Notes", "C:\\A B\\Notes"),            # unquoted with spaces
    ('"C:\\A B"  # comment', "C:\\A B"),             # trailing comment after quotes
    ("~/Documents/Jarvis_Notes", "~/Documents/Jarvis_Notes"),
    ("C:\\Docs\\a#b", "C:\\Docs\\a#b"),              # '#' inside an unquoted path survives
]
for raw, want in CASES:
    assert S.unquote(raw) == want, (raw, S.unquote(raw))

assert S.split_paths('"C:\\a b";"C:\\c;d";C:\\e f; ') == ["C:\\a b", "C:\\c;d", "C:\\e f"]
assert S.split_paths(S.unquote('"C:\\a b";"C:\\c d"')) == ["C:\\a b", "C:\\c d"]

root = tempfile.mkdtemp()
os.environ["TESTROOT"] = root
assert S.as_path('"%TESTROOT%\\My Notes"') == Path(root) / "My Notes" if os.name == "nt" else True
assert S.as_path('"$TESTROOT/My Notes"') == Path(root) / "My Notes" if os.name != "nt" else True
assert str(S.as_path('"~/Jarvis Notes"')).startswith(os.path.expanduser("~"))

# end to end: a .env whose notes dir has spaces and quotes
env = Path(root) / ".env"
target = Path(root) / "My Jarvis Notes"
env.write_text(f'# test\nJARVIS_NOTES_DIR="{target}"\nNOTES_PATHS="{root}";"{target}"\n', encoding="utf-8")
loaded = S.load_env(env)
assert Path(loaded["JARVIS_NOTES_DIR"]) == target, loaded["JARVIS_NOTES_DIR"]
assert [Path(p) for p in S.split_paths(loaded["NOTES_PATHS"])] == [Path(root), target]
print("ok")
