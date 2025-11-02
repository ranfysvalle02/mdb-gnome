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
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, Mapping, List, Tuple
from datetime import datetime, timedelta
from collections import OrderedDict

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

# Attempt to import ExperimentDB for easy database access.
try:
    from experiment_db import ExperimentDB, get_experiment_db
except ImportError:
    ExperimentDB = None
    get_experiment_db = None
    logging.warning(
        "Failed to import ExperimentDB. Easy database access will not function."
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
    # Enable bytecode caching for better performance
    try:
        from jinja2 import FileSystemBytecodeCache
        cache_dir = BASE_DIR / ".jinja_cache"
        cache_dir.mkdir(exist_ok=True, mode=0o755)
        bytecode_cache = FileSystemBytecodeCache(
            directory=str(cache_dir),
            pattern="%s.cache"
        )
        templates = Jinja2Templates(
            directory=str(TEMPLATES_DIR),
            bytecode_cache=bytecode_cache
        )
        logger.info(f"✔️  Global Jinja2 templates loaded from: {TEMPLATES_DIR} with bytecode caching enabled.")
    except Exception as e:
        logger.warning(f" Failed to enable Jinja2 bytecode caching: {e}. Using default configuration.")
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


# --- Request-Scoped Config Caching ---

# Application-level cache for experiment configs with TTL and LRU eviction
# Format: cache_key -> (config_dict, timestamp)
# Uses LRU eviction with maximum size to prevent unbounded growth

# Cache configuration
CACHE_TTL_SECONDS = int(os.getenv("EXPERIMENT_CONFIG_CACHE_TTL_SECONDS", "300"))  # 5 minutes TTL
CACHE_MAX_SIZE = int(os.getenv("EXPERIMENT_CONFIG_CACHE_MAX_SIZE", "100"))  # Maximum number of cache entries

_experiment_config_app_cache: OrderedDict[str, Tuple[Dict[str, Any], datetime]] = OrderedDict()
_experiment_config_cache_lock = asyncio.Lock()  # Lock for thread-safe cache access

logger.info(
    f"Experiment config cache initialized: max_size={CACHE_MAX_SIZE}, ttl={CACHE_TTL_SECONDS}s"
)


async def get_experiment_config(
    request: Request,
    slug_id: str,
    projection: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    FastAPI Dependency: Provides request-scoped and application-level caching for experiment config.
    
    This dependency caches config at two levels:
    1. Request-scoped cache (request.state) to avoid duplicate fetches within a single request
    2. Application-level cache with TTL to reduce database queries across requests
    
    Args:
        request: The FastAPI Request object (from route parameter)
        slug_id: The experiment slug to fetch config for
        projection: Optional MongoDB projection dict (e.g., {"managed_indexes": 1})
    
    Returns:
        The experiment config dict, or None if not found.
        
    Cache Key:
        Uses `request.state.experiment_config_cache` as a dict with slug_id as key.
        Also uses application-level cache with TTL (5 minutes).
    """
    # Check if config cache exists in request.state
    if not hasattr(request.state, "experiment_config_cache"):
        request.state.experiment_config_cache = {}
    
    # Create deterministic cache key for projections
    if projection:
        # Sort projection keys for consistent cache key generation
        sorted_projection = ",".join(f"{k}:{v}" for k, v in sorted(projection.items()))
        cache_key = f"{slug_id}:{sorted_projection}"
    else:
        cache_key = slug_id
    
    # 1. Return cached config if available in request-scoped cache
    if cache_key in request.state.experiment_config_cache:
        logger.debug(
            f"get_experiment_config: Using request-scoped cached config for '{slug_id}' (projection: {projection})"
        )
        return request.state.experiment_config_cache[cache_key]
    
    # 2. Check application-level cache with TTL and LRU eviction (thread-safe)
    async with _experiment_config_cache_lock:
        if cache_key in _experiment_config_app_cache:
            config, timestamp = _experiment_config_app_cache[cache_key]
            age = datetime.now() - timestamp
            if age < timedelta(seconds=CACHE_TTL_SECONDS):
                # Cache hit - still valid, move to end (most recently used)
                # Move to end for LRU ordering
                _experiment_config_app_cache.move_to_end(cache_key)
                logger.debug(
                    f"get_experiment_config: Using application-level cached config for '{slug_id}' "
                    f"(projection: {projection}, age: {age.total_seconds():.1f}s, cache_size={len(_experiment_config_app_cache)})"
                )
                # Also cache in request-scoped cache for this request
                request.state.experiment_config_cache[cache_key] = config
                return config
            else:
                # Cache expired - remove it
                logger.debug(
                    f"get_experiment_config: Application cache expired for '{slug_id}' (age: {age.total_seconds():.1f}s)"
                )
                del _experiment_config_app_cache[cache_key]
    
    # 3. Fetch from database
    db = getattr(request.app.state, "mongo_db", None)
    if not db:
        logger.error("get_experiment_config: MongoDB connection not available.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Database connection not available.",
        )
    
    try:
        if projection:
            config = await db.experiments_config.find_one({"slug": slug_id}, projection)
        else:
            config = await db.experiments_config.find_one({"slug": slug_id})
        
        # Cache the result in both request-scoped and application-level caches
        request.state.experiment_config_cache[cache_key] = config
        
        # Update application-level cache with LRU eviction (thread-safe)
        async with _experiment_config_cache_lock:
            # Remove old entry if it exists (for updates)
            if cache_key in _experiment_config_app_cache:
                del _experiment_config_app_cache[cache_key]
            
            # Add new entry at end (most recently used)
            _experiment_config_app_cache[cache_key] = (config, datetime.now())
            
            # Evict oldest entries if cache exceeds max size (LRU eviction)
            while len(_experiment_config_app_cache) > CACHE_MAX_SIZE:
                # Remove oldest entry (first item in OrderedDict)
                oldest_key = next(iter(_experiment_config_app_cache))
                del _experiment_config_app_cache[oldest_key]
                logger.debug(
                    f"get_experiment_config: LRU cache evicted oldest entry '{oldest_key}' "
                    f"(cache_size exceeded {CACHE_MAX_SIZE})"
                )
        
        if config:
            logger.debug(
                f"get_experiment_config: Fetched and cached config for '{slug_id}' (projection: {projection})"
            )
        else:
            logger.debug(
                f"get_experiment_config: No config found for '{slug_id}' (projection: {projection})"
            )
        
        return config
    except Exception as e:
        logger.error(
            f"get_experiment_config: Error fetching config for '{slug_id}': {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching experiment config: {e}",
        )


async def get_experiment_configs_batch(
    request: Request,
    slug_ids: List[str],
    projection: Optional[Dict[str, int]] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Batch load multiple experiment configs efficiently.
    
    This function loads configs in a single query using MongoDB's $in operator,
    preventing N+1 query problems when loading multiple configs.
    
    Args:
        request: The FastAPI Request object
        slug_ids: List of experiment slugs to fetch
        projection: Optional MongoDB projection dict
    
    Returns:
        Dictionary mapping slug_id to config dict (or None if not found)
    """
    if not slug_ids:
        return {}
    
    # Check request-scoped cache first
    if not hasattr(request.state, "experiment_config_cache"):
        request.state.experiment_config_cache = {}
    
    # Create cache key for batch lookup
    sorted_slugs = sorted(slug_ids)
    batch_cache_key = f"batch:{','.join(sorted_slugs)}:{projection or 'all'}"
    
    # Check if all requested configs are in request-scoped cache
    cached_results = {}
    missing_slugs = []
    
    if projection:
        sorted_projection = ",".join(f"{k}:{v}" for k, v in sorted(projection.items()))
        for slug_id in slug_ids:
            cache_key = f"{slug_id}:{sorted_projection}"
            if cache_key in request.state.experiment_config_cache:
                cached_results[slug_id] = request.state.experiment_config_cache[cache_key]
            else:
                missing_slugs.append(slug_id)
    else:
        for slug_id in slug_ids:
            if slug_id in request.state.experiment_config_cache:
                cached_results[slug_id] = request.state.experiment_config_cache[slug_id]
            else:
                missing_slugs.append(slug_id)
    
    # If all configs are cached, return them
    if not missing_slugs:
        logger.debug(
            f"get_experiment_configs_batch: All {len(slug_ids)} configs found in request-scoped cache"
        )
        return cached_results
    
    # Fetch missing configs from database in batch
    db = getattr(request.app.state, "mongo_db", None)
    if not db:
        logger.error("get_experiment_configs_batch: MongoDB connection not available.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Database connection not available.",
        )
    
    try:
        # Use $in operator to fetch multiple configs in one query
        query = {"slug": {"$in": missing_slugs}}
        cursor = db.experiments_config.find(query, projection)
        fetched_configs = await cursor.to_list(length=len(missing_slugs))
        
        # Create mapping of slug to config
        fetched_map = {cfg.get("slug"): cfg for cfg in fetched_configs if cfg.get("slug")}
        
        # Cache results and build final result dictionary
        results = {}
        async with _experiment_config_cache_lock:
            for slug_id in slug_ids:
                if slug_id in cached_results:
                    results[slug_id] = cached_results[slug_id]
                    continue
                
                config = fetched_map.get(slug_id)
                
                # Cache in request-scoped cache
                if projection:
                    sorted_projection = ",".join(f"{k}:{v}" for k, v in sorted(projection.items()))
                    cache_key = f"{slug_id}:{sorted_projection}"
                else:
                    cache_key = slug_id
                
                request.state.experiment_config_cache[cache_key] = config
                
                # Update application-level cache
                if cache_key in _experiment_config_app_cache:
                    del _experiment_config_app_cache[cache_key]
                
                _experiment_config_app_cache[cache_key] = (config, datetime.now())
                _experiment_config_app_cache.move_to_end(cache_key)
                
                # Evict oldest entries if cache exceeds max size
                while len(_experiment_config_app_cache) > CACHE_MAX_SIZE:
                    oldest_key = next(iter(_experiment_config_app_cache))
                    del _experiment_config_app_cache[oldest_key]
                
                results[slug_id] = config
        
        logger.debug(
            f"get_experiment_configs_batch: Loaded {len(fetched_map)} configs from DB "
            f"(cached: {len(cached_results)}, fetched: {len(missing_slugs)})"
        )
        
        return results
    except Exception as e:
        logger.error(
            f"get_experiment_configs_batch: Error fetching configs: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching experiment configs: {e}",
        )


async def get_cached_experiment_config(
    request: Request,
    slug_id: str,
    projection: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    FastAPI Dependency: Provides request-scoped caching for experiment config.
    
    This dependency caches config in request.state to avoid fetching the same
    config multiple times within a single request. Use this as a FastAPI Depends()
    dependency in your route handlers.
    
    Usage:
        @admin_router.get("/some/{slug_id}")
        async def some_endpoint(
            slug_id: str,
            request: Request,
            config: Optional[Dict[str, Any]] = Depends(lambda slug_id=slug_id, req=request: get_cached_experiment_config(req, slug_id))
        ):
            ...
    
    Or more simply, call get_experiment_config() directly in your handlers:
        config = await get_experiment_config(request, slug_id)
    
    Args:
        request: The FastAPI Request object
        slug_id: The experiment slug to fetch config for (from path parameter)
        projection: Optional MongoDB projection dict
    
    Returns:
        The experiment config dict, or None if not found.
    """
    return await get_experiment_config(request, slug_id, projection)


async def _invalidate_experiment_config_cache_async(slug_id: str):
    """
    Async helper to invalidate cache entries for an experiment config.
    
    Args:
        slug_id: The experiment slug to invalidate cache for
    """
    async with _experiment_config_cache_lock:
        # Remove all cache entries for this slug_id (including projections)
        keys_to_remove = [
            key for key in _experiment_config_app_cache.keys()
            if key.startswith(f"{slug_id}:") or key == slug_id
        ]
        for key in keys_to_remove:
            _experiment_config_app_cache.pop(key, None)
        if keys_to_remove:
            logger.debug(
                f"Invalidated {len(keys_to_remove)} cache entry(ies) for '{slug_id}' "
                f"(remaining cache_size={len(_experiment_config_app_cache)})"
            )


async def get_cache_metrics() -> Dict[str, Any]:
    """
    Get metrics about the experiment config cache.
    
    Returns:
        Dictionary with cache metrics including:
        - cache_size: Current number of entries
        - max_size: Maximum cache size
        - ttl_seconds: Cache TTL in seconds
        - oldest_entry_age: Age of oldest entry in seconds (if any)
    """
    async with _experiment_config_cache_lock:
        cache_size = len(_experiment_config_app_cache)
        
        metrics = {
            "cache_size": cache_size,
            "max_size": CACHE_MAX_SIZE,
            "ttl_seconds": CACHE_TTL_SECONDS,
            "usage_percent": (cache_size / CACHE_MAX_SIZE * 100) if CACHE_MAX_SIZE > 0 else 0,
        }
        
        # Get oldest entry age if cache has entries
        if _experiment_config_app_cache:
            oldest_key = next(iter(_experiment_config_app_cache))
            _, oldest_timestamp = _experiment_config_app_cache[oldest_key]
            oldest_age = (datetime.now() - oldest_timestamp).total_seconds()
            metrics["oldest_entry_age_seconds"] = round(oldest_age, 2)
        else:
            metrics["oldest_entry_age_seconds"] = None
        
        # Warn if cache usage is high
        if metrics["usage_percent"] > 80:
            logger.warning(
                f"Experiment config cache usage is HIGH: {metrics['usage_percent']:.1f}% "
                f"({cache_size}/{CACHE_MAX_SIZE}). Consider increasing CACHE_MAX_SIZE."
            )
        
        return metrics


def invalidate_experiment_config_cache(slug_id: str):
    """
    Invalidate application-level cache for an experiment config.
    
    This should be called when an experiment config is updated to ensure
    subsequent requests get the fresh config.
    Uses the background task manager to prevent task accumulation.
    
    Args:
        slug_id: The experiment slug to invalidate cache for
    """
    # Lazy import to avoid circular dependency issues
    try:
        from main import _task_manager
        # Use background task manager (fire and forget)
        # The task manager's create_task() is async, so wrap it in a task
        async def _wrapper():
            await _task_manager.create_task(
                _invalidate_experiment_config_cache_async(slug_id),
                task_name=f"invalidate_cache_{slug_id}"
            )
        asyncio.create_task(_wrapper())
    except (ImportError, AttributeError):
        # Fallback to raw task creation if task manager not available
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_invalidate_experiment_config_cache_async(slug_id))
            else:
                loop.run_until_complete(_invalidate_experiment_config_cache_async(slug_id))
        except RuntimeError:
            # No event loop available, cache will expire naturally
            logger.debug(f"Could not invalidate cache for '{slug_id}' (no event loop or task manager)")


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