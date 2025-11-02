# File: experiments/_scaffold_template/__init__.py
# This is a scaffold template for new experiments.
# This directory is excluded from normal experiment discovery (starts with _).

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from ray.actor import ActorHandle
from pathlib import Path

# Import the actor definition (which must be named 'ExperimentActor')
from .actor import ExperimentActor

logger = logging.getLogger(__name__)

bp = APIRouter()

# --- Template Setup ---
# Get the directory of *this* file
EXPERIMENT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = EXPERIMENT_DIR / "templates"
INDEX_HTML_FILE = TEMPLATE_DIR / "index.html"

# Check if the template directory and file exist on startup
if not TEMPLATE_DIR.is_dir():
    logger.critical(f"[ScaffoldTemplate] Template directory NOT FOUND at: {TEMPLATE_DIR}")
if not INDEX_HTML_FILE.is_file():
    logger.critical(f"[ScaffoldTemplate] 'index.html' NOT FOUND at: {INDEX_HTML_FILE}")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def get_actor_handle(request: Request) -> ActorHandle:
    """
    Returns the Ray actor handle created by main.py.
    """
    # 1. Check if Ray is available in the main app state
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("[ScaffoldTemplate] Ray is not marked as available in app.state.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ray Service is unavailable. Check Ray cluster and logs."
        )

    try:
        # 2. This is the *only* path that should work.
        # The actor is started by main.py during the app's startup.
        # Note: This template uses a placeholder slug - replace with your experiment slug
        actor_name = "{slug}-actor"
        return ray.get_actor(actor_name, namespace="modular_labs")

    except Exception as e:
        # 3. If we can't get the actor, it's a critical server error.
        logger.error(f"[ScaffoldTemplate] Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        logger.error("This likely means the actor failed to start during the main app's startup sequence.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error connecting to Ray actor '{actor_name}'."
        )


@bp.get("/", response_class=HTMLResponse)
async def scaffold_template_index(request: Request):
    """
    A simple route that calls the Ray actor and returns its response.
    This is a template - customize for your experiment.
    """
    # 1. Check if the template file actually exists before trying to render
    if not INDEX_HTML_FILE.is_file():
        logger.error(f"[ScaffoldTemplate] Cannot render: 'index.html' not found at {INDEX_HTML_FILE}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Template file 'index.html' is missing from {TEMPLATE_DIR}."
        )

    # 2. Get the actor handle
    actor = get_actor_handle(request)

    # 3. Call the actor method
    try:
        greeting = await actor.say_hello.remote()
    except Exception as e:
        logger.error(f"[ScaffoldTemplate] Actor call 'say_hello' failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ray actor method failed: {e}"
        )

    # 4. Render the template
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "greeting": greeting}
    )

