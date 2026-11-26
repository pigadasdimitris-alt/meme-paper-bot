"""Volatility Compression Breakout (paper, 100$) — η στρατηγική που πέρασε το walk-forward backtest (research/quant_bt.py).

Κανόνες (ίδιοι με το backtest, παράμετροι του holdout: sq=0.10, vm=1.5, BTC regime):
- Κεριά 4 ωρών, 11 coins (BTC ETH SOL BNB XRP DOGE ADA AVAX LINK LTC DOT), τιμές Hyperliquid.
- «Συμπίεση»: το εύρος Bollinger (20, 2σ) είναι στο χαμηλότερο 10% των τελευταίων 180 ημερών (μέσα στα 6 τελευταία κεριά).
- Είσοδος: το κερί κλείνει πάνω από το υψηλό των 20 προηγούμενων κεριών (long) ή κάτω από το χαμηλό (short),
  με όγκο > 1,5× του μέσου 20 κεριών. Long μόνο όταν το BTC είναι πάνω από τον μέσο 300 κεριών (50 μέρες), short μόνο κάτω.
- Stop 1,5×ATR(14). Trailing 3×ATR από το καλύτερο κλείσιμο. Μέγιστο κράτημα 30 κεριά (5 μέρες).
- Ρίσκο 1% ανά trade, μόχλευση έως 3x, έως 3 θέσεις ταυτόχρονα.
- Κόστος: fee 0,045% + slippage 0,03% (BTC/ETH) ή 0,10% ανά πλευρά, και το ωριαίο funding του Hyperliquid.
"""
import json
import time
import urllib.request

from . import engine, majors

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT"]
HL = "https://api.hyperliquid.xyz/info"
BAR = 4 * 3600
SQ, VM, LOOK, RANK_N, RANK_MIN = 0.10, 1.5, 20, 1080, 540
STOP_ATR, TRAIL_ATR, MAX_BARS = 1.5, 3.0, 30
RISK, MAX_LEV, MAX_OPEN = 0.01, 3.0, 3
FEE = 0.00045
EPOCH = "2026-11-26-compress"


def slip(coin):
    return 0.0003 if coin in ("BTC", "ETH") else 0.0010


def _post(body):
    req = urllib.request.Request(HL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def hl_candles(coin, now_s):
    """Ολοκληρωμένα κεριά 4ω: λίστα (t_open_s, o, h, l, c, v), παλιότερο πρώτο."""
    d = _post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "4h",
                                                 "startTime": int((now_s - 1300 * BAR) * 1000),
                                                 "endTime": int(now_s * 1000)}})
    if not d:
        return None
    out = [(int(x["t"]) // 1000, float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), float(x["v"]))
           for x in d if int(x["T"]) // 1000 < now_s]      # μόνο κλεισμένα κεριά
    return sorted(out)


def hl_ctx():
    """{coin: (mid, ωριαίο funding)}"""
    d = _post({"type": "metaAndAssetCtxs"})
    if not d:
        return None
    names = [u["name"] for u in d[0]["universe"]]
    return {n: (float(c.get("midPx") or c.get("markPx")), float(c.get("funding") or 0))
            for n, c in zip(names, d[1]) if n in COINS}


# ---------------- δείκτες (χωρίς pandas, για να τρέχει παντού) ----------------

def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def atr(bars, i, n=14):
    trs = []
    for k in range(i - n + 1, i + 1):
        _, o, h, l, c, v = bars[k]
        pc = bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return _mean(trs)


def bbw(bars, i):
    cl = [b[4] for b in bars[i - 19:i + 1]]
    return 4 * _std(cl) / _mean(cl)


def squeeze_recent(bars, i):
    """Η συμπίεση εμφανίστηκε σε κάποιο από τα 6 τελευταία κεριά (όπως rolling(6).max στο backtest)."""
    for j in range(i - 5, i + 1):
        lo = max(20, j - RANK_N + 1)
        if j - lo + 1 < RANK_MIN:
            continue
        hist = [bbw(bars, k) for k in range(lo, j + 1)]
        cur = hist[-1]
        rank = sum(1 for x in hist if x <= cur) / len(hist)
        if rank < SQ:
            return True
    return False


def signal(bars, i, btc_up):
    """Σήμα στο κλείσιμο του κεριού i: +1 long, −1 short, 0 τίποτα. Επιστρέφει (side, atr)."""
    if i < RANK_MIN + 30:
        return 0, None
    c, v = bars[i][4], bars[i][5]
    hi = max(b[2] for b in bars[i - LOOK:i])
    lo = min(b[3] for b in bars[i - LOOK:i])
    if v <= VM * _mean([b[5] for b in bars[i - 19:i + 1]]):
        return 0, None
    if not (c > hi or c < lo) or not squeeze_recent(bars, i):
        return 0, None
    a = atr(bars, i)
    if c > hi and btc_up:
        return 1, a
    if c < lo and not btc_up:
        return -1, a
    return 0, None


def btc_regime(btc_bars):
    cl = [b[4] for b in btc_bars[-300:]]
    return len(cl) == 300 and cl[-1] > _mean(cl)


# ---------------- paper ----------------

def _close(st, pos, price, reason, now):
    side = pos["side"]
    gross = side * (price - pos["entry"]) / pos["entry"] * pos["notional"]
    fees = (FEE + slip(pos["coin"])) * pos["notional"] * (1 + price / pos["entry"])
    pnl = gross - fees - pos["funding_paid"]
    st["balance"] += pnl
    st["positions"].remove(pos)
    st["closed"].append({
        "id": pos["id"], "name": pos["name"], "coin": pos["coin"], "entry_time": pos["entry_time"],
        "exit_time": engine.iso(now), "entry_market": pos["entry"], "exit_market": price,
        "hold_min": round((now - engine.parse_time(pos["entry_time"])).total_seconds() / 60, 1),
        "pnl": round(pnl, 4), "pnl_pct": round(100 * pnl / pos["equity_at_entry"], 2),
        "r_multiple": round(pnl / pos["risk_usd"], 2), "fees": round(fees, 4),
        "funding": round(pos["funding_paid"], 4), "reason": reason})
    engine.log(st, now, f"ΚΛΕΙΣΙΜΟ {pos['name']} — {reason} — {pnl:+.2f}$")


def cycle(st, now=None, candles_fn=hl_candles, ctx_fn=hl_ctx):
    now = now or engine.now_utc()
    now_s = int(now.timestamp())
    ctx = ctx_fn()
    if not ctx:
        raise RuntimeError("δεν ήρθαν τιμές από το Hyperliquid")
    # 1) funding κάθε ώρα για τις ανοιχτές θέσεις (long πληρώνει θετικό funding)
    hour = now_s // 3600
    if st.get("funding_hour") != hour:
        for p in st["positions"]:
            rate = ctx.get(p["coin"], (0, 0))[1]
            p["funding_paid"] += p["side"] * rate * p["notional"]
        st["funding_hour"] = hour
    # 2) stops με την τρέχουσα τιμή
    for p in list(st["positions"]):
        px = ctx.get(p["coin"], (None,))[0]
        if px is None:
            continue
        if (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"]):
            _close(st, p, px, "stop" if p["moved"] == 0 else "trailing stop", now)
    # 3) νέο κλεισμένο κερί 4ω → trailing, λήξη χρόνου, νέες είσοδοι
    bar_close = now_s // BAR * BAR
    if st.get("last_bar") != bar_close:
        bars = {c: candles_fn(c, now_s) for c in COINS}
        if not bars.get("BTC"):
            raise RuntimeError("δεν ήρθαν κεριά BTC")
        st["last_bar"] = bar_close
        btc_up = btc_regime(bars["BTC"])
        st["btc_regime"] = "ανοδικό" if btc_up else "καθοδικό"
        for p in list(st["positions"]):
            b = bars.get(p["coin"])
            if not b:
                continue
            i = len(b) - 1
            last_c = b[i][4]
            p["bars"] += 1
            p["best"] = max(p["best"], last_c) if p["side"] > 0 else min(p["best"], last_c)
            new_stop = p["best"] - p["side"] * TRAIL_ATR * atr(b, i)
            if (p["side"] > 0 and new_stop > p["stop"]) or (p["side"] < 0 and new_stop < p["stop"]):
                p["stop"], p["moved"] = new_stop, p["moved"] + 1
            if p["bars"] >= MAX_BARS:
                _close(st, p, ctx[p["coin"]][0], "λήξη χρόνου (5 μέρες)", now)
        squeezes = []
        for coin in COINS:
            b = bars.get(coin)
            if not b or len(b) < RANK_MIN + 40 or len(st["positions"]) >= MAX_OPEN:
                continue
            if any(p["coin"] == coin for p in st["positions"]):
                continue
            side, a = signal(b, len(b) - 1, btc_up)
            if not side:
                continue
            px = ctx[coin][0]
            stop = px - side * STOP_ATR * a
            dist = abs(px - stop) / px
            eq = majors.equity(st)
            notional = min(RISK * eq / dist, MAX_LEV * eq)
            pos = {"id": st["next_id"], "coin": coin, "name": f"{coin} {'αγορά' if side > 0 else 'short'}",
                   "side": side, "entry": px, "stop": stop, "best": px, "notional": notional,
                   "risk_usd": notional * dist, "equity_at_entry": eq, "entry_time": engine.iso(now),
                   "bars": 0, "moved": 0, "funding_paid": 0.0, "value": 0.0, "image": None}
            st["next_id"] += 1
            st["positions"].append(pos)
            engine.log(st, now, f"ΑΝΟΙΓΜΑ {pos['name']} @ {px:.6g} · stop {stop:.6g} · "
                                f"θέση {notional:.2f}$ · ρίσκο {pos['risk_usd']:.2f}$")
            squeezes.append(coin)
        st["stats"]["bars"] = st["stats"].get("bars", 0) + 1
    # 4) αποτίμηση
    for p in st["positions"]:
        px = ctx.get(p["coin"], (p["entry"],))[0]
        gross = p["side"] * (px - p["entry"]) / p["entry"] * p["notional"]
        p["last_price"] = px
        p["last_pct"] = round(100 * p["side"] * (px / p["entry"] - 1), 2)
        p["value"] = round(gross - (FEE + slip(p["coin"])) * p["notional"] * 2 - p["funding_paid"], 4)
    st["stats"]["scans"] = st["stats"].get("scans", 0) + 1
    majors.snap(st, now)
