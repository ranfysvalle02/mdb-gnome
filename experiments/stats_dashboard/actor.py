# File: /app/experiments/stats_dashboard/actor.py

import logging
import datetime
from typing import List
import ray

# Use the ASYNC motor client
import motor.motor_asyncio

logger = logging.getLogger(__name__)

@ray.remote
class ExperimentActor:
    """
    A Ray Actor that is fully asynchronous using 'motor'.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        # Use the Async client
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]

        self.write_scope = write_scope     # "stats_dashboard"
        self.read_scopes = read_scopes     # ["stats_dashboard", "click_tracker"]

        self.logs_collection = self.db[f"{self.write_scope}_logs"]
        
        self.click_tracker_collection = None
        if "click_tracker" in self.read_scopes:
            self.click_tracker_collection = self.db["click_tracker_clicks"]
        
        logger.info(
            f"[StatsDashboardActor] initialized with write_scope='{self.write_scope}' "
            f"and read_scopes={self.read_scopes} (DB='{db_name}')"
        )
        if self.click_tracker_collection:
             logger.info(f"[StatsDashboardActor] Has read access to 'click_tracker_clicks'")

    # Method must be async
    async def fetch_and_log_view(self, user_email: str) -> dict:
        """
        1. Counts docs in 'click_tracker_clicks' if it has read access.
        2. Inserts a 'dashboard_view' log into 'stats_dashboard_logs'.
        3. Counts total logs in 'stats_dashboard_logs'.
        """
        result = {"total_clicks": 0, "my_logs": 0, "error": None}

        try:
            # 1. CROSS-EXPERIMENT READ
            if self.click_tracker_collection:
                # All DB calls must be awaited
                result["total_clicks"] = await self.click_tracker_collection.count_documents({})
            else:
                logger.warning("[StatsDashboardActor] No read access to 'click_tracker' scope.")
                result["total_clicks"] = -1 # Indicate no access

            # 2. WRITE (SELF-SCOPED)
            # All DB calls must be awaited
            await self.logs_collection.insert_one({
                "event": "dashboard_view",
                "user": user_email,
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            })

            # 3. READ (SELF-SCOPED)
            # All DB calls must be awaited
            result["my_logs"] = await self.logs_collection.count_documents({})

        except Exception as e:
            logger.error(f"[StatsDashboardActor] DB error: {e}", exc_info=True)
            result["error"] = str(e)

        return result