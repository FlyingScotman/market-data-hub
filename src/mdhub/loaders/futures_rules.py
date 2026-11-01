"""Explicit historical tick-term repair from dated, hash-pinned MOEX specifications.

ISS descriptions retain expired contract dates but omit MINSTEP/STEPPRICE. This manual job
fills those fields from the actual specification in force at expiry, captures the original
DOCX and marks the row ``moex_rules``. It never infers terms from a live successor contract,
changes listing dates or overwrites an existing conflicting value. Unknown editions remain
missing and make the run partial. No schedule is installed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from mdhub.core.capture import record_capture
from mdhub.core.iss import IssFormatError
from mdhub.core.upsert import UpsertResult
from mdhub.jobs import JobContext, job


@dataclass(frozen=True, slots=True)
class Rule:
    start: date
    end: date
    document: str
    sha256: str
    asset: str = "RGBI"
    min_step: Decimal = Decimal("1")
    step_price: Decimal = Decimal("1")

    @property
    def url(self) -> str:
        return "https://www.moex.com/files/" + self.document


# Edition dates: https://www.moex.com/ru/documents/
# fyuchersnye-kontrakty-na-indeksy-dolgovogo-i-denezhnogo-rynka
# First three: clauses on minimum price increment and its RUB value.
# 2026-05-19 edition: separate RGBI/RUONIA parameter tables. Values are in the
# contract's quoted points and RUB per tick, not index points or RUB per index unit.
RULES: Final = (
    Rule(
        date(2022, 2, 28),
        date(2025, 4, 27),
        "47vqbfk5hhpn4tnarp3mp3xcce",
        "07de33a45a2fc65278202e27eb5acd74f0a361b4b5e44bf28653fe9c9ea08655",
    ),
    Rule(
        date(2025, 4, 28),
        date(2026, 3, 22),
        "4ze7kndca6j7cyddzh9q7sb04e",
        "afe2c093af26f2b8ed560678ae304be9776c7e96221b8e2b17711e92466b26d8",
    ),
    Rule(
        date(2026, 3, 23),
        date(2026, 5, 18),
        "4m4ky668j6skm1z58xttwyg8c5",
        "48e16e90700dcfb81d0b1961cadf7cdb66d475d91e2c4c45ee62b9c67a8112a5",
    ),
    Rule(
        date(2026, 5, 19),
        date(2026, 9, 13),
        "4ezh2awnts7t4tyf62nntw25d6",
        "78335acee52f162d851036965d2182cac5445ffb98f4958e3d732d2ccf65ab55",
    ),
    Rule(
        date(2026, 5, 19),
        date(2026, 9, 13),
        "4ezh2awnts7t4tyf62nntw25d6",
        "78335acee52f162d851036965d2182cac5445ffb98f4958e3d732d2ccf65ab55",
        asset="RUONIA",
        min_step=Decimal("0.0001"),
    ),
)


@job("futures_contract_rules", "Repair expired rates-futures tick terms from verified MOEX rules")
def futures_contract_rules(ctx: JobContext) -> None:
    with ctx.db.connection() as conn, ctx.http() as http:
        rows = conn.execute(
            """
            SELECT secid, asset_code, last_trade_date, min_step, step_price
            FROM ref.futures_contract
            WHERE asset_code IN ('RGBI', 'RUONIA') AND contract_type = 'dated'
              AND last_trade_date < %s AND last_seen IS NULL
              AND spec_source IN ('history', 'iss_description')
              AND (min_step IS NULL OR step_price IS NULL)
            ORDER BY secid FOR UPDATE
            """,
            (ctx.today(),),
        ).fetchall()
        captures: dict[str, int] = {}
        for row in rows:
            rules = [
                r
                for r in RULES
                if r.asset == row["asset_code"] and r.start <= row["last_trade_date"] <= r.end
            ]
            if not rules:
                ctx.run.mark_partial(f"no verified specification edition for {row['secid']}")
                continue
            if len(rules) != 1:
                raise IssFormatError(f"overlapping specification editions for {row['secid']}")
            rule = rules[0]
            for field in ("min_step", "step_price"):
                if row[field] is not None and row[field] != getattr(rule, field):
                    raise IssFormatError(
                        f"MOEX rules conflict with stored {field} for {row['secid']}"
                    )
            if rule.url not in captures:
                response = http.get(rule.url, headers={"User-Agent": "Mozilla/5.0"})
                if hashlib.sha256(response.content).hexdigest() != rule.sha256:
                    raise IssFormatError(f"MOEX specification document changed: {rule.document}")
                captures[rule.url] = record_capture(
                    conn,
                    ctx.run.id,
                    "moex_rules",
                    "futures_contract_rules",
                    rule.url,
                    response,
                    "1",
                )
            cur = conn.execute(
                """
                UPDATE ref.futures_contract
                SET min_step = %s, step_price = %s, spec_source = 'moex_rules',
                    capture_id = %s, run_id = %s, updated_at = now()
                WHERE secid = %s
                """,
                (rule.min_step, rule.step_price, captures[rule.url], ctx.run.id, row["secid"]),
            )
            ctx.run.add_counts(UpsertResult(updated=cur.rowcount))
