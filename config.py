"""Configuration and constants for the application."""
import os
import logging
from pathlib import Path

# Setup logging first
# Create request ID filter that safely handles missing contextvars
class RequestIDLoggingFilter(logging.Filter):
    """Logging filter that adds request ID from contextvars if available."""
    def filter(self, record: logging.LogRecord) -> bool:
        # Always set request_id attribute to avoid KeyError in format string
        try:
            # Try to import and get request ID from contextvar
            # This will fail during import time, which is fine
            try:
                from request_id_middleware import _request_id_context
                request_id = _request_id_context.get(None)
                record.request_id = request_id if request_id else "no-request-id"
            except (ImportError, AttributeError, RuntimeError):
                # If contextvar not available (during import, in different thread, etc.)
                record.request_id = "no-request-id"
        except Exception:
            # Catch-all: always set request_id to prevent format errors
            record.request_id = "no-request-id"
        return True


class SafeRequestIDFormatter(logging.Formatter):
    """Formatter that safely handles missing request_id attribute."""
    def format(self, record: logging.LogRecord) -> str:
        # Ensure request_id exists before formatting
        if not hasattr(record, 'request_id'):
            record.request_id = "no-request-id"
        return super().format(record)


# Create the filter instance
_request_id_filter = RequestIDLoggingFilter()

# Create formatter with safe request ID handling
_formatter = SafeRequestIDFormatter(
    fmt="%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Apply filter and formatter to root logger and all existing handlers immediately
root_logger = logging.getLogger()
# Remove any existing request ID filters to avoid duplicates
root_logger.filters = [f for f in root_logger.filters if not isinstance(f, RequestIDLoggingFilter)]
root_logger.addFilter(_request_id_filter)

# Also apply to all existing handlers
for handler in root_logger.handlers:
    handler.filters = [f for f in handler.filters if not isinstance(f, RequestIDLoggingFilter)]
    handler.addFilter(_request_id_filter)
    # Also set the safe formatter
    handler.setFormatter(_formatter)

logger = logging.getLogger("modular_labs.config")

# Global Paths
BASE_DIR = Path(__file__).resolve().parent
EXPERIMENTS_DIR = BASE_DIR / "experiments"
TEMPLATES_DIR = BASE_DIR / "templates"
EXPORTS_TEMP_DIR = BASE_DIR / "temp_exports"

# Ensure exports temp directory exists
EXPORTS_TEMP_DIR.mkdir(exist_ok=True, mode=0o755)
logger.info(f"Exports temp directory: {EXPORTS_TEMP_DIR}")

# Application Settings
ENABLE_REGISTRATION = os.getenv("ENABLE_REGISTRATION", "true").lower() in {"true", "1", "yes"}
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/")
DB_NAME = "labs_db"

SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a_very_insecure_default_dev_secret_123!")
if SECRET_KEY == "a_very_insecure_default_dev_secret_123!":
    logger.critical("⚠️ SECURITY WARNING: Using default SECRET_KEY. Set FLASK_SECRET_KEY.")

ADMIN_EMAIL_DEFAULT = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD_DEFAULT = os.getenv("ADMIN_PASSWORD", "password123")
if ADMIN_PASSWORD_DEFAULT == "password123":
    logger.warning("⚠️ Using default admin password.")

# Backblaze B2 Configuration
try:
    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    from b2sdk.v2.exception import B2Error, B2SimpleError
    B2SDK_AVAILABLE = True
except ImportError:
    B2SDK_AVAILABLE = False
    logger.warning("b2sdk library not found. Backblaze B2 integration will be disabled.")
    B2Error = None
    B2SimpleError = None
    InMemoryAccountInfo = None
    B2Api = None

B2_APPLICATION_KEY_ID = os.getenv("B2_APPLICATION_KEY_ID") or os.getenv("B2_ACCESS_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY") or os.getenv("B2_SECRET_ACCESS_KEY")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL")  # Legacy support

B2_ENABLED = all([B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME, B2SDK_AVAILABLE])

if not B2SDK_AVAILABLE:
    logger.critical("b2sdk library not installed. B2 features are impossible. pip install b2sdk")
elif B2_ENABLED:
    logger.info(f"Backblaze B2 integration ENABLED for bucket '{B2_BUCKET_NAME}'.")
else:
    logger.warning("Backblaze B2 integration DISABLED. Missing one or more B2_... env vars.")
    logger.warning("Dynamic experiment uploads via /api/upload-experiment will FAIL.")
    # Diagnostic logging to help debug configuration issues
    missing = []
    if not B2_APPLICATION_KEY_ID:
        missing.append("B2_APPLICATION_KEY_ID or B2_ACCESS_KEY_ID")
    if not B2_APPLICATION_KEY:
        missing.append("B2_APPLICATION_KEY or B2_SECRET_ACCESS_KEY")
    if not B2_BUCKET_NAME:
        missing.append("B2_BUCKET_NAME")
    if not B2SDK_AVAILABLE:
        missing.append("b2sdk library (install via: pip install b2sdk)")
    logger.warning(f"Missing B2 configuration: {', '.join(missing) if missing else 'Unknown'}")

# Optional Dependencies
try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False
    logger.warning("aiofiles not found. File I/O will use asyncio.to_thread() fallback.")

try:
    import ray
    RAY_AVAILABLE = True
except ImportError as e:
    RAY_AVAILABLE = False
    logger.warning(f"⚠️ Ray integration disabled: Ray library not found ({e}).")
except Exception as e:
    RAY_AVAILABLE = False
    logger.error(f"⚠️ Unexpected error importing Ray: {e}", exc_info=True)

try:
    from async_mongo_wrapper import ScopedMongoWrapper, AsyncAtlasIndexManager
    HAVE_MONGO_WRAPPER = True
    INDEX_MANAGER_AVAILABLE = True
except ImportError:
    HAVE_MONGO_WRAPPER = False
    INDEX_MANAGER_AVAILABLE = False
    logger.warning("async_mongo_wrapper not found. Database wrapper unavailable.")
    ScopedMongoWrapper = None
    AsyncAtlasIndexManager = None

