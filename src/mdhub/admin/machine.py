"""Machine page (ADMIN_DESIGN §10): host, disks, network, PostgreSQL and containers.

Values and states come from :mod:`mdhub.hostmon.status`. Sparklines and charts are inline
SVG rendered here from ``ops.host_metric_5m`` (presentation attributes and CSS classes only,
no ``style`` attributes and no script), so the CSP stays ``style-src 'self'``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from markupsafe import Markup, escape

from mdhub.admin.web import ENV, Ctx, render, require
from mdhub.hostmon.status import AREAS, Check, format_value, machine_status, series_key

__all__ = ["router", "svg_chart"]

router = APIRouter()

RANGES: Final = {"24h": ("24 hours", "5 minutes", 300), "7d": ("7 days", "1 hour", 3600)}
CHART_METRICS: Final = (
    "cpu.util_pct",
    "cpu.steal_pct",
    "mem.available_pct",
    "disk.used_pct",
    "net.rx_bps",
    "net.tx_bps",
    "pg.connections_pct",
    "pg.db_size_bytes",
)
Point = tuple[float, float, float]
"""``(epoch, avg, max)`` of one bucket."""

ENV.filters["metric"] = format_value


def svg_chart(
    points: Sequence[Point],
    *,
    t0: float,
    t1: float,
    step: float,
    unit: str,
    label: str,
    width: int = 120,
    height: int = 24,
    limit: float | None = None,
    axes: bool = False,
) -> Markup:
    """Inline SVG line of ``points`` between ``t0`` and ``t1`` (gaps > 3 steps break the line).

    ``axes`` adds the peak line, the threshold, and the value and time labels (charts);
    without it the result is a bare sparkline.
    """
    pad = 14 if axes else 1
    if not points:
        return Markup(  # noqa: S704 - numbers and escaped text only
            f'<svg class="spark empty" width="{width}" height="{height}" role="img" '
            f'aria-label="{escape(label)}: no data"><title>no data</title></svg>'
        )
    lows = [p[1] for p in points]
    peaks = [p[2] for p in points] if axes else lows
    lo = min(0.0, *lows)
    hi = max(*peaks, limit if limit is not None else lo) or 1.0
    if unit == "pct":
        hi = max(hi, 5.0)

    def xy(t: float, val: float) -> tuple[float, float]:
        x = (t - t0) / (t1 - t0) * width
        return x, pad + (1 - (val - lo) / ((hi - lo) or 1.0)) * (height - 2 * pad)

    def lines(index: int, css: str) -> str:
        segments: list[list[tuple[float, float]]] = [[]]
        prev: float | None = None
        for p in points:
            if prev is not None and p[0] - prev > 3 * step:
                segments.append([])
            segments[-1].append(xy(p[0], p[index]))
            prev = p[0]
        return "".join(
            f'<polyline class="{css}" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in seg)}"/>'
            if len(seg) > 1
            else f'<circle class="{css}" cx="{seg[0][0]:.1f}" cy="{seg[0][1]:.1f}" r="1.5"/>'
            for seg in segments
        )

    last = points[-1][1]
    title = f"{label}: last {format_value(last, unit)}, peak {format_value(max(peaks), unit)}"
    body = [lines(1, "avg")]
    if axes:
        body.insert(0, lines(2, "peak"))
        if limit is not None and lo <= limit <= hi:
            y = xy(t0, limit)[1]
            body.append(f'<line class="limit" x1="0" x2="{width}" y1="{y:.1f}" y2="{y:.1f}"/>')
        body.append(
            f'<text x="2" y="10">{escape(format_value(hi, unit))}</text>'
            f'<text x="2" y="{height - 3}">{escape(format_value(lo, unit))}</text>'
        )
    return Markup(  # noqa: S704 - numbers, fixed markup and escaped text only
        f'<svg class="{"chart" if axes else "spark"}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{''.join(body)}</svg>"
    )


def _series(
    conn: Any, keys: set[tuple[str, str]], window: str, bin_: str
) -> dict[tuple[str, str], list[Point]]:
    metrics = sorted({k[0] for k in keys})
    out: dict[tuple[str, str], list[Point]] = {}
    for r in conn.execute(
        """
        SELECT metric, labels,
               extract(epoch FROM date_bin(%s::interval, bucket,
                                           timestamptz '2000-01-01 00:00+00'))::float8 AS t,
               avg(avg) AS avg, max(max) AS max
        FROM ops.host_metric_5m
        WHERE metric = ANY(%s) AND bucket > now() - %s::interval
        GROUP BY 1, 2, 3 ORDER BY 3
        """,
        (bin_, metrics, window),
    ):
        key = (r["metric"], series_key(r["labels"]))
        if key in keys:
            out.setdefault(key, []).append((r["t"], r["avg"], r["max"]))
    return out


@router.get("/machine")
def machine_page(
    ctx: Annotated[Ctx, Depends(require("viewer"))],
    range: Literal["24h", "7d"] = "24h",
) -> Response:
    window, bin_, step = RANGES[range]
    with ctx.db.connection() as conn:
        machine = machine_status(conn, ctx.settings)
        data = _series(conn, {c.series for c in machine.checks if c.series}, window, bin_)
    t1 = machine.now.timestamp()
    t0 = t1 - (86400 if range == "24h" else 7 * 86400)

    def draw(c: Check, **kw: Any) -> Markup | None:
        if c.series is None:
            return None
        return svg_chart(
            data.get(c.series, []),
            t0=t0,
            t1=t1,
            step=step,
            unit=c.unit,
            label=c.label,
            limit=c.limit,
            **kw,
        )

    areas = [
        (a, machine.area_state(a), [(c, draw(c)) for c in machine.checks if c.area == a])
        for a in AREAS
    ]
    charts = [
        (c, draw(c, width=560, height=140, axes=True))
        for c in machine.checks
        if c.series and c.series[0] in CHART_METRICS and data.get(c.series)
    ]
    return render(ctx, "machine.html", m=machine, areas=areas, charts=charts, range=range)
