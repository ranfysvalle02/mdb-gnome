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
├── # Core application modules (at root level)
│   ├── config.py              # Application configuration
│   ├── database.py            # Database initialization and seeding
│   ├── experiment_db.py       # Experiment database abstraction
│   ├── experiment_routes.py   # Experiment route registration
│   ├── lifespan.py            # Application lifespan management
│   ├── manifest_schema.py     # Manifest JSON schema validation
│   └── core_deps.py           # Core FastAPI dependencies
│
├── # Authentication & Authorization (at root level)
│   ├── authz_provider.py      # Authorization provider interface
│   ├── authz_factory.py       # Authorization factory
│   ├── sub_auth.py            # Sub-authentication utilities
│   ├── role_management.py     # Role management functions
│   ├── experiment_auth_restrictions.py  # Experiment auth restrictions
│   └── auth/                   # Auth configuration directory
│       └── casbin_model.conf   # Casbin model configuration
│
├── # Middleware modules (at root level)
│   ├── middleware.py          # Core middleware (experiment scope, HTTPS)
│   ├── request_id_middleware.py  # Request ID tracking
│   └── rate_limit.py          # Rate limiting
│
├── # Utility modules (at root level)
│   ├── utils.py               # General utilities (paths, ObjectId, etc.)
│   ├── export_helpers.py      # Export functionality
│   ├── b2_utils.py            # Backblaze B2 utilities
│   ├── index_management.py    # MongoDB index management
│   ├── background_tasks.py    # Background task management
│   └── ray_decorator.py       # Ray decorators
│
├── # Database modules (at root level)
│   ├── async_mongo_wrapper.py # Async MongoDB wrapper
│   └── mongo_connection_pool.py # Connection pooling
│
├── docs/                      # Documentation
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
│   ├── storyweaver/
│   ├── storyweaver_edu/
│   └── storyweaver_journal/
│
├── core/                       # Empty directory (reserved for future)
├── middleware/                 # Empty directory (reserved for future)
├── utils/                      # Empty directory (reserved for future)
├── config/                     # Empty directory (reserved for future)
└── temp_exports/               # Temporary export files
```

## Module Organization

**Note**: All Python modules are currently at the root level for backwards compatibility. The directories `core/`, `auth/`, `middleware/`, `utils/`, `config/`, and `database/` exist but are empty (except `auth/` contains `casbin_model.conf`). This structure allows for future organization without breaking existing imports.

### Core Modules (Root Level)
Core application functionality:
- `config.py`: Configuration constants and environment variables
- `database.py`: Database initialization, seeding, index management
- `experiment_db.py`: Experiment database abstraction layer
- `experiment_routes.py`: Dynamic experiment route registration
- `lifespan.py`: FastAPI lifespan (startup/shutdown) management
- `manifest_schema.py`: Manifest.json schema validation
- `core_deps.py`: FastAPI dependencies (auth, DB, config)

### Authentication & Authorization (Root Level)
- `authz_provider.py`: Authorization provider interface (Casbin)
- `authz_factory.py`: Factory for creating authorization providers
- `sub_auth.py`: Sub-authentication utilities for experiments
- `role_management.py`: Role assignment and management
- `experiment_auth_restrictions.py`: Experiment-specific auth restrictions
- `auth/casbin_model.conf`: Casbin RBAC model configuration

### Middleware (Root Level)
- `middleware.py`: Experiment scope middleware, HTTPS enforcement
- `request_id_middleware.py`: Request ID tracking for logging
- `rate_limit.py`: Rate limiting using slowapi

### Utilities (Root Level)
- `utils.py`: General utilities (path validation, ObjectId validation, etc.)
- `export_helpers.py`: Export functionality (ZIP creation, B2 upload)
- `b2_utils.py`: Backblaze B2 cloud storage utilities
- `index_management.py`: MongoDB index creation and management
- `background_tasks.py`: Background task management
- `ray_decorator.py`: Ray actor decorators

### Database (Root Level)
- `async_mongo_wrapper.py`: Scoped MongoDB wrapper for experiment isolation
- `mongo_connection_pool.py`: MongoDB connection pool management

## Import Patterns

### Current Import Pattern (All modules at root)
All modules are currently imported from the root level:
```python
from config import BASE_DIR, MONGO_URI
from core_deps import get_current_user
from database import seed_admin
from sub_auth import get_experiment_sub_user
from utils import safe_objectid
from middleware import ExperimentScopeMiddleware
from async_mongo_wrapper import ScopedMongoWrapper
```

### Future Organization (Planned)
The directories `core/`, `auth/`, `middleware/`, `utils/`, `config/`, and `database/` exist but are currently empty. Future reorganization could move modules to these directories, which would require updating imports to:
```python
from core.config import BASE_DIR, MONGO_URI
from core.core_deps import get_current_user
from core.database import seed_admin
from auth.sub_auth import get_experiment_sub_user
from utils.utils import safe_objectid
from middleware.middleware import ExperimentScopeMiddleware
from database.async_mongo_wrapper import ScopedMongoWrapper
```

## Notes

1. **Python modules are currently at root level** - All core modules, auth, middleware, utils, and database modules are at the root level for backwards compatibility
2. **Documentation** is in `docs/` directory
3. **Images** are in `assets/images/` directory
4. **Scripts** are in `scripts/` directory
5. **Empty directories** (`core/`, `auth/`, `middleware/`, `utils/`, `config/`, `database/`) exist but are reserved for future organization
6. **Auth directory** contains only `casbin_model.conf` configuration file

## Current State

- All Python modules are imported directly from root (e.g., `from config import ...`)
- No breaking changes required - all imports work as-is
- Structure is prepared for future organization if needed

## Future Migration Path (Optional)

If reorganizing Python modules in the future:
1. Move modules to organized directories (core/, auth/, etc.)
2. Add `__init__.py` files to make them packages
3. Update all imports throughout codebase
4. Update experiment imports
5. Test all functionality

This would be a breaking change and should be done carefully with comprehensive testing.

