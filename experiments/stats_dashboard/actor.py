# File: /app/experiments/stats_dashboard/actor.py

import logging
import datetime
from typing import List
import ray

logger = logging.getLogger(__name__)

@ray.remote
class ExperimentActor:
    """
    A Ray Actor that uses ExperimentDB for easy database access.
    No MongoDB knowledge required!
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
            
            self.write_scope = write_scope     # "stats_dashboard"
            self.read_scopes = read_scopes     # ["stats_dashboard", "click_tracker"]
        
            logger.info(
                f"[StatsDashboardActor] initialized with write_scope='{self.write_scope}' "
                f"and read_scopes={self.read_scopes} (DB='{db_name}') using ExperimentDB"
            )
            
            if "click_tracker" in self.read_scopes:
                logger.info(f"[StatsDashboardActor] Has read access to 'click_tracker_clicks'")
        except Exception as e:
            logger.critical(f"[StatsDashboardActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.client = None
            self.db = None

    # Method must be async
    async def fetch_and_log_view(self, user_email: str) -> dict:
        """
        1. Counts docs in 'click_tracker_clicks' if it has read access.
        2. Inserts a 'dashboard_view' log into 'stats_dashboard_logs'.
        3. Counts total logs in 'stats_dashboard_logs'.
        
        All using simple ExperimentDB API - no MongoDB knowledge needed!
        """
        result = {"total_clicks": 0, "my_logs": 0, "error": None}

        try:
            if not self.db:
                result["error"] = "Database not initialized"
                return result
                
            # 1. CROSS-EXPERIMENT READ (automatic scoping!)
            if "click_tracker" in self.read_scopes:
                # For cross-experiment access, we need to access the collection directly from raw DB
                # and use ScopedCollectionWrapper manually for automatic scoping
                from async_mongo_wrapper import ScopedCollectionWrapper
                # Access the raw collection (fully prefixed name)
                raw_collection = getattr(self.db.raw._db, "click_tracker_clicks")
                # Create a scoped wrapper for automatic scoping
                scoped_collection = ScopedCollectionWrapper(
                    raw_collection,
                    read_scopes=self.read_scopes,
                    write_scope=self.write_scope
                )
                # Use simple count - scoping is automatic!
                result["total_clicks"] = await scoped_collection.count_documents({})
            else:
                logger.warning("[StatsDashboardActor] No read access to 'click_tracker' scope.")
                result["total_clicks"] = -1 # Indicate no access

            # 2. WRITE (SELF-SCOPED) - MongoDB-style insert!
            await self.db.logs.insert_one({
                "event": "dashboard_view",
                "user": user_email,
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            })

            # 3. READ (SELF-SCOPED) - MongoDB-style count!
            result["my_logs"] = await self.db.logs.count_documents({})

        except Exception as e:
            logger.error(f"[StatsDashboardActor] DB error: {e}", exc_info=True)
            result["error"] = str(e)

        return result