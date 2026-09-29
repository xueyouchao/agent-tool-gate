// A path the adapter mapped into the secret zone is off-limits for ANY action, not just
// writes — despite the id. `context.reads_secret_path` (spec §4) is the adapter's other
// secret signal; it is deliberately NOT a phase-1 forbid, because a *command that merely
// references* a secret path (e.g. `curl -d @~/.ssh/config`) must stay GRAY and reach Jev
// (spec walkthrough B), where `secret-egress-v1` judges the intent.
@id("secrets-never-writable-v1")
forbid (principal, action, resource)
when { resource.zone == "secret" };

// `drop` and `truncate` are here because SQL spells the same operation differently — the adapter
// reads a statement's leading verb into `context.subcommand` so this list can see it, whether the
// statement is the command (`DROP DATABASE prod_db`) or a flag on a client (`psql -c ...`).
@id("prod-delete-class-v1")
forbid (principal, action, resource)
when { resource.scope == "production" && ["delete", "uninstall", "purge", "destroy", "drop", "truncate"].contains(context.subcommand) };

@id("read-only-repo-permit-v1")
permit (principal, action == toolgate::Action::"execute", resource in toolgate::Workspace::"repo")
when { ["ls", "cat", "git"].contains(context.tool) && ["status", "log", "diff"].contains(context.subcommand) };

@id("pipe-to-shell-v1")
forbid (principal, action, resource)
when { context.command like "* | sh*" || context.command like "* | bash*" };
