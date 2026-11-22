"""Δοκιμαστική μεταφορά ~1$ SOL στο Revolut, για να επιβεβαιωθεί ότι φτάνει σωστά."""
import json
import time
from pathlib import Path

from . import config as C
from . import live

out = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": False, "to": C.PAYOUT_ADDRESS}
try:
    w = live.wallet_snapshot()
    sol = round(1.0 / w["sol_price"], 6)
    out["sig"] = live.send_sol(C.PAYOUT_ADDRESS, sol, C.PAYOUT_ADDRESS)
    out.update({"ok": True, "sol": sol, "usd": 1.0})
except Exception as e:
    out["error"] = str(e)[:300]
Path("docs/payout_test.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
