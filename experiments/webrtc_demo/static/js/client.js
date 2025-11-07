// Get DOM elements
const localVideo = document.getElementById('localVideo');
const remoteVideo = document.getElementById('remoteVideo');
const createRoomBtn = document.getElementById('createRoomBtn');
const joinRoomBtn = document.getElementById('joinRoomBtn');
const roomNameInput = document.getElementById('roomName');
const copyRoomBtn = document.getElementById('copyRoomBtn');
const generateRoomBtn = document.getElementById('generateRoomBtn');
const roomStatus = document.getElementById('roomStatus');
const statusIndicator = document.getElementById('statusIndicator');
const connectionInfo = document.getElementById('connectionInfo');
const instructions = document.getElementById('instructions');
const toggleVideoBtn = document.getElementById('toggleVideo');
const toggleAudioBtn = document.getElementById('toggleAudio');
const localNoVideo = document.getElementById('localNoVideo');
const remoteNoVideo = document.getElementById('remoteNoVideo');

// State
let peerConnection;
let localStream;
let currentRoom;
let isVideoEnabled = true;
let isAudioEnabled = true;
let isConnected = false;

// --- 1. WebSocket Setup ---
const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsHost = window.location.host;
const wsPath = window.location.pathname.replace(/\/$/, '') + '/ws';
const socket = new WebSocket(`${wsProtocol}//${wsHost}${wsPath}`);

socket.onopen = () => {
    console.log('WebSocket connection established');
    updateStatus('Connected to server', 'success', 'connected');
    updateConnectionInfo('Online');
};

socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('Received message:', data);

    switch (data.type) {
        case 'room-created':
            updateStatus(`Room "${currentRoom}" created. Waiting for someone to join...`, 'success', 'connecting');
            hideInstructions();
            break;

        case 'room-joined':
            updateStatus(`Joined room "${currentRoom}". Connecting...`, 'success', 'connecting');
            hideInstructions();
            break;

        case 'peer-joined':
            updateStatus(`Someone joined! Connecting...`, 'success', 'connecting');
            createOffer();
            break;

        case 'offer':
            handleOffer(data.offer);
            break;

        case 'answer':
            handleAnswer(data.answer);
            break;

        case 'ice-candidate':
            if (peerConnection) {
                peerConnection.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(err => {
                    console.error('Error adding ICE candidate:', err);
                });
            }
            break;

        case 'peer-left':
            updateStatus(`The other person left the room.`, 'error', 'connected');
            resetConnection();
            showInstructions();
            break;

        case 'error':
            updateStatus(`Error: ${data.message}`, 'error', 'error');
            break;
    }
};

socket.onclose = () => {
    console.log('WebSocket connection closed');
    updateStatus('Disconnected from server. Please refresh the page.', 'error', 'error');
    updateConnectionInfo('Offline');
};

socket.onerror = (error) => {
    console.error('WebSocket error:', error);
    updateStatus('Connection error. Please refresh the page.', 'error', 'error');
};

// Helper to send JSON messages
function sendMessage(message) {
    if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(message));
    } else {
        console.error('WebSocket is not open. Ready state:', socket.readyState);
        updateStatus('Connection lost. Please refresh the page.', 'error', 'error');
    }
}

// Helper to update status message
function updateStatus(message, type = '', connectionState = '') {
    roomStatus.textContent = message;
    roomStatus.className = `status-text ${type}`;
    
    // Update status indicator
    statusIndicator.className = 'status-indicator';
    if (connectionState === 'connected') {
        statusIndicator.classList.add('connected');
    } else if (connectionState === 'connecting') {
        statusIndicator.classList.add('connecting');
    } else if (connectionState === 'error') {
        statusIndicator.classList.add('error');
    }
}

function updateConnectionInfo(info) {
    connectionInfo.textContent = info;
}

function hideInstructions() {
    instructions.style.display = 'none';
}

function showInstructions() {
    instructions.style.display = 'block';
}

// --- 2. WebRTC Setup ---
const servers = {
    iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' }
    ]
};

// Function to start the local video stream
async function startLocalStream() {
    try {
        localStream = await navigator.mediaDevices.getUserMedia({ 
            video: { width: 1280, height: 720 },
            audio: true 
        });
        localVideo.srcObject = localStream;
        localNoVideo.style.display = 'none';
        updateStatus('Camera and microphone ready', 'success', 'connected');
    } catch (error) {
        console.error('Error accessing media devices:', error);
        updateStatus('Please allow camera and microphone access to use video chat.', 'error', 'error');
        localNoVideo.style.display = 'flex';
    }
}

// Function to create the PeerConnection
function createPeerConnection() {
    peerConnection = new RTCPeerConnection(servers);

    // Add local stream tracks to the connection
    if (localStream) {
        localStream.getTracks().forEach(track => {
            peerConnection.addTrack(track, localStream);
        });
    }

    // Handle incoming remote stream
    peerConnection.ontrack = (event) => {
        console.log('Received remote stream');
        remoteVideo.srcObject = event.streams[0];
        remoteNoVideo.style.display = 'none';
        isConnected = true;
        updateStatus('Connected! You can now see and hear each other.', 'success', 'connected');
        updateConnectionInfo('In call');
    };

    // Handle ICE candidates
    peerConnection.onicecandidate = (event) => {
        if (event.candidate) {
            sendMessage({
                type: 'ice-candidate',
                roomName: currentRoom,
                candidate: event.candidate
            });
        }
    };

    // Handle connection state changes
    peerConnection.onconnectionstatechange = () => {
        console.log('Connection state:', peerConnection.connectionState);
        const state = peerConnection.connectionState;
        
        if (state === 'connected') {
            updateStatus('Connected! You can now see and hear each other.', 'success', 'connected');
            updateConnectionInfo('In call');
            isConnected = true;
        } else if (state === 'connecting') {
            updateStatus('Connecting...', '', 'connecting');
            updateConnectionInfo('Connecting');
        } else if (state === 'disconnected' || state === 'failed') {
            updateStatus('Connection lost. Trying to reconnect...', 'error', 'connecting');
            updateConnectionInfo('Reconnecting');
            isConnected = false;
        } else if (state === 'closed') {
            updateStatus('Call ended.', '', 'connected');
            updateConnectionInfo('Ready');
            isConnected = false;
        }
    };

    // Handle ICE connection state
    peerConnection.oniceconnectionstatechange = () => {
        const iceState = peerConnection.iceConnectionState;
        if (iceState === 'failed' || iceState === 'disconnected') {
            console.log('ICE connection state:', iceState);
        }
    };
}

// --- 3. WebRTC Call Flow ---

// (Creator) 1. Create Offer
async function createOffer() {
    try {
        createPeerConnection();
        const offer = await peerConnection.createOffer();
        await peerConnection.setLocalDescription(offer);
        
        sendMessage({
            type: 'offer',
            roomName: currentRoom,
            offer: offer
        });
        updateStatus('Connecting...', '', 'connecting');
    } catch (error) {
        console.error('Error creating offer:', error);
        updateStatus('Error starting connection. Please try again.', 'error', 'error');
    }
}

// (Joiner) 2. Handle Offer and Create Answer
async function handleOffer(offer) {
    try {
        createPeerConnection();
        await peerConnection.setRemoteDescription(new RTCSessionDescription(offer));
        
        const answer = await peerConnection.createAnswer();
        await peerConnection.setLocalDescription(answer);
        
        sendMessage({
            type: 'answer',
            roomName: currentRoom,
            answer: answer
        });
        updateStatus('Connecting...', '', 'connecting');
    } catch (error) {
        console.error('Error handling offer:', error);
        updateStatus('Error connecting. Please try again.', 'error', 'error');
    }
}

// (Creator) 3. Handle Answer
async function handleAnswer(answer) {
    try {
        await peerConnection.setRemoteDescription(new RTCSessionDescription(answer));
        updateStatus('Connecting...', '', 'connecting');
    } catch (error) {
        console.error('Error handling answer:', error);
        updateStatus('Error completing connection. Please try again.', 'error', 'error');
    }
}

// Function to reset the connection
function resetConnection() {
    if (peerConnection) {
        peerConnection.close();
        peerConnection = null;
    }
    remoteVideo.srcObject = null;
    remoteNoVideo.style.display = 'flex';
    isConnected = false;
    createRoomBtn.disabled = false;
    joinRoomBtn.disabled = false;
    roomNameInput.disabled = false;
}

// --- 4. Media Controls ---

toggleVideoBtn.onclick = () => {
    if (localStream) {
        const videoTrack = localStream.getVideoTracks()[0];
        if (videoTrack) {
            isVideoEnabled = !videoTrack.enabled;
            videoTrack.enabled = isVideoEnabled;
            
            toggleVideoBtn.classList.toggle('active', isVideoEnabled);
            localNoVideo.style.display = isVideoEnabled ? 'none' : 'flex';
        }
    }
};

toggleAudioBtn.onclick = () => {
    if (localStream) {
        const audioTrack = localStream.getAudioTracks()[0];
        if (audioTrack) {
            isAudioEnabled = !audioTrack.enabled;
            audioTrack.enabled = isAudioEnabled;
            
            toggleAudioBtn.classList.toggle('muted', !isAudioEnabled);
            toggleAudioBtn.classList.toggle('active', isAudioEnabled);
        }
    }
};

// --- 5. Button Event Listeners ---

createRoomBtn.onclick = () => {
    currentRoom = roomNameInput.value.trim();
    if (currentRoom) {
        sendMessage({
            type: 'create-room',
            roomName: currentRoom
        });
        createRoomBtn.disabled = true;
        joinRoomBtn.disabled = true;
        roomNameInput.disabled = true;
    } else {
        updateStatus('Please enter a room name.', 'error', 'error');
    }
};

joinRoomBtn.onclick = () => {
    currentRoom = roomNameInput.value.trim();
    if (currentRoom) {
        sendMessage({
            type: 'join-room',
            roomName: currentRoom
        });
        createRoomBtn.disabled = true;
        joinRoomBtn.disabled = true;
        roomNameInput.disabled = true;
    } else {
        updateStatus('Please enter a room name.', 'error', 'error');
    }
};

// Allow Enter key to create/join room
roomNameInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        if (createRoomBtn.disabled === false) {
            createRoomBtn.click();
        } else if (joinRoomBtn.disabled === false) {
            joinRoomBtn.click();
        }
    }
});

// --- 6. Room Name Generation ---

// Simple, friendly room name generator
const adjectives = [
    'happy', 'sunny', 'cozy', 'bright', 'calm', 'peaceful', 'warm', 'cool',
    'swift', 'gentle', 'brave', 'wise', 'kind', 'jolly', 'merry', 'lucky',
    'magic', 'stellar', 'cosmic', 'ocean', 'forest', 'mountain', 'river', 'valley'
];

const nouns = [
    'dolphin', 'eagle', 'tiger', 'panda', 'bear', 'wolf', 'fox', 'deer',
    'star', 'moon', 'sun', 'cloud', 'rainbow', 'ocean', 'river', 'lake',
    'mountain', 'valley', 'forest', 'garden', 'castle', 'tower', 'bridge', 'island'
];

function generateRoomName() {
    const adj = adjectives[Math.floor(Math.random() * adjectives.length)];
    const noun = nouns[Math.floor(Math.random() * nouns.length)];
    const num = Math.floor(Math.random() * 999) + 1;
    return `${adj}-${noun}-${num}`;
}

// Generate and set initial room name
function setGeneratedRoomName() {
    const roomName = generateRoomName();
    roomNameInput.value = roomName;
    roomNameInput.focus();
    roomNameInput.select();
}

// Copy room name to clipboard
copyRoomBtn.onclick = async () => {
    const roomName = roomNameInput.value.trim();
    if (!roomName) {
        updateStatus('Please enter or generate a room name first.', 'error', 'error');
        return;
    }
    
    try {
        await navigator.clipboard.writeText(roomName);
        copyRoomBtn.classList.add('copied');
        const originalTitle = copyRoomBtn.title;
        copyRoomBtn.title = 'Copied!';
        
        setTimeout(() => {
            copyRoomBtn.classList.remove('copied');
            copyRoomBtn.title = originalTitle;
        }, 2000);
        
        updateStatus(`Room name "${roomName}" copied to clipboard! Share it with others.`, 'success', 'connected');
    } catch (err) {
        console.error('Failed to copy:', err);
        // Fallback: select the text
        roomNameInput.select();
        roomNameInput.setSelectionRange(0, 99999);
        updateStatus('Room name selected. Press Ctrl+C (or Cmd+C) to copy.', 'success', 'connected');
    }
};

// Generate new room name
generateRoomBtn.onclick = () => {
    setGeneratedRoomName();
    updateStatus('New room name generated! Click "Create Room" to start.', 'success', 'connected');
};

// Start local video on page load
window.onload = () => {
    startLocalStream();
    // Auto-generate and set a room name
    setGeneratedRoomName();
    updateStatus('Room name generated! Click "Create Room" to start, or generate a new one.', 'success', 'connected');
};
