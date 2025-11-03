"""Database initialization, indexing, and seeding functions."""
import os
import asyncio
import logging
import datetime
import bcrypt
from typing import Any, Dict, List, Optional
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorDatabase
from authz_provider import AuthorizationProvider
from config import (
    EXPERIMENTS_DIR,
    ADMIN_EMAIL_DEFAULT,
    ADMIN_PASSWORD_DEFAULT,
    DEMO_EMAIL_DEFAULT,
    DEMO_PASSWORD_DEFAULT,
    DEMO_ENABLED,
)
from utils import read_json_async
from manifest_schema import validate_manifest, validate_managed_indexes

logger = logging.getLogger(__name__)


async def ensure_db_indices(db: AsyncIOMotorDatabase):
    """Ensure core MongoDB indexes exist."""
    try:
        await db.users.create_index("email", unique=True, background=True)
        await db.experiments_config.create_index("slug", unique=True, background=True)
        # Index for owner_email to enable efficient ownership queries (for developers to find their experiments)
        await db.experiments_config.create_index("owner_email", background=True)
        # Compound index for efficient queries by owner and slug
        await db.experiments_config.create_index([("owner_email", 1), ("slug", 1)], background=True)
        # Index for export logs - commonly queried by slug_id
        await db.export_logs.create_index("slug_id", background=True)
        await db.export_logs.create_index("created_at", background=True)
        await db.export_logs.create_index("checksum", background=True)
        await db.export_logs.create_index([("checksum", 1), ("invalidated", 1)], background=True)
        logger.info("✔️ Core MongoDB indexes ensured (users.email, experiments_config.slug, experiments_config.owner_email, export_logs.slug_id, export_logs.checksum).")
    except Exception as e:
        logger.error(f"⚠️ Failed to ensure core MongoDB indexes: {e}", exc_info=True)


async def seed_admin(app: FastAPI):
    """Seed default admin user and sync admin roles/policies."""
    db: AsyncIOMotorDatabase = app.state.mongo_db
    authz: Optional[AuthorizationProvider] = getattr(app.state, "authz_provider", None)
    
    DB_TIMEOUT = 15.0
    
    # Fetch all users who have is_admin=True
    try:
        logger.debug("Fetching admin users from DB...")
        admin_users: List[Dict[str, Any]] = await asyncio.wait_for(
            db.users.find({"is_admin": True}, {"email": 1}).limit(1000).to_list(length=None),
            timeout=DB_TIMEOUT
        )
        logger.debug(f"Found {len(admin_users)} admin user(s).")
    except asyncio.TimeoutError:
        logger.critical(f"❌ CRITICAL: Timed out after {DB_TIMEOUT}s while fetching admin users.")
        logger.critical("This indicates a severe MongoDB connection issue. Aborting startup.")
        raise
    except Exception as e:
        logger.critical(f"❌ CRITICAL: Failed to fetch admin users: {e}", exc_info=True)
        raise
    
    # Case 1: No admin users exist. Create the default one.
    if not admin_users:
        logger.warning("⚠️ No admin user found. Seeding default administrator...")
        email = ADMIN_EMAIL_DEFAULT
        password = ADMIN_PASSWORD_DEFAULT
        
        try:
            pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        except Exception as e:
            logger.error(f"⚠️ Failed to hash default admin password: {e}", exc_info=True)
            return
        
        try:
            await asyncio.wait_for(
                db.users.insert_one({
                    "email": email,
                    "password_hash": pwd_hash,
                    "is_admin": True,
                    "created_at": datetime.datetime.utcnow(),
                }),
                timeout=DB_TIMEOUT
            )
            logger.warning(f"⚠️ Default admin user '{email}' created.")
            logger.warning("⚠️ IMPORTANT: Change the default admin password immediately!")
            
            # Seed the policies for this new user
            if (
                authz
                and hasattr(authz, "add_role_for_user")
                and hasattr(authz, "add_policy")
                and hasattr(authz, "save_policy")
            ):
                logger.info(f"Seeding default Casbin policies for new admin '{email}'...")
                await asyncio.wait_for(authz.add_role_for_user(email, "admin"), timeout=DB_TIMEOUT)
                await asyncio.wait_for(authz.add_policy("admin", "admin_panel", "access"), timeout=DB_TIMEOUT)
                
                # Seed demo role policies (readonly, highly restricted)
                await asyncio.wait_for(authz.add_policy("demo", "experiments", "view"), timeout=DB_TIMEOUT)
                
                # Seed developer role policies
                await asyncio.wait_for(authz.add_policy("developer", "experiments", "view"), timeout=DB_TIMEOUT)
                await asyncio.wait_for(authz.add_policy("developer", "experiments", "manage_own"), timeout=DB_TIMEOUT)
                
                save_op = authz.save_policy()
                if asyncio.iscoroutine(save_op):
                    await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
                
                logger.info(f"✔️ Default policies seeded for admin user '{email}'.")
            else:
                logger.warning("⚠️ AuthZ Provider not available or does not support auto policy seeding.")
        except asyncio.TimeoutError:
            logger.critical(f"❌ CRITICAL: Timed out seeding default admin user '{email}'.")
            raise
        except Exception as e:
            logger.error(f"⚠️ Failed to insert/seed default admin user '{email}': {e}", exc_info=True)
            raise
    
    # Case 2: Admin users already exist. Sync policies.
    else:
        logger.info(
            f"Found {len(admin_users)} admin user(s) already in DB. "
            "Verifying Casbin roles + policies..."
        )
        if not (
            authz
            and hasattr(authz, "add_role_for_user")
            and hasattr(authz, "add_policy")
            and hasattr(authz, "save_policy")
            and hasattr(authz, "has_policy")
            and hasattr(authz, "has_role_for_user")
        ):
            logger.warning(
                "⚠️ AuthZ Provider not available or does not support idempotent policy seeding. "
                "Existing admin user(s) may not have Casbin roles!"
            )
            return
        
        try:
            made_changes = False
            
            # Check if the "admin" role has "admin_panel:access" policy
            logger.debug("Verifying 'admin_panel' policy...")
            policy_exists = await asyncio.wait_for(
                authz.has_policy("admin", "admin_panel", "access"),
                timeout=DB_TIMEOUT
            )
            
            if not policy_exists:
                logger.info("Admin policy 'admin_panel:access' missing. Adding...")
                await asyncio.wait_for(
                    authz.add_policy("admin", "admin_panel", "access"),
                    timeout=DB_TIMEOUT
                )
                made_changes = True
            else:
                logger.debug("Admin policy 'admin_panel:access' already exists.")
            
            # Seed demo role policies (readonly, highly restricted)
            logger.debug("Verifying 'demo' role policies...")
            demo_policies = [
                ("demo", "experiments", "view"),  # Can only view experiments
            ]
            for role, obj, act in demo_policies:
                policy_exists = await asyncio.wait_for(
                    authz.has_policy(role, obj, act),
                    timeout=DB_TIMEOUT
                )
                if not policy_exists:
                    logger.info(f"Demo policy '{role}:{obj}:{act}' missing. Adding...")
                    await asyncio.wait_for(
                        authz.add_policy(role, obj, act),
                        timeout=DB_TIMEOUT
                    )
                    made_changes = True
                else:
                    logger.debug(f"Demo policy '{role}:{obj}:{act}' already exists.")
            
            # Seed developer role policies
            logger.debug("Verifying 'developer' role policies...")
            developer_policies = [
                ("developer", "experiments", "view"),  # Can view all experiments
                ("developer", "experiments", "manage_own"),  # Can manage own experiments
            ]
            for role, obj, act in developer_policies:
                policy_exists = await asyncio.wait_for(
                    authz.has_policy(role, obj, act),
                    timeout=DB_TIMEOUT
                )
                if not policy_exists:
                    logger.info(f"Developer policy '{role}:{obj}:{act}' missing. Adding...")
                    await asyncio.wait_for(
                        authz.add_policy(role, obj, act),
                        timeout=DB_TIMEOUT
                    )
                    made_changes = True
                else:
                    logger.debug(f"Developer policy '{role}:{obj}:{act}' already exists.")
            
            # Check each admin user for the "admin" role (BATCHED for performance)
            admin_emails = [user_doc.get("email") for user_doc in admin_users if user_doc.get("email")]
            logger.debug(f"Verifying 'admin' roles for {len(admin_emails)} user(s) in batch...")
            
            role_check_tasks = [
                asyncio.wait_for(
                    authz.has_role_for_user(email, "admin"),
                    timeout=DB_TIMEOUT
                )
                for email in admin_emails
            ]
            role_results = await asyncio.gather(*role_check_tasks, return_exceptions=True)
            
            # Process results and add missing roles
            for email, has_role in zip(admin_emails, role_results):
                if isinstance(has_role, Exception):
                    logger.error(f"Error checking role for '{email}': {has_role}", exc_info=True)
                    continue
                
                if not has_role:
                    logger.info(f"User '{email}' is admin but missing Casbin role. Adding...")
                    try:
                        await asyncio.wait_for(
                            authz.add_role_for_user(email, "admin"),
                            timeout=DB_TIMEOUT
                        )
                        made_changes = True
                    except Exception as e:
                        logger.error(f"Failed to add role for '{email}': {e}", exc_info=True)
                else:
                    logger.debug(f"User '{email}' already has admin role.")
            
            # Save policies ONLY if we made a change
            if made_changes:
                logger.info("Policy changes were made. Saving Casbin policies...")
                save_op = authz.save_policy()
                if asyncio.iscoroutine(save_op):
                    await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
                logger.info("✔️ Casbin policies saved successfully.")
            else:
                logger.info("✔️ All Casbin admin policies are already in sync. No changes needed.")
        except asyncio.TimeoutError:
            logger.critical(f"❌ CRITICAL: Timed out after {DB_TIMEOUT}s during Casbin policy sync.")
            logger.critical("This is likely a MongoDB connection/firewall issue. Aborting startup.")
            raise
        except Exception as e:
            logger.critical(f"❌ CRITICAL: Failed to sync admin roles/policies: {e}", exc_info=True)
            raise


async def seed_demo_user(app: FastAPI):
    """Seed default demo user with demo role."""
    # Skip if demo is disabled
    if not DEMO_ENABLED or not DEMO_EMAIL_DEFAULT or not DEMO_PASSWORD_DEFAULT:
        logger.debug("Demo user seeding skipped: ENABLE_DEMO not configured or disabled.")
        return
    
    db: AsyncIOMotorDatabase = app.state.mongo_db
    authz: Optional[AuthorizationProvider] = getattr(app.state, "authz_provider", None)
    
    DB_TIMEOUT = 15.0
    demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        # Check if demo user already exists
        existing_demo_user = await asyncio.wait_for(
            db.users.find_one({"email": demo_email}, {"_id": 1, "email": 1}),
            timeout=DB_TIMEOUT
        )
        
        if existing_demo_user:
            logger.debug(f"Demo user '{demo_email}' already exists. Verifying demo role...")
            
            # Verify demo user has demo role
            if authz and hasattr(authz, "has_role_for_user") and hasattr(authz, "add_role_for_user"):
                has_demo_role = await asyncio.wait_for(
                    authz.has_role_for_user(demo_email, "demo"),
                    timeout=DB_TIMEOUT
                )
                
                if not has_demo_role:
                    logger.info(f"Demo user '{demo_email}' exists but missing 'demo' role. Adding...")
                    await asyncio.wait_for(
                        authz.add_role_for_user(demo_email, "demo"),
                        timeout=DB_TIMEOUT
                    )
                    
                    save_op = authz.save_policy()
                    if asyncio.iscoroutine(save_op):
                        await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
                    
                    logger.info(f"✔️ Added 'demo' role to existing user '{demo_email}'.")
                else:
                    logger.debug(f"Demo user '{demo_email}' already has 'demo' role.")
        else:
            # Create demo user
            logger.info(f"Creating demo user '{demo_email}'...")
            password = DEMO_PASSWORD_DEFAULT
            
            try:
                pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
            except Exception as e:
                logger.error(f"⚠️ Failed to hash demo password: {e}", exc_info=True)
                return
            
            try:
                await asyncio.wait_for(
                    db.users.insert_one({
                        "email": demo_email,
                        "password_hash": pwd_hash,
                        "is_admin": False,
                        "created_at": datetime.datetime.utcnow(),
                    }),
                    timeout=DB_TIMEOUT
                )
                logger.info(f"✔️ Demo user '{demo_email}' created.")
                logger.warning(f"⚠️ Demo user password is '{password}'. Consider changing it in production.")
                
                # Assign demo role to the user
                if (
                    authz
                    and hasattr(authz, "add_role_for_user")
                    and hasattr(authz, "save_policy")
                ):
                    logger.info(f"Assigning 'demo' role to demo user '{demo_email}'...")
                    await asyncio.wait_for(
                        authz.add_role_for_user(demo_email, "demo"),
                        timeout=DB_TIMEOUT
                    )
                    
                    save_op = authz.save_policy()
                    if asyncio.iscoroutine(save_op):
                        await asyncio.wait_for(save_op, timeout=DB_TIMEOUT)
                    
                    logger.info(f"✔️ Demo role assigned to user '{demo_email}'.")
                else:
                    logger.warning("⚠️ AuthZ Provider not available. Demo role not assigned.")
            except Exception as e:
                logger.error(f"⚠️ Failed to create demo user '{demo_email}': {e}", exc_info=True)
                # Don't raise - demo user is not critical for system startup
                
    except asyncio.TimeoutError:
        logger.error(f"⚠️ Timed out while seeding demo user '{demo_email}'. Skipping...")
        # Don't raise - demo user is not critical for system startup
    except Exception as e:
        logger.error(f"⚠️ Failed to seed demo user '{demo_email}': {e}", exc_info=True)
        # Don't raise - demo user is not critical for system startup


async def seed_db_from_local_files(db: AsyncIOMotorDatabase):
    """Seed database from local manifest.json files in experiments directory."""
    logger.info("Checking for local manifests to seed database...")
    if not EXPERIMENTS_DIR.is_dir():
        logger.warning(f"⚠️ Experiments directory '{EXPERIMENTS_DIR}' not found, skipping local seed.")
        return
    
    seeded_count = 0
    try:
        for item in EXPERIMENTS_DIR.iterdir():
            if item.is_dir() and not item.name.startswith(("_", ".")):
                slug = item.name
                manifest_path = item / "manifest.json"
                
                if manifest_path.is_file():
                    exists = await db.experiments_config.find_one({"slug": slug}, {"_id": 1, "slug": 1})
                    if not exists:
                        logger.warning(f"[{slug}] No DB config found. Seeding from local 'manifest.json'...")
                        try:
                            manifest_data = await read_json_async(manifest_path)
                            if not isinstance(manifest_data, dict):
                                logger.error(f"[{slug}] ❌ FAILED to seed: manifest.json is not valid JSON object.")
                                continue
                            
                            # Validate manifest before seeding
                            is_valid, validation_error, error_paths = validate_manifest(manifest_data)
                            if not is_valid:
                                error_path_str = f" (errors in: {', '.join(error_paths[:3])})" if error_paths else ""
                                logger.error(
                                    f"[{slug}] ❌ FAILED to seed: Manifest validation failed: {validation_error}{error_path_str}"
                                )
                                continue
                            
                            # Validate managed_indexes if present
                            if "managed_indexes" in manifest_data:
                                is_valid_indexes, index_error = validate_managed_indexes(manifest_data["managed_indexes"])
                                if not is_valid_indexes:
                                    logger.error(f"[{slug}] ❌ FAILED to seed: Index validation failed: {index_error}")
                                    continue
                            
                            manifest_data["slug"] = slug
                            if "status" not in manifest_data:
                                manifest_data["status"] = "active"
                                logger.info(f"[{slug}] No 'status' in manifest, defaulting to 'active'.")
                            
                            # Do not set owner_email for experiments seeded from local files
                            # Ownership will be assigned when the experiment is uploaded via admin panel
                            # This ensures backward compatibility: existing experiments without owners
                            # can only be managed by admins until explicitly assigned an owner
                            
                            status = manifest_data.get("status", "unknown")
                            logger.info(f"[{slug}] Seeding with status='{status}' from manifest (no owner assigned - admins can manage, will get owner when uploaded).")
                            
                            await db.experiments_config.insert_one(manifest_data)
                            logger.info(f"[{slug}] ✅ SUCCESS: Seeded DB from local manifest.json (status='{status}').")
                            seeded_count += 1
                        except Exception as e:
                            logger.error(f"[{slug}] ❌ FAILED to seed: {e}", exc_info=True)
                    else:
                        # Update existing experiment if manifest says "active" but DB has "draft"
                        try:
                            manifest_data = await read_json_async(manifest_path)
                            if isinstance(manifest_data, dict):
                                manifest_status = manifest_data.get("status", "active")
                                db_status = exists.get("status", "draft")
                                
                                if manifest_status == "active" and db_status == "draft":
                                    await db.experiments_config.update_one(
                                        {"slug": slug},
                                        {"$set": {"status": "active"}}
                                    )
                                    logger.info(f"[{slug}] 🔄 Updated status from 'draft' to 'active' to match manifest.")
                                    seeded_count += 1
                                elif manifest_status == "active" and db_status != "active":
                                    logger.debug(f"[{slug}] Manifest says 'active', DB has '{db_status}' - not updating (preserving DB status).")
                        except Exception as e:
                            logger.debug(f"[{slug}] Could not check manifest for status update: {e}")
                        logger.debug(f"[{slug}] Skipping seed: DB config already exists.")
    except OSError as e:
        logger.error(f"⚠️ Error scanning experiments directory for seeding: {e}")
    
    if seeded_count > 0:
        logger.info(f"✔️ Successfully seeded {seeded_count} new experiment(s) from filesystem.")
    else:
        logger.info("✔️ No new local manifests found to seed. Database is up-to-date.")

