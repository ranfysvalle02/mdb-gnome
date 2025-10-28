# core_deps.py
# ================================================================================
# Holds shared dependencies (template engines, auth helpers, DB scoping)
# to prevent circular imports between main.py and experiment modules.
#
# This file is now decoupled from any specific AuthZ implementation.
# It relies on the `AuthorizationProvider` interface defined in `authz_provider.py`.
# ================================================================================

import os
import jwt
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Mapping, List

from fastapi import (
    Request,
    Depends,
    HTTPException,
    status,
    Cookie,
)
from fastapi.templating import Jinja2Templates

# Import the generic AuthZ interface
from authz_provider import AuthorizationProvider

# Attempt to import the ScopedMongoWrapper for database sandboxing.
try:
    from async_mongo_wrapper import ScopedMongoWrapper
except ImportError:
    ScopedMongoWrapper = None
    logging.critical(
        "❌ CRITICAL: Failed to import ScopedMongoWrapper. Database scoping will not function."
    )

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

# Load SECRET_KEY from environment; crucial for JWT security.
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
if not SECRET_KEY:
    logger.critical(
        "❌ SECURITY WARNING: FLASK_SECRET_KEY environment variable not set. Using insecure default. DO NOT USE IN PRODUCTION."
    )
    SECRET_KEY = "a_very_bad_dev_secret_key_12345"  # Insecure default

# --- Global Template Loader ---

TEMPLATES_DIR = BASE_DIR / "templates"
templates: Optional[Jinja2Templates] = None

if not TEMPLATES_DIR.is_dir():
    logger.critical(
        f"❌ Critical Error: Global 'templates' directory not found at {TEMPLATES_DIR}. Core HTML responses will fail."
    )
else:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    logger.info(f"✔️  Global Jinja2 templates loaded from: {TEMPLATES_DIR}")


def _ensure_templates() -> Jinja2Templates:
    """
    Internal dependency helper to check if the global template engine loaded.
    Raises HTTPException if templates are unavailable.
    """
    if not templates:
        logger.error("Template engine check failed: Global templates object is None.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Template engine not loaded.",
        )
    return templates


# --- Security Helpers ---


def _validate_next_url(next_url: Optional[str]) -> str:
    """
    Sanitizes a 'next' URL parameter to prevent Open Redirect vulnerabilities.
    """
    if not next_url:
        return "/"

    if next_url.startswith("/") and "//" not in next_url and ":" not in next_url:
        return next_url

    logger.warning(
        f"Blocked potentially unsafe redirect attempt. Original 'next' URL: '{next_url}'. Sanitized to '/'."
    )
    return "/"


# --- Authentication & Authorization Dependencies ---


async def get_authz_provider(request: Request) -> AuthorizationProvider:
    """
    FastAPI Dependency: Retrieves the shared, pluggable AuthZ provider
    from app.state.
    """
    # This key 'authz_provider' will be set in main.py's lifespan
    provider = getattr(request.app.state, "authz_provider", None)
    if not provider:
        logger.critical(
            "❌ get_authz_provider: AuthZ provider not found on app.state!"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Authorization engine not loaded.",
        )
    return provider


async def get_current_user(
    token: Optional[str] = Cookie(default=None),
) -> Optional[Dict[str, Any]]:
    """
    FastAPI Dependency: Decodes and validates the JWT stored in the 'token' cookie.
    """
    if not token:
        logger.debug("get_current_user: No 'token' cookie found.")
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        logger.debug(
            f"get_current_user: Token successfully decoded for user '{payload.get('email', 'N/A')}'."
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.info("get_current_user: Authentication token has expired.")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"get_current_user: Invalid JWT token presented: {e}")
        return None
    except Exception as e:
        logger.error(
            f"get_current_user: Unexpected error decoding JWT: {e}", exc_info=True
        )
        return None


async def require_admin(
    user: Optional[Mapping[str, Any]] = Depends(get_current_user),
    authz: AuthorizationProvider = Depends(get_authz_provider),
) -> Dict[str, Any]:
    """
    FastAPI Dependency: Enforces admin privileges via the pluggable AuthZ provider.
    """
    user_identifier = "anonymous"
    has_perm = False

    if user and user.get("email"):
        user_identifier = user.get("email")
        # Use the generic, async interface method
        has_perm = await authz.check(
            subject=user_identifier,
            resource="admin_panel",
            action="access",
            user_object=dict(user),  # Pass full context
        )

    if not has_perm:
        logger.warning(
            f"require_admin: Admin access DENIED for {user_identifier}. Failed provider check."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges are required to access this resource.",
        )

    logger.debug(
        f"require_admin: Admin access GRANTED for user '{user.get('email')}' via {authz.__class__.__name__}."
    )
    return dict(user)


async def get_current_user_or_redirect(
    request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    FastAPI Dependency: Enforces user authentication. Redirects to login if not authenticated.
    """
    if not user:
        try:
            login_route_name = "login_get"
            login_url = request.url_for(login_route_name)
            original_path = request.url.path
            safe_next_path = _validate_next_url(original_path)
            redirect_url = f"{login_url}?next={safe_next_path}"

            logger.info(
                f"get_current_user_or_redirect: User not authenticated. Redirecting to login. Original path: '{original_path}', Redirect URL: '{redirect_url}'"
            )
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": redirect_url},
                detail="Not authenticated. Redirecting to login.",
            )
        except Exception as e:
            logger.error(
                f"get_current_user_or_redirect: Failed to generate login redirect URL for route '{login_route_name}': {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required, but redirect failed.",
            )
    return dict(user)


def require_permission(obj: str, act: str, force_login: bool = True):
    """
    Dependency Factory: Creates a dependency checking for a specific permission
    using the pluggable AuthZ provider.

    Args:
        obj: The resource (object) to check.
        act: The action (permission) to check.
        force_login: If True (default), uses `get_current_user_or_redirect`.
                     If False, uses `get_current_user` and checks permissions
                     for 'anonymous' if no user is found.
    """

    # 1. Choose the correct user dependency based on the flag
    user_dependency = get_current_user_or_redirect if force_login else get_current_user

    async def _check_permission(
        # 2. The type hint MUST be Optional now
        user: Optional[Dict[str, Any]] = Depends(user_dependency),
        # 3. Ask for the generic INTERFACE
        authz: AuthorizationProvider = Depends(get_authz_provider),
    ) -> Optional[Dict[str, Any]]:  # 4. Return type is also Optional
        """Internal dependency function performing the AuthZ check."""

        # 5. Check for 'anonymous' if user is None
        user_email = user.get("email") if user else "anonymous"

        if not user_email:
            # This should be unreachable if 'anonymous' is the fallback
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated."
            )

        # 6. Use the generic, async interface method
        has_perm = await authz.check(
            subject=user_email,
            resource=obj,
            action=act,
            user_object=user,  # Pass full context (or None)
        )

        if not has_perm:
            logger.warning(
                f"require_permission: Access DENIED for user '{user_email}' to ('{obj}', '{act}')."
            )

            # 7. Handle the failure
            if not user:
                # User is anonymous and lacks permission.
                # 401 suggests logging in might help.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"You must be logged in with permission to '{act}' on '{obj}'.",
                )
            else:
                # User is logged in but lacks permission
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"You do not have permission to perform '{act}' on the resource '{obj}'.",
                )

        logger.debug(
            f"require_permission: Access GRANTED for user '{user_email}' to ('{obj}', '{act}')."
        )
        return user  # Returns the user dict or None

    return _check_permission


# --- Experiment-Scoped DB Dependency ---


async def get_scoped_db(request: Request) -> ScopedMongoWrapper:
    """
    FastAPI Dependency: Provides a sandboxed `ScopedMongoWrapper` for experiment routes.
    """
    if not ScopedMongoWrapper:
        logger.critical(
            "get_scoped_db: ScopedMongoWrapper class is not available (Import Error)."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Database scoping module failed to load.",
        )

    mongo_db = getattr(request.app.state, "mongo_db", None)
    if mongo_db is None:
        logger.critical(
            "get_scoped_db: Main MongoDB connection ('request.app.state.mongo_db') not found."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Database connection not available.",
        )

    try:
        slug_id: Optional[str] = getattr(request.state, "slug_id", None)
        scopes: Optional[List[str]] = getattr(request.state, "read_scopes", None)

        if slug_id is None or scopes is None:
            logger.error(
                f"get_scoped_db: Scope info ('slug_id'/'read_scopes') not found in request.state for URL '{request.url.path}'."
            )
            raise ValueError("Experiment scope information not found in request state.")

        logger.debug(
            f"get_scoped_db: Provisioning scoped DB for write scope '{slug_id}'. Read scopes: {scopes}."
        )
        return ScopedMongoWrapper(
            real_db=mongo_db, read_scopes=scopes, write_scope=slug_id
        )
    except ValueError as ve:
        logger.error(
            f"get_scoped_db: Config error provisioning scoped DB for URL '{request.url.path}': {ve}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Server configuration error: Could not determine database scope. Details: {ve}",
        )
    except Exception as e:
        logger.error(
            f"get_scoped_db: Unexpected error provisioning scoped DB for URL '{request.url.path}': {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected server error occurred while initializing database access.",
        )