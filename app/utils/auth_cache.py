"""
Authorization cache utilities.

Provides LRU cache with TTL for NAAN authorization checks.
This implementation is thread-safe for concurrent access.
"""

import time
import logging
import threading
from functools import lru_cache
from typing import Dict, Tuple

from dark_orchestrator import DARKOrchestrator

from app.config import get_settings

logger = logging.getLogger(__name__)


class AuthorizationCache:
    """
    LRU cache with TTL for authorization checks.
    
    Caches results of is_authorized_for_naan() calls to reduce
    blockchain queries and improve performance.
    """
    
    def __init__(self, ttl: int = 60, maxsize: int = 1000):
        """
        Initialize cache.
        
        Args:
            ttl: Time-to-live in seconds
            maxsize: Maximum number of cached entries
        """
        self.ttl = ttl
        self.maxsize = maxsize
        self._cache: Dict[Tuple[str, str], Tuple[bool, float]] = {}
        self._lock = threading.Lock()
    
    def get(self, authority_id: str, naan: str) -> Tuple[bool, bool]:
        """
        Get authorization result from cache.
        
        Thread-safe: Uses lock to prevent race conditions.
        
        Args:
            authority_id: Authority UUID
            naan: NAAN to check
        
        Returns:
            Tuple of (is_cached, is_authorized)
            - is_cached: True if found in cache and not expired
            - is_authorized: Authorization result (only valid if is_cached=True)
        """
        key = (authority_id, naan)
        
        with self._lock:
            if key in self._cache:
                result, timestamp = self._cache[key]
                
                # Check if expired
                if time.time() - timestamp < self.ttl:
                    logger.debug(f"Cache HIT: {authority_id} for NAAN {naan}")
                    return True, result
                else:
                    # Expired, remove from cache
                    logger.debug(f"Cache EXPIRED: {authority_id} for NAAN {naan}")
                    del self._cache[key]
        
        logger.debug(f"Cache MISS: {authority_id} for NAAN {naan}")
        return False, False
    
    def set(self, authority_id: str, naan: str, is_authorized: bool) -> None:
        """
        Store authorization result in cache.
        
        Thread-safe: Uses lock to prevent race conditions.
        
        Args:
            authority_id: Authority UUID
            naan: NAAN
            is_authorized: Authorization result
        """
        key = (authority_id, naan)
        
        with self._lock:
            # If cache is full, remove oldest entry (simple LRU approximation)
            if len(self._cache) >= self.maxsize:
                # Remove first (oldest) entry
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                logger.debug(f"Cache evicted oldest entry: {oldest_key}")
            
            self._cache[key] = (is_authorized, time.time())
        logger.debug(f"Cache SET: {authority_id} for NAAN {naan} = {is_authorized}")
    
    def clear(self) -> None:
        """Clear all cache entries. Thread-safe."""
        with self._lock:
            self._cache.clear()
        logger.info("Authorization cache cleared")
    
    def size(self) -> int:
        """Get current cache size. Thread-safe."""
        with self._lock:
            return len(self._cache)


# Global cache instance
_auth_cache: AuthorizationCache = None


def get_auth_cache() -> AuthorizationCache:
    """Get or create the global authorization cache."""
    global _auth_cache
    
    if _auth_cache is None:
        settings = get_settings()
        _auth_cache = AuthorizationCache(
            ttl=settings.auth_cache_ttl,
            maxsize=settings.auth_cache_maxsize,
        )
        logger.info(
            f"Authorization cache initialized (TTL={settings.auth_cache_ttl}s, "
            f"maxsize={settings.auth_cache_maxsize})"
        )
    
    return _auth_cache


def check_authorization_cached(
    orchestrator: DARKOrchestrator,
    authority_id: str,
    naan: str,
) -> bool:
    """
    Check if authority is authorized for NAAN with caching.
    
    Args:
        orchestrator: DARKOrchestrator instance
        authority_id: Authority UUID
        naan: NAAN to check
    
    Returns:
        True if authorized, False otherwise
    """
    cache = get_auth_cache()
    
    # Try cache first
    is_cached, result = cache.get(authority_id, naan)
    if is_cached:
        return result
    
    # Cache miss, query blockchain
    try:
        result = orchestrator.is_authorized_for_naan(authority_id, naan)
        cache.set(authority_id, naan, result)
        return result
    except Exception as e:
        logger.error(f"Error checking authorization: {e}")
        # Don't cache errors
        return False
