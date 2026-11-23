"""Δεύτερο paper bot: αντιγράφει "έξυπνα πορτοφόλια".

1. ΑΝΑΚΑΛΥΨΗ: σε coins που ανεβαίνουν δυνατά, βρίσκει πορτοφόλια που αγόρασαν πριν την άνοδο
   (από τα δημόσια trades του GeckoTerminal). Όσα το κάνουν σε πολλά coins παίρνουν βαθμό.
2. ΠΑΡΑΚΟΛΟΥΘΗΣΗ: κάθε γύρο κοιτάει τις νέες συναλλαγές των καλύτερων πορτοφολιών (δημόσιο Solana RPC).
3. ΣΗΜΑ: όταν COPY_MIN_WALLETS διαφορετικά πορτοφόλια αγοράσουν το ίδιο coin μέσα σε COPY_WINDOW_MIN λεπτά,
   περνάει τους ίδιους ελέγχους ασφαλείας (ρευστότητα, rugcheck, contract) και αγοράζει εικονικά.
4. ΕΞΟΔΟΣ: ίδιοι κανόνες με το κύριο bot (TP/SL/χρόνος/rug) + έξοδος όταν πουλήσουν τα πορτοφόλια.
5. ΜΑΘΗΣΗ: πορτοφόλια που μας έβαλαν σε χαμένα trades χάνουν βαθμό και βγαίνουν από τη λίστα.
"""
import json
import os
import time
import urllib.request

from . import config as C
from . import data, engine

RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
COPY_EPOCH = "2026-11-23-copy-100-v2"

MAX_WALLETS = 25            # πόσα πορτοφόλια παρακολουθούμε
MIN_WALLET_SCORE = 1
COPY_MIN_WALLETS = 2        # πόσα διαφορετικά πορτοφόλια πρέπει να αγοράσουν το ίδιο coin
COPY_WINDOW_MIN = 15
COPY_MIN_BUY_USD = 150      # αγνοούμε "ψίχουλα"
COPY_MIN_AGE_MIN = 60       # όχι coins μικρότερα της 1 ώρας
COPY_MAX_PC_M5 = 30.0       # όχι κυνήγι: αν ήδη +30% στα 5', το αφήνουμε
DISCOVER_EVERY = 10         # κάθε πόσους γύρους ψάχνουμε νέα πορτοφόλια
DISCOVER_POOLS = 3
EARLY_GAIN = 1.3            # αγόρασε σε τιμή που μετά ανέβηκε τουλάχιστον +30%
BOT_TRADES = 25             # πορτοφόλια με τόσα trades στο ίδιο pool είναι bots/arbitrage
MAX_TX_FETCH = 40           # όριο κλήσεων getTransaction ανά γύρο (δημόσιο RPC)
STABLE = set(C.QUOTE_MINTS)


# ---------------- RPC ----------------

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json",
                                                                        "User-Agent": "meme-copy-bot"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                if "429" in str(d["error"]) or "rate" in str(d["error"]).lower():
                    time.sleep(3 * (attempt + 1))
                    continue
                return None
            return d.get("result")
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def token_moves(tx, wallet):
    """Πόσο άλλαξε κάθε token του πορτοφολιού σε μια συναλλαγή: {mint: delta}."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return {}
    bal = {}
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(key) or []:
            if b.get("owner") != wallet:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
            bal[b.get("mint")] = bal.get(b.get("mint"), 0.0) + sign * float(amt)
    return {m: d for m, d in bal.items() if m and abs(d) > 0}


# ---------------- Κατάσταση ----------------

def new_copy_state(now):
    st = engine.new_state(now)
    st.update({"epoch": COPY_EPOCH, "mode": "copy", "wallets": {}, "buys": {}, "sells": {}})
    return st


def load(path, now):
    if path.exists():
        st = json.loads(path.read_text())
        if st.get("epoch") == COPY_EPOCH:
            return st
    return new_copy_state(now)


# ---------------- 1. Ανακάλυψη πορτοφολιών ----------------

def pool_trades(pool):
    d = data._get(f"{data.GT}/networks/solana/pools/{pool}/trades", retries=2)
    time.sleep(1.5)
    return [t.get("attributes", {}) or {} for t in (d or {}).get("data", [])]


def early_winners(trades, base_mint, price_now):
    """Πορτοφόλια που αγόρασαν το coin σε τιμή τουλάχιστον EARLY_GAIN φορές χαμηλότερη από τη σημερινή."""
    count = {}
    for t in trades:
        w = t.get("tx_from_address")
        count[w] = count.get(w, 0) + 1
    out = set()
    for t in trades:
        w = t.get("tx_from_address")
        if not w or count.get(w, 0) >= BOT_TRADES or t.get("kind") != "buy":
            continue
        if t.get("to_token_address") != base_mint:
            continue
        price = data._f(t.get("price_to_in_usd"))
        if price > 0 and data._f(t.get("volume_in_usd")) >= COPY_MIN_BUY_USD and price_now >= EARLY_GAIN * price:
            out.add(w)
    return out


def discover(st, pools, now):
    gain = lambda p: max(p.get("pc_h1", 0), p.get("pc_h6", 0), p.get("pc_h24", 0))
    def old_enough(p):
        try:
            return (now - engine.parse_time(p["created_at"])).total_seconds() >= 6 * 3600
        except Exception:
            return False
    # μόνο coins που αντέχουν (ρευστότητα + ηλικία 6+ ωρών), για να βρούμε traders και όχι snipers μικρών coins
    hot = [p for p in pools if gain(p) >= 50 and p.get("liquidity", 0) >= 2 * C.MIN_LIQUIDITY_USD
           and old_enough(p) and p.get("base_mint") not in STABLE and p.get("quote_mint") in STABLE]
    hot.sort(key=gain, reverse=True)
    done = set(st.setdefault("discovered_pools", {}))
    wallets = st["wallets"]
    found = 0
    picked = [h for h in hot if h["pool"] not in done][:DISCOVER_POOLS]
    for p in picked:
        st["discovered_pools"][p["pool"]] = engine.iso(now)
        trades = pool_trades(p["pool"])
        winners = early_winners(trades, p["base_mint"], p["price"])
        found += len(winners)
        for w in winners:
            info = wallets.setdefault(w, {"score": 0, "coins": [], "added": engine.iso(now), "last_sig": None})
            if p["base_mint"] not in info["coins"]:
                info["coins"] = (info["coins"] + [p["base_mint"]])[-20:]
                info["score"] += 1
    if picked:
        engine.log(st, now, f"Ανακάλυψη: {len(picked)} coins σε άνοδο ({', '.join(p['name'] for p in picked)}) "
                            f"→ {found} πορτοφόλια που αγόρασαν νωρίς")
    # κρατάμε μικρό το αρχείο
    cutoff = now.timestamp() - 48 * 3600
    st["discovered_pools"] = {k: v for k, v in st["discovered_pools"].items()
                              if engine.parse_time(v).timestamp() > cutoff}
    ranked = sorted(wallets.items(), key=lambda kv: (kv[1]["score"], kv[1]["added"]), reverse=True)
    st["wallets"] = dict(ranked[:200])


def tracked(st):
    ranked = sorted(((w, i) for w, i in st["wallets"].items() if i["score"] >= MIN_WALLET_SCORE),
                    key=lambda kv: (kv[1]["score"], kv[1]["added"]), reverse=True)
    return [w for w, _ in ranked[:MAX_WALLETS]]


# ---------------- 2. Παρακολούθηση ----------------

def watch(st, now, fetch_budget=MAX_TX_FETCH):
    budget = fetch_budget
    for w in tracked(st):
        info = st["wallets"][w]
        params = {"limit": 10}
        if info.get("last_sig"):
            params["until"] = info["last_sig"]
        sigs = rpc("getSignaturesForAddress", [w, params]) or []
        time.sleep(0.25)
        if not sigs:
            continue
        first_time = info.get("last_sig") is None
        info["last_sig"] = sigs[0]["signature"]
        if first_time:   # πρώτη φορά: μόνο σημάδι εκκίνησης, όχι παλιές αγορές
            continue
        for s in sigs:
            if budget <= 0:
                return
            if s.get("err"):
                continue
            tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                          "maxSupportedTransactionVersion": 0}])
            budget -= 1
            time.sleep(0.25)
            for mint, delta in token_moves(tx, w).items():
                if mint in STABLE:
                    continue
                book = st["buys"] if delta > 0 else st["sells"]
                book.setdefault(mint, {})[w] = engine.iso(now)


def prune_books(st, now):
    cutoff = now.timestamp() - 6 * 3600
    for key in ("buys", "sells"):
        st[key] = {m: {w: t for w, t in ws.items() if engine.parse_time(t).timestamp() > cutoff}
                   for m, ws in st[key].items()}
        st[key] = {m: ws for m, ws in st[key].items() if ws}


# ---------------- 3. Σήμα και αγορά ----------------

def signals(st, now):
    out = []
    win = now.timestamp() - COPY_WINDOW_MIN * 60
    for mint, ws in st["buys"].items():
        recent = [w for w, t in ws.items() if engine.parse_time(t).timestamp() >= win]
        if len(recent) >= COPY_MIN_WALLETS:
            out.append((mint, recent))
    out.sort(key=lambda x: len(x[1]), reverse=True)
    return out


def best_pool(mint):
    d = data._get(f"{data.DS}/tokens/{mint}", retries=2)
    pairs = [p for p in (d or {}).get("pairs", []) or []
             if p.get("chainId") == "solana" and (p.get("baseToken") or {}).get("address") == mint
             and (p.get("quoteToken") or {}).get("address") in STABLE]
    if not pairs:
        return None
    pairs.sort(key=lambda p: data._f((p.get("liquidity") or {}).get("usd")), reverse=True)
    got = data.fetch_pools([pairs[0]["pairAddress"]])
    return got[0] if got else None


def copy_reject(p, now):
    if not p or p["price"] <= 0:
        return "δεν βρέθηκε pool"
    if p["liquidity"] < C.MIN_LIQUIDITY_USD:
        return "χαμηλή ρευστότητα"
    if p.get("created_at"):
        age = (now - engine.parse_time(p["created_at"])).total_seconds() / 60
        if age < COPY_MIN_AGE_MIN:
            return "πολύ καινούργιο (<1 ώρα)"
    if p.get("pc_m5", 0) > COPY_MAX_PC_M5:
        return "ήδη εκτοξευμένο"
    if engine.symbol(p["name"]) in (set(engine.SHARED_RUGS) | engine.SEED_RUG_NAMES):
        return "όνομα που έκανε rug"
    return None


def enter(st, now, flags_fn=data.token_flags, rug_fn=data.rugcheck, pool_fn=best_pool):
    reasons = st.setdefault("reasons", {})
    held = {p["mint"] for p in st["positions"]}
    for mint, ws in signals(st, now):
        if mint in held or mint in st["seen"]:
            continue
        blocked = engine.can_open(st)
        if blocked:
            return
        st["seen"][mint] = engine.iso(now)   # ένα σήμα ανά coin για REENTRY_HOURS
        p = pool_fn(mint)
        why = copy_reject(p, now)
        if not why:
            f = flags_fn(mint) or {}
            if f.get("mint_authority") or f.get("freeze_authority"):
                why = "ύποπτο contract"
            elif f.get("image"):
                p = {**p, "image": f["image"]}
        if not why and getattr(C, "RUGCHECK", False):
            why = engine.rug_reason(rug_fn(mint, p["pool"]))
        name = p["name"] if p else mint[:6]
        if why:
            reasons[mint] = [why, engine.iso(now), name, len(ws)]
            continue
        pos = engine.open_position(st, p, now)
        if pos:
            pos["wallets"] = ws
            held.add(mint)
            reasons[mint] = ["ΑΓΟΡΑ", engine.iso(now), name, len(ws)]
            engine.log(st, now, f"   ↳ σήμα από {len(ws)} πορτοφόλια")


# ---------------- 4. Έξοδος όταν πουλάνε τα πορτοφόλια ----------------

def follow_sells(st, quotes, now):
    for pos in list(st["positions"]):
        sold = [w for w, t in st["sells"].get(pos["mint"], {}).items()
                if w in pos.get("wallets", []) and t >= pos["entry_time"]]
        if len(sold) >= min(2, len(pos.get("wallets", [])) or 1):
            q = quotes.get(pos["pool"])
            if q:
                engine.close_position(st, pos, q["price"], now, "πούλησαν τα πορτοφόλια",
                                      q.get("liquidity") if q.get("src") == "gt" else None)


# ---------------- 5. Μάθηση ----------------

def learn(st, before):
    for t in st["closed"][before:]:
        pos_wallets = t.get("wallets") or []
        for w in pos_wallets:
            if w in st["wallets"]:
                st["wallets"][w]["score"] += 1 if t.get("pnl", 0) > 0 else -1
    st["wallets"] = {w: i for w, i in st["wallets"].items() if i["score"] > -2}


# ---------------- Ένας γύρος ----------------

def cycle(st, pools):
    now = engine.now_utc()
    engine.roll_day(st, now)
    before = len(st["closed"])
    if st["positions"]:
        quotes = data.pools_now(st["positions"])
        confirm = lambda pos: (data.ds_price(pos["pool"], pos["mint"])
                               if quotes.get(pos["pool"], {}).get("src") == "gt" else None)
        follow_sells(st, quotes, engine.now_utc())
        engine.manage_positions(st, quotes, engine.now_utc(), confirm_fn=confirm)
    learn(st, before)
    st["stats"]["scans"] += 1
    if st["stats"]["scans"] % DISCOVER_EVERY == 1 or not st["wallets"]:
        discover(st, pools, now)
    watch(st, engine.now_utc())
    now = engine.now_utc()
    prune_books(st, now)
    enter(st, now)
    cutoff = now.timestamp() - getattr(C, "REENTRY_HOURS", 6) * 3600
    st["seen"] = {k: v for k, v in st["seen"].items() if engine.parse_time(v).timestamp() > cutoff}
    st["reasons"] = {k: v for k, v in st.get("reasons", {}).items()
                     if engine.parse_time(v[1]).timestamp() > now.timestamp() - 12 * 3600}
    st["stats"]["tracked_wallets"] = len(tracked(st))
    st["stats"]["known_wallets"] = len(st["wallets"])
    engine.snapshot(st, engine.now_utc())
