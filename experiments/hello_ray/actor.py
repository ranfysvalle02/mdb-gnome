# actor.py  
  
import logging  
# Instead of importing ray directly here, import the custom decorator you created:  
# If your decorator is in main.py, do:  
# from main import ray_actor  
# or if you placed ray_actor into a separate module, import from there:  
from main import ray_actor    
  
logger = logging.getLogger(__name__)  
  
  
@ray_actor(  
    name="hello_ray-actor",       # Tells Ray to register the actor by this name  
    namespace="modular_labs",     # Ray namespace  
    lifetime="detached",          # Actor’s lifetime in Ray cluster  
    max_restarts=-1,              # Unlimited restarts  
    get_if_exists=True,           # Reuse if actor already exists  
    fallback_if_no_db=True        # Example: handle no PyMongo scenario gracefully  
)  
class ExperimentActor:  
    """  
    Minimal Ray actor that can accept g.nome’s extra constructor parameters.  
    Note the name MUST be 'ExperimentActor' for g.nome to auto-detect it!  
    """  
  
    def __init__(  
        self,  
        mongo_uri: str = None,  
        db_name: str = None,  
        write_scope: str = "hello_ray",  
        read_scopes: list[str] = None  
    ):  
        self.mongo_uri = mongo_uri  
        self.db_name = db_name  
        self.write_scope = write_scope  
        self.read_scopes = read_scopes or []  
  
        logger.info(  
            f"[HelloRayActor] Initialized with "  
            f"mongo_uri={mongo_uri}, db_name={db_name}, "  
            f"write_scope={write_scope}, read_scopes={read_scopes}"  
        )  
  
    def say_hello(self) -> str:  
        logger.info("[HelloRayActor] say_hello() called.")  
        return "Hello from Ray Actor!"  