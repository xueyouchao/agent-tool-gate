"""The Jev HTTP adapter — the fail-closed boundary, driven through its internal opener seam.

``_http_invoke`` is where a network fault becomes ``JevOutage`` and where a raw body becomes the
§5.2 response. Both are security-relevant, so they are tested through the seam rather than by
patching ``urllib`` from the outside.
"""
from __future__ import annotations

import json
import urllib.error

import pytest
from fakes import clean_answers

from toolgate.domain.battery import (BATTERY, INPUT_USD_PER_MTOK, JevMalformed, JevOutage)
from toolgate.infrastructure.jev import JevClient


class _Opener:
    """Stands in for ``urllib.request.urlopen``."""

    def __init__(self, *, body: bytes = b"", fault: Exception | None = None):
        self.body, self.fault, self.seen = body, fault, []

    def __call__(self, request, timeout):
        self.seen.append((request, timeout))
        if self.fault is not None:
            raise self.fault
        return self.body


def _body(**over) -> bytes:
    payload = {"answers": clean_answers(), "usage": {"input_tokens": 606, "output_tokens": 0}}
    payload.update(over)
    return json.dumps(payload).encode()


def _client(opener, **kw) -> JevClient:
    return JevClient(api_key="k", opener=opener, **kw)


def test_response_is_parsed_with_its_usage_and_cost():
    res = _client(_Opener(body=_body())).invoke({"tool": "bash"})
    assert res.answers["well_formed"]["noul"] == 0.95
    assert res.input_tokens == 606
    assert res.cost_usd == pytest.approx(606 / 1_000_000 * INPUT_USD_PER_MTOK)


def test_request_carries_the_bearer_token_the_battery_and_the_timeout():
    opener = _Opener(body=_body())
    _client(opener, base_url="https://example.test/jev").invoke({"tool": "bash"})
    request, timeout = opener.seen[0]
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert request.full_url == "https://example.test/jev"
    assert request.get_method() == "POST"
    assert headers["authorization"] == "Bearer k"
    assert set(json.loads(request.data)["questions"]) == set(BATTERY)
    assert timeout == 10.0


@pytest.mark.parametrize("fault", [
    urllib.error.URLError("connection refused"),
    TimeoutError("timed out"),
    json.JSONDecodeError("not json", "", 0),
])
def test_any_transport_fault_fails_closed(fault):
    with pytest.raises(JevOutage):
        _client(_Opener(fault=fault)).invoke({"tool": "bash"})


def test_an_off_contract_body_is_malformed_not_an_outage():
    """A reachable Jev that answers wrongly is a different failure from an unreachable one."""
    body = _body(answers={"destructive": {"type": "noul", "noul": 0.1}})
    with pytest.raises(JevMalformed):
        _client(_Opener(body=body)).invoke({"tool": "bash"})


def test_no_api_key_is_an_outage_and_never_touches_the_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    opener = _Opener(body=_body())
    with pytest.raises(JevOutage):
        JevClient(api_key=None, opener=opener).invoke({"tool": "bash"})
    assert opener.seen == []
