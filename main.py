#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import fnmatch
import io
import re # Needed for requirement parsing
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import quote

# Attempt to see if PyMongo is installed (for fallback logic in ray_actor)
try:
 import pymongo # or from async_mongo_wrapper import ScopedMongoWrapper
 HAVE_PYMONGO = True
except ImportError:
 HAVE_PYMONGO = False

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


#############################################
# NEW: Ray Actor Decorator With DB Fallback #
#############################################
def ray_actor(
 name: str = None,
 namespace: str = "modular_labs",
 lifetime: str = "detached",
 max_restarts: int = -1,
 get_if_exists: bool = True,
 fallback_if_no_db: bool = True,
):
 """
 A decorator that transforms a normal class into a Ray actor.
 If 'fallback_if_no_db' is True and we detect PyMongo is missing,
 we automatically pass 'use_in_memory_fallback=True' to the actor
 constructor, letting the actor skip real DB usage.
 """

 def decorator(user_class):
  # Convert user_class => Ray-remote class
  ray_remote_cls = ray.remote(user_class)

  @classmethod
  def spawn(cls, *args, runtime_env=None, **kwargs):
   # Decide actor_name
   actor_name = name if name else f"{user_class.__name__}_actor"
   # If fallback requested and no PyMongo, pass "use_in_memory_fallback"
   if fallback_if_no_db and not HAVE_PYMONGO:
    kwargs["use_in_memory_fallback"] = True

   return cls.options(
    name=actor_name,
    namespace=namespace,
    lifetime=lifetime,
    max_restarts=max_restarts,
    get_if_exists=get_if_exists,
    runtime_env=runtime_env or {},
   ).remote(*args, **kwargs)

  setattr(ray_remote_cls, "spawn", spawn)
  return ray_remote_cls

 return decorator


# ============================
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
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL") # e.g., "https://s3.us-west-004.backblazeb2.com"
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
def _generate_presigned_download_url(s3_client: boto3.client, bucket_name: str, object_key: str) -> str:
 """
 Generates a secure, time-limited HTTPS URL for Ray's runtime environment download.
 This uses the Boto3 client proven to connect successfully in the driver process.
 """
 return s3_client.generate_presigned_url(
  'get_object',
  Params={'Bucket': bucket_name, 'Key': object_key},
  ExpiresIn=3600 # URL valid for 1 hour
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

 # Ray Cluster Connection
 if RAY_AVAILABLE:
   # Get the address without a default value
   RAY_CONNECTION_ADDRESS = os.getenv("RAY_ADDRESS")

   job_runtime_env: Dict[str, Any] = {"working_dir": str(BASE_DIR)}

   if B2_ENABLED:
    # Pass B2 keys as environment variables for Ray to use in its actors/workers
    job_runtime_env["env_vars"] = {
      "AWS_ENDPOINT_URL": B2_ENDPOINT_URL,
      "AWS_ACCESS_KEY_ID": B2_ACCESS_KEY_ID,
      "AWS_SECRET_ACCESS_KEY": B2_SECRET_ACCESS_KEY
    }
    logger.info("Passing B2 (as AWS) credentials to Ray job runtime environment.")

   try:
     if RAY_CONNECTION_ADDRESS:
       # Explicit connection attempt to an external or specific cluster
       logger.info(f"Connecting to Ray cluster (address='{RAY_CONNECTION_ADDRESS}', namespace='modular_labs')...")
       ray.init(
         address=RAY_CONNECTION_ADDRESS,
         namespace="modular_labs",
         ignore_reinit_error=True,
         runtime_env=job_runtime_env,
         log_to_driver=False,
 m.
         num_cpus=2, # <-- Limit Ray's core usage
         object_store_memory=2_000_000_000
       )
       connect_mode = f"EXTERNAL ({RAY_CONNECTION_ADDRESS})"
     else:
       # Start a new, local Ray runtime within the container
       logger.info("Starting a new LOCAL Ray cluster instance inside the container...")
       ray.init(
         namespace="modular_labs",
         ignore_reinit_error=True,
         runtime_env=job_runtime_env,
         log_to_driver=False,
         num_cpus=2, # <-- Limit Ray's core usage
         object_store_memory=2_000_000_000
       )
       connect_mode = "LOCAL INSTANCE"
      
     app.state.ray_is_available = True
     logger.info(f" Ray connection successful (Mode: {connect_mode}).")
    
     try:
      dash_url = ray.get_dashboard_url()
      if dash_url:
       logger.info(f"Ray Dashboard URL: {dash_url}")
     except Exception:
      pass
     
   except Exception as e:
    _http.
     logger.exception(f" Ray connection failed: {e}. Ray features will be disabled.")
     app.state.ray_is_available = False
 else:
   logger.warning("Ray library not found. Ray integration is disabled.")

 # MongoDB Connection
 logger.info(f"Connecting to MongoDB at '{MONGO_URI}'...")
 try:
  client = AsyncIOMotorClient(
   MONGO_URI,
   serverSelectionTimeoutMS=5000,
 _async.
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

 # Pluggable Authorization Provider Initialization
 AUTHZ_PROVIDER = os.getenv("AUTHZ_PROVIDER", "casbin").lower()
Example.
 logger.info(f"Initializing Authorization Provider: '{AUTHZ_PROVIDER}'...")
 provider_settings = {"mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR}
 try:
  authz_instance = await create_authz_provider(AUTHZ_PROVIDER, provider_settings)
  app.state.authz_provider = authz_instance
  logger.info(f" Authorization Provider '{authz_instance.__class__.__name__}' initialized.")
.
 except Exception as e:
  logger.critical(f" CRITICAL ERROR: Failed to initialize AuthZ provider '{AUTHZ_PROVIDER}': {e}", exc_info=True)
  raise RuntimeError(f"Authorization provider initialization failed: {e}") from e

 # Initial Database Setup
 try:
  await _ensure_db_indices(db)
  await _seed_admin(app)
  await _seed_db_from_local_files(db)
  logger.info(" Essential database setup completed.")
 except Exception as e:
  logger.error(f" Error during initial database setup: {e}", exc_info=True)
.
 # Load Initial Active Experiments
 try:
  await reload_active_experiments(app)
 except Exception as e:
  logger.error(f" Error during initial experiment load: {e}", exc_info=True)

 logger.info(" Application startup sequence complete. Ready to serve requests.")
 try:
  yield # The application runs here
 finally:
  logger.info(" Application shutdown sequence initiated...")
  if hasattr(app.state, "mongo_client") and app.state.mongo_client:
   logger.info("Closing MongoDB connection...")
   app.state.mongo_client.close()

  if hasattr(app.state, "ray_is_available") and app.state.ray_is_available:
   logger.info("Shutting down Ray connection...")
   ray.shutdown()

  logger.info(" Application shutdown complete.")


app = FastAPI(
 title="Modular Experiment Labs",
 version="2.1.0-B2", # Example version
.
 docs_url="/api/docs",
 redoc_url="/api/redoc",
 openapi_url="/api/openapi.json",
 lifespan=lifespan,
)
app.router.redirect_slashes = True


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
 
  # Define a reasonable timeout for DB operations
  DB_TIMEOUT = 15.0

  # 1) Fetch all users who have is_admin=True
  try:
    logger.debug("Fetching admin users from DB...")
    admin_users: List[Dict[str, Any]] = await asyncio.wait_for(
      db.users.find({"is_admin": True}).to_list(length=None),
      timeout=DB_TIMEOUT
    )
    logger.debug(f"Found {len(admin_users)} admin user(s).")
   
  except asyncio.TimeoutError:
    logger.critical(f"CRITICAL: Timed out after {DB_TIMEOUT}s while fetching admin users.")
    logger.critical("This indicates a severe MongoDB connection issue. Aborting startup.")
    raise # Stop the application lifespan
  except Exception as e:
    logger.critical(f"CRITICAL: Failed to fetch admin users: {e}", exc_info=True)
    raise # Stop the application lifespan


  # --------------------------------------------------
  # Case 1: No admin users exist. Create the default one.
  # --------------------------------------------------
  if not admin_users:
    logger.warning(" No admin user found. Seeding default administrator...")
    email = ADMIN_EMAIL_DEFAULT
    password = ADMIN_PASSWORD_DEFAULT

    try:
      pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    except Exception as e:
      logger.error(f" Failed to hash default admin password: {e}", exc_info=True)
      return # Don't proceed if hashing fails

    try:
      # Insert the new user
      await asyncio.wait_for(
        db.users.insert_one({
          "email": email,
          "password_hash": pwd_hash,
          "is_admin": True,
          "created_at": datetime.datetime.utcnow(),
        }),
        timeout=DB_TIMEOUT
      )
      logger.warning(f" Default admin user '{email}' created.")
      logger.warning(" IMPORTANT: Change the default admin password immediately!")
     
      # Now, seed the policies for this new user
      if (
        authz
        and hasattr(authz, "add_role_for_user")
        and hasattr(authz, "add_policy")
        and hasattr(authz, "save_policy")
      ):
        logger.info(f"Seeding default Casbin policies for new admin '{email}'...")
        await asyncio.wait_for(authz.add_role_for_user(email, "admin"), timeout=DB_TIMEOUT)
        await asyncio.wait_for(authz.add_policy("admin", "admin_panel", "access"), timeout=DB_TIMEOUT)
warning.
       
        save_op = authz.save_policy()
        if asyncio.iscoroutine(save_op):
          await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
       
        logger.info(f" Default policies seeded for admin user '{email}'.")
      else:
        logger.warning("AuthZ Provider not available or does not support auto policy seeding.")

    except asyncio.TimeoutError:
      logger.critical(f"CRITICAL: Timed out seeding default admin user '{email}'.")
      raise
    except Exception as e:
      logger.error(f" Failed to insert/seed default admin user '{email}': {e}", exc_info=True)
      # If seeding fails, we should probably stop.
      raise
.
  # --------------------------------------------------
  # Case 2: Admin users already exist. Sync policies.
  # --------------------------------------------------
  else:
    logger.info(
      f"Found {len(admin_users)} admin user(s) already in DB. "
      "Verifying Casbin roles + policies..."
    )
    if not (
      authz
      and hasattr(authz, "add_role_for_user")
      and hasattr(authz, "add_policy")
      and hasattr(authz, "save_policy")
      and hasattr(authz, "has_policy")   # <-- Check for existence
      and hasattr(authz, "has_role_for_user") # <-- Check for existence
I/O.
    ):
      logger.warning(
        "AuthZ Provider not available or does not support idempotent policy seeding. "
        "Existing admin user(s) may not have Casbin roles!"
      )
      return # Continue startup, but warn

    try:
      made_changes = False
     
      # 1. Check if the "admin" role has "admin_panel:access" policy
      logger.debug("Verifying 'admin_panel' policy...")
      policy_exists = await asyncio.wait_for(
        authz.has_policy("admin", "admin_panel", "access"),
.
        timeout=DB_TIMEOUT
      )
     
      if not policy_exists:
        logger.info("Admin policy 'admin_panel:access' missing. Adding...")
        await asyncio.wait_for(
          authz.add_policy("admin", "admin_panel", "access"),
          timeout=DB_TIMEOUT
        )
        made_changes = True
      else:
        logger.debug("Admin policy 'admin_panel:access' already exists.")

      # 2. Check each admin user for the "admin" role
      for user_doc in admin_users:
        email = user_doc["email"]
        logger.debug(f"Verifying 'admin' role for user '{email}'...")
        has_role = await asyncio.wait_for(
          authz.has_role_for_user(email, "admin"),
.
          timeout=DB_TIMEOUT
        )
       
        if not has_role:
          logger.info(f"User '{email}' is admin but missing Casbin role. Adding...")
          await asyncio.wait_for(
            authz.add_role_for_user(email, "admin"),
            timeout=DB_TIMEOUT
          )
          made_changes = True
        else:
          logger.debug(f"User '{email}' already has admin role.")

section.
      # 3. Save policies ONLY if we made a change
      if made_changes:
        logger.info("Policy changes were made. Saving Casbin policies...")
        save_op = authz.save_policy()
        if asyncio.iscoroutine(save_op):
          await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
        logger.info("Casbin policies saved successfully.")
      else:
        logger.info("All Casbin admin policies are already in sync. No changes needed.")
.
    except asyncio.TimeoutError:
      logger.critical(f"CRITICAL: Timed out after {DB_TIMEOUT}s during Casbin policy sync.")
      logger.critical("This is likely a MongoDB connection/firewall issue. Aborting startup.")
      raise
    except Exception as e:
      logger.critical(f"CRITICAL: Failed to sync admin roles/policies: {e}", exc_info=True)
      raise

async def _seed_db_from_local_files(db: AsyncIOMotorDatabase):
 logger.info("Checking for local manifests to seed database...")
 if not EXPERIMENTS_DIR.is_dir():
  logger.warning(f"Experiments directory '{EXPERIMENTS_DIR}' not found, skipping local seed.")
  return

 seeded_count = 0
 try:
  for item in EXPERIMENTS_DIR.iterdir():
   if item.is_dir() and not item.name.startswith(("_", ".")):
.
    slug = item.name
    manifest_path = item / "manifest.json"

    if manifest_path.is_file():
     exists = await db.experiments_config.find_one({"slug": slug})
     if not exists:
      logger.warning(f"[{slug}] No DB config found. Seeding from local 'manifest.json'...")
      try:
       with manifest_path.open("r", encoding="utf-8") as f:
        manifest_data = json.load(f)
       if not isinstance(manifest_data, dict):
        logger.error(f"[{slug}] FAILED to seed: manifest.json is not valid JSON object.")
        continue
       manifest_data["slug"] = slug
       manifest_data.setdefault("status", "draft")

section.
       await db.experiments_config.insert_one(manifest_data)
       logger.info(f"[{slug}] SUCCESS: Seeded DB from local manifest.json.")
       seeded_count += 1
      except json.JSONDecodeError:
I/O.
       logger.error(f"[{slug}] FAILED to seed: manifest.json is invalid JSON.")
      except Exception as e:
       logger.error(f"[{slug}] FAILED to seed: {e}", exc_info=True)
     else:
      logger.debug(f"[{slug}] Skipping seed: DB config already exists.")
 except OSError as e:
  logger.error(f"Error scanning experiments directory for seeding: {e}")

 if seeded_count > 0:
  logger.info(f"Successfully seeded {seeded_count} new experiment(s) from filesystem.")
 else:
  logger.info("No new local manifests found to seed. Database is up-to-date.")


def _scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
 tree: List[Dict[str, Any]] = []
 if not dir_path.is_dir():
  return tree
.
 try:
  for item in sorted(dir_path.iterdir()):
   if item.name in ("__pycache__", ".DS_Store", ".git", ".idea", ".vscode"):
    continue
   relative_path = item.relative_to(base_path)
   if item.is_dir():
    tree.append({
     "name": item.name,
     "type": "dir",
     "path": str(relative_path),
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
section.
  return absolute_path
 except ValueError:
  logger.warning(f"Invalid path requested: base='{base_dir}', requested='{relative_path_str}'")
  raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path.")
 except Exception as e:
  logger.error(f"Unexpected error resolving path: {e}", exc_info=True)
  raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing file path.")


auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
async def login_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
 if not templates:
  raise HTTPException(500, "Template engine not available.")
 safe_next = next if next and next.startswith("/") else "/"
 return templates.TemplateResponse("login.html", {
  "request": request,
  "next": safe_next,
.
  "message": message,
  "error": None,
  "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
 })


@auth_router.post("/login")
async def login_post(request: Request):
 if not templates:
  raise HTTPException(500, "Template engine not available.")
 db: AsyncIOMotorDatabase = request.app.state.mongo_db
 form_data = await request.form()
 email = form_data.get("email")
 password = form_data.get("password")
 next_url = form_data.get("next", "/")
 safe_next_url = next_url if next_url and next_url.startswith("/") else "/"

 if not email or not password:
  return templates.TemplateResponse("login.html", {
   "request": request,
   "error": "Email and password are required.",
   "next": safe_next_url,
.
   "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
  }, status_code=status.HTTP_400_BAD_REQUEST)

 user = await db.users.find_one({"email": email})
 if user and bcrypt.checkpw(password.encode("utf-8"), user.get("password_hash", b"")):
  logger.info(f"Successful login for user: {email}")
  payload = {
   "user_id": str(user["_id"]),
   "is_admin": user.get("is_admin", False),
   "email": user.get("email"),
   "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1),
  }
  try:
   token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
  except Exception as e:
   logger.error(f"JWT encoding failed for user {email}: {e}", exc_info=True)
   raise HTTPException(500, "Login failed due to server error.")

  response = RedirectResponse(safe_next_url, status_code=status.HTTP_303_SEE_OTHER)
.
  response.set_cookie(
   key="token",
   value=token,
   httponly=True,
   secure=(request.url.scheme == "https"),
   samesite="lax",
   max_age=60 * 60 * 24,
  )
  return response
 else:
  logger.warning(f"Failed login attempt for email: {email}")
  return templates.TemplateResponse("login.html", {
   "request": request,
   "error": "Invalid email or password.",
   "next": safe_next_url,
   "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
  }, status_code=status.HTTP_401_UNAUTHORIZED)
.

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
  if not templates:
   raise HTTPException(500, "Template engine not available.")
  safe_next = next if next and next.startswith("/") else "/"
  return templates.TemplateResponse("register.html", {
   "request": request,
   "next": safe_next,
.
   "message": message,
   "error": None
  })

 @auth_router.post("/register", name="register_post")
 async def register_post(request: Request):
  if not templates:
   raise HTTPException(500, "Template engine not available.")
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  form_data = await request.form()
  email = form_data.get("email")
  password = form_data.get("password")
  password_confirm = form_data.get("password_confirm")
  next_url = form_data.get("next", "/")
  safe_next_url = next_url if next_url and next_url.startswith("/") else "/"

  if not email or not password or not password_confirm:
.
   return templates.TemplateResponse("register.html", {
    "request": request,
    "error": "All fields are required.",
    "next": safe_next_url
   }, status_code=status.HTTP_400_BAD_REQUEST)

  if password != password_confirm:
   return templates.TemplateResponse("register.html", {
    "request": request,
    "error": "Passwords do not match.",
    "next": safe_next_url
   }, status_code=status.HTTP_400_BAD_REQUEST)

  if await db.users.find_one({"email": email}):
   return templates.TemplateResponse("register.html", {
    "request": request,
    "error": "An account with this email already exists.",
    "next": safe_next_url
.
   }, status_code=status.HTTP_409_CONFLICT)

  try:
   pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
   new_user = {
    "email": email,
    "password_hash": pwd_hash,
    "is_admin": False,
    "created_at": datetime.datetime.utcnow(),
M.
   }
   result = await db.users.insert_one(new_user)
   logger.info(f"New user registered: {email} (ID: {result.inserted_id})")

   payload = {
    "user_id": str(result.inserted_id),
    "is_admin": False,
    "email": email,
    "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1),
   }
   token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
.
   response = RedirectResponse(safe_next_url, status_code=status.HTTP_303_SEE_OTHER)
   response.set_cookie(
    key="token",
    value=token,
    httponly=True,
    secure=(request.url.scheme == "https"),
    samesite="lax",
    max_age=60 * 60 * 24
   )
   return response
  except Exception as e:
   logger.error(f"Error during registration for {email}: {e}", exc_info=True)
section.
   return templates.TemplateResponse("register.html", {
    "request": request,
    "error": "A server error occurred.",
    "next": safe_next_url
   }, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
else:
 logger.info("Registration is DISABLED. Skipping /register routes.")

app.include_router(auth_router)


admin_router = APIRouter(
 prefix="/admin",
 tags=["Admin Panel"],
 dependencies=[Depends(require_admin)]
)

# ##########################################################################
# --- NEW: STANDALONE DOCKER EXPORT FUNCTIONS ---
# ##########################################################################

async def _dump_db_for_export(db: AsyncIOMotorDatabase, slug_id: str) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
 """
 Dumps the experiment config and all associated collection data into
 Python dictionaries, ready for JSON serialization.
 """
 # 1. Get the experiment config document
 config_doc = await db.experiments_config.find_one({"slug": slug_id})
 if not config_doc:
  raise ValueError(f"No experiment config found for slug '{slug_id}'")
 
 config_data = dict(config_doc)
 if "_id" in config_data:
  config_data["_id"] = str(config_data["_id"]) # Convert ObjectId

 # 2. Find all collections associated with this experiment
 sub_collections = []
 all_coll_names = await db.list_collection_names()
 for cname in all_coll_names:
  # We find collections that START with the slug prefix
  if cname.startswith(f"{slug_id}_"):
   sub_collections.append(cname)

 # 3. Dump all documents from each associated collection
 collections_data: Dict[str, List[Dict[str, Any]]] = {}
 for coll_name in sub_collections:
  docs_list = []
  cursor = db[coll_name].find()
  async for doc in cursor:
   doc_dict = dict(doc)
   if "_id" in doc_dict:
    doc_dict["_id"] = str(doc_dict["_id"]) # Convert ObjectId
   docs_list.append(doc_dict)
  # Use the full prefixed name as the key
  collections_data[coll_name] = docs_list
  logger.info(f"[_dump_db_for_export] Dumped {len(docs_list)} docs from '{coll_name}'")

 return config_data, collections_data

def _get_standalone_docker_compose() -> str:
 """Generates the content for the docker-compose.yml file."""
 return """
version: '3.8'

services:
 app:
  build:
   context: ./app
  ports:
   - "8000:8000"
  volumes:
   - ./data:/app/data
   - ./app:/app
  depends_on:
   - mongo
  environment:
   - MONGO_URI=mongodb://mongo:27017/
   - DB_NAME=standalone_db
   - FLASK_SECRET_KEY=a_secure_standalone_secret_key
   - ENABLE_REGISTRATION=false
   - ADMIN_EMAIL=admin@example.com
   - ADMIN_PASSWORD=admin
  command: /app/docker-entrypoint.sh

 mongo:
  image: mongo:5.0
  volumes:
   - mongo_data:/data/db
  ports:
   - "27017:27017"

volumes:
 mongo_data:
"""

def _get_standalone_dockerfile() -> str:
 """Generates the content for the Dockerfile."""
 return """
FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
# - curl and gnupg are needed to add mongo-tools repo
# - procps is needed for `ps` (used by wait-for-it)
# - mongo-tools contains `mongoimport`
RUN apt-get update && apt-get install -y --no-install-recommends \
  curl \
  gnupg \
  procps \
  && \
  curl -fsSL https://www.mongodb.org/static/pgp/server-5.0.asc | apt-key add - && \
  echo "deb [ arch=amd64,arm64 ] https://repo.mongodb.org/apt/debian buster/mongodb-org/5.0 main" | tee /etc/apt/sources.list.d/mongodb-org-5.0.list && \
  apt-get update && \
  apt-get install -y --no-install-recommends mongodb-org-tools && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entrypoint script and make it executable
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Copy the application code
# The docker-compose.yml will mount the code for development,
# but we COPY it here for a production build.
COPY . .
"""

def _get_standalone_entrypoint(config_file: str, data_files: List[str]) -> str:
 """
 Generates the docker-entrypoint.sh script.
 This script waits for Mongo, then imports all data from the /app/data dir.
 """
 import_commands = []
 # 1. Import the experiment config
 import_commands.append(
  f'echo "Importing config from {config_file}..."'
 )
 import_commands.append(
  f'mongoimport --uri="$MONGO_URI" --db="$DB_NAME" --collection="experiments_config" --type=json --file="/app/data/{config_file}" --jsonArray --drop'
 )
 
 # 2. Import all data collections
 for data_file in data_files:
  # The data_file is the full prefixed name, e.g., "data_imaging_workouts.json"
  # The collection name is the part before ".json"
  collection_name = data_file.replace(".json", "")
  import_commands.append(
   f'echo "Importing data from {data_file} into {collection_name}..."'
  )
  import_commands.append(
   f'mongoimport --uri="$MONGO_URI" --db="$DB_NAME" --collection="{collection_name}" --type=json --file="/app/data/{data_file}" --jsonArray --drop'
  )
 
 all_imports = "\n".join(import_commands)

 return f"""
#!/bin/bash
set -e

# Wait for MongoDB to be ready
echo "Waiting for MongoDB at $MONGO_URI..."
until mongo --host mongo --eval "print(\\"MongoDB is up!\\")" > /dev/null 2>&1; do
 echo " - Still waiting..."
 sleep 1
done
echo "MongoDB is ready. Starting data import..."

# --- Import Data ---
{all_imports}
# --- End Import ---

echo "Data import complete. Starting application server..."

# Execute the main container command (uvicorn)
exec uvicorn main:app --host 0.0.0.0 --port 8000
"""

def _get_standalone_readme() -> str:
 """Generates a new README for the Docker package."""
 return """
# Standalone Experiment Package (Docker)

This package contains everything needed to run the experiment
in a self-contained Docker environment using `docker-compose`.

## Requirements

- Docker
- Docker Compose (v1 or v2)

## How to Run

1.  **Environment Setup**
  
  This package uses a `.env` file for configuration. You can
  create one by copying the example:

  ```bash
  cp .env.example .env
  ```
  
  You can modify the `ADMIN_EMAIL` and `ADMIN_PASSWORD` in
  the `.env` file if you wish.

2.  **Build and Run**

  Use `docker-compose` to build and start the services:

  ```bash
  docker-compose up --build
  ```

3.  **Initialization**

  On the first run, the `app` service will:
  a. Wait for the `mongo` service to be ready.
  b. Automatically import all experiment data from the `/data`
   directory into the `mongo` service.
  c. Start the FastAPI application server.

4.  **Access**

  Once the server is running, you can access the experiment at:
  
  **http://localhost:8000/**

  You can log in using the credentials from your `.env` file
  (default: `admin@example.com` / `admin`).
"""

def _get_standalone_env_example(slug_id: str) -> str:
 """Generates a .env.example file."""
 return f"""
# --- Standalone Experiment Environment ---
# This file is used by docker-compose.yml

# MongoDB settings
MONGO_URI=mongodb://mongo:27017/
DB_NAME=standalone_db

# Application settings
FLASK_SECRET_KEY=a_very_secure_standalone_secret_key_123!
EXPERIMENT_SLUG_ID={slug_id}

# Default admin user to create
ENABLE_REGISTRATION=false
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin
"""

def _generate_standalone_main_py(slug_id: str) -> str:
 """
 Generates the content for the `standalone_main.py`.
 This is a lightweight version of the main platform, which only
 loads the *single* exported experiment.
 """
 # We replace '-' with '_' for valid module import names
 slug_module_name = slug_id.replace('-', '_')
 
 return f"""
import os
import sys
import logging
import importlib
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping, Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# --- Import Core Dependencies ---
# These files (core_deps.py, etc.) must be in the same directory
try:
  from core_deps import (
    get_current_user,
    get_current_user_or_redirect,
    require_admin,
    get_scoped_db
  )
  from authz_provider import AuthorizationProvider
  from authz_factory import create_authz_provider
  from async_mongo_wrapper import AsyncAtlasIndexManager
except ImportError as e:
  print(f"CRITICAL: Failed to import core dependencies: {e}")
  sys.exit(1)

# --- Configuration ---
logging.basicConfig(
  level=os.getenv("LOG_LEVEL", "INFO").upper(),
  format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
  datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("standalone_app")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
EXPERIMENTS_DIR = BASE_DIR / "experiments"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/")
DB_NAME = os.getenv("DB_NAME", "standalone_db")
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "standalone_secret")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin")
EXPERIMENT_SLUG_ID = "{slug_id}"

# --- ExperimentScopeMiddleware (simplified) ---
class ExperimentScopeMiddleware(BaseHTTPMiddleware):
  async def dispatch(self, request: Request, call_next: ASGIApp):
    request.state.slug_id = EXPERIMENT_SLUG_ID
    # In standalone, the experiment can read its own data
    request.state.read_scopes = [EXPERIMENT_SLUG_ID]
    response = await call_next(request)
    return response

# --- Lifespan (Startup/Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
  logger.info("Standalone app startup...")
  app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

  # Connect to MongoDB
  try:
    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    await client.admin.command("ping")
    db = client[DB_NAME]
    app.state.mongo_client = client
    app.state.mongo_db = db
    logger.info(f"MongoDB connection successful (DB: '{DB_NAME}').")
  except Exception as e:
    logger.critical(f"CRITICAL: Failed to connect to MongoDB: {e}", exc_info=True)
    raise RuntimeError(f"MongoDB connection failed: {e}") from e

  # Init AuthZ provider
  try:
    provider_settings = {{"mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR}}
    authz_instance = await create_authz_provider("casbin", provider_settings)
    app.state.authz_provider = authz_instance
    logger.info("AuthZ provider initialized.")
  except Exception as e:
    logger.critical(f"CRITICAL: Failed to initialize AuthZ provider: {e}", exc_info=True)
    raise RuntimeError(f"AuthZ provider failed: {e}") from e

  # Seed Admin User (simplified, no bcrypt for standalone)
  try:
    import bcrypt # Import here
    admin = await db.users.find_one({{"email": ADMIN_EMAIL}})
    if not admin:
      logger.warning(f"No admin user found. Seeding '{ADMIN_EMAIL}'...")
      pwd_hash = bcrypt.hashpw(ADMIN_PASS.encode("utf-8"), bcrypt.gensalt())
      await db.users.insert_one({{"email": ADMIN_EMAIL, "password_hash": pwd_hash, "is_admin": True}})
      await authz_instance.add_role_for_user(ADMIN_EMAIL, "admin")
      await authz_instance.add_policy("admin", "admin_panel", "access")
      save_op = authz_instance.save_policy()
      if asyncio.iscoroutine(save_op):
        await save_op
      logger.info(f"Default admin '{ADMIN_EMAIL}' created.")
  except Exception as e:
    logger.error(f"Failed to seed admin user: {e}", exc_info=True)

  # Load the *one* experiment
  try:
    await _load_standalone_experiment(app, EXPERIMENT_SLUG_ID)
  except Exception as e:
    logger.critical(f"Failed to load experiment '{EXPERIMENT_SLUG_ID}': {e}", exc_info=True)
  
  logger.info("Standalone app startup complete.")
  try:
    yield
  finally:
    logger.info("Standalone app shutdown...")
    if hasattr(app.state, "mongo_client"):
      app.state.mongo_client.close()

async def _load_standalone_experiment(app: FastAPI, slug: str):
  """Loads the single experiment's router and static files."""
  logger.info(f"Loading standalone experiment: '{slug}'")
  exp_path = EXPERIMENTS_DIR / slug
  
  # 1. Mount Static Files
  static_dir = exp_path / "static"
  if static_dir.is_dir():
    mount_path = f"/experiments/{slug}/static"
    app.mount(mount_path, StaticFiles(directory=str(static_dir)), name=f"exp_{slug}_static")
    logger.info(f"Mounted static files at '{mount_path}'")
  
  # 2. Load and include the router
  init_mod_name = f"experiments.{slug_module_name}"
  try:
    init_mod = importlib.import_module(init_mod_name)
    if not hasattr(init_mod, "bp"):
      logger.error(f"'__init__.py' has no 'bp' (APIRouter).")
      return
    
    router = getattr(init_mod, "bp")
    
    # Note: We are not including auth dependencies here,
    # as the experiment's router might have different needs.
    # This assumes the lightweight server doesn't need auth,
    # or the experiment handles it.
    # For simplicity, let's assume no auth for the standalone.
    prefix = f"/experiments/{slug}"
    app.include_router(router, prefix=prefix, tags=[f"Experiment: {slug}"])
    logger.info(f"Experiment router mounted at '{prefix}'")

  except ModuleNotFoundError:
    logger.critical(f"Could not find experiment module: '{init_mod_name}'")
  except Exception as e:
    logger.critical(f"Error loading experiment router: {e}", exc_info=True)

# --- Create FastAPI App ---
app = FastAPI(
  title=f"Standalone Experiment: {EXPERIMENT_SLUG_ID}",
  lifespan=lifespan
)
app.add_middleware(ExperimentScopeMiddleware)

# --- Root Redirect ---
@app.get("/", response_class=RedirectResponse)
async def root():
  # Redirect root to the experiment's gallery page
  return RedirectResponse(url=f"/experiments/{EXPERIMENT_SLUG_ID}/")

# Note: We are *not* including the auth_router or admin_router
# to keep this server lightweight and focused *only* on the experiment.
# A real implementation might want to include auth.
"""

def _get_merged_requirements(exp_path: Path) -> List[str]:
 """Helper to merge global and local requirements.txt."""
 global MASTER_REQUIREMENTS
 local_reqs_path = exp_path / "requirements.txt"
 local_reqs = []
 if local_reqs_path.is_file():
  local_reqs = _parse_requirements_file(local_reqs_path)
 
 # Merge, removing duplicates (local wins)
 combined_dict: Dict[str, str] = {}
 for line in MASTER_REQUIREMENTS:
  name = _extract_pkgname(line)
  if name:
   combined_dict[name] = line
 for line in local_reqs:
  name = _extract_pkgname(line)
  if name:
   combined_dict[name] = line
 
 final_list = sorted(combined_dict.values(), key=lambda x: _extract_pkgname(x))
 
 # Ensure minimal dependencies are present
 minimal_deps = {"fastapi", "uvicorn", "motor", "pymongo", "bcrypt", "pyjwt", "python-multipart"}
 for dep in minimal_deps:
  if dep not in combined_dict:
   final_list.append(dep)
   
 return final_list

def _copy_platform_files_to_zip(zip_file: zipfile.ZipFile, base_dir: Path, zip_prefix: str):
 """Copies essential platform .py files to the zip."""
 platform_files = [
  "core_deps.py",
  "authz_provider.py",
  "authz_factory.py",
  "async_mongo_wrapper.py"
 ]
 for file_name in platform_files:
  file_path = base_dir / file_name
  if file_path.is_file():
   zip_file.write(file_path, arcname=f"{zip_prefix}/{file_name}")
  else:
   logger.warning(f"Required platform file not found: {file_name}")

def _copy_dir_to_zip(zip_file: zipfile.ZipFile, dir_path: Path, zip_prefix: str):
 """Recursively copies a directory into a zip file."""
 EXCLUSION_PATTERNS = ["__pycache__", ".DS_Store", "*.pyc"]
 
 for item in dir_path.rglob('*'):
  if any(fnmatch.fnmatch(item.name, p) for p in EXCLUSION_PATTERNS):
   continue
  if item.is_file():
   # Calculate the relative path to be used inside the zip
   relative_path = item.relative_to(dir_path.parent)
   zip_file.write(item, arcname=f"{zip_prefix}/{relative_path}")


async def _create_standalone_docker_package(
 slug_id: str,
 db: AsyncIOMotorDatabase
) -> io.BytesIO:
 """
 Creates the full standalone Docker package in an in-memory zip file.
 """
 logger.info(f"Starting Docker package creation for '{slug_id}'.")
 zip_buffer = io.BytesIO()
 
 # 1. Dump database content
 try:
  config_data, collections_data = await _dump_db_for_export(db, slug_id)
  logger.info(f"Database dump complete. Found {len(collections_data)} data collections.")
 except Exception as e:
  logger.error(f"Failed to dump database for export: {e}", exc_info=True)
  raise
 
 # 2. Generate file contents
 exp_path = EXPERIMENTS_DIR / slug_id
 merged_reqs_list = _get_merged_requirements(exp_path)
 merged_reqs_content = "\n".join(merged_reqs_list)
 
 config_json_file = "config.json"
 data_json_files = [f"{coll_name}.json" for coll_name in collections_data.keys()]
 
 docker_compose_content = _get_standalone_docker_compose()
 dockerfile_content = _get_standalone_dockerfile()
 entrypoint_content = _get_standalone_entrypoint(config_json_file, data_json_files)
 readme_content = _get_standalone_readme()
 env_example_content = _get_standalone_env_example(slug_id)
 standalone_main_content = _generate_standalone_main_py(slug_id)
 
 # 3. Create ZIP archive
 with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
  # --- Write generated config files ---
  zf.writestr("docker-compose.yml", docker_compose_content)
  zf.writestr("README.md", readme_content)
  zf.writestr(".env.example", env_example_content)
  
  # --- Write app files ---
  zf.writestr("app/Dockerfile", dockerfile_content)
  zf.writestr("app/docker-entrypoint.sh", entrypoint_content)
  zf.writestr("app/main.py", standalone_main_content)
  zf.writestr("app/requirements.txt", merged_reqs_content)
  
  # --- Write core platform code to app/ ---
  _copy_platform_files_to_zip(zf, BASE_DIR, "app")
  
  # --- Write experiment-specific code to app/experiments/<slug_id> ---
  if exp_path.is_dir():
   _copy_dir_to_zip(zf, exp_path, f"app/experiments")
  else:
   logger.error(f"Experiment directory '{exp_path}' not found! Package may be broken.")
   
  # --- Write templates to app/templates ---
  # The standalone app needs the *global* templates (login.html, etc.)
  if TEMPLATES_DIR.is_dir():
   _copy_dir_to_zip(zf, TEMPLATES_DIR, "app/templates")
  else:
   logger.error(f"Global templates directory '{TEMPLATES_DIR}' not found!")

  # --- Write data dump files ---
  zf.writestr(f"data/{config_json_file}", json.dumps([config_data], indent=2))
  for coll_name, docs in collections_data.items():
   zf.writestr(f"data/{coll_name}.json", json.dumps(docs, indent=2))
  
 zip_buffer.seek(0)
 logger.info(f"Standalone Docker package created successfully for '{slug_id}'.")
 return zip_buffer


# ##########################################################################
# --- END: STANDALONE DOCKER EXPORT FUNCTIONS ---
# ##########################################################################


# -----------------------------------------------------
# Public Standalone Export Endpoint (UPDATED)
# -----------------------------------------------------
public_api_router = APIRouter(prefix="/api", tags=["Public API"])

@public_api_router.get("/package-standalone/{slug_id}", name="package_standalone")
async def package_standalone_experiment(
 request: Request,
 slug_id: str,
 user: Optional[Mapping[str, Any]] = Depends(get_current_user) # Allows unauthenticated access
):
 db: AsyncIOMotorDatabase = request.app.state.mongo_db

 # 1. Check Experiment Configuration
 config = await db.experiments_config.find_one({"slug": slug_id})
 if not config or config.get("status") != "active":
  raise HTTPException(status_code=404, detail="Experiment not found or not active.")

 auth_required = config.get("auth_required", False)

 # 2. Enforce Authentication if required
 if auth_required:
  if not user:
   current_path = quote(request.url.path)
   login_url = request.url_for("login_get", next=current_path)
   response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
   return response

 # 3. Perform Packaging (UPDATED)
 try:
  # Use the new Docker packaging function
  zip_buffer = await _create_standalone_docker_package(slug_id, db)

  file_name = f"{slug_id}_standalone_docker_{datetime.datetime.now().strftime('%Y%m%d')}.zip"
  response = FastAPIResponse(
   content=zip_buffer.getvalue(),
   media_type="application/zip",
   headers={
    "Content-Disposition": f'attachment; filename="{file_name}"',
    "Content-Length": str(zip_buffer.getbuffer().nbytes),
   }
  )
  user_email = user.get('email', 'Guest') if user else 'Guest'
  logger.info(f"Sending standalone DOCKER package for '{slug_id}' (User: {user_email}, Auth Required: {auth_required}).")
  return response
 except ValueError as e:
  logger.error(f"Error packaging experiment '{slug_id}': {e}")
  raise HTTPException(status_code=404, detail=str(e))
 except Exception as e:
  logger.error(f"Unexpected error packaging '{slug_id}': {e}", exc_info=True)
  raise HTTPException(500, "Unexpected server error during packaging.")


app.include_router(public_api_router)
# -----------------------------------------------------


@admin_router.get("/package/{slug_id}", name="package_experiment")
async def package_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
 """
 Admin-only package export. (UPDATED)
 """
 db: AsyncIOMotorDatabase = request.app.state.mongo_db
 try:
  # Use the new Docker packaging function
  zip_buffer = await _create_standalone_docker_package(slug_id, db)
  
  file_name = f"{slug_id}_standalone_docker_{datetime.datetime.now().strftime('%Y%m%d')}.zip"
  response = FastAPIResponse(
   content=zip_buffer.getvalue(),
   media_type="application/zip",
   headers={
    "Content-Disposition": f'attachment; filename="{file_name}"',
    "Content-Length": str(zip_buffer.getbuffer().nbytes),
   }
  )
  logger.info(f"Sending standalone DOCKER package for ADMIN '{user.get('email', 'Unknown')}' - '{slug_id}'.")
  return response
 except ValueError as e:
  logger.error(f"Error packaging experiment '{slug_id}': {e}")
  raise HTTPException(status_code=404, detail=str(e))
 except Exception as e:
  logger.error(f"Unexpected error packaging '{slug_id}': {e}", exc_info=True)
  raise HTTPException(500, "Unexpected server error during packaging.")


@admin_router.get("/", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(request: Request, user: Dict[str, Any] = Depends(require_admin), message: Optional[str] = None):
 if not templates:
  raise HTTPException(500, "Template engine not available.")
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
   cfg["code_found"] = True
   configured_experiments.append(cfg)
  else:
   discovered_slugs.append(slug)

 for cfg in db_configs_list:
  if cfg.get("slug") not in code_slugs:
   cfg["code_found"] = False
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
 return {
  "slug": slug_id,
  "name": f"Experiment: {slug_id}",
  "description": "",
  "status": "draft",
  "auth_required": False,
  "data_scope": ["self"],
  "managed_indexes": {}
 }


@admin_router.get("/configure/{slug_id}", response_class=HTMLResponse, name="configure_experiment_get")
async def configure_experiment_get(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
 if not templates:
  raise HTTPException(500, "Template engine not available.")
 db: AsyncIOMotorDatabase = request.app.state.mongo_db
 experiment_path = EXPERIMENTS_DIR / slug_id
 manifest_data: Optional[Dict[str, Any]] = None
 manifest_content: str = ""

 try:
  db_config = await db.experiments_config.find_one({"slug": slug_id})
  if db_config:
   db_config.pop("_id", None)
   db_config.pop("runtime_s3_uri", None)
   db_config.pop("runtime_pip_deps", None)
   manifest_data = db_config
   manifest_content = json.dumps(manifest_data, indent=2)
  else:
   logger.info(f"No manifest found in DB for '{slug_id}'. Using default template.")
   manifest_data = _get_default_manifest(slug_id)
   manifest_content = json.dumps(manifest_data, indent=2)
 except Exception as e:
  logger.error(f"Error loading manifest for '{slug_id}' from DB: {e}", exc_info=True)
  manifest_data = {"error": f"DB load failed: {e}"}
  manifest_content = json.dumps(manifest_data, indent=2)

 file_tree = _scan_directory(experiment_path, experiment_path)

 discovery_info = {
  "has_actor_file": False,
  "defines_actor_class": False,
  "has_requirements": False,
  "requirements": [],
  "has_static_dir": False,
  "has_templates_dir": False,
 }
 if experiment_path.is_dir():
  actor_path = experiment_path / "actor.py"
  discovery_info["has_actor_file"] = actor_path.is_file()
  if discovery_info["has_actor_file"]:
   try:
    with actor_path.open("r", encoding="utf-8") as f:
     actor_content = f.read()
    discovery_info["defines_actor_class"] = "class ExperimentActor" in actor_content
   except Exception as e:
    logger.warning(f"Could not read actor.py: {e}")

  reqs_path = experiment_path / "requirements.txt"
  discovery_info["has_requirements"] = reqs_path.is_file()
  if discovery_info["has_requirements"]:
   try:
    discovery_info["requirements"] = _parse_requirements_file(reqs_path)
   except Exception as e:
    logger.warning(f"Could not read requirements.txt for '{slug_id}': {e}")

  discovery_info["has_static_dir"] = (experiment_path / "static").is_dir()
  discovery_info["has_templates_dir"] = (experiment_path / "templates").is_dir()

 core_info = {
  "ray_available": getattr(request.app.state, "ray_is_available", False),
  "environment_mode": getattr(request.app.state, "environment_mode", "unknown"),
  "isolation_enabled": getattr(request.app.state, "environment_mode", "") == "isolated",
  "b2_enabled": B2_ENABLED,
  "b2_bucket": B2_BUCKET_NAME,
 }

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
 request: Request,
 slug_id: str,
 data: Dict[str, str] = Body(...),
 user: Dict[str, Any] = Depends(require_admin)
):
 db: AsyncIOMotorDatabase = request.app.state.mongo_db
 content = data.get("content")
 if content is None:
  raise HTTPException(400, "Missing 'content' in request body.")

 message = f"Manifest for '{slug_id}' saved successfully."
 try:
  new_manifest_data = json.loads(content)
  if not isinstance(new_manifest_data, dict):
   raise ValueError("Manifest must be a JSON object.")

  old_config = await db.experiments_config.find_one({"slug": slug_id})
  old_status = old_config.get("status", "draft") if old_config else "draft"
  new_status = new_manifest_data.get("status", "draft")

  if old_config and old_config.get("runtime_s3_uri"):
   config_data = old_config.copy()
   config_data.update(new_manifest_data)
   config_data["slug"] = slug_id
   update_doc = {"$set": config_data}
  else:
   config_data = {"slug": slug_id, **new_manifest_data}
   update_doc = {"$set": config_data}

  await db.experiments_config.update_one({"slug": slug_id}, update_doc, upsert=True)
  logger.info(f"Upserted experiment config in DB for '{slug_id}'.")

  if old_status != new_status:
   logger.info(f"Experiment '{slug_id}' status changed from '{old_status}' to '{new_status}'. Reloading...")
   await reload_active_experiments(request.app)
   message = f"Manifest saved. Status changed to '{new_status}', experiments reloaded."

  return JSONResponse({"message": message})
 except json.JSONDecodeError as e:
  raise HTTPException(400, f"Invalid JSON format: {e}")
 except ValueError as e:
  raise HTTPException(400, f"Invalid manifest data: {e}")
 except Exception as e:
  logger.error(f"Error saving manifest for '{slug_id}': {e}", exc_info=True)
  raise HTTPException(500, f"Server error: {e}")


@admin_router.post("/api/reload-experiment/{slug_id}", response_class=JSONResponse)
async def reload_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
 admin_email = user.get('email', 'Unknown Admin')
 logger.info(f"Admin '{admin_email}' triggered manual reload for '{slug_id}'.")
 try:
  await reload_active_experiments(request.app)
  return JSONResponse({"message": "Experiment reload triggered successfully."})
 except Exception as e:
  logger.error(f"Manual experiment reload failed: {e}", exc_info=True)
  raise HTTPException(500, f"Reload failed: {e}")


@admin_router.get("/api/index-status/{slug_id}", response_class=JSONResponse)
async def get_index_status(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
 no_cache_headers = {
  "Cache-Control": "no-cache, no-store, must-revalidate",
  "Pragma": "no-cache",
  "Expires": "0"
 }
 if not INDEX_MANAGER_AVAILABLE:
  return JSONResponse({"error": "Index Management not available."}, status_code=501, headers=no_cache_headers)

 db: AsyncIOMotorDatabase = request.app.state.mongo_db
 try:
  config = await db.experiments_config.find_one({"slug": slug_id})
 except Exception as e:
  logger.error(f"Failed to fetch config for '{slug_id}': {e}", exc_info=True)
  return JSONResponse({"error": "Failed to fetch config."}, status_code=500, headers=no_cache_headers)

 if not config:
  return JSONResponse([], headers=no_cache_headers)

 managed_indexes: Dict[str, List[Dict]] = config.get("managed_indexes", {})
 if not managed_indexes:
  return JSONResponse([], headers=no_cache_headers)

 status_list = []
 for collection_base_name, indexes_to_create in managed_indexes.items():
  if not collection_base_name or not isinstance(indexes_to_create, list):
   continue
  prefixed_collection_name = f"{slug_id}_{collection_base_name}"
  try:
   real_collection = db[prefixed_collection_name]
   index_manager = AsyncAtlasIndexManager(real_collection)
   for index_def in indexes_to_create:
    index_base_name = index_def.get("name")
    index_type = index_def.get("type")
    if not index_base_name or not index_type:
     continue
    prefixed_index_name = f"{slug_id}_{index_base_name}"
    status_info = {
     "collection": prefixed_collection_name,
     "name": prefixed_index_name,
     "type": index_type,
     "status": "UNKNOWN",
     "queryable": False,
    }
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
   status_list.append({
    "collection": prefixed_collection_name,
    "name": "*",
    "status": "MANAGER_ERROR",
    "type": "error",
    "queryable": False
   })

 return JSONResponse(status_list, headers=no_cache_headers)


@admin_router.get("/api/get-file-content/{slug_id}", response_class=JSONResponse)
async def get_file_content(slug_id: str, path: str = Query(...), user: Dict[str, Any] = Depends(require_admin)):
 experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
 try:
  file_path = _secure_path(experiment_path, path)
 except HTTPException as e:
  return JSONResponse({"error": e.detail}, status_code=e.status_code)

 if not file_path.is_file():
  return JSONResponse({"error": "File not found."}, status_code=404)

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
 file: UploadFile = File(...),
 user: Dict[str, Any] = Depends(require_admin)
):
 admin_email = user.get('email', 'Unknown Admin')
 logger.info(f"Admin '{admin_email}' initiated zip upload for '{slug_id}'.")

 s3: Optional[boto3.client] = getattr(request.app.state, "s3_client", None)
 if not B2_ENABLED or not s3:
  logger.error(f"Cannot upload '{slug_id}': B2 not configured or no s3 client.")
  raise HTTPException(501, "S3/B2 not configured; upload impossible.")

 if file.content_type not in ("application/zip", "application/x-zip-compressed"):
  raise HTTPException(400, "Invalid file type; must be .zip.")

 experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
 try:
  zip_data = await file.read()
  with io.BytesIO(zip_data) as buff:
   with zipfile.ZipFile(buff, "r") as zip_ref:
    namelist = zip_ref.namelist()
    for member in zip_ref.infolist():
     target_path = (experiment_path / member.filename).resolve()
     if experiment_path not in target_path.parents and target_path != experiment_path:
      logger.error(f"SECURITY ALERT: Zip Slip attempt in '{slug_id}'! '{member.filename}'")
      raise HTTPException(400, f"Path traversal in zip member '{member.filename}'")

    manifest_path_in_zip = next((p for p in namelist if p.endswith("manifest.json") and "MACOSX" not in p), None)
    if not manifest_path_in_zip:
     raise HTTPException(400, "Zip must contain 'manifest.json'.")
    if not any(p.endswith("actor.py") for p in namelist):
     raise HTTPException(400, "Zip must contain an 'actor.py' file.")
    if not any(p.endswith("__init__.py") for p in namelist):
     raise HTTPException(400, "Zip must contain an '__init__.py'.")

    with zip_ref.open(manifest_path_in_zip) as mf:
     parsed_manifest = json.load(mf)

    reqs_path_in_zip = next((p for p in namelist if p.endswith("requirements.txt") and "MACOSX" not in p), None)
    parsed_reqs = []
    if reqs_path_in_zip:
     with zip_ref.open(reqs_path_in_zip) as rf:
      parsed_reqs = _parse_requirements_from_string(rf.read().decode("utf-8"))
 except zipfile.BadZipFile:
  raise HTTPException(400, "Invalid/corrupted .zip file.")
 except HTTPException:
  raise
 except Exception as e:
  logger.error(f"Zip pre-check error for '{slug_id}': {e}", exc_info=True)
  raise HTTPException(500, f"Error reading zip: {e}")

 timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
 b2_object_key = f"{slug_id}/runtime-{timestamp}.zip"
 try:
  logger.info(f"[{slug_id}] Uploading runtime zip to B2 object '{b2_object_key}'...")
  s3.upload_fileobj(io.BytesIO(zip_data), B2_BUCKET_NAME, b2_object_key)
  logger.info(f"[{slug_id}] B2 upload successful. Generating presigned URL...")
  b2_final_uri = _generate_presigned_download_url(s3, B2_BUCKET_NAME, b2_object_key)
 except ClientError as e:
  logger.error(f"[{slug_id}] B2 upload failed: {e}", exc_info=True)
  raise HTTPException(500, f"Failed to upload runtime: {e}")
 except Exception as e:
  logger.error(f"[{slug_id}] B2 upload unknown error: {e}", exc_info=True)
  raise HTTPException(500, f"Unexpected B2 upload error: {e}")

 try:
  if experiment_path.exists():
   logger.info(f"[{slug_id}] Deleting old directory at {experiment_path}")
   shutil.rmtree(experiment_path)
  experiment_path.mkdir(parents=True)
  logger.info(f"[{slug_id}] Extracting local 'thin client' to {experiment_path}...")
  with io.BytesIO(zip_data) as buff:
   with zipfile.ZipFile(buff, "r") as zip_ref:
    zip_ref.extractall(experiment_path)
 except Exception as e:
  logger.error(f"[{slug_id}] Extraction error: {e}", exc_info=True)
  raise HTTPException(500, f"Zip extraction error: {e}")

 try:
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  config_data = {
   **parsed_manifest,
   "slug": slug_id,
   "runtime_s3_uri": b2_final_uri,
   "runtime_pip_deps": parsed_reqs,
  }
  await db.experiments_config.update_one({"slug": slug_id}, {"$set": config_data}, upsert=True)
  logger.info(f"[{slug_id}] Updated DB config with new B2 runtime.")
 except Exception as e:
  logger.error(f"[{slug_id}] DB config update error: {e}", exc_info=True)
  raise HTTPException(500, f"Failed saving config: {e}")

 try:
  logger.info(f"[{slug_id}] Reloading after zip upload...")
  await reload_active_experiments(request.app)
  return JSONResponse({"message": f"Successfully uploaded and reloaded '{slug_id}'."})
 except Exception as e:
  logger.error(f"[{slug_id}] Reload failure: {e}", exc_info=True)
  raise HTTPException(500, f"Reload failed: {e}")


app.include_router(admin_router)


def _create_experiment_proxy_router(slug: str, cfg: Dict[str, Any], exp_path: Path, templates_global: Optional[Jinja2Templates]) -> APIRouter:
 """
 Stub for further custom logic. Not used in final code, intentionally left blank.
 """
 pass


async def reload_active_experiments(app: FastAPI):
 db: AsyncIOMotorDatabase = app.state.mongo_db
 logger.info(" Reloading active experiments from DB...")
 try:
  active_cfgs = await db.experiments_config.find({"status": "active"}).to_list(None)
  logger.info(f"Found {len(active_cfgs)} active experiment(s).")
  await _register_experiments(app, active_cfgs, is_reload=True)
  logger.info(" Experiment reload complete.")
 except Exception as e:
  logger.error(f" Reload error: {e}", exc_info=True)
  app.state.experiments.clear()


async def _register_experiments(app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False):
 if is_reload:
  logger.debug("Clearing old experiment state...")
  app.state.experiments.clear()

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
   # Check if mount already exists to prevent error on reload
   if not any(route.path == mount_path for route in app.routes if hasattr(route, "path")):
    try:
     app.mount(mount_path, StaticFiles(directory=str(static_dir)), name=mount_name)
     logger.debug(f"[{slug}] Mounted static at '{mount_path}'.")
    except Exception as e:
     logger.error(f"[{slug}] Static mount error: {e}", exc_info=True)
   else:
    logger.debug(f"[{slug}] Static mount '{mount_path}' already exists.")

  if INDEX_MANAGER_AVAILABLE and "managed_indexes" in cfg:
   managed_indexes: Dict[str, List[Dict]] = cfg["managed_indexes"]
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
     _run_index_creation_for_collection(db, slug, prefixed_collection_name, prefixed_defs)
    )
  elif "managed_indexes" in cfg:
   logger.warning(f"[{slug}] 'managed_indexes' present but index manager not available.")

  # 4. Load the local APIRouter from '__init__.py'
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
  app.include_router(proxy_router, prefix=prefix, tags=[f"Experiment: {slug}"], dependencies=deps)
  cfg["url"] = prefix
  app.state.experiments[slug] = cfg
  logger.info(f"[{slug}] Experiment mounted at '{prefix}'")

  # If Ray is not available, skip actor logic
  if not getattr(app.state, "ray_is_available", False):
   logger.warning(f"[{slug}] No Ray available; skipping actor.")
   continue

  # 3. Load the local 'actor.py'
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
   actor_handle = actor_cls.options(
    name=actor_name, namespace="modular_labs", lifetime="detached",
    get_if_exists=True, max_restarts=-1, runtime_env=actor_runtime_env
   ).remote(
    mongo_uri=MONGO_URI, db_name=DB_NAME,
    write_scope=slug, read_scopes=read_scopes
   )
   logger.info(f"[{slug}] Ray Actor '{actor_name}' started in {env_mode.upper()} mode.")
s.
  except Exception as e:
   logger.error(f"[{slug}] Actor start error: {e}", exc_info=True)
   continue


async def _run_index_creation_for_collection(
 db: AsyncIOMotorDatabase,
 slug: str,
 collection_name: str,
 index_definitions: List[Dict[str, Any]]
):
 log_prefix = f"[{slug} -> {collection_name}]"
 try:
  real_collection = db[collection_name]
  index_manager = AsyncAtlasIndexManager(real_collection)
  logger.info(f"{log_prefix} Checking {len(index_definitions)} index defs.")
section.
 except Exception as e:
  logger.error(f"{log_prefix} IndexManager init error: {e}", exc_info=True)
  return

 for index_def in index_definitions:
  index_name = index_def.get("name")
  index_type = index_def.get("type")
  try:
   if index_type == "regular":
    keys = index_def.get("keys")
    options = {**index_def.get("options", {}), "name": index_name}
    if not keys:
     logger.warning(f"{log_prefix} Missing 'keys' on index '{index_name}'.")
     continue
    existing_index = await index_manager.get_index(index_name)
    if existing_index:
     # Compare keys to see if they differ
     tmp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
.
     key_doc = {k: v for k, v in tmp_keys}
     if existing_index.get("key") != key_doc:
      logger.warning(f"{log_prefix} Index '{index_name}' mismatch -> drop & recreate.")
      await index_manager.drop_index(index_name)
     else:
      logger.info(f"{log_prefix} Regular index '{index_name}' matches; skipping.")
      continue
    logger.info(f"{log_prefix} Creating regular index '{index_name}'...")
    await index_manager.create_index(keys, **options)
    logger.info(f"{log_prefix} Created regular index '{index_name}'.")
   elif index_type in ("vectorSearch", "search"):
    definition = index_def.get("definition")
    if not definition:
     logger.warning(f"{log_prefix} Missing 'definition' for search index '{index_name}'.")
     continue
    existing_index = await index_manager.get_search_index(index_name)
.
    if existing_index:
     current_def = existing_index.get("latestDefinition", existing_index.get("definition"))
     if current_def == definition:
      logger.info(f"{log_prefix} Search index '{index_name}' definition matches.")
      if not existing_index.get("queryable") and existing_index.get("status") != "FAILED":
       logger.info(f"{log_prefix} Index '{index_name}' not queryable yet; waiting.")
s.
       await index_manager._wait_for_search_index_ready(index_name, index_manager.DEFAULT_SEARCH_TIMEOUT)
       logger.info(f"{log_prefix} Index '{index_name}' now ready.")
      elif existing_index.get("status") == "FAILED":
       logger.error(f"{log_prefix} Index '{index_name}' state=FAILED. Manual fix needed.")
s.
      else:
       logger.info(f"{log_prefix} Index '{index_name}' is ready.")
     else:
      logger.warning(f"{log_prefix} Search index '{index_name}' def changed; updating.")
      await index_manager.update_search_index(name=index_name, definition=definition, wait_for_ready=True)
      logger.info(f"{log_prefix} Updated search index '{index_name}'.")
s.
    else:
     logger.info(f"{log_prefix} Creating new search index '{index_name}'...")
     await index_manager.create_search_index(name=index_name, definition=definition, index_type=index_type, wait_for_ready=True)
     logger.info(f"{log_prefix} Created new '{index_type}' index '{index_name}'.")
   else:
    logger.warning(f"{log_prefix} Unknown index type '{index_type}'; skipping.")
s.
  except Exception as e:
   logger.error(f"{log_prefix} Error managing index '{index_name}': {e}", exc_info=True)


@app.get("/", response_class=HTMLResponse, name="home")
async def root(request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)):
 if not templates:
  raise HTTPException(500, "Template engine not available.")
 return templates.TemplateResponse("index.html", {
  "request": request,
  "experiments": getattr(request.app.state, "experiments", {}),
  "current_user": user,
  "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
 })


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