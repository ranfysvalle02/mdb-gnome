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
from experiment_auth_restrictions import block_demo_users, require_non_demo_user

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
# Authentication Strategy: Hybrid (Platform + Sub-Auth)
#
# StoryWeaver uses a hybrid authentication strategy defined in manifest.json:
#   - auth_required: true (enforces authentication at router level)
#   - sub_auth.enabled: true
#   - sub_auth.strategy: "hybrid"
#   - sub_auth.link_platform_users: true (links platform users to experiment profiles)
#
# This means StoryWeaver accepts BOTH:
#   1. Platform Authentication: Users who logged in at /auth/login (platform-level JWT)
#   2. Sub-Authentication: Users who logged in within StoryWeaver (experiment-level session)
#
# The router-level dependency (in experiment_routes.py) handles this automatically.
# Routes can also use _get_user_from_request() directly if needed.
#
# Example usage in routes:
#   @bp.get("/")
#   async def index(request: Request):
#       user = await _get_user_from_request(request)  # Gets user from platform OR sub-auth
#       user_id = get_user_id_for_actor(user)  # Extracts appropriate ID for actor calls
async def _get_user_from_request(request: Request) -> Dict[str, Any]:
    """
    Intelligently get authenticated user from either platform auth or sub-auth.
    
    This is StoryWeaver's internal authentication function. It implements the hybrid
    strategy that allows both platform and experiment-specific users to access the app.
    
    GOD-LEVEL ACCESS: Admins bypass ALL checks and get immediate access.
    
    Authentication Flow:
        0. GOD-LEVEL: Check if user is admin (bypasses all other checks)
        1. Platform Auth: Checks for 'token' cookie (JWT from platform login)
           - If found and valid, checks if user has linked experiment profile
           - Returns platform user (with experiment_user_id if linked)
        2. Sub-Auth: Checks for experiment session cookie (from experiment login)
           - If found and valid, returns experiment user
        3. Error: Raises HTTPException if neither authentication succeeds
    
    Returns unified user dict with:
        - user_id: Always present (platform or experiment user ID)
        - email: User email
        - experiment_user_id: Present if from sub-auth or if platform user is linked
        - platform_user_id: Present if from platform auth
        - platform_auth: True if authenticated via platform
        - sub_auth: True if authenticated via sub-auth
        - is_admin: True if user is admin (god-level access)
    
    Raises:
        HTTPException: 401 Unauthorized if neither authentication method succeeds
    
    Note: This function is called by route handlers. The router-level dependency
    in experiment_routes.py handles most authentication automatically.
    """
    slug_id = getattr(request.state, "slug_id", "storyweaver")
    
    # STEP 0: GOD-LEVEL ACCESS - Check if user is admin
    # Admins bypass ALL authentication checks and get immediate access
    try:
        from core_deps import SECRET_KEY, get_authz_provider
        import jwt as jwt_lib
        
        token = request.cookies.get("token")
        if token:
            try:
                payload = jwt_lib.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_id = payload.get("user_id")
                email = payload.get("email")
                is_admin = payload.get("is_admin", False)
                
                # If user is admin (from JWT), grant immediate access
                if is_admin and user_id and email:
                    logger.info(
                        f"_get_user_from_request ({slug_id}): Admin '{email}' granted GOD-LEVEL access (JWT is_admin=True)"
                    )
                    return {
                        "user_id": user_id,
                        "email": email,
                        "platform_user_id": user_id,
                        "platform_auth": True,
                        "is_admin": True,
                        "god_access": True
                    }
                
                # Also check via authz provider if JWT doesn't have is_admin flag
                if user_id and email:
                    authz = await get_authz_provider(request)
                    is_admin_via_authz = await authz.check(
                        subject=email,
                        resource="admin_panel",
                        action="access",
                        user_object={"user_id": user_id, "email": email, "is_admin": is_admin}
                    )
                    if is_admin_via_authz:
                        logger.info(
                            f"_get_user_from_request ({slug_id}): Admin '{email}' granted GOD-LEVEL access (via authz provider)"
                        )
                        return {
                            "user_id": user_id,
                            "email": email,
                            "platform_user_id": user_id,
                            "platform_auth": True,
                            "is_admin": True,
                            "god_access": True
                        }
            except (jwt_lib.ExpiredSignatureError, jwt_lib.InvalidTokenError):
                pass  # Token invalid/expired, continue with normal auth
            except Exception as e:
                logger.debug(f"Admin check failed in _get_user_from_request for {slug_id}: {e}")
    except Exception as e:
        logger.debug(f"Error checking admin status in _get_user_from_request for {slug_id}: {e}")
    
    # Try platform authentication first (top-level JWT)
    try:
        from core_deps import get_experiment_config
        from experiment_db import get_experiment_db
        
        token = request.cookies.get("token")
        if token:
            try:
                payload = jwt_lib.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_id = payload.get("user_id")
                email = payload.get("email")
                
                if user_id and email:
                    # Platform user found - check if they have experiment profile
                    config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
                    sub_auth = config.get("sub_auth", {}) if config else {}
                    
                    if sub_auth.get("enabled") and sub_auth.get("link_platform_users"):
                        # Try to link to experiment-specific profile
                        # Use ExperimentDB for clean MongoDB-style API
                        db = await get_experiment_db(request)
                        collection_name = sub_auth.get("collection_name", "users")
                        
                        # Look for experiment user linked to platform user
                        # ExperimentDB provides MongoDB-style access via collection() method
                        from bson.objectid import ObjectId
                        experiment_user = await db.collection(collection_name).find_one({
                            "platform_user_id": user_id
                        })
                        
                        if experiment_user:
                            # Return hybrid user with both IDs
                            return {
                                "user_id": user_id,  # Platform user ID for compatibility
                                "email": email,
                                "platform_user_id": user_id,
                                "experiment_user_id": str(experiment_user["_id"]),
                                "platform_auth": True
                            }
                        
                        # No experiment profile linked, but platform auth is valid
                        return {
                            "user_id": user_id,
                            "email": email,
                            "platform_user_id": user_id,
                            "platform_auth": True
                        }
                    else:
                        # Sub-auth not enabled or linking disabled, use platform auth
                        return {
                            "user_id": user_id,
                            "email": email,
                            "platform_user_id": user_id,
                            "platform_auth": True
                        }
            except jwt_lib.ExpiredSignatureError:
                pass  # Fall through to sub-auth
            except jwt_lib.InvalidTokenError:
                pass  # Fall through to sub-auth
    except Exception as e:
        logger.debug(f"Platform auth check failed, trying sub-auth: {e}")
    
    # Try sub-authentication (experiment-specific session)
    try:
        from sub_auth import get_experiment_sub_user
        from core_deps import get_experiment_config, get_scoped_db
        
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        if not config:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        
        sub_auth = config.get("sub_auth", {})
        if not sub_auth.get("enabled", False):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        
        # Use ExperimentDB for clean MongoDB-style API
        from experiment_db import get_experiment_db
        db = await get_experiment_db(request)
        experiment_user = await get_experiment_sub_user(request, slug_id, db, config)
        
        if experiment_user:
            # Return experiment user with experiment_user_id
            return {
                "user_id": experiment_user.get("experiment_user_id") or str(experiment_user.get("_id")),
                "email": experiment_user.get("email"),
                "experiment_user_id": str(experiment_user.get("_id")),
                "platform_user_id": experiment_user.get("platform_user_id"),
                "sub_auth": True
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Sub-auth check failed: {e}")
    
    # Neither platform nor sub-auth available
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required. Please log in.")


def get_user_id_for_actor(user: Dict[str, Any]) -> str:
    """
    Extract the appropriate user_id for actor calls from unified user dict.
    
    Prefers experiment_user_id if available (for sub-auth), otherwise uses platform user_id.
    This allows actors to work seamlessly with both platform and experiment users.
    
    Args:
        user: User dict from _get_user_from_request()
    
    Returns:
        str: User ID string to use in actor calls
    """
    # Prefer experiment_user_id for sub-auth users, fall back to platform user_id
    return user.get("experiment_user_id") or user.get("platform_user_id") or user.get("user_id")


# --- Authentication Routes (Sub-Auth) ---
# StoryWeaver supports sub-authentication, allowing users to register and login
# directly within the experiment (in addition to platform-level authentication)

@bp.get("/login", response_class=HTMLResponse, dependencies=[Depends(block_demo_users)])
async def login_get(
    request: Request,
    error: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    next: Optional[str] = Query(None)
):
    """
    Display login page for StoryWeaver sub-authentication.
    
    Note: Demo users cannot access this route - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    try:
        from fastapi.responses import HTMLResponse, RedirectResponse
        
        # Check if user is already authenticated (via sub-auth or platform)
        try:
            user = await _get_user_from_request(request)
            if user:
                # Already authenticated, redirect to home
                redirect_path = next if next and next.startswith("/") else "/experiments/storyweaver/"
                return RedirectResponse(url=redirect_path, status_code=status.HTTP_303_SEE_OTHER)
        except HTTPException:
            pass  # Not authenticated, continue to login page
        
        # Use experiment's own template directory (not global templates)
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        experiment_dir = Path(__file__).resolve().parent
        templates_dir = experiment_dir / "templates"
        experiment_templates = Jinja2Templates(directory=str(templates_dir))
        
        # Render login template
        return experiment_templates.TemplateResponse(
            "login.html",  # Relative to experiment's templates directory
            {
                "request": request,
                "error": error,
                "email": email,
                "next": next or "/experiments/storyweaver/"
            }
        )
    except Exception as e:
        logger.error(f"Error rendering login page: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error rendering login page")


@bp.post("/login", dependencies=[Depends(block_demo_users)])
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(None)
):
    """
    Handle login for StoryWeaver sub-authentication.
    
    Note: Demo users cannot access this route - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    try:
        from sub_auth import authenticate_experiment_user, create_experiment_session
        from experiment_db import get_experiment_db
        from core_deps import get_experiment_config
        from fastapi.responses import RedirectResponse
        
        slug_id = getattr(request.state, "slug_id", "storyweaver")
        
        # Get database and config
        db = await get_experiment_db(request)
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if not config:
            raise HTTPException(status_code=500, detail="Experiment configuration not found")
        
        sub_auth_cfg = config.get("sub_auth", {})
        if not sub_auth_cfg.get("enabled", False):
            raise HTTPException(status_code=500, detail="Sub-authentication not enabled")
        
        collection_name = sub_auth_cfg.get("collection_name", "users")
        
        # Authenticate user
        user = await authenticate_experiment_user(db, email, password, None, collection_name)
        
        if not user:
            # Authentication failed - redirect back with error
            redirect_url = f"/experiments/storyweaver/login?error=Invalid email or password&email={email}"
            if next:
                redirect_url += f"&next={next}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        
        # Create session
        response = RedirectResponse(url=next or "/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)
        await create_experiment_session(
            request, slug_id, str(user["_id"]), config, response
        )
        
        logger.info(f"User '{email}' logged in successfully via sub-auth")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during login: {e}", exc_info=True)
        redirect_url = f"/experiments/storyweaver/login?error=Login failed. Please try again."
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@bp.get("/register", response_class=HTMLResponse, dependencies=[Depends(block_demo_users)])
async def register_get(
    request: Request,
    error: Optional[str] = Query(None),
    email: Optional[str] = Query(None)
):
    """
    Display registration page for StoryWeaver sub-authentication.
    
    Note: Demo users cannot access this route - they are trapped in demo mode
    for security reasons (see sub_auth.py).
    """
    try:
        from fastapi.responses import RedirectResponse
        
        # Check if registration is allowed
        slug_id = getattr(request.state, "slug_id", "storyweaver")
        from core_deps import get_experiment_config
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if not config:
            raise HTTPException(status_code=500, detail="Experiment configuration not found")
        
        sub_auth_cfg = config.get("sub_auth", {})
        if not sub_auth_cfg.get("allow_registration", False):
            # Registration disabled - redirect to login
            return RedirectResponse(url="/experiments/storyweaver/login", status_code=status.HTTP_303_SEE_OTHER)
        
        # Check if user is already authenticated
        try:
            user = await _get_user_from_request(request)
            if user:
                # Already authenticated, redirect to home
                return RedirectResponse(url="/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)
        except HTTPException:
            pass  # Not authenticated, continue to registration page
        
        # Use experiment's own template directory (not global templates)
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        experiment_dir = Path(__file__).resolve().parent
        templates_dir = experiment_dir / "templates"
        experiment_templates = Jinja2Templates(directory=str(templates_dir))
        
        # Render registration template
        return experiment_templates.TemplateResponse(
            "register.html",  # Relative to experiment's templates directory
            {
                "request": request,
                "error": error,
                "email": email
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rendering registration page: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error rendering registration page")


@bp.post("/register", dependencies=[Depends(block_demo_users)])
async def register_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...)
):
    """
    Handle registration for StoryWeaver sub-authentication.
    
    Note: Demo users cannot access this route - they are trapped in demo mode
    for security reasons (see sub_auth.py).
    """
    try:
        from sub_auth import create_experiment_user, create_experiment_session
        from experiment_db import get_experiment_db
        from core_deps import get_experiment_config
        from fastapi.responses import RedirectResponse
        
        slug_id = getattr(request.state, "slug_id", "storyweaver")
        
        # Get database and config
        db = await get_experiment_db(request)
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if not config:
            raise HTTPException(status_code=500, detail="Experiment configuration not found")
        
        sub_auth_cfg = config.get("sub_auth", {})
        if not sub_auth_cfg.get("enabled", False):
            raise HTTPException(status_code=500, detail="Sub-authentication not enabled")
        
        if not sub_auth_cfg.get("allow_registration", False):
            raise HTTPException(status_code=403, detail="Registration is disabled")
        
        # Validate passwords match
        if password != confirm_password:
            redirect_url = f"/experiments/storyweaver/register?error=Passwords do not match&email={email}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        
        collection_name = sub_auth_cfg.get("collection_name", "users")
        
        # Create user
        user = await create_experiment_user(
            db, email, password, "user", None, collection_name, hash_password=True
        )
        
        if not user:
            # User creation failed (likely email already exists)
            redirect_url = f"/experiments/storyweaver/register?error=Email already registered&email={email}"
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        
        # Create session and redirect
        response = RedirectResponse(url="/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)
        await create_experiment_session(
            request, slug_id, str(user["_id"]), config, response
        )
        
        logger.info(f"User '{email}' registered successfully via sub-auth")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during registration: {e}", exc_info=True)
        redirect_url = f"/experiments/storyweaver/register?error=Registration failed. Please try again."
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@bp.post("/logout", dependencies=[Depends(block_demo_users)])
async def logout_post(request: Request):
    """
    Handle logout for StoryWeaver sub-authentication.
    """
    try:
        from fastapi.responses import RedirectResponse
        from core_deps import get_experiment_config
        
        slug_id = getattr(request.state, "slug_id", "storyweaver")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        if config:
            sub_auth_cfg = config.get("sub_auth", {})
            session_cookie_name = sub_auth_cfg.get("session_cookie_name", "storyweaver_session")
            cookie_name = f"{session_cookie_name}_{slug_id}"
            
            # Clear session cookie
            response = RedirectResponse(url="/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie(key=cookie_name, httponly=True, samesite="lax")
            
            logger.info(f"User logged out from StoryWeaver sub-auth")
            return response
        
        return RedirectResponse(url="/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)
        
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        return RedirectResponse(url="/experiments/storyweaver/", status_code=status.HTTP_303_SEE_OTHER)


@bp.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Display workspace with all projects. Uses hybrid authentication (platform + sub-auth)."""
    # Get user from either platform auth or sub-auth
    # Note: Router-level dependency handles authentication, but demo mode is allowed
    user = await _get_user_from_request(request)
    
    try:
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        from bson.objectid import ObjectId
        from experiment_db import get_experiment_db
        from config import MONGO_URI, DB_NAME, DEMO_EMAIL_DEFAULT
        from core_deps import get_experiment_config
        
        actor = await get_actor_handle(request)
        
        # Get database to fetch projects directly
        db = await get_experiment_db(request)
        
        # DEMO USER DETECTION AND USER ID RESOLUTION
        # Note: Demo seeding happens at actor initialization, so content should already be ready.
        # Here we just need to ensure the user has the correct experiment_user_id for fetching projects.
        
        user_email = user.get('email', '')
        is_demo_user = (
            user_email == DEMO_EMAIL_DEFAULT or 
            user.get('is_demo') or 
            user.get('demo_mode') or 
            user_email.startswith('demo@')
        )
        
        # Get the user_id for fetching projects (prefer experiment_user_id)
        user_id = get_user_id_for_actor(user)
        
        # If user is demo and doesn't have experiment_user_id, try to find it from database
        # The actor initialization should have created it, but we check here to ensure we use correct ID
        # Note: Demo content is seeded at actor initialization, so it should already be ready!
        if is_demo_user and not user.get("experiment_user_id"):
            try:
                # Look up the experiment-specific demo user (created by actor initialization)
                if hasattr(db, 'collection'):
                    demo_user_doc = await db.collection("users").find_one({"email": user_email}, {"_id": 1})
                else:
                    users_collection = getattr(db, "users")
                    demo_user_doc = await users_collection.find_one({"email": user_email}, {"_id": 1})
                
                if demo_user_doc:
                    experiment_user_id = str(demo_user_doc["_id"])
                    user["experiment_user_id"] = experiment_user_id
                    user_id = experiment_user_id
                    logger.info(f"Found experiment demo user ID: {experiment_user_id} (content should be pre-seeded by actor)")
                else:
                    logger.debug(f"Demo user '{user_email}' experiment profile not found (may need actor initialization)")
            except Exception as e:
                logger.debug(f"Could not find experiment demo user: {e}")
        
        # Fetch projects for user (after potential seeding)
        projects = await db.projects.find({"user_id": ObjectId(user_id)}).sort("created_at", -1).to_list(length=None)
        
        logger.info(f"index: Found {len(projects)} projects for user_id={user_id} (experiment_user_id={user.get('experiment_user_id')}, email={user_email})")
        
        for p in projects:
            p['_id'] = str(p['_id'])
            logger.debug(f"index: Project found - name='{p.get('name')}', id={p['_id']}, user_id={p.get('user_id')}")
        
        # Use experiment's own template directory (not global templates)
        experiment_dir = Path(__file__).resolve().parent
        templates_dir = experiment_dir / "templates"
        experiment_templates = Jinja2Templates(directory=str(templates_dir))
        
        # Render template with user and projects (for base.html navigation)
        return experiment_templates.TemplateResponse(
            "index.html",  # Relative to experiment's templates directory
            {
                "request": request,
                "user": user,  # Pass user for base.html login/logout display
                "projects": projects
            }
        )
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
    """
    Display project view.
    
    Note: For demo users, we must use their experiment_user_id (not platform user_id)
    to match the user_id used when seeding projects.
    """
    user = await _get_user_from_request(request)
    
    # Apply same user_id resolution logic as index route for demo users
    from config import DEMO_EMAIL_DEFAULT
    from experiment_db import get_experiment_db
    from bson.objectid import ObjectId
    
    user_email = user.get('email', '')
    is_demo_user = (
        user_email == DEMO_EMAIL_DEFAULT or 
        user.get('is_demo') or 
        user.get('demo_mode') or 
        user_email.startswith('demo@')
    )
    
    # Get the user_id for fetching projects (prefer experiment_user_id)
    user_id = get_user_id_for_actor(user)
    
    # If user is demo and doesn't have experiment_user_id, try to find it from database
    # This ensures we use the same user_id that was used when seeding projects
    if is_demo_user and not user.get("experiment_user_id"):
        try:
            db = await get_experiment_db(request)
            # Look up the experiment-specific demo user (created by actor initialization)
            if hasattr(db, 'collection'):
                demo_user_doc = await db.collection("users").find_one({"email": user_email}, {"_id": 1})
            else:
                users_collection = getattr(db, "users")
                demo_user_doc = await users_collection.find_one({"email": user_email}, {"_id": 1})
            
            if demo_user_doc:
                experiment_user_id = str(demo_user_doc["_id"])
                user["experiment_user_id"] = experiment_user_id
                user_id = experiment_user_id
                logger.info(f"project_view: Found experiment demo user ID: {experiment_user_id}")
        except Exception as e:
            logger.debug(f"project_view: Could not find experiment demo user: {e}")
    
    logger.info(f"project_view: Looking up project_id={project_id} with user_id={user_id} (experiment_user_id={user.get('experiment_user_id')}, email={user_email})")
    
    try:
        html = await actor.render_project_view.remote(user_id, project_id)
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    """
    Create a new project.
    
    Note: Demo users cannot create projects - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    try:
        logger.info("create_project: Request received")
        logger.info(f"create_project: User authenticated: {user.get('email', 'unknown')} (platform: {user.get('platform_auth')}, sub-auth: {user.get('sub_auth')})")
        
        user_id = get_user_id_for_actor(user)
        if not user_id:
            logger.error(f"create_project: No user_id found. User payload: {user}")
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    """
    Delete a project.
    
    Note: Demo users cannot delete projects - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    try:
        logger.info(f"delete_project: Request received for project_id={project_id}")
        user_id = get_user_id_for_actor(user)
        
        if not user_id:
            logger.error(f"delete_project: No user_id found. User payload: {user}")
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    """
    Add a new note.
    
    Note: Demo users cannot add notes - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    try:
        # User is already authenticated and verified as non-demo via dependency
        user_id = get_user_id_for_actor(user)
        if not user_id:
            logger.error(f"add_note: No user_id found. User payload: {user}")
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
    user_id = get_user_id_for_actor(user)
    try:
        notes = await actor.get_notes.remote(
            user_id,
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    """
    Update a note.
    
    Note: Demo users cannot update notes - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.update_note.remote(user_id, note_id, data)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_note: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to update note: {e}")


@bp.delete("/api/notes/{note_id}")
async def delete_note(
    request: Request,
    note_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Dict[str, Any] = Depends(require_non_demo_user)
):
    """
    Delete a note.
    
    Note: Demo users cannot delete notes - they are trapped in demo mode
    for security reasons (demo mode is read-only).
    """
    user_id = get_user_id_for_actor(user)
    try:
        result = await actor.delete_note.remote(user_id, note_id)
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_invite_token.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_shared_token.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.suggest_tags.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        result = await actor.search_notes.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        tags = await actor.get_tags.remote(user_id, project_id)
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
    user_id = get_user_id_for_actor(user)
    try:
        contributors = await actor.get_contributors.remote(user_id, project_id)
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_notes.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_quiz.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_story.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_study_guide.remote(
            user_id,
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
    user_id = get_user_id_for_actor(user)
    try:
        data = await request.json()
        result = await actor.generate_song.remote(
            user_id,
            data
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for generate_song: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to generate song: {e}")

