Sure. If you're going to give the task to another AI/coding agent, I'd use a prompt that emphasizes **market-neutral execution, no look-ahead bias, realistic fills, and risk limits**.

```text
You are an expert quantitative researcher and Python developer.

I am researching a "safer" market-neutral strategy for Polymarket BTC 5-minute UP/DOWN markets.

IMPORTANT:
This is a research/backtesting project. Do NOT assume the strategy is profitable. Do not invent missing data. Clearly identify assumptions and limitations.

==================================================
GOAL
==================================================

Build and backtest a market-neutral UP+DOWN strategy.

The basic idea is:

    UP + DOWN = $1 at settlement

Therefore, investigate opportunities where:

    UP_ASK + DOWN_ASK < $1

However, do NOT treat this as automatically profitable.

The backtest must account for:

1. Actual available order-book size
2. Partial fills
3. Bid/ask spread
4. Trading fees
5. Maker/taker status
6. Slippage
7. Timing/latency
8. The possibility that only one side fills
9. Capital constraints
10. Market resolution
11. Position inventory
12. Failed/cancelled orders

The goal is to determine whether this strategy has a REAL historical edge after realistic execution.

==================================================
AVAILABLE DATA
==================================================

I have the following parquet files for approximately 2026-08-07 through 2026-08-12.

--------------------------------------------------
1. binance_price_orderbook.parquet
--------------------------------------------------

Columns:

timestamp BIGINT
Binance_BTC FLOAT

ask_0_1 FLOAT
ask_1_3 FLOAT
ask_3_7 FLOAT
ask_7_15 FLOAT
ask_15_31 FLOAT
ask_31_63 FLOAT
ask_63_127 FLOAT
ask_127_255 FLOAT
ask_255_511 FLOAT
ask_511_1023 FLOAT
ask_1023_ FLOAT

bid_0_1 FLOAT
bid_1_3 FLOAT
bid_3_7 FLOAT
bid_7_15 FLOAT
bid_15_31 FLOAT
bid_31_63 FLOAT
bid_63_127 FLOAT
bid_127_255 FLOAT
bid_255_511 FLOAT
bid_511_1023 FLOAT
bid_1023_ FLOAT

--------------------------------------------------
2. binance_trades.parquet
--------------------------------------------------

timestamp BIGINT
price FLOAT
quantity FLOAT
buyer_is_maker BOOLEAN

--------------------------------------------------
3. chainlink_price.parquet
--------------------------------------------------

timestamp BIGINT
Chainlink_BTC FLOAT
twap FLOAT

--------------------------------------------------
4. orderbooks.parquet
--------------------------------------------------

timestamp BIGINT

up_price FLOAT
down_price FLOAT

up_bid_price FLOAT
up_bid_shares UINTEGER

up_ask_price FLOAT
up_ask_shares UINTEGER

down_bid_price FLOAT
down_bid_shares UINTEGER

down_ask_price FLOAT
down_ask_shares UINTEGER

up_ask_0_1 UINTEGER
up_ask_1_3 UINTEGER
up_ask_3_7 UINTEGER
up_ask_7_15 UINTEGER
up_ask_15_30 UINTEGER
up_ask_30_plus UINTEGER

up_bid_0_1 UINTEGER
up_bid_1_3 UINTEGER
up_bid_3_7 UINTEGER
up_bid_7_15 UINTEGER
up_bid_15_30 UINTEGER
up_bid_30_plus UINTEGER

down_ask_0_1 UINTEGER
down_ask_1_3 UINTEGER
down_ask_3_7 UINTEGER
down_ask_7_15 UINTEGER
down_ask_15_30 UINTEGER
down_ask_30_plus UINTEGER

down_bid_0_1 UINTEGER
down_bid_1_3 UINTEGER
down_bid_3_7 UINTEGER
down_bid_7_15 UINTEGER
down_bid_15_30 UINTEGER
down_bid_30_plus UINTEGER

--------------------------------------------------
5. trades.parquet
--------------------------------------------------

timestamp BIGINT
transaction_hash VARCHAR
wallet VARCHAR
is_up BOOLEAN
is_buy BOOLEAN
is_taker BOOLEAN
price DOUBLE
shares DOUBLE
fill_index INTEGER

--------------------------------------------------
6. meta.json
--------------------------------------------------

Contains:

market_id
condition_id
slug
question
up_token_id
down_token_id
start_time
end_time
resolved_at
btc_open_price
btc_close_price
winner
active
closed
trades_mode
data_health
data_health_checked_at
data_health_comment

Example:

btc_open_price = 63725.65387919053
btc_close_price = 63697.57133523465
winner = false

==================================================
STEP 1 — UNDERSTAND THE DATA
==================================================

Before writing the strategy:

1. Inspect all parquet schemas.
2. Determine timestamp units and timezone.
3. Determine whether timestamps are milliseconds, microseconds, or nanoseconds.
4. Determine whether orderbook rows represent snapshots or changes.
5. Determine exactly what up_ask_shares/down_ask_shares represent.
6. Determine whether the depth bucket columns represent cumulative depth or depth within each price interval.
7. Determine the meaning of is_taker.
8. Determine whether trades.parquet represents actual fills.
9. Verify how winner maps to UP/DOWN.
10. Never assume semantics if they can be inferred from the data.

Print a concise data-quality report.

==================================================
STEP 2 — CREATE MARKET-LEVEL TABLE
==================================================

Create one row per 5-minute market:

market_slug
market_id
start_time
end_time
resolved_at
btc_open_price
btc_close_price
winner

Also calculate:

duration_seconds
final_btc_return
final_btc_return_bps

Verify that winner agrees with btc_open_price and btc_close_price.

==================================================
STEP 3 — CREATE 1-SECOND FEATURE TABLE
==================================================

Resample/synchronize the data into approximately 1-second intervals for each market.

Do NOT use future information.

For each timestamp, create:

market_slug
timestamp
seconds_since_start
seconds_remaining

BTC:

Chainlink_BTC
btc_open
distance_usd
distance_bps

Binance:

Binance_BTC
binance_chainlink_diff

Polymarket:

up_bid_price
up_ask_price
down_bid_price
down_ask_price

up_bid_shares
up_ask_shares
down_bid_shares
down_ask_shares

Calculate:

up_spread
down_spread

up_down_ask_sum
up_down_bid_sum

Potential gross arbitrage:

gross_edge = 1.0 - up_ask_price - down_ask_price

Depth:

up_bid_depth
up_ask_depth
down_bid_depth
down_ask_depth

If bucket values are not cumulative, calculate the correct total depth according to their semantics.

Order-book imbalance:

up_obi =
(up_bid_depth - up_ask_depth)
/
(up_bid_depth + up_ask_depth)

down_obi =
(down_bid_depth - down_ask_depth)
/
(down_bid_depth + down_ask_depth)

==================================================
STEP 4 — TRADE/FLOW FEATURES
==================================================

From trades.parquet calculate rolling 1s, 5s, and 10s statistics:

UP:

taker_buy_volume
taker_sell_volume
taker_net_flow

DOWN:

taker_buy_volume
taker_sell_volume
taker_net_flow

maker volume
taker volume

Do not use trades occurring after the feature timestamp.

==================================================
STEP 5 — IDENTIFY ARBITRAGE OPPORTUNITIES
==================================================

For every timestamp calculate:

gross_cost =
up_ask_price + down_ask_price

gross_edge =
1.0 - gross_cost

But only consider a trade executable if:

up_ask_shares >= requested_size

AND

down_ask_shares >= requested_size

for the requested quantity.

Also investigate partial-fill cases separately.

Do NOT assume that seeing:

UP ask = 0.48
DOWN ask = 0.50

means I can buy unlimited shares at those prices.

==================================================
STEP 6 — EXECUTION SIMULATION
==================================================

Simulate several execution modes.

A. Perfect theoretical execution

Assume both sides fill immediately.

B. Conservative taker execution

Buy UP at ask.
Buy DOWN at ask.

C. Maker execution

Attempt to place limit orders at bid or inside the spread.

Use historical trade/fill information to estimate whether those orders would actually fill.

D. Partial fill scenario

If only one side fills, record the resulting directional inventory.

Do NOT silently assume the second side fills later at the same price.

==================================================
STEP 7 — FEES AND REBATES
==================================================

Do not hard-code a fee unless verified from current Polymarket documentation or explicitly provided by the user.

Create configurable parameters:

taker_fee_rate
maker_fee_rate
maker_rebate_rate
gas_cost
slippage

Run multiple scenarios:

optimistic
base
conservative
stress

Clearly separate:

gross P&L
fees
rebates
slippage
net P&L

==================================================
STEP 8 — CAPITAL/RISK MODEL
==================================================

Initial capital:

$1,000

Test several maximum position sizes:

$5
$10
$20
$50
$100

Never allow total capital committed to exceed available capital.

Track:

cash
UP inventory
DOWN inventory
total exposure
unhedged exposure
realized P&L
unrealized P&L
maximum drawdown

Especially track the dangerous situation:

UP fills
but DOWN does not fill.

==================================================
STEP 9 — MARKET-NEUTRAL HEDGE ANALYSIS
==================================================

For every pair trade:

number of UP shares
number of DOWN shares

Track whether:

UP shares == DOWN shares

If not, calculate the remaining directional exposure.

Calculate:

hedged_cost
unhedged_cost
hedged_profit
unhedged_risk

The strategy should NOT be considered market-neutral unless the two sides are actually matched.

==================================================
STEP 10 — SAFE ENTRY RULES
==================================================

Test several thresholds.

Example:

gross_edge > 0.5 cents
gross_edge > 1 cent
gross_edge > 2 cents
gross_edge > 3 cents
gross_edge > 5 cents

Then test after costs:

net_expected_edge > 0
net_expected_edge > 0.5 cents
net_expected_edge > 1 cent
net_expected_edge > 2 cents

Determine which threshold produces the best risk-adjusted result.

==================================================
STEP 11 — BACKTEST
==================================================

Do NOT randomly shuffle individual rows.

Use chronological market-level splits.

For example:

TRAIN:
8/7
8/8
8/9

VALIDATION:
8/10

TEST:
8/11
8/12

If there are insufficient markets, explain the limitation.

Avoid look-ahead bias.

For every trade, record:

market_slug
timestamp
side
requested_shares
filled_shares
entry_price
hedge_price
fees
rebates
gross_profit
net_profit
unhedged_exposure
exit/resolution_value

==================================================
STEP 12 — PERFORMANCE REPORT
==================================================

Produce:

total trades
total markets
profitable trades
losing trades
win rate

gross P&L
fees
rebates
slippage
net P&L

average profit/trade
median profit/trade
largest profit
largest loss

daily P&L

maximum drawdown

Sharpe ratio if meaningful

capital utilization

average holding time

average unhedged exposure

percentage of trades where both sides filled

percentage where only one side filled

fill rate

==================================================
STEP 13 — IMPORTANT SAFETY TEST
==================================================

The strategy should be considered unsafe if:

1. It depends on perfect simultaneous fills.
2. It becomes unprofitable after realistic fees.
3. It has significant unhedged exposure.
4. A small increase in slippage destroys profitability.
5. Results depend on a few unusual markets.
6. Results disappear on the out-of-sample dates.
7. The strategy requires impossible liquidity.
8. The backtest assumes future information.

Perform stress tests:

- 100ms execution delay
- 500ms execution delay
- 1 second delay
- 2 second delay

And:

- 0.1 cent slippage
- 0.5 cent slippage
- 1 cent slippage
- 2 cent slippage

==================================================
STEP 14 — OUTPUT FILES
==================================================

Create:

market_summary.parquet
features_1s.parquet
arbitrage_opportunities.parquet
backtest_trades.parquet
daily_pnl.parquet

Also create:

strategy_report.html

The report should contain:

1. Strategy explanation
2. Data quality
3. Number of opportunities
4. Fill rates
5. P&L
6. Drawdown
7. Risk analysis
8. Stress tests
9. Out-of-sample performance
10. Clear conclusion

==================================================
FINAL QUESTION
==================================================

Answer this precisely:

"If I start with $1,000, is this strategy realistically capable of generating positive expected return after fees, slippage, partial fills, and execution risk?"

Do NOT answer "yes" merely because the theoretical UP+DOWN price is below $1.

If the strategy is not profitable, say so.

If the result is uncertain because the dataset is too short, say so.

Most importantly:

PRIORITIZE CAPITAL PRESERVATION OVER PROFIT.

Do not optimize for maximum raw P&L.

Optimize for:

1. low maximum drawdown
2. low unhedged exposure
3. robust out-of-sample performance
4. realistic execution
5. stable returns across markets
6. positive net expectancy

Use Python + DuckDB + Pandas/Polars as appropriate.

Keep the implementation modular and reproducible.
```
