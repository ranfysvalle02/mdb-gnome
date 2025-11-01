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
)
from utils import read_json_async

logger = logging.getLogger(__name__)


async def ensure_db_indices(db: AsyncIOMotorDatabase):
    """Ensure core MongoDB indexes exist."""
    try:
        await db.users.create_index("email", unique=True, background=True)
        await db.experiments_config.create_index("slug", unique=True, background=True)
        # Index for export logs - commonly queried by slug_id
        await db.export_logs.create_index("slug_id", background=True)
        await db.export_logs.create_index("created_at", background=True)
        await db.export_logs.create_index("checksum", background=True)
        await db.export_logs.create_index([("checksum", 1), ("invalidated", 1)], background=True)
        logger.info("✔️ Core MongoDB indexes ensured (users.email, experiments_config.slug, export_logs.slug_id, export_logs.checksum).")
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
                            manifest_data["slug"] = slug
                            if "status" not in manifest_data:
                                manifest_data["status"] = "active"
                                logger.info(f"[{slug}] No 'status' in manifest, defaulting to 'active'.")
                            
                            status = manifest_data.get("status", "unknown")
                            logger.info(f"[{slug}] Seeding with status='{status}' from manifest.")
                            
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

