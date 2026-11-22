"""Διάγνωση: γιατί αποτυγχάνει η πώληση ενός token (μόνο ανάγνωση, χωρίς συναλλαγές)."""
import json
import os
import sys
import time
from pathlib import Path

from . import live

mint, pool = sys.argv[1], sys.argv[2]
out = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mint": mint, "quotes": {}}
hdr = {"x-api-key": os.environ["JUPITER_API_KEY"]}
try:
    raw, dec = live.token_balance(mint)
    out["balance_raw"], out["decimals"] = raw, dec
    for frac in (1.0, 0.5, 0.1):
        amt = int(raw * frac)
        for name, url in [("v2", f"{live.JUP}/order?inputMint={mint}&outputMint={live.SOL_MINT}&amount={amt}"),
                          ("v1", f"https://api.jup.ag/swap/v1/quote?inputMint={mint}&outputMint={live.SOL_MINT}&amount={amt}&slippageBps=1500")]:
            try:
                r = live._http(url, headers=hdr)
                out["quotes"][f"{name}_{frac}"] = {k: r.get(k) for k in ("outAmount", "router", "errorCode", "errorMessage", "priceImpactPct")}
            except Exception as e:
                out["quotes"][f"{name}_{frac}"] = str(e)[:200]
    ds = live._http(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    out["pairs"] = [{"dex": p.get("dexId"), "pair": p.get("pairAddress"), "liq": (p.get("liquidity") or {}).get("usd"),
                     "price": p.get("priceUsd"), "vol5m": (p.get("volume") or {}).get("m5")} for p in (ds.get("pairs") or [])][:6]
except Exception as e:
    out["error"] = str(e)[:300]
Path("docs/diag.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
