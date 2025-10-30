# experiments/click_tracker/actor.py  
  
import logging  
import datetime  
from typing import List  
  
import ray  
from pymongo import MongoClient  
  
logger = logging.getLogger(__name__)  
  
@ray.remote  
class ExperimentActor:  
    """  
    Ray Actor for handling clicks.   
    Ray is always installed, so no fallback is needed.  
    """  
  
    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):  
        self.client = MongoClient(mongo_uri)  
        self.db = self.client[db_name]  
        # We'll store clicks in a "clicks" collection  
        self.collection = self.db.clicks  
  
        self.write_scope = write_scope  
        self.read_scopes = read_scopes  
  
        logger.info(  
            f"[ClickTrackerActor] started with write_scope='{self.write_scope}' "  
            f"(DB='{db_name}')"  
        )  
  
    def _get_scoped_filter(self) -> dict:  
        return {"experiment_id": self.write_scope}  
  
    def record_click(self) -> int:  
        """  
        Inserts a new click doc and returns the updated count.  
        """  
        try:  
            self.collection.insert_one({  
                "event": "button_click",  
                "timestamp": datetime.datetime.now(datetime.timezone.utc),  
                "experiment_id": self.write_scope  
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