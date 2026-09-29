"""Jev (TypeSafe "System One") HTTP adapter.

The battery, its §5.2 response contract and its failure vocabulary live in ``domain.battery``;
this module only speaks HTTP and turns transport faults into the fail-closed ``JevOutage``.

The opener is an *internal* seam — an implementation detail of this module, used by this
module's own tests. Production passes ``urllib``; a test passes a stand-in and asserts that a
fault becomes ``JevOutage``, which is the fail-closed boundary and was previously untested.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

from ..domain.battery import BATTERY, JevOutage, JevResponse, validate_jev_response

Opener = Callable[[urllib.request.Request, float], bytes]


def _urllib_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return r.read()


class JevClient:
    """The HTTP adapter for the ``application.ports.Jev`` port."""

    def __init__(self, api_key: str | None = None,
                 base_url: str = "https://api.typesafe.ai/v1/systemone",
                 timeout: float = 10.0, opener: Opener = _urllib_opener):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
        self.base_url = base_url
        self.timeout = timeout
        self.opener = opener

    def invoke(self, state: dict) -> JevResponse:
        if not self.api_key:
            raise JevOutage("no TYPESAFE_API_KEY / JEV_API_KEY")
        return self._http_invoke(state)

    def _http_invoke(self, state: dict) -> JevResponse:
        payload = {"state": state, "model": "jev-latest", "questions": BATTERY}
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            body = json.loads(self.opener(req, self.timeout))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise JevOutage(f"jev unreachable: {e}")
        usage = body.get("usage", {})
        answers = body.get("answers")
        validate_jev_response(answers)
        return JevResponse(answers=answers, input_tokens=usage.get("input_tokens", 0),
                           output_tokens=usage.get("output_tokens", 0))
