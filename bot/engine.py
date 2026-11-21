"""Λογική του bot: φίλτρα, εικονικές αγορές/πωλήσεις, όρια ρίσκου. Χωρίς δίκτυο — εύκολα testable."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config as C


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def new_state(now):
    return {
        "version": 1,
        "started_at": iso(now),
        "start_balance": C.START_BALANCE_USD,
        "balance": C.START_BALANCE_USD,
        "positions": [],
        "closed": [],
        "seen": {},
        "daily": {"date": local_date(now), "realized": 0.0, "entries": 0},
        "stats": {"scans": 0, "pools_checked": 0, "rejects": {}},
        "equity_history": [],
        "log": [],
        "last_run": None,
        "next_id": 1,
    }


def local_date(now):
    return now.astimezone(ZoneInfo(C.TIMEZONE)).strftime("%Y-%m-%d")


def log(state, now, msg):
    state["log"].append({"t": iso(now), "msg": msg})
    state["log"] = state["log"][-300:]


def roll_day(state, now):
    d = local_date(now)
    if state["daily"]["date"] != d:
        state["daily"] = {"date": d, "realized": 0.0, "entries": 0}


# ---------------- Φίλτρα εισόδου ----------------

def reject_reason(p, now):
    """Επιστρέφει None αν το pool περνάει όλα τα φίλτρα, αλλιώς τον λόγο απόρριψης."""
    if p["base_mint"] in C.QUOTE_MINTS or not p["base_mint"]:
        return "όχι meme token"
    if p["price"] <= 0:
        return "χωρίς τιμή"
    created = parse_time(p["created_at"])
    if not created:
        return "χωρίς ηλικία"
    age = (now - created).total_seconds() / 60
    if age < C.MIN_AGE_MIN:
        return "πολύ καινούργιο"
    if age > C.MAX_AGE_MIN:
        return "πολύ παλιό"
    if p["liquidity"] < C.MIN_LIQUIDITY_USD:
        return "χαμηλή ρευστότητα"
    if not (C.MIN_FDV_USD <= p["fdv"] <= C.MAX_FDV_USD):
        return "FDV εκτός ορίων"
    if p["vol_m5"] < C.MIN_VOLUME_M5_USD:
        return "χαμηλός όγκος 5'"
    if p["buyers_m5"] < C.MIN_BUYERS_M5:
        return "λίγοι αγοραστές"
    sells = max(p["sells_h1"], 1)
    if p["buys_h1"] / sells < C.MIN_BUY_SELL_RATIO_H1:
        return "πολλές πωλήσεις"
    if p["pc_m5"] < C.MIN_PRICE_CHANGE_M5:
        return "χωρίς ορμή"
    if p["pc_m5"] > C.MAX_PRICE_CHANGE_M5 or p["pc_h1"] > C.MAX_PRICE_CHANGE_H1:
        return "ήδη εκτοξευμένο"
    return None


def can_open(state):
    if len(state["positions"]) >= C.MAX_OPEN_POSITIONS:
        return "max ανοιχτά"
    if state["daily"]["realized"] <= -C.MAX_DAILY_LOSS_USD:
        return "όριο ημερήσιας ζημιάς"
    if state["daily"]["entries"] >= C.MAX_ENTRIES_PER_DAY:
        return "max αγορές ημέρας"
    if state["balance"] < C.POSITION_SIZE_USD + C.FEE_USD_PER_TX:
        return "χωρίς υπόλοιπο"
    return None


def count_reject(state, reason):
    r = state["stats"]["rejects"]
    r[reason] = r.get(reason, 0) + 1


# ---------------- Εικονικές συναλλαγές ----------------

def open_position(state, p, now):
    size = C.POSITION_SIZE_USD
    fill = p["price"] * (1 + C.SLIPPAGE_PER_SIDE)
    qty = size / fill
    state["balance"] -= size + C.FEE_USD_PER_TX
    pos = {
        "id": state["next_id"],
        "pool": p["pool"],
        "name": p["name"],
        "mint": p["base_mint"],
        "dex": p["dex"],
        "entry_time": iso(now),
        "entry_market": p["price"],
        "entry_fill": fill,
        "qty": qty,
        "cost": size + C.FEE_USD_PER_TX,
        "liq_entry": p["liquidity"],
        "peak_pct": 0.0,
        "stop_pct": C.STOP_LOSS_PCT,
        "last_price": p["price"],
        "last_pct": 0.0,
        "entry_signals": {k: p[k] for k in ("liquidity", "fdv", "vol_m5", "pc_m5", "buyers_m5", "buys_h1", "sells_h1")},
    }
    state["next_id"] += 1
    state["positions"].append(pos)
    state["daily"]["entries"] += 1
    log(state, now, f"ΑΓΟΡΑ #{pos['id']} {p['name']} @ ${p['price']:.8g} (${size:.2f})")
    return pos


def close_position(state, pos, price, now, reason):
    proceeds = pos["qty"] * price * (1 - C.SLIPPAGE_PER_SIDE) - C.FEE_USD_PER_TX
    proceeds = max(proceeds, 0.0)
    pnl = proceeds - pos["cost"]
    state["balance"] += proceeds
    state["daily"]["realized"] += pnl
    hold = (now - parse_time(pos["entry_time"])).total_seconds() / 60
    closed = dict(pos)
    closed.update({
        "exit_time": iso(now),
        "exit_market": price,
        "proceeds": round(proceeds, 4),
        "pnl": round(pnl, 4),
        "pnl_pct": round(100 * pnl / pos["cost"], 2),
        "reason": reason,
        "hold_min": round(hold, 1),
    })
    state["positions"] = [x for x in state["positions"] if x["id"] != pos["id"]]
    state["closed"].append(closed)
    log(state, now, f"ΠΩΛΗΣΗ #{pos['id']} {pos['name']} — {reason} — {pnl:+.2f}$")
    return closed


def manage_positions(state, quotes, now):
    """Ελέγχει stop / take profit / χρόνο / rug για κάθε ανοιχτό trade."""
    for pos in list(state["positions"]):
        q = quotes.get(pos["pool"])
        hold = (now - parse_time(pos["entry_time"])).total_seconds() / 60
        if not q:
            # Χωρίς τιμή: αν λείπει για πολύ, θεωρούμε ότι χάθηκε (συνήθως rug/delist).
            if hold > C.MAX_HOLD_MIN + 30:
                close_position(state, pos, 0.0, now, "χάθηκε η τιμή (rug;)")
            continue
        price = q["price"]
        pct = 100 * (price / pos["entry_market"] - 1)
        pos["last_price"] = price
        pos["last_pct"] = round(pct, 2)
        pos["peak_pct"] = max(pos["peak_pct"], pct)
        if pos["peak_pct"] >= C.BREAKEVEN_TRIGGER_PCT:
            pos["stop_pct"] = max(pos["stop_pct"], 0.0)

        if pct >= C.TAKE_PROFIT_PCT:
            close_position(state, pos, price, now, "take profit")
        elif pct <= pos["stop_pct"]:
            close_position(state, pos, price, now, "stop loss" if pos["stop_pct"] < 0 else "stop στο 0")
        elif pos["liq_entry"] > 0 and q.get("liquidity", 0) > 0 and q["liquidity"] < pos["liq_entry"] * C.LIQUIDITY_DROP_EXIT:
            close_position(state, pos, price, now, "έπεσε η ρευστότητα")
        elif hold >= C.MAX_HOLD_MIN:
            close_position(state, pos, price, now, "λήξη χρόνου")


def update_watchlist(state, fresh_pools, now):
    """Τα πολύ καινούργια coins μπαίνουν σε λίστα παρακολούθησης για να ξαναελεγχθούν στα 10′+."""
    w = state.setdefault("watch", {})
    for p in fresh_pools:
        if not p["pool"] or p["base_mint"] in C.QUOTE_MINTS or not p["created_at"]:
            continue
        if p["liquidity"] >= C.MIN_LIQUIDITY_USD * C.WATCH_MIN_LIQ_FRACTION:
            w[p["pool"]] = p["created_at"]
    # καθάρισμα: ό,τι πέρασε το μέγιστο όριο ηλικίας
    state["watch"] = {k: v for k, v in w.items()
                      if parse_time(v) and (now - parse_time(v)).total_seconds() / 60 <= C.MAX_AGE_MIN}


def watch_due(state, now):
    """Pools της λίστας παρακολούθησης που έχουν πια την ηλικία που θέλουμε (πιο φρέσκα πρώτα)."""
    due = []
    for k, v in state.get("watch", {}).items():
        age = (now - parse_time(v)).total_seconds() / 60
        if C.MIN_AGE_MIN <= age <= C.MAX_AGE_MIN and k not in state["seen"]:
            due.append((age, k))
    due.sort()
    return [k for _, k in due[:C.WATCH_MAX_FETCH]]


def scan_and_enter(state, pools, now, flags_fn=None):
    """Αξιολογεί pools σε κάθε γύρο και ανοίγει εικονικές θέσεις στα καλύτερα.
    Κάθε coin ξαναελέγχεται σε κάθε γύρο (μπορεί να "ωριμάσει"), αλλά αγοράζεται το πολύ μία φορά."""
    state["stats"]["scans"] += 1
    reasons = state.setdefault("reasons", {})
    held = {p["pool"] for p in state["positions"]}
    candidates, done = [], set()
    for p in pools:
        if not p["pool"] or p["pool"] in done or p["pool"] in held or p["pool"] in state["seen"]:
            continue
        done.add(p["pool"])
        state["stats"]["pools_checked"] += 1
        why = reject_reason(p, now)
        if why:
            reasons[p["pool"]] = [why, iso(now)]
            continue
        candidates.append(p)

    # προτεραιότητα: περισσότεροι αγοραστές, μετά όγκος
    candidates.sort(key=lambda p: (p["buyers_m5"], p["vol_m5"]), reverse=True)
    for p in candidates:
        blocked = can_open(state)
        if blocked:
            count_reject(state, blocked)
            reasons[p["pool"]] = [blocked, iso(now)]
            continue
        if flags_fn:
            f = flags_fn(p["base_mint"])
            if f.get("mint_authority") or f.get("freeze_authority"):
                state["seen"][p["pool"]] = iso(now)
                reasons[p["pool"]] = ["ύποπτο contract", iso(now)]
                continue
        state["seen"][p["pool"]] = iso(now)
        reasons[p["pool"]] = ["ΑΓΟΡΑ", iso(now)]
        open_position(state, p, now)

    # κρατάμε τα αρχεία μικρά (τελευταίες ~6 ώρες)
    cutoff = now.timestamp() - 6 * 3600
    state["seen"] = {k: v for k, v in state["seen"].items() if parse_time(v).timestamp() > cutoff}
    state["reasons"] = {k: v for k, v in reasons.items() if parse_time(v[1]).timestamp() > cutoff}
    # σύνοψη λόγων απόρριψης ανά μοναδικό coin (τελευταίος λόγος του καθενός)
    summary = {}
    for why, _ in state["reasons"].values():
        if why != "ΑΓΟΡΑ":
            summary[why] = summary.get(why, 0) + 1
    state["stats"]["rejects"] = summary


def equity(state):
    return state["balance"] + sum(
        max(p["qty"] * p["last_price"] * (1 - C.SLIPPAGE_PER_SIDE) - C.FEE_USD_PER_TX, 0) for p in state["positions"]
    )


def snapshot(state, now):
    state["equity_history"].append([iso(now), round(equity(state), 4)])
    state["equity_history"] = state["equity_history"][-5000:]
    state["last_run"] = iso(now)
