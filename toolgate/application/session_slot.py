"""SessionSlot — one authorization decision at a time per session (spec §10).

The critical section spans the *whole* decision, not just the record. The spend ceiling is
checked before the judgment call and recorded after it, so a slot covering only the record
would let concurrent calls all pass the check before any of them recorded — measurably
breaching §5.1's ceiling.

Serialization is per session: concurrent calls in one session queue while other sessions
proceed. A hung judgment backend holds its session for up to the client timeout (~10s), after
which the decision fails closed to ESCALATE.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class SessionSlot:
    """A lock per session id, created on first use."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._registry = threading.Lock()

    @contextmanager
    def hold(self, session: str) -> Iterator[None]:
        """Serializes this session's decisions; never held for the tool's own execution."""
        with self._lock_for(session):
            yield

    def _lock_for(self, session: str) -> threading.Lock:
        # The registry lock is separate from, and never held together with, a session lock.
        with self._registry:
            lock = self._locks.get(session)
            if lock is None:
                lock = self._locks[session] = threading.Lock()
            return lock
