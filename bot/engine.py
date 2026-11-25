"""Λογική του bot: φίλτρα, εικονικές αγορές/πωλήσεις, όρια ρίσκου. Χωρίς δίκτυο — εύκολα testable."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config as C


# Για live λειτουργία: {"buy": fn(p, size_usd) -> dict, "sell": fn(pos) -> dict}. Κενό = μόνο paper.
LIVE_HOOKS = {}


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
    if p.get("quote_mint") and p["quote_mint"] not in C.QUOTE_MINTS:
        return "όχι ζεύγος SOL/USDC"
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
    spike = getattr(C, "MIN_VOL_SPIKE", 0.0)
    if spike and p["vol_m5"] < spike * max(p.get("vol_h1", 0.0) / 12.0, 1.0):
        return "χωρίς έκρηξη όγκου"
    if p["buys_m5"] / max(p["sells_m5"], 1) < getattr(C, "MIN_BUY_SELL_RATIO_M5", 0.0):
        return "πωλητές στα 5'"
    if p["pc_m5"] < C.MIN_PRICE_CHANGE_M5:
        return "χωρίς ορμή"
    if p["pc_h1"] < getattr(C, "MIN_PRICE_CHANGE_H1", -1e9):
        return "πτωτική ώρα"
    if p["pc_m5"] > C.MAX_PRICE_CHANGE_M5 or p["pc_h1"] > C.MAX_PRICE_CHANGE_H1:
        return "ήδη εκτοξευμένο"
    return None


def payout_due(state, now, equity_now):
    """Πόσα USD πρέπει να μεταφερθούν τώρα (0 αν όχι)."""
    if equity_now < C.PAYOUT_KEEP_USD + C.PAYOUT_TRIGGER_USD:
        return 0.0
    last = (state.get("payouts") or [{}])[-1].get("t")
    if last and (now - parse_time(last)).total_seconds() < C.PAYOUT_MIN_HOURS * 3600:
        return 0.0
    return round(equity_now - C.PAYOUT_KEEP_USD, 2)


def can_open(state):
    if len(state["positions"]) >= C.MAX_OPEN_POSITIONS:
        return "max ανοιχτά"
    if state["daily"].get("halted") or state["daily"]["realized"] <= -C.MAX_DAILY_LOSS_USD:
        # μόλις χτυπήσει το όριο, μένει σταματημένο ως τα μεσάνυχτα (ένα κέρδος μετά δεν το "ξεκλειδώνει")
        state["daily"]["halted"] = True
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
    live = state.get("mode") == "live"
    if live:
        try:
            r = LIVE_HOOKS["buy"](p, size)
        except Exception as e:
            log(state, now, f"ΑΠΟΤΥΧΙΑ ΑΓΟΡΑΣ {p['name']}: {str(e)[:160]}")
            return None
        qty, cost = r["qty"], r["cost_usd"]
        fill = cost / qty if qty else p["price"]
        state["balance"] -= cost
    else:
        fill = p["price"] * (1 + C.SLIPPAGE_PER_SIDE)
        qty = size / fill
        cost = size + C.FEE_USD_PER_TX
        state["balance"] -= cost
    pos = {
        "id": state["next_id"],
        "pool": p["pool"],
        "name": p["name"],
        "mint": p["base_mint"],
        "dex": p["dex"],
        "image": p.get("image"),
        "entry_time": iso(now),
        "entry_market": p["price"],
        "entry_fill": fill,
        "qty": qty,
        "cost": cost,
        "liq_entry": p["liquidity"],
        "peak_pct": 0.0,
        "stop_pct": C.STOP_LOSS_PCT,
        "last_price": p["price"],
        "last_pct": 0.0,
        "entry_signals": {k: p[k] for k in ("liquidity", "fdv", "vol_m5", "pc_m5", "buyers_m5", "buys_h1", "sells_h1")},
    }
    if live:
        pos.update({"raw": r.get("raw"), "decimals": r.get("decimals"), "buy_sig": r.get("sig")})
    state["next_id"] += 1
    state["positions"].append(pos)
    state["daily"]["entries"] += 1
    log(state, now, f"ΑΓΟΡΑ #{pos['id']} {p['name']} @ ${p['price']:.8g} (${cost:.2f})")
    return pos


SEED_RUG_NAMES = {"MCAT", "CATME"}   # γνωστά rugs από το πρώτο live


def symbol(name):
    return str(name).split("/")[0].strip().upper()


SHARED_RUGS = set()   # κοινή λίστα για paper και live (γεμίζει από το run.py)


def mark_rug(state, name):
    names = set(state.get("rug_names") or []) | SEED_RUG_NAMES
    names.add(symbol(name))
    state["rug_names"] = sorted(names)
    SHARED_RUGS.add(symbol(name))


NO_ROUTE_MARKERS = ("No routes", "NO_ROUTE", "Failed to get quotes")
NO_ROUTE_GIVE_UP = 4   # ~6 λεπτά χωρίς καμία διαδρομή πώλησης = το pool άδειασε (rug)


def is_rug_liquidity(pos, exit_liq):
    return exit_liq is not None and exit_liq < max(1000.0, 0.05 * pos.get("liq_entry", 0))


def close_position(state, pos, price, now, reason, exit_liq=None):
    sell_sig = None
    if state.get("mode") == "live":
        try:
            r = LIVE_HOOKS["sell"](pos)
        except Exception as e:
            msg = str(e)
            pos["sell_fail"] = pos.get("sell_fail", 0) + 1
            if any(m in msg for m in NO_ROUTE_MARKERS):
                pos["no_route"] = pos.get("no_route", 0) + 1
            if pos.get("no_route", 0) >= NO_ROUTE_GIVE_UP:
                # Δεν υπάρχει πια αγοραστής: καταγράφουμε τη ζημιά (ώστε να μετρήσει στο όριο ημέρας)
                # και ελευθερώνουμε τη θέση. Το token μένει στο πορτοφόλι για μελλοντική απόπειρα.
                state.setdefault("zombies", []).append({"id": pos["id"], "mint": pos["mint"], "pool": pos["pool"],
                                                        "name": pos["name"], "since": iso(now)})
                log(state, now, f"#{pos['id']} {pos['name']}: δεν πουλιέται (το pool άδειασε) — καταγραφή ως ζημιά")
                r = {"proceeds_usd": 0.0, "sig": None}
                reason = "rug — δεν πουλιέται"
            else:
                log(state, now, f"ΑΠΟΤΥΧΙΑ ΠΩΛΗΣΗΣ #{pos['id']} {pos['name']} ({reason}): {msg[:160]} — ξαναδοκιμάζει")
                return None
        proceeds, sell_sig = r["proceeds_usd"], r.get("sig")
    else:
        proceeds = pos["qty"] * price * (1 - C.SLIPPAGE_PER_SIDE) - C.FEE_USD_PER_TX
        if is_rug_liquidity(pos, exit_liq):
            # Paper: αν το pool έχει αδειάσει, στην πραγματικότητα δεν θα πουλούσαμε τίποτα.
            proceeds = 0.0
            reason = reason + " (rug)"
    proceeds = max(proceeds, 0.0)
    if "rug" in reason or (state.get("mode") == "live" and proceeds < 0.2 * pos["cost"]):
        mark_rug(state, pos["name"])
    pnl = proceeds - pos["cost"]
    if pnl < 0 and ("stop" in reason or "rug" in reason):
        # μαθαίνει από τα λάθη: coin που μας έβγαλε με ζημιά δεν ξαναγοράζεται για 24 ώρες
        state.setdefault("losers", {})[pos["mint"]] = iso(now)
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
        "exit_liq": round(exit_liq) if exit_liq is not None else None,
    })
    if sell_sig:
        closed["sell_sig"] = sell_sig
    state["positions"] = [x for x in state["positions"] if x["id"] != pos["id"]]
    state["closed"].append(closed)
    log(state, now, f"ΠΩΛΗΣΗ #{pos['id']} {pos['name']} — {reason} — {pnl:+.2f}$")
    return closed


def _liquidity_gone(pos, q):
    """Χρειάζονται 2 συνεχόμενες μετρήσεις (ίδιας πηγής) κάτω από το όριο — μία μόνη μπορεί να είναι λάθος του API."""
    liq = q.get("liquidity", 0)
    if q.get("src", "gt") != "gt" or "liquidity" not in q or pos["liq_entry"] <= 0:
        return False
    if liq < pos["liq_entry"] * C.LIQUIDITY_DROP_EXIT:
        pos["liq_low"] = pos.get("liq_low", 0) + 1
    else:
        pos["liq_low"] = 0
    return pos["liq_low"] >= 2


def agrees(a, b, tol=C.SOURCE_TOLERANCE):
    return a > 0 and b > 0 and (1 - tol) < a / b < (1 + tol)


def _resolve_price(state, pos, price, ratio, now, confirm_fn):
    """Απότομη αλλαγή >4x από την κύρια πηγή. Επιστρέφει αξιόπιστη τιμή ή None.
    - Αν η δεύτερη, ανεξάρτητη πηγή διαφωνεί αλλά συμφωνεί με την προηγούμενη αξιόπιστη τιμή,
      η κύρια πηγή έκανε λάθος → χρησιμοποιούμε τη δεύτερη (έτσι η θέση δεν "κολλάει").
    - Αν συμφωνούν και οι δύο, η αλλαγή γίνεται δεκτή αφού επιβεβαιωθεί και στον επόμενο γύρο.
    - Χωρίς δεύτερη πηγή: 3 συνεχόμενες ίδιες μετρήσεις για άνοδο, 2 για πτώση."""
    ref = pos.get("last_price") or pos["entry_market"]
    second = confirm_fn(pos) if confirm_fn else None
    if second is not None and not agrees(price, second):
        pos["suspect_price"], pos["suspect_count"] = None, 0
        if ref > 0 and 1 / C.MAX_JUMP < second / ref < C.MAX_JUMP:
            if not pos.get("glitch_logged"):
                log(state, now, f"#{pos['id']} {pos['name']}: λάθος τιμή από την κύρια πηγή — χρήση δεύτερης πηγής")
                pos["glitch_logged"] = True
            return second
        log(state, now, f"#{pos['id']} {pos['name']}: οι δύο πηγές διαφωνούν — η τιμή αγνοείται")
        return None
    sus = pos.get("suspect_price")
    if sus and agrees(price, sus):
        pos["suspect_count"] = pos.get("suspect_count", 1) + 1
    else:
        pos["suspect_price"], pos["suspect_count"] = price, 1
        log(state, now, f"#{pos['id']} {pos['name']}: ύποπτη τιμή ({ratio:.3g}x) — περιμένει επιβεβαίωση")
    need = 2 if second is not None else (3 if ratio > 1 else 2)
    return price if pos["suspect_count"] >= need else None


def manage_positions(state, quotes, now, confirm_fn=None):
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
        # Προστασία από λάθος τιμές: απότομη αλλαγή >4x (πάνω ή κάτω) μέσα σε έναν γύρο
        # γίνεται δεκτή μόνο αν επιβεβαιωθεί και στον επόμενο γύρο.
        ref = pos.get("last_price") or pos["entry_market"]
        ratio = price / ref if ref > 0 else 1.0
        if ratio > C.MAX_JUMP or ratio < 1 / C.MAX_JUMP:
            trusted = _resolve_price(state, pos, price, ratio, now, confirm_fn)
            if trusted is None:
                # καμία αξιόπιστη τιμή: η θέση δεν μένει ανοιχτή για πάντα
                if hold >= C.MAX_HOLD_MIN + 30:
                    close_position(state, pos, pos["last_price"], now, "λήξη χρόνου (αβέβαιη τιμή)")
                continue
            price = trusted
        pos.pop("suspect_price", None)
        pos.pop("suspect_count", None)
        pct = 100 * (price / pos["entry_market"] - 1)
        pos["last_price"] = price
        pos["last_pct"] = round(pct, 2)
        pos["peak_pct"] = max(pos["peak_pct"], pct)
        if pos["peak_pct"] >= C.BREAKEVEN_TRIGGER_PCT:
            pos["stop_pct"] = max(pos["stop_pct"], 0.0)
        trail = getattr(C, "TRAIL_PCT", None)
        if trail and pos["peak_pct"] >= trail:
            pos["stop_pct"] = max(pos["stop_pct"], pos["peak_pct"] - trail)

        liq = q.get("liquidity") if q.get("src", "gt") == "gt" and "liquidity" in q else None
        if pct >= C.TAKE_PROFIT_PCT:
            close_position(state, pos, price, now, "take profit", liq)
        elif pct <= pos["stop_pct"]:
            close_position(state, pos, price, now, "stop loss" if pos["stop_pct"] < 0 else
                           ("trailing stop" if pos["stop_pct"] > 0 else "stop στο 0"), liq)
        elif _liquidity_gone(pos, q):
            close_position(state, pos, price, now, "έπεσε η ρευστότητα", liq)
        elif hold >= C.MAX_HOLD_MIN:
            close_position(state, pos, price, now, "λήξη χρόνου", liq)


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


LOSER_COOLDOWN_H = 24


def recent_loser(state, mint, now):
    t = (state.get("losers") or {}).get(mint)
    return bool(t) and (now - parse_time(t)).total_seconds() < LOSER_COOLDOWN_H * 3600


def rug_reason(rc):
    """Κανόνες RugCheck. Αν δεν απαντήσει, δεν αγοράζουμε (ξαναδοκιμάζει στον επόμενο γύρο)."""
    if not rc.get("ok"):
        return "rugcheck μη διαθέσιμο"
    if rc.get("rugged") or rc.get("danger"):
        return "rugcheck: κίνδυνος"
    if rc.get("top10", 0) > getattr(C, "MAX_TOP10_PCT", 35.0):
        return "top-10 κάτοχοι πολλά"
    lp = rc.get("lp_locked")
    if lp is not None and lp < getattr(C, "MIN_LP_LOCKED_PCT", 90.0):
        return "LP ξεκλείδωτη"
    return None


def scan_and_enter(state, pools, now, flags_fn=None, verify_fn=None, rug_fn=None):
    """Αξιολογεί pools σε κάθε γύρο και ανοίγει εικονικές θέσεις στα καλύτερα.
    Κάθε coin ξαναελέγχεται σε κάθε γύρο (μπορεί να "ωριμάσει"), αλλά αγοράζεται το πολύ μία φορά."""
    state["stats"]["scans"] += 1
    reasons = state.setdefault("reasons", {})
    held = {p["pool"] for p in state["positions"]}
    held_mints = {p["mint"] for p in state["positions"]}
    candidates, done = [], set()
    for p in pools:
        if not p["pool"] or p["pool"] in done or p["pool"] in held or p["pool"] in state["seen"] \
                or p["base_mint"] in held_mints:
            continue
        done.add(p["pool"])
        state["stats"]["pools_checked"] += 1
        why = reject_reason(p, now)
        if not why and symbol(p["name"]) in (set(state.get("rug_names") or []) | SEED_RUG_NAMES | SHARED_RUGS):
            why = "όνομα που έκανε rug"
        if not why and recent_loser(state, p["base_mint"], now):
            why = "έχασε πρόσφατα (24ω)"
        if why:
            reasons[p["pool"]] = [why, iso(now), p["name"], round(p["liquidity"])]
            continue
        candidates.append(p)

    # προτεραιότητα: περισσότεροι αγοραστές, μετά όγκος
    candidates.sort(key=lambda p: (p["buyers_m5"], p["vol_m5"]), reverse=True)
    for p in candidates:
        blocked = can_open(state)
        if blocked:
            count_reject(state, blocked)
            reasons[p["pool"]] = [blocked, iso(now), p["name"], round(p["liquidity"])]
            continue
        if flags_fn:
            f = flags_fn(p["base_mint"])
            if f.get("image"):
                p = {**p, "image": f["image"]}
            if f.get("mint_authority") or f.get("freeze_authority"):
                state["seen"][p["pool"]] = iso(now)
                reasons[p["pool"]] = ["ύποπτο contract", iso(now), p["name"], round(p["liquidity"])]
                continue
        if p["base_mint"] in held_mints:
            continue
        if rug_fn and getattr(C, "RUGCHECK", False):
            rc = rug_fn(p["base_mint"], p["pool"])
            why = rug_reason(rc)
            if why:
                if rc.get("ok"):  # σίγουρη απόρριψη: δεν ξαναελέγχεται για λίγες ώρες
                    state["seen"][p["pool"]] = iso(now)
                reasons[p["pool"]] = [why, iso(now), p["name"], round(p["liquidity"])]
                continue
        if verify_fn:  # η τιμή αγοράς πρέπει να συμφωνεί και με δεύτερη πηγή
            second = verify_fn(p)
            if second is not None and not agrees(p["price"], second):
                reasons[p["pool"]] = ["ασυμφωνία τιμών", iso(now), p["name"], round(p["liquidity"])]
                continue
        state["seen"][p["pool"]] = iso(now)
        if open_position(state, p, now) is None:
            reasons[p["pool"]] = ["αποτυχία αγοράς", iso(now), p["name"], round(p["liquidity"])]
            continue
        reasons[p["pool"]] = ["ΑΓΟΡΑ", iso(now), p["name"], round(p["liquidity"])]
        held_mints.add(p["base_mint"])

    # κρατάμε τα αρχεία μικρά (τελευταίες ~6 ώρες)
    cutoff = now.timestamp() - getattr(C, "REENTRY_HOURS", 6) * 3600
    state["seen"] = {k: v for k, v in state["seen"].items() if parse_time(v).timestamp() > cutoff}
    state["losers"] = {k: v for k, v in (state.get("losers") or {}).items() if recent_loser(state, k, now)}
    state["reasons"] = {k: v for k, v in reasons.items() if parse_time(v[1]).timestamp() > cutoff}
    # σύνοψη λόγων απόρριψης ανά μοναδικό coin (τελευταίος λόγος του καθενός)
    summary = {}
    for why, *_ in state["reasons"].values():
        if why != "ΑΓΟΡΑ":
            summary[why] = summary.get(why, 0) + 1
    state["stats"]["rejects"] = summary


def equity(state):
    return state["balance"] + sum(
        max(p["qty"] * p["last_price"] * (1 - C.SLIPPAGE_PER_SIDE) - C.FEE_USD_PER_TX, 0) for p in state["positions"]
    )


def snapshot(state, now):
    # τα χρήματα που μεταφέρθηκαν στο Revolut μετράνε ως κέρδος (δεν χάθηκαν)
    state["equity_history"].append([iso(now), round(equity(state) + state.get("withdrawn_usd", 0.0), 4)])
    state["equity_history"] = state["equity_history"][-5000:]
    state["last_run"] = iso(now)
