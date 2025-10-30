# experiments/hello_ray/actor.py  
  
import logging  
import ray  
  
logger = logging.getLogger(__name__)  
  
@ray.remote  
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
        # Provide a default if none was passed  
        self.read_scopes = read_scopes or []  
  
        logger.info(  
            f"[HelloRayActor] Initialized with "  
            f"mongo_uri={mongo_uri}, db_name={db_name}, "  
            f"write_scope={write_scope}, read_scopes={read_scopes}"  
        )  
  
    def say_hello(self) -> str:  
        logger.info("[HelloRayActor] say_hello() called.")  
        return "Hello from Ray Actor!"  