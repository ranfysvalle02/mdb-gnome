# (This is the NEW "Thin Client")

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Query  # <-- UPDATED
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
    
    FIX: This now checks the application's Ray availability status
    before attempting to retrieve the actor handle.
    """
    def _get_handle(request: Request) -> "ray.actor.ActorHandle":
        # Check global Ray availability state set during main.py lifespan
        if not getattr(request.app.state, "ray_is_available", False):
            logger.error("Ray is globally unavailable, blocking actor handle request.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
                detail="Ray service is unavailable. Check Ray cluster status."
            )
            
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
            # This indicates the actor was not successfully launched (e.g., due to a crash)
            logger.error(f"CRITICAL: Actor '{actor_name}' found no process running.")
            raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
        except Exception as e:
            logger.error(f"Failed to get actor handle '{actor_name}': {e}", exc_info=True)
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
    actor: "ray.actor.ActorHandle" = get_actor_handle(),
    # --- NEW: Add query parameters for channel selection ---
    r: str = Query("heart_rate", alias="r_key"),
    g: str = Query("calories_per_min", alias="g_key"),
    b: str = Query("speed_kph", alias="b_key")
):
    """
    Renders a detail page by calling the actor. (UPDATED)
    Accepts query parameters ?r_key=...&g_key=...&b_key=...
    """
    context = {"url": str(request.url)}
    try:
        # Pass the new keys to the actor
        html = await actor.render_detail_page.remote(
            workout_id, 
            context,
            r_key=r,
            g_key=g,
            b_key=b
        )
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
        # PRESERVE query params on redirect
        redirect_url = request.url_for("show_detail", workout_id=workout_id)
        
        # Append existing query params (r_key, g_key, b_key)
        if request.url.query:
            redirect_url = f"{redirect_url}?{request.url.query}"

        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed for run_analysis: {e}", exc_info=True)
        raise HTTPException(500, f"Actor analysis failed: {e}")

@bp.post("/generate-demo")
async def generate_demo(
    request: Request,
    actor: "ray.actor.ActorHandle" = get_actor_handle()
):
    """
    Calls the Ray Actor to create *multiple* new workout docs for a demo,
    then redirects to the gallery page.
    """
    # --- Configuration ---
    NUM_GENERATIONS = 100
    # ---------------------

    logger.info(f"Initiating bulk generation of {NUM_GENERATIONS} workout docs.")
    
    try:
        # 1. Start all actor calls concurrently
        tasks = [actor.generate_one.remote() for _ in range(NUM_GENERATIONS)]
        
        # 2. Wait for all tasks to complete (This can take a while)
        # Note: We discard the results, as we just want the operation to complete.
        await ray.get(tasks) 
        
        logger.info(f"Successfully generated {NUM_GENERATIONS} workout docs.")

        # Use the request's URL to build a relative path back to the gallery
        redirect_url = request.url_for("show_gallery")
        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed during bulk generation: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate demo docs: {e}")
        
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