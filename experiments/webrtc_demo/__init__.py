"""
WebRTC Demo Experiment
FastAPI routes that handle WebSocket signaling and delegate to the Ray Actor.
"""

import logging
import json
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import Any, Dict, Optional
from pathlib import Path

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# Path setup
EXPERIMENT_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(EXPERIMENT_DIR / "templates"))

# Dictionary to hold active WebSocket connections by room name
# { "room_name": [ws1, ws2] }
connected_peers: Dict[str, list] = {}


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the WebRTC Demo actor handle."""
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=503,
            detail="Ray service is unavailable. Check Ray cluster status."
        )
    
    slug_id = getattr(request.state, "slug_id", None)
    if not slug_id:
        logger.error("Server error: slug_id not found in request state.")
        raise HTTPException(500, "Server error: slug_id not found in request state.")
    
    actor_name = f"{slug_id}-actor"
    
    try:
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' found no process running.")
        raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
    except Exception as e:
        logger.error(f"Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


async def broadcast(room_name: str, sender_ws: WebSocket, message: Dict[str, Any]):
    """Helper function to broadcast messages to all peers in a room except the sender."""
    if room_name in connected_peers:
        for ws in connected_peers[room_name]:
            if ws != sender_ws:
                try:
                    await ws.send_json(message)
                except Exception as e:
                    logger.error(f"Error broadcasting to peer in room {room_name}: {e}")


@bp.get("/", response_class=HTMLResponse, name="webrtc_demo_index")
async def index(request: Request):
    """The main UI route for the WebRTC Demo experiment."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


@bp.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for WebRTC signaling."""
    await websocket.accept()
    current_room: Optional[str] = None
    
    # Get actor handle - we need to get it from the app state
    # Since WebSocket doesn't support Depends, we'll get it manually
    # Extract slug_id from the WebSocket path
    try:
        # The path will be /experiments/webrtc_demo/ws
        # We can extract it from the URL or use a default
        path_parts = websocket.url.path.strip("/").split("/")
        slug_id = "webrtc_demo"  # Default
        if len(path_parts) >= 2 and path_parts[0] == "experiments":
            slug_id = path_parts[1]
        
        # Check Ray availability
        if not getattr(websocket.app.state, "ray_is_available", False):
            logger.error("Ray is globally unavailable in WebSocket")
            await websocket.close(code=503, reason="Service unavailable")
            return
        
        actor_name = f"{slug_id}-actor"
        actor = ray.get_actor(actor_name, namespace="modular_labs")
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' not found in WebSocket")
        await websocket.close(code=503, reason="Service unavailable")
        return
    except Exception as e:
        logger.error(f"Failed to get actor in WebSocket: {e}", exc_info=True)
        await websocket.close(code=503, reason="Service unavailable")
        return
    
    try:
        while True:
            # Receive message from client
            data = await websocket.receive_text()
            message = json.loads(data)
            action = message.get('type')
            room_name = message.get('roomName')
            
            if not room_name:
                continue
            
            current_room = room_name
            
            # --- Room Management ---
            if action == 'create-room':
                logger.info(f"User creating room: {room_name}")
                # Clear any old room data
                await actor.clear_room.remote(room_name)
                # Create a new, empty room
                await actor.create_room.remote(room_name)
                
                if room_name not in connected_peers:
                    connected_peers[room_name] = []
                connected_peers[room_name].append(websocket)
                await websocket.send_json({'type': 'room-created'})
            
            elif action == 'join-room':
                logger.info(f"User joining room: {room_name}")
                room_exists = await actor.room_exists.remote(room_name)
                
                if room_exists:
                    if room_name not in connected_peers:
                        connected_peers[room_name] = []
                    connected_peers[room_name].append(websocket)
                    
                    await websocket.send_json({'type': 'room-joined'})
                    # Notify the *other* peer (the creator) to start the connection
                    await broadcast(room_name, websocket, {'type': 'peer-joined'})
                else:
                    await websocket.send_json({'type': 'error', 'message': 'Room not found'})
            
            # --- WebRTC Signaling ---
            elif action == 'offer':
                logger.info(f"Got offer for room {room_name}")
                offer = message.get('offer')
                # Save offer to MongoDB
                await actor.save_offer.remote(room_name, offer)
                # Broadcast the offer to the other peer
                await broadcast(room_name, websocket, {'type': 'offer', 'offer': offer})
            
            elif action == 'answer':
                logger.info(f"Got answer for room {room_name}")
                answer = message.get('answer')
                # Save answer to MongoDB
                await actor.save_answer.remote(room_name, answer)
                # Broadcast the answer to the other peer
                await broadcast(room_name, websocket, {'type': 'answer', 'answer': answer})
            
            elif action == 'ice-candidate':
                logger.info(f"Got ICE candidate for room {room_name}")
                candidate = message.get('candidate')
                # Save candidate to MongoDB
                if candidate:
                    await actor.save_ice_candidate.remote(room_name, candidate)
                    # Broadcast the candidate to the other peer
                    await broadcast(room_name, websocket, {'type': 'ice-candidate', 'candidate': candidate})
    
    except WebSocketDisconnect:
        logger.info(f"User disconnected from room {current_room}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        # --- Cleanup ---
        if current_room and current_room in connected_peers:
            if websocket in connected_peers[current_room]:
                connected_peers[current_room].remove(websocket)
            if not connected_peers[current_room]:
                del connected_peers[current_room]
        # Notify the other peer (if any) that this user left
        if current_room:
            await broadcast(current_room, websocket, {'type': 'peer-left'})
