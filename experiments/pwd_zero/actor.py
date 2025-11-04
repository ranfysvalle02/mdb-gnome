"""
Password Manager Actor
A Ray Actor that handles all Password Manager operations.
"""

import os
import secrets
import string
import base64
import logging
import pathlib
from typing import Dict, Any, List, Optional
import ray
from bson import ObjectId
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# Actor-local paths
experiment_dir = pathlib.Path(__file__).parent
templates_dir = experiment_dir / "templates"


@ray.remote
class ExperimentActor:
    """
    Password Manager Ray Actor.
    Handles all Password Manager operations using the experiment database abstraction.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        
        # Load templates
        try:
            from fastapi.templating import Jinja2Templates
            
            if templates_dir.is_dir():
                self.templates = Jinja2Templates(directory=str(templates_dir))
            else:
                self.templates = None
                logger.warning(f"[{write_scope}-Actor] Template dir not found at {templates_dir}")
            
            logger.info(f"[{write_scope}-Actor] Successfully loaded templates.")
        except ImportError as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to load templates: {e}", exc_info=True)
            self.templates = None
        
        # Database initialization
        try:
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            logger.info(
                f"[{write_scope}-Actor] initialized with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to init DB: {e}", exc_info=True)
            self.db = None

    def _check_ready(self):
        """Check if actor is ready."""
        if not self.db:
            raise RuntimeError("Database not initialized. Check logs for import errors.")
        if not self.templates:
            raise RuntimeError("Templates not loaded. Check logs for import errors.")

    # --- Security & Encryption Helpers ---
    
    @staticmethod
    def get_encryption_key_from_password(password: str, salt: bytes) -> bytes:
        """Derives a secure 32-byte encryption key from a user's master password and a salt."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,  # Increased iterations for stronger security (OWASP recommendation)
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return key

    @staticmethod
    def encrypt_data(data: str, key: bytes) -> str:
        """Encrypts data using the derived Fernet key."""
        f = Fernet(key)
        return f.encrypt(data.encode()).decode()

    @staticmethod
    def decrypt_data(token: str, key: bytes) -> str:
        """Decrypts data using the derived Fernet key."""
        f = Fernet(key)
        return f.decrypt(token.encode()).decode()

    # --- Template Rendering Methods ---
    
    async def render_index(self):
        """Render the main password manager page."""
        self._check_ready()
        try:
            return self.templates.TemplateResponse(
                "index.html",
                {
                    "request": type('Request', (), {'url': type('URL', (), {'path': '/'})()})()
                }
            ).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering index: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    # --- API Methods ---
    
    async def register_user(self, username: str, password: str) -> Dict[str, Any]:
        """Register a new user."""
        self._check_ready()
        try:
            username_lower = username.lower()
            
            if not username_lower or len(username_lower) < 3:
                return {"status": "error", "error": "Username must be at least 3 characters long"}
            
            if not password or len(password) < 12:
                return {"status": "error", "error": "Master password must be at least 12 characters long"}
            
            # Check if user already exists
            existing_user = await self.db.users.find_one({"username": username_lower})
            if existing_user:
                return {"status": "error", "error": "Username is already taken. Please choose another."}
            
            # Generate salt and hash password
            salt = os.urandom(16)
            hashed_password = generate_password_hash(password)
            
            # Create user document
            # Note: sub_auth expects an email field, so we use username as email
            user_doc = {
                "username": username_lower,
                "email": username_lower,  # Use username as email for sub_auth compatibility
                "password": hashed_password,
                "salt": salt
            }
            
            result = await self.db.users.insert_one(user_doc)
            user_id = str(result.inserted_id)
            
            # Generate encryption key for session
            encryption_key = self.get_encryption_key_from_password(password, salt)
            
            return {
                "status": "success",
                "message": "Registration successful",
                "user_id": user_id,
                "encryption_key": encryption_key.decode()
            }
        except Exception as e:
            logger.error(f"Error registering user: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def login_user(self, username: str, password: str) -> Dict[str, Any]:
        """Authenticate a user and return user info with encryption key."""
        self._check_ready()
        try:
            username_lower = username.lower()
            
            if not username_lower or not password:
                return {"status": "error", "error": "Username and password are required"}
            
            user = await self.db.users.find_one({"username": username_lower})
            if not user or not check_password_hash(user["password"], password):
                return {"status": "error", "error": "Invalid username or master password"}
            
            # Generate encryption key from password and salt
            encryption_key = self.get_encryption_key_from_password(password, user["salt"])
            
            return {
                "status": "success",
                "message": "Login successful",
                "user_id": str(user["_id"]),
                "encryption_key": encryption_key.decode()
            }
        except Exception as e:
            logger.error(f"Error logging in user: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def check_session(self, user_id: str) -> Dict[str, Any]:
        """Check if user exists (for session validation)."""
        self._check_ready()
        try:
            user = await self.db.users.find_one({"_id": ObjectId(user_id)})
            has_user = await self.db.users.count_documents({}) > 0
            
            return {
                "authenticated": user is not None,
                "has_user": has_user
            }
        except Exception as e:
            logger.error(f"Error checking session: {e}", exc_info=True)
            return {"authenticated": False, "has_user": False}

    async def get_passwords(self, user_id: str, encryption_key: str) -> List[Dict[str, Any]]:
        """Get all passwords for a user and decrypt them."""
        self._check_ready()
        try:
            passwords = await self.db.passwords.find(
                {"user_id": ObjectId(user_id)}
            ).sort("website", 1).to_list(length=None)
            
            key = encryption_key.encode()
            decrypted_passwords = []
            
            for p in passwords:
                try:
                    p["_id"] = str(p["_id"])
                    p.pop("user_id", None)  # Don't send user_id to client
                    
                    # Decrypt the fields
                    p["website"] = self.decrypt_data(p["website"], key)
                    p["username"] = self.decrypt_data(p["username"], key)
                    p["password"] = self.decrypt_data(p["password"], key)
                    
                    decrypted_passwords.append(p)
                except Exception as e:
                    # Handle cases where decryption might fail for a specific password
                    logger.warning(f"Could not decrypt password for entry {p.get('_id')}. Error: {e}. Skipping.")
                    continue
            
            return decrypted_passwords
        except Exception as e:
            logger.error(f"Error getting passwords: {e}", exc_info=True)
            return []

    async def add_password(self, user_id: str, encryption_key: str, website: str, username: str, password: str) -> Dict[str, Any]:
        """Add a new password entry."""
        self._check_ready()
        try:
            if not all([website, username, password]):
                return {"status": "error", "error": "Missing required data fields"}
            
            key = encryption_key.encode()
            
            # Encrypt the data
            encrypted_doc = {
                "user_id": ObjectId(user_id),
                "website": self.encrypt_data(website, key),
                "username": self.encrypt_data(username, key),
                "password": self.encrypt_data(password, key)
            }
            
            result = await self.db.passwords.insert_one(encrypted_doc)
            
            # Return the decrypted version for immediate UI update
            return {
                "status": "success",
                "_id": str(result.inserted_id),
                "website": website,
                "username": username,
                "password": password
            }
        except Exception as e:
            logger.error(f"Error adding password: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def update_password(self, user_id: str, encryption_key: str, password_id: str, website: str = None, username: str = None, password: str = None) -> Dict[str, Any]:
        """Update a password entry."""
        self._check_ready()
        try:
            key = encryption_key.encode()
            update_data = {}
            
            # Encrypt each field if provided
            if website is not None:
                update_data["website"] = self.encrypt_data(website, key)
            if username is not None:
                update_data["username"] = self.encrypt_data(username, key)
            if password is not None:
                update_data["password"] = self.encrypt_data(password, key)
            
            if not update_data:
                return {"status": "error", "error": "No fields to update provided"}
            
            result = await self.db.passwords.update_one(
                {"_id": ObjectId(password_id), "user_id": ObjectId(user_id)},
                {"$set": update_data}
            )
            
            if result.matched_count == 0:
                return {"status": "error", "error": "Password not found or access denied"}
            
            # Return the updated document (decrypted)
            updated_doc = {
                "_id": password_id,
                "website": website,
                "username": username,
                "password": password
            }
            
            return {"status": "success", **updated_doc}
        except Exception as e:
            logger.error(f"Error updating password: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def delete_password(self, user_id: str, password_id: str) -> Dict[str, Any]:
        """Delete a password entry."""
        self._check_ready()
        try:
            result = await self.db.passwords.delete_one(
                {"_id": ObjectId(password_id), "user_id": ObjectId(user_id)}
            )
            
            if result.deleted_count == 0:
                return {"status": "error", "error": "Password not found or access denied"}
            
            return {"status": "success", "success": True}
        except Exception as e:
            logger.error(f"Error deleting password: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def generate_password(self, length: int = 16, uppercase: bool = True, lowercase: bool = True, numbers: bool = True, symbols: bool = True) -> Dict[str, Any]:
        """Generate a secure password."""
        self._check_ready()
        try:
            if not 12 <= length <= 99:
                return {"status": "error", "error": "Password length must be between 12 and 99."}
            
            alphabet = ''
            password_parts = []
            
            # Build the character set and guarantee at least one of each selected type
            if uppercase:
                alphabet += string.ascii_uppercase
                password_parts.append(secrets.choice(string.ascii_uppercase))
            if lowercase:
                alphabet += string.ascii_lowercase
                password_parts.append(secrets.choice(string.ascii_lowercase))
            if numbers:
                alphabet += string.digits
                password_parts.append(secrets.choice(string.digits))
            if symbols:
                # Use a curated list of symbols to avoid issues with certain websites
                safe_symbols = '!@#$%^&*()_+-=[]{}|;:,.<>?'
                alphabet += safe_symbols
                password_parts.append(secrets.choice(safe_symbols))
            
            if not alphabet:
                return {"status": "error", "error": "At least one character type must be selected."}
            
            # Fill the rest of the password length with characters from the full alphabet
            remaining_length = length - len(password_parts)
            for _ in range(remaining_length):
                password_parts.append(secrets.choice(alphabet))
            
            # Shuffle the list to ensure the guaranteed characters are not always at the start
            secrets.SystemRandom().shuffle(password_parts)
            
            password = "".join(password_parts)
            
            return {"status": "success", "password": password}
        except Exception as e:
            logger.error(f"Error generating password: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def initialize(self):
        """
        Post-initialization hook: ensures demo users exist and seeds demo content.
        This is called automatically when the actor starts up.
        
        This hook:
        1. Ensures demo user exists with username "demouser" and master password
        2. Seeds demo passwords for the demo user if they don't have any yet
        
        IMPORTANT: This seeding happens at actor initialization, regardless of who logs in.
        This ensures demo content is ready when demo users access the experiment.
        """
        import sys
        print(f"[{self.write_scope}-Actor] ⚡ INITIALIZE CALLED - Starting post-initialization setup...", flush=True, file=sys.stderr)
        logger.info(f"[{self.write_scope}-Actor] ⚡ INITIALIZE CALLED - Starting post-initialization setup...")
        
        try:
            # Ensure demo users exist
            from sub_auth import ensure_demo_users_for_actor
            from config import DEMO_EMAIL_DEFAULT, DEMO_ENABLED
            
            print(f"[{self.write_scope}-Actor] 📧 DEMO_ENABLED={DEMO_ENABLED}", flush=True, file=sys.stderr)
            logger.info(f"[{self.write_scope}-Actor] 📧 DEMO_ENABLED={DEMO_ENABLED}")
            
            # Only seed if demo is enabled
            if not DEMO_ENABLED:
                print(f"[{self.write_scope}-Actor] ⏭️  Demo seeding skipped: ENABLE_DEMO not configured", flush=True, file=sys.stderr)
                logger.info(f"[{self.write_scope}-Actor] Demo seeding skipped: ENABLE_DEMO not configured")
                return
            
            # Get the demo master password from config or use default
            demo_master_password = "DemoMasterPassword123!"
            demo_username = "demouser"
            demo_email = "demouser@pwdmanager.com"
            
            # Check if demo user already exists
            db_user = await self.db.users.find_one({"username": demo_username.lower()})
            
            if not db_user:
                # Create demo user with proper password hash and salt (password manager uses werkzeug)
                print(f"[{self.write_scope}-Actor] Creating demo user 'DEMOUSER'...", flush=True, file=sys.stderr)
                logger.info(f"[{self.write_scope}-Actor] Creating demo user 'DEMOUSER'...")
                
                salt = os.urandom(16)
                hashed_password = generate_password_hash(demo_master_password)
                
                user_doc = {
                    "username": demo_username.lower(),
                    "email": demo_email,
                    "password": hashed_password,
                    "salt": salt,
                    "is_demo": True,
                    "role": "demo"
                }
                
                result = await self.db.users.insert_one(user_doc)
                demo_user_id = result.inserted_id
                logger.info(f"[{self.write_scope}-Actor] ✅ Created demo user 'DEMOUSER' with ID: {demo_user_id}")
            else:
                # Demo user exists, ensure it has proper password hash and salt
                demo_user_id = db_user["_id"]
                needs_update = False
                update_data = {}
                
                # Check if password needs to be hashed
                if "password" not in db_user or not db_user.get("password") or not db_user["password"].startswith("pbkdf2:"):
                    # Password is not hashed, hash it with werkzeug
                    hashed_password = generate_password_hash(demo_master_password)
                    update_data["password"] = hashed_password
                    needs_update = True
                    logger.info(f"[{self.write_scope}-Actor] Hashing demo user password")
                
                # Get or create salt for demo user
                if "salt" not in db_user or not db_user.get("salt"):
                    # Create salt if it doesn't exist
                    salt = os.urandom(16)
                    update_data["salt"] = salt
                    needs_update = True
                    logger.info(f"[{self.write_scope}-Actor] Created salt for demo user")
                else:
                    salt = db_user["salt"]
                
                # Update user if needed
                if needs_update:
                    await self.db.users.update_one(
                        {"_id": demo_user_id},
                        {"$set": update_data}
                    )
                    logger.info(f"[{self.write_scope}-Actor] Updated demo user with password hash and salt")
            
            # Derive encryption key from master password and salt
            encryption_key = self.get_encryption_key_from_password(demo_master_password, salt)
            
            # Call demo seed
            from .demo_seed import check_and_seed_demo
            
            print(f"[{self.write_scope}-Actor] 🌱 Calling check_and_seed_demo for 'DEMOUSER'...", flush=True, file=sys.stderr)
            logger.info(f"[{self.write_scope}-Actor] Calling check_and_seed_demo for 'DEMOUSER'...")
            
            success = await check_and_seed_demo(
                db=self.db,
                mongo_uri=self.mongo_uri,
                db_name=self.db_name,
                demo_email=demo_email,
                encryption_key=encryption_key
            )
            
            print(f"[{self.write_scope}-Actor] ✅ check_and_seed_demo returned success={success}", flush=True, file=sys.stderr)
            logger.info(f"[{self.write_scope}-Actor] ✅ check_and_seed_demo returned success={success}")
            
            if success:
                print(f"[{self.write_scope}-Actor] ✅ Demo seeding completed successfully", flush=True, file=sys.stderr)
                logger.info(f"[{self.write_scope}-Actor] ✅ Demo seeding completed successfully")
            else:
                print(f"[{self.write_scope}-Actor] ⚠️  Demo seeding skipped or failed (may already have content)", flush=True, file=sys.stderr)
                logger.warning(f"[{self.write_scope}-Actor] ⚠️ Demo seeding skipped or failed (may already have content)")
                
        except Exception as e:
            import traceback
            print(f"[{self.write_scope}-Actor] ❌ ERROR during demo seeding: {e}", flush=True, file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            logger.error(f"[{self.write_scope}-Actor] ❌ ERROR during demo seeding: {e}", exc_info=True)

