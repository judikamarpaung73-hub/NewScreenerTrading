"""
broker_analysis.py -- diagnostik OPSIONAL kelompok broker (bukan syarat prediktif)
dari data broker summary yang diunggah pengguna secara MANUAL.

PENTING: modul ini TIDAK mengambil data dari Stockbit secara otomatis.
Kemampuan ekspor Broker Summary Stockbit ke Excel/CSV belum terkonfirmasi
dari dokumentasi (PDF hal. 8, baris 'Ekspor'). Pengguna mengisi/menyalin
data ke format CSV sesuai templates/broker_summary_template.csv, lalu
mengunggahnya lewat bot Telegram (lihat run_bot.py).

Kalender sesi bursa diambil dari data HARGA (via yfinance), BUKAN ditebak
dari hari-kerja kalender biasa -- BEI punya hari libur sendiri (Lebaran,
Nyepi, dll) yang tidak sama dengan kalender kerja umum.
"""
import pandas as pd
import numpy as np

import strategy_params as sp
from .utils import logger


REQUIRED_COLUMNS = ["Date", "Ticker", "Market", "BrokerCode", "BuyVol", "BuyVal", "SellVol", "SellVal"]
NUMERIC_COLUMNS = ["BuyVol", "BuyVal", "SellVol", "SellVal"]


class BrokerDataError(Exception):
    pass


def _read_table(file_path: str) -> pd.DataFrame:
    lower = file_path.lower()
    if lower.endswith(".xls"):
        raise BrokerDataError("Format .xls (Excel lama) tidak didukung. Simpan ulang sebagai .xlsx atau .csv.")
    # dtype=str: semua sel dibaca sebagai TEKS MENTAH supaya pola seperti
    # "500.000" (Indonesia: lima ratus ribu) tidak diam-diam jadi 500.0 oleh
    # pandas sebelum sempat kita periksa. Konversi angka dilakukan eksplisit.
    if lower.endswith(".xlsx"):
        return pd.read_excel(file_path, dtype=str)
    # sep=None + engine python: deteksi otomatis pemisah "," atau ";" (Excel
    # versi Indonesia sering menyimpan CSV dengan titik-koma).
    try:
        return pd.read_csv(file_path, sep=None, engine="python", encoding="utf-8-sig", dtype=str)
    except UnicodeDecodeError:
        return pd.read_csv(file_path, sep=None, engine="python", encoding="latin-1", dtype=str)


def _parse_dates(col: pd.Series) -> pd.Series:
    """Parse tanggal per-format secara EKSPLISIT. Sengaja TIDAK memakai
    dayfirst=True secara global: itu ikut membalik tanggal ISO (2026-09-03
    terbaca 3 Maret). Urutan: ISO yyyy-mm-dd dulu, baru konvensi Indonesia
    dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy. Sel datetime asli (xlsx) dipakai apa adanya."""
    if pd.api.types.is_datetime64_any_dtype(col):
        return col
    s = col.astype(str).str.strip().str.slice(0, 10)
    out = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        mask = out.isna()
        if not mask.any():
            break
        out = out.fillna(pd.to_datetime(s.where(mask), format=fmt, errors="coerce"))
    return out


def load_broker_csv(file_path: str) -> pd.DataFrame:
    """Baca & validasi file broker summary yang diunggah pengguna (CSV atau
    .xlsx), sesuai skema PDF hal. 7. Prinsipnya: LEBIH BAIK MENOLAK dengan
    pesan jelas daripada membaca angka dengan salah secara diam-diam.
    Hasil: DataFrame bersih; df.attrs['dropped_rows'] = jumlah baris dibuang."""
    df = _read_table(file_path)

    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise BrokerDataError(
            f"Kolom wajib tidak lengkap: {', '.join(missing)}. "
            f"Gunakan template di templates/broker_summary_template.csv "
            f"(kolom terbaca: {', '.join(map(str, df.columns))})."
        )

    # Tanggal: ISO yyyy-mm-dd, atau konvensi Indonesia dd/mm/yyyy (lihat _parse_dates).
    df["Date"] = _parse_dates(df["Date"])

    # --- Validasi angka: tolak, jangan menebak (PDF hal. 7: K/M/B/T berbeda
    #     arti antar aplikasi; pemisah desimal/ribuan Indonesia vs internasional) ---
    n_rows = max(len(df), 1)
    bad_cells = 0
    bad_examples = []
    for c in NUMERIC_COLUMNS:
        raw = df[c]
        # Pola "1.234" / "500.000" / "1.234.567" = titik sebagai pemisah ribuan
        # (format Indonesia). Volume & nilai broker selalu bilangan bulat, jadi
        # ini hampir pasti ribuan -- tolak, jangan baca sebagai desimal.
        thousands = raw.dropna().astype(str).str.strip().str.fullmatch(r"\d{1,3}(\.\d{3})+")
        if thousands.any():
            example = raw.dropna().astype(str).str.strip()[thousands].iloc[0]
            raise BrokerDataError(
                f"Kolom {c} memakai titik sebagai pemisah ribuan (contoh: '{example}'). Format Indonesia ini "
                f"berisiko terbaca salah ('500.000' menjadi 500, bukan 500000). Ubah ke angka penuh tanpa "
                f"pemisah ribuan (500000)."
            )
        num = pd.to_numeric(raw, errors="coerce")
        bad_mask = num.isna() & raw.notna() & (raw.astype(str).str.strip() != "")
        if bad_mask.any():
            bad_cells += int(bad_mask.sum())
            bad_examples += [f"{c}='{v}'" for v in raw[bad_mask].astype(str).head(2)]
        df[c] = num

    if bad_cells > 0.2 * n_rows:
        raise BrokerDataError(
            f"{bad_cells} sel angka tidak bisa dibaca sebagai angka (contoh: {', '.join(bad_examples[:4])}). "
            f"Isi kolom BuyVol/BuyVal/SellVol/SellVal dengan ANGKA PENUH tanpa singkatan "
            f"(K/M/B/T berbeda arti antar aplikasi) dan tanpa pemisah ribuan."
        )

    for c in NUMERIC_COLUMNS:
        vals = df[c].dropna()
        if len(vals) == 0:
            continue
        if (vals < 0).any():
            raise BrokerDataError(
                f"Ada nilai negatif di kolom {c}. Kolom Buy/Sell harus GROSS (bukan net) -- net dihitung otomatis."
            )
        frac_share = float((vals % 1 != 0).mean())
        if frac_share > 0.05:
            raise BrokerDataError(
                f"Kolom {c} berisi nilai desimal di {frac_share:.0%} baris. Volume (lembar) dan nilai (Rupiah) "
                f"seharusnya bilangan bulat -- kemungkinan titik ribuan format Indonesia (mis. '1.234') terbaca "
                f"sebagai desimal. Ubah ke angka penuh tanpa pemisah ribuan."
            )

    before = len(df)
    df = df.dropna(subset=["Date", "Ticker", "BrokerCode"] + NUMERIC_COLUMNS)
    dropped = before - len(df)
    if dropped:
        raise BrokerDataError(f"{dropped} baris tidak lengkap. Jangan menguji data yang terpotong.")

    df["Date"] = df["Date"].dt.normalize()
    df["Ticker"] = df["Ticker"].astype(str).str.strip().str.upper().str.replace(".JK", "", regex=False)
    df["BrokerCode"] = df["BrokerCode"].astype(str).str.strip().str.upper()

    if not np.isfinite(df[NUMERIC_COLUMNS].to_numpy(dtype=float)).all():
        raise BrokerDataError("Angka nonfinite di file broker")
    df["Market"] = df["Market"].astype(str).str.strip().str.upper()
    if not df["Market"].isin(["RG", "REGULER", "REGULAR"]).all():
        raise BrokerDataError("Hanya Market=RG; pisahkan pasar nego/tunai dan isi market kosong")
    df["Market"] = "RG"
    if df.duplicated(["Date","Ticker","Market","BrokerCode"]).any():
        raise BrokerDataError("Baris broker duplikat; gabungkan dari sumber sebelum analisis")
    df = df.reset_index(drop=True)
    df.attrs["dropped_rows"] = dropped
    return df


def analyze_broker_group(broker_df: pd.DataFrame, ticker: str, trading_dates, daily_value: dict = None):
    """
    trading_dates: list tanggal sesi bursa TERURUT NAIK, minimal
    (BROKER_SELECTION_WINDOW + BROKER_TEST_WINDOW) sesi TERAKHIR hingga
    tanggal sinyal t, diambil dari kalender sesi pada sumber harga yang diverifikasi --
    BUKAN ditebak dari kalender hari kerja biasa.

    daily_value: dict opsional {pd.Timestamp tanggal: nilai transaksi
    reguler}, dipakai sbg penyebut A10. Kalau tidak diberikan, dipakai
    jumlah BuyVal seluruh broker pada data yang diunggah (kurang andal
    kalau data broker tidak lengkap/tidak mencakup semua broker).
    """
    trading_dates = sorted(pd.Timestamp(d).normalize() for d in trading_dates)
    need = sp.BROKER_SELECTION_WINDOW + sp.BROKER_TEST_WINDOW
    if len(trading_dates) < need:
        return {
            "status": "BELUM_TERVERIFIKASI",
            "reason": f"trading_dates kurang dari {need} sesi (ada {len(trading_dates)}).",
        }

    last_n = trading_dates[-need:]
    sel_days = last_n[: sp.BROKER_SELECTION_WINDOW]
    test_days = last_n[sp.BROKER_SELECTION_WINDOW:]
    confirm_days = test_days[-sp.BROKER_CONFIRM_WINDOW:]

    tdf = broker_df[broker_df["Ticker"].astype(str).str.upper() == ticker.upper()].copy()
    if tdf.empty:
        return {
            "status": "BELUM_TERVERIFIKASI",
            "reason": f"Tidak ada data broker untuk {ticker} pada file yang diunggah.",
        }

    tdf["NV"] = tdf["BuyVal"] - tdf["SellVal"]
    tdf["NQ"] = tdf["BuyVol"] - tdf["SellVol"]

    have_sel = tdf[tdf["Date"].isin(sel_days)]["Date"].nunique()
    have_test = tdf[tdf["Date"].isin(test_days)]["Date"].nunique()
    if have_sel < sp.BROKER_SELECTION_WINDOW or have_test < sp.BROKER_TEST_WINDOW:
        return {
            "status": "BELUM_TERVERIFIKASI",
            "reason": (
                f"Cakupan tanggal tidak lengkap di file: {have_sel}/{sp.BROKER_SELECTION_WINDOW} sesi "
                f"window seleksi, {have_test}/{sp.BROKER_TEST_WINDOW} sesi window pengujian ada datanya."
            ),
        }

    if daily_value is None or any(d not in daily_value or not np.isfinite(daily_value[d]) or daily_value[d] <= 0 for d in last_n):
        return {"status":"BELUM_TERVERIFIKASI", "reason":"Wajib RegularValue reguler untuk 20 sesi; Close x Volume tidak cukup."}
    for day in last_n:
        rows = tdf[tdf.Date == day]
        regular = float(daily_value[day])
        if any(abs(float(rows[col].sum())/regular-1)*100 > sp.DATA_MISMATCH_WARN_PCT for col in ["BuyVal","SellVal"]):
            return {"status":"BELUM_TERVERIFIKASI", "reason":f"{day.date()}: cakupan broker tidak cocok RegularValue"}

    # --- Pemilihan kelompok G dari window seleksi (t-19..t-10) ---
    sel = tdf[tdf["Date"].isin(sel_days)]
    by_broker = sel.groupby("BrokerCode")[["NV", "NQ"]].sum()
    qualifying = by_broker[(by_broker["NV"] > 0) & (by_broker["NQ"] > 0)].sort_values("NV", ascending=False)

    if len(qualifying) < sp.BROKER_GROUP_SIZE:
        return {
            "status": "TIDAK_LOLOS",
            "stage": "pemilihan_kelompok",
            "reason": (
                f"Hanya {len(qualifying)} broker dengan NV & NQ positif di window seleksi, "
                f"perlu {sp.BROKER_GROUP_SIZE}."
            ),
        }

    group_g = list(qualifying.index[: sp.BROKER_GROUP_SIZE])

    # --- Pengujian: bekukan G, uji window t-9..t ---
    test = tdf[tdf["Date"].isin(test_days) & tdf["BrokerCode"].isin(group_g)]
    daily_group = test.groupby("Date")[["NV", "NQ"]].sum().reindex(test_days, fill_value=0)

    b10 = float(daily_group["NV"].sum())
    q10 = float(daily_group["NQ"].sum())

    if daily_value is None or any(d not in daily_value or not np.isfinite(daily_value[d]) or daily_value[d] <= 0 for d in last_n):
        return {"status":"BELUM_TERVERIFIKASI", "reason":"Wajib RegularValue reguler terverifikasi untuk 20 sesi; Close x Volume tidak cukup."}
    if daily_value is not None:
        denom = sum(float(daily_value.get(d, 0)) for d in test_days)
        denom_source = "RegularValue eksternal (keaslian wajib diverifikasi)"
    else:
        denom = float(tdf[tdf["Date"].isin(test_days)]["BuyVal"].sum())
        denom_source = "jumlah BuyVal broker ter-upload (kurang andal -- lihat peringatan data)"

    a10 = (b10 / denom * 100) if denom else 0.0

    # --- Pemeriksaan silang kewajaran data (PDF hal. 7, poin 3) ---
    warnings = []
    for d in last_n:
        day_rows = tdf[tdf["Date"] == d]
        buy_total = day_rows["BuyVal"].sum()
        sell_total = day_rows["SellVal"].sum()
        regular = float(daily_value[d])
        if abs(buy_total/regular-1)*100 > sp.DATA_MISMATCH_WARN_PCT or abs(sell_total/regular-1)*100 > sp.DATA_MISMATCH_WARN_PCT:
            warnings.append(f"{d.date()}: gross broker tidak cocok RegularValue; cakupan tidak lengkap")
        if buy_total > 0:
            mismatch_pct = abs(buy_total - sell_total) / buy_total * 100
            if mismatch_pct > sp.DATA_MISMATCH_WARN_PCT:
                warnings.append(
                    f"{d.date()}: selisih gross buy vs sell {mismatch_pct:.0f}% -- "
                    f"data broker kemungkinan tidak lengkap (bukan semua broker ter-input)."
                )

    gate_a10 = (a10 >= sp.MIN_A10_PCT) and (q10 > 0)

    # --- Persistensi: >=6/10 hari NV & NQ kelompok > 0 bersamaan ---
    persistence_days = int(((daily_group["NV"] > 0) & (daily_group["NQ"] > 0)).sum())
    gate_persistence = persistence_days >= sp.MIN_PERSISTENCE_DAYS

    # --- Keberlanjutan: 5 sesi terakhir NV & NQ kelompok > 0 ---
    confirm_slice = daily_group.reindex(confirm_days, fill_value=0)
    nv5 = float(confirm_slice["NV"].sum())
    nq5 = float(confirm_slice["NQ"].sum())
    gate_continuity = (nv5 > 0) and (nq5 > 0)

    passed = gate_a10 and gate_persistence and gate_continuity

    # --- Diagnostik tambahan, BUKAN gerbang wajib (PDF hal. 8) ---
    dk = {}
    for k in (3, 5):
        top_k_buy = sel.groupby("BrokerCode")["BuyVal"].sum().nlargest(k).sum()
        top_k_sell = sel.groupby("BrokerCode")["SellVal"].sum().nlargest(k).sum()
        dk[f"D{k}"] = float(top_k_buy - top_k_sell)

    return {
        "status": "BELUM_TERVERIFIKASI" if warnings else ("LOLOS" if passed else "TIDAK_LOLOS"),
        "reason": "; ".join(warnings),
        "stage": "pengujian_kelompok",
        "group_g": group_g,
        "b10": b10,
        "q10": q10,
        "a10_pct": a10,
        "denom_source": denom_source,
        "gate_a10": gate_a10,
        "persistence_days": persistence_days,
        "gate_persistence": gate_persistence,
        "nv5": nv5,
        "nq5": nq5,
        "gate_continuity": gate_continuity,
        "dk_diagnostic": dk,
        "data_warnings": warnings,
    }
