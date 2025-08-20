#!/usr/bin/env python3
import os, sys, json, decimal
from decimal import Decimal
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3.exceptions import ContractLogicError
import requests, time

# ============================= Harga CTXC =============================
# Cache harga gabungan: usd & idr
_PRICE_CACHE = {"usd": None, "idr": None, "ts": 0}

def get_ctxc_prices(ttl_sec: int = 60) -> tuple[Decimal | None, Decimal | None]:
    """
    Ambil harga CTXC ke USD dan IDR via CoinGecko Simple Price API.
    Di-cache selama ttl_sec detik untuk mengurangi rate limit.
    """
    now = time.time()
    if (
        _PRICE_CACHE["usd"] is not None
        and _PRICE_CACHE["idr"] is not None
        and (now - _PRICE_CACHE["ts"]) < ttl_sec
    ):
        return _PRICE_CACHE["usd"], _PRICE_CACHE["idr"]

    url = os.getenv(
        "CTXC_PRICE_API_MULTI",
        "https://api.coingecko.com/api/v3/simple/price?ids=cortex&vs_currencies=usd%2Cidr",
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json().get("cortex", {})
        usd = data.get("usd")
        idr = data.get("idr")
        usd_d = Decimal(str(usd)) if usd is not None else None
        idr_d = Decimal(str(idr)) if idr is not None else None
        _PRICE_CACHE.update(usd=usd_d, idr=idr_d, ts=now)
        return usd_d, idr_d
    except Exception:
        return None, None

# Kompatibilitas untuk kode lain yang mungkin masih memanggil fungsi lama
def get_ctxc_price_usd(ttl_sec: int = 60) -> Decimal | None:
    usd, _ = get_ctxc_prices(ttl_sec)
    return usd

def get_ctxc_price_idr(ttl_sec: int = 60) -> Decimal | None:
    _, idr = get_ctxc_prices(ttl_sec)
    return idr

decimal.getcontext().prec = 50
FAV_FILE = os.path.join(os.path.dirname(__file__), "ctxc_favorites.json")

# ============================ Utils ============================
def to_wei(amount_ctxc: str | float | Decimal) -> int:
    return int(Decimal(str(amount_ctxc)) * Decimal(10) ** 18)

def from_wei(wei_amount: int) -> Decimal:
    return Decimal(wei_amount) / Decimal(10) ** 18

def normalize_rpc(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return "http://" + url
    return url

def load_web3() -> Web3:
    load_dotenv()
    rpc = os.getenv("CTXC_RPC_URL", "security.cortexlabs.ai:30088")
    rpc = normalize_rpc(rpc)
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print(f"ERROR: Gagal konek ke RPC '{rpc}'. Periksa CTXC_RPC_URL / koneksi internet.", file=sys.stderr)
        sys.exit(2)
    return w3

def enforced_chain_id(w3: Web3) -> int:
    env_chain = os.getenv("CTXC_CHAIN_ID")
    try:
        env_chain = int(env_chain) if env_chain else 21
    except Exception:
        env_chain = 21
    try:
        actual = w3.eth.chain_id
        if actual != env_chain:
            print(f"PERINGATAN: chain id node={actual} ≠ CTXC_CHAIN_ID={env_chain}. Menggunakan {actual}.")
            return actual
        return actual
    except Exception:
        return env_chain

def symbol() -> str:
    return os.getenv("CTXC_SYMBOL", "CTXC")

# ======================== Multi Accounts =======================
def load_accounts(w3: Web3) -> dict[str, LocalAccount]:
    """
    Baca semua env CTXC_PK_* sebagai akun.
    Dukung CTXC_PRIVATE_KEY (lama) → nama 'LEGACY'.
    """
    accounts: dict[str, LocalAccount] = {}
    legacy = os.getenv("CTXC_PRIVATE_KEY")
    if legacy:
        try:
            acct = Account.from_key(legacy)
            w3.to_checksum_address(acct.address)
            accounts["LEGACY"] = acct
        except Exception as e:
            print(f"PERINGATAN: CTXC_PRIVATE_KEY tidak valid: {e}", file=sys.stderr)

    for k, v in os.environ.items():
        if not k.startswith("CTXC_PK_"):
            continue
        name = k.replace("CTXC_PK_", "", 1).strip()
        if not name or not v:
            continue
        try:
            acct = Account.from_key(v)
            w3.to_checksum_address(acct.address)
            accounts[name] = acct
        except Exception as e:
            print(f"PERINGATAN: {k} tidak valid: {e}", file=sys.stderr)

    return dict(sorted(accounts.items(), key=lambda x: x[0].lower()))

def get_all_balances(w3: Web3, accounts: dict[str, LocalAccount]) -> dict[str, dict]:
    res = {}
    for name, acct in accounts.items():
        try:
            addr = w3.to_checksum_address(acct.address)
            wei = w3.eth.get_balance(addr)
            res[name] = {"address": addr, "wei": int(wei), "ctxc": from_wei(wei)}
        except Exception as e:
            res[name] = {"address": getattr(acct, "address", "-"), "wei": 0, "ctxc": Decimal(0), "error": str(e)}
    return res

def print_balances_table(balances: dict[str, dict]):
    price_usd, price_idr = get_ctxc_prices()
    print("\nAkun ditemukan di .env:")
    for name, info in balances.items():
        if "error" in info:
            print(f"- {name}: {info['address']}  (ERROR: {info['error']})")
        else:
            line = f"- {name}: {info['address']}  | Saldo: {info['ctxc']:,.7f} {symbol()}"
            try:
                ctxc_amt = Decimal(str(info["ctxc"]))
                parts = []
                if price_idr is not None:
                    idr_value = ctxc_amt * price_idr
                    parts.append(f"Rp {idr_value:,.0f}")
                if price_usd is not None:
                    usd_value = ctxc_amt * price_usd
                    parts.append(f"USD {usd_value:,.2f}")
                if parts:
                    line += " (≈ " + " | ".join(parts) + ")"
            except Exception:
                pass
            print(line)

    if price_usd is not None or price_idr is not None:
        ringkas = [f"\nHarga 1 {symbol()} ≈"]
        if price_idr is not None:
            ringkas.append(f"\033[92mRp {price_idr:,.0f}\033[0m")
        if price_usd is not None:
            ringkas.append(f"| \033[92mUSD {price_usd:,.4f}\033[0m")
        print(" ".join(ringkas) + " (sumber: CoinGecko)")
    else:
        print("\n[Gagal ambil harga; cek koneksi atau set CTXC_PRICE_API_MULTI]")

def pick_account(w3: Web3, accounts: dict[str, LocalAccount]) -> tuple[str, LocalAccount] | None:
    if not accounts:
        print("ERROR: Tidak ada akun di .env (CTXC_PK_* atau CTXC_PRIVATE_KEY).")
        return None
    names = list(accounts.keys())
    print("\n=========================== PILIH AKUN ===========================")
    for i, n in enumerate(names, start=1):
        print(f"{i}) {n}  →  {w3.to_checksum_address(accounts[n].address)}")
    raw = input("Pilih nomor / nama (Enter batal): ").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(names):
            n = names[idx]
            return n, accounts[n]
    if raw in accounts:
        return raw, accounts[raw]
    print("Pilihan tidak valid.")
    return None

# ========================== Favorites ==========================
def fav_load() -> dict:
    if not os.path.exists(FAV_FILE):
        return {}
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def fav_save(data: dict) -> None:
    with open(FAV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def fav_list(w3: Web3) -> None:
    favs = fav_load()
    if not favs:
        print("\nBelum ada alamat favorit.\n"); return
    print("\n========================= DAFTAR FAVORIT =========================")
    for i, (name, addr) in enumerate(favs.items(), start=1):
        try:
            print(f"{i}. {name}: {w3.to_checksum_address(addr)}")
        except Exception:
            print(f"{i}. {name}: {addr} (alamat tidak valid)")
    print()

def fav_add(w3: Web3) -> None:
    print("\n========================= TAMBAH FAVORIT =========================")
    name = input("Nama panggilan: ").strip()
    if not name:
        print("Nama tidak boleh kosong."); return
    addr = input("Alamat 0x...: ").strip()
    try:
        checksum = w3.to_checksum_address(addr)
    except Exception:
        print("Alamat tidak valid."); return
    favs = fav_load()
    if name in favs:
        y = input("Nama sudah ada. Timpa? ketik 'YA' untuk lanjut: ").strip().lower()
        if y != "ya":
            print("Dibatalkan."); return
    favs[name] = checksum
    fav_save(favs)
    print(f"Disimpan: {name} → {checksum}\n")

def fav_remove() -> None:
    favs = fav_load()
    if not favs:
        print("\nTidak ada favorit untuk dihapus.\n"); return
    print("\n========================== HAPUS FAVORIT =========================")
    names = list(favs.keys())
    for i, n in enumerate(names, start=1):
        print(f"{i}) {n}")
    choice = input("Pilih nomor / nama (Enter batal): ").strip()
    if not choice:
        print("Dibatalkan.\n"); return
    target = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(names):
            target = names[idx]
    elif choice in favs:
        target = choice
    if not target:
        print("Pilihan tidak valid.\n"); return
    del favs[target]
    fav_save(favs)
    print(f"Favorit '{target}' dihapus.\n")

def fav_pick(w3: Web3) -> str | None:
    favs = fav_load()
    if not favs:
        print("Belum ada favorit.")
        return None
    names = list(favs.keys())
    print("\n========================== PILIH FAVORIT =========================")
    for i, n in enumerate(names, start=1):
        print(f"{i}) {n}  →  {favs[n]}")
    raw = input("Pilih nomor / nama (Enter batal): ").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(names):
            return Web3.to_checksum_address(favs[names[idx]])
    elif raw in favs:
        return Web3.to_checksum_address(favs[raw])
    print("Pilihan tidak valid.")
    return None

# ========================== Transaksi ==========================
def build_tx(w3: Web3, from_addr: str, to_addr: str, value_wei: int, nonce: int, chain_id: int, tip_gwei: Decimal | None):
    """Transfer CTXC, gas 21000, EIP-1559 jika ada, fallback legacy."""
    GAS_LIMIT_TRANSFER = 21000
    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")
    except Exception:
        base_fee = None
    if tip_gwei is None:
        try:
            priority = w3.eth.max_priority_fee
        except Exception:
            priority = w3.to_wei(2, "gwei")
    else:
        priority = w3.to_wei(tip_gwei, "gwei")

    if base_fee is None:
        try:
            gas_price = w3.eth.gas_price
        except Exception:
            gas_price = w3.to_wei(2, "gwei")
        tx = {
            "from": from_addr, "to": to_addr, "value": value_wei, "nonce": nonce,
            "gas": GAS_LIMIT_TRANSFER, "gasPrice": int(gas_price), "chainId": chain_id,
        }
        fee_model = "legacy"
    else:
        max_fee = int(base_fee) * 2 + int(priority)
        tx = {
            "from": from_addr, "to": to_addr, "value": value_wei, "nonce": nonce,
            "gas": GAS_LIMIT_TRANSFER, "maxFeePerGas": int(max_fee),
            "maxPriorityFeePerGas": int(priority), "chainId": chain_id,
        }
        fee_model = "eip1559"
    return tx, fee_model

def send_ctxc(w3: Web3, acct: LocalAccount, chain_id: int):
    print("\n=========================== KIRIM CTXC ===========================")
    from_addr = w3.to_checksum_address(acct.address)
    try:
        bal_wei = w3.eth.get_balance(from_addr)
        bal_ctxc = from_wei(bal_wei)
        bal_str = f"{bal_ctxc:.8f}".rstrip("0").rstrip(".")
        saldo_prompt = f" [\033[92mSaldo: {bal_str} {symbol()}\033[0m]"
    except Exception:
        saldo_prompt = ""

    print("Pilih tujuan:")
    print("1) Dari favorit")
    print("2) Input manual")
    choice = input("Pilih [1/2]: ").strip() or "2"
    if choice == "1":
        picked = fav_pick(w3)
        if not picked:
            print("Tidak ada yang dipilih."); return
        to_addr = picked
    else:
        to_addr = input("Alamat tujuan 0x...: ").strip()
        try:
            to_addr = w3.to_checksum_address(to_addr)
        except Exception:
            print("Alamat tujuan tidak valid."); return

    amount_str = input(f"Nominal {symbol()} (misal 1.0){saldo_prompt}: ").strip()
    try:
        value_wei = to_wei(amount_str)
    except Exception:
        print("Nominal tidak valid."); return

    tip_in = input("Priority fee (gwei) [kosong = auto]: ").strip()
    tip_gwei = None
    if tip_in:
        try:
            tip_gwei = Decimal(tip_in)
        except Exception:
            print("Priority fee tidak valid, gunakan otomatis."); tip_gwei = None

    nonce = w3.eth.get_transaction_count(from_addr)
    tx, fee_model = build_tx(w3, from_addr, to_addr, value_wei, nonce, chain_id, tip_gwei)

    if "gasPrice" in tx:
        est_fee = tx["gas"] * tx["gasPrice"]
        gas_price_gwei = w3.from_wei(tx["gasPrice"], "gwei")
    else:
        est_fee = tx["gas"] * tx["maxFeePerGas"]
        tip_gwei_show = w3.from_wei(tx["maxPriorityFeePerGas"], "gwei")

    balance = w3.eth.get_balance(from_addr)
    total_needed = value_wei + est_fee

    print("\n============================ RINGKASAN ===========================")
    print(f"Dari            : {from_addr}")
    print(f"Ke              : {to_addr}")
    print(f"Jumlah          : {from_wei(value_wei)} {symbol()}")
    print(f"Gas Limit       : {tx['gas']}")
    if "gasPrice" in tx:
        print(f"Fee Model       : legacy")
        print(f"Gas Price       : {gas_price_gwei} gwei")
    else:
        print(f"Fee Model       : eip1559")
        print(f"Priority Fee    : {tip_gwei_show} gwei")
    print(f"Perkiraan Fee   : {from_wei(est_fee)} {symbol()}")
    print(f"Saldo saat ini  : {from_wei(balance)} {symbol()}")
    print(f"Total dibutuhkan: {from_wei(total_needed)} {symbol()}")

    if balance < total_needed:
        print("\nERROR: Saldo tidak cukup untuk nilai + fee estimasi.\n")
        return

    y = input("\nKirim transaksi? ketik 'YA' untuk lanjut: ").strip().lower()
    if y != "ya":
        print("Dibatalkan."); return

    try:
        signed = w3.eth.account.sign_transaction(tx, private_key=acct.key)
        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw is None:
            raise RuntimeError("Tidak menemukan raw tx pada SignedTransaction (cek versi web3.py).")
        tx_hash = w3.eth.send_raw_transaction(raw)
        print("\n=== Transaksi dikirim! ===")
        print(f"Chain ID  : {chain_id}")
        print(f"Tx Hash   : https://cerebro.cortexlabs.ai/tx/0x{tx_hash.hex()}")
        print("Tempel hash ke block explorer Cortex (jika tersedia).")
    except (ValueError, ContractLogicError, RuntimeError) as e:
        print(f"ERROR kirim transaksi: {e}")

# ============================ Menu ============================
def menu_favorites(w3: Web3):
    while True:
        print("\n============================= FAVORIT ============================")
        print("1) List favorit")
        print("2) Tambah favorit")
        print("3) Hapus favorit")
        print("b) Kembali")
        c = input("Pilih: ").strip().lower()
        if c == "1":
            fav_list(w3)
        elif c == "2":
            fav_add(w3)
        elif c == "3":
            fav_remove()
        elif c in ("b", "back"):
            print(); return
        else:
            print("Opsi tidak dikenali.")

def main():
    w3 = load_web3()
    chain_id = enforced_chain_id(w3)
    accounts = load_accounts(w3)
    balances = get_all_balances(w3, accounts)
    print_balances_table(balances)
    active_name, active_acct = None, None
    if accounts:
        picked = pick_account(w3, accounts)
        if picked:
            active_name, active_acct = picked
        else:
            print("Dibatalkan."); return
    else:
        print("\nPERINGATAN: Tidak ada akun di .env. (Masih bisa kelola favorit.)")

    print("==================================================================")
    print(" CTXC TOOL (Send, Favorites, Multi-Account)")
    print("==================================================================")
    print(f"Node          : {os.getenv('CTXC_RPC_URL', 'security.cortexlabs.ai:30088')}")
    try:
        actual_chain = w3.eth.chain_id
        print(f"Chain ID node : {actual_chain}")
    except Exception:
        print(f"Target Chain  : {chain_id}")
    if active_acct:
        print(f"Akun aktif    : {active_name} ({w3.to_checksum_address(active_acct.address)})")
    else:
        print("Akun aktif     : -")
    print("------------------------------------------------------------------")
    while True:
        print(f"1) Kirim {symbol()}")
        print("2) Favorit (list/tambah/hapus)")
        print("3) Ganti akun aktif")
        print("q) Keluar")
        choice = input("Pilih opsi: ").strip().lower()

        if choice == "1":
            if not active_acct:
                print("ERROR: Tidak ada akun aktif. Tambahkan CTXC_PK_* di .env dan pilih akun."); continue
            send_ctxc(w3, active_acct, chain_id)
        elif choice == "2":
            menu_favorites(w3)
        elif choice == "3":
            picked = pick_account(w3, accounts)
            if picked:
                active_name, active_acct = picked
                print(f"Akun aktif diganti ke: {active_name} ({w3.to_checksum_address(active_acct.address)})")
            else:
                print("Batal mengganti akun.")
        elif choice in ("q", "quit", "exit"):
            print("Selesai."); break
        else:
            print("Opsi tidak dikenali.\n")

if __name__ == "__main__":
    main()
