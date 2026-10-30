"""Build-and-publish entry points used by the jobs (:mod:`mdhub.loaders.snapshots`), the CLI
and tests.

Each build reads in one ``REPEATABLE READ, READ ONLY`` transaction (one consistent view for
all datasets); publishing runs in a second transaction under the publish lock. A market cut
first builds the static data as known at the same instant and reuses (or publishes, cut type
``change``) the static snapshot with that content, so a market snapshot always names a static
snapshot whose instruments are exactly the universe its quotes were filtered by.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database, DictConnection
from mdhub.snapshot.build import Build, Cut
from mdhub.snapshot.market import MarketParams, build_market
from mdhub.snapshot.publish import PublishResult, ensure_static, publish
from mdhub.snapshot.static import MATURED_LOOKBACK_DAYS, build_static

__all__ = ["MarketOutcome", "publish_market_snapshot", "publish_static_snapshot"]


def _read_only(conn: DictConnection) -> None:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


def _static_cut(cut_type: str, business_date: date, cut_ts: datetime, known_at: datetime) -> Cut:
    return Cut("static", cut_type, business_date, cut_ts, known_at)


def publish_static_snapshot(
    db: Database,
    *,
    business_date: date,
    cut_ts: datetime,
    known_at: datetime,
    cut_type: str = "daily",
    run_id: int | None = None,
    matured_lookback_days: int = MATURED_LOOKBACK_DAYS,
) -> PublishResult:
    """Build the static snapshot as known at ``known_at`` and publish it for its cut
    (``unchanged`` when the same cut already holds that content)."""
    cut = _static_cut(cut_type, business_date, cut_ts, known_at)
    with db.connection() as conn:
        _read_only(conn)
        build = build_static(conn, cut=cut, matured_lookback_days=matured_lookback_days)
    with db.connection() as conn:
        return publish(conn, build, run_id=run_id)


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    market: PublishResult
    static: PublishResult


def publish_market_snapshot(
    db: Database,
    *,
    cut_type: str,
    business_date: date,
    cut_ts: datetime,
    known_at: datetime,
    run_id: int | None = None,
    params: MarketParams | None = None,
    matured_lookback_days: int = MATURED_LOOKBACK_DAYS,
    extra_stale: Mapping[str, Sequence[str]] | None = None,
    historical: bool = False,
) -> MarketOutcome:
    """Build and publish one market cut (plus its static snapshot when the static data
    changed since the last static snapshot with the same content). ``extra_stale``: stale
    reasons per dataset added to the manifest (:func:`mdhub.snapshot.market.build_market`).

    ``historical``: a cut of a past business date built later (a rebuild, a first publication
    of a missing day): its static is built for the business date, with terms as known at
    ``known_at`` and time-varying values as of the business date
    (:func:`mdhub.snapshot.static.build_static`); otherwise for the build day."""
    static_date = business_date if historical else known_at.astimezone(MSK).date()
    static_cut = _static_cut("change", static_date, cut_ts, known_at)
    market_cut = Cut("market", cut_type, business_date, cut_ts, known_at)
    with db.connection() as conn:
        _read_only(conn)
        quoted_secids = (
            frozenset(
                row["secid"]
                for row in conn.execute(
                    "SELECT secid FROM md.bond_eod_known_at(%(t)s, %(d)s, %(d)s) "
                    "UNION SELECT secid FROM md.bond_eod_yield_known_at(%(t)s, %(d)s, %(d)s)",
                    {"t": known_at, "d": business_date},
                ).fetchall()
            )
            if cut_type in ("eod_preliminary", "eod_final")
            else frozenset()
        )
        static_build: Build = build_static(
            conn,
            cut=static_cut,
            matured_lookback_days=matured_lookback_days,
            quoted_secids=quoted_secids,
            historical=historical,
        )
        market_build = build_market(
            conn, cut=market_cut, static=static_build, params=params, extra_stale=extra_stale
        )
    with db.connection() as conn:
        static_res = ensure_static(conn, static_build, run_id=run_id)
        market_build.static_snapshot_id = static_res.id  # bound once the static is stored
        market_res = publish(conn, market_build, run_id=run_id)
    return MarketOutcome(market=market_res, static=static_res)
