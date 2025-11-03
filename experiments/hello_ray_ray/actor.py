# File: /app/experiments/demo_ray/actor.py

import logging

logger = logging.getLogger(__name__)

import ray

@ray.remote
class ExperimentActor:
    """
    Minimal Ray actor.
    The name MUST be 'ExperimentActor' for main.py to auto-detect it.
    Now uses ExperimentDB for easy database access - no MongoDB knowledge needed!
    """

    def __init__(
        self,
        mongo_uri: str = None,
        db_name: str = None,
        write_scope: str = "demo_ray",
        read_scopes: list[str] = None
    ):
        self.write_scope = write_scope
        self.read_scopes = read_scopes or []
        
        # Database initialization (follows pattern from other experiments)
        try:
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            logger.info(
                f"[HelloRayActor] Initialized with "
                f"write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[HelloRayActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None

    def say_hello(self) -> str:
        logger.info("[HelloRayActor] say_hello() called.")
        return "Hello from Ray Actor!"