"""
Password Manager Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from typing import Optional, Dict, Any
from bson import ObjectId

from .actor import ExperimentActor
from experiment_auth_restrictions import block_demo_users

logger = logging.getLogger(__name__)
bp = APIRouter()


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Password Manager actor handle."""
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


async def get_user_from_request(request: Request) -> Dict[str, Any]:
    """Get authenticated user from sub-auth session."""
    from sub_auth import get_experiment_sub_user
    from experiment_db import get_experiment_db
    from core_deps import get_experiment_config
    
    slug_id = getattr(request.state, "slug_id", "pwd_zero")
    
    config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
    if not config:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    
    sub_auth = config.get("sub_auth", {})
    if not sub_auth.get("enabled", False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    
    db = await get_experiment_db(request)
    experiment_user = await get_experiment_sub_user(request, slug_id, db, config)
    
    if not experiment_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required. Please log in.")
    
    return {
        "user_id": str(experiment_user.get("_id")),
        "email": experiment_user.get("email"),
        "experiment_user_id": str(experiment_user.get("_id")),
        "username": experiment_user.get("username")
    }


def get_encryption_key_from_session(request: Request) -> Optional[str]:
    """Get encryption key from session cookie."""
    encryption_key_cookie = request.cookies.get("pwd_zero_encryption_key")
    return encryption_key_cookie


def set_encryption_key_cookie(response: RedirectResponse, encryption_key: str):
    """Set encryption key in secure cookie."""
    response.set_cookie(
        key="pwd_zero_encryption_key",
        value=encryption_key,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True in production with HTTPS
        max_age=86400  # 24 hours
    )


def clear_encryption_key_cookie(response: RedirectResponse):
    """Clear encryption key cookie."""
    response.delete_cookie(
        key="pwd_zero_encryption_key",
        httponly=True,
        samesite="lax"
    )


# --- Authentication Routes ---

@bp.get("/login", response_class=HTMLResponse, dependencies=[Depends(block_demo_users)])
async def login_get(request: Request, error: Optional[str] = Query(None)):
    """Display login page."""
    try:
        # Check if user is already authenticated
        try:
            user = await get_user_from_request(request)
            if user:
                return RedirectResponse(url="/experiments/pwd_zero/", status_code=status.HTTP_303_SEE_OTHER)
        except HTTPException:
            pass  # Not authenticated, continue to login page
        
        # Use experiment's own template directory
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        experiment_dir = Path(__file__).resolve().parent
        templates_dir = experiment_dir / "templates"
        experiment_templates = Jinja2Templates(directory=str(templates_dir))
        
        return experiment_templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": error,
                "show_login": True
            }
        )
    except Exception as e:
        logger.error(f"Error rendering login page: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error rendering login page")


@bp.post("/login", dependencies=[Depends(block_demo_users)])
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Handle login."""
    try:
        result = await actor.login_user.remote(username, password)
        
        if result.get("status") == "error":
            error_msg = result.get('error', 'Login failed')
            # Check if request accepts JSON
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"error": error_msg}, status_code=401)
            redirect_url = f"/experiments/pwd_zero/login?error={error_msg}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        
        # Create sub-auth session
        from sub_auth import create_experiment_session
        from experiment_db import get_experiment_db
        from core_deps import get_experiment_config
        from fastapi.responses import RedirectResponse
        
        slug_id = getattr(request.state, "slug_id", "pwd_zero")
        db = await get_experiment_db(request)
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if not config:
            raise HTTPException(status_code=500, detail="Experiment configuration not found")
        
        logger.info(f"User '{username}' logged in successfully")
        
        # Check if request accepts JSON
        if request.headers.get("accept", "").startswith("application/json"):
            # Create JSON response and set cookies on it
            json_response = JSONResponse({"status": "success", "message": "Login successful"})
            await create_experiment_session(
                request, slug_id, result["user_id"], config, json_response
            )
            set_encryption_key_cookie(json_response, result["encryption_key"])
            return json_response
        
        # Create redirect response and set cookies on it
        response = RedirectResponse(url="/experiments/pwd_zero/", status_code=status.HTTP_303_SEE_OTHER)
        await create_experiment_session(
            request, slug_id, result["user_id"], config, response
        )
        set_encryption_key_cookie(response, result["encryption_key"])
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during login: {e}", exc_info=True)
        error_msg = "Login failed. Please try again."
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": error_msg}, status_code=500)
        redirect_url = f"/experiments/pwd_zero/login?error={error_msg}"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@bp.post("/register", dependencies=[Depends(block_demo_users)])
async def register_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Handle registration."""
    try:
        # Create user via actor (this creates user in users collection with password hash and salt)
        result = await actor.register_user.remote(username, password)
        
        if result.get("status") == "error":
            error_msg = result.get('error', 'Registration failed')
            # Check if request accepts JSON
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"error": error_msg}, status_code=400)
            redirect_url = f"/experiments/pwd_zero/login?error={error_msg}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        
        # The actor already created the user in the users collection with password hash
        # Now we need to create a sub-auth session for that user
        from sub_auth import create_experiment_session
        from experiment_db import get_experiment_db
        from core_deps import get_experiment_config
        
        slug_id = getattr(request.state, "slug_id", "pwd_zero")
        db = await get_experiment_db(request)
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if not config:
            raise HTTPException(status_code=500, detail="Experiment configuration not found")
        
        sub_auth_cfg = config.get("sub_auth", {})
        if not sub_auth_cfg.get("allow_registration", False):
            error_msg = "Registration is disabled"
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"error": error_msg}, status_code=403)
            raise HTTPException(status_code=403, detail=error_msg)
        
        # The user is already created by the actor, so we just need to create the session
        logger.info(f"User '{username}' registered successfully")
        
        # Check if request accepts JSON
        if request.headers.get("accept", "").startswith("application/json"):
            # Create JSON response and set cookies on it
            json_response = JSONResponse({"status": "success", "message": "Registration successful"})
            await create_experiment_session(
                request, slug_id, result["user_id"], config, json_response
            )
            set_encryption_key_cookie(json_response, result["encryption_key"])
            return json_response
        
        # Create redirect response and set cookies on it
        response = RedirectResponse(url="/experiments/pwd_zero/", status_code=status.HTTP_303_SEE_OTHER)
        await create_experiment_session(
            request, slug_id, result["user_id"], config, response
        )
        set_encryption_key_cookie(response, result["encryption_key"])
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during registration: {e}", exc_info=True)
        error_msg = "Registration failed. Please try again."
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": error_msg}, status_code=500)
        redirect_url = f"/experiments/pwd_zero/login?error={error_msg}"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@bp.post("/logout")
async def logout_post(request: Request):
    """Handle logout."""
    try:
        from fastapi.responses import RedirectResponse
        from core_deps import get_experiment_config
        
        slug_id = getattr(request.state, "slug_id", "pwd_zero")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if config:
            sub_auth_cfg = config.get("sub_auth", {})
            session_cookie_name = sub_auth_cfg.get("session_cookie_name", "pwd_zero_session")
            cookie_name = f"{session_cookie_name}_{slug_id}"
            
            # Clear session cookie and encryption key cookie
            response = RedirectResponse(url="/experiments/pwd_zero/login", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie(key=cookie_name, httponly=True, samesite="lax")
            clear_encryption_key_cookie(response)
            
            logger.info(f"User logged out")
            return response
        
        return RedirectResponse(url="/experiments/pwd_zero/login", status_code=status.HTTP_303_SEE_OTHER)
        
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        return RedirectResponse(url="/experiments/pwd_zero/login", status_code=status.HTTP_303_SEE_OTHER)


# --- Main Application Route ---

@bp.get("/", response_class=HTMLResponse)
async def index(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Display main password manager page."""
    try:
        html = await actor.render_index.remote()
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_index: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


# --- API Routes ---

@bp.get("/api/session")
async def get_session(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Check session status."""
    try:
        user = await get_user_from_request(request)
        encryption_key = get_encryption_key_from_session(request)
        
        if not encryption_key:
            return JSONResponse({"authenticated": False, "has_user": True})
        
        result = await actor.check_session.remote(user["user_id"])
        result["authenticated"] = result["authenticated"] and encryption_key is not None
        return JSONResponse(result)
    except HTTPException:
        # Not authenticated
        try:
            # Check if any users exist
            from experiment_db import get_experiment_db
            db = await get_experiment_db(request)
            has_user = await db.users.count_documents({}) > 0
            return JSONResponse({"authenticated": False, "has_user": has_user})
        except:
            return JSONResponse({"authenticated": False, "has_user": False})


@bp.get("/api/passwords")
async def get_passwords(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get all passwords for authenticated user."""
    user = await get_user_from_request(request)
    encryption_key = get_encryption_key_from_session(request)
    
    if not encryption_key:
        raise HTTPException(status_code=401, detail="Encryption key not found. Please log in again.")
    
    try:
        passwords = await actor.get_passwords.remote(user["user_id"], encryption_key)
        return JSONResponse(passwords)
    except Exception as e:
        logger.error(f"Actor call failed for get_passwords: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to get passwords: {e}")


@bp.post("/api/passwords")
async def add_password(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Add a new password entry."""
    user = await get_user_from_request(request)
    encryption_key = get_encryption_key_from_session(request)
    
    if not encryption_key:
        raise HTTPException(status_code=401, detail="Encryption key not found. Please log in again.")
    
    try:
        data = await request.json()
        if not all(k in data for k in ['website', 'username', 'password']):
            raise HTTPException(status_code=400, detail="Missing required data fields")
        
        result = await actor.add_password.remote(
            user["user_id"],
            encryption_key,
            data["website"],
            data["username"],
            data["password"]
        )
        
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "Failed to add password"))
        
        return JSONResponse(result, status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for add_password: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to add password: {e}")


@bp.put("/api/passwords/{password_id}")
async def update_password(
    request: Request,
    password_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Update a password entry."""
    user = await get_user_from_request(request)
    encryption_key = get_encryption_key_from_session(request)
    
    if not encryption_key:
        raise HTTPException(status_code=401, detail="Encryption key not found. Please log in again.")
    
    try:
        data = await request.json()
        result = await actor.update_password.remote(
            user["user_id"],
            encryption_key,
            password_id,
            data.get("website"),
            data.get("username"),
            data.get("password")
        )
        
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "Failed to update password"))
        
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for update_password: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to update password: {e}")


@bp.delete("/api/passwords/{password_id}")
async def delete_password(
    request: Request,
    password_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Delete a password entry."""
    user = await get_user_from_request(request)
    
    try:
        result = await actor.delete_password.remote(user["user_id"], password_id)
        
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "Failed to delete password"))
        
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for delete_password: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to delete password: {e}")


@bp.post("/api/generate-password")
async def generate_password(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Generate a secure password."""
    user = await get_user_from_request(request)
    
    try:
        data = await request.json()
        result = await actor.generate_password.remote(
            length=data.get("length", 16),
            uppercase=data.get("uppercase", True),
            lowercase=data.get("lowercase", True),
            numbers=data.get("numbers", True),
            symbols=data.get("symbols", True)
        )
        
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to generate password"))
        
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Actor call failed for generate_password: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate password: {e}")

