"""
Story Weaver Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from typing import Optional, Dict, Any, List

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# Note: Authentication is handled at router level via manifest.json auth_required: true
# Router-level dependency ensures authentication. Routes access user from request.state
# We avoid importing core_deps at module level to prevent circular imports


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Story Weaver actor handle."""
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


# --- Core Application Routes ---
# IMPORTANT: Authentication is handled by the top-level auth system
# - Users MUST be created by admins at the top level (via admin panel)
# - Story Weaver does NOT provide registration functionality
# - Users login at /login (root level), not within Story Weaver
# - The experiment uses get_current_user_or_redirect which redirects to top-level login
# - All user_id values come from top-level users collection
async def _get_user_from_request(request: Request) -> Dict[str, Any]:
    """Helper to get authenticated user from request cookie. Lazy import to avoid circular dependency."""
    from core_deps import SECRET_KEY
    import jwt as jwt_lib
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        payload = jwt_lib.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt_lib.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt_lib.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@bp.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Display workspace with all projects. Uses top-level authentication."""
    # Router-level dependency (auth_required: true) ensures authentication
    # Get user from cookie
    user = await _get_user_from_request(request)
    try:
        actor = await get_actor_handle(request)
        html = await actor.render_index.remote(user.get("user_id"))
        return HTMLResponse(html)
    except HTTPException as e:
        # Re-raise HTTP exceptions (like 503 for actor not available)
        raise e
    except Exception as e:
        logger.error(f"Actor call failed for render_index: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/project/{project_id}", response_class=HTMLResponse)
async def project_view(
    request: Request,
    project_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Display project view."""
    user = await _get_user_from_request(request)
    try:
        html = await actor.render_project_view.remote(user.get("user_id"), project_id)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_project_view: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/invite/{invite_token}", response_class=HTMLResponse, dependencies=[])
async def invite_note(
    request: Request,
    invite_token: str
):
    """Display invite page for contributors."""
    try:
        actor = await get_actor_handle(request)
        html = await actor.render_invite_page.remote(invite_token)
        return HTMLResponse(html)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Actor call failed for render_invite_page: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/share/{share_token_param}", response_class=HTMLResponse, dependencies=[])
async def shared_invite_page(
    request: Request,
    share_token_param: str
):
    """Display shared invite page."""
    try:
        actor = await get_actor_handle(request)
        html = await actor.render_shared_invite_page.remote(share_token_param)
        return HTMLResponse(html)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Actor call failed for render_shared_invite_page: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/quiz/{share_token}", response_class=HTMLResponse, dependencies=[])
async def shared_quiz_page(
    request: Request,
    share_token: str
):
    """Display shared quiz page."""
    try:
        actor = await get_actor_handle(request)
        html = await actor.render_shared_quiz_page.remote(share_token)
        return HTMLResponse(html)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Actor call failed for render_shared_quiz_page: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


# --- API Routes ---
@bp.post("/api/projects")
async def create_project(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Create a new project."""
    try:
        logger.info("create_project: Request received")
        user = await _get_user_from_request(request)
        logger.info(f"create_project: User authenticated: {user.get('email', 'unknown')}")
        
        user_id = user.get("user_id")
        if not user_id:
            logger.error(f"create_project: No user_id found in JWT token. User payload: {user}")
            raise HTTPException(status_code=400, detail="Invalid user authentication. Please log in again.")
        
        data = await request.json()
        logger.info(f"create_project: Request data: {data}")
        
        if not data.get("name"):
            raise HTTPException(status_code=400, detail="Project name is required")
        if not data.get("project_goal"):
            raise HTTPException(status_code=400, detail="Project goal is required")
        
        logger.info(f"create_project: Calling actor with user_id={user_id}, name={data.get('name')}, goal={data.get('project_goal')}, type={data.get('project_type', 'story')}")
        
        result = await actor.create_project.remote(
            user_id,
            data.get("name"),
            data.get("project_goal"),
            data.get("project_type", "story")
        )
        
        logger.info(f"create_project: Actor returned: {result}")
        
        if result.get("status") == "error":
            logger.error(f"Actor returned error for create_project: {result.get('message')}")
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to create project"))
        
        return JSONResponse(result)
    except HTTPException as e:
        logger.error(f"create_project: HTTPException: {e.status_code} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Actor call failed for create_project: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Actor failed to create project: {str(e)}")


@bp.delete("/api/projects/{project_id}")
async def delete_project(
    request: Request,
    project_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Delete a project."""
    try:
        logger.info(f"delete_project: Request received for project_id={project_id}")
        user = await _get_user_from_request(request)
        user_id = user.get("user_id")
        
        if not user_id:
            logger.error(f"delete_project: No user_id found in JWT token. User payload: {user}")
            raise HTTPException(status_code=400, detail="Invalid user authentication. Please log in again.")
        
        logger.info(f"delete_project: Calling actor with user_id={user_id}, project_id={project_id}")
        result = await actor.delete_project.remote(user_id, project_id)
        logger.info(f"delete_project: Actor returned: {result}")
        
        if result.get("status") == "error":
            logger.error(f"Actor returned error for delete_project: {result.get('message')}")
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to delete project"))
        
        return JSONResponse(result)
    except HTTPException as e:
        logger.error(f"delete_project: HTTPException: {e.status_code} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Actor call failed for delete_project: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Actor failed to delete project: {str(e)}")


@bp.post("/api/notes")
async def add_note(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Add a new note."""
    try:
        # Verify user is authenticated
        user = await _get_user_from_request(request)
        user_id = user.get("user_id")
        if not user_id:
            logger.error(f"add_note: No user_id found in JWT token. User payload: {user}")
            raise HTTPException(status_code=400, detail="Invalid user authentication. Please log in again.")
        
        data = await request.json()
        logger.info(f"add_note: Request data: {data}")
        
        result = await actor.add_note.remote(data)
        
        if result.get("status") == "error":
            logger.error(f"Actor returned error for add_note: {result.get('message')}")
            raise HTTPException(status_code=500, detail=result.get("message", "Failed to add note"))
        
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for add_note: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Actor failed to add note: {str(e)}")


@bp.get("/api/notes/{project_id}")
async def get_notes(
    request: Request,
    project_id: str,
    page: int = Query(1),
    contributor_filter: Optional[str] = Query(None),
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get notes for a project."""
    user = await _get_user_from_request(request)
    try:
        notes = await actor.get_notes.remote(
            user.get("user_id"),
            project_id,
            page,
            contributor_filter
        )
        return JSONResponse(notes)
    except Exception as e:
        logger.error(f"Actor call failed for get_notes: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to get notes: {e}")


@bp.put("/api/notes/{note_id}")
async def update_note(
    request: Request,
    note_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Update a note."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.update_note.remote(user.get("user_id"), note_id, data)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_note: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to update note: {e}")


@bp.delete("/api/notes/{note_id}")
async def delete_note(
    request: Request,
    note_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Delete a note."""
    user = await _get_user_from_request(request)
    try:
        result = await actor.delete_note.remote(user.get("user_id"), note_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for delete_note: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to delete note: {e}")


@bp.post("/api/generate-token")
async def generate_token(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate an invite token."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_invite_token.remote(
            user.get("user_id"),
            data.get("project_id"),
            data.get("label"),
            data.get("prompt")
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_invite_token: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate token: {e}")


@bp.post("/api/generate-shared-token")
async def generate_shared_token(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate a shared invite token."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_shared_token.remote(
            user.get("user_id"),
            data.get("project_id"),
            data.get("prompt")
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_shared_token: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate shared token: {e}")


@bp.post("/api/suggest-tags")
async def suggest_tags(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get AI-suggested tags."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.suggest_tags.remote(
            user.get("user_id"),
            data.get("project_id"),
            data.get("content")
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for suggest_tags: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to suggest tags: {e}")


@bp.get("/api/search-notes/{project_id}")
async def search_notes(
    request: Request,
    project_id: str,
    q: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    search_type: Optional[str] = Query("text"),
    page: int = Query(1),
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Search notes."""
    user = await _get_user_from_request(request)
    try:
        result = await actor.search_notes.remote(
            user.get("user_id"),
            project_id,
            q,
            tags,
            search_type,
            page
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for search_notes: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to search notes: {e}")


@bp.get("/api/get-tags/{project_id}")
async def get_tags(
    request: Request,
    project_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get all tags for a project."""
    user = await _get_user_from_request(request)
    try:
        tags = await actor.get_tags.remote(user.get("user_id"), project_id)
        return JSONResponse(tags)
    except Exception as e:
        logger.error(f"Actor call failed for get_tags: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to get tags: {e}")


@bp.get("/api/contributors/{project_id}")
async def get_contributors(
    request: Request,
    project_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get all contributors for a project."""
    user = await _get_user_from_request(request)
    try:
        contributors = await actor.get_contributors.remote(user.get("user_id"), project_id)
        return JSONResponse(contributors)
    except Exception as e:
        logger.error(f"Actor call failed for get_contributors: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to get contributors: {e}")


@bp.post("/api/generate-notes")
async def generate_notes(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate AI study notes."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_notes.remote(
            user.get("user_id"),
            data.get("project_id"),
            data.get("topic"),
            data.get("num_notes", 5),
            data.get("use_web_search", False)
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_notes: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate notes: {e}")


@bp.post("/api/generate-quiz")
async def generate_quiz(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate a quiz from notes."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_quiz.remote(
            user.get("user_id"),
            data
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_quiz: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate quiz: {e}")


@bp.post("/api/generate-story")
async def generate_story(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate a story from notes."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_story.remote(
            user.get("user_id"),
            data
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_story: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate story: {e}")


@bp.post("/api/generate-study-guide")
async def generate_study_guide(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate a study guide from notes."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_study_guide.remote(
            user.get("user_id"),
            data
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_study_guide: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate study guide: {e}")


@bp.post("/api/generate-song")
async def generate_song(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate song lyrics from notes."""
    user = await _get_user_from_request(request)
    try:
        data = await request.json()
        result = await actor.generate_song.remote(
            user.get("user_id"),
            data
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_song: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate song: {e}")

