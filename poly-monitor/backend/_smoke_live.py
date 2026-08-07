import asyncio
from app.live import get_live_service

async def main():
    s = get_live_service()
    r = await asyncio.wait_for(s.snapshot(), timeout=25)
    keys = ["type", "market_id", "slug", "btc_price", "up_price", "down_price", "remaining_seconds", "error"]
    print({k: r.get(k) for k in keys})
    book = r.get("book") or {}
    up = book.get("up") or {}
    print("book_mode", book.get("mode"), "asks", len(up.get("asks") or []))

asyncio.run(main())
