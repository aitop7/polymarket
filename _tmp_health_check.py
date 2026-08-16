from pathlib import Path
import pyarrow.parquet as pq
from datetime import datetime
from zoneinfo import ZoneInfo

et = ZoneInfo("America/New_York")
d = Path(r"E:\DataSets\poly\live\2026-08-16\3603729")
ob = pq.read_table(d / "pm_orderbooks.parquet").to_pandas()
tr = pq.read_table(d / "trades.parquet").to_pandas()
print("pm_orderbooks rows", len(ob), "cols", list(ob.columns)[:12])
# guess timestamp col
for c in ob.columns:
    if "time" in c.lower() or c in ("t","ts","timestamp"):
        print("ob time col", c, "nunique", ob[c].nunique(), "min", ob[c].min(), "max", ob[c].max())
print("trades rows", len(tr), "cols", list(tr.columns)[:15])
for c in tr.columns:
    if "time" in c.lower() or c in ("t","ts","timestamp"):
        ts = tr[c]
        print("tr time col", c, "n", len(ts), "min", ts.min(), "max", ts.max())
        # histogram by minute
        def minute(ms):
            return datetime.fromtimestamp(int(ms)/1000, tz=et).strftime("%H:%M")
        # normalize to ms
        vals = ts.astype("int64")
        if vals.max() < 10**12:
            vals = vals * 1000
        from collections import Counter
        mins = Counter(minute(int(v)) for v in vals)
        print("trades by minute ET:", dict(sorted(mins.items())))
