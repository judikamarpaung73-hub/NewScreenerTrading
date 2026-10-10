"""Research portfolio simulator. Signals use data through t; fills at t+1 open.
No intraday limit-fill inference, no parameter optimization, no fake win rates.
"""
from collections import Counter
import numpy as np
import pandas as pd
import strategy_params as sp
from .market_data import add_base_indicators
from .signal_engine import evaluate_swing_setup, suggest_position_size, validate_entry, slippage
from .eligibility import check_eligibility
from .execution import exit_decision


def summarize(trades, equity, initial):
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    pnl = np.array([t["pnl"] for t in closed], dtype=float)
    r = np.array([t["net_r"] for t in closed], dtype=float)
    wins = int((pnl > 0).sum())
    n = len(closed)
    # Wilson interval is descriptive only; trades may be correlated.
    if n:
        z, p = 1.96, wins/n
        center = (p+z*z/(2*n))/(1+z*z/n)
        width = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        ci = [max(0,center-width), min(1,center+width)]
    else:
        ci = None
    eq = pd.Series([initial]+[x["equity"] for x in equity], dtype=float)
    losses = -pnl[pnl < 0].sum()
    return dict(closed_trades=n, open_or_unresolved=len(trades)-n,
        win_rate=wins/n if n else None, win_rate_wilson95_descriptive=ci,
        expectancy_net_r=float(r.mean()) if n else None,
        profit_factor=float(pnl[pnl > 0].sum()/losses) if losses else None,
        net_pnl_closed=float(pnl.sum()), ending_marked_equity=float(eq.iloc[-1]),
        return_pct=float((eq.iloc[-1]/initial-1)*100),
        max_drawdown_pct=float((eq/eq.cummax()-1).min()*100),
        sample_warning="SYNTHETIC/SMALL/DEPENDENT samples do not establish an edge; inspect provenance",
        annualization=None)


def run_backtest(histories, benchmark, metadata, initial=100_000_000.,
                 start=None, end=None, setup="breakout", technical_only=False):
    if initial <= 0:
        raise ValueError("Initial equity must be positive")
    stock = {t: add_base_indicators(d) for t,d in histories.items()}
    b = add_base_indicators(benchmark)
    dates = b.index
    # No pretending split-adjusted history is historical order-price data.
    for ticker, df in histories.items():
        if "Stock Splits" in df and (df["Stock Splits"] != 0).any():
            raise ValueError(f"{ticker}: split accounting not supported; use an event-aware external backtester")
    pending, opened, trades, curve = [], {}, [], []
    rejects = Counter()
    cash = initial
    last_marks = {}
    first = pd.Timestamp(start) if start else dates[sp.MIN_HISTORY_DAYS-1]
    last = pd.Timestamp(end) if end else dates[-1]
    for i, day in enumerate(dates):
        if day < first or day > last:
            continue
        # Fill only signals from the immediately preceding BENCHMARK session.
        existing = set(opened)
        equity_open = cash+sum(p["shares"]*last_marks[t] for t,p in opened.items())
        for item in pending:
            t, r, sector = item["ticker"], item["result"], item["sector"]
            if t in opened or day not in stock[t].index:
                rejects["NO_NEXT_SESSION_BAR_OR_ALREADY_OPEN"] += 1
                continue
            bar = stock[t].loc[day]
            p = r["plan"]
            if bar.Volume <= 0 or bar.High == bar.Low:
                rejects["LOCKED_ENTRY"] += 1
                continue
            fill = float(bar.Open)+slippage(float(bar.Open), r["close"])
            ok, _ = validate_entry(p, fill)
            if not ok or not p["exec_band_low"] <= bar.Open <= p["exec_band_high"]:
                rejects["OPEN_OUTSIDE_ENTRY_BAND"] += 1
                continue
            sizing = suggest_position_size(fill-p["stop"], fill, r["avg_value_20"],
                equity_open, plan=p, portfolio=list(opened.values()), sector=sector)
            n = sizing["shares"]
            cost = n*fill*(1+sp.BUY_FEE_PCT)+sp.FIXED_COST_RESERVE_PER_TRADE/2
            if n == 0 or cost > cash:
                rejects["SIZING_OR_CASH"] += 1
                continue
            cash -= cost
            opened[t] = dict(ticker=t, signal_date=str(dates[i-1].date()), entry_date=str(day.date()),
                entry_price=fill, entry_idx=i, shares=n, lots=n//sp.LOT_SIZE,
                stop=p["stop"], target=p["target"], pivot_p=p["pivot"], sector=sector,
                max_hold_sessions=p["max_hold_sessions"], entry_cost=cost,
                planned_risk=sizing["planned_loss"], setup=r["setup"], score=r["score"],
                dividends=0., mae_pct=0., mfe_pct=0.)
            last_marks[t] = fill
        pending = []
        # Evaluate all positions, including opening entries; they were filled at open.
        for t, pos in list(opened.items()):
            if day not in stock[t].index:
                rejects["OPEN_POSITION_MISSING_BAR"] += 1
                if i-pos["entry_idx"]+1 >= pos["max_hold_sessions"]:
                    pos["pending_exit"] = "TIME_NEXT_OPEN"
                continue
            bar = stock[t].loc[day]
            last_marks[t] = float(bar.Close)
            if t in existing and "Dividends" in bar and bar.Dividends > 0:
                # Gross cash dividend credit; applicable tax is not modeled.
                credit = float(bar.Dividends)*pos["shares"]
                cash += credit
                pos["dividends"] += credit
            pos["mae_pct"] = min(pos["mae_pct"], float(bar.Low/pos["entry_price"]-1)*100)
            pos["mfe_pct"] = max(pos["mfe_pct"], float(bar.High/pos["entry_price"]-1)*100)
            decision = exit_decision(pos, bar, i-pos["entry_idx"]+1)
            if decision and decision["pending"]:
                pos["pending_exit"] = decision["reason"]
            elif decision:
                px = max(0., decision["price"]-slippage(decision["price"]))
                proceeds = px*pos["shares"]*(1-sp.SELL_FEE_PCT)-sp.FIXED_COST_RESERVE_PER_TRADE/2
                cash += proceeds
                pnl = proceeds+pos["dividends"]-pos["entry_cost"]
                trades.append(dict(pos, status="CLOSED", exit_date=str(day.date()), exit_price=px,
                    reason=decision["reason"], pnl=pnl, net_r=pnl/pos["planned_risk"],
                    holding_sessions=i-pos["entry_idx"]+1))
                del opened[t]
        eq = cash+sum(p["shares"]*last_marks[t]*(1-sp.SELL_FEE_PCT) for t,p in opened.items())
        curve.append(dict(date=str(day.date()), cash=cash, equity=eq, open_positions=len(opened)))
        candidates = []
        for t, df in stock.items():
            if t in opened:
                continue
            through = df.loc[:day]
            ok, r = evaluate_swing_setup(through, b.loc[:day], as_of=day, setup=setup)
            if not ok:
                rejects[r["reason"]] += 1
                continue
            if not r["risk_on"]:
                rejects["RISK_OFF"] += 1
                continue
            eligibility = check_eligibility(metadata, t, day)
            if not technical_only and (not eligibility["verified"] or r["flags"]):
                rejects["UNVERIFIED_ELIGIBILITY_OR_SOURCE"] += 1
                continue
            candidates.append(dict(ticker=t, result=r, sector=eligibility["sector"]))
        pending = sorted(candidates, key=lambda a: (-a["result"]["score"], a["ticker"]))
    for t, pos in opened.items():
        trades.append(dict(pos, status="OPEN_UNRESOLVED", last_mark=last_marks[t]))
    # Outcomes are added AFTER all decisions; never used by the signal engine.
    for trade in trades:
        t, start_i, entry = trade["ticker"], trade["entry_idx"], trade["entry_price"]
        for horizon in (5, 10):
            end_i = start_i+horizon-1
            key = f"forward_net_{horizon}_pct"
            trade[key] = None
            if end_i >= len(dates) or dates[end_i] > last:
                continue
            window = stock[t].reindex(dates[start_i:end_i+1])
            if window.Close.isna().any() or (window.Volume <= 0).any():
                continue
            px = float(window.Close.iloc[-1])
            net_exit = (px-slippage(px))*(1-sp.SELL_FEE_PCT)
            trade[key] = (net_exit/(entry*(1+sp.BUY_FEE_PCT))-1)*100
    summary = summarize(trades, curve, initial)
    summary["fixed_horizon_hypothetical"] = {}
    for horizon in (5, 10):
        values = [t[f"forward_net_{horizon}_pct"] for t in trades if t[f"forward_net_{horizon}_pct"] is not None]
        summary["fixed_horizon_hypothetical"][str(horizon)] = {
            "n": len(values), "positive_fraction": sum(v > 0 for v in values)/len(values) if values else None,
            "mean_net_pct": float(np.mean(values)) if values else None,
            "note": "Hypothetical full hold, ignores earlier exits; variable costs only, excludes dividends/fixed costs"}
    summary.update(technical_only=technical_only, setup=setup, rejections=dict(rejects),
        assumptions=["conditional t+1 OPEN only", "stop first if both touched",
         "one-price/zero-volume bars have no guaranteed fill", "unclosed positions marked, not deleted",
         "fixed reserve Rp20000 per round trip; datafeed excluded",
         "no order-book queue, market impact, settlement ledger, or dividend tax model",
         "MAE/MFE uses whole daily bar and can extend beyond intraday exit",
         "survivorship / price-basis validity depends on user-supplied data"],
        performance_validated=False)
    return trades, curve, summary
