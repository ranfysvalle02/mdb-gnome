# File: /app/experiments/click_tracker/actor.py

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
    Ray Actor for handling clicks.
    This version is fully asynchronous using 'motor'.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        # Use the Async client
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]
        
        collection_name = f"{write_scope}_clicks"
        self.collection = self.db[collection_name]

        self.write_scope = write_scope
        self.read_scopes = read_scopes

        logger.info(
            f"[ClickTrackerActor] started with write_scope='{self.write_scope}' "
            f"(DB='{db_name}', Collection='{collection_name}')"
        )

    def _get_scoped_filter(self) -> dict:
        return {} # No filter needed since we're in our own collection

    # Methods must be async
    async def record_click(self) -> int:
        """
        Inserts a new click doc and returns the updated count.
        """
        try:
            # All DB calls must be awaited
            await self.collection.insert_one({
                "event": "button_click",
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            })
            return await self.collection.count_documents(self._get_scoped_filter())
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in record_click: {e}", exc_info=True)
            return -1

    # Methods must be async
    async def get_count(self) -> int:
        """
        Returns how many click docs exist for this experiment (by scope).
        """
        try:
            # All DB calls must be awaited
            return await self.collection.count_documents(self._get_scoped_filter())
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in get_count: {e}", exc_info=True)
            return -1