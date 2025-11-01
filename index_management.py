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
        logger.error(f"{log_prefix} IndexManager init error: {e}", exc_info=True)
        return
    
    for index_def in index_definitions:
        index_name = index_def.get("name")
        index_type = index_def.get("type")
        try:
            if index_type == "regular":
                keys = index_def.get("keys")
                if not keys:
                    logger.warning(f"{log_prefix} Missing 'keys' on index '{index_name}'.")
                    continue
                
                # Check if this is an _id index (MongoDB creates these automatically)
                is_id_index = False
                if isinstance(keys, dict):
                    is_id_index = len(keys) == 1 and "_id" in keys
                elif isinstance(keys, list):
                    is_id_index = len(keys) == 1 and keys[0][0] == "_id"
                
                if is_id_index:
                    logger.info(f"{log_prefix} Skipping '_id' index '{index_name}' - MongoDB creates _id indexes automatically and they can't be customized.")
                    continue
                
                # For non-_id indexes, process options normally but filter invalid ones
                options = {**index_def.get("options", {}), "name": index_name}
                
                existing_index = await index_manager.get_index(index_name)
                if existing_index:
                    # Compare keys to see if they differ
                    tmp_keys = [(k, v) for k, v in keys.items()] if isinstance(keys, dict) else keys
                    key_doc = {k: v for k, v in tmp_keys}
                    if existing_index.get("key") != key_doc:
                        logger.warning(f"{log_prefix} Index '{index_name}' mismatch -> drop & recreate.")
                        await index_manager.drop_index(index_name)
                    else:
                        logger.info(f"{log_prefix} Regular index '{index_name}' matches; skipping.")
                        continue
                logger.info(f"{log_prefix} Creating regular index '{index_name}'...")
                await index_manager.create_index(keys, **options)
                logger.info(f"{log_prefix} ✔️ Created regular index '{index_name}'.")
            elif index_type in ("vectorSearch", "search"):
                definition = index_def.get("definition")
                if not definition:
                    logger.warning(f"{log_prefix} Missing 'definition' for search index '{index_name}'.")
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
                            logger.error(f"{log_prefix} Index '{index_name}' state=FAILED. Manual fix needed.")
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
                            logger.error(f"{log_prefix} ❌ Failed to update search index '{index_name}': {update_err}", exc_info=True)
                else:
                    logger.info(f"{log_prefix} Creating new search index '{index_name}'...")
                    await index_manager.create_search_index(name=index_name, definition=definition, index_type=index_type, wait_for_ready=True)
                    logger.info(f"{log_prefix} ✔️ Created new '{index_type}' index '{index_name}'.")
            else:
                logger.warning(f"{log_prefix} Unknown index type '{index_type}'; skipping.")
        except Exception as e:
            logger.error(f"{log_prefix} Error managing index '{index_name}': {e}", exc_info=True)

