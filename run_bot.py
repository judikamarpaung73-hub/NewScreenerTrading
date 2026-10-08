#!/usr/bin/env python3
"""
run_bot.py -- proses yang BERJALAN TERUS (BUKAN cron job). Mendengarkan
pesan Telegram lewat long polling:
  - Menerima unggahan file broker summary (.csv/.xlsx) -> menjalankan uji
    kelompok broker Tahap 3 (PDF hal. 4) dan membalas hasilnya.
  - Perintah /entry, /exit, /positions, /help.

Jalankan ini sebagai service yang SELALU HIDUP (lihat README bagian
systemd) -- BUKAN lewat crontab. cron untuk tugas terjadwal sekali jalan
(run_scanner.py); proses ini untuk mendengarkan pesan masuk kapan saja.

Alur kerja yang dimaksud (lihat README untuk detail lengkap):
  1. run_scanner.py (cron) mengirim sinyal teknikal via Telegram.
  2. Anda buka Stockbit (Bandarmology / Broker Summary) untuk ticker itu,
     pilih rentang 20 sesi bursa terakhir, lalu catat/ekspor ke CSV/Excel
     SESUAI templates/broker_summary_template.csv (kemampuan ekspor
     langsung dari Stockbit belum terkonfirmasi -- lihat README).
  3. Unggah file itu ke chat bot ini -> bot membalas LOLOS/TIDAK LOLOS
     uji kelompok broker.
  4. Jika Anda jadi eksekusi besok paginya, catat lewat
     /entry TICKER harga_eksekusi [lot] -- posisi otomatis dipantau
     oleh run_scanner.py setiap hari setelahnya.
"""
import os
import tempfile
import time
from datetime import date

import pandas as pd
import yfinance as yf

import strategy_params as sp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from screener.broker_analysis import load_broker_csv, analyze_broker_group, BrokerDataError
from screener.market_data import fetch_history_with_retry
from screener.positions import PositionStore, DATA_DIR
from screener.telegram_client import TelegramClient
from screener.utils import logger, escape_html, fmt_rp

OFFSET_PATH = os.path.join(DATA_DIR, "bot_offset.txt")


def _load_offset():
    """Offset update Telegram terakhir yang sudah diproses. Disimpan ke disk
    supaya restart bot (deploy, reboot VPS) tidak memproses ulang perintah
    lama -- mis. /entry yang sama dua kali -> posisi dobel."""
    try:
        with open(OFFSET_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_offset(offset: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OFFSET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(offset))
    os.replace(tmp, OFFSET_PATH)


HELP_TEXT = (
    "<b>Perintah yang tersedia:</b>\n"
    "/help -- tampilkan pesan ini\n"
    "/positions -- daftar posisi terbuka yang sedang dilacak\n"
    "/entry TICKER harga [lot] -- catat posisi yang BENAR-BENAR sudah Anda "
    "eksekusi (otomatis mengambil SL/TP dari sinyal screener terakhir utk ticker itu)\n"
    "/exit TICKER harga -- catat penutupan posisi yang BENAR-BENAR sudah Anda eksekusi\n\n"
    "<b>Broker summary (Tahap 3):</b> unggah file .csv/.xlsx sesuai template "
    "(templates/broker_summary_template.csv) untuk menjalankan uji kelompok "
    "broker. Kolom Ticker di file menentukan saham yang dianalisis; boleh "
    "berisi beberapa ticker sekaligus dalam satu file."
)


def format_broker_result(ticker: str, r: dict) -> str:
    if r["status"] == "BELUM_TERVERIFIKASI":
        return f"❓ <b>{escape_html(ticker)}</b>: BELUM TERVERIFIKASI\n{escape_html(r['reason'])}"

    if r["status"] == "TIDAK_LOLOS" and r.get("stage") == "pemilihan_kelompok":
        return f"🚫 <b>{escape_html(ticker)}</b>: TIDAK LOLOS (pemilihan kelompok)\n{escape_html(r['reason'])}"

    lines = [
        f"{'✅ LOLOS' if r['status'] == 'LOLOS' else '🚫 TIDAK LOLOS'} <b>{escape_html(ticker)}</b> -- uji kelompok broker",
        f"Kelompok G: {', '.join(escape_html(g) for g in r['group_g'])}",
        f"A10: {r['a10_pct']:.1f}% (syarat ≥{sp.MIN_A10_PCT:.0f}%; penyebut: {escape_html(r['denom_source'])}) "
        f"{'✅' if r['gate_a10'] else '❌'}",
        f"Persistensi: {r['persistence_days']}/{sp.BROKER_TEST_WINDOW} sesi "
        f"(syarat ≥{sp.MIN_PERSISTENCE_DAYS}) {'✅' if r['gate_persistence'] else '❌'}",
        f"Keberlanjutan 5 sesi akhir: NV {r['nv5']:+,.0f} / NQ {r['nq5']:+,.0f} "
        f"{'✅' if r['gate_continuity'] else '❌'}",
        f"Diagnostik (bukan gerbang wajib): D3={r['dk_diagnostic']['D3']:+,.0f}, "
        f"D5={r['dk_diagnostic']['D5']:+,.0f}",
    ]
    if r.get("data_warnings"):
        shown = r["data_warnings"][:3]
        lines.append("⚠️ " + " / ".join(escape_html(w) for w in shown))
    return "\n".join(lines)


def handle_broker_file(tg: TelegramClient, file_path: str):
    try:
        df = load_broker_csv(file_path)
    except BrokerDataError as e:
        tg.send_message(f"❌ {escape_html(str(e))}")
        return
    except Exception as e:
        logger.exception("Gagal baca file broker")
        tg.send_message(f"❌ Gagal membaca file: {escape_html(str(e))}")
        return

    tickers = sorted(df["Ticker"].astype(str).str.upper().unique())
    if not tickers:
        tg.send_message("❌ Tidak ada baris data valid di file (cek kolom Ticker/angka).")
        return
    dropped = df.attrs.get("dropped_rows", 0)
    if dropped:
        tg.send_message(f"⚠️ {dropped} baris dibuang karena tidak lengkap/tidak valid. Periksa file bila ini tak wajar.")

    for ticker in tickers:
        try:
            yf_ticker = ticker if ticker.endswith(".JK") else ticker + ".JK"
            stock = yf.Ticker(yf_ticker)
            hist = fetch_history_with_retry(stock, period="3mo")
            if hist.empty or len(hist) < 20:
                tg.send_message(f"⚠️ <b>{escape_html(ticker)}</b>: gagal ambil data harga pembanding dari yfinance, dilewati.")
                continue

            last20 = hist.index[-20:]
            trading_dates = [pd.Timestamp(d).normalize() for d in last20]
            daily_value = {
                pd.Timestamp(d).normalize(): float(hist.loc[d, "Close"] * hist.loc[d, "Volume"])
                for d in last20
            }

            result = analyze_broker_group(df, ticker, trading_dates, daily_value)
            tg.send_message(format_broker_result(ticker, result))
        except Exception as e:
            logger.exception(f"Gagal analisis broker {ticker}")
            tg.send_message(f"❌ <b>{escape_html(ticker)}</b>: error saat analisis -- {escape_html(str(e))}")


def handle_command(tg: TelegramClient, store: PositionStore, text: str):
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]  # buang @botname kalau ada

    if cmd in ("/start", "/help"):
        tg.send_message(HELP_TEXT)
        return

    if cmd == "/positions":
        open_pos = store.open_positions()
        if not open_pos:
            tg.send_message("Tidak ada posisi terbuka yang dilacak.")
            return
        lines = ["<b>Posisi terbuka:</b>"]
        for p in open_pos:
            sl = fmt_rp(p["stop"]) if p.get("stop") is not None else "-"
            tp = fmt_rp(p["target"]) if p.get("target") is not None else "-"
            lines.append(
                f"{escape_html(p['ticker'])} | entry {fmt_rp(p['entry_price'])} ({p['entry_date']}) | "
                f"SL {sl} | TP {tp} | sesi ke-{p.get('session_count', 0)}"
            )
        tg.send_message("\n".join(lines))
        return

    if cmd == "/entry":
        if len(parts) < 3:
            tg.send_message("Format: /entry TICKER harga_eksekusi [lot]\nContoh: /entry BBCA 10150 50")
            return
        try:
            ticker = parts[1].upper()
            exec_price = float(parts[2])
            lots = int(parts[3]) if len(parts) > 3 else None
        except ValueError:
            tg.send_message("Harga/lot tidak valid. Format: /entry TICKER harga_eksekusi [lot]")
            return

        if store.has_open_position(ticker):
            tg.send_message(
                f"❌ Sudah ada posisi terbuka untuk {escape_html(ticker)}. Satu ticker = satu posisi. "
                f"Tutup dulu dengan /exit {escape_html(ticker)} harga_aktual bila memang ingin mencatat ulang."
            )
            return

        pending = store.pop_pending_signal(ticker)
        shares = lots * sp.LOT_SIZE if lots else None
        if pending is None:
            store.add_position(ticker, date.today(), exec_price, None, None, None, lots, shares)
            tg.send_message(
                f"⚠️ Tidak ada sinyal tertunda utk {escape_html(ticker)} (mungkin sudah lewat beberapa hari, "
                f"atau tidak pernah lolos screener). Posisi dicatat TANPA SL/TP otomatis -- "
                f"pantau manual atau isi langsung di data/positions.json."
            )
            return

        pos = store.add_position(
            ticker, date.today(), exec_price,
            pending["pivot_p"], pending["stop"], pending["target"], lots, shares,
        )
        tg.send_message(
            f"✅ Posisi dicatat: <b>{escape_html(ticker)}</b> @ {fmt_rp(exec_price)}"
            + (f", {lots} lot" if lots else "") + f" (sinyal {pending['signal_date']})\n"
            f"Pivot: {fmt_rp(pending['pivot_p'])} | SL: {fmt_rp(pending['stop'])} | TP: {fmt_rp(pending['target'])}\n"
            f"Akan dipantau otomatis setiap hari lewat run_scanner.py."
        )
        return

    if cmd == "/exit":
        if len(parts) < 3:
            tg.send_message("Format: /exit TICKER harga_aktual")
            return
        try:
            ticker = parts[1].upper()
            price = float(parts[2])
        except ValueError:
            tg.send_message("Harga tidak valid. Format: /exit TICKER harga_aktual")
            return
        pos = store.close_position(ticker, date.today(), price, reason="manual_confirm")
        if pos:
            tg.send_message(f"✅ Posisi {escape_html(ticker)} ditutup @ {fmt_rp(price)}.")
        else:
            tg.send_message(f"❌ Tidak ditemukan posisi terbuka untuk {escape_html(ticker)}.")
        return

    tg.send_message("Perintah tidak dikenali. Ketik /help untuk daftar perintah.")


def process_update(tg: TelegramClient, store: PositionStore, upd: dict):
    msg = upd.get("message")
    if not msg:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        # Siapa pun bisa mengirim pesan ke username bot publik -- hanya chat pemilik yang dilayani.
        logger.warning(f"Pesan dari chat_id tidak dikenal ({chat_id}), diabaikan.")
        return

    if "document" in msg:
        doc = msg["document"]
        fname = os.path.basename(doc.get("file_name") or "upload.csv")  # cegah path aneh
        tg.send_message(f"📥 Menerima {escape_html(fname)}, menganalisis...")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, fname)
            if tg.download_file(doc["file_id"], dest):
                handle_broker_file(tg, dest)
            else:
                tg.send_message("❌ Gagal mengunduh file dari Telegram.")

    elif "text" in msg:
        handle_command(tg, store, msg["text"])


def main_loop():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi di config.py. Berhenti.")
        return

    tg = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    store = PositionStore()
    offset = _load_offset()
    logger.info(f"Bot listener dimulai (long polling), offset awal={offset}")
    print("Bot listener berjalan. Tekan Ctrl+C untuk berhenti (atau kelola lewat systemd).")

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=30)
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    process_update(tg, store, upd)
                except Exception as e:
                    # Satu pesan bermasalah tidak boleh memblokir antrean selamanya.
                    logger.exception(f"Gagal memproses update {upd.get('update_id')}: {e}")
                    tg.send_message(f"⚠️ Error saat memproses pesan terakhir: {escape_html(str(e))}")
                finally:
                    _save_offset(offset)

        except KeyboardInterrupt:
            logger.info("Bot dihentikan manual (KeyboardInterrupt).")
            break
        except Exception as e:
            logger.exception(f"Error di main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main_loop()
