from datetime import timedelta

from bot import config as C
from bot import copybot, engine


def gt_pool(now, mint="MINTX", **kw):
    p = {"pool": "PX", "name": "COPY / SOL", "base_mint": mint, "quote_mint": C.SOL_MINT, "dex": "raydium",
         "price": 0.01, "created_at": engine.iso(now - timedelta(days=2)), "fdv": 2e6, "liquidity": 80_000,
         "vol_m5": 5000, "vol_h1": 40000, "pc_m5": 3.0, "pc_h1": 10.0, "buys_m5": 50, "sells_m5": 30,
         "buyers_m5": 30, "buys_h1": 300, "sells_h1": 200}
    p.update(kw)
    return p


GOOD_RC = lambda m, p: {"ok": True, "top10": 15.0, "lp_locked": 100.0, "danger": [], "rugged": False}


def test_token_moves():
    tx = {"meta": {"err": None,
                   "preTokenBalances": [{"owner": "W", "mint": "M", "uiTokenAmount": {"uiAmount": 0}}],
                   "postTokenBalances": [{"owner": "W", "mint": "M", "uiTokenAmount": {"uiAmount": 500}},
                                         {"owner": "OTHER", "mint": "M", "uiTokenAmount": {"uiAmount": 9}}]}}
    assert copybot.token_moves(tx, "W") == {"M": 500.0}
    assert copybot.token_moves({"meta": {"err": "x"}}, "W") == {}


def test_early_winners_skips_bots_and_late_buyers():
    trades = [{"tx_from_address": "EARLY", "kind": "buy", "to_token_address": "M", "price_to_in_usd": "0.004",
               "volume_in_usd": "500"},
              {"tx_from_address": "LATE", "kind": "buy", "to_token_address": "M", "price_to_in_usd": "0.009",
               "volume_in_usd": "500"}]
    trades += [{"tx_from_address": "BOT", "kind": "buy", "to_token_address": "M", "price_to_in_usd": "0.001",
                "volume_in_usd": "500"}] * copybot.BOT_TRADES
    assert copybot.early_winners(trades, "M", 0.01) == {"EARLY"}


def test_signal_buys_when_two_wallets_agree():
    now = engine.now_utc()
    st = copybot.new_copy_state(now)
    st["buys"] = {"MINTX": {"W1": engine.iso(now)}}
    copybot.enter(st, now, flags_fn=lambda m: {}, rug_fn=GOOD_RC, pool_fn=lambda m: gt_pool(now))
    assert not st["positions"]   # ένα πορτοφόλι δεν αρκεί
    st["buys"]["MINTX"]["W2"] = engine.iso(now)
    copybot.enter(st, now, flags_fn=lambda m: {}, rug_fn=GOOD_RC, pool_fn=lambda m: gt_pool(now))
    assert len(st["positions"]) == 1 and set(st["positions"][0]["wallets"]) == {"W1", "W2"}


def test_signal_rejected_by_safety():
    now = engine.now_utc()
    for kw, rc in [({"liquidity": 5000}, GOOD_RC),
                   ({"created_at": engine.iso(now - timedelta(minutes=10))}, GOOD_RC),
                   ({}, lambda m, p: {"ok": True, "top10": 80.0, "lp_locked": 100.0, "danger": []})]:
        st = copybot.new_copy_state(now)
        st["buys"] = {"MINTX": {"W1": engine.iso(now), "W2": engine.iso(now)}}
        copybot.enter(st, now, flags_fn=lambda m: {}, rug_fn=rc, pool_fn=lambda m, kw=kw: gt_pool(now, **kw))
        assert not st["positions"]


def test_follow_sells_and_learning():
    now = engine.now_utc()
    st = copybot.new_copy_state(now)
    st["wallets"] = {"W1": {"score": 1, "coins": [], "added": engine.iso(now), "last_sig": None},
                     "W2": {"score": 1, "coins": [], "added": engine.iso(now), "last_sig": None}}
    st["buys"] = {"MINTX": {"W1": engine.iso(now), "W2": engine.iso(now)}}
    copybot.enter(st, now, flags_fn=lambda m: {}, rug_fn=GOOD_RC, pool_fn=lambda m: gt_pool(now))
    later = now + timedelta(minutes=5)
    st["sells"] = {"MINTX": {"W1": engine.iso(later), "W2": engine.iso(later)}}
    before = len(st["closed"])
    copybot.follow_sells(st, {"PX": {"price": 0.009, "liquidity": 80_000, "src": "gt"}}, later)
    assert st["closed"][-1]["reason"] == "πούλησαν τα πορτοφόλια"
    copybot.learn(st, before)
    assert st["wallets"]["W1"]["score"] == 0
