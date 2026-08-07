Your Step 1 is a good **raw alignment layer**. Do not throw it away. The next steps should build on top of it.

Your pipeline should become:

```text
Raw Data
(btc/orderbook/trades/meta)
        |
        v
Step 1: Base Dataset  ✅ DONE
(1 row = 1 second market state)
        |
        v
Step 2: Feature Engineering
        |
        v
Step 3: Train LightGBM Models
        |
        v
Step 4: Backtesting
        |
        v
Step 5: Trading Decision Engine
```

---

# Step 2: Feature Engineering (next)

Now create a **feature dataset** from your base dataset.

Do not modify Step 1 output.

Create:

```text
features/
    train/
        market_id.parquet
    validation/
        market_id.parquet
    test/
        market_id.parquet
```

---

## 2.1 BTC features

From:

```text
btc_price
```

Create:

### Returns

```text
btc_return_1s

btc_return_5s

btc_return_10s

btc_return_30s

btc_return_60s
```

Formula:

```
(price_now / price_previous) - 1
```

---

### Momentum

```text
btc_momentum_10s

btc_momentum_30s
```

---

### Volatility

```text
btc_volatility_10s

btc_volatility_30s

btc_volatility_60s
```

---

### Market position

```text
btc_from_open

btc_high_30s_distance

btc_low_30s_distance
```

---

# 2.2 Order book features

You already have excellent raw data.

Now derive:

## Spread

```text
up_spread =
up_ask_price - up_bid_price

down_spread =
down_ask_price - down_bid_price
```

---

## Mid price

```text
up_mid =
(up_bid_price + up_ask_price) / 2

down_mid =
(down_bid_price + down_ask_price) / 2
```

---

## Total depth

```text
up_bid_depth =
sum(
up_bid_0_1,
up_bid_1_3,
...
)

up_ask_depth =
sum(
up_ask_0_1,
up_ask_1_3,
...
)
```

Same for DOWN.

---

## Order imbalance ⭐ important

Formula:

```
(bid_depth - ask_depth)
/
(bid_depth + ask_depth)
```

Create:

```text
up_order_imbalance

down_order_imbalance
```

Range:

```
-1 to +1
```

---

## Liquidity pressure

Examples:

```text
up_near_bid_ratio

up_near_ask_ratio

down_near_bid_ratio

down_near_ask_ratio
```

Meaning:

"How much liquidity is close to the current price?"

---

# 2.3 Trade features

Your Step 1 has only 1-second aggregates.

Now create rolling windows:

## Last 5 seconds

```text
trade_count_5s

buy_volume_5s

sell_volume_5s

up_buy_volume_5s

down_buy_volume_5s
```

---

## Last 10 seconds

Same.

---

## Last 30 seconds

Same.

---

## Trade imbalance

Example:

```
(buy_volume - sell_volume)
/
(buy_volume + sell_volume)
```

Create:

```text
trade_imbalance_5s

trade_imbalance_10s

trade_imbalance_30s
```

---

# 2.4 Prediction market features

These are likely very important.

## UP/DOWN disagreement

Current:

```
up_price + down_price
```

should approximately equal 1.

Create:

```text
market_probability_gap =
up_price + down_price - 1
```

---

## Price movement

```text
up_price_change_5s

up_price_change_10s

up_price_change_30s
```

---

## BTC vs Polymarket divergence

Example:

```
up_price_change_10s
-
normalized_btc_return_10s
```

Create:

```text
btc_market_divergence_10s
btc_market_divergence_30s
```

This is potentially one of your strongest features.

---

# 2.5 Time features

Add:

```text
elapsed_seconds

remaining_seconds

market_progress
```

Example:

```
remaining_seconds = end_time - timestamp
```

---

# Step 3: First LightGBM model

Start simple.

Target:

```text
winner
```

Input:

All engineered features.

Output:

```
P(UP)
```

Example:

```
Model output:

P(UP)=0.73
```

---

# Step 4: Evaluate correctly

Do NOT optimize:

```
accuracy
```

First check:

## Prediction quality

* Log loss
* ROC AUC
* Calibration

## Trading quality

Simulate:

```
If model P(UP) - market UP price > threshold:

BUY UP
```

Measure:

* Total PnL
* Sharpe
* Max drawdown
* Win rate

---

# Step 5: Only then add wallet intelligence

You currently removed wallet information during Step 1 aggregation.

That's okay.

Later create:

```text
wallet_features.parquet
```

from original `trades.parquet`.

Examples:

```
whale_buy_volume_10s

top_wallet_share

large_trade_count

wallet_concentration

smart_wallet_score
```

Then join those features into Step 2.

---

## Your immediate next action

I would do:

1. ✅ Finish Step 1 dataset generation.
2. Verify one market manually.
3. Build Step 2 feature engineering.
4. Train LightGBM baseline.
5. Only after seeing feature importance, decide whether wallet features are worth adding.

Do not jump to deep learning yet. Your first LightGBM model will tell you whether your current data already contains predictive signal.
