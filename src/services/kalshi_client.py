import aiohttp
import asyncio
import logging
import base64
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization

from ..config.env import Config

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self):
        self._private_key = None
        self._load_private_key()

    def _load_private_key(self):
        # 1. Try file (local dev)
        try:
            with open(Config.KALSHI_DEMO_KEY_FILE_PATH, "rb") as key_file:
                self._private_key = serialization.load_pem_private_key(
                    key_file.read(), password=None
                )
            return
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error("Failed to load RSA key from file %s: %s", Config.KALSHI_DEMO_KEY_FILE_PATH, e)
            return

        # 2. Fall back to env var (Azure / CI)
        pem = Config.KALSHI_DEMO_PRIVATE_KEY
        if pem:
            try:
                self._private_key = serialization.load_pem_private_key(
                    pem.encode(), password=None
                )
                logger.info("Loaded RSA private key from KALSHI_DEMO_PRIVATE_KEY env var.")
            except Exception as e:
                logger.error("Failed to load RSA key from KALSHI_DEMO_PRIVATE_KEY env var: %s", e)
        else:
            logger.error(
                "No RSA private key available: file not found at %s and "
                "KALSHI_DEMO_PRIVATE_KEY env var is not set.",
                Config.KALSHI_DEMO_KEY_FILE_PATH,
            )

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        if not self._private_key:
            return ""

        path_clean = path.split('?')[0]
        msg = timestamp + method + path_clean
        msg_bytes = msg.encode('utf-8')

        try:
            signature = self._private_key.sign(
                msg_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            logger.error(f"Signing failed: {e}")
            return ""

    def _get_headers(self, method: str, path: str) -> Dict[str, str]:
        if not self._private_key:
            return {}

        timestamp = str(int(datetime.now().timestamp() * 1000))
        signature = self._sign_request(method, path, timestamp)

        return {
            'KALSHI-ACCESS-KEY': Config.KALSHI_DEMO_KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': signature,
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
            'Content-Type': 'application/json'
        }

    async def get_balance(self) -> float:
        """Fetch balance from Demo API."""
        path_suffix = '/portfolio/balance'
        url = f"{Config.BASE_URL}{path_suffix}"
        signing_path = f"/trade-api/v2{path_suffix}"
        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("GET", signing_path)
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('balance', 0) / 100.0
                    else:
                        logger.error(f"Failed to fetch balance: {await resp.text()}")
        except Exception as e:
            logger.error(f"Exception fetching balance: {e}")
        return 0.0

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Fetch market details, normalising price fields to integer cents."""
        path_suffix = f"/markets/{ticker}"
        url = f"{Config.BASE_URL}{path_suffix}"
        signing_path = f"/trade-api/v2{path_suffix}"

        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("GET", signing_path)
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        market = data.get('market', {})
                        # API returns prices as *_dollars floats — convert to integer cents
                        # so the rest of the codebase can use yes_ask / no_ask uniformly.
                        for field in ("yes_ask", "yes_bid", "no_ask", "no_bid"):
                            dollars_key = f"{field}_dollars"
                            if field not in market and dollars_key in market:
                                val = market[dollars_key]
                                market[field] = round(float(val) * 100) if val is not None else None
                        return market
                    else:
                        logger.error(f"Failed to fetch market {ticker}: {await resp.text()}")
        except Exception as e:
            logger.error(f"Exception fetching market: {e}")
        return {}

    async def get_market_settlement(self, ticker: str) -> Dict[str, Any]:
        """Fetch settlement details for a specific market."""
        market_data = await self.get_market(ticker)
        # Kalshi includes status and result fields on settled markets
        return {
            "status": market_data.get("status"),
            "result": market_data.get("result"),
            "settle_details": market_data.get("settle_details", ""),
            "settlement_value": market_data.get("settlement_value", 0)
        }

    async def get_order_book(self, ticker: str) -> Dict[str, Any]:
        """Fetch the current orderbook for a market."""
        path_suffix = f"/markets/{ticker}/orderbook"
        url = f"{Config.BASE_URL}{path_suffix}"
        signing_path = f"/trade-api/v2{path_suffix}"
        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("GET", signing_path)
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"Orderbook fetch {resp.status} for {ticker}")
        except Exception as e:
            logger.debug(f"Orderbook fetch failed for {ticker}: {e}")
        return {}

    async def submit_order(self, ticker: str, action: str, side: str, count: int,
                           price_cents: int, client_order_id: Optional[str] = None,
                           expiration_ts: Optional[int] = None) -> Dict[str, Any]:
        """
        Submit a limit order.
        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        """
        path = '/portfolio/orders'
        signing_path = f'/trade-api/v2{path}'
        url = f"{Config.BASE_URL}{path}"
        
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())

        payload = {
            "action": action,
            "side": side,
            "count": count,
            "ticker": ticker,
            "type": "limit",
            "client_order_id": client_order_id,
        }
        if expiration_ts is not None:
            payload["expiration_ts"] = expiration_ts
        
        if side == 'yes':
            payload['yes_price'] = price_cents
        else:
            payload['no_price'] = price_cents
            
        logger.info(f"Submitting Order: {payload}")
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers('POST', signing_path)
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status not in [200, 201]:
                        text = await resp.text()
                        logger.error(f"Order Error {resp.status}: {text}")
                        return {'status': 'error', 'error': text}
                    
                    data = await resp.json()
                    order_data = data.get('order', {})
                    logger.info(f"✅ Order submitted successfully: {order_data.get('order_id')}")
                    return {'status': 'submitted', 'order': order_data}
        except Exception as e:
            logger.error(f"Exception submitting order: {e}")
            return {'status': 'error', 'error': str(e)}

    async def get_active_markets(self, limit: int = 50) -> list:
        """Fetch a list of currently open markets."""
        all_markets = []
        cursor = None
        
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    path_suffix = f"/markets?limit={limit}&status=open"
                    if cursor:
                        path_suffix += f"&cursor={cursor}"
                    url = f"{Config.BASE_URL}{path_suffix}"
                    signing_path = f"/trade-api/v2{path_suffix}"
                    
                    headers = self._get_headers("GET", signing_path)
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            all_markets.extend(data.get('markets', []))
                            cursor = data.get('cursor')
                            if not cursor:
                                break
                        else:
                            logger.error(f"Failed to fetch active markets: {await resp.text()}")
                            break
        except Exception as e:
            logger.error(f"Exception fetching active markets: {e}")
            
        return all_markets

    # ===============================
    # WEATHER SERIES-BASED INGESTION
    # ===============================

    async def _fetch_weather_series(self) -> List[str]:
        """
        Fetch series tickers in the 'Climate and Weather' category from the
        demo trade API, mirroring the logic in the main bot's KalshiClient.
        """
        path_suffix = "/series?category=Climate%20and%20Weather&limit=1000"
        signing_path = f"/trade-api/v2{path_suffix}"
        url = f"{Config.BASE_URL}{path_suffix}"

        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("GET", signing_path)
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        series = data.get("series", [])
                        tickers = [s.get("ticker") for s in series if s.get("ticker")]
                        logger.info(
                            "Fetched %d series from 'Climate and Weather' category",
                            len(tickers),
                        )
                        return tickers
                    else:
                        text = await resp.text()
                        logger.error(
                            "Failed to fetch weather series: %s %s", resp.status, text
                        )
                        return []
        except Exception as e:
            logger.error(f"Error fetching weather series: {e}")
            return []

    async def get_weather_markets(self) -> List[Dict[str, Any]]:
        """
        Return all open markets belonging to the Climate & Weather series in
        the demo environment, using the series-based scan logic.
        """
        if not self._private_key:
            logger.error("No Private Key available for weather market scan.")
            return []

        series_tickers = await self._fetch_weather_series()
        if not series_tickers:
            logger.warning("No weather series found via demo API.")
            return []

        logger.info(
            "Scanning %d weather series for open markets...", len(series_tickers)
        )

        all_markets: List[Dict[str, Any]] = []
        semaphore = asyncio.Semaphore(2)

        async def fetch_series_markets(s_ticker: str, session: aiohttp.ClientSession):
            path_suffix = f"/markets?series_ticker={s_ticker}&limit=100&status=open"
            signing_path = f"/trade-api/v2{path_suffix}"
            url = f"{Config.BASE_URL}{path_suffix}"
            async with semaphore:
                try:
                    await asyncio.sleep(0.2)
                    headers = self._get_headers("GET", signing_path)
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data.get("markets", [])
                        else:
                            text = await resp.text()
                            logger.warning(
                                "Failed to fetch markets for series %s: %s %s",
                                s_ticker,
                                resp.status,
                                text,
                            )
                            return []
                except Exception as e:
                    logger.error(f"Error scanning series {s_ticker}: {e}")
                    return []

        try:
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_series_markets(s, session) for s in series_tickers]
                results = await asyncio.gather(*tasks)

            for res in results:
                all_markets.extend(res or [])

            logger.info("Weather market scan retrieved %d markets.", len(all_markets))
            return all_markets
        except Exception as e:
            logger.error(f"Failed to fetch weather markets: {e}", exc_info=True)
            return []

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Fetch order status by exchange order_id."""
        path_suffix = f"/portfolio/orders/{order_id}"
        url = f"{Config.BASE_URL}{path_suffix}"
        signing_path = f"/trade-api/v2{path_suffix}"
        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("GET", signing_path)
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("order", {})
                    logger.warning("get_order %s returned %s", order_id, resp.status)
        except Exception as e:
            logger.debug("get_order failed: %s", e)
        return {}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Returns True on success."""
        path_suffix = f"/portfolio/orders/{order_id}/cancel"
        url = f"{Config.BASE_URL}{path_suffix}"
        signing_path = f"/trade-api/v2{path_suffix}"
        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers("DELETE", signing_path)
                async with session.delete(url, headers=headers) as resp:
                    return resp.status in (200, 204)
        except Exception as e:
            logger.debug("cancel_order failed: %s", e)
        return False

# Export an instance
client = KalshiClient()
