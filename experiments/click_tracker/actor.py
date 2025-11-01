# File: /app/experiments/click_tracker/actor.py

import logging
import datetime
from typing import List
import ray

logger = logging.getLogger(__name__)

@ray.remote
class ExperimentActor:
    """
    Ray Actor for handling clicks.
    Now uses ExperimentDB for easy database access - no MongoDB knowledge needed!
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        try:
            # Import ExperimentDB and dependencies
            import motor.motor_asyncio
            from async_mongo_wrapper import ScopedMongoWrapper
            from experiment_db import ExperimentDB
            
            # Setup database connection
            self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
            real_db = self.client[db_name]
            
            # Create ScopedMongoWrapper for isolation
            scoped_wrapper = ScopedMongoWrapper(
                real_db=real_db,
                read_scopes=read_scopes,
                write_scope=write_scope
            )
            
            # Create ExperimentDB for easy access
            self.db = ExperimentDB(scoped_wrapper)
            
            self.write_scope = write_scope
            self.read_scopes = read_scopes

            logger.info(
                f"[ClickTrackerActor] started with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using ExperimentDB"
            )
        except Exception as e:
            logger.critical(f"[ClickTrackerActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.client = None
            self.db = None

    # Methods must be async
    async def record_click(self) -> int:
        """
        Inserts a new click doc and returns the updated count.
        """
        try:
            if not self.db:
                return -1
                
            # Use MongoDB-style API - familiar API!
            await self.db.clicks.insert_one({
                "event": "button_click",
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            })
            
            # Get count using MongoDB API
            return await self.db.clicks.count_documents({})
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in record_click: {e}", exc_info=True)
            return -1

    # Methods must be async
    async def get_count(self) -> int:
        """
        Returns how many click docs exist for this experiment (by scope).
        """
        try:
            if not self.db:
                return -1
                
            # MongoDB-style count - scoping is automatic!
            return await self.db.clicks.count_documents({})
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in get_count: {e}", exc_info=True)
            return -1