#!/usr/bin/env python3
"""
Hyperliquid daily Bitcoin DCA (spot UBTC/USDC).

Safe by default:
  * DRY RUN unless you pass --live (prints what it would do, places nothing).
  * Signs with a Hyperliquid API ("agent") wallet. API wallets can trade but
    cannot withdraw funds. The script refuses to run live if the key belongs
    to your main account.
  * One aggressive IOC limit buy per chunk, capped by MAX_SLIPPAGE_BPS, so it
    can never pay more than best ask + the cap.
  * Checks your USDC spot balance first and skips if it's too low.
  * Logs every fill (time, price, size, fee) to a CSV.

Usage:
  python dca.py                    # dry run, uses .env
  python dca.py --amount 250       # dry run for $250 (default DCA_USD is 50)
  python dca.py --live             # REAL order(s)
  python dca.py --chunks 4 --chunk-delay 900 --live   # 4 buys, 15 min apart
  python dca.py --testnet          # use Hyperliquid testnet

Not financial advice. Use at your own risk. MIT licensed, provided "as is"
with no warranty (see LICENSE). Unofficial: not affiliated with Hyperliquid or Unit.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

HERE = Path(__file__).resolve().parent
SPOT_MIN_ORDER_QUOTE = 10.0  # API rejects spot orders < 10 units of quote token (MinTradeSpotNtl)


def log(msg: str) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] {msg}", flush=True)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def resolve_pair(spot_meta: dict, base: str, quote: str) -> dict:
    """Find the spot market for base/quote in live spotMeta (never hardcode the index)."""
    tokens = {t["index"]: t for t in spot_meta["tokens"]}
    matches = []
    for u in spot_meta["universe"]:
        b, q = u["tokens"]
        if tokens[b]["name"] == base and tokens[q]["name"] == quote:
            matches.append((u, tokens[b], tokens[q]))
    if not matches:
        raise SystemExit(f"No spot market {base}/{quote} found in spotMeta.")
    if len(matches) > 1:
        names = ", ".join(m[0]["name"] for m in matches)
        raise SystemExit(f"Ambiguous: several {base}/{quote} markets ({names}). Set SPOT_COIN explicitly.")
    u, bt, qt = matches[0]
    return {
        "coin": u["name"],               # e.g. "@142" - name used by the API/SDK
        "asset": 10000 + u["index"],     # spot asset id = 10000 + universe index
        "base": bt["name"], "base_token": bt["index"], "sz_decimals": bt["szDecimals"],
        "quote": qt["name"], "quote_token": qt["index"],
        "base_full_name": bt.get("fullName"),
    }


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid spot price rules: <=5 significant figures, <= 8-szDecimals decimals.
    Integer prices are always valid."""
    if px >= 100000:  # 6+ digit integers are allowed regardless of sig figs
        return float(math.floor(px))
    return round(float(f"{px:.5g}"), 8 - sz_decimals)


def floor_sz(sz: float, sz_decimals: int) -> float:
    q = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(str(sz)).quantize(q, rounding=ROUND_DOWN))


def usdc_available(info: Info, address: str, quote: str) -> tuple[float, float]:
    st = info.spot_user_state(address)
    for b in st.get("balances", []):
        if b["coin"] == quote:
            total, hold = float(b["total"]), float(b["hold"])
            return total - hold, total
    return 0.0, 0.0


def simulate_fill(asks: list[dict], usd: float, limit_px: float) -> tuple[float, float, float]:
    """Walk the ask side up to limit_px. Returns (base_size, usd_spent, avg_px)."""
    remaining, size, spent = usd, 0.0, 0.0
    for lvl in asks:
        px, sz = float(lvl["px"]), float(lvl["sz"])
        if px > limit_px or remaining <= 0:
            break
        take = min(sz, remaining / px)
        size += take
        spent += take * px
        remaining -= take * px
    return size, spent, (spent / size if size else 0.0)


def write_fills(csv_path: Path, rows: list[dict]) -> None:
    new = not csv_path.exists()
    fields = ["time_utc", "time_local", "network", "coin", "side", "oid", "tid", "price",
              "size_btc", "notional_usdc", "fee", "fee_token", "hash"]
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid spot BTC DCA (dry run by default)")
    ap.add_argument("--live", action="store_true", help="Actually place orders. Without this it's a dry run.")
    ap.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet instead of mainnet")
    ap.add_argument("--amount", type=float, help="USDC to spend this run (overrides DCA_USD)")
    ap.add_argument("--chunks", type=int, help="Split this run into N smaller buys (overrides CHUNKS)")
    ap.add_argument("--chunk-delay", type=int, help="Seconds between chunks (overrides CHUNK_DELAY_SEC)")
    ap.add_argument("--env", default=str(HERE / ".env"), help="Path to .env file")
    args = ap.parse_args()

    load_dotenv(args.env)
    testnet = args.testnet or os.getenv("HL_NETWORK", "mainnet").lower() == "testnet"
    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    network = "testnet" if testnet else "mainnet"

    amount = args.amount if args.amount is not None else env_float("DCA_USD", 50)
    chunks = args.chunks if args.chunks is not None else int(env_float("CHUNKS", 1))
    chunk_delay = args.chunk_delay if args.chunk_delay is not None else int(env_float("CHUNK_DELAY_SEC", 0))
    slippage_bps = env_float("MAX_SLIPPAGE_BPS", 50)          # 0.50% default cap
    max_per_run = env_float("MAX_USD_PER_RUN", 1000)          # fat-finger guard
    min_reserve = env_float("MIN_USDC_RESERVE", 0)            # keep this much USDC untouched
    max_spread_bps = env_float("MAX_SPREAD_BPS", 50)          # skip if the book looks broken
    max_dev_bps = env_float("MAX_DEVIATION_BPS", 200)         # skip if spot ask is far from BTC perp mid
    account = (os.getenv("HL_ACCOUNT_ADDRESS") or "").strip()
    base_name = os.getenv("BASE_TOKEN", "UNIT" if testnet else "UBTC")  # Unit BTC: UBTC on mainnet, UNIT (index 1129) on testnet
    quote_name = os.getenv("QUOTE_TOKEN", "USDC")
    csv_path = Path(os.getenv("FILLS_CSV", str(HERE / "fills.csv")))
    mode = "LIVE" if args.live else "DRY RUN"

    log(f"=== Hyperliquid BTC DCA | {mode} | {network} ===")
    if amount <= 0:
        raise SystemExit("Amount must be > 0.")
    if amount > max_per_run:
        raise SystemExit(f"Amount ${amount:,.2f} exceeds MAX_USD_PER_RUN ${max_per_run:,.2f}. Raise it in .env if intended.")
    if chunks < 1:
        raise SystemExit("CHUNKS must be >= 1")
    per_chunk = amount / chunks
    if per_chunk < SPOT_MIN_ORDER_QUOTE:
        raise SystemExit(f"Each chunk is ${per_chunk:.2f}; Hyperliquid rejects spot orders under {SPOT_MIN_ORDER_QUOTE:.0f} {quote_name}.")

    info = Info(base_url, skip_ws=True)
    spot_meta = info.spot_meta()
    pair = resolve_pair(spot_meta, base_name, quote_name)
    if os.getenv("SPOT_COIN"):
        pair["coin"] = os.getenv("SPOT_COIN")
    log(f"Market: {pair['base']}/{pair['quote']} (base full name: {pair['base_full_name']}) -> "
        f"coin '{pair['coin']}', asset id {pair['asset']}, size step {10**-pair['sz_decimals']:.{pair['sz_decimals']}f} {pair['base']}")
    log(f"Plan: spend ${amount:,.2f} in {chunks} chunk(s) of ${per_chunk:,.2f}, max slippage {slippage_bps/100:.2f}% over best ask")

    exchange = None
    if args.live:
        if not account:
            raise SystemExit("HL_ACCOUNT_ADDRESS (your MAIN wallet address) is required for live trading.")
        key = os.getenv("HL_API_WALLET_PRIVATE_KEY", "").strip()
        if not key:
            raise SystemExit("HL_API_WALLET_PRIVATE_KEY is not set (put the API wallet key in .env).")
        import eth_account
        from hyperliquid.exchange import Exchange
        wallet = eth_account.Account.from_key(key)
        del key
        if wallet.address.lower() == account.lower():
            raise SystemExit("Refusing: that key is your MAIN wallet key. Use an API wallet key (can trade, cannot withdraw).")
        agents = info.extra_agents(account) or []
        approved = [a for a in agents if a.get("address", "").lower() == wallet.address.lower()]
        if agents and not approved:
            log(f"WARNING: API wallet {wallet.address} not in named agents list for {account}; "
                "it may be your unnamed agent. If the order is rejected, re-create it on app.hyperliquid.xyz/API.")
        for a in approved:
            vu = a.get("validUntil")
            if vu:
                days = (vu / 1000 - time.time()) / 86400
                log(f"API wallet '{a.get('name')}' valid until {datetime.fromtimestamp(vu/1000).astimezone():%Y-%m-%d} ({days:.0f} days left)")
                if days < 7:
                    log("WARNING: API wallet expires within 7 days. Create a new one soon.")
        exchange = Exchange(wallet, base_url, account_address=account, spot_meta=spot_meta)
        log(f"Signing with API wallet {wallet.address} on behalf of {account}")

    if account:
        avail, total = usdc_available(info, account, pair["quote"])
        log(f"{pair['quote']} spot balance: {avail:,.2f} available ({total:,.2f} total)")
        try:
            fees = info.user_fees(account)
            log(f"Your fee rates: spot taker {float(fees['userSpotCrossRate'])*100:.4f}%, spot maker {float(fees['userSpotAddRate'])*100:.4f}%")
        except Exception as e:  # informational only
            log(f"(could not read fee tier: {e})")
        need = amount * (1 + slippage_bps / 1e4)  # worst case if every chunk fills at the cap
        if avail - min_reserve < need:
            msg = (f"SKIP: need up to ${need:,.2f} {pair['quote']} (+${min_reserve:,.2f} reserve) but only ${avail:,.2f} available in SPOT. "
                   "Top up USDC on Hyperliquid (and, if your account is in Standard mode, transfer Perps -> Spot).")
            if args.live:
                log(msg)
                return 2
            log(msg + " [dry run continues to show the order it would place]")
    else:
        log("No HL_ACCOUNT_ADDRESS set: skipping balance check (dry run only).")

    all_rows = []
    for i in range(1, chunks + 1):
        book = info.l2_snapshot(pair["coin"])
        bids, asks = book["levels"]
        if not asks:
            log("SKIP: order book has no asks.")
            return 3
        best_ask = float(asks[0]["px"])
        best_bid = float(bids[0]["px"]) if bids else best_ask
        mid = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid * 1e4
        try:
            perp_mid = float(info.all_mids()["BTC"])
        except Exception:
            perp_mid = 0.0
        dev_bps = (best_ask / perp_mid - 1) * 1e4 if perp_mid else 0.0
        if spread_bps > max_spread_bps or abs(dev_bps) > max_dev_bps:
            log(f"SKIP: market looks off (ask {best_ask:,.2f}, bid {best_bid:,.2f}, spread {spread_bps:.1f} bps, "
                f"{dev_bps:+.1f} bps vs BTC perp mid {perp_mid:,.2f}). Limits: spread {max_spread_bps:.0f}, deviation {max_dev_bps:.0f} bps.")
            return 3
        limit_px = round_px(best_ask * (1 + slippage_bps / 1e4), pair["sz_decimals"])
        # Size for the chunk amount at the best ask; the limit caps the worst case at +slippage.
        sz = floor_sz(per_chunk / best_ask, pair["sz_decimals"])
        worst_cost = sz * limit_px
        sim_sz, sim_cost, sim_avg = simulate_fill(asks, per_chunk, limit_px)
        log(f"Chunk {i}/{chunks}: bid {best_bid:,.2f} / ask {best_ask:,.2f} (spread {spread_bps:.2f} bps, {dev_bps:+.1f} bps vs perp mid). "
            f"IOC BUY {sz} {pair['base']} @ limit {limit_px:,.2f} (max cost ${worst_cost:,.2f}). "
            f"Book walk for ${per_chunk:,.2f}: avg {sim_avg:,.2f}, ~{sim_sz:.5f} {pair['base']}")
        if args.live and i > 1:  # re-check balance before each later chunk
            avail, _ = usdc_available(info, account, pair["quote"])
            if avail - min_reserve < sz * limit_px:
                log(f"SKIP chunk {i}: only ${avail:,.2f} {pair['quote']} left.")
                break
        if sz * best_ask < SPOT_MIN_ORDER_QUOTE:
            log("SKIP: order would be under the 10 USDC minimum.")
            continue
        if sim_cost < per_chunk * 0.98:
            log(f"WARNING: only ${sim_cost:,.2f} of asks within the slippage cap; order may partially fill.")

        if not args.live:
            log("DRY RUN: no order sent.")
        else:
            start_ms = int(time.time() * 1000) - 5000
            res = exchange.order(pair["coin"], True, sz, limit_px, {"limit": {"tif": "Ioc"}})
            if res.get("status") != "ok":
                log(f"ORDER REJECTED: {res}")
                return 4
            st = res["response"]["data"]["statuses"][0]
            if "error" in st:
                log(f"ORDER ERROR: {st['error']}")
                return 4
            if "filled" not in st:
                log(f"Not filled (IOC cancelled): {st}")
            else:
                f = st["filled"]
                oid = f["oid"]
                log(f"FILLED: {f['totalSz']} {pair['base']} @ avg {float(f['avgPx']):,.2f} (oid {oid})")
                rows = []
                for _ in range(5):  # fills can take a moment to appear
                    time.sleep(2)
                    fills = info.user_fills_by_time(account, start_ms)
                    rows = [x for x in fills if x.get("oid") == oid]
                    if rows:
                        break
                out = []
                for x in rows:
                    t = datetime.fromtimestamp(x["time"] / 1000, tz=timezone.utc)
                    out.append({
                        "time_utc": t.isoformat(), "time_local": t.astimezone().isoformat(),
                        "network": network, "coin": f"{pair['base']}/{pair['quote']}", "side": x["side"],
                        "oid": x["oid"], "tid": x.get("tid"), "price": x["px"], "size_btc": x["sz"],
                        "notional_usdc": f"{float(x['px']) * float(x['sz']):.2f}",
                        "fee": x.get("fee"), "fee_token": x.get("feeToken"), "hash": x.get("hash"),
                    })
                if not out:  # fall back to the order response so nothing goes unlogged
                    t = datetime.now(timezone.utc)
                    out.append({"time_utc": t.isoformat(), "time_local": t.astimezone().isoformat(),
                                "network": network, "coin": f"{pair['base']}/{pair['quote']}", "side": "B",
                                "oid": oid, "tid": "", "price": f["avgPx"], "size_btc": f["totalSz"],
                                "notional_usdc": f"{float(f['avgPx']) * float(f['totalSz']):.2f}",
                                "fee": "unknown", "fee_token": "", "hash": ""})
                write_fills(csv_path, out)
                for r in out:
                    log(f"  fill: {r['size_btc']} @ {r['price']} fee {r['fee']} {r['fee_token']}")
                log(f"Logged {len(out)} fill(s) to {csv_path}")
                all_rows += out
        if i < chunks and chunk_delay > 0 and args.live:
            log(f"Waiting {chunk_delay}s before next chunk...")
            time.sleep(chunk_delay)

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
