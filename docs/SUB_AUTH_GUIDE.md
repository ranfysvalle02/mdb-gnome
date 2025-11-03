# Sub-Authentication Guide for Experiments

This guide explains how experiments can implement their own user authentication and session management (sub-authentication) separate from platform-level authentication.

## Overview

**Sub-authentication** allows experiments to:
- Manage their own user accounts independently of platform users
- Create experiment-specific sessions
- Support anonymous users with session tracking
- Integrate with OAuth providers
- Link platform users to experiment-specific profiles

## Architecture

Sub-authentication works in **layers**:
1. **Platform Auth** (if enabled): Controls who can access the experiment
2. **Sub-Auth** (experiment-level): Manages experiment-specific user identities and sessions

### Flow Example

```
1. User accesses /experiments/store_factory (platform auth checks access)
2. Experiment checks for sub-auth session cookie
3. If no session, user can:
   - Register (if allow_registration: true)
   - Login with experiment credentials
   - Use as anonymous (if anonymous_session strategy)
4. Experiment creates session token and sets cookie
5. Subsequent requests use session token for experiment-level auth
```

## Manifest Configuration

### Basic Setup

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "session_cookie_name": "experiment_session",
    "session_ttl_seconds": 86400,
    "allow_registration": false
  }
}
```

### Strategy Options

#### 1. `experiment_users` (Default)
Experiment-specific user accounts stored in experiment's users collection.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "allow_registration": true
  }
}
```

#### 2. `anonymous_session`
Anonymous users with session-based tracking.

```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "anonymous_session",
    "anonymous_user_prefix": "guest"
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

#### 4. `hybrid`
Combine platform users with experiment-specific profiles.

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

## Implementation Examples

### Example 1: Store Factory (Experiment Users)

Store Factory uses `experiment_users` strategy for demo stores:

**manifest.json:**
```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
    "collection_name": "users",
    "session_cookie_name": "store_factory_session"
  }
}
```

**Route Implementation:**
```python
from sub_auth import get_experiment_sub_user, create_experiment_session, authenticate_experiment_user
from core_deps import get_scoped_db, get_experiment_config

@bp.post("/{store_slug}/admin/login")
async def admin_login_post(request: Request, store_slug: str, ...):
    # Authenticate against experiment users collection
    db = await get_scoped_db(request)
    user = await authenticate_experiment_user(
        db, email, password, store_id=store_id, collection_name="users"
    )
    
    if user:
        # Create session
        slug_id = "store_factory"
        config = await get_experiment_config(request, slug_id)
        response = RedirectResponse(...)
        await create_experiment_session(request, slug_id, str(user["_id"]), config, response)
        return response
```

### Example 2: Protected Route

```python
from sub_auth import get_experiment_sub_user
from core_deps import get_scoped_db, get_experiment_config

@bp.get("/protected")
async def protected_route(request: Request):
    slug_id = getattr(request.state, "slug_id", "my_experiment")
    db = await get_scoped_db(request)
    config = await get_experiment_config(request, slug_id)
    
    # Get experiment user
    user = await get_experiment_sub_user(request, slug_id, db, config)
    if not user:
        raise HTTPException(401, "Experiment authentication required")
    
    return {"user_id": user["experiment_user_id"], "email": user["email"]}
```

### Example 3: Registration

```python
from sub_auth import create_experiment_user, create_experiment_session

@bp.post("/register")
async def register(request: Request, email: str, password: str):
    slug_id = "my_experiment"
    db = await get_scoped_db(request)
    config = await get_experiment_config(request, slug_id)
    
    # Create user
    user = await create_experiment_user(
        db, email, password, role="user", collection_name="users"
    )
    
    if not user:
        raise HTTPException(400, "User already exists")
    
    # Create session
    response = JSONResponse({"success": True})
    await create_experiment_session(request, slug_id, str(user["_id"]), config, response)
    return response
```

## Helper Functions

### `get_experiment_sub_user()`
Get experiment-specific user from session cookie.

```python
user = await get_experiment_sub_user(request, slug_id, db, config)
```

### `create_experiment_session()`
Create session token and set cookie.

```python
token = await create_experiment_session(request, slug_id, user_id, config, response)
```

### `authenticate_experiment_user()`
Authenticate user against experiment users collection.

```python
user = await authenticate_experiment_user(db, email, password, store_id=store_id)
```

### `create_experiment_user()`
Create new user in experiment users collection.

```python
user = await create_experiment_user(db, email, password, role="user", hash_password=True)
```

### `get_or_create_anonymous_user()`
Get or create anonymous user for anonymous_session strategy.

```python
user = await get_or_create_anonymous_user(request, slug_id, db, config)
```

## Intelligent Demo User Support

Sub-authentication includes **intelligent demo user support** that automatically creates and links demo users based on platform configuration. This provides "batteries included" functionality for experiments.

### Automatic Demo User Linking

When `auto_link_platform_demo: true` (default), the system automatically:

1. **Detects platform demo user** (from `ENABLE_DEMO` environment variable)
2. **Creates experiment-specific demo user** if it doesn't exist
3. **Links to platform demo user** if `link_platform_users: true`

### Manifest Configuration

**Simple Configuration (Auto-Detect Platform Demo):**
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

This configuration automatically:
- Uses platform demo user email/password from `ENABLE_DEMO` env var
- Creates experiment demo user on first access or actor initialization
- Links platform demo to experiment profile

**Custom Demo Users:**
```json
{
  "sub_auth": {
    "enabled": true,
    "strategy": "experiment_users",
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

### Actor Initialization

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

### Demo User Seeding Strategy

- **`auto`** (default): Automatically create/link demo users on first access or actor initialization
- **`manual`**: Require explicit creation via API
- **`disabled`**: No automatic demo user handling

### Platform Demo User

The platform demo user is configured via environment variable:

```bash
# Format: ENABLE_DEMO=email:password
ENABLE_DEMO=demo@demo.com:demo123
```

When enabled, sub-auth automatically:
1. Detects platform demo user
2. Creates experiment-specific demo user with same email
3. Links them if `link_platform_users: true` (hybrid strategy)

### Helper Functions

**`ensure_demo_users_exist()`**: Ensure demo users exist based on manifest config
```python
from sub_auth import ensure_demo_users_exist

demo_users = await ensure_demo_users_exist(
    db=db,
    slug_id="my_experiment",
    config=config,  # From manifest.json
    mongo_uri=mongo_uri,
    db_name=db_name
)
```

**`ensure_demo_users_for_actor()`**: Convenience function for actors (reads manifest automatically)
```python
from sub_auth import ensure_demo_users_for_actor

demo_users = await ensure_demo_users_for_actor(
    db=self.db,
    slug_id=self.write_scope,
    mongo_uri=self.mongo_uri,
    db_name=self.db_name
)
```

**`get_or_create_demo_user_for_request()`**: Get/create demo user for current request
```python
from sub_auth import get_or_create_demo_user_for_request

demo_user = await get_or_create_demo_user_for_request(
    request=request,
    slug_id="my_experiment",
    db=db,
    config=config
)
```

**`get_platform_demo_user()`**: Get platform demo user information
```python
from sub_auth import get_platform_demo_user

platform_demo = await get_platform_demo_user(mongo_uri, db_name)
if platform_demo:
    print(f"Platform demo: {platform_demo['email']}")
```

## Session Management

Sessions use JWT tokens stored in HTTP-only cookies:
- Cookie name: `{session_cookie_name}_{slug_id}` (e.g., `store_factory_session_store_factory`)
- TTL: Configurable via `session_ttl_seconds` (default: 86400 = 24 hours)
- Secure: Automatically set based on request scheme or `G_NOME_ENV`

## Integration with Platform Auth

Sub-authentication works alongside platform authentication:

1. **Platform Auth** checks experiment access (via `auth_policy` or `auth_required`)
2. **Sub-Auth** manages experiment-specific user identity

Both layers can be active simultaneously:
- Platform users can have experiment-specific profiles (`hybrid` strategy)
- Anonymous platform access can use sub-auth for tracking (`anonymous_session`)

## Best Practices

1. **Use bcrypt for passwords**: Set `hash_password: true` in `create_experiment_user()` for production
2. **Secure cookies**: Enable HTTPS in production (`G_NOME_ENV=production`)
3. **Session expiration**: Set reasonable `session_ttl_seconds` values
4. **Collection naming**: Use descriptive collection names (default: "users")
5. **Error handling**: Always check if sub-auth is enabled before using helpers

## Security Considerations

- **Password Storage**: Always hash passwords in production (use bcrypt)
- **Session Security**: Use HTTP-only, secure cookies with appropriate TTL
- **Token Validation**: Always validate session tokens and check experiment slug
- **Access Control**: Combine sub-auth with platform auth for defense in depth

## Troubleshooting

### Session Not Persisting
- Check cookie name matches configuration
- Verify cookie settings (httponly, secure, samesite)
- Check session TTL hasn't expired

### User Not Found
- Verify collection name matches `sub_auth.collection_name`
- Check user document structure (must have `_id` field)
- Ensure database scope is correct

### Authentication Failing
- Verify password hash format (bcrypt vs plain text)
- Check store_id filter if using multi-store scenarios
- Verify email/query matches user document

