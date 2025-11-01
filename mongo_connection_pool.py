"""
Shared MongoDB Connection Pool Manager (mongo_connection_pool.py)
================================================================================

Provides a singleton MongoDB connection pool manager for Ray actors to share
database connections efficiently. This prevents each actor from creating its
own connection pool, reducing total connections from N×50 to N×5.

This module implements a thread-safe singleton pattern that ensures all Ray
actors in the same process share a single MongoDB client instance with a
reasonable connection pool size.

Usage:
    from mongo_connection_pool import get_shared_mongo_client
    
    client = get_shared_mongo_client(mongo_uri)
    db = client[db_name]
"""

import asyncio
import logging
import threading
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# Global singleton instance
_shared_client: Optional[AsyncIOMotorClient] = None
# Use threading.Lock for cross-thread safety in Ray's actor environment
# Ray actors may run in different threads, so asyncio.Lock isn't sufficient
_init_lock = threading.Lock()


def get_shared_mongo_client(
    mongo_uri: str,
    max_pool_size: int = 10,
    min_pool_size: int = 1,
    server_selection_timeout_ms: int = 5000,
    max_idle_time_ms: int = 45000,
    retry_writes: bool = True,
    retry_reads: bool = True,
) -> AsyncIOMotorClient:
    """
    Gets or creates a shared MongoDB client instance.
    
    This function implements a singleton pattern to ensure all Ray actors
    in the same process share a single MongoDB client connection pool.
    
    Args:
        mongo_uri: MongoDB connection URI
        max_pool_size: Maximum connection pool size (default: 10 for actors, vs 50 for main app)
        min_pool_size: Minimum connection pool size (default: 1)
        server_selection_timeout_ms: Server selection timeout in milliseconds
        max_idle_time_ms: Maximum idle time before closing connections
        retry_writes: Enable automatic retry for write operations
        retry_reads: Enable automatic retry for read operations
    
    Returns:
        Shared AsyncIOMotorClient instance
    
    Example:
        client = get_shared_mongo_client("mongodb://mongo:27017/")
        db = client["my_database"]
    """
    global _shared_client
    
    # Fast path: return existing client if already initialized
    if _shared_client is not None:
        # Verify client is still connected
        try:
            # Non-blocking check - if client was closed, it will be None or invalid
            if hasattr(_shared_client, '_topology') and _shared_client._topology is not None:
                return _shared_client
        except Exception:
            # Client was closed or invalid, reset and recreate
            _shared_client = None
    
    # Thread-safe initialization with lock
    # Use threading lock for Ray's multi-threaded actor environment
    with _init_lock:
        # Double-check pattern: another thread may have initialized while we waited
        if _shared_client is not None:
            try:
                if hasattr(_shared_client, '_topology') and _shared_client._topology is not None:
                    return _shared_client
            except Exception:
                # Client was closed or invalid, reset and recreate
                _shared_client = None
        
        logger.info(
            f"Creating shared MongoDB client with pool_size={max_pool_size}, "
            f"min_pool_size={min_pool_size} (singleton for all actors in this process)"
        )
        
        try:
            _shared_client = AsyncIOMotorClient(
                mongo_uri,
                serverSelectionTimeoutMS=server_selection_timeout_ms,
                appname="ModularLabsActor",
                maxPoolSize=max_pool_size,
                minPoolSize=min_pool_size,
                maxIdleTimeMS=max_idle_time_ms,
                retryWrites=retry_writes,
                retryReads=retry_reads,
            )
            
            logger.info(
                f"Shared MongoDB client created successfully "
                f"(pool_size={max_pool_size}, min_pool_size={min_pool_size})"
            )
            
            # Note: Ping verification happens asynchronously on first use
            # This avoids blocking during synchronous __init__ in Ray actors
            
            return _shared_client
        except Exception as e:
            logger.error(f"Failed to create shared MongoDB client: {e}", exc_info=True)
            _shared_client = None
            raise
    
    return _shared_client




async def verify_shared_client() -> bool:
    """
    Verifies that the shared MongoDB client is connected.
    Should be called from async context after initialization.
    
    Returns:
        True if client is connected and responsive, False otherwise
    """
    global _shared_client
    
    if _shared_client is None:
        logger.warning("Shared MongoDB client is None - cannot verify")
        return False
    
    try:
        await _shared_client.admin.command("ping")
        logger.debug("Shared MongoDB client verification successful")
        return True
    except Exception as e:
        logger.error(f"Shared MongoDB client verification failed: {e}")
        return False


def close_shared_client():
    """
    Closes the shared MongoDB client.
    Should be called during application shutdown.
    """
    global _shared_client
    
    if _shared_client is not None:
        try:
            _shared_client.close()
            logger.info("Shared MongoDB client closed")
        except Exception as e:
            logger.warning(f"Error closing shared MongoDB client: {e}")
        finally:
            _shared_client = None

