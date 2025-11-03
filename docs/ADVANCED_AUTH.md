# Advanced Authentication System

This document provides comprehensive documentation for the advanced authentication system in mdb-gnome, including experiment-level authorization, sub-authentication, and intelligent demo user support.

## Table of Contents

1. [Overview](#overview)
2. [Experiment-Level Authorization](#experiment-level-authorization)
3. [Sub-Authentication](#sub-authentication)
4. [Intelligent Demo User Support](#intelligent-demo-user-support)
5. [Best Practices](#best-practices)
6. [Security Considerations](#security-considerations)
7. [Troubleshooting](#troubleshooting)
8. [Advanced Patterns](#advanced-patterns)

## Overview

The mdb-gnome platform provides a **multi-layered authentication and authorization system**:

1. **Platform-Level Authentication**: Controls who can access the platform
2. **Experiment-Level Authorization**: Fine-grained control over experiment access
3. **Sub-Authentication**: Experiment-specific user management and sessions
4. **Demo User Support**: Automatic demo user creation and linking

### Architecture Layers

```
┌─────────────────────────────────────┐
│  Platform-Level Authentication     │
│  (JWT-based, platform users)       │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Experiment-Level Authorization     │
│  (auth_policy in manifest.json)    │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Sub-Authentication                │
│  (Experiment-specific users)        │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Demo User Support                  │
│  (Automatic linking/creation)      │
└─────────────────────────────────────┘
```

## Experiment-Level Authorization

Experiment-level authorization provides fine-grained control over who can access experiments, defined declaratively in `manifest.json`.

### Configuration

**Basic Setup:**
```json
{
  "auth_policy": {
    "required": true,
    "allowed_roles": ["admin", "developer"],
    "owner_can_access": true
  }
}
```

**Advanced Configuration:**
```json
{
  "auth_policy": {
    "required": true,
    "allowed_roles": ["admin", "developer"],
    "allowed_users": ["user@example.com"],
    "denied_users": ["blocked@example.com"],
    "required_permissions": ["experiments:view"],
    "custom_resource": "experiment:my_experiment",
    "custom_actions": ["access", "read", "write"],
    "allow_anonymous": false,
    "owner_can_access": true
  }
}
```

### Access Control Rules

The system evaluates access in this order:

1. **Denied Users** (blacklist) - Highest priority
2. **Allowed Users** (whitelist) - If provided, only these users can access
3. **Allowed Roles** - Users must have at least one of these roles
4. **Required Permissions** - Users must have all listed permissions
5. **Owner Access** - Experiment owner can always access (if enabled)
6. **Anonymous Access** - Only if `allow_anonymous: true` and `required: false`

### Policy Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `required` | boolean | `true` | Whether authentication is required |
| `allowed_roles` | string[] | `[]` | Roles that can access (OR logic) |
| `allowed_users` | string[] | `[]` | Specific user emails (whitelist) |
| `denied_users` | string[] | `[]` | User emails to deny (blacklist) |
| `required_permissions` | string[] | `[]` | Permissions required (AND logic) |
| `custom_resource` | string | `experiment:{slug}` | Custom Casbin resource |
| `custom_actions` | string[] | `["access"]` | Actions to check |
| `allow_anonymous` | boolean | `false` | Allow unauthenticated access |
| `owner_can_access` | boolean | `true` | Owner can always access |

### Backward Compatibility

The system maintains backward compatibility with the simple `auth_required` boolean:

```json
{
  "auth_required": true
}
```

If `auth_policy` is defined, it takes precedence over `auth_required`.

## Sub-Authentication

Sub-authentication allows experiments to manage their own user accounts and sessions independently of platform authentication.

### Strategies

#### 1. `experiment_users` (Default)
Experiment-specific user accounts stored in experiment's users collection.

**Use Case**: Independent user management (e.g., Store Factory demo stores)

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "session_cookie_name": "my_experiment_session",
    "session_ttl_seconds": 86400,
    "allow_registration": false
  }
}
```

#### 2. `anonymous_session`
Anonymous users with session-based tracking.

**Use Case**: Temporary sessions for unauthenticated users

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "anonymous_session",
    "anonymous_user_prefix": "guest",
    "collection_name": "users"
  }
}
```

#### 3. `oauth`
OAuth integration for external authentication.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "oauth",
    "oauth_providers": [
      {
        "name": "google",
        "client_id": "OAUTH_GOOGLE_CLIENT_ID",
        "client_secret": "OAUTH_GOOGLE_CLIENT_SECRET"
      }
    ]
  }
}
```

#### 4. `hybrid` (Recommended for Platform Integration)
Combine platform users with experiment-specific profiles.

**Use Case**: Platform users with experiment-specific data (e.g., Story Weaver)

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "hybrid",
    "link_platform_users": true,
    "user_id_field": "experiment_user_id"
  }
}
```

### Implementation Pattern

```python
from sub_auth import get_experiment_sub_user, create_experiment_session, authenticate_experiment_user
from core_deps import get_scoped_db, get_experiment_config

@bp.post("/login")
async def login(request: Request, email: str, password: str):
    slug_id = "my_experiment"
    db = await get_scoped_db(request)
    
    # Authenticate against experiment users collection
    user = await authenticate_experiment_user(
        db, email, password, collection_name="users"
    )
    
    if not user:
        raise HTTPException(401, "Invalid credentials")
    
    # Create session
    config = await get_experiment_config(request, slug_id)
    response = RedirectResponse(...)
    await create_experiment_session(
        request, slug_id, str(user["_id"]), config, response
    )
    return response

@bp.get("/protected")
async def protected_route(request: Request):
    slug_id = "my_experiment"
    db = await get_scoped_db(request)
    config = await get_experiment_config(request, slug_id)
    
    # Get experiment user
    user = await get_experiment_sub_user(request, slug_id, db, config)
    if not user:
        raise HTTPException(401, "Experiment authentication required")
    
    return {"user_id": user["experiment_user_id"], "email": user["email"]}
```

## Intelligent Demo User Support

The platform includes **intelligent demo user support** that automatically creates and links demo users based on configuration.

### Automatic Detection

When `auto_link_platform_demo: true` (default), the system automatically:

1. **Detects platform demo user** from `ENABLE_DEMO` environment variable
2. **Creates experiment-specific demo user** if it doesn't exist
3. **Links to platform demo** if `link_platform_users: true`

### Configuration

**Simple Auto-Detect:**
```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "hybrid",
    "auto_link_platform_demo": true,
    "demo_user_seed_strategy": "auto"
  }
}
```

**Custom Demo Users:**
```json
{
  "sub_auth": {
    "enabled": true,
    "demo_users": [
      {
        "email": "demo@myexperiment.com",
        "password": "demo123",
        "role": "demo",
        "auto_create": true,
        "link_to_platform": true,
        "extra_data": {
          "preferences": {"theme": "dark"}
        }
      }
    ],
    "demo_user_seed_strategy": "auto"
  }
}
```

### Demo User Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `email` | string | platform demo | Email address |
| `password` | string | platform demo | Password (plain text for demo) |
| `role` | string | `"user"` | User role in experiment |
| `auto_create` | boolean | `true` | Automatically create if missing |
| `link_to_platform` | boolean | `false` | Link to platform demo user |
| `extra_data` | object | `{}` | Additional user data |

### Seeding Strategy

| Strategy | Description |
|----------|-------------|
| `auto` | Automatically create/link on first access or actor initialization |
| `manual` | Require explicit creation via API |
| `disabled` | No automatic demo user handling |

### Actor Integration

Actors can automatically ensure demo users exist during initialization:

```python
async def initialize(self):
    """Post-initialization hook with demo user support."""
    if not self.db:
        return
    
    # Intelligently ensure demo users exist
    from sub_auth import ensure_demo_users_for_actor
    demo_users = await ensure_demo_users_for_actor(
        db=self.db,
        slug_id=self.write_scope,
        mongo_uri=self.mongo_uri,
        db_name=self.db_name
    )
    
    if demo_users:
        logger.info(f"Ensured {len(demo_users)} demo user(s) exist")
```

**Note**: In isolated Ray environments, manifest.json might not be accessible via filesystem. The system gracefully handles this and auto-creates demo users on first access via request context.

## Best Practices

### 1. Authentication Strategy Selection

- **`experiment_users`**: For completely independent user management
- **`hybrid`**: For platform integration with experiment-specific data
- **`anonymous_session`**: For temporary guest sessions
- **`oauth`**: For external identity providers

### 2. Password Security

- **Production**: Always use `hash_password: true` in `create_experiment_user()`
- **Demo**: Plain text passwords are acceptable for demo users only
- **Never**: Store plain text passwords in production

```python
# Production
user = await create_experiment_user(
    db, email, password, role="user", hash_password=True
)

# Demo only
user = await create_experiment_user(
    db, email, password, role="demo", hash_password=False
)
```

### 3. Session Management

- **TTL**: Set reasonable `session_ttl_seconds` (default: 86400 = 24 hours)
- **Cookies**: Use HTTP-only, secure cookies in production
- **Expiration**: Always validate session token expiration

### 4. Error Handling

Always handle authentication errors gracefully:

```python
try:
    user = await get_experiment_sub_user(request, slug_id, db, config)
    if not user:
        raise HTTPException(401, "Authentication required")
except Exception as e:
    logger.error(f"Authentication error: {e}", exc_info=True)
    raise HTTPException(500, "Authentication service error")
```

### 5. Demo User Management

- **Auto-Detect**: Use `auto_link_platform_demo: true` for seamless demo support
- **Custom Users**: Define custom demo users in manifest for flexibility
- **Seeding**: Use `demo_user_seed_strategy: "auto"` for automatic setup

## Security Considerations

### 1. Password Storage

- **Bcrypt**: Always hash passwords in production using bcrypt
- **Salt**: Bcrypt automatically generates salt per password
- **Plain Text**: Only acceptable for demo users in development

### 2. Session Security

- **JWT**: Sessions use JWT tokens with expiration
- **HTTP-Only**: Cookies are HTTP-only to prevent XSS attacks
- **Secure**: Cookies use `secure` flag in HTTPS environments
- **SameSite**: Cookies use `lax` SameSite policy

### 3. Access Control

- **Layered Defense**: Combine platform auth, experiment auth, and sub-auth
- **Least Privilege**: Grant minimum required permissions
- **Validation**: Always validate user permissions before actions

### 4. Demo Users

- **Isolation**: Demo users should not have production data access
- **Linking**: Carefully consider `link_to_platform` implications
- **Cleanup**: Consider cleanup strategies for demo user data

## Troubleshooting

### Session Not Persisting

**Symptoms**: User logged out after page refresh

**Solutions**:
1. Check cookie name matches configuration
2. Verify cookie settings (httponly, secure, samesite)
3. Check session TTL hasn't expired
4. Ensure HTTPS in production (secure cookies require HTTPS)

### Demo Users Not Created

**Symptoms**: Demo users don't exist after actor initialization

**Possible Causes**:
1. `demo_user_seed_strategy` is `"disabled"`
2. `auto_link_platform_demo` is `false` and no `demo_users` configured
3. Platform demo user not enabled (`ENABLE_DEMO` not set)
4. Manifest.json not accessible (isolated Ray environment)

**Solutions**:
- Check manifest.json configuration
- Verify `ENABLE_DEMO` environment variable
- Check actor logs for errors
- Demo users will auto-create on first access if configured

### Authentication Failing

**Symptoms**: Users cannot authenticate even with correct credentials

**Possible Causes**:
1. Password hash format mismatch (bcrypt vs plain text)
2. Collection name mismatch
3. Database scope incorrect
4. Email format validation failure

**Solutions**:
- Verify password hash format matches authentication logic
- Check collection name matches `sub_auth.collection_name`
- Verify database scope is correct
- Check email format (must contain "@" and ".")

### Platform Demo User Not Linking

**Symptoms**: Platform demo user doesn't link to experiment demo

**Possible Causes**:
1. `link_platform_users: false` (hybrid strategy)
2. `link_to_platform: false` in demo_users config
3. Email mismatch between platform and experiment demo

**Solutions**:
- Enable `link_platform_users: true` for hybrid strategy
- Set `link_to_platform: true` in demo_users config
- Ensure platform demo email matches experiment demo email

## Advanced Patterns

### Pattern 1: Multi-Store Authentication

Example: Store Factory with multiple stores, each with their own users.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "demo_users": [
      {
        "email": "owner@store1.com",
        "password": "demo123",
        "role": "owner",
        "extra_data": {"store_id": "store1"}
      }
    ]
  }
}
```

Authentication with store_id filter:
```python
user = await authenticate_experiment_user(
    db, email, password, store_id=store_id, collection_name="users"
)
```

### Pattern 2: Platform User with Experiment Profile

Example: Story Weaver linking platform users to experiment profiles.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "hybrid",
    "link_platform_users": true,
    "auto_link_platform_demo": true
  }
}
```

Retrieving user:
```python
# Try platform user first
platform_user = await get_current_user(request)

# Get experiment user
experiment_user = await get_experiment_sub_user(request, slug_id, db, config)

# Link if needed
if platform_user and not experiment_user:
    # Create experiment profile for platform user
    experiment_user = await create_experiment_user_profile_for_platform_user(
        db, platform_user, slug_id
    )
```

### Pattern 3: Anonymous Session with Upgrade

Example: Anonymous users can upgrade to full accounts.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "anonymous_session",
    "allow_registration": true
  }
}
```

Upgrade flow:
```python
# Get anonymous user
anon_user = await get_or_create_anonymous_user(request, slug_id, db, config)

# Later: upgrade to full account
if anon_user and anon_user.get("is_anonymous"):
    # Create full account
    full_user = await create_experiment_user(
        db, email, password, role="user"
    )
    
    # Transfer anonymous data to full account
    await transfer_anonymous_data(db, anon_user, full_user)
```

### Pattern 4: Role-Based Experiment Access

Example: Different access levels based on roles.

```json
{
  "auth_policy": {
    "required": true,
    "allowed_roles": ["admin", "developer"],
    "required_permissions": ["experiments:view"],
    "custom_resource": "experiment:premium_feature",
    "custom_actions": ["access", "read", "write"]
  }
}
```

Checking permissions:
```python
# In route handler
authz = request.app.state.authz_provider
has_access = await authz.check(
    user_email, "experiment:premium_feature", "write"
)
```

## Reference

### Helper Functions

#### `get_experiment_sub_user()`
Get experiment-specific user from session cookie.

```python
user = await get_experiment_sub_user(request, slug_id, db, config)
```

#### `create_experiment_session()`
Create session token and set cookie.

```python
token = await create_experiment_session(
    request, slug_id, user_id, config, response
)
```

#### `authenticate_experiment_user()`
Authenticate user against experiment users collection.

```python
user = await authenticate_experiment_user(
    db, email, password, store_id=store_id, collection_name="users"
)
```

#### `create_experiment_user()`
Create new user in experiment users collection.

```python
user = await create_experiment_user(
    db, email, password, role="user", hash_password=True
)
```

#### `ensure_demo_users_exist()`
Ensure demo users exist based on manifest config.

```python
demo_users = await ensure_demo_users_exist(
    db, slug_id, config, mongo_uri, db_name
)
```

#### `ensure_demo_users_for_actor()`
Convenience function for actors (reads manifest automatically).

```python
demo_users = await ensure_demo_users_for_actor(
    db, slug_id, mongo_uri, db_name
)
```

#### `get_or_create_demo_user_for_request()`
Get/create demo user for current request context.

```python
demo_user = await get_or_create_demo_user_for_request(
    request, slug_id, db, config
)
```

#### `get_platform_demo_user()`
Get platform demo user information.

```python
platform_demo = await get_platform_demo_user(mongo_uri, db_name)
```

### Configuration Reference

See [SUB_AUTH_GUIDE.md](./SUB_AUTH_GUIDE.md) for detailed configuration options.

### Related Documentation

- [EXPERIMENT_AUTH.md](./EXPERIMENT_AUTH.md) - Experiment-level authorization
- [SUB_AUTH_GUIDE.md](./SUB_AUTH_GUIDE.md) - Sub-authentication guide
- [SEED_DEMO.md](./SEED_DEMO.md) - Demo seeding patterns

