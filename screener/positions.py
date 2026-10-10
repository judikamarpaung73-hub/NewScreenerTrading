"""Atomic JSON position journal. No orders, no automatic confirmation of fills.
Use Linux/macOS for the shared scanner+bot process lock (fcntl).
Windows is supported for a single writer only; simultaneous writers are refused.
"""
import contextlib
import json
import os
import time

import strategy_params as sp
from .utils import logger

try:
    import fcntl  # hanya ada di Linux/macOS
except ImportError:  # pragma: no cover  (Windows)
    fcntl = None

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_PATH = os.path.join(DATA_DIR, "positions.json")


class PositionStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self.lock_path = path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._data = self._load()

    # ---------------- infrastruktur: muat / simpan / transaksi ----------------

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("open", [])
                data.setdefault("closed", [])
                data.setdefault("pending", [])
                return data
            except Exception as e:
                raise RuntimeError("Position file corrupt; restore backup before scanning") from e
        return {"open": [], "closed": [], "pending": []}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _txn(self):
        """Transaksi baca-ubah-tulis lintas proses: kunci -> muat ulang data
        terbaru dari disk -> (pemanggil mengubah self._data) -> simpan atomik."""
        lock_f = None
        try:
            if fcntl is not None:
                lock_f = open(self.lock_path, "w")
                fcntl.flock(lock_f, fcntl.LOCK_EX)
            if fcntl is None:
                # Exclusive create also provides cross-process protection on Windows.
                try:
                    lock_f = os.open(self.lock_path + ".exclusive", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError as e:
                    raise RuntimeError("Position store busy; retry after other writer finishes") from e
            self._data = self._load()
            yield
            self._save()
        finally:
            if lock_f is not None and fcntl is not None:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
                finally:
                    lock_f.close()
            elif lock_f is not None:
                os.close(lock_f)
                os.unlink(self.lock_path + ".exclusive")

    # ---------------- sinyal tertunda (hasil scan, belum dikonfirmasi) ----------------

    def save_pending_signal(self, ticker, signal_date, plan):
        ticker = ticker.upper().removesuffix(".JK")
        with self._txn():
            self._data["pending"] = [p for p in self._data["pending"] if p["ticker"] != ticker]
            self._data["pending"].append(dict(ticker=ticker, signal_date=str(signal_date),
                                               pivot_p=plan["pivot"], **plan))

    def get_pending_signal(self, ticker):
        self._data = self._load()
        return next((dict(p) for p in self._data["pending"] if p["ticker"] == ticker.upper().removesuffix(".JK")), None)

    def pop_pending_signal(self, ticker):
        ticker = ticker.upper().removesuffix(".JK")
        with self._txn():
            for p in self._data["pending"]:
                if p["ticker"] == ticker:
                    self._data["pending"].remove(p)
                    return p
        return None

    def open_positions(self):
        with self._txn():
            return [dict(p) for p in self._data["open"]]

    def has_open_position(self, ticker):
        return any(p["ticker"] == ticker.upper().removesuffix(".JK") for p in self.open_positions())

    def add_position(self, ticker, entry_date, entry_price, pivot_p, stop, target, lots, shares,
                     sector="UNKNOWN", max_hold_sessions=None, warnings=None):
        import math
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in [entry_price,lots,shares]):
            raise ValueError("Harga, lot dan lembar wajib positif")
        if not stop or not target or not 0 < stop < entry_price < target:
            raise ValueError("Wajib 0 < stop < entry < target")
        ticker = ticker.upper().removesuffix(".JK")
        pos = dict(ticker=ticker, entry_date=str(entry_date), entry_price=entry_price,
            pivot_p=pivot_p, stop=stop, target=target, lots=lots, shares=shares,
            sector=sector, session_count=1, last_checked_date=None, status="open",
            max_hold_sessions=max_hold_sessions or sp.MAX_HOLD_SESSIONS, warnings=warnings or [])
        with self._txn():
            if any(p["ticker"] == ticker for p in self._data["open"]):
                raise ValueError("Ticker already open")
            self._data["open"].append(pos)
            self._data["pending"] = [p for p in self._data["pending"] if p["ticker"] != ticker]
        return pos

    def close_position(self, ticker, exit_date, exit_price, reason):
        import math
        if not math.isfinite(exit_price) or exit_price <= 0:
            raise ValueError("Harga keluar harus positif dan finite")
        ticker = ticker.upper().removesuffix(".JK")
        with self._txn():
            for pos in list(self._data["open"]):
                if pos["ticker"] == ticker:
                    pos.update(status="closed", exit_date=str(exit_date), exit_price=exit_price, exit_reason=reason)
                    self._data["open"].remove(pos)
                    self._data["closed"].append(pos)
                    return pos
        return None

    def check_exits(self, ticker, bar_date, today_open, today_high, today_low, today_close,
                    today_volume=1., session_count=None):
        from .execution import exit_decision
        ticker, bar_date = ticker.upper().removesuffix(".JK"), str(bar_date)
        with self._txn():
            for pos in self._data["open"]:
                if pos["ticker"] != ticker:
                    continue
                last = pos.get("last_checked_date")
                if bar_date < pos["entry_date"] or (last and bar_date <= last):
                    return None
                n = session_count or (1 if bar_date == pos["entry_date"] else pos.get("session_count",1)+1)
                pos.update(session_count=n,last_checked_date=bar_date)
                if not pos.get("stop") or not pos.get("target") or not pos.get("pivot_p"):
                    return {"action":"LEGACY_POSITION_NEEDS_PLAN","position":dict(pos)}
                bar = dict(Open=today_open,High=today_high,Low=today_low,Close=today_close,Volume=today_volume)
                # Manual fills may have happened intraday: no claim about entry-day touches.
                if bar_date == pos["entry_date"]:
                    decision = {"reason":"INVALIDATION_NEXT_OPEN","pending":True} if today_close < pos["pivot_p"] else None
                else:
                    decision = exit_decision(pos,bar,n)
                if decision:
                    pos["pending_exit"] = decision["reason"]
                    return {"action":decision["reason"],"exit_price_plan":decision.get("price"),"position":dict(pos)}
        return None
