"""PyArrow schemas and column lists for fetch_live tables."""

from __future__ import annotations

import pyarrow as pa

BUCKET_SUFFIXES = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")


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


def _btc_depth_fields() -> list[pa.Field]:
    fields: list[pa.Field] = [pa.field("timestamp", pa.int64())]
    for i in range(1, 11):
        fields.append(pa.field(f"bid_price_{i}", pa.float32()))
        fields.append(pa.field(f"bid_qty_{i}", pa.float32()))
    for i in range(1, 11):
        fields.append(pa.field(f"ask_price_{i}", pa.float32()))
        fields.append(pa.field(f"ask_qty_{i}", pa.float32()))
    return fields


SCHEMAS: dict[str, pa.Schema] = {
    "btc_trades": pa.schema(
        [
            pa.field("timestamp", pa.int64()),
            pa.field("price", pa.float32()),
            pa.field("quantity", pa.float32()),
            pa.field("buyer_is_maker", pa.bool_()),
        ]
    ),
    "btc_depth": pa.schema(_btc_depth_fields()),
    "orderbooks": pa.schema(_orderbook_fields()),
    "trades": pa.schema(
        [
            pa.field("timestamp", pa.int64()),
            pa.field("wallet", pa.string()),
            pa.field("token", pa.bool_()),  # 0=UP, 1=DOWN
            pa.field("side", pa.bool_()),  # 0=BUY, 1=SELL
            pa.field("price", pa.float32()),
            pa.field("shares", pa.uint32()),
        ]
    ),
}

TABLE_FILES = {
    "btc_trades": "btc_trades.parquet",
    "btc_depth": "btc_depth.parquet",
    "orderbooks": "orderbooks.parquet",
    "trades": "trades.parquet",
}

ORDERBOOK_COLUMNS = [f.name for f in SCHEMAS["orderbooks"]]
BTC_DEPTH_COLUMNS = [f.name for f in SCHEMAS["btc_depth"]]
