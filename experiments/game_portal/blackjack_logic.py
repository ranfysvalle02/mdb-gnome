"""
Blackjack Game Logic
Core game logic for Blackjack gameplay.
"""

import random

# --- Constants ---
SUITS = ['♥', '♦', '♣', '♠']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
VALUES = {'A': 11, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9, '10': 10, 'J': 10, 'Q': 10, 'K': 10}


def create_deck():
    """Creates a shuffled deck of cards."""
    deck = [{'rank': rank, 'suit': suit, 'value': VALUES[rank]} for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


def calculate_hand_value(hand):
    """Calculates the best value of a hand, handling Aces."""
    value = sum(card['value'] for card in hand)
    aces = sum(1 for card in hand if card['rank'] == 'A')
    
    while value > 21 and aces > 0:
        value -= 10
        aces -= 1
    return value


def create_new_game(player_ids: list, game_mode: str = "best_of_5"):
    """Sets up a new Blackjack game."""
    deck = create_deck()
    hands = {}
    
    # Determine wins needed based on game mode
    wins_needed = 3 if game_mode == "best_of_5" else 5 if game_mode == "best_of_10" else 3
    
    # Initialize scores and round tracking
    scores = {pid: 0 for pid in player_ids}
    hand_wins = {pid: 0 for pid in player_ids}
    round_number = 1
    
    # Create player hands
    for pid in player_ids:
        hands[pid] = {
            "hand": [deck.pop(), deck.pop()],
            "value": 0,
            "status": "playing",
            "bet": 10
        }
    
    # Create dealer hand
    dealer_hand = [deck.pop(), deck.pop()]
    
    for pid in player_ids:
        hands[pid]['value'] = calculate_hand_value(hands[pid]['hand'])
    
    game_state = {
        "deck": deck,
        "hands": hands,
        "dealer_hand": dealer_hand,
        "dealer_value": calculate_hand_value(dealer_hand),
        "players": player_ids,
        "current_turn_index": 0,
        "status": "in_progress",
        "scores": scores,
        "hand_wins": hand_wins,
        "round_number": round_number,
        "game_mode": game_mode,
        "wins_needed": wins_needed,
        "log": [f"Round {round_number} started. Dealing hands. (Best of {wins_needed * 2 - 1})"]
    }
    
    # Check for immediate Blackjacks
    for pid in player_ids:
        if hands[pid]['value'] == 21:
            hands[pid]['status'] = "stood"
            game_state['log'].append(f"{pid} has Blackjack!")
    
    # Check if all players are done
    if all(p['status'] != 'playing' for p in hands.values()):
        game_state = _run_dealer_turn(game_state)
        game_state = _calculate_winners(game_state)
    
    return game_state


def play_move(game_state: dict, player_id: str, move_data: dict):
    """Handles a player's move (hit or stand)."""
    turn_index = game_state['current_turn_index']
    if turn_index >= len(game_state['players']):
        raise ValueError("It's not the players' turn.")
    
    turn_player_id = game_state['players'][turn_index]
    if player_id != turn_player_id:
        raise ValueError("It's not your turn.")
    
    action = move_data.get('action')
    player_state = game_state['hands'][player_id]
    if player_state['status'] != 'playing':
        raise ValueError("You have already stood or busted.")
    
    if action == "hit":
        new_card = game_state['deck'].pop()
        player_state['hand'].append(new_card)
        player_state['value'] = calculate_hand_value(player_state['hand'])
        game_state['log'].append(f"{player_id} hits and gets a {new_card['rank']}{new_card['suit']}.")
        
        if player_state['value'] > 21:
            player_state['status'] = "busted"
            game_state['log'].append(f"{player_id} busts!")
    
    elif action == "stand":
        player_state['status'] = "stood"
        game_state['log'].append(f"{player_id} stands with {player_state['value']}.")
    else:
        raise ValueError("Invalid action. Must be 'hit' or 'stand'.")
    
    # If the current player is done, move to the next.
    if player_state['status'] != 'playing':
        game_state['current_turn_index'] += 1
    
    # If all players have had their turn
    if game_state['current_turn_index'] == len(game_state['players']):
        game_state['log'].append("All players are done. Dealer's turn.")
        game_state = _run_dealer_turn(game_state)
        game_state = _calculate_winners(game_state)
    
    return game_state


def _run_dealer_turn(game_state: dict):
    """Runs the dealer's turn automatically."""
    dealer_hand = game_state['dealer_hand']
    game_state['dealer_value'] = calculate_hand_value(dealer_hand)
    
    while game_state['dealer_value'] < 17:
        new_card = game_state['deck'].pop()
        dealer_hand.append(new_card)
        game_state['dealer_value'] = calculate_hand_value(dealer_hand)
        game_state['log'].append(f"Dealer hits and gets a {new_card['rank']}{new_card['suit']}.")
    
    if game_state['dealer_value'] > 21:
        game_state['log'].append(f"Dealer busts with {game_state['dealer_value']}!")
    else:
        game_state['log'].append(f"Dealer stands with {game_state['dealer_value']}.")
    
    return game_state


def _calculate_winners(game_state: dict):
    """Calculates winners and updates scores."""
    dealer_val = game_state['dealer_value']
    scores = game_state.get('scores', {})
    hand_wins = game_state.get('hand_wins', {})
    round_num = game_state.get('round_number', 1)
    
    winners = []
    losers = []
    ties = []
    round_winners = []
    
    for pid, p_state in game_state['hands'].items():
        player_val = p_state['value']
        
        if p_state['status'] == 'busted':
            losers.append((pid, player_val))
        elif dealer_val > 21:
            round_winners.append((pid, player_val))
            if player_val == 21:
                points = 30
            elif player_val >= 20:
                points = 20
            elif player_val >= 18:
                points = 15
            else:
                points = 10
            scores[pid] = scores.get(pid, 0) + points
            winners.append((pid, player_val, points))
        elif player_val > dealer_val:
            round_winners.append((pid, player_val))
            margin = player_val - dealer_val
            if player_val == 21:
                points = 25 + margin
            elif player_val >= 20:
                points = 15 + margin
            elif player_val >= 18:
                points = 10 + margin
            else:
                points = 5 + margin
            scores[pid] = scores.get(pid, 0) + points
            winners.append((pid, player_val, points))
        elif player_val < dealer_val:
            losers.append((pid, player_val))
        else:
            ties.append((pid, player_val))
            scores[pid] = scores.get(pid, 0) + 2
    
    game_state['scores'] = scores
    
    if round_winners:
        round_winners.sort(key=lambda x: (scores.get(x[0], 0) - game_state.get('previous_scores', {}).get(x[0], 0), x[1]), reverse=True)
        round_winner_id = round_winners[0][0]
        hand_wins[round_winner_id] = hand_wins.get(round_winner_id, 0) + 1
        game_state['hand_wins'] = hand_wins
        game_state['round_winner'] = round_winner_id
        
        wins_needed = game_state.get('wins_needed', 3)
        if hand_wins[round_winner_id] >= wins_needed:
            game_state['status'] = 'finished'
            game_state['winner'] = round_winner_id
            game_mode_str = "Best of 5" if wins_needed == 3 else "Best of 10"
            game_state['log'].append(f"🎊🎊🎊 {round_winner_id} WINS THE GAME ({game_mode_str})! 🎊🎊🎊")
        else:
            game_state['status'] = 'round_finished'
            game_state['round_winner'] = round_winner_id
            game_state['ready_for_next_round'] = {}
            game_state['log'].append(f"🏆 Round {round_num} complete! Player wins this round. (Wins: {hand_wins[round_winner_id]}/{wins_needed})")
    else:
        game_state['status'] = 'round_finished'
        game_state['round_winner'] = None
        game_state['ready_for_next_round'] = {}
        game_state['log'].append(f"😢 Round {round_num} complete! Dealer wins this round.")
    
    if winners:
        winner_msgs = []
        for pid, val, points in winners:
            hand_desc = "BLACKJACK!" if val == 21 else f"{val}"
            winner_msgs.append(f"{hand_desc} (+{points} pts)")
        if len(winner_msgs) == 1:
            game_state['log'].append(f"🎉 Player beats dealer with {winner_msgs[0]}")
        else:
            game_state['log'].append(f"🎉 Players beat dealer: {', '.join(winner_msgs)}")
    
    if losers:
        loser_count = len(losers)
        if loser_count == 1:
            game_state['log'].append(f"😢 Player busts with {losers[0][1]}")
        else:
            game_state['log'].append(f"😢 {loser_count} players bust")
    
    if ties:
        game_state['log'].append(f"🤝 {len(ties)} player(s) push with dealer")
    
    game_state['previous_scores'] = scores.copy()
    
    return game_state

