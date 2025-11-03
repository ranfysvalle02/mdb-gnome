# Security Analysis and Vulnerabilities

This document provides a comprehensive security analysis of the mdb-gnome platform, including identified vulnerabilities, fixes implemented, and remaining security considerations.

## Table of Contents

1. [Fixed Vulnerabilities](#fixed-vulnerabilities)
2. [Known Security Issues](#known-security-issues)
3. [Security Best Practices](#security-best-practices)
4. [Security Utilities](#security-utilities)
5. [Remediation Roadmap](#remediation-roadmap)

## Fixed Vulnerabilities

### 1. ✅ NoSQL Injection in Export File Query (FIXED)

**Severity**: High  
**Location**: `main.py:1908-1917`  
**Issue**: User-provided filename was used directly in MongoDB regex query without escaping.

**Original Code**:
```python
export_log = await db.export_logs.find_one({
  "$or": [
    {"local_file_path": {"$regex": safe_filename}},
    {"b2_file_name": {"$regex": safe_filename}}
  ]
})
```

**Fix**: Escaped regex special characters using `sanitize_for_regex()` utility function.

**Fixed Code**:
```python
from utils import sanitize_for_regex
escaped_filename = sanitize_for_regex(safe_filename)
export_log = await db.export_logs.find_one({
  "$or": [
    {"local_file_path": {"$regex": f"^{escaped_filename}$"}},
    {"b2_file_name": {"$regex": f"^{escaped_filename}$"}}
  ]
})
```

**Status**: ✅ Fixed

---

### 2. ✅ Missing ObjectId Validation Utilities (FIXED)

**Severity**: Medium  
**Location**: Multiple experiment files  
**Issue**: User-provided ObjectId strings were not consistently validated before use in queries.

**Fix**: Created security utilities in `utils.py`:
- `validate_objectid()`: Validates ObjectId format before conversion
- `safe_objectid()`: Validates and converts ObjectId, raising HTTPException on failure

**Usage**:
```python
from utils import safe_objectid

# Safe ObjectId conversion
try:
    project_id = safe_objectid(project_id_str, "project_id")
except HTTPException:
    # Invalid format - automatically returns 400
    pass
```

**Status**: ✅ Fixed (utilities available, experiments should adopt)

---

### 3. ✅ Regex Injection Vulnerability (FIXED)

**Severity**: Medium  
**Location**: `main.py:1910`  
**Issue**: User input used in regex patterns without escaping.

**Fix**: Added `sanitize_for_regex()` utility function that escapes all regex special characters.

**Status**: ✅ Fixed

---

### 4. ✅ Path Traversal Protection (VERIFIED)

**Severity**: High  
**Location**: Multiple file operations  
**Issue**: Potential path traversal in file operations.

**Status**: ✅ Protected
- `secure_path()` function in `utils.py` validates and sanitizes paths
- Export file serving uses `Path(filename).name` to extract basename
- ZIP extraction validates paths for traversal attempts

---

### 5. ✅ JWT Algorithm Verification (VERIFIED)

**Severity**: Critical  
**Location**: `core_deps.py`, `sub_auth.py`, `main.py`  
**Issue**: JWT tokens must specify algorithms to prevent "none" algorithm attacks.

**Status**: ✅ Protected
- All JWT decode operations explicitly specify `algorithms=["HS256"]`
- No JWT operations allow "none" algorithm
- SECRET_KEY validation prevents use of default keys in production

---

## Known Security Issues

### 1. ⚠️ CSRF Protection Not Implemented

**Severity**: High  
**Location**: All state-changing endpoints (POST, PUT, DELETE)

**Issue**: No CSRF token protection on state-changing endpoints. Attackers can perform actions on behalf of authenticated users via malicious websites.

**Affected Endpoints**:
- `/admin/api/upload-experiment` (POST)
- `/admin/api/save-manifest` (POST)
- `/auth/login` (POST)
- `/auth/register` (POST)
- All experiment endpoints that modify data

**Current Mitigation**:
- `SameSite=Lax` cookies provide some protection against cross-site requests
- JWT tokens in HTTP-only cookies (prevents XSS-based CSRF in some cases)

**Recommended Fix**:
```python
# Add CSRF token middleware
from fastapi.middleware.csrf import CSRFMiddleware

app.add_middleware(
    CSRFMiddleware,
    secret=SECRET_KEY,
    cookie_name="csrf_token",
    cookie_httponly=True,
    header_name="X-CSRF-Token"
)

# Or implement custom CSRF protection
# 1. Generate CSRF tokens on GET requests
# 2. Validate CSRF tokens on POST/PUT/DELETE requests
# 3. Use double-submit cookie pattern
```

**Risk**: High - Allows attackers to perform actions as authenticated users

**Priority**: High

---

### 2. ⚠️ Rate Limiting Can Be Bypassed via IP Rotation

**Severity**: Medium  
**Location**: Rate limiting implementation

**Issue**: In-memory rate limiting can be bypassed by rotating IP addresses (proxy, VPN, botnet).

**Current Implementation**:
- Rate limiting uses IP address as key
- Stored in memory (per-process)
- Can be reset by restarting application

**Recommended Fix**:
- Move to Redis-based rate limiting
- Use user ID + IP address for authenticated requests
- Implement progressive delays
- Add CAPTCHA after multiple failures
- Use distributed rate limiting for horizontal scaling

**Risk**: Medium - Allows brute force attacks and resource exhaustion

**Priority**: Medium

---

### 3. ⚠️ Missing Security Headers

**Severity**: Medium  
**Location**: HTTP response headers

**Issue**: Missing security headers to prevent common attacks:
- `X-Frame-Options`: Prevents clickjacking
- `X-Content-Type-Options`: Prevents MIME sniffing
- `Content-Security-Policy`: Prevents XSS attacks
- `Strict-Transport-Security`: Enforces HTTPS
- `X-XSS-Protection`: Legacy XSS protection

**Recommended Fix**:
```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
```

**Risk**: Medium - Increases attack surface

**Priority**: Medium

---

### 4. ⚠️ Default Credentials Warning

**Severity**: Critical (if not changed)  
**Location**: `config.py`

**Issue**: Default admin credentials and SECRET_KEY are logged as warnings but still usable.

**Default Values**:
- `ADMIN_EMAIL`: `"admin@example.com"`
- `ADMIN_PASSWORD`: `"password123"`
- `FLASK_SECRET_KEY`: `"a_very_insecure_default_dev_secret_123!"`

**Current Protection**:
- Warnings logged on startup
- SECRET_KEY validation prevents use of default in production (if `G_NOME_ENV=production`)

**Recommended Fix**:
- **Require** SECRET_KEY to be set in production (fail startup if default)
- **Require** admin password to be changed on first login
- Add startup check that fails if defaults are detected in production

**Risk**: Critical in production if defaults are not changed

**Priority**: High

---

### 5. ⚠️ Information Disclosure in Error Messages

**Severity**: Low-Medium  
**Location**: Error handling throughout application

**Issue**: Error messages may leak sensitive information:
- File paths
- Database structure
- Stack traces (in development mode)
- Internal error details

**Current Protection**:
- Production error handling (may hide stack traces)
- Generic error messages for authentication failures

**Recommended Fix**:
- Sanitize all error messages before returning to client
- Log detailed errors server-side only
- Use error IDs that map to detailed logs
- Never expose file paths, database names, or internal structure

**Risk**: Low-Medium - Information disclosure aids attackers

**Priority**: Low

---

### 6. ⚠️ Session Fixation Vulnerability

**Severity**: Low  
**Location**: Authentication flow

**Issue**: Old JWT tokens are not explicitly invalidated on logout or password change.

**Current Behavior**:
- JWT tokens expire after 24 hours
- No token revocation mechanism
- Tokens cannot be invalidated before expiration

**Recommended Fix**:
- Implement token blacklist (Redis-based)
- Invalidate tokens on logout
- Invalidate tokens on password change
- Add refresh token mechanism with short-lived access tokens

**Risk**: Low - Token expiration limits impact

**Priority**: Low

---

### 7. ⚠️ Open Redirect Vulnerability (Partially Mitigated)

**Severity**: Medium  
**Location**: `core_deps.py:_validate_next_url()`

**Issue**: "next" URL parameter could be used for open redirect attacks.

**Current Protection**:
- `_validate_next_url()` function sanitizes redirect URLs
- Only allows relative paths starting with "/"
- Blocks URLs containing "//" or ":"

**Remaining Risk**:
- JavaScript URLs may still be possible in some contexts
- Relative URLs can still redirect to different paths

**Recommended Fix**:
- Maintain current validation (good)
- Add allowlist of allowed redirect paths
- Use absolute URLs with domain validation
- Consider removing redirect functionality entirely for sensitive operations

**Risk**: Low-Medium - Partially mitigated

**Priority**: Low

---

## Security Best Practices

### Input Validation

**Always validate user input**:
- Use `validate_objectid()` for MongoDB ObjectId strings
- Use `sanitize_for_regex()` for regex patterns
- Use `sanitize_filename()` for file operations
- Use `secure_path()` for file paths

**Example**:
```python
from utils import safe_objectid, sanitize_for_regex

# Validate ObjectId
project_id = safe_objectid(project_id_str, "project_id")

# Sanitize for regex
safe_pattern = sanitize_for_regex(user_input)
```

### NoSQL Injection Prevention

**Always use parameterized queries**:
- Never concatenate user input into queries
- Escape regex patterns using `sanitize_for_regex()`
- Validate ObjectId format before use
- Use MongoDB operators safely (avoid `$where`, `$function`)

**Example**:
```python
# ✅ GOOD: Parameterized query
query = {"email": email, "store_id": ObjectId(store_id)}

# ❌ BAD: String concatenation
query = {"email": f"{user_input}"}
```

### Authentication Best Practices

**JWT Security**:
- Always specify `algorithms` parameter explicitly
- Never use `None` algorithm
- Use strong SECRET_KEY (minimum 32 bytes)
- Set appropriate expiration times
- Use HTTP-only cookies for token storage

**Password Security**:
- Always hash passwords with bcrypt in production
- Never store plain text passwords
- Use strong password policies
- Implement password reset securely

### File Operations

**Always validate file paths**:
- Use `secure_path()` for path resolution
- Use `sanitize_filename()` for filenames
- Validate file types and sizes
- Check file extensions
- Limit file upload sizes

**Example**:
```python
from utils import secure_path, sanitize_filename

# Secure path resolution
file_path = secure_path(base_dir, user_path)

# Sanitize filename
safe_name = sanitize_filename(user_filename)
```

## Security Utilities

The following security utilities are available in `utils.py`:

### `validate_objectid(oid_string, field_name="id")`
Validates MongoDB ObjectId format before conversion.

**Returns**: `(is_valid: bool, error_message: Optional[str])`

### `safe_objectid(oid_string, field_name="id")`
Validates and converts ObjectId, raising HTTPException on failure.

**Returns**: `ObjectId` instance

**Raises**: `HTTPException(400)` if invalid

### `sanitize_for_regex(text)`
Escapes regex special characters to prevent regex injection.

**Returns**: Escaped string safe for regex patterns

### `sanitize_filename(filename)`
Sanitizes filename to prevent path traversal.

**Returns**: Basename only (path components removed)

**Raises**: `ValueError` if invalid after sanitization

### `secure_path(base_dir, relative_path_str)`
Securely resolves relative paths, preventing directory traversal.

**Returns**: Absolute Path within base_dir

**Raises**: `HTTPException` if traversal detected

## Remediation Roadmap

### Immediate (High Priority)

1. **Implement CSRF Protection**
   - Add CSRF middleware or custom implementation
   - Validate CSRF tokens on all state-changing endpoints
   - Use double-submit cookie pattern

2. **Enforce Strong SECRET_KEY in Production**
   - Fail startup if default SECRET_KEY detected
   - Require minimum length (32 bytes)
   - Validate key strength

3. **Add Security Headers**
   - Implement security headers middleware
   - Add X-Frame-Options, CSP, HSTS headers
   - Configure appropriately for production

### Short Term (Medium Priority)

4. **Improve Rate Limiting**
   - Move to Redis-based rate limiting
   - Add progressive delays
   - Implement CAPTCHA after failures

5. **Enhanced Input Validation**
   - Adopt `safe_objectid()` throughout codebase
   - Add input validation decorators
   - Validate all user-provided data

6. **Session Management**
   - Implement token blacklist
   - Add logout token invalidation
   - Consider refresh token pattern

### Long Term (Low Priority)

7. **Security Audit Logging**
   - Log all authentication attempts
   - Log all admin actions
   - Implement security event monitoring

8. **Penetration Testing**
   - Regular security audits
   - Automated vulnerability scanning
   - Bug bounty program

9. **Security Documentation**
   - Security architecture documentation
   - Threat modeling
   - Incident response procedures

## Security Checklist

### Pre-Production Checklist

- [ ] Change default SECRET_KEY
- [ ] Change default admin credentials
- [ ] Enable CSRF protection
- [ ] Add security headers
- [ ] Configure rate limiting
- [ ] Enable HTTPS only
- [ ] Disable debug mode
- [ ] Configure error handling (hide details)
- [ ] Review and test input validation
- [ ] Security audit of all endpoints
- [ ] Penetration testing
- [ ] Security headers audit

### Ongoing Security Maintenance

- [ ] Regular dependency updates
- [ ] Security patch monitoring
- [ ] Log analysis for attacks
- [ ] Regular security audits
- [ ] User access review
- [ ] Secret rotation
- [ ] Backup security

## Reporting Security Issues

If you discover a security vulnerability, please report it responsibly:

1. **DO NOT** open a public issue
2. Email security details to maintainers
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if available)

## References

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [FastAPI Security](https://fastapi.tiangolo.com/advanced/security/)
- [MongoDB Security](https://www.mongodb.com/docs/manual/security/)
- [JWT Best Practices](https://datatracker.ietf.org/doc/html/rfc8725)

---

**Last Updated**: 2024  
**Security Contact**: See repository maintainers

