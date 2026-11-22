"""Πραγματικές συναλλαγές (live) μέσω Jupiter Swap API v2 + Solana RPC.

Ασφάλεια:
- Το κλειδί διαβάζεται ΜΟΝΟ από τη μεταβλητή περιβάλλοντος SOLANA_PRIVATE_KEY (GitHub Secret) και δεν τυπώνεται ποτέ.
- Κρατάμε πάντα ένα απόθεμα SOL για fees (SOL_RESERVE).
- Πουλάμε πάντα το ΠΡΑΓΜΑΤΙΚΟ υπόλοιπο του token όπως το δείχνει το blockchain.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "https://api.jup.ag/swap/v2"
GT = "https://api.geckoterminal.com/api/v2"
RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
SOL_RESERVE = 0.015          # SOL που μένουν πάντα για fees
LAMPORTS = 1_000_000_000


class LiveError(Exception):
    pass


def configured():
    return bool(os.environ.get("SOLANA_PRIVATE_KEY")) and bool(os.environ.get("JUPITER_API_KEY"))


_kp = None

# Η διεύθυνση του Phantom (δημόσια πληροφορία) για να επιλεγεί σωστά το κλειδί από τις 12 λέξεις.
ADDRESS_HINT = os.environ.get("SOLANA_ADDRESS_HINT", "")  # π.χ. "6zAx…gyjn"
PATHS = ["m/44'/501'/0'/0'", "m/44'/501'/0'", "m/44'/501'/0'/0'/0'", "m/44'/501'/1'/0'"]


def _hint_ok(addr):
    if not ADDRESS_HINT:
        return True
    pre, _, suf = ADDRESS_HINT.replace("...", "…").partition("…")
    return addr.startswith(pre) and addr.endswith(suf)


def mnemonic_report():
    """Διαγνωστικά ΧΩΡΙΣ να αποκαλύπτουν τις λέξεις: πλήθος, εγκυρότητα, ποιες λέξεις δεν υπάρχουν στη λίστα BIP39."""
    raw = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    words = raw.split()
    rep = {"words": len(words)}
    if len(words) in (12, 24):
        try:
            from mnemonic import Mnemonic
            m = Mnemonic("english")
            norm = " ".join(w.lower() for w in words)
            rep["checksum_ok"] = m.check(norm)
            rep["unknown_word_positions"] = [i + 1 for i, w in enumerate(norm.split()) if w not in m.wordlist]
        except ImportError:
            pass
        rep["candidates"] = [str(k.pubkey())[:4] + "…" + str(k.pubkey())[-4:] for _, k in _from_words(words)]
    return rep


def _from_words(words):
    import hashlib
    from solders.keypair import Keypair
    seed = hashlib.pbkdf2_hmac("sha512", " ".join(w.lower() for w in words).encode(), b"mnemonic", 2048)
    out = [(p, Keypair.from_seed_and_derivation_path(seed, p)) for p in PATHS]
    out.append(("seed32", Keypair.from_seed(seed[:32])))
    return out


def keypair():
    global _kp
    if _kp is None:
        from solders.keypair import Keypair
        raw = os.environ["SOLANA_PRIVATE_KEY"].strip()
        try:
            words = raw.split()
            if len(words) in (12, 24):  # 12/24 λέξεις του Phantom
                match = [k for _, k in _from_words(words) if _hint_ok(str(k.pubkey()))]
                if not match:
                    raise LiveError("Οι λέξεις δεν δίνουν τη διεύθυνση του Phantom")
                _kp = match[0]
            elif raw.startswith("["):  # μορφή πίνακα αριθμών
                _kp = Keypair.from_bytes(bytes(json.loads(raw)))
            else:                     # μορφή base58
                _kp = Keypair.from_base58_string(raw)
        except LiveError:
            raise
        except Exception:
            raise LiveError("Μη έγκυρο SOLANA_PRIVATE_KEY") from None  # χωρίς λεπτομέρειες για να μη διαρρεύσει
    return _kp


def owner():
    return str(keypair().pubkey())


def _http(url, body=None, headers=None, timeout=25):
    h = {"Accept": "application/json", "User-Agent": "meme-bot/1.0"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        raise LiveError(f"HTTP {e.code} {url.split('?')[0]} {detail}") from None


def rpc(method, params):
    last = None
    for attempt in range(3):
        try:
            r = _http(RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in r:
                raise LiveError(f"RPC {method}: {r['error']}")
            return r["result"]
        except LiveError as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def sol_price():
    """Τιμή SOL σε USD (GeckoTerminal, εφεδρικά DexScreener)."""
    try:
        d = _http(f"{GT}/simple/networks/solana/token_price/{SOL_MINT}")
        p = float(d["data"]["attributes"]["token_prices"][SOL_MINT])
        if p > 0:
            return p
    except Exception:
        pass
    d = _http(f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}")
    for pair in d.get("pairs") or []:
        if (pair.get("baseToken") or {}).get("address") == SOL_MINT and (pair.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT"):
            return float(pair["priceUsd"])
    raise LiveError("Δεν βρέθηκε τιμή SOL")


def sol_balance():
    return rpc("getBalance", [owner(), {"commitment": "confirmed"}])["value"] / LAMPORTS


def token_balance(mint):
    """(raw ποσότητα, decimals) του token στο πορτοφόλι μας."""
    res = rpc("getTokenAccountsByOwner", [owner(), {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    raw, dec = 0, None
    for acc in res.get("value", []):
        info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        raw += int(info["amount"])
        dec = int(info["decimals"])
    return raw, dec


def _sign(tx_b64):
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction
    kp = keypair()
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    n = tx.message.header.num_required_signatures
    signers = list(tx.message.account_keys[:n])
    if kp.pubkey() not in signers:
        raise LiveError("Η συναλλαγή δεν ζητά την υπογραφή μας")
    sigs = list(tx.signatures)
    sigs[signers.index(kp.pubkey())] = kp.sign_message(to_bytes_versioned(tx.message))
    return base64.b64encode(bytes(VersionedTransaction.populate(tx.message, sigs))).decode()


def swap(input_mint, output_mint, amount_raw):
    """Μία ανταλλαγή μέσω Jupiter (/order → υπογραφή → /execute). Επιστρέφει το αποτέλεσμα ή LiveError."""
    hdr = {"x-api-key": os.environ["JUPITER_API_KEY"]}
    q = f"{JUP}/order?inputMint={input_mint}&outputMint={output_mint}&amount={int(amount_raw)}&taker={owner()}"
    order = _http(q, headers=hdr)
    if not order.get("transaction"):
        raise LiveError(f"Jupiter order χωρίς συναλλαγή: {order.get('errorCode')} {order.get('errorMessage', '')}")
    signed = _sign(order["transaction"])
    body = {"signedTransaction": signed, "requestId": order["requestId"]}
    if order.get("lastValidBlockHeight"):
        body["lastValidBlockHeight"] = order["lastValidBlockHeight"]
    res = _http(f"{JUP}/execute", body, headers=hdr, timeout=60)
    if res.get("status") != "Success":
        raise LiveError(f"Jupiter execute απέτυχε: code={res.get('code')} {res.get('error', '')} sig={res.get('signature')}")
    return {
        "signature": res.get("signature"),
        "in": int(res.get("totalInputAmount") or res.get("inputAmountResult") or 0),
        "out": int(res.get("totalOutputAmount") or res.get("outputAmountResult") or 0),
    }


# ---------------- Hooks για το engine ----------------

def buy(p, size_usd):
    """Αγοράζει token αξίας size_usd με SOL. Επιστρέφει dict για το engine ή None αν δεν έγινε."""
    price = sol_price()
    sol_needed = size_usd / price
    if sol_balance() - SOL_RESERVE < sol_needed:
        raise LiveError("Δεν φτάνει το SOL (κρατάμε απόθεμα για fees)")
    r = swap(SOL_MINT, p["base_mint"], round(sol_needed * LAMPORTS))
    raw, dec = 0, None
    for _ in range(6):  # περιμένουμε να φανεί το token στο πορτοφόλι
        raw, dec = token_balance(p["base_mint"])
        if raw > 0 and dec is not None:
            break
        time.sleep(2)
    if dec is None:
        raise LiveError(f"Η αγορά έγινε ({r['signature']}) αλλά δεν φαίνεται το token")
    qty = raw / 10 ** dec
    cost = r["in"] / LAMPORTS * price
    return {"qty": qty, "raw": raw, "decimals": dec, "cost_usd": cost, "sig": r["signature"]}


def sell(pos):
    """Πουλάει ΟΛΟ το πραγματικό υπόλοιπο του token για SOL."""
    raw, dec = token_balance(pos["mint"])
    if raw <= 0:
        return {"proceeds_usd": 0.0, "sig": None, "note": "μηδενικό υπόλοιπο token"}
    price = sol_price()
    r = swap(pos["mint"], SOL_MINT, raw)
    return {"proceeds_usd": r["out"] / LAMPORTS * price, "sig": r["signature"]}


def wallet_snapshot():
    price = sol_price()
    sol = sol_balance()
    return {"sol": sol, "sol_price": price, "sol_usd": sol * price}
