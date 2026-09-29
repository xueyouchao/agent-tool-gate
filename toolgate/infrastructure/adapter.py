"""Deterministic adapter: normalize a tool call into Cedar entities + request.

The adapter is the "garbage-in" boundary (spec §4): it maps tool args to Cedar
entities and context by parsing alone — no Jev, no semantics. A wrong mapping
silently disables policies, so this is load-bearing and unit-tested.
"""
from __future__ import annotations

import re
import shlex
from urllib.parse import urlparse

from ..domain.battery import QUESTIONS
from ..domain.model import Principal, SECRET_PATH_SHAPES


# Phase-2 context fields are filled by the thresholding layer (M2). Cedar requires every
# declared context field present, so for phase 1 they default fail-closed: `unknown` for the
# choice question (thresholding treats it as a breach) and `False` for every noul.
PHASE2_CONTEXT_DEFAULTS = {
    **{q.context: "unknown" if q.kind == "choice" else False
       for q in QUESTIONS if q.context},
    "confidence_floor": 0,
}

# Context fields no battery question feeds: four the adapter parses, one thresholding derives.
# Declared so `tests/test_battery.py` can account for every field the schema declares.
DERIVED_CONTEXT_FIELDS = ("tool", "subcommand", "command", "reads_secret_path",
                          "confidence_floor")

DELETE_SUBCOMMANDS = ("delete", "uninstall", "purge", "destroy", "drop", "truncate")
# Cluster tools whose destructive subcommands name an object inside a namespace. They differ in
# where they put the name, but agree on the part that decides the verdict: `-n/--namespace` is the
# scope, so `-n prod` is production whether the tool is helm or kubectl.
NAMESPACED_TOOLS = ("helm", "kubectl")
REPO_TOOLS = ("ls", "cat", "git")
REPO_SUBCOMMANDS = ("status", "log", "diff")
SECRET_FILE_TOOLS = ("rm", "cp", "mv", "echo", "chmod", "chown", "cat", "less", "sed", "awk")

# SQL clients that carry a statement in a flag, and the destructive DDL vocabulary. SQL puts the
# verb FIRST — `DROP DATABASE prod_db` — which is the opposite of the `<tool> <verb>` order the
# positional parse below assumes, so the verb lands in `tool` where no policy reads it.
SQL_CLIENTS = ("psql", "mysql", "mariadb", "sqlite3")
SQL_CLIENTS_FLAGS = ("-c", "--command", "-e", "--execute")
DDL_VERBS = ("drop", "truncate")
DDL_TARGETS = ("database", "schema", "table")


def _flag_value(tokens: list[str], *flags: str) -> str | None:
    """The value given to any of *flags*, in the `-n prod` or the `--namespace=prod` form.

    Both spellings are ordinary usage, and only the first one used to parse — so a production
    namespace written with `=` read as `default` and slipped past the delete-class policy.
    """
    for i, t in enumerate(tokens):
        if t in flags and i + 1 < len(tokens):
            return tokens[i + 1]
        for flag in flags:
            if t.startswith(flag + "="):
                return t[len(flag) + 1:]
    return None


def _namespaced_target(tool: str, tokens: list[str]) -> str:
    """The object a namespaced delete names — best-effort, and only used to label the record.

    The grammars disagree: `helm delete <release>` names it first, while `kubectl delete <kind>
    <name>` puts a *kind* first, so reading kubectl's second token would label the call
    `Database::"pod"`. Only the leading positionals are read; from the first flag on, the flags own
    the rest. The target never decides anything — the namespace does — so a miss here is a cosmetic
    label, not a missed block.
    """
    leading: list[str] = []
    for token in tokens[2:]:
        if token.startswith("-"):
            break
        leading.append(token)
    if not leading:
        return "unknown"
    if tool == "kubectl" and len(leading) > 1:
        return leading[1]
    return leading[0]


def _namespace_to_scope(ns: str) -> str:
    return {"prod": "production", "production": "production", "staging": "staging", "stage": "staging"}.get(ns, ns)


def _name_to_scope(name: str) -> str:
    """The scope a *name* implies, for a resource with no namespace flag to read.

    ``_namespace_to_scope`` matches the whole string, which suits a namespace; a database is named
    ``prod_db`` or ``production_orders``, so fall back to splitting on separators. Splitting matters
    rather than a substring test, or ``reproduce`` would read as production.
    """
    lowered = name.lower()
    mapped = _namespace_to_scope(lowered)
    if mapped != lowered:
        return mapped
    words = set(re.split(r"[^a-z0-9]+", lowered))
    return "production" if words & {"prod", "production"} else "dev"


def _sql_ddl(tokens: list[str]) -> tuple[str, str] | None:
    """``(verb, name)`` for a destructive schema change, or ``None``.

    Two shapes reach here: the statement *is* the command (``DROP DATABASE prod_db``), or a client
    carries it in a flag (``psql -c "DROP DATABASE prod_db"``). Read by keyword rather than by
    position, because the verb comes first in SQL.
    """
    if not tokens:
        return None
    if tokens[0] in SQL_CLIENTS:
        payload = _flag_value(tokens, *SQL_CLIENTS_FLAGS)
        if not payload:
            return None
        try:
            statement = shlex.split(payload)
        except ValueError:      # unbalanced quotes: not a statement we can read
            return None
    else:
        statement = tokens
    if len(statement) >= 3 and statement[0].lower() in DDL_VERBS \
            and statement[1].lower() in DDL_TARGETS:
        return statement[0].lower(), statement[2]
    return None


def _find_url(tokens: list[str]) -> str | None:
    for t in tokens:
        if t.startswith(("http://", "https://")):
            return t
    return None


def _detects_secret_read(command: str) -> bool:
    """Whether the command names a credential store — the list the scrubber redacts by, too.

    Matching is on the shape alone, so `rm ~/.ssh/config` and `rm /home/agent/.ssh/config` are the
    same call. It is deliberately broad: a false positive here maps an ordinary path into the
    secret zone and phase 1 refuses it, which is the fail-closed direction.
    """
    return any(shape in command for shape in SECRET_PATH_SHAPES)


def normalize(command: str, principal: Principal, cwd: str = "/workspace") -> tuple[list[dict], dict]:
    """Return ``(entities, request)`` for a single tool call's command string."""
    tokens = shlex.split(command) if command.strip() else []
    exec_name = tokens[0] if tokens else ""
    subcommand = tokens[1] if len(tokens) > 1 else ""

    # A destructive DDL verb is the *first* token, so the positional read above puts it in `tool`,
    # where no policy looks. Surface it as the subcommand, which is what the delete-class policy
    # reads — otherwise `DROP DATABASE prod_db` normalizes to tool="DROP", subcommand="DATABASE".
    ddl = _sql_ddl(tokens)
    if ddl:
        subcommand = ddl[0]

    context = {
        "tool": exec_name,
        "subcommand": subcommand,
        "command": command,
        "reads_secret_path": _detects_secret_read(command),
        **PHASE2_CONTEXT_DEFAULTS,
    }

    entities: list[dict] = [principal.entity()]
    resource_type = resource_id = None
    resource_attrs: dict = {}
    workspace_id: str | None = None

    if ddl:
        # the database it names, scoped by that name: there is no namespace flag to read here
        _verb, db_name = ddl
        resource_type, resource_id = "Database", db_name
        resource_attrs = {"scope": _name_to_scope(db_name), "zone": "infra"}
        workspace_id = "default"

    elif exec_name in NAMESPACED_TOOLS and subcommand in DELETE_SUBCOMMANDS:
        # `Database` is this schema's namespaced-infra slot: a helm release and a kubectl object
        # both land there, and `scope`/`zone` are all any policy reads.
        target = _namespaced_target(exec_name, tokens)
        ns = _flag_value(tokens, "-n", "--namespace") or "default"
        resource_type, resource_id = "Database", target
        resource_attrs = {"scope": _namespace_to_scope(ns), "zone": "infra"}
        workspace_id = ns

    elif exec_name == "curl":
        url = _find_url(tokens)
        host = urlparse(url).hostname if url else "unknown"
        resource_type, resource_id = "Url", (host or "unknown")
        resource_attrs = {"allowlisted": False, "zone": "unknown", "scope": "external"}

    elif exec_name in SECRET_FILE_TOOLS and _detects_secret_read(command):
        # a file op on a secret path (e.g. `rm ~/.ssh/config`) → Path in the secret zone
        target = next((t for t in tokens[1:] if _detects_secret_read(t)), cwd)
        resource_type, resource_id = "Path", target
        resource_attrs = {"zone": "secret", "scope": "dev"}
        workspace_id = "repo"

    elif exec_name in REPO_TOOLS:
        resource_type, resource_id = "Path", cwd
        resource_attrs = {"zone": "workspace", "scope": "dev"}
        workspace_id = "repo"

    else:
        resource_type, resource_id = "Path", cwd
        resource_attrs = {"zone": "workspace", "scope": "dev"}
        workspace_id = "default"

    if workspace_id:
        entities.append({
            "uid": {"type": "toolgate::Workspace", "id": workspace_id},
            "attrs": {"root": cwd},
            "parents": [],
        })

    entities.append({
        "uid": {"type": f"toolgate::{resource_type}", "id": resource_id},
        "attrs": resource_attrs,
        "parents": [{"type": "toolgate::Workspace", "id": workspace_id}] if workspace_id else [],
    })

    request = {
        "principal": {"type": "toolgate::Agent", "id": principal.id},
        "action": {"type": "toolgate::Action", "id": "execute"},
        "resource": {"type": f"toolgate::{resource_type}", "id": resource_id},
        "context": context,
    }
    return entities, request
