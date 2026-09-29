"""Container tests — the dependency-injector composition root and its override seams."""
import argparse
import inspect

from dependency_injector import providers
from fakes import FakeJev

from toolgate.__main__ import _parse_args, build_engine
from toolgate.application.engine import Engine
from toolgate.container import Container
from toolgate.infrastructure.jev import JevClient


def _container(tmp_path) -> Container:
    c = Container()
    c.config.from_dict({"log_path": str(tmp_path / "d.jsonl"),
                        "approvals_path": str(tmp_path / "pa.json")})
    return c


def test_container_wires_engine_with_one_shared_decision_record(tmp_path):
    c = _container(tmp_path)
    engine = c.engine()
    assert engine.decisions is c.decision_record()           # writer and reader, one record
    assert engine.decisions.approvals is c.approval_store()  # the CLI reads the same file
    assert isinstance(engine.jev, JevClient)


def test_container_shares_the_slot_record_and_policy_set(tmp_path):
    """A per-engine slot or record would stop serializing and duplicate sequence numbers."""
    c = _container(tmp_path)
    assert c.engine().slot is c.engine().slot       # `engine` is a Factory; the slot is not
    assert c.engine().decisions is c.engine().decisions
    assert c.engine().policy_version == c.policy_set().version


def test_engine_takes_its_whole_world_and_no_test_hooks():
    """The engine reads no policy files: its signature is the list of things it needs."""
    params = inspect.signature(Engine.__init__).parameters
    assert not {"log_path", "schema_text", "gate_text", "judgment_text"} & set(params)
    assert [n for n, p in params.items()
            if p.kind is p.POSITIONAL_OR_KEYWORD] == ["self", "principal"]
    assert {"gate_authorizer", "judgment_authorizer", "decisions", "policy_version"} <= set(params)


def test_engine_builds_none_of_its_collaborators():
    """Each one is handed over, so none can be a default that reaches the world.

    ``jev`` used to default to a real ``JevClient``: a caller that forgot the argument — and the
    shared test helper copied the same default — made billed HTTP calls. The injected double
    belongs in ``fakes.make_engine``, where a test can see it, not in the engine.
    """
    params = inspect.signature(Engine.__init__).parameters
    for name in ("gate_authorizer", "judgment_authorizer", "decisions", "policy_version", "jev"):
        assert params[name].default is inspect.Parameter.empty, f"{name} has a default"


def test_container_config_defaults_apply(tmp_path):
    c = Container()
    c.config.from_dict({"log_path": str(tmp_path / "d.jsonl")})
    assert c.config.consistency_samples() == 3      # default survives a partial from_dict
    assert c.config.agent_name() == "cli-agent"


def test_container_injects_a_test_double(tmp_path):
    c = _container(tmp_path)
    with c.jev_client.override(providers.Object(FakeJev())):
        entry = c.engine().authorize_tool_call("bash", {"command": "echo hello"})
    assert entry["phase"] == 2                       # gray call judged by the injected double
    assert entry["answers"] is not None


def test_build_engine_uses_the_container(tmp_path):
    # parsed rather than hand-built, so adding a flag cannot leave this test behind
    args = _parse_args(["--log", str(tmp_path / "d.jsonl"),
                        "--approvals", str(tmp_path / "pa.json"),
                        "--consistency-samples", "1"])
    assert build_engine(args).decisions is not None
