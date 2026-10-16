"""CLI extensions.

Every module in this package that exposes ``app: typer.Typer`` is registered as a
sub-command group of the ``mdhub`` CLI. The group name is the module attribute ``name``
(a ``str``) if present, otherwise the module name with ``_`` replaced by ``-``.
See ``docs/DEVELOPING.md``.
"""
