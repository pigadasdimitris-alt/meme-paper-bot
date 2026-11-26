"""Ξεχωριστός, γρήγορος runner για τη στρατηγική «trench» (μόνο paper).

Τα φρέσκα pump.fun coins κινούνται σε δευτερόλεπτα: στο κοινό bot ο κύκλος κρατάει ~3 λεπτά, οπότε τα stops
«γλιστρούσαν» (−25% έγινε −54% / −97%). Εδώ οι ανοιχτές θέσεις ελέγχονται κάθε 15", το σκανάρισμα κάθε ~60",
σε δικό του runner του GitHub (άρα και δικό του όριο στο API του GeckoTerminal).
"""
import json
import os
import sys
import time

from bot import config as C
from bot import data, engine, run

NAME = "trench"
FAST_SECONDS = 15
SCAN_EVERY = 4          # σκανάρισμα κάθε 4 γρήγορους κύκλους (~60")


def check_positions(st):
    if not st["positions"]:
        return
    quotes = data.pools_now(st["positions"])
    confirm = lambda pos: (data.ds_price(pos["pool"], pos["mint"])
                           if quotes.get(pos["pool"], {}).get("src") == "gt" else None)
    engine.manage_positions(st, quotes, engine.now_utc(), confirm_fn=confirm)


def save(st):
    run.LAB_DIR.mkdir(parents=True, exist_ok=True)
    (run.LAB_DIR / f"{NAME}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))


def main():
    run_minutes = float(os.environ.get("RUN_MINUTES", "55"))
    commit_seconds = float(os.environ.get("COMMIT_SECONDS", "120"))
    deadline = time.time() + run_minutes * 60
    st = run.load_lab(engine.now_utc())[NAME]
    # Μετάβαση: όσο το κοινό bot (παλιός κώδικας) γράφει ακόμα το trench.json, δεν ξεκινάμε — αλλιώς δύο
    # προγράμματα θα έγραφαν το ίδιο αρχείο. Το cron ξαναδοκιμάζει κάθε 30'.
    last = engine.parse_time(st.get("last_run"))
    if st.get("runner") != "separate" and last and (engine.now_utc() - last).total_seconds() < 15 * 60:
        print("Το κοινό bot γράφει ακόμα το trench — ξαναδοκιμή αργότερα", flush=True)
        (run.ROOT / ".trench_skipped").write_text("1")   # ο workflow δεν ξεκινά αμέσως νέο γύρο (περιμένει το cron)
        return
    st["runner"] = "separate"
    for f in run.LAB_DIR.glob("*.json"):   # κοινή μαύρη λίστα rugs με τις άλλες στρατηγικές
        try:
            engine.SHARED_RUGS.update(json.loads(f.read_text()).get("rug_names") or [])
        except Exception:
            pass
    i, last_commit = 0, time.time()
    while True:
        t0 = time.time()
        try:
            with run.overrides(C.LAB[NAME]):
                if i % SCAN_EVERY == 0:
                    run.lab_cycle(st, [])          # πλήρης κύκλος: θέσεις + νέα coins
                else:
                    check_positions(st)            # μόνο οι ανοιχτές θέσεις
                    engine.snapshot(st, engine.now_utc())
            run.health(NAME)
        except Exception as e:  # ένας κακός κύκλος δεν σταματά τον runner
            run.health(NAME, e)
            engine.log(st, engine.now_utc(), f"Σφάλμα γύρου: {str(e)[:200]}")
            print("trench error:", str(e)[:200], file=sys.stderr, flush=True)
        save(st)
        if i % SCAN_EVERY == 0:
            print(f"[{engine.iso(engine.now_utc())}] trench open={len(st['positions'])} closed={len(st['closed'])} "
                  f"equity=${engine.equity(st):.2f} cycle={time.time() - t0:.1f}s", flush=True)
        if time.time() - last_commit >= commit_seconds:
            run.git_commit("trench update")
            last_commit = time.time()
        i += 1
        if time.time() + FAST_SECONDS > deadline:
            break
        time.sleep(max(1, FAST_SECONDS - (time.time() - t0)))
    save(st)
    run.git_commit("trench update")


if __name__ == "__main__":
    main()
