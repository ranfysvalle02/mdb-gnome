# ==============================================================================
# AuthZ Provider Factory (FINAL DE-COUPLED VERSION)
# ==============================================================================

import logging
from pathlib import Path
from typing import Any, Dict

# --- Generic Protocol Import (MUST remain at the top) ---
# CasbinAdapter and OsoAdapter are imported here as they are part of the common 'authz_provider' file.
from authz_provider import AuthorizationProvider, CasbinAdapter, OsoAdapter

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

    model_path = base_dir / "auth" / "casbin_model.conf"
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
    """
    Builds and returns an OsoAdapter.
    Supports both OSO Cloud (via oso-cloud library) and OSO library.
    **Dependencies (oso or oso-cloud) are imported LOCALLY.**
    """
    # --- OSO-Specific Imports (NOW LOCALIZED) ---
    # Try OSO Cloud first (recommended for production)
    oso_client = None
    try:
        # Try different possible import paths for OSO Cloud
        try:
            from oso import OsoCloud
        except ImportError:
            try:
                from oso_cloud import OsoCloud
            except ImportError:
                try:
                    from oso.cloud import OsoCloud
                except ImportError:
                    raise ImportError("OSO Cloud client not found")
        
        import os
        
        # Extract settings for OSO Cloud
        oso_api_key = os.getenv("OSO_API_KEY")
        oso_url = os.getenv("OSO_URL", "https://cloud.osohq.com")
        
        if not oso_api_key:
            logger.warning(
                "OSO_API_KEY not found. OSO Cloud requires an API key. "
                "Falling back to OSO library mode."
            )
            raise ImportError("OSO Cloud requires OSO_API_KEY environment variable")
        
        logger.info(f"Initializing OSO Cloud client (URL: {oso_url})...")
        oso_client = OsoCloud(url=oso_url, api_key=oso_api_key)
        logger.info("✔️  OSO Cloud client initialized.")
        
    except (ImportError, ValueError) as e:
        # Fall back to OSO library (for local development)
        try:
            from oso import Oso
            from pathlib import Path
            
            logger.info("OSO Cloud not available, using OSO library instead...")
            oso_client = Oso()
            
            # Load policy file if provided
            policy_path = settings.get("policy_path")
            if policy_path:
                policy_file = Path(policy_path)
                if policy_file.is_file():
                    logger.info(f"Loading OSO policy from: {policy_file}")
                    oso_client.load_file(str(policy_file))
                else:
                    logger.warning(f"OSO policy file not found: {policy_file}")
            else:
                # Try default location
                base_dir = settings.get("base_dir")
                if base_dir:
                    default_policy = base_dir / "auth" / "oso_policy.polar"
                    if default_policy.is_file():
                        logger.info(f"Loading OSO policy from default location: {default_policy}")
                        oso_client.load_file(str(default_policy))
                    else:
                        logger.warning(
                            f"No OSO policy file found at default location: {default_policy}. "
                            "You may need to define policies programmatically or create a policy file."
                        )
            
            logger.info("✔️  OSO library initialized.")
            
        except ImportError as e:
            # Neither OSO Cloud nor OSO library is available
            raise RuntimeError(
                f"OSO provider selected, but required libraries are not installed: {e}. "
                f"Install either 'oso-cloud' (for OSO Cloud) or 'oso' (for OSO library). "
                f"Example: pip install oso-cloud"
            )
    
    if oso_client is None:
        raise RuntimeError("Failed to initialize OSO client (neither Cloud nor library mode worked)")
    
    # Wrap the OSO client in our standard adapter
    authz_instance = OsoAdapter(oso_client)
    logger.info("✔️  OSO AuthZ provider loaded and ready.")
    return authz_instance


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