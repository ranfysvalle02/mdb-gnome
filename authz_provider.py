# authz_provider.py
# ==============================================================================
# Defines the pluggable Authorization (AuthZ) interface for the platform.
#
# This file provides:
# 1. AuthorizationProvider (Protocol): An interface "contract" that any
#    AuthZ system (Casbin, Oso, OpenFGA, etc.) must implement.
# 2. CasbinAdapter: A concrete implementation of the protocol that wraps
#    the Casbin AsyncEnforcer.
# ==============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol

import casbin  # type: ignore

logger = logging.getLogger(__name__)


class AuthorizationProvider(Protocol):
    """
    Defines the "contract" for any pluggable authorization provider.

    This is a typing.Protocol, meaning classes don't need to inherit from it,
    they just need to "look like" it (i.e., implement the `check` method).
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

        Args:
            subject: The ID of the user/actor (e.g., 'user@example.com' or 'anonymous').
            resource: The object being accessed (e.g., 'admin_panel', 'experiment-slug').
            action: The action being performed (e.g., 'access', 'read', 'write').
            user_object: The full, decoded user token (for potential attribute-based checks).

        Returns:
            True if allowed, False otherwise.
        """
        ...


class CasbinAdapter:
    """
    Implements the AuthorizationProvider interface using the Casbin AsyncEnforcer.
    """

    def __init__(self, enforcer: casbin.AsyncEnforcer):
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

        *** NOTE: This is the fix. ***
        The AsyncEnforcer's .enforce() method, in this stack,
        behaves SYNCHRONOUSLY and returns a bool, not a coroutine.
        We do NOT await it. The `check` method itself remains async
        to satisfy the protocol.
        """
        try:
            # --- THIS IS THE FIX ---
            # Removed 'await' from the beginning of this line.
            result = self._enforcer.enforce(subject, resource, action)
            # --- END FIX ---
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
            # This method IS async
            return await self._enforcer.add_policy(*params)
        except Exception:
            logger.warning("Failed to add policy", exc_info=True)
            return False

    async def add_role_for_user(self, *params) -> bool:
        """Helper to pass-through role additions for seeding."""
        try:
            # This method IS async
            return await self._enforcer.add_role_for_user(*params)
        except Exception:
            logger.warning("Failed to add role for user", exc_info=True)
            return False

    async def save_policy(self) -> bool:
        """Helper to pass-through policy saving for seeding."""
        try:
            # This method IS async
            return await self._enforcer.save_policy()
        except Exception:
            logger.warning("Failed to save policy", exc_info=True)
            return False