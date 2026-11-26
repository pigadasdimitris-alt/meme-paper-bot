"""Κύριος βρόχος: τρέχει ~55 λεπτά, κάθε 60'' ελέγχει θέσεις και σαρώνει νέα coins, και αποθηκεύει στο repo."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import contextlib
import urllib.request

from . import compress, orb, config as C
from . import copybot, data, engine, live, majors
from .report import write_readme

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STATE = DOCS / "state.json"
TRADES = DOCS / "trades.csv"
LIVE_STATE = DOCS / "live.json"
CONTROL = ROOT / "control.json"
COPY_STATE = DOCS / "copy.json"
COPY_TRADES = DOCS / "copy_trades.csv"
LAB_DIR = DOCS / "lab"
LAB_SUMMARY = DOCS / "lab.json"


PAPER_EPOCH = "2026-11-23-established-100"   # νέα αρχή του paper με ειλικρινή καταγραφή των rugs


def load_state(now):
    if STATE.exists():
        st = json.loads(STATE.read_text())
        if st.get("epoch") == PAPER_EPOCH:
            if "losers" not in st:   # μία φορά: τα χαμένα trades του παρελθόντος μπαίνουν κι αυτά στην παύση 24ω
                st["losers"] = {t["mint"]: t["exit_time"] for t in st["closed"]
                                if t.get("pnl", 0) < 0 and ("stop" in t["reason"] or "rug" in t["reason"])}
            return st
        archive = DOCS / f"state_before_{PAPER_EPOCH}.json"
        if not archive.exists():
            archive.write_text(json.dumps(st, ensure_ascii=False))
    st = engine.new_state(now)
    st["epoch"] = PAPER_EPOCH
    return st


TRADE_COLS = ["id", "name", "dex", "entry_time", "exit_time", "hold_min", "entry_market", "exit_market",
              "cost", "proceeds", "pnl", "pnl_pct", "peak_pct", "reason", "pool", "mint"]


def save_copy(cst):
    COPY_STATE.write_text(json.dumps(cst, ensure_ascii=False, indent=1))
    with COPY_TRADES.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS, extrasaction="ignore")
        w.writeheader()
        for t in cst["closed"]:
            w.writerow(t)


def save(state):
    DOCS.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    cols = ["id", "name", "dex", "entry_time", "exit_time", "hold_min", "entry_market", "exit_market",
            "cost", "proceeds", "pnl", "pnl_pct", "peak_pct", "reason", "pool", "mint"]
    with TRADES.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in state["closed"]:
            w.writerow(t)
    write_readme(state, ROOT / "README.md")


def git_commit(msg):
    if os.environ.get("NO_GIT"):
        return
    subprocess.run(["git", "add", "docs", "README.md"], cwd=ROOT, check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=False)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=ROOT, check=False)


def one_cycle(state):
    now = engine.now_utc()
    engine.roll_day(state, now)
    if state["positions"]:
        quotes = data.pools_now(state["positions"])
        # δεύτερη πηγή για επιβεβαίωση απότομων αλλαγών (μόνο όταν η πρώτη ήταν GeckoTerminal)
        confirm = lambda pos: (data.ds_price(pos["pool"], pos["mint"])
                               if quotes.get(pos["pool"], {}).get("src") == "gt" else None)
        engine.manage_positions(state, quotes, engine.now_utc(), confirm_fn=confirm)
    fresh = data.new_pools(C.NEW_POOL_PAGES)
    now = engine.now_utc()
    engine.update_watchlist(state, fresh, now)
    pools = data.fetch_pools(engine.watch_due(state, now))
    for duration, page in C.TRENDING_SOURCES:
        pools += data.trending_pools(duration, page)
    # οι σελίδες των καθιερωμένων coins ελέγχονται εκ περιτροπής (3 ανά γύρο), για να μη μας "φρενάρει" το API
    pages = [("h24_volume_usd_desc", p) for p in range(1, getattr(C, "TOP_VOLUME_PAGES", 0) + 1)] + \
            [("h24_tx_count_desc", p) for p in range(1, getattr(C, "TOP_TX_PAGES", 0) + 1)]
    if pages:
        k = state["stats"].get("scans", 0) * 3
        for i in range(3):
            sort, page = pages[(k + i) % len(pages)]
            pools += data.top_volume_pools(page, sort=sort)
    now = engine.now_utc()
    if not fresh and not pools:
        engine.log(state, now, "Δεν ήρθαν δεδομένα από το API σε αυτόν τον γύρο")
    if getattr(C, "MOMENTUM_ON", True):
        engine.scan_and_enter(state, pools, now, flags_fn=data.token_flags, rug_fn=data.rugcheck,
                             verify_fn=lambda p: data.ds_price(p["pool"], p["base_mint"]))
    engine.snapshot(state, engine.now_utc())
    return pools


# ---------------- ΣΥΝΑΓΕΡΜΟΣ ----------------
# Αν μια στρατηγική βγάζει σφάλμα 3 γύρους στη σειρά, ανοίγει GitHub issue -> ειδοποίηση στο κινητό.

ERRORS = {}
ALERTED = set()


def alert(title, body):
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token) or title in ALERTED:
        return
    ALERTED.add(title)
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues", method="POST",
            data=json.dumps({"title": title, "body": body}).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "meme-bot"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("alert failed:", repr(e)[:200], file=sys.stderr, flush=True)


def health(name, err=None):
    if err is None:
        ERRORS[name] = 0
        return
    ERRORS[name] = ERRORS.get(name, 0) + 1
    if ERRORS[name] == 3:
        alert(f"ΠΡΟΒΛΗΜΑ: η στρατηγική {name} βγάζει σφάλμα",
              f"3 γύροι στη σειρά με σφάλμα στις {engine.iso(engine.now_utc())}.\n\n`{str(err)[:500]}`\n\n"
              "Πες στο Claude να το δει.")


# ---------------- ΕΡΓΑΣΤΗΡΙΟ ----------------

@contextlib.contextmanager
def overrides(params):
    params = {k: v for k, v in params.items() if k != "label"}
    missing = object()
    old = {k: getattr(C, k, missing) for k in params}
    for k, v in params.items():
        setattr(C, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:   # η ρύθμιση δεν υπήρχε: τη σβήνουμε (όχι None, που χαλάει τις συγκρίσεις)
                delattr(C, k)
            else:
                setattr(C, k, v)


def load_lab(now):
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in C.LAB:
        f = LAB_DIR / f"{name}.json"
        st = json.loads(f.read_text()) if f.exists() else None
        if not st or st.get("epoch") != C.LAB_EPOCH:
            st = engine.new_state(now)
            st.update({"epoch": C.LAB_EPOCH, "mode": name})
        out[name] = st
    return out


def trench_pools(st, now):
    """Πηγή για το «trench»: φρέσκα migrations + λίστα παρακολούθησης (ένα coin ελέγχεται για ώρες μετά το migration)."""
    cyc = st.setdefault("tcycle", 0)
    st["tcycle"] = cyc + 1
    fresh = data.fresh_migrations(new_pages=3, tx_pages=2 if cyc % 5 == 0 else 0)
    watch = st.setdefault("twatch", {})
    max_age = C.MAX_AGE_MIN * 60
    for p in fresh:
        created = engine.parse_time(p["created_at"])
        if p["pool"] and created and (now - created).total_seconds() < max_age:
            watch[p["pool"]] = p["created_at"]
    for k in [k for k, v in watch.items() if (now - engine.parse_time(v)).total_seconds() >= max_age]:
        del watch[k]
    known = {p["pool"] for p in fresh}
    rest = [a for a in watch if a not in known][:60]
    return fresh + (data.fetch_pools(rest) if rest else [])


def lab_cycle(st, pools):
    now = engine.now_utc()
    engine.roll_day(st, now)
    if getattr(C, "TRENCH_SOURCE", False):
        pools = trench_pools(st, now)
    if st["positions"]:
        quotes = data.pools_now(st["positions"])
        confirm = lambda pos: (data.ds_price(pos["pool"], pos["mint"])
                               if quotes.get(pos["pool"], {}).get("src") == "gt" else None)
        engine.manage_positions(st, quotes, engine.now_utc(), confirm_fn=confirm)
    engine.scan_and_enter(st, pools, engine.now_utc(), flags_fn=data.token_flags, rug_fn=data.rugcheck,
                         verify_fn=lambda p: data.ds_price(p["pool"], p["base_mint"]))
    engine.snapshot(st, engine.now_utc())


def lab_row(key, label, st):
    closed = st.get("closed") or []
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    hist = st.get("equity_history") or []
    eq = hist[-1][1] if hist else st.get("start_balance", 100.0)
    return {"key": key, "label": label, "equity": round(eq, 2), "start": st.get("start_balance", 100.0),
            "trades": len(closed), "wins": len(wins), "open": len(st.get("positions") or []),
            "rugs": sum(1 for t in closed if "rug" in str(t.get("reason"))), "last_run": st.get("last_run")}


TIMES = {}


MAJORS = {"trend": ("Trend BTC/ETH/SOL", majors.TREND_EPOCH, majors.trend_cycle),
          "funding": ("Funding arbitrage", majors.FUND_EPOCH, majors.funding_cycle),
          "orb": ("ORB Nasdaq (QQQ)", orb.EPOCH, orb.cycle),
          "compress": ("Compression breakout", compress.EPOCH, compress.cycle)}


def load_majors(now):
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    return {k: majors.load(LAB_DIR / f"{k}.json", now, ep, k) for k, (_, ep, _) in MAJORS.items()}


def save_lab(lab, state, cst, mj=None):
    for name, st in list(lab.items()) + list((mj or {}).items()):
        (LAB_DIR / f"{name}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))
    sep = []   # στρατηγικές με δικό τους runner (π.χ. trench): διαβάζουμε το αρχείο τους, δεν το γράφουμε
    for n, cfg in C.LAB.items():
        f = LAB_DIR / f"{n}.json"
        if cfg.get("SEPARATE") and f.exists():
            try:
                sep.append(lab_row(n, cfg["label"], json.loads(f.read_text())))
            except Exception:
                pass
    rows = ([lab_row("paper", "Momentum", state)] if getattr(C, "MOMENTUM_ON", True) else []) + \
           [lab_row(n, C.LAB[n]["label"], st) for n, st in lab.items()] + sep + \
           ([lab_row("copy", "Copy πορτοφολιών", cst)] if getattr(C, "COPY_BOT", False) else []) + \
           [lab_row(k, MAJORS[k][0], st) for k, st in (mj or {}).items()]
    rows.sort(key=lambda r: r["equity"], reverse=True)
    LAB_SUMMARY.write_text(json.dumps({"updated": engine.iso(engine.now_utc()), "since": "2026-11-24",
                                       "seconds": {k: round(v, 1) for k, v in TIMES.items()}, "rows": rows}, ensure_ascii=False, indent=1))


# ---------------- LIVE ----------------

@contextlib.contextmanager
def live_config():
    old = {k: getattr(C, k) for k in C.LIVE_OVERRIDES}
    for k, v in C.LIVE_OVERRIDES.items():
        setattr(C, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(C, k, v)


def read_control(previous):
    """Διαβάζει το control.json φρέσκο από το GitHub σε κάθε γύρο (για άμεσο STOP). Σε σφάλμα κρατά την προηγούμενη τιμή."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    try:
        if repo and token:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/contents/control.json?ref=main",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json",
                         "User-Agent": "meme-bot"})
            with urllib.request.urlopen(req, timeout=15) as r:
                cfg = json.loads(r.read().decode())
        else:
            cfg = json.loads(CONTROL.read_text())
        return "on" if str(cfg.get("live", "off")).lower() == "on" else "off"
    except Exception:
        return previous or "off"


def load_live(now):
    if LIVE_STATE.exists():
        return json.loads(LIVE_STATE.read_text())
    st = engine.new_state(now)
    st["mode"] = "live"
    st["start_balance"] = None
    return st


def live_cycle(lst, pools):
    with live_config():
        now = engine.now_utc()
        engine.roll_day(lst, now)
        prev = lst.get("control")
        lst["control"] = read_control(prev)
        if lst["control"] != prev:
            engine.log(lst, now, f"LIVE {'ΕΝΕΡΓΟ' if lst['control'] == 'on' else 'ΣΤΑΜΑΤΗΜΕΝΟ'}")
            if lst["control"] == "on" and prev == "off":
                # επανεκκίνηση με απόφαση του χρήστη: το όριο ημερήσιας ζημιάς μετράει από τώρα
                lst["daily"]["realized"] = 0.0
                engine.log(lst, now, "Το όριο ημερήσιας ζημιάς μετράει ξανά από τώρα")
        if lst["control"] == "off" and not lst["positions"]:
            lst["last_run"] = engine.iso(now)
            return
        w = live.wallet_snapshot()
        lst["wallet"] = {"address": live.owner(), **{k: round(v, 6) for k, v in w.items()}}
        lst["balance"] = w["sol_usd"]
        if lst["start_balance"] is None:
            lst["start_balance"] = round(engine.equity(lst), 2)
            engine.log(lst, now, f"LIVE ξεκίνησε με ${lst['start_balance']:.2f} ({w['sol']:.4f} SOL)")
        if lst["positions"]:
            quotes = data.pools_now(lst["positions"])
            confirm = lambda pos: (data.ds_price(pos["pool"], pos["mint"])
                                   if quotes.get(pos["pool"], {}).get("src") == "gt" else None)
            if lst["control"] == "off":  # STOP: πουλάμε ό,τι είναι ανοιχτό
                for pos in list(lst["positions"]):
                    q = quotes.get(pos["pool"], {})
                    engine.close_position(lst, pos, q.get("price", pos["last_price"]), engine.now_utc(), "STOP")
            else:
                engine.manage_positions(lst, quotes, engine.now_utc(), confirm_fn=confirm)
        if lst["control"] == "on":
            engine.scan_and_enter(lst, pools, engine.now_utc(), flags_fn=data.token_flags, rug_fn=data.rugcheck,
                                 verify_fn=lambda p: data.ds_price(p["pool"], p["base_mint"]))
        # επιστροφή ενοικίου από άδειους λογαριασμούς token (όσων πουλήθηκαν)
        try:
            n = live.close_empty_accounts()
            if n:
                engine.log(lst, engine.now_utc(), f"Κλείσιμο {n} άδειων λογαριασμών token — επιστροφή SOL")
        except Exception as e:
            print("close accounts:", str(e)[:150], file=sys.stderr, flush=True)
        # το υπόλοιπο σε SOL ξαναμετριέται από το blockchain μετά από αγορές/πωλήσεις
        if lst["positions"] or lst["closed"]:
            lst["balance"] = live.wallet_snapshot()["sol_usd"]
        # αυτόματη μεταφορά κερδών στο Revolut
        due = engine.payout_due(lst, engine.now_utc(), engine.equity(lst))
        if due > 0:
            w = live.wallet_snapshot()
            send_usd = min(due, w["sol_usd"] - live.SOL_RESERVE * w["sol_price"] - 0.5)
            if send_usd >= 5:
                try:
                    sig = live.send_sol(C.PAYOUT_ADDRESS, round(send_usd / w["sol_price"], 6), C.PAYOUT_ADDRESS)
                    lst.setdefault("payouts", []).append({"t": engine.iso(engine.now_utc()), "usd": round(send_usd, 2), "sig": sig})
                    lst["withdrawn_usd"] = round(lst.get("withdrawn_usd", 0.0) + send_usd, 2)
                    lst["balance"] = live.wallet_snapshot()["sol_usd"]
                    engine.log(lst, engine.now_utc(), f"ΜΕΤΑΦΟΡΑ ${send_usd:.2f} στο Revolut")
                except Exception as e:
                    engine.log(lst, engine.now_utc(), f"Αποτυχία μεταφοράς κερδών: {str(e)[:150]}")
        engine.snapshot(lst, engine.now_utc())


def main():
    run_minutes = float(os.environ.get("RUN_MINUTES", "55"))
    commit_every = int(os.environ.get("COMMIT_EVERY", "10"))
    deadline = time.time() + run_minutes * 60
    state = load_state(engine.now_utc())
    live_on = live.configured()
    lst = load_live(engine.now_utc()) if live_on else None
    if live_on:
        engine.LIVE_HOOKS.update({"buy": live.buy, "sell": live.sell})
    # κοινή λίστα rugs: ό,τι έκανε rug στο paper δεν αγοράζεται στο live και αντίστροφα
    engine.SHARED_RUGS.update(state.get("rug_names") or [])
    if lst is not None:
        engine.SHARED_RUGS.update(lst.get("rug_names") or [])
    cst = copybot.load(COPY_STATE, engine.now_utc())
    engine.SHARED_RUGS.update(cst.get("rug_names") or [])
    lab = {n: st for n, st in load_lab(engine.now_utc()).items() if not C.LAB[n].get("SEPARATE")}
    mj = load_majors(engine.now_utc())
    for st in lab.values():
        engine.SHARED_RUGS.update(st.get("rug_names") or [])
    cycle = 0
    while True:
        t0 = time.time()
        pools = []
        need_scan = getattr(C, "MOMENTUM_ON", True) or bool(lab) or bool(state["positions"]) or getattr(C, "COPY_BOT", False) or \
            (lst is not None and (lst.get("control") == "on" or lst.get("positions")))
        try:
            if not need_scan:   # καμία meme στρατηγική στο κοινό bot: δεν χρειάζεται σκανάρισμα
                raise StopIteration
            pools = one_cycle(state)
            TIMES["scan"] = time.time() - t0
            n = len(pools)
            print(f"[{engine.iso(engine.now_utc())}] evaluated={n} watch={len(state.get('watch', {}))} "
                  f"open={len(state['positions'])} "
                  f"closed={len(state['closed'])} equity=${engine.equity(state):.2f}", flush=True)
            health("momentum")
        except StopIteration:
            pass
        except Exception as e:  # ένας κακός γύρος δεν σταματά το bot
            health("momentum", e)
            engine.log(state, engine.now_utc(), f"Σφάλμα γύρου: {e!r}")
            print("cycle error:", repr(e), file=sys.stderr, flush=True)
        t1 = time.time()
        try:   # δεύτερο paper bot: αντιγραφή έξυπνων πορτοφολιών (σταματημένο: δεν έβγαζε trades)
            if not getattr(C, "COPY_BOT", False):
                raise StopIteration
            copybot.cycle(cst, pools)
            print(f"  copy: wallets={cst['stats'].get('tracked_wallets')} open={len(cst['positions'])} "
                  f"closed={len(cst['closed'])} equity=${engine.equity(cst):.2f}", flush=True)
            health("copy")
        except StopIteration:
            pass
        except Exception as e:
            health("copy", e)
            engine.log(cst, engine.now_utc(), f"Σφάλμα γύρου: {str(e)[:200]}")
            print("copy error:", str(e)[:200], file=sys.stderr, flush=True)
        save_copy(cst)
        TIMES["copy"] = time.time() - t1
        for name, st in lab.items():   # εργαστήριο: ίδια coins, άλλοι κανόνες
            t2 = time.time()
            try:
                with overrides(C.LAB[name]):
                    lab_cycle(st, pools)
                health(name)
            except Exception as e:
                health(name, e)
                engine.log(st, engine.now_utc(), f"Σφάλμα γύρου: {str(e)[:200]}")
                print(f"lab {name} error:", str(e)[:200], file=sys.stderr, flush=True)
            TIMES[name] = time.time() - t2
        for k, st in mj.items():   # BTC / ETH / SOL
            t2 = time.time()
            try:
                MAJORS[k][2](st)
                health(k)
            except Exception as e:
                health(k, e)
                engine.log(st, engine.now_utc(), f"Σφάλμα γύρου: {str(e)[:200]}")
                print(f"{k} error:", str(e)[:200], file=sys.stderr, flush=True)
            TIMES[k] = time.time() - t2
        print("  lab: " + " ".join(f"{n}=${engine.equity(st):.2f}" for n, st in lab.items()) + " " +
              " ".join(f"{k}=${majors.equity(st):.2f}" for k, st in mj.items()), flush=True)
        TIMES["cycle"] = time.time() - t0
        save_lab(lab, state, cst, mj)
        if lst is not None:
            try:
                live_cycle(lst, pools)
                print(f"  live: control={lst.get('control')} open={len(lst['positions'])} "
                      f"closed={len(lst['closed'])}", flush=True)
            except Exception as e:
                engine.log(lst, engine.now_utc(), f"Σφάλμα live γύρου: {str(e)[:200]}")
                print("live error:", str(e)[:200], file=sys.stderr, flush=True)
            LIVE_STATE.write_text(json.dumps(lst, ensure_ascii=False, indent=1))
        cycle += 1
        save(state)
        if cycle % commit_every == 0:
            git_commit(f"bot: ενημέρωση {engine.iso(engine.now_utc())}")
        if time.time() + C.POLL_SECONDS > deadline:
            break
        time.sleep(max(1, C.POLL_SECONDS - (time.time() - t0)))
    save(state)
    save_copy(cst)
    save_lab(lab, state, cst, mj)
    if lst is not None:
        LIVE_STATE.write_text(json.dumps(lst, ensure_ascii=False, indent=1))
    git_commit(f"bot: τέλος γύρου {engine.iso(engine.now_utc())}")


if __name__ == "__main__":
    main()
