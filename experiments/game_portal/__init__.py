"""
Game Portal Experiment
FastAPI routes that handle HTTP API and WebSocket connections for multiplayer games.
"""

import logging
import json
import asyncio
import ray
from fastapi import APIRouter, Request, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from pathlib import Path

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# Path setup
EXPERIMENT_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(EXPERIMENT_DIR / "templates"))

# Connection manager for WebSocket connections
# { "game_id": { "player_id": WebSocket } }
active_connections: Dict[str, Dict[str, WebSocket]] = {}


def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Game Portal actor handle."""
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


async def get_actor_handle_ws(websocket: WebSocket) -> "ray.actor.ActorHandle":
    """Get actor handle for WebSocket connections."""
    path_parts = websocket.url.path.strip("/").split("/")
    slug_id = "game_portal"
    if len(path_parts) >= 2 and path_parts[0] == "experiments":
        slug_id = path_parts[1]
    
    if not getattr(websocket.app.state, "ray_is_available", False):
        logger.error("Ray is globally unavailable in WebSocket")
        await websocket.close(code=503, reason="Service unavailable")
        return None
    
    actor_name = f"{slug_id}-actor"
    try:
        return ray.get_actor(actor_name, namespace="modular_labs")
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' not found in WebSocket")
        await websocket.close(code=503, reason="Service unavailable")
        return None
    except Exception as e:
        logger.error(f"Failed to get actor in WebSocket: {e}", exc_info=True)
        await websocket.close(code=503, reason="Service unavailable")
        return None


async def broadcast_to_game(game_id: str, message: Dict[str, Any]):
    """Broadcast message to all players in a game."""
    if game_id in active_connections:
        for player_id, connection in active_connections[game_id].items():
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to {player_id}: {e}")


async def send_to_player(game_id: str, player_id: str, message: Dict[str, Any]):
    """Send message to a specific player."""
    if game_id in active_connections and player_id in active_connections[game_id]:
        try:
            await active_connections[game_id][player_id].send_json(message)
        except Exception as e:
            logger.error(f"Error sending to {player_id}: {e}")


async def process_ai_moves_with_broadcast(actor, game_id: str, player_ids: list):
    """Processes AI moves and broadcasts state updates after each move."""
    max_iterations = 20
    iteration = 0
    
    while iteration < max_iterations:
        iteration += 1
        
        # Process a single AI move
        result = await actor.process_single_ai_move.remote(game_id)
        
        # Always broadcast updated state after AI move
        updated_game = await actor.get_game.remote(game_id)
        if not updated_game:
            break
        
        players_with_ai_status = []
        for p in updated_game.get('players', []):
            player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
            pid = player_dict.get('player_id', player_dict)
            player_dict['isAI'] = await actor.is_ai_player.remote(game_id, pid)
            players_with_ai_status.append(player_dict)
        
        for pid in player_ids:
            sanitized = await actor.sanitize_game_state_for_player.remote(
                updated_game.get('game_type'), updated_game.get('game_state'), pid
            )
            await send_to_player(game_id, pid, {
                "type": "state_update",
                "game_state": sanitized,
                "players": players_with_ai_status
            })
        
        # Check if we should continue
        if not result.get('continue', False):
            break
        
        # Small delay between AI moves for better UX
        await asyncio.sleep(0.3)


# --- HTTP API Models ---
class CreateGameRequest(BaseModel):
    player_id: str = Field(..., min_length=1, max_length=200)
    game_type: str = Field(..., description="e.g., 'dominoes' or 'blackjack'")
    game_mode: str = Field(default="classic", description="For dominoes: 'classic' or 'boricua'. For blackjack: 'best_of_5' or 'best_of_10'")


class JoinGameRequest(BaseModel):
    player_id: str = Field(..., min_length=1, max_length=200)


# --- HTTP API Endpoints ---
@bp.get("/", response_class=HTMLResponse, name="game_portal_index")
async def index(request: Request):
    """The main UI route for the Game Portal experiment."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


@bp.post("/api/game/create")
async def create_game(request: Request, create_req: CreateGameRequest):
    """Creates a new game lobby."""
    actor = get_actor_handle(request)
    result = await actor.create_game.remote(
        player_id=create_req.player_id,
        game_type=create_req.game_type,
        game_mode=create_req.game_mode
    )
    return result


@bp.post("/api/game/{game_id}/join")
async def join_game(request: Request, game_id: str, join_req: JoinGameRequest):
    """Allows a new player to join a waiting game."""
    actor = get_actor_handle(request)
    result = await actor.join_game.remote(game_id, join_req.player_id)
    
    # Notify lobby via WebSocket
    await broadcast_to_game(game_id, {
        "type": "player_joined",
        "player_id": join_req.player_id
    })
    
    return result


# --- WebSocket Endpoint ---
@bp.websocket("/ws/game/{game_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str, player_id: str):
    """WebSocket endpoint for real-time game communication."""
    actor = await get_actor_handle_ws(websocket)
    if not actor:
        return
    
    # Verify game and player
    game = await actor.get_game.remote(game_id)
    if not game:
        await websocket.close(code=1008, reason="Game not found")
        return
    
    player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in game.get('players', [])]
    if player_id not in player_ids:
        await websocket.close(code=1008, reason="Player not in game")
        return
    
    # Connect
    await websocket.accept()
    if game_id not in active_connections:
        active_connections[game_id] = {}
    active_connections[game_id][player_id] = websocket
    logger.info(f"Player {player_id} connected to game {game_id}.")
    
    # Send initial state
    sanitized_state = await actor.sanitize_game_state_for_player.remote(
        game.get('game_type'), game.get('game_state'), player_id
    )
    
    # Mark AI players
    players_with_ai_status = []
    for p in game.get('players', []):
        player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
        player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
        players_with_ai_status.append(player_dict)
    
    await websocket.send_json({
        "type": "connection_success",
        "game_state": sanitized_state,
        "players": players_with_ai_status,
        "game_type": game.get('game_type')
    })
    
    await broadcast_to_game(game_id, {
        "type": "player_connected",
        "player_id": player_id
    })
    
    try:
        while True:
            data = await websocket.receive_json()
            action_type = data.get('type')
            
            # Get fresh game state
            current_game = await actor.get_game.remote(game_id)
            if not current_game:
                await websocket.send_json({"type": "error", "message": "Game not found"})
                continue
            
            if action_type == 'start_game':
                if current_game.get('host_id') != player_id:
                    await websocket.send_json({"type": "error", "message": "Only the host can start."})
                    continue
                
                result = await actor.start_game.remote(game_id, player_id)
                if result.get('error'):
                    await websocket.send_json({"type": "error", "message": result['error']})
                    continue
                
                # Broadcast game started
                updated_game = await actor.get_game.remote(game_id)
                players_with_ai_status = []
                for p in updated_game.get('players', []):
                    player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                    player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                    players_with_ai_status.append(player_dict)
                
                player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in updated_game.get('players', [])]
                for pid in player_ids:
                    sanitized = await actor.sanitize_game_state_for_player.remote(
                        updated_game.get('game_type'), updated_game.get('game_state'), pid
                    )
                    await send_to_player(game_id, pid, {
                        "type": "game_started",
                        "game_state": sanitized,
                        "players": players_with_ai_status
                    })
                
                # Process AI moves with state broadcasting
                await process_ai_moves_with_broadcast(actor, game_id, player_ids)
            
            elif action_type == 'make_move':
                try:
                    result = await actor.play_move.remote(
                        game_id, player_id, data.get('move_data', {})
                    )
                    if result.get('error'):
                        await websocket.send_json({"type": "error", "message": result['error']})
                        continue
                    
                    # Broadcast updated state
                    updated_game = await actor.get_game.remote(game_id)
                    players_with_ai_status = []
                    for p in updated_game.get('players', []):
                        player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                        player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                        players_with_ai_status.append(player_dict)
                    
                    player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in updated_game.get('players', [])]
                    for pid in player_ids:
                        sanitized = await actor.sanitize_game_state_for_player.remote(
                            updated_game.get('game_type'), updated_game.get('game_state'), pid
                        )
                        await send_to_player(game_id, pid, {
                            "type": "state_update",
                            "game_state": sanitized,
                            "players": players_with_ai_status
                        })
                    
                    # Process AI moves with state broadcasting
                    await process_ai_moves_with_broadcast(actor, game_id, player_ids)
                except Exception as e:
                    logger.error(f"Error processing move: {e}", exc_info=True)
                    await websocket.send_json({"type": "error", "message": f"Failed to process move: {str(e)}"})
            
            elif action_type == 'ready_for_next_round':
                result = await actor.ready_for_next_round.remote(game_id, player_id)
                if result.get('error'):
                    await websocket.send_json({"type": "error", "message": result['error']})
                    continue
                
                # Broadcast updated state
                updated_game = await actor.get_game.remote(game_id)
                players_with_ai_status = []
                for p in updated_game.get('players', []):
                    player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                    player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                    players_with_ai_status.append(player_dict)
                
                player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in updated_game.get('players', [])]
                for pid in player_ids:
                    sanitized = await actor.sanitize_game_state_for_player.remote(
                        updated_game.get('game_type'), updated_game.get('game_state'), pid
                    )
                    await send_to_player(game_id, pid, {
                        "type": "state_update",
                        "game_state": sanitized,
                        "players": players_with_ai_status
                    })
                
                # Check if all ready and start next round
                if result.get('all_ready'):
                    await asyncio.sleep(0.5)
                    await actor.start_next_round.remote(game_id)
                    updated_game = await actor.get_game.remote(game_id)
                    players_with_ai_status = []
                    for p in updated_game.get('players', []):
                        player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                        player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                        players_with_ai_status.append(player_dict)
                    
                    for pid in player_ids:
                        sanitized = await actor.sanitize_game_state_for_player.remote(
                            updated_game.get('game_type'), updated_game.get('game_state'), pid
                        )
                        await send_to_player(game_id, pid, {
                            "type": "state_update",
                            "game_state": sanitized,
                            "players": players_with_ai_status
                        })
                    
                    await process_ai_moves_with_broadcast(actor, game_id, player_ids)
            
            elif action_type == 'ready_for_next_hand':
                result = await actor.ready_for_next_hand.remote(game_id, player_id)
                if result.get('error'):
                    await websocket.send_json({"type": "error", "message": result['error']})
                    continue
                
                # Broadcast updated state
                updated_game = await actor.get_game.remote(game_id)
                players_with_ai_status = []
                for p in updated_game.get('players', []):
                    player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                    player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                    players_with_ai_status.append(player_dict)
                
                player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in updated_game.get('players', [])]
                for pid in player_ids:
                    sanitized = await actor.sanitize_game_state_for_player.remote(
                        updated_game.get('game_type'), updated_game.get('game_state'), pid
                    )
                    await send_to_player(game_id, pid, {
                        "type": "state_update",
                        "game_state": sanitized,
                        "players": players_with_ai_status
                    })
                
                # Check if all ready and start next hand
                if result.get('all_ready'):
                    await asyncio.sleep(0.5)
                    await actor.start_next_hand.remote(game_id)
                    updated_game = await actor.get_game.remote(game_id)
                    players_with_ai_status = []
                    for p in updated_game.get('players', []):
                        player_dict = p.copy() if isinstance(p, dict) else {"player_id": p}
                        player_dict['isAI'] = await actor.is_ai_player.remote(game_id, player_dict.get('player_id', player_dict))
                        players_with_ai_status.append(player_dict)
                    
                    for pid in player_ids:
                        sanitized = await actor.sanitize_game_state_for_player.remote(
                            updated_game.get('game_type'), updated_game.get('game_state'), pid
                        )
                        await send_to_player(game_id, pid, {
                            "type": "state_update",
                            "game_state": sanitized,
                            "players": players_with_ai_status
                        })
                    
                    await process_ai_moves_with_broadcast(actor, game_id, player_ids)
    
    except WebSocketDisconnect:
        logger.info(f"Player {player_id} disconnected from game {game_id}.")
        if game_id in active_connections and player_id in active_connections[game_id]:
            del active_connections[game_id][player_id]
        await broadcast_to_game(game_id, {
            "type": "player_disconnected",
            "player_id": player_id
        })
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        if game_id in active_connections and player_id in active_connections[game_id]:
            del active_connections[game_id][player_id]
