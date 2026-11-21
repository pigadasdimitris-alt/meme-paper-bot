from datetime import timedelta
from pathlib import Path

from bot import config as C
from bot import data, engine
from bot.report import write_readme


def pool(now, **kw):
    p = {
        "pool": "POOL1", "name": "TEST / SOL", "base_mint": "MINT1", "quote_mint": C.SOL_MINT, "dex": "raydium",
        "price": 0.001, "created_at": engine.iso(now - timedelta(minutes=30)), "fdv": 300_000, "liquidity": 40_000,
        "vol_m5": 10_000, "vol_h1": 80_000, "pc_m5": 8.0, "pc_h1": 60.0, "buys_m5": 80, "sells_m5": 40,
        "buyers_m5": 50, "buys_h1": 600, "sells_h1": 300,
    }
    p.update(kw)
    return p


def test_good_pool_passes():
    now = engine.now_utc()
    assert engine.reject_reason(pool(now), now) is None


def test_filters_reject():
    now = engine.now_utc()
    assert engine.reject_reason(pool(now, liquidity=1000), now) == "χαμηλή ρευστότητα"
    assert engine.reject_reason(pool(now, pc_m5=80), now) == "ήδη εκτοξευμένο"
    assert engine.reject_reason(pool(now, created_at=engine.iso(now - timedelta(minutes=3))), now) == "πολύ καινούργιο"
    assert engine.reject_reason(pool(now, base_mint=C.SOL_MINT), now) == "όχι meme token"
    assert engine.reject_reason(pool(now, buys_h1=100, sells_h1=300), now) == "πολλές πωλήσεις"


def test_take_profit_with_costs():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    assert len(s["positions"]) == 1
    assert abs(s["balance"] - (70 - 10 - C.FEE_USD_PER_TX)) < 1e-9
    engine.manage_positions(s, {"POOL1": {"price": 0.00135, "liquidity": 40_000}}, now + timedelta(minutes=5))
    assert not s["positions"]
    t = s["closed"][0]
    assert t["reason"] == "take profit"
    # +35% αγορά, μείον 2x2.5% slippage και fees → καθαρό κέρδος περίπου +28%
    assert 2.5 < t["pnl"] < 3.0


def test_stop_and_breakeven():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    engine.manage_positions(s, {"POOL1": {"price": 0.00118, "liquidity": 40_000}}, now + timedelta(minutes=2))
    assert s["positions"][0]["stop_pct"] == 0.0
    engine.manage_positions(s, {"POOL1": {"price": 0.00099, "liquidity": 40_000}}, now + timedelta(minutes=4))
    assert s["closed"][0]["reason"] == "stop στο 0"
    assert s["closed"][0]["pnl"] < 0  # ακόμα και στο 0 χάνει λόγω κόστους — ρεαλιστικό


def test_rug_exit_and_daily_limit():
    now = engine.now_utc()
    s = engine.new_state(now)
    pools = [pool(now, pool=f"P{i}", base_mint=f"M{i}") for i in range(6)]
    engine.scan_and_enter(s, pools, now)
    assert len(s["positions"]) == C.MAX_OPEN_POSITIONS
    quotes = {p["pool"]: {"price": 0.0003, "liquidity": 5_000} for p in s["positions"]}
    engine.manage_positions(s, quotes, now + timedelta(minutes=3))
    assert len(s["closed"]) == 4 and s["daily"]["realized"] < -C.MAX_DAILY_LOSS_USD
    more = [pool(now, pool=f"Q{i}", base_mint=f"N{i}") for i in range(3)]
    engine.scan_and_enter(s, more, now + timedelta(minutes=4))
    assert not s["positions"]
    assert s["stats"]["rejects"].get("όριο ημερήσιας ζημιάς")


def test_suspicious_contract_skipped():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now, flags_fn=lambda m: {"mint_authority": True})
    assert not s["positions"]


def test_parse_gt_pool_shape():
    raw = {
        "id": "solana_ADDR",
        "attributes": {
            "address": "ADDR", "name": "WIF / SOL", "base_token_price_usd": "0.0123",
            "pool_created_at": "2026-11-21T15:00:00Z", "fdv_usd": "250000", "reserve_in_usd": "30000.5",
            "price_change_percentage": {"m5": "5.1", "h1": "40"},
            "transactions": {"m5": {"buys": 50, "sells": 20, "buyers": 30, "sellers": 15}, "h1": {"buys": 400, "sells": 200}},
            "volume_usd": {"m5": "9000", "h1": "70000"},
        },
        "relationships": {
            "base_token": {"data": {"id": "solana_MINTX", "type": "token"}},
            "quote_token": {"data": {"id": f"solana_{C.SOL_MINT}", "type": "token"}},
            "dex": {"data": {"id": "raydium", "type": "dex"}},
        },
    }
    p = data.parse_gt_pool(raw)
    assert p["base_mint"] == "MINTX" and p["price"] == 0.0123 and p["buyers_m5"] == 30 and p["dex"] == "raydium"


def test_readme_renders(tmp_path: Path):
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    engine.manage_positions(s, {"POOL1": {"price": 0.0008, "liquidity": 40_000}}, now + timedelta(minutes=1))
    engine.snapshot(s, now)
    out = tmp_path / "README.md"
    write_readme(s, out)
    assert "stop loss" in out.read_text()
