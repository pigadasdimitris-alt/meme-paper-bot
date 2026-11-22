"""Έλεγχος της ροής Jupiter/RPC με ψεύτικες απαντήσεις (χωρίς δίκτυο, χωρίς πραγματικό κλειδί)."""
import base64

import pytest

solders = pytest.importorskip("solders")
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from bot import live


@pytest.fixture
def wallet(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(kp))
    monkeypatch.setenv("JUPITER_API_KEY", "test")
    monkeypatch.setattr(live, "_kp", None)
    return kp


def unsigned_tx(payer):
    msg = MessageV0.try_compile(payer, [Instruction(Pubkey.default(), b"x", [AccountMeta(payer, True, True)])], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


def test_swap_signs_with_our_key(wallet, monkeypatch):
    sent = {}
    def fake_http(url, body=None, headers=None, timeout=25):
        assert headers["x-api-key"] == "test"
        if "/order" in url:
            assert f"taker={wallet.pubkey()}" in url
            return {"transaction": unsigned_tx(wallet.pubkey()), "requestId": "R1"}
        sent.update(body)
        return {"status": "Success", "signature": "SIG", "totalInputAmount": "1000", "totalOutputAmount": "5000"}
    monkeypatch.setattr(live, "_http", fake_http)
    r = live.swap(live.SOL_MINT, "MINT", 1000)
    assert r == {"signature": "SIG", "in": 1000, "out": 5000}
    tx = VersionedTransaction.from_bytes(base64.b64decode(sent["signedTransaction"]))
    assert tx.verify_with_results() == [True] and sent["requestId"] == "R1"


def test_failed_execute_raises(wallet, monkeypatch):
    def fake_http(url, body=None, headers=None, timeout=25):
        if "/order" in url:
            return {"transaction": unsigned_tx(wallet.pubkey()), "requestId": "R1"}
        return {"status": "Failed", "code": -1005}
    monkeypatch.setattr(live, "_http", fake_http)
    with pytest.raises(live.LiveError):
        live.swap(live.SOL_MINT, "MINT", 1000)


def test_buy_respects_sol_reserve(wallet, monkeypatch):
    monkeypatch.setattr(live, "sol_price", lambda: 150.0)
    monkeypatch.setattr(live, "sol_balance", lambda: 0.03)  # 0.015 απόθεμα → μένουν ~2.25$
    with pytest.raises(live.LiveError):
        live.buy({"base_mint": "MINT"}, 4.0)


def test_buy_and_sell_amounts(wallet, monkeypatch):
    monkeypatch.setattr(live, "sol_price", lambda: 200.0)
    monkeypatch.setattr(live, "sol_balance", lambda: 0.2)
    monkeypatch.setattr(live, "swap", lambda i, o, a: {"signature": "S", "in": a, "out": 15_000_000 if o == live.SOL_MINT else 123})
    monkeypatch.setattr(live, "token_balance", lambda mint: (2_000_000_000, 6))
    b = live.buy({"base_mint": "MINT"}, 4.0)
    assert abs(b["cost_usd"] - 4.0) < 1e-6 and b["qty"] == 2000.0
    s = live.sell({"mint": "MINT"})
    assert abs(s["proceeds_usd"] - 3.0) < 1e-9


def test_bad_key_does_not_leak(monkeypatch):
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "not-a-real-secret-123")
    monkeypatch.setattr(live, "_kp", None)
    with pytest.raises(live.LiveError) as e:
        live.keypair()
    assert "not-a-real-secret" not in str(e.value)


def test_recovery_phrase_matches_phantom_account_1(monkeypatch):
    # Δημόσιο δοκιμαστικό mnemonic (BIP39 test vector) → γνωστή διεύθυνση Phantom "Account 1"
    m = "abandon " * 11 + "about"
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", m)
    monkeypatch.setattr(live, "_kp", None)
    assert live.owner() == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


def test_hint_selects_right_key_or_refuses(monkeypatch):
    m = "abandon " * 11 + "about"
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", m)
    monkeypatch.setattr(live, "_kp", None)
    monkeypatch.setattr(live, "ADDRESS_HINT", "HAgk…Kpqk")
    assert live.owner() == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
    monkeypatch.setattr(live, "_kp", None)
    monkeypatch.setattr(live, "ADDRESS_HINT", "6zAx…gyjn")
    with pytest.raises(live.LiveError):
        live.keypair()
    rep = live.mnemonic_report()
    assert rep["words"] == 12 and rep.get("checksum_ok", True) and "abandon" not in str(rep)


def test_numbered_copy_is_cleaned(monkeypatch):
    words = ["abandon"] * 11 + ["about"]
    pasted = " ".join(f"{i+1} {w}" for i, w in enumerate(words))          # "1 abandon 2 abandon ..."
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", pasted)
    monkeypatch.setattr(live, "_kp", None)
    monkeypatch.setattr(live, "ADDRESS_HINT", "")
    assert live.owner() == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "\n".join(f"{i+1}. {w}" for i, w in enumerate(words)))
    monkeypatch.setattr(live, "_kp", None)
    assert live.owner() == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
