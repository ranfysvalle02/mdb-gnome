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

# Import ScopedMongoWrapper - this is the whole point
try:
  from async_mongo_wrapper import ScopedMongoWrapper
  HAVE_MONGO_WRAPPER = True
except ImportError:
  HAVE_MONGO_WRAPPER = False
  logging.warning("async_mongo_wrapper not found. Database wrapper unavailable.")

# --- NEW: Backblaze B2 SDK (Native Client) ---
try:
  from b2sdk.v2 import InMemoryAccountInfo, B2Api
  from b2sdk.exception import B2Error, B2SimpleError
  B2SDK_AVAILABLE = True
except ImportError:
  B2SDK_AVAILABLE = False
  logging.warning("b2sdk library not found. Backblaze B2 integration will be disabled.")

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

# Async file I/O
try:
  import aiofiles
  AIOFILES_AVAILABLE = True
except ImportError:
  AIOFILES_AVAILABLE = False
  logging.warning("aiofiles not found. File I/O will use asyncio.to_thread() fallback.")

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
  If 'fallback_if_no_db' is True and we detect ScopedMongoWrapper is missing,
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
      # If fallback requested and no ScopedMongoWrapper, pass "use_in_memory_fallback"
      if fallback_if_no_db and not HAVE_MONGO_WRAPPER:
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
  )
  # ScopedMongoWrapper is imported above from async_mongo_wrapper
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
EXPORTS_TEMP_DIR = BASE_DIR / "temp_exports"  # Temporary local storage for exports

# Ensure exports temp directory exists
EXPORTS_TEMP_DIR.mkdir(exist_ok=True, mode=0o755)
logger.info(f"Exports temp directory: {EXPORTS_TEMP_DIR}")

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
# B2 SDK uses applicationKeyId and applicationKey (not endpoint_url)
B2_APPLICATION_KEY_ID = os.getenv("B2_APPLICATION_KEY_ID") or os.getenv("B2_ACCESS_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY") or os.getenv("B2_SECRET_ACCESS_KEY")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")

# Legacy env var support (B2_ENDPOINT_URL not needed for native SDK)
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL")  # Kept for Ray compatibility

B2_ENABLED = all([B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME, B2SDK_AVAILABLE])
b2_api = None  # Will be initialized in lifespan
b2_bucket = None  # Will be initialized in lifespan
_b2_init_lock = asyncio.Lock()  # Lock to prevent race condition during B2 initialization

if not B2SDK_AVAILABLE:
  logger.critical("b2sdk library not installed. B2 features are impossible. pip install b2sdk")
elif B2_ENABLED:
  logger.info(f"Backblaze B2 integration ENABLED for bucket '{B2_BUCKET_NAME}'.")
else:
  logger.warning("Backblaze B2 integration DISABLED. Missing one or more B2_... env vars.")
  logger.warning("Dynamic experiment uploads via /api/upload-experiment will FAIL.")


## Utility: B2 Presigned URL Generator (Native B2 SDK)
def _generate_presigned_download_url(b2_bucket, file_name: str, duration_seconds: int = 3600) -> str:
  """
  Generates a secure, time-limited HTTPS download URL using native B2 SDK.
  B2 SDK always returns HTTPS URLs, avoiding mixed content warnings.
  
  Args:
    b2_bucket: B2 bucket instance from B2Api
    file_name: Object key in the bucket
    duration_seconds: URL expiration time (default: 1 hour)
    
  Returns:
    HTTPS presigned download URL
  """
  try:
    # B2 SDK generates HTTPS URLs by default
    url = b2_bucket.get_download_url(file_name, duration_seconds)
    
    # Verify it's HTTPS (B2 SDK should always return HTTPS, but be defensive)
    if not url.startswith('https://'):
      logger.error(f"CRITICAL: B2 SDK returned non-HTTPS URL: {url[:100]}...")
      if url.startswith('http://'):
        url = url.replace('http://', 'https://', 1)
        logger.warning(f"Forced HTTPS on B2 presigned URL (was HTTP): {file_name}")
      else:
        raise ValueError(f"Invalid URL scheme from B2 SDK: {url[:50]}")
    
    logger.debug(f"Generated B2 presigned HTTPS URL for: {file_name}")
    return url
  except B2Error as e:
    logger.error(f"B2 SDK error generating presigned URL for '{file_name}': {e}")
    raise


## Utility: Async File I/O Helpers
async def _read_file_async(file_path: Path, encoding: str = "utf-8") -> str:
  """
  Asynchronously read a text file.
  Uses aiofiles if available, otherwise falls back to asyncio.to_thread().
  """
  if AIOFILES_AVAILABLE:
    async with aiofiles.open(file_path, "r", encoding=encoding) as f:
      return await f.read()
  else:
    return await asyncio.to_thread(file_path.read_text, encoding=encoding)

async def _read_json_async(file_path: Path, encoding: str = "utf-8") -> Any:
  """
  Asynchronously read and parse a JSON file.
  Uses async file reading and offloads JSON parsing to thread pool.
  """
  content = await _read_file_async(file_path, encoding)
  # JSON parsing is CPU-bound, so offload it
  return await asyncio.to_thread(json.loads, content)

async def _write_file_async(file_path: Path, content: bytes | str) -> None:
  """
  Asynchronously write to a file.
  Uses aiofiles if available, otherwise falls back to asyncio.to_thread().
  Automatically detects if content is str or bytes.
  """
  if AIOFILES_AVAILABLE:
    if isinstance(content, str):
      async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
        await f.write(content)
    else:
      async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)
  else:
    if isinstance(content, str):
      await asyncio.to_thread(file_path.write_text, content, encoding="utf-8")
    else:
      await asyncio.to_thread(file_path.write_bytes, content)

## Utility: Estimate Export Size
def _estimate_export_size(
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]],
  source_dir: Path,
  slug_id: str
) -> int:
  """
  Estimates the size of the export in bytes.
  Used to determine if disk streaming is needed.
  """
  size = 0
  
  # Estimate size of JSON data
  try:
    size += len(json.dumps(db_data, indent=2).encode('utf-8'))
    size += len(json.dumps(db_collections, indent=2).encode('utf-8'))
  except Exception:
    pass
  
  # Estimate size of experiment files
  experiment_path = source_dir / "experiments" / slug_id
  if experiment_path.is_dir():
    for root, dirs, files in os.walk(experiment_path):
      for file_name in files:
        file_path = Path(root) / file_name
        try:
          if file_path.is_file():
            size += file_path.stat().st_size
        except Exception:
          pass
  
  return size


## Utility: Determine if Disk Streaming is Needed
def _should_use_disk_streaming(estimated_size: int, max_size_mb: int = 100) -> bool:
  """
  Determines if disk streaming should be used based on estimated size.
  Default threshold: 100 MB
  """
  max_size_bytes = max_size_mb * 1024 * 1024
  return estimated_size > max_size_bytes


## Utility: Upload Export to B2
async def _upload_export_to_b2(
  b2_bucket,
  zip_source: io.BytesIO | Path,
  b2_filename: str
) -> str:
  """
  Uploads export ZIP to B2 storage.
  Accepts either BytesIO or Path.
  Returns the B2 file key/name.
  """
  try:
    # Get ZIP data
    if isinstance(zip_source, Path):
      zip_data = zip_source.read_bytes()
    else:
      zip_source.seek(0)
      zip_data = zip_source.getvalue()
    
    # Upload to B2
    b2_bucket.upload_bytes(zip_data, b2_filename)
    logger.info(f"Uploaded export to B2: {b2_filename}")
    return b2_filename
  except B2Error as e:
    logger.error(f"B2 upload failed for '{b2_filename}': {e}", exc_info=True)
    raise
  except Exception as e:
    logger.error(f"Failed to upload export to B2: {e}", exc_info=True)
    raise

## Utility: Save Export Locally
async def _save_export_locally(zip_source: io.BytesIO | Path, filename: str) -> Path:
  """
  Saves export ZIP to local temp directory.
  Accepts either BytesIO or Path (if already on disk).
  Returns the file path for serving.
  """
  export_file = EXPORTS_TEMP_DIR / filename
  try:
    if isinstance(zip_source, Path):
      # Already on disk, just copy/rename if needed
      if zip_source != export_file:
        shutil.copy2(zip_source, export_file)
        zip_source.unlink()  # Remove temporary file
      logger.debug(f"Export already on disk: {export_file}")
    else:
      # BytesIO - write to disk
      zip_source.seek(0)
      zip_data = zip_source.getvalue()
      await _write_file_async(export_file, zip_data)
      logger.debug(f"Saved export to local temp: {export_file}")
    return export_file
  except Exception as e:
    logger.error(f"Failed to save export locally: {e}", exc_info=True)
    raise

## Utility: Cleanup Old Exports
async def _cleanup_old_exports(max_age_hours: int = 24):
  """
  Removes export files older than max_age_hours from temp directory.
  Runs asynchronously in background.
  """
  try:
    cutoff_time = datetime.datetime.now().timestamp() - (max_age_hours * 3600)
    deleted_count = 0
    for export_file in EXPORTS_TEMP_DIR.glob("*.zip"):
      try:
        if export_file.stat().st_mtime < cutoff_time:
          export_file.unlink()
          deleted_count += 1
          logger.debug(f"Cleaned up old export: {export_file.name}")
      except Exception as e:
        logger.warning(f"Failed to delete old export {export_file.name}: {e}")
    
    if deleted_count > 0:
      logger.info(f"Cleaned up {deleted_count} old export file(s)")
  except Exception as e:
    logger.error(f"Error during export cleanup: {e}", exc_info=True)

## Utility: Log Export to Database
async def _log_export(
  db: AsyncIOMotorDatabase,
  slug_id: str,
  export_type: str,
  user_email: str,
  local_file_path: Optional[str] = None,
  file_size: Optional[int] = None,
  b2_file_name: Optional[str] = None,
  invalidated: bool = False
):
  """
  Logs an export event to the database for tracking purposes.
  export_type should be 'standalone' or 'docker' or 'intelligent'
  """
  try:
    export_log = {
      "slug_id": slug_id,
      "export_type": export_type,
      "user_email": user_email,
      "local_file_path": local_file_path,
      "file_size": file_size,
      "b2_file_name": b2_file_name,
      "invalidated": invalidated,
      "created_at": datetime.datetime.utcnow(),
    }
    result = await db.export_logs.insert_one(export_log)
    logger.debug(f"Logged export: {slug_id} ({export_type}) by {user_email}, ID: {result.inserted_id}")
    return result.inserted_id
  except Exception as e:
    logger.error(f"Failed to log export to database: {e}", exc_info=True)
    # Don't fail the export if logging fails
    return None


## Utility: Parse and Merge Requirements for Ray Isolation
def _parse_requirements_file_sync(req_path: Path) -> List[str]:
  """Synchronous version for module-level initialization."""
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

async def _parse_requirements_file(req_path: Path) -> List[str]:
  """
  Asynchronously parse a requirements file.
  Uses async file reading and offloads parsing to thread pool if needed.
  """
  if not req_path.is_file():
    return []
  try:
    content = await _read_file_async(req_path)
    lines = []
    for raw_line in content.splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#"):
        continue
      lines.append(line)
    return lines
  except Exception as e:
    logger.error(f" Failed to read requirements file '{req_path}': {e}")
    return []

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


MASTER_REQUIREMENTS = _parse_requirements_file_sync(BASE_DIR / "requirements.txt")
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

  # --- NEW: Initialize B2 SDK Client ---
  global b2_api, b2_bucket
  if not B2_ENABLED:
    app.state.b2_api = None
    app.state.b2_bucket = None
    logger.warning("B2 SDK not initialized (B2_ENABLED=False).")
  else:
    async with _b2_init_lock:
      # Double-check pattern: check again after acquiring lock to prevent race condition
      if not b2_api:
        try:
          # Initialize B2 API with account info
          account_info = InMemoryAccountInfo()
          b2_api = B2Api(account_info)
          
          # Authorize account (authenticates and caches credentials)
          b2_api.authorize_account("production", B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY)
          
          # Get bucket by name
          b2_bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
          
          # Store in app state
          app.state.b2_api = b2_api
          app.state.b2_bucket = b2_bucket
          
          logger.info(f"Backblaze B2 SDK initialized successfully for bucket '{B2_BUCKET_NAME}'.")
        except B2Error as e:
          logger.error(f"Failed to initialize B2 SDK during lifespan: {e}", exc_info=True)
          app.state.b2_api = None
          app.state.b2_bucket = None
          b2_api = None
          b2_bucket = None
        except Exception as e:
          logger.error(f"Unexpected error initializing B2 SDK: {e}", exc_info=True)
          app.state.b2_api = None
          app.state.b2_bucket = None
          b2_api = None
          b2_bucket = None

  # Ray Cluster Connection
  if RAY_AVAILABLE:
      # Get the address without a default value
      RAY_CONNECTION_ADDRESS = os.getenv("RAY_ADDRESS") 

      job_runtime_env: Dict[str, Any] = {"working_dir": str(BASE_DIR)}

      if B2_ENABLED:
        # Pass B2 keys as environment variables for Ray to use in its actors/workers
        # Note: Ray might still use S3-compatible interface, so we keep AWS-style env vars
        job_runtime_env["env_vars"] = {
            "AWS_ENDPOINT_URL": B2_ENDPOINT_URL or "",  # May be None with native SDK
            "AWS_ACCESS_KEY_ID": B2_APPLICATION_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": B2_APPLICATION_KEY,
            "B2_APPLICATION_KEY_ID": B2_APPLICATION_KEY_ID,
            "B2_APPLICATION_KEY": B2_APPLICATION_KEY,
            "B2_BUCKET_NAME": B2_BUCKET_NAME
        }
        logger.info("Passing B2 credentials to Ray job runtime environment.")

      try:
          if RAY_CONNECTION_ADDRESS:
              # --- THIS BLOCK IS FOR DOCKER-COMPOSE ---
              # We remove the lines here because we are connecting
              # to an existing cluster that already has its own config.
              logger.info(f"Connecting to Ray cluster (address='{RAY_CONNECTION_ADDRESS}', namespace='modular_labs')...")
              ray.init(
                  address=RAY_CONNECTION_ADDRESS,
                  namespace="modular_labs",
                  ignore_reinit_error=True,
                  runtime_env=job_runtime_env,
                  log_to_driver=False
                  # num_cpus=2,  <--- REMOVE THIS
                  # object_store_memory=2_000_000_000 <--- AND REMOVE THIS
              )
              connect_mode = f"EXTERNAL ({RAY_CONNECTION_ADDRESS})"
          else:
              # We LEAVE this block 100% UNCHANGED.
              # These settings are correct for limiting a new local instance.
              logger.info("Starting a new LOCAL Ray cluster instance inside the container...")
              ray.init(
                  namespace="modular_labs",
                  ignore_reinit_error=True,
                  runtime_env=job_runtime_env,
                  log_to_driver=False,
                  num_cpus=2, # <-- THIS STAYS (for Render)
                  object_store_memory=2_000_000_000 # <-- THIS STAYS (for Render)
              )
              connect_mode = "LOCAL INSTANCE"
              
          app.state.ray_is_available = True
      except Exception as e:
          logger.error(f"Failed to initialize Ray: {e}", exc_info=True)
          app.state.ray_is_available = False
  else:
      logger.warning("Ray library not found. Ray integration is disabled.")

  # MongoDB Connection
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

  # Pluggable Authorization Provider Initialization
  AUTHZ_PROVIDER = os.getenv("AUTHZ_PROVIDER", "casbin").lower()
  logger.info(f"Initializing Authorization Provider: '{AUTHZ_PROVIDER}'...")
  provider_settings = {"mongo_uri": MONGO_URI, "db_name": DB_NAME, "base_dir": BASE_DIR}
  try:
    authz_instance = await create_authz_provider(AUTHZ_PROVIDER, provider_settings)
    app.state.authz_provider = authz_instance
    logger.info(f" Authorization Provider '{authz_instance.__class__.__name__}' initialized.")
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

  # Load Initial Active Experiments
  logger.info(" About to call reload_active_experiments...")
  try:
    await reload_active_experiments(app)
    logger.info(" reload_active_experiments completed successfully.")
  except Exception as e:
    logger.error(f" ❌ Error during initial experiment load: {e}", exc_info=True)
    import traceback
    logger.error(f" ❌ Full traceback: {traceback.format_exc()}")

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


class ProxyAwareHTTPSMiddleware(BaseHTTPMiddleware):
  """
  Proxy-aware middleware: Detects proxy headers and rewrites request.url
  to reflect the actual client scheme/host BEFORE route handlers execute.
  This ensures FastAPI's url_for() generates correct HTTPS URLs when behind proxies.
  
  Handles multiple proxy header formats:
  - X-Forwarded-Proto (Render.com, AWS ELB, etc.)
  - X-Forwarded-Ssl (older proxies)
  - Forwarded (RFC 7239)
  - X-Forwarded-Host
  """
  async def dispatch(self, request: Request, call_next: ASGIApp):
    # Store original values for debugging
    original_scheme = request.url.scheme
    original_host = request.url.hostname
    
    # Detect if we're in localhost/development (no proxy)
    is_localhost = original_host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    has_proxy_headers = any((
      request.headers.get("X-Forwarded-Proto"),
      request.headers.get("X-Forwarded-Host"),
      request.headers.get("Forwarded"),
      request.headers.get("X-Forwarded-Ssl")
    ))
    
    # Detect actual scheme from proxy headers (priority order)
    # Only trust proxy headers if we're NOT on localhost OR we have explicit proxy headers
    detected_scheme = original_scheme
    detected_host = original_host
    # Get port from original request URL or server scope
    detected_port = request.url.port
    if not detected_port:
      # Try to get from server scope
      server = request.scope.get("server")
      if server and len(server) == 2:
        detected_port = server[1]
    
    # On localhost without proxy headers, respect the original scheme
    # If user accesses via https://localhost, keep HTTPS; if http://localhost, keep HTTP
    # This allows local HTTPS development while defaulting to HTTP when appropriate
    if is_localhost and not has_proxy_headers:
      # Respect the original scheme - if user accessed via HTTPS, keep it as HTTPS
      detected_scheme = original_scheme
      logger.debug(f"Localhost request without proxy headers - respecting original scheme: {detected_scheme}")
    else:
      # We're behind a proxy or on a real server - check proxy headers
      
      # Check X-Forwarded-Proto (most common - Render.com, AWS ELB)
      forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
      if forwarded_proto in ("https", "http"):
        detected_scheme = forwarded_proto
        logger.debug(f"Detected scheme from X-Forwarded-Proto: {detected_scheme}")
      
      # Check X-Forwarded-Ssl (older proxies)
      if request.headers.get("X-Forwarded-Ssl", "").lower() == "on":
        detected_scheme = "https"
        logger.debug(f"Detected HTTPS from X-Forwarded-Ssl header")
      
      # Check Forwarded header (RFC 7239 format: proto=https;host=example.com)
      forwarded_header = request.headers.get("Forwarded", "")
      if forwarded_header:
        # Simple parsing for proto parameter
        if "proto=https" in forwarded_header.lower():
          detected_scheme = "https"
          logger.debug(f"Detected HTTPS from Forwarded header")
        # Parse host from Forwarded header if present
        host_match = re.search(r'host=([^;,\s]+)', forwarded_header, re.IGNORECASE)
        if host_match:
          host_value = host_match.group(1).strip('"')
          if ":" in host_value:
            detected_host, port_str = host_value.rsplit(":", 1)
            try:
              detected_port = int(port_str)
            except ValueError:
              pass
          else:
            detected_host = host_value
      
      # Check X-Forwarded-Host
      forwarded_host = request.headers.get("X-Forwarded-Host")
      if forwarded_host:
        # X-Forwarded-Host may include port
        if ":" in forwarded_host:
          detected_host, port_str = forwarded_host.rsplit(":", 1)
          try:
            detected_port = int(port_str)
          except ValueError:
            pass
        else:
          detected_host = forwarded_host
        logger.debug(f"Detected host from X-Forwarded-Host: {detected_host}")
    
    # Force HTTPS in production when FORCE_HTTPS env var is set
    # But NOT on localhost unless explicitly requested
    force_https = os.getenv("FORCE_HTTPS", "").lower() == "true"
    if force_https and not is_localhost:
      detected_scheme = "https"
      logger.debug("Forcing HTTPS due to FORCE_HTTPS environment variable")
    
    # Store corrected values in request.state for later use
    request.state.original_scheme = original_scheme
    request.state.original_host = original_host
    request.state.detected_scheme = detected_scheme
    request.state.detected_host = detected_host
    request.state.detected_port = detected_port
    
    # Rewrite request.scope to reflect actual client scheme/host
    # This ensures request.url and url_for() use correct values
    # Always update if scheme changed, or if we need to preserve non-standard ports
    if detected_scheme != original_scheme or detected_host != original_host or (
      detected_port and detected_port != request.scope.get("server", (None, None))[1]
    ):
      # Modify the ASGI scope to change URL components
      request.scope["scheme"] = detected_scheme
      # Update server tuple (host, port)
      # Preserve the original port if it's non-standard, or use detected_port
      port_to_use = detected_port
      if not port_to_use:
        # Preserve original port if it's non-standard
        original_server = request.scope.get("server")
        if original_server and len(original_server) == 2:
          original_port = original_server[1]
          default_port = 443 if detected_scheme == "https" else 80
          if original_port != default_port:
            port_to_use = original_port
        
        # If still no port, use default based on scheme
        if not port_to_use:
          port_to_use = 443 if detected_scheme == "https" else 80
      
      request.scope["server"] = (detected_host, port_to_use)
      
      # Reconstruct the URL object
      # Note: Starlette's Request.url is cached, so we need to clear it
      if hasattr(request, "_url"):
        delattr(request, "_url")
      
      logger.info(
        f"Proxy-aware HTTPS: Rewrote request URL "
        f"{original_scheme}://{original_host}:{request.scope.get('server', (None, None))[1]} -> "
        f"{detected_scheme}://{detected_host}:{port_to_use}"
      )
    
    response = await call_next(request)
    return response


class HTTPSEnforcementMiddleware(BaseHTTPMiddleware):
  """
  Proxy-aware security middleware: Only enforces HTTPS when the request actually
  came via HTTPS (detected through proxy headers or direct connection).
  
  This is intelligent for deployments behind proxies like Render.com:
  - If the proxy indicates HTTPS (X-Forwarded-Proto: https), enforces HTTPS
  - If the proxy indicates HTTP, does NOT enforce HTTPS (allows proxy to handle it)
  - Prevents HTTP downgrade attacks and mixed content issues only when HTTPS is active.
  """
  async def dispatch(self, request: Request, call_next: ASGIApp):
    response = await call_next(request)
    
    # Check if this request is actually using HTTPS
    # Use the detected scheme from ProxyAwareHTTPSMiddleware if available
    # Otherwise check the request URL scheme directly
    detected_scheme = getattr(request.state, "detected_scheme", None)
    if detected_scheme is None:
      # Fallback: check proxy headers directly
      forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
      if forwarded_proto in ("https", "http"):
        detected_scheme = forwarded_proto
      elif request.headers.get("X-Forwarded-Ssl", "").lower() == "on":
        detected_scheme = "https"
      elif "proto=https" in request.headers.get("Forwarded", "").lower():
        detected_scheme = "https"
      else:
        detected_scheme = request.url.scheme
    
    # Only enforce HTTPS if the request actually came via HTTPS
    # This allows the proxy (like Render.com) to handle HTTPS termination
    # without our app trying to force it when it shouldn't
    is_https = detected_scheme == "https"
    
    if not is_https:
      # Request came via HTTP - don't enforce HTTPS
      # This allows proper operation behind proxies that handle HTTPS at the edge
      logger.debug(f"Skipping HTTPS enforcement - request came via {detected_scheme}")
      return response
    
    # Request came via HTTPS - enforce it
    logger.debug("Enforcing HTTPS - request came via HTTPS")
    
    # Add HSTS header - tells browsers to always use HTTPS for this domain
    # Only add this when we're actually serving HTTPS
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    
    # Force HTTPS on any Location redirect headers - catch ANY http:// redirect
    if "Location" in response.headers:
      location = response.headers["Location"]
      # Replace http:// with https:// directly (simple string replacement)
      if location.startswith("http://"):
        https_location = location.replace("http://", "https://", 1)
        response.headers["Location"] = https_location
        logger.debug(f"Enforced HTTPS redirect: {location} -> {https_location}")
    
    # Sanitize mixed content in response bodies
    content_type = response.headers.get("content-type", "").lower()
    
    # Only process text-based content types
    text_content_types = [
      "application/json",
      "text/html",
      "text/css",
      "text/javascript",
      "application/javascript",
      "text/plain",
      "text/xml",
      "application/xml",
    ]
    
    if any(ct in content_type for ct in text_content_types):
      # Check if response has a body
      if hasattr(response, "body") and response.body:
        try:
          # Decode response body
          if isinstance(response.body, bytes):
            body_text = response.body.decode("utf-8")
          else:
            body_text = str(response.body)
          
          original_body = body_text
          
          # Replace http:// URLs with https://
          # Pattern 1: Quoted URLs in JSON/HTML attributes: "http://...
          body_text = re.sub(r'"http://', '"https://', body_text)
          
          # Pattern 2: Unquoted URLs: http://...
          body_text = re.sub(r'\bhttp://', 'https://', body_text)
          
          # Pattern 3: HTML/CSS attributes: src="http://, href="http://, url(http://
          body_text = re.sub(r'(src|href)=["\']http://', r'\1="https://', body_text)
          body_text = re.sub(r'url\(http://', 'url(https://', body_text)
          
          # Pattern 4: JSON string values (more specific)
          body_text = re.sub(r'":\s*"http://', '": "https://', body_text)
          
          # Only update if changes were made
          if body_text != original_body:
            # Re-encode as bytes
            response.body = body_text.encode("utf-8")
            # Update content length if present
            if "content-length" in response.headers:
              response.headers["content-length"] = str(len(response.body))
            
            logger.debug("Sanitized mixed content in response body (HTTP -> HTTPS)")
        
        except (UnicodeDecodeError, AttributeError) as e:
          # Skip if body isn't text or can't be decoded
          logger.debug(f"Skipping mixed content sanitization: {e}")
    
    return response


# Middleware order matters: Proxy-aware middleware must run FIRST
# so that request.url is corrected before route handlers execute
app.add_middleware(ProxyAwareHTTPSMiddleware)
app.add_middleware(ExperimentScopeMiddleware)
G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()

if G_NOME_ENV == "production":
    logger.info("Production environment detected. Enabling proxy-aware HTTPSEnforcementMiddleware.")
    logger.info("HTTPS will only be enforced when requests actually come via HTTPS (detected from proxy headers).")
    app.add_middleware(HTTPSEnforcementMiddleware)
else:
    logger.warning(f"Non-production environment ('{G_NOME_ENV}') detected. Skipping HTTPS enforcement.")


async def _ensure_db_indices(db: AsyncIOMotorDatabase):
  try:
    await db.users.create_index("email", unique=True, background=True)
    await db.experiments_config.create_index("slug", unique=True, background=True)
    # Index for export logs - commonly queried by slug_id
    await db.export_logs.create_index("slug_id", background=True)
    await db.export_logs.create_index("created_at", background=True)
    logger.info(" Core MongoDB indexes ensured (users.email, experiments_config.slug, export_logs.slug_id).")
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
            and hasattr(authz, "has_policy")      # <-- Check for existence
            and hasattr(authz, "has_role_for_user") # <-- Check for existence
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

            # 3. Save policies ONLY if we made a change
            if made_changes:
                logger.info("Policy changes were made. Saving Casbin policies...")
                save_op = authz.save_policy()
                if asyncio.iscoroutine(save_op):
                    await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
                logger.info("Casbin policies saved successfully.")
            else:
                logger.info("All Casbin admin policies are already in sync. No changes needed.")

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
        slug = item.name
        manifest_path = item / "manifest.json"

        if manifest_path.is_file():
          exists = await db.experiments_config.find_one({"slug": slug})
          if not exists:
            logger.warning(f"[{slug}] No DB config found. Seeding from local 'manifest.json'...")
            try:
              manifest_data = await _read_json_async(manifest_path)
              if not isinstance(manifest_data, dict):
                logger.error(f"[{slug}] FAILED to seed: manifest.json is not valid JSON object.")
                continue
              manifest_data["slug"] = slug
              # Preserve status from manifest, default to "active" if not specified (was "draft" before)
              if "status" not in manifest_data:
                manifest_data["status"] = "active"
                logger.info(f"[{slug}] No 'status' in manifest, defaulting to 'active'.")
              
              status = manifest_data.get("status", "unknown")
              logger.info(f"[{slug}] Seeding with status='{status}' from manifest.")

              await db.experiments_config.insert_one(manifest_data)
              logger.info(f"[{slug}] ✅ SUCCESS: Seeded DB from local manifest.json (status='{status}').")
              seeded_count += 1
            except json.JSONDecodeError:
              logger.error(f"[{slug}] FAILED to seed: manifest.json is invalid JSON.")
            except Exception as e:
              logger.error(f"[{slug}] FAILED to seed: {e}", exc_info=True)
          else:
            # Update existing experiment if manifest says "active" but DB has "draft"
            try:
              manifest_data = await _read_json_async(manifest_path)
              if isinstance(manifest_data, dict):
                manifest_status = manifest_data.get("status", "active")
                db_status = exists.get("status", "draft")
                
                # If manifest says "active" but DB has "draft", update it
                if manifest_status == "active" and db_status == "draft":
                  await db.experiments_config.update_one(
                    {"slug": slug},
                    {"$set": {"status": "active"}}
                  )
                  logger.info(f"[{slug}] 🔄 Updated status from 'draft' to 'active' to match manifest.")
                  seeded_count += 1
                elif manifest_status == "active" and db_status != "active":
                  logger.debug(f"[{slug}] Manifest says 'active', DB has '{db_status}' - not updating (preserving DB status).")
            except Exception as e:
              logger.debug(f"[{slug}] Could not check manifest for status update: {e}")
            logger.debug(f"[{slug}] Skipping seed: DB config already exists.")
  except OSError as e:
    logger.error(f"Error scanning experiments directory for seeding: {e}")

  if seeded_count > 0:
    logger.info(f"Successfully seeded {seeded_count} new experiment(s) from filesystem.")
  else:
    logger.info("No new local manifests found to seed. Database is up-to-date.")


async def _scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
  """Async wrapper for directory scanning that runs the synchronous operation in a thread."""
  return await asyncio.to_thread(_scan_directory_sync, dir_path, base_path)


def _scan_directory_sync(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
  """Synchronous directory scanning implementation that runs in a thread pool."""
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
          "name": item.name,
          "type": "dir",
          "path": str(relative_path),
          "children": _scan_directory_sync(item, base_path)
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


auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
async def login_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
  if not templates:
    raise HTTPException(500, "Template engine not available.")
  safe_next = next if next and next.startswith("/") else "/"
  return templates.TemplateResponse("login.html", {
    "request": request,
    "next": safe_next,
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
      }, status_code=status.HTTP_409_CONFLICT)

    try:
      pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
      new_user = {
        "email": email,
        "password_hash": pwd_hash,
        "is_admin": False,
        "created_at": datetime.datetime.utcnow(),
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


def _make_json_serializable(obj: Any) -> Any:
  """
  Recursively converts MongoDB objects (datetime, ObjectId, etc.) to JSON-serializable types.
  """
  if isinstance(obj, datetime.datetime):
    return obj.isoformat()
  elif isinstance(obj, dict):
    return {key: _make_json_serializable(value) for key, value in obj.items()}
  elif isinstance(obj, list):
    return [_make_json_serializable(item) for item in obj]
  elif isinstance(obj, (datetime.date, datetime.time)):
    return obj.isoformat()
  elif hasattr(obj, '__str__') and not isinstance(obj, (str, int, float, bool, type(None))):
    # Handle ObjectId and other MongoDB types that have __str__
    try:
      return str(obj)
    except Exception:
      return repr(obj)
  return obj


async def _dump_db_to_json(db: AsyncIOMotorDatabase, slug_id: str) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
  config_doc = await db.experiments_config.find_one({"slug": slug_id})
  if not config_doc:
    raise ValueError(f"No experiment config found for slug '{slug_id}'")
  config_data = dict(config_doc)
  if "_id" in config_data:
    config_data["_id"] = str(config_data["_id"])
  
  # Make config data JSON-serializable
  config_data = _make_json_serializable(config_data)

  sub_collections = []
  all_coll_names = await db.list_collection_names()
  for cname in all_coll_names:
    if cname.startswith(f"{slug_id}_"):
      sub_collections.append(cname)

  collections_data: Dict[str, List[Dict[str, Any]]] = {}
  for coll_name in sub_collections:
    docs_list = []
    cursor = db[coll_name].find()
    async for doc in cursor:
      doc_dict = dict(doc)
      if "_id" in doc_dict:
        doc_dict["_id"] = str(doc_dict["_id"])
      # Make document JSON-serializable
      doc_dict = _make_json_serializable(doc_dict)
      docs_list.append(doc_dict)
    collections_data[coll_name] = docs_list

  return config_data, collections_data


def _make_standalone_main_py(slug_id: str) -> str:
  global templates
  if not templates:
    raise RuntimeError("Jinja2 templates object is not initialized.")
  template = templates.get_template("standalone_main.py.jinja2")
  standalone_main_source = template.render(slug_id=slug_id)
  return standalone_main_source


def _make_intelligent_standalone_main_py(slug_id: str) -> str:
  """Generate intelligent standalone main.py using real MongoDB."""
  global templates
  if not templates:
    raise RuntimeError("Jinja2 templates object is not initialized.")
  template = templates.get_template("standalone_main_intelligent.py.jinja2")
  standalone_main_source = template.render(slug_id=slug_id)
  return standalone_main_source


########################################################
# NEW: advanced fix_static_paths to handle single quotes
########################################################
def _fix_static_paths(html_content: str, slug_id: str) -> str:
  """
  Rewrite references to "static/" => "/experiments/<slug>/static/".
  Handles:
   - src="static/..." or src='static/...'
   - href="static/..." or href='static/...'
   - CSS url("static/..."), url('static/...'), url(static/...)
  """
  replaced = html_content

  pattern_src_href = re.compile(r'(src|href)=([\'"])static/')
  def replacer_src_href(match):
    attr = match.group(1)
    quote_char = match.group(2)
    return f'{attr}={quote_char}/experiments/{slug_id}/static/'

  replaced = pattern_src_href.sub(replacer_src_href, replaced)

  pattern_url = re.compile(r'url\(\s*[\'"]?\s*static/')
  replaced = pattern_url.sub(f'url("/experiments/{slug_id}/static/', replaced)
  return replaced


## Helper: Create ZIP on Disk (for large exports)
def _create_zip_to_disk(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]],
  zip_path: Path,
  experiment_path: Path,
  templates_dir: Path | None,
  standalone_main_source: str,
  requirements_content: str,
  readme_content: str,
  include_templates: bool = False,
  include_docker_files: bool = False,
  dockerfile_content: str | None = None,
  docker_compose_content: str | None = None,
  main_file_name: str = "standalone_main.py"
) -> Path:
  """
  Creates a ZIP archive directly on disk for large exports.
  This avoids loading the entire ZIP into memory.
  """
  EXCLUSION_PATTERNS = [
    "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
  ]
  
  with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    # Include experiment directory
    if experiment_path.is_dir():
      logger.debug(f"Including experiment code from: {experiment_path}")
      for folder_name, _, file_names in os.walk(experiment_path):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue
        
        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue
          
          file_path = Path(folder_name) / file_name
          try:
            arcname = str(file_path.relative_to(experiment_path))
          except ValueError:
            logger.error(f"Failed to get relative path for {file_path}")
            continue
          
          if file_path.suffix in (".html", ".htm"):
            original_html = file_path.read_text(encoding="utf-8")
            fixed_html = _fix_static_paths(original_html, slug_id)
            zf.writestr(arcname, fixed_html)
          else:
            zf.write(file_path, arcname)
    
    # Include templates directory if requested
    if include_templates and templates_dir and templates_dir.is_dir():
      for folder_name, _, file_names in os.walk(templates_dir):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue
        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue
          file_path = Path(folder_name) / file_name
          try:
            arcname = f"templates/{file_path.relative_to(templates_dir)}"
          except ValueError:
            continue
          zf.write(file_path, arcname)
    
    # Add __init__.py for experiments package (if needed)
    experiments_init = source_dir / "experiments" / "__init__.py"
    if experiments_init.is_file():
      zf.write(experiments_init, "experiments/__init__.py")
    
    # Add additional files for intelligent export
    if include_docker_files:
      mongo_wrapper = source_dir / "async_mongo_wrapper.py"
      if mongo_wrapper.is_file():
        zf.write(mongo_wrapper, "async_mongo_wrapper.py")
      
      experiment_db = source_dir / "experiment_db.py"
      if experiment_db.is_file():
        zf.write(experiment_db, "experiment_db.py")
      
      if dockerfile_content:
        zf.writestr("Dockerfile", dockerfile_content)
      if docker_compose_content:
        zf.writestr("docker-compose.yml", docker_compose_content)
    
    # Add generated core files
    zf.writestr("db_config.json", json.dumps(db_data, indent=2))
    zf.writestr("db_collections.json", json.dumps(db_collections, indent=2))
    zf.writestr(main_file_name, standalone_main_source)
    zf.writestr("README.md", readme_content)
    
    if requirements_content:
      zf.writestr("requirements.txt", requirements_content)
  
  return zip_path


def _create_standalone_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]]
) -> io.BytesIO | Path:
  """
  Creates a standalone ZIP package.
  Uses disk streaming for large exports (>100MB) to avoid memory issues.
  Returns either io.BytesIO (small exports) or Path (large exports).
  """
  logger.info(f"Starting creation of standalone package for '{slug_id}'.")
  experiment_path = source_dir / "experiments" / slug_id
  templates_dir = source_dir / "templates"

  # --- 1. Check if we should use disk streaming ---
  estimated_size = _estimate_export_size(db_data, db_collections, source_dir, slug_id)
  use_disk_streaming = _should_use_disk_streaming(estimated_size, max_size_mb=100)
  
  if use_disk_streaming:
    logger.info(f"[{slug_id}] Large export detected ({estimated_size / (1024*1024):.1f} MB), using disk streaming")
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_path = EXPORTS_TEMP_DIR / f"{slug_id}_standalone_export_{timestamp}.zip"
  else:
    zip_path = None

  # --- 2. Generate core files ---
  standalone_main_source = _make_standalone_main_py(slug_id)
  # Note: No root template needed - standalone app serves experiment's own index.html

  # --- 3. Determine local requirements to include ---
  local_reqs_path = experiment_path / "requirements.txt"
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    # Exclude requirements already present in the master environment
    master_pkg_names = {_extract_pkgname(req) for req in MASTER_REQUIREMENTS}
    standalone_requirements = [
      req for req in local_requirements
      if _extract_pkgname(req) not in master_pkg_names
    ]
    requirements_content = "\n".join(standalone_requirements)
  else:
    requirements_content = ""

  # --- 4. Generate README.md with instructions ---
  readme_content = f"""# Standalone Experiment Package: {slug_id}

This package contains a self-contained, single-file server (`standalone_main.py`)
to run the experiment locally, independent of the main platform.

## How to Run

1. **Setup Environment:**
 We recommend using a fresh Python virtual environment.
 ```bash
 python3 -m venv venv
 source venv/bin/activate # or venv\\Scripts\\activate on Windows
 ```

2. **Install Dependencies:**
 This experiment requires FastAPI, Uvicorn, Motor, and PyMongo.
 If a `requirements.txt` file is present in this directory, install those dependencies as well.
 ```bash
 # Base requirements
 pip install fastapi uvicorn 'motor[asyncio]' pymongo

 # Install experiment-specific requirements (if file exists)
 if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
 ```

3. **Start the Server:**
 Run the standalone application. It will automatically load the snapshot data.
 ```bash
 python standalone_main.py
 ```

4. **Access:**
 Open your browser and navigate to: http://127.0.0.1:8000/
"""

  # --- 5. Create ZIP archive (on disk or in memory) ---
  if use_disk_streaming:
    _create_zip_to_disk(
      slug_id=slug_id,
      source_dir=source_dir,
      db_data=db_data,
      db_collections=db_collections,
      zip_path=zip_path,
      experiment_path=experiment_path,
      templates_dir=None,
      standalone_main_source=standalone_main_source,
      requirements_content=requirements_content,
      readme_content=readme_content,
      include_templates=False,
      include_docker_files=False,
      main_file_name="standalone_main.py"
    )
    logger.info(f"Standalone package created successfully on disk: {zip_path}")
    return zip_path
  else:
    zip_buffer = io.BytesIO()
    EXCLUSION_PATTERNS = [
      "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
    ]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
      if experiment_path.is_dir():
        logger.debug(f"Including thin client code from: {experiment_path}")
        # Walk the experiment directory to include files
        for folder_name, _, file_names in os.walk(experiment_path):
          # Skip excluded folders
          if Path(folder_name).name in EXCLUSION_PATTERNS:
            continue

          for file_name in file_names:
            # Skip excluded files
            if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
              continue

            file_path = Path(folder_name) / file_name
            # Archive name should be relative to the root of the experiment directory
            try:
              arcname = str(file_path.relative_to(experiment_path))
            except ValueError:
              logger.error(f"Failed to get relative path for {file_path}")
              continue

            # Rewrite paths in HTML files
            if file_path.suffix in (".html", ".htm"):
              original_html = file_path.read_text(encoding="utf-8")
              fixed_html = _fix_static_paths(original_html, slug_id)
              zf.writestr(arcname, fixed_html)
            else:
              zf.write(file_path, arcname)

      # --- 6. Add generated core files to the root of the ZIP ---
      # No root template needed - standalone app serves experiment's own index.html directly

      zf.writestr("db_config.json", json.dumps(db_data, indent=2))
      zf.writestr("db_collections.json", json.dumps(db_collections, indent=2))
      zf.writestr("standalone_main.py", standalone_main_source)
      zf.writestr("README.md", readme_content)

      # Conditionally add requirements.txt
      if requirements_content:
        zf.writestr("requirements.txt", requirements_content)

    zip_buffer.seek(0)
    logger.info(f"Standalone package created successfully in memory for '{slug_id}'.")
    return zip_buffer


def _create_dockerfile_for_experiment(slug_id: str, source_dir: Path, experiment_path: Path) -> str:
  """
  Generates a Dockerfile specifically for the data_imaging experiment.
  Based on the root Dockerfile but customized for the experiment.
  """
  local_reqs_path = experiment_path / "requirements.txt"
  
  # Get combined requirements (root + experiment-specific)
  all_requirements = MASTER_REQUIREMENTS.copy()
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    for req in local_requirements:
      pkg_name = _extract_pkgname(req)
      # Replace if exists, otherwise add
      all_requirements = [r for r in all_requirements if _extract_pkgname(r) != pkg_name]
      all_requirements.append(req)
  
  # Build Dockerfile with requirements written properly
  dockerfile_lines = [
    "# --- Stage 1: Build dependencies ---",
    "FROM python:3.10-slim-bookworm as builder",
    "WORKDIR /app",
    "",
    "# Install build deps",
    "RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*",
    "",
    "# Create requirements file"
  ]
  
  # Write requirements one by one
  for req in all_requirements:
    # Escape any single quotes in the requirement
    escaped_req = req.replace("'", "'\"'\"'")
    dockerfile_lines.append(f"RUN echo '{escaped_req}' >> /tmp/requirements.txt")
  
  dockerfile_lines.append("")
  dockerfile_lines.append("# Install dependencies")
  dockerfile_lines.append("RUN python -m venv /opt/venv && \\")
  dockerfile_lines.append("    . /opt/venv/bin/activate && \\")
  dockerfile_lines.append("    pip install --upgrade pip && \\")
  dockerfile_lines.append("    pip install -r /tmp/requirements.txt")
  dockerfile_lines.append("")
  dockerfile_lines.append("# --- Stage 2: Final image ---")
  dockerfile_lines.append("FROM python:3.10-slim-bookworm")
  dockerfile_lines.append("WORKDIR /app")
  dockerfile_lines.append("")
  dockerfile_lines.append("# Copy venv from builder")
  dockerfile_lines.append("COPY --from=builder /opt/venv /opt/venv")
  dockerfile_lines.append('ENV PATH="/opt/venv/bin:$PATH"')
  dockerfile_lines.append("")
  dockerfile_lines.append("# Copy experiment code")
  dockerfile_lines.append(f"COPY experiments/{slug_id} /app/experiments/{slug_id}")
  dockerfile_lines.append("COPY experiments/__init__.py /app/experiments/__init__.py")
  dockerfile_lines.append("COPY templates /app/templates")
  dockerfile_lines.append("COPY db_config.json /app/db_config.json")
  dockerfile_lines.append("COPY db_collections.json /app/db_collections.json")
  dockerfile_lines.append("COPY main.py /app/main.py")
  dockerfile_lines.append("")
  dockerfile_lines.append("# Create non-root user")
  dockerfile_lines.append("RUN addgroup --system app && adduser --system --group app")
  dockerfile_lines.append("RUN chown -R app:app /app")
  dockerfile_lines.append("USER app")
  dockerfile_lines.append("")
  dockerfile_lines.append("# Build ARG and ENV for port")
  dockerfile_lines.append("ARG APP_PORT=8000")
  dockerfile_lines.append("ENV PORT=$APP_PORT")
  dockerfile_lines.append("")
  dockerfile_lines.append("# Expose the port")
  dockerfile_lines.append("EXPOSE ${APP_PORT}")
  dockerfile_lines.append("")
  dockerfile_lines.append("# Default command: Run the standalone server")
  dockerfile_lines.append("CMD python main.py")
  
  return "\n".join(dockerfile_lines)


def _create_intelligent_dockerfile(slug_id: str, source_dir: Path, experiment_path: Path) -> str:
  """
  Generates a clean Dockerfile WITH Ray dependencies (Ray is required).
  Includes FastAPI, MongoDB (Motor), Ray, and experiment-specific requirements.
  """
  local_reqs_path = experiment_path / "requirements.txt"
  
  # Base requirements WITH Ray (Ray is a core component and required)
  base_requirements = [
    "fastapi",
    "uvicorn[standard]",
    "motor>=3.0.0",
    "pymongo==4.15.3",
    "python-multipart",
    "jinja2",
    "ray[default]>=2.9.0",  # Ray is required - matches master requirements pattern
  ]
  
  # Add experiment-specific requirements
  all_requirements = base_requirements.copy()
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    for req in local_requirements:
      pkg_name = _extract_pkgname(req)
      # Ray is now in base requirements, so if it's in experiment requirements, use experiment version
      # Replace if exists, otherwise add
      all_requirements = [r for r in all_requirements if _extract_pkgname(r) != pkg_name]
      all_requirements.append(req)
  
  # Also need async_mongo_wrapper - check if it needs to be included
  # For now, we'll copy it in the Dockerfile
  
  dockerfile_lines = [
    "# --- Stage 1: Build dependencies ---",
    "FROM python:3.10-slim-bookworm as builder",
    "WORKDIR /app",
    "",
    "# Install build deps",
    "RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*",
    "",
    "# Create requirements file"
  ]
  
  # Write requirements
  for req in all_requirements:
    escaped_req = req.replace("'", "'\"'\"'")
    dockerfile_lines.append(f"RUN echo '{escaped_req}' >> /tmp/requirements.txt")
  
  dockerfile_lines.extend([
    "",
    "# Install dependencies",
    "RUN python -m venv /opt/venv && \\",
    "    . /opt/venv/bin/activate && \\",
    "    pip install --upgrade pip && \\",
    "    pip install -r /tmp/requirements.txt",
    "",
    "# --- Stage 2: Final image ---",
    "FROM python:3.10-slim-bookworm",
    "WORKDIR /app",
    "",
    "# Copy venv from builder",
    "COPY --from=builder /opt/venv /opt/venv",
    'ENV PATH="/opt/venv/bin:$PATH"',
    "",
    "# Copy core MongoDB wrapper (required for scoped access)",
    "COPY async_mongo_wrapper.py /app/async_mongo_wrapper.py",
    "",
    "# Copy experiment code",
    f"COPY experiments/{slug_id} /app/experiments/{slug_id}",
    "COPY experiments/__init__.py /app/experiments/__init__.py",
    "",
    "# Copy configuration files",
    "COPY db_config.json /app/db_config.json",
    "COPY db_collections.json /app/db_collections.json",
    "",
    "# Copy standalone main application",
    "COPY main.py /app/main.py",
    "",
    "# Create non-root user",
    "RUN addgroup --system app && adduser --system --group app",
    "RUN chown -R app:app /app",
    "USER app",
    "",
    "# Build ARG and ENV for port",
    "ARG APP_PORT=8000",
    "ENV PORT=$APP_PORT",
    "",
    "# Expose the port",
    "EXPOSE ${APP_PORT}",
    "",
    "# Default command: Run the standalone server",
    "CMD python main.py",
  ])
  
  return "\n".join(dockerfile_lines)


def _create_docker_compose_for_experiment(slug_id: str, source_dir: Path) -> str:
  """
  Generates a docker-compose.yml specifically for the data_imaging experiment.
  Based on the root docker-compose.yml but customized for standalone experiment.
  """
  docker_compose_content = f"""services:
  # --------------------------------------------------------------------------
  # Experiment Application (data_imaging)
  # --------------------------------------------------------------------------
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {slug_id}-app
    ports:
      - "8000:8000"
    environment:
      - MONGO_URI=mongodb://mongo:27017/
      - DB_NAME=labs_db
      - PORT=8000
    depends_on:
      mongo:
        condition: service_healthy

  # --------------------------------------------------------------------------
  # MongoDB Database
  # --------------------------------------------------------------------------
  mongo:
    image: mongodb/mongodb-atlas-local:latest
    container_name: {slug_id}-mongo
    ports:
      - "27017:27017"
    volumes:
      - mongo-data:/data/db
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.hello()", "--quiet"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s

volumes:
  mongo-data:
"""
  return docker_compose_content


def _create_intelligent_docker_compose(slug_id: str) -> str:
  """
  Generates an intelligent docker-compose.yml with MongoDB Atlas Local
  and optional Ray service for distributed computing.
  """
  docker_compose_content = f"""services:
  # --------------------------------------------------------------------------
  # FastAPI Application (Standalone Experiment)
  # --------------------------------------------------------------------------
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {slug_id}-app
    platform: linux/arm64
    ports:
      - "8000:8000"
    environment:
      - MONGO_URI=mongodb://mongo:27017/
      - DB_NAME=labs_db
      - PORT=8000
      - LOG_LEVEL=INFO
    volumes:
      - .:/app
    depends_on:
      mongo:
        condition: service_healthy
      # Optional: Uncomment below to enable Ray support
      # ray-head:
      #   condition: service_healthy
    restart: unless-stopped

  # --------------------------------------------------------------------------
  # MongoDB Atlas Local (for index management support)
  # --------------------------------------------------------------------------
  mongo:
    image: mongodb/mongodb-atlas-local:latest
    container_name: {slug_id}-mongo
    platform: linux/arm64
    ports:
      - "27017:27017"
    volumes:
      - mongo-data:/data/db
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.hello()", "--quiet"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s
    restart: unless-stopped

  # --------------------------------------------------------------------------
  # Ray Head Node (OPTIONAL - Uncomment to enable distributed computing)
  # --------------------------------------------------------------------------
  # ray-head:
  #   build:
  #     context: .
  #     dockerfile: Dockerfile.ray
  #   container_name: {slug_id}-ray-head
  #   platform: linux/arm64
  #   ports:
  #     - "10001:10001"  # Ray client server port
  #     - "8265:8265"     # Ray Dashboard
  #   volumes:
  #     - .:/app
  #   environment:
  #     - RAY_ENABLE_RUNTIME_ENV=1
  #     - RAY_RUNTIME_ENV_WORKING_DIR=/app
  #     - RAY_NAMESPACE=experiment_{slug_id}
  #   command: [
  #     "ray", "start", "--head",
  #     "--port=6379",
  #     "--ray-client-server-port=10001",
  #     "--dashboard-host=0.0.0.0",
  #     "--disable-usage-stats",
  #     "--block"
  #   ]
  #   shm_size: 4gb
  #   healthcheck:
  #     test: ["CMD", "python", "-c", "import socket; s = socket.socket(); s.connect(('localhost', 10001))"]
  #     interval: 5s
  #     timeout: 2s
  #     retries: 10
  #     start_period: 5s
  #   restart: unless-stopped

volumes:
  mongo-data:
    driver: local
"""
  return docker_compose_content


def _create_docker_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]]
) -> io.BytesIO:
  """
  Creates a Docker export package for data_imaging with Dockerfile and docker-compose.yml.
  """
  logger.info(f"Starting creation of Docker package for '{slug_id}'.")
  zip_buffer = io.BytesIO()
  experiment_path = source_dir / "experiments" / slug_id
  templates_dir = source_dir / "templates"

  # --- 1. Generate core files ---
  standalone_main_source = _make_standalone_main_py(slug_id)
  # Note: No root template needed - standalone app serves experiment's own index.html

  # --- 2. Get all requirements ---
  local_reqs_path = experiment_path / "requirements.txt"
  all_requirements = MASTER_REQUIREMENTS.copy()
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    for req in local_requirements:
      pkg_name = _extract_pkgname(req)
      all_requirements = [r for r in all_requirements if _extract_pkgname(r) != pkg_name]
      all_requirements.append(req)
  requirements_content = "\n".join(all_requirements)

  # --- 3. Generate Docker files ---
  dockerfile_content = _create_dockerfile_for_experiment(slug_id, source_dir, experiment_path)
  docker_compose_content = _create_docker_compose_for_experiment(slug_id, source_dir)

  # --- 4. Generate README.md with Docker instructions ---
  readme_content = f"""# Docker Export Package: {slug_id}

This package contains everything needed to run the experiment in Docker containers.

## Prerequisites

- Docker and Docker Compose installed on your system

## How to Run

1. **Extract the package:**
   ```bash
   unzip {slug_id}_docker_package_*.zip
   cd {slug_id}_docker_package_*
   ```

2. **Build and start containers:**
   ```bash
   docker-compose up --build
   ```

3. **Access:**
   Open your browser and navigate to: http://localhost:8000/

## Docker Structure

- **Dockerfile**: Builds the application container with all dependencies
- **docker-compose.yml**: Orchestrates the app and MongoDB containers
- **main.py**: Standalone server entry point
- **experiments/{slug_id}/**: Experiment code
- **db_config.json**: Experiment configuration snapshot
- **db_collections.json**: Database collections snapshot

## Stopping

Press `Ctrl+C` to stop the containers, or run:
```bash
docker-compose down
```

To remove volumes (including MongoDB data):
```bash
docker-compose down -v
```
"""

  # --- 5. Create ZIP archive ---
  EXCLUSION_PATTERNS = [
    "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
  ]
  with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
    # Include experiment directory
    if experiment_path.is_dir():
      logger.debug(f"Including experiment code from: {experiment_path}")
      for folder_name, _, file_names in os.walk(experiment_path):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue

        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue

          file_path = Path(folder_name) / file_name
          try:
            arcname = f"experiments/{slug_id}/{file_path.relative_to(experiment_path)}"
          except ValueError:
            logger.error(f"Failed to get relative path for {file_path}")
            continue

          if file_path.suffix in (".html", ".htm"):
            original_html = file_path.read_text(encoding="utf-8")
            fixed_html = _fix_static_paths(original_html, slug_id)
            zf.writestr(arcname, fixed_html)
          else:
            zf.write(file_path, arcname)

    # Include templates directory
    if templates_dir.is_dir():
      for folder_name, _, file_names in os.walk(templates_dir):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue
        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue
          file_path = Path(folder_name) / file_name
          try:
            arcname = f"templates/{file_path.relative_to(templates_dir)}"
          except ValueError:
            continue
          zf.write(file_path, arcname)

    # Add __init__.py for experiments package
    experiments_init = source_dir / "experiments" / "__init__.py"
    if experiments_init.is_file():
      zf.write(experiments_init, "experiments/__init__.py")

    # Add generated files
    # No root template needed - standalone app serves experiment's own index.html directly

    zf.writestr("Dockerfile", dockerfile_content)
    zf.writestr("docker-compose.yml", docker_compose_content)
    zf.writestr("db_config.json", json.dumps(db_data, indent=2))
    zf.writestr("db_collections.json", json.dumps(db_collections, indent=2))
    zf.writestr("main.py", standalone_main_source)
    zf.writestr("requirements.txt", requirements_content)
    zf.writestr("README.md", readme_content)

  zip_buffer.seek(0)
  logger.info(f"Docker package created successfully for '{slug_id}'.")
  return zip_buffer


def _create_intelligent_readme(slug_id: str, experiment_name: str, description: str) -> str:
  """
  Generates a comprehensive README with scaling instructions.
  """
  readme_content = f"""# Intelligent Export: {experiment_name}

{description}

This is a clean, production-ready FastAPI application extracted from the MDB-Gnome platform. It includes everything needed to run this experiment as a standalone service, with MongoDB Atlas Local support for index management and optional Ray integration for distributed computing.

## 🚀 Quick Start

### Prerequisites

- **Docker** and **Docker Compose** installed on your system
- **Python 3.10+** (if running locally without Docker)

### Option 1: Run with Docker (Recommended)

1. **Extract the package:**
   ```bash
   unzip {slug_id}_intelligent_export_*.zip
   cd {slug_id}_intelligent_export_*
   ```

2. **Start the services:**
   ```bash
   docker-compose up --build
   ```

3. **Access the application:**
   - Main application: http://localhost:8000
   - API documentation: http://localhost:8000/docs
   - MongoDB: localhost:27017

### Option 2: Run Locally (Without Docker)

1. **Setup Python environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Ensure MongoDB is running:**
   - You need MongoDB Atlas Local or a local MongoDB instance
   - Default connection: `mongodb://localhost:27017/`
   - Set `MONGO_URI` environment variable if different

4. **Run the application:**
   ```bash
   export MONGO_URI=mongodb://localhost:27017/
   export DB_NAME=labs_db
   export PORT=8000
   python main.py
   ```

## 📦 Package Contents

- **`main.py`**: Clean FastAPI application without Ray dependencies (Ray optional via docker-compose)
- **`experiments/{slug_id}/`**: Complete experiment code (router, templates, static files)
- **`async_mongo_wrapper.py`**: MongoDB scoped wrapper for data isolation
- **`db_config.json`**: Experiment configuration snapshot
- **`db_collections.json`**: Database collections snapshot (for initial seeding)
- **`Dockerfile`**: Multi-stage build without Ray
- **`docker-compose.yml`**: Includes MongoDB Atlas Local + optional Ray service
- **`requirements.txt`**: Clean dependencies (Ray excluded by default)

## 🎯 Key Features

### ✅ Clean FastAPI Application
- No Ray dependencies required (works out of the box)
- Uses real MongoDB (Motor) for persistent storage
- Proper database initialization and index management
- Experiment-scoped data isolation

### ✅ MongoDB Atlas Local
- Full index management support (vector search, Lucene search, standard indexes)
- Data persistence via Docker volumes
- Production-ready database setup

### ✅ Optional Ray Integration
- Ray service available via docker-compose (commented out by default)
- Uncomment `ray-head` service in `docker-compose.yml` to enable
- Distributed computing support when needed

## 🔧 Configuration

### Environment Variables

- `MONGO_URI`: MongoDB connection string (default: `mongodb://mongo:27017/`)
- `DB_NAME`: Database name (default: `labs_db`)
- `PORT`: Application port (default: `8000`)
- `LOG_LEVEL`: Logging level (default: `INFO`)

### Enabling Ray (Optional)

To enable Ray support:

1. **Edit `docker-compose.yml`** and uncomment the `ray-head` service
2. **Update `app` service** to depend on `ray-head`:
   ```yaml
   depends_on:
     mongo:
       condition: service_healthy
     ray-head:
       condition: service_healthy  # Uncomment this
   ```
3. **Rebuild and start:**
   ```bash
   docker-compose up --build
   ```

## 📈 Scaling This Experiment

### Horizontal Scaling (Multiple Instances)

1. **Scale the FastAPI application:**
   ```bash
   docker-compose up --scale app=3
   ```
   Use a load balancer (nginx, traefik) in front of multiple app instances.

2. **For Ray workloads**, connect multiple Ray workers:
   ```bash
   # Start Ray head node
   docker-compose up ray-head
   
   # Connect worker nodes (on different machines)
   ray start --address=<ray-head-ip>:6379
   ```

### Vertical Scaling (Single Instance)

1. **Increase container resources:**
   ```yaml
   # In docker-compose.yml
   app:
     deploy:
       resources:
         limits:
           cpus: '4'
           memory: 8G
   ```

2. **Use multiple Uvicorn workers:**
   ```bash
   # Instead of python main.py
   uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
   ```

### Database Scaling

1. **MongoDB Replica Set:**
   - Replace single MongoDB instance with a replica set
   - Update `MONGO_URI` to use replica set connection string

2. **MongoDB Atlas Cloud:**
   - Point `MONGO_URI` to your Atlas cluster
   - Remove local MongoDB service from docker-compose.yml

### Production Deployment

1. **Use a production WSGI server:**
   ```bash
   pip install gunicorn
   gunicorn main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
   ```

2. **Add reverse proxy (nginx):**
   ```nginx
   upstream app {{
       server app:8000;
   }}
   
   server {{
       listen 80;
       server_name your-domain.com;
       
       location / {{
           proxy_pass http://app;
       }}
   }}
   ```

3. **Enable HTTPS:**
   - Use Let's Encrypt with certbot
   - Or terminate SSL at the load balancer

4. **Monitoring & Logging:**
   - Add Prometheus metrics endpoint
   - Integrate with logging aggregation (ELK, Loki)
   - Set up health checks and alerts

## 🐛 Troubleshooting

### MongoDB Connection Issues

- **Check MongoDB is running:** `docker-compose ps`
- **Verify connection string:** Check `MONGO_URI` environment variable
- **Check logs:** `docker-compose logs mongo`

### Ray Issues

- **Ray not available:** This is expected if Ray service is commented out. The app works without Ray.
- **Enable Ray:** Uncomment Ray service in docker-compose.yml
- **Ray dashboard:** http://localhost:8265 (if Ray is enabled)

### Index Management

- **Indexes not created:** Check MongoDB logs for index creation errors
- **Vector search not working:** Ensure MongoDB Atlas Local supports vector search indexes
- **Verify indexes:** Connect to MongoDB and run `db.getCollection('{slug_id}_<collection>').getIndexes()`

## 📚 Next Steps

1. **Review the experiment code** in `experiments/{slug_id}/`
2. **Customize configuration** as needed
3. **Add environment-specific settings** via `.env` file
4. **Set up CI/CD** for automated deployments
5. **Configure monitoring** and alerting
6. **Scale based on traffic** using the guidelines above

## 🔗 Resources

- **FastAPI Documentation:** https://fastapi.tiangolo.com
- **MongoDB Atlas Local:** https://www.mongodb.com/docs/atlas/atlas-local/
- **Ray Documentation:** https://docs.ray.io
- **Docker Compose:** https://docs.docker.com/compose/

## 📝 License

Same license as the original MDB-Gnome platform.

---

**Generated by MDB-Gnome Intelligent Export**
"""
  return readme_content


def _create_intelligent_export_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]]
) -> io.BytesIO:
  """
  Creates an intelligent export package:
  - Clean FastAPI application WITH Ray dependencies (Ray is required)
  - MongoDB Atlas Local via docker-compose
  - Ray is a core component and must be available
  - Comprehensive README with scaling instructions
  - Proper index management support
  """
  logger.info(f"Starting creation of intelligent export package for '{slug_id}'.")
  zip_buffer = io.BytesIO()
  experiment_path = source_dir / "experiments" / slug_id
  
  # --- 1. Generate intelligent standalone main.py (with real MongoDB) ---
  standalone_main_source = _make_intelligent_standalone_main_py(slug_id)
  
  # --- 2. Generate requirements WITH Ray (Ray is required) ---
  local_reqs_path = experiment_path / "requirements.txt"
  base_requirements = [
    "fastapi",
    "uvicorn[standard]",
    "motor>=3.0.0",
    "pymongo==4.15.3",
    "python-multipart",
    "jinja2",
    "ray[default]>=2.9.0",  # Ray is required - matches master requirements pattern
  ]
  
  all_requirements = base_requirements.copy()
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    for req in local_requirements:
      pkg_name = _extract_pkgname(req)
      # Ray is now in base requirements, so if it's in experiment requirements, use experiment version
      # Replace if exists, otherwise add
      all_requirements = [r for r in all_requirements if _extract_pkgname(r) != pkg_name]
      all_requirements.append(req)
  requirements_content = "\n".join(all_requirements)
  
  # --- 3. Generate Docker files ---
  dockerfile_content = _create_intelligent_dockerfile(slug_id, source_dir, experiment_path)
  docker_compose_content = _create_intelligent_docker_compose(slug_id)
  
  # --- 4. Generate comprehensive README ---
  experiment_name = db_data.get("name", slug_id)
  experiment_description = db_data.get("description", f"Standalone experiment: {slug_id}")
  readme_content = _create_intelligent_readme(slug_id, experiment_name, experiment_description)
  
  # --- 5. Create ZIP archive ---
  EXCLUSION_PATTERNS = [
    "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
  ]
  with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
    # Include experiment directory
    if experiment_path.is_dir():
      logger.debug(f"Including experiment code from: {experiment_path}")
      for folder_name, _, file_names in os.walk(experiment_path):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue

        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue

          file_path = Path(folder_name) / file_name
          try:
            arcname = f"experiments/{slug_id}/{file_path.relative_to(experiment_path)}"
          except ValueError:
            logger.error(f"Failed to get relative path for {file_path}")
            continue

          if file_path.suffix in (".html", ".htm"):
            original_html = file_path.read_text(encoding="utf-8")
            fixed_html = _fix_static_paths(original_html, slug_id)
            zf.writestr(arcname, fixed_html)
          else:
            zf.write(file_path, arcname)

    # Add __init__.py for experiments package
    experiments_init = source_dir / "experiments" / "__init__.py"
    if experiments_init.is_file():
      zf.write(experiments_init, "experiments/__init__.py")
    
    # Add async_mongo_wrapper.py (required for scoped access)
    mongo_wrapper = source_dir / "async_mongo_wrapper.py"
    if mongo_wrapper.is_file():
      zf.write(mongo_wrapper, "async_mongo_wrapper.py")
      logger.debug("Included async_mongo_wrapper.py")
    else:
      logger.warning("async_mongo_wrapper.py not found - export may not work correctly")
    
    # Add experiment_db.py (required for actor initialization)
    experiment_db = source_dir / "experiment_db.py"
    if experiment_db.is_file():
      zf.write(experiment_db, "experiment_db.py")
      logger.debug("Included experiment_db.py")
    else:
      logger.warning("experiment_db.py not found - export may not work correctly")

    # Add generated files
    zf.writestr("Dockerfile", dockerfile_content)
    zf.writestr("docker-compose.yml", docker_compose_content)
    zf.writestr("db_config.json", json.dumps(db_data, indent=2))
    zf.writestr("db_collections.json", json.dumps(db_collections, indent=2))
    zf.writestr("main.py", standalone_main_source)
    zf.writestr("requirements.txt", requirements_content)
    zf.writestr("README.md", readme_content)

  zip_buffer.seek(0)
  logger.info(f"Intelligent export package created successfully for '{slug_id}'.")
  return zip_buffer


# -----------------------------------------------------
# NEW: Public Standalone Export Endpoint
# -----------------------------------------------------
public_api_router = APIRouter(prefix="/api", tags=["Public API"])


def _build_absolute_https_url(request: Request, relative_url: str) -> str:
  """
  Build an absolute URL from a relative URL, handling proxy headers intelligently.
  
  Uses request.state values from ProxyAwareHTTPSMiddleware if available,
  falls back to checking proxy headers directly.
  
  Args:
    request: FastAPI Request object
    relative_url: Relative URL path (e.g., "/exports/file.zip") or absolute URL
    
  Returns:
    Absolute URL (HTTPS in production when behind proxy, respects actual scheme in dev)
  """
  # Handle absolute URLs
  if not relative_url.startswith("/"):
    # Already absolute, but ensure HTTPS in production (but not on localhost)
    if relative_url.startswith("http://"):
      G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()
      host = request.url.hostname
      is_localhost = host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
      
      if G_NOME_ENV == "production" and not is_localhost:
        https_url = relative_url.replace("http://", "https://", 1)
        logger.warning(f"Forced HTTPS on absolute URL: {relative_url} -> {https_url}")
        return https_url
    return relative_url
  
  # Use detected scheme/host from proxy middleware if available
  if hasattr(request.state, "detected_scheme") and hasattr(request.state, "detected_host"):
    scheme = request.state.detected_scheme
    host = request.state.detected_host
    # Include port if it's non-standard (not 80 for HTTP, not 443 for HTTPS)
    detected_port = getattr(request.state, "detected_port", None)
    if detected_port:
      default_port = 443 if scheme == "https" else 80
      if detected_port != default_port:
        host = f"{host}:{detected_port}"
  else:
    # Fallback: check proxy headers directly
    is_localhost = request.url.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    has_proxy_headers = any((
      request.headers.get("X-Forwarded-Proto"),
      request.headers.get("X-Forwarded-Host"),
      request.headers.get("Forwarded"),
      request.headers.get("X-Forwarded-Ssl")
    ))
    
    # On localhost without proxy headers, respect the original scheme
    # If the request came in as HTTPS, use HTTPS; if HTTP, use HTTP
    if is_localhost and not has_proxy_headers:
      # Check the original scheme from request.state if available, otherwise use request.url.scheme
      if hasattr(request.state, "original_scheme"):
        scheme = request.state.original_scheme
      else:
        scheme = request.url.scheme
    else:
      # Behind proxy or real server - trust proxy headers
      scheme = "https" if (
        request.url.scheme == "https" or 
        request.headers.get("X-Forwarded-Proto", "").lower() == "https" or
        request.headers.get("X-Forwarded-Ssl", "").lower() == "on"
      ) else request.url.scheme
    
    host = (
      request.headers.get("X-Forwarded-Host") or 
      request.headers.get("Host") or 
      request.url.hostname
    )
    # Include port if it's non-standard for localhost requests
    if request.url.port:
      default_port = 443 if scheme == "https" else 80
      if request.url.port != default_port:
        # Remove port from host if it's already there, then add correct port
        host = host.split(":")[0] if ":" in host else host
        host = f"{host}:{request.url.port}"
  
  # Force HTTPS in production, but NOT on localhost (server may not have SSL)
  G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()
  is_localhost = host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]") or (host and "localhost" in host.lower())
  
  if G_NOME_ENV == "production" and scheme != "https" and not is_localhost:
    scheme = "https"
    logger.debug(f"Forcing HTTPS URL in production environment (non-localhost)")
  
  # Build absolute URL
  absolute_url = f"{scheme}://{host}{relative_url}"
  return absolute_url


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
      # If auth is required and no user is logged in, redirect to login
      current_path = quote(request.url.path)
      login_url = request.url_for("login_get", next=current_path)
      # Use 302 Found or 303 See Other for redirects after unauthenticated access attempt
      response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
      return response

  # 3. Perform Intelligent Packaging
  try:
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data
    )

    user_email = user.get('email', 'Guest') if user else 'Guest'
    file_name = f"{slug_id}_intelligent_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_size = zip_buffer.getbuffer().nbytes
    
    # Upload to B2 if enabled, otherwise save locally
    b2_file_name = None
    download_url = None
    b2_bucket = getattr(request.app.state, "b2_bucket", None)
    
    if B2_ENABLED and b2_bucket:
      try:
        # Upload to B2
        b2_file_name = f"exports/{file_name}"
        logger.info(f"[{slug_id}] Uploading export to B2: {b2_file_name}")
        await _upload_export_to_b2(b2_bucket, zip_buffer, b2_file_name)
        
        # Generate presigned download URL from B2
        download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
        logger.info(f"[{slug_id}] Export uploaded to B2. Generated presigned URL.")
      except Exception as e:
        logger.error(f"[{slug_id}] B2 upload failed, falling back to local storage: {e}", exc_info=True)
        # Fallback to local storage
        export_file_path = await _save_export_locally(zip_buffer, file_name)
        relative_url = str(request.url_for("exports", filename=file_name))
        download_url = _build_absolute_https_url(request, relative_url)
        logger.info(f"[{slug_id}] Export saved locally as fallback.")
    else:
      # Save to local temp directory (fallback if B2 not enabled)
      logger.info(f"[{slug_id}] B2 not enabled, saving export locally: {file_name}")
      export_file_path = await _save_export_locally(zip_buffer, file_name)
      relative_url = str(request.url_for("exports", filename=file_name))
      download_url = _build_absolute_https_url(request, relative_url)
    
    # Trigger cleanup of old exports in background (non-blocking)
    asyncio.create_task(_cleanup_old_exports(max_age_hours=24))
    
    # Log export to database
    export_log_id = await _log_export(
      db=db,
      slug_id=slug_id,
      export_type="intelligent",
      user_email=user_email,
      local_file_path=str(export_file_path.relative_to(BASE_DIR)) if 'export_file_path' in locals() else None,
      file_size=file_size,
      b2_file_name=b2_file_name,
      invalidated=False
    )
    
    logger.info(f"[{slug_id}] Export created successfully. Download URL generated.")
    # Return JSON with download URL
    return JSONResponse({
      "status": "success",
      "download_url": str(download_url),
      "filename": file_name,
      "export_id": str(export_log_id) if export_log_id else None,
      "message": "Export ready for download"
    })
  except ValueError as e:
    logger.error(f"Error packaging experiment '{slug_id}': {e}")
    raise HTTPException(status_code=404, detail=str(e))
  except Exception as e:
    logger.error(f"Unexpected error packaging '{slug_id}': {e}", exc_info=True)
    raise HTTPException(500, "Unexpected server error during packaging.")


@public_api_router.get("/package-docker/{slug_id}", name="package_docker")
async def package_docker_experiment(
  request: Request,
  slug_id: str,
  user: Optional[Mapping[str, Any]] = Depends(get_current_user) # Allows unauthenticated access
):
  """
  Docker export endpoint specifically for data_imaging experiment.
  Returns a ZIP package with Dockerfile and docker-compose.yml.
  Backwards compatible - existing standalone export still works.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db

  # 1. Check Experiment Configuration
  config = await db.experiments_config.find_one({"slug": slug_id})
  if not config or config.get("status") != "active":
    raise HTTPException(status_code=404, detail="Experiment not found or not active.")

  auth_required = config.get("auth_required", False)

  # 2. Enforce Authentication if required
  if auth_required:
    if not user:
      # If auth is required and no user is logged in, redirect to login
      current_path = quote(request.url.path)
      login_url = request.url_for("login_get", next=current_path)
      # Use 302 Found or 303 See Other for redirects after unauthenticated access attempt
      response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
      return response

  # 3. Perform Intelligent Packaging (Docker-enabled)
  try:
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data
    )

    user_email = user.get('email', 'Guest') if user else 'Guest'
    file_name = f"{slug_id}_intelligent_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_size = zip_buffer.getbuffer().nbytes
    
    # Upload to B2 if enabled, otherwise save locally
    b2_file_name = None
    download_url = None
    b2_bucket = getattr(request.app.state, "b2_bucket", None)
    
    if B2_ENABLED and b2_bucket:
      try:
        # Upload to B2
        b2_file_name = f"exports/{file_name}"
        logger.info(f"[{slug_id}] Uploading export to B2: {b2_file_name}")
        await _upload_export_to_b2(b2_bucket, zip_buffer, b2_file_name)
        
        # Generate presigned download URL from B2
        download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
        logger.info(f"[{slug_id}] Export uploaded to B2. Generated presigned URL.")
      except Exception as e:
        logger.error(f"[{slug_id}] B2 upload failed, falling back to local storage: {e}", exc_info=True)
        # Fallback to local storage
        export_file_path = await _save_export_locally(zip_buffer, file_name)
        relative_url = str(request.url_for("exports", filename=file_name))
        download_url = _build_absolute_https_url(request, relative_url)
        logger.info(f"[{slug_id}] Export saved locally as fallback.")
    else:
      # Save to local temp directory (fallback if B2 not enabled)
      logger.info(f"[{slug_id}] B2 not enabled, saving export locally: {file_name}")
      export_file_path = await _save_export_locally(zip_buffer, file_name)
      relative_url = str(request.url_for("exports", filename=file_name))
      download_url = _build_absolute_https_url(request, relative_url)
    
    # Trigger cleanup of old exports in background (non-blocking)
    asyncio.create_task(_cleanup_old_exports(max_age_hours=24))
    
    # Log export to database
    export_log_id = await _log_export(
      db=db,
      slug_id=slug_id,
      export_type="intelligent",
      user_email=user_email,
      local_file_path=str(export_file_path.relative_to(BASE_DIR)) if 'export_file_path' in locals() else None,
      file_size=file_size,
      b2_file_name=b2_file_name,
      invalidated=False
    )
    
    logger.info(f"[{slug_id}] Docker export created successfully. Download URL generated.")
    # Return JSON with download URL
    return JSONResponse({
      "status": "success",
      "download_url": str(download_url),
      "filename": file_name,
      "export_id": str(export_log_id) if export_log_id else None,
      "message": "Export ready for download"
    })
  except ValueError as e:
    logger.error(f"Error packaging Docker export for '{slug_id}': {e}")
    raise HTTPException(status_code=404, detail=str(e))
  except Exception as e:
    logger.error(f"Unexpected error packaging Docker export for '{slug_id}': {e}", exc_info=True)
    raise HTTPException(500, "Unexpected server error during Docker packaging.")


# Export file serving endpoint (with cleanup check)
@public_api_router.get("/export/{filename:path}", name="exports")
async def serve_export_file(filename: str, request: Request):
  """
  Serves export files from temp directory or B2.
  Checks if export is invalidated and returns 410 Gone if so.
  Files are automatically cleaned up after 24 hours.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  try:
    # Security: prevent directory traversal
    safe_filename = Path(filename).name
    if safe_filename != filename or ".." in filename:
      raise HTTPException(status_code=400, detail="Invalid filename")
    
    # Check if export is invalidated in database
    export_log = await db.export_logs.find_one({
      "$or": [
        {"local_file_path": {"$regex": safe_filename}},
        {"b2_file_name": {"$regex": safe_filename}}
      ]
    })
    
    if export_log and export_log.get("invalidated", False):
      logger.info(f"Export file '{safe_filename}' is invalidated, returning 410 Gone")
      raise HTTPException(status_code=410, detail="Export has been invalidated and is no longer available")
    
    # Try to serve from local temp directory (fallback)
    export_file = EXPORTS_TEMP_DIR / safe_filename
    if export_file.exists() and export_file.is_file():
      # Check if file is too old (24 hours)
      file_age_hours = (datetime.datetime.now().timestamp() - export_file.stat().st_mtime) / 3600
      if file_age_hours > 24:
        logger.info(f"Export file expired (age: {file_age_hours:.1f}h), deleting: {safe_filename}")
        export_file.unlink()
        raise HTTPException(status_code=404, detail="Export file expired")
      
      return FileResponse(
        path=str(export_file),
        filename=safe_filename,
        media_type="application/zip",
        headers={
          "Content-Disposition": f'attachment; filename="{safe_filename}"',
        }
      )
    
    # If not found locally and B2 is enabled, check if it's in B2
    # But we can't serve directly from B2 here - user should use presigned URL
    raise HTTPException(status_code=404, detail="Export file not found or expired")
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error serving export file '{filename}': {e}", exc_info=True)
    raise HTTPException(status_code=500, detail="Error serving export file")


app.include_router(public_api_router)
# -----------------------------------------------------


@admin_router.get("/package/{slug_id}", name="package_experiment")
async def package_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_admin)):
  """
  NOTE: This is the original, restricted admin route.
  It is kept for separation, even though it currently mirrors the public logic.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  try:
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data
    )
    
    user_email = user.get('email', 'Unknown')
    file_name = f"{slug_id}_intelligent_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_size = zip_buffer.getbuffer().nbytes
    
    # Upload to B2 if enabled, otherwise save locally
    b2_file_name = None
    download_url = None
    b2_bucket = getattr(request.app.state, "b2_bucket", None)
    
    if B2_ENABLED and b2_bucket:
      try:
        # Upload to B2
        b2_file_name = f"exports/{file_name}"
        logger.info(f"[{slug_id}] Uploading export to B2: {b2_file_name}")
        await _upload_export_to_b2(b2_bucket, zip_buffer, b2_file_name)
        
        # Generate presigned download URL from B2
        download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
        logger.info(f"[{slug_id}] Export uploaded to B2. Generated presigned URL.")
      except Exception as e:
        logger.error(f"[{slug_id}] B2 upload failed, falling back to local storage: {e}", exc_info=True)
        # Fallback to local storage
        export_file_path = await _save_export_locally(zip_buffer, file_name)
        relative_url = str(request.url_for("exports", filename=file_name))
        download_url = _build_absolute_https_url(request, relative_url)
        logger.info(f"[{slug_id}] Export saved locally as fallback.")
    else:
      # Save to local temp directory (fallback if B2 not enabled)
      logger.info(f"[{slug_id}] B2 not enabled, saving export locally: {file_name}")
      export_file_path = await _save_export_locally(zip_buffer, file_name)
      relative_url = str(request.url_for("exports", filename=file_name))
      download_url = _build_absolute_https_url(request, relative_url)
    
    # Trigger cleanup of old exports in background (non-blocking)
    asyncio.create_task(_cleanup_old_exports(max_age_hours=24))
    
    # Log export to database
    export_log_id = await _log_export(
      db=db,
      slug_id=slug_id,
      export_type="intelligent",
      user_email=user_email,
      local_file_path=str(export_file_path.relative_to(BASE_DIR)) if 'export_file_path' in locals() else None,
      file_size=file_size,
      b2_file_name=b2_file_name,
      invalidated=False
    )
    
    logger.info(f"[{slug_id}] Export created successfully. Download URL generated.")
    # Return JSON with download URL
    return JSONResponse({
      "status": "success",
      "download_url": str(download_url),
      "filename": file_name,
      "export_id": str(export_log_id) if export_log_id else None,
      "message": "Export ready for download"
    })
  except ValueError as e:
    logger.error(f"Error packaging experiment '{slug_id}': {e}")
    raise HTTPException(status_code=404, detail=str(e))
  except Exception as e:
    logger.error(f"Unexpected error packaging '{slug_id}': {e}", exc_info=True)
    raise HTTPException(500, "Unexpected server error during packaging.")


@admin_router.get("/api/exports", response_class=JSONResponse, name="list_exports")
async def list_exports(
  request: Request,
  user: Dict[str, Any] = Depends(require_admin),
  slug_id: Optional[str] = Query(None)
):
  """
  Lists all exports, optionally filtered by slug_id.
  Returns export information including invalidated status.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  try:
    query = {}
    if slug_id:
      query["slug_id"] = slug_id
    
    exports = []
    async for export_log in db.export_logs.find(query).sort("created_at", -1):
      export_dict = {
        "_id": str(export_log["_id"]),
        "slug_id": export_log.get("slug_id"),
        "export_type": export_log.get("export_type"),
        "user_email": export_log.get("user_email"),
        "file_size": export_log.get("file_size"),
        "b2_file_name": export_log.get("b2_file_name"),
        "local_file_path": export_log.get("local_file_path"),
        "invalidated": export_log.get("invalidated", False),
        "created_at": export_log.get("created_at").isoformat() if export_log.get("created_at") else None,
      }
      exports.append(export_dict)
    
    return JSONResponse({
      "status": "success",
      "exports": exports,
      "count": len(exports)
    })
  except Exception as e:
    logger.error(f"Error listing exports: {e}", exc_info=True)
    raise HTTPException(status_code=500, detail=f"Error listing exports: {e}")


@admin_router.post("/api/exports/{export_id}/invalidate", response_class=JSONResponse, name="invalidate_export")
async def invalidate_export(
  request: Request,
  export_id: str,
  user: Dict[str, Any] = Depends(require_admin),
  delete_from_b2: bool = Body(True, embed=True)
):
  """
  Invalidates an export and optionally deletes it from B2.
  This marks the export as invalidated in the database and disables the download link.
  If delete_from_b2 is True and the export is in B2, it will be deleted from B2 as well.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  b2_bucket = getattr(request.app.state, "b2_bucket", None)
  
  try:
    from bson import ObjectId
    
    # Find the export
    export_log = await db.export_logs.find_one({"_id": ObjectId(export_id)})
    if not export_log:
      raise HTTPException(status_code=404, detail="Export not found")
    
    if export_log.get("invalidated", False):
      return JSONResponse({
        "status": "success",
        "message": "Export was already invalidated",
        "export_id": export_id
      })
    
    # Delete from B2 if requested and file exists in B2
    b2_deleted = False
    if delete_from_b2 and B2_ENABLED and b2_bucket and export_log.get("b2_file_name"):
      try:
        b2_file_name = export_log["b2_file_name"]
        # Delete file from B2
        try:
          file_version = b2_bucket.get_file_info_by_name(b2_file_name)
          if file_version:
            b2_bucket.delete_file_version(file_version.id_, file_version.file_name)
            b2_deleted = True
            logger.info(f"Deleted export from B2: {b2_file_name}")
        except B2Error as b2_err:
          # File might not exist anymore - that's okay
          if "not found" in str(b2_err).lower() or "does not exist" in str(b2_err).lower():
            logger.info(f"Export file already deleted from B2: {b2_file_name}")
          else:
            raise
      except B2Error as e:
        logger.warning(f"Failed to delete export from B2: {e}")
      except Exception as e:
        logger.warning(f"Error deleting export from B2: {e}", exc_info=True)
    
    # Mark as invalidated in database
    await db.export_logs.update_one(
      {"_id": ObjectId(export_id)},
      {"$set": {"invalidated": True, "invalidated_at": datetime.datetime.utcnow(), "invalidated_by": user.get("email")}}
    )
    
    logger.info(f"Export {export_id} invalidated by {user.get('email')}")
    
    return JSONResponse({
      "status": "success",
      "message": "Export invalidated successfully",
      "export_id": export_id,
      "b2_deleted": b2_deleted
    })
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error invalidating export {export_id}: {e}", exc_info=True)
    raise HTTPException(status_code=500, detail=f"Error invalidating export: {e}")


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

  # Fetch export counts for all configured experiments
  export_counts: Dict[str, int] = {}
  try:
    export_aggregation = await db.export_logs.aggregate([
      {"$group": {
        "_id": "$slug_id",
        "count": {"$sum": 1}
      }}
    ]).to_list(length=None)
    export_counts = {item["_id"]: item["count"] for item in export_aggregation if item.get("_id")}
  except Exception as e:
    logger.warning(f"Error fetching export counts: {e}", exc_info=True)

  db_slug_map = {cfg.get("slug"): cfg for cfg in db_configs_list if cfg.get("slug")}
  configured_experiments: List[Dict[str, Any]] = []
  discovered_slugs: List[str] = []
  orphaned_configs: List[Dict[str, Any]] = []

  for slug in code_slugs:
    if slug in db_slug_map:
      cfg = db_slug_map[slug]
      cfg["code_found"] = True
      cfg["export_count"] = export_counts.get(slug, 0)
      configured_experiments.append(cfg)
    else:
      discovered_slugs.append(slug)

  for cfg in db_configs_list:
    if cfg.get("slug") not in code_slugs:
      cfg["code_found"] = False
      cfg["export_count"] = export_counts.get(cfg.get("slug", ""), 0)
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

  file_tree = await _scan_directory(experiment_path, experiment_path)

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
        actor_content = await _read_file_async(actor_path)
        discovery_info["defines_actor_class"] = "class ExperimentActor" in actor_content
      except Exception as e:
        logger.warning(f"Could not read actor.py: {e}")

    reqs_path = experiment_path / "requirements.txt"
    discovery_info["has_requirements"] = reqs_path.is_file()
    if discovery_info["has_requirements"]:
      try:
        discovery_info["requirements"] = await _parse_requirements_file(reqs_path)
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
    content = await _read_file_async(file_path)
    logger.debug(f"Read text content for file: {file_path}")
    return JSONResponse({"content": content, "is_binary": False})
  except UnicodeDecodeError:
    logger.debug(f"File detected as binary: {file_path}")
    return JSONResponse({"content": "[Binary file - Content not displayed]", "is_binary": True})
  except UnicodeError:
    # Handle other unicode errors gracefully
    logger.debug(f"File detected as binary (UnicodeError): {file_path}")
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

  b2_bucket = getattr(request.app.state, "b2_bucket", None)
  if not B2_ENABLED or not b2_bucket:
    logger.error(f"Cannot upload '{slug_id}': B2 not configured or no B2 bucket.")
    raise HTTPException(501, "B2 not configured; upload impossible.")

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
    # Upload using B2 SDK (native client always uses HTTPS)
    b2_bucket_instance = getattr(request.app.state, "b2_bucket", None)
    if not b2_bucket_instance:
      raise ValueError("B2 bucket not available")
    b2_bucket_instance.upload_bytes(zip_data, b2_object_key)
    logger.info(f"[{slug_id}] B2 upload successful. Generating presigned URL...")
    # B2 SDK always returns HTTPS URLs
    b2_final_uri = _generate_presigned_download_url(b2_bucket_instance, b2_object_key)
  except B2Error as e:
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
    if len(active_cfgs) == 0:
      logger.warning(" ⚠️  No active experiments found! Check experiment status in database.")
      # Try to list all experiments to help debug
      all_cfgs = await db.experiments_config.find({}).to_list(None)
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
      
      # Call initialize hook if it exists (for post-startup tasks like data seeding)
      if hasattr(actor_cls, "initialize"):
        try:
          asyncio.create_task(_call_actor_initialize(actor_handle, slug))
          logger.info(f"[{slug}] Scheduled post-initialization task for actor '{actor_name}'.")
        except Exception as e:
          logger.warning(f"[{slug}] Failed to schedule actor initialization: {e}")
    except Exception as e:
      logger.error(f"[{slug}] Actor start error: {e}", exc_info=True)
      continue


async def _call_actor_initialize(actor_handle: Any, slug: str):
  """
  Helper to call the actor's initialize method asynchronously.
  This allows actors to perform post-initialization tasks like
  waiting for indexes and seeding initial data.
  """
  try:
    logger.info(f"[{slug}] Calling actor initialize hook...")
    await actor_handle.initialize.remote()
    logger.info(f"[{slug}] Actor initialize hook completed.")
  except Exception as e:
    logger.error(f"[{slug}] Actor initialize hook failed: {e}", exc_info=True)


def _normalize_json_def(obj: Any) -> Any:
  """
  Normalize a JSON-serializable object for comparison by:
  1. Converting to JSON string (which sorts dict keys)
  2. Parsing back to dict/list
  This makes comparisons order-insensitive and format-insensitive.
  """
  try:
    return json.loads(json.dumps(obj, sort_keys=True))
  except (TypeError, ValueError) as e:
    # If it can't be serialized, return as-is for fallback comparison
    logger.warning(f"Could not normalize JSON def: {e}")
    return obj


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
  except Exception as e:
    logger.error(f"{log_prefix} IndexManager init error: {e}", exc_info=True)
    return

  for index_def in index_definitions:
    index_name = index_def.get("name")
    index_type = index_def.get("type")
    try:
      if index_type == "regular":
        keys = index_def.get("keys")
        if not keys:
          logger.warning(f"{log_prefix} Missing 'keys' on index '{index_name}'.")
          continue
        
        # Check if this is an _id index (MongoDB creates these automatically)
        is_id_index = False
        if isinstance(keys, dict):
          is_id_index = len(keys) == 1 and "_id" in keys
        elif isinstance(keys, list):
          is_id_index = len(keys) == 1 and keys[0][0] == "_id"
        
        if is_id_index:
          # _id indexes are automatically created by MongoDB and can't be customized
          # We skip creation attempts since MongoDB handles this automatically
          logger.info(f"{log_prefix} Skipping '_id' index '{index_name}' - MongoDB creates _id indexes automatically and they can't be customized.")
          continue
        
        # For non-_id indexes, process options normally but filter invalid ones
        options = {**index_def.get("options", {}), "name": index_name}
        
        existing_index = await index_manager.get_index(index_name)
        if existing_index:
          # Compare keys to see if they differ
          tmp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
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
        if existing_index:
          current_def = existing_index.get("latestDefinition", existing_index.get("definition"))
          # Normalize both definitions for order-insensitive comparison
          normalized_current = _normalize_json_def(current_def)
          normalized_expected = _normalize_json_def(definition)
          
          if normalized_current == normalized_expected:
            logger.info(f"{log_prefix} Search index '{index_name}' definition matches.")
            if not existing_index.get("queryable") and existing_index.get("status") != "FAILED":
              logger.info(f"{log_prefix} Index '{index_name}' not queryable yet; waiting.")
              await index_manager._wait_for_search_index_ready(index_name, index_manager.DEFAULT_SEARCH_TIMEOUT)
              logger.info(f"{log_prefix} Index '{index_name}' now ready.")
            elif existing_index.get("status") == "FAILED":
              logger.error(f"{log_prefix} Index '{index_name}' state=FAILED. Manual fix needed.")
            else:
              logger.info(f"{log_prefix} Index '{index_name}' is ready.")
          else:
            logger.warning(f"{log_prefix} Search index '{index_name}' definition changed; updating.")
            # Extract field paths for clearer logging
            current_fields = normalized_current.get('fields', []) if isinstance(normalized_current, dict) else []
            expected_fields = normalized_expected.get('fields', []) if isinstance(normalized_expected, dict) else []
            
            current_paths = [f.get('path', '?') for f in current_fields if isinstance(f, dict)]
            expected_paths = [f.get('path', '?') for f in expected_fields if isinstance(f, dict)]
            
            logger.info(f"{log_prefix} Current index filter fields: {current_paths}")
            logger.info(f"{log_prefix} Expected index filter fields: {expected_paths}")
            logger.info(f"{log_prefix} Updating index '{index_name}' with new definition (this may take a few moments)...")
            
            try:
              await index_manager.update_search_index(name=index_name, definition=definition, wait_for_ready=True)
              logger.info(f"{log_prefix} ✅ Successfully updated search index '{index_name}'. Index is now ready.")
            except Exception as update_err:
              logger.error(f"{log_prefix} ❌ Failed to update search index '{index_name}': {update_err}", exc_info=True)
              # Don't re-raise - log and continue, the index might still work
        else:
          logger.info(f"{log_prefix} Creating new search index '{index_name}'...")
          await index_manager.create_search_index(name=index_name, definition=definition, index_type=index_type, wait_for_ready=True)
          logger.info(f"{log_prefix} Created new '{index_type}' index '{index_name}'.")
      else:
        logger.warning(f"{log_prefix} Unknown index type '{index_type}'; skipping.")
    except Exception as e:
      logger.error(f"{log_prefix} Error managing index '{index_name}': {e}", exc_info=True)


@app.get("/", response_class=HTMLResponse, name="home")
async def root(request: Request, user: Optional[Mapping[str, Any]] = Depends(get_current_user)):
  """Root route - serves the home page with list of experiments."""
  try:
    if not templates:
      logger.error("Template engine not available for root route")
      raise HTTPException(500, "Template engine not available.")
    
    experiments = getattr(request.app.state, "experiments", {})
    logger.debug(f"Root route accessed. Found {len(experiments)} registered experiment(s).")
    
    # Convert relative experiment URLs to absolute HTTPS URLs
    experiments_with_https_urls = {}
    for slug, meta in experiments.items():
      meta_copy = dict(meta)
      if "url" in meta_copy and meta_copy["url"]:
        # Convert relative URL to absolute HTTPS URL
        meta_copy["url"] = _build_absolute_https_url(request, meta_copy["url"])
      experiments_with_https_urls[slug] = meta_copy
    
    return templates.TemplateResponse("index.html", {
      "request": request,
      "experiments": experiments_with_https_urls,
      "current_user": user,
      "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error in root route: {e}", exc_info=True)
    raise HTTPException(500, f"Server error: {e}")


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
    proxy_headers=True,  
    forwarded_allow_ips="*" 
  )