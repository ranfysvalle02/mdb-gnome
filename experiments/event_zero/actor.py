# File: /app/experiments/event_zero/actor.py

import logging
import time
import uuid
import os
import io
import base64
from enum import Enum
from dataclasses import dataclass, field, asdict, is_dataclass
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta

import ray
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False
    logger.warning("qrcode library not available. QR code generation will be disabled.")

# ==============================================================================
# 0. DATA MODELS & ENUMS
# ==============================================================================

class UserRole(str, Enum): 
    ADMIN = "admin"
    BUYER = "buyer"

class BookingStatus(str, Enum): 
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"

class TicketStatus(str, Enum): 
    VALID = "valid"
    CHECKED_IN = "checked_in"

@dataclass
class User:
    email: str
    password_hash: str
    id: str = field(default_factory=lambda: f"usr_{uuid.uuid4().hex[:10]}")
    role: UserRole = UserRole.BUYER

@dataclass
class Hotel:
    name: str
    location: str
    id: str = field(default_factory=lambda: f"hot_{uuid.uuid4().hex[:10]}")
    image_url: str = "https://placehold.co/600x400/14b8a6/white?text=Hotel"

@dataclass
class TicketTier:
    name: str
    price: float
    capacity: int
    id: str = field(default_factory=lambda: f"tier_{uuid.uuid4().hex[:6]}")
    sold_count: int = 0
    includes_hotel: bool = False
    hotel_id: Optional[str] = None
    nights_included: int = 0
    
    @property
    def available(self) -> int: 
        return self.capacity - self.sold_count

@dataclass
class Event:
    creator_id: str
    name: str
    venue: str
    starts_at: int
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:10]}")
    description: str = "No description provided."
    image_url: str = "https://placehold.co/600x400/6366f1/white?text=Event"
    is_published: bool = False
    ticket_tiers: List[TicketTier] = field(default_factory=list)

@dataclass
class Ticket:
    booking_id: str
    event_id: str
    tier_name: str
    id: str = field(default_factory=lambda: f"tkt_{uuid.uuid4().hex[:12]}")
    status: TicketStatus = TicketStatus.VALID
    lodging_details: Optional[Dict] = None

@dataclass
class Booking:
    user_id: str
    event_id: str
    total_amount: float
    ticket_ids: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"bk_{uuid.uuid4().hex[:12]}")
    status: BookingStatus = BookingStatus.PAID
    created_at: int = field(default_factory=lambda: int(time.time()))

# ==============================================================================
# 1. CUSTOM EXCEPTIONS
# ==============================================================================

class ApiException(Exception):
    status_code = 500
    message = "An unexpected error occurred."
    
    def __init__(self, message=None, status_code=None):
        super().__init__()
        if message is not None:
            self.message = message
        if status_code is not None:
            self.status_code = status_code
    
    def to_dict(self):
        return {'msg': self.message}

class NotFound(ApiException):
    status_code = 404

class Conflict(ApiException):
    status_code = 409

class ValidationError(ApiException):
    status_code = 400

class Forbidden(ApiException):
    status_code = 403

# ==============================================================================
# 2. UTILITIES
# ==============================================================================

def to_mongo(obj: Any) -> Any:
    """Converts a dataclass instance to a MongoDB-compatible dictionary."""
    if obj is None or isinstance(obj, (str, int, float, bool, datetime)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list):
        return [to_mongo(item) for item in obj]
    if isinstance(obj, dict):
        return {k: to_mongo(v) for k, v in obj.items()}
    if is_dataclass(obj):
        d = asdict(obj)
        if 'id' in d:
            d['_id'] = d.pop('id')
        return d
    return str(obj)

# ==============================================================================
# 3. RAY ACTOR
# ==============================================================================

@ray.remote
class ExperimentActor:
    """
    Tix & Lodging Platform Actor.
    The name MUST be 'ExperimentActor' for main.py to auto-detect it.
    Uses ExperimentDB for easy database access - no MongoDB knowledge needed!
    """

    def __init__(
        self,
        mongo_uri: str = None,
        db_name: str = None,
        write_scope: str = "event_zero",
        read_scopes: list[str] = None
    ):
        self.write_scope = write_scope
        self.read_scopes = read_scopes or []
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        
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
                f"[EventZeroActor] Initialized with "
                f"write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[EventZeroActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None

    async def initialize(self):
        """Idempotently seeds the database with initial data."""
        # Force log to stdout/stderr so it shows up in main process logs
        import sys
        print(f"[{self.write_scope}-Actor] ⚡ INITIALIZE CALLED - Starting post-initialization setup...", flush=True, file=sys.stderr)
        logger.critical(f"[{self.write_scope}-Actor] ⚡ INITIALIZE CALLED - Starting post-initialization setup...")
        
        if not self.db:
            print(f"[{self.write_scope}-Actor] ❌ SKIPPING: DB not ready", flush=True, file=sys.stderr)
            logger.critical(f"[{self.write_scope}-Actor] ❌ SKIPPING: DB not ready")
            logger.warning(f"[{self.write_scope}-Actor] Skipping initialize - DB not ready.")
            return
        
        logger.info(f"[{self.write_scope}-Actor] Starting post-initialization setup...")
        
        try:
            # Check what needs to be seeded
            users_count = await self.db.users.count_documents({})
            events_count = await self.db.events.count_documents({})
            hotels_count = await self.db.hotels.count_documents({})
            
            print(f"[{self.write_scope}-Actor] Current counts - Users: {users_count}, Events: {events_count}, Hotels: {hotels_count}", flush=True, file=sys.stderr)
            logger.info(f"[{self.write_scope}-Actor] Current counts - Users: {users_count}, Events: {events_count}, Hotels: {hotels_count}")
            
            # Always ensure users exist (idempotent) - use sub-auth to create users
            if users_count == 0:
                print(f"[{self.write_scope}-Actor] Seeding users via sub-auth...", flush=True, file=sys.stderr)
                logger.info("Seeding users via sub-auth...")
                
                # Use sub-auth to ensure demo users exist (from manifest.json)
                try:
                    from sub_auth import ensure_demo_users_exist
                    from core_deps import get_experiment_config
                    from fastapi import Request
                    
                    # Get experiment config for sub-auth
                    # Note: We need to create a minimal request-like object or pass config directly
                    # For now, we'll fetch config manually
                    import sys
                    from pathlib import Path
                    
                    # Fetch config - try to get from app state or create minimal config
                    # Since we're in actor context, we'll use the config pattern
                    try:
                        # Try importing config helper
                        from core_deps import get_experiment_config
                        # We need a request object, but we're in actor context
                        # For now, let's manually load manifest or use a workaround
                        config = None
                        try:
                            import json
                            manifest_path = Path(__file__).parent / "manifest.json"
                            if manifest_path.exists():
                                with open(manifest_path, 'r') as f:
                                    manifest = json.load(f)
                                    config = {"sub_auth": manifest.get("sub_auth", {})}
                        except Exception as e:
                            logger.warning(f"Could not load manifest for sub-auth: {e}")
                        
                        if config:
                            created_users = await ensure_demo_users_exist(
                                db=self.db,
                                slug_id=self.write_scope,
                                config=config,
                                mongo_uri=self.mongo_uri,
                                db_name=self.db_name
                            )
                            if created_users:
                                print(f"[{self.write_scope}-Actor] ✅ Created {len(created_users)} user(s) via sub-auth.", flush=True, file=sys.stderr)
                                logger.info(f"Created {len(created_users)} user(s) via sub-auth.")
                            else:
                                # Fallback: create users manually if sub-auth didn't create them
                                print(f"[{self.write_scope}-Actor] ⚠️  Sub-auth didn't create users, falling back to manual creation...", flush=True, file=sys.stderr)
                                logger.warning("Sub-auth didn't create users, falling back to manual creation")
                                admin = User(
                                    id="usr_admin",
                                    email="admin@example.com",
                                    password_hash=generate_password_hash("password123"),
                                    role=UserRole.ADMIN
                                )
                                buyer = User(
                                    id="usr_buyer",
                                    email="alice@example.com",
                                    password_hash=generate_password_hash("password123"),
                                    role=UserRole.BUYER
                                )
                                await self.db.users.insert_many([to_mongo(admin), to_mongo(buyer)])
                                print(f"[{self.write_scope}-Actor] ✅ Users seeded manually.", flush=True, file=sys.stderr)
                                logger.info("Users seeded manually.")
                        else:
                            # Fallback: create users manually
                            admin = User(
                                id="usr_admin",
                                email="admin@example.com",
                                password_hash=generate_password_hash("password123"),
                                role=UserRole.ADMIN
                            )
                            buyer = User(
                                id="usr_buyer",
                                email="alice@example.com",
                                password_hash=generate_password_hash("password123"),
                                role=UserRole.BUYER
                            )
                            await self.db.users.insert_many([to_mongo(admin), to_mongo(buyer)])
                            print(f"[{self.write_scope}-Actor] ✅ Users seeded manually (no config available).", flush=True, file=sys.stderr)
                            logger.info("Users seeded manually (no config available).")
                    except Exception as e:
                        logger.error(f"Error creating users via sub-auth: {e}", exc_info=True)
                        # Fallback: create users manually
                        admin = User(
                            id="usr_admin",
                            email="admin@example.com",
                            password_hash=generate_password_hash("password123"),
                            role=UserRole.ADMIN
                        )
                        buyer = User(
                            id="usr_buyer",
                            email="alice@example.com",
                            password_hash=generate_password_hash("password123"),
                            role=UserRole.BUYER
                        )
                        await self.db.users.insert_many([to_mongo(admin), to_mongo(buyer)])
                        print(f"[{self.write_scope}-Actor] ✅ Users seeded manually (fallback).", flush=True, file=sys.stderr)
                        logger.info("Users seeded manually (fallback).")
                except Exception as e:
                    logger.error(f"Error in user seeding: {e}", exc_info=True)
                    # Final fallback
                    admin = User(
                        id="usr_admin",
                        email="admin@example.com",
                        password_hash=generate_password_hash("password123"),
                        role=UserRole.ADMIN
                    )
                    buyer = User(
                        id="usr_buyer",
                        email="alice@example.com",
                        password_hash=generate_password_hash("password123"),
                        role=UserRole.BUYER
                    )
                    await self.db.users.insert_many([to_mongo(admin), to_mongo(buyer)])
                    print(f"[{self.write_scope}-Actor] ✅ Users seeded manually (exception fallback).", flush=True, file=sys.stderr)
                    logger.info("Users seeded manually (exception fallback).")
            
            # Ensure admin and buyer users exist (final verification)
            admin_doc = await self.db.users.find_one({"_id": "usr_admin"})
            if not admin_doc:
                admin_doc = await self.db.users.find_one({"email": "admin@example.com"})
            if not admin_doc:
                # Create admin user if it doesn't exist
                print(f"[{self.write_scope}-Actor] ⚠️  Admin user not found, creating...", flush=True, file=sys.stderr)
                admin = User(
                    id="usr_admin",
                    email="admin@example.com",
                    password_hash=generate_password_hash("password123"),
                    role=UserRole.ADMIN
                )
                await self.db.users.insert_one(to_mongo(admin))
                admin_doc = await self.db.users.find_one({"_id": "usr_admin"})
                print(f"[{self.write_scope}-Actor] ✅ Admin user created.", flush=True, file=sys.stderr)
                logger.info("Admin user created.")
            
            buyer_doc = await self.db.users.find_one({"_id": "usr_buyer"})
            if not buyer_doc:
                buyer_doc = await self.db.users.find_one({"email": "alice@example.com"})
            if not buyer_doc:
                # Create buyer user if it doesn't exist
                print(f"[{self.write_scope}-Actor] ⚠️  Buyer user not found, creating...", flush=True, file=sys.stderr)
                buyer = User(
                    id="usr_buyer",
                    email="alice@example.com",
                    password_hash=generate_password_hash("password123"),
                    role=UserRole.BUYER
                )
                await self.db.users.insert_one(to_mongo(buyer))
                buyer_doc = await self.db.users.find_one({"_id": "usr_buyer"})
                print(f"[{self.write_scope}-Actor] ✅ Buyer user created.", flush=True, file=sys.stderr)
                logger.info("Buyer user created.")
            
            # Get admin ID for event creation
            admin_id = admin_doc["_id"] if admin_doc else "usr_admin"
            
            # Always ensure hotels exist (idempotent)
            if hotels_count == 0:
                print(f"[{self.write_scope}-Actor] Seeding hotels...", flush=True, file=sys.stderr)
                logger.info("Seeding hotels...")
                hot1 = Hotel(
                    id="hot_metrogrand",
                    name="Metropolis Grand Hotel",
                    location="Downtown Metropolis",
                    image_url="https://images.unsplash.com/photo-1566073771259-6a8506099945?w=600&auto=format&fit=crop"
                )
                hot2 = Hotel(
                    id="hot_riverside",
                    name="The Riverside Inn",
                    location="River District",
                    image_url="https://images.unsplash.com/photo-1582719508461-905c673771fd?w=600&auto=format&fit=crop"
                )
                await self.db.hotels.insert_many([to_mongo(hot1), to_mongo(hot2)])
                print(f"[{self.write_scope}-Actor] ✅ Hotels seeded.", flush=True, file=sys.stderr)
                logger.info("Hotels seeded.")
            
            # Get hotel IDs for event creation
            hot1_doc = await self.db.hotels.find_one({"_id": "hot_metrogrand"})
            hot1_id = hot1_doc["_id"] if hot1_doc else "hot_metrogrand"
            
            # Always ensure events exist (idempotent - check for specific event IDs)
            evt1_exists = await self.db.events.find_one({"_id": "evt_synthfest26"})
            evt2_exists = await self.db.events.find_one({"_id": "evt_indierock"})
            
            if not evt1_exists or not evt2_exists:
                print(f"[{self.write_scope}-Actor] Seeding events...", flush=True, file=sys.stderr)
                logger.info("Seeding events...")
                now = int(time.time())
                three_months = now + (3 * 30 * 24 * 60 * 60)
                
                events_to_insert = []
                
                if not evt1_exists:
                    evt1 = Event(
                        id="evt_synthfest26",
                        creator_id=admin_id,
                        name="SynthWave Fest 2026",
                        venue="Metropolis Grand Arena",
                        starts_at=three_months,
                        is_published=True,
                        description="A 3-day immersive experience celebrating synthwave.",
                        image_url="https://images.unsplash.com/photo-1516450360452-9312f5e86fc7?w=600&auto=format&fit=crop",
                        ticket_tiers=[
                            TicketTier(
                                id='tier_ga',
                                name='General Admission',
                                price=99.00,
                                capacity=200
                            ),
                            TicketTier(
                                id='tier_vip',
                                name='VIP Package (2 Nights)',
                                price=749.00,
                                capacity=50,
                                sold_count=0,
                                includes_hotel=True,
                                hotel_id=hot1_id,
                                nights_included=2
                            )
                        ]
                    )
                    events_to_insert.append(to_mongo(evt1))
                
                if not evt2_exists:
                    evt2 = Event(
                        id="evt_indierock",
                        creator_id=admin_id,
                        name="Indie Rock Showcase",
                        venue="The Underground Stage",
                        starts_at=three_months + 15*24*60*60,
                        is_published=True,
                        description="Discover your next favorite band.",
                        image_url="https://images.unsplash.com/photo-1540039155733-5bb30b53aa14?w=600&auto=format&fit=crop",
                        ticket_tiers=[
                            TicketTier(
                                id='tier_gen',
                                name='General Ticket',
                                price=45.00,
                                capacity=150
                            )
                        ]
                    )
                    events_to_insert.append(to_mongo(evt2))
                
                if events_to_insert:
                    await self.db.events.insert_many(events_to_insert)
                    print(f"[{self.write_scope}-Actor] ✅ Seeded {len(events_to_insert)} event(s).", flush=True, file=sys.stderr)
                    logger.info(f"Seeded {len(events_to_insert)} event(s).")
            else:
                print(f"[{self.write_scope}-Actor] Events already exist. Skipping event seed.", flush=True, file=sys.stderr)
                logger.info("Events already exist. Skipping event seed.")
            
            # Verify events exist after seeding
            final_events_count = await self.db.events.count_documents({})
            print(f"[{self.write_scope}-Actor] ✅ Initialization complete. Final events count: {final_events_count}", flush=True, file=sys.stderr)
            logger.info(f"Initialization complete. Final events count: {final_events_count}")
            
            # Demo seeding - seed bookings/tickets for demo users
            try:
                from config import DEMO_ENABLED, DEMO_EMAIL_DEFAULT
                
                if DEMO_ENABLED:
                    print(f"[{self.write_scope}-Actor] 🌱 Starting demo seeding...", flush=True, file=sys.stderr)
                    logger.info(f"[{self.write_scope}-Actor] Starting demo seeding...")
                    
                    from .demo_seed import check_and_seed_demo
                    
                    success = await check_and_seed_demo(
                        db=self.db,
                        mongo_uri=self.mongo_uri if hasattr(self, 'mongo_uri') else None,
                        db_name=self.db_name if hasattr(self, 'db_name') else None,
                        demo_email=DEMO_EMAIL_DEFAULT
                    )
                    
                    if success:
                        print(f"[{self.write_scope}-Actor] ✅ Demo seeding completed successfully", flush=True, file=sys.stderr)
                        logger.info(f"[{self.write_scope}-Actor] ✅ Demo seeding completed successfully")
                    else:
                        print(f"[{self.write_scope}-Actor] ⚠️  Demo seeding skipped or failed (may already have content)", flush=True, file=sys.stderr)
                        logger.warning(f"[{self.write_scope}-Actor] ⚠️ Demo seeding skipped or failed (may already have content)")
                else:
                    print(f"[{self.write_scope}-Actor] ⏭️  Demo seeding skipped: ENABLE_DEMO not configured", flush=True, file=sys.stderr)
                    logger.info(f"[{self.write_scope}-Actor] Demo seeding skipped: ENABLE_DEMO not configured")
            except Exception as demo_error:
                print(f"[{self.write_scope}-Actor] ⚠️  Error during demo seeding: {demo_error}", flush=True, file=sys.stderr)
                logger.warning(f"[{self.write_scope}-Actor] Error during demo seeding: {demo_error}", exc_info=True)
                # Don't fail initialization if demo seeding fails
                
        except Exception as e:
            print(f"[{self.write_scope}-Actor] ❌ CRITICAL: Error during initialization: {e}", flush=True, file=sys.stderr)
            logger.critical(f"[{self.write_scope}-Actor] ❌ CRITICAL: Error during initialization: {e}")
            logger.error(f"[{self.write_scope}-Actor] Error during initialization: {e}", exc_info=True)

    # ==============================================================================
    # 4. SERVICE LAYER (Business Logic)
    # ==============================================================================

    async def check_in_ticket_service(self, ticket_id: str) -> dict:
        """Check in a ticket."""
        ticket_doc = await self.db.tickets.find_one({"_id": ticket_id})
        if not ticket_doc:
            raise NotFound(f"Ticket '{ticket_id}' not found")
        
        if ticket_doc['status'] == TicketStatus.CHECKED_IN.value:
            raise Conflict(f"Ticket '{ticket_id}' has already been checked in.")
        
        await self.db.tickets.update_one(
            {"_id": ticket_id},
            {"$set": {"status": TicketStatus.CHECKED_IN.value}}
        )
        
        event_doc = await self.db.events.find_one({"_id": ticket_doc['event_id']})
        event_name = event_doc['name'] if event_doc else "Unknown Event"
        
        return {
            "msg": "Check-in successful!",
            "ticket_id": ticket_id,
            "event_name": event_name,
            "tier_name": ticket_doc['tier_name']
        }

    # ==============================================================================
    # 5. AUTHENTICATION METHODS
    # ==============================================================================

    async def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """Authenticate a user by email and password.
        
        Supports both:
        - password_hash: bcrypt hashed password (production)
        - password: plain text password (demo users from sub-auth)
        """
        user_doc = await self.db.users.find_one({"email": email})
        if not user_doc:
            return None
        
        # Check password - support both password_hash (bcrypt) and password (plain text for demo)
        stored_password = user_doc.get('password_hash') or user_doc.get('password')
        if not stored_password:
            return None
        
        # If it's a bcrypt hash, verify it
        if user_doc.get('password_hash'):
            # Password is hashed (bcrypt)
            if check_password_hash(stored_password, password):
                return {
                    "user_id": user_doc['_id'],
                    "email": user_doc['email'],
                    "role": user_doc.get('role', 'buyer')
                }
        else:
            # Password is plain text (demo users from sub-auth)
            if stored_password == password:
                return {
                    "user_id": user_doc['_id'],
                    "email": user_doc['email'],
                    "role": user_doc.get('role', 'buyer')
                }
        
        return None

    async def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile by ID."""
        user_doc = await self.db.users.find_one(
            {"_id": user_id},
            projection={"password_hash": 0}
        )
        return user_doc

    # ==============================================================================
    # 6. HOTEL METHODS
    # ==============================================================================

    async def list_hotels(self, admin_only: bool = False) -> List[Dict[str, Any]]:
        """List hotels. Admin-only mode returns full details."""
        if admin_only:
            hotels = await self.db.hotels.find({}).to_list(length=None)
        else:
            hotels = await self.db.hotels.find(
                {},
                projection={"_id": 1, "name": 1, "location": 1}
            ).to_list(length=None)
        return hotels

    async def create_hotel(self, name: str, location: str, image_url: Optional[str] = None) -> Dict[str, Any]:
        """Create a new hotel."""
        if not name or not location:
            raise ValidationError("'name' and 'location' are required.")
        
        hotel = Hotel(name=name, location=location, image_url=image_url)
        try:
            await self.db.hotels.insert_one(to_mongo(hotel))
        except Exception as e:
            if "duplicate" in str(e).lower() or "E11000" in str(e):
                raise Conflict(f"Hotel with ID '{hotel.id}' already exists.")
            raise
        
        return to_mongo(hotel)

    async def update_hotel(self, hotel_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update a hotel."""
        result = await self.db.hotels.update_one(
            {"_id": hotel_id},
            {"$set": data}
        )
        if result.matched_count == 0:
            raise NotFound(f"Hotel '{hotel_id}' not found")
        return data

    async def delete_hotel(self, hotel_id: str) -> Dict[str, str]:
        """Delete a hotel if not in use."""
        # Check if hotel is in use by any event ticket tier
        is_used = await self.db.events.find_one({"ticket_tiers.hotel_id": hotel_id})
        if is_used:
            raise Conflict("Cannot delete hotel that is part of a ticket package.")
        
        result = await self.db.hotels.delete_one({"_id": hotel_id})
        if result.deleted_count == 0:
            raise NotFound(f"Hotel '{hotel_id}' not found")
        
        return {"msg": f"Hotel '{hotel_id}' deleted."}

    # ==============================================================================
    # 7. EVENT METHODS
    # ==============================================================================

    def _get_event_aggregation_pipeline(self):
        """Get MongoDB aggregation pipeline for events with hotel lookups."""
        return [
            {"$unwind": {"path": "$ticket_tiers", "preserveNullAndEmptyArrays": True}},
            {"$lookup": {
                "from": "hotels",
                "localField": "ticket_tiers.hotel_id",
                "foreignField": "_id",
                "as": "ticket_tiers.hotel_details"
            }},
            {"$unwind": {"path": "$ticket_tiers.hotel_details", "preserveNullAndEmptyArrays": True}},
            {"$addFields": {
                "ticket_tiers.hotel_name": "$ticket_tiers.hotel_details.name"
            }},
            {"$group": {
                "_id": "$_id",
                "doc": {"$first": "$$ROOT"},
                "ticket_tiers": {"$push": "$ticket_tiers"}
            }},
            {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$doc", {"ticket_tiers": "$ticket_tiers"}]}}},
            {"$project": {"doc": 0, "ticket_tiers.hotel_details": 0}}
        ]

    async def list_events(self, published_only: bool = True, admin_view: bool = False) -> List[Dict[str, Any]]:
        """List events. Published-only mode only shows published events."""
        # Debug: Check total events count
        total_count = await self.db.events.count_documents({})
        published_count = await self.db.events.count_documents({"is_published": True})
        logger.info(f"[{self.write_scope}-Actor] list_events - Total: {total_count}, Published: {published_count}, published_only={published_only}, admin_view={admin_view}")
        
        if admin_view:
            # Admin view: return all events without aggregation
            events = await self.db.events.find({}).to_list(length=None)
        elif published_only:
            # Public view: use aggregation with hotel lookups
            pipeline = [{"$match": {"is_published": True}}] + self._get_event_aggregation_pipeline()
            events = await self.db.raw.events.aggregate(pipeline).to_list(length=None)
        else:
            # All events with aggregation
            pipeline = self._get_event_aggregation_pipeline()
            events = await self.db.raw.events.aggregate(pipeline).to_list(length=None)
        
        logger.info(f"[{self.write_scope}-Actor] list_events - Returning {len(events)} event(s)")
        return events

    async def get_event_details(self, event_id: str) -> Dict[str, Any]:
        """Get event details with hotel lookups."""
        pipeline = [{"$match": {"_id": event_id}}] + self._get_event_aggregation_pipeline()
        result = await self.db.raw.events.aggregate(pipeline).to_list(length=1)
        
        if not result:
            raise NotFound(f"Event '{event_id}' not found")
        
        return result[0]

    async def create_event(self, creator_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new event."""
        new_tiers = [TicketTier(**tier) for tier in data.get('ticket_tiers', [])]
        if not new_tiers:
            raise ValidationError("At least one ticket tier is required.")
        
        new_event = Event(
            creator_id=creator_id,
            name=data['name'],
            venue=data['venue'],
            starts_at=int(data['starts_at']),
            description=data.get('description', 'No description provided.'),
            image_url=data.get('image_url', 'https://placehold.co/600x400/6366f1/white?text=Event'),
            is_published=data.get('is_published', False),
            ticket_tiers=new_tiers
        )
        
        await self.db.events.insert_one(to_mongo(new_event))
        return to_mongo(new_event)

    async def update_event(self, event_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update an event."""
        if 'ticket_tiers' in data:
            data['ticket_tiers'] = [to_mongo(TicketTier(**tier)) for tier in data['ticket_tiers']]
        
        result = await self.db.events.update_one(
            {"_id": event_id},
            {"$set": data}
        )
        if result.matched_count == 0:
            raise NotFound(f"Event '{event_id}' not found")
        
        return data

    async def delete_event(self, event_id: str) -> Dict[str, str]:
        """Delete an event if no bookings exist."""
        booking_exists = await self.db.bookings.find_one({"event_id": event_id})
        if booking_exists:
            raise Conflict("Cannot delete event with existing bookings.")
        
        result = await self.db.events.delete_one({"_id": event_id})
        if result.deleted_count == 0:
            raise NotFound(f"Event '{event_id}' not found")
        
        return {"msg": f"Event '{event_id}' deleted."}

    # ==============================================================================
    # 8. BOOKING METHODS
    # ==============================================================================

    async def checkout(self, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process checkout with transaction support."""
        event_id = data.get('event_id')
        selections = data.get('selections', {}).get('tiers', [])
        
        if not event_id or not selections:
            raise ValidationError("event_id and selections are required.")
        
        # Get the MongoDB client from the underlying database
        database = self.db.database
        client = database.client
        
        # Start session and transaction
        session = await client.start_session()
        try:
            async with session.start_transaction():
                # Get event
                event_doc = await self.db.events.find_one(
                    {"_id": event_id},
                    session=session
                )
                if not event_doc or not event_doc.get('is_published'):
                    raise ValidationError("Event is not available for booking")
                
                total_amount = 0
                new_tickets = []
                tier_map = {tier['id']: tier for tier in event_doc['ticket_tiers']}
                
                # Process selections
                for sel in selections:
                    tier_id = sel.get('tier_id')
                    quantity = sel.get('quantity', 0)
                    
                    if quantity <= 0:
                        continue
                    
                    tier = tier_map.get(tier_id)
                    if not tier:
                        raise ValidationError(f"Invalid tier_id: {tier_id}")
                    
                    if (tier['capacity'] - tier['sold_count']) < quantity:
                        raise Conflict(f"Not enough tickets for {tier['name']}")
                    
                    total_amount += tier['price'] * quantity
                    
                    # Create tickets
                    for _ in range(quantity):
                        ticket = Ticket(
                            booking_id="",
                            event_id=event_id,
                            tier_name=tier['name']
                        )
                        
                        # Add lodging details if included
                        if tier.get('includes_hotel') and tier.get('hotel_id'):
                            hotel_doc = await self.db.hotels.find_one(
                                {"_id": tier['hotel_id']},
                                session=session
                            )
                            if hotel_doc:
                                check_in_dt = datetime.fromtimestamp(event_doc['starts_at']).replace(
                                    hour=15, minute=0, second=0, microsecond=0
                                )
                                check_out_dt = check_in_dt + timedelta(days=tier.get('nights_included', 1))
                                ticket.lodging_details = {
                                    "hotel_name": hotel_doc['name'],
                                    "hotel_location": hotel_doc['location'],
                                    "check_in": check_in_dt.isoformat(),
                                    "check_out": check_out_dt.isoformat()
                                }
                        
                        new_tickets.append(ticket)
                    
                    # Update sold_count
                    await self.db.events.update_one(
                        {"_id": event_id, "ticket_tiers.id": tier_id},
                        {"$inc": {"ticket_tiers.$.sold_count": quantity}},
                        session=session
                    )
                
                if not new_tickets:
                    raise ValidationError("No tickets selected.")
                
                # Create booking
                booking = Booking(
                    user_id=user_id,
                    event_id=event_id,
                    total_amount=total_amount
                )
                
                # Update ticket booking IDs
                for t in new_tickets:
                    t.booking_id = booking.id
                
                booking.ticket_ids = [t.id for t in new_tickets]
                
                # Insert booking and tickets
                await self.db.bookings.insert_one(to_mongo(booking), session=session)
                await self.db.tickets.insert_many([to_mongo(t) for t in new_tickets], session=session)
                
                return to_mongo(booking)
        finally:
            await session.end_session()

    async def get_user_bookings(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all bookings for a user."""
        bookings = await self.db.bookings.find({"user_id": user_id}).to_list(length=None)
        return bookings

    async def get_booking_tickets(self, booking_id: str, user_id: str, is_admin: bool = False) -> List[Dict[str, Any]]:
        """Get tickets for a booking."""
        booking_doc = await self.db.bookings.find_one({"_id": booking_id})
        if not booking_doc:
            raise NotFound("Booking not found")
        
        if booking_doc['user_id'] != user_id and not is_admin:
            raise Forbidden("Access forbidden")
        
        tickets = await self.db.tickets.find(
            {"_id": {"$in": booking_doc['ticket_ids']}}
        ).to_list(length=None)
        
        return tickets

    # ==============================================================================
    # 9. QR CODE METHODS
    # ==============================================================================

    async def generate_ticket_qr_code(self, ticket_id: str, user_id: str) -> Optional[str]:
        """Generate QR code for a ticket as base64-encoded PNG."""
        if not QRCODE_AVAILABLE:
            raise ApiException("QR code generation is not available. Please install qrcode library.")
        
        # Verify ticket exists and user has access
        ticket_doc = await self.db.tickets.find_one({"_id": ticket_id})
        if not ticket_doc:
            raise NotFound(f"Ticket '{ticket_id}' not found")
        
        # Get booking to verify user access
        booking_doc = await self.db.bookings.find_one({"_id": ticket_doc['booking_id']})
        if not booking_doc:
            raise NotFound("Booking not found")
        
        # Check if user owns the booking (or is admin - check via actor context)
        if booking_doc['user_id'] != user_id:
            raise Forbidden("Access forbidden")
        
        # Generate QR code data (ticket ID + booking ID for verification)
        qr_data = f"ticket:{ticket_id}:booking:{ticket_doc['booking_id']}"
        
        # Create QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        # Create image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to base64
        img_buffer = io.BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        img_base64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')
        
        return f"data:image/png;base64,{img_base64}"
    
    async def verify_ticket_qr_code(self, qr_data: str) -> Dict[str, Any]:
        """Verify a QR code and return ticket information."""
        if not QRCODE_AVAILABLE:
            raise ApiException("QR code verification is not available.")
        
        # Parse QR code data format: "ticket:{ticket_id}:booking:{booking_id}"
        try:
            parts = qr_data.split(':')
            if len(parts) != 4 or parts[0] != 'ticket' or parts[2] != 'booking':
                raise ValidationError("Invalid QR code format")
            
            ticket_id = parts[1]
            booking_id = parts[3]
            
            # Verify ticket exists and matches booking
            ticket_doc = await self.db.tickets.find_one({"_id": ticket_id})
            if not ticket_doc:
                raise NotFound("Ticket not found")
            
            if ticket_doc['booking_id'] != booking_id:
                raise ValidationError("Ticket does not match booking")
            
            # Get booking and event info
            booking_doc = await self.db.bookings.find_one({"_id": booking_id})
            if not booking_doc:
                raise NotFound("Booking not found")
            
            event_doc = await self.db.events.find_one({"_id": booking_doc['event_id']})
            if not event_doc:
                raise NotFound("Event not found")
            
            return {
                "ticket_id": ticket_id,
                "booking_id": booking_id,
                "event_id": event_doc['_id'],
                "event_name": event_doc.get('name', 'Unknown Event'),
                "tier_name": ticket_doc.get('tier_name', 'Unknown'),
                "status": ticket_doc.get('status', 'unknown'),
                "valid": ticket_doc.get('status') == TicketStatus.VALID.value
            }
        except Exception as e:
            logger.error(f"Error verifying QR code: {e}", exc_info=True)
            raise ValidationError(f"Invalid QR code: {str(e)}")

    # ==============================================================================
    # 10. DEBUG METHODS
    # ==============================================================================

    async def get_debug_status(self) -> Dict[str, Any]:
        """Get debug status for database."""
        users_count = await self.db.users.count_documents({})
        events_count = await self.db.events.count_documents({})
        hotels_count = await self.db.hotels.count_documents({})
        published_events_count = await self.db.events.count_documents({"is_published": True})
        
        # Get sample events (limit to 5)
        sample_events = await self.db.events.find({}).limit(5).to_list(length=5)
        
        return {
            "users_count": users_count,
            "events_count": events_count,
            "hotels_count": hotels_count,
            "published_events_count": published_events_count,
            "sample_events": sample_events
        }

