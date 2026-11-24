"""Ανάγνωση δημόσιων live δεδομένων (GeckoTerminal, εφεδρικά DexScreener). Μόνο ανάγνωση."""
import json
import time
import urllib.error
import urllib.request

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com/latest/dex"
UA = {"User-Agent": "meme-paper-bot/1.0 (paper trading research)", "Accept": "application/json"}


def _get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _rel_id(rel, key):
    try:
        return rel[key]["data"]["id"].split("_", 1)[1]
    except Exception:
        return ""


def parse_gt_pool(p):
    """Μετατρέπει pool του GeckoTerminal σε απλό dict."""
    a = p.get("attributes", {}) or {}
    rel = p.get("relationships", {}) or {}
    tx = a.get("transactions", {}) or {}
    m5 = tx.get("m5", {}) or {}
    h1 = tx.get("h1", {}) or {}
    vol = a.get("volume_usd", {}) or {}
    pc = a.get("price_change_percentage", {}) or {}
    return {
        "pool": a.get("address", ""),
        "name": a.get("name", "?"),
        "base_mint": _rel_id(rel, "base_token"),
        "quote_mint": _rel_id(rel, "quote_token"),
        "dex": _rel_id(rel, "dex") or (rel.get("dex", {}).get("data", {}) or {}).get("id", ""),
        "price": _f(a.get("base_token_price_usd")),
        "created_at": a.get("pool_created_at"),
        "fdv": _f(a.get("fdv_usd")),
        "liquidity": _f(a.get("reserve_in_usd")),
        "vol_m5": _f(vol.get("m5")),
        "vol_h1": _f(vol.get("h1")),
        "pc_m5": _f(pc.get("m5")),
        "pc_h1": _f(pc.get("h1")),
        "pc_h6": _f(pc.get("h6")),
        "pc_h24": _f(pc.get("h24")),
        "buys_m5": int(_f(m5.get("buys"))),
        "sells_m5": int(_f(m5.get("sells"))),
        "buyers_m5": int(_f(m5.get("buyers"))),
        "buys_h1": int(_f(h1.get("buys"))),
        "sells_h1": int(_f(h1.get("sells"))),
    }


def new_pools(pages):
    out = []
    for page in range(1, pages + 1):
        d = _get(f"{GT}/networks/solana/new_pools?page={page}")
        if not d:
            break
        out += [parse_gt_pool(p) for p in d.get("data", [])]
        time.sleep(1.5)
    return out


def trending_pools(duration, page):
    d = _get(f"{GT}/networks/solana/trending_pools?duration={duration}&page={page}")
    time.sleep(1.5)
    return [parse_gt_pool(p) for p in (d or {}).get("data", [])]


def top_volume_pools(page, sort="h24_volume_usd_desc"):
    d = _get(f"{GT}/networks/solana/pools?page={page}&sort={sort}")
    time.sleep(1.5)
    return [parse_gt_pool(p) for p in (d or {}).get("data", [])]


def fetch_pools(addresses):
    """Πλήρη στοιχεία για συγκεκριμένα pools (έως 30 ανά κλήση)."""
    out = []
    addrs = list(addresses)
    for i in range(0, len(addrs), 30):
        d = _get(f"{GT}/networks/solana/pools/multi/{','.join(addrs[i:i + 30])}")
        out += [parse_gt_pool(p) for p in (d or {}).get("data", [])]
        time.sleep(1.5)
    return out


def pools_now(positions):
    """Τρέχουσα τιμή/ρευστότητα για τα ανοιχτά trades.
    Δέχεται μόνο τιμές όπου το token του pool είναι πράγματι το δικό μας (όχι ανάποδο ζευγάρι)."""
    res = {}
    if not positions:
        return res
    mint_of = {p["pool"]: p["mint"] for p in positions}
    addrs = list(mint_of)[:30]
    d = _get(f"{GT}/networks/solana/pools/multi/{','.join(addrs)}")
    if d:
        for p in d.get("data", []):
            q = parse_gt_pool(p)
            if q["pool"] in mint_of and q["base_mint"] == mint_of[q["pool"]] and q["price"] > 0:
                res[q["pool"]] = {"price": q["price"], "liquidity": q["liquidity"], "src": "gt"}
    missing = [a for a in addrs if a not in res]
    if missing:  # εφεδρικά: DexScreener (μόνο τιμή — η ρευστότητά του μετριέται αλλιώς)
        d = _get(f"{DS}/pairs/solana/{','.join(missing)}")
        for p in (d or {}).get("pairs", []) or []:
            addr = p.get("pairAddress")
            base = (p.get("baseToken") or {}).get("address")
            price = _f(p.get("priceUsd"))
            if addr in mint_of and base == mint_of[addr] and price > 0:
                res[addr] = {"price": price, "liquidity": 0.0, "src": "ds"}
    return res


def ds_price(pool, mint):
    """Τιμή από δεύτερη, ανεξάρτητη πηγή (DexScreener). None αν δεν υπάρχει."""
    d = _get(f"{DS}/pairs/solana/{pool}", retries=2)
    for p in (d or {}).get("pairs", []) or []:
        if p.get("pairAddress") == pool and (p.get("baseToken") or {}).get("address") == mint:
            price = _f(p.get("priceUsd"))
            return price if price > 0 else None
    return None


_CACHE = {}


def _cached(key, fn, ttl=1800):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if not (isinstance(val, dict) and val.get("ok") is False):   # αποτυχίες δεν αποθηκεύονται
        _CACHE[key] = (time.time(), val)
    return val


def token_flags(mint):
    return _cached(("flags", mint), lambda: _token_flags(mint))


def _token_flags(mint):
    """Έλεγχος απάτης όσο γίνεται χωρίς RPC: mint/freeze authority αν τα δίνει το API."""
    d = _get(f"{GT}/networks/solana/tokens/{mint}/info")
    if not d:
        return {"checked": False}
    a = (d.get("data") or {}).get("attributes", {}) or {}

    def active(v):
        return v not in (None, "", "no", "none", "null", False)

    return {
        "checked": True,
        "mint_authority": active(a.get("mint_authority")),
        "freeze_authority": active(a.get("freeze_authority")),
        "image": a.get("image_url") if str(a.get("image_url") or "").startswith("https://") else None,
    }


RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{}/report"
_RC_SKIP_TYPES = {"AMM", "LOCKER", "amm", "locker"}


def parse_rugcheck(rep, pool):
    """Μετατρέπει την αναφορά RugCheck σε απλά νούμερα: top10 %, LP κλειδωμένη %, επικίνδυνα ρίσκα."""
    known = rep.get("knownAccounts") or {}
    skip = {k for k, v in known.items() if isinstance(v, dict) and str(v.get("type", "")) in _RC_SKIP_TYPES}
    markets = rep.get("markets") or []
    for m in markets:
        skip.add(m.get("pubkey"))
        lp = m.get("lp") or {}
        for key in ("baseVault", "quoteVault"):
            if lp.get(key):
                skip.add(lp.get(key))
        for key in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if m.get(key):
                skip.add(m.get(key))
    top = 0.0
    n = 0
    for h in rep.get("topHolders") or []:
        if h.get("owner") in skip or h.get("address") in skip:
            continue
        top += _f(h.get("pct"))
        n += 1
        if n == 10:
            break
    lp_locked = None
    for m in markets:
        if m.get("pubkey") == pool:
            lp_locked = _f((m.get("lp") or {}).get("lpLockedPct"), None)
    danger = [r.get("name") for r in rep.get("risks") or [] if str(r.get("level")).lower() == "danger"]
    return {"ok": True, "top10": round(top, 1), "lp_locked": lp_locked, "danger": danger,
            "rugged": bool(rep.get("rugged"))}


def rugcheck(mint, pool):
    return _cached(("rc", mint, pool), lambda: _rugcheck(mint, pool))


def _rugcheck(mint, pool):
    rep = _get(RUGCHECK_API.format(mint), retries=3)
    if rep and isinstance(rep, dict):
        try:
            return parse_rugcheck(rep, pool)
        except Exception:
            pass
    # εφεδρικά: η σύντομη αναφορά (πιο ελαφριά). Τα top-10 εκεί ελέγχονται μέσω των "danger" ρίσκων.
    summ = _get(RUGCHECK_API.format(mint) + "/summary", retries=2)
    if summ and isinstance(summ, dict) and "risks" in summ:
        danger = [r.get("name") for r in summ.get("risks") or [] if str(r.get("level")).lower() == "danger"]
        lp = summ.get("lpLockedPct")
        return {"ok": True, "top10": 0.0, "lp_locked": _f(lp, None) if lp is not None else None,
                "danger": danger, "rugged": bool(summ.get("rugged"))}
    return {"ok": False}
