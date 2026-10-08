"""
strategy_params.py
===================
SEMUA parameter strategi (bukan rahasia/token) ada di sini. File ini
SENGAJA tidak di-.gitignore, supaya setiap perubahan tercatat di riwayat
git -- sesuai prinsip riset v1.0: "Parameter dapat direvisi hanya melalui
proses evaluasi yang tercatat." Jangan ubah angka di sini tanpa alasan
yang dicatat di commit message / jurnal trading Anda.

Sumber utama: Riset & Spesifikasi Screener Saham Indonesia v1.0
(dibaca/ditelusuri 21 Sept 2026), kecuali bagian yang ditandai [DEVIASI]
atau [CATATAN] -- lihat README.md bagian "Perbedaan dari draf kode awal"
untuk penjelasan lengkap tiap keputusan.
"""

BENCHMARK = "^JKSE"

# Harga mentah (as-traded) vs disesuaikan dividen. Output sistem ini adalah
# harga order literal (pivot, band eksekusi, SL, TP), jadi dipakai harga
# mentah -- sama dengan yang tampil di chart Stockbit. Split/reverse split
# tetap disesuaikan oleh Yahoo walau auto_adjust=False (PDF hal. 3: "sesuaikan
# OHLC dan volume untuk split/reverse split secara konsisten"). Jika True,
# harga historis digeser oleh dividen sehingga pivot bisa meleset dari harga
# transaksi aktual bila ada ex-dividend dalam 20 sesi terakhir.
YF_AUTO_ADJUST = False

# ------------------------------------------------------------------
# TAHAP 1 -- KELAYAKAN & LIKUIDITAS  (PDF hal. 3)
# ------------------------------------------------------------------
MIN_PRICE = 200.0
# [CATATAN] Ini batas DESAIN, bukan batas minimum resmi BEI. BEI menurunkan
# batas harga minimum resminya dari Rp50 ke Rp1 mulai 28 Sept 2026 (SK
# Direksi BEI No. Kep-00136/BEI/09-2026). Rp200 tetap dipertahankan di sini
# sebagai pilihan desain untuk menghindari saham receh yang sangat tipis
# dan rawan "digoreng" -- bukan karena itu batas resmi BEI.
MIN_HISTORY_DAYS = 260               # ~1 tahun bursa; perlu utk SMA200 & swing-high 120 sesi
MIN_AVG_VALUE_20 = 10_000_000_000    # Rp10 M rata-rata nilai transaksi reguler 20 sesi
MIN_MEDIAN_VALUE_20 = 5_000_000_000  # Rp5 M median nilai transaksi reguler 20 sesi
REQUIRE_ALL_20_DAYS_TRADED = True    # Tidak boleh ada sesi bervolume 0 dalam 20 sesi

# ------------------------------------------------------------------
# TAHAP 2 -- KONDISI PASAR, TREN, KEKUATAN RELATIF (PDF hal. 3)
# ------------------------------------------------------------------
MARKET_SMA_WINDOW = 50               # IHSG close > SMA50 IHSG -> syarat wajib buka posisi baru
STOCK_SMA_FAST = 20
STOCK_SMA_SLOW = 50
TREND_SLOPE_LOOKBACK = 5             # SMA50(t) > SMA50(t-5)
RELATIVE_STRENGTH_LOOKBACK = 20      # Return saham 20 sesi > return IHSG 20 sesi

# ------------------------------------------------------------------
# TAHAP 4 -- PEMICU BREAKOUT (PDF hal. 3)
# ------------------------------------------------------------------
BREAKOUT_LOOKBACK = 20               # P = max High(t-20..t-1)
BREAKOUT_MIN_RVOL = 1.5              # Volume(t) / rata2 Volume(t-20..t-1) >= 1.5
BREAKOUT_MIN_CLV = 0.70              # (Close-Low)/(High-Low) >= 0.70
# Jarak breakout: 0 < Close(t)-P <= ATR14(t)  -> dicek langsung di signal_engine.py

# ------------------------------------------------------------------
# TAHAP 5 -- ENTRY, STOP, TARGET, UKURAN POSISI (PDF hal. 5)
# ------------------------------------------------------------------
STOP_ATR_MULT = 0.5                  # S = min(Low(t), P) - 0.5*ATR14(t)
MAX_STOP_WIDTH_PCT = 8.0             # (E-S)/E <= 8%, lewati jika lebih lebar
TARGET_R_MULTIPLE = 3.0              # T = E + 3*(E-S)
SWING_HIGH_LOOKBACK = 120            # cek swing high 120 sesi yg menghalangi ruang 2R
MIN_RR_AFTER_COST = 2.0              # (T-E-biaya)/(E-S+biaya) >= 2

# Estimasi biaya transaksi Stockbit, dicek per 21 Sept 2026 (PDF ref [8]).
# !! VERIFIKASI ULANG BERKALA -- tarif broker bisa berubah sewaktu-waktu.
BUY_FEE_PCT = 0.0015                 # 0.15%
SELL_FEE_PCT = 0.0025                # 0.25%
EXTRA_SLIPPAGE_PER_SHARE = 0.0       # Rp/lembar, cadangan tambahan opsional (default: tidak ada)

# Ukuran posisi (lihat juga ACCOUNT_EQUITY di config.py)
RISK_PER_TRADE_PCT = 0.5             # % modal yang boleh hilang per transaksi
MAX_POSITION_PCT = 15.0              # % modal maksimum di satu posisi
MAX_POV_RATE = 0.005                 # Maksimum porsi dari rata-rata nilai transaksi harian (0.5%)
LOT_SIZE = 100                       # 1 lot BEI = 100 lembar

# ARB: lihat screener/utils.py fungsi get_arb_ceiling_pct() -- dibuat
# sadar-tanggal karena aturan ARB BEI sedang masa transisi (berubah lagi
# per 1 Jan 2027). ARB_SAFETY_MARGIN = pakai sekian % dari batas resmi
# sebagai pagar aman (jangan mepet ke batas persis).
ARB_SAFETY_MARGIN = 0.9

# ------------------------------------------------------------------
# TAHAP 3 -- FILTER BROKER / BANDARMOLOGY (PDF hal. 4)
# ------------------------------------------------------------------
BROKER_SELECTION_WINDOW = 10         # t-19..t-10 : pemilihan kelompok G
BROKER_TEST_WINDOW = 10              # t-9..t     : pengujian kelompok G (dibekukan)
BROKER_CONFIRM_WINDOW = 5            # t-4..t     : keberlanjutan
BROKER_GROUP_SIZE = 3                # ambil 3 broker net buyer terbesar
MIN_A10_PCT = 10.0                   # A10 >= 10%
MIN_PERSISTENCE_DAYS = 6             # minimal 6 dari 10 sesi uji NV&NQ kelompok > 0
DATA_MISMATCH_WARN_PCT = 10.0        # toleransi peringatan selisih gross buy vs sell

# ------------------------------------------------------------------
# MANAJEMEN POSISI TERBUKA (PDF hal. 5)
# ------------------------------------------------------------------
EARLY_INVALIDATION_WINDOW = 3        # 3 sesi pertama: exit sesi berikutnya jika close < P
MAX_HOLD_SESSIONS = 20               # keluar di PEMBUKAAN sesi ke-21 (entry = sesi ke-1)

# ------------------------------------------------------------------
# OUTPUT / OPERASIONAL
# ------------------------------------------------------------------
MAX_STOCKS_TO_SEND = 8
REQUEST_SLEEP_SEC = 0.3
HISTORY_FETCH_RETRIES = 2
HISTORY_FETCH_RETRY_DELAY = 1.5
TELEGRAM_MSG_SOFT_LIMIT = 3900        # batas aman di bawah limit Telegram 4096 char
