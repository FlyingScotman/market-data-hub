"""Source facts for timestamp-aligned, indicative FX inputs; no rate calculation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, DecimalException, Inexact, localcontext
from typing import Any, Final

from mdhub.core.errors import FormatDriftError
from mdhub.core.iss import parse_block, to_datetime, to_decimal, to_int, to_time
from mdhub.core.timeutil import MSK
from mdhub.sources.moex_fx import (
    parse_futures_marketdata,
    parse_futures_securities,
    parse_fx_settle_dates,
    price_factor,
    snapshot_ts,
)

ASSETS: Final = {"USD": "Si", "EUR": "Eu", "CNY": "CNY"}
FUTURE_CAPTURE_WINDOW: Final = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Spot:
    currency: str
    source: str
    instrument: str
    quote_kind: str
    quote_ts: datetime | None = None
    bid: Decimal | None = None
    offer: Decimal | None = None
    last: Decimal | None = None
    update_time: time | None = None
    update_ts: datetime | None = None
    source_ts: datetime | None = None
    trade_date: date | None = None
    trade_time: time | None = None
    settle_date: date | None = None
    reasons: tuple[str, ...] = ()


def parse_cny_spot(payload: Mapping[str, Any]) -> Spot:
    """CETS CNY/RUB TOM LAST. TIME is dated only by an established matching session.

    The source's TIME, UPDATETIME and SYSTIME are kept separate. The dated LAST clock
    requires a trade in the stated session; a prior session or missing TIME is not guessed.
    """
    raw = [
        r
        for r in parse_block(payload, "marketdata", required=("SECID", "BOARDID"))
        if r["SECID"] == "CNYRUB_TOM" and r["BOARDID"] == "CETS"
    ]
    if len(raw) != 1:
        return Spot(
            "CNY",
            "moex_iss",
            "CNYRUB_TOM",
            "last",
            reasons=("spot_missing_or_duplicate", "spot_value_date_unknown"),
        )
    source_row = raw[0]
    reasons = []
    try:
        session, settle_rows = parse_fx_settle_dates(payload)
    except FormatDriftError:
        session, settle_rows = None, []
        reasons.append("spot_session_or_settlement_invalid")
    settles = [
        r["settle_date"] for r in settle_rows if r["secid"] == "CNYRUB_TOM" and r["board"] == "CETS"
    ]
    settle = settles[0] if len(settles) == 1 else None
    try:
        trade_time = to_time(source_row.get("TIME"), field="TIME")
    except FormatDriftError:
        trade_time = None
    try:
        update_time = to_time(source_row.get("UPDATETIME"), field="UPDATETIME")
    except FormatDriftError:
        update_time = None
    try:
        source_ts = to_datetime(source_row.get("SYSTIME"), field="SYSTIME")
    except FormatDriftError:
        source_ts = None
    try:
        num_trades = to_int(source_row.get("NUMTRADES"), field="NUMTRADES")
    except FormatDriftError:
        num_trades = None
    try:
        last = to_decimal(source_row.get("LAST"), field="LAST")
    except FormatDriftError:
        last = None
    if update_time is None:
        reasons.append("spot_update_clock_unknown")
    if source_ts is None:
        reasons.append("spot_source_clock_unknown")
    update_ts = (
        snapshot_ts(update_time, source_ts)
        if update_time is not None and source_ts is not None
        else None
    )
    if update_ts is not None and source_ts is not None and update_ts > source_ts:
        reasons.append("spot_clock_order_unknown")
    if (
        session is not None
        and update_ts is not None
        and session != update_ts.astimezone(MSK).date()
    ):
        reasons.append("spot_session_clock_mismatch")
    quote_ts = None
    if (
        session is not None
        and trade_time is not None
        and update_ts is not None
        and source_ts is not None
        and update_ts <= source_ts
        and session == update_ts.astimezone(MSK).date()
        and num_trades is not None
        and num_trades > 0
    ):
        candidate = datetime.combine(session, trade_time, tzinfo=MSK)
        if candidate <= update_ts and candidate <= source_ts:
            quote_ts = candidate
    if quote_ts is None:
        reasons.append("spot_time_unknown")
    if last is None or last <= 0:
        last = None
        reasons.append("spot_price_missing_or_invalid")
    if settle is None:
        reasons.append("spot_value_date_unknown")
    return Spot(
        "CNY",
        "moex_iss",
        "CNYRUB_TOM",
        "last",
        quote_ts=quote_ts,
        last=last,
        update_time=update_time,
        update_ts=update_ts,
        source_ts=source_ts,
        trade_date=session,
        trade_time=trade_time,
        settle_date=settle,
        reasons=tuple(sorted(reasons)),
    )


def _empty_node(position: int, contract: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    return {
        "position": position,
        "secid": contract.get("secid") if contract else None,
        "asset_code": contract.get("asset_code") if contract else None,
        "first_trade_date": contract.get("first_trade_date") if contract else None,
        "last_trade_date": contract.get("last_trade_date") if contract else None,
        "last_del_date": contract.get("last_del_date") if contract else None,
        "underlying_value_date": None,
        "definition_source": contract.get("spec_source") if contract else None,
        "definition_run_id": contract.get("definition_capture_run_id") if contract else None,
        "definition_capture_sha256": contract.get("definition_capture_sha256")
        if contract
        else None,
        "lot_volume": None,
        "min_step": None,
        "step_price": None,
        "bid_rub_per_unit": None,
        "offer_rub_per_unit": None,
        "update_time": None,
        "update_ts": None,
        "source_ts": None,
        "captured_at": None,
        "capture_sha256": None,
        "update_skew_seconds": None,
        "source_skew_seconds": None,
        "reasons": [reason],
    }


def _seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(
        1_000_000
    )


def _contracts(
    currency: str, spot_ts: datetime, catalog: Sequence[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], str | None]:
    asset = ASSETS[currency]
    day = spot_ts.astimezone(MSK).date()
    relevant = [
        c
        for c in catalog
        if c.get("asset_code") == asset
        and c.get("contract_type") == "dated"
        and c.get("is_fx") is True
        and c.get("base_ccy") == currency
        and c.get("quote_ccy") == "RUB"
    ]
    if any(not isinstance(c.get("last_trade_date"), date) for c in relevant):
        return [], "contract_expiry_unknown"
    selected = [
        c
        for c in relevant
        if c["last_trade_date"] >= day and c["last_trade_date"].month in (3, 6, 9, 12)
    ]
    if any(not isinstance(c.get("first_trade_date"), date) for c in selected):
        return [], "contract_start_unknown"
    selected = [c for c in selected if c["first_trade_date"] <= day]
    selected.sort(key=lambda c: (c["last_trade_date"], c["secid"]))
    if len({c["last_trade_date"] for c in selected[:4]}) != len(selected[:4]):
        return [], "duplicate_contract_expiry"
    return selected[:4], None


def match_futures(
    currency: str,
    spot_ts: datetime,
    catalog: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any] | None,
    *,
    captured_at: datetime | None,
    capture_sha256: str | None,
) -> list[dict[str, Any]]:
    """Select the first four full-size quarterlies and inspect one earlier APIM capture.

    Missing/invalid contracts stay missing; neither a fifth maturity nor another source is
    substituted. The capture clock, each row's UPDATETIME and its SYSTIME remain distinct.
    """
    contracts, error = _contracts(currency, spot_ts, catalog)
    nodes = [
        _empty_node(
            i + 1, contracts[i] if i < len(contracts) else None, error or "contract_missing"
        )
        for i in range(4)
    ]
    if error:
        return nodes
    capture_error = None
    if payload is None or captured_at is None or capture_sha256 is None:
        capture_error = "future_capture_missing"
    elif captured_at > spot_ts:
        capture_error = "future_capture_after_spot"
    elif spot_ts - captured_at > FUTURE_CAPTURE_WINDOW:
        capture_error = "future_capture_outside_window"
    if capture_error:
        for node in nodes[: len(contracts)]:
            node["reasons"] = [capture_error]
        return nodes
    assert payload is not None
    assert captured_at is not None
    assert capture_sha256 is not None
    for node in nodes[: len(contracts)]:
        node.update({"captured_at": captured_at, "capture_sha256": capture_sha256})
    try:
        specs = parse_futures_securities(payload)
        market = parse_block(payload, "marketdata", required=("SECID",))
    except FormatDriftError:
        for node in nodes[: len(contracts)]:
            node["reasons"] = ["future_capture_invalid"]
        return nodes
    for i, contract in enumerate(contracts):
        node = nodes[i]
        secid = contract["secid"]
        matching_specs = [s for s in specs if s["secid"] == secid]
        matching_rows = [r for r in market if r["SECID"] == secid]
        clock_invalid = False
        update_time = None
        source_ts = None
        if len(matching_rows) == 1:
            raw = matching_rows[0]
            try:
                update_time = to_time(raw.get("UPDATETIME"), field="UPDATETIME")
            except FormatDriftError:
                clock_invalid = True
            try:
                source_ts = to_datetime(raw.get("SYSTIME"), field="SYSTIME")
            except FormatDriftError:
                clock_invalid = True
            node.update({"update_time": update_time, "source_ts": source_ts})
            if source_ts is not None:
                node["source_skew_seconds"] = _seconds(spot_ts - source_ts)
            if update_time is not None and source_ts is not None:
                update_ts = snapshot_ts(update_time, source_ts)
                node["update_ts"] = update_ts
                if update_ts is not None:
                    node["update_skew_seconds"] = _seconds(spot_ts - update_ts)
        if len(matching_specs) != 1:
            node["reasons"] = [
                "future_contract_missing" if not matching_specs else "future_contract_duplicate"
            ]
            continue
        if len(matching_rows) != 1:
            node["reasons"] = [
                "future_quote_missing" if not matching_rows else "future_quote_duplicate"
            ]
            continue
        spec, raw = matching_specs[0], matching_rows[0]
        node.update(
            {
                "lot_volume": spec["lot_volume"],
                "min_step": spec["min_step"],
                "step_price": spec["step_price"],
                "captured_at": captured_at,
                "capture_sha256": capture_sha256,
            }
        )
        if any(
            spec[k] != contract.get(k) for k in ("asset_code", "last_trade_date", "last_del_date")
        ):
            node["reasons"] = ["future_contract_spec_mismatch"]
            continue
        if (
            spec["first_trade_date"] is None
            or spec["first_trade_date"] > spot_ts.astimezone(MSK).date()
        ):
            node["reasons"] = ["future_contract_start_unknown"]
            continue
        terms = (spec["step_price"], spec["min_step"], spec["lot_volume"])
        if any(term is None or term <= 0 for term in terms):
            node["reasons"] = ["future_contract_terms_invalid"]
            continue
        if clock_invalid:
            node["reasons"] = ["future_clock_invalid"]
            continue
        if update_time is None or source_ts is None:
            node["reasons"] = ["future_clock_unknown"]
            continue
        try:
            # Fail closed if a non-terminating factor or multiplication would round. Normal
            # FX contract terms are terminating; never silently claim rounded digits are exact.
            with localcontext() as ctx:
                ctx.traps[Inexact] = True
                factor = price_factor(*terms)
                single = {"marketdata": {"columns": list(raw), "data": [list(raw.values())]}}
                rows, _ = parse_futures_marketdata(single, {secid: factor})
        except DecimalException:
            node["reasons"] = ["future_normalization_inexact"]
            continue
        except FormatDriftError:
            node["reasons"] = ["future_quote_invalid"]
            continue
        if len(rows) != 1:
            node["reasons"] = ["future_quote_missing"]
            continue
        row = rows[0]
        update_ts, source_ts = row["ts"], row["source_ts"]
        node.update({"update_ts": update_ts, "source_ts": source_ts})
        if update_ts is None or source_ts is None:
            node["reasons"] = ["future_clock_unknown"]
            continue
        node["update_skew_seconds"] = _seconds(spot_ts - update_ts)
        node["source_skew_seconds"] = _seconds(spot_ts - source_ts)
        if update_ts > spot_ts or source_ts > spot_ts:
            node["reasons"] = ["future_clock_after_spot"]
            continue
        if (
            update_ts > source_ts
            or update_ts.astimezone(MSK).date() != source_ts.astimezone(MSK).date()
        ):
            node["reasons"] = ["future_clock_order_unknown"]
            continue
        bid, offer = row["bid_rub_per_unit"], row["offer_rub_per_unit"]
        if bid is None or offer is None or bid <= 0 or offer <= 0:
            node["reasons"] = ["future_price_missing_or_invalid"]
            continue
        if bid > offer:
            node["reasons"] = ["crossed_future"]
            continue
        node.update({"bid_rub_per_unit": bid, "offer_rub_per_unit": offer, "reasons": []})
    return nodes
