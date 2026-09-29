"""TraceLog — what crossed each boundary of one call, in a second append-only file.

The decision log records the *verdict*; this records the traffic that produced it — the arguments
as they arrived, the normalized request, the Cedar call, the judgment's prompt and answers. The
viewer reads it back and fills in the boundaries that would otherwise read "not recorded".

**It scrubs by default.** The payloads here are exactly the ones the gate exists to catch — the
demo's `curl -d @~/.ssh/config` is a real line in a real log — so the safe setting has to be the
one you get without asking. `raw=True` is opt-in, and it makes this file as sensitive as the
agent's own context: whoever can read it can read what the agent was about to send.
"""
from __future__ import annotations

import json
from pathlib import Path

from .scrub import scrub_payload


class TraceLog:
    """One JSON line per (call, boundary). Implements ``application.ports.Trace``."""

    def __init__(self, path: str | Path, *, raw: bool = False):
        self.path = Path(path)
        self.raw = raw
        self._ready = False

    def append(self, event: dict) -> None:
        if not self._ready:  # created on first write, so reading never makes a directory
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._ready = True
        body = event if self.raw else {**event, "payload": scrub_payload(event.get("payload"))}
        with self.path.open("a") as f:
            f.write(json.dumps(body) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def read_traces(path: str | Path) -> dict[str, dict[str, dict]]:
    """call_id → boundary → the recorded event, which is the shape the viewer wants.

    Read straight from the file rather than through a ``TraceLog``, so a read has no side effect,
    and re-read on every poll for the same reason as the decision log: the process writing it is
    a different one.
    """
    source = Path(path)
    if not source.exists():
        return {}
    traces: dict[str, dict[str, dict]] = {}
    for line in source.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        call_id, boundary = event.get("call_id"), event.get("boundary")
        if call_id and boundary:
            traces.setdefault(call_id, {})[boundary] = event
    return traces
