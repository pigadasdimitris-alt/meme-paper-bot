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


RAW = {}
DEXES = ["raydium", "pumpswap", "meteora", "meteora-damm-v2", "raydium-clmm", "orca", "meteora-dbc"]
DEX_PAGES = int(os.environ.get("BT_DEX_PAGES", "5"))
MIN_STD5M = 0.4   # % — κάτω από αυτό δεν είναι meme coin (SOL, BTC, μετοχές σε token, staked SOL…)


def universe():
    pools = []
    for dur, page in C.TRENDING_SOURCES:
        got = data.trending_pools(dur, page); time.sleep(1)
        RAW[f"trend_{dur}_{page}"] = len(got); pools += got
    for page in range(1, C.TOP_VOLUME_PAGES + 1):
        got = data.top_volume_pools(page); time.sleep(1)
        RAW[f"vol_{page}"] = len(got); pools += got
    for page in range(1, C.TOP_TX_PAGES + 1):
        got = data.top_volume_pools(page, sort="h24_tx_count_desc"); time.sleep(1)
        RAW[f"tx_{page}"] = len(got); pools += got
    for dex in DEXES:
        for page in range(1, DEX_PAGES + 1):
            dd = data._get(f"{data.GT}/networks/solana/dexes/{dex}/pools?page={page}&sort=h24_volume_usd_desc")
            time.sleep(2.1)
            got = [data.parse_gt_pool(x) for x in (dd or {}).get("data", [])]
            RAW[f"{dex}_{page}"] = len(got); pools += got
    RAW["total"] = len(pools)
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
    return out


def vol(cs):
    r = [cs[i][4] / cs[i - 1][4] - 1 for i in range(1, len(cs)) if cs[i - 1][4] > 0]
    return round(100 * statistics.pstdev(r), 2) if len(r) > 2 else None


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
    out = [rows[k] for k in sorted(rows) if k >= since]
    # προστασία από λάθη δεδομένων (όπως το MAX_JUMP του bot): φυτίλι >4x από το προηγούμενο κλείσιμο αγνοείται
    for i in range(1, len(out)):
        pc = out[i - 1][4]
        if pc > 0:
            o, h, l, c = out[i][1:5]
            if h > C.MAX_JUMP * pc:
                out[i][2] = max(o, c) if max(o, c) <= C.MAX_JUMP * pc else pc
            if l < pc / C.MAX_JUMP:
                out[i][3] = min(o, c) if min(o, c) >= pc / C.MAX_JUMP else pc
            if not (pc / C.MAX_JUMP <= c <= C.MAX_JUMP * pc):
                out[i][1] = out[i][4] = out[i][2] = out[i][3] = pc
    return out


# ---------- σήματα εισόδου (ίδια όρια με το config.LAB, όσο το επιτρέπουν τα κεριά) ----------

def params(key):
    base = {k: getattr(C, k) for k in dir(C) if k.isupper()}
    base.update({**getattr(C, "LAB_RETIRED", {}), **C.LAB}.get(key, {}))
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


def load_series(now, since):
    cache = os.environ.get("BT_CACHE")
    if cache and os.path.exists(cache):
        with open(cache) as f:
            d = json.load(f)
        return {k: (v[0], v[1]) for k, v in d.items()}
    uni = universe()[:MAX_POOLS]
    print(f"σύμπαν: {len(uni)} pools", flush=True)
    series = {}
    for n, p in enumerate(uni, 1):
        cs = candles(p["pool"], since)
        if len(cs) >= 50 and (vol(cs) or 0) >= MIN_STD5M:
            series[p["pool"]] = (p, cs)
        elif len(cs) >= 50:
            RAW.setdefault("low_vol", []).append(p["name"])
        if n % 10 == 0:
            print(f"κεριά: {n}/{len(uni)} ({len(series)} με δεδομένα)", flush=True)
    if cache:
        with open(cache, "w") as f:
            json.dump({k: [m, c] for k, (m, c) in series.items()}, f)
    return series


def main():
    now = time.time()
    since = int(now - DAYS * 86400) - 12 * STEP
    series = load_series(now, since)
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
    out["universe"] = [{"name": m["name"], "liq": round(m["liquidity"]), "fdv": round(m["fdv"]),
                        "created": m["created_at"], "candles": len(c), "std5m_pct": vol(c)}
                       for m, c in series.values()]
    out["universe_raw"] = len(uni)
    out["fetched_raw"] = RAW
    signal_counts = {}
    for key in ["scalp", "trail", "dip", "spike"]:
        P = params(key)
        tr = run_strategy(key, P, series)
        signal_counts[key] = len(tr)
        out["results"][key] = summarize(tr, t_mid)
        out["results"][key]["sample"] = [{**t, "move": round(t["move"], 2)} for t in tr[:15]]
        out["results"][key]["top5"] = sorted(({**t, "move": round(t["move"], 2)} for t in tr),
                                             key=lambda t: -t["move"])[:5]
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
