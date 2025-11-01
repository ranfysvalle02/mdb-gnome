"""
Asynchronous MongoDB Scoped Wrapper (async_mongo_wrapper.py)
================================================================================

Provides an asynchronous, experiment-scoped proxy wrapper around Motor's
`AsyncIOMotorDatabase` and `AsyncIOMotorCollection` objects.

Core Features:
- `ScopedMongoWrapper`: Proxies a database. When a collection is
  accessed (e.g., `db.my_collection`), it returns a `ScopedCollectionWrapper`.
- `ScopedCollectionWrapper`: Proxies a collection, automatically injecting
  `experiment_id` filters into all read operations (find, aggregate, count)
  and adding the `experiment_id` to all write operations (insert).
- `AsyncAtlasIndexManager`: Provides an async-native interface for managing
  both standard MongoDB indexes and Atlas Search/Vector indexes. This
  manager is available via `collection_wrapper.index_manager` and
  operates on the *unscoped* collection for administrative purposes.

This design ensures data isolation between experiments while providing
a familiar (Motor-like) developer experience.
"""
import time
import logging
import asyncio
from typing import (
    Optional, List, Mapping, Any, Dict, Union, Tuple, ClassVar
)
from motor.motor_asyncio import (
    AsyncIOMotorDatabase,
    AsyncIOMotorCollection,
    AsyncIOMotorCursor
)
from pymongo.results import (
    InsertOneResult,
    InsertManyResult,
    UpdateResult,
    DeleteResult
)
from pymongo.operations import SearchIndexModel
from pymongo.errors import OperationFailure, CollectionInvalid
from pymongo import ASCENDING, DESCENDING, TEXT

# --- FIX: Configure logger *before* first use ---
logger = logging.getLogger(__name__)
# --- END FIX ---

# --- ROBUST IMPORT FIX ---
# Try to import GEO2DSPHERE, which fails in some environments.
# If it fails, define it manually as it's a stable string constant.
try:
    from pymongo import GEO2DSPHERE
except ImportError:
    logger.warning("Could not import GEO2DSPHERE from pymongo. Defining manually.")
    GEO2DSPHERE = "2dsphere"
# --- END FIX ---


# ##########################################################################
# ASYNCHRONOUS ATLAS INDEX MANAGER
# ##########################################################################

class AsyncAtlasIndexManager:
    """
    Manages MongoDB Atlas Search indexes (Vector & Lucene) and standard
    database indexes with an asynchronous (Motor-native) interface.

    This class is designed to be attached to a `ScopedCollectionWrapper`
    and operates directly on the underlying, *unscoped* `AsyncIOMotorCollection`,
    as index management is an administrative, collection-wide task.
    """
    
    __slots__ = ('_collection',)

    # Class-level constants for polling
    DEFAULT_POLL_INTERVAL: ClassVar[int] = 5  # seconds
    DEFAULT_SEARCH_TIMEOUT: ClassVar[int] = 600  # 10 minutes
    DEFAULT_DROP_TIMEOUT: ClassVar[int] = 300  # 5 minutes

    def __init__(self, real_collection: AsyncIOMotorCollection):
        """
        Initializes the manager with a direct reference to the
        real (unscoped) AsyncIOMotorCollection.
        """
        self._collection = real_collection

    # --- Atlas Search Index Methods (Vector & Lucene) ---

    async def create_search_index(
        self,
        name: str,
        definition: Dict[str, Any],
        index_type: str = "search",
        wait_for_ready: bool = True,
        timeout: int = DEFAULT_SEARCH_TIMEOUT
    ) -> bool:
        """
        Creates or updates an Atlas Search index.

        If the index already exists, it checks its status. If it's queryable,
        it returns True. If it's FAILED, it returns False.

        Args:
            name: The name for the new search index.
            definition: The Atlas Search index definition document.
            index_type: The type of search index ('search' or 'vectorSearch').
            wait_for_ready: If True, blocks asynchronously until the index
                            reports a 'queryable' status.
            timeout: Max seconds to wait for the index to become ready.

        Returns:
            True if the index was created (or already exists) and is queryable
            (if `wait_for_ready` is True).
            False if the index exists in a FAILED state.

        Raises:
            TimeoutError: If `wait_for_ready` is True and the index
                          does not become queryable within the timeout.
            Exception: For other database or build-level errors.
        """
        
        # --- 🚀 FIX: Handle 'Collection already exists' gracefully ---
        # Ensure the collection exists *before* trying to create an index on it.
        try:
            coll_name = self._collection.name
            await self._collection.database.create_collection(coll_name)
            logger.debug(f"Ensured collection '{coll_name}' exists.")
        except CollectionInvalid as e:
            # Catch the specific error raised by pymongo when the collection exists
            if "already exists" in str(e):
                logger.warning(f"Prerequisite collection '{coll_name}' already exists. Continuing index creation.")
                pass # This is the expected and harmless condition
            else:
                # Re-raise for other CollectionInvalid errors
                logger.error(f"Failed to ensure collection '{self._collection.name}' exists: {e}")
                raise Exception(f"Failed to create prerequisite collection '{self._collection.name}': {e}") from e
        except Exception as e:
            logger.error(f"Failed to ensure collection '{self._collection.name}' exists: {e}")
            # If we can't even create the collection, we must fail.
            raise Exception(f"Failed to create prerequisite collection '{self._collection.name}': {e}")
        # --- END FIX ---
            
        try:
            existing_index = await self.get_search_index(name)
            
            if existing_index:
                logger.info(f"Search index '{name}' already exists.")
                if existing_index.get("queryable"):
                    logger.info(f"Search index '{name}' is already queryable.")
                    return True
                if existing_index.get("status") == "FAILED":
                     logger.error(f"Search index '{name}' exists but is in a FAILED state. Please drop and recreate.")
                     return False
            else:
                logger.info(f"Creating new search index '{name}' of type '{index_type}'...")
                search_index_model = SearchIndexModel(
                    definition=definition,
                    name=name,
                    type=index_type
                )
                await self._collection.create_search_index(model=search_index_model)
                logger.info(f"Search index '{name}' build has been submitted.")

            if wait_for_ready:
                return await self._wait_for_search_index_ready(name, timeout)
            
            return True

        except OperationFailure as e:
            logger.error(f"OperationFailure during search index creation: {e}")
            raise
        except Exception as e:
            logger.error(f"An error occurred creating search index '{name}': {e}")
            raise

    async def get_search_index(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the status for a single Atlas Search index by name.

        Args:
            name: The name of the search index.

        Returns:
            A dictionary with the index information, or None if not found.
        """
        try:
            index_status_cursor = self._collection.list_search_indexes(name=name)
            # Efficiently get the first (and only) item from the cursor
            async for index_info in index_status_cursor:
                return index_info
            return None  # Not found
        except Exception as e:
            logger.error(f"Error retrieving search index '{name}': {e}")
            return None

    async def list_search_indexes(self) -> List[Dict[str, Any]]:
        """
        Lists all Atlas Search indexes for the collection.

        Returns:
            A list of index information dictionaries.
        """
        try:
            # .to_list(None) asynchronously exhausts the cursor
            return await self._collection.list_search_indexes().to_list(None)
        except Exception as e:
            logger.error(f"Error listing search indexes: {e}")
            return []

    async def drop_search_index(
        self,
        name: str,
        wait_for_drop: bool = True,
        timeout: int = DEFAULT_DROP_TIMEOUT
    ) -> bool:
        """
        Drops an Atlas Search index by name.

        Args:
            name: The name of the search index to drop.
            wait_for_drop: If True, blocks asynchronously until the index
                           is fully removed.
            timeout: Max seconds to wait for the index to be dropped.

        Returns:
            True if the index was dropped successfully or did not exist.
        """
        try:
            if not await self.get_search_index(name):
                logger.info(f"Search index '{name}' does not exist. Nothing to drop.")
                return True
                
            await self._collection.drop_search_index(name=name)
            logger.info(f"Submitted request to drop search index '{name}'.")

            if wait_for_drop:
                return await self._wait_for_search_index_drop(name, timeout)
            
            return True
        except OperationFailure as e:
            if "IndexNotFound" in str(e):
                logger.info(f"Search index '{name}' was already deleted.")
                return True
            logger.error(f"OperationFailure dropping search index '{name}': {e}")
            raise
        except Exception as e:
            logger.error(f"Error dropping search index '{name}': {e}")
            raise

    async def update_search_index(
        self,
        name: str,
        definition: Dict[str, Any],
        wait_for_ready: bool = True,
        timeout: int = DEFAULT_SEARCH_TIMEOUT
    ) -> bool:
        """
        Updates the definition of an existing Atlas Search index.
        This operation triggers a rebuild of the index.

        Args:
            name: The name of the index to update.
            definition: The new index definition document.
            wait_for_ready: If True, blocks asynchronously until the index
                            is 'queryable' after the update.
            timeout: Max seconds to wait.

        Returns:
            True if the update was successful and the index is ready (if waited).
        """
        try:
            logger.info(f"Updating search index '{name}'...")
            await self._collection.update_search_index(name=name, definition=definition)
            logger.info(f"Search index '{name}' update submitted. Rebuild initiated.")
            
            if wait_for_ready:
                return await self._wait_for_search_index_ready(name, timeout)
            
            return True
        except Exception as e:
            logger.error(f"Error updating search index '{name}': {e}")
            raise

    async def _wait_for_search_index_ready(self, name: str, timeout: int) -> bool:
        """Async polling helper for an index to become queryable."""
        start_time = time.time()
        logger.info(f"Waiting for search index '{name}' to become queryable...")
        
        while True:
            index_info = await self.get_search_index(name)
            status = None
            queryable = False
            
            if index_info:
                status = index_info.get("status")
                queryable = index_info.get("queryable")
                
                if queryable:
                    logger.info(f"Search index '{name}' is ready and queryable.")
                    return True
                
                if status == "FAILED":
                    logger.error(f"Search index '{name}' failed to build.")
                    raise Exception(f"Index build failed for '{name}'.")
                
                if status == "STALE":
                    logger.warning(f"Search index '{name}' is STALE. Rebuild may be in progress.")
                    
            elif time.time() - start_time > 15 and not index_info:
                 # Give it a few seconds to appear, but fail if it never does.
                 logger.error(f"Search index '{name}' not found after 15s. Build may have failed to start.")
                 raise Exception(f"Index '{name}' did not appear in index list.")

            if time.time() - start_time > timeout:
                logger.error(f"Timeout: Index '{name}' did not become ready within {timeout}s.")
                raise TimeoutError(f"Index '{name}' did not become ready within {timeout}s.")

            logger.debug(f"Polling for '{name}'. Current status: {status or 'NOT_FOUND'}. Queryable: {queryable}")
            await asyncio.sleep(self.DEFAULT_POLL_INTERVAL)

    async def _wait_for_search_index_drop(self, name: str, timeout: int) -> bool:
        """Async polling helper for an index to be dropped."""
        start_time = time.time()
        logger.info(f"Waiting for search index '{name}' to be dropped...")

        while True:
            index_info = await self.get_search_index(name)
            
            if not index_info:
                logger.info(f"Search index '{name}' has been successfully dropped.")
                return True

            if time.time() - start_time > timeout:
                logger.error(f"Timeout: Index '{name}' was not dropped within {timeout}s.")
                raise TimeoutError(f"Index '{name}' was not dropped within {timeout}s.")

            logger.debug(f"Polling for '{name}' drop. Still present.")
            await asyncio.sleep(self.DEFAULT_POLL_INTERVAL)

    # --- Regular Database Index Methods ---

    async def create_index(
        self,
        keys: Union[str, List[Tuple[str, Union[int, str]]]],
        **kwargs: Any
    ) -> str:
        """
        Creates a standard database index (e.g., single-field, compound).
        
        Args:
            keys: A single field name (str) for an ascending index,
                  or a list of (field, direction) tuples.
                  Direction can be ASCENDING (1), DESCENDING (-1),
                  TEXT, GEO2DSPHERE, etc.
            **kwargs: Additional index options (e.g., name, unique, sparse).

        Returns:
            The name of the created index.
        """
        if isinstance(keys, str):
            keys = [(keys, ASCENDING)]
            
        try:
            name = await self._collection.create_index(keys, **kwargs)
            logger.info(f"Successfully created regular index '{name}'.")
            return name
        except Exception as e:
            logger.error(f"Failed to create regular index with keys '{keys}': {e}")
            raise

    async def create_text_index(
        self,
        fields: List[str],
        weights: Optional[Dict[str, int]] = None,
        name: str = "text_index",
        **kwargs: Any
    ) -> str:
        """
        Helper method to create a standard text index.

        Args:
            fields: A list of field names to include in the text index.
            weights: An optional dictionary of field names to weights.
            name: The name for the index.
            **kwargs: Other options (e.g., default_language).

        Returns:
            The name of the created index.
        """
        keys = [(field, TEXT) for field in fields]
        if weights:
            kwargs["weights"] = weights
        if name:
            kwargs["name"] = name
            
        return await self.create_index(keys, **kwargs)

    async def create_geo_index(
        self,
        field: str,
        name: Optional[str] = None,
        **kwargs: Any
    ) -> str:
        """
        Helper method to create a 2dsphere (geospatial) index.

        Args:
            field: The field containing GeoJSON data.
            name: The name for the index. Defaults to '{field}_2dsphere'.
            **kwargs: Other index options.

        Returns:
            The name of the created index.
        """
        keys = [(field, GEO2DSPHERE)]
        if name:
            kwargs["name"] = name
        
        return await self.create_index(keys, **kwargs)

    async def drop_index(self, name: str):
        """
        Drops a standard database index by its name.

        Args:
            name: The name of the index to drop (e.g., 'email_1').
        """
        try:
            await self._collection.drop_index(name)
            logger.info(f"Successfully dropped regular index '{name}'.")
        except OperationFailure as e:
            if "index not found" in str(e).lower():
                logger.info(f"Regular index '{name}' does not exist. Nothing to drop.")
            else:
                logger.error(f"Failed to drop regular index '{name}': {e}")
                raise
        except Exception as e:
            logger.error(f"Failed to drop regular index '{name}': {e}")
            raise

    async def list_indexes(self) -> List[Dict[str, Any]]:
        """
        Lists all standard database indexes for the collection.

        Returns:
            A list of index specification dictionaries.
        """
        try:
            return await self._collection.list_indexes().to_list(None)
        except Exception as e:
            logger.error(f"Error listing regular indexes: {e}")
            return []

    async def get_index(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the specification for a single standard database index.

        Args:
            name: The name of the index.

        Returns:
            The index specification dictionary, or None if not found.
        """
        indexes = await self.list_indexes()
        for index in indexes:
            if index.get("name") == name:
                return index
        return None


# ##########################################################################
# SCOPED WRAPPER CLASSES
# ##########################################################################

class ScopedCollectionWrapper:
    """
    Wraps an `AsyncIOMotorCollection` to enforce experiment data scoping.

    This class intercepts all data access methods (find, insert, update, etc.)
    to automatically inject `experiment_id` filters and data.

    - Read operations (`find`, `find_one`, `count_documents`, `aggregate`) are
      filtered to only include documents matching the `read_scopes`.
    - Write operations (`insert_one`, `insert_many`) automatically add the
      `write_scope` as the document's `experiment_id`.
    
    Administrative methods (e.g., `drop_index`) are not proxied directly
    but are available via the `.index_manager` property.
    """
    
    # Use __slots__ for memory and speed optimization
    __slots__ = ('_collection', '_read_scopes', '_write_scope', '_index_manager')

    def __init__(
        self,
        real_collection: AsyncIOMotorCollection,
        read_scopes: List[str],
        write_scope: str
    ):
        self._collection = real_collection
        self._read_scopes = read_scopes
        self._write_scope = write_scope
        # Lazily instantiated and cached
        self._index_manager: Optional[AsyncAtlasIndexManager] = None

    @property
    def index_manager(self) -> AsyncAtlasIndexManager:
        """
        Gets the AsyncAtlasIndexManager for this collection.
        
        It is lazily-instantiated and cached on first access.
        
        Note: Index operations are administrative and are NOT
        scoped by 'experiment_id'. They apply to the
        entire underlying collection.
        """
        if self._index_manager is None:
            # Create and cache it.
            # Pass the *real* collection, not 'self', as indexes
            # are not scoped by experiment_id.
            self._index_manager = AsyncAtlasIndexManager(self._collection)
        return self._index_manager

    def _inject_read_filter(self, filter: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """
        Combines the user's filter with our mandatory scope filter.
        
        Optimization: If the user filter is empty, just return the scope filter.
        Otherwise, combine them robustly with $and.
        """
        scope_filter = {"experiment_id": {"$in": self._read_scopes}}
        
        # If filter is None or {}, just return the scope filter
        if not filter:
            return scope_filter
            
        # If filter exists, combine them robustly with $and
        return {"$and": [filter, scope_filter]}

    async def insert_one(
        self,
        document: Mapping[str, Any],
        *args,
        **kwargs
    ) -> InsertOneResult:
        """
        Injects the experiment_id before writing.
        
        Safety: Creates a copy of the document to avoid mutating the caller's data.
        """
        # Use dictionary spread to create a non-mutating copy
        doc_to_insert = {**document, 'experiment_id': self._write_scope}
        return await self._collection.insert_one(doc_to_insert, *args, **kwargs)

    async def insert_many(
        self,
        documents: List[Mapping[str, Any]],
        *args,
        **kwargs
    ) -> InsertManyResult:
        """
        Injects the experiment_id into all documents before writing.
        
        Safety: Uses a list comprehension to create copies of all documents,
        avoiding in-place mutation of the original list.
        """
        docs_to_insert = [
            {**doc, 'experiment_id': self._write_scope} for doc in documents
        ]
        return await self._collection.insert_many(docs_to_insert, *args, **kwargs)

    async def find_one(
        self,
        filter: Optional[Mapping[str, Any]] = None,
        *args,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Applies the read scope to the filter."""
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.find_one(scoped_filter, *args, **kwargs)

    def find(
        self,
        filter: Optional[Mapping[str, Any]] = None,
        *args,
        **kwargs
    ) -> AsyncIOMotorCursor:
        """
        Applies the read scope to the filter.
        Returns an async cursor, just like motor.
        """
        scoped_filter = self._inject_read_filter(filter)
        return self._collection.find(scoped_filter, *args, **kwargs)

    async def update_one(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
        *args,
        **kwargs
    ) -> UpdateResult:
        """
        Applies the read scope to the filter.
        Note: This only scopes the *filter*, not the update operation.
        """
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.update_one(scoped_filter, update, *args, **kwargs)

    async def update_many(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
        *args,
        **kwargs
    ) -> UpdateResult:
        """
        Applies the read scope to the filter.
        Note: This only scopes the *filter*, not the update operation.
        """
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.update_many(scoped_filter, update, *args, **kwargs)

    async def delete_one(
        self,
        filter: Mapping[str, Any],
        *args,
        **kwargs
    ) -> DeleteResult:
        """Applies the read scope to the filter."""
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.delete_one(scoped_filter, *args, **kwargs)

    async def delete_many(
        self,
        filter: Mapping[str, Any],
        *args,
        **kwargs
    ) -> DeleteResult:
        """Applies the read scope to the filter."""
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.delete_many(scoped_filter, *args, **kwargs)

    async def count_documents(
        self,
        filter: Optional[Mapping[str, Any]] = None,
        *args,
        **kwargs
    ) -> int:
        """Applies the read scope to the filter for counting."""
        scoped_filter = self._inject_read_filter(filter)
        return await self._collection.count_documents(scoped_filter, *args, **kwargs)

    def aggregate(  
        self,  
        pipeline: List[Dict[str, Any]],  
        *args,  
        **kwargs  
    ) -> AsyncIOMotorCursor:  
        """  
        Injects a scope filter into the pipeline. For normal pipelines, we prepend   
        a $match stage. However, if the first stage is $vectorSearch, we embed   
        the read_scope filter into its 'filter' property, because $vectorSearch must   
        remain the very first stage in Atlas.  
        """  
        if not pipeline:  
            # No stages given, just prepend our $match  
            scope_match_stage = {  
                "$match": {"experiment_id": {"$in": self._read_scopes}}  
            }  
            pipeline = [scope_match_stage]  
            return self._collection.aggregate(pipeline, *args, **kwargs)  
    
        # Identify the first stage  
        first_stage = pipeline[0]  
        first_stage_op = next(iter(first_stage.keys()), None)  # e.g. "$match", "$vectorSearch", etc.  
    
        if first_stage_op == "$vectorSearch":  
            # We must not prepend $match or it breaks the pipeline.  
            # Instead, embed our scope in the 'filter' of $vectorSearch.  
            vs_stage = first_stage["$vectorSearch"]  
            existing_filter = vs_stage.get("filter", {})  
            scope_filter = {"experiment_id": {"$in": self._read_scopes}}  
    
            if existing_filter:  
                # Combine the user's existing filter with our scope filter via $and  
                new_filter = {"$and": [existing_filter, scope_filter]}  
            else:  
                new_filter = scope_filter  
    
            vs_stage["filter"] = new_filter  
            # Return the pipeline as-is, so that $vectorSearch remains the first stage  
            return self._collection.aggregate(pipeline, *args, **kwargs)  
        else:  
            # Normal case: pipeline doesn't start with $vectorSearch,   
            # so we can safely prepend a $match stage for scoping.  
            scope_match_stage = {  
                "$match": {"experiment_id": {"$in": self._read_scopes}}  
            }  
            scoped_pipeline = [scope_match_stage] + pipeline  
            return self._collection.aggregate(scoped_pipeline, *args, **kwargs)  

class ScopedMongoWrapper:
    """
    Wraps an `AsyncIOMotorDatabase` to provide scoped collection access.

    When a collection attribute is accessed (e.g., `db.my_collection`),
    this class returns a `ScopedCollectionWrapper` instance for that
    collection, configured with the appropriate read/write scopes.

    It caches these `ScopedCollectionWrapper` instances to avoid
    re-creating them on every access within the same request context.
    """
    
    __slots__ = ('_db', '_read_scopes', '_write_scope', '_wrapper_cache')

    def __init__(
        self,
        real_db: AsyncIOMotorDatabase,
        read_scopes: List[str],
        write_scope: str
    ):
        self._db = real_db
        self._read_scopes = read_scopes
        self._write_scope = write_scope
        
        # Cache for created collection wrappers.
        self._wrapper_cache: Dict[str, ScopedCollectionWrapper] = {}

    def __getattr__(self, name: str) -> ScopedCollectionWrapper:
        """
        Proxies attribute access to the underlying database.
        
        If `name` is a collection, returns a `ScopedCollectionWrapper`.
        """
            
        # Prevent proxying private/special attributes
        if name.startswith('_'):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'. "
                "Access to private attributes is blocked."
            )
        
        # Construct the prefixed collection name, e.g., "data_imaging_workouts"
        # `self._write_scope` holds the slug (e.g., "data_imaging")
        # `name` holds the base name (e.g., "workouts")
        prefixed_name = f"{self._write_scope}_{name}"

        # Check cache first using the *prefixed_name*
        if prefixed_name in self._wrapper_cache:
            return self._wrapper_cache[prefixed_name]

        # Get the real collection from the motor db object using the *prefixed_name*
        real_collection = getattr(self._db, prefixed_name)
        # --- END FIX ---
        
        # Ensure we are actually wrapping a collection object
        if not isinstance(real_collection, AsyncIOMotorCollection):
            raise AttributeError(
                f"'{name}' (prefixed as '{prefixed_name}') is not an AsyncIOMotorCollection. "
                f"ScopedMongoWrapper can only proxy collections (found {type(real_collection)})."
            )

        # Create the new wrapper
        wrapper = ScopedCollectionWrapper(
            real_collection=real_collection,
            read_scopes=self._read_scopes,
            write_scope=self._write_scope
        )
        
        # Store it in the cache for this instance using the *prefixed_name*
        self._wrapper_cache[prefixed_name] = wrapper
        return wrapper