# ==============================================================================
# Defines the pluggable Authorization (AuthZ) interface for the platform.
# (FINAL DE-COUPLED VERSION)
# ==============================================================================

from __future__ import annotations # MUST be the first import for string type hints

import logging
from typing import Any, Dict, Optional, Protocol

# REMOVED: import casbin  # type: ignore (Avoids module-level dependency)

logger = logging.getLogger(__name__)


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
    """

    # Use a string literal for the type hint to prevent module-level import
    def __init__(self, enforcer: 'casbin.AsyncEnforcer'):
        """
        Initializes the adapter with a pre-configured Casbin AsyncEnforcer.
        """
        self._enforcer = enforcer
        logger.info("✔️  CasbinAdapter initialized.")

    async def check(
        self,
        subject: str,
        resource: str,
        action: str,
        user_object: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Performs the authorization check using the wrapped enforcer.
        """
        try:
            # The 'enforcer' object *is* an instance of casbin.AsyncEnforcer, 
            # so this call is valid at runtime.
            
            # 
            # --- 🚀 CRITICAL FIX ---
            # The enforce method is asynchronous and MUST be awaited.
            # Without 'await', it returns a 'truthy' coroutine object,
            # causing authorization checks to pass incorrectly.
            #
            result = await self._enforcer.enforce(subject, resource, action)
            # --- END FIX ---
            #
            
            return result
        except Exception as e:
            logger.error(
                f"Casbin 'enforce' check failed for ({subject}, {resource}, {action}): {e}",
                exc_info=True,
            )
            return False

    async def add_policy(self, *params) -> bool:
        """Helper to pass-through policy additions for seeding."""
        try:
            return await self._enforcer.add_policy(*params)
        except Exception:
            logger.warning("Failed to add policy", exc_info=True)
            return False

    async def add_role_for_user(self, *params) -> bool:
        """Helper to pass-through role additions for seeding."""
        try:
            return await self._enforcer.add_role_for_user(*params)
        except Exception:
            logger.warning("Failed to add role for user", exc_info=True)
            return False

    async def save_policy(self) -> bool:
        """Helper to pass-through policy saving for seeding."""
        try:
            return await self._enforcer.save_policy()
        except Exception:
            logger.warning("Failed to save policy", exc_info=True)
            return False