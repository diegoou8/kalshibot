# API Conventions

## Kalshi REST API
- Demo base URL: `https://demo-api.kalshi.co/trade-api/v2`
- Live base URL: `https://trading-api.kalshi.co/trade-api/v2`
- Auth headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- Signature: RSA-PSS SHA256 over `f"{method}{path}{timestamp}"`
- Content-Type: `application/json` on all requests
- Price format: integer cents (0–100), where 100 = $1.00 max payout per contract
- Quantity: integer contracts

## Kalshi WebSocket
- Demo endpoint: `wss://demo-api.kalshi.co/trade-api/ws/v2`
- Same RSA auth in connection headers
- Subscribe message: `{"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker", "orderbook_delta"], "market_tickers": [...]}}`
- Reconnect: 5-second fixed backoff, then re-subscribe

## Open-Meteo REST API
- Forecast: `https://api.open-meteo.com/v1/forecast`
- Historical: `https://archive-api.open-meteo.com/v1/archive`
- No API key required
- Key params: `latitude`, `longitude`, `hourly=temperature_2m,precipitation`
- Timezone: always request `timezone=America/{city_tz}` for correct daily high

## General HTTP Rules
- Never create an `aiohttp.ClientSession` per request — one session per client lifetime
- Always set timeout: `aiohttp.ClientTimeout(total=30)`
- Retry on 429 and 5xx with `src/utils/retry.py` (exponential backoff)
- Fail fast on 401 (bad credentials — retry won't help)
- Log all non-200 responses: URL + status code + response body
- Never log private key material or full auth headers

## Rate Limiting
- Kalshi REST: implement token bucket if hitting 429s frequently
- Open-Meteo: no official limit but stay under 100 req/hour per IP
- WebSocket: single persistent connection, no rate limit concern
