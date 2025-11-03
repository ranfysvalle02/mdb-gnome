# Manifest Schema Validation Guide

This document covers best practices, debugging tips, and detailed formats for `manifest.json` validation and automated index management in g.nome.

## Table of Contents

1. [Overview](#overview)
2. [Manifest Structure](#manifest-structure)
3. [Index Types & Formats](#index-types--formats)
4. [Best Practices](#best-practices)
5. [Debugging Tips](#debugging-tips)
6. [Common Issues & Solutions](#common-issues--solutions)
7. [Validation Flow](#validation-flow)

## Overview

g.nome automatically validates all `manifest.json` files using JSON Schema before:
- Saving manifests via the admin API
- Seeding from local filesystem
- Uploading ZIP files
- Registering experiments

Invalid manifests are **blocked** from registration with detailed error messages pointing to the exact issue.

## Manifest Structure

### Required Fields

- **`name`**: Human-readable experiment name (string, min 1 character)

### Optional Fields

- **`slug`**: Experiment identifier (lowercase alphanumeric, underscores, hyphens)
- **`description`**: Experiment description (string)
- **`status`**: One of `"active"`, `"draft"`, `"archived"`, `"inactive"` (default: `"draft"`)
- **`auth_required`**: Boolean (default: `false`)
- **`data_scope`**: Array of experiment slugs (default: `["self"]`)
- **`runtime_s3_uri`**: S3 URI for production deployments
- **`runtime_pip_deps`**: Array of pip dependency strings
- **`managed_indexes`**: Object mapping collection names to index definitions
- **`collection_settings`**: Object mapping collection names to collection configuration

### Minimal Valid Manifest

```json
{
  "name": "My Experiment"
}
```

## Index Types & Formats

### Regular Indexes

Standard MongoDB indexes for equality, range queries, and sorting.

**Format:**
```json
{
  "name": "index_name",
  "type": "regular",
  "keys": {
    "field1": 1,
    "field2": -1
  },
  "options": {
    "unique": true,
    "sparse": false,
    "background": true
  }
}
```

**Alternative array format:**
```json
{
  "name": "index_name",
  "type": "regular",
  "keys": [
    ["field1", 1],
    ["field2", -1]
  ],
  "options": {
    "unique": true
  }
}
```

**Key Points:**
- `keys` can be an object `{field: direction}` or array `[["field", direction]]`
- Direction: `1` (ascending) or `-1` (descending)
- Cannot index `_id` field (MongoDB creates this automatically)
- `options.name` is automatically prefixed with `{slug}_`, don't include it manually

### TTL Indexes

Time-to-live indexes that automatically delete documents after a specified time.

**Format:**
```json
{
  "name": "session_ttl",
  "type": "ttl",
  "keys": {
    "created_at": 1
  },
  "options": {
    "expireAfterSeconds": 3600
  }
}
```

**Key Points:**
- **Requires** `options.expireAfterSeconds` (positive integer, 1-31536000 seconds)
- Field must be a date/timestamp type
- Recommended: 1 hour (3600) to 1 year (31536000) seconds
- MongoDB runs TTL cleanup every 60 seconds

**Example:**
```json
{
  "managed_indexes": {
    "sessions": [
      {
        "name": "session_expiry",
        "type": "ttl",
        "keys": {"created_at": 1},
        "options": {"expireAfterSeconds": 86400}
      }
    ]
  }
}
```

### Partial Indexes

Indexes that only index documents matching a filter expression.

**Format:**
```json
{
  "name": "active_users_index",
  "type": "partial",
  "keys": {
    "user_id": 1,
    "email": 1
  },
  "options": {
    "unique": true,
    "partialFilterExpression": {
      "active": true,
      "email": {"$exists": true}
    }
  }
}
```

**Key Points:**
- **Requires** `options.partialFilterExpression` (valid MongoDB query)
- Reduces index size by only indexing matching documents
- Useful for indexes on frequently queried subsets of data
- Filter expression must be a valid MongoDB query object

**Example:**
```json
{
  "managed_indexes": {
    "users": [
      {
        "name": "active_users_email",
        "type": "partial",
        "keys": {"email": 1},
        "options": {
          "unique": true,
          "partialFilterExpression": {
            "active": true,
            "deleted_at": {"$exists": false}
          }
        }
      }
    ]
  }
}
```

### Text Indexes

Full-text search indexes for searching text content.

**Format:**
```json
{
  "name": "product_search",
  "type": "text",
  "keys": {
    "name": "text",
    "description": "text"
  },
  "options": {
    "weights": {
      "name": 10,
      "description": 5
    },
    "default_language": "english",
    "language_override": "language"
  }
}
```

**Alternative array format:**
```json
{
  "name": "product_search",
  "type": "text",
  "keys": [
    ["name", "text"],
    ["description", "text"]
  ],
  "options": {
    "weights": {"name": 10, "description": 5}
  }
}
```

**Key Points:**
- **Requires** at least one field with `"text"` type in `keys`
- Field weights determine relevance (higher = more important)
- Supports multi-field text search
- Use `$text` query operator in MongoDB queries

**Example:**
```json
{
  "managed_indexes": {
    "products": [
      {
        "name": "product_fulltext",
        "type": "text",
        "keys": {
          "title": "text",
          "description": "text",
          "tags": "text"
        },
        "options": {
          "weights": {
            "title": 10,
            "tags": 5,
            "description": 2
          }
        }
      }
    ]
  }
}
```

### Geospatial Indexes

Indexes for location-based queries (2dsphere, 2d, geoHaystack).

**Format:**
```json
{
  "name": "location_index",
  "type": "geospatial",
  "keys": {
    "location": "2dsphere"
  }
}
```

**Alternative array format:**
```json
{
  "name": "location_index",
  "type": "geospatial",
  "keys": [
    ["location", "2dsphere"]
  ]
}
```

**Key Points:**
- **Requires** at least one field with geospatial type: `"2dsphere"`, `"2d"`, or `"geoHaystack"`
- `2dsphere` is most common (supports GeoJSON and legacy coordinates)
- Fields should contain coordinates: `[longitude, latitude]` or GeoJSON objects
- Use `$near`, `$geoWithin`, `$geoIntersects` operators in queries

**Example:**
```json
{
  "managed_indexes": {
    "places": [
      {
        "name": "location_2dsphere",
        "type": "geospatial",
        "keys": {
          "coordinates": "2dsphere"
        }
      }
    ]
  }
}
```

### Vector Search Indexes

Atlas Vector Search indexes for semantic similarity queries.

**Format:**
```json
{
  "name": "embedding_index",
  "type": "vectorSearch",
  "definition": {
    "fields": [
      {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 384,
        "similarity": "cosine"
      },
      {
        "type": "filter",
        "path": "experiment_id"
      }
    ]
  }
}
```

**Key Points:**
- **Requires** `definition` object (not `keys`)
- Vector fields require `numDimensions` (1-10000, typically 128-1536)
- Supported similarity metrics: `"cosine"`, `"euclidean"`, `"dotProduct"`
- Filter fields enable pre-filtering by scalar fields
- Always include `experiment_id` as a filter field for scoping

**Example:**
```json
{
  "managed_indexes": {
    "documents": [
      {
        "name": "document_embeddings",
        "type": "vectorSearch",
        "definition": {
          "fields": [
            {
              "type": "vector",
              "path": "embedding_vector",
              "numDimensions": 1536,
              "similarity": "cosine"
            },
            {
              "type": "filter",
              "path": "experiment_id"
            },
            {
              "type": "filter",
              "path": "category"
            }
          ]
        }
      }
    ]
  }
}
```

### Atlas Search Indexes (Lucene)

Atlas Search indexes for full-text search with Lucene.

**Format:**
```json
{
  "name": "fulltext_search",
  "type": "search",
  "definition": {
    "mappings": {
      "dynamic": false,
      "fields": {
        "title": {
          "type": "string",
          "analyzer": "lucene.standard"
        },
        "description": {
          "type": "string",
          "analyzer": "lucene.english"
        }
      }
    }
  }
}
```

**Key Points:**
- **Requires** `definition` object with `mappings`
- More flexible than standard text indexes
- Supports advanced analyzers, faceting, and highlighting
- Use `$search` aggregation stage in queries

## Best Practices

### 1. Index Naming Conventions

- Use descriptive names: `product_sku_unique`, `user_email_text`, `location_2dsphere`
- Include collection context: `users_email_index` (not just `email_index`)
- Use lowercase with underscores: `my_index_name` (not `MyIndexName` or `my-index-name`)
- Index names are automatically prefixed with `{slug}_`, so use base names only

### 2. Index Key Formats

**Prefer object format for clarity:**
```json
"keys": {"field1": 1, "field2": -1}
```

**Use array format only when needed:**
```json
"keys": [["field1", 1], ["field2", -1]]
```

### 3. Compound Indexes

- Put equality fields first, then range/sort fields
- Limit to 3-4 fields per compound index (MongoDB best practice)
- Consider query patterns when ordering fields

**Good example:**
```json
{
  "name": "products_category_price",
  "type": "regular",
  "keys": {
    "category": 1,      // Equality filter first
    "price": -1,        // Sort field second
    "in_stock": 1       // Additional filter third
  }
}
```

### 4. Vector Search Dimensions

- Common dimensions: 128, 384, 512, 768, 1536
- Match your embedding model's output dimension
- Validate `numDimensions` is between 1 and 10000

### 5. TTL Index Considerations

- Don't set TTL too short (< 60 seconds) - MongoDB cleanup runs every 60s
- Don't set TTL too long (> 1 year) - consider if you really need that retention
- Use meaningful field names: `created_at`, `expires_at`, `timestamp`

### 6. Partial Index Filters

- Keep filter expressions simple and focused
- Test filter queries before using in partial indexes
- Document why a partial index is needed

## Debugging Tips

### 1. Validation Error Messages

When validation fails, you'll see:
```
Manifest validation failed: Field 'managed_indexes.products[0].keys': Invalid format
```

**How to read:**
- `managed_indexes.products[0]` = First index in `products` collection
- Check the field path to locate the exact issue

### 2. Common Schema Errors

**Error: "Field 'X': Invalid format"**
- Check the type matches what's expected (object vs array vs string)
- Verify enum values match exactly (case-sensitive)

**Error: "Missing required field 'X'"**
- Index type-specific requirements:
  - `regular`, `text`, `geospatial`, `ttl`, `partial`: Require `keys`
  - `vectorSearch`, `search`: Require `definition`
  - `ttl`: Also requires `options.expireAfterSeconds`
  - `partial`: Also requires `options.partialFilterExpression`

**Error: "numDimensions must be between 1 and 10000"**
- Vector search dimension is out of range
- Check your embedding model's output dimension

**Error: "expireAfterSeconds too large"**
- TTL value exceeds 1 year (31536000 seconds)
- Consider if such a long TTL is needed

### 3. Testing Manifests Locally

Before committing, validate your manifest:

```python
# Quick validation script
from manifest_schema import validate_manifest
import json

with open('experiments/my_experiment/manifest.json') as f:
    data = json.load(f)

is_valid, error, paths = validate_manifest(data)
if is_valid:
    print("✅ Manifest is valid!")
else:
    print(f"❌ Validation failed: {error}")
    print(f"Errors in: {', '.join(paths)}")
```

### 4. Checking Index Status

After registration, check if indexes were created:
- Admin panel: `/admin/api/index-status/{slug_id}`
- Logs: Look for `✔️ Created` messages
- MongoDB: Query the database directly

### 5. Understanding Error Paths

Error paths use dot notation:
- `managed_indexes.products[0].keys` = First index in products collection, keys field
- `collection_settings.sessions.validation` = Sessions collection, validation settings
- `status` = Top-level status field

Use these paths to quickly locate issues in your manifest.

## Common Issues & Solutions

### Issue: "Additional properties not allowed"

**Cause:** You removed `additionalProperties: True` from schema (this is expected)

**Solution:** Remove extra fields or add them to the schema definition. The schema allows additional properties by default.

### Issue: "keys format mismatch"

**Cause:** Mixed object and array formats, or invalid key structure

**Solution:**
- Use consistent format: object `{"field": 1}` or array `[["field", 1]]`
- Don't mix formats in the same index
- Ensure array format has exactly 2 elements per tuple: `[field_name, direction]`

### Issue: "Vector search dimension validation failed"

**Cause:** `numDimensions` is 0, negative, or > 10000

**Solution:**
- Check your embedding model's actual output dimension
- Common values: 384, 768, 1536
- Ensure it's a positive integer in the valid range

### Issue: "TTL index missing expireAfterSeconds"

**Cause:** TTL index declared but no expiration time specified

**Solution:**
```json
{
  "type": "ttl",
  "keys": {"created_at": 1},
  "options": {
    "expireAfterSeconds": 3600  // REQUIRED for TTL indexes
  }
}
```

### Issue: "Partial index missing partialFilterExpression"

**Cause:** Partial index type used but no filter expression

**Solution:**
```json
{
  "type": "partial",
  "keys": {"user_id": 1},
  "options": {
    "partialFilterExpression": {"active": true}  // REQUIRED for partial indexes
  }
}
```

### Issue: "Text index has no text fields"

**Cause:** Text index declared but no fields have `"text"` type

**Solution:**
```json
{
  "type": "text",
  "keys": {
    "description": "text"  // Must have at least one "text" type field
  }
}
```

### Issue: "Geospatial index has no geospatial fields"

**Cause:** Geospatial index declared but no fields have geospatial types

**Solution:**
```json
{
  "type": "geospatial",
  "keys": {
    "location": "2dsphere"  // Must have "2dsphere", "2d", or "geoHaystack"
  }
}
```

### Issue: Experiments not loading after validation

**Cause:** Validation blocking registration

**Solution:**
1. Check logs for validation errors
2. Fix the manifest.json according to error messages
3. Reload experiments: `/admin/api/reload-experiment/{slug_id}`

## Validation Flow

### 1. Save via Admin API

```
Admin saves manifest → Validate schema → If valid: Save to DB → Reload experiments
                     → If invalid: Return 400 error with details
```

### 2. Seed from Local Files

```
Startup scans /experiments → Read manifest.json → Validate schema → If valid: Insert to DB
                           → If invalid: Log error, skip experiment
```

### 3. Upload ZIP File

```
Upload ZIP → Extract manifest.json → Validate schema → If valid: Save to DB → Register
           → If invalid: Return 400 error, don't save
```

### 4. Experiment Registration

```
Load from DB → Validate schema → If valid: Register routes/actors/indexes
             → If invalid: Skip experiment, log error, continue with others
```

**Important:** Validation failures during registration are **non-blocking** for other experiments. The failing experiment is skipped, but others continue to load.

## Complete Example

Here's a complete manifest.json showcasing all features:

```json
{
  "slug": "product_catalog",
  "name": "Product Catalog",
  "description": "E-commerce product catalog with search and recommendations",
  "status": "active",
  "auth_required": false,
  "data_scope": ["self"],
  "managed_indexes": {
    "products": [
      {
        "name": "product_sku_unique",
        "type": "regular",
        "keys": {"sku": 1},
        "options": {"unique": true}
      },
      {
        "name": "product_category_price",
        "type": "regular",
        "keys": [
          ["category", 1],
          ["price", -1]
        ]
      },
      {
        "name": "product_search",
        "type": "text",
        "keys": {
          "name": "text",
          "description": "text",
          "tags": "text"
        },
        "options": {
          "weights": {
            "name": 10,
            "tags": 5,
            "description": 2
          },
          "default_language": "english"
        }
      },
      {
        "name": "product_location",
        "type": "geospatial",
        "keys": {
          "location": "2dsphere"
        }
      }
    ],
    "sessions": [
      {
        "name": "session_expiry",
        "type": "ttl",
        "keys": {"created_at": 1},
        "options": {"expireAfterSeconds": 3600}
      },
      {
        "name": "active_session_user",
        "type": "partial",
        "keys": {
          "user_id": 1,
          "session_id": 1
        },
        "options": {
          "unique": true,
          "partialFilterExpression": {"active": true}
        }
      }
    ],
    "embeddings": [
      {
        "name": "product_embeddings",
        "type": "vectorSearch",
        "definition": {
          "fields": [
            {
              "type": "vector",
              "path": "embedding_vector",
              "numDimensions": 384,
              "similarity": "cosine"
            },
            {
              "type": "filter",
              "path": "experiment_id"
            },
            {
              "type": "filter",
              "path": "category"
            }
          ]
        }
      }
    ]
  },
  "collection_settings": {
    "products": {
      "validation": {
        "validator": {
          "$jsonSchema": {
            "required": ["name", "price"],
            "properties": {
              "name": {"bsonType": "string"},
              "price": {"bsonType": "decimal"}
            }
          }
        },
        "validationLevel": "moderate",
        "validationAction": "warn"
      }
    }
  }
}
```

## Quick Reference

### Index Type Requirements

| Type | Requires `keys` | Requires `definition` | Additional Requirements |
|------|----------------|---------------------|------------------------|
| `regular` | ✅ | ❌ | None |
| `text` | ✅ (with `"text"` type) | ❌ | At least one field with `"text"` type |
| `geospatial` | ✅ (with geo type) | ❌ | At least one field with `"2dsphere"`, `"2d"`, or `"geoHaystack"` |
| `ttl` | ✅ | ❌ | `options.expireAfterSeconds` (1-31536000) |
| `partial` | ✅ | ❌ | `options.partialFilterExpression` |
| `vectorSearch` | ❌ | ✅ | `definition.fields` with vector field, `numDimensions` (1-10000) |
| `search` | ❌ | ✅ | `definition.mappings` |

### Key Formats

- **Object format:** `{"field": direction}` where direction is `1`, `-1`, or a string like `"text"`, `"2dsphere"`
- **Array format:** `[["field", direction], ...]` for compound indexes
- **Text/Geospatial:** Must explicitly use string type: `"text"`, `"2dsphere"`, `"2d"`, `"geoHaystack"`

### Validation Checklist

Before saving your manifest:

- [ ] All index names are lowercase with underscores
- [ ] `regular`/`text`/`geospatial`/`ttl`/`partial` indexes have `keys`
- [ ] `vectorSearch`/`search` indexes have `definition`
- [ ] TTL indexes have `expireAfterSeconds` in options
- [ ] Partial indexes have `partialFilterExpression` in options
- [ ] Text indexes have at least one `"text"` type field
- [ ] Geospatial indexes have at least one geo type field
- [ ] Vector search has valid `numDimensions` (1-10000)
- [ ] No `_id` field in regular index keys
- [ ] All enum values match exactly (case-sensitive)

## Need Help?

1. **Check logs** - Validation errors include field paths
2. **Test locally** - Use the validation script above
3. **Check examples** - See `experiments/indexing_demo/manifest.json` for all index types
4. **Review error paths** - Use dot notation to locate exact issues

