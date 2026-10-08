"""
config.py -- data PERSONAL & RAHASIA. JANGAN pernah di-commit ke git (sudah
masuk .gitignore). Salin file ini:

    cp config.example.py config.py

lalu isi config.py dengan nilai Anda sendiri.

Parameter STRATEGI (bukan rahasia) ada di strategy_params.py, sengaja
dipisah dan TIDAK di-gitignore -- supaya setiap perubahan parameter
strategi tercatat di riwayat git, sesuai prinsip riset v1.0:
"Parameter dapat direvisi hanya melalui proses evaluasi yang tercatat."
"""

# --- Wajib: dari @BotFather saat membuat bot Telegram ---
TELEGRAM_BOT_TOKEN = "ISI_TOKEN_DARI_BOTFATHER"

# --- Wajib: chat_id Anda sendiri (atau grup) tempat bot mengirim pesan ---
# Cara cepat dapatkan: chat /start ke bot Anda, lalu buka
#   https://api.telegram.org/bot<TOKEN>/getUpdates
# dan cari "chat":{"id": ...}
TELEGRAM_CHAT_ID = "ISI_CHAT_ID_ANDA"

# --- Wajib: modal trading Anda dalam Rupiah ---
# Dipakai untuk menghitung ukuran posisi (RiskBudget, PositionValueCap).
# Ini SENGAJA di config.py (bukan strategy_params.py) karena ini data
# personal, bukan parameter strategi.
ACCOUNT_EQUITY = 100_000_000

# --- Opsional: override universe saham ---
# Kosongkan (biarkan []) untuk memakai idx_tickers.txt sebagai gantinya.
IDX_TICKERS = []
