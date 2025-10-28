# main.py – Modular Experiment Labs (FastAPI + Ray), Full Version
# ===============================================================
#
# This FastAPI application serves as the core engine for Modular Experiment Labs.
# It manages experiment loading, routing, database interactions (via MongoDB),
# user authentication, administration, and optional Ray integration for
# distributed execution.
#
# Key Features:
# - Dynamic Experiment Loading: Loads experiments from the `experiments/` directory
#   based on configuration stored in MongoDB (`experiments_config` collection).
# - Manifest-Driven Configuration: Each experiment defines its properties (name,
#   status, auth requirements, data scoping, managed indexes) in `manifest.json`.
# - Automatic Index Management: Ensures standard and Atlas Search/Vector indexes
#   defined in the manifest exist in MongoDB, applying slug-based prefixes for
#   scoping.
# - Scoped Database Access: Provides experiments with sandboxed database access,
#   automatically filtering reads/writes by `experiment_id`.
# - Ray Integration (Optional): Can leverage Ray actors for isolated and potentially
#   distributed experiment execution.
# - Pluggable Authorization: Uses an AuthorizationProvider interface for flexible
#   permission management (default implementation likely Casbin).
# - Admin Interface: Web UI for viewing, configuring, activating/deactivating,
#   and managing experiment code (including zip uploads).
# - Authentication: Basic email/password authentication with JWT cookies.
#
# Conventions:
# - Experiment code resides in `experiments/{slug_id}/`.
# - Main experiment logic often in `experiments/{slug_id}/__init__.py`.
# - Static files in `experiments/{slug_id}/static/`.
# - Templates in `experiments/{slug_id}/templates/`.
# - Dependencies in `experiments/{slug_id}/requirements.txt`.
# - Configuration in `experiments/{slug_id}/manifest.json`.
# - Database collections and indexes managed via manifest are automatically
#   prefixed with `{slug_id}_` to prevent collisions.
# ===============================================================
from __future__ import annotations

# Standard library imports
import os
import sys
import json
import jwt
import bcrypt
import datetime
import logging
import importlib
import asyncio
import shutil
import zipfile
import io
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from contextlib import asynccontextmanager
from urllib.parse import quote

# FastAPI & Starlette imports
from fastapi import (
    FastAPI,
    APIRouter,
    Request,
    Depends,
    HTTPException,
    status,
    Form,
    Cookie,
    Body,
    Query,
    UploadFile,
    File,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    Response as FastAPIResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# Database imports (Motor for async MongoDB)
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Ray integration (optional)
try:
    import ray
    RAY_AVAILABLE = True
except ImportError as e:
    RAY_AVAILABLE = False
    logging.warning(f"⚠️ Ray integration disabled: Ray library not found ({e}).")
except Exception as e:
    RAY_AVAILABLE = False
    logging.error(f"❌ Unexpected error importing Ray: {e}", exc_info=True)

# Core application dependencies (authentication helpers, DB wrapper)
try:
    from core_deps import (
        get_current_user,
        get_current_user_or_redirect,
        require_admin,
    )
except ImportError as e:
    logging.critical(
        f"❌ CRITICAL ERROR: Failed to import core dependencies from 'core_deps.py'. Authentication and database scoping will not work. Error: {e}"
    )
    sys.exit(1) # Exit if core components are missing

# Pluggable Authorization imports
try:
    from authz_provider import AuthorizationProvider
    from authz_factory import create_authz_provider
except ImportError as e:
    logging.critical(
        f"❌ CRITICAL ERROR: Failed to import authorization components ('authz_provider.py', 'authz_factory.py'). Authorization will not work. Error: {e}"
    )
    sys.exit(1)

# Index Management import
try:
    from async_mongo_wrapper import AsyncAtlasIndexManager
    INDEX_MANAGER_AVAILABLE = True
except ImportError:
    INDEX_MANAGER_AVAILABLE = False
    logging.warning("⚠️ Index Management disabled: 'async_mongo_wrapper.py' (or dependencies) not found. Automatic index creation/updates will not occur.")

###############################################################################
# Logging Configuration
###############################################################################
# Configure logging level and format. Defaults to INFO level.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s", # Added level alignment
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("modular_labs.main") # Logger specific to this main module

###############################################################################
# Global Paths and Configuration Constants
###############################################################################
# Define base directory and key subdirectories
BASE_DIR = Path(__file__).resolve().parent
EXPERIMENTS_DIR = BASE_DIR / "experiments"
TEMPLATES_DIR = BASE_DIR / "templates"

# Feature flags and default settings from environment variables
ENABLE_REGISTRATION = os.getenv("ENABLE_REGISTRATION", "true").lower() in {"true", "1", "yes"}
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/") # Default assumes Docker Compose network
DB_NAME = "labs_db" # Default database name

# Security critical: Secret key for JWT signing. Must be overridden in production.
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a_very_insecure_default_dev_secret_123!")
if SECRET_KEY == "a_very_insecure_default_dev_secret_123!":
    logger.critical(
        "❌ SECURITY WARNING: Using default SECRET_KEY. This is highly insecure. "
        "Set the FLASK_SECRET_KEY environment variable to a strong, random value in production."
    )

# Default administrator credentials (used for seeding if no admin exists)
ADMIN_EMAIL_DEFAULT = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD_DEFAULT = os.getenv("ADMIN_PASSWORD", "password123")
if ADMIN_PASSWORD_DEFAULT == "password123":
    logger.warning("⚠️ Using default admin password. Change this or ensure DB persistence.")

###############################################################################
# Jinja2 Template Engine Setup
###############################################################################
# Initialize Jinja2 templates if the directory exists.
if not TEMPLATES_DIR.is_dir():
    logger.critical(f"❌ CRITICAL ERROR: Templates directory not found at '{TEMPLATES_DIR}'. UI rendering will fail.")
    templates: Optional[Jinja2Templates] = None
else:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    logger.info(f"✔️ Jinja2 templates loaded from '{TEMPLATES_DIR}'")


###############################################################################
# FastAPI Application Lifespan (Startup & Shutdown)
###############################################################################
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages application startup and shutdown events.
    - Initializes state variables.
    - Connects to Ray (if available).
    - Connects to MongoDB.
    - Initializes the Authorization provider.
    - Ensures essential DB setup (indices, admin user).
    - Loads initial active experiments.
    - Cleans up connections on shutdown.
    """
    logger.info("🚀 Application startup sequence initiated...")
    # Initialize application state storage
    app.state.experiments = {} # Holds loaded experiment configurations
    app.state.ray_is_available = False # Flag for Ray connection status
    app.state.environment_mode = os.getenv("G_NOME_ENV", "production").lower() # Store env mode ('production', 'isolated', etc.)

    # --- Ray Cluster Connection (Optional) ---
    if RAY_AVAILABLE:
        job_runtime_env: Dict[str, Any] = {} # Default Ray job runtime environment
        # In 'isolated' mode, set working_dir for better dependency management
        if app.state.environment_mode == "isolated":
            job_runtime_env = {"working_dir": str(BASE_DIR)}
            logger.info(f"Ray job-level isolation enabled (working_dir='{BASE_DIR}')")
        try:
            # Attempt to connect to the Ray cluster. 'auto' tries common addresses.
            # Use a namespace to avoid collisions if Ray cluster is shared.
            logger.info("Connecting to Ray cluster (address='auto', namespace='modular_labs')...")
            ray.init(
                address="auto",
                namespace="modular_labs",
                ignore_reinit_error=True, # Allow re-initialization if already connected (e.g., during dev reload)
                runtime_env=job_runtime_env,
                log_to_driver=False, # Prevent Ray logs from flooding the main app log excessively
            )
            app.state.ray_is_available = True
            logger.info("✔️ Ray connection successful.")
            # Log the dashboard URL if available
            try:
                dash_url = ray.get_dashboard_url()
                if dash_url: logger.info(f"Ray Dashboard URL: {dash_url}")
            except Exception: pass # Ignore if dashboard URL retrieval fails
        except Exception as e:
            # Log connection failure but continue running without Ray features
            logger.exception(f"⚠️ Ray connection failed: {e}. Ray features will be disabled.")
            app.state.ray_is_available = False
    else:
        logger.warning("Ray library not found. Ray integration is disabled.")

    # --- MongoDB Connection ---
    logger.info(f"Connecting to MongoDB at '{MONGO_URI}'...")
    try:
        # Establish connection using Motor (async driver)
        # Use a short server selection timeout to fail fast if DB is unavailable.
        # Set appname for easier identification in MongoDB logs/monitoring.
        client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            appname="ModularLabsAPI"
        )
        # Verify connection by pinging the admin database
        await client.admin.command("ping")
        db = client[DB_NAME] # Get database object
        # Store client and db object in app state for access in routes/dependencies
        app.state.mongo_client = client
        app.state.mongo_db = db
        logger.info(f"✔️ MongoDB connection successful (Database: '{DB_NAME}').")
    except Exception as e:
        logger.critical(f"❌ CRITICAL ERROR: Failed to connect to MongoDB at '{MONGO_URI}'. Error: {e}", exc_info=True)
        # If DB connection fails, the application cannot function. Re-raise to halt startup.
        raise RuntimeError(f"MongoDB connection failed: {e}") from e

    # --- Pluggable Authorization Provider Initialization ---
    AUTHZ_PROVIDER = os.getenv("AUTHZ_PROVIDER", "casbin").lower() # Get provider type from env (default: casbin)
    authz_instance: Optional[AuthorizationProvider] = None
    logger.info(f"Initializing Authorization Provider: '{AUTHZ_PROVIDER}'...")
    # Prepare settings to pass to the provider factory
    provider_settings = { "mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR, }
    try:
        # Use the factory function to create the specific provider instance
        authz_instance = await create_authz_provider(AUTHZ_PROVIDER, provider_settings)
        # Store the initialized provider in app state
        app.state.authz_provider = authz_instance
        logger.info(f"✔️ Authorization Provider '{authz_instance.__class__.__name__}' initialized successfully.")
    except (NotImplementedError, RuntimeError, FileNotFoundError) as e:
        # Log critical error and halt startup if authz provider fails
        logger.critical(f"❌ CRITICAL ERROR: Failed to initialize AuthZ provider '{AUTHZ_PROVIDER}': {e}", exc_info=True)
        raise RuntimeError(f"Authorization provider initialization failed: {e}") from e

    # --- Initial Database Setup ---
    try:
        # Ensure essential indexes exist (e.g., unique email for users)
        await _ensure_db_indices(db)
        # Seed the first administrator user if none exists
        await _seed_admin(app)
        logger.info("✔️ Essential database setup completed (indexes checked, admin seeded if necessary).")
    except Exception as e:
        logger.error(f"⚠️ Error during initial database setup: {e}", exc_info=True)
        # Log error but allow startup to continue if seeding/indexing fails (might be recoverable)

    # --- Load Initial Active Experiments ---
    try:
        # Perform the first load of experiments marked as 'active'
        await reload_active_experiments(app)
    except Exception as e:
        logger.error(f"⚠️ Error during initial experiment load: {e}", exc_info=True)
        # Log error but allow startup to continue

    logger.info("✅ Application startup sequence complete. Ready to serve requests.")

    # --- Yield control to the application ---
    try:
        yield # The application runs here
    finally:
        # --- Shutdown Sequence ---
        logger.info("🔌 Application shutdown sequence initiated...")
        # Close MongoDB connection
        if hasattr(app.state, "mongo_client") and app.state.mongo_client:
            logger.info("Closing MongoDB connection...")
            app.state.mongo_client.close()
            logger.info("✔️ MongoDB connection closed.")
        # Shutdown Ray connection
        if hasattr(app.state, "ray_is_available") and app.state.ray_is_available:
            logger.info("Shutting down Ray connection...")
            ray.shutdown()
            logger.info("✔️ Ray connection shut down.")
        logger.info("🏁 Application shutdown complete.")


###############################################################################
# FastAPI Application Instance
###############################################################################
# Create the main FastAPI application instance, configuring lifespan and docs URLs.
app = FastAPI(
    title="Modular Experiment Labs",
    version="1.7.0", # Increment version for new features
    docs_url="/api/docs", # OpenAPI/Swagger UI endpoint
    redoc_url="/api/redoc", # ReDoc UI endpoint
    openapi_url="/api/openapi.json", # OpenAPI schema endpoint
    lifespan=lifespan, # Register the lifespan context manager
)
# Ensure trailing slashes are handled consistently (redirects if necessary)
app.router.redirect_slashes = True


###############################################################################
# Middleware Configuration
###############################################################################
class ExperimentScopeMiddleware(BaseHTTPMiddleware):
    """
    Middleware to determine the current experiment context based on the URL path.
    It injects `slug_id` and `read_scopes` into `request.state` for requests
    under `/experiments/{slug_id}/...`. This information is used by dependencies
    like `get_scoped_db`.
    """
    async def dispatch(self, request: Request, call_next: ASGIApp):
        # Initialize state variables for every request
        request.state.slug_id = None
        request.state.read_scopes = None
        path = request.url.path

        # Check if the path matches the experiment route pattern
        if path.startswith("/experiments/"):
            parts = path.strip("/").split("/")
            # Ensure path has at least /experiments/{slug}
            if len(parts) >= 2:
                slug = parts[1]
                # Look up the loaded configuration for this slug
                exp_cfg = getattr(request.app.state, "experiments", {}).get(slug)
                if exp_cfg:
                    # If config found, store slug and resolved read scopes in request state
                    request.state.slug_id = slug
                    request.state.read_scopes = exp_cfg.get("resolved_read_scopes", [slug]) # Default to self if missing
                    logger.debug(f"Scope set for request {request.url}: slug='{slug}', scopes={request.state.read_scopes}")

        # Proceed to the next middleware or route handler
        response = await call_next(request)
        return response

# Add the middleware to the FastAPI application
app.add_middleware(ExperimentScopeMiddleware)


###############################################################################
# Database Helper Functions
###############################################################################
async def _ensure_db_indices(db: AsyncIOMotorDatabase):
    """
    Ensures essential database indexes exist for core collections.
    Creates indexes in the background if they don't exist.
    """
    try:
        # Unique index on user email for login and registration uniqueness
        await db.users.create_index("email", unique=True, background=True)
        # Unique index on experiment slug for configuration lookup
        await db.experiments_config.create_index("slug", unique=True, background=True)
        logger.info("✔️ Core MongoDB indexes ensured (users.email, experiments_config.slug).")
    except Exception as e:
        logger.error(f"⚠️ Failed to ensure core MongoDB indexes: {e}", exc_info=True)
        # Log error but don't halt application, as it might recover or indexes might exist

async def _seed_admin(app: FastAPI):
    """
    Creates the initial administrator user if no admin exists in the database.
    Uses credentials from environment variables or defaults.
    Also attempts to seed default admin policies via the AuthZ provider if supported.
    """
    db: AsyncIOMotorDatabase = app.state.mongo_db
    authz: Optional[AuthorizationProvider] = getattr(app.state, "authz_provider", None)

    # Check if any admin user already exists
    if await db.users.count_documents({"is_admin": True}) > 0:
        logger.info("Admin user already exists. Skipping seeding.")
        return

    logger.warning("🚨 No admin user found. Seeding default administrator...")
    email = ADMIN_EMAIL_DEFAULT
    password = ADMIN_PASSWORD_DEFAULT
    # Hash the password securely using bcrypt
    try:
        pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    except Exception as e:
        logger.error(f"❌ Failed to hash default admin password: {e}", exc_info=True)
        return # Cannot proceed without hashed password

    # Insert the admin user document
    try:
        await db.users.insert_one(
            {
                "email": email,
                "password_hash": pwd_hash,
                "is_admin": True,
                "created_at": datetime.datetime.utcnow(),
            }
        )
        logger.warning(f"✔️ Default admin user '{email}' created with the default password.")
        logger.warning("🚨 IMPORTANT: Change the default admin password immediately if this is a persistent environment!")
    except Exception as e:
        logger.error(f"❌ Failed to insert default admin user '{email}': {e}", exc_info=True)
        return # If user insert fails, cannot seed policies

    # Attempt to seed default admin policies if the AuthZ provider supports it
    # Check for specific methods indicating seeding capability
    if (authz and hasattr(authz, "add_role_for_user") and
            hasattr(authz, "add_policy") and hasattr(authz, "save_policy")):
        logger.info(f"AuthZ Provider '{authz.__class__.__name__}' supports policy seeding. Applying default admin policies.")
        try:
            # Assign the 'admin' role to the new user
            await authz.add_role_for_user(email, "admin") # type: ignore
            # Grant the 'admin' role permission to access the 'admin_panel' resource
            await authz.add_policy("admin", "admin_panel", "access") # type: ignore
            # Persist the policy changes (if required by the provider)
            if asyncio.iscoroutinefunction(authz.save_policy): # Check if save_policy is async
                 await authz.save_policy() # type: ignore
            else:
                 authz.save_policy() # type: ignore
            logger.info(f"✔️ Default policies seeded for admin user '{email}'.")
        except Exception as e:
            logger.error(f"⚠️ Failed to seed default policies using provider '{authz.__class__.__name__}': {e}", exc_info=True)
    else:
        logger.warning(f"AuthZ Provider '{authz.__class__.__name__ if authz else 'None'}' does not support automatic policy seeding (missing required methods). Manual policy setup might be needed.")


###############################################################################
# File System Helper Functions
###############################################################################
def _parse_requirements_file(exp_path: Path) -> List[str]:
    """
    Reads an experiment's requirements.txt file, returning a list of dependencies.
    Skips empty lines and comments.
    """
    req_file = exp_path / "requirements.txt"
    if not req_file.is_file():
        return [] # Return empty list if file doesn't exist
    try:
        with req_file.open("r", encoding="utf-8") as fh:
            # List comprehension to efficiently parse lines
            return [
                line.strip() for line in fh
                if line.strip() and not line.strip().startswith("#")
            ]
    except Exception as e:
        logger.error(f"⚠️ Failed to parse requirements.txt for '{exp_path.name}': {e}")
        return [] # Return empty list on error

def _scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
    """
    Recursively scans a directory and returns a tree structure suitable for JSON.
    Skips common unwanted files/directories. Used for the admin file browser.

    Args:
        dir_path: The current directory Path object being scanned.
        base_path: The root directory of the experiment (used for relative paths).

    Returns:
        A list of dictionaries, where each dict represents a file or directory.
        Directories contain a 'children' key with a recursive list.
    """
    tree: List[Dict[str, Any]] = []
    if not dir_path.is_dir():
        return tree # Return empty if path isn't a directory

    try:
        # Iterate through items, sorted alphabetically
        for item in sorted(dir_path.iterdir()):
            # Skip common temporary/version control/cache directories/files
            if item.name in ("__pycache__", ".DS_Store", ".git", ".idea", ".vscode"):
                continue

            # Calculate path relative to the experiment's base directory
            relative_path = item.relative_to(base_path)

            if item.is_dir():
                # Recursively scan subdirectories
                tree.append({
                    "name": item.name,
                    "type": "dir",
                    "path": str(relative_path), # Store relative path as string
                    "children": _scan_directory(item, base_path) # Recursive call
                })
            else:
                # Add file entry
                tree.append({
                    "name": item.name,
                    "type": "file",
                    "path": str(relative_path), # Store relative path as string
                })
    except OSError as e:
        logger.error(f"⚠️ Error scanning directory '{dir_path}': {e}")
        # Add an error node to the tree if scanning fails
        tree.append({"name": f"[Error: {e.strerror}]", "type": "error", "path": str(dir_path.relative_to(base_path))})

    return tree

def _secure_path(base_dir: Path, relative_path_str: str) -> Path:
    """
    Safely resolves a relative path against a base directory.
    Crucially prevents directory traversal attacks (e.g., '../../etc/passwd').

    Args:
        base_dir: The trusted base directory (e.g., the experiment's root).
        relative_path_str: The untrusted relative path string from the client/manifest.

    Returns:
        A resolved Path object guaranteed to be inside the base_dir.

    Raises:
        HTTPException: With status 403 if traversal is detected.
                       With status 400 if path is invalid.
    """
    try:
        # Normalize the relative path (e.g., collapse '..' or '//')
        # This step is important but not sufficient on its own.
        normalized_relative = Path(os.path.normpath(relative_path_str))

        # Ensure the normalized path doesn't start with '/' or '..' after normalization
        # This prevents absolute paths and paths trying to escape upwards immediately.
        if normalized_relative.is_absolute() or str(normalized_relative).startswith(".."):
             raise ValueError("Invalid relative path.")

        # Resolve the absolute path by joining base dir and normalized relative path
        absolute_path = (base_dir.resolve() / normalized_relative).resolve()

        # THE CRITICAL CHECK: Verify the resolved absolute path is *still* within the base directory.
        # We check if base_dir is one of the parents of the resolved path.
        if base_dir.resolve() not in absolute_path.parents and absolute_path != base_dir.resolve():
            logger.warning(f"Directory traversal attempt blocked: base='{base_dir}', requested='{relative_path_str}', resolved='{absolute_path}'")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Directory traversal attempt blocked."
            )

        return absolute_path
    except ValueError: # Catches invalid paths from normalization or initial checks
         logger.warning(f"Invalid path requested: base='{base_dir}', requested='{relative_path_str}'")
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path.")
    except Exception as e: # Catch any other unexpected errors during path resolution
        logger.error(f"Unexpected error resolving path: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing file path.")

###############################################################################
# Router: Authentication Routes (/auth)
###############################################################################
auth_router = APIRouter(prefix="/auth", tags=["Authentication"])

@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
async def login_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
    """Serves the login page."""
    if not templates: raise HTTPException(500, "Template engine not available.")
    # Ensure 'next' URL is safe (basic check for relative path)
    safe_next = next if next and next.startswith("/") else "/"
    return templates.TemplateResponse("login.html", {
        "request": request, "next": safe_next, "message": message, "error": None,
        "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })

@auth_router.post("/login")
async def login_post(request: Request):
    """Handles login form submission, verifies credentials, and sets JWT cookie."""
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    form_data = await request.form()
    email = form_data.get("email")
    password = form_data.get("password")
    next_url = form_data.get("next", "/")
    safe_next_url = next_url if next_url and next_url.startswith("/") else "/" # Sanitize redirect

    # Basic validation
    if not email or not password:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Email and password are required.", "next": safe_next_url,
             "ENABLE_REGISTRATION": ENABLE_REGISTRATION, # Pass flag back on error
        }, status_code=status.HTTP_400_BAD_REQUEST)

    # Find user and verify password hash
    user = await db.users.find_one({"email": email})
    if user and bcrypt.checkpw(password.encode("utf-8"), user.get("password_hash", b"")):
        logger.info(f"Successful login for user: {email}")
        # Create JWT payload with essential user info and expiration
        payload = {
            "user_id": str(user["_id"]),
            "is_admin": user.get("is_admin", False),
            "email": user.get("email"),
            "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1), # 1 day expiration
        }
        # Encode JWT using the secret key
        try:
            token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        except Exception as e:
             logger.error(f"JWT encoding failed for user {email}: {e}", exc_info=True)
             raise HTTPException(500, "Login failed due to server error.")

        # Redirect user to the originally requested page (or home)
        response = RedirectResponse(safe_next_url, status_code=status.HTTP_303_SEE_OTHER)
        # Set the JWT as an HTTP-only cookie for security
        response.set_cookie(
            key="token", value=token, httponly=True, # httponly prevents JS access
            secure=(request.url.scheme == "https"), # secure ensures cookie sent only over HTTPS
            samesite="lax", # samesite provides CSRF protection
            max_age=60 * 60 * 24, # Cookie expires in 1 day (matches JWT)
        )
        return response
    else:
        logger.warning(f"Failed login attempt for email: {email}")
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Invalid email or password.", "next": safe_next_url,
             "ENABLE_REGISTRATION": ENABLE_REGISTRATION, # Pass flag back on error
        }, status_code=status.HTTP_401_UNAUTHORIZED)

@auth_router.get("/logout", name="logout", response_class=RedirectResponse)
async def logout(request: Request):
    """Logs the user out by deleting the JWT cookie."""
    # Log who is logging out for audit purposes
    token_data = await get_current_user(request.cookies.get("token")) # Use dependency to decode
    user_email = token_data.get("email") if token_data else "Unknown/Expired"
    logger.info(f"User logging out: {user_email}")

    # Redirect to home page
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    # Instruct the browser to delete the cookie
    response.delete_cookie("token")
    return response

# Include the authentication router in the main application
app.include_router(auth_router)


###############################################################################
# Router: Admin Panel Routes (/admin)
###############################################################################
# All routes in this router require admin privileges, enforced by the dependency.
admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin Panel"],
    dependencies=[Depends(require_admin)] # Apply admin check to all routes here
)

@admin_router.get("/", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin), # Get authenticated admin user
    message: Optional[str] = None, # Optional flash message
):
    """Serves the main admin dashboard page."""
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    error_message: Optional[str] = None # For displaying critical errors

    # Discover experiments present in the filesystem
    code_slugs = set()
    if EXPERIMENTS_DIR.is_dir():
        try:
            code_slugs = {
                item.name for item in EXPERIMENTS_DIR.iterdir()
                # Must be a directory, not starting with _ or .
                if item.is_dir() and not item.name.startswith(("_", "."))
            }
        except OSError as e:
            error_message = f"Error reading experiments directory '{EXPERIMENTS_DIR}': {e}"
            logger.error(error_message)
    else:
        error_message = f"Experiments directory missing at '{EXPERIMENTS_DIR}'"
        logger.error(error_message)

    # Fetch all experiment configurations from the database
    try:
        db_configs_list = await db.experiments_config.find().to_list(length=None) # Fetch all
    except Exception as e:
        error_message = f"Error fetching experiment configurations from DB: {e}"
        logger.error(error_message, exc_info=True)
        db_configs_list = [] # Proceed with empty list on error

    # Create a lookup map for faster access
    db_slug_map = {cfg.get("slug"): cfg for cfg in db_configs_list if cfg.get("slug")}

    # Categorize experiments based on presence in code and DB
    configured_experiments: List[Dict[str, Any]] = [] # Configured in DB, code exists
    discovered_slugs: List[str] = [] # Code exists, but no DB config
    orphaned_configs: List[Dict[str, Any]] = [] # Configured in DB, but no code directory

    # Match code slugs with DB configs
    for slug in code_slugs:
        if slug in db_slug_map:
            cfg = db_slug_map[slug]
            cfg["code_found"] = True # Mark that code exists
            configured_experiments.append(cfg)
        else:
            # Code found, but no corresponding DB entry
            discovered_slugs.append(slug)

    # Identify DB configs without matching code directories
    for cfg in db_configs_list:
        if cfg.get("slug") not in code_slugs:
            cfg["code_found"] = False # Mark that code is missing
            orphaned_configs.append(cfg)

    # Sort lists alphabetically for consistent display
    configured_experiments.sort(key=lambda x: x.get("slug", ""))
    discovered_slugs.sort()
    orphaned_configs.sort(key=lambda x: x.get("slug", ""))

    # Render the admin dashboard template
    return templates.TemplateResponse("admin/index.html", {
        "request": request,
        "configured": configured_experiments,
        "discovered": discovered_slugs,
        "orphaned": orphaned_configs,
        "message": message, # Pass flash message if any
        "error_message": error_message, # Pass critical error message if any
        "current_user": user, # Pass authenticated admin user info
    })

def _get_default_manifest(slug_id: str) -> Dict[str, Any]:
    """Returns a default, minimal manifest structure for a new experiment."""
    return {
        "slug": slug_id, # Ensure slug matches the directory/URL
        "name": f"Experiment: {slug_id}", # Default name
        "description": "", # Empty description
        "status": "draft", # Default to inactive status
        "auth_required": False, # Default to public access
        "data_scope": ["self"], # Default scope to only its own data
        "managed_indexes": {} # No managed indexes by default
    }

@admin_router.get("/configure/{slug_id}", response_class=HTMLResponse, name="configure_experiment_get")
async def configure_experiment_get(
    request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)
):
    """
    Serves the experiment configuration page.
    - Loads manifest content (from file, then DB, then default).
    - Scans the experiment's directory structure.
    - Gathers quick check info about the experiment's code.
    - Gathers core engine status (Ray, Isolation).
    """
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    experiment_path = EXPERIMENTS_DIR / slug_id
    manifest_path = experiment_path / "manifest.json"
    manifest_data: Optional[Dict[str, Any]] = None
    manifest_content: str = "" # Raw content for the editor

    # Priority order for loading manifest: file > database > default
    # 1. Try loading from manifest.json file
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                manifest_content = f.read()
            if manifest_content.strip(): # Check if file is not empty
                manifest_data = json.loads(manifest_content)
                logger.info(f"Loaded manifest for '{slug_id}' from file.")
            else:
                logger.warning(f"Manifest file for '{slug_id}' is empty. Will check DB.")
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding manifest.json for '{slug_id}': {e}")
            manifest_content = f'{{"error": "Manifest file is invalid JSON: {e}"}}' # Show error in editor
        except OSError as e:
            logger.error(f"Error reading manifest.json for '{slug_id}': {e}")
            manifest_content = f'{{"error": "Could not read manifest file: {e}"}}' # Show error in editor

    # 2. If file load failed or file empty/missing, try loading from DB
    if manifest_data is None:
        try:
            db_config = await db.experiments_config.find_one({"slug": slug_id})
            if db_config:
                logger.info(f"Loaded manifest for '{slug_id}' from database.")
                db_config.pop("_id", None) # Remove MongoDB internal ID
                manifest_data = db_config
                # Re-serialize to get consistent formatting for the editor
                manifest_content = json.dumps(manifest_data, indent=2)
            else:
                # 3. If not in DB either, create a default manifest
                logger.info(f"No manifest found in file or DB for '{slug_id}'. Generating default.")
                manifest_data = _get_default_manifest(slug_id)
                manifest_content = json.dumps(manifest_data, indent=2)
        except Exception as e:
             logger.error(f"Error loading manifest for '{slug_id}' from DB: {e}", exc_info=True)
             manifest_data = {"error": f"DB load failed: {e}"} # Show error
             manifest_content = json.dumps(manifest_data, indent=2)


    # --- File Discovery ---
    # Scan the directory recursively
    file_tree = _scan_directory(experiment_path, experiment_path) # Pass base path for relative paths

    # --- Quick Checks (derived from file presence/content) ---
    discovery_info = {
        "has_module": False, "defines_actor": False, "defines_bp": False,
        "has_requirements": False, "requirements": [],
        "has_static_dir": False, "has_templates_dir": False,
    }
    if experiment_path.is_dir():
        # Check for __init__.py and peek inside
        module_path = experiment_path / "__init__.py"
        discovery_info["has_module"] = module_path.is_file()
        if discovery_info["has_module"]:
            try:
                with module_path.open("r", encoding="utf-8") as f: module_content = f.read()
                discovery_info["defines_actor"] = "ExperimentActor" in module_content
                discovery_info["defines_bp"] = "bp = APIRouter" in module_content # Simple string check
            except Exception as e: logger.warning(f"Could not read module '{slug_id}/__init__.py' for discovery: {e}")
        # Check for requirements.txt
        reqs_path = experiment_path / "requirements.txt"
        discovery_info["has_requirements"] = reqs_path.is_file()
        if discovery_info["has_requirements"]:
             try: discovery_info["requirements"] = _parse_requirements_file(experiment_path)
             except Exception as e: logger.warning(f"Could not read requirements.txt for '{slug_id}': {e}")
        # Check for standard directories
        discovery_info["has_static_dir"] = (experiment_path / "static").is_dir()
        discovery_info["has_templates_dir"] = (experiment_path / "templates").is_dir()

    # --- Core Engine Status ---
    # Get status flags from application state
    core_info = {
        "ray_available": getattr(request.app.state, "ray_is_available", False),
        "environment_mode": getattr(request.app.state, "environment_mode", "unknown"),
        "isolation_enabled": getattr(request.app.state, "environment_mode", "") == "isolated",
    }

    # Render the configuration template with all gathered data
    return templates.TemplateResponse("admin/configure.html", {
        "request": request,
        "slug_id": slug_id,
        "current_user": user,
        "manifest_content": manifest_content, # Raw JSON string for editor
        "discovery_info": discovery_info,    # Quick checks summary
        "file_tree": file_tree,              # Detailed file structure
        "core_info": core_info,              # Ray/Isolation status
    })


@admin_router.post("/api/save-manifest/{slug_id}", response_class=JSONResponse)
async def save_manifest(
    request: Request, slug_id: str, data: Dict[str, str] = Body(...),
    user: Dict[str, Any] = Depends(require_admin)
):
    """
    API Endpoint: Validates and saves the manifest content to both the
    `manifest.json` file and the MongoDB configuration collection.
    Triggers an experiment reload if the 'status' field changed.
    """
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    content = data.get("content")
    if content is None: raise HTTPException(400, "Missing 'content' in request body.")

    experiment_path = EXPERIMENTS_DIR / slug_id
    # Ensure experiment directory exists, create if not (e.g., for newly discovered)
    if not experiment_path.is_dir():
        try: experiment_path.mkdir(parents=True, exist_ok=True); logger.info(f"Created missing experiment directory: {experiment_path}")
        except OSError as e: logger.error(f"Failed create dir for '{slug_id}': {e}"); raise HTTPException(500, f"Directory '{slug_id}' not found and could not be created: {e}")

    manifest_path = experiment_path / "manifest.json"
    message = f"Manifest for '{slug_id}' saved successfully." # Default success message

    try:
        # 1. Validate incoming content as JSON
        new_manifest_data = json.loads(content)
        if not isinstance(new_manifest_data, dict): raise ValueError("Manifest must be a JSON object.")
        # Optionally add more schema validation here if needed

        # 2. Get current status from DB to check for changes
        old_config = await db.experiments_config.find_one({"slug": slug_id})
        old_status = old_config.get("status", "draft") if old_config else "draft"
        new_status = new_manifest_data.get("status", "draft") # Default to draft if missing

        # 3. Write validated content to manifest.json (pretty-printed)
        try:
            with manifest_path.open("w", encoding="utf-8") as f:
                json.dump(new_manifest_data, f, indent=2)
            logger.info(f"Saved manifest file for '{slug_id}'.")
        except OSError as e:
            logger.error(f"Failed write manifest file for '{slug_id}': {e}", exc_info=True)
            raise HTTPException(500, f"Failed to write manifest file: {e}")

        # 4. Update the database configuration (upsert ensures creation if missing)
        # Ensure the 'slug' field from the URL matches the data being saved
        config_data = {"slug": slug_id, **new_manifest_data}
        await db.experiments_config.update_one(
            {"slug": slug_id}, {"$set": config_data}, upsert=True
        )
        logger.info(f"Upserted experiment config in DB for '{slug_id}'.")

        # 5. Trigger reload if status changed from draft/inactive to active, or vice-versa
        if old_status != new_status:
            logger.info(f"Experiment '{slug_id}' status changed: '{old_status}' -> '{new_status}'. Triggering experiment reload.")
            await reload_active_experiments(request.app) # Reload all active experiments
            message = f"Manifest saved. Status changed to '{new_status}', experiments reloaded."

        return JSONResponse({"message": message})

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to save manifest for '{slug_id}': Invalid JSON provided. {e}")
        raise HTTPException(400, f"Invalid JSON format: {e}")
    except ValueError as e: # Catch other validation errors
         logger.warning(f"Failed to save manifest for '{slug_id}': Invalid data. {e}")
         raise HTTPException(400, f"Invalid manifest data: {e}")
    except Exception as e: # Catch unexpected errors during DB update or reload
        logger.error(f"Unexpected error saving manifest/reloading for '{slug_id}': {e}", exc_info=True)
        raise HTTPException(500, f"An unexpected server error occurred: {e}")


@admin_router.post("/api/reload-experiment/{slug_id}", response_class=JSONResponse)
async def reload_experiment(
    request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)
):
    """API Endpoint: Manually triggers a reload of all active experiments."""
    admin_email = user.get('email', 'Unknown Admin')
    logger.info(f"Admin '{admin_email}' triggered manual reload via API (context: '{slug_id}').")
    try:
        await reload_active_experiments(request.app)
        return JSONResponse({"message": "Experiment reload triggered successfully. Check server logs for details."})
    except Exception as e:
        logger.error(f"Manual experiment reload failed (triggered by '{admin_email}'): {e}", exc_info=True)
        raise HTTPException(500, f"Failed to trigger experiment reload: {e}")


@admin_router.get("/api/index-status/{slug_id}", response_class=JSONResponse)
async def get_index_status(
    request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)
):
    """
    API Endpoint: Gets the real-time status of managed indexes for an experiment.
    Applies slug prefixing to collection/index names before checking.
    """
    # Headers to prevent browser caching of this dynamic status endpoint
    no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

    if not INDEX_MANAGER_AVAILABLE:
        return JSONResponse({"error": "Index Management feature is not available."}, status_code=501, headers=no_cache_headers)

    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    try:
        # Fetch the canonical configuration from the database
        config = await db.experiments_config.find_one({"slug": slug_id})
    except Exception as e:
         logger.error(f"Failed to fetch config for '{slug_id}' for index status: {e}", exc_info=True)
         return JSONResponse({"error": "Failed to fetch experiment config."}, status_code=500, headers=no_cache_headers)

    if not config:
        return JSONResponse([], headers=no_cache_headers) # No config, so no managed indexes

    managed_indexes: Dict[str, List[Dict]] = config.get("managed_indexes", {})
    if not managed_indexes:
        return JSONResponse([], headers=no_cache_headers) # Config exists, but no indexes defined

    status_list = []
    # Iterate through BASE collection names defined in the manifest
    for collection_base_name, indexes_to_create in managed_indexes.items():
        if not collection_base_name or not isinstance(indexes_to_create, list): continue # Skip invalid entries

        # Apply prefix convention
        prefixed_collection_name = f"{slug_id}_{collection_base_name}"

        try:
            # Get the actual collection object using the prefixed name
            real_collection = db[prefixed_collection_name]
            index_manager = AsyncAtlasIndexManager(real_collection)

            # Iterate through BASE index names defined for this collection
            for index_def in indexes_to_create:
                index_base_name = index_def.get("name")
                index_type = index_def.get("type")
                if not index_base_name or not index_type: continue # Skip invalid index defs

                # Apply prefix convention
                prefixed_index_name = f"{slug_id}_{index_base_name}"

                # Prepare status info dict (using prefixed names for reporting)
                status_info = {
                    "collection": prefixed_collection_name,
                    "name": prefixed_index_name,
                    "type": index_type,
                    "status": "UNKNOWN", # Default status
                    "queryable": False,
                }

                try:
                    # Check status based on index type
                    if index_type in ("vectorSearch", "search"):
                        # Use manager to get Atlas Search index status
                        index_data = await index_manager.get_search_index(prefixed_index_name)
                        if index_data:
                            status_info["status"] = index_data.get("status", "UNKNOWN")
                            status_info["queryable"] = index_data.get("queryable", False)
                        else:
                            status_info["status"] = "NOT_FOUND" # Index doesn't exist

                    elif index_type == "regular":
                        # Use manager to get standard index status
                        index_data = await index_manager.get_index(prefixed_index_name)
                        if index_data:
                            # Standard indexes are effectively queryable if they exist
                            status_info["status"] = "QUERYABLE"
                            status_info["queryable"] = True
                        else:
                            status_info["status"] = "NOT_FOUND"

                    else:
                        status_info["status"] = "INVALID_TYPE" # Unknown type in manifest

                except Exception as e:
                    # Error during the check for a specific index
                    logger.warning(f"Error checking index '{prefixed_index_name}' on '{prefixed_collection_name}': {e}")
                    status_info["status"] = "CHECK_ERROR"

                status_list.append(status_info)

        except Exception as e:
            # Error initializing the index manager for the collection (e.g., DB connection issue)
            logger.error(f"Failed init index manager for '{prefixed_collection_name}': {e}", exc_info=True)
            # Add a placeholder error for this collection
            status_list.append({"collection": prefixed_collection_name, "name": "*", "status": "MANAGER_ERROR", "type": "error", "queryable": False})

    return JSONResponse(status_list, headers=no_cache_headers)


@admin_router.get("/api/get-file-content/{slug_id}", response_class=JSONResponse)
async def get_file_content(
    slug_id: str, path: str = Query(..., description="Relative path of the file within the experiment directory"),
    user: Dict[str, Any] = Depends(require_admin)
):
    """
    API Endpoint: Safely reads and returns the content of a specified file
    within an experiment's directory. Prevents directory traversal.
    Detects binary files.
    """
    experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
    try:
        # Use secure_path to resolve and validate the requested path
        file_path = _secure_path(experiment_path, path)
    except HTTPException as e:
        # Forward security/validation errors from _secure_path
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    if not file_path.is_file():
        return JSONResponse({"error": "File not found at specified path."}, status_code=404)

    try:
        # Attempt to read as UTF-8 text
        with file_path.open("r", encoding="utf-8") as f:
            content = f.read()
        logger.debug(f"Read text content for file: {file_path}")
        return JSONResponse({"content": content, "is_binary": False})
    except UnicodeDecodeError:
        # If UTF-8 decoding fails, assume it's binary
        logger.debug(f"File detected as binary (cannot decode as UTF-8): {file_path}")
        return JSONResponse({"content": "[Binary file - Content not displayed]", "is_binary": True})
    except OSError as e:
        logger.error(f"Error reading file '{file_path}': {e}", exc_info=True)
        return JSONResponse({"error": f"Could not read file: {e}"}, status_code=500)
    except Exception as e: # Catch any other unexpected errors
        logger.error(f"Unexpected error reading file '{file_path}': {e}", exc_info=True)
        return JSONResponse({"error": "An unexpected error occurred while reading the file."}, status_code=500)

@admin_router.post("/api/upload-experiment/{slug_id}", response_class=JSONResponse)
async def upload_experiment_zip(
    request: Request, # Need request for reload_active_experiments
    slug_id: str,
    file: UploadFile = File(..., description="Zip file containing the experiment code"),
    user: Dict[str, Any] = Depends(require_admin)
):
    """
    API Endpoint: Handles experiment code upload via zip file.
    - Validates file type and essential zip contents (manifest.json).
    - Checks for directory traversal vulnerabilities (Zip Slip).
    - Deletes the existing experiment directory.
    - Safely extracts the zip contents.
    - Triggers an experiment reload.
    """
    admin_email = user.get('email', 'Unknown Admin')
    logger.info(f"Admin '{admin_email}' initiated zip upload for experiment '{slug_id}'.")

    # 1. Validate file type
    if file.content_type not in ["application/zip", "application/x-zip-compressed"]:
        logger.warning(f"Invalid file type uploaded for '{slug_id}': {file.content_type}")
        raise HTTPException(400, "Invalid file type. Please upload a .zip file.")

    experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
    zip_data: bytes # To store the read zip data

    # 2. Pre-check zip contents in memory before touching the filesystem
    try:
        zip_data = await file.read() # Read the entire file into memory
        await file.seek(0) # Reset file pointer in case needed later (though we use zip_data now)

        with io.BytesIO(zip_data) as zip_buffer:
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                namelist = zip_ref.namelist() # Get list of all files/dirs in the zip

                # --- Essential File Check ---
                # Check for manifest.json, allowing for it to be in a subdirectory if zip contains a root folder
                has_manifest = any(p == "manifest.json" or p.endswith("/manifest.json") for p in namelist)
                if not has_manifest:
                    logger.warning(f"Zip upload rejected for '{slug_id}': Missing 'manifest.json'.")
                    raise HTTPException(400, "Invalid zip: Upload must contain a 'manifest.json' file.")

                # --- Security Check: Zip Slip ---
                for member in zip_ref.infolist():
                    # Attempt to resolve the target path *as if* it were extracted
                    target_path = (experiment_path / member.filename).resolve()
                    # Check if the resolved path escapes the intended experiment directory
                    if experiment_path not in target_path.parents and target_path != experiment_path:
                        logger.error(f"SECURITY ALERT: Zip Slip attempt blocked for '{slug_id}'! Member: '{member.filename}', Target: '{target_path}'")
                        raise HTTPException(400, f"Invalid zip: Contains potentially malicious path traversal ('{member.filename}').")

                # --- Optional Checks (log warnings) ---
                has_init = any(p == "__init__.py" or p.endswith("/__init__.py") for p in namelist)
                if not has_init:
                    logger.warning(f"Uploaded zip for '{slug_id}' is missing '__init__.py'. Experiment might not load correctly.")

                logger.info(f"Zip pre-checks passed for '{slug_id}'. Proceeding with overwrite.")

    except zipfile.BadZipFile:
        logger.warning(f"Invalid zip file uploaded for '{slug_id}'.")
        raise HTTPException(400, "Invalid or corrupted .zip file.")
    except HTTPException as http_exc:
        raise http_exc # Re-raise validation/security exceptions
    except Exception as e:
        logger.error(f"Error during zip pre-check for '{slug_id}': {e}", exc_info=True)
        raise HTTPException(500, f"Error reading or validating zip file: {e}")

    # 3. If pre-checks passed, delete the old directory
    try:
        if experiment_path.exists():
            logger.info(f"Deleting existing directory: {experiment_path}")
            shutil.rmtree(experiment_path)
            logger.info(f"Successfully deleted: {experiment_path}")
    except OSError as e:
        logger.error(f"Failed to delete old experiment directory '{experiment_path}': {e}", exc_info=True)
        raise HTTPException(500, f"Failed to clear old experiment directory: {e}")

    # 4. Re-create the base directory and extract
    try:
        experiment_path.mkdir(parents=True) # Recreate the base directory
        logger.info(f"Extracting zip contents to: {experiment_path}")
        with io.BytesIO(zip_data) as zip_buffer: # Use the previously read data
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                zip_ref.extractall(experiment_path) # Extract all validated contents
        logger.info(f"Successfully extracted zip for '{slug_id}'.")
    except Exception as e:
        logger.error(f"Failed to extract zip file for '{slug_id}' after deleting old dir: {e}", exc_info=True)
        # Attempt to clean up potentially partially extracted directory
        try: shutil.rmtree(experiment_path)
        except Exception: pass
        raise HTTPException(500, f"Error extracting zip file: {e}")

    # 5. Trigger experiment reload
    try:
        logger.info(f"Triggering experiment reload after zip upload for '{slug_id}'.")
        await reload_active_experiments(request.app)
        return JSONResponse({
            "message": f"Experiment '{slug_id}' uploaded, extracted, and reloaded successfully."
        })
    except Exception as e:
        logger.error(f"Experiment reload failed after zip upload for '{slug_id}': {e}", exc_info=True)
        # Zip was extracted, but reload failed. Return success with warning.
        raise HTTPException(500, f"Zip extracted successfully, but experiment reload failed: {e}")


app.include_router(admin_router) # Add admin routes to the application


###############################################################################
# Experiment Loading and Routing Logic
###############################################################################
async def reload_active_experiments(app: FastAPI):
    """
    Reloads all experiments marked as 'active' in the database.
    - Clears existing experiment state.
    - Fetches active configurations from DB.
    - Calls `_register_experiments` to load and mount them.
    """
    db: AsyncIOMotorDatabase = app.state.mongo_db
    logger.info("🔄 Starting reload of active experiments...")
    try:
        # Find all documents in experiments_config where status is 'active'
        active_cfgs = await db.experiments_config.find({"status": "active"}).to_list(None)
        logger.info(f"Found {len(active_cfgs)} active experiment configurations in database.")
        # Call the registration function with the fetched configs
        await _register_experiments(app, active_cfgs, is_reload=True)
        logger.info("✔️ Experiment reload process complete.")
    except Exception as e:
        logger.error(f"❌ Critical error during experiment reload: {e}", exc_info=True)
        # Depending on severity, might want to clear app.state.experiments or handle differently
        app.state.experiments.clear() # Clear state on failure to avoid inconsistency

async def _register_experiments(
    app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False
):
    """
    Loads, configures, and mounts routes for a list of active experiment configurations.

    Args:
        app: The FastAPI application instance.
        active_cfgs: A list of experiment configuration dictionaries from the DB.
        is_reload: Boolean flag indicating if this is a reload operation (clears previous state).
    """
    if is_reload:
        logger.debug("Clearing previous experiment state...")
        # TODO: Need a way to gracefully shut down existing Ray actors or routers if reloading
        app.state.experiments.clear() # Clear the dictionary holding loaded experiment configs

    # Ensure the base experiments directory exists
    if not EXPERIMENTS_DIR.is_dir():
        logger.error(f"❌ Cannot load experiments: Base directory '{EXPERIMENTS_DIR}' not found.")
        return

    env_mode = getattr(app.state, "environment_mode", "production") # Get env mode from state

    # Process each active configuration
    for cfg in active_cfgs:
        slug = cfg.get("slug")
        if not slug:
            logger.warning(f"Skipping experiment config with missing 'slug': {cfg.get('_id', 'N/A')}")
            continue

        logger.debug(f"Registering experiment: '{slug}'")
        exp_path = EXPERIMENTS_DIR / slug

        # Check if the code directory exists for this slug
        if not exp_path.is_dir():
            logger.warning(f"⚠️ Code directory not found for active experiment '{slug}' at '{exp_path}'. Skipping registration.")
            continue

        # --- Data Scoping ---
        # Resolve 'self' in data_scope to the actual slug_id
        # This determines which experiment_ids this experiment can read
        read_scopes = [slug if s == "self" else s for s in cfg.get("data_scope", ["self"])] # Default to self-only
        cfg["resolved_read_scopes"] = read_scopes
        logger.debug(f"[{slug}] Read scopes resolved to: {read_scopes}")

        # --- Static Files Mounting ---
        # Mount the experiment's static directory if it exists
        static_dir = exp_path / "static"
        if static_dir.is_dir():
            mount_path = f"/experiments/{slug}/static"
            mount_name = f"experiment_{slug}_static"
            # Check if this mount path/name already exists to avoid duplication errors on reload
            if not any(route.path == mount_path for route in app.routes if hasattr(route, "path")):
                 try:
                    app.mount(mount_path, StaticFiles(directory=str(static_dir)), name=mount_name)
                    logger.debug(f"[{slug}] Mounted static directory at '{mount_path}'.")
                 except Exception as e: # Catch potential errors during mounting
                     logger.error(f"[{slug}] Failed to mount static directory '{static_dir}': {e}", exc_info=True)
            else:
                 logger.debug(f"[{slug}] Static mount '{mount_path}' already exists.")

        # --- Python Module Loading ---
        router = APIRouter() # Create a new router for this experiment's endpoints
        exp_mod = None # Variable to hold the loaded module
        try:
            # Construct the Python module name (e.g., experiments.my_exp)
            # Replace hyphens with underscores for valid Python identifiers
            mod_name = f"experiments.{slug.replace('-', '_')}"
            # If reloading, force Python to re-read the module file
            if mod_name in sys.modules and is_reload:
                logger.debug(f"[{slug}] Reloading existing module: {mod_name}")
                exp_mod = importlib.reload(sys.modules[mod_name])
            else:
                logger.debug(f"[{slug}] Importing module: {mod_name}")
                exp_mod = importlib.import_module(mod_name) # Standard import
        except ModuleNotFoundError:
            # This is expected if an experiment has no Python code (e.g., static site only)
            logger.info(f"[{slug}] No Python module ('__init__.py') found. Skipping Python component loading.")
        except Exception as e:
            # Log errors during module import/reload but continue registration
            logger.error(f"❌ Error loading Python module for experiment '{slug}': {e}", exc_info=True)
            # TODO: Should this experiment be marked as failed or skipped entirely?

        # --- Automatic Index Management (with Prefixing) ---
        if INDEX_MANAGER_AVAILABLE and "managed_indexes" in cfg:
            db: AsyncIOMotorDatabase = app.state.mongo_db
            managed_indexes: Dict[str, List[Dict]] = cfg.get("managed_indexes", {})
            logger.debug(f"[{slug}] Processing {len(managed_indexes)} managed collection(s) for indexes.")

            for collection_base_name, indexes_to_create in managed_indexes.items():
                if not collection_base_name or not isinstance(indexes_to_create, list):
                    logger.warning(f"[{slug}] Invalid 'managed_indexes' format for key '{collection_base_name}'. Skipping.")
                    continue

                prefixed_collection_name = f"{slug}_{collection_base_name}" # Apply prefix
                try:
                    real_collection = db[prefixed_collection_name] # Get collection object
                    index_manager = AsyncAtlasIndexManager(real_collection) # Init manager for this collection
                    logger.debug(f"[{slug}] Initialized Index Manager for collection '{prefixed_collection_name}'.")

                    for index_def in indexes_to_create:
                        index_base_name = index_def.get("name")
                        index_type = index_def.get("type")
                        if not index_base_name or not index_type:
                            logger.warning(f"[{slug} -> {prefixed_collection_name}] Skipping index definition missing 'name' or 'type': {index_def}")
                            continue

                        prefixed_index_name = f"{slug}_{index_base_name}" # Apply prefix
                        prefixed_index_def = index_def.copy()
                        prefixed_index_def["name"] = prefixed_index_name # Update definition with prefixed name

                        logger.info(f"[{slug}] Scheduling index check/creation for: '{prefixed_collection_name}' / '{prefixed_index_name}' (Type: {index_type})")
                        # Schedule index creation/update as a background task
                        asyncio.create_task(
                            _run_index_creation(
                                index_manager=index_manager, index_type=index_type, index_name=prefixed_index_name,
                                index_def=prefixed_index_def, slug=slug, collection_name=prefixed_collection_name
                            )
                        )
                except Exception as e:
                    logger.error(f"[{slug}] Failed to initialize Index Manager or schedule tasks for collection '{prefixed_collection_name}': {e}", exc_info=True)
        elif "managed_indexes" in cfg:
             logger.warning(f"[{slug}] Manifest contains 'managed_indexes' but Index Manager is not available. Indexes will not be managed.")

        # --- Ray Actor Initialization (Optional) ---
        if getattr(app.state, "ray_is_available", False) and exp_mod and hasattr(exp_mod, "ExperimentActor"):
            logger.debug(f"[{slug}] Ray Actor 'ExperimentActor' found in module.")
            actor_cls = getattr(exp_mod, "ExperimentActor")
            actor_name = f"{slug}-actor" # Convention-based actor name
            actor_runtime_env = {} # Runtime env for the actor (dependencies, env vars)

            # Determine runtime environment based on application mode
            if env_mode == "isolated":
                # In isolated mode, try to parse requirements.txt for the actor's env
                pip_deps = _parse_requirements_file(exp_path)
                if pip_deps:
                    actor_runtime_env = {"pip": pip_deps}
                    logger.info(f"[{slug}] Using isolated Ray runtime env with pip: {pip_deps}")
                else:
                    logger.info(f"[{slug}] Using isolated Ray runtime env (no requirements.txt found).")
            elif env_mode == "production":
                # In production, look for an explicit `runtime_env` dict in the module
                actor_runtime_env = getattr(exp_mod, "runtime_env", {}) or {}
                logger.info(f"[{slug}] Using production Ray runtime env: {actor_runtime_env}")

            try:
                # Get or create the Ray actor
                # - `name` & `namespace`: Uniquely identify the actor
                # - `lifetime='detached'`: Actor persists even if the original handle goes out of scope
                # - `get_if_exists=True`: Connect to existing actor if available (useful for reloads)
                # - `runtime_env`: Specifies dependencies/environment for the actor process
                actor_handle = actor_cls.options(
                    name=actor_name, namespace="modular_labs",
                    lifetime="detached", get_if_exists=True,
                    runtime_env=actor_runtime_env,
                ).remote( # Pass necessary initialization arguments to the actor's __init__
                    mongo_uri=MONGO_URI, db_name=DB_NAME,
                    write_scope=slug, read_scopes=read_scopes,
                )
                # Optionally store actor handle in app state if needed for direct calls (less common)
                # app.state.actors[slug] = actor_handle
                logger.info(f"✔️ [{slug}] Ray Actor '{actor_name}' started/connected.")
            except Exception as e:
                logger.error(f"❌ [{slug}] Failed to start/connect Ray Actor '{actor_name}': {e}", exc_info=True)
                # Consider how to handle actor startup failure - maybe disable experiment?

        # --- FastAPI Router Inclusion ---
        # Include the experiment's own APIRouter (`bp`) if defined in its module
        if exp_mod and hasattr(exp_mod, "bp"):
            bp = getattr(exp_mod, "bp")
            if isinstance(bp, APIRouter):
                logger.debug(f"[{slug}] Including APIRouter 'bp' from module.")
                router.include_router(bp)
            else:
                 logger.warning(f"[{slug}] Found 'bp' in module, but it's not an APIRouter instance. Skipping.")

        # --- Default Route ---
        # If the experiment module didn't provide any routes, add a default placeholder page
        if not router.routes:
            logger.debug(f"[{slug}] No routes defined in module or 'bp'. Adding default route.")
            @router.get("/", response_class=HTMLResponse, include_in_schema=False)
            async def default_experiment_page(request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)):
                if not templates: raise HTTPException(500, "Template engine not available.")
                # Render a generic template displaying experiment info
                return templates.TemplateResponse("default_experiment.html", {
                    "request": request, "experiment": cfg, "current_user": user,
                })

        # --- Mount Experiment Router ---
        # Define dependencies based on manifest's `auth_required` flag
        deps = [Depends(get_current_user_or_redirect)] if cfg.get("auth_required") else []
        prefix = f"/experiments/{slug}" # Base path for all experiment routes
        # Add the experiment's router to the main application
        app.include_router(
            router,
            prefix=prefix,
            tags=[f"Experiment: {slug}"], # Group routes in OpenAPI docs
            dependencies=deps, # Apply auth dependency if required
        )

        # Store the loaded configuration (including resolved scopes and URL) in app state
        cfg["url"] = prefix # Add the base URL for linking
        app.state.experiments[slug] = cfg
        logger.info(f"✔️ Registered Experiment: '{slug}' (Auth Required: {cfg.get('auth_required', False)}) at '{prefix}'")


async def _run_index_creation(
    index_manager: AsyncAtlasIndexManager, index_type: str, index_name: str,
    index_def: Dict[str, Any], slug: str, collection_name: str
):
    """
    Background task to ensure a specific index exists and matches the manifest definition.
    Handles both regular and Atlas Search/Vector indexes. Uses prefixed names.

    Args:
        index_manager: An initialized AsyncAtlasIndexManager for the correct (prefixed) collection.
        index_type: 'regular', 'vectorSearch', or 'search'.
        index_name: The prefixed name of the index to manage.
        index_def: The definition dictionary from the manifest (contains prefixed name).
        slug: Original experiment slug (for logging).
        collection_name: Prefixed collection name (for logging).
    """
    log_prefix = f"[{slug} -> {collection_name}]" # Use prefixed collection name in logs
    logger.info(f"{log_prefix} Starting index management task for '{index_name}' (Type: {index_type})")
    try:
        if index_type == "regular":
            keys = index_def.get("keys")
            # Extract options, ensuring 'name' from index_def overrides any in options
            options = {**index_def.get("options", {}), "name": index_name}
            if not keys: logger.warning(f"{log_prefix} Skipping regular index '{index_name}': 'keys' missing."); return

            # Check if index exists and if its key structure matches
            existing_index = await index_manager.get_index(index_name)
            if existing_index:
                # Build a comparable key document from the definition
                temp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
                key_doc = {k: v for k, v in temp_keys}
                # Compare key structure (MongoDB stores it in 'key' field)
                if existing_index.get("key") != key_doc:
                    logger.warning(f"{log_prefix} ⚠️ Index '{index_name}' exists with different keys. Dropping and recreating. (DB: {existing_index.get('key')}, Manifest: {key_doc})")
                    await index_manager.drop_index(index_name) # Drop the old index
                    # Fall through to create the new one
                else:
                    # Index exists and keys match. Check for option differences? (More complex, skipping for now)
                    logger.info(f"{log_prefix} ✅ Regular index '{index_name}' already exists and matches keys. No operation needed.")
                    return # Index is okay

            # If index doesn't exist or was dropped, create it
            logger.info(f"{log_prefix} Ensuring regular index '{index_name}' exists...")
            await index_manager.create_index(keys, **options) # Use unpacked options which includes name
            logger.info(f"{log_prefix} ✔️ Successfully ensured regular index '{index_name}'.")

        elif index_type in ("vectorSearch", "search"):
            definition = index_def.get("definition")
            if not definition: logger.warning(f"{log_prefix} Skipping search index '{index_name}': 'definition' missing."); return

            # Check if search index exists
            existing_index = await index_manager.get_search_index(index_name)
            if existing_index:
                # Compare current definition with manifest definition
                # Atlas stores the applied definition in 'latestDefinition' or 'definition'
                current_def = existing_index.get("latestDefinition", existing_index.get("definition"))
                if current_def == definition:
                    logger.info(f"{log_prefix} ✅ Search index '{index_name}' already exists and definition matches. Checking status...")
                    # Definition matches, but check if it's actually queryable
                    if not existing_index.get("queryable") and existing_index.get("status") != "FAILED":
                         logger.info(f"{log_prefix} Index '{index_name}' exists but is not queryable (Status: {existing_index.get('status', 'UNKNOWN')}). Waiting for it to become ready...")
                         await index_manager._wait_for_search_index_ready(index_name, index_manager.DEFAULT_SEARCH_TIMEOUT) # Wait if building/stale
                         logger.info(f"{log_prefix} ✔️ Index '{index_name}' is now ready.")
                    elif existing_index.get("status") == "FAILED":
                         logger.error(f"{log_prefix} ❌ Index '{index_name}' exists but is in FAILED state. Manual intervention required (drop and recreate).")
                    else:
                         logger.info(f"{log_prefix} ✔️ Index '{index_name}' is ready and queryable.")
                    return # Index okay or failed (don't try to update failed)
                else:
                    # Definition differs, trigger an update
                    logger.warning(f"{log_prefix} ⚠️ Search index '{index_name}' exists but definition differs from manifest. Submitting update...")
                    await index_manager.update_search_index(
                        name=index_name, definition=definition, wait_for_ready=True # Wait for update to complete
                    )
                    logger.info(f"{log_prefix} ✔️ Successfully updated search index '{index_name}'.")
            else:
                # Index does not exist, create it
                logger.info(f"{log_prefix} Search index '{index_name}' not found. Creating new index (Type: {index_type})...")
                await index_manager.create_search_index(
                    name=index_name, definition=definition, index_type=index_type,
                    wait_for_ready=True # Wait for initial build
                )
                logger.info(f"{log_prefix} ✔️ Successfully created new search index '{index_name}'.")
        else:
            logger.warning(f"{log_prefix} Skipping index '{index_name}': Unknown type '{index_type}' in manifest.")

    except Exception as e:
        # Catch-all for unexpected errors during index management for this specific index
        logger.error(f"{log_prefix} ❌ FAILED to manage index '{index_name}': {e}", exc_info=True)


###############################################################################
# Root Route (/)
###############################################################################
@app.get("/", response_class=HTMLResponse, name="home")
async def root(request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)):
    """Serves the main landing page, listing active experiments."""
    if not templates: raise HTTPException(500, "Template engine not available.")
    # Pass loaded experiments, user info, and registration flag to template
    return templates.TemplateResponse("index.html", {
        "request": request,
        "experiments": getattr(request.app.state, "experiments", {}), # Get loaded experiments from state
        "current_user": user,
        "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })


###############################################################################
# Development Server Entry Point
###############################################################################
# This block allows running the app directly using `python main.py` for development.
# It uses uvicorn with auto-reload enabled. Not recommended for production.
if __name__ == "__main__":
    import uvicorn
    logger.warning("🚀 Starting application in DEVELOPMENT mode with Uvicorn auto-reload.")
    logger.warning("   Do NOT use 'python main.py' in production. Use a proper ASGI server like Uvicorn or Hypercorn managed by a process manager.")
    # Run uvicorn programmatically
    uvicorn.run(
        "main:app", # App instance location (module:variable)
        host="0.0.0.0", # Listen on all available network interfaces
        port=int(os.getenv("PORT", "10000")), # Port from env or default
        reload=True, # Enable auto-reload on code changes
        reload_dirs=[str(BASE_DIR)], # Watch the entire base directory for changes
        log_level="info", # Uvicorn's log level
    )