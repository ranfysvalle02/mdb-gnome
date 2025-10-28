# ==============================================================================
# AuthZ Provider Factory
#
# This file imports all concrete AuthZ implementations and provides
# a single factory function (`create_authz_provider`) to build one
# based on a name.
#
# main.py will call this factory, keeping main.py free of
# provider-specific imports like 'casbin' or 'oso'.
# ==============================================================================

import logging
from pathlib import Path
from typing import Any, Dict

# --- Generic Protocol Import ---
from authz_provider import AuthorizationProvider, CasbinAdapter

# --- Casbin-Specific Imports ---
try:
    import casbin  # type: ignore
    from casbin_motor_adapter import Adapter as CasbinMotorAdapter
    CASBIN_AVAILABLE = True
except ImportError:
    CASBIN_AVAILABLE = False

# --- Other Provider Imports (Future) ---
# try:
#     import oso
#     OSO_AVAILABLE = True
# except ImportError:
#     OSO_AVAILABLE = False


logger = logging.getLogger(__name__)


async def _create_casbin_provider(settings: Dict[str, Any]) -> AuthorizationProvider:
    """Builds and returns a CasbinAdapter."""
    if not CASBIN_AVAILABLE:
        raise RuntimeError(
            "Casbin provider selected, but 'casbin' or 'casbin-motor-adapter' "
            "are not installed."
        )

    # Extract required settings
    try:
        mongo_uri = settings["mongo_uri"]
        db_name = settings["db_name"]
        base_dir = settings["base_dir"]
    except KeyError as e:
        raise RuntimeError(f"Missing required setting for Casbin provider: {e}")

    model_path = base_dir / "casbin_model.conf"
    if not model_path.is_file():
        raise FileNotFoundError(f"Missing Casbin model file: {model_path}")

    logger.info(f"Using Casbin model: {model_path}")
    adapter = CasbinMotorAdapter(mongo_uri, db_name)
    enforcer = casbin.AsyncEnforcer(str(model_path), adapter)
    await enforcer.load_policy()

    # Wrap the enforcer in our standard adapter
    authz_instance = CasbinAdapter(enforcer)
    logger.info("✔️  Casbin AuthZ provider loaded and ready.")
    return authz_instance


async def _create_oso_provider(settings: Dict[str, Any]) -> AuthorizationProvider:
    """Placeholder for building an Oso provider."""
    logger.error("Oso Cloud provider is not yet implemented.")
    raise NotImplementedError("AUTHZ_PROVIDER 'oso' is not yet implemented.")


async def _create_openfga_provider(settings: Dict[str, Any]) -> AuthorizationProvider:
    """Placeholder for building an OpenFGA provider."""
    logger.error("OpenFGA provider is not yet implemented.")
    raise NotImplementedError("AUTHZ_PROVIDER 'openfga' is not yet implemented.")


# This is the main factory function main.py will call
PROVIDER_FACTORIES = {
    "casbin": _create_casbin_provider,
    "oso": _create_oso_provider,
    "openfga": _create_openfga_provider,
}


async def create_authz_provider(
    provider_name: str, settings: Dict[str, Any]
) -> AuthorizationProvider:
    """
    Factory function to create a pluggable authorization provider.
    """
    factory_func = PROVIDER_FACTORIES.get(provider_name)

    if factory_func is None:
        raise RuntimeError(
            f"Unknown or unsupported AUTHZ_PROVIDER: '{provider_name}'. "
            f"Supported: {list(PROVIDER_FACTORIES.keys())}"
        )

    return await factory_func(settings)