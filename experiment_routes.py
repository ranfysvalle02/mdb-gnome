"""
Experiment Registration and Authentication System

This module handles experiment registration and intelligent authentication routing.

AUTHENTICATION ARCHITECTURE
============================

Experiments can use three authentication strategies:

1. NO AUTH (auth_required: false)
   - No authentication required
   - All routes are publicly accessible
   - Example: Public demos, informational sites

2. PLATFORM AUTH (auth_required: true, sub_auth not enabled)
   - Requires platform-level authentication (/auth/login)
   - Uses JWT token stored in 'token' cookie
   - Users must log in at platform level to access experiment
   - Example: Admin-only experiments

3. HYBRID AUTH (auth_required: true, sub_auth.enabled: true)
   - Supports BOTH platform and sub-authentication
   - Platform Auth: Users logged in at /auth/login (JWT token)
   - Sub-Auth: Users logged in within experiment (session cookie)
   - Automatically tries platform auth first, falls back to sub-auth
   - Example: StoryWeaver (platform users can access, experiment users can too)

4. SUB-AUTH ONLY (auth_required: false, sub_auth.enabled: true)
   - Experiment manages its own users
   - No platform authentication required
   - Example: StoreFactory (each store has its own admin users)

5. DEMO MODE (sub_auth.allow_demo_access: true)
   - Automatic demo user authentication for seamless demo experience
   - When enabled, unauthenticated users are automatically logged in as demo user
   - Requires demo users to be configured via demo_users or auto_link_platform_demo
   - Example: Can be added to any experiment for easy demo access

AUTHENTICATION FLOW FOR HYBRID AUTH
====================================

Router-level dependency (experiment_routes.py):
  1. Check platform auth (JWT token in 'token' cookie)
     - If valid, return platform user (may have linked experiment profile)
  2. Check sub-auth (experiment session cookie)
     - If valid, return experiment user
  3. Redirect to login if neither succeeds

Route-level handling (experiments/*/__init__.py):
  - Routes can use router-level dependency (automatic)
  - Routes can implement custom auth logic if needed
  - Routes should use get_user_id_for_actor() to extract appropriate user ID

EXAMPLES
========

StoreFactory (sub-auth only):
  - manifest.json: auth_required: false, sub_auth.enabled: true
  - Users log in directly within StoreFactory
  - Each store has its own admin users
  - No platform authentication required

StoryWeaver (hybrid auth):
  - manifest.json: auth_required: true, sub_auth.enabled: true, strategy: "hybrid"
  - Platform users can access (if they logged in at /auth/login)
  - Experiment users can also access (if they logged in within StoryWeaver)
  - Users can be linked (platform_user_id -> experiment_user_id)
"""
import sys
import importlib
import asyncio
import logging
from typing import Any, Dict, List, Optional, Mapping

from fastapi import APIRouter, Depends, FastAPI, Request, HTTPException
from starlette import status
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorDatabase

from config import EXPERIMENTS_DIR, MONGO_URI, DB_NAME
from core_deps import (
    get_current_user,
    get_current_user_or_redirect,
    get_authz_provider,
)
from authz_provider import AuthorizationProvider
from background_tasks import safe_background_task as _safe_background_task
from index_management import (
    normalize_json_def as _normalize_json_def,
    run_index_creation_for_collection as _run_index_creation_for_collection,
)
from manifest_schema import validate_manifest, validate_managed_indexes

logger = logging.getLogger(__name__)


async def reload_active_experiments(app: FastAPI):
    """Reload all active experiments from the database."""
    db: AsyncIOMotorDatabase = app.state.mongo_db
    logger.info(" Reloading active experiments from DB...")
    try:
        # Limit to 500 active experiments to prevent accidental large result sets
        active_cfgs = await db.experiments_config.find({"status": "active"}).limit(500).to_list(None)
        logger.info(f"Found {len(active_cfgs)} active experiment(s).")
        if len(active_cfgs) == 0:
            logger.warning(" ⚠️  No active experiments found! Check experiment status in database.")
            # Try to list all experiments to help debug (limit to 500)
            all_cfgs = await db.experiments_config.find({}).limit(500).to_list(None)
            logger.info(f"Total experiments in DB: {len(all_cfgs)}")
            for cfg in all_cfgs:
                slug = cfg.get("slug", "unknown")
                status = cfg.get("status", "no-status")
                logger.info(f"  - {slug}: status='{status}'")
        await _register_experiments(app, active_cfgs, is_reload=True)
        registered_count = len(app.state.experiments)
        logger.info(f" Experiment reload complete. {registered_count} experiment(s) registered.")
        if registered_count == 0:
            logger.warning(" ⚠️  No experiments were registered! Check logs above for errors.")
    except Exception as e:
        logger.error(f" Reload error: {e}", exc_info=True)
        app.state.experiments.clear()


async def _register_experiments(app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False):
    """Register experiments from configuration."""
    if is_reload:
        logger.debug("Clearing old experiment state...")
        app.state.experiments.clear()
        # Also clear mounted static paths cache on reload
        if hasattr(app.state, "mounted_static_paths"):
            app.state.mounted_static_paths.clear()

    if not EXPERIMENTS_DIR.is_dir():
        logger.error(f"Cannot load experiments: base '{EXPERIMENTS_DIR}' missing.")
        return

    env_mode = getattr(app.state, "environment_mode", "production")
    global_templates = getattr(app.state, "templates", None)

    for cfg in active_cfgs:
        slug = cfg.get("slug")
        if not slug:
            logger.warning("Skipped config: missing 'slug'.")
            continue
        logger.debug(f"Registering experiment '{slug}'...")
        
        # Validate manifest schema before registration (with versioning and caching support)
        try:
            # Use synchronous wrapper for backward compatibility
            # Schema versioning and caching are handled automatically
            is_valid, validation_error, error_paths = validate_manifest(cfg, use_cache=True)
            if not is_valid:
                error_path_str = f" (errors in: {', '.join(error_paths[:3])})" if error_paths else ""
                logger.error(
                    f"[{slug}] ❌ Registration BLOCKED: Manifest validation failed: {validation_error}{error_path_str}. "
                    f"Please fix the manifest.json and reload the experiment."
                )
                continue  # Skip this experiment, continue with others
        except Exception as validation_err:
            logger.error(
                f"[{slug}] ❌ Registration BLOCKED: Error during validation: {validation_err}. "
                f"Skipping this experiment.",
                exc_info=True
            )
            continue  # Skip this experiment, continue with others

        exp_path = EXPERIMENTS_DIR / slug
        runtime_s3_uri = cfg.get("runtime_s3_uri")
        local_dev_mode = (env_mode != "production")

        actor_runtime_env: Dict[str, Any] = {}
        runtime_pip_deps = cfg.get("runtime_pip_deps", [])

        if runtime_s3_uri:
            actor_runtime_env["py_modules"] = [runtime_s3_uri]
            if env_mode == "isolated" and runtime_pip_deps:
                actor_runtime_env["pip"] = runtime_pip_deps
                logger.info(f"[{slug}] ISOLATED runtime with {len(runtime_pip_deps)} pip deps.")
            elif env_mode == "isolated":
                logger.info(f"[{slug}] ISOLATED runtime (no extra deps).")
            else:
                logger.info(f"[{slug}] SHARED runtime from S3. 'pip' isolation off.")
                if runtime_pip_deps:
                    logger.warning(f"[{slug}] Has pip deps, but SHARED mode ignoring them.")
        elif not runtime_s3_uri and local_dev_mode:
            logger.info(f"[{slug}] No 'runtime_s3_uri', using local code for dev mode.")
        else:
            if not local_dev_mode:
                logger.error(f"[{slug}] Active but no 'runtime_s3_uri' in production. Skipping.")
            continue

        if not exp_path.is_dir():
            logger.warning(f"[{slug}] Skipped: local Thin Client directory not found at '{exp_path}'.")
            continue

        read_scopes = [slug if s == "self" else s for s in cfg.get("data_scope", ["self"])]
        cfg["resolved_read_scopes"] = read_scopes

        static_dir = exp_path / "static"
        if static_dir.is_dir():
            mount_path = f"/experiments/{slug}/static"
            mount_name = f"exp_{slug}_static"
            # Check if mount already exists (use cached set for O(1) lookup)
            if not hasattr(app.state, "mounted_static_paths"):
                app.state.mounted_static_paths = set()
            
            if mount_path not in app.state.mounted_static_paths:
                # Double-check by checking routes (cache might be stale after reload)
                if not any(route.path == mount_path for route in app.routes if hasattr(route, "path")):
                    try:
                        app.mount(mount_path, StaticFiles(directory=str(static_dir)), name=mount_name)
                        app.state.mounted_static_paths.add(mount_path)  # Cache the mount
                        logger.debug(f"[{slug}] Mounted static at '{mount_path}'.")
                    except Exception as e:
                        logger.error(f"[{slug}] Static mount error: {e}", exc_info=True)
                else:
                    # Route exists but not in cache - add to cache
                    app.state.mounted_static_paths.add(mount_path)
                    logger.debug(f"[{slug}] Static mount '{mount_path}' already exists (cache updated).")
            else:
                logger.debug(f"[{slug}] Static mount '{mount_path}' already exists (cached).")

        # Check for index management (import INDEX_MANAGER_AVAILABLE from config)
        from config import INDEX_MANAGER_AVAILABLE
        if INDEX_MANAGER_AVAILABLE and "managed_indexes" in cfg:
            managed_indexes: Dict[str, List[Dict]] = cfg["managed_indexes"]
            
            # Validate managed_indexes structure before processing (non-blocking)
            try:
                is_valid_indexes, index_error = validate_managed_indexes(managed_indexes)
                if not is_valid_indexes:
                    logger.warning(
                        f"[{slug}] ⚠️ Invalid 'managed_indexes' configuration: {index_error}. "
                        f"Skipping index creation for this experiment, but continuing with registration."
                    )
                    # Continue with experiment registration, just skip index creation
                else:
                    db: AsyncIOMotorDatabase = app.state.mongo_db
                    logger.debug(f"[{slug}] Managing indexes for {len(managed_indexes)} collection(s).")
                    for collection_base_name, indexes in managed_indexes.items():
                        if not collection_base_name or not isinstance(indexes, list):
                            logger.warning(f"[{slug}] Invalid 'managed_indexes' for '{collection_base_name}'.")
                            continue
                        prefixed_collection_name = f"{slug}_{collection_base_name}"
                        prefixed_defs = []
                        for idx_def in indexes:
                            idx_n = idx_def.get("name")
                            if not idx_n or not idx_def.get("type"):
                                logger.warning(f"[{slug}] Skipping malformed index def in '{collection_base_name}'.")
                                continue
                            idx_copy = idx_def.copy()
                            idx_copy["name"] = f"{slug}_{idx_n}"
                            prefixed_defs.append(idx_copy)

                        if not prefixed_defs:
                            continue

                        logger.info(f"[{slug}] Scheduling index creation for '{prefixed_collection_name}'.")
                        asyncio.create_task(
                            _safe_background_task(_run_index_creation_for_collection(db, slug, prefixed_collection_name, prefixed_defs))
                        )
            except Exception as validation_err:
                logger.warning(
                    f"[{slug}] ⚠️ Error validating 'managed_indexes' (non-critical): {validation_err}. "
                    f"Continuing with experiment registration without index validation."
                )
        elif "managed_indexes" in cfg:
            logger.warning(f"[{slug}] 'managed_indexes' present but index manager not available.")

        # Load the local APIRouter from '__init__.py'
        init_mod_name = f"experiments.{slug.replace('-', '_')}"
        try:
            if init_mod_name in sys.modules and is_reload:
                init_mod = importlib.reload(sys.modules[init_mod_name])
            else:
                init_mod = importlib.import_module(init_mod_name)

            if not hasattr(init_mod, "bp"):
                logger.error(f"[{slug}] '__init__.py' has no 'bp' (APIRouter). Skipped.")
                continue
            proxy_router = getattr(init_mod, "bp")
        except ModuleNotFoundError:
            logger.warning(f"[{slug}] No local module '{init_mod_name}' found. Skipped.")
            logger.warning(f" ENSURE 'experiments/__init__.py' and 'experiments/{slug}/__init__.py' exist.")
            continue
        except Exception as e:
            logger.error(f"[{slug}] Error loading __init__.py: {e}", exc_info=True)
            continue

        if not isinstance(proxy_router, APIRouter):
            logger.error(f"[{slug}] 'bp' is not an APIRouter. Skipped.")
            continue

        # ========================================================================
        # AUTHENTICATION STRATEGY SELECTION
        # ========================================================================
        # Based on manifest.json configuration, select the appropriate auth dependency:
        #
        # Priority Order:
        #   1. auth_policy (if defined) -> Use require_experiment_access (fine-grained RBAC)
        #   2. auth_required: true + sub_auth.enabled: true -> Use hybrid auth (platform OR sub-auth)
        #   3. auth_required: true + sub_auth not enabled -> Use platform auth only
        #   4. auth_required: false -> Use optional auth (get_current_user, returns None if not logged in)
        #
        # StoreFactory Example (sub-auth only):
        #   auth_required: false, sub_auth.enabled: true
        #   -> No router-level dependency (routes handle auth themselves)
        #
        # StoryWeaver Example (hybrid auth):
        #   auth_required: true, sub_auth.enabled: true, strategy: "hybrid"
        #   -> Uses hybrid_auth_dep (checks platform auth first, falls back to sub-auth)
        # ========================================================================
        deps = []
        # Check if auth_policy is defined (takes precedence over auth_required)
        auth_policy = cfg.get("auth_policy")
        if auth_policy:
            # Use intelligent auth dependency that handles auth_policy
            # Create a dependency function that captures the slug
            def create_auth_dep(slug_id: str):
                async def auth_dep(
                    request: Request,
                    user: Optional[Mapping[str, Any]] = Depends(get_current_user),
                    authz: AuthorizationProvider = Depends(get_authz_provider),
                ) -> Dict[str, Any]:
                    from core_deps import require_experiment_access
                    return await require_experiment_access(request, slug_id, user, authz)
                return auth_dep
            
            deps = [Depends(create_auth_dep(slug))]
        elif cfg.get("auth_required"):
            # Check if sub-auth is enabled - if so, allow either platform or sub-auth
            sub_auth = cfg.get("sub_auth", {})
            if sub_auth.get("enabled", False):
                # Hybrid auth: allow platform OR sub-auth
                # This implements the same hybrid authentication strategy that StoryWeaver uses internally.
                # It checks platform auth (JWT token) first, then falls back to sub-auth if needed.
                def create_hybrid_auth_dep(slug_id: str):
                    """
                    Creates a hybrid authentication dependency for experiments with sub-auth enabled.
                    
                    Authentication Flow:
                    1. Platform Auth (JWT token): Checks for 'token' cookie, decodes JWT
                    2. Sub-Auth (experiment session): Checks for experiment-specific session cookie
                    3. Redirect: If neither succeeds, redirects to login page
                    
                    Returns unified user dict with:
                    - user_id: Always present (platform or experiment user ID)
                    - email: User email
                    - platform_user_id: Present if authenticated via platform
                    - experiment_user_id: Present if authenticated via sub-auth or linked
                    """
                    async def hybrid_auth_dep(request: Request) -> Dict[str, Any]:
                        # Import dependencies at function level to avoid circular imports
                        from core_deps import SECRET_KEY, get_experiment_config, _validate_next_url, get_authz_provider
                        from experiment_db import get_experiment_db
                        import jwt as jwt_lib
                        
                        # STEP 0: GOD-LEVEL ACCESS - Check if user is admin
                        # Admins bypass ALL authentication checks and get immediate access
                        token = request.cookies.get("token")
                        if token:
                            try:
                                payload = jwt_lib.decode(token, SECRET_KEY, algorithms=["HS256"])
                                user_id = payload.get("user_id")
                                email = payload.get("email")
                                is_admin = payload.get("is_admin", False)
                                
                                if user_id and email:
                                    # Check admin status via authz provider FIRST (most reliable)
                                    # This works even if JWT doesn't have is_admin flag set
                                    try:
                                        authz = await get_authz_provider(request)
                                        is_admin_via_authz = await authz.check(
                                            subject=email,
                                            resource="admin_panel",
                                            action="access",
                                            user_object={"user_id": user_id, "email": email, "is_admin": is_admin}
                                        )
                                        if is_admin_via_authz:
                                            logger.info(
                                                f"Hybrid auth for {slug_id}: Admin '{email}' granted GOD-LEVEL access (via authz provider)"
                                            )
                                            return {
                                                "user_id": user_id,
                                                "email": email,
                                                "platform_user_id": user_id,
                                                "platform_auth": True,
                                                "is_admin": True,
                                                "god_access": True
                                            }
                                    except Exception as authz_err:
                                        logger.warning(f"Authz admin check failed for {slug_id} (user: {email}): {authz_err}")
                                    
                                    # Fallback: If JWT has is_admin flag set, use that
                                    if is_admin:
                                        logger.info(
                                            f"Hybrid auth for {slug_id}: Admin '{email}' granted GOD-LEVEL access (JWT is_admin=True)"
                                        )
                                        return {
                                            "user_id": user_id,
                                            "email": email,
                                            "platform_user_id": user_id,
                                            "platform_auth": True,
                                            "is_admin": True,
                                            "god_access": True
                                        }
                                    
                                    # STEP 1: Platform Authentication (for non-admin users with valid JWT)
                                    # Platform user authenticated - they can access the experiment
                                    logger.info(f"Hybrid auth for {slug_id}: Platform user '{email}' authenticated (non-admin)")
                                    
                                    config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
                                    sub_auth_cfg = config.get("sub_auth", {}) if config else {}
                                    
                                    # If experiment supports linking platform users, check for experiment profile
                                    if sub_auth_cfg.get("enabled") and sub_auth_cfg.get("link_platform_users"):
                                        # Use ExperimentDB for clean MongoDB-style API
                                        db = await get_experiment_db(request)
                                        collection_name = sub_auth_cfg.get("collection_name", "users")
                                        
                                        # Look for experiment user linked to this platform user
                                        # ExperimentDB provides MongoDB-style access via attribute access
                                        from bson.objectid import ObjectId
                                        experiment_user = await db.collection(collection_name).find_one({
                                            "platform_user_id": user_id
                                        })
                                        
                                        if experiment_user:
                                            # User has both platform and experiment profiles - return hybrid user
                                            logger.info(
                                                f"Hybrid auth for {slug_id}: Platform user '{email}' has linked experiment profile"
                                            )
                                            return {
                                                "user_id": user_id,  # Platform user ID for compatibility
                                                "email": email,
                                                "platform_user_id": user_id,
                                                "experiment_user_id": str(experiment_user["_id"]),
                                                "platform_auth": True
                                            }
                                        
                                        # No experiment profile linked - try to auto-link if this is platform demo user
                                        # This is important for StoryWeaver with auto_link_platform_demo: true
                                        from config import DEMO_EMAIL_DEFAULT
                                        auto_link_demo = sub_auth_cfg.get("auto_link_platform_demo", True)
                                        seed_strategy = sub_auth_cfg.get("demo_user_seed_strategy", "auto")
                                        
                                        if email == DEMO_EMAIL_DEFAULT and auto_link_demo and seed_strategy == "auto":
                                            logger.info(
                                                f"Hybrid auth for {slug_id}: Platform demo user '{email}' accessing - "
                                                f"attempting to auto-link experiment profile"
                                            )
                                            try:
                                                from sub_auth import ensure_demo_users_exist
                                                # MONGO_URI and DB_NAME already imported from config at top of file
                                                
                                                # Ensure demo user exists and is linked
                                                demo_users = await ensure_demo_users_exist(
                                                    db, slug_id, config, MONGO_URI, DB_NAME
                                                )
                                                
                                                if demo_users and len(demo_users) > 0:
                                                    # Find the demo user linked to this platform user
                                                    # ExperimentDB provides MongoDB-style access
                                                    linked_demo_user = await db.collection(collection_name).find_one({
                                                        "platform_user_id": user_id
                                                    })
                                                    
                                                    if linked_demo_user:
                                                        logger.info(
                                                            f"Hybrid auth for {slug_id}: Auto-linked platform demo user '{email}' "
                                                            f"to experiment profile"
                                                        )
                                                        return {
                                                            "user_id": user_id,
                                                            "email": email,
                                                            "platform_user_id": user_id,
                                                            "experiment_user_id": str(linked_demo_user["_id"]),
                                                            "platform_auth": True
                                                        }
                                            except Exception as auto_link_err:
                                                logger.warning(
                                                    f"Hybrid auth for {slug_id}: Failed to auto-link demo user '{email}': {auto_link_err}",
                                                    exc_info=True
                                                )
                                    
                                    # Platform user authenticated, but no experiment profile linked
                                    # This is valid - they can still access the experiment
                                    logger.info(
                                        f"Hybrid auth for {slug_id}: Platform user '{email}' authenticated "
                                        f"(no experiment profile linked, but platform auth is sufficient)"
                                    )
                                    return {
                                        "user_id": user_id,
                                        "email": email,
                                        "platform_user_id": user_id,
                                        "platform_auth": True
                                    }
                            except jwt_lib.ExpiredSignatureError:
                                logger.debug(f"JWT token expired for {slug_id}, trying sub-auth")
                                pass  # Token expired, fall through to sub-auth
                            except jwt_lib.InvalidTokenError:
                                logger.debug(f"Invalid JWT token for {slug_id}, trying sub-auth")
                                pass  # Invalid token, fall through to sub-auth
                            except Exception as e:
                                logger.warning(f"Platform auth check failed for {slug_id}: {e}", exc_info=True)
                        
                        # STEP 2: Try Sub-Authentication (Experiment-specific session)
                        # This is for users who logged in directly within the experiment
                        # Also supports demo mode if allow_demo_access is enabled
                        try:
                            from sub_auth import get_experiment_sub_user
                            
                            config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
                            if not config:
                                # No config means experiment doesn't exist - can't use sub-auth
                                # But we already checked admin/platform auth above, so if we get here
                                # and have a valid token, something is wrong - log it
                                if token:
                                    logger.warning(
                                        f"Hybrid auth for {slug_id}: Config not found but token exists. "
                                        f"This should not happen - admin/platform check should have passed."
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_401_UNAUTHORIZED,
                                    detail="Authentication required"
                                )
                            
                            sub_auth_cfg = config.get("sub_auth", {})
                            if not sub_auth_cfg.get("enabled", False):
                                # Sub-auth not enabled for this experiment
                                # If we have a valid token, platform auth should have worked above
                                if token:
                                    logger.warning(
                                        f"Hybrid auth for {slug_id}: Sub-auth not enabled but token exists. "
                                        f"This should not happen - platform auth should have passed."
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_401_UNAUTHORIZED,
                                    detail="Authentication required"
                                )
                            
                            # Check if demo mode is enabled for automatic demo access
                            allow_demo = sub_auth_cfg.get("allow_demo_access", False)
                            
                            # Also check for intelligent demo auto-linking (even if allow_demo_access not set)
                            # If auto_link_platform_demo is true and demo_user_seed_strategy is auto,
                            # we should try to get/create demo user as fallback
                            auto_link_demo = sub_auth_cfg.get("auto_link_platform_demo", True)
                            seed_strategy = sub_auth_cfg.get("demo_user_seed_strategy", "auto")
                            
                            # Enable demo fallback if:
                            # 1. allow_demo_access is explicitly true, OR
                            # 2. auto_link_platform_demo is true and seed_strategy is auto (intelligent demo support)
                            enable_demo_fallback = allow_demo or (auto_link_demo and seed_strategy == "auto")
                            
                            # Get experiment-specific user from session cookie
                            # If demo mode enabled, will auto-authenticate as demo user if no session exists
                            # Use ExperimentDB for clean MongoDB-style API
                            db = await get_experiment_db(request)
                            
                            logger.debug(
                                f"Hybrid auth for {slug_id}: Attempting sub-auth "
                                f"(enable_demo_fallback={enable_demo_fallback})"
                            )
                            
                            experiment_user = await get_experiment_sub_user(
                                request, slug_id, db, config,
                                allow_demo_fallback=enable_demo_fallback
                            )
                            
                            if experiment_user:
                                logger.info(
                                    f"Hybrid auth for {slug_id}: Sub-auth successful for user "
                                    f"'{experiment_user.get('email')}'"
                                )
                                # Sub-authentication successful (or demo mode auto-login)
                                user_dict = {
                                    "user_id": experiment_user.get("experiment_user_id") or str(experiment_user.get("_id")),
                                    "email": experiment_user.get("email"),
                                    "experiment_user_id": str(experiment_user.get("_id")),
                                    "platform_user_id": experiment_user.get("platform_user_id"),
                                    "sub_auth": True
                                }
                                
                                # Mark if this was demo mode auto-login
                                session_cookie_name = sub_auth_cfg.get("session_cookie_name", "experiment_session")
                                has_session_cookie = request.cookies.get(f"{session_cookie_name}_{slug_id}")
                                if enable_demo_fallback and not has_session_cookie:
                                    # SECURITY: Mark user as demo user - they cannot escape demo mode
                                    user_dict["demo_mode"] = True
                                    user_dict["is_demo"] = True
                                    logger.info(
                                        f"Hybrid auth for {slug_id}: Demo user '{experiment_user.get('email')}' "
                                        f"auto-authenticated via demo mode (SECURITY: trapped in demo role)"
                                    )
                                
                                return user_dict
                        except HTTPException:
                            # Re-raise HTTP exceptions (like 401) immediately
                            raise
                        except Exception as e:
                            logger.debug(f"Sub-auth check failed for {slug_id}: {e}")
                        
                        # STEP 3: Neither authentication method succeeded
                        # Before redirecting, do final checks:
                        # 1. Double-check if user is admin (fallback check)
                        # 2. Try demo mode one more time (fallback for intelligent demo auto-linking)
                        
                        # Final admin check (if token exists)
                        if token:
                            try:
                                payload = jwt_lib.decode(token, SECRET_KEY, algorithms=["HS256"])
                                user_id = payload.get("user_id")
                                email = payload.get("email")
                                
                                if user_id and email:
                                    # Final admin check before redirect
                                    try:
                                        authz = await get_authz_provider(request)
                                        is_admin_via_authz = await authz.check(
                                            subject=email,
                                            resource="admin_panel",
                                            action="access",
                                            user_object={"user_id": user_id, "email": email}
                                        )
                                        if is_admin_via_authz:
                                            logger.info(
                                                f"Hybrid auth for {slug_id}: Admin '{email}' granted GOD-LEVEL access "
                                                f"(final fallback check before redirect)"
                                            )
                                            return {
                                                "user_id": user_id,
                                                "email": email,
                                                "platform_user_id": user_id,
                                                "platform_auth": True,
                                                "is_admin": True,
                                                "god_access": True
                                            }
                                    except Exception as final_check_err:
                                        logger.debug(f"Final admin check failed: {final_check_err}")
                            except Exception:
                                pass  # Token decode failed, continue with checks
                        
                        # Final demo mode check (fallback for intelligent demo auto-linking)
                        # This handles cases where demo mode might have been missed in STEP 2
                        # SECURITY: Skip demo mode for auth routes - demo users cannot access login/registration
                        try:
                            # Check if this is an authentication route - SECURITY: demo users cannot access these
                            request_path = request.url.path.lower()
                            auth_route_patterns = ["/login", "/register", "/signin", "/signup", "/auth"]
                            is_auth_route = any(pattern in request_path for pattern in auth_route_patterns)
                            
                            # Only try demo mode if NOT an auth route - demo users are trapped in demo mode
                            if not is_auth_route:
                                config_final = await get_experiment_config(request, slug_id, {"sub_auth": 1})
                                if config_final:
                                    sub_auth_final = config_final.get("sub_auth", {})
                                    if sub_auth_final.get("enabled", False):
                                        auto_link_demo_final = sub_auth_final.get("auto_link_platform_demo", True)
                                        seed_strategy_final = sub_auth_final.get("demo_user_seed_strategy", "auto")
                                        allow_demo_final = sub_auth_final.get("allow_demo_access", False)
                                        
                                        # Enable demo fallback if intelligent demo auto-linking is enabled
                                        enable_demo_fallback_final = allow_demo_final or (auto_link_demo_final and seed_strategy_final == "auto")
                                        
                                        if enable_demo_fallback_final:
                                            # Use ExperimentDB for clean MongoDB-style API
                                            db_final = await get_experiment_db(request)
                                            demo_user_final = await get_experiment_sub_user(
                                                request, slug_id, db_final, config_final,
                                                allow_demo_fallback=True  # Force demo mode check
                                            )
                                            
                                            if demo_user_final:
                                                logger.info(
                                                    f"Hybrid auth for {slug_id}: Demo user '{demo_user_final.get('email')}' "
                                                    f"auto-authenticated via final fallback check"
                                                )
                                                return {
                                                    "user_id": demo_user_final.get("experiment_user_id") or str(demo_user_final.get("_id")),
                                                    "email": demo_user_final.get("email"),
                                                    "experiment_user_id": str(demo_user_final.get("_id")),
                                                    "platform_user_id": demo_user_final.get("platform_user_id"),
                                                    "sub_auth": True,
                                                    "demo_mode": True,
                                                    "is_demo": True  # Security flag: user is permanently in demo mode
                                                }
                            else:
                                logger.debug(
                                    f"Hybrid auth for {slug_id}: SECURITY - Blocking demo mode for auth route '{request_path}' "
                                    f"(demo users cannot access login/registration)"
                                )
                        except Exception as final_demo_err:
                            logger.debug(f"Final demo mode check failed for {slug_id}: {final_demo_err}")
                        
                        try:
                            login_route_name = "login_get"
                            login_url = request.url_for(login_route_name)
                            original_path = request.url.path
                            safe_next_path = _validate_next_url(original_path)
                            redirect_url = f"{login_url}?next={safe_next_path}"
                            
                            logger.warning(
                                f"Hybrid auth for {slug_id}: User not authenticated after all checks. "
                                f"Redirecting to login. Original path: '{original_path}', "
                                f"Redirect URL: '{redirect_url}'"
                            )
                            raise HTTPException(
                                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                                headers={"Location": redirect_url},
                                detail="Not authenticated. Redirecting to login.",
                            )
                        except HTTPException:
                            # Re-raise redirect HTTPException
                            raise
                        except Exception as e:
                            logger.error(
                                f"Hybrid auth for {slug_id}: Failed to generate login redirect URL: {e}",
                                exc_info=True,
                            )
                            raise HTTPException(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Authentication required.",
                            )
                    
                    return hybrid_auth_dep
                
                deps = [Depends(create_hybrid_auth_dep(slug))]
            else:
                # Backward compatibility: use simple auth_required boolean
                deps = [Depends(get_current_user_or_redirect)]
        else:
            # No auth required
            deps = [Depends(get_current_user)]

        prefix = f"/experiments/{slug}"
        try:
            app.include_router(proxy_router, prefix=prefix, tags=[f"Experiment: {slug}"], dependencies=deps)
            cfg["url"] = prefix
            app.state.experiments[slug] = cfg
            logger.info(f"[{slug}] ✅ Experiment mounted at '{prefix}'")
        except Exception as e:
            logger.error(f"[{slug}] ❌ Failed to mount experiment at '{prefix}': {e}", exc_info=True)
            continue

        # If Ray is not available, skip actor logic
        if not getattr(app.state, "ray_is_available", False):
            logger.warning(f"[{slug}] No Ray available; skipping actor.")
            continue

        # Load the local 'actor.py'
        actor_mod_name = f"experiments.{slug.replace('-', '_')}.actor"
        try:
            if actor_mod_name in sys.modules and is_reload:
                actor_mod = importlib.reload(sys.modules[actor_mod_name])
            else:
                actor_mod = importlib.import_module(actor_mod_name)

            if not hasattr(actor_mod, "ExperimentActor"):
                logger.warning(f"[{slug}] actor.py lacks 'ExperimentActor'. Skipped.")
                continue
            actor_cls = getattr(actor_mod, "ExperimentActor")
        except ModuleNotFoundError:
            logger.warning(f"[{slug}] No local actor module found ('{actor_mod_name}'). Skipped.")
            continue
        except Exception as e:
            logger.error(f"[{slug}] Error loading actor: {e}", exc_info=True)
            continue

        actor_name = f"{slug}-actor"
        try:
            import ray
            actor_handle = actor_cls.options(
                name=actor_name, namespace="modular_labs", lifetime="detached",
                get_if_exists=True, max_restarts=-1, runtime_env=actor_runtime_env
            ).remote(
                mongo_uri=MONGO_URI, db_name=DB_NAME,
                write_scope=slug, read_scopes=read_scopes
            )
            logger.info(f"[{slug}] Ray Actor '{actor_name}' started in {env_mode.upper()} mode.")
            
            # Call initialize hook if it exists (for post-startup tasks like data seeding)
            if hasattr(actor_cls, "initialize"):
                try:
                    asyncio.create_task(_safe_background_task(_call_actor_initialize(actor_handle, slug)))
                    logger.info(f"[{slug}] Scheduled post-initialization task for actor '{actor_name}'.")
                except Exception as e:
                    logger.warning(f"[{slug}] Failed to schedule actor initialization: {e}")
        except Exception as e:
            logger.error(f"[{slug}] Actor start error: {e}", exc_info=True)
            continue


async def _call_actor_initialize(actor_handle: Any, slug: str):
    """Helper to call the actor's initialize method asynchronously."""
    try:
        logger.info(f"[{slug}] Calling actor initialize hook...")
        await actor_handle.initialize.remote()
        logger.info(f"[{slug}] Actor initialize hook completed.")
    except Exception as e:
        logger.error(f"[{slug}] Actor initialize hook failed: {e}", exc_info=True)

