"""
Experiment Database Wrapper (experiment_db.py)
================================================================================

A MongoDB-style database abstraction layer for experiments.
Follows MongoDB API conventions for familiarity and ease of use.

This module provides an easy-to-use API that matches MongoDB's API closely,
so experiments can use familiar MongoDB methods. All operations automatically
handle experiment scoping and indexing behind the scenes.

Usage:
    from experiment_db import ExperimentDB
    from core_deps import get_scoped_db
    
    @bp.get("/")
    async def my_route(db: ExperimentDB = Depends(get_experiment_db)):
        # MongoDB-style operations - familiar API!
        doc = await db.my_collection.find_one({"_id": "doc_123"})
        docs = await db.my_collection.find({"status": "active"}).to_list(length=10)
        await db.my_collection.insert_one({"name": "Test"})
        count = await db.my_collection.count_documents({})
"""

import logging
from typing import Optional, Dict, Any, List, Union, Tuple

try:
    from async_mongo_wrapper import ScopedMongoWrapper
except ImportError:
    ScopedMongoWrapper = None
    logging.warning("Failed to import ScopedMongoWrapper. ExperimentDB will not function.")

try:
    from motor.motor_asyncio import AsyncIOMotorCursor
    from pymongo.results import InsertOneResult, InsertManyResult, UpdateResult, DeleteResult
except ImportError:
    AsyncIOMotorCursor = None
    InsertOneResult = None
    InsertManyResult = None
    UpdateResult = None
    DeleteResult = None
    logging.warning("Failed to import Motor types. Type hints may not work correctly.")

logger = logging.getLogger(__name__)


class Collection:
    """
    A MongoDB collection wrapper that follows MongoDB API conventions.
    
    This class wraps a ScopedCollectionWrapper and provides MongoDB-style methods
    that match the familiar Motor/pymongo API. All operations automatically handle
    experiment scoping and indexing.
    
    Example:
        collection = Collection(scoped_wrapper.my_collection)
        doc = await collection.find_one({"_id": "doc_123"})
        docs = await collection.find({"status": "active"}).to_list(length=10)
        await collection.insert_one({"name": "Test"})
        count = await collection.count_documents({})
    """
    
    def __init__(self, scoped_collection):
        """
        Initialize a Collection wrapper around a ScopedCollectionWrapper.
        
        Args:
            scoped_collection: A ScopedCollectionWrapper instance
        """
        self._collection = scoped_collection
    
    async def find_one(
        self,
        filter: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Find a single document matching the filter.
        
        This matches MongoDB's find_one() API exactly.
        
        Args:
            filter: Optional dict of field/value pairs to filter by
                   Example: {"_id": "doc_123"}, {"status": "active"}
            *args, **kwargs: Additional arguments passed to find_one()
                            (e.g., projection, sort, etc.)
        
        Returns:
            The document as a dict, or None if not found
        
        Example:
            doc = await collection.find_one({"_id": "doc_123"})
            doc = await collection.find_one({"status": "active"})
        """
        try:
            return await self._collection.find_one(filter or {}, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in find_one: {e}", exc_info=True)
            return None
    
    def find(
        self,
        filter: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs
    ) -> AsyncIOMotorCursor:
        """
        Find documents matching the filter.
        
        This matches MongoDB's find() API exactly. Returns a cursor
        that you can iterate or call .to_list() on.
        
        Args:
            filter: Optional dict of field/value pairs to filter by
                   Example: {"status": "active"}, {"age": {"$gte": 18}}
            *args, **kwargs: Additional arguments passed to find()
                            (e.g., projection, sort, limit, skip, etc.)
        
        Returns:
            AsyncIOMotorCursor that can be iterated or converted to list
        
        Example:
            cursor = collection.find({"status": "active"})
            docs = await cursor.to_list(length=10)
            
            # Or with sort, limit
            cursor = collection.find({"status": "active"}).sort("created_at", -1).limit(10)
            docs = await cursor.to_list(length=None)
        """
        return self._collection.find(filter or {}, *args, **kwargs)
    
    async def insert_one(
        self,
        document: Dict[str, Any],
        *args,
        **kwargs
    ) -> InsertOneResult:
        """
        Insert a single document.
        
        This matches MongoDB's insert_one() API exactly.
        
        Args:
            document: The document to insert
            *args, **kwargs: Additional arguments passed to insert_one()
        
        Returns:
            InsertOneResult with inserted_id
        
        Example:
            result = await collection.insert_one({"name": "Test"})
            print(result.inserted_id)
        """
        try:
            return await self._collection.insert_one(document, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in insert_one: {e}", exc_info=True)
            raise
    
    async def insert_many(
        self,
        documents: List[Dict[str, Any]],
        *args,
        **kwargs
    ) -> InsertManyResult:
        """
        Insert multiple documents at once.
        
        This matches MongoDB's insert_many() API exactly.
        
        Args:
            documents: List of documents to insert
            *args, **kwargs: Additional arguments passed to insert_many()
                            (e.g., ordered=True/False)
        
        Returns:
            InsertManyResult with inserted_ids
        
        Example:
            result = await collection.insert_many([{"name": "A"}, {"name": "B"}])
            print(result.inserted_ids)
        """
        try:
            return await self._collection.insert_many(documents, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in insert_many: {e}", exc_info=True)
            raise
    
    async def update_one(
        self,
        filter: Dict[str, Any],
        update: Dict[str, Any],
        *args,
        **kwargs
    ) -> UpdateResult:
        """
        Update a single document matching the filter.
        
        This matches MongoDB's update_one() API exactly.
        
        Args:
            filter: Dict of field/value pairs to match documents
                   Example: {"_id": "doc_123"}
            update: Update operations (e.g., {"$set": {...}}, {"$inc": {...}})
            *args, **kwargs: Additional arguments passed to update_one()
                            (e.g., upsert=True/False)
        
        Returns:
            UpdateResult with modified_count and upserted_id
        
        Example:
            result = await collection.update_one(
                {"_id": "doc_123"},
                {"$set": {"status": "active"}}
            )
        """
        try:
            return await self._collection.update_one(filter, update, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in update_one: {e}", exc_info=True)
            raise
    
    async def update_many(
        self,
        filter: Dict[str, Any],
        update: Dict[str, Any],
        *args,
        **kwargs
    ) -> UpdateResult:
        """
        Update multiple documents matching the filter.
        
        This matches MongoDB's update_many() API exactly.
        
        Args:
            filter: Dict of field/value pairs to match documents
            update: Update operations (e.g., {"$set": {...}}, {"$inc": {...}})
            *args, **kwargs: Additional arguments passed to update_many()
                            (e.g., upsert=True/False)
        
        Returns:
            UpdateResult with modified_count
        
        Example:
            result = await collection.update_many(
                {"status": "pending"},
                {"$set": {"status": "active"}}
            )
        """
        try:
            return await self._collection.update_many(filter, update, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in update_many: {e}", exc_info=True)
            raise
    
    async def delete_one(
        self,
        filter: Dict[str, Any],
        *args,
        **kwargs
    ) -> DeleteResult:
        """
        Delete a single document matching the filter.
        
        This matches MongoDB's delete_one() API exactly.
        
        Args:
            filter: Dict of field/value pairs to match documents
                   Example: {"_id": "doc_123"}
            *args, **kwargs: Additional arguments passed to delete_one()
        
        Returns:
            DeleteResult with deleted_count
        
        Example:
            result = await collection.delete_one({"_id": "doc_123"})
            print(result.deleted_count)
        """
        try:
            return await self._collection.delete_one(filter, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in delete_one: {e}", exc_info=True)
            raise
    
    async def delete_many(
        self,
        filter: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs
    ) -> DeleteResult:
        """
        Delete multiple documents matching the filter.
        
        This matches MongoDB's delete_many() API exactly.
        
        Args:
            filter: Optional dict of field/value pairs to match documents
            *args, **kwargs: Additional arguments passed to delete_many()
        
        Returns:
            DeleteResult with deleted_count
        
        Example:
            result = await collection.delete_many({"status": "deleted"})
            print(result.deleted_count)
        """
        try:
            return await self._collection.delete_many(filter or {}, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in delete_many: {e}", exc_info=True)
            raise
    
    async def count_documents(
        self,
        filter: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs
    ) -> int:
        """
        Count documents matching the filter.
        
        This matches MongoDB's count_documents() API exactly.
        
        Args:
            filter: Optional dict of field/value pairs to filter by
            *args, **kwargs: Additional arguments passed to count_documents()
                            (e.g., limit, skip)
        
        Returns:
            Number of matching documents
        
        Example:
            count = await collection.count_documents({"status": "active"})
            count = await collection.count_documents({})  # Count all
        """
        try:
            return await self._collection.count_documents(filter or {}, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in count_documents: {e}", exc_info=True)
            return 0
    
    def aggregate(
        self,
        pipeline: List[Dict[str, Any]],
        *args,
        **kwargs
    ) -> AsyncIOMotorCursor:
        """
        Perform aggregation pipeline.
        
        This matches MongoDB's aggregate() API exactly.
        
        Args:
            pipeline: List of aggregation stages
            *args, **kwargs: Additional arguments passed to aggregate()
        
        Returns:
            AsyncIOMotorCursor for iterating results
        
        Example:
            pipeline = [
                {"$match": {"status": "active"}},
                {"$group": {"_id": "$category", "count": {"$sum": 1}}}
            ]
            cursor = collection.aggregate(pipeline)
            results = await cursor.to_list(length=None)
        """
        return self._collection.aggregate(pipeline, *args, **kwargs)


class ExperimentDB:
    """
    A MongoDB-style database interface for experiments.
    
    This class wraps ScopedMongoWrapper and provides MongoDB-style methods
    that match the familiar Motor/pymongo API. All operations automatically
    handle experiment scoping and indexing.
    
    Example:
        from experiment_db import ExperimentDB
        from core_deps import get_scoped_db
        
        @bp.get("/")
        async def my_route(db: ExperimentDB = Depends(get_experiment_db)):
            # MongoDB-style operations - familiar API!
            doc = await db.users.find_one({"_id": "user_123"})
            docs = await db.users.find({"status": "active"}).to_list(length=10)
            await db.users.insert_one({"name": "John", "email": "john@example.com"})
            count = await db.users.count_documents({})
    """
    
    def __init__(self, scoped_wrapper: ScopedMongoWrapper):
        """
        Initialize ExperimentDB with a ScopedMongoWrapper.
        
        Args:
            scoped_wrapper: A ScopedMongoWrapper instance from core_deps.get_scoped_db
        """
        if not ScopedMongoWrapper:
            raise RuntimeError("ScopedMongoWrapper is not available. Check imports.")
        
        self._wrapper = scoped_wrapper
        self._collection_cache: Dict[str, Collection] = {}
    
    def collection(self, name: str) -> Collection:
        """
        Get a Collection wrapper for a collection by name.
        
        Args:
            name: The collection name (base name, without experiment prefix)
                 Example: "users", "products", "orders"
        
        Returns:
            A Collection instance for easy database operations
        
        Example:
            users = db.collection("users")
            doc = await users.get("user_123")
        """
        if name in self._collection_cache:
            return self._collection_cache[name]
        
        # Get the scoped collection from wrapper
        scoped_collection = getattr(self._wrapper, name)
        
        # Create and cache Collection wrapper
        collection = Collection(scoped_collection)
        self._collection_cache[name] = collection
        
        return collection
    
    def __getattr__(self, name: str) -> Collection:
        """
        Allow direct access to collections as attributes.
        
        Example:
            db.users.get("user_123")  # Instead of db.collection("users").get("user_123")
        """
        # Only proxy collection names, not internal attributes
        if name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        return self.collection(name)
    
    @property
    def raw(self) -> ScopedMongoWrapper:
        """
        Access the underlying ScopedMongoWrapper for advanced operations.
        
        Use this if you need to access MongoDB-specific features that aren't
        covered by the simple API. For most cases, you won't need this.
        
        Example:
            # Advanced aggregation
            pipeline = [{"$match": {...}}, {"$group": {...}}]
            results = await db.raw.my_collection.aggregate(pipeline).to_list(None)
        """
        return self._wrapper


# FastAPI dependency helper
async def get_experiment_db(request) -> ExperimentDB:
    """
    FastAPI Dependency: Provides an ExperimentDB instance.
    
    This is a convenience wrapper around get_scoped_db that returns
    an ExperimentDB instance instead of ScopedMongoWrapper.
    
    Usage:
        from experiment_db import get_experiment_db
        
        @bp.get("/")
        async def my_route(db: ExperimentDB = Depends(get_experiment_db)):
            doc = await db.users.get("user_123")
    """
    from core_deps import get_scoped_db
    
    scoped_db = await get_scoped_db(request)
    return ExperimentDB(scoped_db)

