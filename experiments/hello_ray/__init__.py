# experiments/hello_ray/__init__.py  
  
"""  
{  
  "name": "Hello Ray",  
  "description": "A minimal example showing how Ray Actors integrate with g.nome.",  
  "status": "draft",  
  "auth_required": false,  
  "data_scope": ["self"]  
}  
"""  
  
import logging  
import ray  
from fastapi import APIRouter, Request  
from fastapi.responses import HTMLResponse  
  
# Import the actor definition (which must be named `ExperimentActor`).  
from .actor import ExperimentActor  
  
logger = logging.getLogger(__name__)  
  
bp = APIRouter()  
  
def get_actor_handle() -> "ray.actor.ActorHandle":  
    """  
    Returns (or creates) the Hello Ray actor.  
    g.nome expects to find it by a stable name in the "modular_labs" namespace.  
    """  
    actor_name = "hello_ray-actor"  
    try:  
        # If an actor with this name already exists, return it  
        return ray.get_actor(actor_name, namespace="modular_labs")  
    except ValueError:  
        # Actor doesn’t exist yet, so create it  
        logger.info(f"[HelloRay] Creating new actor '{actor_name}' in 'modular_labs'...")  
        return ExperimentActor.options(  
            name=actor_name,  
            namespace="modular_labs",  
            lifetime="detached",  
            get_if_exists=True,  
        ).remote()  
  
@bp.get("/", response_class=HTMLResponse)  
async def hello_ray_index(request: Request):  
    """  
    A simple route that calls the Ray actor and returns its response.  
    """  
    actor = get_actor_handle()  # Returns or creates the Ray actor  
    future = actor.say_hello.remote()  
    greeting = await future  
  
    html_content = f"""  
    <h1>Hello Ray!</h1>  
    <p>The actor responded: <strong>{greeting}</strong></p>  
    """  
    return HTMLResponse(html_content)  