# Code Analysis: g.nome Core Platform

This document provides a comprehensive analysis of the g.nome platform's core architecture and functionality, focusing on the engine that powers the system.

## Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Core Components](#core-components)
4. [Request Flow](#request-flow)
5. [Key Mechanisms](#key-mechanisms)
6. [Data Layer](#data-layer)
7. [Authorization & Security](#authorization--security)
8. [Experiment Management](#experiment-management)
9. [Configuration & Startup](#configuration--startup)

---

## Project Overview

g.nome is a **"WordPress-like" platform** for Python and MongoDB projects. It's designed to minimize the friction between idea and live application by providing a centralized engine that handles common infrastructure concerns (authentication, authorization, database access, routing) so developers can focus on building features.

### Core Philosophy

- **The CORE (Engine)**: A single FastAPI application (`main.py`) that provides shared infrastructure
- **Experiments (Plugins)**: Self-contained modules dropped into `/experiments` directory
- **Zero Boilerplate**: Experiments inherit authentication, data sandboxing, and routing automatically

---

## Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI Application                      │
│                      (main.py + routes)                       │
├─────────────────────────────────────────────────────────────┤
│  Middleware Layer                                            │
│  ├── ExperimentScopeMiddleware                               │
│  ├── ProxyAwareHTTPSMiddleware                               │
│  └── HTTPSEnforcementMiddleware                              │
├─────────────────────────────────────────────────────────────┤
│  Core Dependencies (core_deps.py)                            │
│  ├── Authentication (JWT-based)                              │
│  ├── Authorization (Casbin RBAC)                             │
│  └── Database Wrapper (ScopedMongoWrapper)                  │
├─────────────────────────────────────────────────────────────┤
│  Experiment Loader (experiment_routes.py)                    │
│  └── Dynamic Module Loading                                  │
├─────────────────────────────────────────────────────────────┤
│  Experiment Modules (/experiments/{slug}/)                    │
│  ├── __init__.py (APIRouter as 'bp')                         │
│  ├── actor.py (Ray Actor, optional)                          │
│  └── manifest.json (Configuration)                           │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
    MongoDB               Ray Cluster         Backblaze B2
    (Motor/Async)         (Optional)          (Optional)
```

### Technology Stack

- **Web Framework**: FastAPI with ASGI (Uvicorn)
- **Database**: MongoDB via Motor (async driver)
- **Authorization**: Casbin (pluggable via Protocol interface)
- **Computation**: Ray (for isolated experiment execution)
- **Storage**: Backblaze B2 (S3-compatible, optional)
- **Templates**: Jinja2 with bytecode caching
- **Session**: JWT-based authentication via cookies

---

## Core Components

### 1. Application Entry Point (`main.py`)

The main FastAPI application that:
- Initializes the application with lifespan management
- Registers core routes (auth, admin panel)
- Sets up middleware pipeline
- Handles experiment discovery and loading

**Key Routes:**
- `/auth/login`, `/auth/logout` - Authentication endpoints
- `/admin/*` - Admin panel for experiment management
- `/experiments/{slug}/*` - Dynamically loaded experiment routes

### 2. Lifespan Management (`lifespan.py`)

Manages application startup and shutdown lifecycle:

**Startup Sequence:**
1. Initialize Jinja2 templates with bytecode caching
2. Initialize Backblaze B2 client (if configured)
3. Connect to Ray cluster (or start local instance)
4. Connect to MongoDB with connection pooling
5. Initialize authorization provider (Casbin by default)
6. Seed database (create indexes, admin user, demo user)
7. Discover experiments from filesystem (`seed_db_from_local_files`)
8. Load active experiments (`reload_active_experiments`)

**Shutdown Sequence:**
1. Cancel background tasks (export cleanup)
2. Close MongoDB connection
3. Shutdown Ray cluster connection

### 3. Experiment Loader (`experiment_routes.py`)

Dynamic module loading system that:

1. **Discovers Experiments**: Scans `/experiments` directory for folders containing `manifest.json`
2. **Validates Manifests**: Uses `manifest_schema.py` to validate experiment configuration
3. **Loads Modules**: Dynamically imports `experiments.{slug}.__init__.py` and extracts `bp` (APIRouter)
4. **Registers Routes**: Mounts each experiment's router at `/experiments/{slug}/`
5. **Starts Ray Actors**: If Ray is available, loads `actor.py` and creates Ray actors
6. **Mounts Static Files**: Automatically serves static files from `/experiments/{slug}/static/`
7. **Manages Indexes**: Creates/updates MongoDB indexes defined in `manifest.json`

**Experiment Registration Flow:**
```
For each active experiment:
  ├── Validate manifest.json schema
  ├── Import experiments.{slug}.__init__.py
  ├── Extract 'bp' (APIRouter) variable
  ├── Add authentication dependencies
  ├── Mount router at /experiments/{slug}/
  ├── Load actor.py (if exists) and create Ray actor
  ├── Mount static files from /experiments/{slug}/static/
  └── Create/update indexes from manifest.json
```

### 4. Database Wrapper (`async_mongo_wrapper.py`)

Provides automatic data sandboxing through two wrapper classes:

#### `ScopedMongoWrapper`
- Wraps `AsyncIOMotorDatabase`
- Provides attribute-based collection access (e.g., `db.my_collection`)
- Automatically prefixes collection names with experiment slug
- Example: `db.clicks` → actual collection: `click_tracker_clicks`

#### `ScopedCollectionWrapper`
- Wraps `AsyncIOMotorCollection`
- **Read Operations**: Automatically filters by `experiment_id` using `read_scopes`
- **Write Operations**: Automatically injects `experiment_id` using `write_scope`
- **Index Management**: Provides `index_manager` property for Atlas Search/Vector indexes
- **Auto-Indexing**: Automatically creates indexes based on query patterns

**Data Sandboxing Example:**
```python
# In an experiment route
@bp.get("/")
async def index(db: ScopedMongoWrapper = Depends(get_scoped_db)):
    # Read: Automatically filtered to experiment_id in read_scopes
    count = await db.clicks.count_documents({})  # Only sees own data
    
    # Write: Automatically tagged with experiment_id
    await db.clicks.insert_one({"event": "click"})  # Gets experiment_id: "my_experiment"
```

**Two-Layer Scoping:**
1. **Physical Layer**: Collection names prefixed with slug (`{slug}_{collection}`)
2. **Logical Layer**: Documents tagged with `experiment_id` field for cross-experiment access

### 5. Core Dependencies (`core_deps.py`)

FastAPI dependency injection helpers:

#### Authentication Dependencies
- `get_current_user()` - Optional user from JWT cookie
- `get_current_user_or_redirect()` - Required user, redirects to login if missing
- `require_admin()` - Requires admin role via authorization provider

#### Authorization Dependencies
- `require_permission(obj, act)` - Factory function for permission checks
- `require_experiment_ownership_or_admin()` - Checks experiment ownership
- `get_authz_provider()` - Retrieves pluggable authorization provider

#### Database Dependencies
- `get_scoped_db()` - Provides `ScopedMongoWrapper` with experiment context
- `get_experiment_config()` - Cached experiment configuration lookup
- `get_experiment_configs_batch()` - Batch config loading (prevents N+1 queries)

#### Template Dependencies
- `_ensure_templates()` - Validates global Jinja2 templates are loaded

### 6. Middleware (`middleware.py`)

#### `ExperimentScopeMiddleware`
- Intercepts requests to `/experiments/{slug}/...`
- Extracts experiment slug from URL path
- Looks up experiment config from `app.state.experiments`
- Injects `slug_id` and `read_scopes` into `request.state`
- Enables `get_scoped_db()` to determine experiment context

#### `ProxyAwareHTTPSMiddleware`
- Detects proxy headers (`X-Forwarded-Proto`, `Forwarded`, etc.)
- Rewrites `request.url` to reflect actual client scheme/host
- Ensures `url_for()` generates correct HTTPS URLs behind proxies
- Handles localhost vs. production environments

#### `HTTPSEnforcementMiddleware`
- Adds HSTS headers when HTTPS is detected
- Sanitizes response bodies to replace `http://` with `https://`
- Prevents mixed content issues
- Only enforces when request actually came via HTTPS

### 7. Authorization Provider (`authz_provider.py`)

Pluggable authorization system using Python Protocol:

```python
class AuthorizationProvider(Protocol):
    async def check(subject, resource, action, user_object) -> bool
```

**Default Implementation: `CasbinAdapter`**
- Wraps Casbin `AsyncEnforcer`
- Uses thread pool execution to prevent blocking event loop
- Implements 5-minute TTL cache for authorization results
- Provides helper methods: `add_policy()`, `add_role_for_user()`, etc.

**Authorization Factory (`authz_factory.py`)**
- Creates appropriate provider based on `AUTHZ_PROVIDER` environment variable
- Default: `casbin` (uses Casbin with MongoDB adapter)
- Stores policies in MongoDB collection

---

## Request Flow

### Typical Request Flow for Experiment Route

```
1. Request arrives at FastAPI
   ↓
2. ProxyAwareHTTPSMiddleware
   - Detects proxy headers
   - Rewrites request.url for correct scheme/host
   ↓
3. HTTPSEnforcementMiddleware
   - Adds HSTS headers if HTTPS
   - Sanitizes mixed content
   ↓
4. ExperimentScopeMiddleware
   - Parses /experiments/{slug}/...
   - Extracts slug from path
   - Looks up experiment config
   - Injects slug_id + read_scopes into request.state
   ↓
5. FastAPI Route Handler
   - Extracts path parameters
   - Executes dependencies (auth, db, etc.)
   ↓
6. Dependency Injection
   ├── get_current_user() → Decodes JWT cookie
   ├── get_scoped_db() → Creates ScopedMongoWrapper with experiment context
   └── require_permission() → Checks authorization via Casbin
   ↓
7. Route Handler Logic
   - Accesses database via ScopedMongoWrapper
   - All reads automatically filtered by experiment_id
   - All writes automatically tagged with experiment_id
   ↓
8. Response
   - Renders template or returns JSON
   - HTTPSEnforcementMiddleware sanitizes response
```

### Database Access Flow

```python
# In experiment route
@bp.get("/")
async def index(
    request: Request,
    db: ScopedMongoWrapper = Depends(get_scoped_db)
):
    # 1. get_scoped_db() is called
    #    - Reads request.state.slug_id (from middleware)
    #    - Reads request.state.read_scopes (from middleware)
    #    - Creates ScopedMongoWrapper(real_db, read_scopes, write_scope)
    
    # 2. Access collection
    collection = db.clicks  # Attribute access
    
    # 3. ScopedMongoWrapper.__getattr__()
    #    - Prefixes: "clicks" → "my_experiment_clicks"
    #    - Gets real collection from MongoDB
    #    - Creates ScopedCollectionWrapper
    
    # 4. Query operation
    count = await db.clicks.count_documents({})
    
    # 5. ScopedCollectionWrapper.count_documents()
    #    - Injects filter: {"experiment_id": {"$in": read_scopes}}
    #    - Calls real collection.count_documents(scoped_filter)
    #    - Returns result
```

---

## Key Mechanisms

### 1. Automatic Data Sandboxing

**Problem**: Multiple experiments sharing one MongoDB database need isolation.

**Solution**: Two-layer scoping system:

1. **Physical Scoping** (Collection Prefixing)
   - Collection names prefixed: `{slug}_{collection_name}`
   - Prevents name collisions between experiments
   - Example: `db.clicks` in `click_tracker` → `click_tracker_clicks`

2. **Logical Scoping** (experiment_id Tagging)
   - All writes automatically include `experiment_id` field
   - All reads automatically filtered by `experiment_id` in `read_scopes`
   - Enables cross-experiment data sharing via `data_scope` config

**Cross-Experiment Access:**
```python
# In stats_dashboard experiment
# If data_scope includes ["self", "click_tracker"]:

# Read from another experiment's collection
clicks = await db.get_collection("click_tracker_clicks").count_documents({})
# Automatically filtered to: {"experiment_id": {"$in": ["self", "click_tracker"]}}

# Write to own collection
await db.logs.insert_one({"event": "view"})
# Automatically tagged with: {"experiment_id": "stats_dashboard"}
```

### 2. Dynamic Experiment Loading

Experiments are discovered and loaded at runtime:

1. **Filesystem Discovery** (`seed_db_from_local_files`):
   - Scans `/experiments` for directories
   - Reads `manifest.json` from each directory
   - Creates database records if manifest exists but no DB entry
   - Updates status if manifest says "active" but DB has "draft"

2. **Module Loading** (`_register_experiments`):
   - Dynamically imports `experiments.{slug}.__init__.py`
   - Extracts `bp` variable (must be `APIRouter`)
   - Validates manifest schema before registration
   - Registers router with authentication dependencies

3. **Ray Actor Loading**:
   - If `actor.py` exists, imports `ExperimentActor` class
   - Creates Ray actor with experiment-specific `runtime_env`
   - Parses `requirements.txt` for isolated dependencies (if `G_NOME_ENV=isolated`)
   - Stores actor handle in `app.state` for experiment use

### 3. Index Management

**Declarative Index Definition** (`manifest.json`):
```json
{
  "managed_indexes": {
    "workouts": [
      {
        "name": "user_timestamp",
        "type": "regular",
        "keys": [["user_id", 1], ["timestamp", -1]]
      },
      {
        "name": "embedding_index",
        "type": "vectorSearch",
        "definition": { ... }
      }
    ]
  }
}
```

**Automatic Index Creation**:
- Indexes are created/updated when experiment is loaded
- Collection names and index names are prefixed with slug
- Atlas Search/Vector indexes wait until `QUERYABLE` before completion
- Regular indexes are created with `background=True` to avoid blocking

**Auto-Indexing** (Query-Based):
- `ScopedCollectionWrapper` analyzes query filters and sort specifications
- Automatically creates indexes based on query patterns
- Uses usage threshold (default: 3 queries) before creating
- Prevents over-indexing with intelligent heuristics

### 4. Authentication & Authorization Flow

**Authentication (JWT-based)**:
1. User submits login form (`/auth/login`)
2. Password verified against MongoDB `users` collection (bcrypt)
3. JWT token created with user email and metadata
4. Token stored in HTTP-only cookie named `token`
5. Subsequent requests decode token in `get_current_user()`

**Authorization (Casbin RBAC)**:
1. Casbin model defines roles and permissions (stored in `casbin_model.conf`)
2. Policies stored in MongoDB: `(role, resource, action)` tuples
3. Roles assigned to users: `(user_email, role)`
4. Permission checks via `require_permission()` dependency factory
5. Results cached for 5 minutes to reduce database queries

**Role Hierarchy**:
- `admin` → Full access to admin panel and all experiments
- `developer` → Can manage own experiments (`experiments:manage_own`)
- `demo` → Read-only access to view experiments (`experiments:view`)
- Default users → No special permissions

### 5. Configuration Management

**Configuration Sources** (priority order):
1. **Database** (`experiments_config` collection) - Source of truth at runtime
2. **Filesystem** (`manifest.json`) - Used for discovery and local development
3. **Environment Variables** - Application-level config (MongoDB, B2, Ray, etc.)

**Experiment Configuration Fields**:
- `slug` - Unique identifier (directory name)
- `name` - Display name
- `description` - Human-readable description
- `status` - `active` | `draft` | `inactive`
- `auth_required` - Whether login is required
- `data_scope` - Array of experiment slugs this experiment can read from
- `managed_indexes` - Declarative index definitions
- `owner_email` - Experiment owner (for developer access control)
- `runtime_s3_uri` - S3/B2 URL for experiment code (for isolated execution)

**Configuration Caching**:
- Request-scoped cache: Avoids duplicate fetches within single request
- Application-level cache: 5-minute TTL with LRU eviction (max 100 entries)
- Batch loading: `get_experiment_configs_batch()` prevents N+1 queries

---

## Data Layer

### MongoDB Connection Management

**Connection Pooling**:
- Motor client configured with `maxPoolSize` and `minPoolSize` (from env or defaults)
- Connection pool shared across all experiments
- Automatic retry for writes and reads (`retryWrites`, `retryReads`)

**Database Structure**:
```
labs_db (database)
├── users (collection)
│   ├── email (unique index)
│   ├── password_hash
│   └── is_admin
├── experiments_config (collection)
│   ├── slug (unique index)
│   ├── owner_email (index)
│   └── ... (manifest fields)
├── casbin_rule (collection)
│   └── (Casbin policies)
└── {slug}_{collection_name} (experiment collections)
    └── experiment_id (auto-indexed)
```

### Collection Naming Convention

- **Experiment Collections**: `{slug}_{collection_name}`
  - Example: `click_tracker_clicks`, `stats_dashboard_logs`
- **Core Collections**: No prefix
  - Example: `users`, `experiments_config`, `casbin_rule`

### Index Management Strategy

1. **Experiment_id Index**: Automatically created on all collections (used in all queries)
2. **Declarative Indexes**: Defined in `manifest.json`, created on experiment load
3. **Auto-Indexes**: Created based on query patterns (equality, range, sorting)
4. **Atlas Search/Vector**: Managed via `AsyncAtlasIndexManager`, waits until queryable

---

## Authorization & Security

### Authentication Implementation

**JWT Token Structure**:
```python
{
  "email": "user@example.com",
  "is_admin": true/false,
  "exp": <timestamp>
}
```

**Cookie Configuration**:
- Name: `token`
- HTTP-only: Yes (prevents XSS)
- Secure: Yes (HTTPS only, when detected)
- SameSite: Lax (CSRF protection)

### Authorization Provider Interface

**Protocol-Based Design**:
- `AuthorizationProvider` is a Python Protocol (duck typing)
- Allows swapping implementations (Casbin, custom, etc.)
- Factory function creates provider based on `AUTHZ_PROVIDER` env var

**Casbin Model** (`casbin_model.conf`):
```
[request_definition]
r = sub, obj, act

[policy_definition]
p = role, resource, action

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.role) && r.obj == p.resource && r.act == p.action
```

**Policy Storage** (MongoDB):
- Policies: `(role, resource, action)` tuples
- Role assignments: `(user_email, role)` tuples
- Stored in `casbin_rule` collection via `CasbinMotorAdapter`

### Security Features

1. **Open Redirect Prevention**: `_validate_next_url()` sanitizes redirect URLs
2. **HTTPS Enforcement**: Middleware detects and enforces HTTPS when available
3. **Mixed Content Prevention**: Response sanitization replaces HTTP URLs with HTTPS
4. **XSS Protection**: HTTP-only cookies, input sanitization
5. **CSRF Protection**: SameSite cookie attribute
6. **Password Hashing**: bcrypt with automatic salt generation

---

## Experiment Management

### Admin Panel

Located at `/admin`, provides:

1. **Experiment Dashboard**:
   - Lists all experiments with status, owner, auth requirements
   - Search and filter capabilities
   - Export/download functionality

2. **Experiment Configuration**:
   - Activate/deactivate experiments
   - Configure `auth_required`
   - Configure `data_scope` (which experiments can be read)
   - Set `owner_email` for developer access control
   - Upload experiments via ZIP file

3. **Export Management**:
   - Create standalone packages (with platform files)
   - Create upload-ready packages (experiment files only)
   - Create Docker packages
   - View export history

### Experiment Upload Flow

1. **ZIP Upload** (`/admin/api/upload-experiment`):
   - Validates ZIP structure (must contain `manifest.json`, `actor.py`, `__init__.py`)
   - Extracts to `/experiments/{slug}/`
   - Validates manifest schema
   - Updates database record

2. **B2 Storage** (if configured):
   - Uploads ZIP to Backblaze B2 bucket
   - Generates presigned download URL
   - Stores URL in `runtime_s3_uri` field
   - Ray uses URL for isolated runtime environment

3. **Reload**:
   - Calls `reload_active_experiments()`
   - Registers routes, starts Ray actors, creates indexes

### Experiment Export Types

1. **Standalone Export**:
   - Includes platform files (`main.py`, `Dockerfile`, etc.)
   - Includes database snapshots
   - Generates `standalone_main.py` for independent execution
   - For "graduating" experiments to independent apps

2. **Upload-Ready Export**:
   - Experiment files only (no platform files)
   - Files at root level (no nested structure)
   - Optimized for iterative development

3. **Docker Export**:
   - Includes platform files and database snapshots
   - Optimized for containerized deployments

---

## Configuration & Startup

### Environment Variables

**Required:**
- `MONGO_URI` - MongoDB connection string
- `FLASK_SECRET_KEY` - JWT signing secret (should be strong random string)

**Optional:**
- `ADMIN_EMAIL` - Default admin email (default: `admin@example.com`)
- `ADMIN_PASSWORD` - Default admin password (default: `password123`)
- `ENABLE_DEMO` - Demo user credentials (`email:password` format)
- `ENABLE_REGISTRATION` - Allow user registration (default: `true`)
- `G_NOME_ENV` - Environment mode: `production` | `isolated` (default: `production`)
- `RAY_ADDRESS` - Ray cluster address (if remote, else starts local)
- `AUTHZ_PROVIDER` - Authorization provider name (default: `casbin`)
- `FORCE_HTTPS` - Force HTTPS detection (default: `false`)
- `LOG_LEVEL` - Logging level (default: `INFO`)

**Backblaze B2 (Optional):**
- `B2_APPLICATION_KEY_ID` / `B2_ACCESS_KEY_ID`
- `B2_APPLICATION_KEY` / `B2_SECRET_ACCESS_KEY`
- `B2_BUCKET_NAME`
- `B2_ENDPOINT_URL` (legacy support)

**MongoDB Pool Configuration:**
- `MONGO_MAIN_MAX_POOL_SIZE` - Max connection pool size (default: `50`)
- `MONGO_MAIN_MIN_POOL_SIZE` - Min connection pool size (default: `10`)

**Cache Configuration:**
- `EXPERIMENT_CONFIG_CACHE_TTL_SECONDS` - Config cache TTL (default: `300`)
- `EXPERIMENT_CONFIG_CACHE_MAX_SIZE` - Max cache entries (default: `100`)

### Startup Sequence

1. **Configuration Loading** (`config.py`):
   - Loads environment variables
   - Validates required configuration
   - Sets up logging with request ID context

2. **Application Initialization** (`lifespan.py`):
   - Initializes templates
   - Connects to B2 (if configured)
   - Connects to Ray (or starts local)
   - Connects to MongoDB
   - Initializes authorization provider
   - Seeds database (indexes, admin user, demo user)
   - Discovers experiments from filesystem
   - Loads active experiments

3. **Middleware Registration** (`main.py`):
   - `ProxyAwareHTTPSMiddleware`
   - `HTTPSEnforcementMiddleware`
   - `ExperimentScopeMiddleware`
   - `RequestIDMiddleware` (for logging)

4. **Route Registration** (`main.py`):
   - Core routes (auth, admin panel)
   - Experiment routes (dynamically loaded)

### Error Handling

- **Startup Errors**: Critical errors (MongoDB connection failure, AuthZ initialization failure) cause application to crash
- **Experiment Loading Errors**: Individual experiment failures are logged but don't block other experiments
- **Runtime Errors**: Unhandled exceptions return 500 with error details in development, generic message in production

---

## Key Design Patterns

### 1. Dependency Injection

FastAPI's dependency injection system is used throughout:
- Authentication checks via `Depends(get_current_user)`
- Database access via `Depends(get_scoped_db)`
- Authorization checks via `Depends(require_permission(...))`
- Experiment config via `Depends(get_experiment_config)`

### 2. Protocol-Based Pluggability

Authorization uses Python Protocol for pluggability:
- `AuthorizationProvider` Protocol defines interface
- Implementations can be swapped without changing core code
- Factory function creates provider based on configuration

### 3. Middleware Pipeline

Middleware processes requests in order:
1. Proxy detection and URL rewriting
2. HTTPS enforcement
3. Experiment scope extraction
4. Request ID injection (for logging)

### 4. Lazy Loading

- Ray actors are created on-demand when experiments are loaded
- Index managers are instantiated lazily when first accessed
- Template bytecode cache is generated on first render

### 5. Background Tasks

- Index creation runs in background (non-blocking)
- Export cleanup runs on schedule (every 6 hours)
- Managed via `BackgroundTaskManager` to prevent task accumulation

---

## Summary

g.nome provides a powerful platform for building modular Python applications with:

- **Automatic Data Isolation**: Two-layer scoping ensures experiments can't access each other's data
- **Zero-Config Authentication**: JWT-based auth with automatic cookie handling
- **Flexible Authorization**: Pluggable RBAC system with Casbin as default
- **Dynamic Loading**: Experiments discovered and loaded at runtime
- **Isolated Execution**: Ray integration for dependency isolation
- **Declarative Configuration**: Manifests define experiments, not code
- **Developer Experience**: Admin panel for visual experiment management

The core engine handles all the infrastructure concerns, allowing developers to focus purely on building features in their experiments.

