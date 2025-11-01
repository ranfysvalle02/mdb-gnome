# (This is the NEW "Thin Client" - NOW FIXED)

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette import status
from typing import Any 

from core_deps import get_scoped_db

try:
    from async_mongo_wrapper import ScopedMongoWrapper
except ImportError:
    from core_deps import ScopedMongoWrapper # Fallback import
      
# --- This is the *only* local import ---
from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# --- Actor Handle Dependency (FIXED) ---
# This now matches the "working" pattern from click_tracker:
# 1. It's an `async def` function, not a factory.
# 2. It `Depends` on `get_scoped_db`.
# 3. It keeps the superior dynamic slug_id logic.

async def get_actor_handle(
    request: Request,
    # This dependency is present in the working click_tracker.
    # It ensures all platform dependencies are ready.
    db: ScopedMongoWrapper = Depends(get_scoped_db) 
) -> "ray.actor.ActorHandle":
    """
    FastAPI Dependency to get the handle for the experiment's
    dedicated Ray actor.
    """
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Ray service is unavailable. Check Ray cluster status."
        )
        
    slug_id = getattr(request.state, "slug_id", None)
    if not slug_id:
        logger.error("Server error: slug_id not found in request state.")
        raise HTTPException(500, "Server error: slug_id not found in request state.")
    
    actor_name = f"{slug_id}-actor"
    
    try:
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' found no process running.")
        raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
    except Exception as e:
        logger.error(f"Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


# --- Thin Client Routes (Pure Forwarders) ---

@bp.get("/", response_class=HTMLResponse)
async def show_gallery(
    request: Request, 
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle) # <-- FIX: Was get_actor_handle()
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle), # <-- FIX: Was get_actor_handle()
    r: str = Query("heart_rate", alias="r_key"),
    g: str = Query("calories_per_min", alias="g_key"),
    b: str = Query("speed_kph", alias="b_key")
):
    """
    Renders the *full HTML page* on initial load, respecting query params.
    """
    context = {"url": str(request.url)}
    try:
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


# ---
# --- NEW: API endpoint to get *only* viz data as JSON
# ---
@bp.get("/workout/{workout_id}/viz", response_class=JSONResponse)
async def get_viz_data(
    request: Request, 
    workout_id: int, 
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle), # <-- FIX: Was get_actor_handle()
    r_key: str = Query(...),
    g_key: str = Query(...),
    b_key: str = Query(...)
):
    """
    Returns only the updated visualization data as JSON for client-side updates.
    """
    try:
        viz_json = await actor.get_dynamic_viz_data.remote(
            workout_id, 
            r_key=r_key,
            g_key=g_key,
            b_key=b_key
        )
        return JSONResponse(content=viz_json)
    except Exception as e:
        logger.error(f"Actor call failed for get_dynamic_viz_data: {e}", exc_info=True)
        raise HTTPException(500, f"Actor viz data failed: {e}")
# --- END NEW ---


@bp.post("/workout/{workout_id}/analyze")
async def analyze_workout(
    request: Request, 
    workout_id: int, 
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle) # <-- FIX: Was get_actor_handle()
):
    """
    Calls the actor to run analysis, then redirects.
    """
    try:
        await actor.run_analysis.remote(workout_id)
        redirect_url = request.url_for("show_detail", workout_id=workout_id)
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle) # <-- FIX: Was get_actor_handle()
):
    """
    Calls the Ray Actor to create *multiple* new workout docs for a demo.
    """
    NUM_GENERATIONS = 10
    logger.info(f"Initiating bulk generation of {NUM_GENERATIONS} workout docs.")
    try:
        tasks = [actor.generate_one.remote() for _ in range(NUM_GENERATIONS)]
        await ray.get(tasks) 
        logger.info(f"Successfully generated {NUM_GENERATIONS} workout docs.")
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle) # <-- FIX: Was get_actor_handle()
):
    """
    Calls the Ray Actor to create a new workout doc in isolation.
    """
    try:
        new_suffix = await actor.generate_one.remote()
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle) # <-- FIX: Was get_actor_handle()
):
    """
    Calls the actor to clear all docs in the scoped collection.
    """
    try:
        await actor.clear_all.remote()
        redirect_url = request.url_for("show_gallery")
        return RedirectResponse(
            url=redirect_url, 
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        logger.error(f"Actor call failed for clear_all: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to clear docs: {e}")