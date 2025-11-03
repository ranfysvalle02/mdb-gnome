"""Utility functions for file I/O, path handling, directory operations, and security."""
import os
import json
import re
import asyncio
import logging
import datetime
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple
from fastapi import HTTPException, status
from config import AIOFILES_AVAILABLE

logger = logging.getLogger(__name__)

# Directory scan cache
_dir_scan_cache: Dict[str, tuple] = {}
_dir_scan_cache_lock = asyncio.Lock()
DIR_SCAN_CACHE_TTL_SECONDS = 60


# ============================================================================
# File I/O Functions
# ============================================================================

async def read_file_async(file_path: Path, encoding: str = "utf-8") -> str:
    """Asynchronously read a text file."""
    if AIOFILES_AVAILABLE:
        import aiofiles
        async with aiofiles.open(file_path, "r", encoding=encoding) as f:
            return await f.read()
    else:
        return await asyncio.to_thread(file_path.read_text, encoding=encoding)


async def read_json_async(file_path: Path, encoding: str = "utf-8") -> Any:
    """Asynchronously read and parse a JSON file."""
    content = await read_file_async(file_path, encoding)
    return await asyncio.to_thread(json.loads, content)


async def write_file_async(file_path: Path, content: bytes | str) -> None:
    """Asynchronously write to a file."""
    if AIOFILES_AVAILABLE:
        import aiofiles
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


# ============================================================================
# Directory Operations
# ============================================================================

def calculate_dir_size_sync(path: Path) -> int:
    """Synchronously calculate total size of all files in a directory tree."""
    total_size = 0
    for root, dirs, files in os.walk(path):
        for file_name in files:
            file_path = Path(root) / file_name
            try:
                if file_path.is_file():
                    total_size += file_path.stat().st_size
            except Exception:
                pass
    return total_size


def scan_directory_sync(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
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
                    "children": scan_directory_sync(item, base_path)
                })
            else:
                tree.append({"name": item.name, "type": "file", "path": str(relative_path)})
    except OSError as e:
        logger.error(f"⚠️ Error scanning directory '{dir_path}': {e}")
        tree.append({"name": f"[Error: {e.strerror}]", "type": "error", "path": str(dir_path.relative_to(base_path))})
    return tree


async def scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
    """Async wrapper for directory scanning with caching."""
    cache_key = str(dir_path)
    
    # Check cache (thread-safe)
    async with _dir_scan_cache_lock:
        if cache_key in _dir_scan_cache:
            tree, timestamp = _dir_scan_cache[cache_key]
            age = datetime.datetime.now() - timestamp
            if age < datetime.timedelta(seconds=DIR_SCAN_CACHE_TTL_SECONDS):
                logger.debug(f"_scan_directory: Using cached result for '{dir_path}' (age: {age.total_seconds():.1f}s)")
                return tree
            else:
                logger.debug(f"_scan_directory: Cache expired for '{dir_path}' (age: {age.total_seconds():.1f}s)")
                del _dir_scan_cache[cache_key]
    
    # Perform actual scan
    tree = await asyncio.to_thread(scan_directory_sync, dir_path, base_path)
    
    # Update cache (thread-safe)
    async with _dir_scan_cache_lock:
        _dir_scan_cache[cache_key] = (tree, datetime.datetime.now())
    
    return tree


# ============================================================================
# Security Functions
# ============================================================================

def secure_path(base_dir: Path, relative_path_str: str) -> Path:
    """Securely resolve a relative path, preventing directory traversal."""
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


def validate_objectid(oid_string: str, field_name: str = "id") -> Tuple[bool, Optional[str]]:
    """
    Securely validate and convert a string to MongoDB ObjectId.
    
    Args:
        oid_string: String to validate and convert
        field_name: Name of the field (for error messages)
    
    Returns:
        Tuple of (is_valid: bool, error_message: Optional[str])
        If valid, returns (True, None). If invalid, returns (False, error_message).
    
    Security Notes:
    - Validates ObjectId format before conversion
    - Prevents ObjectId injection attacks
    - Returns safe error messages (no information disclosure)
    """
    if not oid_string:
        return False, f"{field_name} is required"
    
    if not isinstance(oid_string, str):
        return False, f"{field_name} must be a string"
    
    # Validate ObjectId format: exactly 24 hexadecimal characters
    if not re.match(r'^[0-9a-fA-F]{24}$', oid_string):
        return False, f"Invalid {field_name} format"
    
    try:
        from bson.objectid import ObjectId
        # Verify it's a valid ObjectId (this will throw if invalid)
        test_oid = ObjectId(oid_string)
        # Double-check the string representation matches
        if str(test_oid) != oid_string:
            return False, f"Invalid {field_name} format"
        return True, None
    except Exception:
        return False, f"Invalid {field_name} format"


def safe_objectid(oid_string: str, field_name: str = "id"):
    """
    Securely convert a string to MongoDB ObjectId, raising HTTPException on failure.
    
    Args:
        oid_string: String to convert to ObjectId
        field_name: Name of the field (for error messages)
    
    Returns:
        ObjectId instance
    
    Raises:
        HTTPException: 400 if the string is not a valid ObjectId
    
    Security Notes:
    - Validates format before conversion
    - Prevents ObjectId injection attacks
    - Returns generic error messages (no information disclosure)
    """
    is_valid, error_message = validate_objectid(oid_string, field_name)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_message or f"Invalid {field_name}")
    
    from bson.objectid import ObjectId
    return ObjectId(oid_string)


def sanitize_for_regex(text: str) -> str:
    """
    Escape special regex characters in a string to prevent regex injection.
    
    Args:
        text: String to escape
    
    Returns:
        Escaped string safe for use in regex patterns
    
    Security Notes:
    - Escapes all regex special characters
    - Prevents regex injection attacks
    - Use this when building regex patterns from user input
    """
    return re.escape(text)


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename to prevent path traversal and other file system attacks.
    
    Args:
        filename: Filename to sanitize
    
    Returns:
        Sanitized filename (only the basename, with path components removed)
    
    Security Notes:
    - Removes path components (prevents directory traversal)
    - Returns only the basename
    - Strips leading/trailing whitespace and dots
    """
    # Remove path components - only keep basename
    safe_name = Path(filename).name
    
    # Remove leading/trailing dots and whitespace (can be used for hidden files)
    safe_name = safe_name.strip('. ')
    
    # Empty filename after sanitization is invalid
    if not safe_name:
        raise ValueError("Filename is invalid after sanitization")
    
    return safe_name


# ============================================================================
# URL and Cookie Functions
# ============================================================================

def build_absolute_https_url(request, relative_url: str) -> str:
    """
    Build an absolute URL from a relative URL, handling proxy headers intelligently.
    
    Uses request.state values from ProxyAwareHTTPSMiddleware if available,
    falls back to checking proxy headers directly.
    
    When behind a proxy (like Render.com), excludes internal application ports
    (e.g., port 10000) from URLs since the proxy handles port mapping externally.
    """
    # Get the default application port (used internally, not externally)
    default_app_port = int(os.getenv("PORT", "10000"))
    
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
    
    # Detect if we're behind a proxy
    is_localhost = request.url.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    has_proxy_headers = any((
        request.headers.get("X-Forwarded-Proto"),
        request.headers.get("X-Forwarded-Host"),
        request.headers.get("Forwarded"),
        request.headers.get("X-Forwarded-Ssl")
    ))
    is_behind_proxy = has_proxy_headers and not is_localhost
    
    # Use detected scheme/host from proxy middleware if available
    if hasattr(request.state, "detected_scheme") and hasattr(request.state, "detected_host"):
        scheme = request.state.detected_scheme
        host = request.state.detected_host
        detected_port = getattr(request.state, "detected_port", None)
        if detected_port:
            default_port = 443 if scheme == "https" else 80
            # Exclude internal app port (10000) when behind a proxy
            # Also exclude default ports (443/80) as they shouldn't be in URLs
            if is_behind_proxy and detected_port == default_app_port:
                logger.debug(f"Excluding internal app port {detected_port} from URL (behind proxy)")
                # Don't include port in URL
            elif detected_port != default_port:
                host = f"{host}:{detected_port}"
    else:
        # Fallback: check proxy headers directly
        if is_localhost and not has_proxy_headers:
            if hasattr(request.state, "original_scheme"):
                scheme = request.state.original_scheme
            else:
                scheme = request.url.scheme
        else:
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
        
        # Clean host (remove port if it's already in the host header)
        if ":" in host and not is_localhost:
            # If host header includes port, extract it
            host_with_port = host
            host = host.split(":")[0]
            try:
                port_from_host = int(host_with_port.split(":")[1])
            except (ValueError, IndexError):
                port_from_host = None
        else:
            port_from_host = None
        
        # Use port from request.url if not in host header
        port_to_check = port_from_host or request.url.port
        
        if port_to_check:
            default_port = 443 if scheme == "https" else 80
            # Exclude internal app port (10000) when behind a proxy
            if is_behind_proxy and port_to_check == default_app_port:
                logger.debug(f"Excluding internal app port {port_to_check} from URL (behind proxy)")
                # Don't include port in URL
            elif port_to_check != default_port:
                host = f"{host}:{port_to_check}"
    
    # Force HTTPS in production, but NOT on localhost
    G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()
    is_localhost = host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]") or (host and "localhost" in host.lower())
    
    if G_NOME_ENV == "production" and scheme != "https" and not is_localhost:
        scheme = "https"
        logger.debug(f"Forcing HTTPS URL in production environment (non-localhost)")
    
    # Build absolute URL
    absolute_url = f"{scheme}://{host}{relative_url}"
    return absolute_url


def should_use_secure_cookie(request) -> bool:
    """
    Determines if cookies should use the secure flag.
    
    In production: Always returns True (cookies must be secure).
    In development: Returns True if HTTPS is detected (behind proxy or direct).
    
    Uses request.state.detected_scheme from ProxyAwareHTTPSMiddleware if available,
    otherwise checks proxy headers or request.url.scheme.
    
    Args:
        request: FastAPI Request object
        
    Returns:
        bool: True if cookies should use secure flag, False otherwise
    """
    G_NOME_ENV = os.getenv("G_NOME_ENV", "production").lower()
    
    # Always use secure cookies in production for security
    if G_NOME_ENV == "production":
        return True
    
    # In development, check if HTTPS is being used
    # Use detected_scheme from middleware if available (handles proxies correctly)
    if hasattr(request.state, "detected_scheme"):
        detected_scheme = request.state.detected_scheme
        if detected_scheme == "https":
            return True
        # If not HTTPS and we're behind a proxy, check original scheme
        if detected_scheme == "http" and hasattr(request.state, "original_scheme"):
            return request.state.original_scheme == "https"
    
    # Fallback: check proxy headers directly
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
    if forwarded_proto == "https":
        return True
    
    if request.headers.get("X-Forwarded-Ssl", "").lower() == "on":
        return True
    
    forwarded_header = request.headers.get("Forwarded", "")
    if forwarded_header and "proto=https" in forwarded_header.lower():
        return True
    
    # Final fallback: check request.url.scheme
    # This should work if middleware hasn't run yet or isn't available
    return request.url.scheme == "https"


# ============================================================================
# JSON Serialization
# ============================================================================

def make_json_serializable(obj: Any) -> Any:
    """Recursively converts MongoDB objects (datetime, ObjectId, etc.) to JSON-serializable types."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: make_json_serializable(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    elif isinstance(obj, (datetime.date, datetime.time)):
        return obj.isoformat()
    elif hasattr(obj, '__str__') and not isinstance(obj, (str, int, float, bool, type(None))):
        try:
            return str(obj)
        except Exception:
            return repr(obj)
    return obj
