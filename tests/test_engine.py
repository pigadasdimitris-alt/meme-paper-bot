from datetime import timedelta
from pathlib import Path

from bot import config as C
from bot import data, engine
from bot.report import write_readme


def pool(now, **kw):
    p = {
        "pool": "POOL1", "name": "TEST / SOL", "base_mint": "MINT1", "quote_mint": C.SOL_MINT, "dex": "raydium",
        "price": 0.001, "created_at": engine.iso(now - timedelta(days=3)), "fdv": 3_000_000, "liquidity": 150_000,
        "vol_m5": 10_000, "vol_h1": 80_000, "pc_m5": 3.0, "pc_h1": 12.0, "buys_m5": 80, "sells_m5": 40,
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
    assert abs(s["balance"] - (C.START_BALANCE_USD - 10 - C.FEE_USD_PER_TX)) < 1e-9
    engine.manage_positions(s, {"POOL1": {"price": 0.00135, "liquidity": 150_000}}, now + timedelta(minutes=5))
    assert not s["positions"]
    t = s["closed"][0]
    assert t["reason"] == "take profit"
    # +35% αγορά, μείον 2x2.5% slippage και fees → καθαρό κέρδος περίπου +28%
    assert 2.5 < t["pnl"] < 3.0


def test_stop_and_breakeven():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    engine.manage_positions(s, {"POOL1": {"price": 0.00118, "liquidity": 150_000}}, now + timedelta(minutes=2))
    assert s["positions"][0]["stop_pct"] == 0.0
    engine.manage_positions(s, {"POOL1": {"price": 0.00099, "liquidity": 150_000}}, now + timedelta(minutes=4))
    assert s["closed"][0]["reason"] == "stop στο 0"
    assert s["closed"][0]["pnl"] < 0  # ακόμα και στο 0 χάνει λόγω κόστους — ρεαλιστικό


def test_rug_exit_and_daily_limit(monkeypatch):
    monkeypatch.setattr(C, "MAX_DAILY_LOSS_USD", 15.0)
    now = engine.now_utc()
    s = engine.new_state(now)
    pools = [pool(now, pool=f"P{i}", base_mint=f"M{i}") for i in range(6)]
    engine.scan_and_enter(s, pools, now)
    assert len(s["positions"]) == C.MAX_OPEN_POSITIONS
    quotes = {p["pool"]: {"price": 0.0003, "liquidity": 5_000} for p in s["positions"]}
    engine.manage_positions(s, quotes, now + timedelta(minutes=3))
    assert len(s["closed"]) == 4 and s["daily"]["realized"] < -C.MAX_DAILY_LOSS_USD
    more = [pool(now, pool=f"Q{i}", base_mint=f"N{i}", name=f"Q{i} / SOL") for i in range(3)]
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
    engine.manage_positions(s, {"POOL1": {"price": 0.0008, "liquidity": 150_000}}, now + timedelta(minutes=1))
    engine.snapshot(s, now)
    out = tmp_path / "README.md"
    write_readme(s, out)
    assert "stop loss" in out.read_text()


def test_watchlist_matures():
    now = engine.now_utc()
    s = engine.new_state(now)
    young = pool(now, pool="Y1", created_at=engine.iso(now - timedelta(minutes=2)), liquidity=C.MIN_LIQUIDITY_USD)
    tiny = pool(now, pool="Y2", base_mint="M2", created_at=engine.iso(now - timedelta(minutes=2)), liquidity=500)
    engine.update_watchlist(s, [young, tiny], now)
    assert set(s["watch"]) == {"Y1"}
    assert engine.watch_due(s, now) == []
    later = now + timedelta(minutes=C.MIN_AGE_MIN + 2)
    assert engine.watch_due(s, later) == ["Y1"]
    engine.update_watchlist(s, [], now + timedelta(minutes=C.MAX_AGE_MIN + 10))
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
    engine.manage_positions(s, {"POOL1": {"price": 100.0, "liquidity": 150_000, "src": "gt"}}, now + timedelta(minutes=1))
    assert s["positions"] and not s["closed"]
    # επόμενη μέτρηση κανονική — η ύποπτη ξεχνιέται
    engine.manage_positions(s, {"POOL1": {"price": 0.00105, "liquidity": 150_000, "src": "gt"}}, now + timedelta(minutes=2))
    assert s["positions"] and "suspect_price" not in s["positions"][0]
    # πραγματικό rug: -95% δύο φορές στη σειρά → κλείνει
    q = {"POOL1": {"price": 0.00005, "liquidity": 150_000, "src": "gt"}}
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
    q = {"POOL1": {"price": 0.001, "liquidity": 50_000, "src": "gt"}}
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
    bad = {"POOL1": {"price": 120.0, "liquidity": 150_000, "src": "gt"}}
    s = _open_one(now)
    for i in range(5):  # η κύρια πηγή επιμένει σε λάθος τιμή → χρησιμοποιείται η δεύτερη, ποτέ ψεύτικο κέρδος
        engine.manage_positions(s, bad, now + timedelta(minutes=i + 1), confirm_fn=lambda p: 0.0011)
    assert s["positions"] and not s["closed"]
    assert s["positions"][0]["last_price"] == 0.0011


def test_real_jump_accepted_when_sources_agree():
    now = engine.now_utc()
    s = _open_one(now)
    q = {"POOL1": {"price": 0.005, "liquidity": 150_000, "src": "gt"}}
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
    bad = {"POOL1": {"price": 120.0, "liquidity": 150_000, "src": "gt"}}
    engine.manage_positions(s, bad, now + timedelta(minutes=C.MAX_HOLD_MIN + 35), confirm_fn=lambda p: 50.0)  # και οι δύο τρελές
    assert s["closed"] and s["closed"][0]["reason"] == "λήξη χρόνου (αβέβαιη τιμή)"
    assert abs(s["closed"][0]["exit_market"] - 0.001) < 1e-12


def test_glitchy_primary_still_hits_time_exit():
    now = engine.now_utc()
    s = _open_one(now)
    bad = {"POOL1": {"price": 120.0, "liquidity": 150_000, "src": "gt"}}
    engine.manage_positions(s, bad, now + timedelta(minutes=C.MAX_HOLD_MIN + 1), confirm_fn=lambda p: 0.00102)
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
    engine.manage_positions(s, {"POOL1": {"price": 0.00135, "liquidity": 150_000}}, now + timedelta(minutes=3))
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
    q = {"POOL1": {"price": 0.0008, "liquidity": 150_000}}
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
    q = {"POOL1": {"price": 0.0008, "liquidity": 150_000, "src": "gt"}}
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


def test_daily_halt_is_sticky_until_midnight(monkeypatch):
    monkeypatch.setattr(C, "MAX_DAILY_LOSS_USD", 15.0)
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


def test_only_sol_or_stable_pairs():
    now = engine.now_utc()
    assert engine.reject_reason(pool(now, quote_mint="XsNVDA"), now) == "όχι ζεύγος SOL/USDC"
    assert engine.reject_reason(pool(now), now) is None


def test_rugcheck_blocks_and_passes():
    from bot import data
    now = engine.now_utc()
    good = lambda m, p: {"ok": True, "top10": 20.0, "lp_locked": 100.0, "danger": [], "rugged": False}
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now, rug_fn=good)
    assert len(s["positions"]) == 1
    for rc, why in [({"ok": False}, "rugcheck μη διαθέσιμο"),
                    ({"ok": True, "top10": 60.0, "lp_locked": 100.0, "danger": []}, "top-10 κάτοχοι πολλά"),
                    ({"ok": True, "top10": 10.0, "lp_locked": 5.0, "danger": []}, "LP ξεκλείδωτη"),
                    ({"ok": True, "top10": 10.0, "lp_locked": 100.0, "danger": ["Freeze Authority"]}, "rugcheck: κίνδυνος")]:
        s = engine.new_state(now)
        engine.scan_and_enter(s, [pool(now)], now, rug_fn=lambda m, p, rc=rc: rc)
        assert not s["positions"] and s["reasons"]["POOL1"][0] == why
    # μη διαθέσιμο → ξαναδοκιμάζει στον επόμενο γύρο
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now, rug_fn=lambda m, p: {"ok": False})
    assert "POOL1" not in s["seen"]


def test_parse_rugcheck_skips_pool_accounts():
    from bot import data
    rep = {"knownAccounts": {"AMMOWNER": {"type": "AMM"}},
           "markets": [{"pubkey": "POOL1", "lp": {"lpLockedPct": 100}}],
           "topHolders": [{"owner": "AMMOWNER", "pct": 40}] + [{"owner": f"W{i}", "pct": 2} for i in range(12)],
           "risks": [{"name": "Mutable metadata", "level": "warn"}]}
    r = data.parse_rugcheck(rep, "POOL1")
    assert r["top10"] == 20.0 and r["lp_locked"] == 100.0 and r["danger"] == []


def test_no_rebuy_after_stop_loss():
    now = engine.now_utc()
    s = engine.new_state(now)
    engine.scan_and_enter(s, [pool(now)], now)
    pos = s["positions"][0]
    engine.close_position(s, pos, pos["entry_market"] * 0.85, now, "stop loss")
    s["seen"] = {}
    later = now + timedelta(hours=3)
    engine.scan_and_enter(s, [pool(later)], later)
    assert not s["positions"] and s["reasons"]["POOL1"][0] == "έχασε πρόσφατα (24ω)"


def test_trailing_stop_and_lab_overrides():
    from bot import run
    now = engine.now_utc()
    with run.overrides(C.LAB_RETIRED["trail"]):
        s = engine.new_state(now)
        engine.scan_and_enter(s, [pool(now)], now)
        pos = s["positions"][0]
        e = pos["entry_market"]
        engine.manage_positions(s, {"POOL1": {"price": e * 1.5, "liquidity": 150_000, "src": "gt"}}, now)
        assert s["positions"]  # δεν υπάρχει σταθερό take profit
        engine.manage_positions(s, {"POOL1": {"price": e * 1.40, "liquidity": 150_000, "src": "gt"}}, now)
        assert s["closed"][-1]["reason"] == "trailing stop"
    assert C.TAKE_PROFIT_PCT == 20.0 and getattr(C, "TRAIL_PCT", None) is None


def test_dip_entry_filter():
    from bot import run
    now = engine.now_utc()
    with run.overrides(C.LAB_RETIRED["dip"]):
        assert engine.reject_reason(pool(now, pc_h1=-20, pc_m5=2, buys_m5=60, sells_m5=30), now) is None
        assert engine.reject_reason(pool(now, pc_h1=12), now) == "ήδη εκτοξευμένο"


def test_overrides_leave_config_clean_for_momentum():
    from bot import run
    now = engine.now_utc()
    for name in C.LAB:
        with run.overrides(C.LAB[name]):
            pass
    assert not hasattr(C, "MIN_BUY_SELL_RATIO_M5") and not hasattr(C, "TRAIL_PCT")
    assert engine.reject_reason(pool(now), now) is None   # το momentum δεν σπάει μετά το εργαστήριο


def test_volume_spike_filter():
    from bot import run
    now = engine.now_utc()
    with run.overrides(C.LAB_RETIRED["spike"]):
        assert engine.reject_reason(pool(now, vol_m5=30_000, vol_h1=80_000), now) is None
        assert engine.reject_reason(pool(now, vol_m5=7_000, vol_h1=80_000), now) == "χωρίς έκρηξη όγκου"


def test_trench_filters():
    from bot import run
    now = engine.now_utc()
    with run.overrides(C.LAB["trench"]):
        young = (now - timedelta(minutes=40)).isoformat()
        ok = pool(now, created_at=young, liquidity=20_000, fdv=200_000, vol_h24=300_000, buyers_m5=30,
                  pc_m5=3, pc_h1=50, buys_m5=60, sells_m5=30, buys_h1=200, sells_h1=150)
        assert engine.reject_reason(ok, now) is None          # φρέσκο coin επιτρέπεται μόνο στο trench
        assert engine.reject_reason({**ok, "vol_h24": 100_000}, now) == "όγκος < market cap"
        assert engine.rug_reason({"ok": True, "top10": 20, "lp_locked": 100, "holders": 50}) == "λίγοι κάτοχοι"
        assert engine.rug_reason({"ok": True, "top10": 33, "lp_locked": 100, "holders": 500}) == "top-10 κάτοχοι πολλά"
        assert engine.rug_reason({"ok": True, "top10": 20, "lp_locked": 100, "holders": 500}) is None
        assert abs(C.SLIPPAGE_PER_SIDE - 0.04) < 1e-9
    assert engine.reject_reason(ok, now) == "πολύ καινούργιο"  # εκτός trench ισχύει ο κανόνας της 1 μέρας
    assert not hasattr(C, "TRENCH_SOURCE") and not hasattr(C, "MIN_HOLDERS")


def test_trench_pools_watchlist(monkeypatch):
    from bot import run, data
    now = engine.now_utc()
    mk = lambda a, mins: {"pool": a, "created_at": (now - timedelta(minutes=mins)).isoformat(), "dex": "pumpswap"}
    monkeypatch.setattr(data, "fresh_migrations", lambda **k: [mk("A", 10), mk("OLD", 500)])
    monkeypatch.setattr(data, "fetch_pools", lambda addrs: [mk(a, 60) for a in addrs])
    st = {}
    with run.overrides(C.LAB["trench"]):
        got = run.trench_pools(st, now)
        assert [p["pool"] for p in got] == ["A", "OLD"] and set(st["twatch"]) == {"A"}
        monkeypatch.setattr(data, "fresh_migrations", lambda **k: [])
        got = run.trench_pools(st, now)
        assert [p["pool"] for p in got] == ["A"]               # συνεχίζει να το παρακολουθεί


def test_trench_runner_waits_for_old_bot(monkeypatch, tmp_path):
    from bot import run, trench_run
    monkeypatch.setattr(run, "LAB_DIR", tmp_path)
    monkeypatch.setenv("NO_GIT", "1")
    st = engine.new_state(engine.now_utc())
    st.update({"epoch": C.LAB_EPOCH, "mode": "trench", "last_run": engine.iso(engine.now_utc())})
    (tmp_path / "trench.json").write_text(__import__("json").dumps(st))
    called = []
    monkeypatch.setattr(run, "lab_cycle", lambda *a: called.append(1))
    trench_run.main()
    assert not called                       # το παλιό bot γράφει ακόμα → δεν ξεκινά


def test_main_bot_skips_separate_strategies():
    assert C.LAB["trench"].get("SEPARATE")


# ---------------- ORB Nasdaq ----------------

def _orb_bars(day_utc_930, first, rest):
    """first=(o,h,l,c) για το κερί 9:30· rest = λίστα (o,h,l,c) για τα επόμενα κεριά 5'."""
    out = [(day_utc_930, *first)]
    for i, b in enumerate(rest, 1):
        out.append((day_utc_930 + 300 * i, *b))
    return out


def _orb_setup():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from bot import orb, majors
    t930 = datetime(2026, 11, 24, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    st = majors.new_state(t930, orb.EPOCH, "orb")
    return orb, st, t930


def test_orb_long_hits_target():
    orb, st, t930 = _orb_setup()
    ts = int(t930.timestamp())
    bars = _orb_bars(ts, (500, 501, 499.5, 500.8), [(500.8, 500.9, 500.7, 500.8)])
    orb.cycle(st, t930 + timedelta(minutes=6), bars_fn=lambda: bars)      # 9:36 → είσοδος long @500.8
    p = st["positions"][0]
    assert p["side"] == "long" and p["stop"] == 499.5 and abs(p["target"] - (500.8 + 13)) < 1e-6
    assert abs(p["risk_usd"] - 1.0) < 1e-6 or p["shares"] * p["entry"] <= 4 * 100 + 1e-6
    bars2 = bars + [(ts + 600, 501, 514.0, 500.9, 513.5)]
    orb.cycle(st, t930 + timedelta(minutes=11), bars_fn=lambda: bars2)
    t = st["closed"][-1]
    assert t["reason"] == "στόχος 10R" and t["pnl"] > 0 and not st["positions"]


def test_orb_short_stop_and_one_trade_per_day():
    orb, st, t930 = _orb_setup()
    ts = int(t930.timestamp())
    bars = _orb_bars(ts, (500, 500.5, 499, 499.2), [(499.2, 499.3, 499.1, 499.2)])
    orb.cycle(st, t930 + timedelta(minutes=6), bars_fn=lambda: bars)
    assert st["positions"][0]["side"] == "short" and st["positions"][0]["stop"] == 500.5
    bars2 = bars + [(ts + 600, 499.2, 501, 499, 500.9)]
    orb.cycle(st, t930 + timedelta(minutes=11), bars_fn=lambda: bars2)
    t = st["closed"][-1]
    assert t["reason"] == "stop" and t["pnl"] < 0 and abs(t["exit_market"] - 500.5) < 1e-9
    orb.cycle(st, t930 + timedelta(minutes=14), bars_fn=lambda: bars2)
    assert not st["positions"] and len(st["closed"]) == 1          # μόνο ένα trade τη μέρα


def test_orb_closes_at_end_of_day_and_skips_outside_hours():
    orb, st, t930 = _orb_setup()
    ts = int(t930.timestamp())
    bars = _orb_bars(ts, (500, 501, 499.5, 500.8), [(500.8, 500.9, 500.7, 500.8)])
    orb.cycle(st, t930 + timedelta(minutes=6), bars_fn=lambda: bars)
    late = bars + [(ts + 300 * k, 501, 501.5, 500.6, 501.2) for k in range(2, 78)]
    orb.cycle(st, t930 + timedelta(hours=6, minutes=26), bars_fn=lambda: late)   # 15:56
    assert st["closed"][-1]["reason"] == "κλείσιμο αγοράς" and not st["positions"]
    called = []
    orb.cycle(st, t930 + timedelta(hours=8), bars_fn=lambda: called.append(1))
    assert not called                                                   # εκτός ωραρίου: καμία κλήση
    small = _orb_bars(ts, (500, 500.01, 499.99, 500.001), [(500, 500, 500, 500)])
    orb2, st2, _ = _orb_setup()
    orb2.cycle(st2, t930 + timedelta(minutes=6), bars_fn=lambda: small)
    assert not st2["positions"]                                         # κερί χωρίς κίνηση → τίποτα


# ---------------- Compression breakout ----------------

def _comp_bars(n, t_end, breakout=0, vol_spike=1.0, base=100.0):
    """n κεριά 4ω: πρώτα θορυβώδη, μετά πολύ ήσυχα (συμπίεση), και στο τέλος προαιρετικό breakout."""
    import math
    bars, t0 = [], t_end - n * 4 * 3600
    for k in range(n):
        amp = 2.0 if k < n - 60 else 0.05          # τα τελευταία 60 κεριά: ακραία ησυχία
        c = base + amp * math.sin(k * 0.7)
        o = base + amp * math.sin((k - 1) * 0.7)
        bars.append((t0 + k * 4 * 3600, o, max(o, c) + 0.01, min(o, c) - 0.01, c, 1000.0))
    if breakout:
        t, o, h, l, c, v = bars[-1]
        c2 = base + breakout
        bars[-1] = (t, base, max(base, c2) + 0.01, min(base, c2) - 0.01, c2, 1000.0 * vol_spike)
    return bars


def test_compress_signal_needs_squeeze_breakout_volume_and_regime():
    from bot import compress
    up = _comp_bars(800, 10**9, breakout=1.0, vol_spike=3.0)
    assert compress.signal(up, len(up) - 1, True)[0] == 1          # long
    assert compress.signal(up, len(up) - 1, False)[0] == 0         # BTC καθοδικό → όχι long
    weak = _comp_bars(800, 10**9, breakout=1.0, vol_spike=1.2)
    assert compress.signal(weak, len(weak) - 1, True)[0] == 0      # χωρίς όγκο → τίποτα
    dn = _comp_bars(800, 10**9, breakout=-1.0, vol_spike=3.0)
    assert compress.signal(dn, len(dn) - 1, False)[0] == -1        # short


def test_compress_cycle_opens_trails_and_stops():
    from datetime import datetime, timezone
    from bot import compress, majors
    now = datetime(2026, 11, 26, 12, 0, 5, tzinfo=timezone.utc)
    st = majors.new_state(now, compress.EPOCH, "compress")
    now_s = int(now.timestamp())
    btc_rising = [(t, o + k * 0.5, h + k * 0.5, l + k * 0.5, c + k * 0.5, v)
                  for k, (t, o, h, l, c, v) in enumerate(_comp_bars(800, now_s - now_s % (4 * 3600)))]
    eth = _comp_bars(800, now_s - now_s % (4 * 3600), breakout=1.0, vol_spike=3.0)
    flat = _comp_bars(800, now_s - now_s % (4 * 3600))
    cands = lambda coin, ns: btc_rising if coin == "BTC" else (eth if coin == "ETH" else flat)
    ctx = {c: (101.0 if c == "ETH" else 100.0, 0.00001) for c in compress.COINS}
    ctx["BTC"] = (btc_rising[-1][4], 0.00001)
    compress.cycle(st, now, candles_fn=cands, ctx_fn=lambda: ctx)
    assert [p["coin"] for p in st["positions"]] == ["ETH"]
    p = st["positions"][0]
    assert p["side"] == 1 and p["stop"] < 101.0 and p["notional"] <= 3 * 100 + 1e-9
    assert abs(p["risk_usd"] - 1.0) < 0.05 or p["notional"] >= 3 * 100 - 1e-6
    compress.cycle(st, now, candles_fn=cands, ctx_fn=lambda: ctx)          # ίδιο κερί → καμία νέα είσοδος
    assert len(st["positions"]) == 1
    ctx2 = dict(ctx, ETH=(p["stop"] - 0.01, 0.00001))
    compress.cycle(st, now, candles_fn=cands, ctx_fn=lambda: ctx2)
    assert not st["positions"] and st["closed"][-1]["reason"] == "stop"
    assert st["closed"][-1]["pnl"] < 0 and st["closed"][-1]["r_multiple"] < -0.9


def test_compress_variants_differ_only_where_intended():
    from bot import compress
    b = compress.VARIANTS
    assert b["compress"] == {"vm": 1.5, "trail": 3.0, "calm": False}          # baseline ανέγγιχτο
    assert b["compress_vol2"] == dict(b["compress"], vm=2.0)
    assert b["compress_calm_t2"] == dict(b["compress"], calm=True, trail=2.0)
    mid = _comp_bars(800, 10**9, breakout=1.0, vol_spike=1.8)               # όγκος 1,8×: περνά στο baseline, όχι στο vol2
    assert compress.signal(mid, len(mid) - 1, True, b["compress"])[0] == 1
    assert compress.signal(mid, len(mid) - 1, True, b["compress_vol2"])[0] == 0


def test_btc_calm():
    from bot import compress
    import math
    quiet_now = [(k, 0, 0, 0, 100 * (1 + (0.02 if k < 1200 else 0.001) * math.sin(k)), 1) for k in range(1300)]
    wild_now = [(k, 0, 0, 0, 100 * (1 + (0.001 if k < 1200 else 0.02) * math.sin(k)), 1) for k in range(1300)]
    assert compress.btc_calm(quiet_now) is True
    assert compress.btc_calm(wild_now) is False


# ---------------- Attention ----------------

def test_attention_warmup_then_fresh_entries_and_24h_exit():
    from datetime import datetime, timezone, timedelta
    from bot import attention, majors
    t = datetime(2026, 11, 27, 10, 0, 30, tzinfo=timezone.utc)
    st = majors.new_state(t, attention.EPOCH, "attention")
    mkt = {"BTC": (60000.0, 0.00001), "PEPE": (1.0, 0.0001), "SOL": (70.0, 0.00001)}
    attention.cycle(st, t, trending_fn=lambda: ["SOL", "PEPE"], market_fn=lambda: mkt, side_fn=lambda: None)
    assert not st["positions"]                                   # warm-up: καμία αγορά την πρώτη ώρα
    attention.cycle(st, t + timedelta(hours=1), trending_fn=lambda: ["SOL", "PEPE", "WIF"],
                    market_fn=lambda: mkt, side_fn=lambda: None)
    assert not st["positions"]                                   # SOL/PEPE όχι φρέσκα, WIF δεν υπάρχει στο Hyperliquid
    mkt["NEW"] = (2.0, 0.0)
    attention.cycle(st, t + timedelta(hours=2), trending_fn=lambda: ["NEW", "SOL"],
                    market_fn=lambda: mkt, side_fn=lambda: None)
    assert [p["coin"] for p in st["positions"]] == ["NEW"]
    mkt2 = dict(mkt, NEW=(2.2, 0.0), BTC=(61200.0, 0.0))
    attention.cycle(st, t + timedelta(hours=26, minutes=1), trending_fn=lambda: ["NEW"],
                    market_fn=lambda: mkt2, side_fn=lambda: None)
    c = st["closed"][-1]
    assert c["coin"] == "NEW" and c["reason"] == "24 ώρες" and c["pnl"] > 0
    assert abs(c["excess_pct"] - (10.0 - 2.0)) < 0.01           # coin +10%, BTC +2% → excess +8%


def test_calls_parse_warmup_entry_and_tp():
    from datetime import timedelta
    from bot import calls, majors
    t = engine.now_utc()
    mint = "Bz5N9wrcxDw2YTf8tQkG8PJzdbUW89ZNWLWbdqA2pump"

    def page(msgs):
        return "".join(f'<div class="tgme_widget_message" data-post="ch/{i}"><div class="tgme_widget_message_text">{txt}</div>'
                       f'<time datetime="{w.isoformat()}" class="time">x</time></div>' for i, txt, w in msgs)

    old = [(1, "gm", t - timedelta(hours=5)), (2, f"call {mint}", t - timedelta(hours=4))]
    assert calls.parse_channel(page(old))[1][3] == [mint]
    st = majors.new_state(t, calls.EPOCH, "calls")
    pages = {"ch": page(old)}
    price = {"v": 1.0}
    pf = lambda ms: {m: (price["v"], 20000.0, "SHU") for m in ms}
    ff = lambda n: calls.parse_channel(pages[n])
    calls.cycle(st, t, fetch_fn=ff, price_fn=pf, channels=["ch"])
    assert not st["positions"]                                   # warmup: δεν αγοράζει παλιά calls
    pages["ch"] = page(old + [(3, f"new {mint} &amp; go", t + timedelta(minutes=2))])
    calls.cycle(st, t + timedelta(minutes=3), fetch_fn=ff, price_fn=pf, channels=["ch"])
    assert len(st["positions"]) == 1 and st["channels"]["ch"]["calls"] == 1
    price["v"] = 2.2
    calls.cycle(st, t + timedelta(minutes=4), fetch_fn=ff, price_fn=pf, channels=["ch"])
    assert not st["positions"] and st["closed"][0]["reason"].startswith("TP")
    assert st["closed"][0]["pnl"] > 10 and st["channels"]["ch"]["pnl"] > 10
    # το ίδιο token ξανά μέσα σε 24 ώρες → όχι νέα αγορά
    pages["ch"] = page(old + [(3, "x", t), (4, f"again {mint}", t + timedelta(minutes=6))])
    calls.cycle(st, t + timedelta(minutes=7), fetch_fn=ff, price_fn=pf, channels=["ch"])
    assert not st["positions"]
