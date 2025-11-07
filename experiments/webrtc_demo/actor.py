"""
WebRTC Demo Ray Actor
Handles MongoDB operations for WebRTC room data storage.
"""

import logging
from typing import Dict, Any, List, Optional
import ray

logger = logging.getLogger(__name__)


@ray.remote
class ExperimentActor:
    """
    Ray Actor for handling WebRTC room data in MongoDB.
    Uses ExperimentDB for easy database access.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        try:
            # Magical database abstraction - one line to get Motor-like API!
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            
            self.write_scope = write_scope
            self.read_scopes = read_scopes

            logger.info(
                f"[WebRTCDemoActor] started with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[WebRTCDemoActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None

    async def create_room(self, room_name: str) -> bool:
        """Create a new room in MongoDB."""
        try:
            if not self.db:
                return False
            
            # Create a new, empty room
            await self.db.rooms.insert_one({
                'room_name': room_name,
                'offer': None,
                'answer': None,
                'ice_candidates': []
            })
            logger.info(f"[WebRTCDemoActor] Created room: {room_name}")
            return True
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in create_room: {e}", exc_info=True)
            return False

    async def clear_room(self, room_name: str) -> bool:
        """Clear/delete a room from MongoDB."""
        try:
            if not self.db:
                return False
            
            result = await self.db.rooms.delete_one({'room_name': room_name})
            logger.info(f"[WebRTCDemoActor] Cleared room: {room_name}")
            return True
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in clear_room: {e}", exc_info=True)
            return False

    async def room_exists(self, room_name: str) -> bool:
        """Check if a room exists in MongoDB."""
        try:
            if not self.db:
                return False
            
            room = await self.db.rooms.find_one({'room_name': room_name})
            return room is not None
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in room_exists: {e}", exc_info=True)
            return False

    async def save_offer(self, room_name: str, offer: Dict[str, Any]) -> bool:
        """Save WebRTC offer to MongoDB."""
        try:
            if not self.db:
                return False
            
            await self.db.rooms.update_one(
                {'room_name': room_name},
                {'$set': {'offer': offer}}
            )
            logger.debug(f"[WebRTCDemoActor] Saved offer for room: {room_name}")
            return True
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in save_offer: {e}", exc_info=True)
            return False

    async def save_answer(self, room_name: str, answer: Dict[str, Any]) -> bool:
        """Save WebRTC answer to MongoDB."""
        try:
            if not self.db:
                return False
            
            await self.db.rooms.update_one(
                {'room_name': room_name},
                {'$set': {'answer': answer}}
            )
            logger.debug(f"[WebRTCDemoActor] Saved answer for room: {room_name}")
            return True
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in save_answer: {e}", exc_info=True)
            return False

    async def save_ice_candidate(self, room_name: str, candidate: Dict[str, Any]) -> bool:
        """Save WebRTC ICE candidate to MongoDB."""
        try:
            if not self.db:
                return False
            
            await self.db.rooms.update_one(
                {'room_name': room_name},
                {'$push': {'ice_candidates': candidate}}
            )
            logger.debug(f"[WebRTCDemoActor] Saved ICE candidate for room: {room_name}")
            return True
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in save_ice_candidate: {e}", exc_info=True)
            return False

    async def get_room_data(self, room_name: str) -> Optional[Dict[str, Any]]:
        """Get all room data from MongoDB."""
        try:
            if not self.db:
                return None
            
            room = await self.db.rooms.find_one({'room_name': room_name})
            if room:
                # Convert ObjectId to string for JSON serialization
                room['_id'] = str(room['_id'])
            return room
        except Exception as e:
            logger.error(f"[WebRTCDemoActor] error in get_room_data: {e}", exc_info=True)
            return None

