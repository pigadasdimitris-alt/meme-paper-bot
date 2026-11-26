"""ORB Nasdaq (paper, 100$): Opening Range Breakout 5 λεπτών στο QQQ.

Κανόνες (Zarattini & Aziz 2023, «Can Day Trading Really Be Profitable?»· βίντεο @thequantbuilder):
- Στις 9:30 Νέα Υόρκη (16:30 Ελλάδα) κοιτάμε το πρώτο κερί 5'. Αν έκλεισε πάνω από το άνοιγμα → long, κάτω → short.
  Αν σχεδόν δεν κουνήθηκε → καμία θέση εκείνη τη μέρα.
- Είσοδος στην τρέχουσα τιμή αμέσως μετά (μόνο ως τις 9:45· αργότερα παραλείπεται η μέρα).
- Stop στο άλλο άκρο του πρώτου κεριού. Στόχος 10 φορές το ρίσκο. Αλλιώς κλείσιμο στις 15:55.
- Ρίσκο 1% του κεφαλαίου ανά trade, μέγιστη μόχλευση 4x (όπως στην έρευνα).
- Κόστος 0,02% ανά πλευρά (spread + ολίσθηση). Η έρευνα υπέθετε σχεδόν μηδενικό· ανεξάρτητη αναπαραγωγή με κόστη
  βγήκε περίπου μηδέν — γι' αυτό το δοκιμάζουμε.
Stop/στόχος ελέγχονται με τα high/low των κεριών 5', άρα ο αργός κύκλος του bot δεν αλλοιώνει το αποτέλεσμα.
Αν stop και στόχος πιάνονται στο ίδιο κερί, μετράμε stop (συντηρητικά).
"""
import json
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import engine, majors

NY = ZoneInfo("America/New_York")
SYMBOL = "QQQ"
RISK = 0.01
MAX_LEV = 4.0
COST = 0.0002          # ανά πλευρά
TARGET_R = 10.0
MIN_BODY = 0.0001      # κερί σχεδόν χωρίς κίνηση → καμία θέση
LAST_ENTRY = (9, 45)
EXIT_AT = (15, 55)
EPOCH = "2026-11-26-orb"


def yahoo_bars(symbol=SYMBOL):
    """Κεριά 5' της ημέρας: λίστα (ts, o, h, l, c), μόνο ολοκληρωμένα + το τρέχον."""
    for host in ("query1", "query2"):
        try:
            req = urllib.request.Request(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            res = d["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(res.get("timestamp") or []):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None not in (o, h, l, c):
                    out.append((int(t), float(o), float(h), float(l), float(c)))
            return out
        except Exception:
            time.sleep(2)
    return None


def _ny(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(NY)


def _hm(dt):
    return (dt.hour, dt.minute)


def _close(st, pos, price, reason, now):
    side = 1 if pos["side"] == "long" else -1
    gross = side * (price - pos["entry"]) * pos["shares"]
    fees = COST * pos["shares"] * (price + pos["entry"])
    pnl = gross - fees
    st["balance"] += pnl
    st["positions"].remove(pos)
    st["closed"].append({
        "id": pos["id"], "name": pos["name"], "entry_time": pos["entry_time"], "exit_time": engine.iso(now),
        "hold_min": round((now - engine.parse_time(pos["entry_time"])).total_seconds() / 60, 1),
        "entry_market": pos["entry"], "exit_market": price, "cost": round(pos["risk_usd"], 4),
        "pnl": round(pnl, 4), "pnl_pct": round(100 * pnl / pos["equity_at_entry"], 2),
        "r_multiple": round(pnl / pos["risk_usd"], 2) if pos["risk_usd"] else 0.0, "reason": reason})
    engine.log(st, now, f"ΚΛΕΙΣΙΜΟ {pos['name']} — {reason} — {pnl:+.2f}$")


def cycle(st, now=None, bars_fn=yahoo_bars):
    now = now or engine.now_utc()
    now_ny = now.astimezone(NY)
    today = now_ny.date().isoformat()
    pos = st["positions"][0] if st["positions"] else None
    # εκτός ωραρίου και χωρίς θέση: τίποτα να κάνουμε (ούτε κλήση στο Yahoo)
    if pos is None and not ((9, 35) <= _hm(now_ny) <= LAST_ENTRY and now_ny.weekday() < 5):
        majors.snap(st, now)
        return
    bars = bars_fn()
    if not bars:
        raise RuntimeError("δεν ήρθαν τιμές QQQ από το Yahoo")
    day = [b for b in bars if _ny(b[0]).date().isoformat() == today]
    price = bars[-1][4]

    if pos is None and st.get("orb_day") != today:
        first = next((b for b in day if _hm(_ny(b[0])) == (9, 30)), None)
        done = any(_hm(_ny(b[0])) >= (9, 35) for b in day)   # το πρώτο κερί έχει κλείσει
        if first and done:
            st["orb_day"] = today
            _, o, h, l, c = first
            if abs(c - o) / o < MIN_BODY:
                engine.log(st, now, f"{today}: το πρώτο κερί δεν κουνήθηκε — καμία θέση")
            else:
                side = "long" if c > o else "short"
                stop = l if side == "long" else h
                risk_ps = abs(price - stop)
                if (side == "long" and price <= stop) or (side == "short" and price >= stop) or risk_ps <= 0:
                    engine.log(st, now, f"{today}: η τιμή είναι ήδη πέρα από το stop — καμία θέση")
                else:
                    eq = st["balance"]
                    shares = min(RISK * eq / risk_ps, MAX_LEV * eq / price)
                    tgt = price + TARGET_R * risk_ps if side == "long" else price - TARGET_R * risk_ps
                    pos = {"id": st["next_id"], "name": f"QQQ {'αγορά' if side == 'long' else 'short'}",
                           "side": side, "entry": price, "stop": stop, "target": tgt, "shares": shares,
                           "risk_usd": shares * risk_ps, "equity_at_entry": eq, "entry_time": engine.iso(now),
                           "entry_ts": int(now.timestamp()), "value": 0.0, "symbol": SYMBOL}
                    st["next_id"] += 1
                    st["positions"].append(pos)
                    engine.log(st, now, f"ΑΝΟΙΓΜΑ {pos['name']} @ {price:.2f} · stop {stop:.2f} · "
                                        f"στόχος {tgt:.2f} · ρίσκο {pos['risk_usd']:.2f}$")
                    pos = None   # ο έλεγχος stop/στόχου ξεκινά από το επόμενο κερί

    for pos in list(st["positions"]):
        long = pos["side"] == "long"
        for ts, o, h, l, c in bars:
            if ts + 300 <= pos["entry_ts"]:
                continue   # κεριά πριν την είσοδο
            if (l <= pos["stop"]) if long else (h >= pos["stop"]):
                fill = min(o, pos["stop"]) if long else max(o, pos["stop"])
                _close(st, pos, fill, "stop", now)
                break
            if (h >= pos["target"]) if long else (l <= pos["target"]):
                _close(st, pos, pos["target"], "στόχος 10R", now)
                break
        else:
            pnl = (price - pos["entry"]) * pos["shares"] * (1 if long else -1)
            pos["value"] = round(pnl - COST * pos["shares"] * pos["entry"], 4)
            entered_ny = _ny(pos["entry_ts"])
            if _hm(now_ny) >= EXIT_AT or now_ny.date() != entered_ny.date():
                _close(st, pos, price, "κλείσιμο αγοράς", now)
    st["stats"]["scans"] += 1
    majors.snap(st, now)
