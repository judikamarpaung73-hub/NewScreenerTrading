"""Strict daily data validation. Yahoo is a research source, not an IDX feed."""
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import strategy_params as sp
from .utils import logger

WIB = ZoneInfo("Asia/Jakarta")


class DataQualityError(ValueError):
    pass


def jakarta_now():
    return datetime.now(WIB)


def normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        raise DataQualityError("Use one ticker per file, not MultiIndex columns")
    if "Date" in out:
        out.index = pd.to_datetime(out.pop("Date"), errors="raise")
    if not isinstance(out.index, pd.DatetimeIndex):
        raise DataQualityError("DatetimeIndex or Date column required")
    if out.index.tz is not None:
        out.index = out.index.tz_convert(WIB).tz_localize(None)
    out.index = out.index.normalize()
    if out.index.hasnans or out.index.has_duplicates:
        raise DataQualityError("Missing/duplicate session dates")
    out = out.sort_index()
    needed = ["Open", "High", "Low", "Close", "Volume"]
    if any(c not in out for c in needed):
        raise DataQualityError("Required: Date,Open,High,Low,Close,Volume")
    out[needed] = out[needed].apply(pd.to_numeric, errors="coerce")
    a = out[needed].to_numpy(dtype=float)
    valid = (np.isfinite(a).all(axis=1) & (a[:, :4] > 0).all(axis=1)
             & (a[:, 4] >= 0) & (a[:, 1] >= a[:, [0, 2, 3]].max(axis=1))
             & (a[:, 2] <= a[:, [0, 1, 3]].min(axis=1)))
    if not valid.all():
        bad = out.index[~valid][:3].strftime("%Y-%m-%d").tolist()
        raise DataQualityError(f"Invalid OHLCV at {bad}; do not silently drop rows")
    for c in ["RegularValue", "Dividends", "Stock Splits"]:
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce")
            if not np.isfinite(out[c]).all() or (out[c] < 0).any():
                raise DataQualityError(f"Invalid {c}")
    out.index.name = "Date"
    return out


def completed_bars(df, now=None):
    now = now or jakarta_now()
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(WIB)
    day = pd.Timestamp(now.date())
    ready = (now.hour, now.minute) >= (sp.EOD_READY_HOUR_WIB, sp.EOD_READY_MINUTE_WIB)
    return df.loc[df.index <= day] if ready else df.loc[df.index < day]


def fetch_history_with_retry(ticker_obj, period="2y", retries=None, delay=None):
    retries = sp.HISTORY_FETCH_RETRIES if retries is None else retries
    delay = sp.HISTORY_FETCH_RETRY_DELAY if delay is None else delay
    for attempt in range(retries):
        try:
            if isinstance(ticker_obj, str):
                import yfinance as yf
                obj = yf.Ticker(ticker_obj)
            else:
                obj = ticker_obj
            df = normalize_ohlcv(obj.history(period=period, interval="1d",
                auto_adjust=False, actions=True, repair=True, timeout=15))
            if not df.empty:
                df.attrs["source"] = "Yahoo/yfinance; split-adjusted; value proxy"
                return completed_bars(df)
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt or use --data-dir CSV") from exc
        except Exception as exc:
            logger.warning("History attempt %s/%s failed: %s", attempt+1, retries, type(exc).__name__)
        if attempt+1 < retries:
            time.sleep(delay)
    return pd.DataFrame()


def load_history(ticker, data_dir=None):
    if data_dir:
        name = "IHSG" if ticker == sp.BENCHMARK else ticker.removesuffix(".JK")
        df = normalize_ohlcv(pd.read_csv(Path(data_dir) / f"{name}.csv"))
        df.attrs["source"] = "user CSV; provenance must be checked"
        return df
    return fetch_history_with_retry(ticker)


def add_base_indicators(df):
    df = df.copy()
    c, h, low, v = (df[k] for k in ("Close", "High", "Low", "Volume"))
    df["SMA20"] = c.rolling(sp.STOCK_SMA_FAST).mean()
    df["SMA50"] = c.rolling(sp.STOCK_SMA_SLOW).mean()
    df["SMA200"] = c.rolling(200).mean()  # informational only
    tr = pd.concat([h-low, (h-c.shift()).abs(), (low-c.shift()).abs()], axis=1).max(axis=1)
    atr = pd.Series(np.nan, index=df.index)
    if len(df) >= 14:
        seed = tr.iloc[13:].copy()
        seed.iloc[0] = tr.iloc[:14].mean()
        atr.iloc[13:] = seed.ewm(alpha=1/14, adjust=False).mean().to_numpy()
    df["TR"], df["ATR14"] = tr, atr
    df["ATR_PCT"] = atr/c*100
    df["RVOL"] = v/v.rolling(sp.BREAKOUT_LOOKBACK).mean().shift(1).replace(0, np.nan)
    df["Value"] = df["RegularValue"] if "RegularValue" in df else c*v
    df["AVG_VALUE20"] = df["Value"].rolling(20).mean().shift(1)
    df["MEDIAN_VALUE20"] = df["Value"].rolling(20).median().shift(1)
    df["CLV"] = (c-low)/(h-low).replace(0, np.nan)
    df["RET20"], df["RET5"] = c.pct_change(20), c.pct_change(5)
    df["SMA50_SLOPE"] = df["SMA50"]-df["SMA50"].shift(sp.TREND_SLOPE_LOOKBACK)
    df["PIVOT20"] = h.rolling(sp.BREAKOUT_LOOKBACK).max().shift(1)
    df.attrs["value_source"] = "RegularValue (user supplied)" if "RegularValue" in df else "Close x Volume proxy"
    return df


def get_benchmark(data_dir=None):
    df = load_history(sp.BENCHMARK, data_dir)
    return add_base_indicators(df) if len(df) >= sp.MIN_HISTORY_DAYS else None


def market_regime_ok(df):
    if df is None or len(df) < sp.MIN_HISTORY_DAYS:
        return False, "UNKNOWN: benchmark unavailable"
    b = df.iloc[-1]
    ok = bool(b.Close > b.SMA50 and b.SMA50_SLOPE > 0)
    return ok, ("RISK-ON: IHSG > SMA50 dan SMA50 naik" if ok else
                "RISK-OFF: kandidat hanya WATCHLIST")
