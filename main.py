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
import re # Needed for requirement parsing
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from contextlib import asynccontextmanager
from urllib.parse import quote

# --- NEW: Boto3 for Backblaze B2 (S3-compatible) ---
try:
    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    logging.warning("boto3 library not found. Backblaze B2 integration will be disabled.")

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

# Third-party for dependency parsing
try:
    import pkg_resources # Used for robust requirement parsing
except ImportError:
    pass

# Ray integration
try:
    import ray
    RAY_AVAILABLE = True
except ImportError as e:
    RAY_AVAILABLE = False
    logging.warning(f" Ray integration disabled: Ray library not found ({e}).")
except Exception as e:
    RAY_AVAILABLE = False
    logging.error(f" Unexpected error importing Ray: {e}", exc_info=True)

# Core application dependencies
try:
    from core_deps import (
        get_current_user,
        get_current_user_or_redirect,
        require_admin,
        get_scoped_db,
        ScopedMongoWrapper
    )
except ImportError as e:
    logging.critical(f" CRITICAL ERROR: Failed to import core dependencies: {e}")
    sys.exit(1)

# Pluggable Authorization imports
try:
    from authz_provider import AuthorizationProvider
    from authz_factory import create_authz_provider
except ImportError as e:
    logging.critical(f" CRITICAL ERROR: Failed to import authorization components: {e}")
    sys.exit(1)

# Index Management import
try:
    from async_mongo_wrapper import AsyncAtlasIndexManager
    INDEX_MANAGER_AVAILABLE = True
except ImportError:
    INDEX_MANAGER_AVAILABLE = False
    logging.warning(" Index Management disabled: 'async_mongo_wrapper.py' not found.")


## Logging Configuration
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("modular_labs.main")


## Global Paths and Configuration Constants
BASE_DIR = Path(__file__).resolve().parent
EXPERIMENTS_DIR = BASE_DIR / "experiments"
TEMPLATES_DIR = BASE_DIR / "templates"

ENABLE_REGISTRATION = os.getenv("ENABLE_REGISTRATION", "true").lower() in {"true", "1", "yes"}
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/")
DB_NAME = "labs_db"
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a_very_insecure_default_dev_secret_123!")
if SECRET_KEY == "a_very_insecure_default_dev_secret_123!":
    logger.critical(" SECURITY WARNING: Using default SECRET_KEY. Set FLASK_SECRET_KEY.")

ADMIN_EMAIL_DEFAULT = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD_DEFAULT = os.getenv("ADMIN_PASSWORD", "password123")
if ADMIN_PASSWORD_DEFAULT == "password123":
    logger.warning(" Using default admin password.")

# --- NEW: Backblaze B2 Configuration ---
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL")        # e.g., "https://s3.us-west-004.backblazeb2.com"
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")
B2_ACCESS_KEY_ID = os.getenv("B2_ACCESS_KEY_ID")
B2_SECRET_ACCESS_KEY = os.getenv("B2_SECRET_ACCESS_KEY")

B2_ENABLED = all([B2_ENDPOINT_URL, B2_BUCKET_NAME, B2_ACCESS_KEY_ID, B2_SECRET_ACCESS_KEY, BOTO3_AVAILABLE])
s3_client = None # Will be initialized in lifespan

if not BOTO3_AVAILABLE:
     logger.critical("boto3 library not installed. B2 features are impossible. pip install boto3")
elif B2_ENABLED:
    logger.info(f"Backblaze B2 integration ENABLED for bucket '{B2_BUCKET_NAME}'.")
else:
    logger.warning("Backblaze B2 integration DISABLED. Missing one or more B2_... env vars.")
    logger.warning("Dynamic experiment uploads via /api/upload-experiment will FAIL.")


## Utility: B2 Presigned URL Generator
def _generate_presigned_download_url(
    s3_client: boto3.client, bucket_name: str, object_key: str
) -> str:
    """
    Generates a secure, time-limited HTTPS URL for Ray's runtime environment download.
    This uses the Boto3 client proven to connect successfully in the driver process.
    """
    return s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket_name, 'Key': object_key},
        ExpiresIn=3600  # URL valid for 1 hour
    )


## Utility: Parse and Merge Requirements for Ray Isolation
def _parse_requirements_file(req_path: Path) -> List[str]:
    if not req_path.is_file():
        return []
    lines = []
    try:
        with req_path.open("r", encoding="utf-8") as rf:
            for raw_line in rf:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                lines.append(line)
    except Exception as e:
        logger.error(f" Failed to read requirements file '{req_path}': {e}")
        return []
    return lines

def _parse_requirements_from_string(content: str) -> List[str]:
    """Reads a requirements.txt file *content* into a list."""
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines

def _extract_pkgname(line: str) -> str:
    line = line.split("#", 1)[0].strip()
    if not line:
        return ""
    if 'pkg_resources' in sys.modules:
        try:
            req = pkg_resources.Requirement.parse(line) # type: ignore
            return req.name.lower()
        except Exception:
            pass
    match = re.match(r"[-e\s]*([\w\-\._]+)", line)
    if match:
        return match.group(1).lower()
    return line.lower()

def _merge_requirements(main_reqs: List[str], local_reqs: List[str]) -> List[str]:
    combined_dict: Dict[str, str] = {}
    for line in main_reqs:
        name = _extract_pkgname(line)
        if name:
            combined_dict[name] = line
    for line in local_reqs:
        name = _extract_pkgname(line)
        if name:
            combined_dict[name] = line
    final_list = sorted(combined_dict.values(), key=lambda x: _extract_pkgname(x))
    return final_list

MASTER_REQUIREMENTS = _parse_requirements_file(BASE_DIR / "requirements.txt")
if MASTER_REQUIREMENTS:
    logger.info(f"Master environment requirements loaded ({len(MASTER_REQUIREMENTS)} lines).")
else:
    logger.info("No top-level requirements.txt found or empty.")


## Jinja2 Template Engine Setup
if not TEMPLATES_DIR.is_dir():
    logger.critical(f" CRITICAL ERROR: Templates directory not found at '{TEMPLATES_DIR}'.")
    templates: Optional[Jinja2Templates] = None
else:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    logger.info(f" Jinja2 templates loaded from '{TEMPLATES_DIR}'")


## FastAPI Application Lifespan (Startup & Shutdown)
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(" Application startup sequence initiated...")
    app.state.experiments = {}
    app.state.ray_is_available = False
    app.state.environment_mode = os.getenv("G_NOME_ENV", "production").lower()
    app.state.templates = templates

    logger.info(f"G_NOME_ENV set to: '{app.state.environment_mode}'")
    
    # --- NEW: Initialize B2 Client ---
    global s3_client
    if B2_ENABLED and not s3_client:
        try:
            s3_client = boto3.client(
                's3',
                endpoint_url=B2_ENDPOINT_URL,
                aws_access_key_id=B2_ACCESS_KEY_ID,
                aws_secret_access_key=B2_SECRET_ACCESS_KEY
            )
            app.state.s3_client = s3_client # Store in state
            logger.info("Backblaze B2 (S3) client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize B2 client during lifespan: {e}")
            app.state.s3_client = None
    elif not B2_ENABLED:
        app.state.s3_client = None
        logger.warning("B2 client not initialized (B2_ENABLED=False).")

    #  Ray Cluster Connection 
    if RAY_AVAILABLE:
        # Use working_dir to send the main app code (for thin clients)
        job_runtime_env: Dict[str, Any] = {"working_dir": str(BASE_DIR)} 
        
        # --- NEW: Pass B2 Credentials to all Ray workers ---
        if B2_ENABLED:
            job_runtime_env["env_vars"] = {
                "AWS_ENDPOINT_URL": B2_ENDPOINT_URL,
                "AWS_ACCESS_KEY_ID": B2_ACCESS_KEY_ID,
                "AWS_SECRET_ACCESS_KEY": B2_SECRET_ACCESS_KEY
            }
            logger.info("Passing B2 (as AWS) credentials to Ray job runtime environment.")
        
        try:
            logger.info("Connecting to Ray cluster (address='auto', namespace='modular_labs')...")
            ray.init(
                address="auto",
                namespace="modular_labs",
                ignore_reinit_error=True,
                runtime_env=job_runtime_env, # Pass runtime with B2 credentials
                log_to_driver=False,
            )
            app.state.ray_is_available = True
            logger.info(" Ray connection successful.")
            try:
                dash_url = ray.get_dashboard_url()
                if dash_url: logger.info(f"Ray Dashboard URL: {dash_url}")
            except Exception: pass
        except Exception as e:
            logger.exception(f" Ray connection failed: {e}. Ray features will be disabled.")
            app.state.ray_is_available = False
    else:
        logger.warning("Ray library not found. Ray integration is disabled.")

    #  MongoDB Connection 
    logger.info(f"Connecting to MongoDB at '{MONGO_URI}'...")
    try:
        client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            appname="ModularLabsAPI"
        )
        await client.admin.command("ping")
        db = client[DB_NAME]
        app.state.mongo_client = client
        app.state.mongo_db = db
        logger.info(f" MongoDB connection successful (Database: '{DB_NAME}').")
    except Exception as e:
        logger.critical(f" CRITICAL ERROR: Failed to connect to MongoDB: {e}", exc_info=True)
        raise RuntimeError(f"MongoDB connection failed: {e}") from e

    #  Pluggable Authorization Provider Initialization 
    AUTHZ_PROVIDER = os.getenv("AUTHZ_PROVIDER", "casbin").lower()
    logger.info(f"Initializing Authorization Provider: '{AUTHZ_PROVIDER}'...")
    provider_settings = { "mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR, }
    try:
        authz_instance = await create_authz_provider(AUTHZ_PROVIDER, provider_settings)
        app.state.authz_provider = authz_instance
        logger.info(f" Authorization Provider '{authz_instance.__class__.__name__}' initialized.")
    except Exception as e:
        logger.critical(f" CRITICAL ERROR: Failed to initialize AuthZ provider '{AUTHZ_PROVIDER}': {e}", exc_info=True)
        raise RuntimeError(f"Authorization provider initialization failed: {e}") from e

    #  Initial Database Setup 
    try:
        await _ensure_db_indices(db)
        await _seed_admin(app)
        await _seed_db_from_local_files(db) # <-- *** THIS IS THE NEW LINE ***
        logger.info(" Essential database setup completed.")
    except Exception as e:
        logger.error(f" Error during initial database setup: {e}", exc_info=True)

    #  Load Initial Active Experiments 
    try:
        await reload_active_experiments(app)
    except Exception as e:
        logger.error(f" Error during initial experiment load: {e}", exc_info=True)

    logger.info(" Application startup sequence complete. Ready to serve requests.")

    try:
        yield # The application runs here
    finally:
        #  Shutdown Sequence 
        logger.info(" Application shutdown sequence initiated...")
        if hasattr(app.state, "mongo_client") and app.state.mongo_client:
            logger.info("Closing MongoDB connection...")
            app.state.mongo_client.close()
        if hasattr(app.state, "ray_is_available") and app.state.ray_is_available:
            logger.info("Shutting down Ray connection...")
            ray.shutdown()
        logger.info(" Application shutdown complete.")


## FastAPI Application Instance
app = FastAPI(
    title="Modular Experiment Labs",
    version="2.1.0-B2", # Version bump for B2 architecture
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.router.redirect_slashes = True


## Middleware Configuration
class ExperimentScopeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ASGIApp):
        request.state.slug_id = None
        request.state.read_scopes = None
        path = request.url.path
        if path.startswith("/experiments/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 2:
                slug = parts[1]
                exp_cfg = getattr(request.app.state, "experiments", {}).get(slug)
                if exp_cfg:
                    request.state.slug_id = slug
                    request.state.read_scopes = exp_cfg.get("resolved_read_scopes", [slug])
        response = await call_next(request)
        return response

app.add_middleware(ExperimentScopeMiddleware)


## Database Helper Functions
async def _ensure_db_indices(db: AsyncIOMotorDatabase):
    try:
        await db.users.create_index("email", unique=True, background=True)
        await db.experiments_config.create_index("slug", unique=True, background=True)
        logger.info(" Core MongoDB indexes ensured (users.email, experiments_config.slug).")
    except Exception as e:
        logger.error(f" Failed to ensure core MongoDB indexes: {e}", exc_info=True)

async def _seed_admin(app: FastAPI):
    db: AsyncIOMotorDatabase = app.state.mongo_db
    authz: Optional[AuthorizationProvider] = getattr(app.state, "authz_provider", None)
    if await db.users.count_documents({"is_admin": True}) > 0:
        logger.info("Admin user already exists. Skipping seeding.")
        return
    logger.warning(" No admin user found. Seeding default administrator...")
    email = ADMIN_EMAIL_DEFAULT
    password = ADMIN_PASSWORD_DEFAULT
    try:
        pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    except Exception as e:
        logger.error(f" Failed to hash default admin password: {e}", exc_info=True)
        return
    try:
        await db.users.insert_one(
            {"email": email, "password_hash": pwd_hash, "is_admin": True, "created_at": datetime.datetime.utcnow()}
        )
        logger.warning(f" Default admin user '{email}' created.")
        logger.warning(" IMPORTANT: Change the default admin password immediately!")
    except Exception as e:
        logger.error(f" Failed to insert default admin user '{email}': {e}", exc_info=True)
        return
    if (authz and hasattr(authz, "add_role_for_user") and
        hasattr(authz, "add_policy") and hasattr(authz, "save_policy")):
        logger.info(f"AuthZ Provider '{authz.__class__.__name__}' supports policy seeding.")
        try:
            await authz.add_role_for_user(email, "admin") # type: ignore
            await authz.add_policy("admin", "admin_panel", "access") # type: ignore
            if asyncio.iscoroutinefunction(authz.save_policy):
                await authz.save_policy() # type: ignore
            else:
                authz.save_policy() # type: ignore
            logger.info(f" Default policies seeded for admin user '{email}'.")
        except Exception as e:
            logger.error(f" Failed to seed default policies: {e}", exc_info=True)
    else:
        logger.warning(f"AuthZ Provider '{authz.__class__.__name__ if authz else 'None'}' does not support auto policy seeding.")

# --- *** NEW FUNCTION *** ---
async def _seed_db_from_local_files(db: AsyncIOMotorDatabase):
    """
    Scans the local EXPERIMENTS_DIR on startup and creates database
    entries for any manifests that don't already exist in the DB.
    This is for development seeding and does NOT overwrite existing DB configs.
    """
    logger.info("Checking for local manifests to seed database...")
    if not EXPERIMENTS_DIR.is_dir():
        logger.warning("Experiments directory not found, skipping local seed.")
        return

    seeded_count = 0
    try:
        for item in EXPERIMENTS_DIR.iterdir():
            # Check if it's a valid experiment directory
            if item.is_dir() and not item.name.startswith(("_", ".")):
                slug = item.name
                manifest_path = item / "manifest.json"
                
                if manifest_path.is_file():
                    # --- THIS IS THE KEY ---
                    # Check if a config for this slug already exists in the DB
                    exists = await db.experiments_config.find_one({"slug": slug})
                    
                    if not exists:
                        # It's not in the DB, so let's seed it from the file.
                        logger.warning(f"[{slug}] No DB config found. Seeding from local 'manifest.json'...")
                        try:
                            with manifest_path.open("r", encoding="utf-8") as f:
                                manifest_data = json.load(f)
                            
                            if not isinstance(manifest_data, dict):
                                logger.error(f"[{slug}] FAILED to seed: manifest.json is not a valid JSON object.")
                                continue
                            
                            # Enforce the slug and set a default status
                            manifest_data["slug"] = slug
                            manifest_data.setdefault("status", "draft") 

                            await db.experiments_config.insert_one(manifest_data)
                            logger.info(f"[{slug}] SUCCESS: Seeded database from local manifest.json.")
                            seeded_count += 1
                            
                        except json.JSONDecodeError:
                            logger.error(f"[{slug}] FAILED to seed: manifest.json is invalid JSON.")
                        except Exception as e:
                            logger.error(f"[{slug}] FAILED to seed: {e}", exc_info=True)
                    else:
                        # It's already in the DB, do nothing.
                        logger.debug(f"[{slug}] Skipping seed: Config already exists in database.")
                
    except OSError as e:
        logger.error(f"Error scanning experiments directory for seeding: {e}")
    
    if seeded_count > 0:
        logger.info(f"Successfully seeded {seeded_count} new experiment(s) from filesystem.")
    else:
        logger.info("No new local manifests found to seed. Database is up-to-date with local files.")


## File System Helper Functions
def _scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    if not dir_path.is_dir():
        return tree
    try:
        for item in sorted(dir_path.iterdir()):
            if item.name in ("__pycache__", ".DS_Store", ".git", ".idea", ".vscode"):
                continue
            relative_path = item.relative_to(base_path)
            if item.is_dir():
                tree.append({
                    "name": item.name, "type": "dir", "path": str(relative_path),
                    "children": _scan_directory(item, base_path)
                })
            else:
                tree.append({"name": item.name, "type": "file", "path": str(relative_path)})
    except OSError as e:
        logger.error(f" Error scanning directory '{dir_path}': {e}")
        tree.append({"name": f"[Error: {e.strerror}]", "type": "error", "path": str(dir_path.relative_to(base_path))})
    return tree

def _secure_path(base_dir: Path, relative_path_str: str) -> Path:
    try:
        normalized_relative = Path(os.path.normpath(relative_path_str))
        if normalized_relative.is_absolute() or str(normalized_relative).startswith(".."):
            raise ValueError("Invalid relative path.")
        absolute_path = (base_dir.resolve() / normalized_relative).resolve()
        if base_dir.resolve() not in absolute_path.parents and absolute_path != base_dir.resolve():
            logger.warning(f"Directory traversal attempt blocked: base='{base_dir}', requested='{relative_path_str}'")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Directory traversal attempt blocked.")
        return absolute_path
    except ValueError:
        logger.warning(f"Invalid path requested: base='{base_dir}', requested='{relative_path_str}'")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path.")
    except Exception as e:
        logger.error(f"Unexpected error resolving path: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing file path.")


## Router: Authentication Routes (/auth)
auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
async def login_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
    if not templates: raise HTTPException(500, "Template engine not available.")
    safe_next = next if next and next.startswith("/") else "/"
    return templates.TemplateResponse("login.html", {
        "request": request, "next": safe_next, "message": message, "error": None,
        "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })
@auth_router.post("/login")
async def login_post(request: Request):
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    form_data = await request.form()
    email = form_data.get("email")
    password = form_data.get("password")
    next_url = form_data.get("next", "/")
    safe_next_url = next_url if next_url and next_url.startswith("/") else "/"
    if not email or not password:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Email and password are required.", "next": safe_next_url, "ENABLE_REGISTRATION": ENABLE_REGISTRATION,}, status_code=status.HTTP_400_BAD_REQUEST)
    user = await db.users.find_one({"email": email})
    if user and bcrypt.checkpw(password.encode("utf-8"), user.get("password_hash", b"")):
        logger.info(f"Successful login for user: {email}")
        payload = {"user_id": str(user["_id"]), "is_admin": user.get("is_admin", False), "email": user.get("email"), "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1),}
        try:
            token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        except Exception as e:
            logger.error(f"JWT encoding failed for user {email}: {e}", exc_info=True)
            raise HTTPException(500, "Login failed due to server error.")
        response = RedirectResponse(safe_next_url, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="token", value=token, httponly=True, secure=(request.url.scheme == "https"), samesite="lax", max_age=60 * 60 * 24,)
        return response
    else:
        logger.warning(f"Failed login attempt for email: {email}")
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password.", "next": safe_next_url, "ENABLE_REGISTRATION": ENABLE_REGISTRATION,}, status_code=status.HTTP_401_UNAUTHORIZED)
@auth_router.get("/logout", name="logout", response_class=RedirectResponse)
async def logout(request: Request):
    token_data = await get_current_user(request.cookies.get("token"))
    user_email = token_data.get("email") if token_data else "Unknown/Expired"
    logger.info(f"User logging out: {user_email}")
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("token")
    return response
if ENABLE_REGISTRATION:
    logger.info("Registration is ENABLED. Adding /register routes.")
    @auth_router.get("/register", response_class=HTMLResponse, name="register_get")
    async def register_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
        if not templates: raise HTTPException(500, "Template engine not available.")
        safe_next = next if next and next.startswith("/") else "/"
        return templates.TemplateResponse("register.html", {"request": request, "next": safe_next, "message": message, "error": None})
    @auth_router.post("/register", name="register_post")
    async def register_post(request: Request):
        if not templates: raise HTTPException(500, "Template engine not available.")
        db: AsyncIOMotorDatabase = request.app.state.mongo_db
        form_data = await request.form()
        email = form_data.get("email")
        password = form_data.get("password")
        password_confirm = form_data.get("password_confirm")
        next_url = form_data.get("next", "/")
        safe_next_url = next_url if next_url and next_url.startswith("/") else "/"
        if not email or not password or not password_confirm:
            return templates.TemplateResponse("register.html", {"request": request, "error": "All fields are required.", "next": safe_next_url}, status_code=status.HTTP_400_BAD_REQUEST)
        if password != password_confirm:
            return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords do not match.", "next": safe_next_url}, status_code=status.HTTP_400_BAD_REQUEST)
        if await db.users.find_one({"email": email}):
            return templates.TemplateResponse("register.html", {"request": request, "error": "An account with this email already exists.", "next": safe_next_url}, status_code=status.HTTP_409_CONFLICT)
        try:
            pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
            new_user = {"email": email, "password_hash": pwd_hash, "is_admin": False, "created_at": datetime.datetime.utcnow(),}
            result = await db.users.insert_one(new_user)
            logger.info(f"New user registered: {email} (ID: {result.inserted_id})")
            payload = {"user_id": str(result.inserted_id), "is_admin": False, "email": email, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1),}
            token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
            response = RedirectResponse(safe_next_url, status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(key="token", value=token, httponly=True, secure=(request.url.scheme == "https"), samesite="lax", max_age=60 * 60 * 24)
            return response
        except Exception as e:
            logger.error(f"Error during registration for {email}: {e}", exc_info=True)
            return templates.TemplateResponse("register.html", {"request": request, "error": "A server error occurred.", "next": safe_next_url}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
else:
    logger.info("Registration is DISABLED. Skipping /register routes.")
app.include_router(auth_router)


## Router: Admin Panel Routes (/admin)
admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin Panel"],
    dependencies=[Depends(require_admin)]
)

@admin_router.get("/", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(request: Request, user: Dict[str, Any] = Depends(require_admin), message: Optional[str] = None):
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    error_message: Optional[str] = None
    
    code_slugs = set()
    if EXPERIMENTS_DIR.is_dir():
        try:
            code_slugs = {
                item.name for item in EXPERIMENTS_DIR.iterdir()
                if item.is_dir() and not item.name.startswith(("_", "."))
            }
        except OSError as e:
            error_message = f"Error reading experiments directory '{EXPERIMENTS_DIR}': {e}"
            logger.error(error_message)
        else:
            error_message = f"Experiments directory missing at '{EXPERIMENTS_DIR}'"
            logger.error(error_message)

    try:
        db_configs_list = await db.experiments_config.find().to_list(length=None)
    except Exception as e:
        error_message = f"Error fetching experiment configs from DB: {e}"
        logger.error(error_message, exc_info=True)
        db_configs_list = []

    db_slug_map = {cfg.get("slug"): cfg for cfg in db_configs_list if cfg.get("slug")}

    configured_experiments: List[Dict[str, Any]] = []
    discovered_slugs: List[str] = []
    orphaned_configs: List[Dict[str, Any]] = []

    for slug in code_slugs:
        if slug in db_slug_map:
            cfg = db_slug_map[slug]
            cfg["code_found"] = True # Local "Thin Client" code is found
            configured_experiments.append(cfg)
        else:
            discovered_slugs.append(slug)

    for cfg in db_configs_list:
        if cfg.get("slug") not in code_slugs:
            cfg["code_found"] = False # Local "Thin Client" code is missing
            orphaned_configs.append(cfg)

    configured_experiments.sort(key=lambda x: x.get("slug", ""))
    discovered_slugs.sort()
    orphaned_configs.sort(key=lambda x: x.get("slug", ""))

    return templates.TemplateResponse("admin/index.html", {
        "request": request,
        "configured": configured_experiments,
        "discovered": discovered_slugs,
        "orphaned": orphaned_configs,
        "message": message,
        "error_message": error_message,
        "current_user": user,
    })

def _get_default_manifest(slug_id: str) -> Dict[str, Any]:
    return {"slug": slug_id, "name": f"Experiment: {slug_id}", "description": "", "status": "draft", "auth_required": False, "data_scope": ["self"], "managed_indexes": {}}

@admin_router.get("/configure/{slug_id}", response_class=HTMLResponse, name="configure_experiment_get")
async def configure_experiment_get(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
    if not templates: raise HTTPException(500, "Template engine not available.")
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    experiment_path = EXPERIMENTS_DIR / slug_id
    manifest_data: Optional[Dict[str, Any]] = None
    manifest_content: str = ""

    # 1. Load from DB (primary source of truth)
    try:
        db_config = await db.experiments_config.find_one({"slug": slug_id})
        if db_config:
            logger.info(f"Loaded manifest for '{slug_id}' from database.")
            db_config.pop("_id", None)
            # Pop B2-specific keys so admin doesn't edit them
            db_config.pop("runtime_s3_uri", None)
            db_config.pop("runtime_pip_deps", None)
            manifest_data = db_config
            manifest_content = json.dumps(manifest_data, indent=2)
        else:
            # 2. If not in DB, create a default
            logger.info(f"No manifest found in DB for '{slug_id}'. Generating default.")
            manifest_data = _get_default_manifest(slug_id)
            manifest_content = json.dumps(manifest_data, indent=2)
    except Exception as e:
        logger.error(f"Error loading manifest for '{slug_id}' from DB: {e}", exc_info=True)
        manifest_data = {"error": f"DB load failed: {e}"}
        manifest_content = json.dumps(manifest_data, indent=2)

    #  File Discovery (shows local "Thin Client" files)
    file_tree = _scan_directory(experiment_path, experiment_path)

    #  Quick Checks
    discovery_info = {
        "has_actor_file": False, "defines_actor_class": False,
        "has_requirements": False, "requirements": [],
        "has_static_dir": False, "has_templates_dir": False,
    }
    if experiment_path.is_dir():
        actor_path = experiment_path / "actor.py"
        discovery_info["has_actor_file"] = actor_path.is_file()
        if discovery_info["has_actor_file"]:
            try:
                with actor_path.open("r", encoding="utf-8") as f: actor_content = f.read()
                discovery_info["defines_actor_class"] = "class ExperimentActor" in actor_content
            except Exception as e: logger.warning(f"Could not read module '{slug_id}/actor.py': {e}")
        reqs_path = experiment_path / "requirements.txt"
        discovery_info["has_requirements"] = reqs_path.is_file()
        if discovery_info["has_requirements"]:
            try: discovery_info["requirements"] = _parse_requirements_file(reqs_path)
            except Exception as e: logger.warning(f"Could not read requirements.txt for '{slug_id}': {e}")
        discovery_info["has_static_dir"] = (experiment_path / "static").is_dir()
        discovery_info["has_templates_dir"] = (experiment_path / "templates").is_dir()

    #  Core Engine Status
    core_info = {
        "ray_available": getattr(request.app.state, "ray_is_available", False),
        "environment_mode": getattr(request.app.state, "environment_mode", "unknown"),
        "isolation_enabled": getattr(request.app.state, "environment_mode", "") == "isolated",
    }
    
    # NEW: Add B2 info to the UI
    core_info["b2_enabled"] = B2_ENABLED
    core_info["b2_bucket"] = B2_BUCKET_NAME

    return templates.TemplateResponse("admin/configure.html", {
        "request": request,
        "slug_id": slug_id,
        "current_user": user,
        "manifest_content": manifest_content,
        "discovery_info": discovery_info,
        "file_tree": file_tree,
        "core_info": core_info,
    })


@admin_router.post("/api/save-manifest/{slug_id}", response_class=JSONResponse)
async def save_manifest(
    request: Request, slug_id: str, data: Dict[str, str] = Body(...),
    user: Dict[str, Any] = Depends(require_admin)
):
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    content = data.get("content")
    if content is None: raise HTTPException(400, "Missing 'content' in request body.")

    message = f"Manifest for '{slug_id}' saved successfully to database."
    try:
        # 1. Validate incoming content as JSON
        new_manifest_data = json.loads(content)
        if not isinstance(new_manifest_data, dict): raise ValueError("Manifest must be a JSON object.")

        # 2. Get current config from DB
        old_config = await db.experiments_config.find_one({"slug": slug_id})
        old_status = old_config.get("status", "draft") if old_config else "draft"
        new_status = new_manifest_data.get("status", "draft")
        
        # 4. If the config already has B2 info, we must *preserve* it.
        if old_config and old_config.get("runtime_s3_uri"):
            config_data = old_config.copy()
            config_data.update(new_manifest_data) # Overwrite with new manifest edits
            config_data["slug"] = slug_id # Ensure slug
            update_doc = {"$set": config_data} # $set the whole merged doc
        else:
            # This is a new config or one without a zip. Just save the manifest.
            config_data = {"slug": slug_id, **new_manifest_data}
            update_doc = {"$set": config_data}

        await db.experiments_config.update_one(
            {"slug": slug_id}, update_doc, upsert=True
        )
        logger.info(f"Upserted experiment config in DB for '{slug_id}'.")

        # 5. Trigger reload if status changed
        if old_status != new_status:
            logger.info(f"Experiment '{slug_id}' status changed: '{old_status}' -> '{new_status}'. Triggering reload.")
            await reload_active_experiments(request.app)
            message = f"Manifest saved. Status changed to '{new_status}', experiments reloaded."

        return JSONResponse({"message": message})

    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON format: {e}")
    except ValueError as e:
        raise HTTPException(400, f"Invalid manifest data: {e}")
    except Exception as e:
        logger.error(f"Unexpected error saving manifest for '{slug_id}': {e}", exc_info=True)
        raise HTTPException(500, f"An unexpected server error occurred: {e}")


@admin_router.post("/api/reload-experiment/{slug_id}", response_class=JSONResponse)
async def reload_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
    admin_email = user.get('email', 'Unknown Admin')
    logger.info(f"Admin '{admin_email}' triggered manual reload via API (context: '{slug_id}').")
    try:
        await reload_active_experiments(request.app)
        return JSONResponse({"message": "Experiment reload triggered successfully. Check server logs for details."})
    except Exception as e:
        logger.error(f"Manual experiment reload failed: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to trigger experiment reload: {e}")


@admin_router.get("/api/index-status/{slug_id}", response_class=JSONResponse)
async def get_index_status(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
    no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    if not INDEX_MANAGER_AVAILABLE:
        return JSONResponse({"error": "Index Management feature is not available."}, status_code=501, headers=no_cache_headers)
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    try:
        config = await db.experiments_config.find_one({"slug": slug_id})
    except Exception as e:
        logger.error(f"Failed to fetch config for '{slug_id}' for index status: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to fetch experiment config."}, status_code=500, headers=no_cache_headers)
    if not config: return JSONResponse([], headers=no_cache_headers)
    managed_indexes: Dict[str, List[Dict]] = config.get("managed_indexes", {})
    if not managed_indexes: return JSONResponse([], headers=no_cache_headers)
    status_list = []
    for collection_base_name, indexes_to_create in managed_indexes.items():
        if not collection_base_name or not isinstance(indexes_to_create, list): continue
        prefixed_collection_name = f"{slug_id}_{collection_base_name}"
        try:
            real_collection = db[prefixed_collection_name]
            index_manager = AsyncAtlasIndexManager(real_collection)
            for index_def in indexes_to_create:
                index_base_name = index_def.get("name")
                index_type = index_def.get("type")
                if not index_base_name or not index_type: continue
                prefixed_index_name = f"{slug_id}_{index_base_name}"
                status_info = {"collection": prefixed_collection_name, "name": prefixed_index_name, "type": index_type, "status": "UNKNOWN", "queryable": False,}
                try:
                    if index_type in ("vectorSearch", "search"):
                        index_data = await index_manager.get_search_index(prefixed_index_name)
                        if index_data:
                            status_info["status"] = index_data.get("status", "UNKNOWN")
                            status_info["queryable"] = index_data.get("queryable", False)
                        else:
                            status_info["status"] = "NOT_FOUND"
                    elif index_type == "regular":
                        index_data = await index_manager.get_index(prefixed_index_name)
                        if index_data:
                            status_info["status"] = "QUERYABLE"
                            status_info["queryable"] = True
                        else:
                            status_info["status"] = "NOT_FOUND"
                    else:
                        status_info["status"] = "INVALID_TYPE"
                except Exception as e:
                    logger.warning(f"Error checking index '{prefixed_index_name}': {e}")
                    status_info["status"] = "CHECK_ERROR"
                status_list.append(status_info)
        except Exception as e:
            logger.error(f"Failed init index manager for '{prefixed_collection_name}': {e}", exc_info=True)
            status_list.append({"collection": prefixed_collection_name, "name": "*", "status": "MANAGER_ERROR", "type": "error", "queryable": False})
    return JSONResponse(status_list, headers=no_cache_headers)

@admin_router.get("/api/get-file-content/{slug_id}", response_class=JSONResponse)
async def get_file_content(slug_id: str, path: str = Query(...), user: Dict[str, Any] = Depends(require_admin)):
    experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
    try:
        file_path = _secure_path(experiment_path, path)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    if not file_path.is_file():
        return JSONResponse({"error": "File not found at specified path."}, status_code=404)
    try:
        with file_path.open("r", encoding="utf-8") as f:
            content = f.read()
        logger.debug(f"Read text content for file: {file_path}")
        return JSONResponse({"content": content, "is_binary": False})
    except UnicodeDecodeError:
        logger.debug(f"File detected as binary: {file_path}")
        return JSONResponse({"content": "[Binary file - Content not displayed]", "is_binary": True})
    except OSError as e:
        logger.error(f"Error reading file '{file_path}': {e}", exc_info=True)
        return JSONResponse({"error": f"Could not read file: {e}"}, status_code=500)
    except Exception as e:
        logger.error(f"Unexpected error reading file '{file_path}': {e}", exc_info=True)
        return JSONResponse({"error": "An unexpected error occurred."}, status_code=500)


@admin_router.post("/api/upload-experiment/{slug_id}", response_class=JSONResponse)
async def upload_experiment_zip(
    request: Request,
    slug_id: str,
    file: UploadFile = File(..., description="Zip file containing the experiment code"),
    user: Dict[str, Any] = Depends(require_admin)
):
    admin_email = user.get('email', 'Unknown Admin')
    logger.info(f"Admin '{admin_email}' initiated zip upload for experiment '{slug_id}'.")
    
    s3: Optional[boto3.client] = getattr(request.app.state, "s3_client", None)
    if not B2_ENABLED or not s3:
        logger.error(f"Upload failed for '{slug_id}': Backblaze B2 integration is not configured or enabled.")
        raise HTTPException(501, "Experiment upload failed: S3/B2 runtime storage is not configured.")

    if file.content_type not in ["application/zip", "application/x-zip-compressed"]:
        raise HTTPException(400, "Invalid file type. Please upload a .zip file.")

    experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
    zip_data: bytes
    parsed_manifest: Dict[str, Any]
    parsed_reqs: List[str]

    # 1. Read and Validate Zip in Memory
    try:
        zip_data = await file.read()
        with io.BytesIO(zip_data) as zip_buffer:
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                namelist = zip_ref.namelist()

                for member in zip_ref.infolist():
                    target_path = (experiment_path / member.filename).resolve()
                    if experiment_path not in target_path.parents and target_path != experiment_path:
                        logger.error(f"SECURITY ALERT: Zip Slip attempt blocked for '{slug_id}'! Member: '{member.filename}'")
                        raise HTTPException(400, f"Invalid zip: Contains path traversal ('{member.filename}').")

                manifest_path_in_zip = next((p for p in namelist if p.endswith("manifest.json") and "MACOSX" not in p), None)
                if not manifest_path_in_zip:
                    raise HTTPException(400, "Invalid zip: Must contain a 'manifest.json' file.")
                
                if not any(p.endswith("actor.py") for p in namelist):
                    raise HTTPException(400, "Invalid zip: Must contain an 'actor.py' file.")
                
                if not any(p.endswith("__init__.py") for p in namelist):
                    raise HTTPException(400, "Invalid zip: Must contain an '__init__.py' (for the Thin Client routes).")

                with zip_ref.open(manifest_path_in_zip) as mf:
                    parsed_manifest = json.load(mf)
                
                reqs_path_in_zip = next((p for p in namelist if p.endswith("requirements.txt") and "MACOSX" not in p), None)
                parsed_reqs = []
                if reqs_path_in_zip:
                    with zip_ref.open(reqs_path_in_zip) as rf:
                        parsed_reqs = _parse_requirements_from_string(rf.read().decode("utf-8"))
                    logger.info(f"[{slug_id}] Found and parsed {len(parsed_reqs)} dependencies from zip.")

    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid or corrupted .zip file.")
    except HTTPException as http_exc:
        raise http_exc # Re-raise validation errors
    except Exception as e:
        logger.error(f"Error during zip pre-check for '{slug_id}': {e}", exc_info=True)
        raise HTTPException(500, f"Error reading or validating zip file: {e}")

    # 2. Upload Runtime Zip to Backblaze B2 & Get Pre-signed HTTPS URL
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    b2_object_key = f"{slug_id}/runtime-{timestamp}.zip"
    b2_final_uri: str
    
    try:
        logger.info(f"[{slug_id}] Uploading runtime zip to B2: '{b2_object_key}'")
        s3.upload_fileobj(
            io.BytesIO(zip_data), # Upload the in-memory data
            B2_BUCKET_NAME,
            b2_object_key
        )
        logger.info(f"[{slug_id}] B2 Upload successful. Generating pre-signed URL...")
        
        # *** FIX: Generate HTTPS URL for Ray to download ***
        b2_final_uri = _generate_presigned_download_url(
            s3, B2_BUCKET_NAME, b2_object_key
        )
        # **************************************************
        
    except ClientError as e:
        logger.error(f"[{slug_id}] FAILED to upload runtime to B2: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to upload experiment runtime to B2: {e}")
    except Exception as e:
        logger.error(f"[{slug_id}] FAILED to upload runtime to B2 (unknown error): {e}", exc_info=True)
        raise HTTPException(500, f"An unexpected error occurred during B2 upload: {e}")

    # 3. Extract "Thin Client" files to local filesystem
    try:
        if experiment_path.exists():
            logger.info(f"[{slug_id}] Deleting existing local directory: {experiment_path}")
            shutil.rmtree(experiment_path)
        
        experiment_path.mkdir(parents=True)
        logger.info(f"[{slug_id}] Extracting local files to: {experiment_path}")
        with io.BytesIO(zip_data) as zip_buffer:
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                zip_ref.extractall(experiment_path)
        logger.info(f"[{slug_id}] Successfully extracted local files.")
    except Exception as e:
        logger.error(f"[{slug_id}] Failed to extract zip file locally: {e}", exc_info=True)
        raise HTTPException(500, f"Error extracting zip file to local app: {e}")

    # 4. Update MongoDB Config
    try:
        db: AsyncIOMotorDatabase = request.app.state.mongo_db
        config_data = {
            **parsed_manifest, # Data from manifest.json
            "slug": slug_id,    # Enforce slug from URL
            "runtime_s3_uri": b2_final_uri, # Add the HTTPS link
            "runtime_pip_deps": parsed_reqs # Add the parsed requirements
        }
        
        await db.experiments_config.update_one(
            {"slug": slug_id},
            {"$set": config_data},
            upsert=True
        )
        logger.info(f"[{slug_id}] Successfully updated config in MongoDB with new B2 runtime URI.")
    except Exception as e:
        logger.error(f"[{slug_id}] FAILED to update config in MongoDB: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to save experiment config to database: {e}")

    # 5. Trigger Reload
    try:
        logger.info(f"Triggering experiment reload after zip upload for '{slug_id}'.")
        await reload_active_experiments(request.app)
        return JSONResponse({
            "message": f"Experiment '{slug_id}' uploaded, extracted, and reloaded successfully."
        })
    except Exception as e:
        logger.error(f"Experiment reload failed after zip upload for '{slug_id}': {e}", exc_info=True)
        raise HTTPException(500, f"Zip extracted, but experiment reload failed: {e}")


app.include_router(admin_router)


## Experiment Loading and Routing Logic
def _create_experiment_proxy_router(slug: str, cfg: Dict[str, Any], exp_path: Path, templates_global: Optional[Jinja2Templates]) -> APIRouter:
    # (This function is unchanged, so it is omitted for brevity)
    pass


async def reload_active_experiments(app: FastAPI):
    db: AsyncIOMotorDatabase = app.state.mongo_db
    logger.info(" Starting reload of active experiments (B2 + Ray Actor Pattern)...")
    try:
        active_cfgs = await db.experiments_config.find({"status": "active"}).to_list(None)
        logger.info(f"Found {len(active_cfgs)} active experiment configurations in database.")
        await _register_experiments(app, active_cfgs, is_reload=True)
        logger.info(" Experiment reload process complete.")
    except Exception as e:
        logger.error(f" Critical error during experiment reload: {e}", exc_info=True)
        app.state.experiments.clear()


async def _register_experiments(
    app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False
):
    if is_reload:
        logger.debug("Clearing previous experiment state...")
        # TODO: Gracefully shut down existing actors and unmount routers
        app.state.experiments.clear()

    if not EXPERIMENTS_DIR.is_dir():
        logger.error(f" Cannot load experiments: Base directory '{EXPERIMENTS_DIR}' not found.")
        return

    env_mode = getattr(app.state, "environment_mode", "production")
    global_templates = getattr(app.state, "templates", None)

    for cfg in active_cfgs:
        slug = cfg.get("slug")
        if not slug:
            logger.warning("Skipping experiment config with missing 'slug'.")
            continue

        logger.debug(f"Registering experiment: '{slug}'")
        exp_path = EXPERIMENTS_DIR / slug # Path to *local* thin client files

        # --- *** "AUTOMAGIC" DEV-MODE UPLOAD (NEW LOGIC) *** ---
        runtime_s3_uri = cfg.get("runtime_s3_uri")
        local_dev_mode = (env_mode != "production") # "isolated" from compose counts as dev

        # --- NEW: Check for automagic upload ---
        if not runtime_s3_uri and local_dev_mode and env_mode == "isolated":
            logger.warning(f"[{slug}] ISOLATED DEV MODE: No 'runtime_s3_uri' found. Attempting 'automagic' upload from '{exp_path}'...")
            
            s3: Optional[boto3.client] = getattr(app.state, "s3_client", None)
            db: AsyncIOMotorDatabase = app.state.mongo_db
            
            if not B2_ENABLED or not s3:
                logger.error(f"[{slug}] FAILED automagic upload: B2/S3 client is not available. Actor will fail.")
                continue # Skip this experiment
            
            if not exp_path.is_dir():
                     logger.error(f"[{slug}] FAILED automagic upload: Local path '{exp_path}' not found. Actor will fail.")
                     continue

            try:
                # 1. Zip the local directory in-memory
                logger.debug(f"[{slug}] Zipping local directory...")
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_f:
                    for file_path in exp_path.rglob('*'):
                        if '__pycache__' in str(file_path) or '.git' in str(file_path):
                            continue
                        archive_path = file_path.relative_to(exp_path)
                        zip_f.write(file_path, archive_path)
                zip_data = zip_buffer.getvalue()

                # 2. Upload to B2
                timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                b2_object_key = f"{slug}/runtime-dev-automagic-{timestamp}.zip"
                
                s3.upload_fileobj(io.BytesIO(zip_data), B2_BUCKET_NAME, b2_object_key)
                
                # *** FIX: Generate HTTPS URL for Ray to download ***
                b2_final_uri = _generate_presigned_download_url(
                    s3, B2_BUCKET_NAME, b2_object_key
                )
                # **************************************************
                
                # 3. Parse local requirements
                local_reqs_path = exp_path / "requirements.txt"
                parsed_reqs = _parse_requirements_file(local_reqs_path)
                
                # 4. Update the DB
                await db.experiments_config.update_one(
                    {"slug": slug},
                    {"$set": {
                        "runtime_s3_uri": b2_final_uri,
                        "runtime_pip_deps": parsed_reqs
                    }}
                )
                logger.info(f"[{slug}] Automagic upload successful. DB updated with '{b2_final_uri}' and {len(parsed_reqs)} deps.")
                
                # 5. Update local state FOR THIS RUN
                cfg["runtime_s3_uri"] = b2_final_uri
                cfg["runtime_pip_deps"] = parsed_reqs
                runtime_s3_uri = b2_final_uri # This is the key
                runtime_pip_deps = parsed_reqs

            except Exception as e:
                logger.error(f"[{slug}] FAILED 'automagic' upload: {e}", exc_info=True)
                continue # Skip
        # --- END OF NEW BLOCK ---

        # --- Now, the original logic continues, but the automagic upload ---
        # --- has populated the variables it needs. ---
        actor_runtime_env: Dict[str, Any] = {} 
        runtime_pip_deps = cfg.get("runtime_pip_deps", []) # Get the (potentially new) deps

        if runtime_s3_uri:
            # --- S3 PATH (Now catches automagic uploads) ---
            # NOTE: runtime_s3_uri is now an HTTPS link!
            actor_runtime_env["py_modules"] = [runtime_s3_uri]
            
            if env_mode == "isolated" and runtime_pip_deps:
                actor_runtime_env["pip"] = runtime_pip_deps
                logger.info(f"[{slug}] Configuring ISOLATED runtime with {len(runtime_pip_deps)} pip deps from S3.")
            elif env_mode == "isolated":
                logger.info(f"[{slug}] Configuring ISOLATED runtime with no extra pip deps from S3.")
            else:
                logger.info(f"[{slug}] Configuring SHARED runtime from S3. 'pip' isolation is disabled.")
                if runtime_pip_deps:
                    logger.warning(f"[{slug}] Experiment has pip deps, but mode is SHARED. Deps will be ignored!")

        elif not runtime_s3_uri and local_dev_mode:
            # --- LOCAL DEVELOPMENT "MAGICAL" PATH (NOW ONLY FOR 'production' env_mode) ---
            # This block will now only be hit if env_mode is "production" (dev) or "development"
            # AND runtime_s3_uri is still None.
            logger.info(f"[{slug}] No 'runtime_s3_uri' found. Using LOCAL path for SHARED DEV mode (inheriting from job env).")
            if runtime_pip_deps:
                logger.warning(f"[{slug}] LOCAL DEV mode: 'runtime_pip_deps' in manifest are IGNORED. Actor will use main venv.")

        elif not runtime_s3_uri and not local_dev_mode:
            # --- PRODUCTION ERROR PATH ---
            logger.error(f"[{slug}] Skipping (PRODUCTION): Experiment is 'active' but has no 'runtime_s3_uri'. Upload required.")
            continue
            
        # We still need the local "thin client" files to exist for routes
        if not exp_path.is_dir():
            logger.warning(f"[{slug}] Skipping: Local 'Thin Client' directory not found at '{exp_path}'.")
            continue
            
        # --- (End of modified logic block) ---

        #  Data Scoping 
        read_scopes = [slug if s == "self" else s for s in cfg.get("data_scope", ["self"])]
        cfg["resolved_read_scopes"] = read_scopes
        logger.debug(f"[{slug}] Read scopes resolved to: {read_scopes}")

        #  Static Files Mounting (from local unpack)
        static_dir = exp_path / "static"
        if static_dir.is_dir():
            mount_path = f"/experiments/{slug}/static"
            mount_name = f"experiment_{slug}_static"
            if not any(route.path == mount_path for route in app.routes if hasattr(route, "path")):
                try:
                    app.mount(mount_path, StaticFiles(directory=str(static_dir)), name=mount_name)
                    logger.debug(f"[{slug}] Mounted static directory at '{mount_path}'.")
                except Exception as e:
                    logger.error(f"[{slug}] Failed to mount static directory '{static_dir}': {e}", exc_info=True)
                else:
                    logger.debug(f"[{slug}] Static mount '{mount_path}' already exists.")

        #  Automatic Index Management (Unchanged)
        if INDEX_MANAGER_AVAILABLE and "managed_indexes" in cfg:
            db: AsyncIOMotorDatabase = app.state.mongo_db
            managed_indexes: Dict[str, List[Dict]] = cfg.get("managed_indexes", {})
            logger.debug(f"[{slug}] Processing {len(managed_indexes)} managed collection(s) for indexes.")
            for collection_base_name, indexes_to_create in managed_indexes.items():
                if not collection_base_name or not isinstance(indexes_to_create, list):
                    logger.warning(f"[{slug}] Invalid 'managed_indexes' format for key '{collection_base_name}'. Skipping.")
                    continue
                prefixed_collection_name = f"{slug}_{collection_base_name}"
                prefixed_index_defs = []
                for index_def in indexes_to_create:
                    index_base_name = index_def.get("name")
                    if not index_base_name or not index_def.get("type"):
                        logger.warning(f"[{slug} -> {prefixed_collection_name}] Skipping index definition missing 'name' or 'type'.")
                        continue
                    prefixed_index_def = index_def.copy()
                    prefixed_index_def["name"] = f"{slug}_{index_base_name}"
                    prefixed_index_defs.append(prefixed_index_def)
                if not prefixed_index_defs:
                    continue
                logger.info(f"[{slug}] Scheduling index management for collection '{prefixed_collection_name}' ({len(prefixed_index_defs)} indexes).")
                asyncio.create_task(
                    _run_index_creation_for_collection(
                        db=db, slug=slug, collection_name=prefixed_collection_name,
                        index_definitions=prefixed_index_defs
                    )
                )
        elif "managed_indexes" in cfg:
            logger.warning(f"[{slug}] Manifest contains 'managed_indexes' but Index Manager is not available.")

        #  Ray Actor Initialization (Using B2 URI)
        if not getattr(app.state, "ray_is_available", False):
            logger.warning(f"[{slug}] Ray is not available. Skipping registration.")
            continue

        # 1. Load the ExperimentActor class *definition*
        actor_cls = None
        try:
            actor_mod_name = f"experiments.{slug.replace('-', '_')}.actor"
            if actor_mod_name in sys.modules and is_reload:
                actor_mod = importlib.reload(sys.modules[actor_mod_name])
            else:
                actor_mod = importlib.import_module(actor_mod_name)
            
            if hasattr(actor_mod, "ExperimentActor"):
                actor_cls = getattr(actor_mod, "ExperimentActor")
            else:
                logger.warning(f"[{slug}] 'actor.py' (local) does not define 'ExperimentActor'. Skipping.")
                continue
        except ModuleNotFoundError:
             logger.warning(f"[{slug}] No local 'actor.py' found (for class definition). Skipping.")
             continue
        except Exception as e:
            logger.error(f" Error loading local actor module for '{slug}': {e}", exc_info=True)
            continue

        # 2. Configure Runtime Environment
        #     This section is now handled by the "automagic" block above.
        logger.info(f"[{slug}] Configured actor runtime: {actor_runtime_env}")

        # 3. Start the Ray Actor
        actor_name = f"{slug}-actor"
        try:
            actor_handle = actor_cls.options(
                name=actor_name, namespace="modular_labs",
                lifetime="detached", get_if_exists=True,
                runtime_env=actor_runtime_env, # This is now valid!
                # NOTE: Added max_restarts for fault tolerance against init crashes
                max_restarts=-1,
            ).remote( # Pass initialization arguments to the actor's __init__
                mongo_uri=MONGO_URI, db_name=DB_NAME,
                write_scope=slug, read_scopes=read_scopes,
            )
            logger.info(f" [{slug}] Ray Actor '{actor_name}' started/connected in {env_mode.upper()} mode.")
        except Exception as e:
            logger.error(f" [{slug}] FAILED to start Ray Actor '{actor_name}': {e}", exc_info=True)
            continue

        #  FastAPI Proxy Router Inclusion 
        try:
            init_mod_name = f"experiments.{slug.replace('-', '_')}"
            init_mod = None
            if init_mod_name in sys.modules and is_reload:
                init_mod = importlib.reload(sys.modules[init_mod_name])
            else:
                init_mod = importlib.import_module(init_mod_name)
            
            if not hasattr(init_mod, "bp"):
                logger.error(f"[{slug}] FAILED: Local 'experiments/{slug}/__init__.py' has no 'bp' variable. Skipping.")
                continue

            proxy_router = getattr(init_mod, "bp")
            if not isinstance(proxy_router, APIRouter):
                logger.error(f"[{slug}] FAILED: Local 'bp' variable is not an APIRouter. Skipping.")
                continue
            
            logger.info(f"[{slug}] Successfully loaded 'bp' (Thin Client Router) from local __init__.py.")

        except ModuleNotFoundError:
            logger.warning(f"[{slug}] No local '__init__.py' file found. Cannot load routes. Skipping.")
            continue
        except Exception as e:
            logger.error(f" Error loading local __init__.py module for '{slug}': {e}", exc_info=True)
            continue

        #  Mount Experiment Router 
        deps = []
        if cfg.get("auth_required"):
            deps = [Depends(get_current_user_or_redirect)]
        else:
            deps = [Depends(get_current_user)]
            
        prefix = f"/experiments/{slug}"
        app.include_router(
            proxy_router,
            prefix=prefix,
            tags=[f"Experiment: {slug}"],
            dependencies=deps,
        )

        cfg["url"] = prefix
        app.state.experiments[slug] = cfg
        logger.info(f" Registered Experiment: '{slug}' (Auth: {cfg.get('auth_required', False)}) at '{prefix}'")
        

# --- NEW: Refactored Index Creation to prevent race conditions ---
async def _run_index_creation_for_collection(
    db: AsyncIOMotorDatabase,
    slug: str,
    collection_name: str,
    index_definitions: List[Dict[str, Any]]
):
    """
    Background task to ensure ALL indexes for a single collection exist.
    This runs *serially* for each index to avoid race conditions.
    """
    log_prefix = f"[{slug} -> {collection_name}]"
    try:
        real_collection = db[collection_name]
        index_manager = AsyncAtlasIndexManager(real_collection)
        logger.info(f"{log_prefix} Starting index management task for {len(index_definitions)} indexes.")
    except Exception as e:
        logger.error(f"{log_prefix} FAILED to initialize Index Manager: {e}", exc_info=True)
        return

    # Process each index one-by-one
    for index_def in index_definitions:
        index_name = index_def.get("name") # This is the *prefixed* name
        index_type = index_def.get("type")

        try:
            if index_type == "regular":
                keys = index_def.get("keys")
                options = {**index_def.get("options", {}), "name": index_name}
                if not keys:
                    logger.warning(f"{log_prefix} Skipping regular index '{index_name}': 'keys' missing.")
                    continue

                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    temp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
                    key_doc = {k: v for k, v in temp_keys}
                    if existing_index.get("key") != key_doc:
                        logger.warning(f"{log_prefix} Index '{index_name}' exists with different keys. Dropping and recreating.")
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Regular index '{index_name}' already exists and matches keys.")
                        continue
                
                logger.info(f"{log_prefix} Ensuring regular index '{index_name}' exists...")
                await index_manager.create_index(keys, **options)
                logger.info(f"{log_prefix} Successfully ensured regular index '{index_name}'.")

            elif index_type in ("vectorSearch", "search"):
                definition = index_def.get("definition")
                if not definition:
                    logger.warning(f"{log_prefix} Skipping search index '{index_name}': 'definition' missing.")
                    continue

                existing_index = await index_manager.get_search_index(index_name)
                if existing_index:
                    current_def = existing_index.get("latestDefinition", existing_index.get("definition"))
                    if current_def == definition:
                        logger.info(f"{log_prefix} Search index '{index_name}' exists and definition matches. Checking status...")
                        if not existing_index.get("queryable") and existing_index.get("status") != "FAILED":
                            logger.info(f"{log_prefix} Index '{index_name}' is not queryable (Status: {existing_index.get('status', 'UNKNOWN')}). Waiting...")
                            await index_manager._wait_for_search_index_ready(index_name, index_manager.DEFAULT_SEARCH_TIMEOUT)
                            logger.info(f"{log_prefix} Index '{index_name}' is now ready.")
                        elif existing_index.get("status") == "FAILED":
                            logger.error(f"{log_prefix} Index '{index_name}' is in FAILED state. Manual intervention required.")
                        else:
                            logger.info(f"{log_prefix} Index '{index_name}' is ready and queryable.")
                        continue
                    else:
                        logger.warning(f"{log_prefix} Search index '{index_name}' exists but definition differs. Submitting update...")
                        await index_manager.update_search_index(
                            name=index_name, definition=definition, wait_for_ready=True
                        )
                        logger.info(f"{log_prefix} Successfully updated search index '{index_name}'.")
                else:
                    logger.info(f"{log_prefix} Search index '{index_name}' not found. Creating new index...")
                    await index_manager.create_search_index(
                        name=index_name, definition=definition, index_type=index_type,
                        wait_for_ready=True
                    )
                    logger.info(f"{log_prefix} Successfully created new search index '{index_name}'.")
            else:
                logger.warning(f"{log_prefix} Skipping index '{index_name}': Unknown type '{index_type}'.")

        except Exception as e:
            logger.error(f"{log_prefix} FAILED to manage index '{index_name}': {e}", exc_info=True)
            continue # Continue to the next index in the list


## Root Route (/)
@app.get("/", response_class=HTMLResponse, name="home")
async def root(request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)):
    if not templates: raise HTTPException(500, "Template engine not available.")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "experiments": getattr(request.app.state, "experiments", {}),
        "current_user": user,
        "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })


## Development Server Entry Point
if __name__ == "__main__":
    import uvicorn
    logger.warning(" Starting application in DEVELOPMENT mode with Uvicorn auto-reload.")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=True,
        reload_dirs=[str(BASE_DIR)],
        log_level="info",
    )