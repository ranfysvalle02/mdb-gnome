# Request ID System Guide

## Overview

The request ID system provides **traceability** for all HTTP requests. Every request gets a unique ID that appears in:
- HTTP response headers (`X-Request-ID`)
- All log messages
- `request.state` (for use in route handlers)

## How It Works

### 1. Middleware Generation

**File**: `request_id_middleware.py`

```python
# For each request:
1. Generate UUID v4 (uses first 8 characters for readability)
2. Store in request.state.request_id
3. Set in contextvars for logging
4. Add to response headers as X-Request-ID
```

**Example**:
- Full UUID: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`
- Short ID (used): `a1b2c3d4`

### 2. Logging Integration

**File**: `config.py`

The request ID is automatically added to **every log message**:

```python
# Log format:
"%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s"

# Example output:
2024-01-15 10:30:45 | [a1b2c3d4] | main | INFO     | User logged in
2024-01-15 10:30:46 | [a1b2c3d4] | database | DEBUG | Query executed
```

### 3. Context Variables

The request ID is stored in a **contextvar** (`contextvars.ContextVar`), which means:
- ✅ Available throughout the request lifecycle
- ✅ Automatically propagated to async tasks
- ✅ Thread-safe (each thread/async context has its own value)
- ✅ Resets after request completes

## Where You Can See Request IDs

### 1. HTTP Response Headers

Every HTTP response includes the `X-Request-ID` header:

```bash
# Example request
curl -v http://localhost:8000/

# Response headers will include:
# X-Request-ID: a1b2c3d4
```

**Test it**:
```bash
curl -i http://localhost:8000/ | grep X-Request-ID
# Output: X-Request-ID: a1b2c3d4
```

### 2. Log Messages

All log messages include the request ID in square brackets:

```
2024-01-15 10:30:45 | [a1b2c3d4] | main | INFO     | Request started
2024-01-15 10:30:46 | [a1b2c3d4] | auth | INFO     | User logged in: user@example.com
2024-01-15 10:30:47 | [a1b2c3d4] | database | DEBUG | Query: db.users.find_one()
2024-01-15 10:30:48 | [a1b2c3d4] | main | DEBUG | Request completed: GET /
```

**Test it**:
```bash
# Make a request and check logs
curl http://localhost:8000/
# Look at application logs - you'll see [a1b2c3d4] in all log messages
```

### 3. In Route Handlers (request.state)

You can access the request ID in your route handlers:

```python
@app.get("/")
async def my_route(request: Request):
    request_id = request.state.request_id  # e.g., "a1b2c3d4"
    logger.info(f"Processing request {request_id}")
    return {"request_id": request_id}
```

### 4. In Background Tasks

If you start background tasks within a request, they inherit the request ID context:

```python
async def background_task():
    # This will have the request ID from the parent request
    logger.info("Background task running")  # Includes [a1b2c3d4]
```

## Verification

### Test 1: Check Response Headers

```bash
# Make a request and check headers
curl -i http://localhost:8000/

# Look for:
# X-Request-ID: <8-character-id>
```

**Expected output**:
```
HTTP/1.1 200 OK
content-type: text/html; charset=utf-8
X-Request-ID: a1b2c3d4
...
```

### Test 2: Check Logs

```bash
# Make a request
curl http://localhost:8000/

# Check application logs - all messages should include [request_id]
# Example:
# 2024-01-15 10:30:45 | [a1b2c3d4] | main | INFO | Request completed
```

### Test 3: Multiple Requests

```bash
# Make multiple requests - each should have a unique ID
curl -i http://localhost:8000/ | grep X-Request-ID
curl -i http://localhost:8000/ | grep X-Request-ID
curl -i http://localhost:8000/ | grep X-Request-ID

# Output:
# X-Request-ID: a1b2c3d4
# X-Request-ID: e5f6g7h8
# X-Request-ID: i9j0k1l2
```

### Test 4: Verify in Route Handler

Add this test route:

```python
@app.get("/test-request-id")
async def test_request_id(request: Request):
    return {
        "request_id": request.state.request_id,
        "from_state": request.state.request_id,
        "message": "Check the X-Request-ID header too!"
    }
```

Then test:
```bash
curl http://localhost:8000/test-request-id
# Response:
# {"request_id": "a1b2c3d4", "from_state": "a1b2c3d4", "message": "Check the X-Request-ID header too!"}

curl -i http://localhost:8000/test-request-id | grep X-Request-ID
# X-Request-ID: a1b2c3d4
```

## Troubleshooting

### Request ID Not Appearing in Logs

**Problem**: Logs show `[no-request-id]` instead of actual request ID.

**Causes**:
1. Logging happens outside request context (e.g., during startup)
2. Logging in a different thread without context propagation
3. Request ID middleware not registered

**Check**:
```python
# Verify middleware is registered
print(app.middleware_stack)  # Should include RequestIDMiddleware

# Check log format
print(logging.getLogger().handlers[0].formatter._fmt)
# Should include: [%(request_id)s]
```

### Request ID Not in Response Headers

**Problem**: `X-Request-ID` header missing from responses.

**Causes**:
1. Middleware not registered (should be first)
2. Response is being modified after middleware runs
3. Compression middleware removing headers (unlikely)

**Check**:
```python
# In main.py, verify middleware order:
app.add_middleware(RequestIDMiddleware)  # Should be FIRST
# ... other middleware
```

### Different Request IDs in Same Request

**Problem**: Logs show different request IDs for the same request.

**Causes**:
1. Context not properly propagated to async tasks
2. Multiple middleware generating IDs (only should be one)

**Check**:
- Only `RequestIDMiddleware` should generate IDs
- All logging should use the same contextvar

## Architecture Details

### Middleware Order

The request ID middleware runs **FIRST**:

```python
# Order matters:
1. RequestIDMiddleware          # First - generates ID
2. ProxyAwareHTTPSMiddleware    # Second - uses ID in logs
3. ExperimentScopeMiddleware    # Third - uses ID in logs
4. HTTPSEnforcementMiddleware   # Uses ID in logs
5. GZipMiddleware               # Last - compresses response (with X-Request-ID header)
```

### Context Variable Flow

```
Request arrives
  ↓
RequestIDMiddleware generates ID: "a1b2c3d4"
  ↓
Stores in request.state.request_id
  ↓
Sets contextvar: _request_id_context.set("a1b2c3d4")
  ↓
All logging during request uses "a1b2c3d4"
  ↓
Response includes X-Request-ID: a1b2c3d4
  ↓
Context resets after request
```

### Log Format

The log format is configured in `config.py`:

```python
fmt="%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s"
```

**Breakdown**:
- `%(asctime)s`: Timestamp
- `[%(request_id)s]`: Request ID in brackets
- `%(name)s`: Logger name (e.g., `main`, `auth`, `database`)
- `%(levelname)-8s`: Log level (INFO, DEBUG, ERROR, etc.)
- `%(message)s`: Log message

## Use Cases

### 1. Request Tracing

Trace a request through the entire application:

```bash
# Find request ID from error response
curl -i http://localhost:8000/error
# X-Request-ID: a1b2c3d4

# Search logs for that ID
grep "a1b2c3d4" /var/log/app.log

# See all operations for that request:
# 2024-01-15 10:30:45 | [a1b2c3d4] | main | INFO | Request started
# 2024-01-15 10:30:46 | [a1b2c3d4] | auth | DEBUG | Checking credentials
# 2024-01-15 10:30:47 | [a1b2c3d4] | database | DEBUG | Query executed
# 2024-01-15 10:30:48 | [a1b2c3d4] | main | ERROR | Request failed
```

### 2. Debugging

Use request ID to correlate logs with specific requests:

```python
# In route handler
@app.post("/api/upload")
async def upload(request: Request):
    request_id = request.state.request_id
    logger.info(f"[{request_id}] Starting upload")
    
    try:
        # ... upload logic
        logger.info(f"[{request_id}] Upload successful")
    except Exception as e:
        logger.error(f"[{request_id}] Upload failed: {e}", exc_info=True)
        raise
```

### 3. Monitoring

Track request patterns:

```bash
# Count requests by ID
grep "Request completed" /var/log/app.log | wc -l

# Find slow requests (add timing to logs)
grep "Request completed" /var/log/app.log | grep -v "\[a1b2c3d4\]"
```

## Configuration

### Change Request ID Format

Edit `request_id_middleware.py`:

```python
# Current: First 8 chars of UUID
request_id = str(uuid.uuid4())[:8]

# Options:
# Full UUID: str(uuid.uuid4())
# Custom format: f"{uuid.uuid4().hex[:12]}"
# Timestamp-based: f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
```

### Change Log Format

Edit `config.py`:

```python
# Current:
fmt="%(asctime)s | [%(request_id)s] | %(name)s | %(levelname)-8s | %(message)s"

# Options:
# Shorter: "%(asctime)s [%(request_id)s] %(levelname)s: %(message)s"
# JSON format: Use JSON formatter with request_id field
```

### Disable Request ID

Remove from `main.py`:

```python
# Comment out:
# app.add_middleware(RequestIDMiddleware)
```

Or make it conditional:

```python
if os.getenv("ENABLE_REQUEST_ID", "true").lower() == "true":
    app.add_middleware(RequestIDMiddleware)
```

## Summary

✅ **Request ID is automatically generated** for every request  
✅ **Available in response headers** as `X-Request-ID`  
✅ **Included in all log messages** automatically  
✅ **Accessible in route handlers** via `request.state.request_id`  
✅ **Context-aware** (propagates to async tasks)  

**Quick Verification**:
```bash
curl -i http://localhost:8000/ | grep X-Request-ID
# Should show: X-Request-ID: <8-char-id>
```

