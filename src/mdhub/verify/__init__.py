"""Read-only verification of loaded data against what the sources publish.

* :mod:`mdhub.verify.bondmath` - pure bond arithmetic: coupon periods, outstanding face,
  accrued interest under candidate day-count conventions, MOEX-style effective yield, static
  schedule invariants (no DB access),
* :mod:`mdhub.verify.static` - ``mdhub verify static``: recomputes accrued interest and yields
  from ``ref.bond*`` and compares them with MOEX ``md.bond_eod`` / ``md.bond_eod_yield``,
* :mod:`mdhub.verify.render` - Markdown report and per-bond CSV of a verification run.
"""
