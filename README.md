# hl-dca: Bitcoin DCA on Hyperliquid

A small, cautious Python script that buys a fixed dollar amount of Bitcoin with USDC on [Hyperliquid](https://app.hyperliquid.xyz) spot each time it runs. Schedule it daily (or several times a day) and it dollar-cost-averages for you.

- **Dry run by default.** It only prints what it would do until you add `--live`.
- **Trade-only key.** It signs with a Hyperliquid *API wallet*, which can place trades but cannot withdraw funds. It refuses to run with a main wallet key.
- **Capped price.** Every buy is an immediate-or-cancel limit order that will never pay more than the best ask + 0.5% (configurable).
- **Logs every fill** (time, price, size, fee) to a CSV you can use for your records.

Withdrawing the BTC to your own wallet is deliberately **not** automated. You do it yourself through [Unit](https://app.hyperunit.xyz), signing with your main wallet.

> ## ⚠️ Disclaimer: not financial advice, use at your own risk
> This is an unofficial, community script. It is **not affiliated with, endorsed by, or supported by Hyperliquid or Unit**. Nothing here is financial, investment, tax or legal advice. Crypto is volatile and you can lose money. Software can have bugs, and exchanges, bridges and APIs can fail or change without notice. You are solely responsible for your keys, your funds, your trades and complying with the laws and taxes where you live. The software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). **Read the code, run the dry run, and start with a small amount.**

---

## How it works

1. You deposit USDC to your Hyperliquid account.
2. On each run the script:
   - looks up the UBTC/USDC spot market from Hyperliquid's live metadata (no hardcoded index),
   - checks your available spot USDC and skips with a clear message if it's too low,
   - checks that the market looks sane (spread and price versus Hyperliquid's BTC perp),
   - places one IOC limit buy for your dollar amount (or several smaller buys), and
   - writes each fill to `fills.csv`.
3. Whenever you like, you withdraw the accumulated BTC to your own Bitcoin wallet through Unit.

### What it buys

| Item | Value (checked against the live API, Nov 2026) |
|---|---|
| Market | **UBTC/USDC** spot. UBTC is "Unit Bitcoin", BTC bridged by Unit that can be withdrawn as native Bitcoin |
| API coin name | `@142` |
| Order asset id | `10142` (spot asset ids are 10000 + market index) |
| Token | UBTC, token index 197, size step 0.00001 BTC |
| Minimum order | 10 USDC per order |

---

## Costs

| What | Cost | Source |
|---|---|---|
| Spot buy (taker), base tier (under $5M 14-day volume) | **0.070%** | [Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) |
| Spot maker (resting order) | 0.040% | same |
| HYPE staking discount | 5% to 40% off | same |
| Your actual rate | printed on every run (via the `userFees` API) | |
| USDC deposit | source-chain gas only. **Minimum 5 USDC, less is lost** | [Hyperliquid USDC docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/bridge2) |
| BTC withdrawal via Unit | no Unit fee, you pay the Bitcoin network fee (live estimate when checked: 715 sats). **Minimum 0.0003 BTC** | [Unit fees](https://docs.hyperunit.xyz/faq/how-do-fees-on-unit-work), [minimums](https://docs.hyperunit.xyz/unit/about-unit/supported-assets), [live estimate](https://api.hyperunit.xyz/v2/estimate-fees) |

Example: $500 a day for a year is $182,500 of buys. At 0.070% that's about $128 in trading fees, plus the bid/ask spread (a fraction of a basis point to a few bps when tested). Fees and minimums can change, so check the linked pages.

---

## Setup

You need Python 3.9 to 3.13, an EVM wallet (for example Rabby or MetaMask) holding USDC, and a Bitcoin wallet you control (a hardware wallet is ideal).

### 1. Install
```bash
git clone <this repo> hl-dca && cd hl-dca     # or unzip the download
python3 -m venv .venv
source .venv/bin/activate                     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                          # Windows: copy .env.example .env
```

### 2. Deposit USDC to Hyperliquid
1. Open https://app.hyperliquid.xyz and connect your wallet.
2. Click **Deposit**, choose **USDC** and a supported chain (Arbitrum is the common one, and you need a little of that chain's gas token), enter the amount and confirm. **Send at least 5 USDC.**
3. Open **Portfolio**. If your account shows separate **Perps** and **Spot** balances (Standard mode), use **Transfer** to move USDC from Perps to Spot. In Unified mode there is one balance and nothing to do. The script reads your **spot** USDC balance. See [account modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes).

### 3. Create an API wallet (trade-only key)
1. Go to https://app.hyperliquid.xyz/API.
2. Enter a name (for example `dca`), click **Generate**, and choose how long it stays valid (180 days max).
3. Click **Authorize API Wallet** and sign with your main wallet.
4. Copy the **API wallet private key**. It is shown only once.
5. Edit `.env`:
   - `HL_ACCOUNT_ADDRESS`: your **main** wallet's public address (the account holding the USDC)
   - `HL_API_WALLET_PRIVATE_KEY`: the API wallet key from step 4 (**never** your main wallet key)
   - `DCA_USD`: dollars per run (default 50)

API wallets expire. The script warns you when fewer than 7 days are left. Create a new one before then. Hyperliquid recommends a fresh API wallet each time, not reusing an old address. See [API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets).

### 4. Dry run, one small live buy, then schedule
```bash
python dca.py                        # dry run: market, balance, fee rate, exact order. Nothing sent
python dca.py --amount 15 --live     # one small real buy
cat fills.csv                        # check the logged fill
```
Then schedule it (see below).

### 5. Withdraw BTC to your own wallet (manual)
The API key can't withdraw, which is the point, so you do this yourself:
1. Open https://app.hyperunit.xyz (or Hyperliquid **Portfolio → Withdraw**) and connect your **main** wallet.
2. Choose **Withdraw → Bitcoin**.
3. Paste a receive address from your Bitcoin wallet (on a hardware wallet, check it on the device screen) and enter an amount. **Minimum 0.0003 BTC.**
4. Sign. Unit's guardians send native BTC once the transfer is confirmed. Timing depends on the withdrawal queue and Bitcoin blocks (tens of minutes is typical).
5. Track it in the Unit app or on https://mempool.space.

Withdraw in batches (weekly or monthly) to spend less on network fees. Never send any other asset to a Unit withdrawal address. See [Unit withdraw guide](https://docs.hyperunit.xyz/how-to/withdraw).

---

## Options

| Flag / setting | What it does | Default |
|---|---|---|
| `--live` | actually place orders | off (dry run) |
| `--amount N` / `DCA_USD` | USDC to spend this run | 50 |
| `--chunks N` / `CHUNKS` | split the run into N buys | 1 |
| `--chunk-delay S` / `CHUNK_DELAY_SEC` | seconds between chunks | 0 |
| `--testnet` / `HL_NETWORK=testnet` | use Hyperliquid testnet (its BTC market is often empty or mispriced, so the sanity check may skip) | mainnet |
| `MAX_SLIPPAGE_BPS` | max price above best ask (bps) | 50 (0.5%) |
| `MAX_USD_PER_RUN` | refuses bigger runs (typo guard) | 1000 |
| `MIN_USDC_RESERVE` | USDC always left untouched | 0 |
| `MAX_SPREAD_BPS` | skip if the spread is wider | 50 |
| `MAX_DEVIATION_BPS` | skip if the spot ask is this far from the BTC perp mid | 200 |
| `FILLS_CSV` | where fills are logged | `./fills.csv` |

Exit codes: `0` ok, `2` skipped for low balance, `3` skipped for an odd market, `4` order rejected or errored.

---

## Scheduling

Run it on a machine that's on at that time. A sleeping laptop skips runs.

**Mac / Linux (cron):** run `crontab -e` and add (daily at 09:00 in that machine's local time):
```
0 9 * * * cd /full/path/to/hl-dca && .venv/bin/python dca.py --live >> dca.log 2>&1
```
On macOS you may need to give `cron` Full Disk Access (System Settings → Privacy & Security).

**Windows (Task Scheduler):** go to Create Basic Task → Daily → Start a program.
- Program: `C:\full\path\to\hl-dca\.venv\Scripts\python.exe`
- Arguments: `dca.py --live`
- Start in: `C:\full\path\to\hl-dca`

In the task's properties, tick *Run whether user is logged on or not* and *Run task as soon as possible after a scheduled start is missed*.

**Smaller buys through the day:**
- One run, split: `python dca.py --live --chunks 4 --chunk-delay 900` (4 buys, 15 min apart), or
- Several runs: set a smaller `DCA_USD` and schedule several times, for example `0 3,9,15,21 * * *`.

Every order must be at least 10 USDC.

---

## Risks

- **Key safety:** `.env` holds your API wallet key. Anyone with it can't withdraw, but **can trade your funds badly**. Keep it private and never commit it (it's in `.gitignore`). If it might have leaked, revoke the API wallet at app.hyperliquid.xyz/API right away.
- **UBTC is a bridged token:** it's only native Bitcoin once withdrawn. It relies on Unit's guardian network honouring withdrawals. A failure or hack there could hurt UBTC's value. Withdraw regularly.
- **Platform risk:** Hyperliquid is a relatively young chain and exchange. Outages, bugs and validator or bridge risks exist. Don't keep more there than you need.
- **Stablecoin risk:** USDC is issued by Circle and can be frozen.
- **Automation risk:** missed runs if the machine is off, double buys if you schedule it twice. Check `fills.csv` regularly.
- **Market risk:** DCA doesn't prevent losses. BTC can fall a long way.
- **Tax and law:** rules differ by country. Keep `fills.csv` as your cost-basis record and get local advice.

---

## Testing

- `python dca.py`: dry run against live public market data. No key needed. Without `HL_ACCOUNT_ADDRESS` it skips the balance check.
- `python test_mock_live.py`: exercises the `--live` code path with a **fake** exchange and a **fake** account. It needs no key and sends no order. It also checks that a main-wallet key is refused.

## Files

| File | Purpose |
|---|---|
| `dca.py` | the script |
| `requirements.txt` | dependencies (official [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) 0.24.0) |
| `.env.example` | settings template. Copy it to `.env` |
| `test_mock_live.py` | offline test of the live path |
| `LICENSE` | MIT |

## Sources
- Hyperliquid: [fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees), [asset ids](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids), [tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size), [error responses (10 USDC minimum)](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses), [API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets), [getting started](https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/how-to-start-trading), [USDC](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/bridge2), [account modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)
- SDK: https://github.com/hyperliquid-dex/hyperliquid-python-sdk · https://pypi.org/project/hyperliquid-python-sdk/
- Unit: [withdraw](https://docs.hyperunit.xyz/how-to/withdraw), [minimums](https://docs.hyperunit.xyz/unit/about-unit/supported-assets), [fees](https://docs.hyperunit.xyz/faq/how-do-fees-on-unit-work), [fee estimate API](https://api.hyperunit.xyz/v2/estimate-fees), [Unit Bitcoin token](https://docs.hyperunit.xyz/developers/key-addresses/mainnet/bitcoin)

## License
MIT. See [LICENSE](LICENSE).
