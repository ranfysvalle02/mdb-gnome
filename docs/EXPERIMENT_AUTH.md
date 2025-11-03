# Experiment-Level Authorization Guide

This guide explains how to configure intelligent, fine-grained authorization for experiments via `manifest.json`.

## Overview

The g.nome platform supports two authorization modes:

1. **Simple Mode** (backward compatible): Uses `auth_required: boolean`
2. **Intelligent Mode** (recommended): Uses `auth_policy: object` for fine-grained control

## Simple Mode: `auth_required`

The simplest way to require authentication:

```json
{
  "name": "My Experiment",
  "auth_required": true
}
```

- `auth_required: true` - Requires authentication (redirects to login if not authenticated)
- `auth_required: false` - Allows anonymous access (default)

## Intelligent Mode: `auth_policy`

For fine-grained control, use the `auth_policy` object. This supports multiple authorization strategies:

### Basic Structure

```json
{
  "name": "My Experiment",
  "auth_policy": {
    "required": true,
    "allow_anonymous": false,
    "owner_can_access": true
  }
}
```

### Authorization Strategies

#### 1. Role-Based Access (`allowed_roles`)

Allow access based on user roles:

```json
{
  "auth_policy": {
    "allowed_roles": ["admin", "developer"]
  }
}
```

- Users must have **at least one** of the listed roles
- Roles are checked via Casbin's role assignments
- Common roles: `admin`, `developer`, `demo`, `user`

#### 2. User-Based Access (`allowed_users`, `denied_users`)

Whitelist/blacklist specific users:

```json
{
  "auth_policy": {
    "allowed_users": ["user1@example.com", "user2@example.com"],
    "denied_users": ["blocked@example.com"]
  }
}
```

- `allowed_users`: Only these users can access (whitelist)
- `denied_users`: These users are explicitly denied (blacklist, takes precedence)

#### 3. Permission-Based Access (`required_permissions`)

Require specific Casbin permissions:

```json
{
  "auth_policy": {
    "required_permissions": ["experiments:view", "experiments:manage_own"]
  }
}
```

- User must have **all** listed permissions
- Format: `"resource:action"` (e.g., `"experiments:view"`)

#### 4. Custom Resource/Actions (`custom_resource`, `custom_actions`)

Fine-grained permissions for specific experiments:

```json
{
  "auth_policy": {
    "custom_resource": "experiment:storyweaver",
    "custom_actions": ["read", "write", "admin"]
  }
}
```

- `custom_resource`: Custom Casbin resource name (defaults to `experiment:{slug}`)
- `custom_actions`: List of actions to check (defaults to `["access"]`)
- User must have **at least one** of the listed actions

### Policy Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `required` | boolean | `true` | Whether authentication is required |
| `allow_anonymous` | boolean | `false` | Allow anonymous (unauthenticated) access |
| `owner_can_access` | boolean | `true` | Experiment owner (developer_id) can always access |
| `allowed_roles` | array | `[]` | Roles that can access (any one required) |
| `allowed_users` | array | `[]` | User emails that can access (whitelist) |
| `denied_users` | array | `[]` | User emails explicitly denied (blacklist) |
| `required_permissions` | array | `[]` | Permissions user must have (all required) |
| `custom_resource` | string | `experiment:{slug}` | Custom Casbin resource name |
| `custom_actions` | array | `["access"]` | Custom actions to check (any one required) |

### Policy Evaluation Order

Authorization checks are performed in this order:

1. **Owner Check**: If `owner_can_access: true`, owner always has access
2. **Denied Users**: Blacklist check (takes highest precedence)
3. **Allowed Users**: Whitelist check (if provided, only these users can access)
4. **Allowed Roles**: Role-based check (user must have at least one role)
5. **Required Permissions**: Permission-based check (user must have all permissions)
6. **Custom Resource/Actions**: Fine-grained Casbin checks

If any check fails, access is denied. If all checks pass (or no checks are specified), access is granted.

### Complete Examples

#### Example 1: Admin and Developer Only

```json
{
  "name": "Admin Dashboard",
  "auth_policy": {
    "allowed_roles": ["admin", "developer"]
  }
}
```

#### Example 2: Specific Users Only

```json
{
  "name": "Beta Test",
  "auth_policy": {
    "allowed_users": ["beta1@example.com", "beta2@example.com"]
  }
}
```

#### Example 3: Permission-Based with Anonymous Read

```json
{
  "name": "Public Data Explorer",
  "auth_policy": {
    "required": false,
    "allow_anonymous": true,
    "custom_resource": "experiment:data_explorer",
    "custom_actions": ["read"],
    "required_permissions": ["experiments:view"]
  }
}
```

#### Example 4: Complex Multi-Strategy

```json
{
  "name": "Enterprise Feature",
  "auth_policy": {
    "required": true,
    "owner_can_access": true,
    "allowed_roles": ["admin"],
    "allowed_users": ["enterprise@example.com"],
    "denied_users": ["blocked@example.com"],
    "required_permissions": ["experiments:view"],
    "custom_resource": "experiment:enterprise",
    "custom_actions": ["read", "write"]
  }
}
```

## Backward Compatibility

The `auth_required` boolean is fully supported for backward compatibility:

- If `auth_policy` is **not** provided, `auth_required` is used
- If `auth_policy` is provided, it **takes precedence** over `auth_required`

## Setting Up Casbin Policies

For `custom_resource` and `custom_actions` to work, you need to create Casbin policies. Use the admin panel or programmatically:

```python
from authz_factory import create_authz_provider

authz = await create_authz_provider("casbin", {...})

# Grant access to specific user
await authz.add_policy("user@example.com", "experiment:storyweaver", "read")

# Grant access to role
await authz.add_policy("developer", "experiment:storyweaver", "read")

# Save policies
await authz.save_policy()
```

## Best Practices

1. **Start Simple**: Use `auth_required: true` for basic authentication
2. **Use Roles**: Prefer `allowed_roles` over `allowed_users` for maintainability
3. **Combine Strategies**: Use multiple strategies for defense in depth
4. **Test Policies**: Always test your authorization policies before deployment
5. **Document Access**: Document who should have access to your experiment

## Troubleshooting

### Access Denied When Should Have Access

1. Check if user has the required role: Use admin panel to verify roles
2. Check if user is in allowed_users list (if provided)
3. Verify Casbin policies exist for custom_resource/actions
4. Check experiment owner (developer_id) if owner_can_access is enabled

### Anonymous Access Not Working

- Ensure `required: false` or `allow_anonymous: true`
- Check that no other policies (roles, users, permissions) are blocking access

### Role Not Working

- Verify role is assigned to user in Casbin
- Check role name matches exactly (case-sensitive)
- Ensure Casbin policies grant the role access to experiments

