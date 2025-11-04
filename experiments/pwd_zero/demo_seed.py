"""
Demo Seed Module for Password Manager

This module provides demo seeding functionality for Password Manager experiment.
It seeds demo passwords for common websites for demo users with username "DEMOUSER",
only if they don't have any passwords yet.

This establishes a clean pattern for experiments to handle demo-specific seeding logic.
"""

import logging
import datetime
from typing import Optional, Tuple
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)

# Demo user configuration - import from config
from config import DEMO_EMAIL_DEFAULT, DEMO_ENABLED

# Fallback for backward compatibility
if DEMO_EMAIL_DEFAULT is None:
    DEMO_EMAIL_DEFAULT = "demo@demo.com"


async def should_seed_demo(db, mongo_uri: str, db_name: str, demo_email: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Check if demo seeding should occur for Password Manager experiment.
    
    Conditions:
    1. Experiment-specific demo user exists with username "DEMOUSER" (in experiment's users collection)
    2. Demo user has no passwords stored
    
    Note: This function checks the experiment-specific users collection (created by sub-auth),
    not the top-level users collection. This ensures compatibility with sub-auth demo user seeding.
    
    Args:
        db: ExperimentDB instance (scoped to Password Manager)
        mongo_uri: MongoDB connection URI (for accessing top-level database)
        db_name: Database name (for accessing top-level database)
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        tuple: (should_seed: bool, demo_user_id: Optional[str])
               If should_seed is True, demo_user_id will be the ObjectId as a string (from experiment's users collection)
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return False, None
        demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        # Check if experiment-specific demo user exists with username "DEMOUSER" (case-insensitive)
        if hasattr(db, 'collection'):
            demo_user = await db.collection("users").find_one({"$or": [{"username": "demouser"}, {"username": "DEMOUSER"}]}, {"_id": 1, "username": 1, "email": 1})
        else:
            users_collection = getattr(db, "users")
            demo_user = await users_collection.find_one({"$or": [{"username": "demouser"}, {"username": "DEMOUSER"}]}, {"_id": 1, "username": 1, "email": 1})
        
        if not demo_user:
            logger.debug("Experiment-specific demo user with username 'DEMOUSER' does not exist. Skipping demo seed.")
            return False, None
        
        demo_user_id = str(demo_user["_id"])
        
        # Check if demo user has any passwords
        if hasattr(db, 'passwords'):
            password_count = await db.passwords.count_documents({"user_id": ObjectId(demo_user_id)})
        else:
            passwords_collection = getattr(db, "passwords")
            password_count = await passwords_collection.count_documents({"user_id": ObjectId(demo_user_id)})
        
        print(f"📊 should_seed_demo (Password Manager): Demo user 'DEMOUSER' has {password_count} password(s)", flush=True)
        logger.info(f"should_seed_demo (Password Manager): Demo user 'DEMOUSER' has {password_count} password(s)")
        
        if password_count > 0:
            print(f"✅ Demo user 'DEMOUSER' already has {password_count} password(s). Skipping seed.", flush=True)
            logger.info(f"Demo user 'DEMOUSER' already has {password_count} password(s). Skipping demo seed.")
            return False, demo_user_id
        
        logger.info(f"Demo user 'DEMOUSER' exists and has no passwords. Proceeding with demo seed.")
        return True, demo_user_id
        
    except Exception as e:
        logger.error(f"Error checking if demo seed should occur: {e}", exc_info=True)
        return False, None


async def seed_demo_content(db, demo_user_id: str, encryption_key: bytes) -> bool:
    """
    Seed demo content for Password Manager.
    
    Creates sample password entries for common websites/services.
    All passwords are encrypted using the provided encryption key.
    
    Args:
        db: ExperimentDB instance (scoped to Password Manager)
        demo_user_id: User ID of the demo user (as string)
        encryption_key: Encryption key derived from demo user's master password
    
    Returns:
        bool: True if seeding was successful, False otherwise
    """
    try:
        print(f"🌱 Starting demo content seeding (Password Manager) for user_id={demo_user_id}", flush=True)
        logger.info(f"Starting demo content seeding (Password Manager) for user_id={demo_user_id}")
        demo_user_obj_id = ObjectId(demo_user_id)
        
        # Import encryption helpers from actor
        from experiments.pwd_zero.actor import ExperimentActor
        
        # Sample demo passwords for common websites
        demo_passwords = [
            {"website": "github.com", "username": "demouser@example.com", "password": "SecureP@ssw0rd123!"},
            {"website": "google.com", "username": "demouser@gmail.com", "password": "MyG00gleP@ss!"},
            {"website": "amazon.com", "username": "demouser@amazon.com", "password": "Am@zon2024!"},
            {"website": "netflix.com", "username": "demouser@netflix.com", "password": "NetflixStr0ng!"},
            {"website": "twitter.com", "username": "@demouser", "password": "Tw1tt3rP@ss!"},
            {"website": "linkedin.com", "username": "demouser@linkedin.com", "password": "Link3dIn2024!"},
            {"website": "spotify.com", "username": "demouser@spotify.com", "password": "Sp0tifyMUS1c!"},
            {"website": "paypal.com", "username": "demouser@paypal.com", "password": "PayP@lSecure!"},
            {"website": "dropbox.com", "username": "demouser@dropbox.com", "password": "Dr0pBox2024!"},
            {"website": "zoom.us", "username": "demouser@zoom.com", "password": "Z00mM33ting!"},
        ]
        
        # Helper function to insert encrypted password
        async def insert_password(password_data):
            try:
                website = password_data["website"]
                username = password_data["username"]
                password = password_data["password"]
                
                # Encrypt the data
                encrypted_doc = {
                    "user_id": demo_user_obj_id,
                    "website": ExperimentActor.encrypt_data(website, encryption_key),
                    "username": ExperimentActor.encrypt_data(username, encryption_key),
                    "password": ExperimentActor.encrypt_data(password, encryption_key),
                    "created_at": datetime.datetime.utcnow()
                }
                
                # Check if password with this website already exists for this user
                if hasattr(db, 'passwords'):
                    existing = await db.passwords.find_one({
                        "user_id": demo_user_obj_id,
                        "website": encrypted_doc["website"]
                    })
                else:
                    passwords_collection = getattr(db, "passwords")
                    existing = await passwords_collection.find_one({
                        "user_id": demo_user_obj_id,
                        "website": encrypted_doc["website"]
                    })
                
                if existing:
                    logger.debug(f"⚠️  Password for '{website}' already exists for user {demo_user_id}, skipping creation")
                    print(f"⚠️  Password for '{website}' already exists, skipping creation", flush=True)
                    return existing['_id']
                
                # Password doesn't exist, create it
                if hasattr(db, 'passwords'):
                    result = await db.passwords.insert_one(encrypted_doc)
                else:
                    passwords_collection = getattr(db, "passwords")
                    result = await passwords_collection.insert_one(encrypted_doc)
                
                logger.debug(f"✅ Inserted password for: {website} -> {result.inserted_id}")
                print(f"✅ Inserted password for: {website}", flush=True)
                return result.inserted_id
            except Exception as e:
                logger.error(f"❌ Failed to insert password for {password_data.get('website')}: {e}", exc_info=True)
                raise
        
        # Insert all demo passwords
        inserted_count = 0
        for password_data in demo_passwords:
            try:
                await insert_password(password_data)
                inserted_count += 1
            except Exception as e:
                logger.error(f"Failed to seed password for {password_data['website']}: {e}", exc_info=True)
                continue
        
        # Verify passwords were created
        if hasattr(db, 'passwords'):
            password_count = await db.passwords.count_documents({"user_id": demo_user_obj_id})
        else:
            passwords_collection = getattr(db, "passwords")
            password_count = await passwords_collection.count_documents({"user_id": demo_user_obj_id})
        
        logger.info("✅ Successfully seeded demo content for Password Manager!")
        logger.info(f"   - Inserted {inserted_count} demo password(s)")
        logger.info(f"   - Verification: {password_count} password(s) total for demo user")
        print(f"✅ Successfully seeded {inserted_count} demo password(s) for Password Manager!", flush=True)
        
        if password_count < len(demo_passwords):
            logger.warning(f"⚠️ Expected {len(demo_passwords)} passwords but found {password_count}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        return False


async def check_and_seed_demo(db, mongo_uri: str, db_name: str, demo_email: Optional[str] = None, encryption_key: Optional[bytes] = None) -> bool:
    """
    Main entry point for demo seeding in Password Manager experiment.
    
    Checks if demo seeding should occur and performs it if conditions are met.
    Note: This requires the encryption key from the demo user's master password.
    
    Args:
        db: ExperimentDB instance (scoped to Password Manager)
        mongo_uri: MongoDB connection URI
        db_name: Database name
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
        encryption_key: Encryption key derived from demo user's master password (required for seeding)
    
    Returns:
        bool: True if seeding occurred (or wasn't needed), False if an error occurred
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return True  # Not an error, just disabled
        demo_email = DEMO_EMAIL_DEFAULT
    
    if encryption_key is None:
        logger.warning("Encryption key not provided for demo seeding. Cannot seed encrypted passwords.")
        return False
    
    try:
        print(f"🌱 check_and_seed_demo (Password Manager): Checking if seeding needed for 'DEMOUSER'...", flush=True)
        logger.info(f"check_and_seed_demo (Password Manager): Checking if seeding needed for 'DEMOUSER'...")
        should_seed, demo_user_id = await should_seed_demo(db, mongo_uri, db_name, demo_email)
        
        print(f"🌱 check_and_seed_demo (Password Manager): should_seed={should_seed}, demo_user_id={demo_user_id}", flush=True)
        logger.info(f"check_and_seed_demo (Password Manager): should_seed={should_seed}, demo_user_id={demo_user_id}")
        
        if not should_seed:
            logger.info(f"Demo seeding skipped (conditions not met or already seeded) for 'DEMOUSER'")
            return True  # Not an error, just didn't need to seed
        
        if not demo_user_id:
            logger.warning(f"Demo user ID not found for 'DEMOUSER', cannot seed")
            return False
        
        logger.info(f"check_and_seed_demo (Password Manager): Proceeding with seeding for user_id={demo_user_id}...")
        success = await seed_demo_content(db, demo_user_id, encryption_key)
        
        logger.info(f"check_and_seed_demo (Password Manager): Seeding completed with success={success}")
        return success
        
    except Exception as e:
        logger.error(f"Error in check_and_seed_demo (Password Manager): {e}", exc_info=True)
        return False

