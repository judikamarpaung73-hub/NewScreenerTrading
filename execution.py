"""Shared exit state machine for historical simulation and EOD position alerts."""
import strategy_params as sp


def exit_decision(pos, bar, session_count):
    """Daily OHLC cannot reveal the order of touches: assume stop first.
    Zero-volume/single-price sessions do not guarantee a fill. A queued exit
    remains pending until a later tradable session. No automatic live orders.
    """
    o, h, low, c, v = [float(bar[k]) for k in ["Open","High","Low","Close","Volume"]]
    locked = v <= 0 or h == low
    if locked:
        if low <= pos["stop"]:
            return {"reason": "STOP_WAIT_LIQUIDITY", "pending": True}
        if pos.get("pending_exit"):
            return {"reason": pos["pending_exit"], "pending": True}
        if session_count >= pos.get("max_hold_sessions", sp.MAX_HOLD_SESSIONS):
            return {"reason": "TIME_NEXT_OPEN", "pending": True}
        return None
    if o <= pos["stop"]:
        return {"reason": "STOP_GAP", "price": o, "pending": False}
    if pos.get("pending_exit"):
        return {"reason": pos["pending_exit"], "price": o, "pending": False}
    if o >= pos["target"]:
        return {"reason": "TARGET_GAP", "price": pos["target"], "pending": False}
    if low <= pos["stop"]:
        return {"reason": "STOP", "price": pos["stop"], "pending": False}
    if h >= pos["target"]:
        return {"reason": "TARGET", "price": pos["target"], "pending": False}
    if session_count <= sp.EARLY_INVALIDATION_WINDOW and c < pos["pivot_p"]:
        return {"reason": "INVALIDATION_NEXT_OPEN", "pending": True}
    risk = pos["entry_price"]-pos["stop"]
    if session_count == sp.PROGRESS_CHECK_SESSION and c-pos["entry_price"] < sp.MIN_PROGRESS_R*risk:
        return {"reason": "NO_PROGRESS_NEXT_OPEN", "pending": True}
    if session_count >= pos.get("max_hold_sessions", sp.MAX_HOLD_SESSIONS):
        return {"reason": "TIME_NEXT_OPEN", "pending": True}
    return None
