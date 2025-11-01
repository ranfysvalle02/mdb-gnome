"""Ray actor decorator with database fallback support."""
import logging
from config import HAVE_MONGO_WRAPPER, RAY_AVAILABLE

logger = logging.getLogger(__name__)

if RAY_AVAILABLE:
    import ray
else:
    ray = None


def ray_actor(
    name: str = None,
    namespace: str = "modular_labs",
    lifetime: str = "detached",
    max_restarts: int = -1,
    get_if_exists: bool = True,
    fallback_if_no_db: bool = True,
):
    """
    A decorator that transforms a normal class into a Ray actor.
    If 'fallback_if_no_db' is True and we detect ScopedMongoWrapper is missing,
    we automatically pass 'use_in_memory_fallback=True' to the actor
    constructor, letting the actor skip real DB usage.
    """
    if not RAY_AVAILABLE:
        logger.warning("Ray is not available. ray_actor decorator will not work.")
        # Return a no-op decorator
        def noop_decorator(cls):
            return cls
        return noop_decorator

    def decorator(user_class):
        # Convert user_class => Ray-remote class
        ray_remote_cls = ray.remote(user_class)

        @classmethod
        def spawn(cls, *args, runtime_env=None, **kwargs):
            # Decide actor_name
            actor_name = name if name else f"{user_class.__name__}_actor"
            # If fallback requested and no ScopedMongoWrapper, pass "use_in_memory_fallback"
            if fallback_if_no_db and not HAVE_MONGO_WRAPPER:
                kwargs["use_in_memory_fallback"] = True

            return cls.options(
                name=actor_name,
                namespace=namespace,
                lifetime=lifetime,
                max_restarts=max_restarts,
                get_if_exists=get_if_exists,
                runtime_env=runtime_env or {},
            ).remote(*args, **kwargs)

        setattr(ray_remote_cls, "spawn", spawn)
        return ray_remote_cls

    return decorator

