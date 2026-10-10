"""Point-in-time, manually verified instrument/event metadata. Unknown != safe."""
from datetime import timedelta
from pathlib import Path
import pandas as pd
import strategy_params as sp

COLUMNS = ["AsOf","Ticker","RegularMarket","Suspended","FCA","UMA","EventRisk",
           "EventsReviewedThrough","Sector","Source"]


def load_metadata(path):
    if not path or not Path(path).exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str).fillna("")
    if any(c not in df for c in COLUMNS):
        raise ValueError("Metadata must contain: "+",".join(COLUMNS))
    for c in ["AsOf", "EventsReviewedThrough"]:
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d", errors="raise")
    df["Ticker"] = df.Ticker.str.upper().str.removesuffix(".JK")
    if df.duplicated(["AsOf", "Ticker"]).any():
        raise ValueError("Duplicate metadata snapshot")
    for c in ["RegularMarket","Suspended","FCA","UMA","EventRisk"]:
        if not df[c].str.lower().isin(["true","false"]).all():
            raise ValueError(f"{c} must be true/false; unknown rows must not be marked safe")
        df[c] = df[c].str.lower().eq("true")
    return df


def check_eligibility(df, ticker, signal_date):
    day = pd.Timestamp(signal_date).normalize()
    rows = df[(df.Ticker == ticker.removesuffix(".JK")) & (df.AsOf == day)]
    if rows.empty:
        return {"verified": False, "sector": "UNKNOWN", "flags": ["STATUS_OR_EVENTS_NOT_VERIFIED"]}
    row = rows.iloc[-1]
    flags = []
    for col in ["Suspended","FCA","UMA","EventRisk"]:
        if bool(row[col]):
            flags.append(col.upper())
    if not bool(row.RegularMarket):
        flags.append("NON_REGULAR_MARKET")
    if row.EventsReviewedThrough < day+timedelta(days=sp.EVENT_REVIEW_CALENDAR_DAYS):
        flags.append("EVENT_REVIEW_WINDOW_TOO_SHORT")
    if not row.Source.strip() or not row.Sector.strip():
        flags.append("MISSING_SOURCE_OR_SECTOR")
    return {"verified": not flags, "sector": row.Sector or "UNKNOWN", "flags": flags}
