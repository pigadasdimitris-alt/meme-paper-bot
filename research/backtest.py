"""Backtest των meme στρατηγικών πάνω σε ιστορικά κεριά 5 λεπτών (GeckoTerminal). Μόνο ανάγνωση, κανένα χρήμα.

Προσεγγίσεις (να τις θυμόμαστε όταν διαβάζουμε τα αποτελέσματα):
- Σύμπαν = pools που ΥΠΑΡΧΟΥΝ σήμερα (top όγκου/συναλλαγών/trending). Όσα έκαναν rug έχουν συνήθως χαθεί
  → το backtest είναι πιο αισιόδοξο από την πραγματικότητα (survivorship bias).
- Τα κεριά δεν έχουν αγοραστές/αγορές/πωλήσεις → τα φίλτρα buyers & buy/sell ratio ΔΕΝ εφαρμόζονται.
- Ρευστότητα & FDV = σημερινές τιμές (όχι της στιγμής του σήματος).
- Είσοδος στο open του επόμενου κεριού. Αν στο ίδιο κερί πιάνονται και TP και SL, μετράμε SL (συντηρητικά).
- Κάθε trade αξιολογείται μόνο του (10$), χωρίς όριο ανοιχτών θέσεων.
"""
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import config as C  # noqa: E402
from bot import data  # noqa: E402

DAYS = int(os.environ.get("BT_DAYS", "10"))
MAX_POOLS = int(os.environ.get("BT_MAX_POOLS", "150"))
STEP = 300  # 5 λεπτά
COSTS = [0.025, 0.01, 0.005]  # slippage ανά πλευρά: τωρινό paper / live / αισιόδοξο
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "backtest.json")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def universe():
    pools = []
    for dur, page in C.TRENDING_SOURCES:
        pools += data.trending_pools(dur, page)
    for page in range(1, C.TOP_VOLUME_PAGES + 1):
        pools += data.top_volume_pools(page)
    for page in range(1, C.TOP_TX_PAGES + 1):
        pools += data.top_volume_pools(page, sort="h24_tx_count_desc")
    out, seen_mint = [], set()
    for p in sorted(pools, key=lambda x: -x["liquidity"]):
        if not p["pool"] or p["base_mint"] in C.QUOTE_MINTS or p["base_mint"] in seen_mint:
            continue
        if p.get("quote_mint") and p["quote_mint"] not in C.QUOTE_MINTS:
            continue
        if not p["created_at"] or p["liquidity"] < C.MIN_LIQUIDITY_USD:
            continue
        if not (C.MIN_FDV_USD <= p["fdv"] <= C.MAX_FDV_USD):
            continue
        seen_mint.add(p["base_mint"])
        out.append(p)
    return out[:MAX_POOLS]


def candles(pool, since):
    """Κεριά 5' [ts, o, h, l, c, v] σε αύξουσα σειρά, από `since` ως τώρα."""
    rows, before = {}, None
    for _ in range(6):
        url = (f"{data.GT}/networks/solana/pools/{pool}/ohlcv/minute?aggregate=5&limit=1000"
               f"&currency=usd&token=base")
        if before:
            url += f"&before_timestamp={before}"
        d = data._get(url)
        time.sleep(2.1)
        lst = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not lst:
            break
        for r in lst:
            rows[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
        before = min(int(r[0]) for r in lst)
        if before <= since or len(lst) < 1000:
            break
    return [rows[k] for k in sorted(rows) if k >= since]


# ---------- σήματα εισόδου (ίδια όρια με το config.LAB, όσο το επιτρέπουν τα κεριά) ----------

def params(key):
    base = {k: getattr(C, k) for k in dir(C) if k.isupper()}
    base.update(C.LAB.get(key, {}))
    return base


def features(cs, i):
    """Δείκτες τη στιγμή του κλεισίματος του κεριού i (χρειάζονται 12 προηγούμενα)."""
    o, c, v = cs[i][1], cs[i][4], cs[i][5]
    c12 = cs[i - 12][4]
    vol_h1 = sum(x[5] for x in cs[i - 11:i + 1])
    return {
        "pc_m5": 100 * (c / o - 1) if o > 0 else 0.0,
        "pc_h1": 100 * (c / c12 - 1) if c12 > 0 else 0.0,
        "vol_m5": v,
        "vol_h1": vol_h1,
    }


def signal(P, f):
    if f["vol_m5"] < P["MIN_VOLUME_M5_USD"]:
        return False
    spike = P.get("MIN_VOL_SPIKE", 0.0)
    if spike and f["vol_m5"] < spike * max(f["vol_h1"] / 12.0, 1.0):
        return False
    if f["pc_m5"] < P["MIN_PRICE_CHANGE_M5"] or f["pc_h1"] < P.get("MIN_PRICE_CHANGE_H1", -1e9):
        return False
    if f["pc_m5"] > P["MAX_PRICE_CHANGE_M5"] or f["pc_h1"] > P["MAX_PRICE_CHANGE_H1"]:
        return False
    return True


def simulate_exit(P, cs, j):
    """Μπαίνει στο open του κεριού j. Επιστρέφει (index εξόδου, % κίνηση αγοράς, λόγος)."""
    entry = cs[j][1]
    if entry <= 0:
        return None
    tp, stop = P["TAKE_PROFIT_PCT"], P["STOP_LOSS_PCT"]
    be, trail = P["BREAKEVEN_TRIGGER_PCT"], P.get("TRAIL_PCT")
    max_k = max(1, int(P["MAX_HOLD_MIN"] // 5))
    peak = 0.0
    for k in range(j, min(len(cs), j + max_k)):
        hi = 100 * (cs[k][2] / entry - 1)
        lo = 100 * (cs[k][3] / entry - 1)
        if lo <= stop:  # πρώτα το stop (συντηρητικά)· αν το κερί άνοιξε ήδη κάτω από το stop, βγαίνουμε στο open
            op = 100 * (cs[k][1] / entry - 1)
            return k, min(stop, op), "stop" if stop < 0 else ("trail" if stop > 0 else "stop 0")
        if hi >= tp:
            return k, tp, "tp"
        peak = max(peak, hi)
        if peak >= be:
            stop = max(stop, 0.0)
        if trail and peak >= trail:
            stop = max(stop, peak - trail)
    k = min(len(cs), j + max_k) - 1
    return k, 100 * (cs[k][4] / entry - 1), "χρόνος"


def net_pct(move_pct, slip):
    """Καθαρό % σε 10$ θέση μετά από slippage και fees (ίδιος τύπος με το engine)."""
    size = C.POSITION_SIZE_USD
    qty = size / (1 + slip)
    proceeds = qty * (1 + move_pct / 100) * (1 - slip) - C.FEE_USD_PER_TX
    cost = size + C.FEE_USD_PER_TX
    return 100 * (max(proceeds, 0) - cost) / cost


def run_strategy(key, P, series, rng=None, rand_rate=0.0):
    trades = []
    for pool, (meta, cs) in series.items():
        created = ts(meta["created_at"])
        busy_until, cool_until = -1, 0
        for i in range(12, len(cs) - 1):
            t = cs[i][0] + STEP
            if i <= busy_until or t < cool_until:
                continue
            age_min = (t - created) / 60
            if age_min < P["MIN_AGE_MIN"] or age_min > P["MAX_AGE_MIN"]:
                continue
            if rng is not None:
                if rng.random() >= rand_rate:
                    continue
            elif not signal(P, features(cs, i)):
                continue
            r = simulate_exit(P, cs, i + 1)
            if not r:
                continue
            k, move, why = r
            trades.append({"pool": pool, "name": meta["name"], "t": t, "move": move, "why": why,
                           "hold": (k - i) * 5})
            busy_until = k
            cool_until = cs[k][0] + STEP + (24 * 3600 if move < 0 and why == "stop" else C.REENTRY_HOURS * 3600)
    return trades


def summarize(trades, t_mid):
    res = {"trades": len(trades)}
    if not trades:
        return res
    res["win_rate_market"] = round(100 * sum(t["move"] > 0 for t in trades) / len(trades), 1)
    res["exits"] = {}
    for t in trades:
        res["exits"][t["why"]] = res["exits"].get(t["why"], 0) + 1
    res["by_cost"] = {}
    for slip in COSTS:
        nets = [net_pct(t["move"], slip) for t in trades]
        first = [n for n, t in zip(nets, trades) if t["t"] < t_mid]
        second = [n for n, t in zip(nets, trades) if t["t"] >= t_mid]
        res["by_cost"][f"{slip * 100:g}%"] = {
            "avg_net_pct": round(statistics.mean(nets), 2),
            "median_net_pct": round(statistics.median(nets), 2),
            "wins_pct": round(100 * sum(n > 0 for n in nets) / len(nets), 1),
            "pnl_usd_10_per_trade": round(sum(nets) / 100 * C.POSITION_SIZE_USD, 2),
            "avg_first_half": round(statistics.mean(first), 2) if first else None,
            "avg_second_half": round(statistics.mean(second), 2) if second else None,
        }
    res["distinct_coins"] = len({t["pool"] for t in trades})
    return res


def main():
    now = time.time()
    since = int(now - DAYS * 86400) - 12 * STEP
    uni = universe()
    print(f"σύμπαν: {len(uni)} pools", flush=True)
    series = {}
    for n, p in enumerate(uni, 1):
        cs = candles(p["pool"], since)
        if len(cs) >= 50:
            series[p["pool"]] = (p, cs)
        if n % 10 == 0:
            print(f"κεριά: {n}/{len(uni)} ({len(series)} με δεδομένα)", flush=True)
    t_mid = since + (now - since) / 2
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": DAYS, "pools": len(series), "candles": sum(len(c) for _, c in series.values()),
        "notes": ["survivorship bias: τα rugs λείπουν → αισιόδοξο",
                  "χωρίς φίλτρα αγοραστών/ratio (δεν υπάρχουν στα κεριά)",
                  "TP+SL στο ίδιο κερί = SL"],
        "costs_per_side": [f"{c * 100:g}%" for c in COSTS],
        "results": {},
    }
    signal_counts = {}
    for key in ["scalp", "trail", "dip", "spike"]:
        P = params(key)
        tr = run_strategy(key, P, series)
        signal_counts[key] = len(tr)
        out["results"][key] = summarize(tr, t_mid)
        print(key, json.dumps(out["results"][key], ensure_ascii=False), flush=True)
    # Βάση σύγκρισης: τυχαίες είσοδοι με τους ίδιους κανόνες εξόδου και περίπου ίδιο πλήθος trades
    total_slots = sum(len(c) for _, c in series.values()) or 1
    for key in ["scalp", "trail", "dip", "spike"]:
        P = params(key)
        rate = max(signal_counts[key], 30) / total_slots
        tr = run_strategy(key, P, series, rng=random.Random(42), rand_rate=rate)
        out["results"][key + "_random"] = summarize(tr, t_mid)
        print(key + "_random", json.dumps(out["results"][key + "_random"], ensure_ascii=False), flush=True)
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
