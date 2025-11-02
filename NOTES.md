# System Architecture & Performance Notes

## Overview

This document tracks architecture decisions, performance characteristics, scalability considerations, and optimization opportunities for the g.nome platform.

## Architecture Components

### Core Stack

- **Framework**: FastAPI (async web framework)
- **Database**: MongoDB via Motor (async driver)
- **Distributed Computing**: Ray (for actor-based isolation)
- **Authentication**: JWT tokens in HTTP-only cookies
- **Authorization**: Casbin (RBAC via pluggable provider)
- **Storage**: Backblaze B2 (optional, for exports)
- **Rate Limiting**: slowapi (in-memory, per-IP)

### Request Flow

1. **Request ID Generation** (RequestIDMiddleware) → First middleware
2. **Proxy Detection** (ProxyAwareHTTPSMiddleware) → Corrects URL scheme/host
3. **Experiment Scoping** (ExperimentScopeMiddleware) → Sets slug_id from path
4. **HTTPS Enforcement** (HTTPSEnforcementMiddleware) → HSTS, mixed content protection
5. **Route Handler** → Business logic execution
6. **Response Compression** (GZipMiddleware) → Last middleware, compresses >1KB

### Database Architecture

**MongoDB Connection Pooling**:
- Main pool: 10-50 connections (configurable via `MONGO_MAIN_MIN_POOL_SIZE`, `MONGO_MAIN_MAX_POOL_SIZE`)
- Per-experiment scoped wrappers share main pool
- Connection pool is global, but database access is scoped per experiment

**Experiment Scoping**:
- Uses `ScopedMongoWrapper` to automatically prefix collection names
- Read scopes: `[slug]` or `[slug, "shared"]`
- Write scope: `slug` (single write scope)
- Prevents cross-experiment data leakage

**Indexing**:
- Core indexes: `users.email` (unique), `experiments_config.slug` (unique)
- Export logs: `slug_id`, `created_at`, `checksum` (compound: `checksum + invalidated`)
- Per-experiment indexes managed via `AsyncAtlasIndexManager`

### Ray Integration

**Ray Initialization**:
- **Local mode**: Starts Ray cluster inside container (2 CPUs, 2GB object store)
- **Cluster mode**: Connects to external Ray cluster via `RAY_ADDRESS`
- Namespace: `"modular_labs"` (isolates actors from other applications)

**Actor Lifecycle**:
- Actors use `lifetime="detached"` for persistence
- `max_restarts=-1` (unlimited restarts)
- Actors are created during app startup, not on-demand
- Actor handles stored in `app.state.actor_handle`

**Actor Runtime Environment**:
- Working directory: Platform root directory
- B2 credentials passed via `runtime_env` if B2 enabled
- Experiment-specific dependencies can be passed per actor (via `runtime_env`)

### Security Architecture

**Authentication**:
- JWT tokens stored in HTTP-only cookies (prevents XSS token theft)
- Cookie `samesite="lax"` (allows GET from external sites, blocks POST)
- Cookie `secure` flag: Enabled in production, conditional in development
- Token expiration: 1 day (24 hours)

**Authorization**:
- Casbin RBAC model (policy file: `casbin_model.conf`)
- Authorization cache: 5-minute TTL, max 1000 entries (FIFO eviction)
- Cache prevents blocking event loop on repeated checks
- Policies checked per-request via `require_admin` dependency

**Rate Limiting**:
- In-memory storage (doesn't persist across restarts)
- Per-IP address (uses `get_remote_address`)
- Limits:
  - Login POST: 5/minute
  - Login GET: 20/minute
  - Register POST: 3/minute
  - Export: 10/minute per slug
  - File download: 30/minute

**File Upload Security**:
- Max upload size: 100MB (configurable via `MAX_UPLOAD_SIZE_MB`)
- ZIP Slip protection: Path traversal validation
- Platform file filtering: Automatically filters platform files during extraction
- Security checks: Validates paths, prevents absolute paths, checks for `..`

## Performance Characteristics

### Request Processing

**Synchronous Operations**:
- File I/O (ZIP extraction, file writes): Uses `asyncio.to_thread()` to prevent blocking
- Ray actor calls: `.remote()` calls are async, but actor methods may block
- Authorization checks: Casbin `enforce()` runs in thread pool (prevents GIL blocking)

**Async Operations**:
- MongoDB queries: Fully async via Motor
- HTTP requests: Async via httpx/aiohttp (in experiments)
- File reads: Uses `aiofiles` if available, falls back to `asyncio.to_thread()`

**Bottlenecks**:
1. **Ray Actor Initialization**: Actors start during app startup (slows startup)
2. **Authorization Cache Misses**: Casbin `enforce()` blocks event loop on cache miss
3. **File I/O**: Large ZIP uploads/extractions can block even with `to_thread()`
4. **MongoDB Connection Pool Exhaustion**: High concurrency can exhaust pool

### Scalability Considerations

**Horizontal Scaling**:
- ✅ **Stateless**: Application is stateless (JWT in cookies, no server-side sessions)
- ✅ **Database**: MongoDB supports horizontal scaling (sharding, replica sets)
- ⚠️ **Rate Limiting**: In-memory rate limiting doesn't work across instances
- ⚠️ **Ray Cluster**: Requires shared Ray cluster for actor coordination

**Vertical Scaling**:
- **Connection Pool**: Increase `MONGO_MAIN_MAX_POOL_SIZE` for more DB connections
- **Ray Resources**: Increase CPU/memory for Ray actors (local mode)
- **File Upload**: Increase `MAX_UPLOAD_SIZE_MB` if needed

**Scaling Limitations**:
- **Rate Limiting**: In-memory only (per-instance), doesn't aggregate across instances
- **Authorization Cache**: Per-instance cache (no shared cache)
- **File Uploads**: Large uploads stored in B2 or local disk (not distributed)
- **Ray Actors**: Actor handles are instance-specific (won't work across instances)

### Resource Usage

**Memory**:
- **Authorization Cache**: Max 1000 entries (minimal memory)
- **Rate Limiter**: Per-IP tracking (grows with unique IPs)
- **MongoDB Connection Pool**: ~1MB per connection (50 connections = ~50MB)
- **Ray Object Store**: 2GB in local mode (configurable)

**CPU**:
- **FastAPI Event Loop**: Single-threaded (async I/O)
- **Ray Actors**: Run in separate processes (bypasses GIL)
- **Thread Pool**: Used for blocking operations (file I/O, Casbin)

**Disk**:
- **Temporary Exports**: `temp_exports/` directory (B2 disabled)
- **Experiment Code**: `experiments/{slug}/` directories
- **Logs**: Application logs (if file-based logging configured)

## Optimization Opportunities

### High Priority

1. **Distributed Rate Limiting**:
   - Replace in-memory rate limiter with Redis-based limiter
   - Enables rate limiting across multiple instances
   - Use `slowapi` with Redis backend or custom Redis implementation

2. **Authorization Cache Sharing**:
   - Move authorization cache to Redis
   - Reduces cache misses across instances
   - Consistent authorization checks across instances

3. **Ray Actor Health Checks**:
   - Implement periodic health checks for actors
   - Auto-restart failed actors
   - Track actor availability metrics

4. **Connection Pool Monitoring**:
   - Add metrics for pool usage (active/idle connections)
   - Alert on pool exhaustion
   - Dynamic pool sizing based on load

### Medium Priority

5. **Async File Operations**:
   - Replace `asyncio.to_thread()` with truly async file I/O where possible
   - Use `aiofiles` for all file operations (ensure it's installed)

6. **ZIP Streaming**:
   - Stream ZIP extraction instead of loading entire ZIP into memory
   - Reduces memory usage for large uploads
   - Use `zipfile` with file handles instead of `BytesIO`

7. **Response Caching**:
   - Add HTTP caching headers for static assets
   - Cache experiment templates/static files
   - Use ETag/Last-Modified for conditional requests

8. **Database Query Optimization**:
   - Add query performance logging
   - Identify slow queries via MongoDB profiling
   - Optimize indexes based on query patterns

### Low Priority

9. **Background Task Queue**:
   - Move long-running tasks to background queue (Celery, RQ, etc.)
   - Export generation, actor initialization, etc.
   - Improves request response times

10. **Compression**:
    - Already using GZip for responses >1KB
    - Consider Brotli compression for better ratios
    - Pre-compress static assets

11. **CDN Integration**:
    - Serve static assets via CDN
    - Cache experiment static files in CDN
    - Reduce server load

## Monitoring & Observability

### Logging

**Request ID Tracking**:
- Every request gets unique ID via `RequestIDMiddleware`
- Request ID propagated via contextvars
- Logs include request ID for tracing

**Log Levels**:
- `DEBUG`: Detailed debugging information
- `INFO`: General application flow
- `WARNING`: Non-critical issues (default admin password, etc.)
- `ERROR`: Errors with full traceback
- `CRITICAL`: Critical failures (MongoDB connection timeout, etc.)

**Structured Logging**:
- Format: `%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s`
- Request ID filter applied to all loggers
- Safe formatter handles missing request IDs

### Metrics to Track

**Application Metrics**:
- Request rate (requests/second)
- Request latency (p50, p95, p99)
- Error rate (4xx, 5xx responses)
- Rate limit hits (429 responses)

**Database Metrics**:
- Connection pool usage (active/idle)
- Query latency (p50, p95, p99)
- Slow query count (>100ms)
- Index usage statistics

**Ray Metrics**:
- Actor count (total, active)
- Actor initialization time
- Actor method call latency
- Ray cluster resources (CPU, memory)

**Resource Metrics**:
- CPU usage
- Memory usage (RSS, heap)
- Disk usage (`temp_exports/`, `experiments/`)
- Network I/O (for B2 uploads)

### Health Checks

**Endpoints**:
- `/health`: Basic health check (MongoDB connectivity)
- `/health/detailed`: Full health check (MongoDB, Ray, B2, disk space)
- Experiment-specific health checks: `/experiments/{slug}/health`

**Health Check Components**:
- MongoDB: Ping database connection
- Ray: Check `ray.is_initialized()`
- B2: Check B2 API connectivity (if enabled)
- Disk: Check available disk space

## Database Schema

### Collections

**`users`**:
- `_id`: ObjectID
- `email`: String (unique index)
- `password_hash`: Binary (bcrypt hash)
- `is_admin`: Boolean
- `created_at`: DateTime

**`experiments_config`**:
- `_id`: ObjectID
- `slug`: String (unique index)
- `name`: String
- `description`: String
- `manifest`: Dict (experiment manifest.json)
- `runtime_s3_uri`: String (B2 presigned URL for Ray)
- `created_at`: DateTime
- `updated_at`: DateTime

**`export_logs`**:
- `_id`: ObjectID
- `slug_id`: String (indexed)
- `export_type`: String (standalone, upload-ready, docker)
- `user_email`: String
- `file_size`: Integer
- `checksum`: String (SHA256, indexed)
- `b2_filename`: String (if B2 enabled)
- `created_at`: DateTime (indexed)
- `invalidated`: Boolean (compound index with checksum)

**Per-Experiment Collections** (prefixed with `{slug}_`):
- Dynamically created by experiments
- Scoped via `ScopedMongoWrapper`
- Indexes managed per experiment

## Deployment Considerations

### Environment Variables

**Required**:
- `MONGO_URI`: MongoDB connection string
- `FLASK_SECRET_KEY`: JWT signing secret (⚠️ CHANGE FROM DEFAULT)

**Optional**:
- `G_NOME_ENV`: `production` or `development` (affects HTTPS enforcement)
- `ENABLE_REGISTRATION`: `true`/`false` (enable user registration)
- `ADMIN_EMAIL`: Default admin email (default: `admin@example.com`)
- `ADMIN_PASSWORD`: Default admin password (default: `password123` ⚠️)
- `LOG_LEVEL`: Logging level (default: `INFO`)
- `MAX_UPLOAD_SIZE_MB`: Max upload size in MB (default: 100)

**B2 Configuration**:
- `B2_APPLICATION_KEY_ID` or `B2_ACCESS_KEY_ID`
- `B2_APPLICATION_KEY` or `B2_SECRET_ACCESS_KEY`
- `B2_BUCKET_NAME`
- `B2_ENDPOINT_URL` (optional, legacy)

**Ray Configuration**:
- `RAY_ADDRESS`: Ray cluster address (if connecting to external cluster)
- If not set, starts local Ray cluster

**MongoDB Pool Configuration**:
- `MONGO_MAIN_MIN_POOL_SIZE`: Minimum pool size (default: 10)
- `MONGO_MAIN_MAX_POOL_SIZE`: Maximum pool size (default: 50)

### Production Checklist

- [ ] Change `FLASK_SECRET_KEY` from default value
- [ ] Change `ADMIN_PASSWORD` from default value
- [ ] Set `G_NOME_ENV=production`
- [ ] Configure B2 credentials (if using B2)
- [ ] Set up MongoDB replica set (if high availability needed)
- [ ] Configure Ray cluster (if using distributed Ray)
- [ ] Set up monitoring/alerting
- [ ] Configure log aggregation
- [ ] Set up backups (MongoDB, B2)
- [ ] Enable HTTPS/TLS termination
- [ ] Configure firewall/security groups
- [ ] Set up rate limiting (Redis-based if multiple instances)
- [ ] Test disaster recovery procedures

## Known Limitations

1. **Rate Limiting**: In-memory only, doesn't work across multiple instances
2. **Authorization Cache**: Per-instance, not shared across instances
3. **Ray Actors**: Instance-specific handles (can't share actors across instances)
4. **File Uploads**: Single-instance processing (no distributed upload handling)
5. **MongoDB Connection Pool**: Fixed size (doesn't auto-scale)
6. **Default Credentials**: Default admin password (`password123`) if not changed

## Performance Benchmarks

**Target Metrics** (to be measured):
- Request latency: <100ms (p95) for simple routes
- Database query latency: <50ms (p95) for indexed queries
- Ray actor initialization: <5s for standard actors
- ZIP upload/extraction: <10s for 10MB ZIP
- Export generation: <30s for 100MB export

**Load Testing**:
- Recommended tool: `locust`, `k6`, or `wrk`
- Test endpoints: `/auth/login`, `/admin/api/upload-experiment`, `/api/package-standalone/{slug}`
- Monitor: Request latency, error rate, resource usage

## Future Improvements

1. **Distributed Rate Limiting**: Redis-based rate limiter
2. **Shared Authorization Cache**: Redis cache for authz results
3. **Actor Health Monitoring**: Periodic health checks, auto-restart
4. **Connection Pool Auto-Scaling**: Dynamic pool sizing based on load
5. **ZIP Streaming**: Stream large ZIP uploads instead of loading into memory
6. **Background Task Queue**: Move long-running tasks to background queue
7. **CDN Integration**: Serve static assets via CDN
8. **Query Performance Monitoring**: Track slow queries, optimize indexes
9. **Distributed Tracing**: OpenTelemetry integration for request tracing
10. **Metrics Export**: Prometheus metrics endpoint
