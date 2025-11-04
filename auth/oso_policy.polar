# OSO/Polar Policy File
# This defines a standard Role-Based Access Control (RBAC) model.
# Equivalent to the Casbin model in casbin_model.conf

# Main authorization rule
# This rule checks if a user has permission to perform an action on a resource
# by checking if the user has a role, and if that role grants the permission.
has_permission(user, permission, resource) if
  has_role(user, role) and
  grants_permission(role, permission, resource);

# Note:
# - has_role(user, role) facts are added via add_role_for_user() (maps to Casbin g(user, role))
# - grants_permission(role, permission, resource) facts are added via add_policy() 
#   (maps to Casbin policy rules p = role, object, action)
#
# Usage:
# The system will call: oso.authorize(user, permission, resource)
# Which internally evaluates: has_permission(user, permission, resource)

