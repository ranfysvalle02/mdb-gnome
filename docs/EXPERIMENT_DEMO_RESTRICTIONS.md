# Experiment Demo User Restrictions

This document explains the reusable pattern for restricting demo users from certain endpoints across experiments.

## Overview

Demo users are "trapped" in their demo role for security reasons. They have read-only access to pre-seeded demo content and cannot:
- Access authentication routes (login, register, logout)
- Create new content (projects, notes, etc.)
- Modify or delete existing content

## Reusable Dependencies

The `experiment_auth_restrictions.py` module provides two FastAPI dependencies:

### 1. `block_demo_users`

Blocks demo users but allows unauthenticated users to pass through. Use this for routes like login/register pages where unauthenticated users should be allowed.

```python
from experiment_auth_restrictions import block_demo_users
from fastapi import Depends

@bp.get("/login", dependencies=[Depends(block_demo_users)])
async def login_get(request: Request):
    # Demo users are blocked, but unauthenticated users can access
    ...

@bp.post("/logout", dependencies=[Depends(block_demo_users)])
async def logout_post(request: Request):
    # Demo users are blocked from logging out
    ...
```

### 2. `require_non_demo_user`

Requires an authenticated, non-demo user. Returns the user object. Use this for routes that need the user object and must be restricted from demo users.

```python
from experiment_auth_restrictions import require_non_demo_user
from fastapi import Depends
from typing import Dict, Any

@bp.post("/api/projects")
async def create_project(
    request: Request,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    # user is guaranteed to NOT be a demo user
    # Demo users are blocked, and unauthenticated users get 401
    ...
```

## Example Usage in StoryWeaver

StoryWeaver uses these dependencies to restrict demo users:

### Authentication Routes (Blocked)
```python
@bp.get("/login", dependencies=[Depends(block_demo_users)])
@bp.post("/login", dependencies=[Depends(block_demo_users)])
@bp.get("/register", dependencies=[Depends(block_demo_users)])
@bp.post("/register", dependencies=[Depends(block_demo_users)])
@bp.post("/logout", dependencies=[Depends(block_demo_users)])
```

### Content Creation/Modification Routes (Blocked)
```python
@bp.post("/api/projects")
async def create_project(
    request: Request,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    ...

@bp.delete("/api/projects/{project_id}")
async def delete_project(
    request: Request,
    project_id: str,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    ...

@bp.post("/api/notes")
async def add_note(
    request: Request,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    ...

@bp.put("/api/notes/{note_id}")
async def update_note(
    request: Request,
    note_id: str,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    ...

@bp.delete("/api/notes/{note_id}")
async def delete_note(
    request: Request,
    note_id: str,
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    ...
```

## Demo User Detection

Demo users are identified by:
1. `user.get('is_demo')` or `user.get('demo_mode')` flags
2. Email matching `DEMO_EMAIL_DEFAULT` (from `config.py`)
3. Email starting with `demo@`

## Response Codes

- **403 Forbidden**: Returned when a demo user tries to access a restricted endpoint
- **401 Unauthorized**: Returned when an unauthenticated user tries to access an endpoint that requires authentication (via `require_non_demo_user`)

## Using in Other Experiments

To apply this pattern to other experiments:

1. **Import the dependencies**:
```python
from experiment_auth_restrictions import block_demo_users, require_non_demo_user
```

2. **Apply to restricted routes**:
```python
# For routes that need to block demo users but allow unauthenticated users
@bp.post("/logout", dependencies=[Depends(block_demo_users)])

# For routes that need authenticated, non-demo users
@bp.post("/api/create", dependencies=[Depends(require_non_demo_user)])
async def create_something(request: Request, user: Dict[str, Any] = Depends(require_non_demo_user)):
    ...
```

## Best Practices

1. **Block authentication routes**: Demo users should not be able to login, register, or logout
2. **Block content creation**: Demo users should not be able to create new content
3. **Block content modification**: Demo users should not be able to modify or delete existing content
4. **Allow read-only access**: Demo users should be able to view and interact with pre-seeded demo content

## Security Notes

- Demo users are "trapped" in demo mode for security
- They cannot escape their demo role through authentication
- This ensures demo environments remain clean and controlled
- Pre-seeded content provides the demo experience without allowing modifications

