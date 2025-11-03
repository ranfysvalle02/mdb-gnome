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
import re  # Needed for requirement parsing
import hashlib  # For checksum calculation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import quote

# ============================================================================
# MODULAR IMPORTS - Configuration and Core Dependencies
# ============================================================================
from config import (
    BASE_DIR,
    EXPERIMENTS_DIR,
    TEMPLATES_DIR,
    EXPORTS_TEMP_DIR,
    ENABLE_REGISTRATION,
    MONGO_URI,
    DB_NAME,
    SECRET_KEY,
    ADMIN_EMAIL_DEFAULT,
    ADMIN_PASSWORD_DEFAULT,
    B2_APPLICATION_KEY_ID,
    B2_APPLICATION_KEY,
    B2_BUCKET_NAME,
    B2_ENDPOINT_URL,
    B2_ENABLED,
    B2SDK_AVAILABLE,
    RAY_AVAILABLE,
    HAVE_MONGO_WRAPPER,
    INDEX_MANAGER_AVAILABLE,
    AIOFILES_AVAILABLE,
    InMemoryAccountInfo,
    B2Api,
    B2Error,
    ScopedMongoWrapper,
    AsyncAtlasIndexManager,
)

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
  StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

# Database imports (Motor for async MongoDB)
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Third-party for dependency parsing
try:
    import pkg_resources  # Used for robust requirement parsing
except ImportError:
  pass

# ============================================================================
# MODULAR IMPORTS - Application Modules
# ============================================================================
# Ray decorator
from ray_decorator import ray_actor

# Middleware
from middleware import (
    ExperimentScopeMiddleware,
    ProxyAwareHTTPSMiddleware,
    HTTPSEnforcementMiddleware,
)
from request_id_middleware import RequestIDMiddleware

# Rate limiting
from rate_limit import (
    limiter,
    LOGIN_POST_LIMIT,
    LOGIN_GET_LIMIT,
    REGISTER_POST_LIMIT,
    REGISTER_GET_LIMIT,
    EXPORT_LIMIT,
    EXPORT_FILE_LIMIT,
    rate_limit_exceeded_handler,
)
from slowapi.errors import RateLimitExceeded

# Background tasks
from background_tasks import (
    BackgroundTaskManager,
    safe_background_task as _safe_background_task,
    get_task_manager,
)

# Utilities
from utils import (
    read_file_async as _read_file_async,
    read_json_async as _read_json_async,
    write_file_async as _write_file_async,
    calculate_dir_size_sync as _calculate_dir_size_sync,
    scan_directory as _scan_directory,
    scan_directory_sync as _scan_directory_sync,
    secure_path as _secure_path,
    build_absolute_https_url as _build_absolute_https_url,
    make_json_serializable as _make_json_serializable,
    should_use_secure_cookie,
)

# B2 utilities
from b2_utils import (
    generate_presigned_download_url as _generate_presigned_download_url,
    upload_export_to_b2 as _upload_export_to_b2,
)

# Lifespan management
from lifespan import lifespan, get_templates

# Manifest validation
from manifest_schema import validate_manifest, validate_manifest_with_db, validate_managed_indexes, validate_developer_id

# Database initialization and seeding
from database import (
    ensure_db_indices,
    seed_admin,
    seed_db_from_local_files,
)

# Index management
from index_management import (
    normalize_json_def as _normalize_json_def,
    run_index_creation_for_collection as _run_index_creation_for_collection,
)
from role_management import (
    assign_role_to_user,
    remove_role_from_user,
)

# Export helpers
from export_helpers import (
    estimate_export_size as _estimate_export_size,
    should_use_disk_streaming as _should_use_disk_streaming,
    calculate_export_checksum as _calculate_export_checksum,
    find_existing_export_by_checksum as _find_existing_export_by_checksum,
    save_export_locally as _save_export_locally,
    cleanup_local_export_file as _cleanup_local_export_file,
    cleanup_old_exports as _cleanup_old_exports,
    log_export as _log_export,
    parse_requirements_file_sync as _parse_requirements_file_sync,
    parse_requirements_from_string as _parse_requirements_from_string,
    dump_db_to_json as _dump_db_to_json,
    make_intelligent_standalone_main_py as _make_intelligent_standalone_main_py,
    fix_static_paths as _fix_static_paths,
    create_zip_to_disk as _create_zip_to_disk,
    MASTER_REQUIREMENTS,
    _extract_pkgname,
)

# Ray integration (for lifespan)
if RAY_AVAILABLE:
    import ray
else:
    ray = None


# ============================
# Core application dependencies
try:
  from core_deps import (
    require_experiment_ownership_or_admin_dep,
    get_user_experiments,
    get_current_user,
    get_current_user_or_redirect,
    require_admin,
    require_admin_or_developer,
    get_scoped_db,
    get_experiment_config,
    get_authz_provider,
  )
  # ScopedMongoWrapper is imported above from async_mongo_wrapper
except ImportError as e:
  logging.critical(f" CRITICAL ERROR: Failed to import core dependencies: {e}")
  sys.exit(1)

# Pluggable Authorization imports
try:
  from authz_provider import AuthorizationProvider, CasbinAdapter
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
logger = logging.getLogger("modular_labs.main")

# B2 API instances (initialized in lifespan)
b2_api = None
b2_bucket = None
_b2_init_lock = asyncio.Lock()

# Export helper functions are now imported from export_helpers.py


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


# Templates are initialized in lifespan module and stored in app.state.templates
from lifespan import get_templates

def get_templates_from_request(request: Request):
    """Get templates from app.state, falling back to get_templates()."""
    return getattr(request.app.state, "templates", None) or get_templates()


## FastAPI Application - Lifespan is now imported from lifespan.py


app = FastAPI(
  title="Modular Experiment Labs",
  version="2.1.0-B2", # Example version
  docs_url="/api/docs",
  redoc_url="/api/redoc",
  openapi_url="/api/openapi.json",
  lifespan=lifespan,
)
app.router.redirect_slashes = True

# Initialize rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Ensure request ID filter is applied to all handlers (including ones created later)
# (It's already set up in config.py, but ensure it's applied here too)
try:
    from config import RequestIDLoggingFilter, SafeRequestIDFormatter, _request_id_filter, _formatter
    
    root_logger = logging.getLogger()
    
    # Ensure filter is on root logger
    if _request_id_filter not in root_logger.filters:
        root_logger.addFilter(_request_id_filter)
    
    # Ensure filter and formatter are on all existing handlers
    for handler in root_logger.handlers:
        if _request_id_filter not in handler.filters:
            handler.addFilter(_request_id_filter)
        # Also ensure safe formatter is used
        if not isinstance(handler.formatter, SafeRequestIDFormatter):
            handler.setFormatter(_formatter)
    
    # Patch Handler.addHandler to automatically add filter to new handlers
    original_add_handler = logging.Logger.addHandler
    
    def add_handler_with_filter(self, handler):
        """Wrapper to ensure new handlers get the request ID filter."""
        result = original_add_handler(self, handler)
        if _request_id_filter not in handler.filters:
            handler.addFilter(_request_id_filter)
        if not isinstance(handler.formatter, SafeRequestIDFormatter):
            handler.setFormatter(_formatter)
        return result
    
    logging.Logger.addHandler = add_handler_with_filter
    
    logger.debug("Request ID logging filter verified and configured for all handlers.")
except Exception as e:
    logger.warning(f"Could not configure request ID logging filter: {e}")

# Exception handler to ensure HTTPException returns JSON for API routes
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Ensure HTTPException returns JSON instead of HTML for API routes."""
    # Check if this is an API route (starts with /admin/api or /api)
    if request.url.path.startswith("/admin/api/") or request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "status": "error"}
        )
    # For non-API routes, use default HTML response
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates
    templates = get_templates_from_request(request)
    if templates:
        # Check if error.html exists, otherwise use default_experiment.html as fallback
        error_template_path = Path(TEMPLATES_DIR / "error.html")
        default_template_path = Path(TEMPLATES_DIR / "default_experiment.html")
        template_name = None
        if error_template_path.exists():
            template_name = "error.html"
        elif default_template_path.exists():
            template_name = "default_experiment.html"
        
        if template_name:
            return templates.TemplateResponse(
                template_name,
                {"request": request, "status_code": exc.status_code, "detail": exc.detail},
                status_code=exc.status_code
            )
    # Fallback to JSON if no template available
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

# General exception handler for unhandled exceptions
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Catch all unhandled exceptions and return JSON for API routes."""
    logger.error(f"Unhandled exception in {request.url.path}: {exc}", exc_info=True)
    # Check if this is an API route
    if request.url.path.startswith("/admin/api/") or request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc) if not isinstance(exc, HTTPException) else exc.detail,
                "status": "error",
                "type": type(exc).__name__
            }
        )
    # For non-API routes, re-raise to use default FastAPI handler
    raise exc


# ============================================================================
# Middleware Setup
# ============================================================================
# Middleware classes are now imported from middleware.py
# Middleware order matters: 
# 1. Request ID middleware should run FIRST to track all requests
# 2. Proxy-aware middleware must run early so request.url is corrected
# 3. Experiment scope middleware runs after proxy detection
app.add_middleware(RequestIDMiddleware)  # First: Generate request IDs for tracing
app.add_middleware(ProxyAwareHTTPSMiddleware)
app.add_middleware(ExperimentScopeMiddleware)
G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()

if G_NOME_ENV == "production":
    logger.info("Production environment detected. Enabling proxy-aware HTTPSEnforcementMiddleware.")
    logger.info("HTTPS will only be enforced when requests actually come via HTTPS (detected from proxy headers).")
    app.add_middleware(HTTPSEnforcementMiddleware)
else:
    logger.warning(f"Non-production environment ('{G_NOME_ENV}') detected. Skipping HTTPS enforcement.")

# Compression middleware should be added LAST to compress final responses after all other middleware
# This ensures responses are compressed after HTTPS enforcement and other modifications
app.add_middleware(GZipMiddleware, minimum_size=1000)  # Compress responses > 1KB


# Database functions (_ensure_db_indices, _seed_admin, _seed_db_from_local_files) are now imported from database.py
# These functions are called in lifespan.py, not here


# Directory scan cache with TTL (60 seconds)
# _scan_directory, _scan_directory_sync, and _secure_path are now imported from utils.py


auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
@limiter.limit(LOGIN_GET_LIMIT)
async def login_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
  templates = getattr(request.app.state, "templates", None) or get_templates()
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
@limiter.limit(LOGIN_POST_LIMIT)
async def login_post(request: Request):
  templates = get_templates_from_request(request)
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

  # Only fetch fields needed for authentication
  user = await db.users.find_one(
    {"email": email},
    {"_id": 1, "email": 1, "password_hash": 1, "is_admin": 1}
  )
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
      secure=should_use_secure_cookie(request),
      samesite="lax",
      max_age=60 * 60 * 24,
    )
    return response
  else:
    logger.warning(f"Failed login attempt for email: {email}")
    templates = get_templates_from_request(request)
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
  # Delete cookie with same parameters used when setting it
  response.delete_cookie(
    key="token",
    httponly=True,
    secure=should_use_secure_cookie(request),
    samesite="lax",
  )
  return response


if ENABLE_REGISTRATION:
  logger.info("Registration is ENABLED. Adding /register routes.")

  @auth_router.get("/register", response_class=HTMLResponse, name="register_get")
  @limiter.limit(REGISTER_GET_LIMIT)
  async def register_get(request: Request, next: Optional[str] = None, message: Optional[str] = None):
    templates = get_templates_from_request(request)
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
  @limiter.limit(REGISTER_POST_LIMIT)
  async def register_post(request: Request):
    templates = get_templates_from_request(request)
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

    if await db.users.find_one({"email": email}, {"_id": 1}):
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
        secure=should_use_secure_cookie(request),
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


# _make_json_serializable is now imported from utils.py


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

  # Parallelize collection dumping for better performance
  # Use batch processing to prevent memory exhaustion on large collections
  BATCH_SIZE = 1000  # Process documents in batches
  MAX_DOCUMENTS = 10000  # Maximum documents per collection
  
  async def _dump_single_collection(coll_name: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Dump a single collection's documents using batch processing."""
    docs_list = []
    processed_count = 0
    try:
      cursor = db[coll_name].find().limit(MAX_DOCUMENTS)
      batch = []
      
      async for doc in cursor:
        if processed_count >= MAX_DOCUMENTS:
          logger.warning(f"Collection '{coll_name}' has more than {MAX_DOCUMENTS} documents. Stopping at limit.")
          break
        
        doc_dict = dict(doc)
        if "_id" in doc_dict:
          doc_dict["_id"] = str(doc_dict["_id"])
        # Make document JSON-serializable
        doc_dict = _make_json_serializable(doc_dict)
        batch.append(doc_dict)
        processed_count += 1
        
        # Process in batches to manage memory
        if len(batch) >= BATCH_SIZE:
          docs_list.extend(batch)
          batch = []
          logger.debug(f"Processed {processed_count} documents from '{coll_name}' (batch of {BATCH_SIZE})")
      
      # Add remaining documents in final batch
      if batch:
        docs_list.extend(batch)
      
      logger.info(f"Collection '{coll_name}': Exported {len(docs_list)} documents")
    except Exception as e:
      logger.error(f"Error dumping collection '{coll_name}': {e}", exc_info=True)
      # Return what we have so far on error
      docs_list = []
    return coll_name, docs_list

  # Dump all collections in parallel
  dump_tasks = [_dump_single_collection(coll_name) for coll_name in sub_collections]
  results = await asyncio.gather(*dump_tasks, return_exceptions=True)
  
  # Build collections_data dict from results
  collections_data: Dict[str, List[Dict[str, Any]]] = {}
  for result in results:
    if isinstance(result, Exception):
      logger.error(f"Error in collection dump task: {result}", exc_info=True)
      continue
    coll_name, docs_list = result
    collections_data[coll_name] = docs_list

  return config_data, collections_data


# Template generation functions are now imported from export_helpers.py
# They accept templates as a parameter, so routes need to pass app.state.templates


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


def _create_intelligent_docker_compose(slug_id: str) -> str:
  """
  Generates an intelligent docker-compose.yml with MongoDB Atlas Local
  and optional Ray service for distributed computing.
  """
  # Sanitize slug_id for Docker container names (can't start with underscore)
  # Remove leading underscores and replace remaining underscores with hyphens
  sanitized_slug = slug_id.lstrip("_").replace("_", "-")
  # Ensure it doesn't start with a hyphen or number (Docker requirement)
  if sanitized_slug and sanitized_slug[0].isdigit():
    sanitized_slug = f"app-{sanitized_slug}"
  if not sanitized_slug or sanitized_slug.startswith("-"):
    sanitized_slug = f"app-{slug_id.lstrip('_').replace('_', '-')}" or "app-template"
  
  docker_compose_content = f"""services:
  # --------------------------------------------------------------------------
  # FastAPI Application (Standalone Experiment)
  # --------------------------------------------------------------------------
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {sanitized_slug}-app
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
    container_name: {sanitized_slug}-mongo
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
  #   container_name: {sanitized_slug}-ray-head
  #   platform: linux/arm64
  #   ports:
  #     - "10001:10001"  # Ray client server port
  #     - "8265:8265"     # Ray Dashboard
  #   volumes:
  #     - .:/app
  #   environment:
  #     - RAY_ENABLE_RUNTIME_ENV=1
  #     - RAY_RUNTIME_ENV_WORKING_DIR=/app
  #     - RAY_NAMESPACE=experiment_{sanitized_slug}
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


def _create_upload_ready_zip(
  slug_id: str,
  source_dir: Path,
  experiment_path: Path
) -> io.BytesIO:
  """
  Creates an upload-ready ZIP package for iterative development.
  This zip can be:
  1. Downloaded and edited locally
  2. Tested locally (with modifications)
  3. Re-uploaded via admin endpoint to wire up the experiment
  
  The zip contains only the experiment files needed for upload:
  - manifest.json, actor.py, __init__.py (required)
  - requirements.txt (optional)
  - templates/, static/ directories (if present)
  - Other experiment-specific files
  """
  logger.info(f"Creating upload-ready package for '{slug_id}'.")
  zip_buffer = io.BytesIO()
  
  # Read experiment requirements if present
  local_reqs_path = experiment_path / "requirements.txt"
  requirements_content = ""
  if local_reqs_path.is_file():
    local_requirements = _parse_requirements_file_sync(local_reqs_path)
    # Include all experiment requirements (they'll be merged with master on upload)
    requirements_content = "\n".join(local_requirements)
  
  # Generate README with workflow instructions
  experiment_name = slug_id.replace('-', ' ').title()
  readme_content = f"""# Experiment Package: {slug_id}

This package contains the experiment files ready for local development and re-upload.

## Workflow

### 1. Edit & Test Locally
- Modify any files as needed (actor.py, __init__.py, templates/, static/, etc.)
- Test your changes locally
- All files in this zip can be modified

### 2. Re-upload to Platform
Once you're satisfied with your changes:

1. **Admin Access Required**: Only admins can upload experiments
2. **Navigate**: Go to Admin → Configure Experiment → Upload ZIP
3. **Upload**: Select this modified zip file
4. **Auto-wiring**: The platform will automatically:
   - Extract files to the experiment directory
   - Upload runtime zip to B2
   - Update experiment configuration
   - Reload and wire up the experiment

## What's Included

- `manifest.json` - Experiment metadata and configuration
- `actor.py` - Ray actor with business logic
- `__init__.py` - FastAPI router (thin client)
- `requirements.txt` - Python dependencies (if present)
- `templates/` - Jinja2 HTML templates (if present)
- `static/` - Static assets (JS, CSS, images) (if present)
- Other experiment-specific files

## Requirements

The experiment requires these base dependencies (installed on platform):
- FastAPI
- Ray >=2.9.0
- Motor/PyMongo
- Jinja2

Your `requirements.txt` should only include experiment-specific dependencies
not already in the master environment.

## Notes

- The platform automatically handles static file mounting and template serving
- Ray actors are automatically started when the experiment is uploaded
- Database collections are automatically scoped to the experiment slug
- The experiment will be available at `/experiments/{slug_id}` after upload
"""
  
  # Create ZIP archive
  EXCLUSION_PATTERNS = [
    "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
  ]
  
  with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
    if experiment_path.is_dir():
      logger.debug(f"Including experiment files from: {experiment_path}")
      for folder_name, _, file_names in os.walk(experiment_path):
        if Path(folder_name).name in EXCLUSION_PATTERNS:
          continue
        
        for file_name in file_names:
          if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
            continue
          
          file_path = Path(folder_name) / file_name
          try:
            # Files go directly at root (not under experiments/{slug}/)
            arcname = str(file_path.relative_to(experiment_path))
          except ValueError:
            logger.error(f"Failed to get relative path for {file_path}")
            continue
          
          # For HTML files, fix static paths (though they may need re-fixing on upload)
          if file_path.suffix in (".html", ".htm"):
            original_html = file_path.read_text(encoding="utf-8")
            # Keep paths as-is or fix them - upload will handle it
            zf.writestr(arcname, original_html)
          else:
            zf.write(file_path, arcname)
    
    # Add README
    zf.writestr("README.md", readme_content)
    
    # Add requirements.txt if present (keep at root)
    if requirements_content:
      zf.writestr("requirements.txt", requirements_content)
  
  zip_buffer.seek(0)
  logger.info(f"Upload-ready package created successfully for '{slug_id}'.")
  return zip_buffer


def _create_intelligent_export_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]],
  templates
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
  standalone_main_source = _make_intelligent_standalone_main_py(slug_id, templates)
  
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
    
    # Add mongo_connection_pool.py (required for actors)
    mongo_pool = source_dir / "mongo_connection_pool.py"
    if mongo_pool.is_file():
      zf.write(mongo_pool, "mongo_connection_pool.py")
      logger.debug("Included mongo_connection_pool.py")
    else:
      logger.warning("mongo_connection_pool.py not found - actors may not work correctly")
    
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


# _build_absolute_https_url is now imported from utils.py


@public_api_router.get("/package-standalone/{slug_id}", name="package_standalone")
@limiter.limit(EXPORT_LIMIT)
async def package_standalone_experiment(
  request: Request,
  slug_id: str,
  user: Optional[Mapping[str, Any]] = Depends(get_current_user) # Allows unauthenticated access
):
  db: AsyncIOMotorDatabase = request.app.state.mongo_db

  # 1. Check Experiment Configuration - only need status and auth_required fields
  config = await db.experiments_config.find_one({"slug": slug_id}, {"status": 1, "auth_required": 1})
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
    templates = get_templates_from_request(request)
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data,
      templates=templates
    )

    user_email = user.get('email', 'Guest') if user else 'Guest'
    file_name = f"{slug_id}_intelligent_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_size = zip_buffer.getbuffer().nbytes
    
    # Calculate checksum for deduplication (runs in thread pool to prevent blocking)
    checksum = await _calculate_export_checksum(zip_buffer)
    
    # Check for existing export with same checksum
    existing_export = await _find_existing_export_by_checksum(db, checksum, slug_id)
    
    # Upload to B2 if enabled, otherwise save locally
    b2_file_name = None
    download_url = None
    b2_bucket = getattr(request.app.state, "b2_bucket", None)
    
    # If we found an existing export with same checksum and it has B2 file, reuse it
    if existing_export and existing_export.get("b2_file_name") and B2_ENABLED and b2_bucket:
      try:
        b2_file_name = existing_export["b2_file_name"]
        # Generate new presigned URL (existing file in B2)
        download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
        logger.info(f"[{slug_id}] Reusing existing B2 export with matching checksum: {b2_file_name}")
      except Exception as e:
        logger.warning(f"[{slug_id}] Failed to generate presigned URL for existing export, uploading new copy: {e}")
        existing_export = None  # Fall through to upload new copy
    
    # If no existing export found, upload/save new one
    export_file_path = None  # Track local file for cleanup
    if not existing_export or not existing_export.get("b2_file_name"):
      if B2_ENABLED and b2_bucket:
        try:
          # Upload to B2
          b2_file_name = f"exports/{file_name}"
          logger.info(f"[{slug_id}] Uploading export to B2: {b2_file_name}")
          await _upload_export_to_b2(b2_bucket, zip_buffer, b2_file_name)
          
          # Generate presigned download URL from B2
          download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
          logger.info(f"[{slug_id}] Export uploaded to B2. Generated presigned URL.")
          
          # Clean up any local files after successful B2 upload (performance optimization)
          # Files can always be re-downloaded from B2 if needed
          local_file = EXPORTS_TEMP_DIR / file_name
          if local_file.exists():
            await _cleanup_local_export_file(local_file)
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
    
    # Note: Export cleanup now runs on a schedule (every 6 hours) instead of per-request
    
    # Log export to database (only if we didn't reuse an existing export)
    export_log_id = None
    if not existing_export:
      export_log_id = await _log_export(
        db=db,
        slug_id=slug_id,
        export_type="intelligent",
        user_email=user_email,
        local_file_path=str(export_file_path.relative_to(BASE_DIR)) if export_file_path is not None else None,
        file_size=file_size,
        b2_file_name=b2_file_name,
        invalidated=False,
        checksum=checksum
      )
    else:
      logger.info(f"[{slug_id}] Reused existing export, skipping duplicate log entry")
    
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


@public_api_router.get("/package-upload-ready/{slug_id}", name="package_upload_ready")
@limiter.limit(EXPORT_LIMIT)
async def package_upload_ready_experiment(
  request: Request,
  slug_id: str,
  user: Optional[Mapping[str, Any]] = Depends(get_current_user) # Allows unauthenticated access
):
  """
  Export experiment as upload-ready ZIP for iterative development.
  This creates a minimal zip containing only experiment files needed for upload.
  Users can download, edit locally, test, and then re-upload via admin endpoint.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db

  # 1. Check Experiment Configuration
  config = await db.experiments_config.find_one({"slug": slug_id}, {"status": 1, "auth_required": 1})
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

  # 3. Create upload-ready zip
  try:
    experiment_path = BASE_DIR / "experiments" / slug_id
    if not experiment_path.is_dir():
      raise HTTPException(status_code=404, detail=f"Experiment directory not found: {slug_id}")
    
    zip_buffer = _create_upload_ready_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      experiment_path=experiment_path
    )
    
    user_email = user.get('email', 'Guest') if user else 'Guest'
    file_name = f"{slug_id}_upload-ready_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    
    logger.info(f"[{slug_id}] Upload-ready export created by {user_email}")
    
    # Return zip file directly for download
    return Response(
      content=zip_buffer.getvalue(),
      media_type="application/zip",
      headers={
        "Content-Disposition": f'attachment; filename="{file_name}"',
        "X-Export-Type": "upload-ready"
      }
    )
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error creating upload-ready export for '{slug_id}': {e}", exc_info=True)
    raise HTTPException(500, f"Error creating export: {e}")


@public_api_router.get("/package-docker/{slug_id}", name="package_docker")
@limiter.limit(EXPORT_LIMIT)
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

  # 1. Check Experiment Configuration - only need status and auth_required fields
  config = await db.experiments_config.find_one({"slug": slug_id}, {"status": 1, "auth_required": 1})
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
    templates = get_templates_from_request(request)
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data,
      templates=templates
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
    
    # Note: Export cleanup now runs on a schedule (every 6 hours) instead of per-request
    
    # Log export to database
    export_log_id = await _log_export(
      db=db,
      slug_id=slug_id,
      export_type="intelligent",
      user_email=user_email,
      local_file_path=str(export_file_path.relative_to(BASE_DIR)) if export_file_path is not None else None,
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
@limiter.limit(EXPORT_FILE_LIMIT)
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
    }, {"invalidated": 1, "slug_id": 1})
    
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
@limiter.limit("20 per minute")  # More lenient limit for authenticated admin users
async def package_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)):
  """
  NOTE: This is the original, restricted admin route.
  It is kept for separation, even though it currently mirrors the public logic.
  """
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  try:
    templates = get_templates_from_request(request)
    config_data, collections_data = await _dump_db_to_json(db, slug_id)
    zip_buffer = _create_intelligent_export_zip(
      slug_id=slug_id,
      source_dir=BASE_DIR,
      db_data=config_data,
      db_collections=collections_data,
      templates=templates
    )
    
    user_email = user.get('email', 'Unknown')
    file_name = f"{slug_id}_intelligent_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_size = zip_buffer.getbuffer().nbytes
    
    # Calculate checksum for deduplication (runs in thread pool to prevent blocking)
    checksum = await _calculate_export_checksum(zip_buffer)
    
    # Check for existing export with same checksum
    existing_export = await _find_existing_export_by_checksum(db, checksum, slug_id)
    
    # Upload to B2 if enabled, otherwise save locally
    b2_file_name = None
    download_url = None
    b2_bucket = getattr(request.app.state, "b2_bucket", None)
    
    # If we found an existing export with same checksum and it has B2 file, reuse it
    if existing_export and existing_export.get("b2_file_name") and B2_ENABLED and b2_bucket:
      try:
        b2_file_name = existing_export["b2_file_name"]
        # Generate new presigned URL (existing file in B2)
        download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
        logger.info(f"[{slug_id}] Reusing existing B2 export with matching checksum: {b2_file_name}")
      except Exception as e:
        logger.warning(f"[{slug_id}] Failed to generate presigned URL for existing export, uploading new copy: {e}")
        existing_export = None  # Fall through to upload new copy
    
    # If no existing export found, upload/save new one
    export_file_path = None  # Track local file for cleanup
    if not existing_export or not existing_export.get("b2_file_name"):
      if B2_ENABLED and b2_bucket:
        try:
          # Upload to B2
          b2_file_name = f"exports/{file_name}"
          logger.info(f"[{slug_id}] Uploading export to B2: {b2_file_name}")
          await _upload_export_to_b2(b2_bucket, zip_buffer, b2_file_name)
          
          # Generate presigned download URL from B2
          download_url = _generate_presigned_download_url(b2_bucket, b2_file_name, duration_seconds=86400)  # 24 hours
          logger.info(f"[{slug_id}] Export uploaded to B2. Generated presigned URL.")
          
          # Clean up any local files after successful B2 upload (performance optimization)
          # Files can always be re-downloaded from B2 if needed
          local_file = EXPORTS_TEMP_DIR / file_name
          if local_file.exists():
            await _cleanup_local_export_file(local_file)
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
    
    # Note: Export cleanup now runs on a schedule (every 6 hours) instead of per-request
    
    # Log export to database (only if we didn't reuse an existing export)
    export_log_id = None
    if not existing_export:
      export_log_id = await _log_export(
        db=db,
        slug_id=slug_id,
        export_type="intelligent",
        user_email=user_email,
        local_file_path=str(export_file_path.relative_to(BASE_DIR)) if export_file_path is not None else None,
        file_size=file_size,
        b2_file_name=b2_file_name,
        invalidated=False,
        checksum=checksum
      )
    else:
      logger.info(f"[{slug_id}] Reused existing export, skipping duplicate log entry")
    
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
    # Project only needed fields for export listing
    projection = {
      "_id": 1,
      "slug_id": 1,
      "export_type": 1,
      "user_email": 1,
      "file_size": 1,
      "b2_file_name": 1,
      "local_file_path": 1,
      "invalidated": 1,
      "created_at": 1,
    }
    # Limit to 1000 exports to prevent accidental large result sets
    async for export_log in db.export_logs.find(query, projection).sort("created_at", -1).limit(1000):
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
    
    # Find the export - only need invalidated field for this check
    export_log = await db.export_logs.find_one({"_id": ObjectId(export_id)}, {"invalidated": 1, "slug_id": 1, "b2_file_name": 1, "local_file_path": 1})
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
async def admin_dashboard(
  request: Request, 
  user: Dict[str, Any] = Depends(require_admin), 
  message: Optional[str] = None,
  search: Optional[str] = Query(None),
  owner: Optional[str] = Query(None),
  status: Optional[str] = Query(None),
):
  """
  Admin dashboard - shows ALL experiments with search/filter capabilities.
  Admins can view everyone's experiments.
  """
  templates = get_templates_from_request(request)
  if not templates:
    raise HTTPException(500, "Template engine not available.")
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  error_message: Optional[str] = None

  # Admin sees ALL experiments - fetch directly from database
  try:
    # Build query filter for search/filter
    query_filter: Dict[str, Any] = {}
    
    if owner:
      query_filter["owner_email"] = owner
    
    if status:
      query_filter["status"] = status
    
    # Fetch ALL experiments (no limit for admin)
    all_experiments = await db.experiments_config.find(query_filter).limit(1000).to_list(length=None)
    
    # Apply text search if provided
    if search:
      search_lower = search.lower()
      all_experiments = [
        exp for exp in all_experiments
        if (
          search_lower in exp.get("slug", "").lower() or
          search_lower in exp.get("name", "").lower() or
          search_lower in exp.get("description", "").lower() or
          search_lower in exp.get("owner_email", "").lower()
        )
      ]
    
    user_experiments = all_experiments
  except Exception as e:
    error_message = f"Error fetching experiment configs from DB: {e}"
    logger.error(error_message, exc_info=True)
    user_experiments = []
  
  # Get unique owners for filter dropdown
  try:
    owners_aggregation = await db.experiments_config.aggregate([
      {"$group": {"_id": "$owner_email"}},
      {"$sort": {"_id": 1}}
    ]).to_list(length=None)
    unique_owners = [item["_id"] for item in owners_aggregation if item.get("_id")]
  except Exception as e:
    logger.warning(f"Error fetching owners: {e}", exc_info=True)
    unique_owners = []
  
  # Fetch export counts in parallel (independent query)
  async def fetch_configs():
    return user_experiments
  
  async def fetch_export_counts():
    try:
      export_aggregation = await db.export_logs.aggregate([
        {"$group": {
          "_id": "$slug_id",
          "count": {"$sum": 1}
        }}
      ]).to_list(length=None)
      return {item["_id"]: item["count"] for item in export_aggregation if item.get("_id")}
    except Exception as e:
      logger.warning(f"Error fetching export counts: {e}", exc_info=True)
      return {}
  
  # Execute independent queries in parallel
  db_configs_list, export_counts = await asyncio.gather(
    fetch_configs(),
    fetch_export_counts()
  )

  # Check for orphaned configs (configs without code on disk)
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

  configured_experiments: List[Dict[str, Any]] = []
  orphaned_configs: List[Dict[str, Any]] = []

  for cfg in db_configs_list:
    slug = cfg.get("slug")
    if slug in code_slugs:
      cfg["code_found"] = True
      cfg["export_count"] = export_counts.get(slug, 0)
      configured_experiments.append(cfg)
    else:
      cfg["code_found"] = False
      cfg["export_count"] = export_counts.get(slug, 0)
      orphaned_configs.append(cfg)

  configured_experiments.sort(key=lambda x: x.get("slug", ""))
  orphaned_configs.sort(key=lambda x: x.get("slug", ""))

  return templates.TemplateResponse("admin/index.html", {
    "request": request,
    "configured": configured_experiments,
    "orphaned": orphaned_configs,
    "message": message,
    "error_message": error_message,
    "current_user": user,
    "search": search,
    "owner": owner,
    "status": status,
    "unique_owners": unique_owners,
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


@admin_router.post("/api/save-manifest/{slug_id}", response_class=JSONResponse)
async def save_manifest(
  request: Request,
  slug_id: str,
  data: Dict[str, str] = Body(...),
  user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)
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

    # Validate manifest against schema
    async def check_developer_exists(dev_email: str) -> bool:
      """Check if developer exists and has developer role."""
      if not dev_email:
        return False
      try:
        # Check if user exists
        user_doc = await db.users.find_one({"email": dev_email}, {"_id": 1})
        if not user_doc:
          return False
        
        # Check if user has developer role
        authz: AuthorizationProvider = await get_authz_provider(request)
        if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
          try:
            has_role = await asyncio.to_thread(
              authz._enforcer.has_role_for_user, dev_email, "developer"
            )
            return has_role
          except Exception:
            return False
        return False
      except Exception as e:
        logger.error(f"Error checking developer '{dev_email}': {e}", exc_info=True)
        return False
    
    # Validate manifest with developer_id check
    is_valid, validation_error, error_paths = await validate_manifest_with_db(
      new_manifest_data,
      check_developer_exists
    )
    if not is_valid:
      error_msg = f"Manifest validation failed: {validation_error}"
      if error_paths:
        error_msg += f" (errors in: {', '.join(error_paths[:3])})"
      logger.warning(f"Manifest validation failed for '{slug_id}': {validation_error}")
      raise ValueError(error_msg)
    
    # Validate managed_indexes if present
    if "managed_indexes" in new_manifest_data:
      is_valid_indexes, index_error = validate_managed_indexes(new_manifest_data["managed_indexes"])
      if not is_valid_indexes:
        logger.warning(f"Index validation failed for '{slug_id}': {index_error}")
        raise ValueError(f"Index validation failed: {index_error}")

    # Use cached config dependency to avoid duplicate fetches
    old_config = await get_experiment_config(request, slug_id)
    old_status = old_config.get("status", "draft") if old_config else "draft"
    new_status = new_manifest_data.get("status", "draft")

    # Map developer_id to owner_email if present
    if "developer_id" in new_manifest_data:
      dev_id = new_manifest_data.pop("developer_id")
      new_manifest_data["owner_email"] = dev_id
      logger.info(f"Mapped developer_id '{dev_id}' to owner_email for experiment '{slug_id}'")

    if old_config and old_config.get("runtime_s3_uri"):
      config_data = old_config.copy()
      config_data.update(new_manifest_data)
      config_data["slug"] = slug_id
      update_doc = {"$set": config_data}
    else:
      config_data = {"slug": slug_id, **new_manifest_data}
      update_doc = {"$set": config_data}

    await db.experiments_config.update_one({"slug": slug_id}, update_doc, upsert=True)
    # Invalidate application-level config cache after update
    from core_deps import invalidate_experiment_config_cache
    invalidate_experiment_config_cache(slug_id)
    logger.info(f"Upserted experiment config in DB for '{slug_id}'.")
    
    # Invalidate cache after update since config has changed
    if hasattr(request.state, "experiment_config_cache"):
      # Clear all cache entries for this slug_id (including any projections)
      keys_to_remove = [key for key in request.state.experiment_config_cache.keys() if key.startswith(f"{slug_id}:") or key == slug_id]
      for key in keys_to_remove:
        del request.state.experiment_config_cache[key]

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
async def reload_experiment(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)):
  admin_email = user.get('email', 'Unknown Admin')
  logger.info(f"Admin '{admin_email}' triggered manual reload for '{slug_id}'.")
  try:
    await reload_active_experiments(request.app)
    return JSONResponse({"message": "Experiment reload triggered successfully."})
  except Exception as e:
    logger.error(f"Manual experiment reload failed: {e}", exc_info=True)
    raise HTTPException(500, f"Reload failed: {e}")


@admin_router.delete("/api/delete-experiment/{slug_id}", response_class=JSONResponse, name="admin_delete_experiment")
async def admin_delete_experiment(
  request: Request,
  slug_id: str,
  body: Dict[str, str] = Body(...),
  user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)
):
  """Admin delete endpoint - delegates to shared delete logic."""
  return await delete_experiment_impl(request, slug_id, body, user)


@app.delete("/api/delete-experiment/{slug_id}", response_class=JSONResponse, name="developer_delete_experiment")
async def developer_delete_experiment(
  request: Request,
  slug_id: str,
  body: Dict[str, str] = Body(...),
  user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)
):
  """Developer delete endpoint - allows developers to delete their own experiments."""
  return await delete_experiment_impl(request, slug_id, body, user)


async def delete_experiment_impl(
  request: Request,
  slug_id: str,
  body: Dict[str, str],
  user: Dict[str, Any]
):
  """
  Shared delete experiment logic.
  Destructively delete an experiment and all associated data.
  Requires confirmation string: 'sudo rm -rf experiments/<slug>'
  """
  confirmation = body.get("confirmation", "")
  expected_confirmation = f"sudo rm -rf experiments/{slug_id}"
  
  if confirmation != expected_confirmation:
    raise HTTPException(
      status_code=400,
      detail=f"Confirmation string must be exactly: '{expected_confirmation}'"
    )
  
  db: AsyncIOMotorDatabase = request.app.state.mongo_db
  logger.info(f"[{slug_id}] Starting destructive deletion by {user.get('email', 'Unknown')}")
  
  cleanup_results = {
    "ray_actor": False,
    "routes": False,
    "database_config": False,
    "export_logs": False,
    "database_collections": [],
    "filesystem": False,
    "b2_storage": False,
    "errors": []
  }
  
  try:
    # 1. Kill Ray Actor
    if RAY_AVAILABLE and getattr(request.app.state, "ray_is_available", False):
      try:
        import ray
        actor_name = f"{slug_id}-actor"
        try:
          actor_handle = ray.get_actor(actor_name, namespace="modular_labs")
          ray.kill(actor_handle, no_restart=True)
          logger.info(f"[{slug_id}] ✅ Ray actor '{actor_name}' killed")
          cleanup_results["ray_actor"] = True
        except ValueError:
          logger.warning(f"[{slug_id}] Ray actor '{actor_name}' not found (may already be stopped)")
          cleanup_results["ray_actor"] = True  # Consider successful if not found
      except Exception as e:
        error_msg = f"Failed to kill Ray actor: {e}"
        logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
        cleanup_results["errors"].append(error_msg)
    
    # 2. Remove routes from FastAPI app
    try:
      prefix = f"/experiments/{slug_id}"
      # Remove routes that match the prefix
      routes_to_remove = [
        route for route in request.app.routes
        if hasattr(route, "path") and route.path.startswith(prefix)
      ]
      for route in routes_to_remove:
        request.app.routes.remove(route)
      
      # Remove from mounted static paths
      if hasattr(request.app.state, "mounted_static_paths"):
        request.app.state.mounted_static_paths.discard(f"{prefix}/static")
      
      # Remove from app.state.experiments
      if hasattr(request.app.state, "experiments") and slug_id in request.app.state.experiments:
        del request.app.state.experiments[slug_id]
      
      logger.info(f"[{slug_id}] ✅ Removed {len(routes_to_remove)} routes")
      cleanup_results["routes"] = True
    except Exception as e:
      error_msg = f"Failed to remove routes: {e}"
      logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
      cleanup_results["errors"].append(error_msg)
    
    # 3. Get experiment config and collect B2 file names before deletion
    config_doc = await db.experiments_config.find_one({"slug": slug_id})
    runtime_s3_uri = config_doc.get("runtime_s3_uri") if config_doc else None
    
    # Collect B2 file names from export logs before deletion
    b2_files_to_delete = []
    try:
      export_logs = await db.export_logs.find({"slug_id": slug_id}).to_list(length=None)
      for log in export_logs:
        if log.get("b2_file_name"):
          b2_files_to_delete.append(log["b2_file_name"])
      logger.info(f"[{slug_id}] Found {len(b2_files_to_delete)} B2 files to delete from export logs")
      
      # Also add runtime_s3_uri if it exists and is a B2 file path (not a presigned URL)
      if runtime_s3_uri and B2_ENABLED:
        # If runtime_s3_uri is not a URL (presigned), it's likely a file path
        if not runtime_s3_uri.startswith("http"):
          if runtime_s3_uri not in b2_files_to_delete:
            b2_files_to_delete.append(runtime_s3_uri)
            logger.info(f"[{slug_id}] Added runtime ZIP to B2 cleanup list: {runtime_s3_uri}")
    except Exception as e:
      logger.warning(f"[{slug_id}] ⚠️ Failed to collect B2 file names: {e}")
    
    # 4. Delete database config
    try:
      result = await db.experiments_config.delete_one({"slug": slug_id})
      if result.deleted_count > 0:
        logger.info(f"[{slug_id}] ✅ Deleted experiment config from database")
        cleanup_results["database_config"] = True
      else:
        logger.warning(f"[{slug_id}] No experiment config found in database")
        cleanup_results["database_config"] = True  # Consider successful if not found
    except Exception as e:
      error_msg = f"Failed to delete database config: {e}"
      logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
      cleanup_results["errors"].append(error_msg)
    
    # 5. Delete export logs
    try:
      result = await db.export_logs.delete_many({"slug_id": slug_id})
      deleted_count = result.deleted_count
      logger.info(f"[{slug_id}] ✅ Deleted {deleted_count} export log entries")
      cleanup_results["export_logs"] = True
    except Exception as e:
      error_msg = f"Failed to delete export logs: {e}"
      logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
      cleanup_results["errors"].append(error_msg)
    
    # 6. Delete database collections (all collections prefixed with slug_)
    try:
      collection_prefix = f"{slug_id}_"
      collections = await db.list_collection_names()
      deleted_collections = []
      for collection_name in collections:
        if collection_name.startswith(collection_prefix):
          await db.drop_collection(collection_name)
          deleted_collections.append(collection_name)
      logger.info(f"[{slug_id}] ✅ Deleted {len(deleted_collections)} database collections: {deleted_collections}")
      cleanup_results["database_collections"] = deleted_collections
    except Exception as e:
      error_msg = f"Failed to delete database collections: {e}"
      logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
      cleanup_results["errors"].append(error_msg)
    
    # 7. Delete filesystem directory
    try:
      exp_path = EXPERIMENTS_DIR / slug_id
      if exp_path.exists() and exp_path.is_dir():
        shutil.rmtree(exp_path)
        logger.info(f"[{slug_id}] ✅ Deleted filesystem directory: {exp_path}")
        cleanup_results["filesystem"] = True
      else:
        logger.warning(f"[{slug_id}] Experiment directory not found on disk: {exp_path}")
        cleanup_results["filesystem"] = True  # Consider successful if not found
    except Exception as e:
      error_msg = f"Failed to delete filesystem directory: {e}"
      logger.error(f"[{slug_id}] ❌ {error_msg}", exc_info=True)
      cleanup_results["errors"].append(error_msg)
    
    # 8. Delete B2 storage (runtime ZIPs and export ZIPs)
    if B2_ENABLED and b2_files_to_delete:
      try:
        b2_bucket = getattr(request.app.state, "b2_bucket", None)
        if b2_bucket:
          deleted_b2_files = []
          for b2_file_name in b2_files_to_delete:
            try:
              # Delete the file from B2
              # Get file info first, then delete by version
              file_info = b2_bucket.get_file_info_by_name(b2_file_name)
              b2_bucket.delete_file_version(file_info.id_, file_info.file_name)
              deleted_b2_files.append(b2_file_name)
              logger.info(f"[{slug_id}] ✅ Deleted B2 file: {b2_file_name}")
            except Exception as e:
              # File might not exist or already deleted
              logger.warning(f"[{slug_id}] ⚠️ Failed to delete B2 file '{b2_file_name}': {e}")
          cleanup_results["b2_storage"] = True
          cleanup_results["deleted_b2_files"] = deleted_b2_files
          logger.info(f"[{slug_id}] ✅ Deleted {len(deleted_b2_files)} B2 files")
        else:
          logger.warning(f"[{slug_id}] ⚠️ B2 bucket not available for cleanup")
          cleanup_results["b2_storage"] = False
      except Exception as e:
        error_msg = f"Failed to access B2 for cleanup: {e}"
        logger.warning(f"[{slug_id}] ⚠️ {error_msg}")
        cleanup_results["errors"].append(error_msg)
    else:
      if not B2_ENABLED:
        logger.info(f"[{slug_id}] B2 not enabled, skipping B2 cleanup")
      cleanup_results["b2_storage"] = True  # Consider successful if no B2 or no files to delete
    
    logger.info(f"[{slug_id}] ✅ Deletion complete. Cleanup results: {cleanup_results}")
    
    return JSONResponse({
      "status": "success",
      "message": f"Experiment '{slug_id}' deleted successfully",
      "cleanup_results": cleanup_results
    })
    
  except Exception as e:
    logger.error(f"[{slug_id}] ❌ Critical error during deletion: {e}", exc_info=True)
    raise HTTPException(
      status_code=500,
      detail=f"Error deleting experiment: {e}"
    )


@admin_router.get("/api/index-status/{slug_id}", response_class=JSONResponse)
async def get_index_status(request: Request, slug_id: str, user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)):
  no_cache_headers = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0"
  }
  if not INDEX_MANAGER_AVAILABLE:
    return JSONResponse({"error": "Index Management not available."}, status_code=501, headers=no_cache_headers)

  try:
    # Use cached config dependency to avoid duplicate fetches
    # Only need managed_indexes field for index status
    projection = {"managed_indexes": 1}
    config = await get_experiment_config(request, slug_id, projection)
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
async def get_file_content(slug_id: str, path: str = Query(...), user: Dict[str, Any] = Depends(require_experiment_ownership_or_admin_dep)):
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


def _detect_slug_from_zip(zip_data: bytes) -> Optional[str]:
  """
  Detects the experiment slug from ZIP file.
  Supports TWO formats:
  1. Upload-ready format: Files at root level (manifest.json, actor.py, __init__.py)
  2. Standalone export format: Files in experiments/{slug}/ directory
  
  Returns the detected slug, or None if detection fails.
  """
  try:
    with io.BytesIO(zip_data) as buff:
      with zipfile.ZipFile(buff, "r") as zip_ref:
        namelist = zip_ref.namelist()
        
        # METHOD 1: Check for upload-ready format (root-level files)
        # Look for manifest.json, actor.py, __init__.py at root level
        # Handle different ZIP path formats (with/without leading slashes, trailing slashes)
        def normalize_path(path: str) -> str:
          """Normalize ZIP path for comparison (remove leading/trailing slashes)."""
          return path.lstrip("/").rstrip("/")
        
        def is_exact_file(normalized_path: str, filename: str) -> bool:
          """Check if normalized path is exactly the filename (root-level)."""
          return normalized_path == filename
        
        # Find files that could be root-level (normalize paths first)
        root_manifest = None
        root_actor = None
        root_init = None
        
        for path in namelist:
          normalized = normalize_path(path)
          
          if is_exact_file(normalized, "manifest.json"):
            root_manifest = path
          elif is_exact_file(normalized, "actor.py"):
            root_actor = path
          elif is_exact_file(normalized, "__init__.py"):
            root_init = path
        
        # Check if all three required files are at root level
        is_root_format = (
          root_manifest is not None and
          root_actor is not None and
          root_init is not None
        )
        
        if is_root_format:
          # Read manifest.json to extract slug
          try:
            with zip_ref.open(root_manifest) as mf:
              manifest_data = json.load(mf)
              detected_slug = manifest_data.get("slug_id") or manifest_data.get("slug")
              
              if detected_slug:
                logger.info(f"Detected slug '{detected_slug}' from upload-ready format (root-level files)")
                return detected_slug
              else:
                logger.warning("Found root-level format but manifest.json missing 'slug_id' or 'slug' field")
          except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.warning(f"Failed to read manifest.json from root format: {e}")
            # Fall through to check standalone export format
        
        # METHOD 2: Check for standalone export format (experiments/{slug}/ structure)
        # Handle both direct experiments/{slug}/ and wrapper/{experiments/{slug}/ patterns
        # Pattern: find */experiments/{slug}/ anywhere in the path (handles wrapper directories)
        experiments_pattern = re.compile(r'.*?/experiments/([a-z0-9\-_]+)/')
        detected_slugs = {}  # slug -> actual path prefix found
        
        for name in namelist:
          # Skip macOS metadata
          if "MACOSX" in name or "__MACOSX" in name:
            continue
          
          # Try pattern that matches wrapper/experiments/{slug}/ or experiments/{slug}/
          match = experiments_pattern.match(name)
          if match:
            detected_slug = match.group(1)
            # Extract the path prefix up to experiments/{slug}/
            match_obj = re.search(r'(.+?/experiments/' + re.escape(detected_slug) + r'/).*', name)
            if match_obj:
              path_prefix = match_obj.group(1)
              # Store the shortest prefix (prefer direct experiments/{slug}/ over wrapper/experiments/{slug}/)
              if detected_slug not in detected_slugs or len(path_prefix) < len(detected_slugs[detected_slug]):
                detected_slugs[detected_slug] = path_prefix
        
        # Validate that we found exactly one experiment slug
        if len(detected_slugs) == 1:
          detected_slug = list(detected_slugs.keys())[0]
          path_prefix = detected_slugs[detected_slug]
          
          # Verify required files exist in path_prefix (which may be wrapper/experiments/{slug}/)
          required_files = {
            f"{path_prefix}manifest.json",
            f"{path_prefix}actor.py",
            f"{path_prefix}__init__.py"
          }
          
          # Also check for files with trailing slash variations
          found_files = set()
          for req_file in required_files:
            for p in namelist:
              normalized_p = normalize_path(p)
              normalized_req = normalize_path(req_file)
              if normalized_p == normalized_req or normalized_p.startswith(normalized_req + "/"):
                found_files.add(req_file)
                break
          
          if found_files and len(found_files) >= 3:  # At least 3 required files found
            logger.info(f"Detected slug '{detected_slug}' from standalone export structure ({path_prefix})")
            return detected_slug
          else:
            logger.warning(f"Found {path_prefix} but missing required files. Found: {found_files}")
        
        elif len(detected_slugs) > 1:
          logger.warning(f"Multiple experiment slugs detected: {list(detected_slugs.keys())}. This is not a valid export.")
        
  except zipfile.BadZipFile:
    logger.error("Invalid/corrupted .zip file during slug detection")
    return None
  except Exception as e:
    logger.error(f"Error detecting slug from ZIP: {e}", exc_info=True)
    return None
  
  logger.debug("Could not detect slug - ZIP does not match either upload-ready format (root-level files) or standalone export structure (experiments/{slug}/)")
  return None


# File upload size limits (configurable via environment)
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024  # 100MB default
MAX_UPLOAD_SIZE_MB = MAX_UPLOAD_SIZE / (1024 * 1024)

@admin_router.post("/api/detect-slug", response_class=JSONResponse)
async def detect_slug_from_zip(
  request: Request,
  file: UploadFile = File(...),
  user: Dict[str, Any] = Depends(require_admin)
):
  """
  Detects the experiment slug from a ZIP file.
  Supports both formats:
  1. Upload-ready format: root-level files (manifest.json, actor.py, __init__.py)
  2. Standalone export format: experiments/{slug}/ structure
  Validates file size before processing to prevent memory exhaustion.
  """
  if file.content_type not in ("application/zip", "application/x-zip-compressed"):
    return JSONResponse({"error": "Invalid file type; must be .zip."}, status_code=400)
  
  # Validate file size before reading into memory
  if hasattr(file, 'size') and file.size and file.size > MAX_UPLOAD_SIZE:
    return JSONResponse({
      "error": f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB:.0f}MB. "
               f"Received: {file.size / (1024 * 1024):.2f}MB"
    }, status_code=413)  # 413 Payload Too Large
  
  try:
    # Read file in chunks to check size and prevent memory exhaustion
    zip_data = b""
    total_size = 0
    
    # Read file in 1MB chunks to validate size early
    while True:
      chunk = await file.read(1024 * 1024)  # 1MB chunks
      if not chunk:
        break
      
      total_size += len(chunk)
      if total_size > MAX_UPLOAD_SIZE:
        return JSONResponse({
          "error": f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB:.0f}MB. "
                   f"Received: {total_size / (1024 * 1024):.2f}MB (exceeded during upload)"
        }, status_code=413)
      
      zip_data += chunk
    
    # Validate we read something
    if not zip_data:
      return JSONResponse({"error": "Empty file received."}, status_code=400)
    
    detected_slug = _detect_slug_from_zip(zip_data)
    
    if detected_slug:
      return JSONResponse({
        "status": "success",
        "slug": detected_slug,
        "message": f"Detected slug '{detected_slug}' from ZIP file."
      })
    else:
      return JSONResponse({
        "status": "not_found",
        "slug": None,
        "message": "ZIP does not match supported formats. Expected either: (1) Upload-ready format: root-level files (manifest.json, actor.py, __init__.py), or (2) Standalone export format: experiments/{slug}/ with manifest.json, actor.py, and __init__.py"
      })
  except Exception as e:
    logger.error(f"Error detecting slug: {e}", exc_info=True)
    return JSONResponse({
      "status": "error",
      "slug": None,
      "error": str(e)
    }, status_code=500)


@admin_router.post("/api/upload-experiment", response_class=JSONResponse, name="admin_upload_experiment")
async def admin_upload_experiment(
  request: Request,
  file: UploadFile = File(...),
  user: Dict[str, Any] = Depends(require_admin_or_developer)
):
  """Admin upload endpoint - delegates to shared upload logic."""
  return await upload_experiment_zip(request, file, user)


async def upload_experiment_zip(
  request: Request,
  file: UploadFile,
  user: Dict[str, Any]
):
  """
  Upload an experiment ZIP file. Slug is automatically detected from ZIP structure.
  Supports both formats: (1) Upload-ready format (root-level files), 
  (2) Standalone export format (experiments/{slug}/ structure).
  Validates file size before processing to prevent memory exhaustion.
  """
  admin_email = user.get('email', 'Unknown Admin')

  b2_bucket = getattr(request.app.state, "b2_bucket", None)

  if file.content_type not in ("application/zip", "application/x-zip-compressed"):
    raise HTTPException(400, "Invalid file type; must be .zip.")

  # Validate file size before reading into memory
  if hasattr(file, 'size') and file.size and file.size > MAX_UPLOAD_SIZE:
    raise HTTPException(
      413,  # Payload Too Large
      detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB:.0f}MB. "
             f"Received: {file.size / (1024 * 1024):.2f}MB"
    )

  # Read file in chunks to validate size and prevent memory exhaustion
  zip_data = b""
  total_size = 0
  
  try:
    # Read file in 1MB chunks to validate size early
    while True:
      chunk = await file.read(1024 * 1024)  # 1MB chunks
      if not chunk:
        break
      
      total_size += len(chunk)
      if total_size > MAX_UPLOAD_SIZE:
        raise HTTPException(
          413,  # Payload Too Large
          detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB:.0f}MB. "
                 f"Received: {total_size / (1024 * 1024):.2f}MB (exceeded during upload)"
        )
      
      zip_data += chunk
    
    # Validate we read something
    if not zip_data:
      raise HTTPException(400, "Empty file received.")
    
    logger.info(
      f"Admin '{admin_email}' uploading ZIP file: {len(zip_data) / (1024 * 1024):.2f}MB "
      f"(limit: {MAX_UPLOAD_SIZE_MB:.0f}MB)"
    )
    
    # Auto-detect slug from ZIP structure (supports both formats)
    slug_id = _detect_slug_from_zip(zip_data)
    
    if not slug_id:
      raise HTTPException(400, "ZIP must follow either format: (1) Upload-ready format: root-level files (manifest.json, actor.py, __init__.py), or (2) Standalone export format: experiments/{slug}/ with manifest.json, actor.py, and __init__.py.")
    
    logger.info(f"User '{admin_email}' initiated zip upload. Auto-detected slug: '{slug_id}'")
    
    # CRITICAL: Check if experiment already exists and validate ownership/protection
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    existing_config = await db.experiments_config.find_one({"slug": slug_id}, {"owner_email": 1, "slug": 1})
    
    # Check if user is admin (store for later use in ownership assignment)
    authz: AuthorizationProvider = await get_authz_provider(request)
    user_email = user.get("email")
    is_admin = await authz.check(
      subject=user_email,
      resource="admin_panel",
      action="access",
      user_object=dict(user)
    )
    
    # Store is_admin in request state for use later in ownership assignment
    request.state.uploader_is_admin = is_admin
    
    if existing_config:
      owner_email = existing_config.get("owner_email")
      
      # If experiment has no owner_email, it's admin-managed (legacy/protected)
      if not owner_email:
        if not is_admin:
          raise HTTPException(
            403,
            detail=f"Experiment '{slug_id}' is admin-managed and cannot be overwritten by developers. Please use a different slug or contact an administrator."
          )
        logger.info(f"[{slug_id}] Admin '{admin_email}' overwriting admin-managed experiment")
      else:
        # Experiment has an owner - check if user owns it or is admin
        if owner_email != user_email and not is_admin:
          raise HTTPException(
            403,
            detail=f"Experiment '{slug_id}' is owned by '{owner_email}' and cannot be overwritten. Please use a different slug or contact the owner."
          )
        elif owner_email == user_email:
          logger.info(f"[{slug_id}] Developer '{admin_email}' overwriting their own experiment")
        else:
          logger.info(f"[{slug_id}] Admin '{admin_email}' overwriting experiment owned by '{owner_email}'")
    else:
      # New experiment - OK to create
      logger.info(f"[{slug_id}] Creating new experiment (uploaded by '{admin_email}')")
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error reading/validating ZIP file: {e}", exc_info=True)
    raise HTTPException(500, f"Error processing ZIP file: {e}")

  experiment_path = (EXPERIMENTS_DIR / slug_id).resolve()
  try:
    # Use the already-read zip_data (don't read file again)
    with io.BytesIO(zip_data) as buff:
      with zipfile.ZipFile(buff, "r") as zip_ref:
        namelist = zip_ref.namelist()
        
        # Determine format: check for root-level files or experiments/{slug}/ structure
        root_manifest = next((p for p in namelist if p == "manifest.json"), None)
        root_actor = next((p for p in namelist if p == "actor.py"), None)
        root_init = next((p for p in namelist if p == "__init__.py"), None)
        
        is_root_format = (
          root_manifest is not None and
          root_actor is not None and
          root_init is not None
        )
        
        # Validate required files exist in appropriate format
        if is_root_format:
          # Upload-ready format: root-level files
          manifest_path_in_zip = root_manifest
          actor_path_in_zip = root_actor
          init_path_in_zip = root_init
          experiment_prefix = ""  # No prefix for root format
        else:
          # Standalone export format: experiments/{slug}/ structure
          manifest_path_in_zip = next((p for p in namelist if p == f"experiments/{slug_id}/manifest.json" or p.endswith(f"experiments/{slug_id}/manifest.json")), None)
          actor_path_in_zip = next((p for p in namelist if p == f"experiments/{slug_id}/actor.py" or p.endswith(f"experiments/{slug_id}/actor.py")), None)
          init_path_in_zip = next((p for p in namelist if p == f"experiments/{slug_id}/__init__.py" or p.endswith(f"experiments/{slug_id}/__init__.py")), None)
          experiment_prefix = f"experiments/{slug_id}/"
        
        if not manifest_path_in_zip:
          raise HTTPException(400, f"Zip must contain 'manifest.json' (at root or in experiments/{slug_id}/).")
        if not actor_path_in_zip:
          raise HTTPException(400, f"Zip must contain 'actor.py' (at root or in experiments/{slug_id}/).")
        if not init_path_in_zip:
          raise HTTPException(400, f"Zip must contain '__init__.py' (at root or in experiments/{slug_id}/).")
        
        # Security check: Ensure all paths are safe
        for member in zip_ref.infolist():
          member_path = member.filename
          # Skip macOS metadata
          if "MACOSX" in member_path or "__MACOSX" in member_path:
            continue
          
          # Allow files from experiment directory or root-level platform files
          if is_root_format:
            # Root format: allow root-level files and filter out platform files
            if member_path.startswith("/") or ".." in member_path:
              logger.error(f"SECURITY ALERT: Invalid path in '{slug_id}': '{member_path}'")
              raise HTTPException(400, f"Invalid path in zip: '{member_path}'")
            
            # Skip platform files (they'll be ignored during extraction)
            if member_path in ("README.md", "db_config.json", "db_collections.json", "standalone_main.py", "Dockerfile", "docker-compose.yml") or member_path.startswith("async_mongo_wrapper") or member_path.startswith("mongo_connection_pool") or member_path.startswith("experiment_db"):
              continue
            
            # For root format, all non-platform files should be extracted
            # Check for path traversal
            if "/" in member_path and not member_path.startswith("templates/") and not member_path.startswith("static/"):
              # Allow subdirectories for templates/ and static/, but validate other paths
              path_parts = member_path.split("/")
              for part in path_parts:
                if part == ".." or (part.startswith(".") and part not in [".", ".."]):
                  logger.error(f"SECURITY ALERT: Zip Slip attempt in '{slug_id}'! '{member_path}'")
                  raise HTTPException(400, f"Path traversal in zip member '{member_path}'")
          else:
            # Standalone export format: only allow experiments/{slug}/ files
            if not member_path.startswith(experiment_prefix):
              # Allow root-level platform files that will be ignored
              if member_path in ("README.md", "db_config.json", "db_collections.json", "standalone_main.py", "Dockerfile", "docker-compose.yml") or member_path.startswith("async_mongo_wrapper") or member_path.startswith("mongo_connection_pool") or member_path.startswith("experiment_db"):
                continue
            else:
              # Check for path traversal within experiment directory
              relative_path = member_path[len(experiment_prefix):]
              target_path = (experiment_path / relative_path).resolve()
              if experiment_path not in target_path.parents and target_path != experiment_path:
                logger.error(f"SECURITY ALERT: Zip Slip attempt in '{slug_id}'! '{member_path}'")
                raise HTTPException(400, f"Path traversal in zip member '{member_path}'")

        with zip_ref.open(manifest_path_in_zip) as mf:
          parsed_manifest = json.load(mf)
        
        # Validate manifest before processing (with developer_id check)
        from manifest_schema import validate_manifest_with_db, validate_managed_indexes
        # Check developer_id exists in system if present
        async def check_developer_exists_upload(dev_email: str) -> bool:
          """Check if developer exists and has developer role."""
          if not dev_email:
            return False
          try:
            # Check if user exists
            user_doc = await db.users.find_one({"email": dev_email}, {"_id": 1})
            if not user_doc:
              return False
            
            # Check if user has developer role
            authz: AuthorizationProvider = await get_authz_provider(request)
            if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
              try:
                has_role = await asyncio.to_thread(
                  authz._enforcer.has_role_for_user, dev_email, "developer"
                )
                return has_role
              except Exception:
                return False
            return False
          except Exception as e:
            logger.error(f"Error checking developer '{dev_email}': {e}", exc_info=True)
            return False
        
        is_valid, validation_error, error_paths = await validate_manifest_with_db(
          parsed_manifest,
          check_developer_exists_upload
        )
        if not is_valid:
          error_path_str = f" (errors in: {', '.join(error_paths[:3])})" if error_paths else ""
          logger.error(f"[{slug_id}] ❌ Upload BLOCKED: Manifest validation failed: {validation_error}{error_path_str}")
          raise HTTPException(
            400,
            detail=f"Manifest validation failed: {validation_error}{error_path_str}. Please fix manifest.json and try again."
          )
        
        # Validate managed_indexes if present
        if "managed_indexes" in parsed_manifest:
          is_valid_indexes, index_error = validate_managed_indexes(parsed_manifest["managed_indexes"])
          if not is_valid_indexes:
            logger.error(f"[{slug_id}] ❌ Upload BLOCKED: Index validation failed: {index_error}")
            raise HTTPException(
              400,
              detail=f"Index validation failed: {index_error}. Please fix managed_indexes in manifest.json and try again."
            )
        
        # AUTOMATICALLY INJECT developer_id for developers (after conflict checks and validation)
        # Conflicts have been checked above, so it's safe to inject
        uploader_is_admin = getattr(request.state, "uploader_is_admin", False)
        user_email = user.get("email")
        
        if not uploader_is_admin and user_email:
          # Developer upload - automatically inject developer_id if not present or mismatched
          existing_dev_id = parsed_manifest.get("developer_id")
          if not existing_dev_id or existing_dev_id != user_email:
            if existing_dev_id and existing_dev_id != user_email:
              # Manifest has a different developer_id - this should have been caught in validation
              # But if it passed validation, it means that developer exists, so allow override
              logger.warning(f"[{slug_id}] Manifest has developer_id '{existing_dev_id}' but uploader is '{user_email}'. Overriding with uploader's email.")
            parsed_manifest["developer_id"] = user_email
            logger.info(f"[{slug_id}] ✅ Auto-injected developer_id '{user_email}' into manifest")
          else:
            logger.debug(f"[{slug_id}] Manifest already has matching developer_id '{user_email}'")

        # Find requirements.txt (could be at root or in experiments/{slug}/)
        reqs_path_in_zip = None
        if is_root_format:
          reqs_path_in_zip = next((p for p in namelist if p == "requirements.txt"), None)
        else:
          reqs_path_in_zip = next((p for p in namelist if p.endswith("requirements.txt") and "MACOSX" not in p and f"experiments/{slug_id}/" in p), None)
        
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
  runtime_uri = None  # Will be set to either B2 URI or local file URL
  using_b2 = False  # Track if we successfully used B2
  
  # Try to upload to B2 if enabled
  if B2_ENABLED and b2_bucket:
    try:
      logger.info(f"[{slug_id}] Uploading runtime zip to B2 object '{b2_object_key}'...")
      # Upload using B2 SDK (native client always uses HTTPS)
      b2_bucket_instance = getattr(request.app.state, "b2_bucket", None)
      if not b2_bucket_instance:
        raise ValueError("B2 bucket not available")
      # Offload to thread pool to avoid blocking event loop
      await asyncio.to_thread(b2_bucket_instance.upload_bytes, zip_data, b2_object_key)
      logger.info(f"[{slug_id}] B2 upload successful. Generating presigned URL...")
      # B2 SDK always returns HTTPS URLs
      runtime_uri = _generate_presigned_download_url(b2_bucket_instance, b2_object_key)
      using_b2 = True
      logger.info(f"[{slug_id}] Uploaded to B2 successfully.")
    except Exception as e:
      logger.error(f"[{slug_id}] B2 upload failed, falling back to local storage: {e}", exc_info=True)
      # Fall through to local storage fallback
      runtime_uri = None
  
  # Fallback to local storage if B2 is not enabled or upload failed
  if not runtime_uri:
    try:
      file_name = f"{slug_id}_runtime-{timestamp}.zip"
      logger.info(f"[{slug_id}] Saving runtime zip to local storage: {file_name}")
      # Save zip file locally
      zip_buffer = io.BytesIO(zip_data)
      await _save_export_locally(zip_buffer, file_name)
      # Generate URL to serve the file
      relative_url = str(request.url_for("exports", filename=file_name))
      runtime_uri = _build_absolute_https_url(request, relative_url)
      logger.info(f"[{slug_id}] Saved runtime zip locally. Using URL: {runtime_uri}")
    except Exception as e:
      logger.error(f"[{slug_id}] Failed to save runtime zip locally: {e}", exc_info=True)
      raise HTTPException(500, f"Failed to save runtime zip: {e}")

  try:
    # Robustly handle existing experiment directory
    if experiment_path.exists():
      logger.info(f"[{slug_id}] Deleting old directory at {experiment_path}")
      try:
        # Try to remove the directory
        shutil.rmtree(experiment_path)
      except OSError as e:
        # If removal fails, try to remove files individually and then directory
        logger.warning(f"[{slug_id}] Could not remove directory in one go: {e}. Attempting to clean up files...")
        try:
          # Remove files and subdirectories manually
          for item in experiment_path.iterdir():
            try:
              if item.is_file():
                item.unlink()
              elif item.is_dir():
                shutil.rmtree(item)
            except OSError as file_error:
              logger.warning(f"[{slug_id}] Could not remove {item.name}: {file_error}")
          # Try to remove the directory again
          experiment_path.rmdir()
        except Exception as cleanup_error:
          logger.error(f"[{slug_id}] Failed to clean up existing directory: {cleanup_error}")
          raise HTTPException(500, f"Could not remove existing experiment directory. Please check permissions or try again later.")
    
    # Create fresh directory
    experiment_path.mkdir(parents=True, exist_ok=True)
    
    # Determine format for extraction (need to check again)
    # Also detect wrapper directories like __zip__1/experiments/{slug}/
    def normalize_path(path: str) -> str:
      """Normalize ZIP path for comparison (remove leading/trailing slashes)."""
      return path.lstrip("/").rstrip("/")
    
    with io.BytesIO(zip_data) as buff:
      with zipfile.ZipFile(buff, "r") as zip_ref:
        namelist = zip_ref.namelist()
        root_manifest = next((p for p in namelist if normalize_path(p) == "manifest.json"), None)
        root_actor = next((p for p in namelist if normalize_path(p) == "actor.py"), None)
        root_init = next((p for p in namelist if normalize_path(p) == "__init__.py"), None)
        is_root_format = (
          root_manifest is not None and
          root_actor is not None and
          root_init is not None
        )
        
        # If not root format, find the actual path prefix for experiments/{slug_id}/
        # Handle wrapper directories like __zip__1/experiments/{slug_id}/
        experiment_prefix = None
        if not is_root_format:
          # Look for any path that contains experiments/{slug_id}/
          experiments_pattern = re.compile(r'(.+?/experiments/' + re.escape(slug_id) + r'/).*')
          for name in namelist:
            if "MACOSX" in name or "__MACOSX" in name:
              continue
            match = experiments_pattern.match(name)
            if match:
              candidate_prefix = match.group(1)
              # Use the first/longest matching prefix (prefer more specific paths)
              if experiment_prefix is None or len(candidate_prefix) >= len(experiment_prefix):
                experiment_prefix = candidate_prefix
          
          # Fallback to direct experiments/{slug_id}/ if not found with wrapper
          if not experiment_prefix:
            experiment_prefix = f"experiments/{slug_id}/"
    
    if is_root_format:
      logger.info(f"[{slug_id}] Extracting upload-ready format (root-level files) to {experiment_path}...")
    else:
      logger.info(f"[{slug_id}] Extracting standalone export format from {experiment_prefix} to {experiment_path}...")
    
    # Extract files from ZIP (supports both formats)
    # Use synchronous extraction (zipfile operations are sync anyway)
    with io.BytesIO(zip_data) as buff:
      with zipfile.ZipFile(buff, "r") as zip_ref:
        # experiment_prefix was already determined above (handles wrapper directories)
        if is_root_format:
          experiment_prefix = ""  # Root format, no prefix needed
        EXCLUSION_PATTERNS = ["__pycache__", "__MACOSX", ".DS_Store", "*.pyc", "*.pyo"]
        PLATFORM_FILES = ["README.md", "db_config.json", "db_collections.json", "standalone_main.py", "Dockerfile", "docker-compose.yml"]
        PLATFORM_PREFIXES = ["async_mongo_wrapper", "mongo_connection_pool", "experiment_db"]
        
        for member in zip_ref.infolist():
          # Skip macOS metadata
          if "MACOSX" in member.filename or "__MACOSX" in member.filename:
            continue
          
          # Skip platform files (not part of experiment)
          # Check if filename ends with platform file or starts with platform prefix
          # Also check normalized path to handle wrapper directories
          normalized_member = normalize_path(member.filename)
          is_platform_file = (
            normalized_member in PLATFORM_FILES or
            any(normalized_member.startswith(prefix) for prefix in PLATFORM_PREFIXES) or
            any(member.filename.endswith(f"/{pf}") for pf in PLATFORM_FILES) or
            any(f"/{pf}/" in normalized_member or normalized_member.startswith(f"{pf}/") for pf in PLATFORM_FILES)
          )
          if is_platform_file:
            logger.debug(f"[{slug_id}] Skipping platform file: {member.filename}")
            continue
          
          # Determine if this file should be extracted
          should_extract = False
          relative_path = None
          
          if is_root_format:
            # Upload-ready format: extract root-level files and subdirectories (skip platform files already handled above)
            # Check if it's a root-level file or in templates/ or static/ subdirectories
            if "/" not in member.filename:
              # Root-level file (no path separators)
              relative_path = member.filename
              should_extract = True
            elif member.filename.startswith("templates/") or member.filename.startswith("static/"):
              # Subdirectories for templates/ and static/
              relative_path = member.filename
              should_extract = True
            elif "/" in member.filename:
              # Other subdirectories - validate and allow (but not platform files)
              path_parts = member.filename.split("/")
              if not any(part == ".." or (part.startswith(".") and part not in [".", ".."]) for part in path_parts):
                # Allow other subdirectories (user-defined experiment subdirs)
                relative_path = member.filename
                should_extract = True
          else:
            # Standalone export format: extract only from experiments/{slug_id}/
            if member.filename.startswith(experiment_prefix):
              # Remove the experiments/{slug_id}/ prefix to get the relative path
              relative_path = member.filename[len(experiment_prefix):]
              should_extract = True
          
          if not should_extract or not relative_path:
            continue
          
          # Skip empty paths or directory entries (those ending with /)
          if not relative_path or relative_path.endswith('/'):
            continue
          
          # Skip excluded patterns (__pycache__, etc.)
          path_parts = relative_path.split("/")
          should_skip = False
          for part in path_parts:
            # Check exact matches and wildcard patterns
            if part in EXCLUSION_PATTERNS:
              should_skip = True
              break
            # Check wildcard patterns
            for pattern in EXCLUSION_PATTERNS:
              if "*" in pattern and fnmatch.fnmatch(part, pattern):
                should_skip = True
                break
            if should_skip:
              break
          
          if should_skip:
            logger.debug(f"[{slug_id}] Skipping excluded file/directory: {relative_path}")
            continue
          
          # Construct target path
          target_path = experiment_path / relative_path
          
          # Security check: ensure target is within experiment_path
          try:
            target_path.resolve().relative_to(experiment_path.resolve())
          except ValueError:
            logger.error(f"SECURITY ALERT: Path traversal attempt in '{slug_id}': '{member.filename}' -> '{relative_path}'")
            continue
          
          try:
            # Remove existing file/directory if it exists (robust handling)
            if target_path.exists():
              if target_path.is_file():
                target_path.unlink()
                logger.debug(f"[{slug_id}] Removed existing file before extraction: {relative_path}")
              elif target_path.is_dir():
                shutil.rmtree(target_path)
                logger.debug(f"[{slug_id}] Removed existing directory before extraction: {relative_path}")
            
            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Extract the file (synchronous file I/O - acceptable for extraction)
            with zip_ref.open(member) as source:
              # Read file content and write synchronously (extraction is typically synchronous)
              content = source.read()
              target_path.write_bytes(content)
              
          except OSError as e:
            # Handle file system errors gracefully (permissions, etc.)
            logger.warning(f"[{slug_id}] Error extracting {relative_path}: {e}. Skipping...")
            continue
          except Exception as e:
            # Handle other errors gracefully
            logger.error(f"[{slug_id}] Unexpected error extracting {relative_path}: {e}", exc_info=True)
            continue
    
    # Write back the modified manifest.json with injected developer_id (if modified)
    # This ensures the extracted manifest.json includes the developer_id we injected
    manifest_path = experiment_path / "manifest.json"
    if manifest_path.exists():
      try:
        # Write the modified parsed_manifest back to disk
        with open(manifest_path, "w", encoding="utf-8") as mf:
          json.dump(parsed_manifest, mf, indent=2, ensure_ascii=False)
        logger.info(f"[{slug_id}] ✅ Updated manifest.json with injected developer_id")
      except Exception as e:
        logger.warning(f"[{slug_id}] Failed to write updated manifest.json: {e}. Continuing...")
    
    # Verify extraction was successful - check for required files
    required_files_exist = (
      (experiment_path / "manifest.json").exists() and
      (experiment_path / "actor.py").exists() and
      (experiment_path / "__init__.py").exists()
    )
    
    if not required_files_exist:
      logger.error(f"[{slug_id}] Extracted files are missing required files. Checking for nested structure...")
      
      # Look for nested structure (in case ZIP had experiments/{slug}/experiments/{slug}/)
      init_py_files = list(experiment_path.rglob("__init__.py"))
      init_py_files = [f for f in init_py_files if "__MACOSX" not in str(f) and "__pycache__" not in str(f)]
      
      if init_py_files:
        candidate_dirs = []
        for init_py in init_py_files:
          init_dir = init_py.parent
          if (init_dir / "actor.py").exists() and (init_dir / "manifest.json").exists():
            candidate_dirs.append(init_dir)
        
        if candidate_dirs:
          experiment_root = max(candidate_dirs, key=lambda p: len(str(p).split(os.sep)))
          
          if experiment_root != experiment_path:
            try:
              experiment_root_relative = experiment_root.relative_to(experiment_path)
              logger.info(f"[{slug_id}] Flattening nested structure from '{experiment_root_relative}'...")
              temp_dir = experiment_path.parent / f"{slug_id}_temp_flatten"
              temp_dir.mkdir(parents=True, exist_ok=True)
              
              for item in experiment_root.iterdir():
                shutil.move(str(item), str(temp_dir / item.name))
              
              shutil.rmtree(experiment_path)
              experiment_path.mkdir(parents=True)
              
              for item in temp_dir.iterdir():
                shutil.move(str(item), str(experiment_path / item.name))
              
              shutil.rmtree(temp_dir)
            except (ValueError, Exception) as e:
              logger.error(f"[{slug_id}] Failed to flatten nested structure: {e}")
              raise HTTPException(500, f"Failed to extract experiment files: {e}")
      
      # Final check - if still missing, error out
      if not ((experiment_path / "manifest.json").exists() and 
              (experiment_path / "actor.py").exists() and 
              (experiment_path / "__init__.py").exists()):
        raise HTTPException(500, f"Failed to extract required files. Check logs for details.")
    
    # Clear Python's module cache for this experiment to ensure fresh imports
    module_name = f"experiments.{slug_id.replace('-', '_')}"
    if module_name in sys.modules:
      del sys.modules[module_name]
    # Also clear any nested module imports (e.g., experiments.click_tracker_v2.actor)
    modules_to_remove = [m for m in sys.modules.keys() if m.startswith(module_name + ".")]
    for m in modules_to_remove:
      del sys.modules[m]
    logger.info(f"[{slug_id}] Cleared module cache for '{module_name}'.")
    
  except Exception as e:
    logger.error(f"[{slug_id}] Extraction error: {e}", exc_info=True)
    raise HTTPException(500, f"Zip extraction error: {e}")

  try:
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    # Get existing config (already checked earlier, but we need it here for ownership preservation)
    existing_config = await db.experiments_config.find_one({"slug": slug_id}, {"owner_email": 1})
    
    # Map developer_id to owner_email if present
    if "developer_id" in parsed_manifest:
      dev_id = parsed_manifest.pop("developer_id")
      parsed_manifest["owner_email"] = dev_id
      logger.info(f"[{slug_id}] Mapped developer_id '{dev_id}' to owner_email")
    
    config_data = {
      **parsed_manifest,
      "slug": slug_id,
      "runtime_s3_uri": runtime_uri,
      "runtime_pip_deps": parsed_reqs,
    }
    
    # Set ownership on creation/update
    # If developer_id was provided, it's already set as owner_email above
    if "owner_email" not in config_data:
      # New experiment: set owner to the uploader (unless they're admin uploading for someone else)
      # But if developer_id was validated, it should be in owner_email already
      if not existing_config:
        # Brand new experiment - set owner to uploader
        config_data["owner_email"] = admin_email
        logger.info(f"[{slug_id}] Setting owner to '{admin_email}' (new experiment)")
      elif existing_config and existing_config.get("owner_email"):
        # Existing experiment - preserve existing ownership (validation already passed above)
        config_data["owner_email"] = existing_config.get("owner_email")
        logger.debug(f"[{slug_id}] Preserving existing owner '{existing_config.get('owner_email')}'")
      else:
        # Existing experiment with no owner (admin-managed) - only admins get here due to validation above
        uploader_is_admin = getattr(request.state, "uploader_is_admin", False)
        if uploader_is_admin:
          # Admin can choose to keep it admin-managed or assign an owner
          # For now, keep it admin-managed (no owner_email)
          logger.info(f"[{slug_id}] Keeping admin-managed experiment (no owner assigned)")
        else:
          # Should not reach here - validation should have blocked this
          config_data["owner_email"] = admin_email
          logger.warning(f"[{slug_id}] Unexpected: Non-admin overwriting admin-managed experiment. Setting owner to '{admin_email}'")
    
    await db.experiments_config.update_one({"slug": slug_id}, {"$set": config_data}, upsert=True)
    if using_b2:
      logger.info(f"[{slug_id}] Updated DB config with new B2 runtime.")
    else:
      logger.info(f"[{slug_id}] Updated DB config with local storage runtime.")
    
    # Invalidate cache after update since config has changed
    if hasattr(request.state, "experiment_config_cache"):
      # Clear all cache entries for this slug_id (including any projections)
      keys_to_remove = [key for key in request.state.experiment_config_cache.keys() if key.startswith(f"{slug_id}:") or key == slug_id]
      for key in keys_to_remove:
        del request.state.experiment_config_cache[key]
    
    # Also invalidate application-level config cache
    from core_deps import invalidate_experiment_config_cache
    invalidate_experiment_config_cache(slug_id)
  except Exception as e:
    logger.error(f"[{slug_id}] DB config update error: {e}", exc_info=True)
    raise HTTPException(500, f"Failed saving config: {e}")

  # Register the route immediately using the same discovery logic
  # This ensures consistency - same code path as startup/discovery
  try:
    # Use the same _register_experiments logic, but only for this one experiment
    # is_reload=False to avoid clearing existing experiments
    await _register_experiments(request.app, [config_data], is_reload=False)
    logger.info(f"[{slug_id}] Route registered immediately using discovery logic.")
  except Exception as e:
    logger.warning(f"[{slug_id}] Failed to register route immediately: {e}. Will retry in background reload.", exc_info=True)
  
  # Reload experiments in background to avoid blocking the upload response
  # The reload can take a while (creating indexes, starting actors, etc.)
  async def _reload_after_upload():
    """Background task to reload experiments after upload."""
    try:
      logger.info(f"[{slug_id}] Starting background reload after zip upload...")
      await reload_active_experiments(request.app)
      logger.info(f"[{slug_id}] Background reload completed successfully.")
    except Exception as e:
      logger.error(f"[{slug_id}] Background reload failure: {e}", exc_info=True)
  
  # Start reload in background (non-blocking)
  asyncio.create_task(_reload_after_upload())
  
  # Return success immediately - route is registered, full reload will happen in background
  return JSONResponse({
    "status": "success",
    "message": f"Successfully uploaded '{slug_id}'. Experiment is active and accessible.",
    "slug": slug_id,  # Use 'slug' to match template expectations
    "slug_id": slug_id,  # Also include for backwards compatibility
    "reload_status": "background"
  })


# ============================================================================
# Event Broadcasting for Real-time Updates (SSE)
# ============================================================================

# Global event queues for SSE broadcasting
_indexes_event_queue: Optional[asyncio.Queue] = None
_users_event_queue: Optional[asyncio.Queue] = None


def _get_indexes_event_queue(app: FastAPI) -> asyncio.Queue:
    """Get or create the indexes event queue."""
    global _indexes_event_queue
    if _indexes_event_queue is None:
        _indexes_event_queue = asyncio.Queue()
    return _indexes_event_queue


def _get_users_event_queue(app: FastAPI) -> asyncio.Queue:
    """Get or create the users event queue."""
    global _users_event_queue
    if _users_event_queue is None:
        _users_event_queue = asyncio.Queue()
    return _users_event_queue


async def _broadcast_indexes_update(app: FastAPI):
    """Broadcast an update event for indexes."""
    queue = _get_indexes_event_queue(app)
    try:
        await queue.put({"type": "update", "timestamp": datetime.datetime.utcnow().isoformat()})
    except Exception as e:
        logger.warning(f"Failed to broadcast indexes update: {e}")


async def _broadcast_users_update(app: FastAPI):
    """Broadcast an update event for users."""
    queue = _get_users_event_queue(app)
    try:
        await queue.put({"type": "update", "timestamp": datetime.datetime.utcnow().isoformat()})
    except Exception as e:
        logger.warning(f"Failed to broadcast users update: {e}")


# ============================================================================
# Admin Panel: Index Management & User/Role Management Routes
# ============================================================================

@admin_router.get("/management", response_class=HTMLResponse, name="admin_management")
async def admin_management_panel(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
    tab: Optional[str] = Query("indexes", alias="tab"),
):
    """
    Admin management panel with tabs for Index Management and User/Role Management.
    """
    templates = get_templates_from_request(request)
    if not templates:
        raise HTTPException(500, "Template engine not available.")
    
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    authz: AuthorizationProvider = await get_authz_provider(request)
    
    # Default to indexes tab
    active_tab = tab if tab in ("indexes", "users") else "indexes"
    
    # Fetch data for both tabs (we'll display based on active_tab)
    indexes_data = []
    users_data = []
    collections_list = []
    
    if active_tab == "indexes":
        # Get all collections and their indexes for monitoring
        try:
            all_collections = await db.list_collection_names()
            # Filter to experiment collections (those with underscore prefix pattern)
            experiment_collections = [
                coll for coll in all_collections 
                if "_" in coll and not coll.startswith(("users", "experiments_config", "export_logs"))
            ]
            
            for coll_name in experiment_collections:
                try:
                    real_collection = db[coll_name]
                    if INDEX_MANAGER_AVAILABLE:
                        index_manager = AsyncAtlasIndexManager(real_collection)
                        
                        # Get regular indexes
                        regular_indexes = await index_manager.list_indexes()
                        for idx in regular_indexes:
                            indexes_data.append({
                                "collection": coll_name,
                                "name": idx.get("name", "unknown"),
                                "type": "regular",
                                "keys": idx.get("key", {}),
                                "unique": idx.get("unique", False),
                                "status": "READY",
                                "queryable": True,
                                "size": idx.get("size", "N/A"),
                            })
                        
                        # Get search indexes (Atlas Search/Vector Search)
                        try:
                            search_indexes = await index_manager.list_search_indexes()
                            for idx in search_indexes:
                                indexes_data.append({
                                    "collection": coll_name,
                                    "name": idx.get("name", "unknown"),
                                    "type": idx.get("type", "search"),
                                    "status": idx.get("status", "UNKNOWN"),
                                    "queryable": idx.get("queryable", False),
                                    "definition": idx.get("latestDefinition", idx.get("definition", {})),
                                })
                        except Exception as e:
                            logger.debug(f"Could not list search indexes for '{coll_name}': {e}")
                    
                    collections_list.append(coll_name)
                except Exception as e:
                    logger.warning(f"Error processing collection '{coll_name}': {e}")
                    
        except Exception as e:
            logger.error(f"Error fetching index data: {e}", exc_info=True)
    
    elif active_tab == "users":
        # Get all users and their roles
        try:
            users_cursor = db.users.find({}, {"email": 1, "is_admin": 1, "created_at": 1}).limit(1000)
            all_users = await users_cursor.to_list(length=None)
            
            for user_doc in all_users:
                user_email = user_doc.get("email")
                if not user_email:
                    continue
                
                # Get roles for this user
                user_roles = []
                # Check if it's a CasbinAdapter and get roles from enforcer
                if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
                    try:
                        # get_roles_for_user is an async method on AsyncEnforcer
                        roles_result = await authz._enforcer.get_roles_for_user(user_email)
                        user_roles = roles_result if roles_result else []
                    except Exception as e:
                        logger.debug(f"Error getting roles for '{user_email}' via enforcer: {e}")
                        # Fallback: check common roles
                        common_roles = ["admin", "developer", "demo", "user"]
                        for role in common_roles:
                            if hasattr(authz, "has_role_for_user"):
                                has_role = await authz.has_role_for_user(user_email, role)
                                if has_role:
                                    user_roles.append(role)
                else:
                    # Fallback: check common roles for non-Casbin providers
                    common_roles = ["admin", "developer", "demo", "user"]
                    for role in common_roles:
                        if hasattr(authz, "has_role_for_user"):
                            has_role = await authz.has_role_for_user(user_email, role)
                            if has_role:
                                user_roles.append(role)
                
                users_data.append({
                    "email": user_email,
                    "is_admin": user_doc.get("is_admin", False),
                    "roles": user_roles,
                    "created_at": user_doc.get("created_at"),
                })
        except Exception as e:
            logger.error(f"Error fetching user data: {e}", exc_info=True)
    
    return templates.TemplateResponse(
        "admin/management.html",
        {
            "request": request,
            "current_user": user,
            "active_tab": active_tab,
            "indexes_data": indexes_data,
            "collections_list": sorted(set(collections_list)),
            "users_data": users_data,
            "INDEX_MANAGER_AVAILABLE": INDEX_MANAGER_AVAILABLE,
        }
    )


@admin_router.get("/api/indexes", response_class=JSONResponse, name="api_list_indexes")
async def api_list_indexes(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
    collection: Optional[str] = Query(None),
):
    """
    API endpoint to list all indexes across collections (read-only monitoring).
    """
    if not INDEX_MANAGER_AVAILABLE:
        return JSONResponse(
            {"error": "Index Management not available."},
            status_code=501
        )
    
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    indexes_data = []
    
    try:
        if collection:
            collections_to_check = [collection]
        else:
            all_collections = await db.list_collection_names()
            collections_to_check = [
                coll for coll in all_collections 
                if "_" in coll and not coll.startswith(("users", "experiments_config", "export_logs"))
            ]
        
        for coll_name in collections_to_check:
            try:
                real_collection = db[coll_name]
                index_manager = AsyncAtlasIndexManager(real_collection)
                
                # Regular indexes
                regular_indexes = await index_manager.list_indexes()
                for idx in regular_indexes:
                    indexes_data.append({
                        "collection": coll_name,
                        "name": idx.get("name", "unknown"),
                        "type": "regular",
                        "keys": idx.get("key", {}),
                        "unique": idx.get("unique", False),
                        "sparse": idx.get("sparse", False),
                        "status": "READY",
                        "queryable": True,
                        "size": idx.get("size", "N/A"),
                        "v": idx.get("v", "N/A"),
                    })
                
                # Search indexes
                try:
                    search_indexes = await index_manager.list_search_indexes()
                    for idx in search_indexes:
                        status_info = {
                            "collection": coll_name,
                            "name": idx.get("name", "unknown"),
                            "type": idx.get("type", "search"),
                            "status": idx.get("status", "UNKNOWN"),
                            "queryable": idx.get("queryable", False),
                            "definition": idx.get("latestDefinition", idx.get("definition", {})),
                        }
                        
                        # Add additional metadata if available
                        if "analyzer" in idx:
                            status_info["analyzer"] = idx["analyzer"]
                        if "mappings" in idx:
                            status_info["mappings"] = idx["mappings"]
                        
                        indexes_data.append(status_info)
                except Exception as e:
                    logger.debug(f"Could not list search indexes for '{coll_name}': {e}")
                    
            except Exception as e:
                logger.warning(f"Error processing collection '{coll_name}': {e}")
                
    except Exception as e:
        logger.error(f"Error fetching indexes: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to fetch indexes: {e}"},
            status_code=500
        )
    
    return JSONResponse({"status": "success", "indexes": indexes_data})


@admin_router.get("/api/indexes/stream", name="api_indexes_stream")
async def api_indexes_stream(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
):
    """
    Server-Sent Events (SSE) endpoint for real-time index updates.
    """
    async def event_generator():
        queue = _get_indexes_event_queue(request.app)
        
        # Send initial connection message
        yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to index updates stream'})}\n\n"
        
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                
                try:
                    # Wait for event with timeout
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    queue.task_done()
                    
                    # Send heartbeat every 30 seconds if no updates
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.datetime.utcnow().isoformat()})}\n\n"
                except Exception as e:
                    logger.error(f"Error in indexes SSE stream: {e}", exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                    break
        except asyncio.CancelledError:
            logger.debug("Indexes SSE stream cancelled")
        except Exception as e:
            logger.error(f"Indexes SSE stream error: {e}", exc_info=True)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@admin_router.get("/api/users", response_class=JSONResponse, name="api_list_users")
async def api_list_users(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
    limit: int = Query(1000, ge=1, le=10000),
):
    """
    API endpoint to list all users and their roles.
    """
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    authz: AuthorizationProvider = await get_authz_provider(request)
    users_data = []
    
    try:
        users_cursor = db.users.find(
            {},
            {"email": 1, "is_admin": 1, "created_at": 1}
        ).limit(limit)
        all_users = await users_cursor.to_list(length=None)
        
        for user_doc in all_users:
            user_email = user_doc.get("email")
            if not user_email:
                continue
            
            # Get roles for this user
            user_roles = []
            # Check if it's a CasbinAdapter and get roles from enforcer
            if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
                try:
                    # get_roles_for_user is an async method on AsyncEnforcer
                    roles_result = await authz._enforcer.get_roles_for_user(user_email)
                    user_roles = roles_result if roles_result else []
                except Exception as e:
                    logger.debug(f"Error getting roles for '{user_email}': {e}")
                    # Fallback: check common roles
                    common_roles = ["admin", "developer", "demo", "user"]
                    for role in common_roles:
                        if hasattr(authz, "has_role_for_user"):
                            has_role = await authz.has_role_for_user(user_email, role)
                            if has_role:
                                user_roles.append(role)
            else:
                # Fallback: check common roles for non-Casbin providers
                common_roles = ["admin", "developer", "demo", "user"]
                for role in common_roles:
                    if hasattr(authz, "has_role_for_user"):
                        has_role = await authz.has_role_for_user(user_email, role)
                        if has_role:
                            user_roles.append(role)
            
            users_data.append({
                "email": user_email,
                "is_admin": user_doc.get("is_admin", False),
                "roles": user_roles,
                "created_at": user_doc.get("created_at").isoformat() if user_doc.get("created_at") else None,
            })
            
    except Exception as e:
        logger.error(f"Error fetching users: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to fetch users: {e}"},
            status_code=500
        )
    
    return JSONResponse({"status": "success", "users": users_data})


@admin_router.get("/api/users/stream", name="api_users_stream")
async def api_users_stream(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
):
    """
    Server-Sent Events (SSE) endpoint for real-time user updates.
    """
    async def event_generator():
        queue = _get_users_event_queue(request.app)
        
        # Send initial connection message
        yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to user updates stream'})}\n\n"
        
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                
                try:
                    # Wait for event with timeout
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    queue.task_done()
                    
                    # Send heartbeat every 30 seconds if no updates
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.datetime.utcnow().isoformat()})}\n\n"
                except Exception as e:
                    logger.error(f"Error in users SSE stream: {e}", exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                    break
        except asyncio.CancelledError:
            logger.debug("Users SSE stream cancelled")
        except Exception as e:
            logger.error(f"Users SSE stream error: {e}", exc_info=True)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@admin_router.post("/api/users/{user_email}/roles/{role}", response_class=JSONResponse)
async def api_assign_role(
    request: Request,
    user_email: str,
    role: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    """
    Assign a role to a user (admin only).
    """
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    authz: AuthorizationProvider = await get_authz_provider(request)
    admin_email = user.get("email")
    
    # Validate user exists
    user_doc = await db.users.find_one({"email": user_email}, {"_id": 1, "is_admin": 1})
    if not user_doc:
        return JSONResponse(
            {"error": f"User '{user_email}' not found."},
            status_code=404
        )
    
    # Prevent role changes for admin or demo users (managed via .env only)
    is_admin_user = user_doc.get("is_admin", False)
    if is_admin_user:
        return JSONResponse(
            {"error": "Cannot modify roles for admin users. Admin users are managed via .env configuration only."},
            status_code=403
        )
    
    # Check if user has demo role
    if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
        try:
            user_roles = await authz._enforcer.get_roles_for_user(user_email)
            if user_roles and "demo" in user_roles:
                return JSONResponse(
                    {"error": "Cannot modify roles for demo users. Demo users are managed via .env configuration only."},
                    status_code=403
                )
        except Exception as e:
            logger.debug(f"Error checking roles for '{user_email}': {e}")
    
    # Validate role - only allow assigning developer or user
    # Admin and demo roles cannot be assigned via UI
    valid_roles = ["developer", "user"]
    if role not in valid_roles:
        return JSONResponse(
            {"error": f"Invalid role. Only 'developer' or 'user' can be assigned via UI. Admin and demo roles are managed via .env"},
            status_code=400
        )
    
    try:
        # Use role_management for proper validation
        success = await assign_role_to_user(
            authz=authz,
            user_email=user_email,
            role=role,
            assigner_email=admin_email,
            assigner_authz=authz,
        )
        
        if success:
            logger.info(f"Admin '{admin_email}' assigned role '{role}' to user '{user_email}'")
            # Broadcast update to users stream
            await _broadcast_users_update(request.app)
            return JSONResponse({
                "status": "success",
                "message": f"Successfully assigned role '{role}' to user '{user_email}'"
            })
        else:
            return JSONResponse(
                {"error": "Failed to assign role."},
                status_code=500
            )
    except ValueError as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=403
        )
    except Exception as e:
        logger.error(f"Error assigning role: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to assign role: {e}"},
            status_code=500
        )


@admin_router.delete("/api/users/{user_email}/roles/{role}", response_class=JSONResponse)
async def api_remove_role(
    request: Request,
    user_email: str,
    role: str,
    user: Dict[str, Any] = Depends(require_admin),
):
    """
    Remove a role from a user (admin only).
    """
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    authz: AuthorizationProvider = await get_authz_provider(request)
    admin_email = user.get("email")
    
    # Validate user exists
    user_doc = await db.users.find_one({"email": user_email}, {"_id": 1, "is_admin": 1})
    if not user_doc:
        return JSONResponse(
            {"error": f"User '{user_email}' not found."},
            status_code=404
        )
    
    # Prevent role changes for admin or demo users (managed via .env only)
    is_admin_user = user_doc.get("is_admin", False)
    if is_admin_user:
        return JSONResponse(
            {"error": "Cannot modify roles for admin users. Admin users are managed via .env configuration only."},
            status_code=403
        )
    
    # Check if user has demo role
    if isinstance(authz, CasbinAdapter) and hasattr(authz, "_enforcer"):
        try:
            user_roles = await authz._enforcer.get_roles_for_user(user_email)
            if user_roles and "demo" in user_roles:
                return JSONResponse(
                    {"error": "Cannot modify roles for demo users. Demo users are managed via .env configuration only."},
                    status_code=403
                )
        except Exception as e:
            logger.debug(f"Error checking roles for '{user_email}': {e}")
    
    # Validate role
    valid_roles = ["admin", "developer", "demo", "user"]
    if role not in valid_roles:
        return JSONResponse(
            {"error": f"Invalid role. Must be one of: {', '.join(valid_roles)}"},
            status_code=400
        )
    
    try:
        # Use role_management for proper validation
        success = await remove_role_from_user(
            authz=authz,
            user_email=user_email,
            role=role,
            remover_email=admin_email,
            remover_authz=authz,
        )
        
        if success:
            logger.info(f"Admin '{admin_email}' removed role '{role}' from user '{user_email}'")
            # Broadcast update to users stream
            await _broadcast_users_update(request.app)
            return JSONResponse({
                "status": "success",
                "message": f"Successfully removed role '{role}' from user '{user_email}'"
            })
        else:
            return JSONResponse(
                {"error": "Failed to remove role."},
                status_code=500
            )
    except ValueError as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=403
        )
    except Exception as e:
        logger.error(f"Error removing role: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to remove role: {e}"},
            status_code=500
        )


@admin_router.post("/api/users/create", response_class=JSONResponse)
async def api_create_user(
    request: Request,
    user: Dict[str, Any] = Depends(require_admin),
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    role: str = Body("developer", embed=True),
):
    """
    Create a new user with a role (admin only).
    
    Restrictions:
    - Cannot create more than 1 admin user
    - Cannot create more than 1 demo user
    - Can create unlimited developer users
    """
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    authz: AuthorizationProvider = await get_authz_provider(request)
    admin_email = user.get("email")
    
    # Validate email format
    if not email or "@" not in email or "." not in email:
        return JSONResponse(
            {"error": "Invalid email format."},
            status_code=400
        )
    
    # Validate password
    if not password or len(password) < 8:
        return JSONResponse(
            {"error": "Password must be at least 8 characters long."},
            status_code=400
        )
    
    # Validate role - only allow 'developer' or 'user'
    # Admin and demo users are managed via .env only
    valid_roles = ["developer", "user"]
    if role not in valid_roles:
        return JSONResponse(
            {"error": f"Invalid role. Only 'developer' or 'user' can be created via UI. Admin and demo users are managed via .env"},
            status_code=400
        )
    
    # Check if user already exists
    existing_user = await db.users.find_one({"email": email}, {"_id": 1})
    if existing_user:
        return JSONResponse(
            {"error": f"User with email '{email}' already exists."},
            status_code=409
        )
    
    try:
        # Hash password
        import bcrypt
        pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        
        # Create user (never admin - admin is managed via .env)
        is_admin = False  # Admin users are managed via .env only
        new_user = {
            "email": email,
            "password_hash": pwd_hash,
            "is_admin": is_admin,
            "created_at": datetime.datetime.utcnow(),
        }
        
        result = await db.users.insert_one(new_user)
        logger.info(f"Admin '{admin_email}' created user '{email}' with role '{role}' (ID: {result.inserted_id})")
        
        # Assign role (user role is default, so only assign if developer)
        if role == "developer":
            success = await assign_role_to_user(
                authz=authz,
                user_email=email,
                role=role,
                assigner_email=admin_email,
                assigner_authz=authz,
            )
            if not success:
                logger.warning(f"Failed to assign role '{role}' to new user '{email}', but user was created.")
        
        # Broadcast update to users stream
        await _broadcast_users_update(request.app)
        
        return JSONResponse({
            "status": "success",
            "message": f"Successfully created user '{email}' with role '{role}'",
            "user_id": str(result.inserted_id),
        })
    except Exception as e:
        logger.error(f"Error creating user: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to create user: {e}"},
            status_code=500
        )


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
    
    # Broadcast indexes update after reload (indexes may have changed)
    await _broadcast_indexes_update(app)
  except Exception as e:
    logger.error(f" Reload error: {e}", exc_info=True)
    app.state.experiments.clear()


async def _register_experiments(app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False):
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
      from manifest_schema import validate_manifest, validate_manifest_with_db
      # During registration, we validate schema only (no DB check for developer_id)
      # DB validation happens during save/upload when database is available
      is_valid, validation_error, error_paths = validate_manifest(cfg)
      if not is_valid:
        error_path_str = f" (errors in: {', '.join(error_paths[:3])})" if error_paths else ""
        logger.error(
          f"[{slug}] ❌ Registration BLOCKED: Manifest validation failed: {validation_error}{error_path_str}. "
          f"Please fix the manifest.json and reload the experiment."
        )
        continue  # Skip this experiment, continue with others
      
      # Warning if developer_id is present but we can't validate during registration
      if "developer_id" in cfg:
        logger.warning(
          f"[{slug}] ⚠️ developer_id '{cfg.get('developer_id')}' present but not validated during registration. "
          f"Validation will occur when manifest is saved via admin panel."
        )
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
          _safe_background_task(_run_index_creation_for_collection(db, slug, prefixed_collection_name, prefixed_defs))
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
          asyncio.create_task(_safe_background_task(_call_actor_initialize(actor_handle, slug)))
          logger.info(f"[{slug}] Scheduled post-initialization task for actor '{actor_name}'.")
        except Exception as e:
          logger.warning(f"[{slug}] Failed to schedule actor initialization: {e}")
    except Exception as e:
      logger.error(f"[{slug}] Actor start error: {e}", exc_info=True)
      continue
  
  # Broadcast indexes update after registration (indexes may have been created/updated)
  if INDEX_MANAGER_AVAILABLE:
    await _broadcast_indexes_update(app)


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
    templates_obj = get_templates_from_request(request)
    if not templates_obj:
      logger.error("Template engine not available for root route")
      raise HTTPException(500, "Template engine not available.")
    
    # Read directly from database to always show fresh data (including newly uploaded experiments)
    # This ensures consistency with the admin dashboard
    db: AsyncIOMotorDatabase = request.app.state.mongo_db
    active_cfgs = await db.experiments_config.find({"status": "active"}).limit(500).to_list(None)
    
    # Convert configs to the same format as app.state.experiments
    experiments = {}
    for cfg in active_cfgs:
      slug = cfg.get("slug")
      if not slug:
        continue
      # Use the same format as _register_experiments uses
      prefix = f"/experiments/{slug}"
      cfg_copy = dict(cfg)
      cfg_copy["url"] = prefix
      experiments[slug] = cfg_copy
    
    logger.debug(f"Root route accessed. Found {len(experiments)} active experiment(s) from DB.")
    
    # Convert relative experiment URLs to absolute HTTPS URLs
    experiments_with_https_urls = {}
    for slug, meta in experiments.items():
      meta_copy = dict(meta)
      if "url" in meta_copy and meta_copy["url"]:
        # Convert relative URL to absolute HTTPS URL
        meta_copy["url"] = _build_absolute_https_url(request, meta_copy["url"])
      experiments_with_https_urls[slug] = meta_copy
    
    # Check if user is a developer (for navigation)
    is_developer = False
    if user:
      user_email = user.get("email")
      if user_email:
        authz = await get_authz_provider(request)
        is_developer = await authz.check(
          subject=user_email,
          resource="experiments",
          action="manage_own",
          user_object=dict(user)
        )
    
    return templates_obj.TemplateResponse("index.html", {
      "request": request,
      "experiments": experiments_with_https_urls,
      "current_user": user,
      "is_developer": is_developer,
      "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error in root route: {e}", exc_info=True)
    raise HTTPException(500, f"Server error: {e}")


@app.post("/api/upload-experiment", response_class=JSONResponse, name="developer_upload_experiment")
async def developer_upload_experiment(
  request: Request,
  file: UploadFile = File(...),
  user: Dict[str, Any] = Depends(require_admin_or_developer)
):
  """
  Developer upload endpoint - allows developers to upload experiments.
  This is a separate route from the admin upload to avoid router-level admin dependencies.
  """
  # Reuse the admin upload logic but from main app router
  return await upload_experiment_zip(request, file, user)


@app.get("/my-experiments", response_class=HTMLResponse, name="my_experiments")
async def my_experiments(
  request: Request,
  user: Optional[Mapping[str, Any]] = Depends(get_current_user),
  authz: AuthorizationProvider = Depends(get_authz_provider),
):
  """
  My Experiments route - shows only experiments owned by the current developer.
  Only accessible to users with developer role.
  """
  # Require authentication
  if not user:
    login_url = request.url_for("login_get")
    original_path = request.url.path
    redirect_url = f"{login_url}?next={original_path}"
    raise HTTPException(
      status_code=status.HTTP_307_TEMPORARY_REDIRECT,
      headers={"Location": redirect_url},
      detail="Authentication required to view your experiments."
    )
  
  user_email = user.get("email")
  if not user_email:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Invalid authentication token."
    )
  
  # Check if user is a developer (has experiments:manage_own permission)
  has_manage_own = await authz.check(
    subject=user_email,
    resource="experiments",
    action="manage_own",
    user_object=dict(user)
  )
  
  if not has_manage_own:
    raise HTTPException(
      status_code=status.HTTP_403_FORBIDDEN,
      detail="You do not have permission to view your experiments. Developer role required."
    )
  
  try:
    templates_obj = get_templates_from_request(request)
    if not templates_obj:
      logger.error("Template engine not available for my_experiments route")
      raise HTTPException(500, "Template engine not available.")
    
    # Get user's experiments using the helper function
    user_experiments_list = await get_user_experiments(request, user, authz)
    
    # Convert to the same format as root route uses
    experiments = {}
    for cfg in user_experiments_list:
      slug = cfg.get("slug")
      if not slug:
        continue
      # Only include active experiments for display
      if cfg.get("status") != "active":
        continue
      # Use the same format as _register_experiments uses
      prefix = f"/experiments/{slug}"
      cfg_copy = dict(cfg)
      cfg_copy["url"] = prefix
      experiments[slug] = cfg_copy
    
    logger.debug(f"My Experiments route accessed by '{user_email}'. Found {len(experiments)} experiment(s).")
    
    # Convert relative experiment URLs to absolute HTTPS URLs
    experiments_with_https_urls = {}
    for slug, meta in experiments.items():
      meta_copy = dict(meta)
      if "url" in meta_copy and meta_copy["url"]:
        # Convert relative URL to absolute HTTPS URL
        meta_copy["url"] = _build_absolute_https_url(request, meta_copy["url"])
      experiments_with_https_urls[slug] = meta_copy
    
    return templates_obj.TemplateResponse("my_experiments.html", {
      "request": request,
      "experiments": experiments_with_https_urls,
      "current_user": user,
      "is_developer": True,  # This page is only accessible to developers
      "ENABLE_REGISTRATION": ENABLE_REGISTRATION,
    })
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error in my_experiments route: {e}", exc_info=True)
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