"""Backtest δύο ιδεών σε 13 perps Binance (1h, 2021–2026, μαζί LUNA/FTT για survivorship):
A) crash_dip: απότομη πτώση 4 ωρών (z-score σε σχέση με τη μεταβλητότητα 30 ημερών) → long για H ώρες.
B) funding_extreme: funding 8h ≥ thr → short (εισπράττει funding), ≤ −thr → long· για H ώρες.
Walk-forward (12μ train / 3μ test, επιλογή παραμέτρων μόνο από το παρελθόν), τυχαίες είσοδοι ίδιας διάρκειας,
διπλά κόστη, χαρτοφυλάκιο έως 3 θέσεις (1/3 κεφαλαίου η καθεμία, χωρίς μόχλευση).
Χρήση: python3 research/edge_bt.py <φάκελος δεδομένων> → docs/edge_bt.json
"""
import gzip
import json
import random
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "LUNA", "FTT"]
FEE = 0.00045


def slip(sym):
    return 0.0003 if sym in ("BTC", "ETH") else 0.0010


def load(d, sym):
    k = pd.read_csv(f"{d}/klines_{sym}USDT.csv.gz")
    k["ts"] = k["ts"].astype("int64")
    k = k[k["ts"] < 10 ** 11].sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    try:
        f = pd.read_csv(f"{d}/funding_{sym}USDT.csv.gz")
        f = f[f["ts"] < 10 ** 11].sort_values("ts")
    except FileNotFoundError:
        f = pd.DataFrame({"ts": [], "rate": []})
    return k, f


def funding_paid(f_ts, f_rate, t0, t1, side):
    """Ό,τι πληρώνει (+) ή εισπράττει (−) η θέση σε funding μεταξύ t0 και t1 (κλάσμα του notional)."""
    a, b = np.searchsorted(f_ts, t0, "right"), np.searchsorted(f_ts, t1, "right")
    return side * float(f_rate[a:b].sum())


def trades_crash(sym, k, f, z_thr, hold, cost_mult=1.0):
    c, o, ts = k["close"].values, k["open"].values, k["ts"].values
    r1 = np.diff(np.log(c), prepend=np.nan)
    sd = pd.Series(r1).rolling(720, min_periods=500).std().values
    r4 = np.log(c / np.roll(c, 4))
    z = r4 / (sd * 2)
    f_ts, f_rate = f["ts"].values, f["rate"].values
    out, i, n = [], 724, len(c)
    while i < n - hold - 1:
        if z[i] <= z_thr:
            e, x = o[i + 1], c[i + hold]
            cost = 2 * (FEE + slip(sym) * 1.5) * cost_mult
            ret = x / e - 1 - cost - funding_paid(f_ts, f_rate, ts[i + 1], ts[i + hold], +1)
            out.append((int(ts[i + 1]), int(ts[i + hold]), sym, float(ret)))
            i += max(hold, 24)
        else:
            i += 1
    return out


def trades_funding(sym, k, f, thr, hold, cost_mult=1.0):
    o, c, ts = k["open"].values, k["close"].values, k["ts"].values
    f_ts, f_rate = f["ts"].values, f["rate"].values
    out, busy = [], 0
    for t, r in zip(f_ts, f_rate):
        if t < busy or abs(r) < thr:
            continue
        side = -1 if r > 0 else 1                      # θετικό funding → short
        i = np.searchsorted(ts, t, "left")
        if i + hold >= len(ts) or i == 0:
            continue
        e, x = o[i], c[i + hold - 1]
        cost = 2 * (FEE + slip(sym)) * cost_mult
        ret = side * (x / e - 1) - cost - funding_paid(f_ts, f_rate, ts[i], ts[i + hold - 1], side)
        out.append((int(ts[i]), int(ts[i + hold - 1]), sym, float(ret)))
        busy = ts[i + hold - 1]
    return out


def random_baseline(data, n_trades, hold, side=1, seed=1):
    rnd, out = random.Random(seed), []
    syms = list(data)
    for _ in range(n_trades):
        s = rnd.choice(syms)
        k, f = data[s]
        if len(k) < hold + 800:
            continue
        i = rnd.randrange(724, len(k) - hold - 1)
        e, x = k["open"].values[i + 1], k["close"].values[i + hold]
        ret = side * (x / e - 1) - 2 * (FEE + slip(s))
        out.append((int(k["ts"].values[i + 1]), int(k["ts"].values[i + hold]), s, float(ret)))
    return out


def portfolio(trades, slots=3):
    """Έως `slots` θέσεις, η καθεμία 1/slots της αξίας τη στιγμή της εισόδου."""
    eq, open_, curve = 1.0, [], []
    for t0, t1, s, r in sorted(trades):
        for p in [p for p in open_ if p[0] <= t0]:
            eq += p[1] * p[2]
            open_.remove(p)
            curve.append((p[0], eq))
        if len(open_) >= slots or any(p[3] == s for p in open_):
            continue
        open_.append((t1, eq / slots, r, s))
    for p in sorted(open_):
        eq += p[1] * p[2]
        curve.append((p[0], eq))
    return eq, curve


def stats(trades, slots=3):
    if not trades:
        return {"trades": 0}
    eq, curve = portfolio(trades, slots)
    t0, t1 = min(t[0] for t in trades), max(t[1] for t in trades)
    yrs = max(0.25, (t1 - t0) / (365.25 * 86400))
    peak, dd = 1.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    by_year = {}
    for a, b in zip([(None, 1.0)] + curve[:-1], curve):
        y = datetime.fromtimestamp(b[0], timezone.utc).year
        by_year[y] = by_year.get(y, 1.0) * (b[1] / a[1])
    r = np.array([t[3] for t in trades])
    return {"trades": len(trades), "per_month": round(len(trades) / (yrs * 12), 1),
            "avg_net_pct": round(100 * r.mean(), 3), "win_pct": round(100 * (r > 0).mean(), 1),
            "cagr_pct": round(100 * (max(eq, 1e-9) ** (1 / yrs) - 1), 1), "max_dd_pct": round(100 * dd, 1),
            "years": {str(y): round(100 * (v - 1), 1) for y, v in sorted(by_year.items())}}


def walk_forward(all_trades, train_m=12, test_m=3, min_trades=10):
    """all_trades: {params: [trades]}. Για κάθε τρίμηνο, διαλέγει τις παραμέτρους με τη μεγαλύτερη μέση
    καθαρή απόδοση/trade στους προηγούμενους 12 μήνες και κρατάει τα trades τους στο τρίμηνο."""
    ts_all = [t[0] for v in all_trades.values() for t in v]
    start, end = min(ts_all), max(ts_all)
    m = 30.44 * 86400
    cur, oos, picks = start + train_m * m, [], []
    while cur < end:
        best, bv = None, -1e9
        for p, tr in all_trades.items():
            x = [t[3] for t in tr if cur - train_m * m <= t[0] < cur]
            if len(x) >= min_trades and np.mean(x) > bv:
                best, bv = p, float(np.mean(x))
        if best is not None and bv > 0:          # αν τίποτα δεν ήταν θετικό στο παρελθόν → δεν παίζει
            oos += [t for t in all_trades[best] if cur <= t[0] < cur + test_m * m]
        picks.append([datetime.fromtimestamp(cur, timezone.utc).strftime("%Y-%m"), str(best), round(100 * bv, 3)])
        cur += test_m * m
    return oos, picks


def main(d):
    data = {s: load(d, s) for s in SYMS}
    res = {}
    for name, fn, grid, hold_of in (
        ("crash_dip", trades_crash, [(z, h) for z in (-2.5, -3.0, -3.5, -4.0) for h in (12, 24, 48)], lambda p: p[1]),
        ("funding_extreme", trades_funding, [(th, h) for th in (0.0003, 0.0005, 0.001) for h in (8, 24, 72)], lambda p: p[1]),
    ):
        allt = {p: [t for s in SYMS for t in fn(s, *data[s], *p)] for p in grid}
        dbl = {p: [t for s in SYMS for t in fn(s, *data[s], *p, cost_mult=2.0)] for p in grid}
        full = {str(p): stats(v) for p, v in allt.items()}
        oos, picks = walk_forward(allt)
        oos2, _ = walk_forward(dbl)
        holds = [t[1] - t[0] for t in oos] or [86400]
        base = stats(random_baseline(data, max(200, len(oos)), int(np.median(holds) / 3600)))
        res[name] = {"grid_full_sample": full, "walk_forward": stats(oos), "walk_forward_double_cost": stats(oos2),
                     "random_same_hold": base, "picks": picks}
        print(name, json.dumps(res[name]["walk_forward"]), "\n  2×κόστη", json.dumps(res[name]["walk_forward_double_cost"]),
              "\n  τυχαίο", json.dumps(base))
    res["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(res, open("docs/edge_bt.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "qdata")
