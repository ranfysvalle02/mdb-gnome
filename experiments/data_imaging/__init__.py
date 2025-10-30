# (This is the NEW "Thin Client")

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette import status

# --- This is the *only* local import ---
# It's safe because actor.py uses lazy-loading in its class.
# We just need the class *definition* for main.py to launch it.
from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# --- Actor Handle Dependency ---

def get_actor_handle():
    """
    FastAPI Dependency to get the handle for the experiment's
    dedicated Ray actor.
    """
    def _get_handle(request: Request) -> "ray.actor.ActorHandle":
        # Get slug_id from middleware
        slug_id = getattr(request.state, "slug_id", None)
        if not slug_id:
            logger.error("Server error: slug_id not found in request state.")
            raise HTTPException(500, "Server error: slug_id not found in request state.")
        
        actor_name = f"{slug_id}-actor"
        
        try:
            # 'modular_labs' is the namespace set in main.py
            handle = ray.get_actor(actor_name, namespace="modular_labs")
            return handle
        except ValueError:
            logger.error(f"CRITICAL: Actor '{actor_name}' not found.")
            raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
        except Exception as e:
            logger.error(f"Failed to get actor handle '{actor_name}': {e}")
            raise HTTPException(500, "Error connecting to experiment service.")

    return Depends(_get_handle)


# --- Thin Client Routes (Pure Forwarders) ---

@bp.get("/", response_class=HTMLResponse)
async def show_gallery(
    request: Request, 
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Renders the main gallery by calling the actor.
    """
    context = {"url": str(request.url)}
    try:
        html = await actor.render_gallery_page.remote(context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_gallery_page: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/workout/{workout_id}", response_class=HTMLResponse)
async def show_detail(
    request: Request, 
    workout_id: int, 
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Renders a detail page by calling the actor.
    """
    context = {"url": str(request.url)}
    try:
        html = await actor.render_detail_page.remote(workout_id, context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_detail_page: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/workout/{workout_id}/analyze")
async def analyze_workout(
    request: Request, 
    workout_id: int, 
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Calls the actor to run analysis, then redirects.
    """
    try:
        await actor.run_analysis.remote(workout_id)
        # Use the request's URL to build a relative path back to the detail page
        redirect_url = request.url_for("show_detail", workout_id=workout_id)
        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed for run_analysis: {e}", exc_info=True)
        raise HTTPException(500, f"Actor analysis failed: {e}")


@bp.post("/generate")
async def generate_one(
    request: Request, 
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Calls the Ray Actor to create a new workout doc in isolation.
    """
    try:
        new_suffix = await actor.generate_one.remote()
        # Use the request's URL to build a relative path to the new detail page
        redirect_url = request.url_for("show_detail", workout_id=new_suffix)
        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed for generate_one: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate doc: {e}")


@bp.post("/clear")
async def clear_all(
    request: Request, 
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Calls the actor to clear all docs in the scoped collection.
    """
    try:
        await actor.clear_all.remote()
        # Use the request's URL to build a relative path back to the gallery
        redirect_url = request.url_for("show_gallery")
        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed for clear_all: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to clear docs: {e}")