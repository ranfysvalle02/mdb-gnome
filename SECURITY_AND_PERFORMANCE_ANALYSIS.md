# Security and Performance Analysis

This document identifies performance concerns and architectural flaws that may lead to security vulnerabilities in the codebase.

## ✅ RESOLVED ISSUES

The following issues have been addressed and resolved:

### 1. **Rate Limiting** ✅ RESOLVED

**Location**: `main.py`, `rate_limit.py`

**Status**: Implemented using `slowapi` library

**Fix Details**:
- Added rate limiting to login endpoints (5 attempts/minute for POST, 20/minute for GET)
- Added rate limiting to registration endpoints (3 attempts/minute for POST, 20/minute for GET)
- Added rate limiting to export endpoints (10 requests/minute for packaging, 30/minute for file downloads)
- Configured per-IP rate limiting with proper error handling
- Custom rate limit exceeded handler returns appropriate responses (JSON for API, HTML for web pages)

**Files Changed**: `main.py`, `rate_limit.py`, `requirements.txt`

---

### 2. **Cookie Secure Flag** ✅ RESOLVED

**Location**: `main.py`, `utils.py`

**Status**: Implemented proxy-aware secure cookie handling

**Fix Details**:
- Created `should_use_secure_cookie()` helper function that always returns `True` in production
- Properly detects HTTPS behind proxies using `request.state.detected_scheme` from middleware
- Falls back to checking proxy headers if middleware state not available
- Updated all cookie setting locations (login and registration endpoints)

**Files Changed**: `main.py`, `utils.py`

---

### 3. **Casbin Authorization Not Async** ✅ RESOLVED

**Location**: `authz_provider.py`

**Status**: Implemented thread pool execution and caching

**Fix Details**:
- Wrapped synchronous `enforce()` call with `asyncio.to_thread()` to prevent blocking event loop
- Added caching with 5-minute TTL to reduce redundant authorization checks
- Cache automatically invalidated when policies are modified
- Cache size limited to 1000 entries with FIFO eviction

**Files Changed**: `authz_provider.py`

---

### 4. **Database Query Pagination Limits** ✅ RESOLVED

**Location**: `main.py`, `export_helpers.py`

**Status**: Implemented batch processing for large queries

**Fix Details**:
- Modified `_dump_db_to_json()` to process documents in 1000-document batches
- Added progress logging for batch processing
- Maintains 10,000 document limit per collection but processes incrementally
- Prevents loading all documents into memory at once

**Files Changed**: `main.py`, `export_helpers.py`

---

### 5. **Database Connection Pool Monitoring** ✅ RESOLVED

**Location**: `mongo_connection_pool.py`, `lifespan.py`

**Status**: Added monitoring and configuration

**Fix Details**:
- Made pool sizes configurable via environment variables (`MONGO_MAIN_MAX_POOL_SIZE`, `MONGO_MAIN_MIN_POOL_SIZE`, `MONGO_ACTOR_MAX_POOL_SIZE`, `MONGO_ACTOR_MIN_POOL_SIZE`)
- Added `get_pool_metrics()` function to monitor connection pool usage
- Added automatic warnings when pool usage exceeds 80%
- Enhanced logging to show pool configuration on initialization

**Files Changed**: `mongo_connection_pool.py`, `lifespan.py`

---

### 6. **Large File Upload Without Size Limits** ✅ RESOLVED

**Location**: `main.py`

**Status**: Implemented size limits and chunked reading

**Fix Details**:
- Added configurable file size limit (default: 100MB via `MAX_UPLOAD_SIZE_MB` environment variable)
- Validates file size before reading into memory
- Reads files in 1MB chunks to prevent memory exhaustion
- Validates size incrementally during upload
- Returns HTTP 413 (Payload Too Large) for oversized files
- Applied to both `/api/detect-slug` and `/api/upload-experiment` endpoints

**Files Changed**: `main.py`

---

### 7. **Application-Level Cache Without Size Limit** ✅ RESOLVED

**Location**: `core_deps.py`

**Status**: Implemented LRU cache with max size

**Fix Details**:
- Replaced unbounded `Dict` with `OrderedDict` for LRU eviction
- Added configurable max cache size (default: 100 entries via `EXPERIMENT_CONFIG_CACHE_MAX_SIZE`)
- Implements LRU eviction: moves accessed entries to end, evicts oldest when limit exceeded
- Added `get_cache_metrics()` function to monitor cache usage
- Automatic warnings when cache usage exceeds 80%
- Enhanced logging shows cache size and eviction events

**Files Changed**: `core_deps.py`

---

### 8. **Export Checksum Calculation Blocks Event Loop** ✅ RESOLVED

**Location**: `export_helpers.py:118-132`

**Status**: Implemented thread pool execution

**Fix Details**:
- Made `calculate_export_checksum()` async function
- Wrapped synchronous hash calculation with `asyncio.to_thread()`
- Uses streaming hash calculation for file paths (1MB chunks)
- Prevents blocking event loop during CPU-intensive operations
- Applied to all checksum calculations in export endpoints

**Files Changed**: `export_helpers.py`, `main.py`

---

### 9. **N+1 Query Problem** ✅ RESOLVED

**Location**: `core_deps.py`, various endpoints

**Status**: Implemented batch loading

**Fix Details**:
- Added `get_experiment_configs_batch()` function for efficient batch loading
- Uses MongoDB `$in` operator to fetch multiple configs in one query
- Batch loading already implemented in `reload_active_experiments`
- Caching prevents redundant queries across requests

**Files Changed**: `core_deps.py`

---

### 10. **No Request ID/Tracing** ✅ RESOLVED

**Location**: Application-wide

**Status**: Implemented request ID middleware and logging

**Fix Details**:
- Created `RequestIDMiddleware` that generates UUID for each request
- Adds `X-Request-ID` header to all responses
- Stores request ID in `request.state` for use in handlers
- Configures logging to include request ID in all log records via contextvars
- Enables request tracing and log correlation

**Additional Fix - Logging Error Prevention**:
- Fixed `KeyError: 'request_id'` errors in logs without HTTP requests (background tasks, lifespan events)
- Added `SafeRequestIDFormatter` that ensures `request_id` attribute exists before formatting
- Enhanced `RequestIDLoggingFilter` to always set `request_id` attribute (defaults to `"no-request-id"` when not in request context)
- Applied filter and formatter to all handlers immediately after `basicConfig`
- Patched `Logger.addHandler` to automatically apply filter to newly created handlers
- Ensures all logs have `request_id` attribute, preventing format string errors

**Files Changed**: `request_id_middleware.py`, `main.py`, `config.py`

---

## 🔴 CRITICAL SECURITY ISSUES

### 1. **Default Insecure Secret Keys**

**Location**: `config.py`, `core_deps.py`

**Issue**: 
- Hardcoded default secret keys if environment variables are not set
- In `config.py`: `SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a_very_insecure_default_dev_secret_123!")`
- In `core_deps.py`: `SECRET_KEY = "a_very_bad_dev_secret_key_12345"` if not set

**Impact**: 
- JWT tokens can be forged if default key is used
- Authentication can be bypassed
- Session hijacking possible

**Recommendation**:
- Fail fast on startup if `SECRET_KEY` is not set
- Never use defaults in production
- Use proper secret management (e.g., AWS Secrets Manager, HashiCorp Vault)

### 2. **Default Admin Credentials**

**Location**: `config.py`, `database.py`

**Issue**:
- Default admin password `"password123"` if not set
- Admin user created with default credentials on first startup

**Impact**:
- Unauthorized admin access if defaults are not changed
- Initial deployment vulnerability

**Recommendation**:
- Require admin password to be set explicitly
- Force password change on first login
- Implement password strength requirements

### 3. **No Rate Limiting** ✅ RESOLVED

**Location**: Authentication endpoints (`main.py`)

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- No rate limiting on login, registration, or export endpoints
- Brute force attacks possible
- DDoS vulnerability

**Fix Applied**:
- Implemented rate limiting using `slowapi` library
- Per-IP limits for all vulnerable endpoints
- Custom error handling for rate limit exceeded

**Files Changed**: `main.py`, `rate_limit.py`, `requirements.txt`

### 4. **JWT Token Expiration Too Long**

**Location**: `main.py:449`

**Issue**:
```python
"exp": datetime.datetime.utcnow() + datetime.timedelta(days=1)
```

**Impact**:
- 24-hour token lifetime is too long
- Stolen tokens remain valid for extended period
- No refresh token mechanism

**Recommendation**:
- Reduce token expiration (e.g., 15-30 minutes)
- Implement refresh tokens for longer sessions
- Add token revocation capability

### 5. **No Input Validation on Critical Endpoints**

**Location**: `main.py` (various endpoints)

**Issues**:
- Slug IDs not validated (could contain path traversal characters)
- Email addresses not validated (no format check)
- File uploads have some validation but could be stronger

**Examples**:
```python
# Slug extracted directly from path without validation
slug_id: str  # No regex validation, could contain "../../etc"

# Email from form without format validation
email = form_data.get("email")  # Could be malicious string
```

**Recommendation**:
- Validate slug format with regex (e.g., `^[a-z0-9_-]+$`)
- Validate email format with proper regex or library
- Use Pydantic models for request validation
- Sanitize all user inputs

### 6. **Zip Slip Vulnerability (Partially Mitigated)**

**Location**: `main.py:2586-2602`

**Issue**:
- Path traversal protection exists but has gaps
- Platform files are allowed through (lines 2596-2597)
- Complex logic that could be bypassed

**Impact**:
- Potential file system access outside experiment directory
- Code injection if malicious files extracted

**Recommendation**:
- Whitelist-only approach (extract only known experiment files)
- More robust path normalization
- Consider using `zipfile` with `extractall` restrictions

### 7. **Casbin Authorization Not Async** ✅ RESOLVED

**Location**: `authz_provider.py:65`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
```python
result = self._enforcer.enforce(subject, resource, action)  # Synchronous!
```

**Fix Applied**:
- Wrapped synchronous call with `asyncio.to_thread()` to prevent blocking
- Added caching with TTL to reduce redundant checks
- Automatic cache invalidation on policy changes

**Files Changed**: `authz_provider.py`

### 8. **No CORS Configuration**

**Location**: Application-wide

**Issue**:
- No explicit CORS middleware configuration found
- Could allow unauthorized cross-origin requests

**Impact**:
- CSRF attacks possible
- Data leakage to malicious sites

**Recommendation**:
- Explicitly configure CORS with allowed origins
- Use `CORSMiddleware` from FastAPI
- Set appropriate CORS headers

### 9. **Missing HTTPS Enforcement in Some Cases** ✅ RESOLVED

**Location**: `main.py:462`, `middleware.py`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- Cookie `secure` flag only set if `request.url.scheme == "https"`
- May not work correctly behind proxies without middleware
- Middleware exists but may not cover all cases

**Fix Applied**:
- Created `should_use_secure_cookie()` helper that always returns `True` in production
- Properly detects HTTPS behind proxies using middleware state
- Updated all cookie setting locations

**Files Changed**: `main.py`, `utils.py`

### 10. **Password Storage Verification**

**Location**: `database.py:66`, `main.py:443`

**Issue**:
- Uses `bcrypt` correctly but no verification of hash format
- No check for weak hashes (e.g., MD5, SHA1)

**Impact**:
- If database is compromised, weak passwords easier to crack
- Migration from weak hashing not detected

**Recommendation**:
- Verify password hash algorithm on login
- Re-hash weak passwords on successful authentication
- Add hash format validation

## 🟡 HIGH PRIORITY PERFORMANCE ISSUES

### 1. **No Database Query Pagination Limits** ✅ RESOLVED

**Location**: `export_helpers.py:287`, `main.py:2903`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
```python
cursor = db[coll_name].find().limit(10000)  # Hard limit, but no pagination
active_cfgs = await db.experiments_config.find({"status": "active"}).limit(500)
```

**Fix Applied**:
- Implemented batch processing (1000 documents per batch)
- Processes documents incrementally to manage memory
- Added progress logging for large exports

**Files Changed**: `main.py`, `export_helpers.py`

### 2. **No Database Connection Pool Limits Check** ✅ RESOLVED

**Location**: `mongo_connection_pool.py:37`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- Default `max_pool_size=10` for actors
- Main application pool size not clearly visible
- No monitoring of pool exhaustion

**Fix Applied**:
- Made pool sizes configurable via environment variables
- Added `get_pool_metrics()` function for monitoring
- Automatic warnings when pool usage exceeds 80%
- Enhanced logging shows pool configuration

**Files Changed**: `mongo_connection_pool.py`, `lifespan.py`

### 3. **Synchronous Operations in Async Context**

**Location**: Multiple files

**Issues**:
- `zipfile` operations are synchronous (blocking event loop)
- File I/O operations mixed sync/async
- `shutil.rmtree()` blocks event loop

**Examples**:
```python
# main.py:2667 - Blocks event loop
shutil.rmtree(experiment_path)

# main.py:2751 - Blocks event loop  
target_path.write_bytes(content)
```

**Impact**:
- Event loop blocked during file operations
- Reduced concurrency
- Slow response times under load

**Recommendation**:
- Use `asyncio.to_thread()` for blocking operations
- Use `aiofiles` for async file I/O
- Offload heavy operations to background tasks

### 4. **No Request Timeout Configuration**

**Location**: Application-wide

**Issue**:
- No explicit request timeouts
- Long-running exports can tie up connections
- No timeout for database operations in some cases

**Impact**:
- Resource exhaustion from hanging requests
- Poor user experience (no feedback)
- Connection pool starvation

**Recommendation**:
- Set request timeouts (e.g., 30 seconds for API, 5 minutes for exports)
- Use `asyncio.wait_for()` for database operations
- Implement progress tracking for long operations

### 5. **Large File Upload Without Size Limits** ✅ RESOLVED

**Location**: `main.py:2542`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- ZIP file uploads read entirely into memory
- No size limit validation before processing
- Large files can cause memory exhaustion

**Example**:
```python
zip_data = await file.read()  # Entire file in memory
```

**Fix Applied**:
- Implemented configurable file size limit (default: 100MB)
- Validates size before and during upload
- Reads files in 1MB chunks to prevent memory exhaustion
- Returns HTTP 413 for oversized files

**Files Changed**: `main.py`

### 6. **Application-Level Cache Without Size Limit** ✅ RESOLVED

**Location**: `core_deps.py:315`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
```python
_experiment_config_app_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}
```
- No maximum size limit
- Cache can grow unbounded
- No eviction policy except TTL

**Fix Applied**:
- Implemented LRU cache using `OrderedDict`
- Added configurable max cache size (default: 100 entries)
- Automatic eviction of oldest entries when limit exceeded
- Added `get_cache_metrics()` function for monitoring

**Files Changed**: `core_deps.py`

### 7. **N+1 Query Problem Potential** ✅ RESOLVED

**Location**: Various endpoints

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- Experiment registration loads configs individually
- Background tasks may query multiple times
- No batch loading where possible

**Fix Applied**:
- Added `get_experiment_configs_batch()` function for batch loading
- Uses MongoDB `$in` operator to fetch multiple configs in one query
- Existing batch loading in `reload_active_experiments` already implemented
- Caching already in place to prevent redundant queries

**Files Changed**: `core_deps.py`

---

### 8. **Export Checksum Calculation Blocks Event Loop** ✅ RESOLVED

**Location**: `export_helpers.py:118-132`

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
```python
zip_data = zip_source.getvalue()  # Can be very large
checksum = hashlib.sha256(zip_data).hexdigest()  # CPU-intensive, blocks
```

**Fix Applied**:
- Made `calculate_export_checksum()` async
- Runs checksum calculation in thread pool using `asyncio.to_thread()`
- Uses streaming hash calculation for file paths (1MB chunks)
- Prevents blocking event loop during CPU-intensive hash calculation

**Files Changed**: `export_helpers.py`, `main.py`

---

## 🟢 MEDIUM PRIORITY ISSUES

### 1. **No Request ID/Tracing** ✅ RESOLVED

**Location**: Application-wide

**Status**: ✅ Fixed - See "RESOLVED ISSUES" section above

**Original Issue**:
- No request IDs for tracing
- Difficult to debug issues in production
- No correlation between logs

**Fix Applied**:
- Added `RequestIDMiddleware` that generates UUID for each request
- Stores request ID in `request.state` for use in handlers
- Adds `X-Request-ID` header to all responses
- Configures logging to include request ID in all log records
- Uses contextvars to make request ID available throughout request lifecycle

**Files Changed**: `request_id_middleware.py`, `main.py`, `config.py`

### 2. **Error Messages May Leak Information**

**Location**: Various endpoints

**Issue**:
- Some error messages may reveal internal structure
- Stack traces exposed to users in some cases

**Recommendation**:
- Sanitize error messages in production
- Return generic errors to users
- Log detailed errors server-side only

### 3. **No CSRF Protection**

**Location**: Forms, POST endpoints

**Issue**:
- No CSRF tokens for form submissions
- Vulnerable to CSRF attacks

**Recommendation**:
- Implement CSRF token validation
- Use `csrfprotect` middleware or similar
- Add SameSite cookie attribute (already partially done)

### 4. **No Audit Logging**

**Location**: Critical operations

**Issue**:
- Admin actions not logged
- No audit trail for security events
- Difficult to investigate breaches

**Recommendation**:
- Log all admin actions
- Log authentication events (success/failure)
- Log data access/modification
- Store audit logs separately

### 5. **Experiment Code Execution Without Sandboxing**

**Location**: `main.py:2940-3044`

**Issue**:
- User-uploaded Python code executed in Ray actors
- No code review or validation
- Potential for malicious code execution [another opportunity to flex your hacker skills]

**Impact**:
- Arbitrary code execution
- Data exfiltration
- System compromise

**Recommendation**:
- Review uploaded code before execution
- Run in more restrictive environment (e.g., containers)
- Limit actor permissions
- Monitor actor behavior

### 6. **No Request Body Size Limits**

**Location**: FastAPI configuration

**Issue**:
- No explicit body size limits
- Large requests can cause memory issues

**Recommendation**:
- Configure FastAPI body size limits
- Set appropriate limits per endpoint type

### 7. **Password Hash Comparison Timing Attack**

**Location**: `main.py:443`

**Issue**:
- Uses `bcrypt.checkpw()` which is designed to be constant-time
- However, early return on user not found creates timing difference

**Impact**:
- Account enumeration via timing attacks
- Reveals which emails are registered

**Recommendation**:
- Always perform hash comparison (even for non-existent users)
- Use dummy hash comparison for invalid emails
- Add random delay to prevent timing analysis

## 📊 PERFORMANCE METRICS RECOMMENDATIONS

### Missing Monitoring

1. **Database Query Performance**
   - No slow query logging
   - No query time metrics
   - No connection pool metrics

2. **Request Metrics**
   - No request duration tracking
   - No error rate monitoring
   - No throughput metrics

3. **Resource Usage**
   - No memory usage monitoring
   - No CPU usage tracking
   - No disk I/O metrics

**Recommendation**:
- Add APM tooling (e.g., New Relic, Datadog, Prometheus)
- Implement health check endpoints with metrics
- Set up alerting for critical metrics

## 🔧 ARCHITECTURAL CONCERNS

### 1. **Inconsistent Error Handling**

**Issue**:
- Some endpoints return different error formats
- Some catch generic `Exception`, others catch specific
- Error logging inconsistent

**Recommendation**:
- Standardize error response format
- Use exception handlers middleware
- Consistent error logging strategy

### 2. **Background Tasks Not Monitored**

**Location**: `background_tasks.py`, `main.py:2877`

**Issue**:
- Background tasks created with `asyncio.create_task()`
- No tracking of task completion
- No retry logic for failed tasks
- Tasks can silently fail

**Recommendation**:
- Use task manager with tracking
- Implement retry logic
- Add task monitoring/logging
- Alert on task failures

### 3. **Configuration Scattered**

**Issue**:
- Configuration in multiple places
- Environment variables not validated on startup
- Default values can mask misconfigurations

**Recommendation**:
- Centralize configuration
- Validate all required env vars on startup
- Fail fast on missing critical config

### 4. **No Database Migration System**

**Issue**:
- Schema changes not tracked
- No versioning system
- Manual migration process

**Recommendation**:
- Implement migration system (e.g., Alembic, migrate-mongo)
- Version database schema
- Document migration process

## ✅ GOOD PRACTICES FOUND

1. ✅ Path traversal protection in file operations
2. ✅ HTTPS enforcement middleware
3. ✅ Password hashing with bcrypt
4. ✅ JWT tokens with expiration
5. ✅ Authorization checks (Casbin)
6. ✅ Input sanitization in some places (URL validation)
7. ✅ Database connection pooling
8. ✅ Async/await pattern used throughout
9. ✅ Caching for experiment configs
10. ✅ Background task manager

## 📝 PRIORITY RECOMMENDATIONS

### Immediate (Critical Security)
1. Remove default secret keys - fail if not set
2. Remove default admin password - require explicit setting
3. Add rate limiting to auth endpoints
4. Reduce JWT expiration time
5. Add input validation (slugs, emails)

### Short Term (High Priority)
1. Implement request timeouts
2. Add file size limits for uploads
3. Fix synchronous operations in async context
4. Add connection pool monitoring
5. Implement pagination for large queries

### Medium Term
1. Add CORS configuration
2. Implement CSRF protection
3. Add audit logging
4. Improve error handling consistency
5. Add monitoring/metrics

### Long Term
1. Implement database migrations
2. Add request tracing
3. Improve background task monitoring
4. Centralize configuration
5. Add comprehensive tests for security

## 🔍 TESTING RECOMMENDATIONS

1. **Security Testing**
   - OWASP Top 10 vulnerability scanning
   - Penetration testing
   - Dependency vulnerability scanning (e.g., Snyk, Dependabot)

2. **Performance Testing**
   - Load testing (e.g., Locust, k6)
   - Stress testing
   - Connection pool exhaustion testing

3. **Integration Testing**
   - Authentication flow tests
   - Authorization tests
   - File upload/download tests
   - Export functionality tests

