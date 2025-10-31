# experiments/hello_ray/__init__.py (Updated)
 
import logging
import ray
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from ray.actor import ActorHandle # Import for type hinting
 
# Import the actor definition (which must be named `ExperimentActor`).
from .actor import ExperimentActor
 
logger = logging.getLogger(__name__)
 
bp = APIRouter()
 
# Point this to your templates directory.
templates = Jinja2Templates(directory="experiments/hello_ray/templates")
 
 
def get_actor_handle(request: Request) -> ActorHandle:
    """
    Returns the Ray actor. Checks the global app state first.
    """
    # 1. Check the FastAPI state set during lifespan (in main.py)
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("[HelloRay] Ray is not marked as available in app.state.")
        # If the Ray cluster is down, raise a 503 service unavailable error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Ray Service is unavailable. Check Ray cluster and logs."
        )
 
    try:
        # The actor should be created during the main.py's reload_active_experiments call
        # We attempt to get the *existing* handle.
        actor_name = "hello_ray-actor"
        return ray.get_actor(actor_name, namespace="modular_labs")
    
    except ValueError:
        # This path means the actor wasn't initialized by the reload logic,
        # but Ray *is* available. Fallback to initializing it here using the decorator's pattern.
        logger.warning(
            f"[HelloRay] Actor '{actor_name}' not found. Attempting creation via spawn..."
        )
        
        # Call the custom spawn method defined by the @ray_actor decorator in main.py.
        # This ensures the actor is created with the correct options and namespace.
        # NOTE: You MUST pass the constructor arguments here if they are required.
        # Since the constructor arguments are optional, we can omit them, 
        # but it's best practice to pass the environment-defined values.
        
        # We retrieve the database config from the app state to pass to the actor.
        mongo_uri = request.app.state.get("MONGO_URI", "mongodb://...")
        db_name = request.app.state.get("DB_NAME", "labs_db")
        
        return ExperimentActor.spawn(
             mongo_uri=mongo_uri, 
             db_name=db_name,
             write_scope="hello_ray",
             read_scopes=["self"]
        )
 
    except Exception as e:
        logger.error(f"[HelloRay] Failed to get/create actor handle: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error connecting to or creating Ray actor."
        )


@bp.get("/", response_class=HTMLResponse)
async def hello_ray_index(request: Request):
    """
    A simple route that calls the Ray actor and returns its response.
    """
    # Pass the request to get_actor_handle() to allow state checking
    actor = get_actor_handle(request)
    # The remote call is an awaitable future
    greeting = await actor.say_hello.remote() 
 
    # Render the 'index.html' template with the greeting
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "greeting": greeting}
    )