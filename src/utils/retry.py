import logging
from typing import Callable, TypeVar, Any
import asyncio

logger = logging.getLogger(__name__)

T = TypeVar('T')

async def retry_with_instant_retry(
    fn: Callable[[], Any],
    max_retries: int = 3,
    operation_name: str = "Operation"
) -> Any:
    last_error: Exception = None
    
    for attempt in range(max_retries + 1):
        try:
            result = await fn()
            if attempt > 0:
                logger.info(f"✅ {operation_name} succeeded on retry attempt {attempt}")
            return result
        except Exception as error:
            last_error = error
            
            error_msg = str(error)
            
            # Kalshi API errors or custom unauthorized check
            if "401" in error_msg or "Unauthorized" in error_msg:
                raise error
                
            if "Cannot" in error_msg or "invalid" in error_msg.lower() or "missing" in error_msg.lower():
                raise error
                
            if attempt < max_retries:
                logger.info(f"🔄 {operation_name} failed (attempt {attempt + 1}/{max_retries + 1}), retrying instantly...")
            else:
                logger.error(f"❌ {operation_name} failed after {max_retries + 1} attempts")
                
    raise last_error
