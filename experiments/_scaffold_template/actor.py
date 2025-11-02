# File: experiments/_scaffold_template/actor.py
# This is a scaffold template for new experiments.

import logging

logger = logging.getLogger(__name__)

import ray
@ray.remote
class ExperimentActor:
    """
    Minimal Ray actor template.
    The name MUST be 'ExperimentActor' for main.py to auto-detect it.
    
    Customize this class for your experiment's needs.
    """

    def __init__(
        self,
        mongo_uri: str = None,
        db_name: str = None,
        write_scope: str = "{slug}",  # Replace {slug} with your experiment slug
        read_scopes: list[str] = None
    ):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.write_scope = write_scope
        self.read_scopes = read_scopes or []

        logger.info(
            f"[ScaffoldTemplateActor] Initialized with "
            f"mongo_uri={mongo_uri}, db_name={db_name}, "
            f"write_scope={write_scope}, read_scopes={read_scopes}"
        )

    def say_hello(self) -> str:
        """
        Example actor method. Replace with your experiment's logic.
        """
        logger.info("[ScaffoldTemplateActor] say_hello() called.")
        return "Hello from Ray Actor! Customize this method for your experiment."

