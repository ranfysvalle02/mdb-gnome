"""  
{  
  "name": "Simple Click Tracker",  
  "description": "Records its own page visits. Has no access to other data.",  
  "status": "active",  
  "auth_required": false,  
  "data_scope": "self"  
}  
"""  
  
import logging  
import os  
import datetime  
  
# --- 1) Attempt to import Ray ---  
ray = None  
try:  
    import ray  
    RAY_IMPORT_SUCCESS = True  
except ImportError:  
    RAY_IMPORT_SUCCESS = False  
    logging.getLogger(__name__).warning("⚠️ Ray library not found. Running in non-distributed mode.")  
except Exception as e:  
    RAY_IMPORT_SUCCESS = False  
    logging.getLogger(__name__).error(f"❌ Unexpected error during Ray import: {e}", exc_info=False)  
  
from fastapi import APIRouter, Depends, Request, HTTPException  
from fastapi.responses import HTMLResponse, JSONResponse  
from fastapi.templating import Jinja2Templates  
from typing import List, Union, Any  
from pathlib import Path  
  
# --- Absolute Imports ---  
from core_deps import get_scoped_db  
from async_mongo_wrapper import ScopedMongoWrapper  
# --- End Imports ---  
  
logger = logging.getLogger(__name__)  
  
# --- 2) Define a conditional decorator so we don't break if 'ray' is None ---  
def _maybe_remote(cls):  
    """  
    If Ray is installed, return ray.remote(cls).  
    Otherwise, return the class unmodified  
    (so it won't crash with "'NoneType' object has no attribute 'remote'").  
    """  
    if RAY_IMPORT_SUCCESS and ray is not None:  
        return ray.remote(cls)  
    return cls  
  
  
# --- 3) Paths / Templates Setup ---  
EXPERIMENT_DIR = Path(__file__).resolve().parent  
EXPERIMENTS_ROOT = EXPERIMENT_DIR.parent  
templates = Jinja2Templates(directory=os.path.join(str(EXPERIMENT_DIR), "templates"))  
  
# -------------------------------------------  
# 4) Ray Actor (Only works if Ray is installed)  
# -------------------------------------------  
@_maybe_remote  
class ExperimentActor:  
    """  
    Ray Actor for handling clicks when the Ray cluster is available.  
    If Ray isn't installed, this class just acts as a normal Python class  
    (the local fallback is used instead).  
    """  
    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):  
        try:  
            from pymongo import MongoClient  
            self.client = MongoClient(mongo_uri)  
            self.db = self.client[db_name]  
            self.collection = self.db.clicks  
            self.write_scope = write_scope  
            self.read_scopes = read_scopes  
            logger.info(f"Ray Actor 'ClickTrackerActor' started. Write Scope: {self.write_scope}")  
        except ImportError as e:  
            logger.critical(f"ClickTrackerActor failed to import pymongo: {e}. Check runtime_env/requirements.")  
            raise e  
        except Exception as e:  
            logger.critical(f"ClickTrackerActor failed during init: {e}", exc_info=True)  
            raise e  
  
    def _get_scoped_filter(self) -> dict:  
        return {"experiment_id": self.write_scope}  
  
    def record_click(self) -> int:  
        try:  
            self.collection.insert_one({  
                "event": "button_click",  
                "timestamp": datetime.datetime.now(datetime.timezone.utc),  
                "experiment_id": self.write_scope  
            })  
            return self.collection.count_documents(self._get_scoped_filter())  
        except Exception as e:  
            logger.error(f"ClickTrackerActor error in record_click: {e}", exc_info=True)  
            return -1  
  
    def get_count(self) -> int:  
        try:  
            return self.collection.count_documents(self._get_scoped_filter())  
        except Exception as e:  
            logger.error(f"ClickTrackerActor error in get_count: {e}", exc_info=True)  
            return -1  
  
  
# -------------------------------------------  
# 6) Local Fallback Service (when Ray is OFF)  
# -------------------------------------------  
class ClickTrackerLocalService:  
    """  
    A non-Ray fallback that mimics the Actor's interface  
    but uses the async ScopedMongoWrapper for I/O.  
    """  
    def __init__(self, db: ScopedMongoWrapper):  
        self.db = db  
        logger.debug("Initialized ClickTrackerLocalService (Ray fallback)")  
  
    async def record_click(self) -> int:  
        try:  
            await self.db.clicks.insert_one({  
                "event": "button_click",  
                "timestamp": datetime.datetime.now(datetime.timezone.utc),  
            })  
            return await self.db.clicks.count_documents({})  
        except Exception as e:  
            logger.error(f"ClickTrackerLocalService error in record_click: {e}", exc_info=True)  
            return -1  
  
    async def get_count(self) -> int:  
        try:  
            return await self.db.clicks.count_documents({})  
        except Exception as e:  
            logger.error(f"ClickTrackerLocalService error in get_count: {e}", exc_info=True)  
            return -1  
  
  
# -------------------------------------------  
# 7) The Agnostic Service Dependency  
# -------------------------------------------  
async def get_click_service(  
    request: Request,  
    db: ScopedMongoWrapper = Depends(get_scoped_db)  
) -> Union[ExperimentActor, ClickTrackerLocalService]:  
    """  
    FastAPI Dependency: Provides the Ray Actor if Ray is available;  
    otherwise the local fallback service.  
    """  
    actor_name = "click_tracker-actor"  
    if request.app.state.ray_is_available:  
        try:  
            actor_handle = ray.get_actor(actor_name, namespace="modular_labs")  
            logger.debug(f"Using Ray Actor for {actor_name}")  
            return actor_handle  
        except ValueError as e:  
            logger.error(  
                f"Ray is available, but failed to get actor '{actor_name}': {e}. "  
                "Check Ray logs for actor startup errors. Using local fallback."  
            )  
            return ClickTrackerLocalService(db)  
        except Exception as e:  
            logger.error(  
                f"Unexpected error getting Ray actor '{actor_name}': {e}. Using local fallback.",  
                exc_info=True  
            )  
            return ClickTrackerLocalService(db)  
    else:  
        logger.warning("Ray is unavailable. Using local fallback service for click_tracker.")  
        return ClickTrackerLocalService(db)  
  
  
# -------------------------------------------  
# 8) Define the APIRouter (The "Thin Client")  
# -------------------------------------------  
bp = APIRouter()  
  
@bp.get('/', response_class=HTMLResponse, name="click_tracker_index")  
async def index(  
    request: Request,  
    service: Any = Depends(get_click_service)  
):  
    """  
    The main UI route for the Click Tracker experiment.  
    """  
    warning_message = None  
    my_click_count = -1  
  
    try:  
        if isinstance(service, ClickTrackerLocalService):  
            warning_message = "Ray compute cluster is offline or actor failed. Running in local mode."  
            my_click_count = await service.get_count()  
        else:  
            my_click_count = await service.get_count.remote()  
        if my_click_count == -1:  
            warning_message = (warning_message or "") + " Error retrieving click count."  
    except Exception as e:  
        logger.error(f"Error in click_tracker index route: {e}", exc_info=True)  
        warning_message = f"Error rendering page: {e}"  
        my_click_count = -1  
  
    return templates.TemplateResponse(  
        'index.html',  
        {  
            "request": request,  
            "count": my_click_count if my_click_count != -1 else "Error",  
            "warning_message": warning_message  
        }  
    )  
  
  
@bp.post('/record-click', name="click_tracker_record_click")  
async def record_click(service: Any = Depends(get_click_service)):  
    """  
    API endpoint to record a new click.  
    """  
    try:  
        if isinstance(service, ClickTrackerLocalService):  
            new_click_count = await service.record_click()  
        else:  
            new_click_count = await service.record_click.remote()  
  
        if new_click_count == -1:  
            raise HTTPException(status_code=500, detail="Error recording click.")  
  
        return {"success": True, "new_count": new_click_count}  
    except Exception as e:  
        logger.error(f"Error in record_click endpoint: {e}", exc_info=True)  
        return JSONResponse(  
            status_code=500,  
            content={"success": False, "error": f"An internal server error occurred: {e}"}  
        )  