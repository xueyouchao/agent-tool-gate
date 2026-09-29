"""Cedar policy artifacts — the domain's authorization rules, as data.

These ``.cedar`` files are authored and reviewed here, in the domain layer, so the rules stay
visible instead of buried in an adapter. Loading, startup validation and versioning live in
``infrastructure.policy_set``; nothing in the domain reads them from disk.
"""
