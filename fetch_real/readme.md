# Python Project Specification: High-Frequency Polymarket BTC Data Collector

## Overview

Build a production-grade Python application that continuously collects high-frequency BTC market data and Polymarket market data, stores it in PostgreSQL/TimescaleDB, computes derived features, and exposes the data for quantitative research and machine learning.

---

# Technology Stack

```text
Python 3.12+

FastAPI
httpx
websockets
asyncio

SQLAlchemy 2.0
asyncpg
Alembic

TimescaleDB (PostgreSQL)

Pandas
NumPy
PyArrow

Loguru
Pydantic

Docker
pytest
```

---

# Project Structure

```text
polymarket-data/

│
├── app/
│
├── config/
│     settings.py
│
├── api/
│     polymarket.py
│     binance.py
│
├── collectors/
│     btc_collector.py
│     market_discovery.py
│     metadata.py
│     orderbook.py
│     trades.py
│     wallet.py
│     orders.py
│
├── features/
│     imbalance.py
│     momentum.py
│     volatility.py
│     whale.py
│     spread.py
│     depth.py
│
├── database/
│     models.py
│     session.py
│     repository.py
│
├── scheduler/
│     tasks.py
│
├── services/
│     synchronizer.py
│
├── utils/
│     logger.py
│     time.py
│
├── tests/
│
├── requirements.txt
│
└── main.py
```

---

# Database Tables

## btc_ticks

```sql
id

timestamp

price

size

side

best_bid

best_ask
```

---

## markets

```sql
market_id

slug

condition_id

start_time

end_time

settlement_time

opening_btc_price

closing_btc_price

winner
```

---

## orderbooks

```sql
timestamp

market_id

best_bid

best_ask

spread

book_json
```

---

## trades

```sql
timestamp

market_id

trade_id

price

size

side

wallet
```

---

## orders

```sql
timestamp

order_id

wallet

price

quantity

event_type
```

---

## wallet_positions

```sql
timestamp

wallet

market_id

yes_position

no_position

pnl
```

---

## features

```sql
timestamp

market_id

spread

imbalance

momentum

volatility

depth

whale_score

time_remaining
```

---

# Python Modules

## BTC Collector

Responsible for

```python
Connect Binance websocket

Receive BTC trades

Receive best bid/ask

Save every tick
```

---

## Market Discovery

```python
Poll Polymarket every 30 seconds

Discover

new markets

closed markets

resolved markets

Update database
```

---

## Metadata Collector

Downloads

```python
Market information

Settlement

Resolution

Opening BTC

Closing BTC
```

---

## Order Book Collector

Runs every

```python
500 milliseconds
```

Downloads

```python
Top 20 bids

Top 20 asks
```

Stores

```python
timestamp

market_id

book_json
```

---

## Trade Collector

Continuously collect

```python
Every executed trade

Trade price

Trade size

Wallet

Side
```

---

## Wallet Collector

Tracks

```python
Wallet balances

Position changes

PnL

YES shares

NO shares
```

---

## Order Event Collector

If available

Collect

```python
NEW

MODIFIED

CANCELLED

FILLED
```

---

# Feature Engineering

Executed immediately after every snapshot.

---

## Spread

```python
spread = ask - bid
```

---

## Mid Price

```python
mid = (bid + ask) / 2
```

---

## Order Imbalance

```python
imbalance = bid_volume / (bid_volume + ask_volume)
```

---

## Market Depth

Compute

```python
Top5

Top10

Top20
```

---

## Momentum

Rolling returns

```python
1 second

5 seconds

30 seconds

60 seconds
```

---

## Volatility

Rolling

```python
Standard deviation

Realized volatility

ATR
```

---

## Whale Detection

Flag

```python
Large trades

Large orders

Large cancellations
```

Threshold configurable

---

## Time Remaining

```python
settlement_time

-

current_time
```

---

# Scheduler

Runs

```python
BTC Collector
```

Continuous

```python
Trade Collector
```

Continuous

```python
Orderbook Collector
```

Every 500 ms

```python
Market Discovery
```

Every 30 seconds

```python
Metadata Refresh
```

Every 5 minutes

```python
Feature Calculation
```

Immediately after each snapshot

---

# Async Architecture

```
asyncio Event Loop
        │
        ├── BTC WebSocket
        │
        ├── Market Discovery Task
        │
        ├── Orderbook Task
        │
        ├── Trade Task
        │
        ├── Wallet Task
        │
        ├── Feature Task
        │
        └── Database Writer
```

---

# Storage

### Raw

```
Parquet
```

Daily partition

```
raw/

2026/

08/

05/
```

---

### Database

```
TimescaleDB
```

Used for

* querying
* feature engineering
* analytics

---

# Logging

Log every

```python
API request

API latency

Reconnect

Missed snapshots

Database errors

WebSocket disconnects
```

---

# Testing

Use

```
pytest
```

Test

* API clients
* Feature calculations
* Database repositories
* Data synchronization
* Collector recovery after disconnects

---

# Expected Outcome

The application will continuously collect and synchronize:

* BTC tick data (1-second or finer)
* Polymarket market metadata
* Order book snapshots (top 20 levels every 500 ms–1 s)
* Executed trades
* Wallet activity (where available)
* Market resolution data
* Derived microstructure features (spread, depth, imbalance, momentum, volatility, whale activity, and time remaining)

The resulting dataset will support historical analysis, backtesting, quantitative research, and machine learning workflows.
