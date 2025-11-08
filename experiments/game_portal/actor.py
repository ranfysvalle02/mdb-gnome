"""
Game Portal Actor
Ray Actor that handles all game portal operations including Blackjack and Dominoes.
"""

import logging
import random
import string
import json
import asyncio
from typing import Dict, Any, List, Optional

import ray
from experiment_db import create_actor_database

from . import blackjack_logic
from . import domino_logic

logger = logging.getLogger(__name__)

# Game logic modules
GAME_LOGIC_MODULES = {
    "dominoes": domino_logic,
    "blackjack": blackjack_logic,
}


@ray.remote
class ExperimentActor:
    """
    Game Portal Ray Actor.
    Handles all game operations including game creation, joining, moves, and AI players.
    """
    
    def __init__(
        self,
        mongo_uri: str = None,
        db_name: str = None,
        write_scope: str = "game_portal",
        read_scopes: list[str] = None
    ):
        self.write_scope = write_scope
        self.read_scopes = read_scopes or []
        
        # Database initialization
        try:
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            logger.info(
                f"[GamePortalActor] Initialized with "
                f"write_scope='{self.write_scope}' "
                f"(DB='{db_name}')"
            )
        except Exception as e:
            logger.critical(f"[GamePortalActor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None
        
        # Track AI players (players without WebSocket connections)
        # { "game_id": set(["player_id1", "player_id2"]) }
        self.ai_players: Dict[str, set] = {}
    
    # --- Utility Functions ---
    
    def generate_game_id(self, length=6):
        """Generates a random game ID."""
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    
    def generate_player_id(self, length=10):
        """Generates a random player ID."""
        return "p_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    
    def get_ai_players_needed(self, game_type: str, current_player_count: int) -> int:
        """Determines how many AI players are needed to fill the game."""
        if game_type == "dominoes":
            max_players = 4
            if current_player_count < max_players:
                return max_players - current_player_count
            return 0
        elif game_type == "blackjack":
            optimal_players = 4
            if current_player_count < optimal_players:
                return optimal_players - current_player_count
            return 0
        return 0
    
    def is_ai_player(self, game_id: str, player_id: str) -> bool:
        """Checks if a player is an AI player."""
        if game_id not in self.ai_players:
            return False
        return player_id in self.ai_players[game_id]
    
    # --- Game Management ---
    
    async def create_game(self, player_id: str, game_type: str, game_mode: str = "classic") -> Dict[str, Any]:
        """Creates a new game lobby."""
        if game_type not in GAME_LOGIC_MODULES:
            return {"error": "Invalid game type."}
        
        game_id = self.generate_game_id()
        
        game_document = {
            "_id": game_id,
            "game_type": game_type,
            "game_mode": game_mode if game_type == "dominoes" else None,
            "host_id": player_id,
            "players": [{"player_id": player_id}],
            "status": "waiting",
            "game_state": None
        }
        
        await self.db.games.insert_one(game_document)
        
        return {"game_id": game_id, "player_id": player_id}
    
    async def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        """Gets a game by ID."""
        return await self.db.games.find_one({"_id": game_id})
    
    async def join_game(self, game_id: str, player_id: str) -> Dict[str, Any]:
        """Allows a new player to join a waiting game."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"error": "Game not found."}
        
        if game.get('status') != 'waiting':
            return {"error": "Game has already started."}
        
        if len(game.get('players', [])) >= 4:
            return {"error": "Game is full."}
        
        # Check if player already in game
        existing_player = next(
            (p for p in game.get('players', []) 
             if (p.get('player_id') if isinstance(p, dict) else p) == player_id),
            None
        )
        if existing_player:
            return {"game_id": game_id, "player_id": player_id}
        
        new_player = {"player_id": player_id}
        
        await self.db.games.update_one(
            {"_id": game_id},
            {"$push": {"players": new_player}}
        )
        
        return {"game_id": game_id, "player_id": player_id}
    
    async def start_game(self, game_id: str, player_id: str) -> Dict[str, Any]:
        """Starts a game."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"error": "Game not found."}
        
        if game.get('host_id') != player_id:
            return {"error": "Only the host can start."}
        
        player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in game.get('players', [])]
        current_count = len(player_ids)
        game_type = game.get('game_type')
        ai_needed = self.get_ai_players_needed(game_type, current_count)
        
        # Auto-fill AI players
        if ai_needed > 0:
            ai_players = []
            for _ in range(ai_needed):
                ai_player_id = self.generate_player_id()
                ai_players.append({"player_id": ai_player_id})
                player_ids.append(ai_player_id)
            
            await self.db.games.update_one(
                {"_id": game_id},
                {"$push": {"players": {"$each": ai_players}}}
            )
            
            # Track AI players
            if game_id not in self.ai_players:
                self.ai_players[game_id] = set()
            for ai_player in ai_players:
                self.ai_players[game_id].add(ai_player["player_id"])
        
        # Refresh game document
        game = await self.db.games.find_one({"_id": game_id})
        player_ids = [p.get('player_id') if isinstance(p, dict) else p for p in game.get('players', [])]
        
        try:
            game_mode = game.get('game_mode', 'classic')
            logic_module = GAME_LOGIC_MODULES[game_type]
            
            if game_type == 'dominoes':
                initial_state = logic_module.create_new_game(player_ids, game_mode)
            else:  # blackjack
                blackjack_mode = game_mode if game_mode in ['best_of_5', 'best_of_10'] else 'best_of_5'
                initial_state = logic_module.create_new_game(player_ids, blackjack_mode)
            
            await self.db.games.update_one(
                {"_id": game_id},
                {"$set": {"game_state": initial_state, "status": "in_progress"}}
            )
            
            return {"success": True}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Error starting game: {e}", exc_info=True)
            return {"error": f"Failed to start game: {str(e)}"}
    
    # --- Game Moves ---
    
    async def play_move(self, game_id: str, player_id: str, move_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handles a player's move."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"error": "Game not found."}
        
        game_state = game.get('game_state')
        if not game_state:
            return {"error": "Game not started."}
        
        game_type = game.get('game_type')
        logic_module = GAME_LOGIC_MODULES.get(game_type)
        if not logic_module:
            return {"error": "Unknown game type."}
        
        try:
            new_state = logic_module.play_move(game_state, player_id, move_data)
            
            await self.db.games.update_one(
                {"_id": game_id},
                {"$set": {"game_state": new_state}}
            )
            
            return {"success": True}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Error processing move: {e}", exc_info=True)
            return {"error": f"Failed to process move: {str(e)}"}
    
    async def ready_for_next_round(self, game_id: str, player_id: str) -> Dict[str, Any]:
        """Marks a player as ready for the next round (Blackjack)."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"error": "Game not found."}
        
        if game.get('game_type') != 'blackjack':
            return {"error": "This action is only for blackjack."}
        
        game_state = game.get('game_state')
        if game_state.get('status') != 'round_finished':
            return {"error": "No round is finished."}
        
        if 'ready_for_next_round' not in game_state:
            game_state['ready_for_next_round'] = {}
        
        game_state['ready_for_next_round'][player_id] = True
        
        # Auto-mark AI players as ready
        all_players = game_state.get('players', [])
        for pid in all_players:
            if self.is_ai_player(game_id, pid) and pid not in game_state['ready_for_next_round']:
                game_state['ready_for_next_round'][pid] = True
        
        ready_count = len(game_state['ready_for_next_round'])
        total_players = len(all_players)
        
        all_ready = ready_count >= total_players
        
        await self.db.games.update_one(
            {"_id": game_id},
            {"$set": {"game_state": game_state}}
        )
        
        return {"success": True, "all_ready": all_ready}
    
    async def ready_for_next_hand(self, game_id: str, player_id: str) -> Dict[str, Any]:
        """Marks a player as ready for the next hand (Dominoes)."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"error": "Game not found."}
        
        if game.get('game_type') != 'dominoes':
            return {"error": "This action is only for dominoes."}
        
        game_state = game.get('game_state')
        if game_state.get('status') != 'hand_finished':
            return {"error": "No hand is finished."}
        
        if 'ready_for_next_hand' not in game_state:
            game_state['ready_for_next_hand'] = {}
        
        game_state['ready_for_next_hand'][player_id] = True
        
        # Auto-mark AI players as ready
        all_players = game_state.get('players', [])
        for pid in all_players:
            if self.is_ai_player(game_id, pid) and pid not in game_state['ready_for_next_hand']:
                game_state['ready_for_next_hand'][pid] = True
        
        ready_count = len(game_state['ready_for_next_hand'])
        total_players = len(all_players)
        
        all_ready = ready_count >= total_players
        
        await self.db.games.update_one(
            {"_id": game_id},
            {"$set": {"game_state": game_state}}
        )
        
        return {"success": True, "all_ready": all_ready}
    
    async def start_next_round(self, game_id: str):
        """Starts the next round (Blackjack)."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return
        
        game_state = game.get('game_state')
        player_ids = game_state.get('players', [])
        game_mode = game_state.get('game_mode', 'best_of_5')
        next_round_number = game_state.get('round_number', 1) + 1
        
        logic_module = GAME_LOGIC_MODULES['blackjack']
        next_round_state = logic_module.create_new_game(player_ids, game_mode)
        
        # Preserve game-level state
        next_round_state['hand_wins'] = game_state.get('hand_wins', {})
        next_round_state['round_number'] = next_round_number
        next_round_state['scores'] = game_state.get('scores', {})
        next_round_state['game_mode'] = game_mode
        next_round_state['wins_needed'] = game_state.get('wins_needed', 3)
        next_round_state['log'] = [f"🔄 Starting Round #{next_round_number}..."] + next_round_state['log']
        
        await self.db.games.update_one(
            {"_id": game_id},
            {"$set": {"game_state": next_round_state}}
        )
    
    async def start_next_hand(self, game_id: str):
        """Starts the next hand (Dominoes)."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return
        
        game_state = game.get('game_state')
        player_ids = game_state.get('players', [])
        game_mode = game_state.get('game_mode', 'classic')
        current_hand_num = game_state.get('hand_number', 1)
        next_hand_number = current_hand_num + 1
        
        next_hand_starter = game_state.get('next_hand_starter')
        
        logic_module = GAME_LOGIC_MODULES['dominoes']
        next_hand_state = logic_module.create_new_game(player_ids, game_mode)
        
        # Handle next_hand_starter if set (from deadlock tie)
        if next_hand_starter and game_mode == "boricua" and next_hand_state.get('teams'):
            teams = next_hand_state['teams']
            if next_hand_starter in teams:
                starting_team_players = teams[next_hand_starter]
                if starting_team_players:
                    start_player_id = starting_team_players[0]
                    start_tile = None
                    
                    for double in range(6, -1, -1):
                        tile = (double, double)
                        for player_id in starting_team_players:
                            if tile in next_hand_state['hands'][player_id]:
                                start_player_id = player_id
                                start_tile = tile
                                break
                        if start_tile:
                            break
                    
                    if not start_tile:
                        max_pips = -1
                        for player_id in starting_team_players:
                            for tile in next_hand_state['hands'][player_id]:
                                if sum(tile) > max_pips:
                                    max_pips = sum(tile)
                                    start_player_id = player_id
                    
                    next_hand_state['current_turn_index'] = player_ids.index(start_player_id)
                    
                    if start_tile:
                        next_hand_state['board'].append(start_tile)
                        next_hand_state['hands'][start_player_id].remove(start_tile)
                        next_hand_state['current_turn_index'] = (next_hand_state['current_turn_index'] + 1) % len(player_ids)
                        next_hand_state['last_tile_played'] = start_tile
                        next_hand_state['log'].append(f"{start_player_id} started with {start_tile} (team who started previous hand).")
        
        # Preserve game-level state
        next_hand_state['hand_wins'] = game_state.get('hand_wins', {})
        next_hand_state['hand_number'] = next_hand_number
        next_hand_state['scores'] = game_state.get('scores', {})
        next_hand_state['teams'] = game_state.get('teams')
        next_hand_state['team_scores'] = game_state.get('team_scores')
        next_hand_state['log'] = [f"🔄 Starting Hand #{next_hand_number}..."] + next_hand_state['log']
        
        await self.db.games.update_one(
            {"_id": game_id},
            {"$set": {"game_state": next_hand_state}}
        )
    
    # --- AI Player Logic ---
    
    async def make_ai_move_blackjack(self, game_id: str, player_id: str):
        """Simple AI for blackjack: hit if value < 17, otherwise stand."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return
        
        game_state = game.get('game_state')
        player_state = game_state['hands'][player_id]
        
        if player_state['status'] != 'playing':
            return
        
        if player_state['value'] < 17:
            move_data = {"action": "hit"}
        else:
            move_data = {"action": "stand"}
        
        await self.play_move(game_id, player_id, move_data)
    
    async def make_ai_move_dominoes(self, game_id: str, player_id: str):
        """Simple AI for dominoes: play first valid tile, or draw if none, or pass."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return
        
        game_state = game.get('game_state')
        hand = game_state['hands'][player_id]
        board = game_state['board']
        
        if not board:
            # First move: play highest tile
            if hand:
                highest_tile = max(hand, key=lambda t: sum(t))
                move_data = {"action": "play", "tile": list(highest_tile), "side": "right"}
                await self.play_move(game_id, player_id, move_data)
                return
        else:
            # Find a playable tile
            left_end, right_end = domino_logic.get_open_ends(board)
            for tile in hand:
                if tile[0] == left_end or tile[1] == left_end:
                    move_data = {"action": "play", "tile": list(tile), "side": "left"}
                    await self.play_move(game_id, player_id, move_data)
                    return
                elif tile[0] == right_end or tile[1] == right_end:
                    move_data = {"action": "play", "tile": list(tile), "side": "right"}
                    await self.play_move(game_id, player_id, move_data)
                    return
            
            # No playable tile: draw if possible, otherwise pass
            if game_state['boneyard']:
                move_data = {"action": "draw"}
                await self.play_move(game_id, player_id, move_data)
                return
            else:
                move_data = {"action": "pass"}
                await self.play_move(game_id, player_id, move_data)
                return
        
        # Fallback: pass
        move_data = {"action": "pass"}
        await self.play_move(game_id, player_id, move_data)
    
    async def process_ai_moves(self, game_id: str):
        """Processes AI moves until it's a human player's turn or the game ends."""
        max_iterations = 20
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            game = await self.db.games.find_one({"_id": game_id})
            if not game:
                break
            
            game_state = game.get('game_state')
            if not game_state:
                break
            
            if game_state.get('status') != 'in_progress':
                break
            
            current_turn_index = game_state.get('current_turn_index', 0)
            if current_turn_index >= len(game_state.get('players', [])):
                break
            
            current_player_id = game_state['players'][current_turn_index]
            
            # Check if current player is AI
            if not self.is_ai_player(game_id, current_player_id):
                break  # It's a human player's turn, stop processing
            
            # Make AI move
            try:
                game_type = game.get('game_type')
                if game_type == 'blackjack':
                    await self.make_ai_move_blackjack(game_id, current_player_id)
                elif game_type == 'dominoes':
                    await self.make_ai_move_dominoes(game_id, current_player_id)
                else:
                    break
                
                # Small delay to make AI moves visible
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error processing AI move for {current_player_id}: {e}", exc_info=True)
                break
    
    async def process_single_ai_move(self, game_id: str) -> Dict[str, Any]:
        """Processes a single AI move and returns whether to continue processing."""
        game = await self.db.games.find_one({"_id": game_id})
        if not game:
            return {"continue": False, "game_state": None}
        
        game_state = game.get('game_state')
        if not game_state:
            return {"continue": False, "game_state": None}
        
        if game_state.get('status') != 'in_progress':
            return {"continue": False, "game_state": game_state}
        
        current_turn_index = game_state.get('current_turn_index', 0)
        if current_turn_index >= len(game_state.get('players', [])):
            return {"continue": False, "game_state": game_state}
        
        current_player_id = game_state['players'][current_turn_index]
        
        # Check if current player is AI
        if not self.is_ai_player(game_id, current_player_id):
            return {"continue": False, "game_state": game_state}
        
        # Make AI move
        try:
            game_type = game.get('game_type')
            if game_type == 'blackjack':
                await self.make_ai_move_blackjack(game_id, current_player_id)
            elif game_type == 'dominoes':
                await self.make_ai_move_dominoes(game_id, current_player_id)
            else:
                return {"continue": False, "game_state": game_state}
            
            # Get updated game state
            updated_game = await self.db.games.find_one({"_id": game_id})
            updated_state = updated_game.get('game_state') if updated_game else None
            
            # Small delay to make AI moves visible
            await asyncio.sleep(0.5)
            
            # Check if we should continue (if next turn is also AI)
            if updated_state and updated_state.get('status') == 'in_progress':
                next_turn_index = updated_state.get('current_turn_index', 0)
                if next_turn_index < len(updated_state.get('players', [])):
                    next_player_id = updated_state['players'][next_turn_index]
                    if self.is_ai_player(game_id, next_player_id):
                        return {"continue": True, "game_state": updated_state}
            
            return {"continue": False, "game_state": updated_state}
        except Exception as e:
            logger.error(f"Error processing AI move for {current_player_id}: {e}", exc_info=True)
            return {"continue": False, "game_state": game_state}
    
    # --- State Sanitization ---
    
    async def sanitize_game_state_for_player(self, game_type: str, game_state: Dict[str, Any], player_id: str) -> Dict[str, Any]:
        """Hides sensitive info (other hands, boneyard, dealer card) before sending."""
        if not game_state:
            return None
        
        # Deep copy to avoid modifying original
        sanitized_state = json.loads(json.dumps(game_state))
        
        # Generic sanitization
        if 'hands' in sanitized_state:
            for pid, hand_data in sanitized_state['hands'].items():
                if pid != player_id:
                    if game_type == 'dominoes':
                        sanitized_state['hands'][pid] = f"{len(hand_data)} tiles"
                    elif game_type == 'blackjack':
                        sanitized_state['hands'][pid]['hand'] = f"{len(hand_data['hand'])} cards"
        
        if 'boneyard' in sanitized_state:
            if isinstance(sanitized_state['boneyard'], list):
                sanitized_state['boneyard_count'] = len(sanitized_state['boneyard'])
            sanitized_state['boneyard'] = f"{len(sanitized_state['boneyard']) if isinstance(sanitized_state['boneyard'], list) else 0} tiles"
        
        if 'deck' in sanitized_state:
            sanitized_state['deck'] = f"{len(sanitized_state['deck'])} cards"
        
        # Game-specific sanitization
        if game_type == 'blackjack' and sanitized_state.get('status') == 'in_progress':
            # Hide dealer's hole card
            if sanitized_state.get('dealer_hand') and len(sanitized_state['dealer_hand']) > 1:
                first_card = sanitized_state['dealer_hand'][0]
                sanitized_state['dealer_hand'] = [first_card, {"rank": "?", "suit": ""}]
                sanitized_state['dealer_value'] = first_card.get('value', 0)
                if first_card.get('rank') == 'A':
                    sanitized_state['dealer_value'] = 11
        
        return sanitized_state

