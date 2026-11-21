"""Κύριος βρόχος: τρέχει ~55 λεπτά, κάθε 60'' ελέγχει θέσεις και σαρώνει νέα coins, και αποθηκεύει στο repo."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config as C
from . import data, engine
from .report import write_readme

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STATE = DOCS / "state.json"
TRADES = DOCS / "trades.csv"


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
        engine.manage_positions(state, quotes, engine.now_utc())
    fresh = data.new_pools(C.NEW_POOL_PAGES)
    now = engine.now_utc()
    engine.update_watchlist(state, fresh, now)
    pools = data.fetch_pools(engine.watch_due(state, now))
    for duration, page in C.TRENDING_SOURCES:
        pools += data.trending_pools(duration, page)
    now = engine.now_utc()
    if not fresh and not pools:
        engine.log(state, now, "Δεν ήρθαν δεδομένα από το API σε αυτόν τον γύρο")
    engine.scan_and_enter(state, pools, now, flags_fn=data.token_flags)
    engine.snapshot(state, engine.now_utc())
    return len(pools)


def main():
    run_minutes = float(os.environ.get("RUN_MINUTES", "55"))
    commit_every = int(os.environ.get("COMMIT_EVERY", "10"))
    deadline = time.time() + run_minutes * 60
    state = load_state(engine.now_utc())
    cycle = 0
    while True:
        t0 = time.time()
        try:
            n = one_cycle(state)
            print(f"[{engine.iso(engine.now_utc())}] evaluated={n} watch={len(state.get('watch', {}))} "
                  f"open={len(state['positions'])} "
                  f"closed={len(state['closed'])} equity=${engine.equity(state):.2f}", flush=True)
        except Exception as e:  # ένας κακός γύρος δεν σταματά το bot
            engine.log(state, engine.now_utc(), f"Σφάλμα γύρου: {e!r}")
            print("cycle error:", repr(e), file=sys.stderr, flush=True)
        cycle += 1
        save(state)
        if cycle % commit_every == 0:
            git_commit(f"bot: ενημέρωση {engine.iso(engine.now_utc())}")
        if time.time() + C.POLL_SECONDS > deadline:
            break
        time.sleep(max(1, C.POLL_SECONDS - (time.time() - t0)))
    save(state)
    git_commit(f"bot: τέλος γύρου {engine.iso(engine.now_utc())}")


if __name__ == "__main__":
    main()
