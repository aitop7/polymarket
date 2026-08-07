import asyncio, time
from app.live import get_live_service

async def main():
    s = get_live_service()
    for i in range(3):
        t = time.perf_counter()
        r = await s.snapshot()
        print(i, round(time.perf_counter() - t, 3), "s", "up", r.get("up_price"))

asyncio.run(main())
