"""
utils.py -- fungsi bantu umum: pembulatan fraksi harga BEI, batas auto
rejection bawah (ARB), escaping HTML untuk Telegram, format Rupiah, dan
logger bersama.
"""
import logging
from datetime import date
from math import floor, ceil

import numpy as np

logging.basicConfig(
    filename="screener.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("idx_screener")


def safe_float(x, default=np.nan):
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def idx_tick(price: float) -> float:
    """Grid inferred from reference price. Use prior close/reference for the
    entry session, then recheck the broker's daily grid for future orders.
    See docs/RISET.md for sources and the 2016 five-band introduction.
    """
    if price < 200:
        return 1.0
    if price < 500:
        return 2.0
    if price < 2_000:
        return 5.0
    if price < 5_000:
        return 10.0
    return 25.0


def round_down_idx(price: float, reference_price=None) -> float:
    tick = idx_tick(price if reference_price is None else reference_price)
    return floor(price / tick) * tick


def round_up_idx(price: float, reference_price=None) -> float:
    tick = idx_tick(price if reference_price is None else reference_price)
    return ceil(price / tick) * tick


def get_arb_ceiling_pct(price: float, as_of: date = None):
    """
    Batas persentase Auto Rejection Bawah (ARB) BEI yang berlaku, menurut
    tanggal. BEI mengubah aturan ini bertahap mulai 28 Sept 2026 (SK
    Direksi BEI No. Kep-00136/BEI/09-2026) dan akan berubah LAGI mulai
    1 Jan 2027 (ARB kembali simetris dengan ARA per rentang harga).

    PERINGATAN: aturan ARA/ARB BEI terbukti berubah beberapa kali dalam
    tahun 2026 saja. Jangan percaya fungsi ini secara membabi-buta --
    cross-check ke pengumuman resmi BEI (idx.co.id) secara berkala,
    terutama menjelang & sesudah 1 Januari 2027.

    Mengembalikan None jika harga di band Rp1-Rp10 (batas di band itu
    NOMINAL Rp1, bukan persentase) -- di luar cakupan screener ini karena
    MIN_PRICE sudah menyaring harga >= Rp200.
    """
    if as_of is None:
        as_of = date.today()

    if price <= 10:
        return None

    if as_of >= date(2027, 1, 1):
        # ARB kembali mengikuti rentang harga seperti ARA (berlaku 1 Jan 2027)
        if price <= 200:
            return 35.0
        if price <= 5_000:
            return 25.0
        return 20.0

    if as_of >= date(2026, 9, 28):
        # Masa transisi 28 Sept - 31 Des 2026: ARB flat 15% utk saham > Rp10
        return 15.0

    if as_of >= date(2025, 4, 8):
        return 15.0
    # Older rules are deliberately unknown here; never use this to move a stop.
    return None


def escape_html(text) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def fmt_rp(value, decimals: int = 0) -> str:
    """Format angka ke Rupiah dengan konvensi Indonesia (titik ribuan,
    koma desimal), misal 1234567.5 -> 'Rp 1.234.568' (decimals=0) atau
    'Rp 1.234.567,5' (decimals=1)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "Rp -"
    if not np.isfinite(value):
        return "Rp -"

    if decimals == 0:
        s = f"{value:,.0f}"
        return "Rp " + s.replace(",", ".")

    s = f"{value:,.{decimals}f}"
    int_part, _, dec_part = s.partition(".")
    int_part = int_part.replace(",", ".")
    return f"Rp {int_part},{dec_part}"
