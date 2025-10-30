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
from fastapi import APIRouter, Depends, Request  
from fastapi.responses import HTMLResponse  
from fastapi.templating import Jinja2Templates  
  
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
    FastAPI dependency to get (or create) the Stats Dashboard actor.  
    Because 'auth_required' is true, the main config ensures a user is logged in.  
    """  
    actor_name = "stats_dashboard-actor"  
    try:  
        # Attempt to get existing actor by name  
        handle = ray.get_actor(actor_name, namespace="modular_labs")  
        return handle  
    except ValueError:  
        # Actor doesn't exist yet; create a new one  
        logger.info(f"[StatsDashboard] Creating Ray actor '{actor_name}' in 'modular_labs'.")  
        handle = ExperimentActor.options(  
            name=actor_name,  
            namespace="modular_labs",  
            lifetime="detached",  
            get_if_exists=True  
        ).remote(  
            # Pass the real DB connection info  
            mongo_uri=db.real_db.client.address,  # or your known MONGO_URI  
            db_name=db.real_db.name,  
            write_scope=db.write_scope,  
            read_scopes=db.read_scopes  
        )  
        return handle  
  
  
@bp.get("/", response_class=HTMLResponse, name="stats-dashboard_index")  
async def index(  
    request: Request,  
    db: ScopedMongoWrapper = Depends(get_scoped_db),  
    current_user: Optional[Mapping] = Depends(get_current_user),  
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)  
):  
    """  
    Serves the Stats Dashboard page.  
  
    - Reads data from 'clicks' if 'click_tracker' is in data_scope.  
    - Writes a view log entry scoped to 'stats-dashboard'.  
    - Reads the count of its own view log entries.  
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