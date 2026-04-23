# Code Style

## Async Pattern
- All I/O (API calls, DB, WebSocket) must use `async/await`
- Never `time.sleep()` — use `await asyncio.sleep()`
- Concurrent tasks: `asyncio.gather()`, not sequential awaits
- All public async methods need type hints

## Data Classes
- Use `@dataclass` for all data transfer objects (TradeIntent, ForecastUpdate, etc.)
- Use `@dataclass(frozen=True)` for value objects crossing layer boundaries
- Never use plain `dict` for structured data between layers — use typed dataclasses

## Type Hints
- Required on all public method parameters and return types
- Use `Optional[T]` not `T | None` (Python 3.9 compatibility)
- Use `TypedDict` for dict shapes at API boundaries (Kalshi response shapes)

## Layer Import Rules
- Layers import downward only: Layer 4 → 3 → 2 → 1 → 0
- No circular imports — math layer never imports from execution layer
- Module-level singleton for shared clients: `client = KalshiClient()`

## Error Handling
- Log error before re-raising or returning `None`
- Use `src/utils/retry.py` for transient API errors (429, 5xx)
- Fail fast on auth errors (401) — don't retry
- Never silently swallow exceptions in trading-critical code paths

## Naming
- `snake_case` — variables, functions, methods
- `PascalCase` — classes
- `UPPER_SNAKE_CASE` — module-level constants
- `_prefix` — private helpers
- Math variables can use short names when they match domain notation (e.g., `ev`, `kelly_f`, `p_yes`)

## Money / Prices
- All prices stored as integer cents (never floats for money)
- Display as dollars when printing to user: `f"${cents/100:.2f}"`
- Kalshi prices: 0–100 integer cents per contract
