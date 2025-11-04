# Project Reorganization Summary

This document describes the reorganization of the mdb-gnome project structure.

## What Changed

### Files Moved

#### Documentation → `docs/`
- All `.md` files except `README.md` and `LICENSE` moved to `docs/`
- `README.md` kept at root (standard practice)
- `LICENSE` kept at root (standard practice)
- `experiments/DATABASE_USAGE.md` moved to `docs/`

#### Images → `assets/images/`
- All `.png` image files moved to `assets/images/`
- Updated references in `README.md` to point to new locations

#### Scripts → `scripts/`
- `llms-code-gen.py` moved to `scripts/`
- `llms-code*.txt` files moved to `scripts/`

#### Configuration Files
- `casbin_model.conf` moved to `auth/` (logical grouping)

### Directories Created

- `docs/` - All documentation
- `assets/images/` - Image assets
- `scripts/` - Utility scripts
- `core/`, `middleware/`, `utils/`, `config/`, `database/` - Created for future organization (currently empty)
- `auth/` - Contains only `casbin_model.conf` (configuration file)

## Current Structure

```
mdb-gnome/
├── README.md                  # Main documentation (kept at root)
├── LICENSE                    # License (kept at root)
├── requirements.txt           # Dependencies
├── Dockerfile                 # Docker config
├── docker-compose.yml         # Docker Compose
├── anyscale-service.yaml      # Deployment config
│
├── main.py                    # Main application
├── *.py                       # Python modules (all at root level)
│   ├── config.py              # Configuration
│   ├── database.py            # Database initialization
│   ├── core_deps.py           # Core dependencies
│   ├── lifespan.py            # Lifespan management
│   ├── experiment_routes.py   # Experiment routes
│   ├── middleware.py          # Middleware
│   ├── authz_provider.py      # Auth provider
│   ├── utils.py               # Utilities
│   └── ...                    # Other modules
│
├── docs/                      # ✨ NEW: All documentation
│   ├── SECURITY.md
│   ├── ADVANCED_AUTH.md
│   ├── SUB_AUTH_GUIDE.md
│   └── ...
│
├── assets/                    # ✨ NEW: Static assets
│   └── images/                # ✨ NEW: Image files
│       ├── gnome.png
│       └── ...
│
├── scripts/                    # ✨ NEW: Utility scripts
│   ├── llms-code-gen.py
│   └── ...
│
├── templates/                 # Templates (unchanged)
├── experiments/               # Experiments (unchanged)
└── temp_exports/              # Temporary files (unchanged)
```

## Why Python Modules Stay at Root

**Current State**: All Python modules remain at root level for backwards compatibility.

**Actual Structure**:
- All core modules (`config.py`, `database.py`, `lifespan.py`, `core_deps.py`, etc.) are at root
- All auth modules (`authz_provider.py`, `authz_factory.py`, `sub_auth.py`, etc.) are at root
- All middleware (`middleware.py`, `request_id_middleware.py`, `rate_limit.py`) are at root
- All utilities (`utils.py`, `export_helpers.py`, `b2_utils.py`, etc.) are at root
- All database modules (`async_mongo_wrapper.py`, `mongo_connection_pool.py`) are at root

**Reason**: Many imports throughout the codebase reference modules directly:
```python
from config import BASE_DIR
from core_deps import get_current_user
from database import seed_admin
from sub_auth import get_experiment_sub_user
```

**Future Organization** (Optional): Python modules could be organized into:
- `core/` - Core application modules (currently empty)
- `auth/` - Authentication & authorization (currently contains only `casbin_model.conf`)
- `middleware/` - Middleware modules (currently empty)
- `utils/` - Utility modules (currently empty)
- `database/` - Database modules (currently empty)

**Migration Path**: See `docs/PROJECT_STRUCTURE.md` for detailed migration plan. This would be a breaking change requiring comprehensive testing.

## Image Path Updates

Images referenced in `README.md` have been updated:
- `gnome.png` → `assets/images/gnome.png`
- `gnome-graveyard.png` → `assets/images/gnome-graveyard.png`

If serving images via web server, ensure `assets/` is properly mounted or images are copied to appropriate static directory.

## Benefits

1. **Cleaner Root Directory**: Easier to navigate and understand project structure
2. **Better Organization**: Related files grouped together
3. **Clear Separation**: Documentation, assets, and scripts separated from code
4. **Future-Ready**: Structure prepared for further Python module organization

## No Breaking Changes

- All Python imports still work (modules at root)
- Application functionality unchanged
- Only file locations changed, not functionality

## Next Steps (Optional)

To fully organize Python modules:

1. **Plan**: Review all imports in codebase
2. **Move**: Move modules to organized directories
3. **Update**: Update all imports
4. **Test**: Comprehensive testing of all functionality
5. **Document**: Update documentation with new import patterns

This is a **breaking change** and should be done with care.

