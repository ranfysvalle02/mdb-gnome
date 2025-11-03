"""Application lifespan management (startup and shutdown)."""
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from config import (
    BASE_DIR,
    MONGO_URI,
    DB_NAME,
    B2_APPLICATION_KEY_ID,
    B2_APPLICATION_KEY,
    B2_BUCKET_NAME,
    B2_ENDPOINT_URL,
    B2_ENABLED,
    B2SDK_AVAILABLE,
    RAY_AVAILABLE,
    InMemoryAccountInfo,
    B2Api,
    B2Error,
)
from authz_factory import create_authz_provider

logger = logging.getLogger(__name__)

# B2 API instances (initialized during lifespan)
b2_api = None
b2_bucket = None
_b2_init_lock = asyncio.Lock()

# Template engine (will be set during initialization)
templates = None


def init_templates():
    """Initialize Jinja2 templates with bytecode caching."""
    from config import TEMPLATES_DIR, BASE_DIR
    from fastapi.templating import Jinja2Templates
    from typing import Optional
    
    global templates
    
    if not TEMPLATES_DIR.is_dir():
        logger.critical(f"❌ CRITICAL ERROR: Templates directory not found at '{TEMPLATES_DIR}'.")
        templates = None
        return
    
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
        logger.info(f"✔️ Jinja2 templates loaded from '{TEMPLATES_DIR}' with bytecode caching enabled.")
    except Exception as e:
        logger.warning(f"⚠️ Failed to enable Jinja2 bytecode caching: {e}. Using default configuration.")
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        logger.info(f"✔️ Jinja2 templates loaded from '{TEMPLATES_DIR}'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI application lifespan (startup and shutdown)."""
    from database import ensure_db_indices, seed_admin, seed_demo_user, seed_db_from_local_files
    from experiment_routes import reload_active_experiments
    from export_helpers import cleanup_old_exports
    
    global b2_api, b2_bucket, templates
    
    logger.info("🚀 Application startup sequence initiated...")
    app.state.experiments = {}
    app.state.ray_is_available = False
    app.state.environment_mode = os.getenv("G_NOME_ENV", "production").lower()
    
    # Initialize templates
    init_templates()
    app.state.templates = templates
    
    logger.info(f"G_NOME_ENV set to: '{app.state.environment_mode}'")
    
    # Initialize B2 SDK Client
    if not B2_ENABLED:
        app.state.b2_api = None
        app.state.b2_bucket = None
        logger.warning("⚠️ B2 SDK not initialized (B2_ENABLED=False).")
    else:
        async with _b2_init_lock:
            if not b2_api:
                try:
                    account_info = InMemoryAccountInfo()
                    b2_api = B2Api(account_info)
                    b2_api.authorize_account("production", B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY)
                    b2_bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
                    
                    app.state.b2_api = b2_api
                    app.state.b2_bucket = b2_bucket
                    
                    logger.info(f"✔️ Backblaze B2 SDK initialized successfully for bucket '{B2_BUCKET_NAME}'.")
                except B2Error as e:
                    logger.error(f"❌ Failed to initialize B2 SDK during lifespan: {e}", exc_info=True)
                    app.state.b2_api = None
                    app.state.b2_bucket = None
                    b2_api = None
                    b2_bucket = None
                except Exception as e:
                    logger.error(f"❌ Unexpected error initializing B2 SDK: {e}", exc_info=True)
                    app.state.b2_api = None
                    app.state.b2_bucket = None
                    b2_api = None
                    b2_bucket = None
    
    # Ray Cluster Connection
    if RAY_AVAILABLE:
        import ray
        RAY_CONNECTION_ADDRESS = os.getenv("RAY_ADDRESS")
        job_runtime_env: Dict[str, Any] = {"working_dir": str(BASE_DIR)}
        
        if B2_ENABLED:
            job_runtime_env["env_vars"] = {
                "AWS_ENDPOINT_URL": B2_ENDPOINT_URL or "",
                "AWS_ACCESS_KEY_ID": B2_APPLICATION_KEY_ID,
                "AWS_SECRET_ACCESS_KEY": B2_APPLICATION_KEY,
                "B2_APPLICATION_KEY_ID": B2_APPLICATION_KEY_ID,
                "B2_APPLICATION_KEY": B2_APPLICATION_KEY,
                "B2_BUCKET_NAME": B2_BUCKET_NAME
            }
            logger.info("Passing B2 credentials to Ray job runtime environment.")
        
        try:
            if RAY_CONNECTION_ADDRESS:
                logger.info(f"Connecting to Ray cluster (address='{RAY_CONNECTION_ADDRESS}', namespace='modular_labs')...")
                ray.init(
                    address=RAY_CONNECTION_ADDRESS,
                    namespace="modular_labs",
                    ignore_reinit_error=True,
                    runtime_env=job_runtime_env,
                    log_to_driver=False
                )
            else:
                logger.info("Starting a new LOCAL Ray cluster instance inside the container...")
                ray.init(
                    namespace="modular_labs",
                    ignore_reinit_error=True,
                    runtime_env=job_runtime_env,
                    log_to_driver=False,
                    num_cpus=2,
                    object_store_memory=2_000_000_000
                )
            
            app.state.ray_is_available = True
        except Exception as e:
            logger.error(f"❌ Failed to initialize Ray: {e}", exc_info=True)
            app.state.ray_is_available = False
    else:
        logger.warning("⚠️ Ray library not found. Ray integration is disabled.")
    
    # MongoDB Connection
    # Get pool sizes from environment or use defaults
    main_max_pool_size = int(os.getenv("MONGO_MAIN_MAX_POOL_SIZE", "50"))
    main_min_pool_size = int(os.getenv("MONGO_MAIN_MIN_POOL_SIZE", "10"))
    
    logger.info(f"Connecting to MongoDB at '{MONGO_URI}'...")
    logger.info(
        f"MongoDB Main Pool Configuration: max_pool_size={main_max_pool_size}, "
        f"min_pool_size={main_min_pool_size} (from env or defaults)"
    )
    try:
        client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            appname="ModularLabsAPI",
            maxPoolSize=main_max_pool_size,
            minPoolSize=main_min_pool_size,
            maxIdleTimeMS=45000,
            retryWrites=True,
            retryReads=True,
        )
        await client.admin.command("ping")
        db = client[DB_NAME]
        app.state.mongo_client = client
        app.state.mongo_db = db
        logger.info(
            f"✔️ MongoDB connection successful (Database: '{DB_NAME}', "
            f"max_pool_size={main_max_pool_size}, min_pool_size={main_min_pool_size})."
        )
        logger.info(
            f"Connection pool limits configured. Monitor pool usage to prevent exhaustion. "
            f"Current limits: max={main_max_pool_size}, min={main_min_pool_size}"
        )
    except Exception as e:
        logger.critical(f"❌ CRITICAL ERROR: Failed to connect to MongoDB: {e}", exc_info=True)
        raise RuntimeError(f"MongoDB connection failed: {e}") from e
    
    # Pluggable Authorization Provider Initialization
    AUTHZ_PROVIDER = os.getenv("AUTHZ_PROVIDER", "casbin").lower()
    logger.info(f"Initializing Authorization Provider: '{AUTHZ_PROVIDER}'...")
    provider_settings = {"mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR}
    try:
        authz_instance = await create_authz_provider(AUTHZ_PROVIDER, provider_settings)
        app.state.authz_provider = authz_instance
        logger.info(f"✔️ Authorization Provider '{authz_instance.__class__.__name__}' initialized.")
    except Exception as e:
        logger.critical(f"❌ CRITICAL ERROR: Failed to initialize AuthZ provider '{AUTHZ_PROVIDER}': {e}", exc_info=True)
        raise RuntimeError(f"Authorization provider initialization failed: {e}") from e
    
    # Initial Database Setup
    try:
        await ensure_db_indices(db)
        await seed_admin(app)
        await seed_demo_user(app)
        await seed_db_from_local_files(db)
        logger.info("✔️ Essential database setup completed.")
    except Exception as e:
        logger.error(f"⚠️ Error during initial database setup: {e}", exc_info=True)
    
    # Load Initial Active Experiments
    logger.info("About to call reload_active_experiments...")
    try:
        await reload_active_experiments(app)
        logger.info("✔️ reload_active_experiments completed successfully.")
    except Exception as e:
        logger.error(f"❌ Error during initial experiment load: {e}", exc_info=True)
        import traceback
        logger.error(f"❌ Full traceback: {traceback.format_exc()}")
    
    logger.info("✔️ Application startup sequence complete. Ready to serve requests.")
    
    # Start scheduled export cleanup task (runs every 6 hours)
    async def scheduled_cleanup_loop():
        """Background task that runs export cleanup on a schedule (every 6 hours)."""
        cleanup_interval_hours = 6
        while True:
            try:
                await asyncio.sleep(cleanup_interval_hours * 3600)
                logger.info(f"Running scheduled export cleanup (every {cleanup_interval_hours} hours)...")
                await cleanup_old_exports(max_age_hours=24)
            except asyncio.CancelledError:
                logger.info("Scheduled export cleanup task cancelled during shutdown.")
                break
            except Exception as e:
                logger.error(f"Error in scheduled export cleanup loop: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    cleanup_task = asyncio.create_task(scheduled_cleanup_loop())
    app.state.export_cleanup_task = cleanup_task
    logger.info("✔️ Scheduled export cleanup task started (runs every 6 hours).")
    
    # Run initial cleanup after 5 minutes to avoid blocking startup
    async def initial_cleanup_delayed():
        await asyncio.sleep(300)
        logger.info("Running initial export cleanup after startup delay...")
        await cleanup_old_exports(max_age_hours=24)
    
    asyncio.create_task(initial_cleanup_delayed())
    
    try:
        yield  # The application runs here
    finally:
        logger.info("🛑 Application shutdown sequence initiated...")
        
        # Cancel scheduled cleanup task
        if hasattr(app.state, "export_cleanup_task"):
            cleanup_task = app.state.export_cleanup_task
            if not cleanup_task.done():
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
                logger.info("Scheduled export cleanup task cancelled.")
        
        if hasattr(app.state, "mongo_client") and app.state.mongo_client:
            logger.info("Closing MongoDB connection...")
            app.state.mongo_client.close()
        
        if hasattr(app.state, "ray_is_available") and app.state.ray_is_available:
            if RAY_AVAILABLE:
                import ray
                logger.info("Shutting down Ray connection...")
                ray.shutdown()
        
        logger.info("✔️ Application shutdown complete.")


def get_templates():
    """Get the global templates instance."""
    return templates

