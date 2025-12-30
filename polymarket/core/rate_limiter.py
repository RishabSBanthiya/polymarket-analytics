"""
Shared rate limiter across all agents.

Uses a sliding window algorithm to enforce rate limits.
Can use storage backend for multi-process coordination.

Polymarket Rate Limits (per 10 seconds):
- General API: 15,000 requests
- CLOB API: 9,000 requests  
- GAMMA API: 4,000 requests
- Data API: 1,000 requests
- CLOB /book endpoint: 1,500 requests

See: https://docs.polymarket.com/quickstart/introduction/rate-limits
"""

import asyncio
import time
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Deque, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from ..trading.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class PolymarketAPI(Enum):
    """Polymarket API types with their rate limits (requests per 10 seconds)"""
    GENERAL = 15000
    CLOB = 9000
    GAMMA = 4000
    DATA = 1000
    CLOB_BOOK = 1500  # More restrictive limit for /book endpoint


# Default to CLOB API limits since that's most common for trading
DEFAULT_REQUESTS_PER_10S = PolymarketAPI.CLOB.value
DEFAULT_WINDOW_SECONDS = 10


@dataclass
class RateLimitEntry:
    """A single rate limit request entry"""
    agent_id: str
    endpoint: str
    timestamp: datetime


class InMemoryRateLimiter:
    """
    Simple in-memory rate limiter for single-process use.
    
    Uses sliding window algorithm - tracks requests in the last N seconds
    and blocks if over limit.
    
    Default: 9,000 requests per 10 seconds (CLOB API limit)
    """
    
    def __init__(
        self, 
        requests_per_window: int = DEFAULT_REQUESTS_PER_10S,
        window_seconds: int = DEFAULT_WINDOW_SECONDS
    ):
        self.limit = requests_per_window
        self.window_seconds = window_seconds
        self.requests: Deque[float] = deque()
        self._lock = asyncio.Lock()
    
    def _cleanup_old_requests(self):
        """Remove requests older than the window"""
        cutoff = time.time() - self.window_seconds
        while self.requests and self.requests[0] < cutoff:
            self.requests.popleft()
    
    async def acquire(self, agent_id: str = "", endpoint: str = "") -> bool:
        """
        Try to acquire a rate limit slot.
        
        Returns True if allowed, False if rate limit exceeded.
        """
        async with self._lock:
            self._cleanup_old_requests()
            
            if len(self.requests) >= self.limit:
                logger.warning(
                    f"Rate limit exceeded: {len(self.requests)}/{self.limit} requests in last {self.window_seconds}s"
                )
                return False
            
            self.requests.append(time.time())
            return True
    
    async def wait_and_acquire(
        self, 
        agent_id: str = "", 
        endpoint: str = "",
        timeout: float = 30.0
    ) -> bool:
        """
        Wait for a rate limit slot to become available.
        
        Returns True if acquired within timeout, False otherwise.
        """
        start = time.time()
        
        while time.time() - start < timeout:
            if await self.acquire(agent_id, endpoint):
                return True
            
            # Calculate wait time until oldest request expires
            async with self._lock:
                self._cleanup_old_requests()
                if self.requests:
                    oldest = self.requests[0]
                    wait_time = (oldest + self.window_seconds) - time.time()
                    wait_time = max(0.1, min(wait_time, 1.0))  # Clamp to reasonable range
                else:
                    wait_time = 0.1
            
            await asyncio.sleep(wait_time)
        
        logger.error(f"Rate limit timeout after {timeout}s")
        return False
    
    @property
    def current_usage(self) -> int:
        """Current number of requests in the window"""
        self._cleanup_old_requests()
        return len(self.requests)
    
    @property
    def available_slots(self) -> int:
        """Number of requests available before hitting limit"""
        return max(0, self.limit - self.current_usage)


class SharedRateLimiter:
    """
    Rate limiter shared across all agents using storage backend.
    
    Uses the storage backend for multi-process coordination.
    Falls back to in-memory limiter if no storage provided.
    
    Default: 9,000 requests per 10 seconds (CLOB API limit)
    """
    
    def __init__(
        self, 
        storage: Optional["StorageBackend"] = None,
        requests_per_window: int = DEFAULT_REQUESTS_PER_10S,
        window_seconds: int = DEFAULT_WINDOW_SECONDS
    ):
        self.storage = storage
        self.limit = requests_per_window
        self.window_seconds = window_seconds
        
        # Fallback to in-memory if no storage
        self._fallback = InMemoryRateLimiter(requests_per_window, window_seconds)
    
    async def acquire(self, agent_id: str, endpoint: str = "") -> bool:
        """
        Acquire a rate limit slot.
        
        Returns True if allowed, False if rate limit exceeded.
        """
        if self.storage is None:
            return await self._fallback.acquire(agent_id, endpoint)
        
        try:
            with self.storage.transaction() as txn:
                # Count requests in last minute
                cutoff = datetime.now() - timedelta(seconds=self.window_seconds)
                count = txn.count_requests_since(cutoff)
                
                if count >= self.limit:
                    logger.warning(
                        f"Rate limit exceeded: {count}/{self.limit} requests in last {self.window_seconds}s"
                    )
                    return False
                
                # Log this request
                txn.log_request(agent_id, endpoint, datetime.now())
                return True
                
        except Exception as e:
            logger.error(f"Error checking rate limit: {e}, falling back to in-memory")
            return await self._fallback.acquire(agent_id, endpoint)
    
    async def wait_and_acquire(
        self, 
        agent_id: str, 
        endpoint: str = "",
        timeout: float = 30.0
    ) -> bool:
        """
        Wait for a rate limit slot to become available.
        
        Returns True if acquired within timeout, False otherwise.
        """
        if self.storage is None:
            return await self._fallback.wait_and_acquire(agent_id, endpoint, timeout)
        
        start = time.time()
        
        while time.time() - start < timeout:
            if await self.acquire(agent_id, endpoint):
                return True
            await asyncio.sleep(0.5)
        
        logger.error(f"Rate limit timeout after {timeout}s")
        return False


class EndpointRateLimiter:
    """
    Rate limiter with per-endpoint limits.
    
    Useful when different API endpoints have different rate limits.
    Pre-configured with Polymarket's actual rate limits.
    """
    
    # Polymarket endpoint patterns and their limits (requests per 10 seconds)
    POLYMARKET_LIMITS = {
        # CLOB API endpoints
        "/book": PolymarketAPI.CLOB_BOOK.value,  # 1,500 per 10s
        "/order": PolymarketAPI.CLOB.value,       # 9,000 per 10s
        "/orders": PolymarketAPI.CLOB.value,
        "/trade": PolymarketAPI.CLOB.value,
        "/trades": PolymarketAPI.CLOB.value,
        
        # Data API endpoints (more restrictive)
        "/markets": PolymarketAPI.DATA.value,     # 1,000 per 10s
        "/events": PolymarketAPI.DATA.value,
        "/prices": PolymarketAPI.DATA.value,
        
        # GAMMA API endpoints
        "/gamma": PolymarketAPI.GAMMA.value,      # 4,000 per 10s
    }
    
    def __init__(
        self, 
        default_requests_per_window: int = DEFAULT_REQUESTS_PER_10S,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        use_polymarket_defaults: bool = True
    ):
        self.default_limit = default_requests_per_window
        self.window_seconds = window_seconds
        self.endpoint_limits: dict[str, int] = {}
        self.endpoint_limiters: dict[str, InMemoryRateLimiter] = {}
        self._global_limiter = InMemoryRateLimiter(default_requests_per_window, window_seconds)
        
        # Pre-configure Polymarket limits
        if use_polymarket_defaults:
            for endpoint, limit in self.POLYMARKET_LIMITS.items():
                self.set_endpoint_limit(endpoint, limit)
    
    def set_endpoint_limit(self, endpoint: str, requests_per_window: int):
        """Set a specific rate limit for an endpoint"""
        self.endpoint_limits[endpoint] = requests_per_window
        self.endpoint_limiters[endpoint] = InMemoryRateLimiter(
            requests_per_window, self.window_seconds
        )
    
    def _get_endpoint_limiter(self, endpoint: str) -> Optional[InMemoryRateLimiter]:
        """Get the limiter for an endpoint, checking for pattern matches"""
        # Exact match first
        if endpoint in self.endpoint_limiters:
            return self.endpoint_limiters[endpoint]
        
        # Check if endpoint contains any of the patterns
        for pattern, limiter in self.endpoint_limiters.items():
            if pattern in endpoint:
                return limiter
        
        return None
    
    async def acquire(self, agent_id: str, endpoint: str) -> bool:
        """
        Acquire a rate limit slot for a specific endpoint.
        
        Checks both endpoint-specific and global limits.
        """
        # Check endpoint-specific limit
        limiter = self._get_endpoint_limiter(endpoint)
        if limiter is not None:
            if not await limiter.acquire(agent_id, endpoint):
                return False
        
        # Check global limit
        return await self._global_limiter.acquire(agent_id, endpoint)
    
    async def wait_and_acquire(
        self, 
        agent_id: str, 
        endpoint: str,
        timeout: float = 30.0
    ) -> bool:
        """Wait for both endpoint and global rate limits"""
        start = time.time()
        
        while time.time() - start < timeout:
            if await self.acquire(agent_id, endpoint):
                return True
            await asyncio.sleep(0.1)  # Faster polling with higher limits
        
        return False


