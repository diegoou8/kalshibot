import time
import asyncio

def get_current_time() -> int:
    return int(time.time())

def get_current_time_ms() -> float:
    return time.time()

async def sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)
