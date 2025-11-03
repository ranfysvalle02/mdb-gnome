"""
Role management utilities for the authorization layer.
Provides helpers for role assignment with proper validation.
"""
import logging
from typing import Dict, Any, Optional
from authz_provider import AuthorizationProvider

logger = logging.getLogger(__name__)


async def assign_role_to_user(
    authz: AuthorizationProvider,
    user_email: str,
    role: str,
    assigner_email: Optional[str] = None,
    assigner_authz: Optional[AuthorizationProvider] = None,
) -> bool:
    """
    Assign a role to a user with proper validation.
    
    For the 'developer' role, only admins can assign it.
    Other roles can be assigned by admins (validation can be extended in the future).
    
    Args:
        authz: The authorization provider instance
        user_email: Email of the user to assign the role to
        role: The role to assign (e.g., 'demo', 'developer', 'admin')
        assigner_email: Email of the user attempting to assign the role (for validation)
        assigner_authz: Optional authz provider for checking assigner permissions
    
    Returns:
        bool: True if role was successfully assigned, False otherwise
    
    Raises:
        ValueError: If validation fails (e.g., non-admin trying to assign developer role)
    """
    # Validate that only admins can assign the 'developer' role
    if role == "developer":
        if not assigner_email or not assigner_authz:
            raise ValueError(
                "Only admins can assign the 'developer' role. "
                "Please provide assigner_email and assigner_authz for validation."
            )
        
        # Check if assigner is an admin
        is_admin = await assigner_authz.check(
            subject=assigner_email,
            resource="admin_panel",
            action="access"
        )
        
        if not is_admin:
            logger.warning(
                f"Non-admin user '{assigner_email}' attempted to assign 'developer' role to '{user_email}'"
            )
            raise ValueError(
                "Only administrators can assign the 'developer' role. "
                "You do not have permission to perform this action."
            )
    
    # Check if authz provider supports role assignment
    if not hasattr(authz, "add_role_for_user"):
        logger.error("Authorization provider does not support role assignment.")
        return False
    
    # Assign the role
    try:
        result = await authz.add_role_for_user(user_email, role)
        if result and hasattr(authz, "save_policy"):
            save_op = authz.save_policy()
            if hasattr(save_op, "__await__"):
                await save_op
            logger.info(f"Successfully assigned role '{role}' to user '{user_email}'")
            return True
        else:
            logger.warning(f"Failed to assign role '{role}' to user '{user_email}'")
            return False
    except Exception as e:
        logger.error(f"Error assigning role '{role}' to user '{user_email}': {e}", exc_info=True)
        return False


async def remove_role_from_user(
    authz: AuthorizationProvider,
    user_email: str,
    role: str,
    remover_email: Optional[str] = None,
    remover_authz: Optional[AuthorizationProvider] = None,
) -> bool:
    """
    Remove a role from a user with proper validation.
    
    For the 'developer' role, only admins can remove it.
    Other roles can be removed by admins (validation can be extended in the future).
    
    Args:
        authz: The authorization provider instance
        user_email: Email of the user to remove the role from
        role: The role to remove (e.g., 'demo', 'developer', 'admin')
        remover_email: Email of the user attempting to remove the role (for validation)
        remover_authz: Optional authz provider for checking remover permissions
    
    Returns:
        bool: True if role was successfully removed, False otherwise
    
    Raises:
        ValueError: If validation fails (e.g., non-admin trying to remove developer role)
    """
    # Validate that only admins can remove the 'developer' role
    if role == "developer":
        if not remover_email or not remover_authz:
            raise ValueError(
                "Only admins can remove the 'developer' role. "
                "Please provide remover_email and remover_authz for validation."
            )
        
        # Check if remover is an admin
        is_admin = await remover_authz.check(
            subject=remover_email,
            resource="admin_panel",
            action="access"
        )
        
        if not is_admin:
            logger.warning(
                f"Non-admin user '{remover_email}' attempted to remove 'developer' role from '{user_email}'"
            )
            raise ValueError(
                "Only administrators can remove the 'developer' role. "
                "You do not have permission to perform this action."
            )
    
    # Check if authz provider supports role removal
    if not hasattr(authz, "remove_role_for_user"):
        logger.error("Authorization provider does not support role removal.")
        return False
    
    # Remove the role
    try:
        result = await authz.remove_role_for_user(user_email, role)
        if result and hasattr(authz, "save_policy"):
            save_op = authz.save_policy()
            if hasattr(save_op, "__await__"):
                await save_op
            logger.info(f"Successfully removed role '{role}' from user '{user_email}'")
            return True
        else:
            logger.warning(f"Failed to remove role '{role}' from user '{user_email}'")
            return False
    except Exception as e:
        logger.error(f"Error removing role '{role}' from user '{user_email}': {e}", exc_info=True)
        return False
