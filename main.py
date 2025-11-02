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
)

# B2 utilities
from b2_utils import (
    generate_presigned_download_url as _generate_presigned_download_url,
    upload_export_to_b2 as _upload_export_to_b2,
)

# Lifespan management
from lifespan import lifespan, get_templates

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
    make_standalone_main_py as _make_standalone_main_py,
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
    get_current_user,
    get_current_user_or_redirect,
    require_admin,
    get_scoped_db,
    get_experiment_config,
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


# ============================================================================
# Middleware Setup
# ============================================================================
# Middleware classes are now imported from middleware.py
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

# Compression middleware should be added LAST to compress final responses after all other middleware
# This ensures responses are compressed after HTTPS enforcement and other modifications
app.add_middleware(GZipMiddleware, minimum_size=1000)  # Compress responses > 1KB


# Database functions (_ensure_db_indices, _seed_admin, _seed_db_from_local_files) are now imported from database.py
# These functions are called in lifespan.py, not here


# Directory scan cache with TTL (60 seconds)
# _scan_directory, _scan_directory_sync, and _secure_path are now imported from utils.py


auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.get("/login", response_class=HTMLResponse, name="login_get")
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
      secure=(request.url.scheme == "https"),
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
  response.delete_cookie("token")
  return response


if ENABLE_REGISTRATION:
  logger.info("Registration is ENABLED. Adding /register routes.")

  @auth_router.get("/register", response_class=HTMLResponse, name="register_get")
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
  async def _dump_single_collection(coll_name: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Dump a single collection's documents."""
    docs_list = []
    try:
      # Limit to 10000 documents per collection to prevent accidental large exports
      cursor = db[coll_name].find().limit(10000)
      async for doc in cursor:
        doc_dict = dict(doc)
        if "_id" in doc_dict:
          doc_dict["_id"] = str(doc_dict["_id"])
        # Make document JSON-serializable
        doc_dict = _make_json_serializable(doc_dict)
        docs_list.append(doc_dict)
    except Exception as e:
      logger.error(f"Error dumping collection '{coll_name}': {e}", exc_info=True)
      # Return empty list on error
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


async def _create_standalone_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]],
  templates
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
  estimated_size = await _estimate_export_size(db_data, db_collections, source_dir, slug_id)
  use_disk_streaming = _should_use_disk_streaming(estimated_size, max_size_mb=100)
  
  if use_disk_streaming:
    logger.info(f"[{slug_id}] Large export detected ({estimated_size / (1024*1024):.1f} MB), using disk streaming")
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_path = EXPORTS_TEMP_DIR / f"{slug_id}_standalone_export_{timestamp}.zip"
  else:
    zip_path = None

  # --- 2. Generate core files ---
  standalone_main_source = _make_standalone_main_py(slug_id, templates)
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

            # Rewrite paths in HTML files (using async file I/O for better concurrency)
            if file_path.suffix in (".html", ".htm"):
              original_html = await _read_file_async(file_path, encoding="utf-8")
              fixed_html = _fix_static_paths(original_html, slug_id)
              zf.writestr(arcname, fixed_html)
            else:
              # For binary files, still use synchronous write since zipfile requires it
              # but we can read asynchronously if needed in the future
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


def _create_docker_zip(
  slug_id: str,
  source_dir: Path,
  db_data: Dict[str, Any],
  db_collections: Dict[str, List[Dict[str, Any]]],
  templates
) -> io.BytesIO:
  """
  Creates a Docker export package for data_imaging with Dockerfile and docker-compose.yml.
  """
  logger.info(f"Starting creation of Docker package for '{slug_id}'.")
  zip_buffer = io.BytesIO()
  experiment_path = source_dir / "experiments" / slug_id
  templates_dir = source_dir / "templates"

  # --- 1. Generate core files ---
  standalone_main_source = _make_standalone_main_py(slug_id, templates)
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
    
    # Calculate checksum for deduplication
    checksum = _calculate_export_checksum(zip_buffer)
    
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
        local_file_path=str(export_file_path.relative_to(BASE_DIR)) if 'export_file_path' in locals() else None,
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
    
    # Note: Export cleanup now runs on a schedule (every 6 hours) instead of per-request
    
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
    
    # Calculate checksum for deduplication
    checksum = _calculate_export_checksum(zip_buffer)
    
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
        local_file_path=str(export_file_path.relative_to(BASE_DIR)) if 'export_file_path' in locals() else None,
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
async def admin_dashboard(request: Request, user: Dict[str, Any] = Depends(require_admin), message: Optional[str] = None):
  templates = get_templates_from_request(request)
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

  # Fetch configs and export counts in parallel (independent queries)
  async def fetch_configs():
    try:
      # Limit to 500 experiment configs to prevent accidental large result sets
      return await db.experiments_config.find().limit(500).to_list(length=None)
    except Exception as e:
      error_message = f"Error fetching experiment configs from DB: {e}"
      logger.error(error_message, exc_info=True)
      return []
  
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
  templates = get_templates_from_request(request)
  if not templates:
    raise HTTPException(500, "Template engine not available.")
  experiment_path = EXPERIMENTS_DIR / slug_id
  manifest_data: Optional[Dict[str, Any]] = None
  manifest_content: str = ""

  try:
    # Use cached config dependency to avoid duplicate fetches
    db_config = await get_experiment_config(request, slug_id)
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

    # Use cached config dependency to avoid duplicate fetches
    old_config = await get_experiment_config(request, slug_id)
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
    # Offload to thread pool to avoid blocking event loop
    await asyncio.to_thread(b2_bucket_instance.upload_bytes, zip_data, b2_object_key)
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

  try:
    logger.info(f"[{slug_id}] Reloading after zip upload...")
    await reload_active_experiments(request.app)
    return JSONResponse({
      "status": "success",
      "message": f"Successfully uploaded and reloaded '{slug_id}'. Experiment is now active.",
      "slug_id": slug_id
    })
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
    
    return templates_obj.TemplateResponse("index.html", {
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