"""Ερευνητικό εργαστήριο για τη Volatility Compression Breakout: audit + experiments A–D + ρίσκο.

Χρήση: python research/compress_lab.py <φάκελος δεδομένων>  → docs/compress_lab.json
Κοινά για όλα: ίδια δεδομένα (13 perps Binance με LUNA/FTT), ίδια κόστη (fee 0,045% + slippage 0,03%/0,10% + funding),
είσοδος στο open του επόμενου 4ωρου κεριού, stop πρώτα αν πιάνονται stop και στόχος στο ίδιο κερί.
Χαρτοφυλάκιο: ακριβής προσομοίωση γεγονότων (το ρίσκο υπολογίζεται στο κεφάλαιο τη στιγμή της εισόδου,
το κέρδος προστίθεται στην έξοδο) — όχι απλός πολλαπλασιασμός αποδόσεων.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quant_bt as Q  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "compress_lab.json")
BASE = {"sq": 0.10, "win": 1080, "vm": 1.5, "reg": "btc", "filt": [], "trail": 3.0, "stop": 1.5,
        "max_bars": 30, "partial": None}
BASE_RISK = {"risk": 0.01, "max_open": 3, "max_lev": 3.0, "corr": False, "dd_cut": False, "vol_size": False}


# ---------------- προετοιμασία δεδομένων ----------------

def prepare():
    syms = sorted({f.split("_", 1)[1].split(".")[0] for f in os.listdir(DATA) if f.startswith("klines_")})
    k1 = {s: Q.load_klines(s) for s in syms}
    k1 = {s: d for s, d in k1.items() if d is not None and len(d) > 3000}
    fund = {s: Q.load_series("funding", s, "rate") for s in k1}
    F = {}
    for s, d in k1.items():
        df = Q.resample(d, "4h")
        c = df["close"]
        a = Q.atr(df)
        bw = (4 * c.rolling(20).std()) / c.rolling(20).mean()
        f = pd.DataFrame(index=df.index)
        f["o"], f["h"], f["l"], f["c"], f["v"] = df["open"], df["high"], df["low"], c, df["volume"]
        f["atr"] = a
        for w in (540, 1080, 2160):
            f[f"rank{w}"] = bw.rolling(w, min_periods=w // 2).rank(pct=True)
        f["hi20"] = df["high"].rolling(20).max().shift(1)
        f["lo20"] = df["low"].rolling(20).min().shift(1)
        f["vr"] = df["volume"] / df["volume"].rolling(20).mean()
        f["sma300"] = c.rolling(300).mean()                       # ~50 μέρες
        f["ret180"] = c / c.shift(180) - 1                         # ~30 μέρες
        f["taker"] = (df["taker_buy_volume"].rolling(3).sum() / df["volume"].rolling(3).sum())
        if fund[s] is not None and len(fund[s]) > 300:
            fr = fund[s].rolling(3).mean()                         # μέσος 24ω
            f["fpct"] = fr.rolling(270, min_periods=150).rank(pct=True).reindex(df.index, method="ffill")
        else:
            f["fpct"] = np.nan
        F[s] = f
    btc = F["BTCUSDT"]
    idx_all = sorted(set().union(*[set(f.index) for f in F.values()]))
    above = pd.DataFrame({s: (f["c"] > f["sma300"]).reindex(idx_all) for s, f in F.items()})
    valid = pd.DataFrame({s: f["sma300"].notna().reindex(idx_all) for s, f in F.items()})
    breadth = (above & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    btc_vol = btc["c"].pct_change().rolling(180).std()
    for s, f in F.items():
        f["btc_up"] = (btc["c"] > btc["sma300"]).reindex(f.index, method="ffill")
        f["btc_ret180"] = btc["ret180"].reindex(f.index, method="ffill")
        f["btc_calm"] = (btc_vol < btc_vol.rolling(1080, min_periods=300).median()).reindex(f.index, method="ffill")
        f["breadth"] = breadth.reindex(f.index, method="ffill")
    return F, fund


# ---------------- σήματα ----------------

def signals(f, p):
    rank = f[f"rank{p['win']}"]
    sq = (rank < p["sq"]).astype(int).rolling(6).max().fillna(0).values.astype(bool)
    c, hi, lo = f["c"].values, f["hi20"].values, f["lo20"].values
    vr, a = f["vr"].values, f["atr"].values
    up = f["btc_up"].values.astype(bool)
    longs = sq & (c > hi) & (vr > p["vm"])
    shorts = sq & (c < lo) & (vr > p["vm"])
    if p["reg"] == "btc":
        longs &= up
        shorts &= ~up
    elif p["reg"] == "btc_calm":                 # BTC regime + ήρεμη αγορά
        calm = f["btc_calm"].fillna(False).values.astype(bool)
        longs &= up & calm
        shorts &= ~up & calm
    for flt in p["filt"]:
        if flt == "daily":                       # το ίδιο το coin σε τάση ημερήσιου χρονικού πλαισίου
            above = (f["c"] > f["sma300"]).values
            longs &= above
            shorts &= ~above
        elif flt == "breadth":                   # >50% των coins πάνω από τον μέσο 50 ημερών
            b = f["breadth"].values
            longs &= b > 0.5
            shorts &= b < 0.5
        elif flt == "rs":                        # σχετική δύναμη έναντι BTC (30 μέρες)
            rs = (f["ret180"] - f["btc_ret180"]).values
            longs &= rs > 0
            shorts &= rs < 0
        elif flt == "vol2":                      # αυστηρότερη επιβεβαίωση όγκου
            longs &= vr > 2.0
            shorts &= vr > 2.0
        elif flt == "cvd":                       # επιθετικοί αγοραστές/πωλητές στην κατεύθυνση του breakout
            t = f["taker"].values
            longs &= t > 0.5
            shorts &= t < 0.5
        elif flt == "funding":                   # αποφυγή crowded: όχι long με πολύ ψηλό funding, όχι short με πολύ χαμηλό
            fp = f["fpct"].values
            ok = ~np.isnan(fp)
            longs &= ~(ok & (fp > 0.9))
            shorts &= ~(ok & (fp < 0.1))
    out = []
    n = len(c)
    for i in np.nonzero(longs | shorts)[0]:
        if i < 1100 or i + 1 >= n or np.isnan(a[i]):
            continue
        out.append((i + 1, 1 if longs[i] else -1, p["stop"] * a[i]))
    return out


def exit_trade(f, i, side, dist, p):
    """Επιστρέφει (exit_idx, λίστα (κλάσμα, τιμή), λόγος)."""
    o, h, l, c, a = (f[k].values for k in ("o", "h", "l", "c", "atr"))
    entry = o[i]
    stop = entry - side * dist
    best, legs, rem = entry, [], 1.0
    tgt = entry + side * p["partial"] * dist if p["partial"] else None
    end = min(len(o), i + p["max_bars"])
    for k in range(i, end):
        if k > i:
            best = max(best, c[k - 1]) if side > 0 else min(best, c[k - 1])
            ts = best - side * p["trail"] * a[k - 1]
            stop = max(stop, ts) if side > 0 else min(stop, ts)
        hit_stop = l[k] <= stop if side > 0 else h[k] >= stop
        if hit_stop:
            px = min(o[k], stop) if side > 0 else max(o[k], stop)
            legs.append((rem, px))
            return k, legs, "stop"
        if tgt is not None and rem == 1.0 and ((h[k] >= tgt) if side > 0 else (l[k] <= tgt)):
            legs.append((0.5, tgt))
            rem = 0.5
            stop = max(stop, entry) if side > 0 else min(stop, entry)   # το υπόλοιπο σε breakeven
    k = end - 1
    legs.append((rem, c[k]))
    return k, legs, "χρόνος"


def candidate_trades(F, fund, p):
    """Όλα τα πιθανά trades (ένα ανοιχτό ανά coin), ανεξάρτητα από το χαρτοφυλάκιο."""
    out = []
    for s, f in F.items():
        slip = Q.SLIP.get(s, Q.SLIP_ALT)
        busy = -1
        idx = f.index
        for i, side, dist in signals(f, p):
            if i <= busy:
                continue
            entry = f["o"].values[i]
            k, legs, why = exit_trade(f, i, side, dist, p)
            gross = sum(fr * side * (px - entry) / entry for fr, px in legs)
            cost = 2 * (Q.FEE + slip)
            fc = Q.funding_between(fund[s], idx[i], idx[k], side)
            out.append({"sym": s, "side": side, "t_in": idx[i], "t_out": idx[k] + pd.Timedelta(hours=4),
                        "dist": dist / entry, "gross": gross, "cost": cost, "funding": fc, "why": why,
                        "atr_pct": f["atr"].values[i - 1] / entry})
            busy = k
    out.sort(key=lambda t: t["t_in"])
    return out


# ---------------- χαρτοφυλάκιο (ακριβής προσομοίωση γεγονότων) ----------------

def portfolio(cands, r, cost_mult=1.0, start=None, end=None):
    eq, peak = 1.0, 1.0
    open_pos, trades, curve = [], [], []
    med_atr = np.median([t["atr_pct"] for t in cands]) if cands else 0.01
    events = [t for t in cands if (start is None or t["t_in"] >= start) and (end is None or t["t_in"] < end)]
    for t in events:
        # κλείσιμο ό,τι έληξε πριν από αυτή την είσοδο
        for p in sorted([p for p in open_pos if p["t_out"] <= t["t_in"]], key=lambda p: p["t_out"]):
            eq += p["pnl"]
            peak = max(peak, eq)
            curve.append((p["t_out"], eq))
            open_pos.remove(p)
        if len(open_pos) >= r["max_open"] or any(p["sym"] == t["sym"] for p in open_pos):
            continue
        risk = r["risk"]
        if r["dd_cut"]:                                     # λιγότερο ρίσκο όταν είμαστε σε πτώση
            dd = eq / peak - 1
            risk *= 0.5 if dd < -0.10 else 1.0
            risk *= 0.5 if dd < -0.20 else 1.0
        if r["corr"]:                                       # θέσεις στην ίδια κατεύθυνση μοιράζονται το ρίσκο
            same = sum(1 for p in open_pos if p["side"] == t["side"])
            risk /= (1 + same)
        if r["vol_size"]:                                   # μικρότερη θέση όταν το coin είναι πιο ευμετάβλητο από το συνηθισμένο
            risk *= min(1.5, max(0.5, med_atr / t["atr_pct"]))
        notional = min(risk * eq / t["dist"], r["max_lev"] * eq)
        pnl = notional * (t["gross"] - cost_mult * t["cost"] - t["funding"])
        rec = dict(t, notional=notional, pnl=pnl, eq_in=eq, risk_usd=notional * t["dist"])
        open_pos.append(rec)
        trades.append(rec)
    for p in sorted(open_pos, key=lambda p: p["t_out"]):
        eq += p["pnl"]
        curve.append((p["t_out"], eq))
    return trades, curve


def stats(trades, curve):
    if not trades:
        return {"trades": 0}
    pnl = np.array([t["pnl"] / t["eq_in"] for t in trades])        # % του κεφαλαίου τη στιγμή της εισόδου
    R = np.array([t["pnl"] / t["risk_usd"] for t in trades])
    eqs = np.array([e for _, e in curve])
    dd = float((eqs / np.maximum.accumulate(np.r_[1.0, eqs])[1:] - 1).min())
    t0, t1 = trades[0]["t_in"], curve[-1][0]
    years = max((t1 - t0).days / 365.25, 0.1)
    total = eqs[-1] - 1
    s = pd.Series({tm: e for tm, e in curve})
    s.index = pd.to_datetime(s.index)
    monthly = s.resample("ME").last().ffill()
    mret = monthly.pct_change().dropna()
    by_year = {}
    for tm, e in curve:
        by_year.setdefault(tm.year, []).append(e)
    yr, prev = {}, 1.0
    for y in sorted(by_year):
        yr[int(y)] = round(100 * (by_year[y][-1] / prev - 1), 1)
        prev = by_year[y][-1]
    gains, losses = pnl[pnl > 0], pnl[pnl <= 0]
    streak = best = 0
    for x in pnl:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    return {"trades": len(trades), "net_return_pct": round(100 * total, 1),
            "cagr_pct": round(100 * ((1 + total) ** (1 / years) - 1), 1) if total > -1 else -100.0,
            "expectancy_pct": round(100 * pnl.mean(), 3), "median_pct": round(100 * float(np.median(pnl)), 3),
            "avg_R": round(float(R.mean()), 3),
            "profit_factor": round(float(gains.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            "win_rate": round(100 * len(gains) / len(pnl), 1),
            "sharpe": round(float(mret.mean() / mret.std() * np.sqrt(12)), 2) if len(mret) > 3 and mret.std() > 0 else None,
            "max_dd_pct": round(100 * dd, 1), "max_consec_losses": int(best),
            "positive_years": f"{sum(v > 0 for v in yr.values())}/{len(yr)}", "by_year_pct": yr}


# ---------------- walk-forward επιλογή μέσα σε μια οικογένεια παραλλαγών ----------------

def walk_forward(family, cache, rset, t_start, t_hold, train_m=12, test_m=3):
    """Σε κάθε παράθυρο διαλέγει την παραλλαγή με το καλύτερο Sharpe-like σκορ ΣΤΗΝ ΕΚΠΑΙΔΕΥΣΗ και κρατά τα trades ελέγχου."""
    t0 = t_start + pd.DateOffset(months=train_m)
    picks, test_trades = [], []
    while t0 + pd.DateOffset(months=test_m) <= t_hold:
        a, b = t0 - pd.DateOffset(months=train_m), t0 + pd.DateOffset(months=test_m)

        def score(name):
            tr, _ = portfolio(cache[name], rset, start=a, end=t0)
            if len(tr) < 20:
                return -9
            x = np.array([t["pnl"] / t["eq_in"] for t in tr])
            return x.mean() / (x.std() + 1e-9)
        best = max(family, key=score)
        picks.append(best)
        test_trades += [t for t in cache[best] if t0 <= t["t_in"] < b]
        t0 = b
    test_trades.sort(key=lambda t: t["t_in"])
    return picks, test_trades


def main():
    F, fund = prepare()
    end = max(f.index[-1] for f in F.values())
    t_hold = end - pd.DateOffset(months=Q.HOLDOUT_MONTHS)
    t_start = min(f.index[0] for f in F.values())
    t_oos = t_start + pd.DateOffset(months=12)

    V = {"baseline": BASE}
    # A — thresholds / windows / regimes (μόνο είσοδος)
    for sq in (0.05, 0.15, 0.20):
        V[f"A_sq{int(sq * 100)}"] = dict(BASE, sq=sq)
    for w in (540, 2160):
        V[f"A_win{w // 6}d"] = dict(BASE, win=w)
    V["A_reg_none"] = dict(BASE, reg="none")
    V["A_reg_btc_calm"] = dict(BASE, reg="btc_calm")
    # B — φίλτρα (ένα κάθε φορά, και όλα μαζί)
    for flt in ("daily", "breadth", "rs", "vol2"):
        V[f"B_{flt}"] = dict(BASE, filt=[flt])
    V["B_all"] = dict(BASE, filt=["daily", "breadth", "rs"])
    # C — μόνο έξοδοι
    V["C_trail2"] = dict(BASE, trail=2.0)
    V["C_trail4"] = dict(BASE, trail=4.0)
    V["C_partial2R"] = dict(BASE, partial=2.0)
    V["C_time12"] = dict(BASE, max_bars=12)
    V["C_time60"] = dict(BASE, max_bars=60)
    # D — συνδυασμοί
    V["D_cvd"] = dict(BASE, filt=["cvd"])
    V["D_funding"] = dict(BASE, filt=["funding"])
    V["D_cvd_funding"] = dict(BASE, filt=["cvd", "funding"])

    cache = {k: candidate_trades(F, fund, p) for k, p in V.items()}
    for k in V:
        print(k, len(cache[k]), flush=True)

    def evaluate(name, rset=BASE_RISK):
        c = cache[name]
        oos = portfolio(c, rset, start=t_oos, end=t_hold)
        dbl = portfolio(c, rset, cost_mult=2.0, start=t_oos, end=t_hold)
        hold = portfolio(c, rset, start=t_hold)
        return {"oos": stats(*oos), "double_costs_oos": stats(*dbl), "holdout_used": stats(*hold)}

    res = {"period": {"oos_from": str(t_oos.date()), "holdout_from": str(t_hold.date()), "end": str(end.date())},
           "variants": {k: dict(evaluate(k), params=V[k]) for k in V}}

    # ---------------- AUDIT του baseline ----------------
    tr, curve = portfolio(cache["baseline"], BASE_RISK, start=t_oos, end=t_hold)
    x = np.array([t["pnl"] / t["eq_in"] for t in tr])
    srt = np.sort(x)[::-1]
    total_profit = x.sum()
    seq_compound = float(np.prod(1 + x) - 1)
    by_coin = pd.Series({t["sym"]: 0.0 for t in tr})
    for t in tr:
        by_coin[t["sym"]] += 100 * t["pnl"] / t["eq_in"]
    hold_tr, hold_curve = portfolio(cache["baseline"], BASE_RISK, start=t_hold)
    hm = pd.Series({t["t_out"]: 100 * t["pnl"] / t["eq_in"] for t in hold_tr})
    hm.index = pd.to_datetime(hm.index)
    res["audit"] = {
        "trades": len(x), "mean_pct": round(100 * x.mean(), 3), "median_pct": round(100 * float(np.median(x)), 3),
        "profit_factor_check": round(float(x[x > 0].sum() / -x[x <= 0].sum()), 3),
        "percentiles_pct": {p: round(100 * float(np.percentile(x, p)), 2) for p in (5, 25, 50, 75, 95, 99)},
        "share_of_profit_top5_pct": round(100 * srt[:5].sum() / total_profit, 1),
        "share_of_profit_top10_pct": round(100 * srt[:10].sum() / total_profit, 1),
        "return_without_top10_pct_sum": round(100 * srt[10:].sum(), 1),
        "max_consec_losses": stats(tr, curve)["max_consec_losses"],
        "by_coin_pct_sum": {k: round(v, 1) for k, v in by_coin.sort_values().items()},
        "event_sim_net_return_pct": stats(tr, curve)["net_return_pct"],
        "sequential_compound_pct": round(100 * seq_compound, 1),
        "simple_sum_pct": round(100 * float(x.sum()), 1),
        "holdout_monthly_pct": {str(k.date()): round(float(v), 1) for k, v in hm.resample("ME").sum().items()},
        "holdout_trades": len(hold_tr),
        "holdout_without_top3_pct_sum": round(float(np.sort(hm.values)[::-1][3:].sum()), 1) if len(hm) > 3 else None,
    }
    # ---------------- ΡΙΣΚΟ (ίδια trades του baseline) ----------------
    R = {"risk_0.5": dict(BASE_RISK, risk=0.005), "risk_0.75": dict(BASE_RISK, risk=0.0075), "risk_1.0": BASE_RISK,
         "max_open_2": dict(BASE_RISK, max_open=2), "max_open_5": dict(BASE_RISK, max_open=5),
         "corr_adjusted": dict(BASE_RISK, corr=True), "dd_cut": dict(BASE_RISK, dd_cut=True),
         "vol_sizing": dict(BASE_RISK, vol_size=True),
         "combo_0.75_corr_ddcut": dict(BASE_RISK, risk=0.0075, corr=True, dd_cut=True)}
    res["risk"] = {k: {"oos": stats(*portfolio(cache["baseline"], r, start=t_oos, end=t_hold)),
                       "double_costs_oos": stats(*portfolio(cache["baseline"], r, cost_mult=2.0, start=t_oos, end=t_hold)),
                       "holdout_used": stats(*portfolio(cache["baseline"], r, start=t_hold))} for k, r in R.items()}
    # ---------------- WALK-FORWARD ανά οικογένεια ----------------
    fams = {"A": ["baseline"] + [k for k in V if k.startswith("A_")],
            "B": ["baseline"] + [k for k in V if k.startswith("B_")],
            "C": ["baseline"] + [k for k in V if k.startswith("C_")],
            "D": ["baseline"] + [k for k in V if k.startswith("D_")]}
    res["walk_forward"] = {}
    for fam, names in fams.items():
        picks, tt = walk_forward(names, cache, BASE_RISK, t_start, t_hold)
        trs, crv = portfolio(tt, BASE_RISK)
        res["walk_forward"][fam] = {"picks": picks, "oos": stats(trs, crv)}
    with open(OUT, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res["audit"], ensure_ascii=False, default=str)[:3000])


if __name__ == "__main__":
    main()
