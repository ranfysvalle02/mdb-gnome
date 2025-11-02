# Database Abstraction Usage Guidelines

This document describes the recommended patterns for using the database abstraction layer in the modular experiments lab ecosystem.

## Overview

The ecosystem provides a **magical database abstraction** (`ExperimentDB`) that handles:
- Automatic experiment scoping (data isolation)
- Automatic index management (performance optimization)
- Connection pool management (efficiency)
- MongoDB-style API (familiar interface)

## Key Components

### 1. `ExperimentDB` Class (`experiment_db.py`)
   - High-level MongoDB-style database interface
   - Wraps `ScopedMongoWrapper` with a familiar Motor/pymongo API
   - Use this for **all database operations** in routes and actors

### 2. `create_actor_database()` Factory (`experiment_db.py`)
   - Factory function for Ray actors
   - Handles connection pooling and scoping automatically
   - Returns an `ExperimentDB` instance
   - **Always use this in Ray actors** for database initialization

### 3. `get_experiment_db()` Dependency (`core_deps.py`)
   - FastAPI dependency for routes
   - Returns an `ExperimentDB` instance
   - Use this when **routes need direct database access**

## Recommended Patterns

### Pattern 1: FastAPI Routes (Thin Client Pattern)

Routes typically delegate all database operations to Ray actors. In this case, **no database dependency is needed**:

```python
# ✅ GOOD: Routes that only delegate to actors
from fastapi import APIRouter, Depends, Request
from ray.actor import ActorHandle

bp = APIRouter()

async def get_actor_handle(request: Request) -> ActorHandle:
    """Get actor handle - no database dependency needed."""
    actor_name = f"{request.state.slug_id}-actor"
    return ray.get_actor(actor_name, namespace="modular_labs")

@bp.get("/")
async def my_route(actor: ActorHandle = Depends(get_actor_handle)):
    """Delegate to actor - no database access here."""
    result = await actor.do_something.remote()
    return result
```

**If routes need direct database access** (less common), use `ExperimentDB`:

```python
# ✅ GOOD: Routes that need direct database access
from core_deps import get_experiment_db
from experiment_db import ExperimentDB

@bp.get("/")
async def my_route(db: ExperimentDB = Depends(get_experiment_db)):
    """Direct database access when needed."""
    doc = await db.my_collection.find_one({"_id": "doc_123"})
    count = await db.my_collection.count_documents({})
    return {"doc": doc, "count": count}
```

**❌ BAD: Don't use `ScopedMongoWrapper` directly in routes** (unless you have a very specific advanced use case):

```python
# ❌ AVOID: Using ScopedMongoWrapper directly in routes
from core_deps import get_scoped_db
from async_mongo_wrapper import ScopedMongoWrapper

@bp.get("/")
async def my_route(db: ScopedMongoWrapper = Depends(get_scoped_db)):
    # This works, but ExperimentDB is recommended for consistency
    pass
```

### Pattern 2: Ray Actors (Headless Server Pattern)

**Always use `create_actor_database()` in actors**:

```python
# ✅ GOOD: Recommended pattern for all actors
import ray
from experiment_db import create_actor_database

@ray.remote
class ExperimentActor:
    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: list[str]):
        # Magical database abstraction - one line!
        self.db = create_actor_database(
            mongo_uri,
            db_name,
            write_scope,
            read_scopes
        )
        
        self.write_scope = write_scope
        self.read_scopes = read_scopes
    
    async def do_something(self):
        # MongoDB-style API - familiar and performant!
        doc = await self.db.my_collection.find_one({"_id": "doc_123"})
        docs = await self.db.my_collection.find({"status": "active"}).to_list(length=10)
        await self.db.my_collection.insert_one({"name": "Test"})
        count = await self.db.my_collection.count_documents({})
        return count
```

### Pattern 3: Cross-Experiment Data Access

When an experiment needs to read data from another experiment (via `data_scope` configuration):

```python
# ✅ GOOD: Cross-experiment read access
async def fetch_cross_experiment_data(self):
    # Use raw.get_collection() with the fully prefixed collection name
    if "other_experiment" in self.read_scopes:
        other_collection = self.db.raw.get_collection("other_experiment_collection_name")
        count = await other_collection.count_documents({})
        return count
```

## API Examples

### Basic CRUD Operations

```python
# Find one document
doc = await db.my_collection.find_one({"_id": "doc_123"})

# Find multiple documents
cursor = db.my_collection.find({"status": "active"})
docs = await cursor.sort("created_at", -1).limit(10).to_list(length=None)

# Insert one document
result = await db.my_collection.insert_one({"name": "Test", "status": "active"})
print(result.inserted_id)

# Insert multiple documents
docs = [{"name": "A"}, {"name": "B"}]
result = await db.my_collection.insert_many(docs)
print(result.inserted_ids)

# Update one document
result = await db.my_collection.update_one(
    {"_id": "doc_123"},
    {"$set": {"status": "active"}}
)

# Update multiple documents
result = await db.my_collection.update_many(
    {"status": "pending"},
    {"$set": {"status": "active"}}
)

# Delete one document
result = await db.my_collection.delete_one({"_id": "doc_123"})

# Delete multiple documents
result = await db.my_collection.delete_many({"status": "deleted"})

# Count documents
count = await db.my_collection.count_documents({"status": "active"})
```

### Aggregation Pipeline

```python
# Standard aggregation
pipeline = [
    {"$match": {"status": "active"}},
    {"$group": {"_id": "$category", "count": {"$sum": 1}}}
]
cursor = db.my_collection.aggregate(pipeline)
results = await cursor.to_list(length=None)

# Vector search aggregation (Atlas Search)
pipeline = [
    {
        "$vectorSearch": {
            "index": "my_vector_index",
            "path": "embedding",
            "queryVector": query_vector,
            "numCandidates": 50,
            "limit": 10,
            "filter": {"status": "active"}
        }
    },
    {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}}}
]
# Note: For vector search, use db.raw.my_collection.aggregate()
cursor = db.raw.my_collection.aggregate(pipeline)
results = await cursor.to_list(length=None)
```

### Advanced Operations

```python
# Access underlying ScopedMongoWrapper for advanced operations
# (e.g., vector search, raw aggregation pipelines)
cursor = db.raw.my_collection.aggregate(complex_pipeline)

# Access underlying AsyncIOMotorDatabase for administrative operations
real_db = db.database
index_manager = AsyncAtlasIndexManager(real_db["my_collection"])

# Use collection's index manager for index operations
index_manager = db.my_collection.index_manager
await index_manager.create_search_index("my_index", definition={...})
```

## Performance Best Practices

1. **Automatic Index Management**: The abstraction automatically creates indexes based on query patterns. No manual index configuration needed!

2. **Connection Pooling**: Actors use shared connection pools via `mongo_connection_pool.py`. Efficient and performant.

3. **Experiment Scoping**: All queries are automatically scoped to the experiment, ensuring data isolation and security.

4. **Query Optimization**: Use projections to limit data transfer:
   ```python
   doc = await db.my_collection.find_one(
       {"_id": "doc_123"},
       {"name": 1, "status": 1}  # Only fetch these fields
   )
   ```

## Summary

- **Routes**: Use `ExperimentDB` via `get_experiment_db()` if routes need database access, otherwise omit database dependency
- **Actors**: Always use `create_actor_database()` for initialization
- **All Operations**: Use `ExperimentDB`'s MongoDB-style API for consistency and performance
- **Cross-Experiment**: Use `db.raw.get_collection()` with fully prefixed collection names
- **Advanced**: Use `db.raw` or `db.database` for advanced operations when needed

The database abstraction handles all the "magic" for you - experiment scoping, index management, and connection pooling are automatic!

