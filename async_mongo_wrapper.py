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
- `AutoIndexManager`: ✨ Magical automatic index management! Automatically
  creates indexes based on query patterns, making it easy to use collections
  without manual index configuration. Enabled by default for all experiments.

This design ensures data isolation between experiments while providing
a familiar (Motor-like) developer experience with automatic index optimization.
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
from pymongo.errors import OperationFailure, CollectionInvalid, AutoReconnect
from pymongo import ASCENDING, DESCENDING, TEXT, MongoClient

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
    
    This class provides a robust, high-level API for index operations,
    including 'wait_for_ready' polling logic to handle the asynchronous
    nature of Atlas index builds.
    """
    # Use __slots__ for minor performance gain (faster attribute access)
    __slots__ = ('_collection',)

    # --- Class-level constants for polling and timeouts ---
    DEFAULT_POLL_INTERVAL: ClassVar[int] = 5  # seconds
    DEFAULT_SEARCH_TIMEOUT: ClassVar[int] = 600  # 10 minutes
    DEFAULT_DROP_TIMEOUT: ClassVar[int] = 300  # 5 minutes

    def __init__(self, real_collection: AsyncIOMotorCollection):
        """
        Initializes the manager with a direct reference to a
        motor.motor_asyncio.AsyncIOMotorCollection.
        """
        if not isinstance(real_collection, AsyncIOMotorCollection):
            raise TypeError(
                f"Expected AsyncIOMotorCollection, got {type(real_collection)}"
            )
        self._collection = real_collection

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
        
        This method is idempotent. It checks if an index with the same name
        and definition already exists and is queryable. If it exists but the
        definition has changed, it triggers an update. If it's building,
        it waits. If it doesn't exist, it creates it.
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
            # Check for existing index
            existing_index = await self.get_search_index(name)

            if existing_index:
                logger.info(f"Search index '{name}' already exists.")
                latest_def = existing_index.get("latestDefinition", {})
                definition_changed = False
                change_reason = ""

                # --- Definition Change Check ---
                # Compare the provided definition with the 'latestDefinition'
                # from the existing index.
                if "fields" in definition and index_type.lower() == "vectorsearch":
                    existing_fields = latest_def.get("fields")
                    if existing_fields != definition["fields"]:
                        definition_changed = True
                        change_reason = "vector 'fields' definition differs."
                elif "mappings" in definition and index_type.lower() == "search":
                    existing_mappings = latest_def.get("mappings")
                    if existing_mappings != definition["mappings"]:
                        definition_changed = True
                        change_reason = "Lucene 'mappings' definition differs."
                else:
                    logger.warning(
                        f"Index definition '{name}' has keys that don't match "
                        f"index_type '{index_type}'. Cannot reliably check for changes."
                    )
                # --- End Check ---

                if definition_changed:
                    # Definitions differ, trigger an update
                    logger.warning(f"Search index '{name}' definition has changed ({change_reason}). Triggering update...")
                    await self.update_search_index(
                        name=name,
                        definition=definition,
                        wait_for_ready=False # Wait logic handled below
                    )
                elif existing_index.get("queryable"):
                    # Index exists, is up-to-date, and ready
                    logger.info(f"Search index '{name}' is already queryable and definition is up-to-date.")
                    return True
                elif existing_index.get("status") == "FAILED":
                    # Index exists but is in a failed state
                    logger.error(
                        f"Search index '{name}' exists but is in a FAILED state. "
                        f"Manual intervention in Atlas UI may be required."
                    )
                    return False
                else:
                    # Index exists, is up-to-date, but not queryable (e.g., "PENDING", "STALE")
                    logger.info(
                        f"Search index '{name}' exists and is up-to-date, "
                        f"but not queryable (Status: {existing_index.get('status')}). Waiting..."
                    )
            
            else:
                # --- Create New Index ---
                try:
                    logger.info(f"Creating new search index '{name}' of type '{index_type}'...")
                    search_index_model = SearchIndexModel(
                        definition=definition,
                        name=name,
                        type=index_type
                    )
                    await self._collection.create_search_index(model=search_index_model)
                    logger.info(f"Search index '{name}' build has been submitted.")
                except OperationFailure as e:
                    # Handle race condition where another process created the index
                    if "IndexAlreadyExists" in str(e) or "DuplicateIndexName" in str(e):
                        logger.warning(f"Race condition: Index '{name}' was created by another process.")
                    else:
                        logger.error(f"OperationFailure during search index creation for '{name}': {e.details}")
                        raise e

            # --- Wait for Ready ---
            # If requested, poll the index status until it's queryable
            if wait_for_ready:
                return await self._wait_for_search_index_ready(name, timeout)
            return True

        except OperationFailure as e:
            logger.error(f"OperationFailure during search index creation/check for '{name}': {e.details}")
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred regarding search index '{name}': {e}")
            raise

    async def get_search_index(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the definition and status of a single search index by name
        using the $listSearchIndexes aggregation stage.
        """
        try:
            pipeline = [{"$listSearchIndexes": {"name": name}}]
            async for index_info in self._collection.aggregate(pipeline):
                # We expect only one or zero results
                return index_info
            return None
        except OperationFailure as e:
            logger.error(f"OperationFailure retrieving search index '{name}': {e.details}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error retrieving search index '{name}': {e}")
            return None

    async def list_search_indexes(self) -> List[Dict[str, Any]]:
        """Lists all Atlas Search indexes for the collection."""
        try:
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
        """
        try:
            # Check if index exists before trying to drop
            if not await self.get_search_index(name):
                logger.info(f"Search index '{name}' does not exist. Nothing to drop.")
                return True

            await self._collection.drop_search_index(name=name)
            logger.info(f"Submitted request to drop search index '{name}'.")

            if wait_for_drop:
                return await self._wait_for_search_index_drop(name, timeout)
            return True
        except OperationFailure as e:
            # Handle race condition where index was already dropped
            if "IndexNotFound" in str(e):
                logger.info(f"Search index '{name}' was already deleted (race condition).")
                return True
            logger.error(f"OperationFailure dropping search index '{name}': {e.details}")
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
        This will trigger a rebuild of the index.
        """
        try:
            logger.info(f"Updating search index '{name}'...")
            await self._collection.update_search_index(name=name, definition=definition)
            logger.info(f"Search index '{name}' update submitted. Rebuild initiated.")
            if wait_for_ready:
                return await self._wait_for_search_index_ready(name, timeout)
            return True
        except OperationFailure as e:
            logger.error(f"Error updating search index '{name}': {e.details}")
            raise
        except Exception as e:
            logger.error(f"Error updating search index '{name}': {e}")
            raise

    async def _wait_for_search_index_ready(self, name: str, timeout: int) -> bool:
        """
        Private helper to poll the index status until it becomes
        queryable or fails.
        """
        start_time = time.time()
        logger.info(f"Waiting up to {timeout}s for search index '{name}' to become queryable...")

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.error(f"Timeout: Index '{name}' did not become queryable within {timeout}s.")
                raise TimeoutError(f"Index '{name}' did not become queryable within {timeout}s.")

            index_info = None
            try:
                # Poll for the index status
                index_info = await self.get_search_index(name)
            except (OperationFailure, AutoReconnect) as e:
                # Handle transient network/DB errors during polling
                logger.warning(f"DB Error during polling for index '{name}': {getattr(e, 'details', e)}. Retrying...")
            except Exception as e:
                logger.error(f"Unexpected error during polling for index '{name}': {e}. Retrying...")

            if index_info:
                status = index_info.get("status")
                if status == "FAILED":
                    # The build failed permanently
                    logger.error(f"Search index '{name}' failed to build (Status: FAILED). Check Atlas UI for details.")
                    raise Exception(f"Index build failed for '{name}'.")

                queryable = index_info.get("queryable")
                if queryable:
                    # Success!
                    logger.info(f"Search index '{name}' is queryable (Status: {status}).")
                    return True

                # Not ready yet, log and wait
                logger.info(f"Polling for '{name}'. Status: {status}. Queryable: {queryable}. Elapsed: {elapsed:.0f}s")
            else:
                # Index not found yet (can happen right after creation command)
                logger.info(f"Polling for '{name}'. Index not found yet (normal during creation). Elapsed: {elapsed:.0f}s")

            await asyncio.sleep(self.DEFAULT_POLL_INTERVAL)

    async def _wait_for_search_index_drop(self, name: str, timeout: int) -> bool:
        """
        Private helper to poll until an index is successfully dropped.
        """
        start_time = time.time()
        logger.info(f"Waiting up to {timeout}s for search index '{name}' to be dropped...")
        while True:
            if time.time() - start_time > timeout:
                logger.error(f"Timeout: Index '{name}' was not dropped within {timeout}s.")
                raise TimeoutError(f"Index '{name}' was not dropped within {timeout}s.")

            index_info = await self.get_search_index(name)
            if not index_info:
                # Success! Index is gone.
                logger.info(f"Search index '{name}' has been successfully dropped.")
                return True

            logger.debug(f"Polling for '{name}' drop. Still present. Elapsed: {time.time() - start_time:.0f}s")
            await asyncio.sleep(self.DEFAULT_POLL_INTERVAL)

    # --- Regular Database Index Methods ---
    # These methods wrap the standard Motor index commands for a
    # consistent async API with the search index methods.
    
    async def create_index(
        self,
        keys: Union[str, List[Tuple[str, Union[int, str]]]],
        **kwargs: Any
    ) -> str:
        """
        Creates a standard (non-search) database index.
        Idempotent: checks if the index already exists first.
        """
        if isinstance(keys, str):
            keys = [(keys, ASCENDING)]

        # Attempt to auto-generate the index name if not provided
        index_name = kwargs.get("name")
        if not index_name:
            try:
                # Use pymongo helper to generate the name PyMongo would use
                from pymongo.helpers import _index_list
                index_doc = MongoClient()._database._CommandBuilder._gen_index_doc(keys, kwargs)
                index_name = _index_list(index_doc['key'].items())
            except Exception:
                # Fallback name generation
                index_name = f"index_{'_'.join([k[0] for k in keys])}"
                logger.warning(f"Could not auto-generate index name, using fallback: {index_name}")

        try:
            # Check if index already exists
            existing_indexes = await self.list_indexes()
            for index in existing_indexes:
                if index.get("name") == index_name:
                    logger.info(f"Regular index '{index_name}' already exists.")
                    return index_name

            # Create the index
            name = await self._collection.create_index(keys, **kwargs)
            logger.info(f"Successfully created regular index '{name}'.")
            return name
        except OperationFailure as e:
            logger.error(f"OperationFailure creating regular index '{index_name}': {e.details}")
            raise
        except Exception as e:
            logger.error(f"Failed to create regular index '{index_name}': {e}")
            raise

    async def create_text_index(
        self, fields: List[str], weights: Optional[Dict[str, int]] = None,
        name: str = "text_index", **kwargs: Any
    ) -> str:
        """Helper to create a standard text index."""
        keys = [(field, TEXT) for field in fields]
        if weights:
            kwargs["weights"] = weights
        if name:
            kwargs["name"] = name
        return await self.create_index(keys, **kwargs)

    async def create_geo_index(
        self, field: str,
        name: Optional[str] = None, **kwargs: Any
    ) -> str:
        """Helper to create a standard 2dsphere index."""
        keys = [(field, GEO2DSPHERE)]
        if name:
            kwargs["name"] = name
        return await self.create_index(keys, **kwargs)

    async def drop_index(self, name: str):
        """Drops a standard (non-search) database index by name."""
        try:
            await self._collection.drop_index(name)
            logger.info(f"Successfully dropped regular index '{name}'.")
        except OperationFailure as e:
            # Handle case where index is already gone
            if "index not found" in str(e).lower():
                logger.info(f"Regular index '{name}' does not exist. Nothing to drop.")
            else:
                logger.error(f"Failed to drop regular index '{name}': {e.details}")
                raise
        except Exception as e:
            logger.error(f"Failed to drop regular index '{name}': {e}")
            raise

    async def list_indexes(self) -> List[Dict[str, Any]]:
        """Lists all standard (non-search) indexes on the collection."""
        try:
            return await self._collection.list_indexes().to_list(None)
        except Exception as e:
            logger.error(f"Error listing regular indexes: {e}")
            return []

    async def get_index(self, name: str) -> Optional[Dict[str, Any]]:
        """Gets a single standard index by name."""
        indexes = await self.list_indexes()
        return next((index for index in indexes if index.get("name") == name), None)


# ##########################################################################
# AUTOMATIC INDEX MANAGEMENT
# ##########################################################################

class AutoIndexManager:
    """
    Magical index manager that automatically creates indexes based on query patterns.
    
    This class analyzes query filters and automatically creates appropriate indexes
    for frequently used fields, making it easy for experiments to use collections
    without manually defining indexes.
    
    Features:
    - Automatically detects query patterns (equality, range, sorting)
    - Creates indexes on-demand based on usage
    - Uses intelligent heuristics to avoid over-indexing
    - Thread-safe with async locks
    """
    
    __slots__ = ('_collection', '_index_manager', '_creation_cache', '_lock', '_query_counts')
    
    def __init__(self, collection: AsyncIOMotorCollection, index_manager: AsyncAtlasIndexManager):
        self._collection = collection
        self._index_manager = index_manager
        # Cache of index creation decisions (index_name -> bool)
        self._creation_cache: Dict[str, bool] = {}
        # Async lock to prevent race conditions during index creation
        self._lock = asyncio.Lock()
        # Track query patterns to determine which indexes to create
        self._query_counts: Dict[str, int] = {}
    
    def _extract_index_fields_from_filter(self, filter: Optional[Mapping[str, Any]]) -> List[Tuple[str, int]]:
        """
        Extracts potential index fields from a MongoDB query filter.
        
        Returns a list of (field_name, direction) tuples where:
        - direction is 1 for ASCENDING, -1 for DESCENDING
        - Only includes fields that would benefit from indexing
        """
        if not filter:
            return []
        
        index_fields = []
        
        def analyze_value(value: Any, field_name: str):
            """Recursively analyze filter values to extract index candidates."""
            if isinstance(value, dict):
                # Handle operators like $gt, $gte, $lt, $lte, $ne, $in, $exists
                if any(op in value for op in ['$gt', '$gte', '$lt', '$lte', '$ne', '$in', '$exists']):
                    # These operators benefit from indexes
                    index_fields.append((field_name, ASCENDING))
                # Handle $and and $or - recursively analyze
                if '$and' in value:
                    for sub_filter in value['$and']:
                        if isinstance(sub_filter, dict):
                            for k, v in sub_filter.items():
                                analyze_value(v, k)
                if '$or' in value:
                    # For $or, we can't easily determine index fields, skip for now
                    pass
            elif value is not None:
                # Direct equality match - very common and benefits from index
                index_fields.append((field_name, ASCENDING))
        
        # Analyze top-level fields
        for key, value in filter.items():
            if not key.startswith('$'):  # Skip operators at top level
                analyze_value(value, key)
        
        return list(set(index_fields))  # Remove duplicates
    
    def _extract_sort_fields(self, sort: Optional[Union[List[Tuple[str, int]], Dict[str, int]]]) -> List[Tuple[str, int]]:
        """
        Extracts index fields from sort specification.
        
        Returns a list of (field_name, direction) tuples.
        """
        if not sort:
            return []
        
        if isinstance(sort, dict):
            return [(field, direction) for field, direction in sort.items()]
        elif isinstance(sort, list):
            return sort
        else:
            return []
    
    def _generate_index_name(self, fields: List[Tuple[str, int]]) -> str:
        """Generate a human-readable index name from field list."""
        if not fields:
            return "auto_idx_empty"
        
        parts = []
        for field, direction in fields:
            dir_str = "asc" if direction == ASCENDING else "desc"
            parts.append(f"{field}_{dir_str}")
        
        return f"auto_{'_'.join(parts)}"
    
    async def ensure_index_for_query(
        self,
        filter: Optional[Mapping[str, Any]] = None,
        sort: Optional[Union[List[Tuple[str, int]], Dict[str, int]]] = None,
        hint_threshold: int = 3
    ):
        """
        Automatically ensure appropriate indexes exist for a given query.
        
        Args:
            filter: The query filter to analyze
            sort: The sort specification to analyze
            hint_threshold: Number of times a query pattern must be seen before creating index
        
        This method:
        1. Extracts potential index fields from filter and sort
        2. Combines them into a composite index if needed
        3. Creates the index if it doesn't exist and usage threshold is met
        4. Uses async lock to prevent race conditions
        """
        # Extract fields from filter and sort
        filter_fields = self._extract_index_fields_from_filter(filter)
        sort_fields = self._extract_sort_fields(sort)
        
        # Combine fields intelligently: filter fields first, then sort fields
        all_fields = []
        filter_field_names = {f[0] for f in filter_fields}
        
        # Add filter fields first
        for field, direction in filter_fields:
            all_fields.append((field, direction))
        
        # Add sort fields that aren't already in filter
        for field, direction in sort_fields:
            if field not in filter_field_names:
                all_fields.append((field, direction))
        
        if not all_fields:
            return  # No index needed
        
        # Limit to first 4 fields (MongoDB compound index best practice)
        all_fields = all_fields[:4]
        
        # Generate index name
        index_name = self._generate_index_name(all_fields)
        
        # Track query pattern usage
        pattern_key = index_name
        self._query_counts[pattern_key] = self._query_counts.get(pattern_key, 0) + 1
        
        # Only create index if usage threshold is met
        if self._query_counts[pattern_key] < hint_threshold:
            return
        
        # Check cache to avoid redundant creation attempts
        async with self._lock:
            if index_name in self._creation_cache:
                return  # Already attempted or created
            
            try:
                # Check if index already exists
                existing_indexes = await self._index_manager.list_indexes()
                for idx in existing_indexes:
                    if idx.get("name") == index_name:
                        self._creation_cache[index_name] = True
                        return  # Index already exists
                
                # Create the index
                keys = all_fields
                await self._index_manager.create_index(keys, name=index_name, background=True)
                self._creation_cache[index_name] = True
                logger.info(f"✨ Auto-created index '{index_name}' on {self._collection.name} for fields: {[f[0] for f in all_fields]}")
            
            except Exception as e:
                # Don't fail the query if index creation fails
                logger.warning(f"Failed to auto-create index '{index_name}': {e}")
                self._creation_cache[index_name] = False


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
    
    Magical Auto-Indexing:
    - Automatically creates indexes based on query patterns
    - Analyzes filter and sort specifications to determine needed indexes
    - Creates indexes in the background without blocking queries
    - Enables experiments to use collections without manual index configuration
    - Can be disabled by setting `auto_index=False` in constructor
    """
    
    # Use __slots__ for memory and speed optimization
    __slots__ = ('_collection', '_read_scopes', '_write_scope', '_index_manager', '_auto_index_manager', '_auto_index_enabled')

    def __init__(
        self,
        real_collection: AsyncIOMotorCollection,
        read_scopes: List[str],
        write_scope: str,
        auto_index: bool = True
    ):
        self._collection = real_collection
        self._read_scopes = read_scopes
        self._write_scope = write_scope
        self._auto_index_enabled = auto_index
        # Lazily instantiated and cached
        self._index_manager: Optional[AsyncAtlasIndexManager] = None
        self._auto_index_manager: Optional[AutoIndexManager] = None

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

    @property
    def auto_index_manager(self) -> Optional[AutoIndexManager]:
        """
        Gets the AutoIndexManager for magical automatic index creation.
        
        Returns None if auto-indexing is disabled.
        """
        if not self._auto_index_enabled:
            return None
        
        if self._auto_index_manager is None:
            # Lazily instantiate auto-index manager
            self._auto_index_manager = AutoIndexManager(
                self._collection,
                self.index_manager  # This will create index_manager if needed
            )
        return self._auto_index_manager

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
        """
        Applies the read scope to the filter.
        Automatically ensures appropriate indexes exist for the query.
        """
        # Magical auto-indexing: ensure indexes exist before querying
        # Note: We analyze the user's filter, not the scoped filter, since
        # experiment_id index is always ensured separately
        if self.auto_index_manager:
            sort = kwargs.get('sort')
            await self.auto_index_manager.ensure_index_for_query(filter=filter, sort=sort)
        
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
        Automatically ensures appropriate indexes exist for the query.
        """
        # Magical auto-indexing: ensure indexes exist before querying
        # Note: This is fire-and-forget, doesn't block cursor creation
        if self.auto_index_manager:
            sort = kwargs.get('sort')
            # Create a task to ensure index (fire and forget)
            asyncio.create_task(
                self.auto_index_manager.ensure_index_for_query(filter=filter, sort=sort)
            )
        
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
        """
        Applies the read scope to the filter for counting.
        Automatically ensures appropriate indexes exist for the query.
        """
        # Magical auto-indexing: ensure indexes exist before querying
        if self.auto_index_manager:
            await self.auto_index_manager.ensure_index_for_query(filter=filter)
        
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
    
    Features:
    - Automatic index management: indexes are created automatically based
      on query patterns, making it easy to use collections without manual
      index configuration. This "magical" feature is enabled by default.
    """
    
    __slots__ = ('_db', '_read_scopes', '_write_scope', '_wrapper_cache', '_auto_index')

    def __init__(
        self,
        real_db: AsyncIOMotorDatabase,
        read_scopes: List[str],
        write_scope: str,
        auto_index: bool = True
    ):
        self._db = real_db
        self._read_scopes = read_scopes
        self._write_scope = write_scope
        self._auto_index = auto_index
        
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

        # Create the new wrapper with auto-indexing enabled by default
        wrapper = ScopedCollectionWrapper(
            real_collection=real_collection,
            read_scopes=self._read_scopes,
            write_scope=self._write_scope,
            auto_index=self._auto_index
        )
        
        # Magically ensure experiment_id index exists (it's always used in queries)
        # This is fire-and-forget, runs in background
        if self._auto_index:
            asyncio.create_task(self._ensure_experiment_id_index(real_collection))
        
        # Store it in the cache for this instance using the *prefixed_name*
        self._wrapper_cache[prefixed_name] = wrapper
        return wrapper
    
    async def _ensure_experiment_id_index(self, collection: AsyncIOMotorCollection):
        """
        Ensures experiment_id index exists on collection.
        This index is always needed since all queries filter by experiment_id.
        """
        try:
            index_manager = AsyncAtlasIndexManager(collection)
            existing_indexes = await index_manager.list_indexes()
            
            # Check if experiment_id index already exists
            experiment_id_index_exists = False
            for idx in existing_indexes:
                keys = idx.get("key", {})
                # Check if experiment_id is indexed (could be single field or part of compound)
                if "experiment_id" in keys:
                    experiment_id_index_exists = True
                    break
            
            if not experiment_id_index_exists:
                # Create experiment_id index
                await index_manager.create_index(
                    [("experiment_id", ASCENDING)],
                    name="auto_experiment_id_asc",
                    background=True
                )
                logger.info(f"✨ Auto-created experiment_id index on {collection.name}")
        except Exception as e:
            # Don't fail if index creation fails, just log
            logger.debug(f"Could not ensure experiment_id index on {collection.name}: {e}")