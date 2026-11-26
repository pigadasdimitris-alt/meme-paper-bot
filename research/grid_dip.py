"""Δοκιμή παραλλαγών του dip σε αποθηκευμένα κεριά, με διαχωρισμό εκπαίδευσης / τελικού ελέγχου.

Χρήση: BT_CACHE=/tmp/candles.json python research/grid_dip.py
Επιλογή ΜΟΝΟ με βάση τις πρώτες μέρες (εκπαίδευση). Οι τελευταίες HOLDOUT_DAYS μέρες
κοιτάζονται μόνο για τις κορυφαίες παραλλαγές — αν δεν περάσουν εκεί, απορρίπτονται.
"""
import itertools
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402

HOLDOUT_DAYS = float(os.environ.get("HOLDOUT_DAYS", "3"))
SLIP = 0.01
OUT = os.path.join(os.path.dirname(B.OUT), "grid_dip.json")


def stats(trades):
    nets = [B.net_pct(t["move"], SLIP) for t in trades]
    if not nets:
        return {"n": 0}
    return {"n": len(nets), "avg": round(statistics.mean(nets), 2), "med": round(statistics.median(nets), 2),
            "wins": round(100 * sum(n > 0 for n in nets) / len(nets), 1),
            "coins": len({t["pool"] for t in trades}),
            # χωρίς το καλύτερο coin: ελέγχει αν όλο το κέρδος έρχεται από ένα μόνο coin
            "avg_wo_best_coin": round(statistics.mean(
                [B.net_pct(t["move"], SLIP) for t in trades if t["pool"] != best_coin(trades)] or [0]), 2)}


def best_coin(trades):
    by = {}
    for t in trades:
        by[t["pool"]] = by.get(t["pool"], 0) + B.net_pct(t["move"], SLIP)
    return max(by, key=by.get) if by else None


def main():
    with open(os.environ["BT_CACHE"]) as f:
        series = {k: (v[0], v[1]) for k, v in json.load(f).items()}
    t_end = max(c[-1][0] for _, c in series.values())
    t_cut = t_end - HOLDOUT_DAYS * 86400
    base = B.params("dip")
    windows = [(-30, -12), (-40, -15), (-25, -8), (-50, -20), (-20, -6)]
    bounces = [(0.5, 8.0), (2.0, 8.0), (-100.0, 8.0), (0.5, 3.0)]
    exits = [(15, -10), (25, -12), (10, -8), (40, -15), (8, -6)]
    holds = [120, 240, 480]
    rows = []
    for (h1lo, h1hi), (m5lo, m5hi), (tp, sl), hold in itertools.product(windows, bounces, exits, holds):
        P = dict(base, MIN_PRICE_CHANGE_H1=h1lo, MAX_PRICE_CHANGE_H1=h1hi, MIN_PRICE_CHANGE_M5=m5lo,
                 MAX_PRICE_CHANGE_M5=m5hi, TAKE_PROFIT_PCT=tp, STOP_LOSS_PCT=sl, MAX_HOLD_MIN=hold,
                 BREAKEVEN_TRIGGER_PCT=1e9)
        tr = B.run_strategy("dip", P, series)
        train = [t for t in tr if t["t"] < t_cut]
        test = [t for t in tr if t["t"] >= t_cut]
        rows.append({"p": {"h1": [h1lo, h1hi], "m5": [m5lo, m5hi], "tp": tp, "sl": sl, "hold": hold},
                     "train": stats(train), "test": stats(test)})
    ok = [r for r in rows if r["train"]["n"] >= 25 and r["train"]["coins"] >= 5]
    ok.sort(key=lambda r: r["train"]["avg"], reverse=True)
    out = {"holdout_days": HOLDOUT_DAYS, "slip": SLIP, "combos": len(rows), "eligible": len(ok),
           "top": ok[:12], "current": next(r for r in rows if r["p"] == {"h1": [-30, -12], "m5": [0.5, 8.0],
                                                                        "tp": 15, "sl": -10, "hold": 120})}
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for r in ok[:12]:
        print(r["p"], "TRAIN", r["train"], "TEST", r["test"])
    print("current", out["current"])


if __name__ == "__main__":
    main()
