"""PyArrow schemas and column lists for fetch_live tables."""

from __future__ import annotations

import pyarrow as pa

BUCKET_SUFFIXES = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")

# Binance USD-distance bands from mid (widths 0.1…51.2 cumulative).
# Edges in dollars: 0, 0.1, 0.3, 0.7, 1.5, 3.1, 6.3, 12.7, 25.5, 51.1, 102.3
# Suffix = edge*10 integers: 0_1, 1_3, …
BINANCE_BAND_WIDTHS_USD: tuple[float, ...] = (
    0.1,
    0.2,
    0.4,
    0.8,
    1.6,
    3.2,
    6.4,
    12.8,
    25.6,
    51.2,
)


def _binance_band_edges() -> list[float]:
    edges = [0.0]
    for w in BINANCE_BAND_WIDTHS_USD:
        edges.append(round(edges[-1] + float(w), 10))
    return edges


BINANCE_BAND_EDGES_USD: tuple[float, ...] = tuple(_binance_band_edges())
BINANCE_BAND_CLOSED_SUFFIXES: tuple[str, ...] = tuple(
    f"{int(round(BINANCE_BAND_EDGES_USD[i] * 10))}_"
    f"{int(round(BINANCE_BAND_EDGES_USD[i + 1] * 10))}"
    for i in range(len(BINANCE_BAND_WIDTHS_USD))
)
# Out-of-range: distance >= last edge (102.3 USD from mid)
BINANCE_BAND_PLUS_SUFFIX = (
    f"{int(round(BINANCE_BAND_EDGES_USD[-1] * 10))}_"
)
BINANCE_BAND_SUFFIXES: tuple[str, ...] = (
    *BINANCE_BAND_CLOSED_SUFFIXES,
    BINANCE_BAND_PLUS_SUFFIX,
)


def _orderbook_fields() -> list[pa.Field]:
    fields: list[pa.Field] = [
        pa.field("timestamp", pa.int64()),
        pa.field("up_price", pa.float32()),
        pa.field("down_price", pa.float32()),
        pa.field("up_bid_price", pa.float32()),
        pa.field("up_bid_shares", pa.uint32()),
        pa.field("up_ask_price", pa.float32()),
        pa.field("up_ask_shares", pa.uint32()),
        pa.field("down_bid_price", pa.float32()),
        pa.field("down_bid_shares", pa.uint32()),
        pa.field("down_ask_price", pa.float32()),
        pa.field("down_ask_shares", pa.uint32()),
    ]
    for side in ("up", "down"):
        for kind in ("ask", "bid"):
            for suffix in BUCKET_SUFFIXES:
                fields.append(pa.field(f"{side}_{kind}_{suffix}", pa.uint32()))
    return fields


def _binance_price_orderbook_fields() -> list[pa.Field]:
    """1s Binance mid + ask/bid USD-distance quantity bands."""
    fields: list[pa.Field] = [
        pa.field("timestamp", pa.int64()),
        pa.field("Binance_BTC", pa.float32()),
    ]
    for kind in ("ask", "bid"):
        for suffix in BINANCE_BAND_SUFFIXES:
            fields.append(pa.field(f"{kind}_{suffix}", pa.float32()))
    return fields


SCHEMAS: dict[str, pa.Schema] = {
    "binance_trades": pa.schema(
        [
            pa.field("timestamp", pa.int64()),
            pa.field("price", pa.float32()),
            pa.field("quantity", pa.float32()),
            pa.field("buyer_is_maker", pa.bool_()),
        ]
    ),
    "binance_price_orderbook": pa.schema(_binance_price_orderbook_fields()),
    "chainlink_price": pa.schema(
        [
            pa.field("timestamp", pa.int64()),
            pa.field("Chainlink_BTC", pa.float32()),
            pa.field("twap", pa.float32()),
        ]
    ),
    "orderbooks": pa.schema(_orderbook_fields()),
    "trades": pa.schema(
        [
            pa.field("timestamp", pa.int64()),
            pa.field("transaction_hash", pa.string()),
            pa.field("wallet", pa.string()),
            pa.field("is_up", pa.bool_()),  # true=Up, false=Down
            pa.field("is_buy", pa.bool_()),  # true=Buy, false=Sell
            pa.field("is_taker", pa.bool_()),  # true=Taker, false=Maker
            pa.field("price", pa.float64()),
            pa.field("shares", pa.float64()),
            # Distinguishes identical Orbscan fills (same wallet/price/size).
            pa.field("fill_index", pa.int32()),
        ]
    ),
}

TABLE_FILES = {
    "binance_trades": "binance_trades.parquet",
    "binance_price_orderbook": "binance_price_orderbook.parquet",
    "chainlink_price": "chainlink_price.parquet",
    "orderbooks": "orderbooks.parquet",
    "trades": "trades.parquet",
}

ORDERBOOK_COLUMNS = [f.name for f in SCHEMAS["orderbooks"]]
BINANCE_PRICE_ORDERBOOK_COLUMNS = [f.name for f in SCHEMAS["binance_price_orderbook"]]
BINANCE_BAND_COLUMNS = [
    c for c in BINANCE_PRICE_ORDERBOOK_COLUMNS if c not in {"timestamp", "Binance_BTC"}
]
