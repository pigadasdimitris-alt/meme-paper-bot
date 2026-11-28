"""Παραλλαγές εξόδου για τα Telegram calls (paper, 100$ η καθεμία).

Ακριβώς τα ίδια calls και οι ίδιες αγορές με το «Telegram calls» (10$, ρευστότητα ≥ 5.000$, έως 30′ μετά το call,
4% slippage ανά πλευρά) — αλλάζει ΜΟΝΟ ο τρόπος πώλησης, ώστε να δούμε ποιος βγάζει περισσότερα:
- calls_half: πουλάει τα ΜΙΣΑ στο +50%· τα υπόλοιπα τρέχουν ως +200%, με stop στο σημείο εισόδου (δεν γυρίζει σε ζημιά).
- calls_tp50: πουλάει όλο στο +50%.
Και οι δύο: −40% stop, 12 ώρες.
"""
from . import calls, engine, majors

VARIANTS = {
    "calls_half": {"half_at": 0.50, "tp": 2.00, "sl": -0.40},
    "calls_tp50": {"half_at": None, "tp": 0.50, "sl": -0.40},
}
EPOCH = "2026-11-28-calls-exit"
QUEUES = {k: calls.SUBS.setdefault(k, []) for k in VARIANTS}


def _sell(st, p, frac, price, now, reason):
    notional = p["notional"] * frac
    pnl = (price * (1 - calls.SLIP) / p["entry"] - 1) * notional
    st["balance"] += pnl
    p["notional"] -= notional
    p["realized"] = round(p.get("realized", 0) + pnl, 4)
    engine.log(st, now, f"{'ΚΛΕΙΣΙΜΟ' if p['notional'] <= 1e-9 else 'ΠΩΛΗΣΗ ΜΙΣΩΝ'} {p['name']} — {reason} — {pnl:+.2f}$")
    if p["notional"] <= 1e-9:
        st["positions"].remove(p)
        st["closed"].append({
            "id": p["id"], "name": p["name"], "mint": p["mint"], "channel": p["channel"],
            "entry_time": p["entry_time"], "exit_time": engine.iso(now), "entry_market": p["entry_market"],
            "exit_market": price, "pnl": p["realized"], "pnl_pct": round(10 * p["realized"], 2),
            "peak_pct": p["peak_pct"], "low_pct": p["low_pct"], "reason": reason,
            "hold_min": round((now - engine.parse_time(p["entry_time"])).total_seconds() / 60, 1)})


def cycle(st, key, now=None, feed=None, price_fn=calls.ds_tokens):
    P = VARIANTS[key]
    now = now or engine.now_utc()
    now_s = int(now.timestamp())
    q = feed if feed is not None else QUEUES[key]
    items, q[:] = list(q), []
    recent = st.setdefault("traded", {})
    cand = [(ch, mid, when, addrs) for ch, mid, when, _t, addrs in items
            if addrs and (when is None or (now - when).total_seconds() <= calls.MAX_AGE_MIN * 60)]
    if cand:
        info = price_fn(list(dict.fromkeys(a for *_, addrs in cand for a in addrs[:3])))
        for ch, mid, when, addrs in cand:
            for mint in addrs[:3]:
                x = info.get(mint)
                if not x or x[1] < calls.MIN_LIQ:
                    continue
                if now_s - recent.get(mint, 0) < 24 * 3600 or len(st["positions"]) >= calls.MAX_OPEN:
                    break
                price, liq, sym = x
                recent[mint] = now_s
                st["positions"].append({"id": st["next_id"], "mint": mint, "name": sym, "channel": ch,
                                        "entry_market": price, "entry": price * (1 + calls.SLIP), "notional": calls.SIZE,
                                        "entry_time": engine.iso(now), "entry_ts": now_s, "peak_pct": 0.0,
                                        "low_pct": 0.0, "value": 0.0})
                st["next_id"] += 1
                engine.log(st, now, f"ΑΓΟΡΑ {sym} — call από {ch} @ {price:.6g}")
                break
    if st["positions"]:
        px = price_fn([p["mint"] for p in st["positions"]])
        for p in list(st["positions"]):
            x = px.get(p["mint"])
            if x is None:
                p["missing"] = p.get("missing", 0) + 1
                if p["missing"] >= 30:
                    _sell(st, p, 1.0, p["entry_market"] * 0.05, now, "χάθηκε η τιμή (rug;)")
                continue
            p["missing"] = 0
            price, liq, _ = x
            move = price / p["entry_market"] - 1
            p["peak_pct"] = round(max(p["peak_pct"], 100 * move), 1)
            p["low_pct"] = round(min(p["low_pct"], 100 * move), 1)
            p["last_pct"] = round(100 * move, 2)
            p["value"] = round(p.get("realized", 0) + (price * (1 - calls.SLIP) / p["entry"] - 1) * p["notional"], 4)
            halved = p.get("halved", False)
            if liq < calls.RUG_LIQ:
                _sell(st, p, 1.0, price * 0.5, now, "rug (έφυγε η ρευστότητα)")
            elif move >= P["tp"]:
                _sell(st, p, 1.0, price, now, f"TP +{int(P['tp'] * 100)}%")
            elif P["half_at"] and not halved and move >= P["half_at"]:
                p["halved"] = True
                _sell(st, p, 0.5, price, now, f"μισά στο +{int(P['half_at'] * 100)}%")
            elif halved and move <= 0:
                _sell(st, p, 1.0, price, now, "stop στην είσοδο (μετά τα μισά)")
            elif move <= P["sl"]:
                _sell(st, p, 1.0, price, now, f"SL {int(P['sl'] * 100)}%")
            elif now_s - p["entry_ts"] >= calls.MAX_H * 3600:
                _sell(st, p, 1.0, price, now, f"{calls.MAX_H} ώρες")
    for k in [k for k, v in recent.items() if now_s - v > 3 * 86400]:
        del recent[k]
    st["stats"]["scans"] = st["stats"].get("scans", 0) + 1
    majors.snap(st, now)
