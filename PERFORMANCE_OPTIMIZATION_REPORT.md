# Performance Optimization Opportunities Report

This document identifies key performance optimization opportunities discovered during the codebase scan.

## Executive Summary

The codebase is well-structured with good async patterns, but several optimization opportunities exist:
- **Database Query Optimization**: Some areas need query batching and better indexing strategies
- **Caching Improvements**: Request-scoped caching exists but could be expanded
- **Blocking Operations**: Some file I/O and CPU-bound tasks could be optimized
- **Connection Pooling**: Already implemented but could be fine-tuned
- **Task Management**: Background tasks are well-managed but could be optimized further

---

## 1. Database Query Optimizations

### 1.1 N+1 Query Patterns

**Location**: `main.py:1324-1344` (Admin seeding)

**Issue**: Iterating over admin users and making individual database calls for role checks:
```python
for user_doc in admin_users:
    email = user_doc.get("email")
    if email:
        # Individual async call per user
        has_role = await asyncio.wait_for(
            authz.has_role_for_user(email, "admin"),
            timeout=DB_TIMEOUT
        )
```

**Optimization**: Batch role checks or use aggregation pipelines:
```python
# Batch check all admin roles at once
admin_emails = [u.get("email") for u in admin_users if u.get("email")]
role_checks = await asyncio.gather(*[
    authz.has_role_for_user(email, "admin")
    for email in admin_emails
], return_exceptions=True)
```

**Impact**: **High** - Reduces database roundtrips from N to 1 batch operation

---

### 1.2 Repeated Config Fetches

**Location**: Multiple admin routes (e.g., `configure_experiment_get`)

**Issue**: While request-scoped caching exists (`get_experiment_config`), some routes may still fetch configs multiple times.

**Current State**: ✅ Request-scoped caching is implemented in `core_deps.py:312-391`

**Recommendation**: Ensure all routes use `get_experiment_config` dependency consistently. Consider adding a global cache with TTL for frequently accessed configs.

**Impact**: **Medium** - Already partially optimized, but could benefit from application-level caching

---

### 1.3 Index Creation Race Conditions

**Location**: `async_mongo_wrapper.py:1036-1057`

**Issue**: Multiple concurrent requests could trigger duplicate index creation tasks:
```python
if collection_name not in ScopedMongoWrapper._experiment_id_index_cache:
    ScopedMongoWrapper._experiment_id_index_cache[collection_name] = False
    asyncio.create_task(_safe_experiment_id_index())
```

**Optimization**: Use a lock to prevent race conditions:
```python
_experiment_id_index_lock = asyncio.Lock()

async def __getattr__(self, name: str):
    # ... existing code ...
    if self._auto_index:
        collection_name = real_collection.name
        async with ScopedMongoWrapper._experiment_id_index_lock:
            if collection_name not in ScopedMongoWrapper._experiment_id_index_cache:
                ScopedMongoWrapper._experiment_id_index_cache[collection_name] = False
                asyncio.create_task(_safe_experiment_id_index())
```

**Impact**: **Medium** - Prevents redundant index creation operations

---

### 1.4 Auto-Index Manager Query Pattern Tracking

**Location**: `async_mongo_wrapper.py:566-642`

**Issue**: The `_query_counts` dictionary grows unbounded and tracks every unique query pattern:
```python
self._query_counts: Dict[str, int] = {}
# ...
pattern_key = index_name
self._query_counts[pattern_key] = self._query_counts.get(pattern_key, 0) + 1
```

**Optimization**: Implement LRU cache or periodic cleanup:
```python
from collections import OrderedDict

# Use OrderedDict for LRU eviction
self._query_counts: OrderedDict[str, int] = OrderedDict()
MAX_PATTERNS = 1000  # Limit tracked patterns

# In ensure_index_for_query:
pattern_key = index_name
if pattern_key in self._query_counts:
    self._query_counts.move_to_end(pattern_key)
    self._query_counts[pattern_key] += 1
else:
    if len(self._query_counts) >= MAX_PATTERNS:
        self._query_counts.popitem(last=False)  # Remove oldest
    self._query_counts[pattern_key] = 1
```

**Impact**: **Low-Medium** - Prevents memory bloat in long-running processes

---

## 2. Caching Improvements

### 2.1 Application-Level Experiment Config Cache

**Location**: `core_deps.py:312-391`

**Issue**: Request-scoped caching is good, but frequently accessed experiment configs could benefit from application-level cache with invalidation.

**Current**: Only request-scoped caching exists.

**Optimization**: Add application-level cache with TTL:
```python
from typing import Dict, Tuple
from datetime import datetime, timedelta

# In core_deps.py or main.py
_experiment_config_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

async def get_experiment_config_cached(
    request: Request,
    slug_id: str,
    projection: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    # Check request cache first
    cached = await get_experiment_config(request, slug_id, projection)
    if cached:
        return cached
    
    # Check application cache
    cache_key = f"{slug_id}:{projection}"
    if cache_key in _experiment_config_cache:
        config, timestamp = _experiment_config_cache[cache_key]
        if datetime.now() - timestamp < timedelta(seconds=CACHE_TTL_SECONDS):
            return config
        else:
            del _experiment_config_cache[cache_key]
    
    # Fetch from DB
    db = getattr(request.app.state, "mongo_db", None)
    if not db:
        raise HTTPException(500, "Database not available")
    
    config = await db.experiments_config.find_one({"slug": slug_id}, projection)
    
    # Store in app cache
    _experiment_config_cache[cache_key] = (config, datetime.now())
    
    return config
```

**Impact**: **High** - Significantly reduces database queries for popular experiments

---

### 2.2 Collection Wrapper Cache Invalidation

**Location**: `async_mongo_wrapper.py:970-1057`

**Issue**: The `_wrapper_cache` in `ScopedMongoWrapper` is per-instance and never invalidated. If collection metadata changes, stale wrappers may be used.

**Current**: Instance-level caching only.

**Optimization**: Add cache invalidation hooks:
```python
# Add class-level cache invalidation
@classmethod
def invalidate_collection_cache(cls, collection_name: str):
    """Invalidate cache for a specific collection across all instances."""
    # Could use weak references or event system for distributed invalidation
    pass
```

**Impact**: **Low** - Currently not a major issue, but good for future-proofing

---

### 2.3 Template Bytecode Cache

**Location**: `core_deps.py:68-85`, `main.py:708-730`

**Status**: ✅ Already implemented with `FileSystemBytecodeCache`

**Recommendation**: Monitor cache directory size and implement periodic cleanup for unused templates.

**Impact**: **Low** - Already optimized

---

## 3. Blocking Operations

### 3.1 Synchronous File Operations in Export Generation

**Location**: `main.py:2036-2080` (ZIP creation)

**Issue**: Some file operations are synchronous and could block the event loop:
```python
for folder_name, _, file_names in os.walk(experiment_path):
    # ...
    if file_path.suffix in (".html", ".htm"):
        original_html = file_path.read_text(encoding="utf-8")  # Synchronous
        fixed_html = _fix_static_paths(original_html, slug_id)
        zf.writestr(arcname, fixed_html)
```

**Optimization**: Use async file I/O:
```python
# Use existing _read_file_async helper
original_html = await _read_file_async(file_path)
fixed_html = _fix_static_paths(original_html, slug_id)
```

**Impact**: **Medium** - Improves concurrency during export generation

---

### 3.2 CPU-Bound Requirement Parsing

**Location**: `main.py:625-698`

**Issue**: Requirement parsing uses synchronous file I/O and CPU-bound regex operations:
```python
def _parse_requirements_file_sync(req_path: Path) -> List[str]:
    # Synchronous file read
    with req_path.open("r", encoding="utf-8") as rf:
        for raw_line in rf:
            # CPU-bound regex operations
            line = raw_line.strip()
```

**Current**: ✅ Async version exists (`_parse_requirements_file`), but sync version is used at module level.

**Optimization**: Offload parsing to thread pool:
```python
# At module level initialization
async def _init_requirements():
    return await asyncio.to_thread(_parse_requirements_file_sync, BASE_DIR / "requirements.txt")

# Use in lifespan or make synchronous but thread-safe
MASTER_REQUIREMENTS = _parse_requirements_file_sync(BASE_DIR / "requirements.txt")
```

**Impact**: **Low** - Only runs at startup, minimal impact

---

### 3.3 Directory Scanning in Admin Routes

**Location**: `main.py:3657` (`_scan_directory`)

**Issue**: Directory scanning for file tree display may be slow for large experiments:
```python
file_tree = await _scan_directory(experiment_path, experiment_path)
```

**Current**: ✅ Already async (`asyncio.to_thread` wrapper exists)

**Optimization**: Add caching for recently scanned directories:
```python
_dir_scan_cache: Dict[str, Tuple[Any, datetime]] = {}
SCAN_CACHE_TTL = 60  # 1 minute

async def _scan_directory_cached(path: Path, base_path: Path):
    cache_key = str(path)
    if cache_key in _dir_scan_cache:
        tree, timestamp = _dir_scan_cache[cache_key]
        if datetime.now() - timestamp < timedelta(seconds=SCAN_CACHE_TTL):
            return tree
    
    tree = await _scan_directory(path, base_path)
    _dir_scan_cache[cache_key] = (tree, datetime.now())
    return tree
```

**Impact**: **Medium** - Reduces I/O for repeated admin panel views

---

## 4. Connection Pooling & Resource Management

### 4.1 MongoDB Connection Pool Sizing

**Location**: `mongo_connection_pool.py:35-122`

**Current**: ✅ Already optimized with singleton pattern and configurable pool sizes (default: max=10, min=1 for actors)

**Status**: Well-implemented. Pool sizes are appropriate for Ray actors.

**Recommendation**: Consider environment-based pool sizing:
```python
max_pool_size = int(os.getenv("MONGODB_MAX_POOL_SIZE", "10"))
min_pool_size = int(os.getenv("MONGODB_MIN_POOL_SIZE", "1"))
```

**Impact**: **Low** - Already optimized, but allows runtime tuning

---

### 4.2 Ray Actor Connection Sharing

**Location**: `mongo_connection_pool.py`

**Status**: ✅ Already implements shared connection pool for Ray actors

**Recommendation**: Monitor connection count under load and adjust pool sizes if needed.

**Impact**: **Low** - Already optimized

---

### 4.3 Background Task Accumulation

**Location**: `main.py:306-344` (`BackgroundTaskManager`)

**Status**: ✅ Already implements task cleanup and prevents accumulation

**Recommendation**: Consider adding metrics/monitoring for task execution times:
```python
async def _safe_task_wrapper(self, coro: Coroutine, task_name: str):
    start_time = time.time()
    try:
        result = await coro
        duration = time.time() - start_time
        if duration > 1.0:  # Log slow tasks
            logger.warning(f"Slow background task '{task_name}': {duration:.2f}s")
        return result
    except Exception as e:
        logger.error(f"Background task '{task_name}' failed: {e}")
        raise
```

**Impact**: **Low** - Already well-optimized

---

## 5. Query Performance

### 5.1 Auto-Index Manager Fire-and-Forget Tasks

**Location**: `async_mongo_wrapper.py:807-815`

**Issue**: Fire-and-forget tasks for index creation could accumulate if many fail silently:
```python
async def _safe_index_task():
    try:
        await self.auto_index_manager.ensure_index_for_query(filter=filter, sort=sort)
    except Exception as e:
        logger.debug(f"Auto-index creation failed for query (non-critical): {e}")
asyncio.create_task(_safe_index_task())
```

**Optimization**: Use background task manager or limit concurrent tasks:
```python
# Limit concurrent index creation tasks
_index_tasks: Set[asyncio.Task] = set()
MAX_CONCURRENT_INDEX_TASKS = 5

if len(_index_tasks) < MAX_CONCURRENT_INDEX_TASKS:
    task = asyncio.create_task(_safe_index_task())
    _index_tasks.add(task)
    task.add_done_callback(_index_tasks.discard)
```

**Impact**: **Low-Medium** - Prevents task accumulation in high-traffic scenarios

---

### 5.2 Experiment ID Index Cache Management

**Location**: `async_mongo_wrapper.py:951-1057`

**Issue**: Class-level `_experiment_id_index_cache` never expires or cleans up:
```python
_experiment_id_index_cache: ClassVar[Dict[str, bool]] = {}
```

**Optimization**: Add periodic cleanup or use bounded cache:
```python
# Use OrderedDict with size limit
from collections import OrderedDict

_experiment_id_index_cache: ClassVar[OrderedDict[str, bool]] = OrderedDict()
MAX_CACHE_SIZE = 1000

# In __getattr__:
if len(_experiment_id_index_cache) >= MAX_CACHE_SIZE:
    _experiment_id_index_cache.popitem(last=False)  # Remove oldest
```

**Impact**: **Low** - Memory footprint is small, but good practice

---

## 6. Aggregation Pipeline Optimizations

### 5.3 Vector Search Filter Combination

**Location**: `async_mongo_wrapper.py:885-932`

**Status**: ✅ Well-optimized - Correctly handles `$vectorSearch` as first stage and embeds scope filter

**Recommendation**: Consider adding index hints for better query planning (if MongoDB version supports it).

**Impact**: **Low** - Already optimized

---

## 7. Request Processing Optimizations

### 7.1 Experiment Router Middleware Efficiency

**Location**: `main.py:4007+` (experiment proxy router creation)

**Issue**: Router creation for each experiment happens on every request check. Consider caching active router instances.

**Current**: Routers are created dynamically from config.

**Optimization**: Cache active routers in `app.state` and invalidate on config changes:
```python
# Cache active routers
if not hasattr(app.state, "experiment_routers"):
    app.state.experiment_routers = {}

# In route handler:
if slug_id in app.state.experiment_routers:
    router = app.state.experiment_routers[slug_id]
else:
    router = _create_experiment_proxy_router(...)
    app.state.experiment_routers[slug_id] = router
```

**Impact**: **Medium** - Reduces overhead for active experiment routes

---

### 7.2 Static File Serving Optimization

**Location**: Static file mounting in `main.py`

**Status**: ✅ Uses FastAPI's `StaticFiles` which is efficient

**Recommendation**: Consider adding cache headers for static assets:
```python
from fastapi.staticfiles import StaticFiles

app.mount(
    f"/experiments/{slug}/static",
    StaticFiles(directory=str(static_path)),
    name=f"{slug}_static"
)
# Could add middleware for cache headers
```

**Impact**: **Medium** - Improves client-side caching

---

## 8. Background Task Optimizations

### 8.1 Export Cleanup Batch Operations

**Location**: `main.py:554-586`

**Status**: ✅ Already implements batch operations for file cleanup

**Recommendation**: Consider using `asyncio.gather` for parallel deletion if many files:
```python
# Current: Sequential deletion
for export_file in files_to_delete:
    try:
        export_file.unlink()
        deleted_count += 1

# Optimized: Parallel deletion
delete_tasks = [asyncio.to_thread(f.unlink) for f in files_to_delete]
results = await asyncio.gather(*delete_tasks, return_exceptions=True)
deleted_count = sum(1 for r in results if not isinstance(r, Exception))
```

**Impact**: **Low-Medium** - Faster cleanup for large directories

---

## 9. Memory Optimizations

### 9.1 __slots__ Usage

**Location**: Multiple classes

**Status**: ✅ Already uses `__slots__` in `ScopedCollectionWrapper` and `AsyncAtlasIndexManager`

**Recommendation**: Consider adding `__slots__` to other frequently instantiated classes:
- `ExperimentDB` (if many instances)
- `Collection` wrapper (if many instances)

**Impact**: **Low** - Minor memory savings, but good practice

---

## 10. Monitoring & Observability

### 10.1 Query Performance Metrics

**Recommendation**: Add logging/metrics for slow queries:
```python
# In ScopedCollectionWrapper methods:
import time

async def find_one(self, filter=None, *args, **kwargs):
    start_time = time.time()
    result = await self._collection.find_one(scoped_filter, *args, **kwargs)
    duration = time.time() - start_time
    if duration > 1.0:  # Log slow queries
        logger.warning(f"Slow query on {self._collection.name}: {duration:.2f}s, filter={filter}")
    return result
```

**Impact**: **Medium** - Helps identify performance bottlenecks in production

---

## Priority Recommendations

### High Priority
1. **Batch Admin Role Checks** (Section 1.1) - Reduces database roundtrips significantly
2. **Application-Level Config Caching** (Section 2.1) - Reduces queries for popular experiments
3. **Async File I/O in Export Generation** (Section 3.1) - Improves concurrency

### Medium Priority
4. **Index Creation Race Condition Fix** (Section 1.3) - Prevents redundant operations
5. **Directory Scan Caching** (Section 3.3) - Reduces I/O for admin panel
6. **Experiment Router Caching** (Section 7.1) - Reduces route resolution overhead

### Low Priority
7. **Auto-Index Manager LRU Cache** (Section 1.4) - Prevents memory bloat
8. **Export Cleanup Parallelization** (Section 8.1) - Faster cleanup
9. **Query Performance Metrics** (Section 10.1) - Better observability

---

## Implementation Notes

- Most optimizations are non-breaking and can be implemented incrementally
- Test thoroughly with production-like data volumes
- Monitor memory usage when adding caches
- Consider using Redis or similar for distributed caching if running multiple instances
- Profile before and after changes to measure impact

---

## Conclusion

The codebase demonstrates good async/await patterns and resource management. The identified optimizations focus on:
- Reducing database roundtrips through batching and caching
- Improving concurrent request handling
- Optimizing resource-intensive operations

Most optimizations are incremental improvements rather than fundamental redesigns, which is a positive sign of good initial architecture.

