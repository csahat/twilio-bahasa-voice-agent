const micButton = document.getElementById('mic-button');
const statusDiv = document.getElementById('status');
const chatContainer = document.getElementById('chat-container');
const pdfUpload = document.getElementById('pdf-upload');
const uploadBtn = document.getElementById('upload-btn');
const uploadStatus = document.getElementById('upload-status');
const indexStats = document.getElementById('index-stats');

// State Aplikasi
const STATE = { IDLE: 'IDLE', RECORDING: 'RECORDING', PROCESSING: 'PROCESSING', SPEAKING: 'SPEAKING' };
let currentState = STATE.IDLE;

let socket;
let mediaRecorder;
let currentAudio;

function connectWebSocket() {
    if (socket && socket.readyState === WebSocket.OPEN) return;
    
    // Auto-detect WebSocket URL based on environment
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const ws_url = `${protocol}//${host}/ws`;
    // const ws_url = `wss://143.198.220.75/ws`;
    console.log("Connecting to WebSocket:", ws_url);
    socket = new WebSocket(ws_url);

    socket.onopen = () => {
        console.log("WebSocket connected successfully");
        setState(STATE.IDLE);
        statusDiv.textContent = 'Koneksi berhasil. Klik mikrofon untuk memulai.';
    };
    
    socket.onclose = (event) => {
        console.log("WebSocket disconnected:", event.code, event.reason);
        setState(STATE.IDLE);
        statusDiv.textContent = 'Koneksi terputus. Mencoba reconnect...';
        micButton.disabled = true;
        
        // Auto-reconnect setelah 3 detik
        setTimeout(() => {
            console.log("Attempting to reconnect...");
            connectWebSocket();
        }, 3000);
    };
    
    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
        statusDiv.textContent = 'Error koneksi. Periksa server.';
    };

    socket.onmessage = async (event) => {
        if (event.data instanceof Blob) {
            playAudioStream(event.data);
        } else {
            const message = JSON.parse(event.data);
            if (message.type === 'user_transcript') {
                addMessageToChat(message.text, 'user');
            } else if (message.type === 'bot_response') {
                addMessageToChat(message.text, 'bot');
            }
        }
    };
}

function setState(newState) {
    currentState = newState;
    switch (newState) {
        case STATE.IDLE:
            micButton.disabled = false;
            micButton.className = 'btn btn-primary rounded-circle';
            micButton.innerHTML = '<i class="bi bi-mic-fill"></i>';
            statusDiv.textContent = 'Klik ikon mikrofon untuk memulai';
            break;
        case STATE.RECORDING:
            micButton.disabled = false;
            micButton.className = 'btn btn-danger rounded-circle recording';
            micButton.innerHTML = '<i class="bi bi-stop-fill"></i>';
            statusDiv.textContent = 'Mendengarkan...';
            break;
        case STATE.PROCESSING:
            micButton.disabled = true;
            micButton.className = 'btn btn-warning rounded-circle';
            micButton.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"></div>';
            statusDiv.textContent = 'AI sedang berpikir...';
            break;
        case STATE.SPEAKING:
            micButton.disabled = false;
            micButton.className = 'btn btn-danger rounded-circle';
            micButton.innerHTML = '<i class="bi bi-stop-fill"></i>';
            statusDiv.textContent = 'AI sedang berbicara... (klik untuk berhenti)';
            break;
    }
}

function addMessageToChat(text, sender) {
    if (!text) return;
    const messageDiv = document.createElement('div');
    messageDiv.classList.add('message', sender === 'user' ? 'user-message' : 'bot-message');
    messageDiv.textContent = text;
    chatContainer.appendChild(messageDiv);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

const audioQueue = [];
let isPlaying = false;

function playAudioStream(audioBlob) {
    audioQueue.push(audioBlob);
    if (!isPlaying) playNextInQueue();
}

function playNextInQueue() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        if (currentState === STATE.SPEAKING) setState(STATE.IDLE);
        return;
    }
    isPlaying = true;
    setState(STATE.SPEAKING);
    const blob = audioQueue.shift();
    const audioUrl = URL.createObjectURL(blob);
    currentAudio = new Audio(audioUrl);
    currentAudio.play();
    currentAudio.onended = () => {
        URL.revokeObjectURL(audioUrl);
        playNextInQueue();
    };
}

function stopAISpeech() {
    if (currentAudio) currentAudio.pause();
    audioQueue.length = 0;
    isPlaying = false;
    setState(STATE.IDLE);
}

async function startRecording() {
    setState(STATE.RECORDING);
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    
    mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0 && socket && socket.readyState === WebSocket.OPEN) {
            socket.send(event.data);
        }
    };
    
    // === PERBAIKAN UTAMA ADA DI SINI ===
    mediaRecorder.onstop = () => {
        // Hentikan track mikrofon untuk melepaskannya
        stream.getTracks().forEach(track => track.stop());
        // Kirim sinyal END_OF_STREAM HANYA SETELAH recorder benar-benar berhenti
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send("END_OF_STREAM");
        }
        console.log("MediaRecorder stopped and END_OF_STREAM sent.");
    };
    // ===================================

    mediaRecorder.start(250);
}

function stopRecording() {
    // Fungsi ini sekarang hanya bertugas memicu proses penghentian
    if (mediaRecorder) {
        mediaRecorder.stop(); // Ini akan memicu onstop handler di atas
    }
    setState(STATE.PROCESSING);
}

micButton.addEventListener('click', () => {
    switch (currentState) {
        case STATE.IDLE:
            if (!socket || socket.readyState === WebSocket.CLOSED) {
                connectWebSocket();
            }
            startRecording();
            break;
        case STATE.RECORDING:
            stopRecording();
            break;
        case STATE.SPEAKING:
            stopAISpeech();
            break;
        case STATE.PROCESSING:
            // Jangan lakukan apa-apa
            break;
    }
});

// Fungsi untuk upload PDF
async function uploadPDF() {
    const file = pdfUpload.files[0];
    if (!file) {
        showUploadStatus('Pilih file PDF terlebih dahulu', 'error');
        return;
    }

    if (!file.name.toLowerCase().endsWith('.pdf')) {
        showUploadStatus('File harus berupa PDF', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    uploadBtn.disabled = true;
    uploadBtn.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"></div> Uploading...';
    showUploadStatus('Mengupload dan memproses PDF...', 'info');

    try {
        const response = await fetch('/upload-pdf', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (response.ok) {
            showUploadStatus(
                `✅ ${result.message}<br>
                📄 File: ${result.filename}<br>
                📖 Halaman diproses: ${result.pages_processed}<br>
                🔗 Chunks dibuat: ${result.chunks_created}`,
                'success'
            );
            // Refresh index stats
            loadIndexStats();
        } else {
            showUploadStatus(`❌ Error: ${result.detail}`, 'error');
        }
    } catch (error) {
        showUploadStatus(`❌ Error: ${error.message}`, 'error');
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.innerHTML = '<i class="bi bi-upload"></i> Upload & Index';
    }
}

// Fungsi untuk menampilkan status upload
function showUploadStatus(message, type) {
    uploadStatus.innerHTML = message;
    uploadStatus.className = `mt-2 alert alert-${type === 'error' ? 'danger' : type === 'success' ? 'success' : 'info'}`;
}

// Fungsi untuk load index stats
async function loadIndexStats() {
    try {
        const response = await fetch('/index-stats');
        const stats = await response.json();
        
        if (response.ok) {
            let documentsList = '';
            if (stats.documents && stats.documents.length > 0) {
                documentsList = `<br>📚 Dokumen: ${stats.documents.join(', ')}`;
            } else {
                documentsList = '<br>📚 Belum ada dokumen';
            }
            
            indexStats.innerHTML = `
                <small class="text-muted">
                    📊 Total Vectors: ${stats.total_vectors} | 
                    📐 Dimension: ${stats.dimension} | 
                    📈 Index Fullness: ${(stats.index_fullness * 100).toFixed(2)}%
                    ${documentsList}
                    <br>🗂️ Index: ${stats.index_name || 'N/A'}
                </small>
            `;
        } else {
            indexStats.innerHTML = '<small class="text-danger">Error loading stats</small>';
        }
    } catch (error) {
        indexStats.innerHTML = '<small class="text-danger">Error loading stats</small>';
    }
}

// Event listeners untuk upload
pdfUpload.addEventListener('change', function() {
    uploadBtn.disabled = !this.files[0];
});

uploadBtn.addEventListener('click', uploadPDF);

// Load index stats saat halaman dimuat
loadIndexStats();

connectWebSocket();