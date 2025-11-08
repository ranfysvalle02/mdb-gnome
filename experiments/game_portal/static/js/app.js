// Game Portal Frontend JavaScript
// Adapted for experiment structure

document.addEventListener('DOMContentLoaded', () => {
    // Get base path from current URL
    const currentPath = window.location.pathname;
    let basePath = '/experiments/game_portal';
    if (currentPath.includes('/experiments/game_portal')) {
        const pathParts = currentPath.split('/experiments/game_portal');
        basePath = pathParts[0] + '/experiments/game_portal';
    }
    
    // --- Browser Fingerprinting ---
    let browserFingerprint = null;
    
    async function generateFingerprint() {
        const stored = localStorage.getItem('browser_fingerprint');
        if (stored) {
            browserFingerprint = stored;
            return stored;
        }
        
        if (typeof FingerprintJS !== 'undefined') {
            try {
                const fp = await FingerprintJS.load();
                const result = await fp.get();
                browserFingerprint = result.visitorId;
                localStorage.setItem('browser_fingerprint', browserFingerprint);
                console.log('Browser Fingerprint (FingerprintJS):', browserFingerprint);
                return browserFingerprint;
            } catch (e) {
                console.warn('FingerprintJS failed, using fallback:', e);
            }
        }
        
        // Fallback: generate a simple fingerprint
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillText('Fingerprint', 2, 2);
        
        const fingerprint = [
            navigator.userAgent,
            navigator.language,
            screen.width + 'x' + screen.height,
            new Date().getTimezoneOffset(),
            canvas.toDataURL()
        ].join('|');
        
        let hash = 0;
        for (let i = 0; i < fingerprint.length; i++) {
            const char = fingerprint.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash;
        }
        browserFingerprint = 'fp_' + Math.abs(hash).toString(36);
        localStorage.setItem('browser_fingerprint', browserFingerprint);
        console.log('Browser Fingerprint (Fallback):', browserFingerprint);
        return browserFingerprint;
    }
    
    generateFingerprint().then(fp => {
        browserFingerprint = fp;
        updateUserIdentityDisplay();
    });
    
    function updateUserIdentityDisplay() {
        if (!browserFingerprint) return;
        
        const userIdentityDiv = document.getElementById('user-identity');
        const userIdenticonSvg = document.getElementById('user-identicon');
        const userDisplayName = document.getElementById('user-display-name');
        const userFingerprint = document.getElementById('user-fingerprint');
        
        if (userIdentityDiv) {
            userIdentityDiv.style.display = 'block';
        }
        
        if (userIdenticonSvg) {
            userIdenticonSvg.setAttribute('data-jdenticon-value', browserFingerprint);
        }
        
        if (userDisplayName) {
            userDisplayName.textContent = `Player ${browserFingerprint.substring(0, 8)}`;
        }
        
        if (userFingerprint) {
            userFingerprint.textContent = browserFingerprint;
        }
        
        setTimeout(() => {
            if (typeof jdenticon !== 'undefined' && userIdenticonSvg) {
                jdenticon.update(userIdenticonSvg);
            }
        }, 100);
    }
    
    // --- Initialize Floating Particles ---
    function initParticles() {
        const particlesContainer = document.getElementById('particles');
        if (!particlesContainer) return;
        
        for (let i = 0; i < 18; i++) {
            const particle = document.createElement('div');
            particle.className = 'particle';
            particlesContainer.appendChild(particle);
        }
    }
    
    document.addEventListener('mousemove', (e) => {
        const mouseX = e.clientX;
        const mouseY = e.clientY;
        
        let styleEl = document.getElementById('mouse-gradient-style');
        if (!styleEl) {
            styleEl = document.createElement('style');
            styleEl.id = 'mouse-gradient-style';
            document.head.appendChild(styleEl);
        }
        styleEl.textContent = `
            body::after {
                left: ${mouseX}px;
                top: ${mouseY}px;
            }
        `;
        
        document.body.classList.add('mouse-active');
    });
    
    document.addEventListener('mouseleave', () => {
        document.body.classList.remove('mouse-active');
    });
    
    initParticles();
    
    // --- Global State ---
    let socket = null;
    let localPlayerId = null;
    let localGameId = null;
    let localGameType = null;
    let currentGameState = null;
    let draggedTile = null;
    let disconnectTimeout = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 5;
    const DISCONNECT_DELAY_MS = 60000; // 60 seconds
    
    // --- UI Elements ---
    const lobbyView = document.getElementById('lobby-view');
    const gameView = document.getElementById('game-view');
    const gameTypeSelect = document.getElementById('game-type');
    const dominoModeSelect = document.getElementById('domino-mode-select');
    const dominoGameModeSelect = document.getElementById('domino-game-mode');
    const blackjackModeSelect = document.getElementById('blackjack-mode-select');
    const blackjackGameModeSelect = document.getElementById('blackjack-game-mode');
    const createGameBtn = document.getElementById('create-game-btn');
    const gameIdInput = document.getElementById('game-id-input');
    const joinGameBtn = document.getElementById('join-game-btn');
    const gameTitle = document.getElementById('game-title');
    const gameIdDisplay = document.getElementById('game-id-display');
    const playerIdDisplay = document.getElementById('player-id-display');
    const turnDisplay = document.getElementById('turn-display');
    const copyLinkBtn = document.getElementById('copy-link-btn');
    const startGameBtn = document.getElementById('start-game-btn');
    const playersList = document.getElementById('players-list');
    const blackjackUI = document.getElementById('blackjack-ui');
    const dominoesUI = document.getElementById('dominoes-ui');
    const dealerHandDiv = document.getElementById('dealer-hand');
    const dealerValueSpan = document.getElementById('dealer-value');
    const hitBtn = document.getElementById('hit-btn');
    const standBtn = document.getElementById('stand-btn');
    const dominoBoardDiv = document.getElementById('domino-board');
    const dominoEndsSpan = document.getElementById('domino-ends');
    const drawBtn = document.getElementById('draw-btn');
    const passBtn = document.getElementById('pass-btn');
    const playerHandDiv = document.getElementById('player-hand');
    const handValueSpan = document.getElementById('hand-value');
    const gameLogDiv = document.getElementById('game-log');
    
    // Show/hide game mode selector based on game type
    function updateGameModeSelector() {
        if (gameTypeSelect.value === 'dominoes') {
            dominoModeSelect.style.display = 'block';
            blackjackModeSelect.style.display = 'none';
        } else if (gameTypeSelect.value === 'blackjack') {
            dominoModeSelect.style.display = 'none';
            blackjackModeSelect.style.display = 'block';
        } else {
            dominoModeSelect.style.display = 'none';
            blackjackModeSelect.style.display = 'none';
        }
    }
    
    gameTypeSelect.addEventListener('change', updateGameModeSelector);
    updateGameModeSelector();
    
    // --- WebSocket Handlers ---
    function connectWebSocket(gameId, playerId) {
        localGameId = gameId;
        localPlayerId = playerId;
        
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProtocol}//${window.location.host}${basePath}/ws/game/${gameId}/${playerId}`;
        
        socket = new WebSocket(wsUrl);
        gameIdDisplay.textContent = gameId;
        
        const playerIdenticonSvg = document.getElementById('player-identicon-display');
        const playerDisplayName = document.getElementById('player-display-name');
        
        if (playerIdenticonSvg) {
            playerIdenticonSvg.setAttribute('data-jdenticon-value', playerId);
            setTimeout(() => {
                if (typeof jdenticon !== 'undefined') {
                    jdenticon.update(playerIdenticonSvg);
                }
            }, 100);
        }
        
        if (playerDisplayName) {
            playerDisplayName.textContent = `Player ${playerId.substring(0, 8)}`;
        }
        
        if (playerIdDisplay) {
            playerIdDisplay.textContent = playerId.substring(0, 12) + '...';
        }
        
        if (copyLinkBtn) {
            const shareUrl = `${window.location.origin}${basePath}?game=${gameId}`;
            copyLinkBtn.onclick = () => {
                navigator.clipboard.writeText(shareUrl).then(() => {
                    copyLinkBtn.textContent = '✓ Copied!';
                    setTimeout(() => {
                        copyLinkBtn.textContent = '📋 Copy Share Link';
                    }, 2000);
                }).catch(() => {
                    const textarea = document.createElement('textarea');
                    textarea.value = shareUrl;
                    document.body.appendChild(textarea);
                    textarea.select();
                    document.execCommand('copy');
                    document.body.removeChild(textarea);
                    copyLinkBtn.textContent = '✓ Copied!';
                    setTimeout(() => {
                        copyLinkBtn.textContent = '📋 Copy Share Link';
                    }, 2000);
                });
            };
        }
        
        socket.onopen = () => {
            console.log('WebSocket connected');
            lobbyView.classList.add('hidden');
            gameView.classList.remove('hidden');
            
            // Clear any disconnect timeout
            if (disconnectTimeout) {
                clearTimeout(disconnectTimeout);
                disconnectTimeout = null;
            }
            reconnectAttempts = 0;
            
            // Save connection state to localStorage
            if (localGameId && localPlayerId) {
                localStorage.setItem('game_portal_game_id', localGameId);
                localStorage.setItem('game_portal_player_id', localPlayerId);
                localStorage.setItem('game_portal_connected', 'true');
                localStorage.removeItem('game_portal_disconnect_time');
            }
        };
        
        socket.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log('Message from server:', data);
            
            switch (data.type) {
                case 'connection_success':
                    localGameType = data.game_type;
                    gameTitle.textContent = `${localGameType} Game`;
                    renderPlayers(data.players);
                    if (data.game_state) {
                        currentGameState = data.game_state;
                        renderGame(data.game_state);
                        
                        // Hide copy link button if game has started
                        if (data.game_state.status !== 'waiting') {
                            const gameInfoGrid = document.querySelector('div[style*="grid-template-columns"]');
                            if (gameInfoGrid && gameInfoGrid.firstElementChild) {
                                gameInfoGrid.firstElementChild.style.display = 'none';
                            }
                        }
                    }
                    break;
                case 'player_joined':
                case 'player_disconnected':
                case 'player_connected':
                    if (data.players) {
                        renderPlayers(data.players);
                    } else {
                        addLogMessage(`Player ${data.player_id ? data.player_id.substring(0, 8) : 'Unknown'} connected/disconnected.`);
                    }
                    break;
                case 'game_started':
                case 'state_update':
                    currentGameState = data.game_state;
                    if (data.players) {
                        renderPlayers(data.players);
                    }
                    renderGame(data.game_state);
                    
                    // Hide copy link button once game has started
                    if (data.game_state && data.game_state.status !== 'waiting') {
                        // Hide the entire card containing the copy link button (first card in the grid)
                        const gameInfoGrid = document.querySelector('div[style*="grid-template-columns"]');
                        if (gameInfoGrid && gameInfoGrid.firstElementChild) {
                            gameInfoGrid.firstElementChild.style.display = 'none';
                        }
                    }
                    if (data.game_state && data.game_state.board) {
                        const leftEndDisplay = document.getElementById('left-end-display');
                        const rightEndDisplay = document.getElementById('right-end-display');
                        if (data.game_state.board.length > 0) {
                            if (leftEndDisplay) leftEndDisplay.textContent = data.game_state.board[0][0];
                            if (rightEndDisplay) rightEndDisplay.textContent = data.game_state.board[data.game_state.board.length - 1][1];
                        } else {
                            if (leftEndDisplay) leftEndDisplay.textContent = '-';
                            if (rightEndDisplay) rightEndDisplay.textContent = '-';
                        }
                    }
                    break;
                case 'error':
                    alert(`Error: ${data.message}`);
                    break;
            }
        };
        
        socket.onclose = (event) => {
            console.log('WebSocket disconnected', event);
            
            // Save disconnect time to localStorage
            if (localGameId && localPlayerId) {
                localStorage.setItem('game_portal_disconnect_time', Date.now().toString());
                localStorage.setItem('game_portal_connected', 'false');
            }
            
            // Only show alert after 60s delay if game has started
            if (currentGameState && currentGameState.status !== 'waiting') {
                // Set a timeout to show alert after delay
                disconnectTimeout = setTimeout(() => {
                    if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                        // Try to reconnect
                        attemptReconnect();
                    } else {
                        alert('Connection lost. Please refresh the page.');
                        lobbyView.classList.remove('hidden');
                        gameView.classList.add('hidden');
                        // Clear localStorage
                        localStorage.removeItem('game_portal_game_id');
                        localStorage.removeItem('game_portal_player_id');
                        localStorage.removeItem('game_portal_connected');
                        localStorage.removeItem('game_portal_disconnect_time');
                    }
                }, DISCONNECT_DELAY_MS);
            } else {
                // If game hasn't started, show alert immediately
                alert('Connection lost. Please refresh.');
                lobbyView.classList.remove('hidden');
                gameView.classList.add('hidden');
            }
        };
        
        socket.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    }
    
    // --- API Call Functions ---
    async function createGame() {
        const gameType = gameTypeSelect.value;
        let gameMode = 'classic';
        if (gameType === 'dominoes') {
            gameMode = dominoGameModeSelect.value;
        } else if (gameType === 'blackjack') {
            gameMode = blackjackGameModeSelect.value;
        }
        
        if (!browserFingerprint) {
            browserFingerprint = await generateFingerprint();
        }
        
        try {
            const response = await fetch(`${basePath}/api/game/create`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ player_id: browserFingerprint, game_type: gameType, game_mode: gameMode })
            });
            const data = await response.json();
            if (response.ok) {
                const shareUrl = `${window.location.origin}${basePath}?game=${data.game_id}`;
                window.history.pushState({ gameId: data.game_id }, '', shareUrl);
                connectWebSocket(data.game_id, data.player_id);
            } else {
                alert(`Error: ${data.detail || data.error || 'Unknown error'}`);
            }
        } catch (err) {
            console.error('Create Game failed:', err);
            alert('Failed to create game. Please try again.');
        }
    }
    
    async function joinGame() {
        const gameId = gameIdInput.value.trim().toUpperCase();
        if (!gameId) { 
            alert('Please enter a Game ID.'); 
            return; 
        }
        
        if (!browserFingerprint) {
            browserFingerprint = await generateFingerprint();
        }
        
        try {
            const response = await fetch(`${basePath}/api/game/${gameId}/join`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ player_id: browserFingerprint })
            });
            const data = await response.json();
            if (response.ok) {
                const shareUrl = `${window.location.origin}${basePath}?game=${data.game_id}`;
                window.history.pushState({ gameId: data.game_id }, '', shareUrl);
                connectWebSocket(data.game_id, data.player_id);
            } else {
                alert(`Error: ${data.detail || data.error || 'Unknown error'}`);
            }
        } catch (err) {
            console.error('Join Game failed:', err);
            alert('Failed to join game. Please try again.');
        }
    }
    
    // --- Send WebSocket Actions ---
    function sendStartGame() {
        if (socket) socket.send(JSON.stringify({ type: 'start_game' }));
    }
    
    function sendMove(moveData) {
        if (socket) {
            if (moveData && moveData.action === 'ready_for_next_hand') {
                socket.send(JSON.stringify({ type: 'ready_for_next_hand' }));
            } else if (moveData && moveData.action === 'ready_for_next_round') {
                socket.send(JSON.stringify({ type: 'ready_for_next_round' }));
            } else {
                socket.send(JSON.stringify({ type: 'make_move', move_data: moveData }));
            }
        }
    }
    
    // --- Render Functions ---
    function renderGame(state) {
        const playerHandTitle = document.getElementById('your-hand-title');
        const playerHandDiv = document.getElementById('player-hand');
        const gameLogTitle = document.getElementById('game-log-title');
        const gameLogDiv = document.getElementById('game-log');
        const turnDisplayCard = document.getElementById('turn-display-card');
        
        if (!state) {
            startGameBtn.classList.remove('hidden');
            blackjackUI.classList.add('hidden');
            dominoesUI.classList.add('hidden');
            if (playerHandTitle) playerHandTitle.style.display = 'none';
            if (playerHandDiv) playerHandDiv.style.display = 'none';
            if (gameLogTitle) gameLogTitle.style.display = 'none';
            if (gameLogDiv) gameLogDiv.style.display = 'none';
            if (turnDisplayCard) turnDisplayCard.style.display = 'none';
            if (playerHandDiv) playerHandDiv.innerHTML = '';
            if (gameLogDiv) gameLogDiv.innerHTML = '';
            return;
        }
        
        startGameBtn.classList.add('hidden');
        if (playerHandTitle) playerHandTitle.style.display = 'block';
        if (playerHandDiv) playerHandDiv.style.display = 'flex';
        if (gameLogTitle) gameLogTitle.style.display = 'block';
        if (gameLogDiv) gameLogDiv.style.display = 'block';
        if (turnDisplayCard) turnDisplayCard.style.display = 'block';
        
        renderLog(state.log);
        
        blackjackUI.classList.add('hidden');
        dominoesUI.classList.add('hidden');
        
        const myTurn = state.players[state.current_turn_index] === localPlayerId;
        const currentPlayerId = state.players[state.current_turn_index];
        
        if (myTurn) {
            turnDisplay.textContent = "YOUR TURN";
        } else {
            let playerName = currentPlayerId;
            const playerCards = playersList.querySelectorAll('div > div');
            playerCards.forEach(card => {
                const svg = card.parentElement.querySelector('svg');
                if (svg && svg.getAttribute('data-jdenticon-value') === currentPlayerId) {
                    const nameEl = card.querySelector('div');
                    if (nameEl) {
                        playerName = nameEl.textContent.split('🤖')[0].trim();
                    }
                }
            });
            turnDisplay.textContent = `${playerName}'s Turn`;
        }
        
        updateThinkingIndicators(state);
        
        if (localGameType === 'blackjack') {
            renderBlackjack(state, myTurn);
        } else if (localGameType === 'dominoes') {
            renderDominoes(state, myTurn);
        }
    }
    
    function updateThinkingIndicators(state) {
        if (!state || !state.players || state.status === 'finished' || state.status === 'hand_finished') {
            const allThinkingSpans = playersList.querySelectorAll('.ai-thinking');
            allThinkingSpans.forEach(span => span.remove());
            return;
        }
        
        if (state.status !== 'in_progress') return;
        
        const currentPlayerId = state.players[state.current_turn_index];
        const playerCards = playersList.querySelectorAll('div[style*="display: inline-flex"]');
        
        playerCards.forEach(card => {
            const svg = card.querySelector('svg');
            if (!svg) return;
            
            const playerId = svg.getAttribute('data-jdenticon-value');
            const isAI = card.textContent.includes('🤖 AI');
            const isCurrentTurn = playerId === currentPlayerId;
            const thinkingSpan = card.querySelector('.ai-thinking');
            
            if (isAI && isCurrentTurn && !thinkingSpan) {
                const thinkingEl = document.createElement('span');
                thinkingEl.className = 'ai-thinking';
                thinkingEl.style.cssText = 'margin-left: 0.5rem; color: #667eea; font-size: 0.875rem; font-style: italic; animation: pulse 1.5s ease-in-out infinite;';
                thinkingEl.textContent = 'thinking';
                const nameDiv = card.querySelector('div > div');
                if (nameDiv && nameDiv.parentElement) {
                    nameDiv.parentElement.appendChild(thinkingEl);
                }
            } else if (thinkingSpan && !isCurrentTurn) {
                thinkingSpan.remove();
            }
        });
    }
    
    function renderPlayers(players) {
        playersList.innerHTML = '<strong>Players:</strong> ';
        players.forEach(p => {
            const isAI = p.isAI || false;
            const playerId = p.player_id || p.playerId || 'unknown';
            const displayName = isAI ? `AutoBot` : `Player ${playerId.substring(0, 8)}`;
            const aiBadge = isAI ? '<span style="background: linear-gradient(135deg, #f6ad55 0%, #ed8936 100%); color: white; padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.75rem; font-weight: 700; margin-left: 0.5rem;">🤖 AI</span>' : '';
            const thinkingIndicator = isAI && currentGameState && currentGameState.status === 'in_progress' && currentGameState.players && currentGameState.players[currentGameState.current_turn_index] === playerId 
                ? '<span class="ai-thinking" style="margin-left: 0.5rem; color: #667eea; font-size: 0.875rem; font-style: italic; animation: pulse 1.5s ease-in-out infinite;">thinking...</span>' : '';
            
            playersList.innerHTML += `
                <div style="display: inline-flex; align-items: center; gap: 0.75rem; margin: 0.5rem 1rem 0.5rem 0; padding: 0.75rem 1rem; background: white; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); transition: all 0.3s ease;">
                    <svg width="40" height="40" data-jdenticon-value="${playerId}" style="border-radius: 50%; background: #f7fafc; padding: 4px; flex-shrink: 0;"></svg>
                    <div style="flex: 1;">
                        <div style="font-weight: 600; color: #2d3748; display: flex; align-items: center; gap: 0.5rem;">${displayName}${aiBadge}</div>
                        <div style="font-size: 0.75rem; color: #718096;">${playerId.substring(0, 12)}...</div>
                        ${thinkingIndicator}
                    </div>
                </div>
            `;
        });
        
        setTimeout(() => {
            if (typeof jdenticon !== 'undefined') {
                jdenticon();
            }
        }, 100);
    }
    
    function renderLog(logEntries) {
        if (!logEntries || logEntries.length === 0) return;
        
        gameLogDiv.innerHTML = '';
        
        const cleanedEntries = [];
        const seenMessages = new Set();
        
        logEntries.forEach(entry => {
            if (entry.includes('LEADERBOARD') || entry.includes('Round Wins:')) {
                return;
            }
            
            if (entry.includes('Round Wins:') && entry.includes(',')) {
                return;
            }
            
            let cleanedEntry = entry;
            if (currentGameState && currentGameState.players) {
                currentGameState.players.forEach(pid => {
                    const displayName = getPlayerDisplayName(pid);
                    cleanedEntry = cleanedEntry.replace(new RegExp(pid, 'g'), displayName);
                });
            }
            
            if (!seenMessages.has(cleanedEntry)) {
                seenMessages.add(cleanedEntry);
                cleanedEntries.push(cleanedEntry);
            }
        });
        
        cleanedEntries.slice().reverse().forEach(entry => {
            let className = '';
            let isExciting = false;
            
            if (entry.includes('WINS THE GAME')) {
                className = 'victory-message';
                isExciting = true;
            } else if (entry.includes('WINS ROUND') || (entry.includes('Round') && entry.includes('complete'))) {
                className = 'round-complete-message';
                isExciting = true;
            } else if (entry.includes('beats dealer') || entry.includes('🎉')) {
                className = 'win-message';
                isExciting = true;
            } else if (entry.includes('loses') || entry.includes('busts') || entry.includes('😢')) {
                className = 'loss-message';
                isExciting = true;
            } else if (entry.includes('Round') && entry.includes('started')) {
                className = 'round-start-message';
                isExciting = true;
            }
            
            if (isExciting) {
                gameLogDiv.innerHTML += `<div class="${className}">${entry}</div>`;
            } else {
                gameLogDiv.innerHTML += `<p style="color: #718096; font-size: 0.9rem; margin: 0.25rem 0;">${entry}</p>`;
            }
        });
    }
    
    function addLogMessage(message) {
        gameLogDiv.innerHTML = `<p>${message}</p>` + gameLogDiv.innerHTML;
    }
    
    // --- Helper: Create a beautiful playing card element ---
    function createPlayingCard(card, isHidden = false) {
        if (isHidden) {
            const cardDiv = document.createElement('div');
            cardDiv.className = 'card hidden-card';
            return cardDiv;
        }
        
        const suit = card.suit || '';
        const rank = card.rank;
        const isRed = suit === '♥' || suit === '♦';
        
        const suitMap = {
            '♥': '♥',
            '♦': '♦',
            '♣': '♣',
            '♠': '♠'
        };
        const suitSymbol = suitMap[suit] || suit;
        
        const cardDiv = document.createElement('div');
        cardDiv.className = `card ${isRed ? 'red' : 'black'}`;
        
        const topLeftCorner = document.createElement('div');
        topLeftCorner.className = 'card-corner card-corner-top';
        const rankTop = document.createElement('span');
        rankTop.className = 'card-rank';
        rankTop.textContent = rank;
        const suitTop = document.createElement('span');
        suitTop.className = 'card-suit';
        suitTop.textContent = suitSymbol;
        topLeftCorner.appendChild(rankTop);
        topLeftCorner.appendChild(suitTop);
        cardDiv.appendChild(topLeftCorner);
        
        const centerSymbol = document.createElement('div');
        centerSymbol.className = 'card-center';
        centerSymbol.textContent = suitSymbol;
        cardDiv.appendChild(centerSymbol);
        
        const bottomRightCorner = document.createElement('div');
        bottomRightCorner.className = 'card-corner card-corner-bottom';
        const rankBottom = document.createElement('span');
        rankBottom.className = 'card-rank';
        rankBottom.textContent = rank;
        const suitBottom = document.createElement('span');
        suitBottom.className = 'card-suit';
        suitBottom.textContent = suitSymbol;
        bottomRightCorner.appendChild(rankBottom);
        bottomRightCorner.appendChild(suitBottom);
        cardDiv.appendChild(bottomRightCorner);
        
        return cardDiv;
    }
    
    // --- Blackjack Specific Render ---
    function renderBlackjack(state, myTurn) {
        blackjackUI.classList.remove('hidden');
        
        if (dealerHandDiv) dealerHandDiv.style.display = 'flex';
        if (playerHandDiv) playerHandDiv.style.display = 'flex';
        
        const nextRoundContainer = document.getElementById('next-round-container');
        const nextRoundBtn = document.getElementById('next-round-btn');
        const roundReadyStatus = document.getElementById('round-ready-status');
        
        if (state.status === 'round_finished') {
            if (nextRoundContainer) {
                nextRoundContainer.classList.remove('hidden');
                nextRoundContainer.style.display = 'block';
                
                setTimeout(() => {
                    nextRoundContainer.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }, 300);
            }
            
            const readyPlayers = state.ready_for_next_round || {};
            const totalPlayers = state.players ? state.players.length : 0;
            const readyCount = Object.keys(readyPlayers).length;
            const isReady = readyPlayers[localPlayerId] || false;
            const roundNum = state.round_number || 1;
            const winsNeeded = state.wins_needed || 3;
            
            if (roundReadyStatus) {
                if (readyCount >= totalPlayers) {
                    roundReadyStatus.textContent = '🎉 All players ready! Starting next round...';
                    roundReadyStatus.style.color = '#c6f6d5';
                    roundReadyStatus.style.fontWeight = '700';
                } else {
                    const remaining = totalPlayers - readyCount;
                    roundReadyStatus.textContent = `Waiting for ${remaining} more player${remaining !== 1 ? 's' : ''}... (${readyCount}/${totalPlayers} ready)`;
                    roundReadyStatus.style.color = 'rgba(255, 255, 255, 0.9)';
                    roundReadyStatus.style.fontWeight = '600';
                }
            }
            
            if (nextRoundBtn) {
                if (isReady) {
                    nextRoundBtn.disabled = true;
                    nextRoundBtn.textContent = '✓ Ready!';
                    nextRoundBtn.style.opacity = '0.7';
                    nextRoundBtn.style.cursor = 'not-allowed';
                } else {
                    nextRoundBtn.disabled = false;
                    const nextRoundNum = roundNum + 1;
                    nextRoundBtn.textContent = `▶ Start Round ${nextRoundNum}`;
                    nextRoundBtn.style.opacity = '1';
                    nextRoundBtn.style.cursor = 'pointer';
                    nextRoundBtn.style.animation = 'pulse 2s ease-in-out infinite';
                }
            }
        } else {
            if (nextRoundContainer) {
                nextRoundContainer.classList.add('hidden');
                nextRoundContainer.style.display = 'none';
            }
        }
        
        dealerValueSpan.textContent = state.dealer_value;
        dealerHandDiv.innerHTML = '';
        if (state.dealer_hand) {
            state.dealer_hand.forEach((card, index) => {
                const isHidden = state.status === 'in_progress' && index === 1 && card.rank === '?';
                const cardElement = createPlayingCard(card, isHidden);
                dealerHandDiv.appendChild(cardElement);
            });
        }
        
        const myHandData = state.hands && state.hands[localPlayerId];
        if (myHandData) {
            handValueSpan.textContent = `Value: ${myHandData.value}`;
            playerHandDiv.innerHTML = '';
            myHandData.hand.forEach(card => {
                const cardElement = createPlayingCard(card, false);
                playerHandDiv.appendChild(cardElement);
            });
        }
        
        if (state.scores) {
            let scoresDiv = document.getElementById('blackjack-scores');
            if (!scoresDiv) {
                scoresDiv = document.createElement('div');
                scoresDiv.id = 'blackjack-scores';
                scoresDiv.className = 'score-display';
                blackjackUI.insertBefore(scoresDiv, blackjackUI.firstChild);
            }
            const sortedScores = Object.entries(state.scores).sort((a, b) => b[1] - a[1]);
            const handWins = state.hand_wins || {};
            const roundNum = state.round_number || 1;
            const winsNeeded = state.wins_needed || 3;
            
            let scoresHtml = `<h4>📊 Round ${roundNum} Leaderboard</h4><div class="leaderboard">`;
            sortedScores.forEach(([pid, score], index) => {
                const medal = index === 0 ? '🥇' : index === 1 ? '🥈' : index === 2 ? '🥉' : '  ';
                const isWinner = index === 0;
                const wins = handWins[pid] || 0;
                const playerName = getPlayerDisplayName(pid);
                scoresHtml += `<div class="leaderboard-item ${isWinner ? 'winner' : ''}">${medal} ${playerName}: ${score} pts (${wins}/${winsNeeded} wins)</div>`;
            });
            scoresHtml += '</div>';
            scoresDiv.innerHTML = scoresHtml;
        }
        
        const actions = document.getElementById('blackjack-actions');
        if (myHandData && myTurn && myHandData.status === 'playing' && state.status === 'in_progress') {
            actions.classList.remove('hidden');
        } else {
            actions.classList.add('hidden');
        }
    }
    
    function getPlayerDisplayName(playerId) {
        if (!playerId) return 'Unknown';
        if (playerId === localPlayerId) return 'You';
        
        const playerCards = playersList.querySelectorAll('div[style*="display: inline-flex"]');
        for (const card of playerCards) {
            const svg = card.querySelector('svg');
            if (svg && svg.getAttribute('data-jdenticon-value') === playerId) {
                if (card.textContent.includes('🤖 AI')) {
                    return 'AutoBot';
                }
                const nameDiv = card.querySelector('div > div');
                if (nameDiv) {
                    return nameDiv.textContent.split('🤖')[0].trim();
                }
            }
        }
        
        return `Player ${playerId.substring(0, 8)}`;
    }
    
    // --- Helper: Create a domino tile element ---
    function createDominoTile(value1, value2, tileData = null) {
        const domino = document.createElement('div');
        domino.className = 'domino';
        
        const half1 = document.createElement('div');
        half1.className = `half half-${value1}`;
        for (let i = 0; i < value1; i++) {
            const pip = document.createElement('span');
            pip.className = 'pip';
            half1.appendChild(pip);
        }
        
        const half2 = document.createElement('div');
        half2.className = `half half-${value2}`;
        for (let i = 0; i < value2; i++) {
            const pip = document.createElement('span');
            pip.className = 'pip';
            half2.appendChild(pip);
        }
        
        domino.appendChild(half1);
        domino.appendChild(half2);
        
        if (tileData) {
            domino.dataset.tile = JSON.stringify(tileData);
        }
        
        return domino;
    }
    
    function renderBoardMap(board) {
        const boardMapDiv = document.getElementById('board-map');
        boardMapDiv.innerHTML = '';
        
        if (board.length === 0) {
            const emptyMsg = document.createElement('div');
            emptyMsg.style.cssText = 'text-align: center; color: rgba(255,255,255,0.6); font-style: italic; padding: 1rem;';
            emptyMsg.textContent = 'No tiles on board';
            boardMapDiv.appendChild(emptyMsg);
            return;
        }
        
        board.forEach((tile, index) => {
            const item = document.createElement('div');
            item.className = 'board-map-item';
            item.innerHTML = `
                <span>#${index + 1}</span>
                <span>[${tile[0]}|${tile[1]}]</span>
            `;
            boardMapDiv.appendChild(item);
        });
    }
    
    // --- Dominoes Specific Render ---
    function renderDominoes(state, myTurn) {
        dominoesUI.classList.remove('hidden');
        
        const nextHandContainer = document.getElementById('next-hand-container');
        const nextHandBtn = document.getElementById('next-hand-btn');
        const readyStatusSpan = document.getElementById('ready-status');
        
        if (state.status === 'hand_finished') {
            if (nextHandContainer) nextHandContainer.classList.remove('hidden');
            if (nextHandContainer) nextHandContainer.style.display = 'block';
            
            const readyPlayers = state.ready_for_next_hand || {};
            const totalPlayers = state.players ? state.players.length : 0;
            const readyCount = Object.keys(readyPlayers).length;
            const isReady = readyPlayers[localPlayerId] || false;
            
            if (readyStatusSpan) {
                if (readyCount >= totalPlayers) {
                    readyStatusSpan.textContent = '🎉 All players ready! Starting next hand...';
                    readyStatusSpan.style.color = '#c6f6d5';
                } else {
                    readyStatusSpan.textContent = `Waiting for players... (${readyCount}/${totalPlayers} ready)`;
                    readyStatusSpan.style.color = 'rgba(255, 255, 255, 0.9)';
                }
            }
            
            if (nextHandBtn) {
                if (isReady) {
                    nextHandBtn.disabled = true;
                    nextHandBtn.textContent = '✓ Ready!';
                    nextHandBtn.style.opacity = '0.7';
                } else {
                    nextHandBtn.disabled = false;
                    nextHandBtn.textContent = '▶ Next Hand';
                    nextHandBtn.style.opacity = '1';
                }
            }
        } else {
            if (nextHandContainer) nextHandContainer.classList.add('hidden');
            if (nextHandContainer) nextHandContainer.style.display = 'none';
        }
        
        if (state.game_mode === 'boricua' && state.team_scores) {
            let scoresDiv = document.getElementById('domino-scores');
            if (!scoresDiv) {
                scoresDiv = document.createElement('div');
                scoresDiv.id = 'domino-scores';
                scoresDiv.className = 'score-display';
                scoresDiv.style.marginBottom = '1rem';
                dominoesUI.insertBefore(scoresDiv, dominoesUI.firstChild);
            }
            const teams = state.teams;
            const teamScores = state.team_scores;
            scoresDiv.innerHTML = `
                <h4>📊 Team Scores (First to 500):</h4>
                <div class="leaderboard">
                    <div class="leaderboard-item ${teamScores.team1 >= teamScores.team2 ? 'winner' : ''}">
                        Team 1 (${teams.team1.join(', ')}): ${teamScores.team1} points
                    </div>
                    <div class="leaderboard-item ${teamScores.team2 >= teamScores.team1 ? 'winner' : ''}">
                        Team 2 (${teams.team2.join(', ')}): ${teamScores.team2} points
                    </div>
                </div>
                <p style="margin-top: 0.5rem; font-size: 0.9rem;">Hand #${state.hand_number}</p>
            `;
        } else if (state.game_mode === 'classic' && state.hand_wins) {
            let scoresDiv = document.getElementById('domino-scores');
            if (!scoresDiv) {
                scoresDiv = document.createElement('div');
                scoresDiv.id = 'domino-scores';
                scoresDiv.className = 'score-display';
                scoresDiv.style.marginBottom = '1rem';
                dominoesUI.insertBefore(scoresDiv, dominoesUI.firstChild);
            }
            const sortedWins = Object.entries(state.hand_wins).sort((a, b) => b[1] - a[1]);
            let scoresHtml = '<h4>📊 Hand Wins (Best of 5):</h4><div class="leaderboard">';
            sortedWins.forEach(([pid, wins], index) => {
                const isWinner = wins >= 3;
                scoresHtml += `<div class="leaderboard-item ${isWinner ? 'winner' : ''}">${pid}: ${wins} wins</div>`;
            });
            scoresHtml += `</div><p style="margin-top: 0.5rem; font-size: 0.9rem;">Hand #${state.hand_number}</p>`;
            scoresDiv.innerHTML = scoresHtml;
        }
        
        handValueSpan.textContent = '';
        
        dominoBoardDiv.innerHTML = '';
        if (state.board.length === 0) {
            const emptyMsg = document.createElement('div');
            emptyMsg.style.cssText = 'text-align: center; color: rgba(255,255,255,0.7); font-style: italic; padding: 1rem;';
            emptyMsg.textContent = 'No tiles on the board yet. Play the first tile!';
            dominoBoardDiv.appendChild(emptyMsg);
            dominoEndsSpan.textContent = 'Empty';
        } else {
            state.board.forEach((tile, index) => {
                const tileSpan = document.createElement('span');
                tileSpan.style.cssText = 'display: inline-block; padding: 0.5rem; background: rgba(255,255,255,0.9); border-radius: 4px; margin: 0.2rem; font-family: monospace; font-weight: bold;';
                tileSpan.textContent = `[${tile[0]}|${tile[1]}]`;
                dominoBoardDiv.appendChild(tileSpan);
            });
            
            const leftEnd = state.board[0][0];
            const rightEnd = state.board[state.board.length - 1][1];
            dominoEndsSpan.textContent = `Left: ${leftEnd} | Right: ${rightEnd}`;
        }
        
        const leftEndDisplay = document.getElementById('left-end-display');
        const rightEndDisplay = document.getElementById('right-end-display');
        if (state.board.length > 0) {
            leftEndDisplay.textContent = state.board[0][0];
            rightEndDisplay.textContent = state.board[state.board.length - 1][1];
        } else {
            leftEndDisplay.textContent = '-';
            rightEndDisplay.textContent = '-';
        }
        
        renderBoardMap(state.board);
        setupGlobalDragAndDrop();
        
        const myHand = state.hands[localPlayerId];
        let playableTiles = [];
        let hasPlayableTile = false;
        
        if (Array.isArray(myHand)) {
            if (state.board.length === 0) {
                playableTiles = myHand.map(t => JSON.stringify(t));
                hasPlayableTile = myHand.length > 0;
            } else {
                const leftEnd = state.board[0][0];
                const rightEnd = state.board[state.board.length - 1][1];
                
                playableTiles = myHand.filter(tile => {
                    return tile[0] === leftEnd || tile[1] === leftEnd || 
                           tile[0] === rightEnd || tile[1] === rightEnd;
                }).map(t => JSON.stringify(t));
                hasPlayableTile = playableTiles.length > 0;
            }
        }
        
        playerHandDiv.innerHTML = '';
        if (Array.isArray(myHand)) {
            myHand.forEach(tile => {
                const tileKey = JSON.stringify(tile);
                const isPlayable = playableTiles.includes(tileKey);
                
                const domino = createDominoTile(tile[0], tile[1], tile);
                
                if (myTurn && state.status === 'in_progress') {
                    if (isPlayable) {
                        domino.classList.add('playable', 'draggable');
                        domino.draggable = true;
                        
                        domino.addEventListener('dragstart', (e) => {
                            e.dataTransfer.effectAllowed = 'move';
                            e.dataTransfer.setData('text/plain', JSON.stringify(tile));
                            draggedTile = tile;
                            domino.classList.add('dragging');
                        });
                        
                        domino.addEventListener('dragend', (e) => {
                            domino.classList.remove('dragging');
                            document.getElementById('drop-zone-left')?.classList.remove('drag-over');
                            document.getElementById('drop-zone-right')?.classList.remove('drag-over');
                            draggedTile = null;
                        });
                        
                        domino.onclick = () => onTileClick(tile, state.board);
                    } else {
                        domino.classList.add('unplayable');
                        domino.title = 'This tile cannot be played';
                        domino.draggable = false;
                    }
                } else {
                    domino.classList.add('disabled');
                    domino.draggable = false;
                }
                
                playerHandDiv.appendChild(domino);
            });
        }
        
        const actions = document.getElementById('domino-actions');
        const drawBtn = document.getElementById('draw-btn');
        const passBtn = document.getElementById('pass-btn');
        
        if (myTurn && state.status === 'in_progress') {
            actions.classList.remove('hidden');
            
            let boneyardCount = 0;
            if (state.boneyard_count !== undefined) {
                boneyardCount = state.boneyard_count;
            } else if (typeof state.boneyard === 'string') {
                const match = state.boneyard.match(/(\d+)/);
                boneyardCount = match ? parseInt(match[1]) : 0;
            } else if (Array.isArray(state.boneyard)) {
                boneyardCount = state.boneyard.length;
            }
            
            if (boneyardCount > 0) {
                drawBtn.disabled = false;
                drawBtn.title = 'Draw a tile from the boneyard';
            } else {
                drawBtn.disabled = true;
                drawBtn.title = 'Boneyard is empty';
            }
            
            if (!hasPlayableTile && boneyardCount === 0) {
                passBtn.disabled = false;
                passBtn.title = 'No playable tiles and boneyard is empty';
            } else if (!hasPlayableTile && boneyardCount > 0) {
                passBtn.disabled = true;
                passBtn.title = 'You must draw from the boneyard first';
            } else {
                passBtn.disabled = true;
                passBtn.title = 'You have playable tiles';
            }
            
            if (!hasPlayableTile) {
                if (boneyardCount > 0) {
                    handValueSpan.textContent = '⚠️ No playable tiles - Click "Draw" to get a new tile';
                    handValueSpan.style.color = '#ff9800';
                } else {
                    handValueSpan.textContent = '⚠️ No playable tiles - Click "Pass" to skip your turn';
                    handValueSpan.style.color = '#ff9800';
                }
            } else {
                handValueSpan.textContent = `✓ ${playableTiles.length} playable tile(s) - Click a highlighted tile to play`;
                handValueSpan.style.color = '#4caf50';
            }
        } else {
            actions.classList.add('hidden');
            handValueSpan.textContent = '';
        }
    }
    
    function onTileClick(tile, board) {
        let side = 'right';
        if (board.length > 0) {
            const leftEnd = board[0][0];
            const rightEnd = board[board.length - 1][1];
            
            const canPlayLeft = tile[0] === leftEnd || tile[1] === leftEnd;
            const canPlayRight = tile[0] === rightEnd || tile[1] === rightEnd;
            
            if (canPlayLeft && canPlayRight && leftEnd !== rightEnd) {
                side = prompt(`Play on (l)eft or (r)ight?`, 'r');
                if (side === 'l') side = 'left';
                else side = 'right';
            } else if (canPlayLeft) {
                side = 'left';
            } else {
                side = 'right';
            }
        }
        sendMove({ action: 'play', tile: tile, side: side });
    }
    
    // --- Setup Global Drag and Drop Handlers ---
    function setupGlobalDragAndDrop() {
        const leftDropZone = document.getElementById('drop-zone-left');
        const rightDropZone = document.getElementById('drop-zone-right');
        
        if (!leftDropZone || !rightDropZone) return;
        
        const newLeftDropZone = leftDropZone.cloneNode(true);
        const newRightDropZone = rightDropZone.cloneNode(true);
        leftDropZone.parentNode.replaceChild(newLeftDropZone, leftDropZone);
        rightDropZone.parentNode.replaceChild(newRightDropZone, rightDropZone);
        
        newLeftDropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            
            if (!draggedTile) return;
            
            const board = currentGameState?.board || [];
            
            if (board.length > 0) {
                const leftEnd = board[0][0];
                const canPlayLeft = draggedTile[0] === leftEnd || draggedTile[1] === leftEnd;
                if (canPlayLeft) {
                    newLeftDropZone.classList.add('drag-over');
                } else {
                    newLeftDropZone.classList.remove('drag-over');
                }
            } else {
                newLeftDropZone.classList.add('drag-over');
            }
        });
        
        newLeftDropZone.addEventListener('dragleave', (e) => {
            newLeftDropZone.classList.remove('drag-over');
        });
        
        newLeftDropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            newLeftDropZone.classList.remove('drag-over');
            
            if (!draggedTile) return;
            
            try {
                const tile = draggedTile;
                const board = currentGameState?.board || [];
                
                if (board.length > 0) {
                    const leftEnd = board[0][0];
                    const canPlayLeft = tile[0] === leftEnd || tile[1] === leftEnd;
                    if (canPlayLeft) {
                        sendMove({ action: 'play', tile: tile, side: 'left' });
                    }
                } else {
                    sendMove({ action: 'play', tile: tile, side: 'left' });
                }
            } catch (err) {
                console.error('Error handling drop:', err);
            }
            
            draggedTile = null;
        });
        
        newRightDropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            
            if (!draggedTile) return;
            
            const board = currentGameState?.board || [];
            
            if (board.length > 0) {
                const rightEnd = board[board.length - 1][1];
                const canPlayRight = draggedTile[0] === rightEnd || draggedTile[1] === rightEnd;
                if (canPlayRight) {
                    newRightDropZone.classList.add('drag-over');
                } else {
                    newRightDropZone.classList.remove('drag-over');
                }
            } else {
                newRightDropZone.classList.add('drag-over');
            }
        });
        
        newRightDropZone.addEventListener('dragleave', (e) => {
            newRightDropZone.classList.remove('drag-over');
        });
        
        newRightDropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            newRightDropZone.classList.remove('drag-over');
            
            if (!draggedTile) return;
            
            try {
                const tile = draggedTile;
                const board = currentGameState?.board || [];
                
                if (board.length > 0) {
                    const rightEnd = board[board.length - 1][1];
                    const canPlayRight = tile[0] === rightEnd || tile[1] === rightEnd;
                    if (canPlayRight) {
                        sendMove({ action: 'play', tile: tile, side: 'right' });
                    }
                } else {
                    sendMove({ action: 'play', tile: tile, side: 'right' });
                }
            } catch (err) {
                console.error('Error handling drop:', err);
            }
            
            draggedTile = null;
        });
    }
    
    // --- Reconnection Logic ---
    async function attemptReconnect() {
        if (!localGameId || !localPlayerId) {
            return;
        }
        
        reconnectAttempts++;
        console.log(`Attempting to reconnect (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);
        
        try {
            // Try to reconnect WebSocket
            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${wsProtocol}//${window.location.host}${basePath}/ws/game/${localGameId}/${localPlayerId}`;
            
            socket = new WebSocket(wsUrl);
            
            socket.onopen = () => {
                console.log('Reconnected successfully!');
                reconnectAttempts = 0;
                if (disconnectTimeout) {
                    clearTimeout(disconnectTimeout);
                    disconnectTimeout = null;
                }
                localStorage.setItem('game_portal_connected', 'true');
                localStorage.removeItem('game_portal_disconnect_time');
            };
            
            socket.onmessage = (event) => {
                const data = JSON.parse(event.data);
                console.log('Message from server:', data);
                
                switch (data.type) {
                    case 'connection_success':
                        localGameType = data.game_type;
                        gameTitle.textContent = `${localGameType} Game`;
                        renderPlayers(data.players);
                        if (data.game_state) {
                            currentGameState = data.game_state;
                            renderGame(data.game_state);
                        }
                        break;
                    case 'game_started':
                    case 'state_update':
                        currentGameState = data.game_state;
                        if (data.players) {
                            renderPlayers(data.players);
                        }
                        renderGame(data.game_state);
                        if (data.game_state && data.game_state.status !== 'waiting') {
                            const gameInfoGrid = document.querySelector('div[style*="grid-template-columns"]');
                            if (gameInfoGrid && gameInfoGrid.firstElementChild) {
                                gameInfoGrid.firstElementChild.style.display = 'none';
                            }
                        }
                        break;
                    case 'error':
                        console.error('Error:', data.message);
                        break;
                }
            };
            
            socket.onclose = (event) => {
                console.log('Reconnection failed', event);
                if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                    // Retry after a delay
                    setTimeout(() => {
                        attemptReconnect();
                    }, 2000 * reconnectAttempts); // Exponential backoff
                } else {
                    alert('Failed to reconnect. Please refresh the page.');
                    localStorage.removeItem('game_portal_game_id');
                    localStorage.removeItem('game_portal_player_id');
                    localStorage.removeItem('game_portal_connected');
                    localStorage.removeItem('game_portal_disconnect_time');
                }
            };
            
            socket.onerror = (error) => {
                console.error('WebSocket error during reconnect:', error);
            };
        } catch (err) {
            console.error('Reconnection error:', err);
            if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                setTimeout(() => {
                    attemptReconnect();
                }, 2000 * reconnectAttempts);
            }
        }
    }
    
    // --- Auto-Recovery on Page Load ---
    async function checkForAutoRecovery() {
        const savedGameId = localStorage.getItem('game_portal_game_id');
        const savedPlayerId = localStorage.getItem('game_portal_player_id');
        const isConnected = localStorage.getItem('game_portal_connected') === 'true';
        const disconnectTime = localStorage.getItem('game_portal_disconnect_time');
        
        if (savedGameId && savedPlayerId) {
            // Check if disconnect was recent (within 60s)
            if (disconnectTime) {
                const timeSinceDisconnect = Date.now() - parseInt(disconnectTime);
                if (timeSinceDisconnect < DISCONNECT_DELAY_MS) {
                    // Auto-recover
                    console.log('Auto-recovering connection...');
                    localGameId = savedGameId;
                    localPlayerId = savedPlayerId;
                    
                    if (!browserFingerprint) {
                        browserFingerprint = await generateFingerprint();
                    }
                    
                    // Verify player ID matches fingerprint
                    if (savedPlayerId === browserFingerprint) {
                        // Auto-reconnect
                        connectWebSocket(savedGameId, savedPlayerId);
                        return true;
                    }
                }
            } else if (isConnected) {
                // Try to reconnect if we think we're connected
                console.log('Attempting to restore connection...');
                localGameId = savedGameId;
                localPlayerId = savedPlayerId;
                
                if (!browserFingerprint) {
                    browserFingerprint = await generateFingerprint();
                }
                
                if (savedPlayerId === browserFingerprint) {
                    connectWebSocket(savedGameId, savedPlayerId);
                    return true;
                }
            }
        }
        return false;
    }
    
    // --- Initial Event Listeners ---
    createGameBtn.addEventListener('click', createGame);
    joinGameBtn.addEventListener('click', joinGame);
    
    function checkUrlForGame() {
        const urlParams = new URLSearchParams(window.location.search);
        const gameId = urlParams.get('game');
        
        if (gameId) {
            gameIdInput.value = gameId.toUpperCase();
            setTimeout(async () => {
                // First check for auto-recovery
                const recovered = await checkForAutoRecovery();
                if (!recovered) {
                    // If no recovery, proceed with normal join
                    if (browserFingerprint) {
                        joinGame();
                    } else {
                        browserFingerprint = await generateFingerprint();
                        joinGame();
                    }
                }
            }, 500);
        } else {
            // Check for auto-recovery even without URL param
            checkForAutoRecovery();
        }
    }
    
    checkUrlForGame();
    
    startGameBtn.addEventListener('click', sendStartGame);
    hitBtn.addEventListener('click', () => sendMove({ action: 'hit' }));
    standBtn.addEventListener('click', () => sendMove({ action: 'stand' }));
    drawBtn.addEventListener('click', () => sendMove({ action: 'draw' }));
    passBtn.addEventListener('click', () => sendMove({ action: 'pass' }));
    
    const nextHandBtn = document.getElementById('next-hand-btn');
    if (nextHandBtn) {
        nextHandBtn.addEventListener('click', () => {
            sendMove({ action: 'ready_for_next_hand' });
            nextHandBtn.disabled = true;
            nextHandBtn.textContent = '✓ Ready!';
        });
    }
    
    const nextRoundBtn = document.getElementById('next-round-btn');
    if (nextRoundBtn) {
        nextRoundBtn.addEventListener('click', () => {
            const btn = document.getElementById('next-round-btn');
            if (!btn.disabled) {
                sendMove({ action: 'ready_for_next_round' });
                btn.disabled = true;
                btn.textContent = '✓ Ready!';
                btn.style.opacity = '0.7';
                btn.style.cursor = 'not-allowed';
            }
        });
    }
});

