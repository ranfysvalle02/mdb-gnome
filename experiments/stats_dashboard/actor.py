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
            # Magical database abstraction - one line to get Motor-like API!
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            
            self.write_scope = write_scope     # "stats_dashboard"
            self.read_scopes = read_scopes     # ["stats_dashboard", "click_tracker"]
        
            logger.info(
                f"[StatsDashboardActor] initialized with write_scope='{self.write_scope}' "
                f"and read_scopes={self.read_scopes} (DB='{db_name}') using magical database abstraction"
            )
            
            if "click_tracker" in self.read_scopes:
                logger.info(f"[StatsDashboardActor] Has read access to 'click_tracker_clicks'")
        except Exception as e:
            logger.critical(f"[StatsDashboardActor] ❌ CRITICAL: Failed to init DB: {e}")
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
                
            # 1. CROSS-EXPERIMENT READ (automatic scoping via raw access!)
            if "click_tracker" in self.read_scopes:
                # Access cross-experiment collection using get_collection (Motor-like API!)
                # The wrapper automatically handles scoping!
                clicks_collection = self.db.raw.get_collection("click_tracker_clicks")
                result["total_clicks"] = await clicks_collection.count_documents({})
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