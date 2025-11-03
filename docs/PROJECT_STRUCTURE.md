# Project Structure

This document describes the organization of the mdb-gnome project.

## Directory Structure

```
mdb-gnome/
├── main.py                    # Main FastAPI application entry point
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker configuration
├── docker-compose.yml         # Docker Compose configuration
├── LICENSE                    # License file
├── README.md                  # Main project documentation
│
├── core/                      # Core application modules
│   ├── config.py              # Application configuration
│   ├── database.py            # Database initialization and seeding
│   ├── experiment_db.py       # Experiment database abstraction
│   ├── experiment_routes.py  # Experiment route registration
│   ├── lifespan.py            # Application lifespan management
│   ├── manifest_schema.py     # Manifest JSON schema validation
│   └── core_deps.py           # Core FastAPI dependencies
│
├── auth/                      # Authentication & Authorization
│   ├── authz_provider.py      # Authorization provider interface
│   ├── authz_factory.py       # Authorization factory
│   ├── sub_auth.py            # Sub-authentication utilities
│   ├── role_management.py     # Role management functions
│   └── casbin_model.conf      # Casbin model configuration
│
├── middleware/                # Middleware modules
│   ├── middleware.py          # Core middleware (experiment scope, HTTPS)
│   ├── request_id_middleware.py  # Request ID tracking
│   └── rate_limit.py          # Rate limiting
│
├── utils/                     # Utility modules
│   ├── utils.py               # General utilities (paths, ObjectId, etc.)
│   ├── export_helpers.py      # Export functionality
│   ├── b2_utils.py            # Backblaze B2 utilities
│   ├── index_management.py    # MongoDB index management
│   ├── background_tasks.py    # Background task management
│   └── ray_decorator.py       # Ray decorators
│
├── database/                  # Database modules
│   ├── async_mongo_wrapper.py # Async MongoDB wrapper
│   └── mongo_connection_pool.py # Connection pooling
│
├── docs/                      # Documentation
│   ├── README.md              # (Moved to root)
│   ├── SECURITY.md            # Security documentation
│   ├── ADVANCED_AUTH.md       # Advanced authentication guide
│   ├── SUB_AUTH_GUIDE.md      # Sub-authentication guide
│   ├── EXPERIMENT_AUTH.md     # Experiment authorization
│   ├── CODE_ANALYSIS.md       # Code analysis documentation
│   └── ...                    # Other documentation files
│
├── assets/                     # Static assets
│   └── images/                # Image files
│       ├── gnome.png
│       └── ...
│
├── scripts/                    # Utility scripts
│   ├── llms-code-gen.py       # Code generation scripts
│   └── ...
│
├── templates/                  # Jinja2 templates
│   ├── admin/                  # Admin templates
│   ├── *.html                  # Platform templates
│   └── standalone_main_intelligent.py.jinja2
│
├── experiments/                # Experiment modules
│   ├── __init__.py
│   ├── click_tracker/
│   ├── data_imaging/
│   ├── hello_ray/
│   ├── indexing_demo/
│   ├── stats_dashboard/
│   ├── store_factory/
│   └── storyweaver/
│
└── temp_exports/               # Temporary export files
```

## Module Organization

### Core Modules
Core application functionality:
- `config.py`: Configuration constants and environment variables
- `database.py`: Database initialization, seeding, index management
- `experiment_db.py`: Experiment database abstraction layer
- `experiment_routes.py`: Dynamic experiment route registration
- `lifespan.py`: FastAPI lifespan (startup/shutdown) management
- `manifest_schema.py`: Manifest.json schema validation
- `core_deps.py`: FastAPI dependencies (auth, DB, config)

### Authentication & Authorization
- `authz_provider.py`: Authorization provider interface (Casbin)
- `authz_factory.py`: Factory for creating authorization providers
- `sub_auth.py`: Sub-authentication utilities for experiments
- `role_management.py`: Role assignment and management
- `casbin_model.conf`: Casbin RBAC model configuration

### Middleware
- `middleware.py`: Experiment scope middleware, HTTPS enforcement
- `request_id_middleware.py`: Request ID tracking for logging
- `rate_limit.py`: Rate limiting using slowapi

### Utilities
- `utils.py`: General utilities (path validation, ObjectId validation, etc.)
- `export_helpers.py`: Export functionality (ZIP creation, B2 upload)
- `b2_utils.py`: Backblaze B2 cloud storage utilities
- `index_management.py`: MongoDB index creation and management
- `background_tasks.py`: Background task management
- `ray_decorator.py`: Ray actor decorators

### Database
- `async_mongo_wrapper.py`: Scoped MongoDB wrapper for experiment isolation
- `mongo_connection_pool.py`: MongoDB connection pool management

## Import Patterns

### From Root (Legacy - for backwards compatibility)
Many modules are still imported from root for backwards compatibility:
```python
from config import BASE_DIR, MONGO_URI
from core_deps import get_current_user
from database import seed_admin
```

### Recommended (Future)
Eventually, modules should be imported from their organized locations:
```python
from core.config import BASE_DIR, MONGO_URI
from core.core_deps import get_current_user
from core.database import seed_admin
from auth.sub_auth import get_experiment_sub_user
from utils.utils import safe_objectid
```

## Notes

1. **Python modules are currently at root** for backwards compatibility with existing imports
2. **Documentation** has been moved to `docs/` directory
3. **Images** have been moved to `assets/images/` directory
4. **Scripts** have been moved to `scripts/` directory
5. Future reorganization can move Python modules to organized directories with import updates

## Migration Path

To fully reorganize Python modules:
1. Move modules to organized directories (core/, auth/, etc.)
2. Add `__init__.py` files to make them packages
3. Update all imports throughout codebase
4. Update experiment imports
5. Test all functionality

This is a breaking change and should be done carefully with comprehensive testing.

