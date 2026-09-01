# Polymarket Hotkey Trader

Ultra-fast trading system for Polymarket driven by global hotkeys. Places orders instantly on a key combination, with real-time fill confirmation over WebSocket and a portfolio view after every trade.

**Target**: < 1 second from keypress to executed order.

## Requirements

- Python 3.11+ (required by `polymarket-client`)
- Windows (for global hotkeys)
- Administrator privileges (to capture global hotkeys)
- pUSD on the signer's **Deposit Wallet** (see [Wallet](#wallet))
- Optionally, an **HTTP proxy** to route the bot's own traffic (see [Network and Proxy](#network-and-proxy))

## SDK

The bot uses **`polymarket-client`**, Polymarket's official unified SDK, pinned to `0.1.0`.

Do not go back to `py-clob-client-v2`: version 1.1.0 polls for settlement hashes
(up to 30s) *before* returning from `POST /order`, holding the order lock and
delaying the next hotkey.

Two version constraints not to relax:

- `polymarket-client==0.1.0` — hard pin: the 0.x line states that minor releases may introduce breaking changes.
- `websockets>=14` — the SDK declares `>=13`, but the `proxy=True` default (which makes it read `HTTPS_PROXY` from the environment) exists only from 14 on. With a 13.x the fill stream would ignore the proxy and go out on the direct connection.

## Installation

### 1. Clone/download the project

```bash
cd "polymarket sports"
```

### 2. Create and activate the virtual environment

**Mandatory**: the bot must run inside a Python virtual environment.

```bash
python -m venv .venv
.venv\Scripts\activate
```

> Every time you open a new terminal to use the bot, remember to activate the venv:
> ```bash
> .venv\Scripts\activate
> ```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

Copy `.env.example` to `.env`:

```bash
copy .env.example .env
```

Edit `.env` with your credentials:

```env
POLYMARKET_PRIVATE_KEY=0x_your_private_key

# Optional: log level (defaults to INFO)
# POLYMARKET_LOG_LEVEL=DEBUG

# Optional: HTTP proxy for the bot's own traffic (see the dedicated section)
# IPROYAL_PROXY=http://user:pass@host:port
```

Only the private key is needed. The wallet is **not configurable** (see [Wallet](#wallet)),
and the L2 API credentials are not set by hand: the bot derives them from the
private key with `create_or_derive_api_key()`.

#### How to get the Private Key from MetaMask:
1. Open MetaMask
2. Click the 3 dots next to the account name
3. "Account details" > "Show private key"
4. Enter your password and copy the key (starts with `0x`)

**IMPORTANT**: Never share your private key or your API keys!

## Wallet

The wallet the bot trades on is **not configurable**: it is always the **Deposit
Wallet** derived from the signer, resolved by the SDK when `wallet` is not passed
(the legacy UUPS one if already deployed, otherwise the beacon).

This is not a style choice. The CLOB accepts an EOA as maker only if it is
allowlisted for trading; otherwise `POST /order` is rejected by the gateway with
`maker address not allowed, please use the deposit wallet flow`, and the
[docs](https://docs.polymarket.com/trading/wallets-auth) describe no procedure
for getting allowlisted. Making the wallet configurable would only buy you the
ability to reproduce that error, so `POLYMARKET_FUNDER_ADDRESS` is no longer
read.

At startup the bot prints the address it resolved:

```
Active wallet: 0x9F24...540b (DEPOSIT_WALLET)
```

**That is where the pUSD has to be.** If you have a Polymarket account created
from this same MetaMask, that address is already your account and the funds are
already there: nothing needs moving. Approvals on the Deposit Wallet are gasless
via the Relayer and the SDK recovers them on its own if an order fails for
allowance reasons, so there is no manual approval step.

## Network and Proxy

The bot can send its own traffic (httpx + WebSocket) through an HTTP proxy, or
run on the machine's direct connection. The route the traffic takes is the
single biggest factor in round-trip latency, so it is worth choosing
deliberately.

### Option 1 - System VPN (simple, suboptimal latency)

Any commercial VPN (Bitdefender, NordVPN, etc.) active at OS level. All the machine's traffic goes through the tunnel.

**Pros**: zero configuration on the bot side. **Cons**: variable latency, encryption overhead on the whole system, consumer servers not tuned for low latency.

### Option 2 - Application-level proxy (recommended for latency)

Set `IPROYAL_PROXY` in `.env`. The bot will route **only its own traffic** (httpx + WebSocket) through the proxy, leaving the rest of the system direct.

```env
IPROYAL_PROXY=http://username:password@proxy-host:port
```

Tested with [IPRoyal](https://iproyal.com) ISP proxies exiting in Ireland (~63ms steady-state vs ~71ms on a consumer VPN, with much lower jitter).

**Important**:
- Disable any system VPN when using `IPROYAL_PROXY`, otherwise you get **double-tunneling** (traffic encrypted twice → latency doubled)
- Use **ISP** or **static datacenter** proxies, NOT **rotating residential** ones (the IP changes mid-trade → connection lost)
- If you see `[proxy] IPROYAL_PROXY active (exit via ...)` at startup, the proxy loaded correctly

If `IPROYAL_PROXY` is not set, the bot runs on a direct connection.

## Running

```bash
.venv\Scripts\activate
python main.py
```

> The program must run as **Administrator** for the global hotkeys.

## How it works

The bot works on **one match at a time**, handling its 3 1X2 markets
simultaneously: **Team1 win**, **Draw**, **Team2 win**. For each it buys and
sells the YES token ("does this team win?").

At startup:
1. The wallet balance is shown
2. You are asked to paste a **Polymarket event URL** (e.g. `https://polymarket.com/sports/sea/sea-gen-tor-2026-02-22`)
3. The bot auto-detects the 3 markets from the event and configures them
4. The hotkeys go live and prices refresh every 2 seconds

With `CTRL+M` you switch to another match by pasting a new URL, without restarting.

### Non-tradable markets

Loading an event filters out the markets Gamma reports as closed or inactive,
but that filter is deliberately skipped when fewer than 3 markets survive it —
otherwise a match whose payload is incomplete would refuse to load at all. A
closed outcome can therefore still get through and end up bound to a hotkey.

`active` and `closed` come fresh from the CLOB, and an outcome failing either is
marked on the board:

```
  [1] Athens Kallithea: $0.09  max 0.99
  [X] Draw: $0.26  max 0.30
  [2] AE Larisas: $0.68  max 0.55  NOT TRADABLE
```

Orders on that outcome will be rejected by the CLOB. The marker is drawn with
the board on every refresh, so it stays visible, and the matching warning is
reprinted under the board (see [Load warnings](#load-warnings)).

### Load warnings

Anything the bot warns about while resolving an event is reprinted under the
market board, between the prices and `Ready for trading`:

```
  [1] Athens Kallithea: $0.09  max 0.99
  [X] Draw: $0.26  max 0.30
  [2] AE Larisas: $0.68  max 0.55  NOT TRADABLE
------------------------------------------------------------

! Outcomes not recognised by name in 'Athens Kallithea vs. AE Larisas': assigned by response order. Check that [1] is Athens Kallithea and [2] is AE Larisas before trading.
! Market 'AE Larisas' is not tradable (closed=True, active=False): orders on that outcome will be rejected.

Ready for trading. Press hotkeys to place orders.
```

They have to be reprinted because loading an event is always followed by a
screen clear, which would otherwise wipe them a moment after they appear — and
one of them is the warning about `[1]` and `[2]` possibly being swapped, which
decides which team a buy hotkey actually hits.

The warnings stay on screen until the next match is loaded. A load that fails
leaves the previous ones in place, since they still describe the match that is
actually on the board.

## Hotkeys

The bot uses a **single amount** for every order, changeable on the fly with `CTRL+A`.

| Hotkey | Action |
|--------|--------|
| `CTRL+F1` | Buy Team 1 |
| `CTRL+F2` | Buy Draw |
| `CTRL+F3` | Buy Team 2 |
| `CTRL+F4` | Sell Team 1 |
| `CTRL+F5` | Sell Draw |
| `CTRL+F6` | Sell Team 2 |
| `CTRL+SHIFT+F1` | Max buy price Team 1 |
| `CTRL+SHIFT+F2` | Max buy price Draw |
| `CTRL+SHIFT+F3` | Max buy price Team 2 |
| `CTRL+A` | Change amount |
| `CTRL+B` | Check balance |
| `CTRL+M` | Change markets (new event URL) |
| `CTRL+Q` | Quit |

> The hotkeys can be customised in `config.json`. The on-screen guide is drawn
> from that file, so it always shows the bindings actually in force.

### Max buy price per outcome

By default the bot buys at any price up to **99c**. With
`CTRL+SHIFT+F1/F2/F3` you lower the cap for a single outcome: the terminal asks
for the new limit **in whole cents** (type `60` for $0.60, from 1 to 99, Enter
to cancel).

From then on a BUY on that outcome fills only the part of the book below the
cap and cancels the rest — if the price has risen past it, it **buys nothing**.
The other two outcomes stay at 99c: the cap is per outcome, not global.

The cap in force is always visible next to the price, in yellow when it has been
lowered:

```
  [1] Athens Kallithea: $0.09  max 0.99
  [X] Draw: $0.26  max 0.30
  [2] AE Larisas: $0.68  max 0.55
```

With `CTRL+M` the three limits **go back to 99c**: slots 1/X/2 point at
different teams, and an inherited cap would silently block buys on the new
match.

> **Why SHIFT and not `CTRL+F1+M`**: the `keyboard` library matches on the
> *exact* set of keys held down at that moment. Pressing CTRL+F1 and then M
> means that, the instant F1 goes down, the set is `(ctrl, f1)` and **a real
> buy order fires**; the prompt would only come afterwards. With
> `(ctrl, shift, f1)` the combination never coincides with `(ctrl, f1)` and the
> buy does not trigger. Hold SHIFT *before* touching F1: if F1 goes down with
> CTRL alone, that is a buy.

## How orders work

Orders are sent as **FAK (Fill-And-Kill)** with aggressive pricing to guarantee immediate execution:

- **BUY**: you pass the **amount in pUSD**, not a size. `max_price` is the cap
  above which it will not fill (default 0.99, per outcome via
  `CTRL+SHIFT+F1/F2/F3`), not the execution price: you buy at the best ask
  available, so $1 against an ask of $0.09 becomes ~11.11 shares
  (`amount / fill price`).
- **SELL**: `min_price=0.01`, sells **every share** held at the best bid. The
  size is floored to 2 decimals (an API requirement), so a remainder under 0.01
  shares can be left behind: it accumulates and is sold on the next round.

The order "sweeps" the orderbook, filling against all the available liquidity.

The price shown on screen is the **ask**: what it costs to press BUY, not what
you would collect by selling.

An immediate round trip therefore costs the whole spread: you buy at the ask and
sell back at the bid. On an outcome at $0.09 with a bid at $0.07 that is 2
cents, i.e. **-22%** on the capital committed; the same 2 cents on an outcome at
$0.68 would be -3%. The less likely the outcome, the more the spread weighs in
percentage terms.

### Real-time fill confirmation

After every order the bot shows a confirmation with the real fill data. Which of
the two paths is used is decided by the `status` the CLOB returns:

- `delayed` — **the normal case on sports markets**: confirmation over WebSocket within ~4 seconds (MATCHED event from the Polymarket user channel)
- `matched` — immediate fill: the data comes straight from the REST response (makingAmount/takingAmount), without waiting for the stream

If no MATCHED arrives within 4 seconds, a WARNING appears with the order id:
the order was accepted by the server and may well have executed anyway, so
balance and positions have to be checked by hand.

The `FillTracker` keeps a persistent WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/user` to receive trade events in real time.

### Portfolio after every fill

After every fill confirmation, `PortfolioDisplay` automatically shows:
- Price and shares of that fill
- **Bid now**: the price you could exit at right now
- **P/L if you exit**: the difference between the price paid and the current bid

Right after a buy that P/L starts negative by the whole spread. That is correct:
you already paid that cost on the way in.

### Speed optimisations

- Prices (bid and ask) are refreshed every 2 seconds in the background with **a single** batched `get_prices` call: no price read at keypress
- `tick_size` and `neg_risk` are served by `order_fastpath.py` from `MarketData`. Without it the SDK would do `GET /tick-size` and `GET /neg-risk` before every signature: two round trips that were worth ~2/3 of a BUY's latency
- BUY: 1 HTTP call only (`POST /order`)
- SELL: 2 calls (reads the shares via `get_position`, then posts the order)
- Persistent HTTP/2 connection with keep-alive to the CLOB (httpx)
- 0.5s cooldown between orders (prevents accidental double-clicks)

`POST /order` cannot be eliminated: the [docs](https://docs.polymarket.com/api-reference/trade/post-a-new-order)
only provide for sending orders over WebSocket for Perps, not for prediction markets.

> To cut latency further: use an application-level proxy instead of a system VPN (see [Network and Proxy](#network-and-proxy)) and reduce network distance to the CLOB (origin AWS us-east-1, Cloudflare edge).

## Configuration

Edit `config.json`:

```json
{
  "hotkeys": {
    "buy_team1": "ctrl+f1",
    "buy_draw": "ctrl+f2",
    "buy_team2": "ctrl+f3",
    "sell_team1": "ctrl+f4",
    "sell_draw": "ctrl+f5",
    "sell_team2": "ctrl+f6",
    "limit_team1": "ctrl+shift+f1",
    "limit_draw": "ctrl+shift+f2",
    "limit_team2": "ctrl+shift+f3",
    "set_amount": "ctrl+a",
    "check_balance": "ctrl+b",
    "change_markets": "ctrl+m",
    "quit": "ctrl+q"
  },
  "default_amount": 1.0,
  "cooldown_seconds": 0.5,
  "clob_host": "https://clob.polymarket.com",
  "gamma_host": "https://gamma-api.polymarket.com"
}
```

### Configurable options

- **hotkeys**: key combinations for each action
- **default_amount**: default amount in pUSD at startup
- **cooldown_seconds**: minimum time between consecutive orders
- **clob_host**: Polymarket CLOB API endpoint (used only by `market_info.py`)
- **gamma_host**: Polymarket Gamma API endpoint (used only by `market_info.py` and `event_fetcher.py`)

These same values are the defaults compiled into `main.py`, so deleting
`config.json` changes nothing about how the bot behaves.

`chain_id` and `signature_type` no longer exist: the SDK ships its own endpoints
and infers both from the wallet address.

## File structure

```
polymarket sports/
├── main.py                # Entry point, app loop, hotkey wiring
├── trader.py              # CLOB trading logic, position/balance tracking
├── order_fastpath.py      # Serves tick_size/neg_risk from cache (2 fewer round trips)
├── fill_tracker.py        # Real-time fill confirmation over WebSocket
├── portfolio_display.py   # Portfolio view after every fill
├── event_fetcher.py       # Fetches football events from a Polymarket URL
├── hotkey_manager.py      # Global hotkey management
├── market_info.py         # API client (Gamma + CLOB), MarketData
├── console_ui.py          # Console interface with colorama
├── config.json            # Hotkey and parameter configuration
├── .env                   # Credentials (do NOT commit!)
├── .env.example           # Credentials template
└── requirements.txt       # Python dependencies
```

## Troubleshooting

### "Hotkeys don't work"
- Run the program as **Administrator**
- On Windows: right-click cmd/PowerShell > "Run as administrator"

### "maker address not allowed, please use the deposit wallet flow"
- The CLOB is rejecting the EOA as maker. Make sure `POLYMARKET_FUNDER_ADDRESS` is not forcing a wallet: this bot ignores it, but an old version of the code did not (see [Wallet](#wallet))

### "Insufficient balance"
- Check the balance of the **Deposit Wallet**, not of the EOA: the right address is the one printed at startup as `Active wallet:`
- Allowances need no manual action: they are gasless via the Relayer and the SDK recovers them on its own

### "Invalid signature"
- Check that the private key in `.env` is correct and starts with `0x`

### "The proxy does not seem to be in use"
- Check that the `[proxy] IPROYAL_PROXY active (exit via ...)` line appears at startup: without it, `IPROYAL_PROXY` was not read from `.env`
- If the line appears, check in your provider's panel which exit IP the proxy is actually using
- If you have a system VPN AND `IPROYAL_PROXY` at the same time: turn the VPN off, they conflict (see [Network and Proxy](#network-and-proxy))

### "Could not identify Team1, Draw, and Team2 markets"
- The event URL has to point at a match with 3 outcomes (Team1, Draw, Team2)
- Check that the markets are still active and not closed
- The bot would rather stop than guess which of the three markets is the draw:
  if it cannot recognise it, it does not load the event

### "Outcomes not recognised by name: assigned by response order"
A warning, not an error: the markets were loaded, but the team names in the
event title did not match those of the individual markets (which happens when
Gamma abbreviates, e.g. "Genoa" against "Genoa CFC"), so `[1]` and `[2]` were
assigned **in the order the API returned them**.

In almost every case that is the right order, but it is a guess: **before
pressing BUY, check that `[1]` really is the team you think it is**, by
comparing the on-screen prices with those on the Polymarket page. If they are
swapped, do not use the bot on that event.

The warning is reprinted under the market board and stays there until you load
another match, so you can check it at the moment you press the key rather than
having to catch it as it scrolls past (see [Load warnings](#load-warnings)).

## Security

- The private key is NEVER printed or logged
- `.env` is in `.gitignore` (it holds the private key and the proxy credentials)
- Orders are signed locally (the key never leaves your machine)
- The L2 API credentials are derived from the private key at every startup and used only to authenticate the CLOB and the WebSocket channel (`wss://ws-subscriptions-clob.polymarket.com/ws/user`); they are never written to disk
- A 0.5s cooldown prevents accidental orders
- The default log level is `INFO`. Raise it to `DEBUG` (`POLYMARKET_LOG_LEVEL=DEBUG`) only for troubleshooting: at that level full order responses are printed and, if you use `IPROYAL_PROXY`, the proxy URL with its credentials can show up in the httpx logs

## Disclaimer

This software places REAL orders with REAL money. The order sweeps the
orderbook at the best price available, not at a specific price: the only
protection is the max buy price, which defaults to 99c, i.e. practically none
until you lower it yourself. Test with small amounts.

## License

MIT License
