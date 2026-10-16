"""``/v1/export/tdm``: zip of Trading Desk Monitor contract-1.3 CSV files."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from mdhub.api.common import bad_request, read_connection
from mdhub.export.tdm import PRICE_BASES, TdmExportOptions, build_tdm_export

router = APIRouter(tags=["export"])


@router.get(
    "/export/tdm",
    summary="TDM contract 1.3 CSV bundle (zip)",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
def export_tdm(
    request: Request,
    day: Annotated[date, Query(alias="date", description="export (TDM run) date")],
    date_from: Annotated[
        date | None, Query(alias="from", description="market series from (default: date)")
    ] = None,
    price_basis: Annotated[
        str, Query(description=f"md_bond_quotes price: {', '.join(PRICE_BASES)}")
    ] = "waprice",
    calendar: Annotated[str, Query(description="hub calendar exported as TDM calendar")] = (
        "MOEX_STOCK"
    ),
    fx_candidate: Annotated[
        bool,
        Query(
            description="include md_fx candidate from MOEX 19:00 indicative rates (opt-in: "
            "TDM loads it as the market FX mark and would overwrite the internal mark)"
        ),
    ] = False,
) -> Response:
    """Files ``<dataset>_<date>.csv`` (``;``-separated, UTF-8 BOM) plus ``manifest.json``
    (row counts, SHA-256, excluded rows, datasets the hub cannot supply)."""
    try:
        options = TdmExportOptions(
            date_to=day,
            date_from=date_from,
            price_basis=price_basis,
            calendar=calendar.upper(),
            include_fx_candidate=fx_candidate,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from None
    with read_connection(request) as conn:
        export = build_tdm_export(conn, options)
    body = export.to_zip()
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="tdm_{options.suffix}.zip"',
            "Cache-Control": "private, no-store",
        },
    )
