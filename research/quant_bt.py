"""Backtest τριών στρατηγικών σε BTC/ETH/SOL + altcoins (Binance USDT-M perps), με walk-forward και κλειδωμένο holdout.

5) Volatility Compression Breakout (4ω)   8) Failed Breakout Reversal (1ω)   6) Funding & OI Dislocation (1ω)

Χρήση: python research/quant_bt.py <φάκελος δεδομένων>   → docs/quant_bt.json
Κανόνες χωρίς look-ahead: τα σήματα βγαίνουν στο κλείσιμο ενός κεριού, η είσοδος στο open του επόμενου.
Stop + στόχος στο ίδιο κερί → μετράει το stop. Αν το κερί ανοίξει πέρα από το stop → γέμισμα στο open.
Κάθε trade ρισκάρει 1% του κεφαλαίου (μέγιστη μόχλευση 3x). Τα κόστη (fee + slippage + funding) αφαιρούνται.
"""
import gzip
import itertools
import json
import os
import random
import sys

import numpy as np
import pandas as pd

DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "quant_bt.json")
FEE = 0.00045
SLIP = {"BTCUSDT": 0.0003, "ETHUSDT": 0.0003}
SLIP_ALT = 0.0010
RISK, MAX_LEV = 0.01, 3.0
HOLDOUT_MONTHS = 6
TRAIN_M, TEST_M = 12, 3


# ---------------- δεδομένα ----------------

def _ts(col):
    v = col.astype("int64")
    unit = "us" if v.max() > 10**14 else ("ms" if v.max() > 10**11 else "s")
    if v.max() < 10**9:   # π.χ. δευτερόλεπτα διαιρεμένα λάθος
        raise ValueError("λάθος timestamps")
    return pd.to_datetime(v, unit=unit, utc=True)


def load_klines(sym):
    f = os.path.join(DATA, f"klines_{sym}.csv.gz")
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    df["ts"] = _ts(df["ts"])
    return df.set_index("ts").astype(float).sort_index()


def load_series(kind, sym, col):
    f = os.path.join(DATA, f"{kind}_{sym}.csv.gz")
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    df["ts"] = _ts(df["ts"])
    return df.set_index("ts")[col].astype(float).sort_index()


def resample(df, rule):
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
         "taker_buy_volume": "sum"}).dropna()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


# ---------------- εκτέλεση ενός trade ----------------

def run_trade(o, h, l, c, i, side, stop, target=None, max_bars=30, trail_atr=None, atr_arr=None):
    """Είσοδος στο open του i. Επιστρέφει (exit_index, exit_price, λόγος)."""
    entry = o[i]
    best = entry
    for k in range(i, min(len(o), i + max_bars)):
        if trail_atr is not None and k > i:
            best = max(best, c[k - 1]) if side > 0 else min(best, c[k - 1])
            ts = best - side * trail_atr * atr_arr[k - 1]
            stop = max(stop, ts) if side > 0 else min(stop, ts)
        if side > 0:
            if l[k] <= stop:
                return k, min(o[k], stop), "stop"
            if target is not None and h[k] >= target:
                return k, max(o[k], target), "στόχος"
        else:
            if h[k] >= stop:
                return k, max(o[k], stop), "stop"
            if target is not None and l[k] <= target:
                return k, min(o[k], target), "στόχος"
    k = min(len(o), i + max_bars) - 1
    return k, c[k], "χρόνος"


def trade_record(sym, side, entry, exitp, stop0, t_in, t_out, funding_cost, slip, cost_mult=1.0):
    """Αποτέλεσμα σε R (πολλαπλάσια του ρίσκου) και σε % κεφαλαίου με ρίσκο 1% και όριο μόχλευσης."""
    dist = abs(entry - stop0) / entry
    if dist <= 0:
        return None
    notional = min(RISK / dist, MAX_LEV)            # σε κλάσμα του κεφαλαίου
    gross = side * (exitp - entry) / entry
    costs = cost_mult * (2 * (FEE + slip)) + funding_cost
    ret = notional * (gross - costs)                # % κεφαλαίου (ως κλάσμα)
    return {"sym": sym, "side": side, "t_in": str(t_in), "t_out": str(t_out), "ret": ret,
            "R": ret / (notional * dist), "gross_ret": notional * gross, "cost_ret": notional * costs}


def funding_between(fund, t_in, t_out, side):
    if fund is None:
        return 0.0
    s = fund[(fund.index > t_in) & (fund.index <= t_out)]
    return float(side * s.sum())                    # long πληρώνει θετικό funding


# ---------------- στρατηγικές: επιστρέφουν λίστα σημάτων (index, side, stop, target, max_bars, trail) ----------------

def sig_compression(df, btc_close, p):
    a = atr(df).values
    sma = df["close"].rolling(20).mean()
    bbw = (4 * df["close"].rolling(20).std()) / sma
    rank = bbw.rolling(1080, min_periods=540).rank(pct=True)
    squeeze = (rank < p["sq"]).astype(int).rolling(6).max().fillna(0).values
    hi = df["high"].rolling(20).max().shift(1).values
    lo = df["low"].rolling(20).min().shift(1).values
    vol_ok = (df["volume"] > p["vm"] * df["volume"].rolling(20).mean()).values
    regime = (btc_close > btc_close.rolling(300).mean()).reindex(df.index).ffill().values
    c = df["close"].values
    out = []
    for i in range(1100, len(df) - 1):
        if not squeeze[i] or not vol_ok[i] or np.isnan(a[i]):
            continue
        if c[i] > hi[i] and (not p["reg"] or regime[i]):
            out.append((i + 1, 1, None, None, 30, 3.0, 1.5 * a[i]))
        elif c[i] < lo[i] and (not p["reg"] or not regime[i]):
            out.append((i + 1, -1, None, None, 30, 3.0, 1.5 * a[i]))
    return out


def sig_failed_breakout(df, btc_close, p):
    n = p["lb"]
    a = atr(df).values
    hi = df["high"].rolling(n).max().shift(1).values
    lo = df["low"].rolling(n).min().shift(1).values
    vol_ok = (df["volume"] > p["vm"] * df["volume"].rolling(20).mean()).values
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    out = []
    for i in range(n + 20, len(df) - 1):
        if not vol_ok[i] or np.isnan(a[i]):
            continue
        mid = (hi[i] + lo[i]) / 2
        if h[i] > hi[i] and c[i] < hi[i]:      # σπάει πάνω, κλείνει μέσα → short
            out.append((i + 1, -1, h[i] + 0.1 * a[i], mid, 24, None, None))
        elif l[i] < lo[i] and c[i] > lo[i]:    # σπάει κάτω, κλείνει μέσα → long
            out.append((i + 1, 1, l[i] - 0.1 * a[i], mid, 24, None, None))
    return out


def sig_funding(df, btc_close, p, fund=None, oi=None):
    if fund is None or len(fund) < 400:
        return []
    f = fund.reindex(df.index, method="ffill")
    frank = fund.rolling(270, min_periods=200).rank(pct=True).reindex(df.index, method="ffill").values
    if p["oi"]:
        if oi is None:
            return []
        o = oi.reindex(df.index, method="ffill")
        oichg = (o / o.shift(48) - 1).values
    else:
        oichg = np.full(len(df), 1.0)
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    lo4 = df["low"].rolling(4).min().shift(1).values
    hi4 = df["high"].rolling(4).max().shift(1).values
    hi24 = df["high"].rolling(24).max().values
    lo24 = df["low"].rolling(24).min().values
    out, last = [], -10**9
    for i in range(300, len(df) - 1):
        if i - last < 24 or np.isnan(frank[i]):
            continue
        if frank[i] > p["pc"] and oichg[i] > p["oig"] and c[i] < lo4[i]:
            out.append((i + 1, -1, hi24[i], None, 72, None, None)); last = i
        elif frank[i] < 1 - p["pc"] and oichg[i] > p["oig"] and c[i] > hi4[i]:
            out.append((i + 1, 1, lo24[i], None, 72, None, None)); last = i
    return out


STRATS = {
    "compression": {"tf": "4h", "fn": sig_compression,
                    "grid": [{"sq": sq, "vm": vm, "reg": rg} for sq, vm, rg in
                             itertools.product([0.1, 0.2], [1.0, 1.5], [True, False])]},
    "failed_breakout": {"tf": "1h", "fn": sig_failed_breakout,
                        "grid": [{"lb": lb, "vm": vm} for lb, vm in itertools.product([24, 48], [1.0, 1.5])]},
    "funding_oi": {"tf": "1h", "fn": sig_funding,
                   "grid": [{"pc": pc, "oig": g, "oi": oi} for pc, g, oi in
                            [(0.95, 0.05, True), (0.95, 0.10, True), (0.98, 0.05, True),
                             (0.95, -9, False), (0.98, -9, False)]]},
}


def simulate(name, sig_list, df, sym, fund, cost_mult=1.0):
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    a = atr(df).values
    idx = df.index
    slip = SLIP.get(sym, SLIP_ALT)
    trades, busy = [], -1
    for (i, side, stop, target, max_bars, trail, init_dist) in sig_list:
        if i <= busy or i >= len(o):
            continue
        entry = o[i]
        if stop is None:
            stop = entry - side * init_dist
        if (side > 0 and stop >= entry) or (side < 0 and stop <= entry):
            continue
        k, px, why = run_trade(o, h, l, c, i, side, stop, target, max_bars, trail, a)
        fc = funding_between(fund, idx[i], idx[k], side)
        rec = trade_record(sym, side, entry, px, stop, idx[i], idx[k], fc, slip, cost_mult)
        if rec:
            rec["why"] = why
            trades.append(rec)
        busy = k
    return trades


def random_signals(sig_list, o, rng):
    """Ίδιο πλήθος σημάτων, ίδιες αποστάσεις stop/στόχου και κανόνες εξόδου, τυχαίο σημείο και κατεύθυνση."""
    out = []
    for (i0, side0, stop, target, mb, trail, init_dist) in sig_list:
        if i0 >= len(o):
            continue
        e0 = o[i0]
        d_stop = init_dist / e0 if stop is None else abs(e0 - stop) / e0
        d_tgt = None if target is None else (target - e0) / e0 * side0   # απόσταση προς την κατεύθυνση του trade
        i = rng.randrange(1200, len(o) - 2)
        side = rng.choice([1, -1])
        e = o[i]
        tgt = None if d_tgt is None else e * (1 + side * d_tgt)
        out.append((i, side, e * (1 - side * d_stop), tgt, mb, trail, None))
    return sorted(out, key=lambda x: x[0])


# ---------------- μετρικές ----------------

def metrics(trades):
    if not trades:
        return {"trades": 0}
    trades = sorted(trades, key=lambda t: t["t_out"])   # χρονολογικά (για drawdown)
    r = np.array([t["ret"] for t in trades])
    R = np.array([t["R"] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    df = pd.DataFrame(trades)
    df["m"] = pd.to_datetime(df["t_out"]).dt.tz_localize(None).dt.to_period("M")
    monthly = df.groupby("m")["ret"].sum()
    eq = np.cumprod(1 + r)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    years = df.assign(y=pd.to_datetime(df["t_out"]).dt.year).groupby("y")["ret"].sum()
    by_sym = df.groupby("sym")["ret"].sum()
    best = by_sym.idxmax()
    return {"trades": len(r), "win_rate": round(100 * len(wins) / len(r), 1),
            "avg_R": round(float(R.mean()), 3), "expectancy_pct": round(100 * float(r.mean()), 3),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            "avg_win_pct": round(100 * float(wins.mean()), 3) if len(wins) else 0,
            "avg_loss_pct": round(100 * float(losses.mean()), 3) if len(losses) else 0,
            "total_pct": round(100 * float(eq[-1] - 1), 1), "max_dd_pct": round(100 * dd, 1),
            "sharpe_monthly_ann": round(float(monthly.mean() / monthly.std() * np.sqrt(12)), 2) if monthly.std() > 0 else None,
            "costs_pct_total": round(100 * float(sum(t["cost_ret"] for t in trades)), 1),
            "by_year_pct": {int(k): round(100 * float(v), 1) for k, v in years.items()},
            "by_symbol_pct": {k: round(100 * float(v), 1) for k, v in by_sym.sort_values().items()},
            "without_best_symbol_pct": round(100 * float(df[df["sym"] != best]["ret"].sum()), 1)}


def portfolio(trades, max_open=3):
    """Ρεαλιστικό χαρτοφυλάκιο: το πολύ max_open ανοιχτές θέσεις ταυτόχρονα, χρονολογικά."""
    open_until, kept = [], []
    for t in sorted(trades, key=lambda t: t["t_in"]):
        open_until = [x for x in open_until if x > t["t_in"]]
        if len(open_until) < max_open:
            open_until.append(t["t_out"])
            kept.append(t)
    m = metrics(kept)
    if kept:
        days = (pd.Timestamp(kept[-1]["t_out"]) - pd.Timestamp(kept[0]["t_in"])).days or 1
        m["cagr_pct"] = round(100 * ((1 + m["total_pct"] / 100) ** (365 / days) - 1), 1)
    return m


# ---------------- walk-forward ----------------

def main():
    syms = sorted({f.split("_", 1)[1].split(".")[0] for f in os.listdir(DATA) if f.startswith("klines_")})
    k1 = {s: load_klines(s) for s in syms}
    k1 = {s: d for s, d in k1.items() if d is not None and len(d) > 3000}
    fund = {s: load_series("funding", s, "rate") for s in k1}
    oi = {s: load_series("oi", s, "oi_usd") for s in k1}
    btc = k1["BTCUSDT"]["close"]
    end = max(d.index[-1] for d in k1.values())
    holdout_start = end - pd.DateOffset(months=HOLDOUT_MONTHS)
    start = min(d.index[0] for d in k1.values())
    out = {"symbols": list(k1), "start": str(start), "end": str(end), "holdout_start": str(holdout_start),
           "costs": {"fee_per_side": FEE, "slip_btc_eth": 0.0003, "slip_alts": SLIP_ALT}, "strategies": {}}

    for name, S in STRATS.items():
        frames = {s: (k1[s] if S["tf"] == "1h" else resample(k1[s], S["tf"])) for s in k1}
        btc_tf = frames["BTCUSDT"]["close"]
        # όλα τα trades για κάθε συνδυασμό παραμέτρων (κοστίζει λίγο, απλοποιεί το walk-forward)
        cache, sigs = {}, {}
        for gi, p in enumerate(S["grid"]):
            allt = []
            for s, df in frames.items():
                kw = {"fund": fund[s], "oi": oi[s]} if name == "funding_oi" else {}
                sl = S["fn"](df, btc_tf, p, **kw)
                sigs[(gi, s)] = sl
                allt += simulate(name, sl, df, s, fund[s])
            cache[gi] = allt
            print(name, p, len(allt), flush=True)

        def in_range(trs, a, b):
            return [t for t in trs if a <= pd.Timestamp(t["t_in"]) < b]

        def score(trs):
            return np.mean([t["R"] for t in trs]) if len(trs) >= 30 else -9

        oos, folds = [], []
        t0 = start + pd.DateOffset(months=TRAIN_M)
        while t0 + pd.DateOffset(months=TEST_M) <= holdout_start:
            tr_a, te_b = t0 - pd.DateOffset(months=TRAIN_M), t0 + pd.DateOffset(months=TEST_M)
            best = max(cache, key=lambda g: score(in_range(cache[g], tr_a, t0)))
            got = in_range(cache[best], t0, te_b)
            oos += got
            folds.append({"test_from": str(t0.date()), "params": S["grid"][best], "trades": len(got),
                          "avg_R": round(float(np.mean([t["R"] for t in got])), 3) if got else None})
            t0 = te_b
        best_all = max(cache, key=lambda g: score(in_range(cache[g], start, holdout_start)))
        hold = in_range(cache[best_all], holdout_start, end + pd.Timedelta(days=1))

        # τυχαία βάση με τα ίδια κόστη/εξόδους
        rng = random.Random(7)
        rnd = []
        for s, df in frames.items():
            sl = [x for x in sigs[(best_all, s)]]
            if sl:
                rnd += in_range(simulate(name, random_signals(sl, df["open"].values, rng), df, s, fund[s]),
                                start + pd.DateOffset(months=TRAIN_M), holdout_start)
        # αντοχή: διπλάσια κόστη στα ίδια OOS σήματα
        double = []
        for s, df in frames.items():
            double += in_range(simulate(name, sigs[(best_all, s)], df, s, fund[s], cost_mult=2.0),
                               start + pd.DateOffset(months=TRAIN_M), holdout_start)
        m_oos = metrics(oos)
        res = {"walk_forward_oos": m_oos, "portfolio_max3_oos": portfolio(oos), "portfolio_max3_holdout": portfolio(hold), "folds": folds, "chosen_params_for_holdout": S["grid"][best_all],
               "holdout": metrics(hold), "random_baseline": metrics(rnd), "double_costs_oos": metrics(double)}
        mo, mh, mr, md = m_oos, res["holdout"], res["random_baseline"], res["double_costs_oos"]
        yrs = mo.get("by_year_pct", {})
        crit = {
            "100+ OOS trades": mo.get("trades", 0) >= 100,
            "θετικό μετά από κόστη": mo.get("expectancy_pct", -1) > 0,
            "≥0 με διπλά κόστη": md.get("expectancy_pct", -1) >= 0,
            "profit factor ≥ 1,2": (mo.get("profit_factor") or 0) >= 1.2,
            "Sharpe ≥ 0,8": (mo.get("sharpe_monthly_ann") or 0) >= 0.8,
            "καλύτερη από τυχαίες": mo.get("expectancy_pct", -1) > mr.get("expectancy_pct", 9),
            "κερδοφόρα στα 2/3 των ετών": sum(v > 0 for v in yrs.values()) >= 2 * len(yrs) / 3 if yrs else False,
            "όχι από ένα coin": mo.get("without_best_symbol_pct", -1) > 0,
            "θετική στο holdout": mh.get("expectancy_pct", -1) > 0,
        }
        res["criteria"] = crit
        res["passes"] = all(crit.values())
        out["strategies"][name] = res
        print(name, json.dumps({k: v for k, v in m_oos.items() if not isinstance(v, dict)}),
              "holdout", mh.get("expectancy_pct"), "random", mr.get("expectancy_pct"), "pass", res["passes"], flush=True)

    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
