"""Configuration and constants for the application."""
import os
import logging
from pathlib import Path

# Setup logging first
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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

