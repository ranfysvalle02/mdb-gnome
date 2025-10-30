# ==============================================================================
# AuthZ Provider Factory (FINAL DE-COUPLED VERSION)
# ==============================================================================

import logging
from pathlib import Path
from typing import Any, Dict

# --- Generic Protocol Import (MUST remain at the top) ---
# CasbinAdapter is imported here as it is part of the common 'authz_provider' file.
from authz_provider import AuthorizationProvider, CasbinAdapter

# The global CASBIN_AVAILABLE check has been removed.

logger = logging.getLogger(__name__)


async def _create_casbin_provider(settings: Dict[str, Any]) -> AuthorizationProvider:
    """
    Builds and returns a CasbinAdapter.
    **Dependencies (casbin, casbin-motor-adapter) are imported LOCALLY.**
    """
    # --- Casbin-Specific Imports (NOW LOCALIZED) ---
    try:
        import casbin  # type: ignore
        from casbin_motor_adapter import Adapter as CasbinMotorAdapter
    except ImportError as e:
        # This RuntimeError is only raised if 'casbin' is the selected provider
        # but the dependencies are missing.
        raise RuntimeError(
            f"Casbin provider selected, but required libraries are not installed: {e}. "
            f"Ensure 'casbin' and 'casbin-motor-adapter' are in your environment."
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
    # casbin.AsyncEnforcer is now available locally
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