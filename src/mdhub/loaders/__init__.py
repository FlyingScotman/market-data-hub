"""Loaders: fetch -> capture -> parse -> upsert, one module per dataset family.

Every module in this package is imported automatically by :func:`mdhub.jobs.discover_jobs`
(via :mod:`pkgutil`), so a new loader only needs to define functions decorated with
:func:`mdhub.jobs.job`. Modules whose name starts with ``_`` are private helpers and are
not imported by discovery (import them explicitly from a loader if needed).
"""
