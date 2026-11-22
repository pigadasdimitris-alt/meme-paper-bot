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

from . import config as C
from . import data, engine, live
from .report import write_readme

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STATE = DOCS / "state.json"
TRADES = DOCS / "trades.csv"
LIVE_STATE = DOCS / "live.json"
CONTROL = ROOT / "control.json"


def load_state(now):
    if STATE.exists():
        return json.loads(STATE.read_text())
    return engine.new_state(now)


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
    now = engine.now_utc()
    if not fresh and not pools:
        engine.log(state, now, "Δεν ήρθαν δεδομένα από το API σε αυτόν τον γύρο")
    engine.scan_and_enter(state, pools, now, flags_fn=data.token_flags,
                         verify_fn=lambda p: data.ds_price(p["pool"], p["base_mint"]))
    engine.snapshot(state, engine.now_utc())
    return pools


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
            engine.scan_and_enter(lst, pools, engine.now_utc(), flags_fn=data.token_flags,
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
    cycle = 0
    while True:
        t0 = time.time()
        pools = []
        try:
            pools = one_cycle(state)
            n = len(pools)
            print(f"[{engine.iso(engine.now_utc())}] evaluated={n} watch={len(state.get('watch', {}))} "
                  f"open={len(state['positions'])} "
                  f"closed={len(state['closed'])} equity=${engine.equity(state):.2f}", flush=True)
        except Exception as e:  # ένας κακός γύρος δεν σταματά το bot
            engine.log(state, engine.now_utc(), f"Σφάλμα γύρου: {e!r}")
            print("cycle error:", repr(e), file=sys.stderr, flush=True)
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
    if lst is not None:
        LIVE_STATE.write_text(json.dumps(lst, ensure_ascii=False, indent=1))
    git_commit(f"bot: τέλος γύρου {engine.iso(engine.now_utc())}")


if __name__ == "__main__":
    main()
