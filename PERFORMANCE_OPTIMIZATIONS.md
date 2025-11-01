# Performance Optimizations for main.py

This document outlines specific performance optimization opportunities identified in `main.py`.

## 🔴 High Priority Optimizations

### 1. **Database Query Optimization**

#### Issue: Sequential Database Queries in `_seed_db_from_local_files`
**Location:** Lines 1032-1091  
**Problem:** Sequential `find_one()` calls in a loop, blocking async execution.

```python
# Current (blocking):
for item in EXPERIMENTS_DIR.iterdir():
    exists = await db.experiments_config.find_one({"slug": slug})
```

**Solution:** Batch queries or use `asyncio.gather()`:
```python
# Parallelized:
slugs = [item.name for item in EXPERIMENTS_DIR.iterdir() if item.is_dir()]
existing_configs = await db.experiments_config.find({"slug": {"$in": slugs}}).to_list(None)
existing_slugs = {cfg["slug"] for cfg in existing_configs}
```

#### Issue: Loading Entire Collections with `.to_list(length=None)`
**Location:** Lines 2707, 3141, 3146  
**Problem:** Loads all documents into memory at once, can be slow for large collections.

**Solution:** Add pagination or limit results:
```python
# Use projection and limit:
db_configs_list = await db.experiments_config.find(
    {}, {"slug": 1, "status": 1, ...}  # Only fetch needed fields
).limit(1000).to_list(None)
```

#### Issue: Missing Database Indexes
**Location:** `_ensure_db_indices` (lines 853-862)  
**Problem:** Some common query patterns may not have indexes.

**Recommendation:** Add compound indexes for common queries:
```python
# Add compound indexes:
await db.experiments_config.create_index([("status", 1), ("slug", 1)], background=True)
await db.export_logs.create_index([("slug_id", 1), ("created_at", -1)], background=True)
```

### 2. **Synchronous File I/O in Async Context**

#### Issue: Blocking File Operations
**Location:** Lines 1042, 1066, 2814, 3003  
**Problem:** Synchronous `open()` and `read()` operations block the event loop.

**Solution:** Use `aiofiles` or `asyncio.to_thread()`:
```python
import aiofiles

# Replace:
with manifest_path.open("r", encoding="utf-8") as f:
    manifest_data = json.load(f)

# With:
async with aiofiles.open(manifest_path, "r", encoding="utf-8") as f:
    content = await f.read()
    manifest_data = json.loads(content)

# Or for CPU-bound JSON parsing:
content = await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
manifest_data = json.loads(content)
```

#### Issue: Synchronous Directory Scanning
**Location:** `_scan_directory` (lines 1094-1115)  
**Problem:** Recursive directory scanning blocks the event loop.

**Solution:** Use `asyncio.to_thread()` or `aiofiles`:
```python
async def _scan_directory(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
    # Wrap synchronous operation:
    return await asyncio.to_thread(_scan_directory_sync, dir_path, base_path)

def _scan_directory_sync(dir_path: Path, base_path: Path) -> List[Dict[str, Any]]:
    # Existing implementation...
```

### 3. **Missing Caching Layer**

#### Issue: Frequently Accessed Data Not Cached
**Location:** Throughout - experiment configs (lines 2431, 2514, 2923), user lookups (line 1172)

**Solution:** Implement in-memory cache with TTL:
```python
from functools import lru_cache
from cachetools import TTLCache
import asyncio

# Add to app.state during lifespan:
app.state.experiment_cache = TTLCache(maxsize=100, ttl=300)  # 5 min TTL
app.state.user_cache = TTLCache(maxsize=500, ttl=600)  # 10 min TTL

# Example cached lookup:
async def get_cached_experiment_config(db, slug_id: str, app_state):
    cache_key = f"exp_config:{slug_id}"
    if cache_key in app_state.experiment_cache:
        return app_state.experiment_cache[cache_key]
    
    config = await db.experiments_config.find_one({"slug": slug_id})
    if config:
        app_state.experiment_cache[cache_key] = config
    return config
```

**Alternative:** Use `aiocache` or `aioredis` for distributed caching:
```python
from aiocache import Cache
from aiocache.serializers import JsonSerializer

cache = Cache(Cache.MEMORY, serializer=JsonSerializer())

@cache.cached(ttl=300, key_builder=lambda f, slug_id: f"exp_config:{slug_id}")
async def get_experiment_config(db, slug_id: str):
    return await db.experiments_config.find_one({"slug": slug_id})
```

### 4. **Sequential Experiment Registration**

#### Issue: Experiments Loaded Sequentially
**Location:** `_register_experiments` (line 3174)  
**Problem:** Each experiment is processed one at a time, slowing startup/reload.

**Solution:** Parallelize where possible:
```python
async def _register_experiments(app: FastAPI, active_cfgs: List[Dict[str, Any]], *, is_reload: bool = False):
    if is_reload:
        app.state.experiments.clear()
    
    # Parallelize non-dependent operations:
    tasks = []
    for cfg in active_cfgs:
        tasks.append(_register_single_experiment(app, cfg, is_reload))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Handle results...
```

## 🟡 Medium Priority Optimizations

### 5. **CPU-Bound Operations Blocking Event Loop**

#### Issue: Synchronous CPU-Heavy Tasks
**Location:** `bcrypt.hashpw` (line 899), `json.dumps/loads` (lines 3366, 2864), `os.walk` (lines 1486, 1930, 2294)

**Solution:** Offload to thread pool:
```python
# Replace:
pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

# With:
pwd_hash = await asyncio.to_thread(
    bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt()
)
```

### 6. **Large Memory Usage for Zip Operations**

#### Issue: Entire ZIP Files Loaded in Memory
**Location:** Lines 1420, 1852, 2248  
**Problem:** `io.BytesIO` holds entire ZIP in memory, problematic for large exports.

**Solution:** Stream to disk for large files:
```python
# For large exports, write directly to disk:
async def _create_large_export_zip(slug_id, max_size_mb=100):
    zip_path = EXPORTS_TEMP_DIR / f"{slug_id}_export.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add files incrementally
        ...
    return zip_path  # Return file path instead of BytesIO
```

### 7. **Redundant Database Queries**

#### Issue: Same Config Fetched Multiple Times
**Location:** Lines 2431, 2514, 2923 - same slug_id queried multiple times  
**Problem:** Admin operations fetch config multiple times in single request.

**Solution:** Use dependency injection or request-scoped caching:
```python
async def get_experiment_config_cached(
    slug_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    request: Request = None
) -> Dict[str, Any]:
    cache_key = f"req_config:{slug_id}"
    if hasattr(request.state, "config_cache"):
        if cache_key in request.state.config_cache:
            return request.state.config_cache[cache_key]
    
    config = await db.experiments_config.find_one({"slug": slug_id})
    if config and hasattr(request.state, "config_cache"):
        request.state.config_cache[cache_key] = config
    return config
```

### 8. **Inefficient Export Aggregation**

#### Issue: Export Count Aggregation Could Use Projection
**Location:** Line 2716-2722  
**Problem:** Aggregation loads full documents when only `slug_id` is needed.

**Solution:** Add projection or use `$count`:
```python
# More efficient - use $count:
export_counts_pipeline = [
    {"$group": {"_id": "$slug_id", "count": {"$sum": 1}}},
    {"$project": {"_id": 1, "count": 1}}  # Only return needed fields
]
export_aggregation = await db.export_logs.aggregate(export_counts_pipeline).to_list(None)
```

## 🟢 Low Priority Optimizations

### 9. **Connection Pool Configuration**

#### Issue: MongoDB Connection Pool May Be Suboptimal
**Location:** Lines 506-510  
**Problem:** Default connection pool settings may not be optimal for workload.

**Solution:** Tune connection pool:
```python
client = AsyncIOMotorClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    appname="ModularLabsAPI",
    maxPoolSize=50,  # Increase pool size
    minPoolSize=10,  # Keep connections warm
    maxIdleTimeMS=45000,  # Close idle connections
    retryWrites=True,  # Enable retry for writes
    retryReads=True,   # Enable retry for reads
)
```

### 10. **Response Compression**

#### Issue: Large JSON Responses Not Compressed
**Location:** Admin dashboard and API endpoints  
**Problem:** Large JSON responses could be compressed.

**Solution:** Add FastAPI compression middleware:
```python
from fastapi.middleware.gzip import GZipMiddleware

app.add_middleware(GZipMiddleware, minimum_size=1000)  # Compress responses > 1KB
```

### 11. **Query Result Projection**

#### Issue: Fetching Unnecessary Fields
**Location:** Various `find_one()` and `find()` calls  
**Problem:** Loading entire documents when only specific fields are needed.

**Solution:** Add projection:
```python
# Instead of:
config = await db.experiments_config.find_one({"slug": slug_id})

# Use:
config = await db.experiments_config.find_one(
    {"slug": slug_id},
    {"slug": 1, "status": 1, "auth_required": 1, "runtime_s3_uri": 1}  # Only needed fields
)
```

### 12. **Background Task Optimization**

#### Issue: Index Creation Tasks Not Rate-Limited
**Location:** Line 3250  
**Problem:** Creating many index tasks simultaneously may overwhelm MongoDB.

**Solution:** Use semaphore to limit concurrent index operations:
```python
# Add to app.state:
app.state.index_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent index ops

# In _register_experiments:
async def _run_index_creation_with_limit(db, slug, collection, indexes):
    async with app.state.index_semaphore:
        await _run_index_creation_for_collection(db, slug, collection, indexes)
```

## 📊 Monitoring and Measurement

### Recommended Metrics to Track:
1. **Database Query Performance:**
   - Average query latency per endpoint
   - Slow query log (queries > 100ms)
   - Connection pool utilization

2. **Memory Usage:**
   - Peak memory during export operations
   - Cache hit rates
   - Memory leaks in long-running processes

3. **File I/O Performance:**
   - Time spent in synchronous file operations
   - Disk I/O throughput

4. **Async Operation Efficiency:**
   - Number of blocking operations per request
   - Event loop blocking time

### Tools:
- **APM:** Use tools like Datadog, New Relic, or Prometheus + Grafana
- **Profiling:** Use `py-spy` or `cProfile` to identify bottlenecks
- **MongoDB Profiler:** Enable slow query logging

## 🚀 Implementation Priority

1. **Start with:** Database query optimization (#1) and caching (#3) - highest impact
2. **Then:** Async file I/O (#2) - improves concurrency
3. **Finally:** Connection pooling (#9) and compression (#10) - polish

## 📝 Notes

- Always benchmark before and after changes
- Monitor production metrics after deploying optimizations
- Consider load testing with realistic data volumes
- Some optimizations may require additional dependencies (e.g., `aiofiles`, `cachetools`)

