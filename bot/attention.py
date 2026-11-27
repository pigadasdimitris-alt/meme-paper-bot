"""Attention (paper, 100$): προηγείται η «προσοχή» του κόσμου της τιμής;

Υπόθεση (Maître et al., Journal of Banking & Finance 2025): ασυνήθιστη προσοχή σε ένα coin συνδέεται με θετική
απόδοση την ίδια και την επόμενη μέρα. Το δοκιμάζουμε ζωντανά, σε paper, πριν το χρησιμοποιήσουμε ως φίλτρο.

Κάθε ώρα:
- Διαβάζει τα trending του CoinGecko (από τις αναζητήσεις των χρηστών).
- Ένα coin που ΜΟΛΙς μπήκε στα trending (όχι τις προηγούμενες 24 ώρες) και υπάρχει ως perpetual στο Hyperliquid
  → αγορά 10$ στην τρέχουσα τιμή, κράτημα 24 ώρες, χωρίς stop (μετράμε το καθαρό φαινόμενο). Έως 10 θέσεις.
- Κόστος: fee 0,045% + slippage 0,10% ανά πλευρά + ωριαίο funding.
- Για κάθε σήμα καταγράφει και την κίνηση του BTC την ίδια περίοδο (excess return = coin − BTC),
  ώστε να ξεχωρίζουμε την «προσοχή» από το αν απλώς ανέβηκε όλη η αγορά.
- Καταγράφει επίσης (χωρίς trades) DexScreener boosts και GeckoTerminal trending για μελλοντική έρευνα στα meme coins.
"""
import json
import time
import urllib.request

from . import engine, majors

HL = "https://api.hyperliquid.xyz/info"
CG_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
DS_BOOSTS = "https://api.dexscreener.com/token-boosts/top/v1"
SIZE, MAX_OPEN, HOLD_H = 10.0, 10, 24
FEE, SLIP = 0.00045, 0.0010
FRESH_H = 24
EPOCH = "2026-11-27-attention"


def _get(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json",
                                                              "User-Agent": "meme-paper-bot/1.0 (paper research)"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def cg_trending():
    """Λίστα συμβόλων (κεφαλαία) με τη σειρά του CoinGecko trending."""
    d = _get(CG_TRENDING)
    if not d:
        return None
    return [str(c["item"]["symbol"]).upper() for c in d.get("coins", []) if c.get("item")]


def hl_market():
    """{coin: (mid, ωριαίο funding)} για όλα τα perps του Hyperliquid."""
    d = _get(HL, {"type": "metaAndAssetCtxs"})
    if not d:
        return None
    out = {}
    for u, c in zip(d[0]["universe"], d[1]):
        if u.get("isDelisted"):
            continue
        px = c.get("midPx") or c.get("markPx")
        if px:
            out[u["name"].upper()] = (float(px), float(c.get("funding") or 0))
    return out


def side_sources():
    """Μόνο καταγραφή: πόσα boosts στο DexScreener (τα 10 πρώτα)."""
    d = _get(DS_BOOSTS)
    if not isinstance(d, list):
        return None
    return [{"chain": x.get("chainId"), "token": x.get("tokenAddress"), "boost": x.get("totalAmount")} for x in d[:10]]


def _close(st, p, px, now, reason):
    gross = (px - p["entry"]) / p["entry"] * p["notional"]
    fees = (FEE + SLIP) * p["notional"] * (1 + px / p["entry"])
    pnl = gross - fees - p["funding_paid"]
    st["balance"] += pnl
    st["positions"].remove(p)
    btc_move = (p.get("btc_now", p["btc_entry"]) / p["btc_entry"] - 1) * 100 if p.get("btc_entry") else None
    coin_move = (px / p["entry"] - 1) * 100
    st["closed"].append({
        "id": p["id"], "name": p["name"], "coin": p["coin"], "entry_time": p["entry_time"],
        "exit_time": engine.iso(now), "entry_market": p["entry"], "exit_market": px,
        "hold_min": round((now - engine.parse_time(p["entry_time"])).total_seconds() / 60, 1),
        "pnl": round(pnl, 4), "pnl_pct": round(100 * pnl / p["notional"], 2), "move_pct": round(coin_move, 2),
        "btc_move_pct": round(btc_move, 2) if btc_move is not None else None,
        "excess_pct": round(coin_move - btc_move, 2) if btc_move is not None else None,
        "rank": p["rank"], "reason": reason})
    engine.log(st, now, f"ΚΛΕΙΣΙΜΟ {p['name']} — {reason} — {pnl:+.2f}$ (coin {coin_move:+.1f}%, BTC {btc_move:+.1f}%)")


def cycle(st, now=None, trending_fn=cg_trending, market_fn=hl_market, side_fn=side_sources):
    now = now or engine.now_utc()
    now_s = int(now.timestamp())
    hour = now_s // 3600
    if st.get("hour") == hour and not st["positions"]:
        majors.snap(st, now)
        return
    mkt = market_fn()
    if not mkt:
        raise RuntimeError("δεν ήρθαν τιμές από το Hyperliquid")
    btc = mkt.get("BTC", (None,))[0]
    # funding μία φορά την ώρα, λήξη 24ώρου
    new_hour = st.get("hour") != hour
    for p in list(st["positions"]):
        px, rate = mkt.get(p["coin"], (None, 0))
        if px is None:
            continue
        if new_hour:
            p["funding_paid"] += rate * p["notional"]
        p["btc_now"] = btc
        if now_s - p["entry_ts"] >= HOLD_H * 3600:
            _close(st, p, px, now, "24 ώρες")
    if new_hour:
        st["hour"] = hour
        tr = trending_fn()
        if tr is None:
            raise RuntimeError("δεν ήρθαν τα trending του CoinGecko")
        warmup = "tseen" not in st                        # πρώτη φορά: μόνο καταγραφή, όχι trades
        seen = st.setdefault("tseen", {})
        log = st.setdefault("tlog", [])
        log.append({"t": engine.iso(now), "cg": tr[:15]})
        try:
            side = side_fn()
            if side:
                log[-1]["ds_boosts"] = side
        except Exception:
            pass
        del log[:-240]                                    # τελευταίες ~10 μέρες
        for rank, sym in enumerate(tr[:15], 1):
            fresh = now_s - seen.get(sym, 0) >= FRESH_H * 3600
            seen[sym] = now_s
            if warmup or not fresh or sym not in mkt or sym in ("BTC", "ETH", "USDT", "USDC"):
                continue
            if len(st["positions"]) >= MAX_OPEN or any(p["coin"] == sym for p in st["positions"]):
                st.setdefault("skipped", 0)
                st["skipped"] += 1
                continue
            px = mkt[sym][0]
            pos = {"id": st["next_id"], "coin": sym, "name": f"{sym} (trending #{rank})", "rank": rank,
                   "entry": px, "notional": SIZE, "entry_time": engine.iso(now), "entry_ts": now_s,
                   "btc_entry": btc, "btc_now": btc, "funding_paid": 0.0, "value": 0.0}
            st["next_id"] += 1
            st["positions"].append(pos)
            engine.log(st, now, f"ΑΝΟΙΓΜΑ {sym} — μόλις μπήκε στα trending (#{rank}) @ {px:.6g}")
        for k in [k for k, v in seen.items() if now_s - v > 7 * 86400]:
            del seen[k]
    for p in st["positions"]:
        px = mkt.get(p["coin"], (p["entry"],))[0]
        p["last_pct"] = round(100 * (px / p["entry"] - 1), 2)
        p["value"] = round((px - p["entry"]) / p["entry"] * p["notional"]
                           - (FEE + SLIP) * p["notional"] * 2 - p["funding_paid"], 4)
    st["stats"]["scans"] = st["stats"].get("scans", 0) + 1
    majors.snap(st, now)
