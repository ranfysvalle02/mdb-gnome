# Store Factory Sub-Authentication Integration

This document explains how Store Factory uses sub-authentication for its demo users and store management.

## Overview

Store Factory uses **sub-authentication** to manage demo store owners and store-specific user accounts independently from platform-level authentication. This allows each demo store to have its own owner credentials that work exclusively within Store Factory.

## Configuration

**manifest.json:**
```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "session_cookie_name": "store_factory_session",
    "session_ttl_seconds": 86400,
    "allow_registration": false
  }
}
```

## Demo User Seeding

When Store Factory initializes (via `actor.initialize()`), it seeds demo stores with owner users:

**Demo Stores:**
- `country-pizza` → `owner@country-pizza.com` / `password123`
- `premium-auto-sales` → `owner@premiumauto.com` / `demo123`
- `quick-fix-auto` → `owner@quickfix.com` / `demo123`
- `pro-services-hub` → `owner@proservices.com` / `demo123`
- `general-store-demo` → `owner@generalstore.com` / `demo123`

**Seeding Process:**
1. Actor checks if demo stores already exist
2. For each demo store, creates:
   - Store document with all configuration
   - Owner user in `users` collection (sub-auth compatible)
   - Demo items (menu items, vehicles, services, etc.)
   - Specials/announcements

**User Creation:**
- Users are created with plain text passwords (for demo)
- Sub-auth's `authenticate_experiment_user()` supports both plain text and bcrypt hashed passwords
- User structure: `{email, password, role: "owner", store_id, date_created}`

## Authentication Flow

### 1. Login Process

**Route:** `POST /experiments/store_factory/{store_slug}/admin/login`

**Flow:**
1. User submits email/password for specific store
2. `actor.admin_login()` authenticates against experiment's `users` collection
3. Checks password (supports plain text for demo, bcrypt for production)
4. Returns `user_id` if successful
5. `admin_login_post` route creates sub-auth session using `create_experiment_session()`
6. Session cookie is set: `store_factory_session_store_factory`
7. User is redirected to admin dashboard

**Code:**
```python
result = await actor.admin_login.remote(store_slug, email, password)
if result.get("success"):
    # Create sub-auth session
    user_id = result.get("user_id")
    await create_experiment_session(request, slug_id, user_id, config, response)
    return response  # Redirect with session cookie
```

### 2. Protected Routes

**Route:** `GET /experiments/store_factory/{store_slug}/admin/dashboard`

**Flow:**
1. Route uses `get_experiment_user_from_session` dependency
2. Dependency checks for sub-auth session cookie
3. Validates session token and fetches user from `users` collection
4. If no session, redirects to login
5. If session valid, proceeds with dashboard

**Code:**
```python
async def admin_dashboard(
    request: Request,
    store_slug: str,
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    if not user:
        return RedirectResponse(url=f".../admin/login")
    # User is authenticated, show dashboard
```

## User Management

### Store-Specific Users

Each store has its own set of users in the `store_factory_users` collection:
- Users are scoped by `store_id` (multi-store scenario)
- Each store can have multiple users
- Users have `role` field (`owner`, `manager`, `staff`, etc.)

### Authentication Methods

**Current Implementation:**
- Plain text passwords for demo users (compatible with sub-auth)
- bcrypt hashing supported (can be enabled for production)
- Password checking in `actor.admin_login()` matches sub-auth logic

**Future Enhancement:**
- Can optionally use `sub_auth.create_experiment_user()` for new user creation
- Can use `sub_auth.authenticate_experiment_user()` for consistency

## Session Management

**Session Cookie:**
- Name: `store_factory_session_store_factory`
- Type: JWT token stored in HTTP-only cookie
- TTL: 86400 seconds (24 hours)
- Secure: Based on request scheme or `G_NOME_ENV`

**Session Token Payload:**
```json
{
  "experiment_slug": "store_factory",
  "experiment_user_id": "<user_id>",
  "exp": <timestamp>,
  "iat": <timestamp>
}
```

## Integration Points

### 1. Demo Store Seeding (`actor.initialize()`)
- Creates demo stores with owner users
- Users created in sub-auth compatible format
- Idempotent: Skips if store/user already exists

### 2. Store Creation (`actor.create_store()`)
- Creates new store with owner user
- Checks for existing users before creating
- Uses sub-auth compatible user format

### 3. Admin Login (`actor.admin_login()`)
- Authenticates against experiment `users` collection
- Supports both plain text and bcrypt passwords
- Returns user_id for session creation

### 4. Protected Routes
- Use `get_experiment_user_from_session` dependency
- Automatically check for valid sub-auth session
- Redirect to login if not authenticated

## Testing Demo Users

**Demo Store Logins:**

1. **Country Pizza:**
   - URL: `/experiments/store_factory/country-pizza`
   - Login: `/experiments/store_factory/country-pizza/admin/login`
   - Email: `owner@country-pizza.com`
   - Password: `password123`

2. **Premium Auto Sales:**
   - URL: `/experiments/store_factory/premium-auto-sales`
   - Email: `owner@premiumauto.com`
   - Password: `demo123`

3. **Quick Fix Auto:**
   - URL: `/experiments/store_factory/quick-fix-auto`
   - Email: `owner@quickfix.com`
   - Password: `demo123`

## Security Considerations

**Current (Demo):**
- Plain text passwords (acceptable for demo environment)
- Session tokens use platform SECRET_KEY
- HTTP-only cookies prevent XSS
- Secure cookies in production

**Production Recommendations:**
1. Hash passwords using bcrypt: `hash_password: true` in `create_experiment_user()`
2. Use secure cookies: Set `G_NOME_ENV=production`
3. Implement password reset functionality
4. Add rate limiting to login endpoints
5. Consider adding 2FA for store owners

## Troubleshooting

### Demo Users Not Working

**Check:**
1. Actor initialization completed: Check logs for "Successfully created X demo store(s)"
2. Users exist: Check `store_factory_users` collection in MongoDB
3. Sub-auth enabled: Verify `sub_auth.enabled: true` in manifest/config

### Session Not Persisting

**Check:**
1. Cookie name: Should be `store_factory_session_store_factory`
2. Cookie settings: HTTP-only, secure (in production), same-site: lax
3. Session TTL: Default 24 hours, can be extended in manifest
4. Token validation: Check JWT payload matches experiment slug

### Login Failing

**Check:**
1. User exists: Query `store_factory_users` collection
2. Store exists: Query `store_factory_stores` collection
3. Password match: Verify plain text password or bcrypt hash
4. Store ID match: User must have matching `store_id`

## Future Enhancements

1. **Password Hashing:** Migrate demo users to bcrypt hashed passwords
2. **User Registration:** Enable `allow_registration: true` for customer accounts
3. **Role-Based Access:** Extend sub-auth for manager/staff roles
4. **Password Reset:** Add password reset functionality
5. **Session Management:** Add logout endpoint and session invalidation

