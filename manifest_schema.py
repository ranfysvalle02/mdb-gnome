"""
JSON Schema validation for experiment manifest.json files.

This module provides comprehensive validation for manifest.json files,
ensuring they conform to the expected structure and include valid index
definitions.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple
from jsonschema import validate, ValidationError, SchemaError

logger = logging.getLogger(__name__)

# JSON Schema definition for manifest.json
MANIFEST_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {
            "type": "string",
            "pattern": "^[a-z0-9_-]+$",
            "description": "Experiment slug (lowercase alphanumeric, underscores, hyphens)"
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Human-readable experiment name"
        },
        "description": {
            "type": "string",
            "description": "Experiment description"
        },
        "status": {
            "type": "string",
            "enum": ["active", "draft", "archived", "inactive"],
            "default": "draft",
            "description": "Experiment status"
        },
        "auth_required": {
            "type": "boolean",
            "default": False,
            "description": "Whether authentication is required for this experiment"
        },
        "data_scope": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "minItems": 1,
            "default": ["self"],
            "description": "List of experiment slugs whose data this experiment can access"
        },
        "runtime_s3_uri": {
            "type": "string",
            "format": "uri",
            "description": "S3 URI for runtime code (for production deployments)"
        },
        "runtime_pip_deps": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "description": "List of pip dependencies for isolated runtime"
        },
        "managed_indexes": {
            "type": "object",
            "patternProperties": {
                "^[a-zA-Z0-9_]+$": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/indexDefinition"
                    },
                    "minItems": 1
                }
            },
            "description": "Collection name -> list of index definitions"
        },
        "collection_settings": {
            "type": "object",
            "patternProperties": {
                "^[a-zA-Z0-9_]+$": {
                    "$ref": "#/definitions/collectionSettings"
                }
            },
            "description": "Collection name -> collection settings"
        }
    },
    "required": [],
    "definitions": {
        "indexDefinition": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-zA-Z0-9_]+$",
                    "minLength": 1,
                    "description": "Base index name (will be prefixed with slug)"
                },
                "type": {
                    "type": "string",
                    "enum": ["regular", "vectorSearch", "search", "text", "geospatial", "ttl", "partial"],
                    "description": "Index type"
                },
                "keys": {
                    "oneOf": [
                        {
                            "type": "object",
                            "patternProperties": {
                                "^[a-zA-Z0-9_.]+$": {
                                    "oneOf": [
                                        {"type": "integer", "enum": [1, -1]},
                                        {"type": "string", "enum": ["text", "2dsphere", "2d", "geoHaystack", "hashed"]}
                                    ]
                                }
                            }
                        },
                        {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 2,
                                "prefixItems": [
                                    {
                                        "type": "string"
                                    },
                                    {
                                        "oneOf": [
                                            {"type": "integer", "enum": [1, -1]},
                                            {"type": "string", "enum": ["text", "2dsphere", "2d", "geoHaystack", "hashed"]}
                                        ]
                                    }
                                ],
                                "items": False
                            }
                        }
                    ],
                    "description": "Index keys (required for regular, text, geospatial, ttl, partial indexes)"
                },
                "definition": {
                    "type": "object",
                    "description": "Index definition (required for vectorSearch and search indexes)"
                },
                "options": {
                    "type": "object",
                    "properties": {
                        "unique": {
                            "type": "boolean"
                        },
                        "sparse": {
                            "type": "boolean"
                        },
                        "background": {
                            "type": "boolean"
                        },
                        "name": {
                            "type": "string"
                        },
                        "partialFilterExpression": {
                            "type": "object",
                            "description": "Filter expression for partial indexes"
                        },
                        "expireAfterSeconds": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "TTL in seconds (required for TTL indexes)"
                        },
                        "weights": {
                            "type": "object",
                            "patternProperties": {
                                "^[a-zA-Z0-9_.]+$": {
                                    "type": "integer",
                                    "minimum": 1
                                }
                            },
                            "description": "Field weights for text indexes"
                        },
                        "default_language": {
                            "type": "string",
                            "description": "Default language for text indexes"
                        },
                        "language_override": {
                            "type": "string",
                            "description": "Language override field for text indexes"
                        }
                    },
                    "description": "Index options (varies by index type)"
                }
            },
            "required": ["name", "type"],
            "allOf": [
                {
                    "if": {
                        "properties": {"type": {"const": "regular"}}
                    },
                    "then": {
                        "required": ["keys"]
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "text"}}
                    },
                    "then": {
                        "required": ["keys"]
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "geospatial"}}
                    },
                    "then": {
                        "required": ["keys"]
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "ttl"}}
                    },
                    "then": {
                        "required": ["keys"]
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "partial"}}
                    },
                    "then": {
                        "required": ["keys", "options"]
                    },
                    "else": {
                        "properties": {
                            "options": {
                                "not": {
                                    "required": ["partialFilterExpression"]
                                }
                            }
                        }
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "vectorSearch"}}
                    },
                    "then": {
                        "required": ["definition"]
                    }
                },
                {
                    "if": {
                        "properties": {"type": {"const": "search"}}
                    },
                    "then": {
                        "required": ["definition"]
                    }
                }
            ]
        },
        "collectionSettings": {
            "type": "object",
            "properties": {
                "validation": {
                    "type": "object",
                    "properties": {
                        "validator": {
                            "type": "object"
                        },
                        "validationLevel": {
                            "type": "string",
                            "enum": ["off", "strict", "moderate"]
                        },
                        "validationAction": {
                            "type": "string",
                            "enum": ["error", "warn"]
                        }
                    }
                },
                "collation": {
                    "type": "object",
                    "properties": {
                        "locale": {"type": "string"},
                        "caseLevel": {"type": "boolean"},
                        "caseFirst": {"type": "string"},
                        "strength": {"type": "integer"},
                        "numericOrdering": {"type": "boolean"},
                        "alternate": {"type": "string"},
                        "maxVariable": {"type": "string"},
                        "normalization": {"type": "boolean"},
                        "backwards": {"type": "boolean"}
                    }
                },
                "capped": {
                    "type": "boolean"
                },
                "size": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum size in bytes for capped collection"
                },
                "max": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of documents for capped collection"
                },
                "timeseries": {
                    "type": "object",
                    "properties": {
                        "timeField": {"type": "string"},
                        "metaField": {"type": "string"},
                        "granularity": {
                            "type": "string",
                            "enum": ["seconds", "minutes", "hours"]
                        }
                    },
                    "required": ["timeField"]
                }
            }
        }
    }
}


def validate_manifest(manifest_data: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[List[str]]]:
    """
    Validate a manifest against the JSON Schema.
    
    Args:
        manifest_data: The manifest data to validate
        
    Returns:
        Tuple of (is_valid, error_message, error_paths)
        - is_valid: True if valid, False otherwise
        - error_message: Human-readable error message (None if valid)
        - error_paths: List of JSON paths with errors (None if valid)
    """
    try:
        validate(instance=manifest_data, schema=MANIFEST_SCHEMA)
        return True, None, None
    except ValidationError as e:
        error_paths = []
        error_messages = []
        
        # Extract error path and message
        error_path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
        error_paths.append(error_path)
        
        # Build human-readable message
        if e.absolute_path:
            field = ".".join(str(p) for p in e.absolute_path)
            error_messages.append(f"Field '{field}': {e.message}")
        else:
            error_messages.append(f"Manifest validation error: {e.message}")
        
        # Collect all validation errors from the context
        for error in e.context:
            ctx_path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "root"
            error_paths.append(ctx_path)
            if error.absolute_path:
                ctx_field = ".".join(str(p) for p in error.absolute_path)
                error_messages.append(f"Field '{ctx_field}': {error.message}")
            else:
                error_messages.append(error.message)
        
        combined_message = " | ".join(error_messages[:3])  # Limit to first 3 errors
        if len(error_messages) > 3:
            combined_message += f" (+ {len(error_messages) - 3} more errors)"
        
        return False, combined_message, error_paths
    except SchemaError as e:
        logger.error(f"Schema error (this is a bug): {e}")
        return False, f"Internal schema validation error: {e}", None
    except Exception as e:
        logger.error(f"Unexpected validation error: {e}", exc_info=True)
        return False, f"Unexpected validation error: {e}", None


def validate_index_definition(index_def: Dict[str, Any], collection_name: str, index_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a single index definition with context-specific checks.
    
    Args:
        index_def: The index definition to validate
        collection_name: Name of the collection (for error context)
        index_name: Name of the index (for error context)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    index_type = index_def.get("type")
    if not index_type:
        return False, f"Index '{index_name}' in collection '{collection_name}' is missing 'type' field"
    
    # Type-specific validation
    if index_type == "regular":
        if "keys" not in index_def:
            return False, f"Regular index '{index_name}' in collection '{collection_name}' requires 'keys' field"
        keys = index_def.get("keys")
        if not keys or (isinstance(keys, dict) and len(keys) == 0) or (isinstance(keys, list) and len(keys) == 0):
            return False, f"Regular index '{index_name}' in collection '{collection_name}' has empty 'keys'"
        
        # Check for _id index
        is_id_index = False
        if isinstance(keys, dict):
            is_id_index = len(keys) == 1 and "_id" in keys
        elif isinstance(keys, list):
            is_id_index = len(keys) == 1 and len(keys[0]) >= 1 and keys[0][0] == "_id"
        
        if is_id_index:
            return False, f"Index '{index_name}' in collection '{collection_name}' cannot target '_id' field (MongoDB creates _id indexes automatically)"
    
    elif index_type == "ttl":
        if "keys" not in index_def:
            return False, f"TTL index '{index_name}' in collection '{collection_name}' requires 'keys' field"
        options = index_def.get("options", {})
        if "expireAfterSeconds" not in options:
            return False, f"TTL index '{index_name}' in collection '{collection_name}' requires 'expireAfterSeconds' in options"
        expire_after = options.get("expireAfterSeconds")
        if not isinstance(expire_after, int) or expire_after < 1:
            return False, f"TTL index '{index_name}' in collection '{collection_name}' requires 'expireAfterSeconds' to be a positive integer"
        # Validate reasonable range (1 second to 1 year = 31536000 seconds)
        if expire_after > 31536000:
            return False, f"TTL index '{index_name}' in collection '{collection_name}' has 'expireAfterSeconds' too large ({expire_after}). Maximum recommended is 31536000 (1 year). Consider if this is intentional."
    
    elif index_type == "partial":
        if "keys" not in index_def:
            return False, f"Partial index '{index_name}' in collection '{collection_name}' requires 'keys' field"
        options = index_def.get("options", {})
        if "partialFilterExpression" not in options:
            return False, f"Partial index '{index_name}' in collection '{collection_name}' requires 'partialFilterExpression' in options"
    
    elif index_type == "text":
        if "keys" not in index_def:
            return False, f"Text index '{index_name}' in collection '{collection_name}' requires 'keys' field"
        keys = index_def.get("keys")
        # Text indexes should have text type in keys
        has_text = False
        if isinstance(keys, dict):
            has_text = any(v == "text" or v == "TEXT" for v in keys.values())
        elif isinstance(keys, list):
            has_text = any(len(k) >= 2 and (k[1] == "text" or k[1] == "TEXT") for k in keys)
        if not has_text:
            return False, f"Text index '{index_name}' in collection '{collection_name}' must have at least one field with 'text' type in keys"
    
    elif index_type == "geospatial":
        if "keys" not in index_def:
            return False, f"Geospatial index '{index_name}' in collection '{collection_name}' requires 'keys' field"
        keys = index_def.get("keys")
        # Geospatial indexes should have geospatial type in keys
        has_geo = False
        if isinstance(keys, dict):
            has_geo = any(v in ["2dsphere", "2d", "geoHaystack"] for v in keys.values())
        elif isinstance(keys, list):
            has_geo = any(len(k) >= 2 and k[1] in ["2dsphere", "2d", "geoHaystack"] for k in keys)
        if not has_geo:
            return False, f"Geospatial index '{index_name}' in collection '{collection_name}' must have at least one field with geospatial type ('2dsphere', '2d', or 'geoHaystack') in keys"
    
    elif index_type in ("vectorSearch", "search"):
        if "definition" not in index_def:
            return False, f"{index_type} index '{index_name}' in collection '{collection_name}' requires 'definition' field"
        definition = index_def.get("definition")
        if not isinstance(definition, dict):
            return False, f"{index_type} index '{index_name}' in collection '{collection_name}' requires 'definition' to be an object"
        
        # Additional validation for vectorSearch indexes
        if index_type == "vectorSearch":
            fields = definition.get("fields", [])
            if not isinstance(fields, list) or len(fields) == 0:
                return False, f"VectorSearch index '{index_name}' in collection '{collection_name}' requires 'definition.fields' to be a non-empty array"
            
            # Validate vector field dimensions
            for field in fields:
                if isinstance(field, dict) and field.get("type") == "vector":
                    num_dims = field.get("numDimensions")
                    if not isinstance(num_dims, int) or num_dims < 1 or num_dims > 10000:
                        return False, f"VectorSearch index '{index_name}' in collection '{collection_name}' requires 'numDimensions' to be between 1 and 10000, got: {num_dims}"
    
    else:
        return False, f"Unknown index type '{index_type}' for index '{index_name}' in collection '{collection_name}'"
    
    return True, None


def validate_managed_indexes(managed_indexes: Dict[str, List[Dict[str, Any]]]) -> Tuple[bool, Optional[str]]:
    """
    Validate all managed indexes with collection and index context.
    
    Args:
        managed_indexes: The managed_indexes object from manifest
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(managed_indexes, dict):
        return False, "'managed_indexes' must be an object mapping collection names to index arrays"
    
    for collection_name, indexes in managed_indexes.items():
        if not isinstance(collection_name, str) or not collection_name:
            return False, f"Collection name must be a non-empty string, got: {collection_name}"
        
        if not isinstance(indexes, list):
            return False, f"Indexes for collection '{collection_name}' must be an array"
        
        if len(indexes) == 0:
            return False, f"Collection '{collection_name}' has an empty indexes array"
        
        for idx, index_def in enumerate(indexes):
            if not isinstance(index_def, dict):
                return False, f"Index #{idx} in collection '{collection_name}' must be an object"
            
            index_name = index_def.get("name", f"index_{idx}")
            is_valid, error_msg = validate_index_definition(index_def, collection_name, index_name)
            if not is_valid:
                return False, error_msg
    
    return True, None

