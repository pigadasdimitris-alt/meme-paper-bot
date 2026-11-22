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


def test_watchlist_matures():
    now = engine.now_utc()
    s = engine.new_state(now)
    young = pool(now, pool="Y1", created_at=engine.iso(now - timedelta(minutes=2)), liquidity=10_000)
    tiny = pool(now, pool="Y2", base_mint="M2", created_at=engine.iso(now - timedelta(minutes=2)), liquidity=500)
    engine.update_watchlist(s, [young, tiny], now)
    assert set(s["watch"]) == {"Y1"}
    assert engine.watch_due(s, now) == []
    later = now + timedelta(minutes=12)
    assert engine.watch_due(s, later) == ["Y1"]
    engine.update_watchlist(s, [], now + timedelta(minutes=200))
    assert s["watch"] == {}


def test_reevaluation_and_unique_reject_counts():
    now = engine.now_utc()
    s = engine.new_state(now)
    weak = pool(now, pc_m5=0.5)
    for i in range(5):
        engine.scan_and_enter(s, [weak], now + timedelta(minutes=i))
    assert s["stats"]["rejects"] == {"χωρίς ορμή": 1} and not s["positions"]
    engine.scan_and_enter(s, [pool(now)], now + timedelta(minutes=6))  # τώρα έχει ορμή → αγορά
    assert len(s["positions"]) == 1
    engine.scan_and_enter(s, [pool(now)], now + timedelta(minutes=7))  # δεν ξαναγοράζει
    assert len(s["positions"]) == 1


def test_price_spike_needs_confirmation():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    # λάθος τιμή 100.000x — αγνοείται
    engine.manage_positions(s, {"POOL1": {"price": 100.0, "liquidity": 40_000, "src": "gt"}}, now + timedelta(minutes=1))
    assert s["positions"] and not s["closed"]
    # επόμενη μέτρηση κανονική — η ύποπτη ξεχνιέται
    engine.manage_positions(s, {"POOL1": {"price": 0.00105, "liquidity": 40_000, "src": "gt"}}, now + timedelta(minutes=2))
    assert s["positions"] and "suspect_price" not in s["positions"][0]
    # πραγματικό rug: -95% δύο φορές στη σειρά → κλείνει
    q = {"POOL1": {"price": 0.00005, "liquidity": 40_000, "src": "gt"}}
    engine.manage_positions(s, q, now + timedelta(minutes=3))
    assert s["positions"]
    engine.manage_positions(s, q, now + timedelta(minutes=4))
    assert s["closed"] and s["closed"][0]["pnl"] < -9


def test_no_double_buy_same_token():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now, pool="A"), pool(now, pool="B")], now)
    assert len(s["positions"]) == 1


def test_dexscreener_liquidity_ignored():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.001, "liquidity": 0.0, "src": "ds"}}
    for i in range(3):
        engine.manage_positions(s, q, now + timedelta(minutes=i + 1))
    assert s["positions"]


def test_liquidity_exit_needs_two_readings():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.001, "liquidity": 5_000, "src": "gt"}}
    engine.manage_positions(s, q, now + timedelta(minutes=1))
    assert s["positions"]
    engine.manage_positions(s, q, now + timedelta(minutes=2))
    assert s["closed"][0]["reason"] == "έπεσε η ρευστότητα"


def _open_one(now):
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    return s


def test_bad_price_needs_second_source():
    now = engine.now_utc()
    bad = {"POOL1": {"price": 120.0, "liquidity": 40_000, "src": "gt"}}
    s = _open_one(now)
    for i in range(5):  # η κύρια πηγή επιμένει σε λάθος τιμή → χρησιμοποιείται η δεύτερη, ποτέ ψεύτικο κέρδος
        engine.manage_positions(s, bad, now + timedelta(minutes=i + 1), confirm_fn=lambda p: 0.0011)
    assert s["positions"] and not s["closed"]
    assert s["positions"][0]["last_price"] == 0.0011


def test_real_jump_accepted_when_sources_agree():
    now = engine.now_utc()
    s = _open_one(now)
    q = {"POOL1": {"price": 0.005, "liquidity": 40_000, "src": "gt"}}
    engine.manage_positions(s, q, now + timedelta(minutes=1), confirm_fn=lambda p: 0.0049)
    assert s["positions"]  # πρώτη μέτρηση: αναμονή
    engine.manage_positions(s, q, now + timedelta(minutes=2), confirm_fn=lambda p: 0.0049)
    assert s["closed"][0]["reason"] == "take profit"


def test_entry_rejected_on_price_disagreement():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now, verify_fn=lambda p: 0.01)
    assert not s["positions"] and s["reasons"]["POOL1"][0] == "ασυμφωνία τιμών"
    engine.scan_and_enter(s, [pool(now)], now, verify_fn=lambda p: None)  # χωρίς 2η πηγή → επιτρέπεται
    assert s["positions"]


def test_coin_image_is_kept():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now, flags_fn=lambda m: {"image": "https://x/y.png"})
    assert s["positions"][0]["image"] == "https://x/y.png"


def test_stuck_position_closes_by_time():
    now = engine.now_utc()
    s = _open_one(now)
    bad = {"POOL1": {"price": 120.0, "liquidity": 40_000, "src": "gt"}}
    engine.manage_positions(s, bad, now + timedelta(minutes=95), confirm_fn=lambda p: 50.0)  # και οι δύο τρελές
    assert s["closed"] and s["closed"][0]["reason"] == "λήξη χρόνου (αβέβαιη τιμή)"
    assert abs(s["closed"][0]["exit_market"] - 0.001) < 1e-12


def test_glitchy_primary_still_hits_time_exit():
    now = engine.now_utc()
    s = _open_one(now)
    bad = {"POOL1": {"price": 120.0, "liquidity": 40_000, "src": "gt"}}
    engine.manage_positions(s, bad, now + timedelta(minutes=61), confirm_fn=lambda p: 0.00102)
    assert s["closed"] and s["closed"][0]["reason"] == "λήξη χρόνου"


# ---------------- live (με ψεύτικα hooks, χωρίς δίκτυο) ----------------

def _live_state(now):
    s = engine.new_state(now)
    s["mode"] = "live"
    s["balance"] = 30.0
    return s


def test_live_buy_and_sell_use_real_amounts(monkeypatch):
    now = engine.now_utc()
    s = _live_state(now)
    monkeypatch.setattr(engine, "LIVE_HOOKS", {
        "buy": lambda p, size: {"qty": 3900.0, "raw": 3900_000000, "decimals": 6, "cost_usd": 3.98, "sig": "B"},
        "sell": lambda pos: {"proceeds_usd": 5.10, "sig": "S"},
    })
    engine.scan_and_enter(s, [pool(now)], now)
    pos = s["positions"][0]
    assert pos["cost"] == 3.98 and pos["buy_sig"] == "B" and abs(s["balance"] - 26.02) < 1e-9
    engine.manage_positions(s, {"POOL1": {"price": 0.00135, "liquidity": 40_000}}, now + timedelta(minutes=3))
    t = s["closed"][0]
    assert t["proceeds"] == 5.10 and abs(t["pnl"] - 1.12) < 1e-9 and t["sell_sig"] == "S"


def test_live_failed_buy_records_nothing(monkeypatch):
    now = engine.now_utc()
    s = _live_state(now)
    def boom(p, size):
        raise RuntimeError("no route")
    monkeypatch.setattr(engine, "LIVE_HOOKS", {"buy": boom, "sell": lambda pos: None})
    engine.scan_and_enter(s, [pool(now)], now)
    assert not s["positions"] and s["balance"] == 30.0
    assert s["reasons"]["POOL1"][0] == "αποτυχία αγοράς"


def test_live_failed_sell_keeps_position_and_retries(monkeypatch):
    now = engine.now_utc()
    s = _live_state(now)
    calls = {"n": 0}
    def flaky_sell(pos):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return {"proceeds_usd": 3.0, "sig": "S2"}
    monkeypatch.setattr(engine, "LIVE_HOOKS", {
        "buy": lambda p, size: {"qty": 4000.0, "cost_usd": 4.0, "sig": "B"}, "sell": flaky_sell})
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.0008, "liquidity": 40_000}}
    engine.manage_positions(s, q, now + timedelta(minutes=2))
    assert s["positions"] and s["positions"][0]["sell_fail"] == 1
    engine.manage_positions(s, q, now + timedelta(minutes=3))
    assert not s["positions"] and s["closed"][0]["sell_sig"] == "S2"


def test_payout_rule():
    now = engine.now_utc()
    s = engine.new_state(now)
    assert engine.payout_due(s, now, 49.0) == 0.0
    assert engine.payout_due(s, now, 52.4) == 22.4
    s["payouts"] = [{"t": engine.iso(now - timedelta(hours=3)), "usd": 20}]
    assert engine.payout_due(s, now, 60.0) == 0.0          # λιγότερο από 12 ώρες από την προηγούμενη
    s["payouts"] = [{"t": engine.iso(now - timedelta(hours=13)), "usd": 20}]
    assert engine.payout_due(s, now, 60.0) == 30.0


def test_paper_rug_exit_pays_nothing():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.0011, "liquidity": 300.0, "src": "gt"}}   # τιμή "ψηλά" αλλά το pool άδειασε
    engine.manage_positions(s, q, now + timedelta(minutes=2))
    engine.manage_positions(s, q, now + timedelta(minutes=3))
    t = s["closed"][0]
    assert t["proceeds"] == 0 and t["reason"].endswith("(rug)") and t["exit_liq"] == 300


def test_paper_normal_liquidity_drop_still_sells():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.0011, "liquidity": 15000.0, "src": "gt"}}  # έπεσε κάτω από 50% αλλά υπάρχει ρευστότητα
    engine.manage_positions(s, q, now + timedelta(minutes=2))
    engine.manage_positions(s, q, now + timedelta(minutes=3))
    assert s["closed"][0]["proceeds"] > 9


def test_live_unsellable_is_written_off_after_retries(monkeypatch):
    now = engine.now_utc()
    s = _live_state(now)
    def no_route(pos):
        raise RuntimeError('HTTP 400 {"error":"No routes found"}')
    monkeypatch.setattr(engine, "LIVE_HOOKS", {"buy": lambda p, size: {"qty": 4000.0, "cost_usd": 4.0}, "sell": no_route})
    engine.scan_and_enter(s, [pool(now)], now)
    q = {"POOL1": {"price": 0.0008, "liquidity": 40_000, "src": "gt"}}
    for i in range(engine.NO_ROUTE_GIVE_UP):
        engine.manage_positions(s, q, now + timedelta(minutes=2 + i))
    assert not s["positions"] and s["closed"][0]["pnl"] == -4.0 and s["zombies"][0]["mint"] == "MINT1"
    assert s["daily"]["realized"] == -4.0


def test_rugged_name_is_never_bought_again():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now, name="MCAT / SOL")], now)
    assert not s["positions"] and s["reasons"]["POOL1"][0] == "όνομα που έκανε rug"
    engine.scan_and_enter(s, [pool(now, pool="P2", base_mint="M2", name="ZZZ / SOL")], now)
    q = {"P2": {"price": 0.0011, "liquidity": 100.0, "src": "gt"}}
    engine.manage_positions(s, q, now + timedelta(minutes=2)); engine.manage_positions(s, q, now + timedelta(minutes=3))
    assert "ZZZ" in s["rug_names"]
    engine.scan_and_enter(s, [pool(now, pool="P3", base_mint="M3", name="zzz / SOL")], now + timedelta(minutes=4))
    assert not s["positions"]


def test_daily_halt_is_sticky_until_midnight():
    now = engine.now_utc()
    s = engine.new_state(now)
    s["daily"]["realized"] = -16.0
    assert engine.can_open(s) == "όριο ημερήσιας ζημιάς"
    s["daily"]["realized"] = -5.0          # ένα κέρδος μετά
    assert engine.can_open(s) == "όριο ημερήσιας ζημιάς"


def test_rug_in_one_book_blocks_the_other():
    now = engine.now_utc()
    engine.SHARED_RUGS.clear()
    paper = engine.new_state(now)
    engine.mark_rug(paper, "KOOP / SOL")
    live_s = engine.new_state(now)
    engine.scan_and_enter(live_s, [pool(now, name="Koop / SOL")], now)
    assert not live_s["positions"]
    engine.SHARED_RUGS.clear()
