# File: /app/experiments/click_tracker/__init__.py

import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import Any
from pathlib import Path
import ray
from starlette import status # Import status for 503

# Core dependencies and DB scoping
from core_deps import get_scoped_db

try:
    # Import for type-hinting, though not strictly required
    from async_mongo_wrapper import ScopedMongoWrapper
except ImportError:
    from core_deps import ScopedMongoWrapper # Fallback import
      
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
    # We still need the scoped DB object to get the write/read scopes easily
    db: ScopedMongoWrapper = Depends(get_scoped_db)  
) -> "ray.actor.ActorHandle":  
    """  
    FastAPI dependency to return the handle to our Ray actor.
    
    This relies on main.py's `reload_active_experiments` 
    to ensure the actor is running.
    """  
    
    # 1. Check global Ray availability state set during main.py lifespan
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("[ClickTracker] Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Ray service is unavailable. Check Ray cluster status."
        )

    actor_name = "click_tracker-actor"  
    try:  
        # Attempt to get existing actor (which should be running due to main.py lifecycle)
        # 'modular_labs' is the namespace set in main.py
        handle = ray.get_actor(actor_name, namespace="modular_labs")  
        return handle  
    except ValueError:
        # If the actor is not found, it means the main application failed to start it
        # or the actor crashed. We should treat this as a service outage.
        logger.error(f"[ClickTracker] CRITICAL: Actor '{actor_name}' not found or crashed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"Experiment service '{actor_name}' is not running or crashed."
        )
    except Exception as e:
        logger.error(f"[ClickTracker] Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")
  
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