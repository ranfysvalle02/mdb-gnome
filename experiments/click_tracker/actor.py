# File: /app/experiments/click_tracker/actor.py

import logging
import datetime
from typing import List
import ray

from pymongo import MongoClient

logger = logging.getLogger(__name__)

# DO NOT add the @ray.remote decorator here
class ExperimentActor:
    """
    Ray Actor for handling clicks.
    This is a plain Python class. main.py is responsible for
    initializing it as a Ray actor.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        self.client = MongoClient(mongo_uri)
        self.db = self.client[db_name]
        
        # We'll store clicks in a collection named after the write_scope's suffix
        # e.g., "click_tracker_clicks"
        collection_name = f"{write_scope}_clicks"
        self.collection = self.db[collection_name]

        self.write_scope = write_scope
        self.read_scopes = read_scopes

        logger.info(
            f"[ClickTrackerActor] started with write_scope='{self.write_scope}' "
            f"(DB='{db_name}', Collection='{collection_name}')"
        )

    def _get_scoped_filter(self) -> dict:
        # Note: Your original code stored clicks in the global 'clicks' collection
        # and filtered by 'experiment_id'.
        # This implementation stores them in a dedicated 'click_tracker_clicks'
        # collection, which is cleaner and removes the need to filter.
        # If you want the old behavior, change self.collection back to self.db.clicks
        # and return {"experiment_id": self.write_scope}
        return {} # No filter needed since we're in our own collection

    def record_click(self) -> int:
        """
        Inserts a new click doc and returns the updated count.
        """
        try:
            self.collection.insert_one({
                "event": "button_click",
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                # "experiment_id" is no longer needed if using a scoped collection
            })
            return self.collection.count_documents(self._get_scoped_filter())
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in record_click: {e}", exc_info=True)
            return -1

    def get_count(self) -> int:
        """
        Returns how many click docs exist for this experiment (by scope).
        """
        try:
            return self.collection.count_documents(self._get_scoped_filter())
        except Exception as e:
            logger.error(f"[ClickTrackerActor] error in get_count: {e}", exc_info=True)
            return -1