#!/usr/bin/env python3
"""
run_scanner.py -- entry point CRON harian (SEKALI per hari bursa, setelah
tutup pasar + jeda waktu agar data EOD final). Melakukan:
  1. Cek regime pasar IHSG (gerbang wajib, fail-closed).
  2. Cek posisi terbuka yang sudah dikonfirmasi pengguna (stop/target/
     invalidasi dini/batas waktu) -- dijalankan LEBIH DULU, lebih penting
     dari mencari sinyal baru.
  3. Jika regime pasar mendukung: pindai seluruh universe saham utk
     sinyal breakout baru (Tahap 1, 2, 4, 5), simpan sbg "pending signal".
  4. Kirim semua notifikasi lewat Telegram.

Ini TUGAS TERJADWAL SEKALI JALAN -- cocok utk cron. Untuk mendengarkan
unggahan broker summary & perintah /entry /exit /positions, jalankan
run_bot.py TERPISAH sebagai proses yang selalu hidup (lihat README).

Contoh crontab (jalan 19:30 WIB Senin-Jumat; lihat README utk alasan jam
ini -- sesi BEI tutup 15:49/16:15 WIB, +jeda agar data yfinance final):

    30 19 * * 1-5 cd /path/ke/idx-swing-screener && /path/ke/venv/bin/python run_scanner.py >> cron_run.log 2>&1
"""
import os
import time
from datetime import date

import yfinance as yf

import strategy_params as sp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_EQUITY
from screener.market_data import (
    get_benchmark, market_regime_ok, fetch_history_with_retry, add_base_indicators,
)
from screener.signal_engine import evaluate_swing_setup, suggest_position_size
from screener.positions import PositionStore
from screener.telegram_client import TelegramClient
from screener.utils import logger, escape_html, fmt_rp


def get_universe():
    try:
        from config import IDX_TICKERS
        if IDX_TICKERS:
            return sorted(set(t.upper() if t.upper().endswith(".JK") else t.upper() + ".JK" for t in IDX_TICKERS))
    except ImportError:
        pass

    if os.path.exists("idx_tickers.txt"):
        tickers = []
        with open("idx_tickers.txt", "r", encoding="utf-8") as f:
            for raw in f.read().splitlines():
                t = raw.strip().upper().replace(".JK", "")
                if t and t.replace("-", "").isalnum():
                    tickers.append(t + ".JK")
        return sorted(set(tickers))

    logger.error("Universe saham kosong: idx_tickers.txt tidak ada & IDX_TICKERS kosong di config.py.")
    return []


def format_signal_block(ticker: str, result: dict) -> str:
    p = result["plan"]
    s = result["sizing"]
    return (
        f"📈 <b>{escape_html(ticker)}</b> | Close: <b>{fmt_rp(result['close'])}</b>\n"
        f"💧 Avg Value 20D: {fmt_rp(result['avg_value_20'] / 1e9, 1)} M "
        f"(median {fmt_rp(result['median_value_20'] / 1e9, 1)} M)\n"
        f"📊 RVOL {result['rvol']:.2f}x | CLV {result['clv']:.2f} | "
        f"RS: saham {result['stock_ret20_pct']:+.1f}% vs IHSG {result['bench_ret20_pct']:+.1f}%\n"
        f"Pivot P: {fmt_rp(p['pivot'])}\n"
        f"🎯 <b>Band order BESOK (t+1), limit order saja:</b> &gt;{fmt_rp(p['exec_band_low'])} s/d {fmt_rp(p['exec_band_high'])}\n"
        f"SL: {fmt_rp(p['stop'])} (-{p['stop_pct']:.1f}%) | TP: {fmt_rp(p['target'])} (+{p['target_pct']:.1f}%)\n"
        f"📐 RR bersih stlh estimasi fee: <b>1 : {p['net_rr']:.2f}</b>\n"
        f"📦 Saran posisi: <b>{s['lots']} lot</b> (≈{fmt_rp(s['capital'])}) -- pembatas: {s['binding_constraint']}\n"
        f"✅ Kalau sudah eksekusi besok: <code>/entry {escape_html(ticker)} harga_eksekusi [lot]</code>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
    )


def check_open_positions(tg: TelegramClient, store: PositionStore, today: date):
    open_positions = store.open_positions()
    if not open_positions:
        return

    exit_lines = []
    for pos in open_positions:
        try:
            yf_ticker = pos["ticker"] if pos["ticker"].endswith(".JK") else pos["ticker"] + ".JK"
            stock = yf.Ticker(yf_ticker)
            hist = fetch_history_with_retry(stock, period="5d")
            if hist.empty:
                logger.warning(f"Gagal ambil data terbaru utk posisi terbuka {pos['ticker']}, dilewati hari ini.")
                continue
            last = hist.iloc[-1]
            # Pakai tanggal BAR data (bukan tanggal server): di hari libur bursa
            # tidak ada bar baru, dan store.check_exits idempoten per tanggal bar.
            bar_date = hist.index[-1].date()
            alert = store.check_exits(
                pos["ticker"], bar_date,
                float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"]),
            )
            if alert:
                action = alert["action"]
                t = pos["ticker"]
                if action == "EXIT_STOP":
                    exit_lines.append(
                        f"🛑 <b>{escape_html(t)}</b>: STOP tersentuh (rencana {fmt_rp(alert['exit_price_plan'])}).\n"
                        f"   Konfirmasi: <code>/exit {escape_html(t)} harga_aktual</code>"
                    )
                elif action == "EXIT_TARGET":
                    exit_lines.append(
                        f"🎯 <b>{escape_html(t)}</b>: TARGET tersentuh (rencana {fmt_rp(alert['exit_price_plan'])}).\n"
                        f"   Konfirmasi: <code>/exit {escape_html(t)} harga_aktual</code>"
                    )
                elif action == "EXIT_EARLY_INVALIDATION_NEXT_OPEN":
                    exit_lines.append(
                        f"⚠️ <b>{escape_html(t)}</b>: close di bawah pivot dalam {sp.EARLY_INVALIDATION_WINDOW} sesi pertama -- "
                        f"EXIT di PEMBUKAAN sesi besok."
                    )
                elif action == "EXIT_TIME_STOP_NEXT_OPEN":
                    exit_lines.append(
                        f"⏱️ <b>{escape_html(t)}</b>: sudah {sp.MAX_HOLD_SESSIONS} sesi -- "
                        f"EXIT di PEMBUKAAN sesi besok (batas waktu)."
                    )
        except Exception:
            logger.exception(f"Gagal cek posisi terbuka {pos['ticker']}")
        time.sleep(sp.REQUEST_SLEEP_SEC)

    if exit_lines:
        tg.send_message("🔔 <b>AKSI POSISI TERBUKA</b>\n\n" + "\n\n".join(exit_lines))


def run_scanner():
    tg = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    today = date.today()
    store = PositionStore()

    benchmark_df = get_benchmark()
    regime_ok, regime_note = market_regime_ok(benchmark_df)

    universe = get_universe()
    tg.send_message(
        f"⏳ <b>IDX SWING SCREENER</b>\n"
        f"Universe: {len(universe)} emiten | {today.isoformat()}\n"
        f"{escape_html(regime_note)}"
    )

    # --- 1) Cek posisi terbuka LEBIH DULU (lebih penting dari sinyal baru) ---
    check_open_positions(tg, store, today)

    # --- 2) Pindai sinyal baru HANYA jika regime pasar mendukung ---
    if not regime_ok:
        tg.send_message(
            "⛔ <b>Tidak membuka posisi baru hari ini</b> -- IHSG di bawah SMA50 "
            "(gerbang wajib spek v1.0). Pemindaian sinyal baru dilewati."
        )
        logger.info("Regime pasar gagal -- scan sinyal baru dilewati.")
        return

    if not universe:
        tg.send_message("❌ Universe saham kosong, tidak ada yang dipindai. Cek idx_tickers.txt / config.py.")
        return

    candidates = []
    for i, ticker in enumerate(universe, start=1):
        print(f"[{i}/{len(universe)}] {ticker}...")
        try:
            stock = yf.Ticker(ticker)
            df = fetch_history_with_retry(stock, period="2y")
            if df.empty:
                continue
            df = add_base_indicators(df)
            passed, result = evaluate_swing_setup(df, benchmark_df, as_of=today)
            if passed:
                sizing = suggest_position_size(
                    result["plan"]["risk_per_share"], result["plan"]["entry_ref"],
                    result["avg_value_20"], ACCOUNT_EQUITY,
                )
                result["sizing"] = sizing
                candidates.append((ticker.replace(".JK", ""), result))
        except Exception:
            logger.exception(f"Gagal evaluasi {ticker}")
        time.sleep(sp.REQUEST_SLEEP_SEC)

    candidates.sort(key=lambda x: x[1]["plan"]["net_rr"], reverse=True)
    top = candidates[: sp.MAX_STOCKS_TO_SEND]

    if not top:
        tg.send_message("🛡 Tidak ada setup breakout yang lolos seluruh gerbang teknikal hari ini.")
        logger.info("Scan selesai: 0 kandidat.")
        return

    # Simpan SEMUA kandidat yang lolos sbg pending signal (bukan cuma yg dikirim)
    for ticker, result in candidates:
        store.save_pending_signal(ticker, today, result["plan"])

    msg = f"🎯 <b>{len(top)} KANDIDAT TEKNIKAL LOLOS</b> (dari {len(candidates)} total lolos gerbang teknikal)\n"
    msg += (
        "<i>Ini BARU lolos gerbang teknikal (Tahap 1,2,4,5). Verifikasi kelompok "
        "broker (Tahap 3) masih manual -- unduh Broker Summary dari Stockbit lalu "
        "kirim ke bot ini. Lihat README.</i>\n\n"
    )
    for ticker, result in top:
        block = format_signal_block(ticker, result)
        if len(msg) + len(block) > sp.TELEGRAM_MSG_SOFT_LIMIT:
            tg.send_message(msg)
            msg = block
        else:
            msg += block
    if msg:
        tg.send_message(msg)

    logger.info(f"Scan selesai: {len(candidates)} lolos teknikal, {len(top)} dikirim.")


if __name__ == "__main__":
    run_scanner()
