"""
{
  "name": "Stats Dashboard",
  "description": "Aggregates data from other experiments.",
  "status": "active",
  "auth_required": true,
  "data_scope": ["self", "click_tracker"]
}
"""
import os
import datetime
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import Optional, Mapping

# --- Absolute Imports ---
# These ensure the dependencies are found correctly regardless of execution context
from core_deps import get_scoped_db, get_current_user
from async_mongo_wrapper import ScopedMongoWrapper
# --- End Imports ---

logger = logging.getLogger(__name__)

# 1. Define the path to *this* experiment's directory
EXPERIMENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Define a LOCAL templates object pointing to this experiment's
#    local 'templates' folder (e.g., /experiments/stats-dashboard/templates)
templates = Jinja2Templates(directory=os.path.join(EXPERIMENT_DIR, "templates"))
# -------------------------------------------

bp = APIRouter()

# --- Route uses the UNIQUE name ---
@bp.get('/', response_class=HTMLResponse, name="stats-dashboard_index")
async def index(
    # --- Dependency Injection ---
    request: Request,
    # Gets the database wrapper, automatically scoped based on admin config
    db: ScopedMongoWrapper = Depends(get_scoped_db),
    # Gets user info if logged in, otherwise None (doesn't force login)
    current_user: Optional[Mapping] = Depends(get_current_user)
):
    """
    Serves the Stats Dashboard page.
    - Reads click counts from scopes permitted by admin config (e.g., 'click_tracker').
    - Writes a view log entry scoped to 'stats-dashboard'.
    - Reads the count of its own view log entries.
    """

    total_clicks = 0
    my_log_count = 0
    user_email = current_user.get('email', 'unknown') if current_user else "Guest"
    error_message = None # To potentially pass to the template

    try:
        # 1. READ (Cross-Experiment Read - Potentially reads 'click_tracker' data)
        # The 'db' wrapper automatically applies the read scopes defined in the admin panel.
        # If 'click_tracker' is not in the allowed scopes, this will count 0 from that collection.
        total_clicks = await db.clicks.count_documents({}) # Attempts to count in 'clicks' collection

        # 2. WRITE (Scoped to Self - always 'stats-dashboard')
        # The 'db' wrapper automatically adds {"experiment_id": "stats-dashboard"}
        await db.logs.insert_one({
            "event": "dashboard_view",
            "user": user_email,
            "timestamp": datetime.datetime.now(datetime.timezone.utc)
        })

        # 3. READ (Scoped to Self - reads 'stats-dashboard' logs)
        # The 'db' wrapper ensures this only counts logs where {"experiment_id": "stats-dashboard"}
        my_log_count = await db.logs.count_documents({})

    except Exception as e:
        # Catch potential database errors (e.g., connection issue, collection not found if 'clicks' never created)
        logger.error(f"stats-dashboard DB error for user {user_email}: {e}", exc_info=True)
        error_message = f"Database error occurred: {e}"
        # Let counts default to 0

    # Render the template using the LOCAL templates object for this experiment
    return templates.TemplateResponse(
        "index.html", # Looks for experiments/stats-dashboard/templates/index.html
        {
            "request": request,
            "total_clicks": total_clicks,
            "my_logs": my_log_count,
            "current_user": current_user,
            "error_message": error_message # Pass error message to template
        }
    )