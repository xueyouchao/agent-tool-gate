"""Read a ``.env`` file into the process environment.

``os.environ`` is where every credential is read from — ``JevClient`` looks there for
``TYPESAFE_API_KEY`` — and ``.env.example`` tells you to copy it to ``.env`` and fill it in. So
something has to read that file, or that instruction is a lie and a key someone configured looks
exactly like a key that was never set.

Two properties this keeps on purpose:

* **An existing variable always wins.** An explicit ``export TYPESAFE_API_KEY=...`` must never be
  overridden by a stale file someone forgot about.
* **No value is ever returned, logged or raised.** The names come back so a caller can report what
  it loaded; the secrets stay in ``os.environ``.
"""
from __future__ import annotations

import os
from pathlib import Path


def _value(raw: str) -> str:
    """The value as a shell would read it: surrounding quotes removed, an unquoted trailing
    ``# comment`` dropped."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()


def load_env_file(path: str | Path = ".env") -> list[str]:
    """Set every variable named in ``path`` that is not already set; return the names it set.

    A missing file is the ordinary case and sets nothing. A line with no ``=`` is skipped rather
    than raising, because a malformed ``.env`` must not be able to stop the gate from booting.
    """
    env_file = Path(path)
    if not env_file.is_file():
        return []

    loaded: list[str] = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, raw = line.partition("=")
        name = name.strip()
        if not sep or not name or name in os.environ:
            continue
        os.environ[name] = _value(raw.strip())
        loaded.append(name)
    return loaded
