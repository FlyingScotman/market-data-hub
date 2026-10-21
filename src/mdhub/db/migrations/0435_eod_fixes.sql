-- EOD fixes of 2026-10-21 (hub review for the desk monitor §6.4): the official close of the
-- session (ISS marketdata LCLOSEPRICE) is stored with the intraday bond quotes, so the
-- eod_preliminary cut can publish the session close while the ISS history of the day is not
-- published yet (bond_quotes coverage basis = intraday_close).

ALTER TABLE md.bond_quote_intraday ADD COLUMN IF NOT EXISTS legal_close numeric;
ALTER TABLE md.bond_quote_last ADD COLUMN IF NOT EXISTS legal_close numeric;

COMMENT ON COLUMN md.bond_quote_intraday.legal_close IS
    'ISS marketdata LCLOSEPRICE: official close price of the session, % of face (NULL until the close)';
COMMENT ON COLUMN md.bond_quote_last.legal_close IS
    'ISS marketdata LCLOSEPRICE (see md.bond_quote_intraday.legal_close)';
