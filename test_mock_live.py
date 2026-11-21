"""Offline test of the --live code path with a FAKE exchange and FAKE account.
No private key, no real account, no order is ever sent. Only public market data
(spot metadata, order book, mid prices) is read from Hyperliquid mainnet.
Run: python test_mock_live.py"""
import os, sys, time, types, tempfile
from unittest import mock
import dca

FAKE_AGENT = "0x00000000000000000000000000000000000000aa"   # dummy API wallet
FAKE_MAIN = "0x00000000000000000000000000000000000000bb"    # dummy main account


class FakeExchange:
    last = None
    def __init__(self, wallet, base_url, account_address=None, spot_meta=None):
        self.sent = []
        FakeExchange.last = self
    def order(self, coin, is_buy, sz, px, order_type):
        self.sent.append((coin, is_buy, sz, px, order_type))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(round(px * 0.995)), "oid": 123}}]}}}


def fake_spot_state(self, address):
    return {"balances": [{"coin": "USDC", "token": 0, "total": "1000.0", "hold": "0.0"}]}

def fake_fees(self, address):
    return {"userSpotCrossRate": "0.0007", "userSpotAddRate": "0.0004"}

def fake_agents(self, user):
    return []

def fake_fills(self, user, start, end=None, aggregate_by_time=False):
    return [{"oid": 123, "tid": 9, "time": int(time.time() * 1000), "side": "B", "px": "60300.0",
             "sz": "0.00082", "fee": "0.00000057", "feeToken": "UBTC", "hash": "0xabc"}]


def patched(env, key_addr, argv):
    import hyperliquid.exchange as hx
    from hyperliquid.info import Info
    return [mock.patch.dict(os.environ, env),
            mock.patch("eth_account.Account.from_key", return_value=types.SimpleNamespace(address=key_addr)),
            mock.patch.object(hx, "Exchange", FakeExchange),
            mock.patch.object(Info, "spot_user_state", fake_spot_state),
            mock.patch.object(Info, "user_fees", fake_fees),
            mock.patch.object(Info, "extra_agents", fake_agents),
            mock.patch.object(Info, "user_fills_by_time", fake_fills),
            mock.patch("time.sleep", lambda s: None),
            mock.patch.object(sys, "argv", argv)]


def run():
    csv = tempfile.mktemp(suffix=".csv")
    env = {"HL_ACCOUNT_ADDRESS": FAKE_MAIN, "HL_API_WALLET_PRIVATE_KEY": "not-a-real-key", "FILLS_CSV": csv}

    from contextlib import ExitStack
    with ExitStack() as st:
        for p in patched(env, FAKE_AGENT, ["dca.py", "--live", "--amount", "50", "--env", os.devnull]):
            st.enter_context(p)
        rc = dca.main()
    print("exit code:", rc)
    print("order sent to FAKE exchange:", FakeExchange.last.sent)
    print(open(csv).read())
    assert rc == 0 and FakeExchange.last.sent[0][4] == {"limit": {"tif": "Ioc"}}

    with ExitStack() as st:
        for p in patched(env, FAKE_MAIN, ["dca.py", "--live", "--env", os.devnull]):
            st.enter_context(p)
        try:
            dca.main()
            raise AssertionError("main-wallet key was accepted")
        except SystemExit as e:
            print("main-key guard OK:", e)
    os.remove(csv)
    print("ALL MOCK TESTS PASSED")


if __name__ == "__main__":
    run()
