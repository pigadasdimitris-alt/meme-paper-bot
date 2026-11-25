"""Στρατηγικές σε BTC / ETH / SOL (paper, 100$ η καθεμία).

trend   — Trend following: κρατάει ένα coin όσο ο γρήγορος μέσος (EMA 50 ωρών) είναι πάνω από τον αργό (EMA 200 ωρών).
          Το πιο τεκμηριωμένο είδος στρατηγικής στην ακαδημαϊκή έρευνα για τα crypto.
funding — Funding arbitrage: για κάθε coin, μισό ποσό αγορά (spot) και μισό short στα perpetual futures.
          Η τιμή δεν μας επηρεάζει· εισπράττουμε (ή πληρώνουμε) το ωριαίο funding του Hyperliquid.
"""
import json
import time
import urllib.request

from . import engine

COINS = {
    "BTC": {"cb": "BTC-USD", "okx": "BTC-USDT-SWAP",
            "image": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png"},
    "ETH": {"cb": "ETH-USD", "okx": "ETH-USDT-SWAP",
            "image": "https://assets.coingecko.com/coins/images/279/large/ethereum.png"},
    "SOL": {"cb": "SOL-USD", "okx": "SOL-USDT-SWAP",
            "image": "https://assets.coingecko.com/coins/images/4128/large/solana.png"},
}
START = 100.0
SLOT = START / len(COINS)

# κόστη (συντηρητικά, όπως σε κανονικό ανταλλακτήριο)
TREND_FEE = 0.001      # 0,1% ανά πλευρά
TREND_SLIP = 0.0005    # 0,05% ανά πλευρά
FAST, SLOW = 50, 200
SPOT_FEE = 0.001       # funding: αγορά spot
PERP_FEE = 0.00045     # funding: short perp (taker Hyperliquid)

TREND_EPOCH = "2026-11-25-trend"
FUND_EPOCH = "2026-11-25-funding"


def _http(url, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "majors-bot",
                                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _try(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            time.sleep(2 * (i + 1))
    return None


# ---------------- δεδομένα ----------------

_CANDLES = {}


def closes(coin):
    """Ωριαίες τιμές κλεισίματος (παλιότερη πρώτη) από Coinbase. Ανανέωση κάθε 10'."""
    hit = _CANDLES.get(coin)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    raw = _try(lambda: _http(f"https://api.exchange.coinbase.com/products/{COINS[coin]['cb']}/candles?granularity=3600"))
    if not raw:
        return hit[1] if hit else None
    rows = sorted(raw, key=lambda r: r[0])
    out = [(int(r[0]), float(r[4])) for r in rows]
    _CANDLES[coin] = (time.time(), out)
    return out


def ema(values, n):
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def funding_rates():
    """Ωριαίο funding ανά coin. Πρώτα Hyperliquid (ωριαίο), αλλιώς OKX (8ωρο / 8)."""
    d = _try(lambda: _http("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"}))
    out = {}
    if d and isinstance(d, list) and len(d) == 2:
        for meta, ctx in zip(d[0].get("universe", []), d[1]):
            if meta.get("name") in COINS:
                out[meta["name"]] = (float(ctx.get("funding", 0) or 0), "hyperliquid")
    for coin in COINS:
        if coin not in out:
            r = _try(lambda: _http(f"https://www.okx.com/api/v5/public/funding-rate?instId={COINS[coin]['okx']}"))
            try:
                out[coin] = (float(r["data"][0]["fundingRate"]) / 8.0, "okx")
            except Exception:
                pass
    return out


def spot_price(coin):
    c = closes(coin)
    return c[-1][1] if c else None


# ---------------- κοινά ----------------

def new_state(now, epoch, mode):
    st = engine.new_state(now)
    st.update({"epoch": epoch, "mode": mode, "start_balance": START, "balance": START})
    return st


def load(path, now, epoch, mode):
    if path.exists():
        st = json.loads(path.read_text())
        if st.get("epoch") == epoch:
            return st
    return new_state(now, epoch, mode)


def equity(st):
    return st["balance"] + sum(p["value"] for p in st["positions"])


def snap(st, now):
    st["equity_history"].append([engine.iso(now), round(equity(st), 4)])
    st["equity_history"] = st["equity_history"][-5000:]
    st["last_run"] = engine.iso(now)


# ---------------- TREND ----------------

def trend_signal(series):
    vals = [c for _, c in series]
    if len(vals) < SLOW + 5:
        return None
    return ema(vals, FAST) > ema(vals, SLOW)


def trend_cycle(st, now=None, closes_fn=closes):
    now = now or engine.now_utc()
    for coin, info in COINS.items():
        series = closes_fn(coin)
        if not series:
            continue
        price = series[-1][1]
        up = trend_signal(series[:-1] + [series[-1]])
        pos = next((p for p in st["positions"] if p["coin"] == coin), None)
        if pos:
            pos["last_price"] = price
            pos["value"] = round(pos["qty"] * price * (1 - TREND_SLIP - TREND_FEE), 4)
            pos["last_pct"] = round((pos["value"] / pos["cost"] - 1) * 100, 2)
            pos["peak_pct"] = max(pos["peak_pct"], pos["last_pct"])
            if up is False:   # η τάση γύρισε: πουλάμε
                st["positions"].remove(pos)
                st["balance"] += pos["value"]
                t = dict(pos)
                t.update({"exit_time": engine.iso(now), "exit_market": price, "proceeds": pos["value"],
                          "pnl": round(pos["value"] - pos["cost"], 4), "pnl_pct": pos["last_pct"],
                          "hold_min": round((now - engine.parse_time(pos["entry_time"])).total_seconds() / 60, 1),
                          "reason": "η τάση γύρισε"})
                st["closed"].append(t)
                engine.log(st, now, f"ΠΩΛΗΣΗ {coin} — η τάση γύρισε — {t['pnl']:+.2f}$")
        elif up and st["balance"] >= SLOT - 0.01:
            cost = min(SLOT, st["balance"])
            fill = price * (1 + TREND_SLIP)
            qty = cost * (1 - TREND_FEE) / fill
            st["balance"] -= cost
            p = {"id": st["next_id"], "coin": coin, "name": f"{coin} / USD", "image": info["image"],
                 "qty": qty, "cost": cost, "entry_market": price, "entry_time": engine.iso(now),
                 "last_price": price, "value": round(qty * price * (1 - TREND_SLIP - TREND_FEE), 4),
                 "last_pct": 0.0, "peak_pct": 0.0, "stop_pct": -100.0, "pool": coin, "mint": coin}
            p["last_pct"] = round((p["value"] / cost - 1) * 100, 2)
            st["next_id"] += 1
            st["positions"].append(p)
            engine.log(st, now, f"ΑΓΟΡΑ #{p['id']} {coin} @ ${price:,.2f} (${cost:.2f}) — ανοδική τάση")
    st["stats"]["scans"] += 1
    snap(st, now)


# ---------------- FUNDING ----------------

def funding_cycle(st, now=None, rates_fn=funding_rates, price_fn=spot_price):
    now = now or engine.now_utc()
    if not st["positions"]:   # άνοιγμα των 3 αντισταθμισμένων θέσεων (μία φορά)
        for coin, info in COINS.items():
            price = price_fn(coin)
            if not price:
                return
        for coin, info in COINS.items():
            price = price_fn(coin)
            half = SLOT / 2
            fees = half * SPOT_FEE + half * PERP_FEE
            st["balance"] -= SLOT
            st["positions"].append({
                "id": st["next_id"], "coin": coin, "name": f"{coin} hedge", "image": info["image"],
                "qty": 1.0, "cost": SLOT, "value": round(SLOT - fees, 6), "notional": half,
                "entry_market": price, "entry_time": engine.iso(now), "last_price": price,
                "last_pct": round(-fees / SLOT * 100, 3), "peak_pct": 0.0, "stop_pct": 0.0,
                "funding_earned": 0.0, "pool": coin, "mint": coin})
            st["next_id"] += 1
        st["funding_hour"] = int(now.timestamp() // 3600)
        engine.log(st, now, "Άνοιγμα: BTC, ETH, SOL — μισό spot, μισό short perp (χωρίς ρίσκο τιμής)")
    hour = int(now.timestamp() // 3600)
    due = hour - st.get("funding_hour", hour)
    if due > 0:
        rates = rates_fn()
        if rates and all(c in rates for c in COINS):
            total = 0.0
            for p in st["positions"]:
                rate, src = rates[p["coin"]]
                gain = p["notional"] * rate * min(due, 6)   # το short εισπράττει όταν το funding είναι θετικό
                p["value"] = round(p["value"] + gain, 6)
                p["funding_earned"] = round(p.get("funding_earned", 0.0) + gain, 6)
                p["last_pct"] = round((p["value"] / p["cost"] - 1) * 100, 3)
                p["peak_pct"] = max(p["peak_pct"], p["last_pct"])
                p["last_rate"] = rate
                total += gain
            st["funding_hour"] = hour
            engine.log(st, now, "Funding: " + ", ".join(
                f"{p['coin']} {rates[p['coin']][0] * 100:+.4f}%/ώρα" for p in st["positions"]) + f" → {total:+.4f}$")
    for p in st["positions"]:
        price = price_fn(p["coin"])
        if price:
            p["last_price"] = price
    st["stats"]["scans"] += 1
    snap(st, now)
