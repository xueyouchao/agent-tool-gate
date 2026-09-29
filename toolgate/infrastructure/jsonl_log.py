"""JsonlLog — the append-only JSONL decision file (spec §10)."""
from __future__ import annotations

import json
from pathlib import Path


class JsonlLog:
    """One JSON object per line, appended in arrival order under a resuming sequence number."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # resume numbering: the file is appended to across process restarts
        self._seq = self._existing_lines()

    def _existing_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open() as f:
            return sum(1 for line in f if line.strip())

    def lines(self) -> list[dict]:
        """Every line already written, oldest first."""
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, entry: dict) -> dict:
        self._seq += 1
        entry = {"seq": self._seq, **entry}
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
