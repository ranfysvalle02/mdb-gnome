"""Experiment registration and reloading."""
import sys
import importlib
import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorDatabase

from config import EXPERIMENTS_DIR, MONGO_URI, DB_NAME
from core_deps import (
    get_current_user,
    get_current_user_or_redirect,
)
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
        
        # Validate manifest schema before registration
        try:
            is_valid, validation_error, error_paths = validate_manifest(cfg)
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

        # Enforce auth if config says "auth_required", else allow optional
        deps = []
        if cfg.get("auth_required"):
            deps = [Depends(get_current_user_or_redirect)]
        else:
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

