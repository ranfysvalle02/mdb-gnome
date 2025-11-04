"""
Demo Seed Module for Event Zero (Tix & Lodging Platform)

This module provides demo seeding functionality for Event Zero experiment.
It seeds demo bookings and tickets for demo users, only if they don't have any bookings yet.

This establishes a clean pattern for experiments to handle demo-specific seeding logic.
"""

import logging
import time
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
    Check if demo seeding should occur for Event Zero experiment.
    
    Conditions:
    1. Experiment-specific demo user exists (with matching demo_email in experiment's users collection)
    2. Demo user has no bookings in Event Zero
    
    Note: This function checks the experiment-specific users collection (created by sub-auth),
    not the top-level users collection. This ensures compatibility with sub-auth demo user seeding.
    
    Args:
        db: ExperimentDB instance (scoped to Event Zero)
        mongo_uri: MongoDB connection URI (for accessing top-level database)
        db_name: Database name (for accessing top-level database)
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        tuple: (should_seed: bool, demo_user_id: Optional[str])
               If should_seed is True, demo_user_id will be the user ID as a string (from experiment's users collection)
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return False, None
        demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        # Check if experiment-specific demo user exists (created by sub-auth)
        if hasattr(db, 'users'):
            demo_user = await db.users.find_one({"email": demo_email}, {"_id": 1, "email": 1, "role": 1})
        else:
            users_collection = getattr(db, "users")
            demo_user = await users_collection.find_one({"email": demo_email}, {"_id": 1, "email": 1, "role": 1})
        
        if not demo_user:
            logger.debug(f"Experiment-specific demo user '{demo_email}' does not exist. Skipping demo seed.")
            return False, None
        
        # Get user ID - handle both string and ObjectId
        # In event_zero, user IDs are stored as strings, but sub-auth might create ObjectId
        demo_user_id_raw = demo_user["_id"]
        if isinstance(demo_user_id_raw, ObjectId):
            # If it's an ObjectId from sub-auth, convert to string for event_zero compatibility
            demo_user_id = str(demo_user_id_raw)
        else:
            # Already a string (event_zero format)
            demo_user_id = demo_user_id_raw
        
        # Check if demo user has any bookings
        # Try both string and ObjectId formats for compatibility
        if hasattr(db, 'bookings'):
            booking_count = await db.bookings.count_documents({
                "$or": [
                    {"user_id": demo_user_id},
                    {"user_id": demo_user_id_raw}
                ]
            })
        else:
            bookings_collection = getattr(db, "bookings")
            booking_count = await bookings_collection.count_documents({
                "$or": [
                    {"user_id": demo_user_id},
                    {"user_id": demo_user_id_raw}
                ]
            })
        
        print(f"📊 should_seed_demo (Event Zero): Demo user '{demo_email}' has {booking_count} booking(s)", flush=True)
        logger.info(f"should_seed_demo (Event Zero): Demo user '{demo_email}' has {booking_count} booking(s)")
        
        if booking_count > 0:
            print(f"✅ Demo user '{demo_email}' already has {booking_count} booking(s). Skipping seed.", flush=True)
            logger.info(f"Demo user '{demo_email}' already has {booking_count} booking(s). Skipping demo seed.")
            return False, demo_user_id
        
        logger.info(f"Demo user '{demo_email}' exists and has no bookings. Proceeding with demo seed.")
        return True, demo_user_id
        
    except Exception as e:
        logger.error(f"Error checking if demo seed should occur: {e}", exc_info=True)
        return False, None


async def seed_demo_content(db, demo_user_id: str) -> bool:
    """
    Seed demo content for Event Zero.
    
    Creates demo bookings and tickets for the demo user:
    1. A booking for SynthWave Fest 2026 with VIP Package (includes hotel)
    2. A booking for Indie Rock Showcase with General Ticket
    
    Args:
        db: ExperimentDB instance (scoped to Event Zero)
        demo_user_id: User ID of the demo user (as string)
    
    Returns:
        bool: True if seeding was successful, False otherwise
    """
    try:
        print(f"🌱 Starting demo content seeding (Event Zero) for user_id={demo_user_id}", flush=True)
        logger.info(f"Starting demo content seeding (Event Zero) for user_id={demo_user_id}")
        
        # Import models from actor
        from experiments.event_zero.actor import Booking, Ticket, TicketStatus, BookingStatus, to_mongo
        
        # Get available events
        evt1_doc = await db.events.find_one({"_id": "evt_synthfest26"})
        evt2_doc = await db.events.find_one({"_id": "evt_indierock"})
        
        if not evt1_doc or not evt2_doc:
            logger.warning("Required events not found. Make sure events are seeded first.")
            print(f"⚠️  Required events not found. Make sure events are seeded first.", flush=True)
            return False
        
        bookings_created = []
        tickets_created = []
        
        # ========================================================================
        # BOOKING 1: SynthWave Fest 2026 - VIP Package (with hotel)
        # ========================================================================
        if evt1_doc:
            # Find VIP tier
            vip_tier = None
            for tier in evt1_doc.get('ticket_tiers', []):
                if tier.get('id') == 'tier_vip' or tier.get('name', '').lower().find('vip') >= 0:
                    vip_tier = tier
                    break
            
            if vip_tier:
                # Create booking
                booking1 = Booking(
                    user_id=demo_user_id,
                    event_id="evt_synthfest26",
                    total_amount=vip_tier.get('price', 749.00),
                    status=BookingStatus.PAID
                )
                
                # Create ticket
                ticket1 = Ticket(
                    booking_id=booking1.id,
                    event_id="evt_synthfest26",
                    tier_name=vip_tier.get('name', 'VIP Package (2 Nights)')
                )
                
                # Add lodging details if hotel is included
                if vip_tier.get('includes_hotel') and vip_tier.get('hotel_id'):
                    hotel_doc = await db.hotels.find_one({"_id": vip_tier.get('hotel_id')})
                    if hotel_doc:
                        from datetime import datetime, timedelta
                        event_timestamp = evt1_doc.get('starts_at', int(time.time()) + 3 * 30 * 24 * 60 * 60)
                        check_in_dt = datetime.fromtimestamp(event_timestamp).replace(
                            hour=15, minute=0, second=0, microsecond=0
                        )
                        nights = vip_tier.get('nights_included', 2)
                        check_out_dt = check_in_dt + timedelta(days=nights)
                        ticket1.lodging_details = {
                            "hotel_name": hotel_doc.get('name'),
                            "hotel_location": hotel_doc.get('location'),
                            "check_in": check_in_dt.isoformat(),
                            "check_out": check_out_dt.isoformat()
                        }
                
                booking1.ticket_ids = [ticket1.id]
                
                # Insert booking and ticket
                await db.bookings.insert_one(to_mongo(booking1))
                await db.tickets.insert_one(to_mongo(ticket1))
                
                # Update sold_count for the tier
                await db.events.update_one(
                    {"_id": "evt_synthfest26", "ticket_tiers.id": vip_tier.get('id')},
                    {"$inc": {"ticket_tiers.$.sold_count": 1}}
                )
                
                bookings_created.append(booking1.id)
                tickets_created.append(ticket1.id)
                logger.info(f"Created booking {booking1.id} for SynthWave Fest VIP Package")
        
        # ========================================================================
        # BOOKING 2: Indie Rock Showcase - General Ticket
        # ========================================================================
        if evt2_doc:
            # Find General tier
            gen_tier = None
            for tier in evt2_doc.get('ticket_tiers', []):
                if tier.get('id') == 'tier_gen' or tier.get('name', '').lower().find('general') >= 0:
                    gen_tier = tier
                    break
            
            if gen_tier:
                # Create booking
                booking2 = Booking(
                    user_id=demo_user_id,
                    event_id="evt_indierock",
                    total_amount=gen_tier.get('price', 45.00),
                    status=BookingStatus.PAID
                )
                
                # Create ticket
                ticket2 = Ticket(
                    booking_id=booking2.id,
                    event_id="evt_indierock",
                    tier_name=gen_tier.get('name', 'General Ticket')
                )
                
                booking2.ticket_ids = [ticket2.id]
                
                # Insert booking and ticket
                await db.bookings.insert_one(to_mongo(booking2))
                await db.tickets.insert_one(to_mongo(ticket2))
                
                # Update sold_count for the tier
                await db.events.update_one(
                    {"_id": "evt_indierock", "ticket_tiers.id": gen_tier.get('id')},
                    {"$inc": {"ticket_tiers.$.sold_count": 1}}
                )
                
                bookings_created.append(booking2.id)
                tickets_created.append(ticket2.id)
                logger.info(f"Created booking {booking2.id} for Indie Rock Showcase")
        
        # Verify bookings were created
        booking_count = await db.bookings.count_documents({"user_id": demo_user_id})
        ticket_count = await db.tickets.count_documents({"booking_id": {"$in": bookings_created}})
        
        logger.info("✅ Successfully seeded demo content for Event Zero!")
        logger.info(f"   - Created {len(bookings_created)} booking(s)")
        logger.info(f"   - Created {len(tickets_created)} ticket(s)")
        logger.info(f"   - Verification: {booking_count} booking(s), {ticket_count} ticket(s) for demo user")
        print(f"✅ Successfully seeded {len(bookings_created)} booking(s) and {len(tickets_created)} ticket(s) for Event Zero!", flush=True)
        
        if booking_count != len(bookings_created):
            logger.warning(f"⚠️ Expected {len(bookings_created)} bookings but found {booking_count}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        print(f"❌ Error seeding demo content: {e}", flush=True)
        return False


async def check_and_seed_demo(db, mongo_uri: str, db_name: str, demo_email: Optional[str] = None) -> bool:
    """
    Main entry point for demo seeding in Event Zero experiment.
    
    Checks if demo seeding should occur and performs it if conditions are met.
    
    Args:
        db: ExperimentDB instance (scoped to Event Zero)
        mongo_uri: MongoDB connection URI
        db_name: Database name
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        bool: True if seeding occurred (or wasn't needed), False if an error occurred
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return True  # Not an error, just disabled
        demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        print(f"🌱 check_and_seed_demo (Event Zero): Checking if seeding needed for '{demo_email}'...", flush=True)
        logger.info(f"check_and_seed_demo (Event Zero): Checking if seeding needed for '{demo_email}'...")
        should_seed, demo_user_id = await should_seed_demo(db, mongo_uri, db_name, demo_email)
        
        print(f"🌱 check_and_seed_demo (Event Zero): should_seed={should_seed}, demo_user_id={demo_user_id}", flush=True)
        logger.info(f"check_and_seed_demo (Event Zero): should_seed={should_seed}, demo_user_id={demo_user_id}")
        
        if not should_seed:
            logger.info(f"Demo seeding skipped (conditions not met or already seeded) for '{demo_email}'")
            return True  # Not an error, just didn't need to seed
        
        if not demo_user_id:
            logger.warning(f"Demo user ID not found for '{demo_email}', cannot seed")
            return False
        
        logger.info(f"check_and_seed_demo (Event Zero): Proceeding with seeding for user_id={demo_user_id}...")
        success = await seed_demo_content(db, demo_user_id)
        
        logger.info(f"check_and_seed_demo (Event Zero): Seeding completed with success={success}")
        return success
        
    except Exception as e:
        logger.error(f"Error in check_and_seed_demo (Event Zero): {e}", exc_info=True)
        return False

