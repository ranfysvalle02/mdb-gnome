# Standalone Export Implementation Notes

## Overview

This document explains the implementation of standalone exports that work with Ray actors and MongoDB. The standalone export creates a self-contained FastAPI application that can run independently of the main platform.

## Upload Compatibility

**Important**: If you want to iterate on an exported experiment and upload it back, see [UPLOAD_COMPATIBILITY.md](./UPLOAD_COMPATIBILITY.md) for details.

**Quick Answer**: Use the `/api/package-upload-ready/{slug_id}` endpoint for iterative development workflow instead of standalone export. The upload-ready export is specifically formatted for upload and doesn't include platform files.

## Key Components

### 1. Template System

- **Template File**: `templates/standalone_main_intelligent.py.jinja2`
- **Purpose**: Generates the `standalone_main.py` file for exported experiments
- **Features**:
  - Real Ray integration (not stubs)
  - Real MongoDB integration using database abstractions
  - Automatic actor creation and management
  - Experiment route injection and dependency patching

### 2. Ray Actor Integration

#### Actor Initialization
- Ray is initialized at **module level** (before FastAPI app creation)
- Ray warning suppressed: `RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO=0`
- Actor is created in the FastAPI `lifespan` manager during app startup
- Actor handle is stored in `app.state.actor_handle`
- Actor is created with `lifetime="detached"` for persistence

#### Actor Handle Injection
The standalone template **injects** a custom `get_actor_handle` function that:
1. Checks if Ray is available (`app.state.ray_is_available`)
2. Returns the actor handle from `app.state.actor_handle` (primary)
3. Falls back to `ray.get_actor()` by name if not in app.state
4. Works as both sync function (direct calls) and FastAPI dependency

**Critical Implementation Details**:
- Function injection happens **BEFORE** the experiment module is imported
- Module is loaded using `importlib.util` to control execution
- `get_actor_handle` is replaced in the module **before** decorators run
- This ensures FastAPI `Depends()` captures the injected version

### 3. Route Mounting and Redirect

**Important**: The root route `/` redirects to `/experiments/{slug}/` to ensure:
- The experiment route handler is called (which calls the actor)
- The actor response is properly passed to the template
- Without redirect, root route would render template without actor call

**Implementation**:
```python
@app.get("/", response_class=HTMLResponse)
async def standalone_index(request: Request):
    experiment_router_prefix = f"/experiments/{SLUG}"
    return RedirectResponse(url=experiment_router_prefix, status_code=status.HTTP_302_FOUND)
```

### 4. Database Abstraction

The standalone export uses the **database abstraction layer**:
- `ScopedMongoWrapper`: Automatic experiment scoping
- `ExperimentDB`: MongoDB-style API with automatic scoping
- `AsyncAtlasIndexManager`: Index management (vector search, Lucene, standard)
- `get_shared_mongo_client`: Connection pooling

**Key Files Included in Export**:
- `async_mongo_wrapper.py`
- `mongo_connection_pool.py`
- `experiment_db.py`

**Usage in Routes**:
```python
async def get_experiment_db_standalone(request: Request) -> ExperimentDB:
    # Returns ExperimentDB instance with automatic scoping
    return ExperimentDB(scoped_db)
```

### 5. Module Injection and Patching

The template uses advanced Python module manipulation:

1. **Pre-injection**: Sets `get_actor_handle` in module **before** execution
2. **Module Execution**: Loads module using `importlib.util`
3. **Post-injection**: Replaces `get_actor_handle` **after** execution (in case module redefined it)
4. **Dependency Patching**: Patches FastAPI route dependencies to use injected function

**Why This Is Necessary**:
- Python decorators evaluate their arguments at decoration time
- `@bp.get("/", Depends(get_actor_handle))` captures the function reference immediately
- We must inject our version **before** the decorator runs

## Common Issues and Solutions

### Issue: Actor Not Responding

**Symptoms**:
- Route returns 200 OK but no actor response
- No logs from actor method calls

**Root Cause**:
- Root route `/` was serving template directly without calling actor
- Experiment route mounted at `/experiments/{slug}/` wasn't being hit

**Solution**:
- Root route now redirects to `/experiments/{slug}/`
- This ensures experiment route handler (which calls actor) is executed

### Issue: Ray Warning Messages

**Symptoms**:
- Ray warning: `RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO`

**Solution**:
```python
os.environ.setdefault("RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO", "0")
```
Set this **before** Ray initialization.

### Issue: Actor Handle Not Found

**Symptoms**:
- `get_actor_handle` fails to find actor
- HTTP 503 or 500 errors

**Checklist**:
1. Verify Ray is initialized (`ray.is_initialized()`)
2. Check `app.state.ray_is_available = True` is set in lifespan
3. Verify actor is created and stored in `app.state.actor_handle`
4. Ensure actor name matches: `{slug}-actor`
5. Ensure namespace matches: `"modular_labs"`

### Issue: Function Injection Not Working

**Symptoms**:
- Original `get_actor_handle` from experiment module is called
- Not seeing logs from injected function

**Solution**:
1. Ensure injection happens **before** module import
2. Verify `get_actor_handle` is replaced in `sys.modules`
3. Check that FastAPI route dependencies are patched
4. Add logging to injected function to verify it's being called

## Export Process

### Files Included in Standalone Export

1. **Python Modules**:
   - `standalone_main.py` (generated from template)
   - `experiments/{slug}/` (experiment code)
   - `async_mongo_wrapper.py`
   - `mongo_connection_pool.py`
   - `experiment_db.py`

2. **Data Files**:
   - `db_config.json` (experiment configuration)
   - `db_collections.json` (database collections snapshot)

3. **Configuration**:
   - `requirements.txt` (includes Ray and MongoDB dependencies)
   - `README.md` (setup instructions)

### Dependencies Required

**Base Requirements** (always included):
- `fastapi`
- `uvicorn[standard]`
- `motor>=3.0.0`
- `pymongo==4.15.3`
- `ray[default]>=2.9.0` (Ray is required for actors)
- `jinja2`
- `python-multipart`

## Usage Example

### Running the Exported Experiment

1. **Extract the export ZIP**
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Set environment variables** (if needed):
   ```bash
   export MONGO_URI=mongodb://localhost:27017/
   export DB_NAME=labs_db
   ```
4. **Run the application**:
   ```bash
   python standalone_main.py
   ```
5. **Access the experiment**:
   - Root: `http://localhost:8000/` (redirects to experiment route)
   - Experiment: `http://localhost:8000/experiments/{slug}/`
   - API Docs: `http://localhost:8000/docs`

## Technical Details

### Ray Initialization Flow

1. **Module Level** (before app creation):
   ```python
   os.environ.setdefault("RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO", "0")
   ray.init(namespace="modular_labs", ...)
   ```

2. **Lifespan Manager** (during app startup):
   ```python
   actor_handle = ExperimentActor.options(...).remote(...)
   app.state.actor_handle = actor_handle
   app.state.ray_is_available = True
   ```

3. **Route Handler** (when route is called):
   ```python
   actor = get_standalone_actor_handle(request)  # Injected function
   greeting = await actor.say_hello.remote()
   ```

### Module Injection Flow

1. **Before Module Load**:
   ```python
   exp_mod.get_actor_handle = get_standalone_actor_handle
   sys.modules[mod_name] = exp_mod
   ```

2. **Module Execution**:
   ```python
   spec.loader.exec_module(exp_mod)  # Decorators run here
   ```

3. **After Module Load**:
   ```python
   exp_mod.get_actor_handle = get_standalone_actor_handle  # Replace again
   # Patch FastAPI route dependencies
   ```

### Database Initialization Flow

1. **Connection**:
   ```python
   _shared_mongo_client = get_shared_mongo_client(MONGO_URI, ...)
   _scoped_db = ScopedMongoWrapper(real_db, read_scopes=[SLUG], write_scope=SLUG)
   ```

2. **Seeding**:
   ```python
   db = ExperimentDB(_scoped_db)
   await collection.insert_many(docs)
   ```

3. **Index Management**:
   ```python
   index_manager = AsyncAtlasIndexManager(real_db[collection_name])
   await index_manager.ensure_indexes(index_definitions)
   ```

## Best Practices

1. **Always use database abstractions**: Use `ExperimentDB` instead of direct Motor/MongoDB access
2. **Test actor calls**: Ensure actor methods are actually being called (add logging)
3. **Verify injection**: Check logs to confirm injected functions are being used
4. **Route paths**: Remember routes are mounted at `/experiments/{slug}/` not root
5. **Actor initialization**: Actors are created during app startup, not on first request

## Debugging Tips

1. **Enable verbose logging**: Set `LOG_LEVEL=DEBUG` in environment
2. **Check actor accessibility**: Use `ray.get_actor(name, namespace)` directly
3. **Verify route mounting**: Check `app.include_router()` calls
4. **Inspect app.state**: Log `app.state` to see what's stored
5. **Test actor directly**: Call actor methods from Python REPL

## Future Improvements

- [ ] Add actor health check endpoint
- [ ] Support multiple actors per experiment
- [ ] Add graceful actor shutdown
- [ ] Support Ray cluster connection (not just local)
- [ ] Add actor metrics/monitoring

