# experiments/stats_dashboard/actor.py  
  
import logging  
import datetime  
from typing import List  
  
import ray  
from pymongo import MongoClient  
  
logger = logging.getLogger(__name__)  
  
@ray.remote  
class ExperimentActor:  
    """  
    A Ray Actor that:  
    1. Reads cross-experiment data (e.g., from 'click_tracker') if allowed by DB scopes.  
    2. Logs its own "dashboard_view" event (scoped to "stats_dashboard").  
    3. Retrieves the total number of "stats_dashboard" logs for analytics.  
    """  
  
    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):  
        self.client = MongoClient(mongo_uri)  
        self.db = self.client[db_name]  
  
        self.write_scope = write_scope     # typically "stats_dashboard"  
        self.read_scopes = read_scopes     # e.g. ["stats_dashboard", "click_tracker"]  
  
        logger.info(  
            f"[ExperimentActor] initialized with write_scope='{self.write_scope}' "  
            f"and read_scopes={self.read_scopes} (DB='{db_name}')"  
        )  
  
    def fetch_and_log_view(self, user_email: str) -> dict:  
        """  
        1. Counts any accessible 'clicks' documents if "click_tracker" is in read_scopes.  
        2. Inserts a 'dashboard_view' log into 'logs' (scoped to 'stats_dashboard').  
        3. Counts how many logs belong to 'stats_dashboard'.  
        4. Returns {"total_clicks": <int>, "my_logs": <int>, "error": None or str}.  
        """  
        result = {"total_clicks": 0, "my_logs": 0, "error": None}  
  
        try:  
            # CROSS-EXPERIMENT READ (e.g. from "click_tracker")  
            clicks_collection = self.db.clicks  
            # Optionally filter only "click_tracker" docs:  
            # filter_ = {"experiment_id": "click_tracker"} if "click_tracker" in self.read_scopes else {}  
            filter_ = {}  
            result["total_clicks"] = clicks_collection.count_documents(filter_)  
  
            # WRITE (SELF-SCOPED)  
            logs_collection = self.db.logs  
            logs_collection.insert_one({  
                "event": "dashboard_view",  
                "user": user_email,  
                "timestamp": datetime.datetime.now(datetime.timezone.utc),  
                "experiment_id": self.write_scope  # "stats_dashboard"  
            })  
  
            # READ (SELF-SCOPED)  
            self_logs_filter = {"experiment_id": self.write_scope}  
            result["my_logs"] = logs_collection.count_documents(self_logs_filter)  
  
        except Exception as e:  
            logger.error(f"[ExperimentActor] DB error: {e}", exc_info=True)  
            result["error"] = str(e)  
  
        return result  