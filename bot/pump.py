"""Pump & Dump (paper, 100$): αναγνωρίζει τα στημένα pumps στα κανάλια Telegram — και μετράει αν βγαίνουν λεφτά.

Τρία κομμάτια, όλα πάνω στα μηνύματα που διαβάζει το bot "Telegram calls":
1. ΑΣΠΙΔΑ: για κάθε νέο call ελέγχει την τελευταία ώρα πριν το call (GeckoTerminal):
   - άνοδος ≥ +100% πριν το call            → «άργησες, έχει ήδη φουσκώσει»
   - 3 πορτοφόλια έκαναν ≥ 50% των αγορών  → «λίγοι μάζεψαν πριν το call (insiders)»
   - οι μεγαλύτεροι αγοραστές πουλάνε ήδη   → «οι πρώτοι βγαίνουν»
   - ρευστότητα < 10.000$ / pool < 30 λεπτών → «ρηχό ή ολοκαίνουριο pool»
   0–1 σημάδια → αγορά 10$. 2+ σημάδια → ΔΕΝ αγοράζει, αλλά παρακολουθεί «σκιώδες» trade,
   ώστε να μετράμε αν η ασπίδα όντως γλιτώνει λεφτά (blocked vs taken).
2. INSIDERS: κρατά τα πορτοφόλια που μάζεψαν πριν το call. Αν πουλήσουν ≥ 50% όσων αγόρασαν
   ενώ κρατάμε → έξοδος «dump: πουλάνε οι insiders».
3. ΠΡΟΓΡΑΜΜΑΤΙΣΜΕΝΑ PUMPS («στις 16:00 GMT στέλνουμε το CA»): σημειώνει την ώρα, και 90″ πριν μπαίνει
   σε γρήγορη λειτουργία — διαβάζει το κανάλι κάθε 3″, αγοράζει μόλις βγει το CA και καταγράφει την
   τιμή κάθε 10″ (καμπύλη του pump) για 6 λεπτά. Έξοδος +50% / −30% / 30 λεπτά.
Κόστος: 4% slippage ανά πλευρά. Μόνο paper.
"""
import re
import time
from datetime import datetime, timedelta, timezone

from . import calls, data, engine, majors

EPOCH = "2026-11-28-pump"
SIZE, MAX_OPEN, SLIP = 10.0, 8, 0.04
TP, SL, MAX_H = 1.00, -0.40, 6                # pumps από calls
S_TP, S_SL, S_MAX_MIN = 0.50, -0.30, 30       # προγραμματισμένα pumps
RISK_MAX = 1
RUNUP, TOP3_SHARE, INSIDER_SOLD, MIN_LIQ, MIN_AGE_MIN = 1.0, 0.50, 0.30, 10000.0, 30
DUMP_EXIT = 0.50
FAST_LEAD, FAST_AFTER, FAST_POLL, PATH_EVERY = 90, 360, 3, 10
GT = data.GT
SCHED = re.compile(r"(\d{1,2})[:.](\d{2})\s*(AM|PM|am|pm)?\s*(GMT|UTC|utc|gmt)")
SCHED_WORDS = re.compile(r"pump|contract|\bCA\b|coin|token|launch", re.I)


# ---------------- δεδομένα ----------------

def gt_pool(mint):
    """(pool, τιμή, ρευστότητα, created_at) του μεγαλύτερου pool του token."""
    d = data._get(f"{GT}/networks/solana/tokens/{mint}/pools?page=1")
    best = None
    for p in (d or {}).get("data", [])[:5]:
        q = data.parse_gt_pool(p)
        if q["base_mint"] != mint or q["price"] <= 0:
            continue
        if best is None or q["liquidity"] > best[2]:
            best = (q["pool"], q["price"], q["liquidity"], q.get("created_at"), q["name"].split(" /")[0])
    return best


def gt_trades(pool):
    """[(ts, πορτοφόλι, buy/sell, $)] — τα ~300 τελευταία trades."""
    d = data._get(f"{GT}/networks/solana/pools/{pool}/trades")
    out = []
    for t in (d or {}).get("data", []):
        a = t.get("attributes") or {}
        ts = engine.parse_time(a.get("block_timestamp"))
        if ts is None:
            continue
        out.append((int(ts.timestamp()), a.get("tx_from_address"), a.get("kind"), data._f(a.get("volume_in_usd"))))
    return out


def gt_ohlcv(pool):
    """[(ts, open, high, low, close)] κεριά 5 λεπτών, τελευταίες ~2 ώρες."""
    d = data._get(f"{GT}/networks/solana/pools/{pool}/ohlcv/minute?aggregate=5&limit=24")
    rows = ((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    return sorted((int(r[0]), r[1], r[2], r[3], r[4]) for r in rows)


# ---------------- 1. ασπίδα ----------------

def risk(mint, now, pool_fn=gt_pool, trades_fn=gt_trades, ohlcv_fn=gt_ohlcv):
    pool = pool_fn(mint)
    if not pool:
        return None
    addr, price, liq, created = pool[:4]
    now_s = int(now.timestamp())
    flags, info = [], {"pool": addr, "price": price, "liq": round(liq), "name": pool[4] if len(pool) > 4 else mint[:6]}
    bars = ohlcv_fn(addr) or []
    past = [b for b in bars if now_s - 3600 <= b[0] <= now_s]
    if past and past[0][1] > 0:
        runup = price / past[0][1] - 1
        info["runup_pct"] = round(100 * runup, 1)
        if runup >= RUNUP:
            flags.append("φούσκωσε ήδη πριν το call")
    trades = [t for t in (trades_fn(addr) or []) if now_s - 3600 <= t[0] <= now_s]
    buys, sells = {}, {}
    for ts, w, kind, usd in trades:
        (buys if kind == "buy" else sells)[w] = (buys if kind == "buy" else sells).get(w, 0) + usd
    tot = sum(buys.values())
    top = sorted(buys, key=buys.get, reverse=True)
    if tot > 0:
        share = sum(buys[w] for w in top[:3]) / tot
        info["top3_share"] = round(share, 2)
        if share >= TOP3_SHARE and len(buys) >= 3:
            flags.append("λίγα πορτοφόλια μάζεψαν πριν το call")
        big = top[:5]
        sold = sum(sells.get(w, 0) for w in big) / max(1e-9, sum(buys[w] for w in big))
        info["insiders_sold"] = round(sold, 2)
        if sold >= INSIDER_SOLD:
            flags.append("οι πρώτοι αγοραστές πουλάνε ήδη")
    info["insiders"] = {w: round(buys[w], 2) for w in top[:5]}
    if liq < MIN_LIQ:
        flags.append("ρηχό pool")
    ct = engine.parse_time(created) if created else None
    if ct and (now - ct).total_seconds() < MIN_AGE_MIN * 60:
        flags.append("ολοκαίνουριο pool")
    info["flags"] = flags
    return info


# ---------------- θέσεις ----------------

def _open(st, now, kind, mint, name, channel, price, info, shadow=False, extra=None):
    pos = {"id": st["next_id"], "kind": kind, "mint": mint, "name": name, "channel": channel,
           "entry_market": price, "entry": price * (1 + SLIP), "notional": SIZE, "entry_time": engine.iso(now),
           "entry_ts": int(now.timestamp()), "pool": info.get("pool"), "flags": info.get("flags", []),
           "insiders": info.get("insiders", {}), "peak_pct": 0.0, "low_pct": 0.0, "value": 0.0, **(extra or {})}
    st["next_id"] += 1
    (st["shadow"] if shadow else st["positions"]).append(pos)
    if not shadow:
        engine.log(st, now, f"ΑΓΟΡΑ {name} [{channel}] — {kind} @ {price:.6g}"
                   + (f" · σημάδια: {', '.join(pos['flags'])}" if pos["flags"] else " · καθαρό"))
    else:
        engine.log(st, now, f"ΑΣΠΙΔΑ: δεν αγόρασα {name} [{channel}] — {', '.join(pos['flags'])}")
    return pos


def _close(st, p, px, now, reason, shadow=False):
    pnl = (px * (1 - SLIP) / p["entry"] - 1) * p["notional"]
    row = {k: p.get(k) for k in ("id", "kind", "mint", "name", "channel", "entry_time", "entry_market", "flags",
                                 "peak_pct", "low_pct", "sec_after", "path")}
    row.update({"exit_time": engine.iso(now), "exit_market": px, "pnl": round(pnl, 4),
                "pnl_pct": round(100 * pnl / p["notional"], 2), "reason": reason,
                "hold_min": round((now - engine.parse_time(p["entry_time"])).total_seconds() / 60, 1)})
    sh = st.setdefault("shield", {"taken": 0, "taken_pnl": 0.0, "blocked": 0, "blocked_pnl": 0.0})
    if shadow:
        st["shadow"].remove(p)
        st.setdefault("shadow_closed", []).append(row)
        sh["blocked"] += 1
        sh["blocked_pnl"] = round(sh["blocked_pnl"] + pnl, 4)
        return
    st["balance"] += pnl
    st["positions"].remove(p)
    st["closed"].append(row)
    if p["kind"] == "call":
        sh["taken"] += 1
        sh["taken_pnl"] = round(sh["taken_pnl"] + pnl, 4)
    engine.log(st, now, f"ΚΛΕΙΣΙΜΟ {p['name']} — {reason} — {pnl:+.2f}$ (κορυφή {p['peak_pct']:+.0f}%)")


def _manage(st, now, price_fn, trades_fn, shadow=False):
    book = st["shadow"] if shadow else st["positions"]
    if not book:
        return
    px = price_fn([p["mint"] for p in book])
    now_s = int(now.timestamp())
    check_dump = not shadow and now_s - st.get("last_dump_check", 0) >= 120
    if check_dump:
        st["last_dump_check"] = now_s
    for p in list(book):
        q = px.get(p["mint"])
        if q is None:
            p["missing"] = p.get("missing", 0) + 1
            if p["missing"] >= 30:
                _close(st, p, p["entry_market"] * 0.05, now, "χάθηκε η τιμή (rug;)", shadow)
            continue
        p["missing"] = 0
        price, liq, _ = q
        move = price / p["entry_market"] - 1
        p["peak_pct"] = round(max(p["peak_pct"], 100 * move), 1)
        p["low_pct"] = round(min(p["low_pct"], 100 * move), 1)
        p["last_pct"] = round(100 * move, 2)
        p["value"] = round((price * (1 - SLIP) / p["entry"] - 1) * p["notional"], 4)
        sched = p["kind"] == "scheduled"
        tp, sl, max_s = (S_TP, S_SL, S_MAX_MIN * 60) if sched else (TP, SL, MAX_H * 3600)
        if liq < 500:
            _close(st, p, price * 0.5, now, "rug (έφυγε η ρευστότητα)", shadow)
        elif move >= tp:
            _close(st, p, price, now, f"TP +{int(tp * 100)}%", shadow)
        elif not sched and p["peak_pct"] >= 100 * calls.LOCK_AT and move <= calls.LOCK_FLOOR:
            _close(st, p, price, now, f"κλείδωμα κέρδους (κορυφή +{p['peak_pct']:.0f}%)", shadow)
        elif move <= sl:
            _close(st, p, price, now, f"SL {int(sl * 100)}%", shadow)
        elif now_s - p["entry_ts"] >= max_s:
            _close(st, p, price, now, "χρόνος", shadow)
        elif check_dump and p.get("insiders") and p.get("pool"):
            tr = [t for t in (trades_fn(p["pool"]) or []) if t[0] >= p["entry_ts"] and t[1] in p["insiders"]]
            sold = sum(t[3] for t in tr if t[2] == "sell")
            bought = sum(p["insiders"].values())
            if bought > 0 and sold / bought >= DUMP_EXIT:
                _close(st, p, price, now, f"dump: οι insiders πούλησαν {100 * sold / bought:.0f}%", shadow)


# ---------------- 3. προγραμματισμένα pumps ----------------

def parse_schedule(text, when):
    """Ώρα (UTC) ενός ανακοινωμένου pump, π.χ. «at 16:00 PM GMT we send the CA». None αν δεν υπάρχει."""
    if not text or not when or not SCHED_WORDS.search(text):
        return None
    m = SCHED.search(text)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ap == "PM" and h < 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    w = when.astimezone(timezone.utc)
    at = w.replace(hour=h, minute=mi, second=0, microsecond=0)
    if at < w - timedelta(minutes=2):
        at += timedelta(days=1)
    return at if at - w <= timedelta(hours=24) else None


def fast_window(st, ev, fetch_fn, price_fn, clock=time.time, sleep=time.sleep):
    """Γρήγορη λειτουργία γύρω από ένα προγραμματισμένο pump."""
    at, end = ev["at"], ev["at"] + FAST_AFTER
    pos, last_path = None, 0
    engine.log(st, engine.now_utc(), f"PUMP σε {int(at - clock())}″ στο {ev['channel']} — γρήγορη λειτουργία")
    while clock() < end:
        t = clock()
        if pos is None and t >= at - 30:
            msgs = fetch_fn(ev["channel"]) or []
            for mid, when, text, addrs in sorted(msgs):
                if mid <= ev["after_id"] or not addrs:
                    continue
                info = price_fn(addrs[:3])
                hit = next(((a, info[a]) for a in addrs[:3] if a in info and info[a][1] >= 3000), None)
                if hit:
                    mint, (price, liq, sym) = hit
                    now = datetime.fromtimestamp(t, timezone.utc)
                    pos = _open(st, now, "scheduled", mint, sym, ev["channel"], price, {"liq": liq},
                                extra={"sec_after": round(t - at, 1), "path": [[round(t - at), 0.0]]})
                    ev["mint"] = mint
                    break
                ev["after_id"] = max(ev["after_id"], mid)
        if pos is not None and t - last_path >= PATH_EVERY:
            last_path = t
            q = price_fn([pos["mint"]]).get(pos["mint"])
            if q:
                pct = round(100 * (q[0] / pos["entry_market"] - 1), 1)
                pos.setdefault("path", []).append([round(t - at), pct])
                pos["peak_pct"] = max(pos["peak_pct"], pct)
                pos["low_pct"] = min(pos["low_pct"], pct)
                if pos in st["positions"]:
                    _manage(st, datetime.fromtimestamp(t, timezone.utc), price_fn, lambda p: [])
        sleep(FAST_POLL)
    ev["done"] = True
    if pos is None:
        engine.log(st, engine.now_utc(), f"PUMP {ev['channel']}: δεν βρέθηκε CA μέσα σε {FAST_AFTER // 60}′")


# ---------------- κύκλος ----------------

def cycle(st, now=None, feed=None, price_fn=calls.ds_tokens, risk_fn=risk, trades_fn=gt_trades,
          fetch_fn=calls.fetch_channel, clock=time.time, sleep=time.sleep):
    now = now or engine.now_utc()
    now_s = int(now.timestamp())
    st.setdefault("shadow", [])
    st.setdefault("events", [])
    seen = st.setdefault("seen_mints", {})
    msgs = feed if feed is not None else calls.FEED
    items, msgs[:] = list(msgs), []
    for channel, mid, when, text, addrs in items:
        at = parse_schedule(text, when)
        if at and not any(e["channel"] == channel and abs(e["at"] - at.timestamp()) < 600 for e in st["events"]):
            st["events"].append({"channel": channel, "at": int(at.timestamp()), "after_id": mid, "msg": mid})
            engine.log(st, now, f"ΑΝΑΚΟΙΝΩΣΗ PUMP στο {channel} για {at.strftime('%H:%M')} UTC")
        if not addrs or (when and (now - when).total_seconds() > calls.MAX_AGE_MIN * 60):
            continue
        for mint in addrs[:3]:
            if now_s - seen.get(mint, 0) < 24 * 3600:
                break
            if any(e.get("mint") == mint for e in st["events"]):
                break
            info = risk_fn(mint, now)
            if not info:
                continue
            seen[mint] = now_s
            shadow = len(info["flags"]) > RISK_MAX
            if not shadow and len(st["positions"]) >= MAX_OPEN:
                break
            _open(st, now, "call", mint, info.get("name") or mint[:6], channel, info["price"], info, shadow=shadow)
            break
    for e in st["events"]:
        if not e.get("done") and e["at"] - FAST_LEAD <= clock() < e["at"] + FAST_AFTER:
            fast_window(st, e, fetch_fn, price_fn, clock, sleep)
        elif not e.get("done") and clock() >= e["at"] + FAST_AFTER:
            e["done"] = True
    st["events"] = [e for e in st["events"] if now_s - e["at"] < 3 * 86400]
    for k in [k for k, v in seen.items() if now_s - v > 3 * 86400]:
        del seen[k]
    _manage(st, now, price_fn, trades_fn)
    _manage(st, now, price_fn, trades_fn, shadow=True)
    st["stats"]["scans"] = st["stats"].get("scans", 0) + 1
    majors.snap(st, now)
