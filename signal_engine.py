"""Auditable hypotheses for 5-session swings; no calibrated win probability."""
import math
import numpy as np
import pandas as pd
import strategy_params as sp
from .market_data import market_regime_ok
from .utils import idx_tick, round_down_idx, round_up_idx


def slippage(price, reference=None):
    return max(price*sp.SLIPPAGE_BPS/10_000,
               sp.SLIPPAGE_TICKS*idx_tick(price if reference is None else reference))


def net_metrics(entry, stop, target):
    buy = entry*(1+sp.BUY_FEE_PCT) + slippage(entry)
    stop_net = max(0, stop-slippage(stop))*(1-sp.SELL_FEE_PCT)
    target_net = max(0, target-slippage(target))*(1-sp.SELL_FEE_PCT)
    risk = buy-stop_net+sp.EXTRA_SLIPPAGE_PER_SHARE
    reward = target_net-buy-sp.EXTRA_SLIPPAGE_PER_SHARE
    return {"net_risk_per_share": risk, "net_reward_per_share": reward,
            "net_rr": reward/risk if risk > 0 else 0}


def _find_swing_highs(df, lookback):
    h = df.High.to_numpy()
    return [float(h[i]) for i in range(max(2, len(h)-lookback), len(h)-2)
            if h[i] > max(h[i-2], h[i-1], h[i+1], h[i+2])]


def is_breakout(b):
    return bool(b.Close > b.PIVOT20 and b.Close-b.PIVOT20 <= b.ATR14
                and b.RVOL >= sp.BREAKOUT_MIN_RVOL and b.CLV >= sp.BREAKOUT_MIN_CLV
                and b.Close > b.Open)


def find_setup(df, setup="breakout"):
    b = df.iloc[-1]
    if setup in ("breakout", "both") and is_breakout(b):
        # Do not publish a fresh breakout repeatedly during the same extension.
        if not any(is_breakout(df.iloc[-k]) for k in (2, 3)):
            return "BREAKOUT", float(b.PIVOT20), float(min(b.Low, b.PIVOT20))
    if setup in ("retest", "both"):
        for age in range(2, sp.RETEST_MAX_AGE+1):
            anchor = df.iloc[-age-1]
            if not is_breakout(anchor):
                continue
            p, a = float(anchor.PIVOT20), float(b.ATR14)
            pull = df.iloc[-age:-1]
            touched = df.Low.iloc[-age:].min() <= p+sp.RETEST_TOLERANCE_ATR*a
            held = df.Close.iloc[-age:].min() >= p-sp.RETEST_TOLERANCE_ATR*a
            dry = pull.Volume.mean() <= 0.85*anchor.Volume
            trigger = (b.Close > max(p, df.High.iloc[-2]) and b.Close > b.Open
                       and b.CLV >= .65 and b.RVOL >= sp.RETEST_MIN_RVOL
                       and b.Volume >= 1.1*df.Volume.iloc[-2]
                       and b.Close-p <= a)
            if touched and held and dry and trigger:
                return "RETEST", p, float(min(p, df.Low.iloc[-age:].min()))
    return None


def build_trade_plan(df, pivot_p, as_of=None, support=None, setup="BREAKOUT"):
    b = df.iloc[-1]
    c, atr = float(b.Close), float(b.ATR14)
    if not math.isfinite(atr) or atr <= 0:
        return None
    # Entry is conditional at t+1 open only. Size at the worst permitted fill.
    low = round_up_idx(max(pivot_p+idx_tick(c), c-.5*atr), c)
    high = round_down_idx(min(pivot_p+atr, c+sp.MAX_ENTRY_GAP_ATR*atr), c)
    if high < low:
        return None
    entry = high
    structural = (min(float(b.Low), pivot_p) if support is None else support)-sp.STOP_ATR_MULT*atr
    stop = round_down_idx(min(structural, entry-sp.MIN_STOP_ATR*atr), c)
    risk = entry-stop
    if stop <= 0 or risk <= 0 or risk/entry*100 > sp.MAX_STOP_WIDTH_PCT or risk > sp.MAX_STOP_ATR*atr:
        return None
    caps = [entry+sp.TARGET_R_MULTIPLE*risk, entry+sp.MAX_TARGET_ATR*atr]
    overhead = [h for h in _find_swing_highs(df, sp.SWING_HIGH_LOOKBACK) if h > entry]
    resistance = min(overhead) if overhead else None
    if resistance is not None:
        caps.append(resistance-idx_tick(c))
    target = round_down_idx(min(caps), c)
    m = net_metrics(entry, stop, target)
    if m["net_rr"] < sp.MIN_RR_AFTER_COST:
        return None
    return dict(entry_ref=entry, pivot=pivot_p, exec_band_low=low,
        exec_band_high=high, stop=stop, target=target, risk_per_share=risk,
        stop_pct=risk/entry*100, target_pct=(target-entry)/entry*100,
        atr=atr, resistance=resistance, setup=setup, entry_model="NEXT_OPEN_ONLY",
        max_hold_sessions=sp.MAX_HOLD_SESSIONS, **m)


def validate_entry(plan, entry):
    if not math.isfinite(entry) or not plan["exec_band_low"] <= entry <= plan["exec_band_high"]:
        return False, "Harga di luar band; jangan mengejar gap atau menunggu retrace intraday"
    m = net_metrics(entry, plan["stop"], plan["target"])
    if not plan["stop"] < entry < plan["target"] or m["net_rr"] < sp.MIN_RR_AFTER_COST:
        return False, "RR/stop tidak lagi valid pada harga aktual"
    return True, m


def suggest_position_size(risk_per_share, entry_ref, avg_value_20, account_equity,
                          *, plan=None, portfolio=None, sector="UNKNOWN"):
    """Includes fees/slippage, fixed-cost reserve, portfolio heat/cash/sector caps."""
    zero = dict(lots=0, shares=0, capital=0., risk_budget=0., planned_loss=0.)
    vals = [risk_per_share, entry_ref, avg_value_20, account_equity]
    if any(not math.isfinite(float(x)) or float(x) <= 0 for x in vals):
        return dict(zero, binding_constraint="Data sizing tidak valid")
    portfolio = portfolio or []
    if len(portfolio) >= sp.MAX_OPEN_POSITIONS:
        return dict(zero, binding_constraint="Batas jumlah posisi")
    used_capital = used_risk = sector_capital = 0.
    for pos in portfolio:
        if not pos.get("shares") or not pos.get("stop") or not pos.get("entry_price"):
            return dict(zero, binding_constraint="Posisi lama tanpa lot/stop; lengkapi sebelum sizing")
        e, s, n = pos["entry_price"], pos["stop"], pos["shares"]
        cap = e*n*(1+sp.BUY_FEE_PCT)
        risk = max(0, e*(1+sp.BUY_FEE_PCT)-(s-slippage(s))*(1-sp.SELL_FEE_PCT))*n
        used_capital += cap
        used_risk += risk+sp.FIXED_COST_RESERVE_PER_TRADE
        if pos.get("sector", "UNKNOWN") in (sector, "UNKNOWN") or sector == "UNKNOWN":
            sector_capital += cap
    budget = min(account_equity*sp.RISK_PER_TRADE_PCT/100,
                 account_equity*sp.MAX_PORTFOLIO_RISK_PCT/100-used_risk)
    net_risk = net_metrics(entry_ref, plan["stop"], plan["target"])["net_risk_per_share"] if plan else risk_per_share
    entry_cost = entry_ref*(1+sp.BUY_FEE_PCT)+slippage(entry_ref)
    fixed = sp.FIXED_COST_RESERVE_PER_TRADE
    constraints = {
        "risiko bersih": (budget-fixed)/net_risk,
        "modal per posisi": account_equity*sp.MAX_POSITION_PCT/100/entry_cost,
        "likuiditas": avg_value_20*sp.MAX_POV_RATE/entry_cost,
        "eksposur total": (account_equity*sp.MAX_GROSS_EXPOSURE_PCT/100-used_capital-fixed)/entry_cost,
        "konsentrasi sektor": (account_equity*sp.MAX_SECTOR_EXPOSURE_PCT/100-sector_capital-fixed)/entry_cost,
    }
    binding = min(constraints, key=constraints.get)
    lots = max(0, int(constraints[binding]//sp.LOT_SIZE))
    shares = lots*sp.LOT_SIZE
    if not shares:
        return dict(zero, binding_constraint=binding)
    m = net_metrics(entry_ref, plan["stop"], plan["target"]) if plan else None
    loss = shares*net_risk+fixed
    rr = (shares*m["net_reward_per_share"]-fixed)/loss if m else None
    if rr is not None and rr < sp.MIN_RR_AFTER_COST:
        return dict(zero, binding_constraint="RR setelah cadangan biaya tetap terlalu rendah")
    return dict(lots=lots, shares=shares, capital=shares*entry_cost+fixed,
                risk_budget=budget, planned_loss=loss, net_rr_total=rr,
                binding_constraint=binding)


def evaluate_swing_setup(df, benchmark_df, as_of=None, setup=None):
    def reject(reason):
        return False, {"reason": reason}
    if df is None or benchmark_df is None or min(len(df), len(benchmark_df)) < sp.MIN_HISTORY_DAYS:
        return reject("HISTORY_TOO_SHORT")
    if df.index[-1] != benchmark_df.index[-1]:
        return reject("SESSION_MISMATCH")
    if as_of is not None and df.index[-1].date() != pd.Timestamp(as_of).date():
        return reject("STALE_OR_FUTURE_BAR")
    # Same observations for relative returns, including missing/suspended sessions.
    if not df.index[-61:].equals(benchmark_df.index[-61:]):
        return reject("MISSING_SESSIONS")
    b, bench = df.iloc[-1], benchmark_df.iloc[-1]
    cols = ["Close","ATR14","SMA20","SMA50","SMA50_SLOPE","RVOL","CLV","RET20","RET5","AVG_VALUE20","MEDIAN_VALUE20"]
    if not np.isfinite(b[cols].to_numpy(dtype=float)).all() or not np.isfinite(bench[["RET20","RET5"]].to_numpy(dtype=float)).all():
        return reject("NONFINITE_INDICATOR")
    if b.Close < sp.MIN_PRICE:
        return reject("PRICE_FLOOR")
    if b.AVG_VALUE20 < sp.MIN_AVG_VALUE_20 or b.MEDIAN_VALUE20 < sp.MIN_MEDIAN_VALUE_20:
        return reject("LIQUIDITY")
    if (df.Volume.iloc[-21:] <= 0).any() or b.High == b.Low:
        return reject("NOT_CONTINUOUSLY_TRADED")
    if not (b.Close > b.SMA20 > b.SMA50 and b.SMA50_SLOPE > 0):
        return reject("TREND")
    if b.RET20 <= bench.RET20:
        return reject("RELATIVE_STRENGTH")
    if not sp.MIN_ATR_PCT <= b.ATR_PCT <= sp.MAX_ATR_PCT:
        return reject("VOLATILITY")
    if (b.Close-b.SMA20)/b.ATR14 > sp.MAX_EXTENSION_ATR:
        return reject("OVEREXTENDED")
    if b.TR > sp.MAX_SIGNAL_TR_ATR*df.ATR14.iloc[-2]:
        return reject("EXHAUSTION_RANGE")
    found = find_setup(df, setup or ("both" if sp.ENABLE_RETEST else "breakout"))
    if found is None:
        return reject("NO_TRIGGER")
    kind, pivot, support = found
    plan = build_trade_plan(df, pivot, as_of, support, kind)
    if plan is None:
        return reject("STOP_TARGET_OR_NET_RR")
    risk_on, note = market_regime_ok(benchmark_df)
    flags = [] if risk_on else ["RISK_OFF"]
    if "RegularValue" not in df:
        flags.append("VALUE_PROXY_VERIFY_REGULAR_MARKET")
    for col in ("Dividends", "Stock Splits"):
        if col in df and (df[col].iloc[-20:] > 0).any():
            flags.append("RECENT_CORPORATE_ACTION_REVIEW")
    if "Repaired?" in df and df["Repaired?"].iloc[-20:].astype(str).str.lower().isin(["true","1"]).any():
        flags.append("REPAIRED_DATA_REVIEW")
    rs20, rs5 = float((b.RET20-bench.RET20)*100), float((b.RET5-bench.RET5)*100)
    # Ordinal heuristic ONLY; not a percent success rate.
    score = (30*min(max(rs20, 0)/10, 1) + 15*min(max(rs5, 0)/5, 1)
             + 20*min(max((b.RVOL-1)/2, 0), 1) + 15*b.CLV
             + 10*min(b.MEDIAN_VALUE20/20e9, 1) + 10*min(plan["net_rr"]/3, 1))
    return True, dict(close=float(b.Close), setup=kind, signal_date=str(df.index[-1].date()),
        avg_value_20=float(b.AVG_VALUE20), median_value_20=float(b.MEDIAN_VALUE20),
        value_source=df.attrs.get("value_source", "unknown"), rvol=float(b.RVOL), clv=float(b.CLV),
        stock_ret20_pct=float(b.RET20*100), bench_ret20_pct=float(bench.RET20*100),
        rs20_pct=rs20, rs5_pct=rs5, atr_pct=float(b.ATR_PCT),
        score=round(float(score), 1), market_note=note, risk_on=risk_on,
        flags=flags, plan=plan)
