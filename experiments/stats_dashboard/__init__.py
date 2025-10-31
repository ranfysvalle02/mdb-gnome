# experiments/stats_dashboard/__init__.py
"""
{
  "name": "Stats Dashboard",
  "description": "Aggregates data from other experiments.",
  "status": "active",
  "auth_required": true,
  "data_scope": ["self", "click_tracker"]
}
"""

import logging
import os
from typing import Optional, Mapping

import ray
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette import status # Import status for HTTP error codes

# Core Dependencies for DB & Auth
from core_deps import get_scoped_db, get_current_user
from async_mongo_wrapper import ScopedMongoWrapper

# IMPORTANT: Import the "ExperimentActor" (renamed from StatsDashboardActor)
from .actor import ExperimentActor

logger = logging.getLogger(__name__)

# 1. LOCAL Path/Template Setup
EXPERIMENT_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(EXPERIMENT_DIR, "templates"))

bp = APIRouter()


async def get_actor_handle(
    request: Request,
    db: ScopedMongoWrapper = Depends(get_scoped_db)
) -> "ray.actor.ActorHandle":
    """
    FastAPI dependency to get the Stats Dashboard actor.

    FIX: Added a gatekeeper check for Ray availability to prevent ConnectionError.
    FIX: Removed the complex fallback creation, relying on main.py's
         `reload_active_experiments` to ensure the actor is running.
    """
    
    # 1. FIX: Check global Ray availability state set during main.py lifespan
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("[StatsDashboard] Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Ray service is unavailable. Check Ray cluster status."
        )

    actor_name = "stats_dashboard-actor"
    try:
        # Attempt to get existing actor by name
        # 'modular_labs' is the namespace set in main.py
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        # If the actor is not found, it means the main application failed to start it
        # or the actor crashed. We should treat this as a service outage.
        logger.error(f"[StatsDashboard] CRITICAL: Actor '{actor_name}' not found or crashed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"Experiment service '{actor_name}' is not running or crashed."
        )
    except Exception as e:
        logger.error(f"[StatsDashboard] Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


@bp.get("/", response_class=HTMLResponse, name="stats-dashboard_index")
async def index(
    request: Request,
    db: ScopedMongoWrapper = Depends(get_scoped_db),
    current_user: Optional[Mapping] = Depends(get_current_user),
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """
    Serves the Stats Dashboard page.
    """
    user_email = current_user["email"] if current_user else "Guest"
    error_message = None
    total_clicks = 0
    my_log_count = 0

    try:
        # Single call to the actor
        future = actor.fetch_and_log_view.remote(user_email)
        result_data = await future
        total_clicks = result_data.get("total_clicks", 0)
        my_log_count = result_data.get("my_logs", 0)
        error_message = result_data.get("error")
    except Exception as e:
        logger.error(f"[StatsDashboard] index route error: {e}", exc_info=True)
        error_message = f"Actor error: {e}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "total_clicks": total_clicks,
            "my_logs": my_log_count,
            "current_user": current_user,
            "error_message": error_message
        }
    )