"""Flattrade scrip-master CSV samples for unit tests.

Rows marked REAL are copied from the official files inspected on 2026-10-15
(docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md, section A5).
Rows marked SYNTHETIC follow the same documented shape but are made up to cover
a rule (the findings only give counts for series BE and stock derivatives).
"""

from __future__ import annotations

HEADER = "Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype"

NSE_EQUITY_CSV = "\n".join(
    [
        HEADER,
        "NSE,2885,1,RELIANCE,RELIANCE-EQ,EQ,,0,",  # REAL
        "NSE,26000,1,NIFTY,Nifty 50,INDEX,,0,",  # REAL
        "NSE,26009,1,BANKNIFTY,Nifty Bank,INDEX",  # REAL (short row, as quoted in A5)
        "NSE,26017,1,INDIAVIX,INDIAVIX,INDEX",  # REAL (short row, as quoted in A5)
        "NSE,26037,1,FINNIFTY,Nifty Fin,INDEX",  # REAL (short row, as quoted in A5)
        "NSE,3045,1,SBIN,SBIN-EQ,EQ,,0,",  # SYNTHETIC token
        "NSE,90001,1,SOMEBE,SOMEBE-BE,BE,,0,",  # SYNTHETIC series-BE row
        "NSE,2885,1,RELIANCE,RELIANCE-EQ,EQ,,0,",  # duplicate row: must stay idempotent
    ]
)

NFO_INDEX_CSV = "\n".join(
    [
        HEADER,
        "NFO,62354,65,NIFTY,NIFTY29DEC26P21050,OPTIDX,29-DEC-2026,21050.00,PE",  # REAL
        "NFO,48704,65,NIFTY,NIFTY27OCT26F,FUTIDX,27-OCT-2026,-0.01,XX",  # REAL
        "NFO,62355,65,NIFTY,NIFTY29DEC26C21050,OPTIDX,29-DEC-2026,21050.00,CE",  # SYNTHETIC token
    ]
)

NFO_EQUITY_CSV = "\n".join(
    [
        HEADER,
        "NFO,70001,500,RELIANCE,RELIANCE27OCT26C1400,OPTSTK,27-OCT-2026,1400.00,CE",  # SYNTHETIC
    ]
)

# File name -> CSV text, as served under the scripmaster/ URL prefix.
FILES: dict[str, str] = {
    "NSE_Equity.csv": NSE_EQUITY_CSV,
    "Nfo_Index_Derivatives.csv": NFO_INDEX_CSV,
    "Nfo_Equity_Derivatives.csv": NFO_EQUITY_CSV,
}
