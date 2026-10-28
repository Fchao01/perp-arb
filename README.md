# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. Select the primary with `--primary`
(default `light-rh` / `lighter-rh`, **RH**), or choose `lighter`, `entropy`
(the `io` dex on Hyperliquid), or `tradexyz`. Select the other leg with
`--hedge` (default **Lighter mainnet**):

| `--hedge` | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood chain: <https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz (Hyperliquid): <https://app.hyperliquid.xyz/join/QUANTGUY>

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book with taker orders,
carrying a delta-neutral position until the premium reverts and the opposite
crossing unwinds it. Every price it acts on is the **actual order book of the
exchange that will fill the order** — Hyperliquid books come from the official
websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from Lighter's
official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute CSV bars**, and the bundled analyzer turns that data into
the three numbers that define the whole strategy.

## The signal

The band is three numbers in `config.yaml`, derived by you from recorded
data:

```
premium_bps = (Entropy price / hedge price − 1) × 10 000

                          ┌──────────────  SELL entropy + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY entropy + SELL hedge
```

- `midline_bps` — where the premium normally sits. Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — the entry bands on each side of the midline.

Both hurdles are applied to **executable** prices (entropy bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. A full round trip therefore nets
**≥ upper + lower bps after fees by construction**.

One consequence worth understanding: with `midline_bps: 5`, the buy-entropy
hurdle is `lower − midline`, which can be **negative**. That is intentional —
if entropy is persistently 5 bps rich, buying it at a 0 bps premium is 5 bps
cheap versus its own equilibrium, and that trade is the profitable unwind of
an earlier sell at `midline + upper`. It also means a **wrong midline loses
money**: if you type `midline_bps: 5` while the true premium sits at 0, the
bot happily buys entropy at fair value all day. Measure first, then trade —
that is what the recorder and analyzer are for.

`midline_bps: auto` takes the midline from the market instead: a
time-weighted EMA of the live mid premium with a time constant of
`midline_window_min` (default 60). `python3 tools/analyze.py --csv
logs/eth/minutes.csv --midline auto --min-band 1.0` replays that midline over
recorded minutes and suggests one symmetric band (never below `--min-band`,
which should be the primary leg's fill slippage from `tools/leg_report.py`).
On start it is seeded from the recorder's
minute bars of the last window, and the bot does not trade until it has
`midline_warmup_min` (default 10) of history. Because the band moves with the
premium, both directions keep firing and the inventory turns over instead of
pinning one side at its cap. A trending premium still pushes one direction
(the midline lags), so keep the inventory ladder on.

## Quick start

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

`--symbol` is required. `--primary` overrides `.env`'s `PRIMARY_VENUE`
(default `light-rh` / RH); `--hedge` defaults to `lighter`. Both legs must be
different markets. For the default pair use `python3 main.py --record-only --symbol LIT`.
For the original pair add `--primary entropy --hedge lighter-rh`.
Re-record data and recalibrate thresholds when changing pairs. The signal
examples use Entropy; at runtime this refers to the selected primary.

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
python3 main.py --record-only --symbol SNDK --primary entropy --hedge lighter-rh
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes `logs/{symbol}/minutes.csv`.

**2. Analyze and set your thresholds:**

```bash
python3 tools/analyze.py --csv logs/sndk/minutes.csv
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --primary entropy --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/{symbol}/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row:

| column | meaning |
|---|---|
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `entropy_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of Entropy over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL entropy (entropy bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY entropy (hedge bid / entropy ask − 1) |
| `samples` | how many of the ~60 seconds both books were fresh |

Recorded edges are pre-fee; the analyzer subtracts `--fees-bps` (pass the
**sum** of both venues' taker fees — default 0.0 for the zero-fee venues,
~1.0 with a `tradexyz` hedge) before counting firings, so its table and
suggestions translate directly into config values. `--hours 24` restricts to
recent data; premiums drift, so re-run it regularly and update
`config.yaml`.

## Configuration

Strategy lives in `config.yaml` (validated — unknown keys are startup
errors), credentials in `.env`, and the markets on the command line
(`--symbol`, `--primary`, `--hedge`). The optional `primary:` section configures
the selected primary; for Entropy it overrides legacy `entropy:` settings. Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `thresholds.midline_bps` | premium center (measure it!), or `auto` | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `thresholds.midline_window_min` | auto midline EMA time constant | 60 |
| `thresholds.midline_warmup_min` | auto midline history needed before trading | 10 |
| `entropy.dex` | Entropy's dex name on Hyperliquid | `io` |
| `*.taker_fee_bps` | per-venue taker fee | 0.0 (tradexyz hedge: 1.0) |
| `*.max_position_usd` | per-venue position cap | 1000 |
| `*.max_orders_per_min` | per-venue send budget (sliding 60 s) | 120; lighter hedges 30 |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | per-slice cap | 500 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.lead_venue` / `lead_slippage_bps` | sequential execution: which leg goes first as a tight IOC limit | `none` / 0 |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `recorder.*` | minute-data recorder | on, `logs/{symbol}/minutes.csv` |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; durable log file in all modes | on, `logs/{symbol}/engine.log` |

Log files use lowercase symbol directories: LIT writes `logs/lit/minutes.csv`,
`logs/lit/trades.csv`, and `logs/lit/engine.log`; BTC writes its files under `logs/btc/`.
Restarting the same symbol appends to its files. The minute recorder avoids
duplicating the same partial minute after a restart. Use `{symbol}` in `recorder.csv`,
`logging.trades_csv`, or `logging.file` for custom paths such as
`logs/{symbol}/trades.csv`. Legacy fixed paths automatically gain a lowercase symbol
directory: `logs/trades.csv` becomes `logs/btc/trades.csv` for BTC. Existing files are not renamed or migrated. Pass the
symbol's minute file to the analyzer with `--csv`. Multiple processes or venue
pairs trading the same symbol still share paths; use separate directories when
those also need isolation.

## Credentials (`.env`, live only)

- **Entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. With `--hedge tradexyz`
  both legs share this account by default (one nonce sequence is handled
  internally); set `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`
  to split them. Fund the dex-specific clearinghouses you trade.
- **Lighter** — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`, and
  `LIGHTER_API_PRIVATE_KEY` are mainnet credentials. RH uses separate
  `LIGHTER_RH_ACCOUNT_INDEX`, `LIGHTER_RH_API_KEY_INDEX`, and
  `LIGHTER_RH_API_PRIVATE_KEY`. The default pair needs both sets. A legacy
  single-RH pair can still use `LIGHTER_*`; the two-chain pair never shares
  those keys. See [lighter-python](https://github.com/elliottech/lighter-python).

## Telegram trade notifications

Set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` to enable Chinese
trade notices; leave both empty to disable. Start the bot in a private chat or
add it to the target group first. Notices use the
[Telegram sendMessage API](https://core.telegram.org/bots/api#sendmessage).

Each pair execution with a fill or an unresolved result queues the requested
trade template: status, `sell_primary` / `buy_primary`, leg fills, planned size,
both-leg notional, trade and cumulative profit, hedge count, balances, equity,
and account equity change. Partial/failed/unresolved executions are labeled
accordingly; unknown values show `—`. Sending runs in the background and failures
only log a warning. Shutdown allows up to 10 seconds to drain the queue. Failed
sends are not retried; network failures or a forced exit can lose notices.

Accounting is unchanged: profit uses matched-fill price edge less configured
fees (`fill_edge`), excluding funding, unmatched inventory PnL, and corrective
hedge costs. Totals are per process session. Existing cumulative volume can use
planned limit prices if fill prices are unavailable; unknown current-trade
values are shown as unavailable. Balances/account change use the existing
30-second balance snapshots; hedge count is the existing corrective attempt count.
Legacy CSV columns and internal `sell_entropy` / `buy_entropy` keys still refer
to the selected primary. Strategy, sizing and risk rules are unchanged.

Execution telemetry is recorded in the trade CSV: actual average fill prices,
configured fee rates and fee amounts, client and exchange order IDs, best-bid/
best-ask snapshots and their ages, plus per-leg `submit_latency_ms`,
`confirm_latency_ms`, `total_latency_ms`, and the concurrent pair total. These
are client-observed intervals; `confirm_latency_ms` includes the exchange
settlement path and network response, so it should not be called pure exchange
matching time. Corrective hedges are written to `logging.hedges_csv` (default
`logs/{symbol}/hedges.csv`) with their own IDs, prices, timings, residual size,
and close PnL when the source fill price is known. A blank close PnL means the
position could not be fully attributed to a fill observed by this process.
Use `python3 tools/analyze_trades.py --csv logs/{symbol}/trades.csv` to print
per-leg and pair latency percentiles and the share under a 350ms target.

`tools/leg_report.py` reads the same files to explain misses: it sorts
executions by outcome (both filled, primary missed, hedge missed, ...),
measures each leg's fill against its planned price, compares planned with
captured edge, adds the repair hedges' P&L, and splits all of it by planned
premium, by which venue's book updated last before the decision, by hour and
by day:

```bash
python3 tools/leg_report.py --csv logs/sndk/trades.csv --hedges logs/sndk/hedges.csv
```

## Read-only all-market screener and dashboard

`tools/market_screener.py` independently observes common active RH/Lighter
perpetual markets. It never loads credentials or submits orders.

```bash
python3 tools/market_screener.py collect --hours 72 --notional 300
# In another terminal: http://127.0.0.1:8765
python3 tools/market_screener.py serve --input logs/screener --port 8765
```

Collection includes fixed-quantity delayed depth probes. Analysis freezes
thresholds fitted on the first 24 hours and evaluates later shadow round trips.
The dashboard exposes live quotes, coverage, delayed signal survival, remaining
inventory, forced exits and profit concentration. Shadow results are not actual
PnL or fill probabilities; funding and contract equivalence require review.
See the [usage and assumptions guide](docs/market_screener.zh-CN.md).

## Edge lifetime (read-only)

`tools/edge_lifetime.py` watches the same two books as the bot, with the same
config, fees, hurdle and sizing, and records every episode in which an
executable edge exists: how many milliseconds it lasts, which venue's update
opened and closed it, and the excess still left at 50 ms ... 2 s. The report
shows how many edges are still there when an order lands, with and without a
persistence gate. It never loads credentials or sends orders. Run it on the
same server as the bot — network location is what it measures.

```bash
python3 tools/edge_lifetime.py collect --symbol SNDK --config sndk.yaml
python3 tools/edge_lifetime.py report --csv logs/sndk/edge_episodes.csv
```

## Maker shadow simulation (read-only)

`tools/maker_shadow.py` tests the alternative to taking stale quotes: being
the RH maker. It shadow-quotes both sides of the primary around the hedge
venue's mid plus the premium center (fixed, or an EMA with
`--center-bps auto`), keeps the quotes with the exchange's place/cancel
latencies and a per-minute action budget, fills them from RH's public trade
stream, and prices a taker hedge on the other venue. Several half-spreads run
at once. The report shows fills per hour, edge against the center, session
mark-to-market, 1/5/30 s markouts (negative = being picked off) and who took
the quotes. Nothing is sent; run it on the bot's server.

```bash
python3 tools/maker_shadow.py collect --symbol SNDK --config sndk.yaml --half-spread-bps 2,3,4,6
python3 tools/maker_shadow.py report --dir logs/sndk
```

## How execution works

- Both legs are **taker** orders sent concurrently: Lighter market orders
  with average-price protection settling on the authenticated account
  websocket; Hyperliquid IOC limits settling synchronously (with
  orderStatus polling for unknown outcomes).
- **Sequential mode** (`lead_venue: primary` or `hedge`): the lead leg goes
  first as an IOC limit at the planned price (plus `lead_slippage_bps`), so
  it only fills while the quoted edge still exists. The other leg is sent
  only for the size the lead filled; a lead miss sends nothing. Use it when
  one venue's fills land far from its book. It adds one order round trip of
  latency to the follower leg.
- A leg filling more than the other counts as an execution problem toward
  `max_consecutive_errors`; trades and expected edge count matched size only.
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Failure containment**: a rate-limited venue pauses briefly; an
  unreachable venue (e.g. exchange maintenance) pauses trading and is probed
  every `venue_probe_sec` until it recovers; `max_consecutive_errors`
  execution pathologies halt the engine entirely.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

## Layout

```
main.py                  entry point (--record-only, or live by default)
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/engine.py    the two-venue strategy loop
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
tools/analyze.py         minutes.csv -> suggested thresholds
tools/edge_lifetime.py   how long executable edges last (read-only)
tools/maker_shadow.py    shadow RH maker quotes + hedge (read-only)
tools/leg_report.py      trades.csv -> why legs miss / fill off plan
tests/                   python3 -m pytest tests/
```

## Known risks

- **A wrong midline is a losing strategy.** The premium center drifts;
  re-measure regularly and keep `config.yaml` current, or use
  `midline_bps: auto`.
- **USDG basis** (`lighter-rh`): the hedge quotes in USDG. Part of any
  persistent premium is the stablecoin itself; your midline absorbs the
  level, but a USDG *move* is real PnL.
- **Funding**: two venues, two independent funding rates; carry is not
  modeled. Position caps bound it — keep them modest.
- **Thin books**: Entropy depth can be tiny; `take_fraction` and notional
  caps keep clips small, but slippage on the hedge leg after a partial fill
  is real.
- **Market hours**: for equity perps (e.g. SNDK), off-hours oracle regimes
  differ per venue; consider wider bands or not trading them.
- **One-leg risk**: a leg can fail after the other filled. The bot hedges
  and reconciles automatically, but you should still watch it.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Start with tiny position caps.

## License

[MIT](LICENSE)
