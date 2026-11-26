"""Backtest του funding (delta-neutral: μισό spot, μισό short perp) στο ιστορικό ωριαίο funding του Hyperliquid.

Μόνο δημόσια δεδομένα, κανένα κλειδί. Αποτέλεσμα: docs/funding_bt.json
Υποθέσεις: κεφάλαιο 100$, τα κόστη όπως στο bot (spot 0,1% + perp 0,045% ανά πλευρά, στο μισό ποσό το καθένα),
το short έχει μόχλευση 1x (το άλλο μισό είναι εγγύηση). Δεν μετράμε: απόκλιση spot/perp τιμής κατά το
κλείσιμο (basis), κόστος δανεισμού, ρίσκο του ίδιου του exchange.
"""
import json
import os
import statistics
import time
import urllib.request

COINS = os.environ.get("FB_COINS", "BTC,ETH,SOL,HYPE,XRP,DOGE,BNB,AVAX,LINK,SUI,LTC,ADA").split(",")
DAYS = int(os.environ.get("FB_DAYS", "180"))
SPOT_FEE, PERP_FEE = 0.001, 0.00045
SWITCH = 0.5 * (SPOT_FEE + PERP_FEE)  # κόστος άνοιγμα ή κλείσιμο ανά 1$ κεφαλαίου (στο μισό το καθένα)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "funding_bt.json")


def post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            print("retry", e, flush=True)
            time.sleep(3 * (i + 1))
    return None


def history(coin, start_ms):
    rows, t = {}, start_ms
    while True:
        d = post({"type": "fundingHistory", "coin": coin, "startTime": t})
        time.sleep(0.4)
        if not d:
            break
        for r in d:
            rows[int(r["time"]) // 3600000] = float(r["fundingRate"])
        last = max(int(r["time"]) for r in d)
        if len(d) < 500 or last <= t:
            break
        t = last + 1
    return rows  # ώρα → ωριαίο rate


def simulate(H, hours, pick, every=168):
    """pick(t) → σύνολο coins που κρατάμε από την ώρα t. Ίσο κεφάλαιο ανά κρατημένο coin. Επιστρέφει ωριαίες αποδόσεις."""
    held, ret = set(), []
    for i, h in enumerate(hours):
        cost = 0.0
        if i % every == 0:
            new = pick(h)
            n_old, n_new = max(len(held), 1), max(len(new), 1)
            # κλείσιμο ό,τι φεύγει, άνοιγμα ό,τι μπαίνει (κατά προσέγγιση με ίσα βάρη)
            cost += SWITCH * (len(held - new) / n_old + len(new - held) / n_new)
            held = new
        r = statistics.mean(0.5 * H[c].get(h, 0.0) for c in held) if held else 0.0
        ret.append(r - cost)
    return ret


def summary(ret, hours):
    total = sum(ret)
    months = {}
    for r, h in zip(ret, hours):
        m = time.strftime("%Y-%m", time.gmtime(h * 3600))
        months[m] = months.get(m, 0.0) + r
    half = len(ret) // 2
    return {"total_pct": round(100 * total, 2), "apr_pct": round(100 * total * 8760 / len(ret), 2),
            "first_half_apr": round(100 * sum(ret[:half]) * 8760 / half, 2),
            "second_half_apr": round(100 * sum(ret[half:]) * 8760 / (len(ret) - half), 2),
            "months_pct": {k: round(100 * v, 2) for k, v in months.items()},
            "worst_month_pct": round(100 * min(months.values()), 2),
            "neg_hours_pct": round(100 * sum(r < 0 for r in ret) / len(ret), 1)}


def main():
    start = int((time.time() - DAYS * 86400) * 1000)
    H = {}
    for c in COINS:
        H[c] = history(c, start)
        print(c, len(H[c]), flush=True)
    H = {c: v for c, v in H.items() if len(v) > 24 * 30}
    hours = sorted(set.intersection(*[set(v) for v in H.values()]))
    hours = hours[24 * 7:]  # η πρώτη εβδομάδα χρειάζεται για τον μέσο όρο του παρελθόντος

    def past_avg(c, h, n=168):
        v = [H[c].get(x) for x in range(h - n, h) if H[c].get(x) is not None]
        return statistics.mean(v) if v else 0.0

    # κατώφλι: ο μέσος όρος της εβδομάδας πρέπει να καλύπτει άνοιγμα+κλείσιμο μέσα σε ~2 εβδομάδες
    th = 2 * SWITCH / (0.5 * 336)
    variants = {
        "now_btc_eth_sol": lambda h: {"BTC", "ETH", "SOL"} & set(H),
        "all_coins_always": lambda h: set(H),
        "on_off_btc_eth_sol": lambda h: {c for c in ("BTC", "ETH", "SOL") if c in H and past_avg(c, h) > th},
        "top3_weekly": lambda h: set(sorted((c for c in H if past_avg(c, h) > th),
                                            key=lambda c: -past_avg(c, h))[:3]),
    }
    out = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "days": len(hours) / 24,
           "coins": list(H), "threshold_hourly": th, "results": {},
           "per_coin_apr_pct": {c: round(100 * 0.5 * statistics.mean(H[c][h] for h in hours if h in H[c]) * 8760, 2)
                                for c in H}}
    for name, pick in variants.items():
        out["results"][name] = summary(simulate(H, hours, pick), hours)
        print(name, json.dumps({k: v for k, v in out["results"][name].items() if k != "months_pct"}), flush=True)
    print("per coin APR", out["per_coin_apr_pct"])
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
