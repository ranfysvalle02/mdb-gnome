# experiments/click_tracker/__init__.py  
"""  
{  
  "name": "Click Tracker",  
  "description": "Tracks user clicks and generates embeddings.",  
  "status": "active",  
  "auth_required": false,  
  "data_scope": ["self"],  
  "managed_indexes": {  
    "clicks": [  
      {  
        "name": "clicks_vector_embedding_index",  
        "type": "vectorSearch",  
        "definition": {  
          "fields": [  
            {  
              "type": "vector",  
              "path": "user_embedding",  
              "numDimensions": 1536,  
              "similarity": "cosine"  
            }  
          ]  
        }  
      }  
    ]  
  }  
}  
"""  
  
import logging  
from fastapi import APIRouter, Depends, Request, HTTPException  
from fastapi.responses import HTMLResponse, JSONResponse  
from fastapi.templating import Jinja2Templates  
from typing import Any  
from pathlib import Path  
import ray  
  
# Core dependencies and DB scoping  
from core_deps import get_scoped_db  
from async_mongo_wrapper import ScopedMongoWrapper  
  
# Our Ray Actor definition for this experiment  
from .actor import ExperimentActor  
  
logger = logging.getLogger(__name__)  
  
# Path setup for the standard Thin Client patterns  
EXPERIMENT_DIR = Path(__file__).resolve().parent  
templates = Jinja2Templates(directory=str(EXPERIMENT_DIR / "templates"))  
  
# Our main router for this experiment  
bp = APIRouter()  
  
async def get_actor_handle(  
    request: Request,  
    db: ScopedMongoWrapper = Depends(get_scoped_db)  
) -> "ray.actor.ActorHandle":  
    """  
    FastAPI dependency to return the handle to our Ray actor.  
    If the actor isn't found, create it.  
    """  
    actor_name = "click_tracker-actor"  
    try:  
        # Attempt to get existing actor  
        handle = ray.get_actor(actor_name, namespace="modular_labs")  
        return handle  
    except ValueError:  
        # Actor not found in the cluster. Let's create a new one.  
        logger.info(  
            f"[ClickTracker] Creating new Ray actor '{actor_name}' in namespace='modular_labs'..."  
        )  
        # We'll pass the real MongoDB connection info from the ScopedWrapper:  
        handle = ExperimentActor.options(  
            name=actor_name,  
            namespace="modular_labs",  
            lifetime="detached",  
            get_if_exists=True  
        ).remote(  
            mongo_uri=db.real_db.client.address, # Or your known MONGO_URI directly  
            db_name=db.real_db.name,  
            write_scope=db.write_scope,  
            read_scopes=db.read_scopes  
        )  
        return handle  
  
@bp.get("/", response_class=HTMLResponse, name="click_tracker_index")  
async def index(request: Request, actor: Any = Depends(get_actor_handle)):  
    """  
    The main UI route for the Click Tracker experiment.  
    """  
    warning_message = None  
  
    try:  
        # We'll call .remote() on the get_count method to get how many clicks exist  
        click_count = await actor.get_count.remote()  
        if click_count == -1:  
            warning_message = "Error retrieving click count."  
    except Exception as e:  
        logger.error(f"[ClickTracker] index route error: {e}", exc_info=True)  
        click_count = -1  
        warning_message = f"Error rendering page: {e}"  
  
    return templates.TemplateResponse(  
        "index.html",  
        {  
            "request": request,  
            "count": click_count if click_count != -1 else "Error",  
            "warning_message": warning_message  
        }  
    )  
  
@bp.post("/record-click", name="click_tracker_record_click")  
async def record_click(actor: Any = Depends(get_actor_handle)):  
    """  
    API endpoint to record a new click, returning the updated count.  
    """  
    try:  
        new_count = await actor.record_click.remote()  
        if new_count == -1:  
            raise HTTPException(status_code=500, detail="Error recording click.")  
        return {"success": True, "new_count": new_count}  
    except Exception as e:  
        logger.error(f"[ClickTracker] record_click error: {e}", exc_info=True)  
        return JSONResponse(  
            status_code=500,  
            content={"success": False, "error": f"An internal server error occurred: {e}"}  
        )  