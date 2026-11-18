# SPDX-License-Identifier: GPL-3.0-or-later
# Set Driven Key v1.7.0 — reconstructs full module from _sdk_src_*.txt parts.
import base64
from pathlib import Path

def _bootstrap():
    d = Path(__file__).resolve().parent
    parts = sorted(d.glob("_sdk_src_*.txt"))
    if not parts:
        raise RuntimeError("Missing _sdk_src_*.txt next to set_driven_key.py")
    raw = b"".join(base64.b64decode(p.read_text(encoding="ascii")) for p in parts)
    exec(compile(raw, str(d / "set_driven_key.py"), "exec"), globals())

_bootstrap()
del _bootstrap, base64, Path
