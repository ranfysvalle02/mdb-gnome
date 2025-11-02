# File: /app/experiments/indexing_demo/__init__.py

import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import Any
from pathlib import Path
import ray
from starlette import status

from .actor import ExperimentActor

logger = logging.getLogger(__name__)

EXPERIMENT_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(EXPERIMENT_DIR / "templates"))

bp = APIRouter()


async def get_actor_handle(
    request: Request
) -> "ray.actor.ActorHandle":
    """
    FastAPI dependency to return the handle to our Ray actor.
    """
    
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("[IndexingDemo] Ray is globally unavailable.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Ray service is unavailable. Check Ray cluster status."
        )

    actor_name = "indexing_demo-actor"
    try:
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        logger.error(f"[IndexingDemo] Actor '{actor_name}' not found.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"Experiment service '{actor_name}' is not running."
        )
    except Exception as e:
        logger.error(f"[IndexingDemo] Failed to get actor handle: {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


@bp.get("/", response_class=HTMLResponse, name="indexing_demo_index")
async def index(request: Request, actor: Any = Depends(get_actor_handle)):
    """
    Main demo page showing all index types and sample data.
    """
    try:
        stats = await actor.get_stats.remote()
    except Exception as e:
        logger.error(f"[IndexingDemo] index route error: {e}", exc_info=True)
        stats = {"error": str(e)}

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": stats
        }
    )


@bp.post("/seed-data", name="indexing_demo_seed")
async def seed_data(actor: Any = Depends(get_actor_handle)):
    """
    Seed sample data for all index types.
    """
    try:
        result = await actor.seed_sample_data.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] seed_data error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-regular", name="indexing_demo_test_regular")
async def test_regular_index(actor: Any = Depends(get_actor_handle)):
    """
    Test regular index queries (unique, compound).
    """
    try:
        result = await actor.test_regular_indexes.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_regular_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-text", name="indexing_demo_test_text")
async def test_text_index(actor: Any = Depends(get_actor_handle)):
    """
    Test text index queries.
    """
    try:
        result = await actor.test_text_index.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_text_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-geospatial", name="indexing_demo_test_geospatial")
async def test_geospatial_index(actor: Any = Depends(get_actor_handle)):
    """
    Test geospatial index queries.
    """
    try:
        result = await actor.test_geospatial_index.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_geospatial_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-partial", name="indexing_demo_test_partial")
async def test_partial_index(actor: Any = Depends(get_actor_handle)):
    """
    Test partial index queries.
    """
    try:
        result = await actor.test_partial_index.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_partial_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-vector", name="indexing_demo_test_vector")
async def test_vector_index(actor: Any = Depends(get_actor_handle)):
    """
    Test vector search index queries.
    """
    try:
        result = await actor.test_vector_index.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_vector_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.get("/test-ttl", name="indexing_demo_test_ttl")
async def test_ttl_index(actor: Any = Depends(get_actor_handle)):
    """
    Test TTL index (sessions should expire automatically).
    """
    try:
        result = await actor.test_ttl_index.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] test_ttl_index error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@bp.delete("/clear-data", name="indexing_demo_clear")
async def clear_data(actor: Any = Depends(get_actor_handle)):
    """
    Clear all demo data.
    """
    try:
        result = await actor.clear_all_data.remote()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[IndexingDemo] clear_data error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )

