# (This is the NEW "Thin Client" - NOW FIXED)

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette import status
from typing import Any 

# Core dependencies
# Note: We don't need database dependency here since routes only delegate to actors
# If routes need direct database access, use ExperimentDB via get_experiment_db from core_deps
      
# --- This is the *only* local import ---
from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# --- Actor Handle Dependency ---
# Routes only delegate to actors, so no database dependency needed here.
# If routes need direct database access, use ExperimentDB via get_experiment_db from core_deps.

async def get_actor_handle(
    request: Request
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
    b: str = Query("speed_kph", alias="b_key"),
    alpha_mode: str = Query("none", alias="alpha_mode"),
    alpha_key: str = Query("cadence", alias="alpha_key")
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
            b_key=b,
            alpha_mode=alpha_mode,
            alpha_key=alpha_key
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
    b_key: str = Query(...),
    alpha_mode: str = Query("none", alias="alpha_mode"),
    alpha_key: str = Query("cadence", alias="alpha_key")
):
    """
    Returns only the updated visualization data as JSON for client-side updates.
    """
    try:
        viz_json = await actor.get_dynamic_viz_data.remote(
            workout_id, 
            r_key=r_key,
            g_key=g_key,
            b_key=b_key,
            alpha_mode=alpha_mode,
            alpha_key=alpha_key
        )
        return JSONResponse(content=viz_json)
    except Exception as e:
        logger.error(f"Actor call failed for get_dynamic_viz_data: {e}", exc_info=True)
        raise HTTPException(500, f"Actor viz data failed: {e}")
# --- END NEW ---


# ---
# --- NEW: API endpoint to find similar workouts using custom RGB channels
# ---
@bp.get("/workout/{workout_id}/similar", response_class=JSONResponse)
async def find_similar_workouts(
    request: Request,
    workout_id: int,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    r_key: str = Query(...),
    g_key: str = Query(...),
    b_key: str = Query(...),
    limit: int = Query(3, ge=1, le=10),
    alpha_mode: str = Query("none", alias="alpha_mode"),
    alpha_key: str = Query("cadence", alias="alpha_key")
):
    """
    Finds similar workouts using custom RGB/RGBA channel assignments.
    Generates vectors on-the-fly for the current view and computes similarity in-memory.
    """
    try:
        similar = await actor.find_similar_workouts_custom.remote(
            workout_id,
            r_key=r_key,
            g_key=g_key,
            b_key=b_key,
            limit=limit,
            alpha_mode=alpha_mode,
            alpha_key=alpha_key
        )
        return JSONResponse(content={"similar": similar})
    except Exception as e:
        logger.error(f"Actor call failed for find_similar_workouts_custom: {e}", exc_info=True)
        raise HTTPException(500, f"Actor similarity search failed: {e}")
# --- END NEW ---


# ---
# --- NEW: API endpoint for Vector Magic (vector arithmetic)
# ---
@bp.get("/workout/{workout_id}/vector-magic", response_class=JSONResponse)
async def vector_magic(
    request: Request,
    workout_id: int,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    operation: str = Query(..., regex="^(add|subtract|scale)$"),
    operand_workout_id: int = Query(None),
    scale_factor: float = Query(None),
    r_key: str = Query("heart_rate"),
    g_key: str = Query("calories_per_min"),
    b_key: str = Query("speed_kph"),
    alpha_mode: str = Query("none", alias="alpha_mode"),
    alpha_key: str = Query("cadence", alias="alpha_key"),
    limit: int = Query(5, ge=1, le=10)
):
    """
    Performs vector arithmetic to find abstract concepts.
    
    Examples:
    - operation=subtract&operand_workout_id=5: Vector(workout_id) - Vector(5) = "effort vector"
    - operation=add&operand_workout_id=5: Vector(workout_id) + Vector(5) = "harder version"
    - operation=scale&scale_factor=0.5: Vector(workout_id) × 0.5 = "50% less intense"
    """
    try:
        # Validate parameters based on operation
        if operation in ["add", "subtract"] and operand_workout_id is None:
            raise HTTPException(400, f"operand_workout_id is required for {operation} operation")
        if operation == "scale" and scale_factor is None:
            raise HTTPException(400, "scale_factor is required for scale operation")
        
        result = await actor.vector_magic_search.remote(
            base_workout_id=workout_id,
            operation=operation,
            operand_workout_id=operand_workout_id,
            scale_factor=scale_factor,
            r_key=r_key,
            g_key=g_key,
            b_key=b_key,
            alpha_mode=alpha_mode,
            alpha_key=alpha_key,
            limit=limit
        )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for vector_magic_search: {e}", exc_info=True)
        raise HTTPException(500, f"Vector magic search failed: {e}")
# --- END NEW ---


# ---
# --- NEW: API endpoint to find workouts by intensity (for Vector Magic)
# ---
@bp.get("/workout/{workout_id}/find-by-intensity", response_class=JSONResponse)
async def find_by_intensity(
    request: Request,
    workout_id: int,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    intensity: str = Query(..., regex="^(hard|easy)$"),
    limit: int = Query(5, ge=1, le=10)
):
    """
    Finds workouts by intensity characteristics (hard or easy).
    Used by Vector Magic to auto-populate when neighbors don't have suitable workouts.
    """
    try:
        workouts = await actor.find_workouts_by_intensity.remote(
            intensity=intensity,
            exclude_workout_id=workout_id,
            limit=limit
        )
        return JSONResponse(content={"workouts": workouts})
    except AttributeError as e:
        # Method doesn't exist on actor yet (needs restart) - use fallback
        logger.warning(f"Actor method not available (needs restart): {e}, using fallback")
        try:
            from experiment_db import get_experiment_db
            
            db = await get_experiment_db(request)
            slug_id = getattr(request.state, "slug_id", None)
            collection = db.workouts  # Use ExperimentDB's collection accessor
            
            hard_tags = ["Tempo Pace", "Threshold", "Race Day", "High Intensity Interval"]
            easy_tags = ["Recovery", "Z2 Cardio", "Easy Recovery Run"]
            
            exclude_doc_id = f"workout_rad_{workout_id}"
            
            if intensity == "hard":
                query = {
                    "$and": [
                        {"$or": [
                            {"session_tag": {"$in": hard_tags}},
                            {"rpe": {"$gte": 7}}
                        ]},
                        {"_id": {"$ne": exclude_doc_id}}
                    ]
                }
            else:  # easy
                query = {
                    "$and": [
                        {"$or": [
                            {"session_tag": {"$in": easy_tags}},
                            {"rpe": {"$lte": 4}}
                        ]},
                        {"_id": {"$ne": exclude_doc_id}}
                    ]
                }
            
            workouts_cursor = collection.find(
                query,
                {"_id": 1, "workout_type": 1, "session_tag": 1, "ai_classification": 1, "rpe": 1}
            ).limit(limit)
            
            workouts = []
            async for w in workouts_cursor:
                suffix = w["_id"].split("_")[-1]
                workouts.append({
                    "workout_id": int(suffix),
                    "workout_type": w.get("workout_type", "?"),
                    "session_tag": w.get("session_tag"),
                    "ai_classification": w.get("ai_classification"),
                    "rpe": w.get("rpe"),
                    "score": 1.0
                })
            
            logger.info(f"Fallback found {len(workouts)} {intensity} workouts")
            return JSONResponse(content={"workouts": workouts})
        except Exception as fallback_error:
            logger.error(f"Fallback also failed: {fallback_error}", exc_info=True)
            raise HTTPException(500, f"Failed to find workouts by intensity. Actor needs restart: {e}")
    except Exception as e:
        logger.error(f"Failed to find workouts by intensity: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to find workouts by intensity: {e}")
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