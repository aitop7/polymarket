# Step 1 - Build Base Training / Validation / Test Dataset

Build the base machine learning dataset from the raw Polymarket market data.

This step should **only merge raw data**. Do **not** perform feature engineering yet.

---

# Input Directory

```text
data/
    {date}/
        {market_id}/
            btc.parquet
            orderbooks.parquet
            trades.parquet
            meta.json
```

---

# Objective

Generate one dataset row for **every order book snapshot**.

The **order book is the master timeline** because it is sampled once per second.

Each row represents the complete market state at that timestamp.

---

# Order Book Schema

Use every column exactly as provided.

```text
timestamp               BIGINT

up_price                FLOAT
down_price              FLOAT

up_bid_price            FLOAT
up_bid_shares           UINTEGER
up_ask_price            FLOAT
up_ask_shares           UINTEGER

down_bid_price          FLOAT
down_bid_shares         UINTEGER
down_ask_price          FLOAT
down_ask_shares         UINTEGER

up_ask_0_1             UINTEGER
up_ask_1_3             UINTEGER
up_ask_3_7             UINTEGER
up_ask_7_15            UINTEGER
up_ask_15_30           UINTEGER
up_ask_30_plus         UINTEGER

up_bid_0_1             UINTEGER
up_bid_1_3             UINTEGER
up_bid_3_7             UINTEGER
up_bid_7_15            UINTEGER
up_bid_15_30           UINTEGER
up_bid_30_plus         UINTEGER

down_ask_0_1           UINTEGER
down_ask_1_3           UINTEGER
down_ask_3_7           UINTEGER
down_ask_7_15          UINTEGER
down_ask_15_30         UINTEGER
down_ask_30_plus       UINTEGER

down_bid_0_1           UINTEGER
down_bid_1_3           UINTEGER
down_bid_3_7           UINTEGER
down_bid_7_15          UINTEGER
down_bid_15_30         UINTEGER
down_bid_30_plus       UINTEGER
```

Keep every column unchanged.

---

# BTC Join

Join `btc.parquet` to each order book snapshot using an **as-of join**.

For every order book timestamp, attach the latest BTC price whose timestamp is less than or equal to the snapshot timestamp.

Append:

```text
btc_price    FLOAT
```

Do not calculate returns or rolling statistics yet.

---

# Trades Join

Do **not** join individual trade rows.

Instead, aggregate trades occurring during the **previous one-second interval** ending at the snapshot timestamp.

Append the following raw aggregated columns:

```text
trade_count

buy_count
sell_count

buy_volume
sell_volume

up_buy_volume
up_sell_volume

down_buy_volume
down_sell_volume

unique_wallets
```

Definitions:

* `trade_count`: number of trades
* `buy_count`: number of BUY trades
* `sell_count`: number of SELL trades
* `buy_volume`: total BUY shares
* `sell_volume`: total SELL shares
* `up_buy_volume`: BUY volume for UP token
* `up_sell_volume`: SELL volume for UP token
* `down_buy_volume`: BUY volume for DOWN token
* `down_sell_volume`: SELL volume for DOWN token
* `unique_wallets`: distinct wallets trading during the interval

Do not compute rolling windows in this step.

---

# Meta Information

Append:

```text
market_id

start_time

end_time

btc_open_price

btc_close_price

winner
```

`winner` is the target label:

```text
0 = DOWN
1 = UP
```

This label is identical for every snapshot within the same market.

---

# Output Row

Each output row should look like:

```text
market_id

timestamp

btc_price

(all order book columns)

trade_count
buy_count
sell_count
buy_volume
sell_volume
up_buy_volume
up_sell_volume
down_buy_volume
down_sell_volume
unique_wallets

start_time
end_time
btc_open_price
btc_close_price

winner
```

No derived features should be added.

---

# Dataset Split

Split **by market**, never by row.

A market must appear in only one dataset.

Recommended chronological split:

* Train: oldest 70% of markets
* Validation: next 15%
* Test: newest 15%

Do not randomly split rows.

---

# Output Structure

```text
training/
    train/
        {market_id}.parquet

    validation/
        {market_id}.parquet

    test/
        {market_id}.parquet
```

Each Parquet file contains every one-second snapshot for a single market.

All files must have identical schemas.

---

# Requirements

* Preserve every raw order book column exactly.
* Use the order book as the master timeline.
* Use an as-of join for BTC.
* Aggregate trades into one-second summaries.
* Do not perform feature engineering.
* Do not normalize values.
* Do not compute rolling statistics.
* Ensure no data leakage by keeping each market entirely within a single dataset split.
