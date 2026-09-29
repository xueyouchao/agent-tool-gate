"""Reading `.env` — the file `.env.example` tells you to fill in.

The point of these tests is the trap they close: `JevClient` reads `os.environ`, and a `.env` that
nothing reads makes a configured key indistinguishable from no key at all. The last test proves the
whole path — file → `os.environ` → the real `JevClient` the container wires — because that is the
one that would have caught it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from toolgate.infrastructure.env_file import load_env_file  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Never inherit a real key from the machine running the suite."""
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY", "TOOLGATE_TEST_ONLY"):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path, body: str) -> pathlib.Path:
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "absent") == []


def test_it_sets_an_unset_variable_and_names_it(tmp_path):
    loaded = load_env_file(_write(tmp_path, "TYPESAFE_API_KEY=apikey_secret\n"))
    assert loaded == ["TYPESAFE_API_KEY"]
    assert os.environ["TYPESAFE_API_KEY"] == "apikey_secret"


def test_an_existing_variable_wins(tmp_path, monkeypatch):
    """An explicit export must never be overridden by a file someone forgot about."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "exported")
    assert load_env_file(_write(tmp_path, "TYPESAFE_API_KEY=from-file\n")) == []
    assert os.environ["TYPESAFE_API_KEY"] == "exported"


def test_comments_blanks_and_export_prefix_are_handled(tmp_path):
    loaded = load_env_file(_write(
        tmp_path,
        "# a comment\n"
        "\n"
        "   \n"
        "export TOOLGATE_TEST_ONLY=via-export\n"
        "not a pair\n",
    ))
    assert loaded == ["TOOLGATE_TEST_ONLY"]
    assert os.environ["TOOLGATE_TEST_ONLY"] == "via-export"


def test_a_malformed_line_is_skipped_rather_than_raising(tmp_path):
    """A typo in `.env` must not be able to stop the gate from booting."""
    load_env_file(_write(tmp_path, "this line has no equals sign\nTYPESAFE_API_KEY=still-read\n"))
    assert os.environ["TYPESAFE_API_KEY"] == "still-read"


def test_quotes_are_stripped_and_an_unquoted_comment_dropped(tmp_path):
    load_env_file(_write(tmp_path, 'TYPESAFE_API_KEY="quoted value"\n'))
    assert os.environ["TYPESAFE_API_KEY"] == "quoted value"

    os.environ.pop("TYPESAFE_API_KEY")
    load_env_file(_write(tmp_path, "TYPESAFE_API_KEY=bare  # my key\n"))
    assert os.environ["TYPESAFE_API_KEY"] == "bare"


def test_a_value_containing_equals_survives(tmp_path):
    """Only the first `=` separates; a padded or base64 value keeps the rest."""
    load_env_file(_write(tmp_path, "TYPESAFE_API_KEY=abc=def==\n"))
    assert os.environ["TYPESAFE_API_KEY"] == "abc=def=="


def test_the_container_reads_dot_env_by_itself(tmp_path):
    """The automatic path, and the reason this module exists.

    Importing the container must be enough — no explicit call, no sourcing. Run in a subprocess
    with a clean environment because this test process imported the container long ago, so only a
    fresh import can prove the load happens on its own.
    """
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=apikey_from_dot_env\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("TYPESAFE_API_KEY", "JEV_API_KEY")}
    env["PYTHONPATH"] = str(ROOT)

    out = subprocess.run(
        [sys.executable, "-c",
         "from toolgate.container import Container\n"
         "c = Container()\n"
         "print(c.jev_client().api_key, c.engine().jev is c.jev_client())\n"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True)

    assert out.stdout.split() == ["apikey_from_dot_env", "True"], out.stderr
