# IDX Swing Screener — Teknikal + Bandarmology

Implementasi kode dari **Riset & Spesifikasi Screener Saham Indonesia v1.0**
(21 Sept 2026): saham likuid dalam tren naik, breakout dengan volume
pendukung, diverifikasi dengan uji konsistensi kelompok broker, batas
kerugian ditetapkan sebelum masuk.

**Status: sama seperti status riset aslinya — rancangan selesai, kinerja
belum tervalidasi. Belum ada backtest. Ini bukan rekomendasi membeli
emiten tertentu maupun janji hasil.** Lihat bagian "Yang belum dikerjakan"
di bawah.

---

## 1. Arsitektur — 3 proses terpisah, sengaja

| Proses | Jenis | Jalan lewat | Tugas |
|---|---|---|---|
| `run_scanner.py` | **Terjadwal, sekali jalan** | cron, 1x/hari | Cek regime IHSG, cek posisi terbuka, cari sinyal breakout baru, kirim ke Telegram |
| `run_bot.py` | **Selalu hidup** | systemd (atau `screen`/`tmux`) | Dengar pesan Telegram masuk: terima unggahan broker summary, proses perintah `/entry` `/exit` `/positions` |
| Anda (manual) | — | Stockbit, lalu Telegram | Verifikasi kelompok broker (Tahap 3) — lihat bagian 3 |

**Kenapa dipisah begini, bukan satu skrip cron saja:** cron cocok untuk tugas
terjadwal (jalan, selesai, keluar), tapi TIDAK bisa dipakai untuk
"mendengarkan pesan kapan saja" — itu perlu proses yang hidup terus
(long polling ke Telegram). Memaksakan keduanya jadi satu cron job berarti
bot cuma "mendengarkan" sesaat tiap kali cron jalan, lalu tuli selama 23
jam sisanya. Karena `run_bot.py` toh harus selalu hidup di sebuah
server/VPS, `run_scanner.py` juga dijalankan di server yang sama lewat
cron — tidak perlu dua tempat hosting berbeda.

**Soal "di GitHub":** saya siapkan proyek ini sebagai repo git yang sudah
`git init` + commit pertama (lihat isi .zip), supaya tinggal:
```bash
cd idx-swing-screener
git remote add origin https://github.com/USERNAME/REPO_ANDA.git
git push -u origin main
```
Saya tidak bisa membuat repo di akun GitHub Anda langsung (tidak punya
kredensial Anda, dan sebaiknya memang begitu) — langkah push di atas tetap
perlu Anda lakukan sendiri setelah membuat repo kosong di GitHub.

---

## 2. Setup awal

```bash
git clone <repo-anda>   # atau cd ke folder hasil unzip
cd idx-swing-screener
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py
nano config.py   # isi TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_EQUITY
```

Dapatkan `TELEGRAM_BOT_TOKEN` dari [@BotFather](https://t.me/BotFather) di
Telegram (`/newbot`). Dapatkan `TELEGRAM_CHAT_ID` dengan chat `/start` ke
bot Anda sendiri lalu buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
di browser — cari `"chat":{"id": ...}`.

Uji coba manual sebelum di-cron-kan:
```bash
python run_scanner.py     # harus kirim pesan ke Telegram Anda
python run_bot.py         # biarkan jalan, lalu coba /help dari Telegram
```

### `config.py` vs `strategy_params.py` — kenapa dipisah

- **`config.py`** (di `.gitignore`, TIDAK pernah masuk git): token bot,
  chat ID, dan `ACCOUNT_EQUITY` — data rahasia/personal.
- **`strategy_params.py`** (SENGAJA masuk git, bukan rahasia): semua angka
  ambang batas strategi (likuiditas, RVOL, RR minimum, dst). Ini mengikuti
  prinsip eksplisit riset v1.0: *"Parameter dapat direvisi hanya melalui
  proses evaluasi yang tercatat."* Setiap kali Anda mengubah parameter,
  lakukan lewat commit git dengan pesan yang menjelaskan alasannya — itu
  jurnal perubahan strategi Anda.

---

## 3. Alur kerja manual (yang TIDAK bisa dikerjakan mesin)

Sesuai arahan Anda: **bot Telegram cuma mengirim sinyal teknikal**. Uji
kelompok broker (Tahap 3 PDF) butuh data Broker Summary yang — berdasarkan
riset Anda sendiri (PDF hal. 8) — **kemampuan ekspornya ke Excel/CSV dari
Stockbit belum terkonfirmasi**. Jadi alurnya:

1. **`run_scanner.py` (otomatis, cron)** mengirim sinyal teknikal ke
   Telegram, misal:
   > 📈 **BBCA** | Close: Rp 10.150
   > Band order besok (t+1): >Rp 10.175 s/d Rp 10.200
   > SL: Rp 9.900 (-2.5%) | TP: Rp 10.750 (+5.9%)

2. **Anda buka Stockbit** → menu Bandarmology / Broker Summary untuk
   ticker itu. Set rentang tanggal ke **20 sesi bursa terakhir** (bukan 20
   hari kalender). Untuk tiap broker yang muncul, catat: BuyVol, BuyVal,
   SellVol, SellVal, per hari.
   - **Kalau Stockbit punya tombol ekspor ke Excel/CSV**: pakai itu,
     lalu sesuaikan nama kolom ke format di `templates/broker_summary_template.csv`.
   - **Kalau tidak ada ekspor langsung** (ini yang riset Anda tandai belum
     pasti): salin manual ke spreadsheet dengan kolom yang SAMA PERSIS:
     `Date,Ticker,Market,BrokerCode,BuyVol,BuyVal,SellVol,SellVal`.
     Volume dalam **lembar** (bukan lot — kalau Stockbit menampilkan lot,
     kalikan 100). `Market` isi `RG` (reguler); jangan campur dengan
     transaksi negosiasi.
   - Simpan sebagai `.csv` atau `.xlsx` (`.xls` lama tidak didukung).
   - **Aturan format — bot MENOLAK file yang melanggarnya, bukan menebak:**
     - Angka = **bilangan bulat polos**: `5250000000`. Jangan pakai singkatan
       (`10.2B`, `345M`, `1.2K` — arti K/M/B/T berbeda antar aplikasi) dan
       **jangan pakai titik ribuan** (`5.250.000.000`; `500.000` bisa terbaca
       500, bukan 500000).
     - Kolom Buy/Sell = **gross**, tidak boleh negatif (net dihitung otomatis).
     - Tanggal: `2026-09-15` (disarankan) atau `15/09/2026` (dd/mm/yyyy,
       konvensi Indonesia — bukan mm/dd).
     - Pemisah CSV `,` atau `;` keduanya dikenali (Excel Indonesia sering `;`).
     - **Isi SEMUA broker** untuk **20 sesi bursa terakhir** (bukan cuma
       top 5). Pemeriksaan silang gross buy vs sell per hari akan memberi
       peringatan kalau datanya kelihatan tidak lengkap — dan kelompok G
       dipilih dari data yang Anda berikan, jadi data yang bolong menghasilkan
       kelompok yang salah.

3. **Unggah file itu ke chat bot Telegram Anda** (drag & drop / attach).
   `run_bot.py` akan otomatis baca kolom `Ticker`, ambil kalender 20 sesi
   bursa terakhir dari data harga (yfinance) — bukan tebakan hari kerja
   biasa — lalu menjalankan uji kelompok broker PDF hal. 4 persis: pilih 3
   broker net-buy terbesar di 10 sesi pertama, bekukan, uji konsistensi
   10 sesi berikutnya (A10 ≥ 10%, persistensi ≥ 6/10 hari, keberlanjutan
   5 hari terakhir positif). Bot membalas LOLOS / TIDAK LOLOS plus
   rinciannya.

4. **Kalau Anda putuskan eksekusi** besok paginya dalam band yang
   diberikan: catat lewat `/entry BBCA 10180 50` (ticker, harga eksekusi
   nyata, jumlah lot) **pada hari yang sama saat order terisi** — hari
   pencatatan dianggap sesi ke-1, jadi mencatat terlambat menggeser hitungan
   invalidasi dini & batas 20 sesi. Satu ticker = satu posisi terbuka; `/entry`
   kedua untuk ticker yang sama ditolak sampai Anda `/exit`. Bot otomatis mengambil SL/TP dari sinyal screener
   yang relevan. Posisi ini lalu dipantau otomatis setiap hari oleh
   `run_scanner.py` (stop/target/invalidasi dini/batas waktu 20 sesi).

5. Saat ada notifikasi "STOP tersentuh" / "TARGET tersentuh" / "EXIT di
   pembukaan besok": **lakukan transaksinya, lalu konfirmasi** dengan
   `/exit BBCA 9910`. Sistem SENGAJA tidak menutup posisi otomatis —
   harga eksekusi riil bisa beda dari rencana (gap, antrean, auto
   rejection), jadi catatan posisi harus selalu mencerminkan apa yang
   benar-benar terjadi, bukan asumsi. Alert dikirim per bar harian: selama
   harga masih di bawah stop, alert yang sama muncul lagi tiap hari sampai
   Anda mengonfirmasi `/exit`. Di hari entry sendiri hanya aturan "close di
   bawah pivot" yang dicek (urutan intraday tidak diketahui, jadi Low/High
   hari entry tidak dipakai menyimpulkan stop/target).

---

## 4. Deploy ke server (VPS kecil, bisa dari ~$5-6/bulan)

```bash
# Scanner: cron
crontab -e
# tempel isi scripts/crontab_example.txt (sesuaikan path & timezone)

# Bot listener: systemd, selalu hidup
sudo cp scripts/idx-swing-bot.service.example /etc/systemd/system/idx-swing-bot.service
sudo nano /etc/systemd/system/idx-swing-bot.service   # sesuaikan User= & path
sudo systemctl daemon-reload
sudo systemctl enable --now idx-swing-bot
sudo systemctl status idx-swing-bot
```

**Kenapa harus server/VPS, bukan laptop Anda:** `run_bot.py` perlu hidup
24/7 supaya tidak melewatkan sinyal atau unggahan Anda, dan `run_scanner.py`
perlu jalan konsisten tiap hari bursa walau laptop Anda mati/tidur.

**Alternatif tanpa VPS, hanya untuk `run_scanner.py`:** GitHub Actions
punya trigger `schedule` (cron) gratis untuk repo publik/dengan kuota
untuk repo privat. Ini bisa menggantikan cron tradisional UNTUK SCANNER
SAJA — tapi `run_bot.py` (perlu selalu hidup, bukan terjadwal) tetap butuh
server sungguhan. Karena itu, kalau Anda sudah punya server untuk bot,
menjalankan scanner lewat cron di server yang sama lebih sederhana
daripada mengurus dua mekanisme berbeda. Kalau tetap mau coba GitHub
Actions untuk scanner saja, strukturnya kurang lebih:

```yaml
# .github/workflows/scanner.yml (buat manual kalau ingin memakai ini)
on:
  schedule:
    - cron: '30 12 * * 1-5'  # 12:30 UTC = 19:30 WIB
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: python run_scanner.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          # perlu sedikit ubah config.py agar baca dari env var di CI
```
(`positions.json` perlu di-commit balik ke repo tiap jalan kalau pakai
jalur ini, karena GitHub Actions tidak punya disk persisten antar-run —
lumayan rapuh untuk melacak posisi; VPS + systemd lebih sederhana untuk
kasus ini.)

---

## 5. Perbedaan dari draf kode awal Anda, dan alasannya

Saya memakai **PDF sebagai sumber kebenaran** (lebih rinci, bernotasi
presisi, dan ditulis dengan sadar soal overfitting), dan draf kode sebagai
titik awal struktur saja. Tempat keduanya beda, saya ikuti PDF:

| Hal | Draf kode awal | PDF v1.0 | Yang dipakai & kenapa |
|---|---|---|---|
| Jenis setup | Breakout + Pullback | Breakout saja | **PDF.** Pullback tidak pernah divalidasi di spek; menambahnya = menambah aturan di luar cakupan yang diuji. |
| Tren | EMA20>EMA50>SMA200 | Close>SMA20>SMA50, SMA50 naik 5 sesi | **PDF**, formula persis. |
| Kekuatan relatif | Mansfield RS | Return 20 sesi saham vs IHSG | **PDF** — PDF eksplisit (hal. 8) menghindari indikator tambahan yang belum terbukti menambah informasi. |
| Regime pasar gagal | Tetap scan "mode defensif" | Tidak buka posisi baru sama sekali | **PDF** — draf kode punya komentar "PERBAIKAN" yang justru membalik aturan wajib PDF. |
| Target | Resistance 60D atau blue-sky | T = E + 3R, ditolak kalau ada swing high 120 sesi menghalangi ruang 2R | **PDF**, termasuk pengecekan swing high yang sama sekali tak ada di draf. |
| Reward/risk | RR kotor | RR BERSIH setelah estimasi biaya (fee 0,15%/0,25%) | **PDF.** |
| POV likuiditas posisi | 5% dari nilai transaksi harian | 0,5% | **PDF** — draf kode 10x lebih agresif dari spek, berisiko membuat order terlalu besar relatif ke likuiditas. |
| Cap nilai posisi | 20% modal | 15% modal | **PDF.** |
| Harga minimum | Rp50 | Rp200 (batas desain, bukan resmi BEI) | **PDF**, dengan catatan BEI baru menurunkan batas resminya ke Rp1 (lihat #6). |
| Manajemen posisi terbuka | Tidak ada | Exit dini 3 sesi, time-stop 20 sesi | **Ditambahkan baru** — PDF mendefinisikannya, draf kode sama sekali tidak punya ini. |

---

**Data harga: mentah, bukan disesuaikan dividen.** Draf awal memakai
`auto_adjust=True` (harga historis digeser dividen). Output sistem ini
adalah harga order literal, jadi saya pakai harga as-traded (`YF_AUTO_ADJUST
= False` di `strategy_params.py`); split/reverse split tetap disesuaikan
Yahoo. Konsekuensi: ex-dividend tampak sebagai gap turun di data — sama
seperti di chart Stockbit Anda. Rights issue tidak disesuaikan oleh Yahoo
dalam mode apa pun; cek aksi korporasi manual sebelum entry.

**File state** (di folder `data/`, di-gitignore): `positions.json` (posisi
terbuka, tertutup, sinyal tertunda) dan `bot_offset.txt` (posisi antrean
Telegram, supaya restart bot tidak memproses ulang perintah lama). Dua
proses (scanner cron & bot) berbagi `positions.json` dengan penguncian
file + muat-ulang tiap operasi; file rusak dicadangkan sebagai
`positions.json.corrupt-<waktu>`, tidak ditimpa diam-diam. Kunci file
memakai `fcntl` (Linux/macOS); di Windows tanpa kunci — aman selama dua
proses tidak menulis di detik yang sama, tapi deploy produksi disarankan di
Linux.

---

## 6. ⚠️ Temuan penting: aturan ARB BEI baru saja berubah

Saat menyiapkan kode ini saya cek ulang aturan fraksi harga & auto
rejection BEI (karena ini langsung memengaruhi perhitungan stop-loss), dan
menemukan **BEI mengumumkan perubahan pada 21 Sept 2026** (SK Direksi BEI
No. Kep-00136/BEI/09-2026, berlaku efektif **28 Sept 2026**) — persis di
sekitar tanggal riset PDF Anda dibuat:

- Batas harga minimum resmi BEI turun dari Rp50 ke **Rp1**.
- Masa transisi (28 Sept – 31 Des 2026): **ARB flat 15%** untuk saham di
  atas Rp10 (band Rp1-Rp10 pakai batas nominal Rp1, bukan persentase).
- Mulai **1 Januari 2027**: ARB kembali simetris dengan ARA sesuai pita
  harga (35% / 25% / 20% tergantung level harga) — berarti **batas 15%
  flat TIDAK berlaku lagi mulai tahun depan.**

Kode ini sudah menangani ini lewat `screener/utils.py` fungsi
`get_arb_ceiling_pct()` yang **sadar tanggal** — otomatis memakai 15% flat
sekarang, dan otomatis beralih ke aturan per-pita-harga begitu tanggal
sistem melewati 1 Jan 2027. **Tetap perlu Anda cross-check berkala ke
pengumuman resmi BEI** (idx.co.id) terutama menjelang pergantian tahun —
saya memberi komentar peringatan langsung di kode itu.

---

## 7. Struktur proyek

```
idx-swing-screener/
├── config.example.py        # salin ke config.py, isi token & modal Anda
├── strategy_params.py       # SEMUA parameter strategi (di-commit ke git)
├── idx_tickers.txt          # universe saham (starter list, lengkapi sendiri)
├── requirements.txt
├── templates/
│   └── broker_summary_template.csv
├── screener/
│   ├── utils.py             # fraksi harga, ARB, format Rupiah, logger
│   ├── market_data.py       # ambil harga yfinance, indikator, regime IHSG
│   ├── signal_engine.py     # Tahap 1,2,4,5: gerbang teknikal + rencana transaksi
│   ├── broker_analysis.py   # Tahap 3: uji kelompok broker dari file unggahan
│   ├── positions.py         # pelacakan posisi terbuka & aturan keluar
│   └── telegram_client.py   # wrapper Telegram Bot API
├── run_scanner.py           # CRON — jalan 1x/hari
├── run_bot.py                # SELALU HIDUP — systemd
└── scripts/                 # contoh crontab & systemd unit
```

---

## 8. Yang belum dikerjakan (sesuai status riset Anda)

PDF Anda eksplisit: *"Rancangan selesai; keunggulan kinerja belum
tervalidasi."* Kode ini mengimplementasikan ATURANNYA dengan presisi,
tapi:

- **Belum ada backtest.** PDF hal. 9 menggariskan prosedurnya sendiri
  (Eksperimen A vs B, walk-forward, biaya aktual, koreksi overfitting ala
  Bailey dkk.) — ini proyek terpisah yang jauh lebih besar (perlu data
  historis broker summary harian + harga, termasuk saham yang sudah
  delisting/disuspensi, yang TIDAK saya punya aksesnya). Jangan anggap
  lolosnya sinyal dari sistem ini sebagai bukti ia akan menghasilkan
  profit.
- **Tarif fee (0,15%/0,25%) dan tarif ARB** bisa berubah — sudah terbukti
  berubah sekali tepat di sekitar tanggal riset Anda. Cross-check berkala.
- **Kemampuan ekspor Broker Summary Stockbit** belum pernah benar-benar
  diuji (riset Anda sendiri menandainya begitu) — jalur manual/copy-paste
  di bagian 3 dirancang supaya tetap jalan walau ekspor otomatis ternyata
  tidak tersedia.
- **Yang sudah diuji vs belum.** Logika inti (gerbang teknikal, rencana
  transaksi, ukuran posisi, uji kelompok broker, pelacakan posisi, parser file,
  perintah bot, orkestrasi scanner) diuji dengan data sintetis dan klien palsu.
  **Belum pernah dijalankan terhadap Yahoo Finance dan api.telegram.org
  sungguhan** — lingkungan tempat kode ini dibuat tidak punya akses ke dua
  domain itu. Jalankan `python run_scanner.py` dan `python run_bot.py` di
  server Anda, lalu periksa `screener.log` bila ada yang tidak jalan.
- `idx_tickers.txt` adalah starter list, bukan daftar resmi LQ45/IDX80
  yang terverifikasi saat ini.

Status BELUM TERVERIFIKASI yang dikembalikan `broker_analysis.py` (data
kurang/tidak lengkap) sengaja dibedakan dari TIDAK LOLOS (data lengkap
tapi gagal gerbang) — supaya data kosong tidak pernah diam-diam dianggap
lolos, sesuai arahan eksplisit PDF hal. 7.
