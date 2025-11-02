# ==============================================================================
# Defines the pluggable Authorization (AuthZ) interface for the platform.
# (FINAL DE-COUPLED VERSION)
# ==============================================================================

from __future__ import annotations # MUST be the first import for string type hints

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Protocol

# REMOVED: import casbin  # type: ignore (Avoids module-level dependency)

logger = logging.getLogger(__name__)

# Cache configuration
AUTHZ_CACHE_TTL = 300  # 5 minutes cache TTL for authorization results


class AuthorizationProvider(Protocol):
    """
    Defines the "contract" for any pluggable authorization provider.
    """

    async def check(
        self,
        subject: str,
        resource: str,
        action: str,
        user_object: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Checks if a subject is allowed to perform an action on a resource.
        """
        ...


class CasbinAdapter:
    """
    Implements the AuthorizationProvider interface using the Casbin AsyncEnforcer.
    Uses thread pool execution and caching to prevent blocking the event loop.
    """

    # Use a string literal for the type hint to prevent module-level import
    def __init__(self, enforcer: 'casbin.AsyncEnforcer'):
        """
        Initializes the adapter with a pre-configured Casbin AsyncEnforcer.
        """
        self._enforcer = enforcer
        # Cache for authorization results: {(subject, resource, action): (result, timestamp)}
        self._cache: Dict[tuple[str, str, str], tuple[bool, float]] = {}
        self._cache_lock = asyncio.Lock()
        logger.info("✔️  CasbinAdapter initialized with async thread pool execution and caching.")

    async def check(
        self,
        subject: str,
        resource: str,
        action: str,
        user_object: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Performs the authorization check using the wrapped enforcer.
        Uses thread pool execution to prevent blocking the event loop and caches results.
        """
        cache_key = (subject, resource, action)
        current_time = time.time()
        
        # Check cache first
        async with self._cache_lock:
            if cache_key in self._cache:
                cached_result, cached_time = self._cache[cache_key]
                # Check if cache entry is still valid
                if current_time - cached_time < AUTHZ_CACHE_TTL:
                    logger.debug(
                        f"Authorization cache HIT for ({subject}, {resource}, {action})"
                    )
                    return cached_result
                # Cache expired, remove it
                del self._cache[cache_key]
        
        try:
            # The .enforce() method on AsyncEnforcer is synchronous and blocks the event loop.
            # Run it in a thread pool to prevent blocking.
            result = await asyncio.to_thread(
                self._enforcer.enforce, subject, resource, action
            )
            
            # Cache the result
            async with self._cache_lock:
                self._cache[cache_key] = (result, current_time)
                # Limit cache size to prevent memory issues
                if len(self._cache) > 1000:
                    # Remove oldest entries (simple FIFO eviction)
                    oldest_key = min(
                        self._cache.items(),
                        key=lambda x: x[1][1]  # Compare by timestamp
                    )[0]
                    del self._cache[oldest_key]
            
            return result
        except Exception as e:
            logger.error(
                f"Casbin 'enforce' check failed for ({subject}, {resource}, {action}): {e}",
                exc_info=True,
            )
            return False
    
    async def clear_cache(self):
        """
        Clears the authorization cache. Useful when policies are updated.
        """
        async with self._cache_lock:
            self._cache.clear()
            logger.info("Authorization cache cleared.")

    async def add_policy(self, *params) -> bool:
        """Helper to pass-through policy additions for seeding."""
        try:
            result = await self._enforcer.add_policy(*params)
            # Clear cache when policies are modified
            if result:
                await self.clear_cache()
            return result
        except Exception:
            logger.warning("Failed to add policy", exc_info=True)
            return False

    async def add_role_for_user(self, *params) -> bool:
        """Helper to pass-through role additions for seeding."""
        try:
            result = await self._enforcer.add_role_for_user(*params)
            # Clear cache when roles are modified
            if result:
                await self.clear_cache()
            return result
        except Exception:
            logger.warning("Failed to add role for user", exc_info=True)
            return False

    async def save_policy(self) -> bool:
        """Helper to pass-through policy saving for seeding."""
        try:
            result = await self._enforcer.save_policy()
            # Clear cache when policies are saved
            if result:
                await self.clear_cache()
            return result
        except Exception:
            logger.warning("Failed to save policy", exc_info=True)
            return False