"""Markdown report and per-bond CSV of a :class:`mdhub.verify.static.VerificationRun`.

The Markdown report is written between :data:`BEGIN` and :data:`END` markers; when the output
file exists, text outside the markers (hand-written findings) is kept
(:func:`merge_generated`). Ordering is deterministic; the only volatile lines are the
generation timestamp and the run time.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Final

from mdhub.verify.bondmath import AI_TOL, AMOUNT_CONVENTIONS, YIELD_TOL_PCT
from mdhub.verify.static import CANDIDATES, REFERENCE_CANDIDATE, BondResult, VerificationRun

__all__ = ["BEGIN", "CSV_COLUMNS", "END", "merge_generated", "render_csv", "render_markdown"]

BEGIN: Final = "<!-- BEGIN GENERATED: mdhub verify static -->"
END: Final = "<!-- END GENERATED: mdhub verify static -->"
EXAMPLES: Final = 5
"""Example SECIDs per mismatch pattern."""

INVARIANT_CHECKS: Final = (
    ("amortization_sum", "Redemptions (amortizations + final) sum to the initial face"),
    ("redemption_at_maturity", "Last redemption is dated on the maturity date"),
    ("coupon_dates", "Coupon dates increase, periods neither overlap nor leave gaps"),
    ("coupon_amount_vs_rate", "Coupon amount = rate x period face x days / 365"),
    ("fixed_coupons_known", "Active fixed bonds: every future coupon has an amount or rate"),
    ("floater_formula", "Every floater has a resolved formula with a benchmark"),
    ("perpetual_label", "Perpetuals labelled; maturity agrees with MOEX EOD MATDATE"),
)

CSV_COLUMNS: Final = (
    "secid",
    "bond_class",
    "coupon_type",
    "status",
    "face_unit",
    "ai_rows",
    "ai_checked",
    "ai_matched",
    "ai_match_pct",
    "ai_convention",
    "ai_basis",
    "ai_ref_match_pct",
    "ai_skipped",
    "ai_reasons",
    "ai_example",
    "y_rows",
    "y_checked",
    "y_matched",
    "y_match_pct",
    "y_max_abs_diff_pp",
    "y_skipped",
    "y_reasons",
    "y_example",
    "amount_convention",
    "invariants_failed",
)


# --------------------------------------------------------------------------- helpers


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.2f}%" if den else "-"


def _counts(c: Counter[str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _md_table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(v) for v in r) + " |")
    return out


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _first_example(results: Sequence[BondResult], reason: str, attr: str) -> str:
    for r in results:
        ex = getattr(r, attr).get(reason)
        if ex:
            return str(ex)
    return "-"


def _top_bonds(items: Iterable[tuple[BondResult, int]], n: int = EXAMPLES) -> str:
    ranked = sorted(items, key=lambda kv: (-kv[1], kv[0].secid))[:n]
    return ", ".join(f"{b.secid} ({k})" for b, k in ranked)


# ---------------------------------------------------------------------- sections


def _summary(run: VerificationRun) -> list[str]:
    bonds = run.bonds
    ai_ref_chk = sum(b.ai_ref_checked for b in bonds)
    ai_ref_ok = sum(b.ai_ref_matched for b in bonds)
    ai_chk = sum(b.ai_checked for b in bonds)
    ai_ok = sum(b.ai_matched for b in bonds)
    y_chk = sum(b.y_checked for b in bonds)
    y_ok = sum(b.y_matched for b in bonds)
    rows: list[list[Any]] = [
        [
            "Accrued interest, reference `coupon_pro_rata@trade`",
            "EOD rows",
            ai_ref_chk,
            ai_ref_ok,
            _pct(ai_ref_ok, ai_ref_chk),
        ],
        [
            "Accrued interest, inferred per-bond convention",
            "EOD rows",
            ai_chk,
            ai_ok,
            _pct(ai_ok, ai_chk),
        ],
        ["Yield (fixed + zero coupon)", "yield rows", y_chk, y_ok, _pct(y_ok, y_chk)],
    ]
    for check, title in INVARIANT_CHECKS:
        res = [i for b in bonds for i in b.invariants if i.check == check and i.ok is not None]
        ok = sum(1 for i in res if i.ok)
        rows.append([f"Invariant `{check}`: {title}", "bonds", len(res), ok, _pct(ok, len(res))])
    return _md_table(["check", "unit", "checked", "matched / passed", "rate"], rows)


def _date_basis(run: VerificationRun) -> list[str]:
    agg: dict[str, Counter[str]] = defaultdict(Counter)
    for b in run.bonds:
        for board, c in b.ai_boards.items():
            agg[board].update(c)
    rows = []
    for board in sorted(agg, key=lambda k: -agg[k]["rows"]):
        c = agg[board]
        t_only, s_only = c["trade"] - c["both"], c["settle"] - c["both"]
        basis = "trade" if t_only > s_only else "settle" if s_only > t_only else "undetermined"
        rows.append(
            [
                board,
                f"T+{run.board_lags[board]}" if board in run.board_lags else "T+1 (default)",
                c["rows"],
                _pct(c["trade"], c["rows"]),
                _pct(c["settle"], c["rows"]),
                _pct(c["both"], c["rows"]),
                basis,
            ]
        )
    return _md_table(
        [
            "board",
            "settle lag",
            "rows",
            "trade-date basis matches",
            "settle-date basis matches",
            "both bases",
            "inferred basis",
        ],
        rows,
    )


def _convention_pivot(run: VerificationRun, group: str) -> list[str]:
    keys_used = [k for k in CANDIDATES if any(b.ai_key == k for b in run.bonds)]
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for b in run.bonds:
        if b.ai_checked == 0:
            continue
        g = getattr(b, group)
        key = b.ai_key if b.ai_key and b.ai_matched > 0 else "no match"
        by[g][key] += 1
    cols = [*keys_used, "no match"]
    rows = []
    for g in sorted(by, key=lambda k: (-sum(by[k].values()), k)):
        total = sum(by[g].values())
        rows.append([g, total, *[by[g].get(k, 0) or "" for k in cols]])
    return _md_table([group.replace("_", " "), "bonds", *[f"`{k}`" for k in cols]], rows)


def _ai_rates(run: VerificationRun) -> list[str]:
    groups: dict[tuple[str, str], list[BondResult]] = defaultdict(list)
    for b in run.bonds:
        if b.ai_rows:
            groups[(b.bond_class, b.coupon_type)].append(b)
    rows = []
    for (cls, ct), bs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        chk = sum(b.ai_checked for b in bs)
        ok = sum(b.ai_matched for b in bs)
        rchk = sum(b.ai_ref_checked for b in bs)
        rok = sum(b.ai_ref_matched for b in bs)
        skipped = sum(sum(b.ai_skipped.values()) for b in bs)
        evaluated = [b for b in bs if b.ai_checked]
        full = sum(1 for b in evaluated if b.ai_matched == b.ai_checked)
        low = sum(1 for b in evaluated if b.ai_matched < 0.95 * b.ai_checked)
        rows.append([cls, ct, len(bs), chk, _pct(rok, rchk), _pct(ok, chk), full, low, skipped])
    return _md_table(
        [
            "bond class",
            "coupon type",
            "bonds",
            "rows checked",
            "match (reference)",
            "match (inferred)",
            "bonds 100%",
            "bonds <95%",
            "rows skipped",
        ],
        rows,
    )


def _skips(run: VerificationRun, attr: str) -> list[str]:
    rows_by: Counter[str] = Counter()
    bonds_by: dict[str, list[tuple[BondResult, int]]] = defaultdict(list)
    for b in run.bonds:
        for reason, n in getattr(b, attr).items():
            rows_by[reason] += n
            bonds_by[reason].append((b, n))
    rows = [
        [f"`{r}`", n, len(bonds_by[r]), _top_bonds(bonds_by[r])]
        for r, n in sorted(rows_by.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return _md_table(["reason", "rows", "bonds", "examples (rows)"], rows)


PATTERN_NOTES: Final = {
    "unexplained": "not explained by the patterns below - inspect",
    "no_coupon_period": "static: MOEX accrues where the schedule has no period",
    "moex_zero": "source: MOEX accrues nothing (default / suspended accrual)",
    "face_differs": "static: EOD face value differs from the schedule's outstanding face",
    "eod_coupon_differs": "static: schedule amount differs from MOEX COUPONVALUE of the day",
    "floater_accrual_vs_final_coupon": "source: floater accrued at an interim rate (in arrears)",
    "full_coupon": "source: MOEX shows the whole coupon (pay-date handling)",
    "coupon_not_fixed": "projection: period coupon is a MOEX projection",
    "accrual_rate_differs": "label: MOEX accrued at another rate (coupon not fixed in advance)",
    "accrued_differs": "MOEX's settlement accrued interest (used) differs from the schedule's",
    "fx_face_bond": "foreign-currency face: MOEX yield a few bp off (convention unknown)",
    "maturity_differs": "static: MOEX yield date differs from ref maturity",
    "offer_not_in_ref": "static: MOEX yields to an offer missing in ref.bond_offer",
    "short_horizon": "horizon <= 1 year: MOEX convention may differ",
    "solver_failed": "no yield solves the price (price/flows inconsistent)",
}
"""Interpretation of a mismatch pattern: ``static`` = points at the reference data, ``source``
= MOEX behaviour, not a static-data error."""


def _note(reason: str) -> str:
    if reason.startswith("day_shift_"):
        return "dates: MOEX accrual is n days off the schedule's period start"
    return PATTERN_NOTES.get(reason, "-")


def _floaters(run: VerificationRun) -> list[str]:
    groups: dict[str, list[BondResult]] = defaultdict(list)
    for b in run.bonds:
        if b.coupon_type == "floating" and b.ai_checked:
            groups[b.benchmark or "(no benchmark)"].append(b)
    rows = []
    for bench, bs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        chk = sum(b.ai_checked for b in bs)
        ok = sum(b.ai_matched for b in bs)
        full = sum(1 for b in bs if b.ai_matched == b.ai_checked)
        low = sum(1 for b in bs if b.ai_matched < 0.95 * b.ai_checked)
        rows.append([bench, len(bs), chk, _pct(ok, chk), full, low])
    return _md_table(
        ["benchmark", "bonds", "rows checked", "match (inferred)", "bonds 100%", "bonds <95%"],
        rows,
    )


def _patterns(run: VerificationRun, attr: str, ex_attr: str) -> list[str]:
    rows_by: Counter[str] = Counter()
    bonds_by: dict[str, list[tuple[BondResult, int]]] = defaultdict(list)
    classes: dict[str, Counter[str]] = defaultdict(Counter)
    for b in run.bonds:
        for reason, n in getattr(b, attr).items():
            rows_by[reason] += n
            bonds_by[reason].append((b, n))
            classes[reason][f"{b.bond_class}/{b.coupon_type}"] += n
    rows = []
    for reason, n in sorted(rows_by.items(), key=lambda kv: (-kv[1], kv[0])):
        bs = bonds_by[reason]
        top = [b for b, _ in sorted(bs, key=lambda kv: (-kv[1], kv[0].secid))]
        rows.append(
            [
                f"`{reason}`",
                _note(reason),
                n,
                len(bs),
                ", ".join(f"{k} ({v})" for k, v in classes[reason].most_common(3)),
                _top_bonds(bs),
                _first_example(top, reason, ex_attr),
            ]
        )
    return _md_table(
        [
            "pattern",
            "meaning",
            "rows",
            "bonds",
            "top class/coupon type (rows)",
            "example SECIDs (rows)",
            "example",
        ],
        rows,
    )


def _yield_rates(run: VerificationRun) -> list[str]:
    groups: dict[tuple[str, str], list[BondResult]] = defaultdict(list)
    for b in run.bonds:
        if b.y_rows:
            groups[(b.bond_class, b.coupon_type)].append(b)
    rows = []
    for (cls, ct), bs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        chk = sum(b.y_checked for b in bs)
        ok = sum(b.y_matched for b in bs)
        skipped = sum(sum(b.y_skipped.values()) for b in bs)
        evaluated = [b for b in bs if b.y_checked]
        full = sum(1 for b in evaluated if b.y_matched == b.y_checked)
        rows.append([cls, ct, len(bs), chk, ok, _pct(ok, chk), full, skipped])
    return _md_table(
        [
            "bond class",
            "coupon type",
            "bonds",
            "rows checked",
            "matched",
            "rate",
            "bonds 100%",
            "rows skipped",
        ],
        rows,
    )


def _invariants(run: VerificationRun) -> list[str]:
    out: list[str] = []
    rows = []
    for check, title in INVARIANT_CHECKS:
        res = [(b, i) for b in run.bonds for i in b.invariants if i.check == check]
        ok = sum(1 for _, i in res if i.ok is True)
        bad = sum(1 for _, i in res if i.ok is False)
        na = Counter(i.reason for _, i in res if i.ok is None)
        rows.append([f"`{check}`", title, ok + bad, ok, bad, _pct(ok, ok + bad), _counts(na)])
    out += _md_table(
        ["check", "rule", "applicable", "pass", "fail", "pass rate", "not applicable"], rows
    )
    out += ["", "### Invariant failures by reason", ""]
    fail_rows = []
    by: dict[tuple[str, str], list[BondResult]] = defaultdict(list)
    details: dict[tuple[str, str], str] = {}
    for b in run.bonds:
        for i in b.failed_invariants():
            by[(i.check, i.reason)].append(b)
            if i.detail and (i.check, i.reason) not in details:
                details[(i.check, i.reason)] = f"{b.secid}: {i.detail}"
    for (check, reason), bs in sorted(by.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
        active = sum(1 for b in bs if b.status == "active")
        cls = Counter(f"{b.bond_class}/{b.coupon_type}" for b in bs).most_common(3)
        examples = sorted(bs, key=lambda b: (b.status != "active", b.secid))[:EXAMPLES]
        fail_rows.append(
            [
                f"`{check}`",
                f"`{reason}`",
                len(bs),
                active,
                ", ".join(f"{k} ({v})" for k, v in cls),
                ", ".join(b.secid for b in examples),
                details.get((check, reason), "-"),
            ]
        )
    out += _md_table(
        [
            "check",
            "reason",
            "bonds",
            "active",
            "top class/coupon type",
            "example SECIDs",
            "example detail",
        ],
        fail_rows,
    )
    out += ["", "### Invariant pass rate by bond class (active bonds)", ""]
    cls_rows = []
    active_classes = sorted({b.bond_class for b in run.bonds if b.status == "active"})
    for bond_class in active_classes:
        bs = [b for b in run.bonds if b.status == "active" and b.bond_class == bond_class]
        cells: list[Any] = [bond_class, len(bs)]
        for check, _ in INVARIANT_CHECKS:
            app = [i for b in bs for i in b.invariants if i.check == check and i.ok is not None]
            passed = sum(1 for i in app if i.ok)
            cells.append(f"{passed}/{len(app)}" if app else "-")
        cls_rows.append(cells)
    out += _md_table(
        ["bond class", "active bonds", *[f"`{c}`" for c, _ in INVARIANT_CHECKS]], cls_rows
    )
    return out


def _amount_conventions(run: VerificationRun) -> list[str]:
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for b in run.bonds:
        conv = b.amount_convention
        if conv is not None:
            by[b.bond_class][conv] += 1
    cols = [c for c in AMOUNT_CONVENTIONS if any(c in v for v in by.values())]
    rows = [
        [cls, sum(by[cls].values()), *[by[cls].get(c, 0) or "" for c in cols]]
        for cls in sorted(by, key=lambda k: (-sum(by[k].values()), k))
    ]
    return _md_table(["bond class", "bonds", *[f"`{c}`" for c in cols]], rows)


# ---------------------------------------------------------------------------- render


def _zcyc_floaters(run: VerificationRun) -> list[str]:
    rows = []
    for z in run.zcyc_floaters:
        r = z.reproduction
        rows.append(
            [
                z.secid,
                r["method"],
                r["tenor_years"] or "-",
                r["fixing_offset"] if r["fixing_offset"] is not None else "-",
                f"{r['periods_matched']}/{r['periods_checked']}",
                r["max_abs_error_pct"] or "-",
                "yes" if r["accepted"] else f"no: {r['reason']}",
                ", ".join(map(str, r["unmatched_coupon_nos"])) or "-",
            ]
        )
    if not rows:
        return ["No floater with benchmark ZCYC."]
    ok = sum(1 for z in run.zcyc_floaters if z.reproduction["accepted"])
    return [
        f"{ok} of {len(rows)} KBD floaters reproduced (accepted).",
        "",
        *_md_table(
            [
                "secid",
                "terms",
                "T, years",
                "N",
                "matched",
                "max error, pp",
                "accepted",
                "unmatched coupons",
            ],
            rows,
        ),
    ]


def _zcyc_calendar(run: VerificationRun) -> list[str]:
    c = run.zcyc_calendar
    if c is None or c.first is None:
        return ["No end-of-day KBD publication in md.zcyc_snapshot."]

    def days(ds: Sequence[Any]) -> str:
        return ", ".join(str(d) for d in ds[:20]) + (" ..." if len(ds) > 20 else "") or "none"

    return [
        f"* End-of-day publications: {c.publication_days:,} days, {c.first} .. {c.last}.",
        f"* MOEX_STOCK business days without a publication ({len(c.business_without_publication)}):"
        f" {days(c.business_without_publication)}.",
        f"* Publications on a non-business day ({len(c.publication_not_business)}):"
        f" {days(c.publication_not_business)}.",
    ]


def render_markdown(run: VerificationRun) -> str:
    """The generated block (including the markers)."""
    with_ai = [b for b in run.bonds if b.ai_rows]
    with_y = [b for b in run.bonds if b.y_rows]
    lines = [
        BEGIN,
        "",
        f"Generated {run.generated_at:%Y-%m-%d %H:%M} UTC by `mdhub verify static` in"
        f" {run.duration_s:.0f} s (read-only).",
        "",
        f"* EOD rows (`md.bond_eod`): {run.eod_rows:,} from {run.eod_first or '-'} to"
        f" {run.eod_last or '-'}, {len(with_ai):,} bonds; `ref.bond`: {run.bonds_in_ref:,} bonds.",
        "* Yield rows (`md.bond_eod_yield`, fixed + zero coupon):"
        f" {sum(b.y_rows for b in with_y):,} on {len(run.yield_dates)} trade dates"
        f" (sample `{run.yield_sample}`), {len(with_y):,} bonds.",
        f"* Tolerances: accrued interest {AI_TOL} (face currency), yield {YIELD_TOL_PCT} pp.",
        f"* Reference candidate `{REFERENCE_CANDIDATE}` = coupon amount x days elapsed / period"
        " days, accrued to the trade date; the inferred candidate is the best of"
        f" {len(CANDIDATES)} convention x date-basis combinations per bond.",
        "",
        "## Summary",
        "",
        *_summary(run),
        "",
        "## 1. Accrued interest vs MOEX ACCRUEDINT",
        "",
        "### Date basis by board",
        "",
        "Matches of each bond's inferred convention evaluated on the trade date vs the settlement"
        " date (trade date + board lag in MOEX business days). A row matches both bases only when"
        " the accrual does not change between the two dates (e.g. zero accrual).",
        "",
        *_date_basis(run),
        "",
        "### Inferred convention by bond class (bonds)",
        "",
        *_convention_pivot(run, "bond_class"),
        "",
        "### Inferred convention by coupon type (bonds)",
        "",
        *_convention_pivot(run, "coupon_type"),
        "",
        "### Match rates by bond class and coupon type",
        "",
        *_ai_rates(run),
        "",
        "### Floaters by benchmark",
        "",
        "MOEX accrues a floater's current period at the rate known so far; the schedule holds"
        " the final coupon. Benchmarks fixed before the period should match; in-arrears"
        " benchmarks (RUONIA averages) cannot until the period ends.",
        "",
        *_floaters(run),
        "",
        "### Top mismatch patterns",
        "",
        "Rows the bond's inferred convention does not match, by the first applicable pattern"
        " (`mdhub.verify.static.classify_ai_mismatch`).",
        "",
        *_patterns(run, "ai_reasons", "ai_examples"),
        "",
        "### Rows not evaluated",
        "",
        *_skips(run, "ai_skipped"),
        "",
        "## 2. Yield vs MOEX effective yield (fixed and zero-coupon bonds)",
        "",
        *_yield_rates(run),
        "",
        "### Yield mismatch patterns",
        "",
        *_patterns(run, "y_reasons", "y_examples"),
        "",
        "### Yield rows not evaluated",
        "",
        *_skips(run, "y_skipped"),
        "",
        "## 3. Static invariants (all bonds in ref.bond)",
        "",
        *_invariants(run),
        "",
        "### Coupon amount convention by bond class (bonds, most coupons explained)",
        "",
        *_amount_conventions(run),
        "",
        "## 4. KBD (ZCYC) floaters: coupon rates reproduced from the ZCYC history",
        "",
        "rate % = round(Y_KBD(T; N-th KBD publication day before the period start), 2) +"
        " margin of the period; a coupon matches within 0.005 pp. `check` = documented"
        " (curated) terms, `fit` = the hub's fit (job `zcyc_floater_fit`; tenor and offset are"
        " published only when accepted).",
        "",
        *_zcyc_floaters(run),
        "",
        "### KBD publication calendar (`zcyc_publication_days`)",
        "",
        *_zcyc_calendar(run),
        "",
        END,
    ]
    return "\n".join(lines) + "\n"


def merge_generated(existing: str | None, generated: str, title: str) -> str:
    """Replace the generated block of ``existing`` (keep hand-written text around it); a new
    document is ``# title`` + the block."""
    if existing and BEGIN in existing and END in existing:
        head, rest = existing.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return head + generated.rstrip("\n") + tail
    return f"# {title}\n\n{generated}"


def render_csv(run: VerificationRun) -> str:
    """One row per bond of ``ref.bond`` (columns :data:`CSV_COLUMNS`)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for b in run.bonds:
        failed = "; ".join(
            f"{i.check}:{i.reason}" + (f" ({i.detail})" if i.detail else "")
            for i in b.failed_invariants()
        )
        w.writerow(
            [
                b.secid,
                b.bond_class,
                b.coupon_type,
                b.status,
                b.face_unit or "",
                b.ai_rows,
                b.ai_checked,
                b.ai_matched,
                f"{100.0 * b.ai_matched / b.ai_checked:.2f}" if b.ai_checked else "",
                b.ai_convention or "",
                b.ai_basis or "",
                f"{100.0 * b.ai_ref_matched / b.ai_ref_checked:.2f}" if b.ai_ref_checked else "",
                _counts(b.ai_skipped),
                _counts(b.ai_reasons),
                next(iter(b.ai_examples.values()), ""),
                b.y_rows,
                b.y_checked,
                b.y_matched,
                f"{100.0 * b.y_matched / b.y_checked:.2f}" if b.y_checked else "",
                f"{b.y_max_abs_diff:.4f}" if b.y_max_abs_diff is not None else "",
                _counts(b.y_skipped),
                _counts(b.y_reasons),
                next(iter(b.y_examples.values()), ""),
                b.amount_convention or "",
                failed,
            ]
        )
    return buf.getvalue()
