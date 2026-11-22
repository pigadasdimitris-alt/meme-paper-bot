"""Δοκιμή live: αγορά ~1$ USDC με SOL και αμέσως πώληση πίσω. Επιβεβαιώνει κλειδί, Jupiter και RPC."""
import json
import time
from pathlib import Path

from . import live

out = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": False, "steps": []}
try:
    out["configured"] = live.configured()
    out["wallet"] = live.owner()
    w = live.wallet_snapshot()
    out["before"] = w
    out["steps"].append(f"wallet {w['sol']:.4f} SOL (${w['sol_usd']:.2f})")
    lamports = round(1.0 / w["sol_price"] * live.LAMPORTS)
    b = live.swap(live.SOL_MINT, live.USDC_MINT, lamports)
    out["steps"].append(f"buy USDC ok sig={b['signature']} got={b['out']/1e6:.4f} USDC")
    time.sleep(4)
    raw, dec = live.token_balance(live.USDC_MINT)
    out["steps"].append(f"USDC balance {raw/10**(dec or 6):.4f}")
    s = live.swap(live.USDC_MINT, live.SOL_MINT, raw)
    out["steps"].append(f"sell USDC ok sig={s['signature']} got={s['out']/live.LAMPORTS:.6f} SOL")
    time.sleep(3)
    out["after"] = live.wallet_snapshot()
    out["cost_usd"] = round(out["before"]["sol_usd"] - out["after"]["sol_usd"], 4)
    out["ok"] = True
except Exception as e:
    out["error"] = str(e)[:400]
Path("docs/live_smoke.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
