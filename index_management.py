"""Index management functions for MongoDB Atlas search and regular indexes."""
import json
import logging
from typing import Any, Dict, List
from motor.motor_asyncio import AsyncIOMotorDatabase
from config import INDEX_MANAGER_AVAILABLE, AsyncAtlasIndexManager

logger = logging.getLogger(__name__)


def normalize_json_def(obj: Any) -> Any:
    """
    Normalize a JSON-serializable object for comparison by:
    1. Converting to JSON string (which sorts dict keys)
    2. Parsing back to dict/list
    This makes comparisons order-insensitive and format-insensitive.
    """
    try:
        return json.loads(json.dumps(obj, sort_keys=True))
    except (TypeError, ValueError) as e:
        # If it can't be serialized, return as-is for fallback comparison
        logger.warning(f"Could not normalize JSON def: {e}")
        return obj


async def run_index_creation_for_collection(
    db: AsyncIOMotorDatabase,
    slug: str,
    collection_name: str,
    index_definitions: List[Dict[str, Any]]
):
    """Create or update indexes for a collection based on index definitions."""
    log_prefix = f"[{slug} -> {collection_name}]"
    
    if not INDEX_MANAGER_AVAILABLE:
        logger.warning(f"{log_prefix} Index Manager not available.")
        return
    
    try:
        real_collection = db[collection_name]
        index_manager = AsyncAtlasIndexManager(real_collection)
        logger.info(f"{log_prefix} Checking {len(index_definitions)} index defs.")
    except Exception as e:
        logger.error(
            f"{log_prefix} Failed to initialize IndexManager for collection '{collection_name}': {e}. "
            f"This prevents all index operations for this collection. Check MongoDB connection and collection permissions.",
            exc_info=True
        )
        return
    
    for index_def in index_definitions:
        index_name = index_def.get("name")
        index_type = index_def.get("type")
        try:
            if index_type == "regular":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(
                        f"{log_prefix} Missing 'keys' field on regular index '{index_name}'. "
                        f"Regular indexes require a 'keys' field specifying which fields to index. "
                        f"Skipping this index definition."
                    )
                    continue
                
                # Check if this is an _id index (MongoDB creates these automatically)
                is_id_index = False
                if isinstance(keys, dict):
                    is_id_index = len(keys) == 1 and "_id" in keys
                elif isinstance(keys, list):
                    is_id_index = len(keys) == 1 and keys[0][0] == "_id"
                
                if is_id_index:
                    logger.info(
                        f"{log_prefix} Skipping '_id' index '{index_name}'. "
                        f"MongoDB automatically creates '_id' indexes on all collections and they cannot be customized. "
                        f"This is expected behavior - no action needed."
                    )
                    continue
                
                # For non-_id indexes, process options normally but filter invalid ones
                options = {**index_def.get("options", {}), "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    # Compare keys to see if they differ
                    tmp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
                    key_doc = {k: v for k, v in tmp_keys}
                    if existing_index.get("key") != key_doc:
                        logger.warning(
                            f"{log_prefix} Index '{index_name}' definition mismatch detected. "
                            f"Existing keys: {existing_index.get('key')}, Expected keys: {key_doc}. "
                            f"Dropping existing index and recreating with new definition."
                        )
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Regular index '{index_name}' matches; skipping.")
                        continue
                logger.info(f"{log_prefix} Creating regular index '{index_name}'...")
                await index_manager.create_index(keys, **options)
                logger.info(f"{log_prefix} ✔️ Created regular index '{index_name}'.")
            elif index_type == "ttl":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(
                        f"{log_prefix} Missing 'keys' field on TTL index '{index_name}'. "
                        f"TTL indexes require a 'keys' field specifying the date/timestamp field. "
                        f"Skipping this index definition."
                    )
                    continue
                
                options = index_def.get("options", {})
                expire_after = options.get("expireAfterSeconds")
                if not expire_after or not isinstance(expire_after, int) or expire_after < 1:
                    logger.warning(
                        f"{log_prefix} TTL index '{index_name}' missing or invalid 'expireAfterSeconds' in options. "
                        f"TTL indexes require 'options.expireAfterSeconds' to be a positive integer. "
                        f"Skipping this index definition."
                    )
                    continue
                
                # Convert keys to standard format if needed
                if isinstance(keys, dict):
                    # For TTL, MongoDB expects a single date field
                    ttl_keys = [(k, v) for k, v in keys.items()]
                else:
                    ttl_keys = keys
                
                # Create TTL index with expireAfterSeconds
                index_options = {**options, "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    existing_key = existing_index.get("key", {})
                    expected_key = {k: v for k, v in ttl_keys} if isinstance(ttl_keys, list) else {k: v for k, v in keys.items()}
                    
                    existing_expire = existing_index.get("expireAfterSeconds")
                    expected_expire = expire_after
                    
                    if existing_key != expected_key or existing_expire != expected_expire:
                        logger.warning(
                            f"{log_prefix} TTL index '{index_name}' definition mismatch. "
                            f"Existing: keys={existing_key}, expireAfterSeconds={existing_expire}. "
                            f"Expected: keys={expected_key}, expireAfterSeconds={expected_expire}. "
                            f"Dropping existing index and recreating."
                        )
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} TTL index '{index_name}' matches; skipping.")
                        continue
                
                logger.info(
                    f"{log_prefix} Creating TTL index '{index_name}' on field(s) {ttl_keys} "
                    f"with expireAfterSeconds={expire_after}..."
                )
                await index_manager.create_index(ttl_keys, **index_options)
                logger.info(f"{log_prefix} ✔️ Created TTL index '{index_name}' (expires after {expire_after} seconds).")
            elif index_type == "partial":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(
                        f"{log_prefix} Missing 'keys' field on partial index '{index_name}'. "
                        f"Partial indexes require a 'keys' field. Skipping this index definition."
                    )
                    continue
                
                options = index_def.get("options", {})
                partial_filter = options.get("partialFilterExpression")
                if not partial_filter:
                    logger.warning(
                        f"{log_prefix} Partial index '{index_name}' missing 'partialFilterExpression' in options. "
                        f"Partial indexes require 'options.partialFilterExpression' to specify which documents to index. "
                        f"Skipping this index definition."
                    )
                    continue
                
                # Convert keys to standard format if needed
                if isinstance(keys, dict):
                    partial_keys = [(k, v) for k, v in keys.items()]
                else:
                    partial_keys = keys
                
                index_options = {**options, "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    existing_key = existing_index.get("key", {})
                    expected_key = {k: v for k, v in partial_keys} if isinstance(partial_keys, list) else {k: v for k, v in keys.items()}
                    
                    existing_partial = existing_index.get("partialFilterExpression")
                    expected_partial = partial_filter
                    
                    if existing_key != expected_key or existing_partial != expected_partial:
                        logger.warning(
                            f"{log_prefix} Partial index '{index_name}' definition mismatch. "
                            f"Existing: keys={existing_key}, filter={existing_partial}. "
                            f"Expected: keys={expected_key}, filter={expected_partial}. "
                            f"Dropping existing index and recreating."
                        )
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Partial index '{index_name}' matches; skipping.")
                        continue
                
                logger.info(
                    f"{log_prefix} Creating partial index '{index_name}' on field(s) {partial_keys} "
                    f"with filter: {partial_filter}..."
                )
                await index_manager.create_index(partial_keys, **index_options)
                logger.info(f"{log_prefix} ✔️ Created partial index '{index_name}'.")
            elif index_type == "text":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(
                        f"{log_prefix} Missing 'keys' field on text index '{index_name}'. "
                        f"Text indexes require a 'keys' field with at least one 'text' type field. "
                        f"Skipping this index definition."
                    )
                    continue
                
                # Convert keys to standard format for text indexes
                if isinstance(keys, dict):
                    text_keys = [(k, v) for k, v in keys.items()]
                else:
                    text_keys = keys
                
                # Verify at least one text field exists
                has_text = any(
                    (isinstance(k, list) and len(k) >= 2 and (k[1] == "text" or k[1] == "TEXT" or k[1] == 1))
                    or (isinstance(k, tuple) and len(k) >= 2 and (k[1] == "text" or k[1] == "TEXT" or k[1] == 1))
                    for k in text_keys
                ) or any(
                    v == "text" or v == "TEXT" or v == 1
                    for k, v in (keys.items() if isinstance(keys, dict) else [])
                )
                
                if not has_text:
                    logger.warning(
                        f"{log_prefix} Text index '{index_name}' has no fields with 'text' type. "
                        f"At least one field must have type 'text'. Skipping this index definition."
                    )
                    continue
                
                options = {**index_def.get("options", {}), "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    existing_key = existing_index.get("key", {})
                    expected_key = {k: v for k, v in text_keys} if isinstance(text_keys, list) else {k: v for k, v in keys.items()}
                    
                    if existing_key != expected_key:
                        logger.warning(
                            f"{log_prefix} Text index '{index_name}' definition mismatch. "
                            f"Existing keys: {existing_key}, Expected keys: {expected_key}. "
                            f"Dropping existing index and recreating."
                        )
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Text index '{index_name}' matches; skipping.")
                        continue
                
                logger.info(f"{log_prefix} Creating text index '{index_name}' on field(s) {text_keys}...")
                await index_manager.create_index(text_keys, **options)
                logger.info(f"{log_prefix} ✔️ Created text index '{index_name}'.")
            elif index_type == "geospatial":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(
                        f"{log_prefix} Missing 'keys' field on geospatial index '{index_name}'. "
                        f"Geospatial indexes require a 'keys' field with at least one geospatial type ('2dsphere', '2d', 'geoHaystack'). "
                        f"Skipping this index definition."
                    )
                    continue
                
                # Convert keys to standard format for geospatial indexes
                if isinstance(keys, dict):
                    geo_keys = [(k, v) for k, v in keys.items()]
                else:
                    geo_keys = keys
                
                # Verify at least one geospatial field exists
                geo_types = ["2dsphere", "2d", "geoHaystack"]
                has_geo = any(
                    (isinstance(k, list) and len(k) >= 2 and k[1] in geo_types)
                    or (isinstance(k, tuple) and len(k) >= 2 and k[1] in geo_types)
                    for k in geo_keys
                ) or any(
                    v in geo_types
                    for k, v in (keys.items() if isinstance(keys, dict) else [])
                )
                
                if not has_geo:
                    logger.warning(
                        f"{log_prefix} Geospatial index '{index_name}' has no fields with geospatial type. "
                        f"At least one field must have type '2dsphere', '2d', or 'geoHaystack'. "
                        f"Skipping this index definition."
                    )
                    continue
                
                options = {**index_def.get("options", {}), "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    existing_key = existing_index.get("key", {})
                    expected_key = {k: v for k, v in geo_keys} if isinstance(geo_keys, list) else {k: v for k, v in keys.items()}
                    
                    if existing_key != expected_key:
                        logger.warning(
                            f"{log_prefix} Geospatial index '{index_name}' definition mismatch. "
                            f"Existing keys: {existing_key}, Expected keys: {expected_key}. "
                            f"Dropping existing index and recreating."
                        )
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Geospatial index '{index_name}' matches; skipping.")
                        continue
                
                logger.info(f"{log_prefix} Creating geospatial index '{index_name}' on field(s) {geo_keys}...")
                await index_manager.create_index(geo_keys, **options)
                logger.info(f"{log_prefix} ✔️ Created geospatial index '{index_name}'.")
            elif index_type in ("vectorSearch", "search"):
                definition = index_def.get("definition")
                if not definition:
                    logger.warning(
                        f"{log_prefix} Missing 'definition' field for {index_type} index '{index_name}'. "
                        f"Atlas Search and Vector Search indexes require a 'definition' object specifying fields and configuration. "
                        f"Skipping this index definition."
                    )
                    continue
                existing_index = await index_manager.get_search_index(index_name)
                if existing_index:
                    current_def = existing_index.get("latestDefinition", existing_index.get("definition"))
                    # Normalize both definitions for order-insensitive comparison
                    normalized_current = normalize_json_def(current_def)
                    normalized_expected = normalize_json_def(definition)
                    
                    if normalized_current == normalized_expected:
                        logger.info(f"{log_prefix} Search index '{index_name}' definition matches.")
                        if not existing_index.get("queryable") and existing_index.get("status") != "FAILED":
                            logger.info(f"{log_prefix} Index '{index_name}' not queryable yet; waiting.")
                            await index_manager._wait_for_search_index_ready(index_name, index_manager.DEFAULT_SEARCH_TIMEOUT)
                            logger.info(f"{log_prefix} Index '{index_name}' now ready.")
                        elif existing_index.get("status") == "FAILED":
                            logger.error(
                                f"{log_prefix} Index '{index_name}' is in FAILED state. "
                                f"This indicates the index build failed - check Atlas UI for detailed error messages. "
                                f"Manual intervention required to resolve the issue before the index can be used."
                            )
                        else:
                            logger.info(f"{log_prefix} Index '{index_name}' is ready.")
                    else:
                        logger.warning(f"{log_prefix} Search index '{index_name}' definition changed; updating.")
                        # Extract field paths for clearer logging
                        current_fields = normalized_current.get('fields', []) if isinstance(normalized_current, dict) else []
                        expected_fields = normalized_expected.get('fields', []) if isinstance(normalized_expected, dict) else []
                        
                        current_paths = [f.get('path', '?') for f in current_fields if isinstance(f, dict)]
                        expected_paths = [f.get('path', '?') for f in expected_fields if isinstance(f, dict)]
                        
                        logger.info(f"{log_prefix} Current index filter fields: {current_paths}")
                        logger.info(f"{log_prefix} Expected index filter fields: {expected_paths}")
                        logger.info(f"{log_prefix} Updating index '{index_name}' with new definition (this may take a few moments)...")
                        
                        try:
                            await index_manager.update_search_index(name=index_name, definition=definition, wait_for_ready=True)
                            logger.info(f"{log_prefix} ✔️ Successfully updated search index '{index_name}'. Index is now ready.")
                        except Exception as update_err:
                            logger.error(
                                f"{log_prefix} ❌ Failed to update search index '{index_name}': {update_err}. "
                                f"Collection: {collection_name}, Index type: {index_type}. "
                                f"Check Atlas UI for more details. Index may still be functional with old definition.",
                                exc_info=True
                            )
                else:
                    logger.info(f"{log_prefix} Creating new search index '{index_name}'...")
                    await index_manager.create_search_index(name=index_name, definition=definition, index_type=index_type, wait_for_ready=True)
                    logger.info(f"{log_prefix} ✔️ Created new '{index_type}' index '{index_name}'.")
            else:
                logger.warning(
                    f"{log_prefix} Unknown index type '{index_type}' for index '{index_name}'. "
                    f"Supported types: 'regular', 'vectorSearch', 'search', 'text', 'geospatial', 'ttl', 'partial'. "
                    f"Skipping this index definition. Update manifest.json with a supported index type."
                )
        except Exception as e:
            logger.error(
                f"{log_prefix} Unexpected error managing index '{index_name}' (type: {index_type}): {e}. "
                f"Collection: {collection_name}. This index will be skipped, but other indexes may still be created.",
                exc_info=True
            )

