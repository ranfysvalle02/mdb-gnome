# File: /app/experiments/hello_ray/actor.py

import logging

logger = logging.getLogger(__name__)

# DO NOT import 'ray_actor' here
# DO NOT add the '@ray_actor' decorator here
import ray

class ExperimentActor:
    """
    Minimal Ray actor.
    The name MUST be 'ExperimentActor' for main.py to auto-detect it.
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