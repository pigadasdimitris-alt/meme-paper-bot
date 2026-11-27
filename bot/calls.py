"""Telegram calls (paper, 100$): βγάζουν λεφτά τα «calls» των καναλιών αν μπαίνεις όταν τα διαβάζεις;

Κάθε 2 λεπτά διαβάζει τη δημόσια σελίδα (t.me/s/<κανάλι>) κάθε καναλιού της λίστας.
- Νέο μήνυμα (έως 30 λεπτά παλιό) με διεύθυνση Solana token που έχει pool με ρευστότητα ≥ 5.000$
  → αγορά 10$ στην τιμή εκείνης της στιγμής (DexScreener) + 4% slippage ανά πλευρά. Έως 10 θέσεις.
- Έξοδος: +100% (TP), −40% (SL), 12 ώρες, ή αν εξαφανιστεί η ρευστότητα (rug).
- Καταγράφει την ψηλότερη/χαμηλότερη τιμή κάθε trade (για να βρούμε αργότερα την καλύτερη έξοδο)
  και στατιστικά ανά κανάλι (πόσο συχνά ποστάρει, πόσα calls, αποτέλεσμα) — έτσι ξεχωρίζουμε τα ενεργά/καλά.
- Πρώτη ανάγνωση κάθε καναλιού: μόνο καταγραφή (δεν αγοράζει παλιά calls).
"""
import html as _html
import json
import re
import time
import urllib.request
from datetime import datetime

from . import engine, majors

CHANNELS = ["shockedcalls", "legends_call", "pyrotradingjournal", "dexpumps", "SIGNALCALLS"]
SIZE, MAX_OPEN = 10.0, 10
TP, SL, MAX_H = 1.00, -0.40, 12
SLIP = 0.04
MIN_LIQ, RUG_LIQ = 5000.0, 500.0
MAX_AGE_MIN = 30
FETCH_EVERY = 120
# αυτόματη ανακάλυψη: κανάλια που αναφέρονται/προωθούνται στα δικά μας δοκιμάζονται και, αν είναι ενεργά, μπαίνουν
PROBE_EVERY, PROBES_PER_RUN = 1800, 3
MAX_ACTIVE, RETIRE_DAYS, PROMOTE_DAYS, PROMOTE_CALLS = 25, 14, 3, 3
HANDLE = re.compile(r"(?:t\.me/|telegram\.me/|@)([A-Za-z][A-Za-z0-9_]{4,31})")
SKIP_H = ("addlist", "joinchat", "share", "iv", "proxy", "socks", "s", "c")
EPOCH = "2026-11-28-calls"
DS_TOK = "https://api.dexscreener.com/latest/dex/tokens/"
UA = {"User-Agent": "meme-paper-bot/1.0 (paper research)", "Accept": "text/html,application/json"}
B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")


def _get(url, as_json=True):
    req = urllib.request.Request(url, headers=UA)
    for i in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                return json.loads(body) if as_json else body
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def parse_channel(page):
    """[(id, datetime ή None, κείμενο, [διευθύνσεις])] από τη σελίδα t.me/s/<κανάλι>."""
    out = []
    for chunk in page.split('data-post="')[1:]:
        m = re.match(r"[^/\"]+/(\d+)\"", chunk)
        if not m:
            continue
        mid = int(m.group(1))
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        when = None
        if tm:
            try:
                when = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00"))
            except ValueError:
                pass
        text = _html.unescape(re.sub(r"<[^>]+>", " ", chunk))
        addrs = list(dict.fromkeys(B58.findall(text)))
        out.append((mid, when, re.sub(r"\s+", " ", text).strip()[:300], addrs))
    return out


def mentioned(page):
    """Κανάλια που αναφέρονται σε μια σελίδα (links t.me, @handles, προωθήσεις). Όχι bots."""
    out = set()
    for h in HANDLE.findall(page or ""):
        if h.lower() in SKIP_H or h.lower().endswith("bot"):
            continue
        out.add(h)
    return out


def fetch_page(name):
    return _get(f"https://t.me/s/{name}", as_json=False)


def fetch_channel(name):
    page = fetch_page(name)
    return None if page is None else parse_channel(page)


def channel_list(st):
    act = st.get("active", [])
    ret = set(st.get("retired", []))
    return [c for c in list(dict.fromkeys(CHANNELS + act)) if c not in ret]


def discover(st, now, page_fn=fetch_page):
    """Δοκιμάζει λίγα υποψήφια κανάλια: ενεργό = ποστ τις τελευταίες 3 μέρες και ≥3 μηνύματα με διεύθυνση token."""
    now_s = int(now.timestamp())
    if now_s - st.get("last_probe", 0) < PROBE_EVERY:
        return
    st["last_probe"] = now_s
    cand = st.setdefault("candidates", {})
    known = {c.lower() for c in channel_list(st) + st.get("retired", [])}
    todo = sorted((h for h, v in cand.items() if "probed" not in v and h.lower() not in known),
                  key=lambda h: -cand[h].get("seen", 0))[:PROBES_PER_RUN]
    for h in todo:
        page = page_fn(h)
        msgs = parse_channel(page) if page else []
        dated = [m[1] for m in msgs if m[1]]
        last = max(dated) if dated else None
        n_calls = sum(1 for m in msgs if m[3])
        fresh = last is not None and (now - last).total_seconds() < PROMOTE_DAYS * 86400
        cand[h].update({"probed": engine.iso(now), "msgs": len(msgs), "calls": n_calls,
                        "last_post": engine.iso(last) if last else None})
        if fresh and n_calls >= PROMOTE_CALLS and len(st.setdefault("active", [])) < MAX_ACTIVE:
            st["active"].append(h)
            cand[h]["added"] = True
            engine.log(st, now, f"ΝΕΟ ΚΑΝΑΛΙ {h} — {n_calls} calls στη σελίδα, τελευταίο ποστ {engine.iso(last)}")
        time.sleep(1)
    # λίστα υποψηφίων: κρατάμε τα 300 πιο «συχνά αναφερόμενα»
    if len(cand) > 300:
        for h in sorted(cand, key=lambda h: cand[h].get("seen", 0))[:len(cand) - 300]:
            del cand[h]


def ds_tokens(mints):
    """{mint: (τιμή, ρευστότητα, όνομα)} — το πιο ρευστό pool στη Solana για κάθε mint."""
    res = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        d = _get(DS_TOK + ",".join(mints[i:i + 30]))
        for p in (d or {}).get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            if mint not in mints:
                continue
            try:
                px = float(p.get("priceUsd") or 0)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                continue
            if px > 0 and (mint not in res or liq > res[mint][1]):
                res[mint] = (px, liq, (p.get("baseToken") or {}).get("symbol") or mint[:6])
    return res


def _close(st, p, px, now, reason):
    exit_eff = px * (1 - SLIP)
    pnl = (exit_eff / p["entry"] - 1) * p["notional"]
    st["balance"] += pnl
    st["positions"].remove(p)
    ch = st["channels"].setdefault(p["channel"], {})
    ch["pnl"] = round(ch.get("pnl", 0) + pnl, 4)
    ch["trades"] = ch.get("trades", 0) + 1
    ch["wins"] = ch.get("wins", 0) + (pnl > 0)
    st["closed"].append({
        "id": p["id"], "name": p["name"], "mint": p["mint"], "channel": p["channel"], "msg": p["msg"],
        "entry_time": p["entry_time"], "exit_time": engine.iso(now), "delay_min": p["delay_min"],
        "entry_market": p["entry_market"], "exit_market": px,
        "hold_min": round((now - engine.parse_time(p["entry_time"])).total_seconds() / 60, 1),
        "pnl": round(pnl, 4), "pnl_pct": round(100 * pnl / p["notional"], 2),
        "peak_pct": p["peak_pct"], "low_pct": p["low_pct"], "reason": reason})
    engine.log(st, now, f"ΚΛΕΙΣΙΜΟ {p['name']} [{p['channel']}] — {reason} — {pnl:+.2f}$ (κορυφή {p['peak_pct']:+.0f}%)")


def cycle(st, now=None, fetch_fn=None, price_fn=ds_tokens, channels=None, page_fn=fetch_page):
    now = now or engine.now_utc()
    now_s = int(now.timestamp())
    chans = st.setdefault("channels", {})
    # 1) τρέχουσες τιμές / έξοδοι
    if st["positions"]:
        px = price_fn([p["mint"] for p in st["positions"]])
        for p in list(st["positions"]):
            q = px.get(p["mint"])
            if q is None:
                p["missing"] = p.get("missing", 0) + 1
                if p["missing"] >= 30:                       # ~30 λεπτά χωρίς τιμή → θεωρείται χαμένο
                    _close(st, p, p["entry_market"] * 0.05, now, "χάθηκε η τιμή (rug;)")
                continue
            p["missing"] = 0
            price, liq, _ = q
            move = price / p["entry_market"] - 1
            p["peak_pct"] = round(max(p["peak_pct"], 100 * move), 1)
            p["low_pct"] = round(min(p["low_pct"], 100 * move), 1)
            p["last_pct"] = round(100 * move, 2)
            p["value"] = round((price * (1 - SLIP) / p["entry"] - 1) * p["notional"], 4)
            if liq < RUG_LIQ:
                _close(st, p, price * 0.5, now, "rug (έφυγε η ρευστότητα)")
            elif move >= TP:
                _close(st, p, price, now, f"TP +{int(TP * 100)}%")
            elif move <= SL:
                _close(st, p, price, now, f"SL {int(SL * 100)}%")
            elif now_s - p["entry_ts"] >= MAX_H * 3600:
                _close(st, p, price, now, f"{MAX_H} ώρες")
    # 2) νέα μηνύματα στα κανάλια
    if now_s - st.get("last_fetch", 0) >= FETCH_EVERY:
        st["last_fetch"] = now_s
        ok, new_calls = 0, []
        cand = st.setdefault("candidates", {})
        for name in channels or channel_list(st):
            c = chans.setdefault(name, {})
            if fetch_fn:
                msgs = fetch_fn(name)
            else:
                page = page_fn(name)
                msgs = None if page is None else parse_channel(page)
                for h in mentioned(page):
                    if h.lower() != name.lower():
                        cand.setdefault(h, {"from": name, "first": engine.iso(now)})
                        cand[h]["seen"] = cand[h].get("seen", 0) + 1
            if msgs is None:
                c["errors"] = c.get("errors", 0) + 1
                continue
            ok += 1
            if not msgs:
                continue
            last = max(m[0] for m in msgs)
            first_time = "last_id" not in c
            dated = [m[1] for m in msgs if m[1]]
            if dated:
                c["last_post"] = engine.iso(max(dated))
            for mid, when, text, addrs in sorted(msgs):
                if first_time or mid <= c.get("last_id", 0):
                    continue
                c["posts"] = c.get("posts", 0) + 1
                if addrs:
                    c["calls"] = c.get("calls", 0) + 1
                    age = (now - when).total_seconds() / 60 if when else 0
                    if age <= MAX_AGE_MIN:
                        new_calls.append((name, mid, round(age, 1), text, addrs[:3]))
            c["last_id"] = max(last, c.get("last_id", 0))
            lp = c.get("last_post")
            if lp and (now - engine.parse_time(lp)).total_seconds() > RETIRE_DAYS * 86400 and not channels:
                st.setdefault("retired", []).append(name)
                engine.log(st, now, f"ΑΠΟΣΥΡΣΗ ΚΑΝΑΛΙΟΥ {name} — κανένα ποστ για {RETIRE_DAYS}+ μέρες")
        if ok == 0 and (channels or channel_list(st)):
            raise RuntimeError("δεν διαβάστηκε κανένα κανάλι Telegram")
        if new_calls:
            cand = list(dict.fromkeys(a for *_, addrs in new_calls for a in addrs))
            info = price_fn(cand)
            recent = st.setdefault("traded", {})
            for name, mid, age, text, addrs in new_calls:
                for mint in addrs:
                    q = info.get(mint)
                    if not q or q[1] < MIN_LIQ:
                        continue
                    if now_s - recent.get(mint, 0) < 24 * 3600 or any(p["mint"] == mint for p in st["positions"]):
                        chans[name]["dupes"] = chans[name].get("dupes", 0) + 1
                        break
                    if len(st["positions"]) >= MAX_OPEN:
                        st["skipped"] = st.get("skipped", 0) + 1
                        break
                    price, liq, sym = q
                    recent[mint] = now_s
                    pos = {"id": st["next_id"], "mint": mint, "name": f"{sym}", "channel": name, "msg": mid,
                           "entry_market": price, "entry": price * (1 + SLIP), "notional": SIZE,
                           "entry_time": engine.iso(now), "entry_ts": now_s, "delay_min": age,
                           "entry_liq": round(liq), "peak_pct": 0.0, "low_pct": 0.0, "value": 0.0}
                    st["next_id"] += 1
                    st["positions"].append(pos)
                    engine.log(st, now, f"ΑΓΟΡΑ {sym} — call από {name} (πριν {age:.0f}′) @ {price:.6g}, ρευστ. {liq:,.0f}$")
                    break
            for k in [k for k, v in recent.items() if now_s - v > 3 * 86400]:
                del recent[k]
    if not channels:
        try:
            discover(st, now, page_fn)
        except Exception:
            pass
    st["stats"]["scans"] = st["stats"].get("scans", 0) + 1
    majors.snap(st, now)
